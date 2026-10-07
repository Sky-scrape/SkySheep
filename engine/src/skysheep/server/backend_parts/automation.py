"""无人值守自动化：日程提醒、定时任务（cron）与任务编排（pipelines）。

从 backend.py 按职责注释整段搬入：无人值守门控别名 CronGate 与
HeadlessGate 同义，随职责区落在本模块。方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from ...bgtasks import spawn_bg
from ...channels.manager import _fmt_dur
from ...core import system_schedule
from ...security.gate import HeadlessGate
from ...tools import ChangeRecorder
from ...tools.pipeline import task_node_fields
from ._shared import SessionRuntime
from .budget_alert import (
    alert_body as budget_alert_body,
)
from .budget_alert import (
    default_state as budget_alert_default_state,
)
from .budget_alert import (
    due_tiers as budget_due_tiers,
)
from .budget_alert import (
    load_state as load_budget_alert_state,
)
from .budget_alert import (
    save_state as save_budget_alert_state,
)
from .budget_alert import (
    today_str as budget_alert_today,
)
from .daily_report import (
    _clip as clip_line,
)
from .daily_report import (
    _day_bounds as today_bounds,
)
from .daily_report import (
    load_state as load_daily_report_state,
)
from .daily_report import (
    parse_hhmm,
    run_daily_report_pass,
)
from .daily_report import (
    save_state as save_daily_report_state,
)

logger = logging.getLogger("skysheep.security")

# 无人值守门控：定时任务与 headless run 共用 security.gate.HeadlessGate
CronGate = HeadlessGate


# ---- 渠道出站推送的公共小件（定时任务 / 流水线节点共用；CLI cron run 也走这里） ----


def _cron_push_targets_of(mgr) -> list:
    """渠道推送目标：启用且配置齐全的渠道。

    聊天渠道还要求允许名单非空（allowed_ids 是主人的准入标识，名单为空的
    渠道拒发一切）；纯出站渠道（webhook）没有入站名单语义，enabled +
    configured 即为目标，allowed_ids 留空代表广播。渠道未启用/未配置 =
    静默跳过，不报错。渠道管理器未就绪时无目标。
    """
    if mgr is None:
        return []
    return [
        channel for channel in list(mgr.channels.values())
        if channel.enabled and channel.configured()
        and (getattr(channel, "pure_outbound", False) or channel.allowed_ids)
    ]


async def push_text_to_targets(mgr, body: str, *, what: str = "定时任务摘要") -> int:
    """把一段文本推给所有目标渠道（纯推送，不需要回复）。返回成功送达的次数。

    聊天渠道逐 chat_id 发送；纯出站渠道（webhook）没有 chat_id 维度，
    整段发一次。单个失败只记日志继续；渠道管理器不可用或没有目标渠道时
    静默返回 0。
    """
    delivered = 0
    for channel in _cron_push_targets_of(mgr):
        if getattr(channel, "pure_outbound", False):
            try:
                if await channel.send_text("", body):
                    delivered += 1
            except Exception as e:  # noqa: BLE001 - 推送失败只记日志
                logger.warning("%s推送 %s 失败：%s", what, channel.name, e)
            continue
        for chat_id in sorted(channel.allowed_ids):
            try:
                if await channel.send_text(chat_id, body):
                    delivered += 1
            except Exception as e:  # noqa: BLE001 - 推送失败只记日志
                logger.warning("%s推送 %s(%s) 失败：%s", what, channel.name, chat_id, e)
    return delivered


def _fire_cron_push(mgr, task: dict, status: str, duration_s: float, body: str) -> None:
    """终态推送的 fire-and-forget 入口：绝不阻塞任务收尾、绝不影响任务结果。

    开关关（默认）直接跳过；推送全程在后台任务里跑，异常由 spawn_bg 的
    收尾钩子记日志（skysheep.bg）。
    """
    if not task.get("notify_channel"):
        return
    try:
        spawn_bg(push_text_to_targets(mgr, body))
    except RuntimeError:
        pass  # 无事件循环（如纯测试环境）


CRON_PUSH_RESULT_CHARS = 500  # 结果摘要的推送上限（任务行本身只存 200 字）


def cron_push_body(task: dict, status: str, duration_s: float) -> str:
    """定时任务终态摘要文案：任务名 / 状态 / 耗时 / 完成时间 / 结果摘要（≤500 字截断）。"""
    name = task.get("name") or "未命名任务"
    result = (task.get("last_result") or "").strip()
    if len(result) > CRON_PUSH_RESULT_CHARS:
        result = result[:CRON_PUSH_RESULT_CHARS] + "…（已截断）"
    if status == "error":
        head = "失败"
    elif status == "empty":
        head = "完成（无产出）"
    else:
        head = "成功"
    lines = [
        f"⏰ 定时任务「{name}」{head}",
        f"状态：{'❌' if status == 'error' else '✅'} {head}",
        f"耗时：{_fmt_dur(duration_s)}",
        "完成时间：" + time.strftime("%Y-%m-%d %H:%M", time.localtime()),
    ]
    if result:
        lines.append(f"结果：{result}")
    elif status != "empty":
        lines.append("结果：（无）")
    return "\n".join(lines)


async def run_cron_task_core(
    store, task_id: int, *, force: bool = False,
    execute,            # async (task) -> tuple[SessionRuntime, str, bool]：宿主差异点
    broadcast=None,     # (task) -> None：任务行变化出口（后端 WS 广播 / CLI 打印）
    notify=None,        # (dict) -> None：桌面通知（CLI 无界面，传 None）
    channels=None,      # 渠道管理器（终态推送目标；CLI 传只发不收的实例）
    running: set | None = None,  # 在跑去重集合（后端扫描循环用；CLI 传 None）
) -> dict | None:
    """执行一个定时任务并收尾：结果写回任务行、重排下次运行、广播与终态推送。

    这是后端扫描循环（AutomationMixin._run_cron_task）与 CLI
    `skysheep cron run <task_id>`（Windows 任务计划程序导出的入口）共用的
    唯一路径：任务行回写、状态判定、next_run 重排、通知与 notify_channel
    推送都在这里，两边不再各写一份。宿主差异只有几处，全部由参数注入：

    - execute(task)：建独立会话 + HeadlessGate + 跑一轮，返回 (runtime, sid, stopped)；
      raise 即按失败收尾（回写 last_status=error 并推送）。
    - broadcast / notify：广播与桌面通知出口（可为 None）。
    - channels：渠道管理器；None = 无推送目标（静默跳过）。

    返回收尾后的任务行；任务不存在 / 未启用且未 force / 已在跑时返回 None。
    执行失败不向上抛（回写错误状态就是收尾），调用方读任务行拿结果。
    """
    task = await store.get_cron_task(task_id)
    if task is None:
        return None
    if running is not None and task_id in running:
        return None  # 已在跑：force 也不重复触发（与既有扫描循环口径一致）
    if not task["enabled"] and not force:
        return None
    added = False
    # 登记与 force 解耦：强制运行（cron.run_now / 连点）也进在跑集合，扫描
    # 循环才能看见它——否则强制运行在飞期间任务仍是 due 状态，下一轮扫描
    # 必然并发二次派跑（双倍 token / 推送翻倍 / 回写互相覆盖）。「已在跑不
    # 重复触发」仍由上面的 running 判定承担，方向相反但口径一致。
    if running is not None:
        running.add(task_id)
        added = True
    started_at = time.time()  # 终态推送要报耗时（成功与失败两条路都要用）
    try:
        runtime, _sid, stopped = await execute(task)
        # 取本轮最后一条助手文本作为结果摘要
        last_text = ""
        for m in reversed(runtime.agent.history):
            if m.role == "assistant" and m.text.strip():
                last_text = m.text.strip().replace("\n", " ")[:200]
                break
        status = "ok" if (not stopped and last_text) else (
            "error" if stopped else "empty")
        # 不回写 enabled 旧快照：update_cron_task 对传入的键一律覆盖，把执行
        # 开始前的快照写回会把执行期间用户的停用悄悄撤销（跑完即「复活」）。
        task = await store.update_cron_task(
            task_id,
            last_run_at=time.time(),
            last_status=status,
            last_result=last_text,
        )
        # 下次运行时间：interval 基于本次完成时刻；daily/weekly 基于当前时刻。
        # 执行期间可能已被停用（enabled=False 且 next_run_at=0）：与错误路径
        # 同口径，停用就不重排，尊重停用而不是把任务再排回队列。
        if task["enabled"]:
            task = await store.update_cron_task(
                task_id,
                next_run_at=store.compute_next_run(
                    {**task, "last_run_at": task["last_run_at"]}),
            )
        if broadcast is not None:
            broadcast(task)
        if notify is not None:
            if status != "error":
                await notify({"title": f"⏰ 定时任务完成：{task['name']}",
                              "body": last_text or "本轮没有产出"})
            else:
                await notify({"title": f"⏰ 定时任务异常：{task['name']}",
                              "body": "本轮被中断或没有结果"})
        # 终态推送（成功/异常同开关）：fire-and-forget，失败只记日志
        duration = time.time() - started_at
        _fire_cron_push(channels, task, status, duration, cron_push_body(task, status, duration))
        return task
    except Exception as e:  # noqa: BLE001 - 任务失败也要回写状态
        try:
            task = await store.update_cron_task(
                task_id,
                last_run_at=time.time(),
                last_status="error",
                last_result=str(e)[:200],
            )
            task = await store.update_cron_task(
                task_id,
                next_run_at=store.compute_next_run(
                    {**task, "last_run_at": task["last_run_at"]}
                ) if task["enabled"] else 0,
            )
            if broadcast is not None:
                broadcast(task)
            if notify is not None:
                await notify({"title": f"⏰ 定时任务失败：{task['name']}",
                              "body": str(e)[:160]})
            # 失败告警与成功摘要同一开关：推送失败不碰上面的状态回写
            duration = time.time() - started_at
            _fire_cron_push(channels, task, "error", duration,
                            cron_push_body(task, "error", duration))
        except Exception:
            pass
        return task
    finally:
        if added and running is not None:
            running.discard(task_id)



class AutomationMixin:
    """日程提醒、定时任务与任务编排（无人值守的周期 Agent 运行）。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 日程：增删改查 + 到点提醒广播 ----

    async def schedule_add(self, params: dict) -> dict:
        title = str(params.get("title", "")).strip()
        if not title:
            raise RuntimeError("日程标题不能为空")
        start_at = params.get("start_at")
        if start_at is None:
            raise RuntimeError("缺少开始时间 start_at")
        row = await self.store.add_schedule(
            title,
            float(start_at),
            notes=str(params.get("notes", "")),
            remind=bool(params.get("remind", True)),
            remind_before=int(params.get("remind_before", 0) or 0),
            end_at=float(params.get("end_at") or 0),
        )
        self.notify_schedule_changed()
        return row

    async def schedule_update(self, params: dict) -> dict:
        sid = int(params.get("id", 0))
        kw: dict = {}
        if params.get("title") is not None:
            kw["title"] = str(params["title"]).strip()
        if params.get("notes") is not None:
            kw["notes"] = str(params["notes"])
        if params.get("start_at") is not None:
            kw["start_at"] = float(params["start_at"])
        if params.get("end_at") is not None:
            kw["end_at"] = float(params["end_at"] or 0)
        if params.get("remind") is not None:
            kw["remind"] = bool(params["remind"])
        if params.get("remind_before") is not None:
            kw["remind_before"] = int(params["remind_before"])
        if params.get("done") is not None:
            kw["done"] = bool(params["done"])
        if not kw:
            raise RuntimeError("没有给出任何要修改的字段")
        row = await self.store.update_schedule(sid, **kw)
        if not row:
            raise RuntimeError("日程不存在")
        self.notify_schedule_changed()
        return row

    async def schedule_delete(self, schedule_id: int) -> dict:
        if not await self.store.delete_schedule(schedule_id):
            raise RuntimeError("日程不存在")
        self.notify_schedule_changed()
        return {"deleted": schedule_id}

    def notify_schedule_changed(self) -> None:
        """日程变化后广播事件（无在线连接时静默，如 CLI/测试）。"""
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(
                    ws_emit({"kind": "schedule_updated"})
                )
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    def _ws_broadcast(self, ev: dict) -> None:
        """引擎侧产生的广播事件（子代理直播 / 任务终态）发给在线前端。"""
        if isinstance(ev, dict) and ev.get("kind") == "task_finished":
            # 任务簿任务终态：挂接了它的流水线节点立即对账，不等下一个扫描周期
            try:
                spawn_bg(self._pipeline_kick())
            except RuntimeError:
                pass  # 无事件循环（如纯测试环境）
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit(ev))
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    async def _record_subagent_usage(
        self, session_id: str, provider: str, model: str,
        in_tokens: int, out_tokens: int, cached_tokens: int = 0,
    ) -> None:
        """子代理任务用量落库：归属派生它的会话（未知时记空串，聚合页仍可见）。"""
        await self.store.add_usage(
            session_id or "",
            provider or self.provider_name,
            model or self.provider_model,
            in_tokens, out_tokens, cached_tokens,
        )
        # 后台分身也在烧每日预算：落库后顺带检查告警（内部不抛，失败只记日志）
        await self._check_budget_alerts()

    # ---- 到点提醒循环 ----

    REMINDER_INTERVAL = 20.0  # 秒

    def start_reminder_loop(self) -> None:
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    def stop_reminder_loop(self) -> None:
        if getattr(self, "_reminder_task", None):
            self._reminder_task.cancel()
            self._reminder_task = None

    async def _reminder_loop(self) -> None:
        """周期扫描到点未提醒的日程，向所有在线前端广播提醒事件。
        应用关闭期间的到期日程会在下次启动扫描时补提醒。"""
        while True:
            try:
                await self._reminder_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.REMINDER_INTERVAL)

    async def _reminder_pass(self) -> None:
        """单轮扫描：把到期未提醒的日程推给所有在线连接并标记已提醒。"""
        due = await self.store.due_schedules()
        for row in due:
            for ws_emit in list(self.ws_emitters):
                try:
                    await ws_emit({"kind": "schedule_reminder", **row})
                except Exception:
                    pass
            await self.store.mark_schedule_reminded(row["id"])

    # ---- 定时任务：无人值守的周期 Agent 运行 ----

    CRON_INTERVAL = 20  # 扫描周期（秒）

    def start_cron_loop(self) -> None:
        self._cron_task = asyncio.create_task(self._cron_loop())
        # 启动对账只在真实进程跑：pytest 下夹具隔离的是 SKYSHEEP_HOME，管不住
        # 机器级的任务计划程序——schtasks /Query / /Delete 绝不能在测试里冒出来
        if "PYTEST_CURRENT_TEST" not in os.environ:
            spawn_bg(self._reconcile_system_schedule())

    def stop_cron_loop(self) -> None:
        if getattr(self, "_cron_task", None):
            self._cron_task.cancel()
            self._cron_task = None

    async def _cron_loop(self) -> None:
        """周期扫描到点的定时任务，逐个后台运行。"""
        while True:
            try:
                await self._cron_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.CRON_INTERVAL)

    async def _cron_pass(self) -> None:
        for row in await self.store.due_cron_tasks():
            if row["id"] in self._cron_running:
                continue
            # 后台触发：任务跑多久都不阻塞扫描循环（下一轮扫描跳过 in-flight 任务）
            spawn_bg(self._run_cron_task(row["id"]))

    def _broadcast_cron(self, task: dict) -> None:
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(
                    ws_emit({"kind": "cron_updated", "task": task})
                )
            except Exception:
                pass

    async def cron_list(self) -> dict:
        self._require_project("列出定时任务")
        tasks = await self.store.list_cron_tasks(self.project.id)
        return {"tasks": tasks}

    async def cron_add(self, params: dict) -> dict:
        self._require_project("新建定时任务")  # 定时任务绑定项目的工作目录与白名单
        name = str(params.get("name", "")).strip()[:40] or "未命名任务"
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError("任务指令不能为空")
        stype = str(params.get("schedule_type", "interval"))
        if stype not in ("interval", "daily", "weekly"):
            raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
        interval = max(1, int(params.get("interval_minutes") or 1))
        # 入口侧校验（安全审查残留发现）：interval 超出 schtasks 可表达范围、
        # time_of_day 不是 HH:MM 的垃圾值以前原样入库——系统级注册静默失败、
        # compute_next_run 静默 +24h，任务行却显示一切正常。在入口明示拒绝。
        problem = system_schedule.validate_interval_minutes(interval)
        if problem:
            raise RuntimeError(problem)
        tod = str(params.get("time_of_day") or "").strip()
        if tod:
            try:
                tod = system_schedule.normalize_time_of_day(tod)
            except ValueError as e:
                raise RuntimeError(str(e)) from None
        # weekday=0（周一）是合法值：必须按 `is not None` 判缺失，`or -1` 会把
        # 0 误判成缺失存成 -1——应用内相位跑偏（compute_next_run 对 wd<0 一律
        # +7 天），导出侧更会因 weekday 无效拒绝 WEEKLY。前端星期下拉
        # value 0..6（0=周一），默认选中周一，创建「每周一」任务传的就是 0。
        raw_wd = params.get("weekday")
        wd = int(raw_wd) if raw_wd is not None else -1
        tools = [str(t).strip() for t in (params.get("allowed_tools") or []) if str(t).strip()]
        task = await self.store.add_cron_task(
            self.project.id, name, prompt, stype, interval, tod, wd, tools,
            notify_channel=bool(params.get("notify_channel")),
        )
        task = await self.store.update_cron_task(
            task["id"], next_run_at=self.store.compute_next_run(task)
        )
        self._broadcast_cron(task)
        sync = await self._sync_system_schedule(task)
        if sync is not None:
            task["schtask_sync"] = sync
        return task

    async def cron_update(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        self._check_cron_ownership(task)
        kw = {}
        if params.get("name") is not None:
            kw["name"] = str(params["name"]).strip()[:40] or task["name"]
        if params.get("prompt") is not None:
            kw["prompt"] = str(params["prompt"]).strip()
        if params.get("schedule_type") is not None:
            stype = str(params["schedule_type"])
            if stype not in ("interval", "daily", "weekly"):
                raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
            kw["schedule_type"] = stype
        if params.get("interval_minutes") is not None:
            interval = max(1, int(params["interval_minutes"]))
            problem = system_schedule.validate_interval_minutes(interval)
            if problem:
                raise RuntimeError(problem)
            kw["interval_minutes"] = interval
        if params.get("time_of_day") is not None:
            tod = str(params["time_of_day"]).strip()
            if tod:
                try:
                    tod = system_schedule.normalize_time_of_day(tod)
                except ValueError as e:
                    raise RuntimeError(str(e)) from None
            kw["time_of_day"] = tod
        if params.get("weekday") is not None:
            kw["weekday"] = int(params["weekday"])
        if params.get("allowed_tools") is not None:
            kw["allowed_tools"] = [str(t).strip() for t in params["allowed_tools"] if str(t).strip()]
        if params.get("enabled") is not None:
            kw["enabled"] = 1 if bool(params["enabled"]) else 0
        if params.get("notify_channel") is not None:
            kw["notify_channel"] = 1 if bool(params["notify_channel"]) else 0
        # 调度/启用变化后重算下次运行时间
        if any(k in kw for k in ("schedule_type", "interval_minutes", "time_of_day",
                                 "weekday", "enabled")) and "next_run_at" not in kw:
            merged = {**task, **kw}
            enabled = kw.get("enabled", task["enabled"])
            kw["next_run_at"] = self.store.compute_next_run(merged) if enabled else 0
        task = await self.store.update_cron_task(tid, **kw)
        self._broadcast_cron(task)
        sync = await self._sync_system_schedule(task)
        if sync is not None:
            task["schtask_sync"] = sync
        return task

    async def cron_delete(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is not None:
            # 归属校验（安全审查 B8）：凭枚举到的 task_id 不能删别的项目的任务
            self._check_cron_ownership(task)
        ok = await self.store.delete_cron_task(tid)
        if ok:
            sync = await self._sync_system_schedule(task, deleted=True)
            return {"deleted": ok, "id": tid, "schtask_sync": sync}
        return {"deleted": ok, "id": tid}

    async def cron_run_now(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        self._check_cron_ownership(task)
        row = await self._run_cron_task(tid, force=True)
        if row is None:
            # 上面已确认任务存在，core 只在「已在跑」（被去重集合挡下）时返回
            # None：如实回报，不再谎报 started:true——前端照 notice 提示用户
            return {"started": False, "id": tid,
                    "notice": "该任务正在运行中，本次未重复触发"}
        return {"started": True, "id": tid}

    def _check_cron_ownership(self, task: dict) -> None:
        """定时任务必须属于当前项目才能改/删/触发（安全审查 B8）。

        报错文案与其他归属校验一致（不区分「不存在/无权」，防枚举）。
        无项目态没有可归属的项目，同样按不存在处理。
        """
        if task.get("project_id") != self._cur_project_id():
            raise RuntimeError("任务不存在: " + str(task.get("id")))

    async def _cron_execution_context(self, task: dict) -> tuple[int | None, Path | None]:
        """定时任务的项目上下文：返回（project_id, working_dir）。

        扫描循环会看到所有项目的到点任务，但后端只挂在当前项目上；
        不区分归属的话，B 项目的任务会把 prompt 执行到 A 项目的文件上
        （会话/白名单/工作目录全部错位）。目录已不存在时回退当前项目，
        任务本身的错误由运行结果体现；无项目态（当前项目已删空）时返回
        (None, None)，由调用方把任务标成「所属项目已不存在」。
        流水线节点的归属上下文同款（原 _pipeline_execution_context 与本方法
        逐字同构，已并入共用，入参是流水线行）。
        """
        pid = task.get("project_id")
        if pid is not None and pid != self._cur_project_id():
            proj = await self.store.get_project(pid)
            if proj is not None and Path(proj.root_path).is_dir():
                return pid, Path(proj.root_path)
        if self.project is None:
            return None, None
        return self.project.id, self.working_dir

    async def _run_cron_task(self, task_id: int, force: bool = False) -> dict | None:
        """跑一个定时任务：独立会话 + headless 门控；结果写回任务行并广播。

        执行/回写/推送的主体在模块级 run_cron_task_core：CLI 的
        `skysheep cron run <task_id>`（Windows 任务计划程序导出的入口）与
        后端扫描循环共用同一条路径，这里的差异只有建会话跑轮的方式。
        返回 core 的收尾任务行（None = 没有执行，如已在跑）：
        cron_run_now 靠它区分「已触发」与「已在跑」。
        """
        return await run_cron_task_core(
            self.store, task_id, force=force,
            execute=self._execute_cron_turn,
            broadcast=self._broadcast_cron,
            notify=self.notify,
            channels=self.channels,
            running=self._cron_running,
        )

    async def _execute_cron_turn(self, task: dict):
        """后端侧的一轮定时任务执行（run_cron_task_core 的 execute 注入点）。

        按任务自身项目跑（见 _cron_execution_context）：会话归属、白名单、
        工作目录都要对上，不能把别的项目的任务挂到当前项目执行。
        返回 (runtime, sid, stopped)。
        """
        if self.provider is None:
            raise RuntimeError("尚未配置可用的模型 API Key")
        cron_pid, cron_workdir = await self._cron_execution_context(task)
        if cron_pid is None or cron_workdir is None:
            # 项目已删空（无项目态）：没有可落的工作目录，不再重排下一次运行
            raise RuntimeError("任务所属的项目已不存在，定时任务已停用")

        # 每个任务一个独立会话（同名），历史随运行累积
        sess = await self.store.create_session(cron_pid, title=f"⏰ {task['name']}")
        sid = sess.id
        gate = CronGate(allowed=task["allowed_tools"], store=self.store,
                        project_id=cron_pid, working_dir=cron_workdir)
        recorder = ChangeRecorder()
        runtime = SessionRuntime(
            sid=sid,
            agent=self._build_agent(
                provider=self.provider,
                gate=gate,
                working_dir=cron_workdir,
                session_id=sid,
                recorder=recorder,
            ),
            recorder=recorder,
        )
        runtime.agent.set_system(self.compose_system_for(cron_workdir))

        async def cron_emit(ev: dict) -> None:
            pass  # 无人值守：流式/权限/队列事件不进前端，结果走任务行

        result = await self._run_turn_pipeline(
            task["prompt"], cron_emit, plan_mode=False,
            images=None, runtime=runtime, session_id=sid,
        )
        return runtime, sid, bool(result.get("stopped"))

    # ---- 定时任务终态推送（渠道出站通知） ----
    # 开关 notify_channel 存在任务行里（默认关，防打扰）；推送目标与失败语义
    # 同流水线收尾推送（_channel_push_pipeline）。这是引擎→用户的出站通知，
    # 不是工具调用，不走权限门；但摘要只取任务行既有字段，绝不读文件或工具
    # 输出全文——聊天渠道不该成为敏感内容外发口。

    def _cron_push_targets(self) -> list:
        """渠道推送目标（见模块级 _cron_push_targets_of；后端挂在 self.channels 上）。"""
        return _cron_push_targets_of(self.channels)

    def _cron_push_body(self, task: dict, status: str, duration_s: float) -> str:
        """终态摘要文案（见模块级 cron_push_body）。"""
        return cron_push_body(task, status, duration_s)

    async def _channel_push_cron(self, task: dict, status: str, duration_s: float) -> None:
        """把定时任务终态摘要推到聊天渠道（纯推送，不需要回复）。"""
        await push_text_to_targets(
            self.channels, self._cron_push_body(task, status, duration_s)
        )

    def _spawn_cron_push(self, task: dict, status: str, duration_s: float) -> None:
        """终态推送的 fire-and-forget 入口（见模块级 _fire_cron_push）。"""
        _fire_cron_push(self.channels, task, status, duration_s,
                        self._cron_push_body(task, status, duration_s))

    # ---- 每日预算告警推送（用量越线 → 渠道提醒，一天一档一条） ----
    # 复用 _cron_push_targets 的目标口径（含 Webhook 广播语义）。前提：告警
    # 跟着渠道走——没有启用且配置齐全的目标就等于不告警，护栏照常在本应用
    # 内生效；这是引擎→用户的出站通知，不走权限门（同终态推送的定性）。

    async def _check_budget_alerts(self) -> None:
        """用量落库后检查是否越线要告警：永不抛（记账收尾不受告警影响）。

        预算未设（0）或渠道无目标时静默跳过——跳过不记档位，当天中途启用
        渠道后越线仍会告警。防重发状态 budget-alert-state.json 的认领段
        （读 → 算 → 写）全程无 await，事件循环内天然串行，并发轮次不会
        双发；状态写不进（磁盘满/文件被锁）只记日志、本轮不推——档位没
        记下，硬推会让之后每轮重复告警。推送本体 spawn_bg 不阻塞轮次，
        失败由 spawn_bg 收尾钩子记日志。
        """
        try:
            budget = int(getattr(self.cfg, "daily_token_budget", 0) or 0)
        except (TypeError, ValueError):
            return
        if budget <= 0 or not self._cron_push_targets():
            return
        try:
            used = await self.store.usage_today()
        except Exception:  # noqa: BLE001 - 用量读不到就不告警，不影响轮次
            return
        # ---- 认领段（无 await）：同日同档最多一条 ----
        today = budget_alert_today()
        state = load_budget_alert_state(today=today)  # 日期对不上即跨天重置
        due = budget_due_tiers(used, budget, state["sent"])
        if not due:
            return
        fresh = budget_alert_default_state()
        fresh["date"] = today
        fresh["sent"] = sorted({*state["sent"], *due})
        try:
            save_budget_alert_state(fresh)
        except Exception as e:  # noqa: BLE001 - 写盘失败（write_text_atomic 的 OSError 面）
            # 只记日志不推：本方法承诺永不抛，主调用点挂在 send() 收尾段且无
            # 兜底，异常上抛会腰斩检查点保存、队列交棒与运行位释放
            logger.warning("预算告警状态写盘失败，本轮不推送：%s", e)
            return
        # ---- 推送段（fire-and-forget）----
        for tier in due:
            body = budget_alert_body(tier, used, budget)
            try:
                spawn_bg(push_text_to_targets(self.channels, body, what="预算告警"))
            except RuntimeError:
                pass  # 无事件循环（如纯测试环境）

    # ---- 导出到 Windows 任务计划程序（应用不开着也能到点跑） ----
    # 原理：schtasks 注册一条 `SkySheep-<taskid>` 系统计划任务，到点由系统直接拉起
    # `skysheep cron run <taskid>`（见 cli/app.py）独立执行，复用后端同一条执行路径
    # （HeadlessGate、结果回写任务行、notify_channel 推送）。创建/删除是用户在
    # 界面上点按钮的直接动作（等同 cron.add 的授权层级，dispatch 层 local_only），
    # 不走 Agent 权限门；命令参数全部由任务行按固定模板拼出，没有任意命令面。
    # schtasks 不可用 / 非 Windows 时以 notice 文案降级，不报错崩。

    SCHTASK_TASK_PREFIX = "SkySheep-"
    SCHTASK_TIMEOUT_S = 30
    # 任务行 weekday 0=周一..6=周日（与 compute_next_run / 前端一致）→ schtasks 三字母
    SCHTASK_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

    def _schtasks(self):
        """schtasks 命令执行入口（可注入）：测试替换 _schtasks_runner，
        绝不真的创建系统计划任务。"""
        runner = getattr(self, "_schtasks_runner", None)
        if runner is not None:
            return runner
        return self._schtasks_run_default

    async def _schtasks_run_default(self, *args: str) -> tuple[int, str]:
        """真实 runner：调系统 schtasks，返回 (退出码, 合并输出)。

        带 CREATE_NO_WINDOW：GUI 进程（无控制台）拉起 schtasks 会让 Windows
        新开一个终端窗口（Win11 默认 Windows Terminal），启动对账时必现闪窗。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "schtasks", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
            )
        except (OSError, FileNotFoundError) as e:
            return 1, f"schtasks 不可用：{e}"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.SCHTASK_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            return 1, "schtasks 执行超时"
        raw = out or b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 中文 Windows 上 schtasks 按控制台代码页（GBK）输出，回退解码（textio 同款探测序）
            text = raw.decode("gb18030", errors="replace")
        return proc.returncode or 0, text

    @classmethod
    def _schtasks_create_args(cls, task_name: str, entry: str, task_id: int, task: dict) -> list[str]:
        """schtasks /Create 参数：按任务行映射调度开关。

        interval → /SC MINUTE /MO n；daily → /SC DAILY /ST hh:mm；
        weekly → /SC WEEKLY /D <三日缩写> /ST hh:mm。weekly 的无效 weekday
        走 DAILY 只是本纯映射函数的兜底分支：schtasks 表达不出「+7 天」的
        相位，真按 DAILY 导出会把频次放大 7 倍——所以导出入口
        （cron_schtask_export）对这种行先拒绝并提示修正，不会落到这里。
        /TR 是 `"<entry>" cron run <task_id>`：入口带空格也要整体加内引号，
        schtasks 才能把带参命令完整存进任务。
        """
        args = ["/Create", "/F", "/TN", task_name]
        stype = str(task.get("schedule_type") or "interval")
        tod = str(task.get("time_of_day") or "09:00")
        # weekday=0（周一）是合法值：必须按 `is not None` 判缺失，`or -1` 会把
        # 0 误判成无效、周一任务降级成每天执行（7 倍频次，无人值守副作用放大）
        raw_wd = task.get("weekday")
        wd = int(raw_wd) if raw_wd is not None else -1
        if stype == "interval":
            args += ["/SC", "MINUTE", "/MO", str(max(1, int(task.get("interval_minutes") or 1)))]
        elif stype == "weekly" and 0 <= wd <= 6:
            args += ["/SC", "WEEKLY", "/D", cls.SCHTASK_WEEKDAYS[wd], "/ST", tod]
        else:
            args += ["/SC", "DAILY", "/ST", tod]
        args += ["/TR", f'"{entry}" cron run {int(task_id)}']
        return args

    @staticmethod
    def _cron_task_entry() -> tuple[str | None, str]:
        """schtasks /TR 的可执行入口：返回 (入口路径, 不可用原因)。

        源码环境：venv 的 Scripts\\skysheep.exe（console script，与当前解释器
        同目录）。打包环境：sys.executable 就是 SkySheep.exe，desktop.main()
        会把 `cron ...` 子命令转交给 cli.main，`SkySheep.exe cron run <id>`
        可直接落地。已知限制：TR 存的是绝对路径，exe 被移动/改名/venv 重建
        到别处后计划任务失效；计划任务以注册时的用户身份运行，读同一份
        ~/.skysheep（SKYSHEEP_HOME 未设置时）。
        """
        exe = Path(sys.executable)
        if getattr(sys, "frozen", False):
            return str(exe), ""
        candidate = exe.with_name("skysheep.exe")
        if candidate.is_file():
            return str(candidate), ""
        return None, "当前 Python 环境没有 skysheep 命令行入口（skysheep.exe），无法导出"

    async def cron_schtask_export(self, params: dict) -> dict:
        """把定时任务导出为 Windows 系统计划任务（schtasks /Create /F）。"""
        if sys.platform != "win32":
            return {"exported": False,
                    "notice": "导出为系统计划任务仅支持 Windows"}
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        self._check_cron_ownership(task)
        entry, problem = self._cron_task_entry()
        if entry is None:
            return {"exported": False, "task_name": self.SCHTASK_TASK_PREFIX + str(tid),
                    "notice": problem}
        # weekly 但 weekday 无效（如旧版 `or -1` 写坏的存量行）：schtasks 表达
        # 不出「+7 天」的相位，静默降级 DAILY 会把频次放大 7 倍（无人值守下
        # 副作用同步放大）——拒绝导出并提示修正，应用内调度不受影响。
        if str(task.get("schedule_type") or "") == "weekly":
            raw_wd = task.get("weekday")
            wd = int(raw_wd) if raw_wd is not None else -1
            if not 0 <= wd <= 6:
                return {"exported": False, "task_name": self.SCHTASK_TASK_PREFIX + str(tid),
                        "notice": "每周任务没有选择有效的星期几，无法导出为系统计划任务："
                                  "请在任务设置里选好星期几后再导出"}
        task_name = self.SCHTASK_TASK_PREFIX + str(tid)
        rc, out = await self._schtasks()(
            *self._schtasks_create_args(task_name, entry, tid, task))
        if rc != 0:
            detail = (out or "").strip().replace("\n", " ")[:160]
            return {"exported": False, "task_name": task_name,
                    "notice": f"创建系统计划任务失败（schtasks 退出码 {rc}）：{detail}"}
        return {"exported": True, "task_name": task_name,
                "command": f"cron run {tid}"}

    async def cron_schtask_remove(self, params: dict) -> dict:
        """移除已导出的系统计划任务（schtasks /Delete /F）。"""
        if sys.platform != "win32":
            return {"removed": False,
                    "notice": "系统计划任务仅存在于 Windows，无需移除"}
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        self._check_cron_ownership(task)
        task_name = self.SCHTASK_TASK_PREFIX + str(tid)
        rc, out = await self._schtasks()("/Delete", "/TN", task_name, "/F")
        if rc != 0:
            detail = (out or "").strip().replace("\n", " ")[:160]
            return {"removed": False, "task_name": task_name,
                    "notice": f"移除系统计划任务失败（schtasks 退出码 {rc}）：{detail}"}
        return {"removed": True, "task_name": task_name}

    async def cron_schtask_status(self, params: dict) -> dict:
        """查询任务是否已导出为系统计划任务（schtasks /Query，只读、可真实执行）。"""
        if sys.platform != "win32":
            return {"supported": False, "exported": False}
        tid = int(params.get("id", 0))
        task_name = self.SCHTASK_TASK_PREFIX + str(tid)
        rc, _out = await self._schtasks()("/Query", "/TN", task_name)
        return {"supported": True, "exported": rc == 0, "task_name": task_name}

    # ---- 系统级定时调度挂钩（[cron] system_schedule = true 时自动同步） ----
    # 与上面用户手动「导出为系统计划任务」并行的自动通道：cron.add /
    # cron.update / cron.delete 成功后按当前任务行把 SkySheepCron-<任务id>
    # 计划任务注册（/F 覆盖重建）或注销（core/system_schedule.py），到点由
    # 系统拉起 `python -m skysheep.cli.app cron-run <任务id>` 独立执行。
    # 同步是 best-effort：失败绝不影响任务行本身的增删改，但结果（成功与否、
    # 失败原因）回传给调用方——静默失败会让「无人值守覆盖」形同虚设，用户
    # 以为有覆盖实际什么都没注册。
    # 注销不受开关限制：开关只在「注册」方向做门。先注册成功、后关开关/改参
    # 失败的场景里，若注销也被开关拦下，残留的旧注册会按旧节奏反复拉起
    # cron-run（任务行没了就 SystemExit），且没有任何自愈路径。

    async def _sync_system_schedule(self, task: dict | None, *, deleted: bool = False) -> dict | None:
        """按任务行同步系统计划任务：启用 → 注册/重建；停用或已删除 → 注销。

        返回同步结果（``{"registered": bool, "notice": str}``，registered=False
        时 notice 带可读原因）供 WS 响应呈现；本次没有同步动作（非 Windows /
        无有效任务行 / 开关关闭且无注销需求）返回 None。
        """
        try:
            cfg = self.cfg
            if cfg is None:
                return None
            if sys.platform != "win32":
                return None
            tid = int((task or {}).get("id") or 0)
            if tid <= 0:
                return None
            if deleted or not task.get("enabled"):
                ok, out = await asyncio.to_thread(system_schedule.unregister, tid)
                if not ok:
                    # 任务从未注册过也会走到这里（schtasks 报不存在），降为日志
                    logger.info("注销系统计划任务未成功（任务 %s）：%s",
                                tid, (out or "").strip()[:160])
                    return {"registered": False,
                            "notice": f"注销旧系统计划任务未成功：{(out or '').strip()[:160]}"}
                return {"registered": False, "notice": ""}
            if not bool(getattr(getattr(cfg, "cron", None), "system_schedule", False)):
                return None
            entry, problem = system_schedule.default_engine_python()
            if entry is None:
                logger.info("跳过注册系统计划任务（任务 %s）：%s", tid, problem)
                return {"registered": False, "notice": problem}
            proj = await self.store.get_project(int(task.get("project_id") or 0))
            if proj is None or not Path(proj.root_path).is_dir():
                logger.warning("项目目录不可用，跳过注册系统计划任务（任务 %s）", tid)
                return {"registered": False, "notice": "项目目录不可用，无法注册系统计划任务"}
            ok, out = await asyncio.to_thread(
                system_schedule.register, tid, task, proj.root_path, entry)
            if not ok:
                reason = (out or "").strip()[:160] or "schtasks 注册失败"
                logger.warning("注册系统计划任务失败（任务 %s）：%s", tid, reason)
                # /F 覆盖重建失败 = 旧注册还在按旧参数触发（如把 daily 改成
                # 每周后注册失败，旧的每天节奏会照跑、无人值守频次放大）——
                # 先把旧注册一并注销：宁可退回「仅应用内调度」（fail-closed），
                # 也不留一条与任务行不符的无人值守执行面
                uok, _uout = await asyncio.to_thread(system_schedule.unregister, tid)
                if uok:
                    logger.info("注册失败已注销旧注册（任务 %s），系统级覆盖暂停", tid)
                return {"registered": False, "notice": reason}
            return {"registered": True, "notice": ""}
        except Exception as e:  # noqa: BLE001 - 同步失败不影响任务增删改
            logger.warning("系统计划任务同步失败（任务 %s）：%s",
                           (task or {}).get("id"), e)
            return {"registered": False, "notice": f"系统计划任务同步异常：{e}"}

    async def _reconcile_system_schedule(self) -> None:
        """启动对账（安全审查残留发现）：系统里已注册的 SkySheepCron-* 与任务
        行对不上的（任务已删 / 已停用）一律注销；开关已关闭时全量清扫——注册
        通道关闭后残留的旧注册没有别的路径能清掉，会按旧节奏反复拉起 cron-run
        （任务行没了就 SystemExit 的必败进程）。best-effort：schtasks 不可用
        只记日志，不拖慢启动。"""
        try:
            cfg = self.cfg
            if cfg is None or sys.platform != "win32":
                return
            ids = await asyncio.to_thread(system_schedule.list_scheduled_ids)
            if not ids:
                return
            if bool(getattr(getattr(cfg, "cron", None), "system_schedule", False)):
                rows = await self.store.list_cron_tasks(None)
                keep = {int(r["id"]) for r in rows if r.get("enabled")}
            else:
                keep = set()  # 开关已关：全量清扫，系统级注册一个不留
            for tid in [i for i in ids if i not in keep]:
                ok, out = await asyncio.to_thread(system_schedule.unregister, tid)
                if ok:
                    logger.info("启动对账：已注销残留的系统计划任务（任务 %s）", tid)
                else:
                    logger.info("启动对账：注销系统计划任务未成功（任务 %s）：%s",
                                tid, (out or "").strip()[:120])
        except Exception as e:  # noqa: BLE001
            logger.warning("系统计划任务启动对账失败：%s", e)

    # ---- 任务编排（pipelines）：按依赖顺序自动跑的无人值守节点 ----
    # 与定时任务同一套执行底座（独立会话 + HeadlessGate 白名单 + 完整工具集），
    # 差别在触发方式：定时任务按时间到点触发，编排节点按「依赖的节点全部完成」
    # 触发——并行开发三个功能、最后一个审查汇总，就是它要解的问题。

    PIPELINE_INTERVAL = 5  # 扫描周期（秒）；节点结束会立刻补扫一次，不等周期
    PIPELINE_GLOBAL_CAP = 3  # 跨流水线同时在跑的节点数上限（保护机器）
    NODE_RESULT_INJECT_CHARS = 2000  # 依赖产出注入下游 prompt 的截断长度
    NODE_TIMEOUT_DEFAULT_S = 3600  # 节点默认超时（秒）；0 = 不限时。防止卡死占用并发槽
    NODE_TIMEOUT_MAX_S = 24 * 3600  # 超时上限：一天。超过这个量级说明指令有问题，不该靠等
    BUSY_RETRY_WAIT_S = 300  # 会话续跑遇忙的退避等待（秒）：不消耗重试次数，等空闲
    DEP_FAIL_MARK = "依赖的节点"  # 依赖失败型错误的固定前缀（重跑时识别可回退的下游）

    def start_pipeline_loop(self) -> None:
        self._pipeline_task = asyncio.create_task(self._pipeline_loop())

    def stop_pipeline_loop(self) -> None:
        if getattr(self, "_pipeline_task", None):
            self._pipeline_task.cancel()
            self._pipeline_task = None

    async def _pipeline_loop(self) -> None:
        while True:
            try:
                await self._pipeline_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.PIPELINE_INTERVAL)

    async def _pipeline_pass(self) -> None:
        for pipe in await self.store.list_pipelines():
            if pipe["status"] != "running":
                continue
            await self._pipeline_advance(pipe)

    async def _pipeline_advance(self, pipe: dict) -> None:
        """一轮推进：挂接节点对账 + 依赖判定（blocked → ready/error/skipped）+ 并发调度 + 终态收尾。"""
        await self._sync_task_nodes(pipe)
        by_id = {n["id"]: n for n in pipe["nodes"]}
        for node in pipe["nodes"]:
            if node["status"] != "blocked" or node["kind"] == "task":
                # 挂接节点跟随原任务（已在跑/已终态），不参与依赖释放，也不会被派跑
                continue
            deps = [by_id[d] for d in node["depends_on"] if d in by_id]
            failed = [d for d in deps if d["status"] in ("error", "cancelled")]
            skipped_dep = next((d for d in deps if d["status"] == "skipped"), None)
            gates = [d for d in deps if d.get("control") == "gate" and d["status"] == "done"]
            gate_open = all(self._gate_passed(d) for d in gates)
            if node["dep_mode"] == "any":
                # any 语义：任一依赖完成就跑。失败/跳过/门拦截只在「全部依赖都已
                # 终态且无一 done」时才定死——上游 A 挂了但 B 还在跑时，下游不能
                # 提前判死（否则 any 退化成 all 的失败敏感版）。
                if not deps:
                    satisfied = True  # 无依赖节点立即就绪（与 all 模式一致）
                else:
                    finished = [d for d in deps if d["status"] in
                                ("done", "error", "cancelled", "skipped")]
                    any_done = any(d["status"] == "done" for d in deps)
                    if any_done and gate_open:
                        satisfied = True  # 走下方 satisfied 分支（stop 节点也在其中）
                    else:
                        satisfied = False
                        if len(finished) == len(deps):
                            # 全部终态仍无一放行：定死原因按优先级取第一个能解释的
                            if failed:
                                reason = (f"{self.DEP_FAIL_MARK}「{failed[0]['title']}」"
                                          "未成功，且其余依赖无一完成")
                                await self.store.update_pipeline_node(
                                    node["id"], status="error", last_error=reason,
                                    finished_at=time.time(),
                                )
                                node["status"] = "error"
                                await self.notify({
                                    "title": f"⚙️ 流水线节点失败：{node['title']}",
                                    "body": reason[:160],
                                })
                                self._spawn_pipeline_node_push(
                                    pipe, node, "error", 0.0, derived=True)
                            elif skipped_dep is not None:
                                await self.store.update_pipeline_node(
                                    node["id"], status="skipped",
                                    last_error=f"上游「{skipped_dep['title']}」被跳过",
                                    finished_at=time.time(),
                                )
                                node["status"] = "skipped"
                                self._spawn_pipeline_node_push(
                                    pipe, node, "skipped", 0.0, derived=True)
                            elif gates and not gate_open:
                                await self.store.update_pipeline_node(
                                    node["id"], status="skipped",
                                    last_error=self._gate_skip_reason(gates[0]),
                                    finished_at=time.time(),
                                )
                                node["status"] = "skipped"
                                self._spawn_pipeline_node_push(
                                    pipe, node, "skipped", 0.0, derived=True)
                        # 未到全终态：还有依赖在跑，继续等（下一轮扫描再判）
                        continue
            else:
                if failed:
                    await self.store.update_pipeline_node(
                        node["id"], status="error",
                        last_error=f"{self.DEP_FAIL_MARK}「{failed[0]['title']}」未成功，未自动运行",
                        finished_at=time.time(),
                    )
                    node["status"] = "error"
                    await self.notify({
                        "title": f"⚙️ 流水线节点失败：{node['title']}",
                        "body": f"依赖的「{failed[0]['title']}」没有成功，可在「任务编排」面板重跑",
                    })
                    self._spawn_pipeline_node_push(pipe, node, "error", 0.0, derived=True)
                    continue
                # 条件门的级联：上游被跳过，本节点也无事可做（终态，不算失败）
                if skipped_dep is not None:
                    await self.store.update_pipeline_node(
                        node["id"], status="skipped",
                        last_error=f"上游「{skipped_dep['title']}」被跳过",
                        finished_at=time.time(),
                    )
                    node["status"] = "skipped"
                    self._spawn_pipeline_node_push(pipe, node, "skipped", 0.0, derived=True)
                    continue
                # 条件门判定：门节点产出 FAIL → 依赖它的下游整体跳过；PASS 照常
                if gates and not gate_open:
                    await self.store.update_pipeline_node(
                        node["id"], status="skipped",
                        last_error=self._gate_skip_reason(gates[0]),
                        finished_at=time.time(),
                    )
                    node["status"] = "skipped"
                    self._spawn_pipeline_node_push(pipe, node, "skipped", 0.0, derived=True)
                    continue
                satisfied = all(d["status"] == "done" for d in deps)
            if satisfied:
                # 终止节点：依赖满足即截停整条流水线（自身不派跑 Agent）
                if node.get("control") == "stop":
                    await self._terminate_pipeline_at(pipe, node)
                    return  # 流水线已被终止收尾，本轮到此为止
                await self.store.update_pipeline_node(node["id"], status="ready")
                node["status"] = "ready"
        # 调度：按 seq 顺序占并发额度；全局有上限，防止多条流水线一起跑拖垮机器。
        # 在跑数从句柄集合统计（而不是本轮 DB 快照）：扫描周期与节点结束的补扫可能
        # 并发推进，快照是调度前的旧状态，会把同一流水线的并发上限算小、短暂超跑。
        # _pipeline_running 兼做去重：DB 里还是 ready 而句柄未注册时不重复派跑。
        # 挂接节点（kind=task）不进调度：它不是编排启动的，不占并发额度。
        running = len(self._pipeline_running.intersection(by_id))
        for node in sorted(
            (n for n in pipe["nodes"] if n["status"] == "ready" and n["kind"] != "task"),
            key=lambda n: n["seq"],
        ):
            if node["id"] in self._pipeline_running:
                continue
            # 会话续跑节点遇忙的退避：未到重试时刻不派跑（不占并发额度）
            if self._pipeline_busy_until.get(node["id"], 0) > time.time():
                continue
            self._pipeline_busy_until.pop(node["id"], None)
            if running >= max(1, int(pipe["concurrency"] or 1)):
                break
            if len(self._pipeline_running) >= self.PIPELINE_GLOBAL_CAP:
                break
            node["status"] = "running"
            running += 1
            self._pipeline_running.add(node["id"])
            task = asyncio.get_running_loop().create_task(self._run_pipeline_node(pipe, node))
            self._pipeline_node_tasks[node["id"]] = task
        await self._maybe_finish_pipeline(pipe)

    async def _sync_task_nodes(self, pipe: dict) -> None:
        """挂接节点（kind=task）与任务簿对账：状态与产出跟随原任务。

        任务簿在内存里、随应用重启清空：还在跑的挂接节点标为错误等用户处理，
        已终态的不动（产出早已落进节点行）。对账有变化时广播一次流水线。
        """
        changed = False
        for node in pipe["nodes"]:
            if node["kind"] != "task" or not node["ref_id"]:
                continue
            rec = self.tasks.status(node["ref_id"]) if self.tasks else None
            if rec is None:
                if node["status"] == "running":
                    await self.store.update_pipeline_node(
                        node["id"], status="error",
                        last_error="任务簿任务已随应用重启丢失，无法继续跟踪",
                        finished_at=time.time(),
                    )
                    node["status"] = "error"
                    changed = True
                continue
            fields = task_node_fields(rec)
            if (node["status"], node["result"], node["last_error"]) != (
                fields["status"], fields["result"], fields["last_error"]
            ):
                await self.store.update_pipeline_node(node["id"], **fields)
                node.update(fields)
                changed = True
        if changed:
            self._broadcast_pipeline(await self.store.get_pipeline(pipe["id"]))

    def _broadcast_pipeline(self, pipe: dict | None) -> None:
        if pipe is None:
            return
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(
                    ws_emit({"kind": "pipeline_updated", "pipeline": pipe})
                )
            except Exception:
                pass

    @staticmethod
    def _gate_passed(node: dict) -> bool:
        """条件门的判定：产出首行以 PASS / FAIL 开头时直接采信；首行没有标记就看
        全文里哪个先出现；两者都没有 = 不放行（保守拦截，宁可跳过不可误跑）。"""
        text = (node.get("result") or "").strip()
        if not text:
            return False
        first = text.splitlines()[0].strip().upper()
        if first.startswith("PASS"):
            return True
        if first.startswith("FAIL"):
            return False
        upper = text.upper()
        p, f = upper.find("PASS"), upper.find("FAIL")
        if f == -1:
            return p != -1
        return p != -1 and p < f

    @staticmethod
    def _gate_marked(node: dict) -> bool:
        """门节点是否明确输出了 PASS / FAIL 标记（区分「判 FAIL」与「忘了输出」）。"""
        text = (node.get("result") or "").strip()
        if not text:
            return False
        first = text.splitlines()[0].strip().upper()
        return first.startswith("PASS") or first.startswith("FAIL") or \
            "PASS" in text.upper() or "FAIL" in text.upper()

    @staticmethod
    def _gate_skip_reason(gate: dict) -> str:
        """门拦截的下游错误文案：FAIL 与「没输出标记」分开说，避免后者被误读成真失败。"""
        if AutomationMixin._gate_marked(gate):
            return f"条件门「{gate['title']}」判定未通过"
        return (f"条件门「{gate['title']}」没有输出 PASS/FAIL 标记，"
                f"按保守策略跳过下游（检查门节点产出，必要时重跑门节点）")

    async def _terminate_pipeline_at(self, pipe: dict, node: dict) -> None:
        """终止节点触发：自身记完成，未开始的其余节点全部取消，流水线置已停止。

        正在跑的节点不打断（自然跑完落状态），只是不再派新节点。
        """
        await self.store.update_pipeline_node(
            node["id"], status="done", result="在此终止流水线", finished_at=time.time(),
        )
        node["status"] = "done"
        for other in pipe["nodes"]:
            if other["id"] == node["id"]:
                continue
            if other["status"] in ("blocked", "ready"):
                await self.store.update_pipeline_node(
                    other["id"], status="cancelled",
                    last_error=f"被终止节点「{node['title']}」截停",
                    finished_at=time.time(),
                )
                other["status"] = "cancelled"
        updated = await self.store.update_pipeline(
            pipe["id"], status="cancelled", finished_at=time.time(),
        )
        self._broadcast_pipeline(updated)
        await self.notify({
            "title": f"⏹ 流水线已终止：{pipe['name']}",
            "body": f"终止节点「{node['title']}」条件满足，后续节点已停止。",
        })

    @staticmethod
    def _node_result_file(node_id: int) -> str:
        """节点产出全文的落盘路径（相对流水线所属项目的工作目录）。

        依赖产出注入下游的摘要只有 2000 字；全文写进约定文件，下游 prompt
        里给路径，Agent 用本就放行的只读工具自取——零新增安全面。
        """
        return f".skysheep/pipeline-results/node-{node_id}.md"

    async def _write_node_result_file(self, node: dict, result: str) -> None:
        """节点完成时把产出全文写进项目目录（供下游读全文；失败静默，摘要注入仍在）。"""
        try:
            path = Path(self._node_result_file(node["id"]))
            if not path.is_absolute():
                pipe = await self.store.get_pipeline(node["pipeline_id"])
                pid = pipe.get("project_id") if pipe else None
                if pid is not None and pid != self._cur_project_id():
                    proj = await self.store.get_project(pid)
                    base = Path(proj.root_path) if proj is not None else self.working_dir
                else:
                    base = self.working_dir
                if base is None:
                    return  # 无项目态：没有可落的工作目录，只留摘要注入
                path = base / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result, encoding="utf-8")
        except Exception:  # noqa: BLE001 - 产文件失败不扣主流程（下游还有摘要）
            pass

    async def _compose_node_prompt(self, node: dict, attempt: int = 1,
                                   prev_error: str = "") -> str:
        """组装节点 prompt：依赖产出摘要注入 + 产出全文路径 + 重试失败上下文。

        汇总/审查节点靠摘要看到上游结果；需要全量时按路径自己去读文件。
        attempt > 1 且 prev_error 非空时附上次失败原因，重试不再是盲目的原样再跑。
        """
        parts = []
        read_hint = []
        for d in node["depends_on"]:
            dep = await self.store.get_pipeline_node(int(d))
            if dep is not None and dep["status"] == "done" and dep["result"]:
                parts.append(
                    f"## 前置任务「{dep['title']}」的产出\n"
                    + dep["result"][: self.NODE_RESULT_INJECT_CHARS]
                )
                full = self._node_result_file(dep["id"])
                read_hint.append(
                    f"「{dep['title']}」的完整产出在 {full}（摘要截断时可用只读工具读全文）"
                )
        prompt = node["prompt"]
        if parts:
            head = (
                "你是任务编排流水线中的一个节点。以下是前置任务的产出，"
                "请结合它们完成自己的任务。\n\n" + "\n\n".join(parts) + "\n\n"
                + ("\n".join(read_hint) + "\n\n---\n\n你的任务：\n")
            )
            prompt = head + prompt
        if attempt > 1 and prev_error:
            prompt += (
                f"\n\n---\n\n（第 {attempt} 次尝试；上一次失败原因：{prev_error[:300]}。"
                "请换思路避免再犯同样的错。）"
            )
        return prompt

    async def _run_pipeline_node(self, pipe: dict, node: dict) -> None:
        """跑一个节点：独立会话 + headless 门控；产出写回节点行（对标 _run_cron_task）。

        超时：timeout_s > 0 时限时执行，超时按可重试失败处理（防止单节点卡死
        占住并发槽）。会话续跑遇忙：不消耗重试次数，退避后重排队。
        """
        sid = ""
        try:
            if self.provider is None:
                raise RuntimeError("尚未配置可用的模型 API Key")
            run_pid, run_workdir = await self._cron_execution_context(pipe)
            if run_pid is None or run_workdir is None:
                raise RuntimeError("流水线所属的项目已不存在，无法继续执行")
            prev_error = node.get("last_error") or ""  # 重试上下文要取更新前的快照
            if node["kind"] == "session":
                # 会话续跑预检：会话正被使用时不硬拒——不占并发、不消耗重试次数，
                # 退避后由扫描循环自动重试（并发额度不浪费在等会话上）
                sess = await self.store.get_session(node["ref_id"])
                if sess is None or sess.project_id != run_pid:
                    raise RuntimeError("会话不存在或不属于本流水线的项目")
                busy_rt = self.runtimes.get(node["ref_id"])
                busy_task = getattr(busy_rt, "run_task", None) if busy_rt else None
                if busy_task is not None and not busy_task.done():
                    self._pipeline_busy_until[node["id"]] = time.time() + self.BUSY_RETRY_WAIT_S
                    wait_min = max(1, self.BUSY_RETRY_WAIT_S // 60)
                    await self.store.update_pipeline_node(
                        node["id"],
                        last_error=f"会话正被使用，{wait_min} 分钟后自动重试（不消耗重试次数）",
                    )
                    return
            node = await self.store.update_pipeline_node(
                node["id"], status="running", started_at=time.time(),
                runs=(node["runs"] or 0) + 1, last_error="",
            )
            self._broadcast_pipeline(await self.store.get_pipeline(pipe["id"]))

            prompt = await self._compose_node_prompt(
                node, attempt=int(node.get("runs") or 1), prev_error=prev_error,
            )
            if node.get("control") == "loop" and (node.get("runs") or 0) > 0:
                # 迭代节点第 2 轮起：把上一轮产出带回去，让它接着推进而不是从零再来
                prompt += (
                    "\n\n---\n\n## 你上一轮的产出\n"
                    + (node.get("result") or "")[: self.NODE_RESULT_INJECT_CHARS]
                    + "\n\n请在此基础上继续推进；如果任务已经完成，把回复的第一行写成 DONE。"
                )
            if node["kind"] == "session":
                # 会话续跑：在本项目已有的会话里继续（带其历史上下文）；
                # 上方预检已确认空闲，这里直接加载历史
                sid = node["ref_id"]
            else:
                sess = await self.store.create_session(
                    run_pid, title=f"⚙️ {pipe['name']} · {node['title']}"
                )
                sid = sess.id
            gate = HeadlessGate(allowed=node["allowed_tools"], store=self.store,
                                project_id=run_pid, working_dir=run_workdir)
            recorder = ChangeRecorder()
            runtime = SessionRuntime(
                sid=sid,
                agent=self._build_agent(
                    provider=self.provider,
                    gate=gate,
                    working_dir=run_workdir,
                    session_id=sid,
                    recorder=recorder,
                ),
                recorder=recorder,
            )
            pipe_system = self.compose_system_for(run_workdir)
            if node["kind"] == "session":
                await self._reload_agent_history(runtime.agent, sid, pipe_system)
            else:
                runtime.agent.set_system(pipe_system)

            async def pipe_emit(ev: dict) -> None:
                pass  # 无人值守：流式/权限事件不进前端，产出走节点行

            timeout_s = max(0, int(node.get("timeout_s") or 0))
            run_coro = self._run_turn_pipeline(
                prompt, pipe_emit, plan_mode=False,
                images=None, runtime=runtime, session_id=sid,
            )
            if timeout_s > 0:
                timeout_txt = f"{timeout_s // 60} 分钟" if timeout_s >= 120 else f"{timeout_s} 秒"
                try:
                    result = await asyncio.wait_for(run_coro, timeout=timeout_s)
                except TimeoutError:
                    await self._pipeline_node_fail(
                        pipe, node, sid,
                        f"节点超时（超过 {timeout_txt}），已中止；可在节点上调大超时或拆小任务",
                        notify_body=f"超过 {timeout_txt} 未完成，自动中止",
                    )
                    return
            else:
                result = await run_coro
            last_text = ""
            for m in reversed(runtime.agent.history):
                if m.role == "assistant" and m.text.strip():
                    last_text = m.text.strip()
                    break
            if result.get("stopped"):
                await self._pipeline_node_fail(
                    pipe, node, sid, "运行被中断", retriable=False, notify_body="运行被中断",
                )
            elif not last_text:
                await self._pipeline_node_fail(
                    pipe, node, sid, "节点没有产出", notify_body="这一轮没有产出文本",
                )
            else:
                final_text, finished = self._loop_settle(node, last_text)
                if finished:
                    node = await self.store.update_pipeline_node(
                        node["id"], status="done", result=final_text,
                        session_id=sid, finished_at=time.time(),
                    )
                    # 产出全文落盘：下游需要全量时按路径自取（摘要注入只有 2000 字）
                    await self._write_node_result_file(node, final_text)
                    # 节点终态推送（开关开才推；就此收尾时让位给收尾推送）
                    self._spawn_pipeline_node_push(
                        pipe, node, "done", self._node_run_duration(node))
                else:
                    # 迭代节点：本轮未见 DONE 标记 → 重新排队，下一轮带上产出继续
                    await self.store.update_pipeline_node(
                        node["id"], status="ready", result=last_text, session_id=sid,
                    )
                    node["status"] = "ready"
        except asyncio.CancelledError:
            await self.store.update_pipeline_node(
                node["id"], status="cancelled", last_error="用户停止流水线",
                session_id=sid, finished_at=time.time(),
            )
            # 开关开时同样报一声（用户自己停的，只跟开关、不算失败）
            self._spawn_pipeline_node_push(
                pipe, node, "cancelled", self._node_run_duration(node))
        except Exception as e:  # noqa: BLE001 - 节点失败也要落状态
            try:
                await self._pipeline_node_fail(pipe, node, sid, str(e)[:300],
                                               notify_body=str(e)[:160])
            except Exception:
                pass
        finally:
            self._pipeline_running.discard(node["id"])
            self._pipeline_node_tasks.pop(node["id"], None)
            # 立刻补扫：刚完成的节点可能解锁了下游，不等下一个扫描周期
            spawn_bg(self._pipeline_kick())

    def _loop_settle(self, node: dict, text: str) -> tuple[str, bool]:
        """迭代节点（control=loop）的收尾：产出首行 DONE = 完成；否则返回未完成、
        由调用方重新排队（下一轮会带上本轮产出继续）。返回 (最终产出, 是否完成)。"""
        if node.get("control") != "loop":
            return text, True
        lines = text.splitlines()
        if lines and lines[0].strip().upper().startswith("DONE"):
            rest = "\n".join(lines[1:]).strip()
            return (rest or text), True
        if (node.get("runs") or 0) >= max(2, int(node.get("max_runs") or 2)):
            return text + f"\n\n（已到最大迭代轮数 {node.get('max_runs')}，未见 DONE 标记）", True
        return text, False

    async def _pipeline_node_fail(
        self, pipe: dict, node: dict, sid: str, message: str,
        retriable: bool = True, notify_body: str = "",
    ) -> None:
        """节点失败落状态：还有重试余量（runs < max_runs）就重新排队自动再试，
        否则标失败并通知。「运行被中断」属于人为操作，不自动重试。"""
        max_runs = max(1, int(node.get("max_runs") or 1))
        runs = int(node.get("runs") or 0)
        if retriable and runs < max_runs:
            await self.store.update_pipeline_node(
                node["id"], status="ready",
                last_error=f"第 {runs}/{max_runs} 次尝试失败（将自动重试）：{message[:200]}",
            )
            node["status"] = "ready"
            return
        node = await self.store.update_pipeline_node(
            node["id"], status="error", last_error=message[:300],
            session_id=sid, finished_at=time.time(),
        )
        await self.notify({
            "title": f"⚙️ 流水线节点失败：{node['title']}",
            "body": notify_body or message[:160],
        })
        # 节点执行失败必推（不受流水线开关限制；流水线就此收尾时由去重逻辑让位给收尾推送）
        self._spawn_pipeline_node_push(pipe, node, "error", self._node_run_duration(node))

    async def _pipeline_kick(self) -> None:
        try:
            await self._pipeline_pass()
        except Exception:
            pass

    # ---- 流水线节点终态推送（渠道出站通知；与定时任务终态推送同一套底座） ----
    # 开关 notify_channel 存在流水线行里（默认关，防打扰）；开关开时每个节点到
    # 终态推一条紧凑行，节点执行失败必推（无人值守最需要知道的正是失败）。
    # 与整体收尾推送的去重：节点到终态正好宣告整条流水线结束时，收尾推送
    # （_channel_push_pipeline，维持原行为、无条件发）马上会发一条汇总，此时
    # 节点行不再发——同一件事不推两遍。挂接节点（kind=task）跟随任务簿状态，
    # 终止节点的批量截停都不在这里推（一次性事件，逐节点只会刷屏）。

    NODE_PUSH_SUMMARY_CHARS = 200  # 紧凑行的摘要上限

    @staticmethod
    def _node_run_duration(node: dict) -> float:
        """节点本次运行的耗时（秒）；没跑过（级联跳过/依赖失败）按 0 报。"""
        started = node.get("started_at") or 0
        if started <= 0:
            return 0.0
        return max(0.0, time.time() - started)

    def _pipeline_node_push_body(
        self, pipe: dict, node: dict, status: str, duration_s: float,
    ) -> str:
        """节点终态的紧凑行：流水线名 / 节点名 / 状态 / 耗时 / 一行摘要（≤200 字）。"""
        title = (node.get("title") or "未命名节点").strip().replace("\n", " ")
        summary = (node.get("result") or node.get("last_error") or "").strip()
        summary = summary.replace("\r", " ").replace("\n", " ")
        if len(summary) > self.NODE_PUSH_SUMMARY_CHARS:
            summary = summary[: self.NODE_PUSH_SUMMARY_CHARS] + "…（已截断）"
        word = {
            "done": "✅ 完成",
            "error": "❌ 失败",
            "skipped": "⏭ 跳过",
            "cancelled": "⏹ 已停止",
        }.get(status, status)
        return (f"⚙️「{pipe.get('name') or '流水线'}」节点「{title}」{word}"
                f"｜耗时 {_fmt_dur(duration_s)}｜{summary or '（无产出）'}")

    async def _channel_push_pipeline_node(
        self, pipe: dict, node: dict, status: str, duration_s: float,
    ) -> None:
        """把节点终态紧凑行推到聊天渠道（推送目标复用 _cron_push_targets）。"""
        # 去重：本节点到终态后全部节点都已终态 → 流水线马上收尾推送汇总，节点行不发
        nodes = await self.store.list_pipeline_nodes(pipe["id"])
        terminal = ("done", "error", "cancelled", "skipped")
        if nodes and all(n["status"] in terminal for n in nodes):
            return
        await push_text_to_targets(
            self.channels,
            self._pipeline_node_push_body(pipe, node, status, duration_s),
            what="流水线节点推送",
        )

    def _spawn_pipeline_node_push(
        self, pipe: dict, node: dict, status: str, duration_s: float,
        *, derived: bool = False,
    ) -> None:
        """节点终态推送入口（fire-and-forget，绝不影响节点收尾）。

        开关开 → 每个节点到终态都推；开关关 → 只有节点自己执行失败（error，
        且不是依赖级联 derived 出来的）必推——无人值守最需要知道的正是失败。
        derived=True 的终态是依赖判定/条件门级联出来的（节点自己没跑），
        只跟流水线开关：真正的失败在它的上游已经推过。
        """
        if not pipe.get("notify_channel") and not (status == "error" and not derived):
            return
        try:
            spawn_bg(self._channel_push_pipeline_node(pipe, node, status, duration_s))
        except RuntimeError:
            pass  # 无事件循环（如纯测试环境）

    async def _channel_push_pipeline(
        self, pipe: dict, ok: bool, total: int, failed: int, skipped: list,
    ) -> None:
        """流水线收尾时向聊天渠道推一条摘要（纯推送，不需要回复）。

        推送目标复用 push_text_to_targets 的统一口径：聊天渠道要求允许名单
        非空（allowed_ids 本来就是主人的准入标识），纯出站渠道（webhook）
        enabled + configured 即推。发送失败静默记日志，不影响桌面端通知与
        流水线状态。
        """
        mgr = self.channels
        if mgr is None:
            return
        status_word = "完成" if ok else "结束（有失败）"
        icon = "✅" if ok else "⚠️"
        body = f"{icon} 流水线「{pipe['name']}」{status_word}：共 {total} 个节点"
        if failed:
            body += f"，{failed} 个未成功"
        if skipped:
            body += f"，{len(skipped)} 个被跳过"
        await push_text_to_targets(mgr, body, what="流水线摘要推送")

    async def _maybe_finish_pipeline(self, pipe: dict) -> None:
        """全部节点到终态时给流水线收尾：done/skipped 视为成功（skipped 是条件门
        主动跳过，不算失败），其余 → failed。通知带上跳过与失败计数，
        「门没输出标记」这类静默情况一眼可见。"""
        if pipe["status"] != "running":
            return
        nodes = await self.store.list_pipeline_nodes(pipe["id"])
        if not nodes or any(n["status"] in ("blocked", "ready", "running") for n in nodes):
            return
        all_done = all(n["status"] in ("done", "skipped") for n in nodes)
        updated = await self.store.update_pipeline(
            pipe["id"], status="done" if all_done else "failed", finished_at=time.time()
        )
        self._broadcast_pipeline(updated)
        skipped = [n for n in nodes if n["status"] == "skipped"]
        bad = [n for n in nodes if n["status"] not in ("done", "skipped")]
        if all_done:
            body = f"全部 {len(nodes)} 个节点完成"
            if skipped:
                body += f"，其中 {len(skipped)} 个被条件门跳过：{skipped[0]['title']}"
        else:
            body = f"{len(bad)} 个节点未成功：{bad[0]['title']}"
            if skipped:
                body += f"；另有 {len(skipped)} 个被跳过"
        await self.notify({
            "title": ("✅ 流水线完成：" if all_done else "⚠️ 流水线结束（有失败）：") + pipe["name"],
            "body": body,
        })
        # 有渠道在线时同步推送一份摘要（纯推送、不含执行，无人值守主场景）
        await self._channel_push_pipeline(pipe, all_done, len(nodes), len(bad), skipped)

    # ---- 任务编排：WS 方法 ----

    async def pipeline_list(self) -> dict:
        self._require_project("列出任务编排")  # 流水线绑定项目的工作目录
        return {"pipelines": await self.store.list_pipelines(self.project.id)}

    async def pipeline_get(self, params: dict) -> dict:
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None or pipe["project_id"] != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        # token 用量汇总：节点会话的 usage_log 聚合（无人值守批量跑的成本一眼可见）
        try:
            usage = await self.store.pipeline_usage(pipe["id"])
        except Exception:
            usage = {"in_tokens": 0, "out_tokens": 0}
        return {"pipeline": pipe, "usage": usage}

    async def pipeline_duplicate(self, params: dict) -> dict:
        """复制一条流水线为草稿（新名字加「副本」）：结构与配置原样，状态全部清零。

        挂接/会话节点保留 kind 与 ref_id 原样复制——原任务/会话若已不在，
        启动时对账会标错，用户可删；不在这里静默转 run（指令可能为空）。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None or pipe["project_id"] != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        by_old = {n["id"]: i for i, n in enumerate(pipe["nodes"])}
        nodes = []
        for n in pipe["nodes"]:
            nodes.append({
                "title": n["title"],
                "prompt": n["prompt"],
                "allowed_tools": n["allowed_tools"],
                # 依赖映射为新批次序号（store 物化回新 id）；指向已删节点的悬空依赖丢弃
                "depends_on": [by_old[d] for d in n["depends_on"] if d in by_old],
                "dep_mode": n["dep_mode"],
                "kind": n["kind"], "ref_id": n["ref_id"],
                "control": n["control"], "max_runs": n["max_runs"],
                "timeout_s": n["timeout_s"],
            })
        dup = await self.store.add_pipeline(
            pipe["project_id"], f"{pipe['name']}（副本）",
            nodes=nodes, concurrency=pipe["concurrency"],
            notify_channel=pipe.get("notify_channel", False),
        )
        self._broadcast_pipeline(dup)
        return {"pipeline": dup}

    async def pipeline_export(self, params: dict) -> dict:
        """导出流水线为可分享的 JSON（依赖转为同批次序号，导入时重新物化）。"""
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None or pipe["project_id"] != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        by_id = {n["id"]: i for i, n in enumerate(pipe["nodes"])}
        nodes = []
        for n in pipe["nodes"]:
            nodes.append({
                "title": n["title"], "prompt": n["prompt"],
                "after": [by_id[d] for d in n["depends_on"] if d in by_id],
                "dep_mode": n["dep_mode"], "allowed_tools": n["allowed_tools"],
                "kind": n["kind"], "ref_id": n["ref_id"],
                "control": n["control"], "max_runs": n["max_runs"],
                "timeout_s": n["timeout_s"],
            })
        return {
            "export": {
                "format": "skysheep-pipeline", "version": 1,
                "name": pipe["name"], "concurrency": pipe["concurrency"],
                "nodes": nodes,
            }
        }

    async def pipeline_import(self, params: dict) -> dict:
        """从导出的 JSON 建一条草稿流水线。节点校验与 create 同一套；
        挂接/会话节点校验 ref 存在性，不存在则拒收该节点（不静默转 run）。"""
        raw = params.get("export")
        if not isinstance(raw, dict) or raw.get("format") != "skysheep-pipeline":
            raise RuntimeError("不是 SkySheep 流水线导出文件（format 不符）")
        name = str(raw.get("name") or "").strip() or "导入的流水线"
        raw_nodes = raw.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise RuntimeError("导出内容里没有节点")
        self._require_project("新建任务编排")  # 流水线绑定项目的工作目录
        nodes = await self._normalize_pipeline_nodes(raw_nodes)
        pipe = await self.store.add_pipeline(
            self.project.id, name, nodes=nodes,
            concurrency=int(raw.get("concurrency") or 2),
        )
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe}

    async def _normalize_pipeline_nodes(self, raw_nodes) -> list[dict]:
        """create / import 共用的节点解析与收敛：校验控制流类型、重试/迭代上限、
        指令非空（终止节点除外）、挂接与续跑的 ref 存在性。

        接受两种引用写法：task_id / session_id（前端表单）与 kind+ref_id
        （导入 JSON），产出同一套 store 节点字典（depends_on 是同批次序号）。
        """
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise RuntimeError("流水线至少要有一个节点")
        nodes = []
        for i, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                continue
            prompt = str(raw.get("prompt") or "").strip()
            task_id = str(raw.get("task_id") or raw.get("ref_id") or "").strip() \
                if str(raw.get("kind") or "") == "task" else str(raw.get("task_id") or "").strip()
            session_id = str(raw.get("session_id") or raw.get("ref_id") or "").strip() \
                if str(raw.get("kind") or "") == "session" else str(raw.get("session_id") or "").strip()
            control = str(raw.get("control") or "")
            if control not in ("", "gate", "stop", "loop"):
                raise RuntimeError(f"第 {i + 1} 个节点的类型不认识：{control}")
            max_runs = max(1, int(raw.get("max_runs") or 1))
            if control == "loop":
                max_runs = max(2, min(10, max_runs))
            elif control in ("gate", "stop"):
                max_runs = 1  # 门/终止都是跑一次定生死的控制节点，没有重试概念
            else:
                max_runs = max(1, min(4, max_runs))
            # 超时（秒）：0 = 不限时；上限一天。默认 3600（1 小时）防卡死
            timeout_raw = raw.get("timeout_s")
            timeout_s = self.NODE_TIMEOUT_DEFAULT_S if timeout_raw is None else max(0, int(timeout_raw))
            timeout_s = min(timeout_s, self.NODE_TIMEOUT_MAX_S)
            # 终止节点是纯控制流（不派 Agent），允许没有指令
            if not prompt and control != "stop" and not task_id and not session_id:
                raise RuntimeError(f"第 {i + 1} 个节点的指令不能为空")
            base = {
                "title": str(raw.get("title") or "").strip(),
                "prompt": prompt,
                # after = 同批次序号（0 起），由 store 物化成节点 id
                "depends_on": [int(d) for d in raw.get("after") or raw.get("depends_on") or []],
                "allowed_tools": [str(t).strip() for t in raw.get("allowed_tools") or []],
                "dep_mode": "any" if raw.get("dep_mode") == "any" else "all",
                "control": control, "max_runs": max_runs, "timeout_s": timeout_s,
            }
            if task_id:
                rec = self.tasks.status(task_id) if self.tasks else None
                if rec is None:
                    raise RuntimeError(f"第 {i + 1} 个节点：任务簿里找不到任务 {task_id}"
                                       "（挂接只对本机任务簿有效）")
                base.update({
                    "title": base["title"] or f"挂接任务 {rec.prompt[:40]}",
                    "kind": "task", "ref_id": task_id,
                    "control": "", "max_runs": 1,
                    "timeout_s": 0,  # 挂接节点不派跑（跟随原任务），无超时概念
                })
            elif session_id:
                sess = await self.store.get_session(session_id)
                if sess is None or sess.project_id != self._cur_project_id():
                    raise RuntimeError(f"第 {i + 1} 个节点的会话不存在或不属于当前项目")
                base.update({
                    "title": base["title"] or f"会话续跑：{(sess.title or session_id)[:40]}",
                    "kind": "session", "ref_id": session_id,
                    "control": "", "max_runs": 1,
                    # 会话续跑会真实派跑，超时照常生效（与 run 节点一致）
                    "timeout_s": timeout_s,
                })
            else:
                base["title"] = base["title"] or f"节点 {i + 1}"
            nodes.append(base)
        if not nodes:
            raise RuntimeError("流水线至少要有一个节点")
        return nodes

    async def pipeline_update(self, params: dict) -> dict:
        """改流水线配置（当前只有 notify_channel 推送开关；默认关、随改随生效）。"""
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        kw = {}
        if params.get("notify_channel") is not None:
            kw["notify_channel"] = 1 if bool(params["notify_channel"]) else 0
        if not kw:
            raise RuntimeError("没有给出任何要修改的字段")
        pipe = await self.store.update_pipeline(pipe["id"], **kw)
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe}

    async def pipeline_create(self, params: dict) -> dict:
        self._require_project("新建任务编排")  # 流水线绑定项目的工作目录
        name = str(params.get("name") or "").strip() or "未命名流水线"
        nodes = await self._normalize_pipeline_nodes(params.get("nodes"))
        pipe = await self.store.add_pipeline(
            self.project.id, name, nodes=nodes,
            concurrency=int(params.get("concurrency") or 2),
            notify_channel=bool(params.get("notify_channel")),
        )
        # 挂接节点建立即跟随任务当前状态（不等下一轮扫描对账）
        for n in pipe["nodes"]:
            if n["kind"] == "task" and n["status"] == "blocked":
                rec = self.tasks.status(n["ref_id"]) if self.tasks else None
                if rec is not None:
                    await self.store.update_pipeline_node(n["id"], **task_node_fields(rec))
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe}

    async def pipeline_attach(self, params: dict) -> dict:
        """把任务簿后台任务挂接为节点：状态与产出跟随原任务，不占流水线并发额度。

        正在跑的任务挂进来，下游节点就能「等它完成」——已在飞的任务由此进入编排。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        task_id = str(params.get("task_id") or "").strip()
        rec = self.tasks.status(task_id) if self.tasks else None
        if rec is None:
            raise RuntimeError("任务簿里找不到任务 " + (task_id or "（空）"))
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"挂接任务 {rec.prompt[:40]}",
            str(params.get("prompt") or ""),
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
            kind="task", ref_id=task_id,
        )
        node = await self.store.update_pipeline_node(node["id"], **task_node_fields(rec))
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node}

    async def pipeline_import_cron(self, params: dict) -> dict:
        """把定时任务的指令与预授权名单复制成 run 节点；原任务默认照常周期运行。"""
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        cron = await self.store.get_cron_task(int(params.get("cron_id", 0)))
        if cron is None or cron["project_id"] != self._cur_project_id():
            raise RuntimeError("定时任务不存在（或不属于当前项目）")
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"定时任务：{cron['name']}",
            str(params.get("prompt") or "").strip() or cron["prompt"],
            allowed_tools=cron["allowed_tools"],
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
        )
        if params.get("disable_source"):
            await self.store.update_cron_task(cron["id"], enabled=0, next_run_at=0)
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node,
                "source_disabled": bool(params.get("disable_source"))}

    async def pipeline_add_task(self, params: dict) -> dict:
        """直接写一条指令新建 run 节点：不必先有定时任务/任务簿/会话，就地排进流水线。

        与 add_session 同样无人值守：按节点预授权名单执行（新建时名单为空，只读放行，
        其余在流水线面板里逐节点补预授权）。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("新任务要写清要做什么（指令不能为空）")
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"新任务：{prompt[:40]}",
            prompt,
            allowed_tools=params.get("allowed_tools") or [],
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
        )
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node}

    async def pipeline_add_session(self, params: dict) -> dict:
        """把已有会话纳入流水线：节点在该会话里续跑（带其历史上下文）。

        会话节点参与依赖释放（等上游完成才续跑）；仍按节点预授权名单无人值守执行，
        不沿用该会话界面上选的权限档。会话正在跑对话时节点会标失败，空闲后可重跑。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        sid = str(params.get("session_id") or "").strip()
        sess = await self.store.get_session(sid)
        if sess is None or sess.project_id != pipe["project_id"]:
            raise RuntimeError("会话不存在或不属于本流水线的项目")
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("会话节点要给出这一轮要做什么（指令不能为空）")
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"会话续跑：{(sess.title or sid)[:40]}",
            prompt,
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
            kind="session", ref_id=sid,
        )
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node}

    def _check_pipeline_ownership(self, pipe: dict) -> None:
        """流水线必须属于当前项目才能改/删/启停（同 _check_cron_ownership，防枚举）。"""
        if pipe.get("project_id") != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(pipe.get("id")))

    async def pipeline_start(self, params: dict) -> dict:
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        if pipe["status"] == "running":
            raise RuntimeError("流水线已在运行中")
        if not pipe["nodes"]:
            raise RuntimeError("流水线没有节点，先编辑再加节点")
        if all(n["status"] == "done" for n in pipe["nodes"]):
            raise RuntimeError("全部节点都已完成；要重跑某个节点请用节点上的重跑按钮")
        # 上次被停止的节点退回等待，重新参与调度
        for n in pipe["nodes"]:
            if n["status"] == "cancelled":
                await self.store.update_pipeline_node(n["id"], status="blocked", last_error="")
        # 全部停在终态（done/error 混合）且没有可重置的停止节点：直接启动只会
        # 立刻再收尾一次（多发一遍完成通知），明确告诉用户先重跑失败节点
        if all(n["status"] in ("done", "error") for n in pipe["nodes"]):
            raise RuntimeError("没有可运行的节点——失败节点请先在详情里点「重跑」，或删除后重建流水线")
        pipe = await self.store.update_pipeline(pipe["id"], status="running", finished_at=0)
        self._broadcast_pipeline(pipe)
        spawn_bg(self._pipeline_pass())
        return {"pipeline": pipe, "started": True}

    async def pipeline_cancel(self, params: dict) -> dict:
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        if pipe["status"] != "running":
            raise RuntimeError("流水线不在运行中")
        pipe = await self.store.update_pipeline(pipe["id"], status="cancelled",
                                                finished_at=time.time())
        for n in pipe["nodes"]:
            if n["status"] in ("blocked", "ready"):
                await self.store.update_pipeline_node(
                    n["id"], status="cancelled", last_error="用户停止流水线",
                    finished_at=time.time(),
                )
            elif n["status"] == "running":
                t = self._pipeline_node_tasks.get(n["id"])
                if t is not None and not t.done():
                    t.cancel()  # 协程收尾会把节点标为 cancelled
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "cancelled": True}

    async def pipeline_delete(self, params: dict) -> dict:
        pid = int(params.get("id", 0))
        pipe = await self.store.get_pipeline(pid)
        if pipe is not None:
            self._check_pipeline_ownership(pipe)
            for n in pipe["nodes"]:
                if n["status"] == "running":
                    t = self._pipeline_node_tasks.get(n["id"])
                    if t is not None and not t.done():
                        t.cancel()
        ok = await self.store.delete_pipeline(pid)
        return {"deleted": ok, "id": pid}

    async def pipeline_node_rerun(self, params: dict) -> dict:
        """重跑一个节点：节点退回等待；因依赖失败而挂掉的下游一并退回等待。

        重跑已完成（done）节点时，下游的产出基于旧结果：默认不自动级联，
        返回 needs_confirm 让前端问一句；用户确认后 cascade=true 把全部
        传递下游（不含挂接节点）一并退回重跑，避免新旧产出混用。
        """
        node = await self.store.get_pipeline_node(int(params.get("id", 0)))
        if node is None:
            raise RuntimeError("节点不存在: " + str(params.get("id")))
        pipe = await self.store.get_pipeline(node["pipeline_id"])
        if pipe is None:
            raise RuntimeError("流水线不存在")
        self._check_pipeline_ownership(pipe)
        if node["kind"] == "task":
            raise RuntimeError("挂接节点跟随原任务，不能单独重跑；请在「任务」里重新派任务后再挂接")
        if node["status"] == "running":
            raise RuntimeError("节点正在运行，不能重跑")
        cascade = bool(params.get("cascade"))
        if node["status"] == "done" and not cascade:
            # 找全部传递下游（不含挂接节点——它们跟随原任务，不重跑）
            downstream = self._pipeline_downstream(pipe["nodes"], node["id"])
            live = [n for n in downstream if n["kind"] != "task"]
            if live:
                return {
                    "needs_confirm": True,
                    "downstream": [n["title"] for n in live],
                }
        await self.store.update_pipeline_node(
            node["id"], status="blocked", result="", last_error="", finished_at=0,
        )
        if cascade and node["status"] == "done":
            # 级联：全部传递下游退回重跑（产出已基于旧上游结果，继续用会失真）
            for n in self._pipeline_downstream(pipe["nodes"], node["id"]):
                if n["kind"] == "task" or n["id"] == node["id"]:
                    continue
                if n["status"] in ("done", "error", "skipped", "cancelled"):
                    await self.store.update_pipeline_node(
                        n["id"], status="blocked", result="", last_error="", finished_at=0,
                    )
        else:
            # 只回退「因依赖失败」的错误下游（DEP_FAIL_MARK 前缀）；自身跑挂的不动
            for n in pipe["nodes"]:
                if (n["id"] != node["id"] and n["status"] == "error"
                        and node["id"] in n["depends_on"]
                        and n["last_error"].startswith(self.DEP_FAIL_MARK)):
                    await self.store.update_pipeline_node(
                        n["id"], status="blocked", last_error="", finished_at=0,
                    )
        if pipe["status"] in ("failed", "cancelled", "done"):
            # done 也要拉回：重跑已完成节点（含级联）后，流水线必须重新参与调度
            pipe = await self.store.update_pipeline(pipe["id"], status="running", finished_at=0)
        self._broadcast_pipeline(pipe)
        if pipe["status"] == "running":
            spawn_bg(self._pipeline_pass())
        return {"pipeline": pipe}

    @staticmethod
    def _pipeline_downstream(nodes: list[dict], node_id: int) -> list[dict]:
        """全部传递下游（依赖 node_id 的节点，含间接），按 seq 排序去重。"""
        out, seen = [], set()
        frontier = [node_id]
        while frontier:
            cur = frontier.pop(0)
            for n in nodes:
                if cur in n["depends_on"] and n["id"] not in seen:
                    seen.add(n["id"])
                    out.append(n)
                    frontier.append(n["id"])
        out.sort(key=lambda n: n["seq"])
        return out

    # ---- 每日运行日报（每天一条的终态汇总推送） ----
    # 状态与汇总主体在 backend_parts/daily_report.py；这里只挂扫描循环与
    # 设置读写。循环要由应用启动处显式 start（与 cron / 流水线循环同款），
    # 接线阶段在 app 启动处调用 start_daily_report_loop 即生效。

    DAILY_REPORT_INTERVAL = 60.0  # 秒：每轮只读一次状态文件判断是否到点，开销可忽略

    def start_daily_report_loop(self) -> None:
        self._daily_report_task = asyncio.create_task(self._daily_report_loop())

    def stop_daily_report_loop(self) -> None:
        if getattr(self, "_daily_report_task", None):
            self._daily_report_task.cancel()
            self._daily_report_task = None

    async def _daily_report_loop(self) -> None:
        """周期检查是否到日报时刻：到点推一次并记日期，单轮失败不终止循环。"""
        while True:
            try:
                await run_daily_report_pass(self.store, self.channels)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮失败不终止循环（与 cron / 流水线扫描循环同一姿态）
            await asyncio.sleep(self.DAILY_REPORT_INTERVAL)

    async def daily_report_status(self) -> dict:
        """日报设置回读（设置页渲染用；WS 方法由接线阶段挂 dispatch）。"""
        return load_daily_report_state()

    async def daily_report_save(self, params: dict) -> dict:
        """保存日报设置：enabled 开关与推送时刻（HH:MM），原子写状态文件。

        时刻不合法直接报错（不静默回落默认值——用户填错要看得见）。
        """
        state = load_daily_report_state()
        if params.get("enabled") is not None:
            state["enabled"] = bool(params["enabled"])
        if params.get("time") is not None:
            raw = str(params["time"]).strip()
            if parse_hhmm(raw) is None:
                raise RuntimeError("推送时间格式应为 HH:MM（24 小时制，如 09:00）")
            state["time"] = raw
        save_daily_report_state(state)
        return await self.daily_report_status()

    # ---- 今日运行总览（只读聚合）：无人值守能力从「配好了」变成「看得见」 ----
    # 数据全在库里：cron_tasks 的 last_run_at/last_status/last_result（每任务只留
    # 最后一次）、流水线节点的终态行、usage_log 当日聚合、日报状态文件与每日预算
    # 字段。取窗与日报同源（本地时区今天，_day_bounds 口径一致）；明细含任务名与
    # 结果摘要，属内容面，按 B14 项目过滤——只聚合当前项目，不跨项目下发。

    RUN_CENTER_RESULT_CHARS = 100  # 明细里「一句话结果」的截断上限

    async def run_center_summary(self) -> dict:
        """聚合「今日运行」总览（只读，不改任何状态）。

        返回：cron（今日定时任务计数 + 明细）、pipeline（今日流水线节点计数 +
        明细）、usage（今日 token / 预算剩余）、next_run_at（下次调度，0 = 无
        排期）、daily_report（日报开关状态）。字段缺失容忍：行缺键按缺省值算；
        预算字段在配置层不存在（前置版本未带 daily_token_budget）或未设（0）
        时按 None 返回，前端隐藏预算段。
        """
        start, end = today_bounds(time.time())
        pid = self._cur_project_id()
        # 无项目态没有可归属的项目：明细为空，全局数字（token/预算/日报）照常
        cron_rows = await self.store.list_cron_tasks(pid) if pid is not None else []
        cron_items: list[dict] = []
        cron_ok = cron_bad = 0
        next_run_at = 0.0
        for t in cron_rows:
            nr = float(t.get("next_run_at") or 0)
            if t.get("enabled") and nr > 0 and (next_run_at == 0 or nr < next_run_at):
                next_run_at = nr
            ran_at = float(t.get("last_run_at") or 0)
            if not start <= ran_at < end:
                continue
            status = str(t.get("last_status") or "")
            # 计数口径与日报一致：ok/empty 算成功，error 算失败，其他只进明细
            if status == "error":
                cron_bad += 1
            elif status in ("ok", "empty"):
                cron_ok += 1
            cron_items.append({
                "id": t.get("id"),
                "name": t.get("name") or "未命名任务",
                "at": ran_at,
                "status": status,
                "result": clip_line(t.get("last_result"), self.RUN_CENTER_RESULT_CHARS),
            })
        cron_items.sort(key=lambda x: x["at"], reverse=True)

        pipe_rows = await self.store.list_pipelines(pid) if pid is not None else []
        pipe_items: list[dict] = []
        pipe_ok = pipe_bad = 0
        for p in pipe_rows:
            for n in p.get("nodes") or []:
                status = str(n.get("status") or "")
                if status not in ("done", "error", "cancelled", "skipped"):
                    continue  # 等待/运行中的节点还没有「结果」可看
                fin = float(n.get("finished_at") or 0)
                if not start <= fin < end:
                    continue
                if status in ("done", "skipped"):
                    pipe_ok += 1
                else:
                    pipe_bad += 1
                pipe_items.append({
                    "pipeline": p.get("name") or "未命名流水线",
                    "title": n.get("title") or "未命名节点",
                    "at": fin,
                    "status": status,
                    "result": clip_line(
                        n.get("result") or n.get("last_error"),
                        self.RUN_CENTER_RESULT_CHARS,
                    ),
                })
        pipe_items.sort(key=lambda x: x["at"], reverse=True)

        try:
            used = int(await self.store.usage_today())
        except Exception:  # noqa: BLE001 - 用量读不到按 0 展示，总览不能整卡崩
            used = 0
        # 预算字段缺失（前置版本）/未设（0）→ None：UI 隐藏预算段
        try:
            budget = max(0, int(getattr(self.cfg, "daily_token_budget", None) or 0))
        except (TypeError, ValueError):
            budget = 0
        remaining = max(0, budget - used) if budget > 0 else None
        return {
            "date": time.strftime("%Y-%m-%d"),
            "cron": {"total": len(cron_items), "ok": cron_ok, "error": cron_bad,
                     "items": cron_items},
            "pipeline": {"total": len(pipe_items), "ok": pipe_ok, "error": pipe_bad,
                         "items": pipe_items},
            "usage": {"today": used, "budget": budget or None, "remaining": remaining},
            "next_run_at": next_run_at,
            "daily_report": load_daily_report_state(),
        }
