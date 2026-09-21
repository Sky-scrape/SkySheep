"""思考过程与用时：实测耗时落库、历史校准、历史恢复与导出。

覆盖三件事：
1. 引擎把真实轮耗时与思考耗时写进消息（此前按 created_at 差值反推，
   而一轮的 user/assistant 是轮末批量落库的，差值只有几毫秒，算不出轮耗时）；
2. store.recent_turn_seconds 优先读实测字段，旧库消息仍按时间差兜底；
3. _msg_brief / 导出把这两个值透给前端与导出件。
"""

from __future__ import annotations

import asyncio
import json

from skysheep.messages import Message, TextBlock, ThinkingBlock
from skysheep.server.backend import (
    _export_body_text,
    _export_eta_html,
    _export_thinking_html,
    _fmt_export_dur,
    _msg_brief,
    _stamp_turn_estimate,
)
from skysheep.session.store import SessionStore, _message_duration_seconds


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- 消息模型：耗时随内容一起序列化 ----


def test_message_carries_duration_and_estimate_roundtrip():
    m = Message.assistant([TextBlock(text="答案")])
    m.duration_ms = 4321
    m.estimate = {"min_seconds": 60, "max_seconds": 300, "level": "normal", "basis": "代码"}
    back = Message.model_validate_json(m.model_dump_json())
    assert back.duration_ms == 4321
    assert back.estimate["max_seconds"] == 300


def test_thinking_block_carries_duration_roundtrip():
    m = Message.assistant([
        ThinkingBlock(text="推理", signature="sig", duration_ms=1500),
        TextBlock(text="答案"),
    ])
    back = Message.model_validate_json(m.model_dump_json())
    assert back.content[0].duration_ms == 1500
    assert back.thinking == "推理"
    assert back.thinking_ms == 1500


def test_old_message_without_new_fields_defaults_to_zero():
    """旧库消息没有这两个字段：默认 0 / None，不能让反序列化失败。"""
    raw = json.dumps({"role": "assistant", "content": [{"type": "text", "text": "旧回答"}]})
    m = Message.model_validate_json(raw)
    assert m.duration_ms == 0
    assert m.estimate is None
    assert m.thinking_ms == 0


def test_thinking_ms_picks_max_of_blocks():
    m = Message.assistant([
        ThinkingBlock(text="a", duration_ms=100),
        ThinkingBlock(text="b", duration_ms=900),
    ])
    assert m.thinking_ms == 900


# ---- store：实测耗时优先，旧消息按时间差兜底 ----


def test_message_duration_seconds_reads_field():
    raw = json.dumps({"role": "assistant", "content": [], "duration_ms": 2500})
    assert _message_duration_seconds(raw) == 2.5


def test_message_duration_seconds_filters_noise_and_overnight():
    """1 秒以内（重试噪音）与 2 小时以上（挂起过夜）都不参与校准。"""
    assert _message_duration_seconds(json.dumps({"duration_ms": 500})) is None
    assert _message_duration_seconds(json.dumps({"duration_ms": 3 * 3600 * 1000})) is None


def test_message_duration_seconds_tolerates_bad_rows():
    assert _message_duration_seconds("not json") is None
    assert _message_duration_seconds("[]") is None
    assert _message_duration_seconds(json.dumps({"duration_ms": "x"})) is None


def test_recent_turn_seconds_prefers_measured_field(tmp_path):
    """轮末批量落库时 created_at 差值只有几毫秒——实测字段必须优先。"""

    async def go():
        store = await SessionStore(tmp_path / "t.db").connect()
        try:
            sess = await store.create_session(None, title="t")
            await store.append_message(sess.id, Message.user("问"))
            answer = Message.assistant([TextBlock(text="答")])
            answer.duration_ms = 12_000  # 实测 12 秒
            await store.append_message(sess.id, answer)
            samples = await store.recent_turn_seconds(None)
            assert samples == [12.0], samples
        finally:
            await store.close()

    _run(go())


def test_recent_turn_seconds_falls_back_to_timestamp_delta(tmp_path):
    """旧库消息没有 duration_ms：退回时间差，且仍过滤 1 秒以内的噪音。"""

    async def go():
        store = await SessionStore(tmp_path / "t.db").connect()
        try:
            sess = await store.create_session(None, title="t")
            await store.append_message(sess.id, Message.user("问"))
            await store.append_message(sess.id, Message.assistant([TextBlock(text="答")]))
            # 批量落库 → 差值毫秒级 → 被当作噪音滤掉（诚实反映：旧库拿不到样本）
            assert await store.recent_turn_seconds(None) == []
        finally:
            await store.close()

    _run(go())


