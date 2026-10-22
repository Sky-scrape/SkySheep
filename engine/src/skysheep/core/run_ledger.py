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

import logging
import time

logger = logging.getLogger("skysheep.run_ledger")

# 运行状态取值（与设计稿 §8.1 一致）：running / done / empty / error /
# interrupted / skipped / orphan / cancelled。本模块只写其中六种，orphan 由
# 工厂页按「所属厂区不可用」的展示口径识别，不落库。
RUN_STATUSES = (
    "running", "done", "empty", "error", "interrupted", "skipped", "orphan", "cancelled",
)


class RunLedger:
    """``automation_runs`` 的写入口（实例由 ServerBackend 持有）。"""

    #: 每多少次 start 清理一次历史（设计稿 §8.1：启动与每 100 次插入各清一次）
    PRUNE_EVERY = 100

    def __init__(self, store, emit=None, keep: int = 2000, days: int = 30) -> None:
        self.store = store
        self.emit = emit
        self.keep = max(1, int(keep))
        self.days = max(1, int(days))
        self._starts = 0

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
        self._starts += 1
        self._emit("started", run)
        if self._starts % self.PRUNE_EVERY == 0:
            await self.maybe_prune()
        return run

    async def finish(
        self,
        run_id: int,
        *,
        status: str,
        result_excerpt: str = "",
        error: str = "",
        session_id: str | None = None,
    ) -> dict | None:
        """收尾一条运行并发 ``finished`` 事件；行不存在返回 None。"""
        row = await self.store.finish_automation_run(
            int(run_id), status=str(status), result_excerpt=result_excerpt,
            error=error, session_id=session_id,
        )
        run = await self._decorate(row)
        if run is not None:
            self._emit("finished", run)
        return run

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
