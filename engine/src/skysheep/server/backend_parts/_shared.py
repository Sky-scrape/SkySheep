"""backend 与各 mixin 共用的小件：类型别名与纯数据类。

不 import backend，也不 import 任何 mixin——只被它们引用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core import Agent
    from ...tools import ChangeRecorder

EmitFn = Callable[[dict], Awaitable[None]]


@dataclass
class SessionRuntime:
    """一个会话的运行时状态：独立的 Agent、文件改动记录器、消息队列与运行任务。

    多会话并行的基础：每个会话的 turn 在自己的 runtime 里跑，互不占用；
    事件发出时带 session_id，前端按标签路由。
    """

    sid: str
    agent: Agent
    recorder: ChangeRecorder
    queue: list = field(default_factory=list)
    run_task: asyncio.Task | None = None
