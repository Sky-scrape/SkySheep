"""上下文管理测试：token 估算与历史压缩。"""

from __future__ import annotations

from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.core import context as context_mod
from skysheep.core.context import (
    _safe_recent_start,
    compact_history,
    estimate_tokens,
    is_compaction_summary,
)
from skysheep.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from skysheep.models.base import ProviderTextDelta
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


# ---- 长工具链的压缩边界（回归：assistant(tool_use) 作保留段首条不再回退） ----


def _tool_chain_history(pairs: int, prefix: list[Message] | None = None) -> list[Message]:
    """[system, *prefix, (assistant(tool_use), tool) × pairs] 的历史形态。

    agent 一轮工具循环里每迭代只追加 assistant(tool_use) 与 tool 两种消息，
    长工具链回合的历史几乎全是这种交替。
    """
    history = [Message.system("sys"), *(prefix or [])]
    for k in range(pairs):
        history.append(Message.assistant([ToolUseBlock(id=f"t{k}", name="grep", input={})]))
        history.append(Message.tool_result(f"t{k}", "x" * 400))
    return history


def _assert_pairs_intact(recent: list[Message]) -> None:
    """保留段内每个 tool_use 的 tool_result 都必须同段（协议自洽）。"""
    for m in recent:
        for tu in m.tool_uses:
            assert any(
                b.tool_use_id == tu.id
                for other in recent
                for b in other.content
                if isinstance(b, ToolResultBlock)
            ), f"tool_use {tu.id} 的结果被摘要掉了"


def test_safe_recent_start_keeps_tool_use_assistant_at_segment_head():
    """保留段首条是 assistant(tool_use) 不回退：它的 tool_result 就在同段内。

    旧实现对此无条件前移，长工具链里起点会级联退到本轮 user——
    to_summarize 变空、compact_history 返回 None，该场景完全保护不到。
    """
    history = _tool_chain_history(14, prefix=[Message.user("帮我连续处理一批文件")])
    start = _safe_recent_start(history, 8)
    assert start > 2  # 没有级联退到本轮 user（下标 2 之前必须还有可摘要内容）
    recent = history[start:]
    assert recent[0].tool_uses  # 首条是带 tool_use 的 assistant：协议合法
    _assert_pairs_intact(recent)

    # 压缩后再压缩：walk-back 停在工具链上而不是停在摘要自己
    compacted = [Message.system("sys"), Message.user("<earlier-conversation-summary>旧摘要")]
    chain = _tool_chain_history(14, prefix=[Message.user("继续")])
    start2 = _safe_recent_start(compacted + chain, 8)
    to_summarize = (compacted + chain)[1:start2]
    assert len(to_summarize) > 1  # 不是只把摘要再摘要一遍，撑爆窗口的链要进摘要
    _assert_pairs_intact((compacted + chain)[start2:])


async def test_compaction_long_tool_chain_mid_turn(tmp_path):
    """长工具链中段触发压缩（回归）：全 assistant(tool_use)/tool 交替的历史
    也能正常摘要，保留段协议自洽，而不是 to_summarize 为空返回 None。"""
    provider = FakeProvider([[TextBlock(text="SUMMARY: 工具链进展摘要。")]])
    agent = make_agent(provider, tmp_path)
    # helper 自带 system（下标 0），不再 set_system 免得插两条
    agent.history.extend(_tool_chain_history(14, prefix=[Message.user("帮我连续处理一批文件")]))
    assert len(agent.history) == 30

    ev = await compact_history(agent, keep_recent=8)
    assert ev is not None
    assert ev.before_messages == 30
    assert ev.after_messages == 10  # system + 摘要 + 最近 8 条
    assert is_compaction_summary(agent.history[1])
    recent = agent.history[2:]
    assert len(recent) == 8
    assert recent[0].tool_uses
    _assert_pairs_intact(recent)


# ---- 截图穿插历史的压缩边界（发现 7 回归：walk-back 对「user 后紧跟 tool」也回退） ----


