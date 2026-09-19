"""渠道抽象：一个聊天平台适配器要实现的最小接口。

与前端 WS 的关系：WS 前端是"全功能遥控端"（能切模型、切项目、改降低防护的开关），
渠道是"受限遥控端"——只允许对话与（可选）审批。这个差别由 ChannelManager 在
路由层强制，不依赖各适配器自觉遵守。
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field


@dataclass
class ChannelMessage:
    """归一化后的入站消息。

    各平台字段差异大（Telegram 是 chat.id + from.id，飞书是 open_id + chat_id），
    统一成这四项后，路由与命令解析就不必感知平台差异。

    approved 是渠道级安全闸门的结果：不在允许名单内的来源，消息仍会构造出来
    （用于"发现新来源"提示），但 approved=False，路由层据此拒绝执行。
    """

    channel: str            # "telegram" / "feishu" / ...
    actor: str              # 发送者标识（用于日志与来源列表展示）
    chat_id: str            # 回消息的目标会话（渠道内唯一）
    text: str = ""
    approved: bool = False  # 来源是否在允许名单内
    raw: dict = field(default_factory=dict)


@dataclass
class ChannelStatus:
    """渠道运行态（给设置页渲染用）。"""

    name: str
    enabled: bool = False
    running: bool = False
    configured: bool = False   # 关键配置是否齐全（如 bot token 已填）
    error: str = ""
    seen_sources: list[dict] = field(default_factory=list)  # [{actor, chat_id, ts}]
    # 适配器特有的补充状态（如微信的 need_relogin / paused）
    extra: dict = field(default_factory=dict)


class Channel(abc.ABC):
    """一个聊天平台适配器。

    生命周期：start() 拉起后台轮询/监听任务；stop() 必须干净取消，不能留下
    悬挂的 asyncio task（桌面端关闭时服务随之退出，漏掉的 task 会在解释器
    收尾时报 "Task was destroyed but it is pending"）。

    出站：send_text() 负责把 Agent 的回复发回聊天窗口，并处理平台长度限制
    （Telegram 单条 4096 字符，超长要切分）。
    """

    name: str = ""

    def __init__(self, config: dict, on_message) -> None:
        self.config = dict(config or {})
        # on_message 由 ChannelManager 注入：async (ChannelMessage) -> None
        self.on_message = on_message
        # on_state 由 ChannelManager 注入（可空）：async (dict) -> None
        # 允许适配器把运行时状态（如微信的 bot_token 与长轮询游标）交给宿主持久化。
        # 用可赋值属性而不是构造参数，是为了让各适配器的构造函数保持同一形状。
        self.on_state = None
        self._task = None
        self.error = ""

    # ---- 生命周期 ----

    @abc.abstractmethod
    async def start(self) -> None:
        """拉起后台任务（长轮询等）。实现里不要阻塞。"""

    async def stop(self) -> None:
        """取消后台任务并等待其退出。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- 配置与状态 ----

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def allowed_ids(self) -> set[str]:
        """允许的来源标识集合。**空集合 = 拒绝一切**（不是放行一切）。"""
        raw = self.config.get("allowed_ids") or []
        return {str(x).strip() for x in raw if str(x).strip()}

    def is_allowed(self, actor: str, chat_id: str) -> bool:
        """来源是否被允许。actor 与 chat_id 任一命中即可。

        两个都认是有意的：Telegram 里私聊时 chat.id == from.id，群聊时两者不同，
        用户可能只填了其中一个。
        """
        allowed = self.allowed_ids
        return bool(allowed) and (str(actor) in allowed or str(chat_id) in allowed)

    @abc.abstractmethod
    async def send_text(self, chat_id: str, text: str) -> bool:
        """把文本发到聊天窗口。返回是否发送成功（失败不抛，调用方要能继续）。"""

    def status(self) -> ChannelStatus:
        return ChannelStatus(
            name=self.name,
            enabled=self.enabled,
            running=self.running,
            configured=self.configured(),
            error=self.error,
        )

    def configured(self) -> bool:
        """关键配置是否齐全——不齐全时启用也只会立刻报错，不如提前在界面提示。"""
        return True


def split_text(text: str, limit: int) -> list[str]:
    """按长度切分长回复，尽量在换行处断开（保留可读性）。空文本返回空列表。

    聊天平台普遍有单条消息长度上限（Telegram 4096，微信侧更保守），
    超长回复必须切成多条发送，否则平台直接报错、用户一个字都收不到。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:  # 找不到合适的换行就硬切
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks
