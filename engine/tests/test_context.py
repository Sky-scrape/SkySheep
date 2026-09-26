"""上下文管理测试：token 估算与历史压缩。"""

from __future__ import annotations

from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.core.context import (
    _safe_recent_start,
    compact_history,
    estimate_tokens,
    is_compaction_summary,
)
from skysheep.messages import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
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


async def test_compaction_summary_ignores_thinking_deltas(tmp_path):
    """思考型模型的 reasoning 增量同样带 text 字段，不许混进压缩摘要（只收正文增量）。"""
    provider = FakeProvider([
        [ThinkingBlock(text="我先想想该总结什么"), TextBlock(text="SUMMARY：用户要求写诗")],
    ])
    agent = make_agent(provider, tmp_path)
    agent.set_system("sys")
    agent.history.append(Message.user("问题" + "长" * 200))
    agent.history.append(Message.assistant([TextBlock(text="回答")]))
    agent.history.append(Message.user("继续"))
    ev = await compact_history(agent, keep_recent=1)
    assert ev is not None and ev.summary_chars > 0
    summary = agent.history[1].text
    assert "SUMMARY" in summary
    assert "想想" not in summary  # 思考内容没有混进摘要


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


async def test_compaction_auto_off_skips_auto_compaction(tmp_path):
    """compaction_auto=False（设置里关掉自动压缩）：占用超过触发比例也不压缩，
    原历史原样保留；同条件下开着（默认）会压——两条对照锁住开关的语义。"""
    filler = "A" * 200  # 历史约 250 tokens，上限 1000、触发 0.5 → 阈值 500，必超

    def build(provider, **kw):
        agent = make_agent(
            provider, tmp_path,
            context_limit_tokens=1000, compaction_keep_recent=2,
            compaction_trigger=0.5, **kw,
        )
        agent.set_system("sys")
        for i in range(12):
            agent.history.append(Message.user(f"第{i}轮 {filler}"))
            agent.history.append(Message.assistant([TextBlock(text=f"回复{i}")]))
        return agent

    # 关：整轮不产生压缩事件，历史只多了本轮的一问一答
    off_provider = FakeProvider([[TextBlock(text="好的，继续。")]])
    off_agent = build(off_provider, compaction_auto=False)
    events = [ev async for ev in off_agent.run_turn("继续")]
    assert all(e.kind != "compaction" for e in events)
    assert not any(
        is_compaction_summary(m) for m in off_agent.history
    )
    assert len(off_agent.history) == 27  # system + 旧 24 条 + 本轮 user + assistant

    # 开（默认）：同条件触发压缩
    on_provider = FakeProvider([
        [TextBlock(text="SUMMARY: 历史摘要。")],
        [TextBlock(text="好的，继续。")],
    ])
    on_agent = build(on_provider)
    events = [ev async for ev in on_agent.run_turn("继续")]
    assert any(e.kind == "compaction" for e in events)
