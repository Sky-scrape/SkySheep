"""工厂（跨项目自动化观战）：第三顶层视图的数据面与派工入口。

与「自动化」右面板分工：那边管当前项目的配置与任务行（cron.* / pipeline.*），
这里第一次把**全部项目**的定时任务与编排摆在一张桌上——观战、计数、派工，
不新增执行模型，也不新增调度语义：

- 扫描范围不变：``due_cron_tasks()`` 与 ``list_pipelines()`` 本来就是全项目口径；
- 执行上下文不变：每个任务仍按自己所属项目的工作目录 / 白名单 / 权限门运行；
- 派工复用 ``_run_cron_task(task_id, force=True)``（与 cron.run_now 同口径：
  停工的产线仍可手动跑一次，同一工位在跑时返回 started=false）；
- 暂停 / 启用等价于改 ``cron_tasks.enabled``（与 cron.update 的 enabled 分支一致）；
- 新建轻量产线写的是普通 ``cron_tasks`` 行，之后完全由现有循环接管。

全部方法在 WS 层 local_only=True（跨项目枚举面不对远程开放，见 server/app.py）。
运行账本（automation_runs）的读写细节在 ``core/run_ledger.py`` 与
``session/store.py``，这里只做聚合与展示口径。
"""

from __future__ import annotations

import time
from pathlib import Path

from ...config import ConfigError, load_config, set_factory_in_config
from ...core import system_schedule

# 工厂页「在途 / 最近」列表最多给多少条（设计稿 §8.3：最多 60 条）
FACTORY_RUN_LIST_LIMIT = 60
# 「需留意」最多给多少条（前端那栏本来就只展示前若干条）
FACTORY_ATTENTION_LIMIT = 50
# 近 24 小时的失败 / 中断 / 跳过运行进入「需留意」
FACTORY_ATTENTION_WINDOW_S = 24 * 3600
# 最近运行查询上限（store.list_automation_runs 的硬上限是 500）
FACTORY_RECENT_SCAN_LIMIT = 500

