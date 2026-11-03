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
    channel_stop(session_id) -> bool   # 是否真的取消到了（/stop 如实回执）
    channel_status_text(session_id) -> str
    channel_list_sessions() -> list[dict]
    channel_submit_decision(binding_key, decision, actor="") -> dict
    channel_has_waiting_decision(binding_key) -> bool
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import commands
from .base import Channel, ChannelMessage, ChannelStatus
from .feishu import FeishuChannel
from .gate import AMBIGUOUS_ACK_WORDS, parse_decision
from .webhook import WebhookChannel
from .weixin import WeixinChannel

logger = logging.getLogger("skysheep.channels")

# 平台名 → 适配器类。加新平台时只改这张表 + 新适配器文件。
ADAPTERS: dict[str, type[Channel]] = {
    "feishu": FeishuChannel,
    "weixin": WeixinChannel,
    "webhook": WebhookChannel,
}

# 上限：发现来源只用于首次配置时认领 chat_id，留最近若干条即可
MAX_SEEN = 20

# 排队上限：一轮消息跑着时再进来的消息按序排队。每条排队消息都是一整轮
# Agent 调用（真金白银的 token），不设上限的话刷屏等于刷预算；超限直接
# 拒收并告诉用户怎么办。
MAX_PENDING_TURNS = 5

# 一批最多合并几条消息：3 条已经覆盖「顺手再做两件事」的日常连发，再多会把
# 一件事的上下文搅在一起（模型容易漏掉中间那条）。
BATCH_MAX = 3

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


def build_channels(config: dict, on_message) -> dict[str, Channel]:
    """按配置构建适配器实例（不启动收消息的后台任务）。

    ChannelManager.restart 与一次性 CLI 进程（skysheep cron run 的只发不收
    渠道）共用这份装配：注册键写回实例、坏配置段跳过，两处口径一致。
    """
    out: dict[str, Channel] = {}
    for name, cls in ADAPTERS.items():
        section = config.get(name) or {}
        if not isinstance(section, dict):
            continue
        channel = cls(section, on_message)
        # 注册键就是该渠道的唯一真名：把它写回实例，让适配器内部（事件里的
        # channel 字段、会话绑定）与字典键一定一致。不这样做时，若适配器的
        # name 属性与注册键对不上，会话与审批会静默串到另一个平台上去。
        channel.name = name
        out[name] = channel
    return out


