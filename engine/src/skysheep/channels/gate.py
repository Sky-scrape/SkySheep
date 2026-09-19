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
ALLOW_WORDS = {"allow", "ok", "yes", "y", "允许", "同意", "可以", "1"}
ALLOW_ALWAYS_WORDS = {"allow always", "always", "总是允许", "永久允许"}
DENY_WORDS = {"deny", "no", "n", "拒绝", "不行", "取消", "0"}


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
        pending.resolve(decision)
        return True

    def submit_latest(self, decision: str) -> str | None:
        """把决定投给当前唯一的待决策项（渠道串行运行，通常只有一个）。

        返回被决策的 request_id；没有待决策项时返回 None。
        """
        if not self.waiting:
            return None
        request_id = next(iter(self.waiting))
        self.submit(request_id, decision)
        return request_id
