"""backend 与各 mixin 共用的小件：类型别名、纯数据类与一次性收流助手。

不 import backend，也不 import 任何 mixin——只被它们引用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ...messages import Message
from ...models.base import Provider, ProviderDone, ProviderTextDelta

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


async def collect_stream_text(provider: Provider, prompt: str) -> str:
    """一次性模型调用收流：发一轮请求，把文本增量拼成完整回复返回。

    server 侧六处「调模型拿一段文本」的消费循环逐字相同（自动标题、提示词
    润色、归档提炼、轮次沉淀、记忆整理、演化摘要），收敛于此：只收
    ProviderTextDelta，见到 ProviderDone 即停。纯消费——不发事件、不落库，
    异常与取消原样上抛，由调用方决定静默还是报错；限时（asyncio.timeout）
    等边界都由调用方在 await 外侧包。
    """
    parts: list[str] = []
    async for ev in provider.stream([Message.user(prompt)], []):
        if isinstance(ev, ProviderTextDelta):
            parts.append(ev.text)
        elif isinstance(ev, ProviderDone):
            break
    return "".join(parts)
