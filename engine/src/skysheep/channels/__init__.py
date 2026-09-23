"""聊天软件渠道（Bot Channel）：把飞书 / 微信一类聊天窗口接成遥控端。

设计要点见 base.Channel 与 manager.ChannelManager。渠道不是"另一个前端"——
它复用引擎的会话与事件流，但走 HeadlessGate 式的无人值守权限姿态：
只读自动放行，其余按渠道配置决定放行或拒绝，绝不静默放行写/执行操作。
"""

from .base import Channel, ChannelMessage, ChannelStatus
from .feishu import FeishuChannel
from .gate import ChannelGate
from .manager import ChannelManager
from .weixin import WeixinChannel

__all__ = [
    "Channel",
    "ChannelGate",
    "ChannelManager",
    "ChannelMessage",
    "ChannelStatus",
    "FeishuChannel",
    "WeixinChannel",
]
