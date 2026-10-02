"""评测驱动器：组装真实 Agent 循环的辅助函数（场景文件从这里导入）。

评测铁律（写新场景前先读 evals/README.md）：
- 用 fake/scripted provider 驱动**真实** Agent 循环 + **真实**工具注册表 +
  **真实**权限门——不许为了好测换掉其中任何一层，评测基线要的就是组装行为；
- SKYSHEEP_HOME 必须经 conftest 的 home 夹具指到临时目录，绝不指向真实
  ~/.skysheep；
- 只断言事件流、工具调用序列与文件系统后果，不评文本质量、不联网。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from skysheep.core import Agent
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools

__all__ = ["finished", "make_agent", "requests", "resolved", "run_eval_turn"]


def make_agent(provider, project_dir: Path, *, gate: PermissionGate | None = None,
               tools=None, **agent_kw) -> Agent:
    """组装评测用 Agent：真实工具注册表 + 真实权限门（与 backend 的装配同构，
    见 server/backend.py 的 PermissionGate(store=..., working_dir=...)）。

    需要调档位（auto_accept_write 等）时自建 gate 传入，与产品一致的改法是
    构造后直接设属性。tools 传入时替换默认注册表的工具清单（默认仍是一整套
    default_tools()）；个别场景要换装测试后门版工具（如 web_fetch 的本机桩
    形态）时用它，其余工具保持与产品装配一致。agent_kw 透传 Agent 其余构造
    参数（如 context_limit_tokens / compaction_*，供压缩边界场景调参）。
    """
    if gate is None:
        gate = PermissionGate(working_dir=project_dir)
    return Agent(
        provider=provider,
        registry=ToolRegistry(tools if tools is not None else default_tools()),
        gate=gate,
        working_dir=project_dir,
        **agent_kw,
    )


async def run_eval_turn(
    agent: Agent, text: str,
    decide: Callable[[object], str | Awaitable[str]] | None = None,
) -> list:
    """驱动一轮真实 Agent 循环，收集完整事件流。

    decide(request_event) -> 决策字符串（allow_once / allow_always / deny），
    每个 permission_request 事件到达时同步回调一次。不传时一律 deny——
    评测基线默认 fail-closed：忘接决策的后果是「什么都没执行」，而不是敏感
    操作被误放行。
    """
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request":
            decision = "deny"
            if decide is not None:
                decision = decide(ev)
                if not isinstance(decision, str):  # 允许 async 回调
                    decision = await decision
            agent.respond_permission(ev.request_id, decision)
    return events


def requests(events) -> list:
    """事件流里的全部 permission_request（按产出顺序）。"""
    return [e for e in events if e.kind == "permission_request"]


def resolved(events) -> list:
    """事件流里的全部 permission_resolved（按产出顺序）。"""
    return [e for e in events if e.kind == "permission_resolved"]


def finished(events) -> dict:
    """tool_call_id → ToolCallFinished 的映射（断言某次调用是否报错用）。"""
    return {e.tool_call_id: e for e in events if e.kind == "tool_call_finished"}
