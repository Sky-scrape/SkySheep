"""流式增量合并（StreamDeltaMerger）测试。

背景：上游 provider 的 delta 粒度是 token 级，一轮长回答会产生上千条事件，
每条都单独过 WS 发送锁 + JSON 序列化一帧。合并器把连续的同类型增量拼成批量帧，
但必须满足三条硬约束，否则宁可不要这个优化：

1. **不丢内容**：合并前后拼接出的文本必须逐字一致；
2. **顺序不变**：非增量事件（工具调用/权限/轮末）必须插在原本的位置；
3. **收尾必冲刷**：轮末/异常/取消三条路径都要 flush，末尾几十毫秒不能丢。
"""

from __future__ import annotations

import asyncio

from skysheep.server.backend import StreamDeltaMerger


def collector():
    """收集 emit 出去的事件，并暴露按类型拼接文本的辅助。"""
    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(dict(ev))

    def text_of(kind: str) -> str:
        return "".join(e.get("text", "") for e in events if e.get("kind") == kind)

    return events, emit, text_of


async def test_repeated_text_delta_is_merged_without_loss():
    """连续 text_delta 被合并成更少的帧，但拼接结果逐字一致。"""
    events, emit, text_of = collector()
    m = StreamDeltaMerger(emit, window_s=0.04)

    for chunk in ("AB", "CD", "EF", "GH"):
        await m.send({"kind": "text_delta", "text": chunk})
    await m.aclose()

    assert text_of("text_delta") == "ABCDEFGH"
    assert len(events) < 4, "应当发生合并（否则这个优化没有意义）"


async def test_first_delta_is_emitted_immediately():
    """首条增量立即发出：合并不该增加首字延迟。"""
    events, emit, _ = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)  # 窗口放大，排除定时器影响

    await m.send({"kind": "text_delta", "text": "首"})
    assert len(events) == 1 and events[0]["text"] == "首"


async def test_non_delta_event_flushes_and_keeps_order():
    """非增量事件到达时先冲刷缓冲，并按原顺序发出。"""
    events, emit, text_of = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)

    await m.send({"kind": "text_delta", "text": "A"})
    await m.send({"kind": "text_delta", "text": "B"})
    await m.send({"kind": "tool_call_started", "name": "read_file"})
    await m.send({"kind": "text_delta", "text": "C"})
    await m.aclose()

    kinds = [e["kind"] for e in events]
    assert kinds == ["text_delta", "text_delta", "tool_call_started", "text_delta"]
    assert text_of("text_delta") == "ABC"


async def test_text_and_thinking_do_not_cross_merge():
    """text 与 thinking 是两路内容，不能跨类型合并。"""
    events, emit, text_of = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)

    await m.send({"kind": "thinking_delta", "text": "想1"})
    await m.send({"kind": "thinking_delta", "text": "想2"})
    await m.send({"kind": "text_delta", "text": "答1"})
    await m.aclose()

    assert text_of("thinking_delta") == "想1想2"
    assert text_of("text_delta") == "答1"
    # 合并后的事件不能带上别的类型文本
    for e in events:
        if e["kind"] == "thinking_delta":
            assert "答" not in e["text"]


async def test_roundtable_members_are_merged_per_member_and_round():
    """圆桌成员增量的合并键是 (member_index, round)：不同成员不互相污染。"""
    events, emit, _ = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)

    await m.send({"kind": "roundtable_member_delta", "member_index": 0, "round": 0, "text": "a"})
    await m.send({"kind": "roundtable_member_delta", "member_index": 0, "round": 0, "text": "b"})
    await m.send({"kind": "roundtable_member_delta", "member_index": 1, "round": 0, "text": "c"})
    await m.aclose()

    by_member: dict[int, str] = {}
    for e in events:
        if e["kind"] == "roundtable_member_delta":
            by_member[e["member_index"]] = by_member.get(e["member_index"], "") + e["text"]
    assert by_member == {0: "ab", 1: "c"}


async def test_extra_fields_survive_merge():
    """合并不能丢掉事件上的其余字段（session_id 是前端路由的依据）。"""
    events, emit, _ = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)

    await m.send({"kind": "text_delta", "text": "A", "session_id": "s1"})
    await m.send({"kind": "text_delta", "text": "B", "session_id": "s1"})
    await m.aclose()

    assert events and all(e.get("session_id") == "s1" for e in events)


async def test_tail_is_flushed_without_further_events():
    """模型中途停顿（没有下一条增量）时，缓冲仍要在窗口后自行上屏。"""
    events, emit, text_of = collector()
    m = StreamDeltaMerger(emit, window_s=0.02)

    await m.send({"kind": "text_delta", "text": "X"})
    await m.send({"kind": "text_delta", "text": "Y"})
    assert text_of("text_delta") == "X", "第二条应还在缓冲里"

    await asyncio.sleep(0.1)
    assert text_of("text_delta") == "XY", "定时冲刷必须把尾巴发出去"
    await m.aclose()


async def test_flush_is_idempotent_and_safe_without_pending():
    """重复 flush / 无缓冲 flush 都不该重复发内容（取消路径会调多次）。"""
    events, emit, text_of = collector()
    m = StreamDeltaMerger(emit, window_s=0.02)

    await m.send({"kind": "text_delta", "text": "A"})
    await m.send({"kind": "text_delta", "text": "B"})
    await m.flush()
    await m.flush()
    await m.aclose()

    assert text_of("text_delta") == "AB"


async def test_long_stream_collapses_frame_count_without_loss():
    """长回答的回归门槛：上千条 token 级增量应收敛成极少的帧，且一字不差。

    这是合并的**目的**所在（减少 WS 帧数与发送锁竞争），用帧数断言代替耗时断言——
    帧数是确定性的，不会因机器快慢而抖动。
    """
    events, emit, text_of = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)  # 窗口放大：只验证同步合并规模

    chunks = [f"tok{i:04d}" for i in range(1000)]
    for c in chunks:
        await m.send({"kind": "text_delta", "text": c})
    await m.aclose()

    assert text_of("text_delta") == "".join(chunks), "合并不得丢字"
    # 单条上限 2000 字符：1000×7=7000 字符无论如何都远少于 1000 帧
    assert len(events) <= 10, f"合并后帧数过多：{len(events)}"
