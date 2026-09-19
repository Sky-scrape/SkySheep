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
    channel_run(session_id, text) -> dict          # {"text": str} 或 {"error": str}
    channel_stop(session_id) -> None
    channel_status_text(session_id) -> str
    channel_list_sessions() -> list[dict]
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import commands
from .base import Channel, ChannelMessage, ChannelStatus
from .gate import parse_decision
from .telegram import TelegramChannel
from .weixin import WeixinChannel

logger = logging.getLogger("skysheep.channels")

# 平台名 → 适配器类。加新平台时只改这张表 + 新适配器文件。
ADAPTERS: dict[str, type[Channel]] = {
    "telegram": TelegramChannel,
    "weixin": WeixinChannel,
}

# 上限：发现来源只用于首次配置时认领 chat_id，留最近若干条即可
MAX_SEEN = 20


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
                    channel.error = "配置不完整（请先填写 Bot Token）"
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
        decision = parse_decision(msg.text)
        if decision is not None:
            hit = await self.host.channel_submit_decision(msg.channel, decision)
            if hit:
                await self._reply(channel, msg, "已收到，继续。" if decision != "deny" else "已拒绝。")
                return

        cmd = commands.parse(msg.text)
        if cmd.name == "__unknown__":
            await self._reply(channel, msg, f"未知命令：{msg.text.split()[0]}\n\n{commands.HELP_TEXT}")
            return
        if cmd.is_command:
            await self._handle_command(channel, msg, cmd)
            return

        # 普通消息：串行跑一轮，避免多渠道交叉
        async with self._lock:
            await self._run_prompt(channel, msg, msg.text)

    async def _reply(self, channel: Channel, msg: ChannelMessage, text: str) -> None:
        try:
            await channel.send_text(msg.chat_id, text)
        except Exception as e:  # noqa: BLE001 - 回消息失败只记日志
            logger.warning("渠道 %s 回消息失败：%s", channel.name, e)

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
        try:
            result = await self.host.channel_run(sid, text)
        except Exception as e:  # noqa: BLE001 - 一轮失败要让用户看到原因
            await self._reply(channel, msg, f"这一轮出错了：{e}")
            return
        if isinstance(result, dict) and result.get("error"):
            await self._reply(channel, msg, f"这一轮出错了：{result['error']}")
            return
        reply = (result or {}).get("text", "") if isinstance(result, dict) else str(result or "")
        await self._reply(channel, msg, reply or "（这一轮没有产出内容）")
