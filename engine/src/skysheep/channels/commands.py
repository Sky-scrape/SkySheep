"""渠道内的斜杠命令解析。

设计取舍：只识别白名单内的命令，其余文本一律当作普通用户消息送进 Agent。
聊天窗口里粘贴的任意内容不能因为长得像指令就被执行——这是把聊天软件接进
Agent 时最容易出的洞（消息内容是不可信输入）。

命令表刻意做小。渠道是"受限遥控端"：能对话、能看状态、能停，但不能切模型服务、
不能改降低防护的开关（那类操作留在桌面端，见 ChannelManager 的路由）。
"""

from __future__ import annotations

from dataclasses import dataclass

HELP_TEXT = """可用命令：
/status  — 看当前会话、模型与运行状态
/new     — 开一个新会话（旧的保留）
/sessions — 列出最近会话
/stop    — 中断正在跑的这一轮
/help    — 看这份说明

直接发文字就是在跟 Agent 对话。"""


@dataclass
class Command:
    """解析结果。name 为空表示这是普通消息，原样交给 Agent。"""

    name: str = ""
    arg: str = ""

    @property
    def is_command(self) -> bool:
        return bool(self.name)


# 命令别名：用户习惯不同，多认几个写法不增加风险（都指向同一实现）
_ALIASES = {
    "status": "status",
    "状态": "status",
    "new": "new",
    "新建": "new",
    "sessions": "sessions",
    "会话": "sessions",
    "stop": "stop",
    "停止": "stop",
    "help": "help",
    "帮助": "help",
}

COMMANDS = tuple(sorted(set(_ALIASES.values())))


def parse(text: str) -> Command:
    """把一条入站文本解析成命令或普通消息。

    只认「以 / 开头且第一段是已知命令」的形式；`/unknown` 视为未知命令（返回
    name="__unknown__"），由调用方回一句提示而不是把它当消息送给模型——否则
    用户打错命令会得到一段莫名其妙的模型回答。
    """
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return Command()
    head, _, rest = raw[1:].partition(" ")
    key = head.strip().lower()
    if not key:
        return Command()
    # 群聊里命令可能写成 /status@botname（部分客户端会自动补机器人名后缀）
    key = key.split("@", 1)[0]
    name = _ALIASES.get(key)
    if name is None:
        return Command(name="__unknown__", arg=rest.strip())
    return Command(name=name, arg=rest.strip())
