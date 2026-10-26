"""工厂（跨项目自动化观战）：第三顶层视图的数据面与派工入口。

与「自动化」右面板分工：那边管当前项目的配置与任务行（cron.* / pipeline.*），
这里第一次把**全部项目**的定时任务与编排摆在一张桌上——观战、计数、派工，
不新增执行模型，也不新增调度语义：

- 扫描范围不变：``due_cron_tasks()`` 与 ``list_pipelines()`` 本来就是全项目口径；
- 执行上下文不变：每个任务仍按自己所属项目的工作目录 / 白名单 / 权限门运行；
- 派工复用 ``_run_cron_task(task_id, force=True)``，但改成后台起跑（请求立刻
  回执，句柄留在 ``_cron_running`` 里供「叫停」）；同一工位在跑时返回
  started=false；
- 叫停只取消本轮（``factory.cancel``），落 ``cancelled`` 终态并重排下次运行，
  不动启用状态、不放松权限；
- 暂停 / 启用等价于改 ``cron_tasks.enabled``（与 cron.update 的 enabled 分支一致）；
- 新建轻量产线写的是普通 ``cron_tasks`` 行，之后完全由现有循环接管。

观战口径：``factory.state`` 一次给齐产线、在途运行、计数、需留意与**产线体检**
（近 7 天成功率 / 平均与 P95 耗时 / 连续失败 / 最近 20 次状态），运行行自带
本次 token 用量、被门控拒绝的次数（``denied_count``）、失败归因（``error_kind``）
与「做成」判定（``outcome`` + 来源）；「需留意」可以按产线标记已处理
（``factory.ack``），更晚的失败会重新浮上来。``factory.run_mark`` 允许用户
人工推翻某一轮运行的「做成」判定（自动判定回答不了「产出对不对」）。
``state.live`` 是在跑的对话轮与子代理的只读快照（core/runs.py 的 RunCenter），
工厂因此覆盖四类 Agent 活动：定时任务、编排节点、对话轮、子代理。

口径原则：**观战窗的结论必须与执行口径一致**。所以厂区目录不存在时，扫描循环
不派工、记一条 error，与这里的「停工」牌是同一个结论；「排队」按真实并发闸算，
不是只数编排里 ready 的节点。

全部方法在 WS 层 local_only=True（跨项目枚举面不对远程开放，见 server/app.py）。
运行账本（automation_runs）的读写细节在 ``core/run_ledger.py`` 与
``session/store.py``，这里只做聚合与展示口径。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from ...bgtasks import is_bg_task, spawn_bg
from ...config import ConfigError, load_config, set_factory_in_config
from ...core import system_schedule

# 工厂页「在途 / 最近」列表最多给多少条（设计稿 §8.3：最多 60 条）
FACTORY_RUN_LIST_LIMIT = 60
# 「需留意」最多给多少条（前端那栏本来就只展示前若干条）
FACTORY_ATTENTION_LIMIT = 50
# 近 24 小时的失败 / 中断 / 跳过运行进入「需留意」
FACTORY_ATTENTION_WINDOW_S = 24 * 3600
# 产线体检的统计窗口（成功率 / 耗时 / 连续失败）
FACTORY_HEALTH_WINDOW_S = 7 * 86400
# 体检里「最近 N 次」状态色条的格数
FACTORY_HEALTH_TAIL = 20
# 「排队中」最多给前端多少条明细（场景最多画 3 枚，多给的用于标签计数）
FACTORY_WAITING_LIMIT = 20

_CRON_LAST_STATUSES = ("ok", "error", "empty", "cancelled")
_PIPELINE_LINE_STATUSES = ("running", "done", "failed", "draft")
_RUN_ATTENTION_TEXT = {
    "error": "上次运行失败",
    "interrupted": "上次运行被中断",
    "empty": "上次运行无产出",
    "skipped": "上一轮未结束，本次跳过",
}
# 「需留意」同一产线只留一条：等级高的覆盖等级低的（孤儿 / 停工结构性问题
# 最优先，其次真失败，partial 只是警示）。这样最新的 partial 不会盖住同一
# 产线更早但更严重的 error。
_ATTENTION_RANK = {
    "orphan": 6, "stopped": 6,
    "error": 5, "interrupted": 4, "empty": 4,
    "partial": 3,
    "skipped": 1,
}
# 体检口径：这些状态不算「一次尝试」，不计入成功率分母与耗时样本
_NON_ATTEMPT_STATUSES = ("running", "skipped", "cancelled")


def factory_line_key(kind: str, line_id) -> str:
    """产线键的唯一拼接入口：定时任务 ``cron:<id>``、编排 ``pipe:<id>``。"""
    prefix = "pipe" if kind in ("pipeline", "pipeline_node") else "cron"
    return f"{prefix}:{int(line_id)}"


def parse_factory_line_key(line_key: str) -> tuple[str | None, int | None]:
    """产线键的唯一解析入口；认不出（空前缀 / 未知类型 / 非数字 id）返回
    ``(None, None)``，调用方按「无名称可补」继续，不抛错。"""
    kind, sep, raw = str(line_key or "").partition(":")
    if not sep or kind not in ("cron", "pipe"):
        return None, None
    try:
        return ("pipeline" if kind == "pipe" else "cron"), int(raw)
    except (TypeError, ValueError):
        return None, None


def factory_run_line_key(run: dict) -> str | None:
    """运行行归属的产线键（与 state 里的 line.key 同一口径）。"""
    if run.get("kind") == "cron":
        return factory_line_key("cron", run["task_id"])
    if run.get("pipeline_id") is not None:
        return factory_line_key("pipeline", run["pipeline_id"])
    return None


def _p95(values: list[float]) -> float:
    """最近邻分位的 P95（样本少时退化为最大值，不做插值假装精度）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return round(float(ordered[idx]), 1)


