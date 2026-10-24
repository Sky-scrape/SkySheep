"""运行账本：把每次自动化运行（定时任务 / 编排节点）记成 ``automation_runs`` 一行。

工厂（跨项目自动化观战）需要「运行」这一实体——既有的 cron_tasks 只保存每个
任务的最后一次结果，没有历史、没有耗时。本模块是表的主要写入口，职责只有两件：

1. 开 / 收一条运行（``start`` / ``finish`` / ``skipped``）；
2. 在开、收两个时刻各发一次 ``factory_run`` 事件（``started`` / ``finished``）。

事件契约（设计稿 §8.2）：``{"kind": "factory_run", "phase": ..., "run": {...}}``，
顶层不带 ``session_id``——前端事件路由会把任何带顶层 session_id 的事件当成
聊天事件自动开标签页，会话 id 只能嵌在 ``run`` 里。``run`` 自带 ``name`` 与
``project_name``，前端拿到就能画，不必二次查询。

构造不需要事件循环：``emit`` 是同步回调，内部用 ``bgtasks.spawn_bg`` 之类
把协程排出去由调用方决定。
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("skysheep.run_ledger")

# 运行状态取值（与设计稿 §8.1 一致）：running / done / empty / error /
# interrupted / skipped / orphan / cancelled。本模块只写其中六种，orphan 由
# 工厂页按「所属厂区不可用」的展示口径识别，不落库。
RUN_STATUSES = (
    "running", "done", "empty", "error", "interrupted", "skipped", "orphan", "cancelled",
)

# 「做成」判定：与 status（跑完了没有）刻意分成两个维度。
#   ok      —— 这轮不仅跑完，而且没有出现「被门控拦下」的迹象；
#   partial —— 跑完了，但这轮里至少有一次工具被无人值守门控拒绝（最常见的
#              真实故障：预授权工具没勾全），产出可能只是模型的绕行说明；
#   failed  —— 没跑成（异常 / 空产出）。
# 空串表示还没有结论（还在跑 / 到点跳过 / 用户叫停）。
RUN_OUTCOMES = ("ok", "partial", "failed")

# 失败归因的分类标签（抽屉与「需留意」里各显示一枚，用户一眼知道该去勾工具
# 还是去修目录）。顺序敏感：先命中先归类。
_ERROR_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "timed out", "超时")),
    ("permission", ("denied", "deny", "permission", "权限", "被拒", "拒绝",
                    "not allowed", "unauthorized", "forbidden")),
    ("budget", ("步数", "max_iterations", "预算", "额度", "budget", "quota",
                "context length", "token")),
    ("path", ("no such file", "not a directory", "filenotfound", "is a directory",
              "file exists", "read-only file system", "目录", "路径", "不存在")),
    ("model", ("api", "模型", "provider", "network", "连接", "stream", "rate limit",
               "429", "500", "502", "503", "ssl", "connection", "http")),
)


def classify_run_error(text: str) -> str:
    """把一段错误摘要归一化成固定几类，认不出来返回 ``unknown``。

    只按关键词做粗分类（不解析栈、不识别具体 provider）：目的是让抽屉里能
    显示「权限被拒」而不是「400 Bad Request」，别把用户丢进日志里。
    """
    low = str(text or "").lower()
    if not low:
        return "unknown"
    for kind, needles in _ERROR_KIND_RULES:
        if any(n in low for n in needles):
            return kind
    return "unknown"


def outcome_for(status: str, *, denied_count: int = 0) -> str:
    """按终态与「被拦次数」推出「做成」判定（'' = 没有结论）。"""
    if status == "done":
        return "partial" if int(denied_count or 0) > 0 else "ok"
    if status in ("empty", "error"):
        return "failed"
    return ""


def denied_tools_json(facts: dict | None) -> str:
    """把本轮「被拦工具名 → 次数」压成一段有界的 JSON 快照。

    只留次数最多的前 8 项、名字截到 60 字符：抽屉要的是「该补哪个工具」，
    不是完整审计日志；表里也不该出现能无限增长的自由文本。
    """
    tools = (facts or {}).get("denied_tools")
    if not isinstance(tools, dict):
        return ""
    items: list[tuple[str, int]] = []
    for name, count in tools.items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        if n > 0:
            items.append((str(name)[:60], n))
    if not items:
        return ""
    items.sort(key=lambda pair: (-pair[1], pair[0]))
    return json.dumps(dict(items[:8]), ensure_ascii=False)


class RunLedger:
    """``automation_runs`` 的写入口（实例由 ServerBackend 持有）。"""

    #: 每多少次 start 清理一次历史（设计稿 §8.1：启动与每 100 次插入各清一次）
    PRUNE_EVERY = 100

    #: 工序进度的最小间隔（秒）。无头运行每秒可能产生几十个工具事件，
    #: 逐条推给前端等于把观战窗变成事件洪流——设计稿 §10 定的 1 次/秒。
    MIN_PROGRESS_INTERVAL = 1.0

    def __init__(self, store, emit=None, keep: int = 2000, days: int = 30) -> None:
        self.store = store
        self.emit = emit
        self.keep = max(1, int(keep))
        self.days = max(1, int(days))
        self._starts = 0
        # (kind, task_id) → {"id": run_id, "pipeline_id": pid}：工序进度只知道
        # 「哪个任务在跑」，运行 id 由这里补——execute() 的签名因此不用改，
        # CLI 与后端共用同一条执行路径。
        self._active: dict[tuple[str, int], dict] = {}
        # run_id → 上次推进度的时间（monotonic），节流用
        self._last_progress: dict[int, float] = {}

    # ---- 事件出口 ----

    def _emit(self, phase: str, run: dict) -> None:
        """发一条 factory_run；emit 抛错绝不影响运行本身。"""
        if self.emit is None:
            return
        try:
            self.emit({"kind": "factory_run", "phase": phase, "run": run})
        except Exception as e:  # noqa: BLE001 - 观测事件不能反噬业务
            logger.debug("factory_run 事件发送失败（忽略）：%s", e)

    # ---- 名称与项目名（前端不再二次查询） ----

    async def _project_name(self, project_id) -> str:
        if project_id is None:
            return ""
        try:
            proj = await self.store.get_project(int(project_id))
        except Exception:  # noqa: BLE001 - 名称缺失不该拦住运行记录
            return ""
        return str(getattr(proj, "name", "") or "") if proj is not None else ""

    async def _decorate(self, run: dict | None) -> dict | None:
        """给运行行补 name / project_name（run 里已有则不再查库）。"""
        if run is None:
            return None
        out = dict(run)
        if "name" not in out or "project_name" not in out:
            name = ""
            if out.get("kind") == "pipeline_node":
                node = await self.store.get_pipeline_node(int(out["task_id"]))
                if node is not None:
                    name = str(node.get("title") or "")
                    if out.get("pipeline_id") is None:
                        out["pipeline_id"] = node.get("pipeline_id")
            else:
                task = await self.store.get_cron_task(int(out["task_id"]))
                if task is not None:
                    name = str(task.get("name") or "")
            out.setdefault("name", name)
            out.setdefault("project_name", await self._project_name(out.get("project_id")))
        return out

    # ---- 生命周期 ----

    async def start(
        self,
        kind: str,
        task_id: int,
        *,
        project_id: int | None = None,
        pipeline_id: int | None = None,
        trigger: str = "schedule",
    ) -> dict:
        """开一条运行（status=running）并发 ``started`` 事件。"""
        kind = str(kind)
        name = ""
        if kind == "pipeline_node":
            node = await self.store.get_pipeline_node(int(task_id))
            if node is not None:
                name = str(node.get("title") or "")
                if pipeline_id is None:
                    pipeline_id = node.get("pipeline_id")
                if project_id is None:
                    node_pipe_id = node.get("pipeline_id")
                    if node_pipe_id is not None:
                        pipe = await self.store.get_pipeline(int(node_pipe_id))
                        if pipe is not None:
                            project_id = pipe.get("project_id")
        else:
            task = await self.store.get_cron_task(int(task_id))
            if task is not None:
                name = str(task.get("name") or "")
                if project_id is None:
                    project_id = task.get("project_id")
        row = await self.store.add_automation_run(
            kind=kind, task_id=int(task_id), pipeline_id=pipeline_id,
            project_id=project_id, trigger=str(trigger), status="running",
        )
        run = {**row, "name": name,
               "project_name": await self._project_name(project_id)}
        self._active[(kind, int(task_id))] = {
            "id": int(run["id"]), "pipeline_id": pipeline_id,
        }
        self._starts += 1
        self._emit("started", run)
        if self._starts % self.PRUNE_EVERY == 0:
            await self.maybe_prune()
        return run

    def progress(self, kind: str, task_id: int, *, tool: str = "",
                 steps: int | None = None, denied: int | None = None) -> bool:
        """推一条工序进度（``factory_run`` / phase=progress），返回是否真的发了。

        只带运行 id 与「此刻在干什么」，不带任何会话 id——顶层带 session_id
        的事件会被前端当成聊天事件自动开标签页（设计稿 §8.2 的坑）。
        节流在账本里做：调用方（无头运行的 emit 钩子）可以逐事件调用。

        ``denied``：本轮累计被门控拒绝的工具次数。它是「预授权没勾全」的
        唯一实时痕迹（无人值守下拒绝事件不进前端），有值时才带上。
        """
        if self.emit is None:
            return False
        active = self._active.get((str(kind), int(task_id)))
        if active is None:
            return False
        rid = int(active["id"])
        now = time.monotonic()
        if now - self._last_progress.get(rid, 0.0) < self.MIN_PROGRESS_INTERVAL:
            return False
        self._last_progress[rid] = now
        payload = {"id": rid, "kind": str(kind), "task_id": int(task_id),
                   "pipeline_id": active.get("pipeline_id"),
                   "progress": {"tool": str(tool or ""), "at": time.time()}}
        if steps is not None:
            payload["progress"]["steps"] = max(0, int(steps))
        if denied:
            payload["progress"]["denied"] = max(0, int(denied))
        self._emit("progress", payload)
        return True

    async def finish(
        self,
        run_id: int,
        *,
        status: str,
        result_excerpt: str = "",
        error: str = "",
        session_id: str | None = None,
        denied_count: int = 0,
        denied_tools: str = "",
        error_kind: str = "",
        outcome: str = "",
        outcome_source: str = "",
    ) -> dict | None:
        """收尾一条运行并发 ``finished`` 事件；行不存在返回 None。

        收尾时按「会话 + 本次运行的时间窗」回填 token 用量：定时任务每次运行
        新建会话，会话续跑型编排节点则复用会话，时间窗把范围收回到本次。
        用量查不到（无会话 / usage_log 已清）就写 0，不影响收尾。
        """
        row = await self.store.get_automation_run(int(run_id))
        sid = session_id if session_id is not None else (row or {}).get("session_id")
        tokens = {"in_tokens": 0, "out_tokens": 0, "cached_tokens": 0}
        if row is not None and sid:
            try:
                tokens = await self.store.session_usage_between(
                    str(sid), float(row.get("started_at") or 0))
            except Exception as e:  # noqa: BLE001 - 用量是附加信息，查不到就记 0
                logger.debug("运行用量汇总失败（忽略）：%s", e)
        self._active.pop(self._active_key(run_id), None)
        self._last_progress.pop(int(run_id), None)
        row = await self.store.finish_automation_run(
            int(run_id), status=str(status), result_excerpt=result_excerpt,
            error=error, session_id=session_id, **tokens,
            denied_count=int(denied_count or 0), denied_tools=str(denied_tools or ""),
            error_kind=str(error_kind or ""),
            outcome=str(outcome or ""), outcome_source=str(outcome_source or ""),
        )
        run = await self._decorate(row)
        if run is not None:
            self._emit("finished", run)
        return run

    def _active_key(self, run_id: int) -> tuple[str, int]:
        """按运行 id 反查登记键（没有登记时返回一个必然不存在的键）。"""
        for key, info in self._active.items():
            if int(info.get("id") or 0) == int(run_id):
                return key
        return ("", -1)

    async def skipped(self, task: dict, *, project_id: int | None = None) -> dict:
        """直接写一条已结束的 ``skipped`` 行（到点时上一轮仍在跑）。

        同一「到点批次」只记一次由调用方按 (task_id, next_run_at) 去重——
        否则长跑期间每 20 秒的扫描都会堆一行。
        """
        tid = int(task.get("id") or 0)
        pid = project_id if project_id is not None else task.get("project_id")
        now = time.time()
        row = await self.store.add_automation_run(
            kind="cron", task_id=tid, project_id=pid,
            trigger="schedule", status="skipped", started_at=now,
        )
        row = await self.store.finish_automation_run(
            row["id"], status="skipped", finished_at=now, duration_s=0.0,
            error="上一轮仍在运行，本次到点跳过",
        )
        run = {
            **row,
            "name": str(task.get("name") or ""),
            "project_name": await self._project_name(pid),
        }
        self._emit("finished", run)
        return run

    async def mark_interrupted(self) -> int:
        """启动收尾：残留的 running 统一标为 interrupted，返回条数。"""
        return await self.store.mark_interrupted_runs()

    async def maybe_prune(self) -> int:
        """按当前保留策略清理历史，返回删除条数；失败只记日志（不拦业务）。"""
        try:
            return await self.store.prune_automation_runs(
                keep=self.keep, days=self.days)
        except Exception as e:  # noqa: BLE001 - 清理是家务，失败不该影响运行
            logger.warning("运行账本清理失败（忽略）：%s", e)
            return 0