# ---- backend：盖章与下发 ----


def test_stamp_turn_estimate_fills_missing_duration_for_roundtable():
    """圆桌路径不走 run_turn，没有引擎耗时：落库前按本轮墙钟补上。"""
    msgs = [Message.user("问"), Message.assistant([TextBlock(text="融合答案")])]
    _stamp_turn_estimate(msgs, {"min_seconds": 5, "max_seconds": 20}, turn_t0=0.0)
    # turn_t0=0 表示没有墙钟起点：不编造耗时，只盖预估
    assert msgs[-1].duration_ms == 0
    assert msgs[-1].estimate["max_seconds"] == 20


def test_stamp_turn_estimate_keeps_engine_duration():
    """普通路径引擎已盖过耗时：只补预估，不覆盖实测值。"""
    answer = Message.assistant([TextBlock(text="答案")])
    answer.duration_ms = 7000
    msgs = [Message.user("问"), answer]
    _stamp_turn_estimate(msgs, {"min_seconds": 1, "max_seconds": 9}, turn_t0=0.0)
    assert answer.duration_ms == 7000
    assert answer.estimate["max_seconds"] == 9


def test_stamp_turn_estimate_skips_estimate_for_cancelled_turn():
    """取消轮没有最终回答：不盖任何字段，前端也就不会显示用时。"""
    msgs = [Message.user("问"), Message.assistant([TextBlock(text="")])]
    _stamp_turn_estimate(msgs, None, turn_t0=0.0)
    assert msgs[-1].duration_ms == 0
    assert msgs[-1].estimate is None


def test_msg_brief_exposes_thinking_ms_and_duration():
    m = Message.assistant([
        ThinkingBlock(text="推理", duration_ms=2400),
        TextBlock(text="答案"),
    ])
    m.duration_ms = 9000
    m.estimate = {"min_seconds": 3, "max_seconds": 15, "level": "normal", "basis": "轻量问答"}
    brief = _msg_brief(m)
    assert brief["thinking"] == "推理"
    assert brief["thinking_ms"] == 2400
    assert brief["duration_ms"] == 9000
    assert brief["estimate"]["max_seconds"] == 15


def test_msg_brief_omits_estimate_for_old_messages():
    brief = _msg_brief(Message.assistant([TextBlock(text="旧回答")]))
    assert brief["duration_ms"] == 0
    assert brief["estimate"] is None


# ---- 导出 ----


def test_fmt_export_dur_units():
    assert _fmt_export_dur(45) == "45 秒"
    assert _fmt_export_dur(200) == "3 分 20 秒"
    assert _fmt_export_dur(120) == "2 分钟"
    assert _fmt_export_dur(3900) == "1 小时 5 分"


def test_export_eta_html_absent_without_duration():
    assert _export_eta_html(Message.assistant([TextBlock(text="x")])) == ""


def test_export_eta_html_includes_range_and_basis():
    m = Message.assistant([TextBlock(text="x")])
    m.duration_ms = 272_000  # 4 分 32 秒
    m.estimate = {"min_seconds": 180, "max_seconds": 480, "basis": "代码 · 编号步骤"}
    html = _export_eta_html(m)
    assert "用时 4 分 32 秒" in html
    assert "预估 3~8 分钟" in html
    assert "预估依据：代码 · 编号步骤" in html


def test_export_thinking_html_is_details_block():
    m = Message.assistant([
        ThinkingBlock(text="先看 A 再看 B", duration_ms=12_000),
        TextBlock(text="答案"),
    ])
    html = _export_thinking_html(m)
    assert "<details" in html
    assert "思考 12 秒" in html
    assert "先看 A 再看 B" in html


def test_export_thinking_html_absent_without_thinking():
    assert _export_thinking_html(Message.assistant([TextBlock(text="x")])) == ""


def test_export_body_text_drops_duplicate_thinking():
    """思考已单独成段：正文不再重复带 [thinking]，但正文本身要完整。"""
    m = Message.assistant([
        ThinkingBlock(text="推理"),
        TextBlock(text="答案正文"),
    ])
    body = _export_body_text(m)
    assert body == "答案正文"
    assert "[thinking]" not in body


def test_export_body_text_keeps_non_assistant_intact():
    m = Message.user("问题")
    assert _export_body_text(m) == "问题"