def factory_line_health(rows: list[dict]) -> dict[str, dict]:
    """按产线聚合体检指标：成功率、平均 / P95 / 最慢耗时、连续失败、最近 N 次。

    ``rows`` 是 ``automation_runs_since`` 的结果（已按开始时间倒序）。调用方
    不需要逐线查库：一次扫描把 7 天样本摊到各条产线上，厂区多了也不退化。

    口径：
    - 成功率的分母只算「真尝试」（排除 running / skipped / cancelled），
      用户叫停不算一次失败，也不该拉低成功率；
    - 分子按 ``outcome`` 算（有 outcome 就用它，旧行 / 无结论回落 status == done）：
      被门控拦过的 done 记 partial，**不计入成功**——否则「模型绕开写总结」会被算成
      做成，成功率永远是绿的；
    - 耗时样本同样只取真尝试，失败的尝试有耗时也计入（一次运行花多久是
      客观事实，不因为结果失败就抹掉）；
    - 连续失败沿「新 → 旧」数，跳过 running / skipped / cancelled（它们不是
      一次判断）；``error / interrupted / empty`` 以及被人工判为 ``failed`` 的运行
      都算一次失败——``empty`` 在 outcome 口径里就是「没做成」，不能只在成功率
      里算失败、连续失败却在它这里清零；
    - ``marked_failed`` 只统计用户亲手判负的运行（``outcome_source = user``）：
      自动判负的空产出不该被显示成「判定不算」。
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        key = factory_run_line_key(row)
        if key is None:
            continue
        buckets.setdefault(key, []).append(row)

    out: dict[str, dict] = {}
    for key, items in buckets.items():
        attempts = [r for r in items if str(r.get("status")) not in _NON_ATTEMPT_STATUSES]
        durations = [
            float(r["duration_s"]) for r in attempts
            if r.get("duration_s") is not None
        ]
        total = len(attempts)
        done = sum(1 for r in attempts if r.get("status") == "done")
        # 「做成」口径：有 outcome 就按 outcome（人工标记优先于自动判定），
        # 没有 outcome（旧行 / 跳过 / 叫停）才退回 status == done。
        def _outcome(r: dict) -> str:
            return str(r.get("outcome") or "")

        succeeded = sum(
            1 for r in attempts
            if (_outcome(r) == "ok" if _outcome(r) else r.get("status") == "done")
        )
        partial = sum(1 for r in attempts if _outcome(r) == "partial")
        marked_failed = sum(
            1 for r in attempts
            if (_outcome(r) == "failed" and r.get("outcome_source") == "user"
                and r.get("status") == "done")
        )
        streak = 0
        for r in items:  # 新 → 旧
            status = str(r.get("status"))
            if status in _NON_ATTEMPT_STATUSES:
                continue  # 还没结论 / 用户叫停 / 到点跳过：都不是一次判断
            if status in ("error", "interrupted", "empty") or _outcome(r) == "failed":
                streak += 1
            else:
                break
        last = next(
            (r for r in items if str(r.get("status")) not in _NON_ATTEMPT_STATUSES),
            None,
        )
        out[key] = {
            "total": total,
            "done": done,
            "succeeded": succeeded,
            "partial": partial,
            "error": sum(1 for r in attempts if r.get("status") == "error"),
            "marked_failed": marked_failed,
            "success_rate": round(succeeded / total, 3) if total else 0.0,
            "avg_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "p95_s": _p95(durations),
            "max_s": round(max(durations), 1) if durations else 0.0,
            "streak": streak,
            "last_status": str(last.get("status") or "") if last else "",
            "last_outcome": str(last.get("outcome") or "") if last else "",
            "last_at": float(last.get("finished_at") or last.get("started_at") or 0)
            if last else 0.0,
            "tail": [str(r.get("status")) for r in items[:FACTORY_HEALTH_TAIL]],
            # 与 tail 等长：前端据此把「done 但有拦截」的格子染成警示色
            "tail_outcomes": [
                str(r.get("outcome") or "") for r in items[:FACTORY_HEALTH_TAIL]
            ],
        }
    return out


class FactoryMixin:
    """工厂：状态聚合、运行历史、派工、启停、新建产线与配置读写。"""

    # ---- 公共小件 ----

    def _factory_config(self) -> dict:
        """[factory] 配置的展示口径（cfg 尚未加载时按默认值兜底）。"""
        sec = getattr(self.cfg, "factory", None) if self.cfg is not None else None
        return {
            "enabled": bool(getattr(sec, "enabled", True)),
            "demo": bool(getattr(sec, "demo", True)),
            "ambient": bool(getattr(sec, "ambient", True)),
            "weather": bool(getattr(sec, "weather", False)),
            "weather_location": str(getattr(sec, "weather_location", "") or ""),
            "history_keep": int(getattr(sec, "history_keep", 2000)),
            "history_days": int(getattr(sec, "history_days", 30)),
            "max_packages": int(getattr(sec, "max_packages", 60)),
            "machine_limit": int(getattr(sec, "machine_limit", 6)),
            "label_limit": int(getattr(sec, "label_limit", 40)),
            "run_timeout_minutes": int(getattr(sec, "run_timeout_minutes", 60)),
            "max_parallel_runs": int(getattr(sec, "max_parallel_runs", 3)),
            "alert_after_failures": int(getattr(sec, "alert_after_failures", 3)),
        }

    @staticmethod
    def _factory_local_midnight(now: float) -> float:
        """本地当天 0 点的时间戳（「今日做成」按用户本地时区算）。"""
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    @staticmethod
    def _factory_trigger_text(task: dict) -> str:
        """产线的触发方式文案（与自动化页 fmtCronSchedule 同一口径，中文）。"""
        stype = str(task.get("schedule_type") or "interval")
        if stype == "interval":
            return f"每 {int(task.get('interval_minutes') or 1)} 分钟"
        if stype == "daily":
            return f"每天 {task.get('time_of_day') or '--:--'}"
        days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd = int(task.get("weekday") or 0)
        return f"每周{days[wd] if 0 <= wd < 7 else '?'} {task.get('time_of_day') or '--:--'}"

    @staticmethod
    def _factory_run_view(run: dict, cron_names: dict, node_names: dict,
                          proj_names: dict, proj_roots: dict | None = None) -> dict:
        """运行行补 name / project_name（前端拿到就能画，不再二次查询）。

        编排节点的产出全文写在 ``<项目>/.skysheep/pipeline-results/node-<id>.md``
        （见 automation._node_result_file）；这里顺手给出绝对路径，前端点「打开
        产出」就能直接走既有的文件预览，不用自己拼项目根目录。
        """
        out = dict(run)
        if out.get("kind") == "pipeline_node":
            out["name"] = node_names.get(out.get("task_id"), "")
            root = (proj_roots or {}).get(out.get("project_id"))
            if root:
                result = (
                    Path(root) / ".skysheep" / "pipeline-results"
                    / f"node-{int(out['task_id'])}.md"
                )
                # 只有真正落盘的产出才给按钮：running / empty / error 以及
                # 迭代节点未收尾时没有这个文件，点了只会弹「打开失败」。
                if result.is_file():
                    out["result_path"] = str(result)
        else:
            out["name"] = cron_names.get(out.get("task_id"), "")
        out["project_name"] = proj_names.get(out.get("project_id"), "")
        return out

    async def _factory_name_maps(self) -> tuple[dict, dict, dict, dict]:
        """一次取齐 name / project_name / result_path 用的四张表，避免逐条回查。"""
        tasks = await self.store.list_cron_tasks()
        pipelines = await self.store.list_pipelines()
        projects = await self.store.list_projects()
        cron_names = {int(t["id"]): str(t.get("name") or "") for t in tasks}
        node_names = {
            int(n["id"]): str(n.get("title") or "")
            for pipe in pipelines for n in (pipe.get("nodes") or [])
        }
        proj_names = {int(p.id): str(p.name or "") for p in projects}
        proj_roots = {int(p.id): str(p.root_path or "") for p in projects}
        return cron_names, node_names, proj_names, proj_roots

    async def _factory_target_project(self, project_id) -> None:
        """派工前置校验：目标厂区的项目行与目录都必须真实存在。

        与 ``_cron_execution_context`` 的回退行为刻意不同——那里目录缺失时
        回退到当前项目继续跑（既有行为不改），但工厂派工是用户显式指定
        目标，跑错目录比报错更糟，所以这里 fail closed。扫描循环与
        ``_execute_cron_turn`` 现在也是同一姿态：到点不派工、立即运行直接报错。
        """
        proj = None
        if project_id is not None:
            try:
                proj = await self.store.get_project(int(project_id))
            except (TypeError, ValueError):
                proj = None
        if proj is None or not Path(proj.root_path).is_dir():
            raise RuntimeError("目标厂区不可用（项目目录不存在）")

    # ---- WS 方法 ----

    async def factory_state(self) -> dict:
        """工厂首屏快照：项目 / 产线 / 在途运行 / 五个计数 / 需留意 / 配置。

        数据源全部走全项目口径的单次查询（项目、定时任务、编排、运行），
        不在项目循环里逐项目回查——厂区多了也不会退化成 N+1。运行数据取
        「近 7 天一次 + 最近 60 条列表」两份：前者供产线体检与需留意识别，
        后者供在途列表，都不按 60 条截断体检样本。
        """
        now = time.time()
        store = self.store
        all_projects = await store.list_projects()
        projects = [p for p in all_projects
                    if p.root_path != store.REMOTE_PROJECT_PATH]
        project_ids = {int(p.id) for p in projects}
        # 哨兵项目（移除中的「远程连接」）不建厂区，挂在它名下的任务也不挂需留意
        sentinel_ids = {
            int(p.id) for p in all_projects
            if p.root_path == store.REMOTE_PROJECT_PATH
        }
        proj_names = {int(p.id): str(p.name or "") for p in all_projects}
        root_ok = {int(p.id): Path(p.root_path).is_dir() for p in projects}
        lines_by_project: dict[int, list] = {int(p.id): [] for p in projects}
        attention: dict[str, dict] = {}
        attention_rank: dict[str, int] = {}
        # 计数与列表分开：列表要给前端画（限 50 条），计数必须以「全部问题」
        # 为准——以前两件事共用一个被截断的 dict，问题超过 50 条时 HUD 永远显示 50。
        seen_attention: set[str] = set()
        # 名称映射在任务 / 编排取回后填充；_attention 在两层循环里用它们把
        # 「上次运行失败」补成「厂区 · 产线：上次运行失败」，否则需留意列表
        # 只是一排同样的字，用户不知道该点哪一条。
        task_names: dict[int, str] = {}
        pipe_names: dict[int, str] = {}

        def _attention(kind: str, project_id, line_key: str, text: str) -> None:
            seen_attention.add(line_key)
            rank = _ATTENTION_RANK.get(kind, 0)
            if line_key in attention:
                if rank <= attention_rank.get(line_key, 0):
                    return
            elif len(attention) >= FACTORY_ATTENTION_LIMIT:
                return
            pname = ""
            if project_id is not None:
                try:
                    pname = proj_names.get(int(project_id), "")
                except (TypeError, ValueError):
                    pname = ""
            lname = ""
            line_kind, line_id = parse_factory_line_key(line_key)
            if line_kind == "cron":
                lname = task_names.get(line_id, "")
            elif line_kind == "pipeline":
                lname = pipe_names.get(line_id, "")
            prefix = " · ".join(part for part in (pname, lname) if part)
            shown = f"{prefix}：{text}" if prefix else text
            attention_rank[line_key] = rank
            attention[line_key] = {
                "kind": kind,
                "project_id": project_id,
                "project_name": pname,
                "line_key": line_key,
                "line_name": lname,
                "text": shown,
            }

        tasks = await store.list_cron_tasks()
        pipelines = await store.list_pipelines()
        task_names.update(
            {int(t["id"]): str(t.get("name") or "") for t in tasks})
        pipe_names.update(
            {int(p["id"]): str(p.get("name") or "") for p in pipelines})
        running = await store.list_automation_runs(status="running", limit=500)
        # 到点的任务（「排队」与「停工」两个计数都看它）：只看 enabled/next_run_at，
        # 目录可用性另判（与 _cron_pass 的派工口径一致）
        due = await store.due_cron_tasks(now)
        running_by_task = {
            int(r["task_id"]): int(r["id"])
            for r in running if r.get("kind") == "cron"
        }
        # 「需留意」的已处理标记（line_key → 时间戳）：比标记更晚的失败才会再浮上来
        acks = await store.list_factory_acks()
        # 近 7 天的运行一次取回：体检与「需留意」共用，不按 60 条截断
        window = await store.automation_runs_since(now - FACTORY_HEALTH_WINDOW_S)
        health = factory_line_health(window)
        midnight = self._factory_local_midnight(now)

        # ---- 定时任务：一条产线 ----
        for task in tasks:
            tid = int(task["id"])
            pid = task.get("project_id")
            key = factory_line_key("cron", tid)
            if pid is None or int(pid) not in project_ids:
                # 项目行没了（历史遗留 / 手工改库）：不建厂区，只挂需留意
                if pid is not None and int(pid) in sentinel_ids:
                    continue
                _attention("orphan", pid, key, "所属项目不存在")
                continue
            pid = int(pid)
            if not task.get("enabled"):
                status = "paused"
            elif not root_ok.get(pid, False):
                status = "stopped"
                _attention("stopped", pid, key, "厂区不可用（项目目录不存在）")
            elif tid in running_by_task:
                status = "running"
            else:
                status = "idle"
            last_status = str(task.get("last_status") or "")
            # 已处理过（且这次失败发生在标记之前）就不再红着：ack 之后的新失败
            # 一定带更新的 last_run_at，会重新浮上来
            if last_status == "error" and float(task.get("last_run_at") or 0) > acks.get(key, 0):
                _attention("error", pid, key, "上次运行失败")
            elif last_status == "empty" and float(task.get("last_run_at") or 0) > acks.get(key, 0):
                _attention("empty", pid, key, "上次运行无产出")
            lines_by_project[pid].append({
                "key": key,
                "kind": "cron",
                "id": tid,
                "project_id": pid,
                "name": str(task.get("name") or ""),
                "status": status,
                "trigger_text": self._factory_trigger_text(task),
                "next_run_at": float(task.get("next_run_at") or 0),
                "last_status": last_status if last_status in _CRON_LAST_STATUSES else "",
                "last_run_at": float(task.get("last_run_at") or 0),
                "running_run_id": running_by_task.get(tid),
                "health": health.get(key),
                "nodes": [],
            })

        # ---- 编排：一条多工位装配线 ----
        for pipe in pipelines:
            pid = pipe.get("project_id")
            key = factory_line_key("pipeline", pipe["id"])
            if pid is None or int(pid) not in project_ids:
                if pid is not None and int(pid) in sentinel_ids:
                    continue
                _attention("orphan", pid, key, "所属项目不存在")
                continue
            pid = int(pid)
            raw_status = str(pipe.get("status") or "draft")
            # 契约的产线状态只有 running/done/failed/draft/idle：
            # 用户主动取消（cancelled）不是故障，落 idle；其余未知值同样落 idle。
            status = raw_status if raw_status in _PIPELINE_LINE_STATUSES else "idle"
            if (raw_status == "failed"
                    and float(pipe.get("finished_at") or 0) > acks.get(key, 0)):
                _attention("error", pid, key, "编排未通过（有节点失败）")
            lines_by_project[pid].append({
                "key": key,
                "kind": "pipeline",
                "id": int(pipe["id"]),
                "project_id": pid,
                "name": str(pipe.get("name") or ""),
                "status": status,
                "trigger_text": "编排",
                "next_run_at": 0,
                "last_status": "",
                "last_run_at": float(pipe.get("finished_at") or 0),
                "running_run_id": None,
                "health": health.get(key),
                "nodes": [
                    {
                        "id": int(n["id"]),
                        "title": str(n.get("title") or ""),
                        "status": str(n.get("status") or ""),
                    }
                    for n in (pipe.get("nodes") or [])
                ],
            })

        # ---- 最近 24h 的失败 / 中断 / 跳过（按 line_key 去重）----
        # 从 7 天窗口里筛，而不是再拉一份「最近 500 条」：24h 内超过 500 次
        # 运行时旧写法会静默漏看更早的失败。
        cutoff = now - FACTORY_ATTENTION_WINDOW_S
        for run in window:
            started = float(run.get("started_at") or 0)
            if started < cutoff:
                continue
            run_status = str(run.get("status") or "")
            line_key = factory_run_line_key(run) or f"node:{int(run['task_id'])}"
            if started <= acks.get(line_key, 0):
                continue  # 这条产线的这个问题已经「知道了」
            # 跑完但有工具被门控拦下：状态仍是 done，但 outcome 已经降级为
            # partial，必须在这里浮出来——否则「预授权没勾全」在工厂里永远是绿的。
            if run_status == "done" and str(run.get("outcome") or "") == "partial":
                n = int(run.get("denied_count") or 0)
                _attention("partial", run.get("project_id"), line_key,
                           "跑完但有工具被拦" + (f"（{n} 次）" if n else ""))
                continue
            if run_status not in _RUN_ATTENTION_TEXT:
                continue
            text = _RUN_ATTENTION_TEXT[run_status]
            if run_status == "error" and run.get("error"):
                text = f"{text}：{str(run['error'])[:60]}"
            _attention(run_status, run.get("project_id"), line_key, text)

        # ---- 在途 / 最近运行（running 优先，再按开始时间倒序）----
        # 列表只要最近 60 条：走带索引的 list_automation_runs，不把 7 天样本
        # 拉回前端（体检在服务端聚合）。
        merged: dict[int, dict] = {}
        for run in await store.list_automation_runs(limit=FACTORY_RUN_LIST_LIMIT):
            merged[int(run["id"])] = run
        for run in running:
            merged.setdefault(int(run["id"]), run)
        ordered = sorted(
            merged.values(),
            key=lambda r: (r.get("status") == "running",
                           float(r.get("started_at") or 0), int(r["id"])),
            reverse=True,
        )[:FACTORY_RUN_LIST_LIMIT]
        cron_names = {int(t["id"]): str(t.get("name") or "") for t in tasks}
        node_names = {
            int(n["id"]): str(n.get("title") or "")
            for pipe in pipelines for n in (pipe.get("nodes") or [])
        }
        proj_roots = {int(p.id): str(p.root_path or "") for p in all_projects}
        runs = [self._factory_run_view(r, cron_names, node_names, proj_names, proj_roots)
                for r in ordered]

        counters = await store.automation_run_counters(done_since=midnight)
        pipeline_queued = sum(
            1 for pipe in pipelines for n in (pipe.get("nodes") or [])
            if n.get("status") == "ready"
        )
        # 「排队」以前只数编排里已就绪的节点，定时任务那一路直接 spawn、没有闸，
        # 于是同一分钟到点十条产线时 HUD 依然显示 0。现在按同一个并发口径算：
        # 到点但没跑上的任务，减去当前空闲额度（额度之外才是真的在排队）。
        cap = max(1, int(self._factory_config()["max_parallel_runs"]))
        cron_running = len(getattr(self, "_cron_running", {}) or {})
        free_slots = max(0, cap - cron_running)
        waiting = [
            t for t in due
            if int(t["id"]) not in running_by_task
            and t.get("project_id") is not None
            and root_ok.get(int(t["project_id"]), False)
        ] if due else []
        queued = pipeline_queued + max(0, len(waiting) - free_slots)
        # 真正在等的那些（额度之外的部分，按到点时间升序）：前端按每条自己等了
        # 多久决定抖动幅度，而不是把整个「排队 N」当成一个数字画三枚一模一样的包裹。
        # 只给画得出来的字段，不含任务指令与预授权。
        over = waiting[free_slots:free_slots + FACTORY_WAITING_LIMIT] if waiting else []
        waiting_view = [
            {
                "key": factory_line_key("cron", t["id"]),
                "name": str(t.get("name") or ""),
                "project_id": int(t["project_id"]) if t.get("project_id") is not None else None,
                "due_at": float(t.get("next_run_at") or 0),
            }
            for t in over
        ]

        return {
            "projects": [
                {
                    "id": int(p.id),
                    "name": str(p.name or ""),
                    "root_ok": bool(root_ok.get(int(p.id), False)),
                    "lines": lines_by_project.get(int(p.id), []),
                }
                for p in projects
            ],
            "counters": {
                "running": int(counters.get("running", 0)),
                "queued": int(queued),
                # 计数用「去重后的全部问题数」，列表仍然只给前 50 条（见 _attention）
                "attention": len(seen_attention),
                "done_today": int(counters.get("done_today", 0)),
                "tokens_today": int(counters.get("tokens_today", 0)),
            },
            "attention": list(attention.values()),
            "attention_total": len(seen_attention),
            "waiting": waiting_view,
            "runs": runs,
            "live": self._factory_live(now),
            "config": self._factory_config(),
            "now": now,
        }

    def _factory_live(self, now: float) -> dict:
        """工厂之外的盲区：对话轮与后台子代理现在在跑什么（只读快照）。

        ``core/runs.py`` 的 RunCenter 早就记着这些运行，但工厂里一格都没有——
        用户在工厂能看见「定时任务」，看不见「我自己刚发的那条对话还在跑」。
        这里只取在跑态（run.state ∈ RUNNING），不读历史、不做任何操作入口；
        stale 是「疑似卡住」的软判定（超过 5 分钟没有任何更新），给用户一个
        「去看看」的理由，硬兜底仍靠运行自身的超时。
        """
        live = {"chat": 0, "subagent": 0, "runs": []}
        center = getattr(self, "runs", None)
        if center is None:
            return live
        try:
            rows = center.running_runs()
        except Exception:  # noqa: BLE001 - 观战快照失败不该拖垮整个 state
            return live
        items = []
        for run in rows:
            try:
                items.append({
                    "id": run.id,
                    "kind": run.kind,
                    "session_id": run.session_id,
                    "title": run.title or "",
                    "state": run.state,
                    "detail": run.detail or "",
                    "started_at": float(run.created_at or 0),
                    "updated_at": float(run.updated_at or 0),
                    "stale": bool(now - float(run.updated_at or 0) > 300),
                })
            except Exception:  # noqa: BLE001 - 单条坏数据跳过
                continue
        items.sort(key=lambda r: r["started_at"], reverse=True)
        live["runs"] = items[:20]
        live["chat"] = sum(1 for r in items if r["kind"] == "turn")
        live["subagent"] = sum(1 for r in items if r["kind"] != "turn")
        return live

    async def factory_live(self) -> dict:
        """轻量实时快照：只读 RunCenter 内存，不查库。

        工厂首屏的 ``factory.state`` 会拉项目 / 任务 / 编排 / 7 天运行等多份
        数据，不适合每十几秒轮询。这里只回答「现在有多少对话轮 / 子代理在跑、
        哪些超过 5 分钟没更新」，前端据此刷新 HUD 与「对话在跑」抽屉。
        """
        now = time.time()
        return {"live": self._factory_live(now), "now": now}

    async def factory_weather(self, params: dict | None = None) -> dict:
        """现实天气：仅在设置里显式开启并填了城市时联网。

        先城市名地理编码、再按坐标取当前天气 / 昼夜 / 日出日落；结果在
        ``core.weather.WeatherService`` 里缓存 30 分钟，失败时优先沿用上一份
        可用数据（``stale=true``），不让场景因为没有网络就突然放晴。
        未开启时返回 ``{enabled: false, weather: null}``，前端据此隐藏天气信息。
        """
        cfg = self._factory_config()
        if not cfg["weather"]:
            return {"enabled": False, "weather": None}
        svc = getattr(self, "_weather_service", None)
        if svc is None:
            from ...core.weather import WeatherService
            svc = WeatherService(getattr(self.cfg, "network", None))
            self._weather_service = svc
        # 设置页改代理 / 自签 CA 后热更新出网方式
        svc.set_network(getattr(self.cfg, "network", None))
        weather = await svc.get(
            str(cfg.get("weather_location") or ""),
            force=bool((params or {}).get("force")),
        )
        return {"enabled": True, "weather": weather}

    async def factory_runs(self, params: dict) -> dict:
        """运行历史分页（按开始时间倒序）。"""

        def _opt_int(key: str):
            value = params.get(key)
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                raise RuntimeError(f"{key} 需要一个整数") from None

        status = params.get("status")
        kind = params.get("kind") or None
        if kind not in (None, "cron", "pipeline_node"):
            raise RuntimeError("kind 只支持 cron / pipeline_node")
        view = str(params.get("view") or "all")
        if view not in ("all", "done_today", "tokens_today"):
            raise RuntimeError("view 只支持 all / done_today / tokens_today")
        midnight = self._factory_local_midnight(time.time())
        rows = await self.store.list_automation_runs(
            kind=kind,
            project_id=_opt_int("project_id"),
            task_id=_opt_int("task_id"),
            pipeline_id=_opt_int("pipeline_id"),
            status=str(status) if status else None,
            outcome="ok_or_legacy" if view == "done_today" else None,
            started_since=midnight if view == "tokens_today" else None,
            finished_since=midnight if view == "done_today" else None,
            limit=_opt_int("limit") or 50,
            offset=_opt_int("offset") or 0,
        )
        if not rows:
            return {"runs": []}
        cron_names, node_names, proj_names, proj_roots = await self._factory_name_maps()
        return {
            "runs": [self._factory_run_view(r, cron_names, node_names, proj_names, proj_roots)
                     for r in rows]
        }

    async def factory_run_mark(self, params: dict) -> dict:
        """人工判定一轮运行「算不算做成」（outcome = ok / failed / ''）。

        自动口径（outcome_source=auto）只看「跑完没有 / 有没有被门控拦过」，
        真实世界里「产出对不对」只有用户知道。这里让人一键改写，之后这条
        运行就以人工判定为准（成功率的分母分子都跟着它走）。
        """
        try:
            run_id = int(params.get("run_id", 0))
        except (TypeError, ValueError):
            raise RuntimeError("run_id 需要一个整数") from None
        outcome = str(params.get("outcome") or "").strip()
        if outcome not in ("", "ok", "failed"):
            raise RuntimeError("outcome 只支持 ok / failed / 空（撤销标记）")
        run = await self.store.get_automation_run(run_id)
        if run is None:
            raise RuntimeError("运行不存在: " + str(run_id))
        if str(run.get("status")) == "running":
            raise RuntimeError("这一轮还没结束，等收尾后再判定")
        row = await self.store.set_automation_run_outcome(
            run_id, outcome, source="user" if outcome else "")
        return {"ok": True, "run": row}

    async def factory_run_now(self, params: dict) -> dict:
        """立即派工：后台起一轮运行，请求立刻返回（句柄留在在跑登记表里）。

        与 ``cron.run_now`` 的差异只在「等不等」：那边是请求内同步跑完（一次
        运行几分钟，按钮一直转），工厂要的是「派完就回执」，而且只有后台
        运行才允许被「叫停」——取消一个正在等回执的请求任务会连带掐掉回执。

        去重口径不变：已在跑时返回 started=false，不重复触发。极小概率的
        双请求竞态由 ``run_cron_task_core`` 的登记判定兜住（只会少跑，不会
        重复跑），回执文案可能乐观一点，可接受。
        """
        tid = int(params.get("task_id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        await self._factory_target_project(task.get("project_id"))
        if tid in self._cron_running:
            return {"started": False, "id": tid,
                    "notice": "该任务正在运行中，本次未重复触发"}
        spawn_bg(self._run_cron_task(tid, force=True))
        return {"started": True, "id": tid}

    async def factory_cancel(self, params: dict) -> dict:
        """叫停一条正在跑的产线（只取消本轮运行，不动启用状态与排期）。

        执行语义与 ``pipeline_cancel`` 同款：取消协程后由
        ``run_cron_task_core`` 的取消分支写 cancelled 终态并重排下次运行。
        拿不到句柄（运行由请求同步执行 / 已结束）时如实回报，不假装成功。
        """
        tid = int(params.get("task_id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        if tid not in self._cron_running:
            return {"cancelled": False, "id": tid,
                    "notice": "这条产线当前没有在跑的运行"}
        handle = self._cron_running.get(tid)
        if handle is None or handle.done():
            return {"cancelled": False, "id": tid,
                    "notice": "这一轮拿不到可取消的句柄（可能刚结束），刷新后再看"}
        if not is_bg_task(handle):
            return {"cancelled": False, "id": tid,
                    "notice": "本轮运行由发起请求同步执行，无法从工厂叫停"}
        handle.cancel()
        return {"cancelled": True, "id": tid}

    async def factory_ack(self, params: dict) -> dict:
        """把「需留意」标记为已处理（按产线记时间戳，之后的新失败会再浮上来）。

        一次可以给多个 line_key（「全部知道了」）：前端把当前列表的键传上来，
        后端只认形如 ``cron:12`` / ``pipe:3`` 的键，不碰别的数据。
        """
        raw = params.get("line_keys")
        if raw is None:
            raw = [params.get("line_key")]
        if not isinstance(raw, list):
            raise RuntimeError("line_keys 需要一个数组")
        keys = [str(k).strip() for k in raw if str(k or "").strip()]
        keys = [k for k in keys if ":" in k and not k.startswith(":")]
        if not keys:
            raise RuntimeError("line_key 需要一个形如 cron:12 / pipe:3 的产线键")
        ts = time.time()
        for key in keys:
            await self.store.factory_ack(key, ts)
        return {"ok": True, "line_keys": keys, "acked_at": ts}

    async def factory_toggle(self, params: dict) -> dict:
        """暂停 / 启用一条产线（复刻 cron.update 的 enabled 分支与广播）。"""
        tid = int(params.get("task_id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        enabled = bool(params.get("enabled"))
        kw: dict = {"enabled": 1 if enabled else 0}
        # 与 cron_update 同一口径：启用后按合并后的行重排下次运行，停用清零
        merged = {**task, **kw}
        kw["next_run_at"] = self.store.compute_next_run(merged) if enabled else 0
        task = await self.store.update_cron_task(tid, **kw)
        self._broadcast_cron(task)
        # 与 cron_update 一致：启停可能改变系统计划任务的注册状态
        sync = await self._sync_system_schedule(task)
        out = {
            "ok": True,
            "id": tid,
            "enabled": bool(task["enabled"]),
            "next_run_at": task["next_run_at"],
        }
        if sync is not None:
            out["schtask_sync"] = sync
        return out

    async def factory_line_add(self, params: dict) -> dict:
        """给指定厂区新建一条轻量定时产线（校验与 cron.add 一致）。"""
        try:
            pid = int(params.get("project_id", 0))
        except (TypeError, ValueError):
            raise RuntimeError("project_id 需要一个整数") from None
        await self._factory_target_project(pid)

        name = str(params.get("name", "")).strip()[:40] or "未命名任务"
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError("任务指令不能为空")
        stype = str(params.get("schedule_type", "interval"))
        if stype not in ("interval", "daily", "weekly"):
            raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
        interval = max(1, int(params.get("interval_minutes") or 1))
        problem = system_schedule.validate_interval_minutes(interval)
        if problem:
            raise RuntimeError(problem)
        tod = str(params.get("time_of_day") or "").strip()
        if tod:
            try:
                tod = system_schedule.normalize_time_of_day(tod)
            except ValueError as e:
                raise RuntimeError(str(e)) from None
        # weekday=0（周一）合法：必须按 is not None 判缺失（同 cron.add）
        raw_wd = params.get("weekday")
        wd = int(raw_wd) if raw_wd is not None else -1
        # allowed_tools 清洗：去空白、丢空项；误传单个字符串也按一项处理
        raw_tools = params.get("allowed_tools") or []
        if isinstance(raw_tools, str):
            raw_tools = [raw_tools]
        tools = [
            str(t).strip() for t in raw_tools
            if str(t).strip()
        ]
        task = await self.store.add_cron_task(
            pid, name, prompt, stype, interval, tod, wd, tools,
            # 终态推送开关：默认关（防打扰），与 cron.add 同一口径；
            # 新建时勾了就在这条产线上生效，之后可在自动化页改
            notify_channel=bool(params.get("notify_channel")),
        )
        task = await self.store.update_cron_task(
            task["id"], next_run_at=self.store.compute_next_run(task)
        )
        self._broadcast_cron(task)
        # 与 cron_add 一致：系统级调度开着时同步注册（关着时该方法立即返回 None）
        try:
            await self._sync_system_schedule(task)
        except Exception:  # noqa: BLE001 - 同步失败不影响产线创建
            pass
        return {"ok": True, "id": int(task["id"])}

    async def factory_save(self, params: dict) -> dict:
        """保存 [factory] 配置；热生效并回传新状态。"""

        def _opt_bool(key: str):
            value = params.get(key)
            return None if value is None else bool(value)

        def _opt_int(key: str):
            value = params.get(key)
            if value is None or value == "":
                return None
            if isinstance(value, bool):
                raise RuntimeError(f"{key} 需要一个整数")
            try:
                return int(value)
            except (TypeError, ValueError):
                raise RuntimeError(f"{key} 需要一个整数") from None

        def _opt_str(key: str):
            value = params.get(key)
            if value is None:
                return None
            return " ".join(str(value).split())[:80]

        try:
            set_factory_in_config(
                enabled=_opt_bool("enabled"),
                demo=_opt_bool("demo"),
                ambient=_opt_bool("ambient"),
                weather=_opt_bool("weather"),
                weather_location=_opt_str("weather_location"),
                history_keep=_opt_int("history_keep"),
                history_days=_opt_int("history_days"),
                max_packages=_opt_int("max_packages"),
                machine_limit=_opt_int("machine_limit"),
                label_limit=_opt_int("label_limit"),
                run_timeout_minutes=_opt_int("run_timeout_minutes"),
                max_parallel_runs=_opt_int("max_parallel_runs"),
                alert_after_failures=_opt_int("alert_after_failures"),
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()  # 热生效：下一轮清理与工厂页都按新配置
        cfg = self._factory_config()
        ledger = getattr(self, "run_ledger", None)
        if ledger is not None:
            ledger.keep = int(cfg["history_keep"])
            ledger.days = int(cfg["history_days"])
        return {"ok": True, "config": cfg}
