"""接力控制与运行期入口。

两段职责：
1. 配置草稿（``relaycfg.get/save``）——仅保存草稿，不创建任务/会话/工具/窗口；
2. 运行期（``relay.start/list/get/approve/pause/resume/stop``）——**无工具串行
   A/B**：固定 A/B 槽位角色、串行无环工作流，每个节点只发一次
   ``provider.stream(messages, [])`` 纯文本调用，**绝不**经 Agent / chat.send /
   hooks / MCP / 工具注册表；节点一旦请求工具即协议错误并暂停。

安全边界：所有运行期方法 local_only；状态变更走 store 的独立短写事务 +
request_id 幂等 + expected_revision CAS；任务按项目归属校验；provider 用
detached 构建（不改全局记账），自建 provider 用完即还，注入的共享 provider
不关。数据目录独占锁确保第二后端不打断活任务。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

from pydantic import ValidationError

from ...config import config_path, load_config, update_config_section
from ...core.context import estimate_tokens
from ...core.relay import (
    RELAY_TERMINAL_STATES,
    RelayConfig,
    RelayLimitError,
    RelayProtocolError,
    RelayWorkflow,
    build_node_messages,
    build_packet,
    compute_budget,
    packet_to_text,
    report_hash,
    run_node_text,
)
from ...models.base import ProviderError
from ...security.gate import Decision, PermissionGate, normalize_decision
from ...tools.fs import WriteFileTool

logger = logging.getLogger("skysheep.relay")

# 节点输出上限（字符）与交接包中各报告摘要保留长度
_RELAY_MAX_OUTPUT_CHARS = 200_000
_RELAY_SUMMARY_CHARS = 1200
_RELAY_RECENT_CONTEXT = 8
# 同进程内按「规范化库路径」的接力独占运行权注册表（跨进程另有文件锁）
_RELAY_LOCKS: dict[str, str] = {}

# 主副屏接力：副屏会话标题（复用它判断「上次点接力留下的空副屏」）
_RELAY_PEER_TITLE = "接力 · 副屏"
# 交接给下一轮时保留的上一轮输出长度（模型不需要全文，太长还会挤掉本轮预算）
_RELAY_DUAL_PREV_CHARS = 6000


class RelaySettingsMixin:
    # ---- 配置草稿（原有） ----

    def relay_settings(self) -> dict:
        # 每次从盘读取，重连/重启不会依赖页面或 backend 的临时草稿。
        cfg = load_config()
        self.cfg = cfg
        return {
            "config": cfg.relay.model_dump(mode="json"),
            "status": "draft",
            "capabilities": {"configuration": True, "execution": False, "native_windows": False},
        }

    async def save_relay_settings(self, params: dict) -> dict:
        """先由权限门准备一次确认，再保存服务端封存的配置；失败不更新内存。"""
        pending = getattr(self, "_relay_config_permissions", None)
        if pending is None:
            pending = self._relay_config_permissions = {}
        now = time.monotonic()
        for key, (expires, permission, _draft) in list(pending.items()):
            if expires <= now:
                permission.resolve(Decision.DENY)
                pending.pop(key)

        permission_id = params.get("permission_id")
        if permission_id is not None:
            if not isinstance(permission_id, str) or permission_id not in pending:
                raise ValueError("relay_permission_expired：保存确认已失效，请重新读取配置")
            _expires, permission, draft = pending.pop(permission_id)
            decision = normalize_decision(params.get("decision"))
            # 配置保存不创建永久白名单，只接受本次确认。
            permission.resolve(Decision.ALLOW_ONCE if decision == Decision.ALLOW_ONCE else Decision.DENY)
            if await permission.wait() != Decision.ALLOW_ONCE:
                return {"saved": False, **self.relay_settings()}
            cfg = load_config()
            if cfg.relay.revision != draft.revision:
                raise ValueError("relay_conflict：配置已更新，请重新读取后保存")
            self._validate_relay_providers(draft, cfg)
            draft = draft.model_copy(update={"revision": draft.revision + 1})
            update_config_section("relay", draft.model_dump(mode="json"))
            return {"saved": True, **self.relay_settings()}

        if set(params) != {"config"}:
            raise ValueError("relay_invalid：请提供完整 config 或保存确认")
        if len(json.dumps(params["config"], ensure_ascii=False)) > 128000:
            raise ValueError("relay_invalid：配置过大")
        try:
            draft = RelayConfig.model_validate(params["config"])
        except ValidationError as exc:
            raise ValueError(
                "relay_invalid_or_unsupported：配置无效；本版仅支持无工具 propose 串行草稿，"
                "不支持执行、分叉、汇合或循环。详情：" + str(exc)
            ) from exc
        cfg = load_config()
        if draft.revision != cfg.relay.revision:
            raise ValueError("relay_conflict：配置已更新，请重新读取后保存")
        self._validate_relay_providers(draft, cfg)
        if len(pending) >= 16:
            raise ValueError("relay_busy：待确认过多，请稍后重试")
        # 独立默认门，不继承聊天的完全访问档或项目白名单。
        gate = PermissionGate()
        permission = await gate.authorize(WriteFileTool(), {
            "path": str(config_path()),
            "content": json.dumps({"relay": draft.model_dump(mode="json")}, ensure_ascii=False, indent=2),
        })
        if permission is None:
            raise RuntimeError("relay_permission_error：配置保存未产生确认")
        pending[permission.request_id] = (now + 300, permission, draft)
        return {
            "needs_confirm": True, "permission_id": permission.request_id,
            "message": "将更新本机接力配置草稿；不会启动任务或打开窗口。此设置写入不提供会话回滚。",
            "config": draft.model_dump(mode="json"),
        }

    @staticmethod
    def _validate_relay_providers(draft, cfg) -> None:
        for slot in draft.slots.values():
            if slot.provider and slot.provider not in cfg.providers:
                raise ValueError("relay_invalid：槽位引用了未配置的模型服务")

    # ================= 主副屏交替（2026-10-18 语义修订） =================
    #
    # 适用场景：发布方案 → 执行 → 反馈这类需要两个分工不同的 Agent 交替推进
    # 的任务。主屏 = 用户当前会话，副屏 = 绑定的另一个正常会话；每一次「执行」
    # 让当前轮到的一方跑**完整的一轮正常对话**（Agent / 工具 / 权限门 / 检查点
    # 全部照常），跑完把轮次记进 relay_turns，并把「轮到谁」交给对方。
    #
    # 与上面的无工具串行 A/B 的根本区别：那一版不碰会话、不执行工具；这一版
    # 刻意走 chat 管道，两屏都是真实对话界面，产出直接留在会话历史里。

    def _relay_dual_state(self) -> dict:
        st = getattr(self, "_relay_dual_runtime", None)
        if st is None:
            st = self._relay_dual_runtime = {
                "workers": {},  # pair_id -> asyncio.Task（强引用）
                "current": {},  # pair_id -> {turn_no, side, session_id}
                # pair_id -> asyncio.Lock：run/stop 的「查重 → 状态提交」是
                # check-then-act，并发下必须串行化（否则同一 request_id 会
                # 一方成功一方报冲突，且 stop 可能与 run 交错改状态）
                "locks": {},
            }
        return st

    async def _relay_dual_emit(self, frame: dict) -> None:
        """把接力轮的对话事件广播给本机在线连接（与 _relay_broadcast 同口径）。"""
        emitters = list(getattr(self, "ws_emitters", []))
        connections = list(getattr(self, "ws_connections", []))
        if connections:
            local_ids = {id(c.get("emit")) for c in connections if c.get("local")}
            emitters = [w for w in emitters if id(w) in local_ids]
        for ws_emit in emitters:
            try:
                await ws_emit(frame)
            except Exception:  # noqa: BLE001 - 单个连接失败不影响其余
                pass

    async def _relay_dual_broadcast(self, snapshot: dict) -> None:
        await self._relay_dual_emit({"kind": "relay_pair_state", "relay": snapshot})

    async def _relay_dual_owned(self, pair_id: str) -> dict:
        if self.store is None:
            raise RuntimeError("relay_unavailable：存储不可用")
        pair = await self.store.relay_pair_get(pair_id)
        if pair is None or pair["project_id"] != self._cur_project_id():
            # 不区分「不存在/不属本项目」，避免给枚举探测提供区分信号
            raise ValueError("relay_not_found：接力任务不存在")
        return pair

    async def _relay_dual_payload(self, snapshot: dict) -> dict:
        """对外返回：快照 + 主/副屏会话的名字。"""
        pair = snapshot["pair"]
        main = await self.store.get_session(pair["main_session_id"])
        peer = await self.store.get_session(pair["peer_session_id"])
        return {
            **snapshot,
            "main": {"id": main.id, "title": main.title} if main else None,
            "peer": {"id": peer.id, "title": peer.title} if peer else None,
        }

    # ---- RPC：open / save / get / run / stop ----

    async def relay_dual_open(self, params: dict) -> dict:
        """点开接力页：返回这个主屏会话最近一次的接力。

        create_peer=False（面板自动加载）时只复用已存在的副屏会话，没有就返回
        peer=None——不能因为「启动时右侧恰好停在接力标签」就在用户没点接力时
        凭空建一条空会话；点左侧「接力」会带 create_peer=True。
        """
        self._relay_reject_unknown(params, ("session_id", "create_peer"))
        session_id = self._relay_str(params, "session_id", maxlen=64)
        session = await self._get_owned_session(session_id, local=True)
        project_id = await self._relay_session_project(session)
        pair = await self.store.relay_pair_latest(session_id)
        peer = None
        if pair is not None:
            peer = await self.store.get_session(pair["peer_session_id"])
        if peer is None:
            peer = await self.store.relay_peer_session(
                project_id, _RELAY_PEER_TITLE, create=bool(params.get("create_peer"))
            )
            if peer is not None and pair is not None:
                await self.store.relay_pair_update(
                    pair["id"], {"peer_session_id": peer.id}, bump=False
                )
        snapshot = await self.store.relay_pair_snapshot(pair["id"]) if pair else None
        payload = (
            await self._relay_dual_payload(snapshot) if snapshot else
            {
                "pair": None,
                "turns": [],
                "main": {"id": session.id, "title": session.title},
                "peer": {"id": peer.id, "title": peer.title} if peer else None,
            }
        )
        payload["locked"] = self._relay_state()["locked"]
        return payload

    async def relay_dual_save(self, params: dict) -> dict:
        """保存总任务 / 两屏任务 / 自动交替（只改内容，不推进 revision）
        ——运行中也能改，下一轮生效。"""
        self._relay_reject_unknown(
            params, ("pair_id", "goal", "main_task", "peer_task", "auto_alternate")
        )
        pair_id = self._relay_str(params, "pair_id", maxlen=64)
        await self._relay_dual_owned(pair_id)
        fields: dict = {}
        if "goal" in params:
            fields["goal"] = self._relay_str(params, "goal", required=False, maxlen=8000)
        if "main_task" in params:
            fields["main_task"] = self._relay_str(params, "main_task", required=False, maxlen=4000)
        if "peer_task" in params:
            fields["peer_task"] = self._relay_str(params, "peer_task", required=False, maxlen=4000)
        if "auto_alternate" in params:
            fields["auto_alternate"] = bool(params.get("auto_alternate"))
        if not fields:
            raise ValueError("relay_invalid：没有可保存的字段")
        snapshot = await self.store.relay_pair_update(pair_id, fields, bump=False)
        if snapshot is None:
            raise ValueError("relay_not_found：接力任务不存在")
        await self._relay_dual_broadcast(snapshot)
        return await self._relay_dual_payload(snapshot)

    async def relay_dual_get(self, params: dict) -> dict:
        self._relay_reject_unknown(params, ("pair_id",))
        pair_id = self._relay_str(params, "pair_id", maxlen=64)
        await self._relay_dual_owned(pair_id)
        snapshot = await self.store.relay_pair_snapshot(pair_id)
        if snapshot is None:
            raise ValueError("relay_not_found：接力任务不存在")
        payload = await self._relay_dual_payload(snapshot)
        payload["locked"] = self._relay_state()["locked"]
        return payload

    async def relay_dual_run(self, params: dict) -> dict:
        """执行当前轮到的一方：一轮完整 Agent 对话；完成后把轮次交给对方。"""
        self._relay_reject_unknown(
            params,
            ("session_id", "pair_id", "goal", "main_task", "peer_task",
             "auto_alternate", "request_id", "expected_revision"),
        )
        self._require_relay_lock()
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        session_id = self._relay_str(params, "session_id", maxlen=64)
        session = await self._get_owned_session(session_id, local=True)
        project_id = await self._relay_session_project(session)

        pair: dict | None = None
        if params.get("pair_id"):
            pair_id = self._relay_str(params, "pair_id", maxlen=64)
            pair = await self._relay_dual_owned(pair_id)
            if pair["main_session_id"] != session_id:
                raise ValueError("relay_forbidden：这个接力不属于当前会话")
        else:
            pair = await self.store.relay_pair_latest(session_id)
        if pair is None:
            peer = await self.store.relay_peer_session(project_id, _RELAY_PEER_TITLE)
            created = await self.store.relay_pair_create({
                "id": uuid.uuid4().hex,
                "project_id": project_id,
                "main_session_id": session_id,
                "peer_session_id": peer.id,
                "goal": self._relay_str(params, "goal", required=False, maxlen=8000),
                "main_task": self._relay_str(params, "main_task", required=False, maxlen=4000),
                "peer_task": self._relay_str(params, "peer_task", required=False, maxlen=4000),
                "auto_alternate": bool(params.get("auto_alternate")),
                "state": "idle",
                "current_side": "main",
            })
            pair = created["pair"]
        elif await self.store.get_session(pair["peer_session_id"]) is None:
            # 副屏会话被用户删了：换一个新的副屏再跑，别把消息写进不存在的会话
            peer = await self.store.relay_peer_session(project_id, _RELAY_PEER_TITLE)
            await self.store.relay_pair_update(
                pair["id"], {"peer_session_id": peer.id}, bump=False
            )
            pair = await self.store.relay_pair_get(pair["id"]) or pair

        async with self._relay_dual_lock(pair["id"]):
            return await self._relay_dual_run_locked(
                params, pair, request_id, expected_revision
            )

    async def _relay_dual_run_locked(
        self, params: dict, pair: dict, request_id: str, expected_revision: int,
    ) -> dict:
        # 幂等：同一 request_id 重复执行返回同一份快照，不重复开轮
        replay = await self.store.relay_action_get(f"dual:{pair['id']}", request_id)
        if replay is not None:
            return {**replay, "replayed": True}
        if pair["state"] == "running":
            raise ValueError("relay_busy：正在执行，等这一轮结束或先点中断")
        if expected_revision and int(pair["revision"]) != expected_revision:
            raise ValueError("relay_conflict：接力状态已更新，请刷新后再执行")

        fields: dict = {}
        for key, limit in (("goal", 8000), ("main_task", 4000), ("peer_task", 4000)):
            if key in params:
                fields[key] = self._relay_str(params, key, required=False, maxlen=limit)
        if "auto_alternate" in params:
            fields["auto_alternate"] = bool(params.get("auto_alternate"))
        goal = str(fields.get("goal", pair["goal"]) or "").strip()
        main_task = str(fields.get("main_task", pair["main_task"]) or "").strip()
        peer_task = str(fields.get("peer_task", pair["peer_task"]) or "").strip()
        if not goal:
            raise ValueError("relay_invalid：请先填写总任务")
        if not main_task:
            raise ValueError("relay_invalid：请先填写主屏每一轮做什么")
        if not peer_task:
            raise ValueError("relay_invalid：请先填写副屏每一轮做什么")
        fields.update({"goal": goal, "main_task": main_task, "peer_task": peer_task})
        fields["state"] = "running"
        fields["error"] = {}
        snapshot = await self.store.relay_pair_update(
            pair["id"], fields, expect_states=("idle", "paused", "interrupted"),
            expected_revision=pair["revision"],
        )
        if snapshot is None:
            raise ValueError("relay_conflict：接力状态已更新，请刷新后再执行")
        self._relay_dual_spawn(pair["id"])
        await self.store.relay_action_put(f"dual:{pair['id']}", request_id, "run", snapshot)
        await self._relay_dual_broadcast(snapshot)
        return await self._relay_dual_payload(snapshot)

    async def relay_dual_stop(self, params: dict) -> dict:
        """中断：取消正在跑的那一轮（连同其中正在执行的工具），任务停在可继续处。"""
        self._relay_reject_unknown(
            params, ("pair_id", "request_id", "expected_revision")
        )
        self._require_relay_lock()
        pair_id = self._relay_str(params, "pair_id", maxlen=64)
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        pair = await self._relay_dual_owned(pair_id)
        # 与 run 同一把按 pair 的锁：避免「stop 判定 worker 在跑 → run 恰好提交
        # 新一轮 → stop 把新一轮也一并取消」这类交错。
        async with self._relay_dual_lock(pair_id):
            return await self._relay_dual_stop_locked(
                pair, request_id, expected_revision
            )

    async def _relay_dual_stop_locked(
        self, pair: dict, request_id: str, expected_revision: int,
    ) -> dict:
        pair_id = pair["id"]
        replay = await self.store.relay_action_get(f"dual-stop:{pair_id}", request_id)
        if replay is not None:
            return {**replay, "replayed": True}
        if expected_revision and int(pair["revision"]) != expected_revision:
            raise ValueError("relay_conflict：接力状态已更新，请刷新后再中断")

        st = self._relay_dual_state()
        current = st["current"].get(pair_id)
        if current:
            self.cancel_run(current["session_id"])
        task = st["workers"].get(pair_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001 - 收尾异常不影响中断本身
                pass
        snapshot = await self.store.relay_pair_snapshot(pair_id)
        if snapshot is not None:
            # worker 被取消后通常已把本轮记成 cancelled、状态落到 idle；
            # 手动中断要显示成「已中断」，所以这里把 cancelled 的收尾统一
            # 抬成 paused（仍可点执行继续）。若本轮恰好已成功完成（最后一条
            # turn 是 done），保持 idle——不能把刚跑完的任务误标成中断。
            turns = snapshot.get("turns") or []
            last = turns[-1] if turns else None
            if snapshot["pair"]["state"] == "running" or (
                last is not None and last["status"] == "cancelled"
            ):
                updated = await self.store.relay_pair_update(
                    pair_id,
                    {"state": "paused",
                     "error": {"code": "relay_paused", "message": "已手动中断"}},
                    expect_states=("idle", "running"),
                )
                if updated is not None:
                    snapshot = updated
        if snapshot is None:
            raise ValueError("relay_not_found：接力任务不存在")
        await self.store.relay_action_put(f"dual-stop:{pair_id}", request_id, "stop", snapshot)
        await self._relay_dual_broadcast(snapshot)
        return await self._relay_dual_payload(snapshot)

    # ---- worker ----

    def _relay_dual_lock(self, pair_id: str):
        """按 pair 串行化 run/stop 的「查重 → 状态提交」临界区（不覆盖模型调用）。"""
        st = self._relay_dual_state()
        lock = st["locks"].get(pair_id)
        if lock is None:
            lock = st["locks"][pair_id] = asyncio.Lock()
        return lock

    def _relay_dual_spawn(self, pair_id: str) -> None:
        st = self._relay_dual_state()
        existing = st["workers"].get(pair_id)
        if existing is not None and not existing.done():
            return
        worker = asyncio.create_task(self._relay_dual_worker(pair_id))
        st["workers"][pair_id] = worker

    async def _relay_dual_worker(self, pair_id: str) -> None:
        st = self._relay_dual_state()
        turn_no = 0
        sid = ""
        try:
            while True:
                pair = await self.store.relay_pair_get(pair_id)
                if pair is None or pair["state"] != "running":
                    return
                side = "peer" if pair["current_side"] == "peer" else "main"
                sid = pair["peer_session_id"] if side == "peer" else pair["main_session_id"]
                label = "副屏" if side == "peer" else "主屏"
                task_text = (pair["peer_task"] if side == "peer" else pair["main_task"]).strip()
                if not task_text:
                    snapshot = await self.store.relay_pair_finish_turn(
                        pair_id, turn_no, status="error",
                        error="这一方的任务为空，已暂停",
                    )
                    if snapshot is not None:
                        await self._relay_dual_broadcast(snapshot)
                    return
                prev = await self._relay_dual_prev_output(pair_id, side)
                brief = _dual_brief(pair["goal"], label, task_text,
                                    "主屏" if side == "peer" else "副屏", prev)
                started = await self.store.relay_pair_start_turn(
                    pair_id, side, brief, expected_revision=pair["revision"]
                )
                if started is None:
                    await asyncio.sleep(0.05)
                    continue  # 状态/版本刚被改掉：回到循环顶部重新判定
                turn_no = started["pair"]["turn_no"]
                st["current"][pair_id] = {
                    "turn_no": turn_no, "side": side, "session_id": sid,
                }
                await self._relay_dual_broadcast(started)
                try:
                    result = await self.send(
                        brief, self._relay_dual_emit, session_id=sid, local=True
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单轮失败落成可读状态
                    logger.warning("接力轮次失败（%s/%s）：%s", pair_id, side, exc)
                    snapshot = await self.store.relay_pair_finish_turn(
                        pair_id, turn_no, status="error", error=str(exc)[:1000]
                    )
                    if snapshot is not None:
                        await self._relay_dual_broadcast(snapshot)
                    return
                stopped = bool(isinstance(result, dict) and result.get("stopped"))
                output = "" if stopped else await self._relay_dual_last_output(sid)
                if not stopped and not output.strip():
                    # 空产出不能当作「完成」：连续交替时会空转、白烧预算
                    snapshot = await self.store.relay_pair_finish_turn(
                        pair_id, turn_no, status="error",
                        error="这一轮没有产出文本，已暂停",
                    )
                    if snapshot is not None:
                        await self._relay_dual_broadcast(snapshot)
                    return
                fresh = await self.store.relay_pair_get(pair_id)
                auto = bool(fresh["auto_alternate"]) if fresh is not None else False
                next_side = "main" if side == "peer" else "peer"
                snapshot = await self.store.relay_pair_finish_turn(
                    pair_id, turn_no,
                    status="cancelled" if stopped else "done",
                    output=output,
                    next_side=None if stopped else next_side,
                    auto=auto,
                )
                if snapshot is not None:
                    await self._relay_dual_broadcast(snapshot)
                if stopped or not auto:
                    return
                # 连续交替：pair 仍是 running，current_side 已翻到对方，继续下一轮
        except asyncio.CancelledError:
            await self._relay_dual_abort(pair_id, turn_no, sid)
            raise
        finally:
            # 只清理自己：stop 超时后用户可能已重新 run 起新 worker，
            # 无条件 pop 会把后继 worker 从注册表里抹掉（之后 stop 找不到它）
            me = asyncio.current_task()
            if st["workers"].get(pair_id) is me:
                st["workers"].pop(pair_id, None)
                st["current"].pop(pair_id, None)

    async def _relay_dual_abort(self, pair_id: str, turn_no: int, sid: str) -> None:
        """worker 被取消时把「正在跑的那一轮 / 运行态」落成可读的停止状态。"""
        snapshot = None
        try:
            if turn_no:
                output = await self._relay_dual_last_output(sid) if sid else ""
                snapshot = await self.store.relay_pair_finish_turn(
                    pair_id, turn_no, status="cancelled", output=output
                )
            else:
                snapshot = await self.store.relay_pair_update(
                    pair_id, {"state": "paused"}, expect_states=("running",)
                )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾尽力而为
            snapshot = None
        if snapshot is not None:
            try:
                await self._relay_dual_broadcast(snapshot)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _relay_dual_prev_output(self, pair_id: str, side: str) -> str:
        """对方最近一次成功的输出（交接材料）；没有就返回空串。"""
        snapshot = await self.store.relay_pair_snapshot(pair_id)
        if not snapshot:
            return ""
        for turn in reversed(snapshot["turns"]):
            if turn["side"] == side or turn["status"] != "done":
                continue
            text = (turn["output"] or "").strip()
            if not text:
                continue
            if len(text) > _RELAY_DUAL_PREV_CHARS:
                text = text[:_RELAY_DUAL_PREV_CHARS] + "\n……（后文已截断）"
            return text
        return ""

    async def _relay_dual_last_output(self, session_id: str) -> str:
        """刚跑完那一轮的助手回复（落库后的最后一条 assistant 文本）。"""
        try:
            messages = await self.store.load_messages(session_id)
        except Exception:  # noqa: BLE001 - 读不到就当空产出
            return ""
        for message in reversed(messages):
            if getattr(message, "role", "") == "assistant":
                text = (getattr(message, "text", "") or "").strip()
                if text:
                    return text
        return ""

    async def _relay_dual_shutdown(self) -> None:
        st = self._relay_dual_state()
        for task in list(st["workers"].values()):
            if task is not None and not task.done():
                task.cancel()
        workers = [t for t in st["workers"].values() if t is not None and not t.done()]
        if workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True), timeout=5.0
                )
            except Exception:  # noqa: BLE001 - 收尾不阻断关机
                pass
        st["workers"].clear()
        st["current"].clear()

    async def _relay_session_project(self, session) -> int | None:
        """接力归属：任务挂当前项目；快聊会话按当前项目收编，跨项目会话拒绝。"""
        current = self._cur_project_id()
        if session.project_id is not None and session.project_id != current:
            proj = await self.store.get_project(session.project_id)
            name = (proj.name if proj is not None else "") or "其他项目"
            raise ValueError(
                f"relay_forbidden：当前会话属于项目「{name}」，请先切换到该项目再启动接力"
            )
        return current

    #
    # 本版仅实现「无工具串行 A/B」闭环——每节点一次 provider.stream(messages, [])，
    # 报告完成后按 require_confirmation 人工批准再提交下一节点。以下均为本机
    # （local_only）RPC，绝不启动 Agent、绝不执行工具、绝不创建普通会话
    # （防止劫持用户会话）。

    # ---- 运行期状态与独占锁 ----

    def _relay_state(self) -> dict:
        st = getattr(self, "_relay_runtime", None)
        if st is None:
            st = self._relay_runtime = {
                "instance": uuid.uuid4().hex,
                "workers": {},   # task_id -> asyncio.Task（强引用）
                "locks": {},     # task_id -> asyncio.Lock
                # project_id -> asyncio.Lock：relay_start 的「查重→建任务」是
                # check-then-act，并发下必须串行化，否则会留下两条非终态任务
                "start_locks": {},
                "locked": False,
                "lock_fh": None,
                "closing": False,
            }
        return st

    def _relay_lock_path(self):
        path = getattr(self.store, "path", None)
        return None if path is None else path.with_suffix(path.suffix + ".relay.lock")

    def _relay_acquire_exclusive(self) -> bool:
        """尝试取得该数据目录的接力独占运行权。

        同进程注册表挡住「同一进程内第二个后端」，文件锁挡住跨进程。取不到即
        说明已有活后端：本后端不执行启动恢复、不允许 relay.start（只读 list/get
        仍可用），从而不会把别的进程正在跑的活任务打断。
        """
        st = self._relay_state()
        if st["locked"]:
            return True  # 同一后端重复 setup 不重复加锁（避免自锁/句柄泄漏）
        path = getattr(self.store, "path", None)
        if path is None:
            return False
        key = os.path.normcase(str(path))
        holder = _RELAY_LOCKS.get(key)
        if holder is not None and holder != st["instance"]:
            return False
        fh = None
        try:
            fh = open(self._relay_lock_path(), "a+b")
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fh is not None:
                fh.close()
            return False
        _RELAY_LOCKS[key] = st["instance"]
        st["lock_fh"] = fh
        return True

    def _relay_release_exclusive(self) -> None:
        st = self._relay_state()
        fh = st.get("lock_fh")
        st["lock_fh"] = None
        path = getattr(self.store, "path", None)
        if path is not None:
            key = os.path.normcase(str(path))
            if _RELAY_LOCKS.get(key) == st["instance"]:
                _RELAY_LOCKS.pop(key, None)
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def _require_relay_lock(self) -> None:
        if not self._relay_state()["locked"]:
            raise RuntimeError(
                "relay_locked：另一个 SkySheep 后端正在使用该数据目录，接力运行被独占锁拒绝"
            )

    async def _relay_startup(self) -> None:
        """setup 尾部调用：取独占锁；持锁者才做启动恢复。"""
        st = self._relay_state()
        st["locked"] = self._relay_acquire_exclusive()
        if not st["locked"] or self.store is None:
            return
        n = await self.store.relay_mark_interrupted(st["instance"])
        if n:
            logger.info("接力：%d 个非终态任务因上次退出被标记为中断，需显式恢复", n)

    async def _relay_shutdown(self) -> None:
        """shutdown 中调用：优先停止并等待 worker 收尾，再释放独占锁。"""
        await self._relay_dual_shutdown()
        st = self._relay_state()
        for t in list(st["workers"].values()):
            if t and not t.done():
                t.cancel()
        workers = [t for t in st["workers"].values() if t and not t.done()]
        if workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True), timeout=5.0
                )
            except Exception:  # noqa: BLE001 - 收尾不阻断关机
                pass
        st["workers"].clear()
        self._relay_release_exclusive()
        st["locked"] = False

    def _relay_worker_live(self, task_id: str) -> bool:
        t = self._relay_state()["workers"].get(task_id)
        return bool(t and not t.done())

    def _relay_spawn(self, task_id: str) -> None:
        st = self._relay_state()
        existing = st["workers"].get(task_id)
        if existing and not existing.done():
            return
        worker = asyncio.create_task(self._relay_worker(task_id))

        def _cleanup(_t, tid=task_id):
            if st["workers"].get(tid) is worker:
                st["workers"].pop(tid, None)
            # 必须用 create_task 把清理逻辑挂到事件循环，因为 add_done_callback
            # 是同步的，而清理涉及 DB 读写。提前退出可能造成「stopping」死锁。
            asyncio.create_task(self._relay_worker_cleanup(tid))

        st["workers"][task_id] = worker
        worker.add_done_callback(_cleanup)

    async def _relay_finalize_stopping(self, task_id: str) -> None:
        """worker 已退出但任务仍停在 stopping 时补做收尾（幂等，CAS 保护）。

        relay_stop 按「worker 是否还活着」决定 stopping/stopped：若判断时
        worker 还活着、随后 worker 恰好在同一轮退出，就没人再执行
        stopping→stopped 的提交。留一条死任务比误推进一步更糟，所以这里兜底。
        """
        try:
            current = await self.store.relay_get_task(task_id)
            if current is None or current["state"] != "stopping":
                return
            result = await self.store.relay_mutate(
                task_id, expect_states=("stopping",), new_state="stopped"
            )
            if result is not None:
                await self._relay_broadcast(result)
        except Exception as exc:  # noqa: BLE001 - 收尾失败只留日志，不打瘫后端
            logger.warning("接力收尾 stopping→stopped 失败（%s）：%s", task_id, exc)

    async def _relay_broadcast(self, snapshot: dict) -> None:
        """把任务快照广播给**本机**在线连接。

        接力控制 RPC 全部 local_only，任务快照里含用户目标与节点报告，不能
        因为事件没有 session_id 就落到远端连接的通用广播里（远端只允许聊天，
        不允许看到本机接力内容）。
        """
        emitters = list(getattr(self, "ws_emitters", []))
        connections = list(getattr(self, "ws_connections", []))
        if connections:
            local_ids = {id(c.get("emit")) for c in connections if c.get("local")}
            emitters = [w for w in emitters if id(w) in local_ids]
        frame = {"kind": "relay_state", "relay": snapshot}
        for ws_emit in emitters:
            try:
                await ws_emit(frame)
            except Exception:  # noqa: BLE001 - 单个连接失败不影响其余
                pass

    # ---- 参数与归属校验 ----

    @staticmethod
    def _relay_reject_unknown(params: dict, allowed: tuple[str, ...]) -> None:
        extra = set(params) - set(allowed)
        if extra:
            raise ValueError("relay_invalid：不支持的参数 " + ", ".join(sorted(extra)))

    @staticmethod
    def _relay_str(params: dict, key: str, *, required: bool = True, maxlen: int = 4000) -> str:
        raw = params.get(key, "")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise ValueError(f"relay_invalid：{key} 必须是字符串")
        raw = raw.strip()
        if required and not raw:
            raise ValueError(f"relay_invalid：缺少 {key}")
        if len(raw) > maxlen:
            raise ValueError(f"relay_invalid：{key} 过长")
        return raw

    @staticmethod
    def _relay_request_id(params: dict) -> str:
        rid = params.get("request_id")
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError("relay_invalid：缺少 request_id（控制命令必须携带幂等键）")
        return rid.strip()

    @staticmethod
    def _relay_revision(params: dict) -> int:
        rev = params.get("expected_revision")
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
            raise ValueError("relay_invalid：expected_revision 必须是非负整数")
        return rev

    async def _relay_owned_task(self, task_id: str) -> dict:
        if self.store is None:
            raise RuntimeError("relay_unavailable：存储不可用")
        task = await self.store.relay_get_task(task_id)
        if task is None:
            raise ValueError("relay_not_found：接力任务不存在")
        if task["project_id"] != self._cur_project_id():
            # 不区分「不存在/不属本项目」，避免给枚举探测提供区分信号
            raise ValueError("relay_not_found：接力任务不属于当前项目")
        return task

    async def _relay_owned_snapshot(self, task_id: str) -> dict:
        await self._relay_owned_task(task_id)
        snap = await self.store.relay_get(task_id)
        if snap is None:
            raise ValueError("relay_not_found：接力任务不存在")
        return snap

    # ---- RPC：list / get / start ----

    async def relay_list(self, params: dict) -> dict:
        self._relay_reject_unknown(params, ("limit",))
        if self.store is None:
            raise RuntimeError("relay_unavailable：存储不可用")
        limit = params.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("relay_invalid：limit 必须是 1..100 的整数")
        tasks = await self.store.relay_list_tasks(self._cur_project_id(), limit)
        return {"tasks": tasks, "locked": self._relay_state()["locked"]}

    async def relay_get(self, params: dict) -> dict:
        self._relay_reject_unknown(params, ("task_id",))
        task_id = self._relay_str(params, "task_id", maxlen=64)
        return await self._relay_owned_snapshot(task_id)

    def _relay_start_lock(self, project_id):
        """按项目串行化 relay_start 的「查重 → 建任务」临界区。

        relay_action_get / relay_list_tasks / relay_create 是三次独立 IO，两个
        并发 start（不同 request_id）都可能在「查无活任务」处放行，各自建出
        一条非终态任务——之后谁都启动不了。锁只护这一段，不覆盖模型调用。
        """
        st = self._relay_state()
        lock = st["start_locks"].get(project_id)
        if lock is None:
            lock = st["start_locks"][project_id] = asyncio.Lock()
        return lock

    async def relay_start(self, params: dict) -> dict:
        self._relay_reject_unknown(
            params, ("session_id", "goal", "workflow", "request_id", "expected_revision")
        )
        self._require_relay_lock()
        if self.store is None:
            raise RuntimeError("relay_unavailable：存储不可用")
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        if expected_revision != 0:
            raise ValueError("relay_conflict：启动新任务请传 expected_revision=0")
        goal = self._relay_str(params, "goal", maxlen=8000)
        session_id = self._relay_str(params, "session_id", maxlen=64)
        session = await self._get_owned_session(session_id, local=True)
        # 接力任务按当前项目归属（relay.list / 归属校验都看它）；快聊会话按
        # 当前项目收编，跨项目会话拒绝并指出该切到哪个项目。
        project_id = await self._relay_session_project(session)

        # 同一项目的并发 start 必须串行：幂等查重与「每项目一个非终态任务」
        # 都是 check-then-act，交错执行会各自放行并留下两条活任务。
        async with self._relay_start_lock(project_id):
            return await self._relay_start_locked(
                params, request_id, goal, session_id, project_id
            )

    async def _relay_start_locked(
        self, params: dict, request_id: str, goal: str,
        session_id: str, project_id,
    ) -> dict:
        # request_id 幂等：同一项目重复 start 返回同一任务快照，不重复建任务
        scope = f"start:{project_id}"
        replay = await self.store.relay_action_get(scope, request_id)
        if replay is not None:
            return {**replay, "replayed": True}

        # 每项目最多一个非终态接力任务
        active = [
            t for t in await self.store.relay_list_tasks(project_id, limit=100)
            if t["state"] not in RELAY_TERMINAL_STATES
        ]
        if active:
            raise ValueError("relay_conflict：当前项目已有未结束的接力任务，请先停止或结束它")

        cfg = self.cfg or load_config()
        if params.get("workflow") is not None:
            try:
                workflow = RelayWorkflow.model_validate(params["workflow"])
            except ValidationError as exc:
                raise ValueError("relay_invalid：工作流无效：" + str(exc)) from exc
        else:
            workflow = cfg.relay.workflow

        # 起始节点标记为「已授权」（用户通过 start 显式授权首个节点执行）
        ordered = _ordered(workflow)
        nodes = []
        for i, node_id in enumerate(ordered):
            node = next(n for n in workflow.nodes if n.id == node_id)
            nodes.append({
                "node_id": node.id,
                "slot": node.slot,
                "ordinal": i,
                "status": "authorized" if i == 0 else "pending",
                "terminal": node.terminal,
                "prompt": node.prompt,
            })

        bundle = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "session_id": session_id,
            "state": "running",
            "current_node": ordered[0],
            "active_slot": nodes[0]["slot"],
            "generation": 0,
            "revision": 0,
            "instruction_revision": 0,
            "step": 0,
            "owner_instance": self._relay_state()["instance"],
            "goal": goal,
            "config": cfg.relay.model_dump(mode="json"),
            "model_snapshot": self._relay_model_snapshot(cfg),
            "error": {},
        }
        # 本次 start 覆写的工作流一并封存（config_snapshot 取实际生效值）
        bundle["config"]["workflow"] = workflow.model_dump(mode="json")
        snapshot = await self.store.relay_create(bundle, nodes)

        # 幂等登记放在创建成功之后：重复 request_id 直接返回这份快照
        await self.store.relay_action_put(scope, request_id, "start", snapshot)
        self._relay_spawn(bundle["id"])
        await self._relay_broadcast(snapshot)
        return snapshot

    # ---- RPC：approve / pause / resume / stop ----

    async def relay_approve(self, params: dict) -> dict:
        self._relay_reject_unknown(
            params, ("task_id", "request_id", "expected_revision", "report_id", "report_hash")
        )
        self._require_relay_lock()
        task_id = self._relay_str(params, "task_id", maxlen=64)
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        report_id = self._relay_str(params, "report_id", required=False, maxlen=200)
        rhash = self._relay_str(params, "report_hash", required=False, maxlen=128)

        # 幂等：重复 request_id 直接返回已存结果（不受当前状态影响，不重复推进）
        replay = await self.store.relay_action_get(task_id, request_id)
        if replay is not None:
            return {**replay, "replayed": True}
        task = await self._relay_owned_task(task_id)
        if task["state"] != "waiting_confirmation":
            raise ValueError("relay_conflict：任务当前不在等待确认状态")
        snapshot = await self._relay_owned_snapshot(task_id)
        node = _node_of(snapshot, task["current_node"])
        if node is None:
            raise ValueError("relay_internal：当前节点缺失")

        if node["status"] == "reported":
            # 批准一个已产出报告的节点：必须绑定该报告的 id 与内容哈希
            if not report_id or not rhash:
                raise ValueError("relay_invalid：批准报告必须带 report_id 与 report_hash")
            if report_id != node["report_id"] or rhash != node["report_hash"]:
                raise ValueError("relay_conflict：报告已变化或哈希不符，请重新读取后确认")
            nxt = _next_node(snapshot, node["node_id"])
            if nxt is None:
                result = await self.store.relay_mutate(
                    task_id, request_id=request_id, action="approve",
                    expect_states=("waiting_confirmation",), expected_revision=expected_revision,
                    new_state="completed", node_id=node["node_id"], node_status="confirmed",
                )
            else:
                result = await self.store.relay_mutate(
                    task_id, request_id=request_id, action="approve",
                    expect_states=("waiting_confirmation",), expected_revision=expected_revision,
                    new_state="running", current_node=nxt["node_id"], active_slot=nxt["slot"],
                    node_id=node["node_id"], node_status="confirmed",
                )
                # 目标节点先「授权」：提交成功后才由 worker 启动（事务先行）。
                # 这里同样按上一笔提交后的 revision 做 CAS——并发 stop/approve
                # 插进来时不能凭旧快照把节点授权进已变化的任务。
                if result is not None:
                    result = await self.store.relay_mutate(
                        task_id, expected_revision=result["task"]["revision"],
                        node_id=nxt["node_id"], node_status="authorized",
                    )
        elif node["status"] in ("pending", "running", "failed"):
            # 显式授权/重试一个未产出报告的节点：绑定为空（不冒充已确认的报告）
            if report_id or rhash:
                raise ValueError("relay_invalid：该节点尚无报告，不应携带 report_id/report_hash")
            if node["status"] == "running" and self._relay_worker_live(task_id):
                raise ValueError("relay_busy：当前节点正在执行，请先暂停后再显式重试")
            result = await self.store.relay_mutate(
                task_id, request_id=request_id, action="approve_retry",
                expect_states=("waiting_confirmation",), expected_revision=expected_revision,
                new_state="running", error={},
                node_id=node["node_id"], node_status="authorized",
            )
        else:
            raise ValueError("relay_conflict：节点状态不允许批准")

        if result is None:
            raise ValueError("relay_conflict：任务已变化，请重新读取后再确认")
        self._relay_spawn(task_id)
        await self._relay_broadcast(result)
        return result

    async def relay_pause(self, params: dict) -> dict:
        self._relay_reject_unknown(params, ("task_id", "request_id", "expected_revision"))
        self._require_relay_lock()
        task_id = self._relay_str(params, "task_id", maxlen=64)
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        await self._relay_owned_task(task_id)
        replay = await self.store.relay_action_get(task_id, request_id)
        if replay is not None:
            return {**replay, "replayed": True}
        result = await self.store.relay_mutate(
            task_id, request_id=request_id, action="pause",
            expect_states=("running", "waiting_confirmation"), expected_revision=expected_revision,
            new_state="paused", error={"code": "relay_paused", "message": "已手动暂停"},
        )
        if result is None:
            raise ValueError("relay_conflict：任务已变化，请重新读取后再暂停")
        await self._relay_broadcast(result)
        return result

    async def relay_resume(self, params: dict) -> dict:
        self._relay_reject_unknown(params, ("task_id", "request_id", "expected_revision"))
        self._require_relay_lock()
        task_id = self._relay_str(params, "task_id", maxlen=64)
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        await self._relay_owned_task(task_id)
        replay = await self.store.relay_action_get(task_id, request_id)
        if replay is not None:
            return {**replay, "replayed": True}
        result = await self.store.relay_mutate(
            task_id, request_id=request_id, action="resume",
            expect_states=("paused", "interrupted"), expected_revision=expected_revision,
            new_state="running", bump_generation=True, error={},
        )
        if result is None:
            raise ValueError("relay_conflict：任务状态不允许恢复，请重新读取")
        self._relay_spawn(task_id)
        await self._relay_broadcast(result)
        return result

    async def relay_stop(self, params: dict) -> dict:
        self._relay_reject_unknown(params, ("task_id", "request_id", "expected_revision"))
        self._require_relay_lock()
        task_id = self._relay_str(params, "task_id", maxlen=64)
        request_id = self._relay_request_id(params)
        expected_revision = self._relay_revision(params)
        await self._relay_owned_task(task_id)
        replay = await self.store.relay_action_get(task_id, request_id)
        if replay is not None:
            return {**replay, "replayed": True}
        target = "stopping" if self._relay_worker_live(task_id) else "stopped"
        result = await self.store.relay_mutate(
            task_id, request_id=request_id, action="stop",
            expect_states=("running", "waiting_confirmation", "paused", "interrupted", "stopping"),
            expected_revision=expected_revision, new_state=target,
        )
        if result is None:
            raise ValueError("relay_conflict：任务已变化，请重新读取后再停止")
        await self._relay_broadcast(result)
        return result

    # ---- worker：无工具串行推进 ----

    async def _relay_worker(self, task_id: str) -> None:
        try:
            while True:
                snapshot = await self.store.relay_get(task_id)
                if snapshot is None:
                    return
                task = snapshot["task"]
                state = task["state"]
                if state in RELAY_TERMINAL_STATES:
                    return
                if state == "stopping":
                    await self.store.relay_mutate(
                        task_id, expect_states=("stopping",), new_state="stopped"
                    )
                    done = await self.store.relay_get(task_id)
                    if done:
                        await self._relay_broadcast(done)
                    return
                if state != "running":
                    return  # paused / interrupted / waiting_confirmation：等外部指令
                node = _node_of(snapshot, task["current_node"])
                if node is None:
                    await self._relay_park(task_id, "relay_internal", "当前节点缺失")
                    return
                status = node["status"]
                if status == "confirmed":
                    nxt = _next_node(snapshot, node["node_id"])
                    if nxt is None:
                        await self._relay_finish(task_id, task["revision"])
                        return
                    result = await self.store.relay_mutate(
                        task_id, expect_states=("running",), expected_revision=task["revision"],
                        current_node=nxt["node_id"], active_slot=nxt["slot"],
                        node_id=nxt["node_id"], node_status="authorized",
                    )
                    if result is not None:
                        await self._relay_broadcast(result)
                    continue
                if status == "reported":
                    if node["terminal"]:
                        await self._relay_finish(task_id, task["revision"])
                    else:
                        await self._relay_park(
                            task_id, "relay_await_confirm",
                            "节点报告已完成，等待人工批准后推进",
                            new_state="waiting_confirmation", expected_revision=task["revision"],
                        )
                    return
                if status == "authorized":
                    # 不直接 return：节点执行期间可能收到 stop（state=stopping），
                    # 必须回到循环顶部把 stopping 收尾成 stopped，否则任务永远停在
                    # 「正在停止」。其余终态/等待态会在下一次迭代顶部自然退出。
                    await self._relay_run_node(task_id, task, node)
                    continue
                # pending / running（可能被暂停打断过）/ failed：绝不自动重跑
                await self._relay_park(
                    task_id, "relay_await_authorization",
                    "节点尚未完成执行，需显式批准后才会运行",
                    new_state="waiting_confirmation", expected_revision=task["revision"],
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker 崩了也要把任务落到可读状态
            logger.warning("接力 worker 异常（%s）：%s", task_id, exc)
            await self._relay_park(task_id, "relay_error", f"接力内部错误：{exc}")

    async def _relay_worker_cleanup(self, task_id: str) -> None:
        """worker 退出后的统一兜底（无论是正常 return、异常还是被取消）。

        如果任务卡在 stopping 状态（例如正在跑 _relay_run_node 时收到 stop，
        但在重新进入循环顶部处理 stopping 前抛错退出了），这里强制把它落定。
        """
        try:
            current = await self.store.relay_get_task(task_id)
            if current is None or current["state"] != "stopping":
                return
            result = await self.store.relay_mutate(
                task_id, expect_states=("stopping",), new_state="stopped"
            )
            if result is not None:
                await self._relay_broadcast(result)
        except Exception as exc:  # noqa: BLE001 - 收尾失败只留日志，不打瘫后端
            logger.warning("接力 worker 收尾异常（%s）：%s", task_id, exc)

    async def _relay_run_node(self, task_id: str, task: dict, node: dict) -> bool:
        """运行一个已授权节点。返回 True 表示 worker 应就此退出（停止/让出）。"""
        cfg = task["config"]
        workflow = RelayWorkflow.model_validate(cfg.get("workflow") or {})
        capacity = self._relay_capacity(task, node)
        threshold, reserve = compute_budget(
            capacity, cfg.get("handoff_ratio", 0.75),
            cfg.get("reserve_tokens", 8192), cfg.get("recent_tokens", 4096),
        )
        if threshold <= 0:
            await self._relay_park(task_id, "relay_budget",
                                   "上下文容量不足以支撑接力所需的最小预算")
            return True
        if task["step"] >= workflow.max_steps:
            await self._relay_park(task_id, "relay_max_steps",
                                   f"已达最大步数（{workflow.max_steps}），已暂停")
            return True
        # 有限尝试：节点已试满 max_retries 次不再自动重跑，需人工处理
        if int(node["attempt"]) >= workflow.max_retries:
            await self._relay_park(
                task_id, "relay_max_retries",
                f"节点已达最大尝试次数（{workflow.max_retries}），已暂停等待人工处理",
            )
            return True

        snapshot = await self.store.relay_get(task_id)
        assert snapshot is not None
        prior = _reports_of(snapshot, upto=node["ordinal"])
        packet = build_packet(
            task_id=task_id,
            goal=task["goal"],
            completed=prior,
            recent_messages=[c["summary"] for c in prior if c.get("summary")][-_RELAY_RECENT_CONTEXT:],
            budget={"capacity": capacity, "reserve": reserve},
        )
        role = (cfg.get("slots", {}).get(node["slot"]) or {}).get("role", node["slot"])
        messages = build_node_messages(role, node["prompt"], packet_to_text(packet))

        est_input = estimate_tokens(messages)
        reserved_output = min(reserve, max(1024, _RELAY_MAX_OUTPUT_CHARS // 4))
        if est_input + reserve > capacity:
            await self._relay_park(task_id, "relay_context_budget",
                                   "下一节点预估输入超出上下文余量，已暂停")
            return True
        used = sum(
            int(n["usage"].get("input_tokens", 0)) + int(n["usage"].get("output_tokens", 0))
            for n in snapshot["nodes"]
        )
        if used + est_input + reserved_output > workflow.token_budget:
            await self._relay_park(task_id, "relay_token_budget",
                                   f"已达 token 预算（{workflow.token_budget}），已暂停")
            return True

        built = self._relay_build_provider(task, node)
        if built is None:
            await self._relay_park(task_id, "relay_provider_error", "节点模型服务不可用")
            return True
        provider, owned = built

        # 标记 running：拿到本轮 epoch 后才能判断迟到返回
        running = await self.store.relay_mutate(
            task_id, expect_states=("running",), expected_revision=task["revision"],
            node_id=node["node_id"], node_status="running",
            node_attempt=int(node["attempt"]) + 1,
        )
        if running is None:
            if owned:
                await _safe_aclose(provider)
            return False
        epoch = (running["task"]["revision"], running["task"]["generation"])

        try:
            result = await run_node_text(
                provider, messages,
                timeout_s=cfg.get("phase_timeout_seconds", 90),
                max_output_chars=_RELAY_MAX_OUTPUT_CHARS,
            )
        except RelayProtocolError as exc:
            await self._relay_park(task_id, "relay_protocol_error", str(exc))
            return True
        except RelayLimitError as exc:
            await self._relay_park(task_id, exc.code, exc.message)
            return True
        except ProviderError as exc:
            await self._relay_park(task_id, "relay_provider_error", f"模型服务错误：{exc}")
            return True
        finally:
            if owned:
                await _safe_aclose(provider)

        # 迟到/被打断的返回不得推进：状态离开 running 或 revision/generation 变化即丢弃
        fresh = await self.store.relay_get_task(task_id)
        if (fresh is None or fresh["state"] != "running"
                or (fresh["revision"], fresh["generation"]) != epoch):
            return True

        text = result["text"].strip()
        if not text:
            await self._relay_park(task_id, "relay_empty_report", "节点未产出任何文本，已暂停")
            return True
        new_state = "completed" if node["terminal"] else "waiting_confirmation"
        applied = await self.store.relay_mutate(
            task_id, expect_states=("running",), expected_revision=epoch[0],
            new_state=new_state, step_delta=1,
            node_id=node["node_id"], node_status="reported", node_report=text,
            node_report_hash=report_hash(text),
            node_report_id=f"{task_id}:{node['node_id']}:{int(node['attempt']) + 1}",
            node_usage=result["usage"], node_error={},
        )
        if applied is not None:
            await self._relay_broadcast(applied)
        return True

    async def _relay_park(
        self, task_id: str, code: str, message: str,
        *, new_state: str = "paused", expected_revision: int | None = None,
    ) -> None:
        result = await self.store.relay_mutate(
            task_id, expect_states=("running",), expected_revision=expected_revision,
            new_state=new_state, error={"code": code, "message": message},
        )
        if result is not None:
            await self._relay_broadcast(result)

    async def _relay_finish(self, task_id: str, expected_revision: int) -> None:
        result = await self.store.relay_mutate(
            task_id, expect_states=("running",), expected_revision=expected_revision,
            new_state="completed",
        )
        if result is not None:
            await self._relay_broadcast(result)

    # ---- provider / 预算辅助 ----

    def _relay_capacity(self, task: dict, node: dict) -> int:
        """当前节点实际使用的服务上下文容量。

        按节点所属槽位取，而不是「快照里第一个有 provider 的槽位」：A/B 可以
        配成不同模型，用错槽位的上限会让预算判断与真实调用脱节。
        """
        cfg = self.cfg
        if cfg is None:
            return 1_000_000
        snap = task.get("model_snapshot") or {}
        name = str((snap.get(node["slot"]) or {}).get("provider") or "")
        if name and name in cfg.providers:
            return cfg.providers[name].effective_context_limit(cfg.context_limit_tokens)
        return cfg.context_limit_tokens

    def _relay_model_snapshot(self, cfg) -> dict:
        out: dict[str, dict] = {}
        for slot_id, slot in cfg.relay.slots.items():
            out[slot_id] = {
                "provider": slot.provider or (self.provider_name or ""),
                "model": slot.model or (self.provider_model or ""),
            }
        return out

    def _relay_build_provider(self, task: dict, node: dict):
        """返回 (provider, owned)；owned=True 表示本方法自建、用完必须释放。

        注入的 provider_factory（测试/演示）可能返回共享 FakeProvider：一律
        视为「不拥有」，绝不关闭。
        """
        slot = (task.get("config", {}).get("slots") or {}).get(node["slot"]) or {}
        name = str(slot.get("provider") or "").strip()
        model = str(slot.get("model") or "").strip()
        if getattr(self, "_provider_factory_override", None) is not None:
            try:
                return self._build_detached_provider(name or "fake", model or None), False
            except Exception as exc:  # noqa: BLE001
                logger.warning("接力槽位 %s provider 构建失败：%s", node["slot"], exc)
                return None
        if not name:
            name = self.provider_name or ""
            model = self.provider_model or ""
        if not name or self.cfg is None or name not in self.cfg.providers:
            return None
        try:
            return self._build_detached_provider(name, model or None), True
        except Exception as exc:  # noqa: BLE001
            logger.warning("接力槽位 %s provider 构建失败：%s", node["slot"], exc)
            return None


def _dual_brief(goal: str, label: str, task: str, peer_label: str, prev: str) -> str:
    """一轮接力的指令：总任务 + 本轮任务 + 对方上一轮的输出。"""
    parts = [
        f"【接力 · {label}】",
        f"总任务：{goal.strip()}",
        f"你这一轮的任务：{task.strip()}",
    ]
    if prev.strip():
        parts.append(f"上一轮（{peer_label}）的输出：\n{prev.strip()}")
    else:
        parts.append("这是接力的第一轮，还没有上一轮输出。")
    parts.append("请只完成你这一轮的任务，输出要能直接交给对方继续处理。")
    return "\n\n".join(parts)


def _ordered(workflow: RelayWorkflow) -> list[str]:
    """串行工作流从起始节点展开的节点 id 顺序。"""
    outgoing = {e.source: e.target for e in workflow.edges}
    out: list[str] = []
    current: str | None = workflow.start_node
    while current is not None and current not in out:
        out.append(current)
        current = outgoing.get(current)
    return out


def _node_of(snapshot: dict, node_id: str) -> dict | None:
    for n in snapshot.get("nodes", []):
        if n["node_id"] == node_id:
            return n
    return None


def _next_node(snapshot: dict, node_id: str) -> dict | None:
    node = _node_of(snapshot, node_id)
    if node is None:
        return None
    for n in snapshot.get("nodes", []):
        if n["ordinal"] == node["ordinal"] + 1:
            return n
    return None


def _reports_of(snapshot: dict, *, upto: int) -> list[dict]:
    out = []
    for n in sorted(snapshot.get("nodes", []), key=lambda x: x["ordinal"]):
        if n["ordinal"] >= upto or not n.get("report"):
            continue
        summary = n["report"].strip().replace("\n", " ")
        out.append({
            "node_id": n["node_id"],
            "slot": n["slot"],
            "report_id": n["report_id"],
            "summary": summary[:_RELAY_SUMMARY_CHARS],
        })
    return out


async def _safe_aclose(provider) -> None:
    try:
        await provider.aclose()
    except Exception:  # noqa: BLE001 - 释放失败不影响主流程
        pass
