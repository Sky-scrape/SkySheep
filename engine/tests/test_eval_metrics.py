"""评测指标层（evals/_metrics.py）与 LLM-as-judge（evals/judge.py）。

全部是确定性单测：不联网、不跑真实模型（judge 用 FakeProvider 回放 JSON）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals._metrics import (
    TurnMetrics,
    aggregate,
    assert_metrics,
    load_records,
    percentile,
    write_records,
)
from evals.judge import JUDGE_SYSTEM, complete_text, judge_answer, parse_verdict
from skysheep.events import (
    PermissionRequest,
    PermissionResolved,
    ToolCallFinished,
    TurnFinished,
    Usage,
)
from skysheep.models.fake import FakeProvider

# ---------------------------------------------------------------- TurnMetrics


def _dict_events() -> list[dict]:
    return [
        {"kind": "turn_started", "ts": 100.0},
        {"kind": "tool_call_finished", "ts": 100.5, "is_error": False, "duration_ms": 120},
        {"kind": "permission_request", "ts": 100.6},
        {"kind": "permission_resolved", "ts": 100.9, "decision": "deny"},
        {"kind": "tool_call_finished", "ts": 101.0, "is_error": True, "duration_ms": 30},
        {"kind": "text_delta", "ts": 101.1, "text": "好的，"},
        {"kind": "usage", "ts": 101.2, "input_tokens": 100, "output_tokens": 20},
        {
            "kind": "turn_finished", "ts": 102.0, "stop_reason": "end_turn",
            "iterations": 2, "duration_ms": 1800, "tools_used": 2,
            "tools_total_ms": 150, "model_retries": 1,
        },
    ]


def test_from_events_dict_shape():
    m = TurnMetrics.from_events(_dict_events())
    assert m.tool_calls == 2 and m.tool_errors == 1
    assert m.permission_requests == 1 and m.permission_denied == 1
    assert m.input_tokens == 100 and m.output_tokens == 20 and m.total_tokens == 120
    assert m.text_chars == 3
    assert m.iterations == 2 and m.model_retries == 1
    assert m.duration_ms == 1800  # 以引擎实测为准（不是墙钟差）
    assert m.tools_total_ms == 150
    assert m.stop_reason == "end_turn"
    assert m.wall_ms == 2000


def test_from_events_object_shape_and_empty():
    events = [
        ToolCallFinished(tool_call_id="t", name="read_file", preview="", duration_ms=12),
        Usage(input_tokens=5, output_tokens=6),
        TurnFinished(stop_reason="error", iterations=1, duration_ms=900),
    ]
    m = TurnMetrics.from_events(events)
    assert m.tool_calls == 1 and m.total_tokens == 11
    assert m.stop_reason == "error" and m.duration_ms == 900
    empty = TurnMetrics.from_events([])
    assert empty.tool_calls == 0 and empty.total_tokens == 0


def test_from_events_permission_allow_is_not_denied():
    events = [PermissionResolved(request_id="r", decision="allow_once")]
    assert TurnMetrics.from_events(events).permission_denied == 0
    assert PermissionRequest(request_id="r").tool_name == ""


def test_as_dict_includes_total_tokens():
    d = TurnMetrics(input_tokens=3, output_tokens=4).as_dict()
    assert d["total_tokens"] == 7 and d["input_tokens"] == 3


# ---------------------------------------------------------------- 预算断言


def test_assert_metrics_pass_and_fail():
    m = TurnMetrics(tool_calls=2, permission_requests=0, input_tokens=10,
                    output_tokens=5, duration_ms=800, model_retries=0)
    assert_metrics(m, max_tool_calls=3, max_permission_requests=0,
                   max_total_tokens=100, max_duration_ms=1000)
    with pytest.raises(AssertionError, match="工具调用次数 2 > 预算 1"):
        assert_metrics(m, max_tool_calls=1)
    with pytest.raises(AssertionError, match="token 总量"):
        assert_metrics(m, max_total_tokens=10)


# ---------------------------------------------------------------- 聚合


def _records() -> list[dict]:
    return [
        {"id": "test_a.py::test_1", "outcome": "passed", "wall_ms": 10,
         "extra": {"total_tokens": 100, "tool_calls": 2}},
        {"id": "test_a.py::test_2", "outcome": "failed", "wall_ms": 30, "extra": {}},
        {"id": "test_b.py::test_3", "outcome": "passed", "wall_ms": 20,
         "extra": {"total_tokens": 50, "tool_calls": 1}},
        {"id": "test_b.py::test_4", "outcome": "skipped", "wall_ms": 0, "extra": {}},
    ]


def test_aggregate_counts_and_buckets():
    agg = aggregate(_records())
    assert agg["total"] == 4 and agg["passed"] == 2
    assert agg["failed"] == 1 and agg["skipped"] == 1
    assert agg["pass_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert agg["total_tokens"] == 150 and agg["tool_calls"] == 3
    assert agg["p50_ms"] == pytest.approx(20.0)
    # p95 只在真正执行过的用例里取（跳过项 wall=0 不进分母）：[10,20,30] → 30
    assert agg["p95_ms"] == pytest.approx(30.0)
    assert set(agg["buckets"]) == {"test_a.py", "test_b.py"}
    assert agg["buckets"]["test_a.py"] == {
        "passed": 1, "failed": 1, "skipped": 0, "p50_ms": 10.0, "p95_ms": 30.0,
    }


def test_aggregate_empty_and_all_skipped():
    assert aggregate([])["pass_rate"] is None
    only_skip = aggregate([{"id": "x", "outcome": "skipped", "wall_ms": 0}])
    assert only_skip["pass_rate"] is None and only_skip["p95_ms"] == 0.0


def test_percentile_nearest_rank():
    assert percentile([], 50) == 0.0
    assert percentile([5], 95) == 5.0
    assert percentile([1, 2, 3, 4], 50) == 2.0
    assert percentile([1, 2, 3, 4], 95) == 4.0


def test_write_and_load_records_roundtrip(tmp_path):
    path = tmp_path / "m.jsonl"
    write_records(path, _records())
    loaded = load_records(path)
    assert len(loaded) == 4 and loaded[0]["id"] == "test_a.py::test_1"
    assert load_records(tmp_path / "missing.jsonl") == []


def test_eval_report_cli_renders_markdown(tmp_path, capsys):
    """报表脚本：从 JSONL 聚合出 Markdown，失败项决定退出码。"""
    import importlib.util

    engine_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "eval_report", engine_dir / "tools" / "eval_report.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    path = tmp_path / "m.jsonl"
    write_records(path, _records())
    assert mod.main(["--jsonl", str(path)]) == 0
    out = capsys.readouterr().out
    assert "# 评测指标摘要" in out and "通过率 66.7%" in out
    assert "test_a.py" in out

    assert mod.main(["--jsonl", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1

    assert mod.main(["--jsonl", str(path), "--fail-on-failed"]) == 1
    assert mod.main(["--jsonl", str(tmp_path / "nope.jsonl")]) == 1


# ---------------------------------------------------------------- judge


def test_parse_verdict_plain_fenced_and_noisy():
    plain = parse_verdict('{"score": 8, "verdict": "pass", "reasons": ["要点齐"]}')
    assert plain.parsed and plain.passed and plain.reasons == ["要点齐"]

    fenced = parse_verdict('好的，评分如下：\n```json\n{"score": 2, "verdict": "fail"}\n```')
    assert fenced.parsed and not fenced.passed

    noisy = parse_verdict('{"score": "7", "verdict": "", "reasons": "只有一条"}')
    assert noisy.parsed and noisy.verdict == "pass" and noisy.reasons == ["只有一条"]

    # 分数与 verdict 冲突时以分数为准（6 分线）
    conflicted = parse_verdict('{"score": 9, "verdict": "fail"}')
    assert conflicted.verdict == "fail"  # 明确写了 fail 就尊重 verdict
    assert parse_verdict("我不会打分").parsed is False


def test_parse_verdict_clamps_score():
    assert parse_verdict('{"score": 99, "verdict": "pass"}').score == 10
    assert parse_verdict('{"score": -3, "verdict": "fail"}').score == 0


async def test_complete_text_and_judge_answer_with_fake_provider():
    provider = FakeProvider([  # 脚本化裁判输出（一组 = 一个字符串块）
        '{"score": 9, "verdict": "pass", "reasons": ["覆盖了默认值与配置项"]}'
    ])
    verdict = await judge_answer(
        provider,
        question="超时默认多少秒？",
        answer="默认 30 秒，可用 request_timeout 调整。",
        criteria="提到 30 秒；提到配置项名称",
    )
    assert verdict.parsed and verdict.passed and verdict.score == 9

    # 提示词契约：system 是裁判角色，user 里带问题/标准/回答
    messages = provider.calls[0]
    assert messages[0].role == "system" and messages[0].text == JUDGE_SYSTEM
    assert "## 问题" in messages[1].text and "## 评分标准" in messages[1].text
    assert "request_timeout" in messages[1].text


async def test_complete_text_without_system():
    provider = FakeProvider(["纯文本回复"])
    assert await complete_text(provider, "你好") == "纯文本回复"
    assert [m.role for m in provider.calls[0]] == ["user"]
