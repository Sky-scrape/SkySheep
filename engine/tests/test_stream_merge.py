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


async def test_interleaved_roundtable_members_merge_independently():
    """并行成员**交错**流式：各成员独立聚批，不互相冲掉缓冲（分桶回归）。

    圆桌成员经 asyncio.gather 并行作答、共享同一个 emit；修复前的单缓冲实现里，
    成员 B 的每条增量都会把成员 A 的缓冲整个冲掉立即原样发出——交错场景下
    几乎每条 delta 都是一帧，合并窗口恰好在其最该起作用的场景失效。
    """
    events, emit, _ = collector()
    m = StreamDeltaMerger(emit, window_s=10.0)

    async def member(i: int) -> None:
        for c in "abcdef":
            await m.send({"kind": "roundtable_member_delta",
                          "member_index": i, "round": 0, "text": c})
            await asyncio.sleep(0)  # 两个成员交错到达

    await asyncio.gather(member(0), member(1))
    await m.aclose()

    by_member: dict[int, str] = {}
    for e in events:
        if e.get("kind") == "roundtable_member_delta":
            by_member[e["member_index"]] = by_member.get(e["member_index"], "") + e["text"]
    assert by_member == {0: "abcdef", 1: "abcdef"}, "内容必须一字不差"
    assert len(events) <= 6, f"交错流式应各自聚批，而不是每条增量一帧：{len(events)} 帧"


async def test_concurrent_delta_during_suspended_flush_is_not_lost():
    """flush 的 emit 挂起期间并发合入的增量不得丢失（冲刷并发窗口回归）。

    圆桌成员经 asyncio.gather 并行共享同一个 merger：一个任务触发 flush（非增量
    事件先冲刷 / 定时器到点），补发尾巴的 emit 挂起期间，另一任务 send() 了同 key
    增量且仍在合并窗口内。修法（快照循环 + 末尾统一 clear）会让这段增量合进已标记
    sent 的桶、随 clear() 整段丢失且无定时器兜底；正确做法是冲刷前先摘桶，让并发
    增量走「新段首条立即发」路径。
    """
    events: list[dict] = []
    release = asyncio.Event()

    async def emit(ev: dict) -> None:
        events.append(dict(ev))
        if ev.get("text") == "B2":  # 只挂起补发尾巴的那次 emit，制造并发窗口
            await release.wait()

    m = StreamDeltaMerger(emit, window_s=10.0)  # 窗口放大：B3 落入时必判「可合并」

    await m.send({"kind": "text_delta", "text": "B1"})  # 首条立即发
    await m.send({"kind": "text_delta", "text": "B2"})  # 合入未发尾巴
    assert [e["text"] for e in events] == ["B1"]

    flush_task = asyncio.create_task(m.flush())
    for _ in range(100):  # 让 flush 跑到补发 B2 的 emit 挂起点（emit 先记账再挂起）
        if len(events) >= 2:
            break
        await asyncio.sleep(0)
    assert [e["text"] for e in events] == ["B1", "B2"], "flush 应已挂起在补发尾巴上"

    # 并发窗口：flush 挂起期间另一任务合入同 key 增量
    await m.send({"kind": "text_delta", "text": "B3"})
    release.set()
    await flush_task
    await m.aclose()

    joined = "".join(e.get("text", "") for e in events if e.get("kind") == "text_delta")
    assert joined == "B1B2B3", f"挂起的 flush 不得吞掉并发合入的增量：{joined!r}"


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
