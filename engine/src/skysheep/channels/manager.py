"""渠道管理器：渠道生命周期、入站路由、以及渠道的权限边界。

安全边界集中在这里，而不是散在各个适配器：
1. **未批准的来源**：只记录到"发现的来源"列表供桌面端展示，不回复任何内容
   （回复等于告诉陌生人这个 bot 是活的）；
2. **降防护操作不可达**：渠道请求只走"对话 + 审批"两条路，无法触发切换到
   accept_edits、开局域网、开电脑控制等操作——这些 method 根本不在渠道的能力集里。
3. **独立会话**：渠道不碰桌面的活动会话，二者可并行使用。

host 对象（通常就是 ServerBackend）需要提供：
    channel_ensure_session(channel_name) -> str
    channel_new_session(channel_name) -> str
    channel_run(session_id, text, actor="") -> dict   # {"text": str} 或 {"error": str}
    channel_stop(session_id) -> None
    channel_status_text(session_id) -> str
    channel_list_sessions() -> list[dict]
    channel_submit_decision(channel_name, decision, actor="") -> dict
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import commands
from .base import Channel, ChannelMessage, ChannelStatus
from .feishu import FeishuChannel
from .gate import AMBIGUOUS_ACK_WORDS, parse_decision
from .weixin import WeixinChannel

logger = logging.getLogger("skysheep.channels")

# 平台名 → 适配器类。加新平台时只改这张表 + 新适配器文件。
ADAPTERS: dict[str, type[Channel]] = {
    "feishu": FeishuChannel,
    "weixin": WeixinChannel,
}

# 上限：发现来源只用于首次配置时认领 chat_id，留最近若干条即可
MAX_SEEN = 20

# 排队上限：一轮消息跑着时再进来的消息按序排队。每条排队消息都是一整轮
# Agent 调用（真金白银的 token），不设上限的话刷屏等于刷预算；超限直接
# 拒收并告诉用户怎么办。
MAX_PENDING_TURNS = 5

# 入站文本上限：飞书单条可到 150 KB，全量进上下文既费 token 也没必要；
# 超过这个长度按截断处理并注明。
MAX_INBOUND_TEXT = 8000

# 这一轮超过多久没回音就先发一条「还在处理」，免得思考型模型跑着时
# 聊天窗口一片死寂（渠道端看不到桌面的用时芯片，会以为掉线了）。
BUSY_NOTICE_DELAY = 10.0
BUSY_NOTICE_TEXT = "⏳ 这一轮还在处理（可能在思考或执行工具），完成后自动回复。"


def _fmt_dur(seconds: float) -> str:
    """渠道回复尾部的时长文案：45 秒 / 3 分 20 秒 / 1 小时 5 分。"""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒" if sec else f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时 {mm} 分" if mm else f"{h} 小时"


def _usage_note(result: dict) -> str:
    """渠道回复尾部的弱化备注：用时（+ 思考字数）。无数据返回空串。

    为什么只报字数不报全文：渠道端多是手机，长思考文本刷屏反而淹没回答；
    但完全不透出也不行——思考型模型思考期间看着就像卡死。字数与耗时
    足以让用户知道「它在想、想了多久」。
    """
    ms = int(result.get("duration_ms") or 0)
    if not ms:
        return ""
    note = f"⏱ 用时 {_fmt_dur(ms / 1000)}"
    think_ms = int(result.get("thinking_ms") or 0)
    chars = int(result.get("thinking_chars") or 0)
    if think_ms or chars:
        bits = []
        if think_ms:
            bits.append(f"思考 {_fmt_dur(think_ms / 1000)}")
        if chars:
            bits.append(f"{chars} 字")
        note += " · " + " · ".join(bits)
    return note


class ChannelManager:
    def __init__(self, host, config_provider) -> None:
        """config_provider 返回 {平台名: {...配置...}}；每次 restart 时重新读取，
        这样设置页改完配置能立即生效，不必重启整个应用。"""
        self.host = host
        self.config_provider = config_provider
        self.channels: dict[str, Channel] = {}
        self.sessions: dict[str, str] = {}   # 平台名 → 渠道会话 id
        self._seen: list[dict] = []
        # 同一时刻只允许一条渠道消息在跑：两个聊天窗口同时下指令会让
        # Agent 的权限确认与上下文交叉，行为难以预期。
        self._lock = asyncio.Lock()
        # 正在等锁的消息数（不含正在跑的那条）：排队回执里报位置、超限拒收
        self._pending_turns = 0

    # ---- 生命周期 ----

    async def restart(self) -> dict:
        """按最新配置重建渠道（设置页保存后调用；应用启动时也走这里）。"""
        await self.stop()
        cfg = self.config_provider() or {}
        for name, cls in ADAPTERS.items():
            section = cfg.get(name) or {}
            if not isinstance(section, dict):
                continue
            channel = cls(section, self._on_message)
            # 注册键就是该渠道的唯一真名：把它写回实例，让适配器内部（事件里的
            # channel 字段、会话绑定）与字典键一定一致。不这样做时，若适配器的
            # name 属性与注册键对不上，会话与审批会静默串到另一个平台上去。
            channel.name = name
            # 适配器可把运行时状态（微信的 bot_token / 游标）交回宿主持久化
            channel.on_state = self._make_state_sink(name)
            self.channels[name] = channel
            if channel.enabled:
                if not channel.configured():
                    # 凭据字段各平台不同（飞书是 App ID + App Secret，微信是扫码
                    # 换来的 bot_token），这里是平台无关的兑底文案，不写死具体字段名。
                    channel.error = "配置不完整（缺少凭据，请在下方填好再启用）"
                    continue
                try:
                    await channel.start()
                except Exception as e:  # noqa: BLE001 - 单个渠道起不来不影响其它
                    channel.error = str(e)
                    logger.warning("渠道 %s 启动失败：%s", name, e)
        return self.status()

    async def stop(self) -> None:
        for channel in self.channels.values():
            try:
                await channel.stop()
            except Exception:  # noqa: BLE001
                pass
        self.channels = {}

    def _make_state_sink(self, name: str):
        """返回一个把适配器状态写回配置的回调。

        微信的 bot_token 是扫码换来的运行时凭据（不是用户手填），游标每次收消息都变；
        两者都必须落盘，否则重启后要么反复要求扫码、要么重放旧消息。

        写入前重新读一遍最新配置再合并，避免把并发期间用户改的其它字段覆盖掉。
        """
        async def sink(state: dict) -> None:
            if not isinstance(state, dict):
                return
            saver = getattr(self.host, "channel_save_state", None)
            if saver is None:
                return
            await saver(name, state)
        return sink

    # ---- 状态 ----

    def status(self) -> dict:
        items: list[ChannelStatus] = []
        for name, channel in self.channels.items():
            st = channel.status()
            st.seen_sources = [s for s in self._seen if s.get("channel") == name]
            items.append(st)
        return {
            "channels": [
                {
                    "name": s.name,
                    "enabled": s.enabled,
                    "running": s.running,
                    "configured": s.configured,
                    "error": s.error,
                    "seen_sources": s.seen_sources,
                    "extra": s.extra,
                }
                for s in items
            ],
            "supported": sorted(ADAPTERS),
        }

    def note_seen(self, msg: ChannelMessage) -> None:
        """记录一个"见过的来源"，供桌面端认领 chat_id。

        只在名单外时记录（名单内的来源已被批准，不需要再提示）；同一 chat_id
        重复出现只刷新时间，避免列表被刷爆。
        """
        for item in self._seen:
            if item["channel"] == msg.channel and item["chat_id"] == msg.chat_id:
                item["ts"] = time.time()
                item["count"] = int(item.get("count", 1)) + 1
                return
        self._seen.append({
            "channel": msg.channel,
            "actor": msg.actor,
            "chat_id": msg.chat_id,
            "ts": time.time(),
            "count": 1,
        })
        overflow = len(self._seen) - MAX_SEEN
        if overflow > 0:
            del self._seen[:overflow]

    # ---- 入站路由 ----

    async def _on_message(self, msg: ChannelMessage) -> None:
        """适配器收到的每条消息都到这里。"""
        channel = self.channels.get(msg.channel)
        if channel is None:
            return
        if len(msg.text) > MAX_INBOUND_TEXT:
            msg.text = msg.text[:MAX_INBOUND_TEXT] + "\n\n（消息过长，超出部分已截断）"

        # 名单外：只记录，不回复。这是刻意的——任何知道 bot 用户名的人都能发消息，
        # 回复会向陌生人确认 bot 是活的。
        if not msg.approved:
            self.note_seen(msg)
            if self.host.store is not None:
                try:
                    await self.host.store.record_channel_source(
                        msg.channel, msg.chat_id, msg.actor
                    )
                except Exception:  # noqa: BLE001 - 记录失败不影响忽略逻辑
                    pass
            logger.info("渠道 %s 收到未授权来源的消息（actor=%s），已忽略", msg.channel, msg.actor)
            return

        # 记下回信地址：审批卡片要发回这个窗口
        self.host.note_channel_chat(msg.channel, msg.chat_id)

        # 审批回复优先于一切：有等待中的确认时，allow / deny 这类词是决定而不是提问。
        # 不先拦这一层，用户回 "allow" 会被当成新消息再跑一轮，确认永远无人应答。
        # 决定只认这一轮的发起人（群聊里名单是按 chat_id 命中的，所有成员的消息
        # 都是 approved 的）；不是发起人时明确告知，而不是把 "allow" 当新消息跑掉。
        decision = parse_decision(msg.text)
        if decision is not None:
            res = await self.host.channel_submit_decision(msg.channel, decision, msg.actor)
            if res.get("hit"):
                await self._reply(
                    channel, msg,
                    "已收到，继续。" if decision != "deny" else "已拒绝。",
                )
                return
            if res.get("actor_mismatch"):
                await self._reply(
                    channel, msg,
                    "这一轮是别人发起的，审批只能由发起人回复。"
                    "你想跑任务的话直接说需求，会另开一轮。",
                )
                return

        # 模棱两可的应和（"ok"/"1"/"可以"）不当作批准：有待审批项时给明确指引，
        # 不把它当新消息排队（前一轮正卡在审批上，排队的消息会一直等到超时被拒）
        ack_like = (msg.text or "").strip().lower() in AMBIGUOUS_ACK_WORDS
        if ack_like and await self.host.channel_has_waiting_decision(msg.channel):
            await self._reply(
                channel, msg,
                "看到你在回应确认卡：批准请回 allow（或 yes / 允许），拒绝请回 deny（或 拒绝）。",
            )
            return

        cmd = commands.parse(msg.text)
        if cmd.name == "__unknown__":
            await self._reply(channel, msg, f"未知命令：{msg.text.split()[0]}\n\n{commands.HELP_TEXT}")
            return
        if cmd.is_command:
            await self._handle_command(channel, msg, cmd)
            return

        # 普通消息：串行跑一轮，避免多渠道交叉。已在跑时排队并回执，让用户
        # 知道消息没有丢；排队过长直接拒收（每条排队消息都是一整轮的 token）。
        if self._lock.locked():
            if self._pending_turns >= MAX_PENDING_TURNS:
                await self._reply(
                    channel, msg,
                    f"排队的消息太多了（前面还有 {self._pending_turns} 条），这条没有执行。"
                    "等当前这轮结束再发，或发 /stop 中断当前轮。",
                )
                return
            self._pending_turns += 1
            try:
                await self._reply(
                    channel, msg,
                    f"已收到。前面还有 {self._pending_turns} 条在排队，轮到后会开始处理。",
                )
                async with self._lock:
                    await self._run_prompt(channel, msg, msg.text)
            finally:
                self._pending_turns -= 1
            return
        async with self._lock:
            await self._run_prompt(channel, msg, msg.text)

    async def _reply(self, channel: Channel, msg: ChannelMessage, text: str) -> None:
        await self._send_text(channel, msg.chat_id, text)

    async def _send_text(self, channel: Channel, chat_id: str, text: str) -> None:
        try:
            await channel.send_text(chat_id, text)
        except Exception as e:  # noqa: BLE001 - 回消息失败只记日志
            logger.warning("渠道 %s 回消息失败：%s", channel.name, e)

    async def _delayed_busy_notice(self, channel: Channel, chat_id: str) -> None:
        """一轮跑了太久时先补一条「还在处理」。完成得快就取消，不打扰。"""
        try:
            await asyncio.sleep(BUSY_NOTICE_DELAY)
        except asyncio.CancelledError:
            return
        await self._send_text(channel, chat_id, BUSY_NOTICE_TEXT)

    async def _handle_command(self, channel: Channel, msg: ChannelMessage, cmd: commands.Command) -> None:
        sid = await self._session_id(channel.name)
        if cmd.name == "help":
            await self._reply(channel, msg, commands.HELP_TEXT)
        elif cmd.name == "status":
            try:
                text = await self.host.channel_status_text(sid)
            except Exception as e:  # noqa: BLE001
                text = f"取状态失败：{e}"
            await self._reply(channel, msg, text)
        elif cmd.name == "new":
            try:
                sid = await self.host.channel_new_session(channel.name)
                self.sessions[channel.name] = sid
                await self._reply(channel, msg, "已开新会话，接着说吧。")
            except Exception as e:  # noqa: BLE001
                await self._reply(channel, msg, f"开新会话失败：{e}")
        elif cmd.name == "sessions":
            try:
                rows = await self.host.channel_list_sessions()
                lines = [
                    f"{'▶ ' if r.get('current') else '   '}{r.get('title') or '（无标题）'}"
                    for r in rows[:10]
                ]
                await self._reply(channel, msg, "最近会话：\n" + "\n".join(lines))
            except Exception as e:  # noqa: BLE001
                await self._reply(channel, msg, f"列表失败：{e}")
        elif cmd.name == "stop":
            try:
                await self.host.channel_stop(sid)
                await self._reply(channel, msg, "已请求中断当前这一轮。")
            except Exception as e:  # noqa: BLE001
                await self._reply(channel, msg, f"中断失败：{e}")

    async def _session_id(self, channel_name: str) -> str:
        sid = self.sessions.get(channel_name)
        if not sid:
            sid = await self.host.channel_ensure_session(channel_name)
            self.sessions[channel_name] = sid
        return sid

    async def _run_prompt(self, channel: Channel, msg: ChannelMessage, text: str) -> None:
        try:
            sid = await self._session_id(channel.name)
        except Exception as e:  # noqa: BLE001
            await self._reply(channel, msg, f"会话不可用：{e}")
            return
        # 跑太久先补一条「还在处理」，最后取消：一轮可能好几分钟（思考型模型
        # + 工具轮次），渠道端没有任何过程反馈，超过 10 秒没动静就像掉线。
        busy = asyncio.create_task(self._delayed_busy_notice(channel, msg.chat_id))
        try:
            try:
                result = await self.host.channel_run(sid, text, actor=msg.actor, chat_id=msg.chat_id)
            except Exception as e:  # noqa: BLE001 - 一轮失败要让用户看到原因
                await self._reply(channel, msg, f"这一轮出错了：{e}")
                return
        finally:
            busy.cancel()
        if isinstance(result, dict) and result.get("error"):
            await self._reply(channel, msg, f"这一轮出错了：{result['error']}")
            return
        reply = (result or {}).get("text", "") if isinstance(result, dict) else str(result or "")
        # 尾部附一行用时（渠道端看不到桌面界面的用时芯片，这是唯一的耗时反馈）。
        # 思考过程不整段回传（遥控端屏幕小、且多是手机）；只报字数与耗时。
        if isinstance(result, dict):
            note = _usage_note(result)
            if note:
                reply = (reply + "\n\n" + note) if reply else note
        await self._reply(channel, msg, reply or "（这一轮没有产出内容）")
