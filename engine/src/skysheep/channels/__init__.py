"""聊天软件渠道（Bot Channel）：把飞书 / 微信一类聊天窗口接成遥控端。

设计要点见 base.Channel 与 manager.ChannelManager。渠道不是"另一个前端"——
它复用引擎的会话与事件流，但走 HeadlessGate 式的无人值守权限姿态：
只读自动放行，其余按渠道配置决定放行或拒绝，绝不静默放行写/执行操作。

webhook 是其中的例外：纯出站（只发不收）的单向推送口，没有会话与审批，
也不受 allowed_ids 的入站准入闸门约束（留空即广播），见 webhook.WebhookChannel。
"""

from .base import Channel, ChannelMessage, ChannelStatus
from .feishu import FeishuChannel
from .gate import ChannelGate
from .manager import ChannelManager
from .webhook import WebhookChannel
from .weixin import WeixinChannel

__all__ = [
    "Channel",
    "ChannelGate",
    "ChannelManager",
    "ChannelMessage",
    "ChannelStatus",
    "FeishuChannel",
    "WeixinChannel",
    "WebhookChannel",
]
