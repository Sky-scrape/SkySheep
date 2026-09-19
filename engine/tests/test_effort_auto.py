"""思考强度「自动」档：按任务复杂度实时估档（core/effort.py）。

覆盖：估算器的档位判定、Provider 的逐调用 effort 覆盖、Agent 循环里
自动档的实时解析（手动档不被覆盖、未声明支持不下发）。
"""

from __future__ import annotations

import asyncio

from test_reasoning import _empty_anthropic_client, _empty_openai_client

from skysheep.messages import Message, TextBlock, ToolUseBlock
from skysheep.models.anthropic_provider import AnthropicProvider
from skysheep.models.openai_compat import OpenAICompatProvider


def _user(text: str) -> Message:
    return Message.user(text)


def _consume(provider, messages=None, **kw) -> None:
    async def go():
        async for _ in provider.stream(messages or [], [], **kw):
            pass

    asyncio.new_event_loop().run_until_complete(go())


# ---- 估算器：档位判定 ----


def test_trivial_and_light_tasks_go_low():
    from skysheep.core.effort import estimate_effort

    assert estimate_effort([_user("你好")]) == "low"
    assert estimate_effort([_user("谢谢！")]) == "low"
    assert estimate_effort([_user("帮我把这段话翻译成英文")]) == "low"
    assert estimate_effort([_user("总结一下这篇文章的要点")]) == "low"


def test_typical_dev_task_and_code_go_medium():
    from skysheep.core.effort import estimate_effort

    assert estimate_effort([_user("帮我写个函数把 CSV 转成 JSON")]) == "medium"
    assert estimate_effort([_user("继续")]) == "medium"
    assert estimate_effort([_user("看看 config.toml 里这个字段是干嘛的")]) == "medium"
    assert estimate_effort([]) == "medium"  # 空历史兜底


def test_debug_and_design_go_high():
    from skysheep.core.effort import estimate_effort

    assert estimate_effort([
        _user("我的程序一启动就报错 NullPointerException，帮我排查一下原因"),
    ]) == "high"
    assert estimate_effort([
        _user("设计一个高并发系统的整体架构，要考虑性能瓶颈和缓存方案"),
    ]) == "high"


def test_tool_errors_escalate_mid_turn():
    """工具连续报错 → 在排查任务，档位随任务推进升高（实时性的关键）。"""
    from skysheep.core.effort import estimate_effort

    history = [_user("继续弄吧")]
    assert estimate_effort(history) == "medium"
    for i in range(2):
        history.append(Message.tool_result(f"t{i}", "Traceback: boom", is_error=True))
    assert estimate_effort(history) == "high"


def test_many_tool_calls_lift_depth():
    """多步工具作业进行中：轻量提问也不再落 low。"""
    from skysheep.core.effort import estimate_effort

    history = [_user("总结一下结果")]
    assert estimate_effort(history) == "low"
    for i in range(4):
        history.append(Message.assistant([
            ToolUseBlock(id=f"c{i}", name="read_file", input={"path": "a.txt"}),
        ]))
        history.append(Message.tool_result(f"c{i}", "ok"))
    assert estimate_effort(history) == "medium"


# ---- resolve_auto_effort：谁有资格拿到估档 ----


class _SupportedProvider:
    supports_reasoning = True
    reasoning_effort = "auto"


def test_resolve_rules():
    from skysheep.core.effort import resolve_auto_effort

    msgs = [_user("你好")]
    p = _SupportedProvider()
    assert resolve_auto_effort(p, msgs) in ("low", "medium", "high")

    p.reasoning_effort = "high"  # 手动档：不覆盖
    assert resolve_auto_effort(p, msgs) is None

    p2 = _SupportedProvider()
    p2.supports_reasoning = False  # 未声明支持：永远不下发
    assert resolve_auto_effort(p2, msgs) is None


# ---- Provider：stream(effort=...) 逐调用覆盖 ----


def test_openai_effort_override():
    captured: dict = {}
    p = OpenAICompatProvider("x", "m", "k", client=_empty_openai_client(captured))
    p.supports_reasoning = True  # 实例档位保持 auto

    _consume(p, effort="high")
    assert captured.get("reasoning_effort") == "high"

    captured.clear()
    _consume(p)  # 不覆盖 → auto 不传参
    assert "reasoning_effort" not in captured


def test_anthropic_effort_override():
    captured: dict = {}
    p = AnthropicProvider("a", "claude", "k", client=_empty_anthropic_client(captured))
    p.supports_reasoning = True

    _consume(p, effort="low")
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    captured.clear()
    _consume(p)
    assert "thinking" not in captured


# ---- Agent 循环：自动档逐轮解析 ----


class _RecordingProvider:
    """记录每次 stream 收到的 effort，内容回放自 FakeProvider 脚本。"""

    name = "fake"
    model = "fake-1"
    supports_reasoning = True
    supports_vision = True
    temperature = None

    def __init__(self) -> None:
        from skysheep.models.fake import FakeProvider

        self._inner = FakeProvider([[TextBlock(text="好的")]]).with_default(
            [TextBlock(text="好的")]
        )
        self.reasoning_effort = "auto"
        self.efforts: list = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.efforts.append(effort)
        async for pe in self._inner.stream(messages, tool_schemas, effort=effort):
            yield pe


def _agent(provider, tmp_path):
    from skysheep.core.agent import Agent
    from skysheep.core.context import estimate_tokens
    from skysheep.security.gate import PermissionGate
    from skysheep.tools.base import ToolRegistry

    return Agent(
        provider=provider, registry=ToolRegistry([]), gate=PermissionGate(),
        working_dir=tmp_path, context_limit_tokens=estimate_tokens([]) + 10_000,
    )


def _run_turn(agent, text: str) -> None:
    async def go():
        async for _ in agent.run_turn(text):
            pass

    asyncio.new_event_loop().run_until_complete(go())


def test_agent_auto_resolves_effort_per_turn(tmp_path):
    p = _RecordingProvider()
    _run_turn(_agent(p, tmp_path), "你好")
    assert p.efforts == ["low"], "寒暄走低档"

    p2 = _RecordingProvider()
    _run_turn(_agent(p2, tmp_path), "程序启动就报错崩溃了，帮我排查修复")
    assert p2.efforts == ["high"], "排错任务升到高档"


def test_agent_manual_effort_not_overridden(tmp_path):
    p = _RecordingProvider()
    p.reasoning_effort = "medium"  # 手动选定 → 不下发覆盖值
    _run_turn(_agent(p, tmp_path), "你好")
    assert p.efforts == [None]


def test_agent_unsupported_provider_gets_none(tmp_path):
    p = _RecordingProvider()
    p.supports_reasoning = False
    _run_turn(_agent(p, tmp_path), "帮我排查这个崩溃问题")
    assert p.efforts == [None]