_CRON_LAST_STATUSES = ("ok", "error", "empty")
_PIPELINE_LINE_STATUSES = ("running", "done", "failed", "draft")
_RUN_ATTENTION_TEXT = {
    "error": "上次运行失败",
    "interrupted": "上次运行被中断",
    "skipped": "上一轮未结束，本次跳过",
}


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
            "history_keep": int(getattr(sec, "history_keep", 2000)),
            "history_days": int(getattr(sec, "history_days", 30)),
            "max_packages": int(getattr(sec, "max_packages", 60)),
        }

    @staticmethod
    def _factory_local_midnight(now: float) -> float:
        """本地当天 0 点的时间戳（「今日完成」按用户本地时区算）。"""
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
                          proj_names: dict) -> dict:
        """运行行补 name / project_name（前端拿到就能画，不再二次查询）。"""
        out = dict(run)
        if out.get("kind") == "pipeline_node":
            out["name"] = node_names.get(out.get("task_id"), "")
        else:
            out["name"] = cron_names.get(out.get("task_id"), "")
        out["project_name"] = proj_names.get(out.get("project_id"), "")
        return out

    async def _factory_name_maps(self) -> tuple[dict, dict, dict]:
        """一次取齐 name / project_name 用的三张表，避免逐条运行回查。"""
        tasks = await self.store.list_cron_tasks()
        pipelines = await self.store.list_pipelines()
        projects = await self.store.list_projects()
        cron_names = {int(t["id"]): str(t.get("name") or "") for t in tasks}
        node_names = {
            int(n["id"]): str(n.get("title") or "")
            for pipe in pipelines for n in (pipe.get("nodes") or [])
        }
        proj_names = {int(p.id): str(p.name or "") for p in projects}
        return cron_names, node_names, proj_names

    async def _factory_target_project(self, project_id) -> None:
        """派工前置校验：目标厂区的项目行与目录都必须真实存在。

        与 ``_cron_execution_context`` 的回退行为刻意不同——那里目录缺失时
        回退到当前项目继续跑（既有行为不改），但工厂派工是用户显式指定
        目标，跑错目录比报错更糟，所以这里 fail closed。
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
        """工厂首屏快照：项目 / 产线 / 在途运行 / 四个计数 / 需留意 / 配置。

        数据源全部走全项目口径的单次查询（项目、定时任务、编排、运行），
        不在项目循环里逐项目回查——厂区多了也不会退化成 N+1。
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

        def _attention(kind: str, project_id, line_key: str, text: str) -> None:
            if line_key in attention or len(attention) >= FACTORY_ATTENTION_LIMIT:
                return
            attention[line_key] = {
                "kind": kind,
                "project_id": project_id,
                "line_key": line_key,
                "text": text,
            }

        tasks = await store.list_cron_tasks()
        pipelines = await store.list_pipelines()
        running = await store.list_automation_runs(status="running", limit=500)
        running_by_task = {
            int(r["task_id"]): int(r["id"])
            for r in running if r.get("kind") == "cron"
        }

        # ---- 定时任务：一条产线 ----
        for task in tasks:
            tid = int(task["id"])
            pid = task.get("project_id")
            key = f"cron:{tid}"
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
            if last_status == "error":
                _attention("error", pid, key, "上次运行失败")
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
                "nodes": [],
            })

        # ---- 编排：一条多工位装配线 ----
        for pipe in pipelines:
            pid = pipe.get("project_id")
            key = f"pipe:{int(pipe['id'])}"
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
            if raw_status == "failed":
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
        recent_scan = await store.list_automation_runs(limit=FACTORY_RECENT_SCAN_LIMIT)
        cutoff = now - FACTORY_ATTENTION_WINDOW_S
        for run in recent_scan:
            if float(run.get("started_at") or 0) < cutoff:
                continue
            run_status = str(run.get("status") or "")
            if run_status not in _RUN_ATTENTION_TEXT:
                continue
            if run.get("kind") == "cron":
                line_key = f"cron:{int(run['task_id'])}"
            elif run.get("pipeline_id") is not None:
                line_key = f"pipe:{int(run['pipeline_id'])}"
            else:
                line_key = f"node:{int(run['task_id'])}"
            text = _RUN_ATTENTION_TEXT[run_status]
            if run_status == "error" and run.get("error"):
                text = f"{text}：{str(run['error'])[:60]}"
            _attention(run_status, run.get("project_id"), line_key, text)

        # ---- 在途 / 最近运行（running 优先，再按开始时间倒序）----
        merged: dict[int, dict] = {}
        for run in recent_scan[:FACTORY_RUN_LIST_LIMIT]:
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
        runs = [self._factory_run_view(r, cron_names, node_names, proj_names)
                for r in ordered]

        counters = await store.automation_run_counters(
            done_since=self._factory_local_midnight(now))
        queued = sum(
            1 for pipe in pipelines for n in (pipe.get("nodes") or [])
            if n.get("status") == "ready"
        )
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
                "attention": len(attention),
                "done_today": int(counters.get("done_today", 0)),
            },
            "attention": list(attention.values()),
            "runs": runs,
            "config": self._factory_config(),
            "now": now,
        }

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
        rows = await self.store.list_automation_runs(
            project_id=_opt_int("project_id"),
            task_id=_opt_int("task_id"),
            pipeline_id=_opt_int("pipeline_id"),
            status=str(status) if status else None,
            limit=_opt_int("limit") or 50,
            offset=_opt_int("offset") or 0,
        )
        if not rows:
            return {"runs": []}
        cron_names, node_names, proj_names = await self._factory_name_maps()
        return {
            "runs": [self._factory_run_view(r, cron_names, node_names, proj_names)
                     for r in rows]
        }

    async def factory_run_now(self, params: dict) -> dict:
        """立即派工：复用 cron.run_now 的路径（force=True）。"""
        tid = int(params.get("task_id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        await self._factory_target_project(task.get("project_id"))
        row = await self._run_cron_task(tid, force=True)
        if row is None:
            # core 只在「已在跑」（被去重集合挡下）时返回 None：如实回报
            return {"started": False, "id": tid,
                    "notice": "该任务正在运行中，本次未重复触发"}
        return {"started": True, "id": tid}

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
            notify_channel=False,
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

        try:
            set_factory_in_config(
                enabled=_opt_bool("enabled"),
                demo=_opt_bool("demo"),
                ambient=_opt_bool("ambient"),
                weather=_opt_bool("weather"),
                history_keep=_opt_int("history_keep"),
                history_days=_opt_int("history_days"),
                max_packages=_opt_int("max_packages"),
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