def _screenshot_interleaved_history() -> list[Message]:
    """旧版并发批产图的穿插形态：截图 user 消息插在 tool_result 中间。

    agent.py 已根修为「整批 tool_result 落齐后追加截图」，但持久化恢复的
    旧会话历史仍是这种形态，压缩边界必须照样安全。
    """
    return [
        Message.system("sys"),
        Message.user("拍三张"),
        Message.assistant([
            ToolUseBlock(id="call_1", name="shot", input={}),
            ToolUseBlock(id="call_2", name="shot", input={}),
            ToolUseBlock(id="call_3", name="shot", input={}),
        ]),
        Message.tool_result("call_1", "ok"),
        Message.user(
            "[screenshot] 截图一",
            images=[ImageBlock(media_type="image/png", data="img1")],
        ),
        Message.tool_result("call_2", "ok"),
        Message.tool_result("call_3", "ok"),
    ]


def _assert_no_orphan_results(recent: list[Message]) -> None:
    """反向配对：保留段里每个 tool_result 都能在同段找到配对 tool_use。"""
    retained_tu = {tu.id for m in recent for tu in m.tool_uses}
    for m in recent:
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                assert b.tool_use_id in retained_tu, f"孤儿 tool_result {b.tool_use_id}"


def test_safe_recent_start_walks_back_screenshot_user_between_tool_results():
    """保留段切在穿插的截图 user 上时也要回退（发现 7 验证员场景）。

    旧实现只对 role=="tool" 回退：keep=2 时起点落在截图 user 上即停，
    保留段 roles=['user','tool','tool']、孤儿 call_2/call_3——压缩后死局。
    """
    history = _screenshot_interleaved_history()
    start = _safe_recent_start(history, 2)
    assert start == 2  # 回退到 assistant(tool_use)，不越过它
    recent = history[start:]
    assert recent[0].role == "assistant" and recent[0].tool_uses
    _assert_pairs_intact(recent)
    _assert_no_orphan_results(recent)


async def test_compaction_screenshot_interleaved_history(tmp_path):
    """压缩边界含截图穿插历史：压缩结果协议自洽，保留段首条是
    assistant(tool_use) 且无孤儿 tool_result（旧 walk-back 会切在截图 user
    上，留下孤儿 call_2/call_3，压缩后序列化 400 死局）。"""
    provider = FakeProvider([[TextBlock(text="SUMMARY: 截图轮摘要。")]])
    agent = make_agent(provider, tmp_path)
    agent.history.extend(_screenshot_interleaved_history())

    ev = await compact_history(agent, keep_recent=2)
    assert ev is not None
    recent = agent.history[2:]
    assert recent[0].tool_uses
    _assert_pairs_intact(recent)
    _assert_no_orphan_results(recent)


# ---- 压缩摘要调用的异常隔离（回归：瞬态 429/超时不再炸掉整轮对话） ----


class _FlakyProvider:
    """按脚本逐次抛异常/成功的假 provider（compact_history 鸭子类型直接可用）。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        yield ProviderTextDelta("SUMMARY: 摘要内容。")


class _StubAgent:
    """compact_history 只需要 history + provider。"""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.history = [
            Message.system("sys"),
            Message.user("问" + "题" * 300),
            Message.assistant([TextBlock(text="答")]),
        ]


async def test_compaction_retries_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(context_mod, "COMPACT_RETRY_BASE_DELAY_S", 0)  # 测试不等退避
    provider = _FlakyProvider([RuntimeError("HTTP 429 too many requests")])
    ev = await compact_history(_StubAgent(provider), keep_recent=1)
    assert ev is not None
    assert provider.calls == 2  # 第一次瞬态失败后退避重试成功


async def test_compaction_transient_error_exhausted_returns_none(monkeypatch):
    monkeypatch.setattr(context_mod, "COMPACT_RETRY_BASE_DELAY_S", 0)
    provider = _FlakyProvider([
        RuntimeError("429"), RuntimeError("request timed out"), RuntimeError("503"),
    ])
    ev = await compact_history(_StubAgent(provider), keep_recent=1)
    assert ev is None  # 重试耗尽：降级为「本轮不压缩」，不向上抛异常
    assert provider.calls == 3  # 1 次原始调用 + COMPACT_STREAM_RETRIES 次重试


async def test_compaction_non_transient_error_gives_up_immediately():
    provider = _FlakyProvider([ValueError("invalid api key")])
    ev = await compact_history(_StubAgent(provider), keep_recent=1)
    assert ev is None
    assert provider.calls == 1  # 非瞬态错误不重试