class ChannelManager:
    def __init__(self, host, config_provider) -> None:
        """config_provider 返回 {平台名: {...配置...}}；每次 restart 时重新读取，
        这样设置页改完配置能立即生效，不必重启整个应用。"""
        self.host = host
        self.config_provider = config_provider
        self.channels: dict[str, Channel] = {}
        # 会话映射键是「绑定键」而不是平台名：默认就是平台名（与改造前一致），
        # 平台能给出话题键时是 `feishu#<thread>`，一个群里多段讨论各走各的
        # 上下文。绑定同样落库（channel_bindings 的 channel 列存整个键），
        # 所以键里带 `#` 不影响持久化。
        self.sessions: dict[str, str] = {}
        self._seen: list[dict] = []
        # 同一时刻只允许一条渠道消息在跑：两个聊天窗口同时下指令会让
        # Agent 的权限确认与上下文交叉，行为难以预期。
        self._lock = asyncio.Lock()
        # 待跑消息（按绑定键分组）+ 「现在是否有一批在跑」（见 _drain）
        self._inbox: dict[str, list[ChannelMessage]] = {}
        self._running = False
        # 当前在跑的那一批属于哪个绑定键：/stop 要停在跑的轮次，而不是停在
        # 发送者自己那个键上（话题分会话后两者可能不是同一个）
        self._running_key = ""

    # ---- 生命周期 ----

    async def restart(self) -> dict:
        """按最新配置重建渠道（设置页保存后调用；应用启动时也走这里）。"""
        await self.stop()
        cfg = self.config_provider() or {}
        for name, channel in build_channels(cfg, self._on_message).items():
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
        """适配器收到的每条消息都到这里。

        顶层兜底：路由里任何宿主调用抛异常都不能冒回适配器——飞书会把它记成
        读流失败，微信的轮询循环会把异常吞掉后丢掉本批剩余消息。这里统一拦下：
        已批准来源回一句可读错误（走既有 _reply 通道），未批准来源保持沉默
        （回复等于向陌生人确认这个 bot 是活的）。
        """
        try:
            await self._route_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("渠道 %s 处理入站消息失败", msg.channel)
            channel = self.channels.get(msg.channel)
            if channel is not None and msg.approved:
                await self._reply(channel, msg, f"处理这条消息时出错了：{e}")

    async def _route_message(self, msg: ChannelMessage) -> None:
        """入站路由的主体：审批应答 → 斜杠命令 → 排队跑一轮。"""
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
        # 按键必须是**绑定键**（feishu#话题）：话题会话的绑定存在完整键下，
        # 按平台名查会永远查不到它自己的审批门。
        decision_key = self._binding_key(msg)
        decision = parse_decision(msg.text)
        if decision is not None:
            res = await self.host.channel_submit_decision(decision_key, decision, msg.actor)
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
            # 门查不到 / 没有待审批项（渠道门被换掉或重建、审批已超时落定）：
            # 审批词到此**不再当新消息重跑**（审查 F1.1③）。它本是审批流的
            # 回执，此时落进对话会被模型当成一条指令执行；且门查不到往往正是
            # 无人值守姿态尚未恢复的时刻（渠道门被交互门顶掉），拿它当新任务
            # 跑一轮风险最大。明确回一句指路，消息丢弃。
            await self._reply(
                channel, msg,
                "当前没有等待审批的操作，这条按审批回复处理、未执行。\n"
                "要下达新任务直接把需求说出来即可。",
            )
            return

        # 模棱两可的应和（"ok"/"1"/"可以"）不当作批准：有待审批项时给明确指引，
        # 不把它当新消息排队（前一轮正卡在审批上，排队的消息会一直等到超时被拒）
        ack_like = (msg.text or "").strip().lower() in AMBIGUOUS_ACK_WORDS
        if ack_like and await self.host.channel_has_waiting_decision(decision_key):
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

        # 普通消息：进收件箱，然后串行消费（同一时刻只跑一轮，避免多渠道
        # 交叉）。正在跑一轮时进来的消息留在箱里，等这一轮结束后与同会话的
        # 其它消息**合并成一轮**——连发「改这个」「顺便跑测试」「再改
        # CHANGELOG」本来就是一件事，拆成三轮既慢又白烧 token。
        #
        # 上限按「1 条在跑 + MAX_PENDING_TURNS 条排队」取：每条排队消息都是一整轮
        # 的量级（token 是真金白银），不设上限的话刷屏等于刷预算。
        # 计数必须**全局**（所有绑定键合计）：话题分会话后键空间不再是一个平台
        # 一个键，按单键计数会让 K 个话题各排 K 份，上限失效。
        bkey = self._binding_key(msg)
        queued = sum(len(items) for items in self._inbox.values())
        # 「前面还有几条」= 所有箱里排着的 + 正在跑的那一批（跑的那批已经从箱里
        # 取走了，不加上它就会告诉用户「前面还有 0 条」，等于没回执）
        ahead = queued + (1 if self._running else 0)
        if ahead >= MAX_PENDING_TURNS + 1:
            await self._reply(
                channel, msg,
                f"排队的消息太多了（前面还有 {queued} 条），"
                "这条没有执行。等当前这轮结束再发，或发 /stop 中断当前轮。",
            )
            return
        self._inbox.setdefault(bkey, []).append(msg)
        if self._running:
            await self._reply(
                channel, msg,
                f"已收到，这条在排队（前面还有 {ahead} 条）。"
                "连着发的几条会合并成一轮处理。",
            )
            # 有 drain 在跑：它会顺手取走箱里的消息，这里直接返回（适配器不必
            # 跟着等，用户已经收到回执）。
            return
        self._running = True
        try:
            await self._drain()
        finally:
            self._running = False

    # ---- 收件箱（把「一条一轮」换成「一批一轮」） ----

    def _next_binding(self) -> str:
        """取下一个有货的会话键（先到先跑）。"""
        for key, items in self._inbox.items():
            if items:
                return key
        return ""

    def _take_batch(self, bkey: str) -> list[ChannelMessage]:
        """取走该绑定键下同一聊天（同一发送者）的最多 BATCH_MAX 条消息。

        合并的前提是「同一个人的同一串话」：跨聊天合并会把 A 聊天的私聊请求
        混进 B 聊天的回复里（回复只发往 batch[0]），跨发送者合并还会让审批
        发起人错绑（gate 只认本轮 actor）。所以按 (chat_id, actor) 分组取批，
        其余消息原样留在箱子里等下一轮。
        """
        items = self._inbox.get(bkey) or []
        if not items:
            return []
        chat_id = items[0].chat_id
        actor = items[0].actor
        batch: list[ChannelMessage] = []
        rest: list[ChannelMessage] = []
        for m in items:
            if m.chat_id == chat_id and m.actor == actor and len(batch) < BATCH_MAX:
                batch.append(m)
            else:
                rest.append(m)
        if rest:
            self._inbox[bkey] = rest
        else:
            self._inbox.pop(bkey, None)
        return batch

    async def _drain(self) -> None:
        """一批接一批地跑到收件箱空。

        跑一轮期间到达的消息不会被这轮忽略：循环回到顶部时会拿到它们，
        要么并入下一批、要么单开一轮。
        """
        while True:
            bkey = self._next_binding()
            if not bkey:
                return
            batch = self._take_batch(bkey)
            if not batch:
                continue
            async with self._lock:
                self._running_key = bkey
                try:
                    await self._run_prompt(batch)
                finally:
                    self._running_key = ""

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
        bkey = self._binding_key(msg)
        if cmd.name == "help":
            await self._reply(channel, msg, commands.HELP_TEXT)
        elif cmd.name == "status":
            try:
                sid = await self._session_id(bkey)
                text = await self.host.channel_status_text(sid)
            except Exception as e:  # noqa: BLE001
                text = f"取状态失败：{e}"
            await self._reply(channel, msg, text)
        elif cmd.name == "new":
            try:
                sid = await self.host.channel_new_session(bkey)
                self.sessions[bkey] = sid
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
            # 停的是**当前在跑的那一批**，不一定是发送者自己的绑定键：话题分会话
            # 后群里 A 话题在跑、用户在 B 话题（或主聊天）发 /stop，按发送者键
            # 找人会停空。排队中的消息一并丢弃（用户说停 = 这一摊都停下）。
            # 不用 _session_id：它会顺手建会话，对没跑过的新话题发 /stop 不该
            # 留下一个空会话。
            target = self._running_key or bkey
            target_sid = self.sessions.get(target) or (self.sessions.get(bkey) if target != bkey else "")
            dropped = len(self._inbox.pop(target, None) or ())
            if target != bkey:
                dropped += len(self._inbox.pop(bkey, None) or ())
            ok = False
            try:
                if target_sid:
                    ok = bool(await self.host.channel_stop(target_sid))
            except Exception as e:  # noqa: BLE001
                await self._reply(channel, msg, f"中断失败：{e}")
                return
            tail = f"，并丢弃了排队中的 {dropped} 条" if dropped else ""
            if ok:
                await self._reply(channel, msg, f"已请求中断当前这一轮{tail}。")
            else:
                await self._reply(
                    channel, msg,
                    f"当前没有在跑的轮次{tail}。要下达新任务直接把需求说出来即可。",
                )

    def _binding_key(self, msg: ChannelMessage) -> str:
        """消息落到哪个会话：平台名，或 `平台#话题键`。

        话题键只在平台真的给出时才用（飞书群话题），拿不到就是平台名——
        与改造前的「一个渠道一个会话」完全一致，不需要迁移老绑定。
        """
        thread = str(getattr(msg, "thread_key", "") or "").strip()
        if not thread:
            return msg.channel
        return f"{msg.channel}#{thread}"

    async def _session_id(self, bkey: str) -> str:
        sid = self.sessions.get(bkey)
        if not sid:
            sid = await self.host.channel_ensure_session(bkey)
            self.sessions[bkey] = sid
        return sid

    @staticmethod
    def _merge_text(batch: list[ChannelMessage]) -> str:
        """把一批消息拼成一轮的输入。

        单条原样返回（占绝大多数，别给模型加无谓的包装）；多条才加编号与
        说明——模型要知道这是连着发的几件事，而不是一段有多段落的话。
        """
        if len(batch) == 1:
            return batch[0].text
        body = "\n\n".join(f"[{i}] {m.text}" for i, m in enumerate(batch, 1))
        return (
            f"（用户连着发了 {len(batch)} 条消息，按顺序合并成这一轮处理；"
            f"逐条都给个交代）\n\n{body}"
        )

    async def _run_prompt(self, batch: list[ChannelMessage]) -> None:
        """跑一批消息（1～BATCH_MAX 条）。回复发到第一条消息所在的聊天。"""
        if not batch:
            return
        msg = batch[0]
        channel = self.channels.get(msg.channel)
        if channel is None:
            return
        try:
            sid = await self._session_id(self._binding_key(msg))
        except Exception as e:  # noqa: BLE001
            await self._reply(channel, msg, f"会话不可用：{e}")
            return
        # 跑太久先补一条「还在处理」，最后取消：一轮可能好几分钟（思考型模型
        # + 工具轮次），渠道端没有任何过程反馈，超过 10 秒没动静就像掉线。
        busy = asyncio.create_task(self._delayed_busy_notice(channel, msg.chat_id))
        try:
            try:
                result = await self.host.channel_run(
                    sid, self._merge_text(batch), actor=msg.actor, chat_id=msg.chat_id
                )
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
