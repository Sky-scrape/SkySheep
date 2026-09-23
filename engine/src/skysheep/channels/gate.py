"""渠道权限门：把「无人值守」与「远程审批」统一到一个门里。

为什么需要它：渠道会话可能在后端无人盯屏时运行（半夜从手机发消息）。默认的
PermissionGate 会产出 PermissionRequest 并**无限期挂起**等前端决策——渠道场景下
没有前端，Agent 就永久卡死。所以这里必须给出确定答案。

两档行为：

* ``approve_enabled=False``（P1 默认）：完全等价于 HeadlessGate——只读放行、
  预授权名单放行、其余立刻 DENY。Agent 收到「已拒绝」后继续，不阻塞。
* ``approve_enabled=True``（P2）：把确认卡推给聊天窗口，等用户回复；**超时
  （approve_timeout 秒）自动 DENY 并告知窗口**。超时兜底是硬要求，不是体验优化：
  respond_permission 对失效的 request_id 返回 False，不超时就会永久挂起。

渠道场景的两条额外收紧（都在 submit 路径上）：

* **决定只认发起人**：``turn_actor`` 由宿主在每轮开始前写入，``submit_latest``
  带 actor 校验——群聊里 chat_id 命中名单时所有成员的消息都是 approved 的，
  没有这层的话别人一句 allow 就能替群友批准写操作。
* **「总是允许」降级为单次**：聊天窗口不提供 allow always（见
  ``_effective_decision``），持久化的预授权只能在桌面端本机界面做。

与 HeadlessGate 的关系：不是子类。HeadlessGate 的语义是"永不提问"，而这里
在开启审批后必须提问并等待，覆写 authorize 更直白，也避免继承带来的语义混淆。
"""

from __future__ import annotations

import asyncio
import logging

from ..security.gate import Decision, PendingPermission, PermissionGate
from ..tools.base import Safety, Tool

logger = logging.getLogger("skysheep.channels.gate")

# 用户在聊天窗口里可用的审批回复词。刻意收窄：允许"允许/拒绝"的自然写法，
# 但不做模糊匹配——误判一个 deny 成 allow 的代价远大于让用户重打一次。
#
# 安全审查低危项：「ok / 1 / 可以」此前也算批准，但这几个词在中文聊天里更常是
# 随口应和（"1" = 收到），待审批窗口期把它们当批准会吞掉正常聊天、甚至替一次
# 写/执行操作背书。批准只认明确授权的词，拒绝方向保持宽松（误判成拒绝是安全侧）。
ALLOW_WORDS = {"allow", "yes", "y", "允许", "同意"}
ALLOW_ALWAYS_WORDS = {"allow always", "always", "总是允许", "永久允许"}
DENY_WORDS = {"deny", "no", "n", "拒绝", "不行", "取消", "0"}
# 模棱两可的确认词：不当作批准，但在有待审批项时给一句明确指引——
# 否则用户以为回「ok」就是批准，实际这条会排队等在前一轮后面，卡到超时被拒。
AMBIGUOUS_ACK_WORDS = {
    "ok", "okay", "k", "1", "可以", "行", "好", "好的", "嗯", "收到", "了解", "明白",
}


def parse_decision(text: str) -> str | None:
    """把聊天窗口的一行文本解析成审批决定；不是审批回复时返回 None。"""
    key = (text or "").strip().lower()
    if not key:
        return None
    if key in ALLOW_ALWAYS_WORDS:
        return Decision.ALLOW_ALWAYS
    if key in ALLOW_WORDS:
        return Decision.ALLOW_ONCE
    if key in DENY_WORDS:
        return Decision.DENY
    return None


