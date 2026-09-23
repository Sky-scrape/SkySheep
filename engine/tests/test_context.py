"""上下文管理测试：token 估算与历史压缩。"""

from __future__ import annotations

from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.core.context import _safe_recent_start, estimate_tokens
from skysheep.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools


def make_agent(provider, tmp_path, **kwargs):
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=PermissionGate(),
        working_dir=tmp_path,
        max_iterations=5,
        **kwargs,
    )


def test_estimate_tokens():
    msgs = [Message.user("x" * 350)]
    assert estimate_tokens(msgs) == 100


def test_safe_recent_start_avoids_orphan_tool_results():
    history = [
        Message.system("sys"),
        Message.user("q1"),
        Message.assistant([TextBlock(text="working")]),
        Message.user("q2"),
        Message.assistant([ToolUseBlock(id="t1", name="grep", input={})]),
        Message.tool_result("t1", "results"),
        Message.user("q3"),
    ]
    # keep=3：候选起点切在 tool_result 上 → 应前移到安全位置
    start = _safe_recent_start(history, 3)
    recent = history[start:]
    assert recent[0].role in ("user", "assistant")
    # assistant(tool_use) 与其 tool_result 必须同段
    for m in recent:
        for tu in m.tool_uses:
            assert any(
                b.tool_use_id == tu.id
                for other in recent
                for b in other.content
                if isinstance(b, ToolResultBlock)
            )


async def test_compaction_triggers_and_replaces_history(tmp_path):
    provider = FakeProvider(
        [
            [TextBlock(text="SUMMARY: 用户要求写诗；已完成初稿。")],  # 压缩调用
            [TextBlock(text="好的，继续。")],  # 正常回合
        ]
    )
    agent = make_agent(provider, tmp_path, context_limit_tokens=10, compaction_keep_recent=2)
    agent.set_system("sys")
    # 塞入足够长的旧历史（> 10 tokens）
    filler = "A" * 400
    for i in range(6):
        agent.history.append(Message.user(f"第{i}轮 {filler}"))
        agent.history.append(Message.assistant([TextBlock(text=f"回复{i}")]))

    events = []
    async for ev in agent.run_turn("继续"):
        events.append(ev)

    kinds = [e.kind for e in events]
    assert "compaction" in kinds
    comp = [e for e in events if e.kind == "compaction"][0]
    assert comp.before_messages > comp.after_messages
    # 新历史：system + 摘要 + 最近的 user("继续") + assistant
    assert agent.history[0].role == "system"
    assert "earlier-conversation-summary" in agent.history[1].text
    assert "SUMMARY" in agent.history[1].text
    assert agent.history[-1].role == "assistant"


async def test_no_compaction_below_threshold(tmp_path):
    provider = FakeProvider([[TextBlock(text="hi")]])
    agent = make_agent(provider, tmp_path, context_limit_tokens=100_000)
    agent.set_system("sys")
    events = []
    async for ev in agent.run_turn("hello"):
        events.append(ev)
    assert all(e.kind != "compaction" for e in events)


async def test_compaction_trigger_ratio_compacts_early(tmp_path):
    """触发比例：占用超过「上限 × 比例」就压缩，不必顶满 100%。

    历史约 300 tokens、上限 1000、触发比例 0.5 → 阈值 500：顶满判断不成立，
    但按新比例该压。
    """
    provider = FakeProvider(
        [
            [TextBlock(text="SUMMARY: 历史摘要。")],  # 压缩调用
            [TextBlock(text="好的，继续。")],  # 正常回合
        ]
    )
    agent = make_agent(
        provider, tmp_path,
        context_limit_tokens=1000, compaction_keep_recent=2, compaction_trigger=0.5,
    )
    agent.set_system("sys")
    filler = "A" * 200  # ≈50 tokens/条
    for i in range(12):
        agent.history.append(Message.user(f"第{i}轮 {filler}"))
        agent.history.append(Message.assistant([TextBlock(text=f"回复{i}")]))

    events = []
    async for ev in agent.run_turn("继续"):
        events.append(ev)
    assert any(e.kind == "compaction" for e in events)


async def test_compaction_trigger_ratio_default_stays_below_limit(tmp_path):
    """默认触发比例 0.9：占用在 90% 以下不压缩（与低占用不压缩同一行为）。"""
    provider = FakeProvider([[TextBlock(text="hi")]])
    agent = make_agent(
        provider, tmp_path,
        context_limit_tokens=10_000, compaction_trigger=0.9,
    )
    agent.set_system("sys")
    filler = "A" * 200  # 历史约 250 tokens（含输入），远低于 9000
    agent.history.append(Message.user(filler))
    agent.history.append(Message.assistant([TextBlock(text=filler)]))
    events = []
    async for ev in agent.run_turn("hello"):
        events.append(ev)
    assert all(e.kind != "compaction" for e in events)
