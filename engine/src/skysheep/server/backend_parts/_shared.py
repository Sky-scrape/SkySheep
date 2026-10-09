"""backend 与各 mixin 共用的小件：类型别名、纯数据类与一次性收流助手。

不 import backend，也不 import 任何 mixin——只被它们引用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ...messages import Message
from ...models.base import Provider, ProviderDone, ProviderTextDelta

if TYPE_CHECKING:
    from ...core import Agent
    from ...core.checkpoints import CheckpointStore
    from ...core.hooks import HookRunner
    from ...core.subagent import TaskManager
    from ...security.gate import PermissionGate
    from ...tools import ChangeRecorder

EmitFn = Callable[[dict], Awaitable[None]]


@dataclass
class ProjectCtx:
    """一个会话 runtime 绑定的项目上下文：工作目录、权限门、检查点、子代理与钩子。

    会话 runtime 此前一律跑在引擎「当前项目」的工作目录与权限门下；分屏要
    把其他项目的会话当完整会话对话（own=True），这些项目级服务必须跟着会话
    所属项目走——与定时任务「按任务自身项目跑」的既有先例同一套口径。
    own=False 表示会话归属当前项目（或快聊/远程连接等开放归属）：字段只是
    引擎单例的快照，消费方照旧读引擎实时值，行为与此前逐字节一致。
    """

    workdir: Path | None
    project_id: int | None
    gate: PermissionGate
    checkpoints: CheckpointStore
    tasks: TaskManager | None
    hooks: HookRunner | None
    own: bool


@dataclass
class SessionRuntime:
    """一个会话的运行时状态：独立的 Agent、文件改动记录器、消息队列与运行任务。

    多会话并行的基础：每个会话的 turn 在自己的 runtime 里跑，互不占用；
    事件发出时带 session_id，前端按标签路由。ctx 为 None 仅见于不经
    _get_runtime 构造的运行时（渠道/定时任务/流水线自建）：消费方按
    「无 ctx = 引擎默认上下文」处理。
    """

    sid: str
    agent: Agent
    recorder: ChangeRecorder
    queue: list = field(default_factory=list)
    run_task: asyncio.Task | None = None
    ctx: ProjectCtx | None = None


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