class ChannelGate(PermissionGate):
    """渠道专用门控。notify 由渠道适配器注入，负责把确认卡发到聊天窗口。"""

    def __init__(
        self,
        allowed: list[str] | None = None,
        approve_enabled: bool = False,
        approve_timeout: int = 120,
        notify=None,
        **kw,
    ) -> None:
        super().__init__(**kw)
        # 创建渠道会话时预授权的工具名（对应渠道配置的 allowed_tools）
        self.allowed = set(allowed or [])
        self.approve_enabled = bool(approve_enabled)
        self.approve_timeout = max(1, int(approve_timeout or 120))
        # async (PendingPermission) -> None：把确认卡推给聊天窗口
        self.notify = notify
        # request_id -> 待决策项（供 manager 把用户的回复投回来）
        self.waiting: dict[str, PendingPermission] = {}
        # 当前这一轮的发起人（由宿主在跑一轮前写入）。审批决定只认发起人：
        # 群聊里 chat_id 命中名单时所有成员的消息都是 approved 的，不绑定的话
        # 任何人回一句 allow 就能替群友批准写操作。为空表示宿主没传（退回旧行为）。
        self.turn_actor = ""

    async def authorize(self, tool: Tool, input_dict: dict) -> PendingPermission | None:
        if tool.safety == Safety.READONLY:
            return None
        if tool.name in self.allowed:
            return None

        # 未开启审批：等价 HeadlessGate——立刻拒绝，不阻塞。
        if not self.approve_enabled:
            pending = await super().authorize(tool, input_dict)
            if pending is not None:
                # 先把 future 落定再返回，循环拿到的是"已拒绝"的结果
                pending.resolve(Decision.DENY)
            return pending

        # 开启审批：先生成 PendingPermission（父类会把 on_request 回调跑掉，
        # 这里不用 on_request 而自己记 waiting，避免两处状态不一致）
        pending = await super().authorize(tool, input_dict)
        if pending is None:
            return None
        self.waiting[pending.request_id] = pending
        try:
            if self.notify is not None:
                await self.notify(pending)
        except Exception as e:  # noqa: BLE001 - 推卡片失败要退化成拒绝，不能挂住
            logger.warning("推送审批卡片失败，按拒绝处理：%s", e)
            pending.resolve(Decision.DENY)
            self.waiting.pop(pending.request_id, None)
            return pending

        # 等用户回复；超时自动拒绝。
        #
        # 这里不能用 asyncio.wait_for(pending.wait(), ...)：wait_for 超时会
        # **取消内层 future**，而 PendingPermission.resolve 只在 future 未完成时
        # 才写入——future 已被取消，resolve 静默失效，随后 await pending.wait()
        # 抛 CancelledError 而不是回一个 deny。结果就是超时路径把异常抛进 Agent
        # 循环，而不是安静地拒绝。改用 asyncio.wait：它只等，不碰被等的 future。
        task = asyncio.ensure_future(self._wait(pending))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.approve_timeout)
            if not done:
                pending.resolve(Decision.DENY)
                logger.info(
                    "渠道审批超时（%ss），已自动拒绝 %s", self.approve_timeout, tool.name
                )
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不等结果
                pass
            self.waiting.pop(pending.request_id, None)
        return pending

    @staticmethod
    async def _wait(pending: PendingPermission) -> None:
        await pending.wait()

    def submit(self, request_id: str, decision: str) -> bool:
        """把用户在聊天窗口的回复投给等待中的请求。返回是否命中。"""
        pending = self.waiting.get(request_id)
        if pending is None:
            return False
        pending.resolve(self._effective_decision(decision))
        return True

    @staticmethod
    def _effective_decision(decision: str) -> str:
        """渠道端把「总是允许」降级为「允许一次」。

        白名单规则是持久化的：allow always 一旦从聊天窗口写入，之后每个渠道
        会话都不再就同一工具询问。聊天账号被盗、或群聊里手滑打错词的代价，
        与「少打一次 allow」的便利完全不成比例——所以渠道一律只给单次放行，
        需要预授权时在桌面端的本机界面操作（那里才提供 allow always）。
        """
        return Decision.ALLOW_ONCE if decision == Decision.ALLOW_ALWAYS else decision

    def submit_latest(self, decision: str, actor: str = "") -> str | None:
        """把决定投给当前唯一的待决策项（渠道串行运行，通常只有一个）。

        返回被决策的 request_id；没有待决策项时返回 None。actor 非空且与
        发起这一轮的 turn_actor 不一致时同样返回 None（决定不生效），由调用方
        区分「没有待决策项」与「不是发起人」并给出对应提示。
        """
        if not self.waiting:
            return None
        if actor and self.turn_actor and actor != self.turn_actor:
            return None
        request_id = next(iter(self.waiting))
        self.submit(request_id, decision)
        return request_id
