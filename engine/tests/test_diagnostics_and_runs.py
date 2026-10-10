"""模型诊断（models/diagnostics.py）与运行中心（core/runs.py）。

覆盖：错误分类与中文建议、测试连接的失败面（不联网）、运行中心的状态机
与崩溃恢复（上次在跑 → interrupted）、终态裁剪、子代理状态镜像。
"""

from __future__ import annotations

import json

import pytest

from skysheep.core.runs import (
    INTERRUPTED,
    RUNNING_STATES,
    Run,
    RunCenter,
)
from skysheep.models.diagnostics import (
    ADVICE,
    HealthRegistry,
    diagnose_error,
    test_connection,
)

# ---- 错误分类 ----


class _FakeHttpError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status_code = status


@pytest.mark.parametrize(
    ("exc", "want"),
    [
        (_FakeHttpError("401 invalid api key", 401), "auth"),
        (_FakeHttpError("403 permission denied", 403), "auth"),
        (_FakeHttpError("429 too many requests", 429), "rate_limit"),
        (_FakeHttpError("insufficient balance", 400), "balance"),
        (_FakeHttpError("欠费，请充值", None), "balance"),
        (_FakeHttpError("maximum context length exceeded", 400), "context_overflow"),
        (_FakeHttpError("model not found: gpt-nope", 404), "model_missing"),
        (_FakeHttpError("connection reset by peer", None), "network"),
        (_FakeHttpError("connect timeout", None), "network"),
        (_FakeHttpError("503 service unavailable", 503), "server"),
        (_FakeHttpError("overloaded_error", 529), "server"),
        (_FakeHttpError("invalid parameter: temperature", 400), "invalid_request"),
        (_FakeHttpError("something entirely odd", None), "unknown"),
    ],
)
def test_diagnose_error_categories(exc, want):
    d = diagnose_error(exc)
    assert d.category == want
    assert d.title == ADVICE[want][0]
    assert d.advice == ADVICE[want][1]


def test_diagnose_error_never_raises():
    # 怪异异常对象也不炸：分类兜底 unknown
    d = diagnose_error(RuntimeError("x" * 10))
    assert d.category in ("unknown", "network")


def test_diagnose_transient_flag():
    d = diagnose_error(_FakeHttpError("429 rate limit", 429))
    assert d.transient is True
    d2 = diagnose_error(_FakeHttpError("invalid api key", 401))
    assert d2.transient is False


# ---- 测试连接（不联网：本地无效端口，快速失败） ----


@pytest.mark.asyncio
async def test_test_connection_down_service():
    r = await test_connection(
        kind="openai",
        base_url="http://127.0.0.1:9",  # discard 端口，连接必然失败
        api_key="sk-x",
        model="m",
        timeout_s=2.0,
    )
    assert r.ok is False
    assert r.category in ("network", "unknown")
    assert r.title
    assert r.advice


# ---- 健康登记 ----


def test_health_registry_flow():
    reg = HealthRegistry()
    reg.record_ok("zhipu", 123)
    snap = {x["name"]: x for x in reg.snapshot(["zhipu", "deepseek"])}
    assert snap["zhipu"]["level"] == "ok"
    assert snap["zhipu"]["latency_ms"] == 123
    assert snap["deepseek"]["level"] == "unknown"  # 没测过的补占位

    d = diagnose_error(_FakeHttpError("429 too many requests", 429))
    reg.record_error("zhipu", d)
    assert reg.get("zhipu").level == "warn"  # 限流是黄灯
    d2 = diagnose_error(_FakeHttpError("invalid api key", 401))
    reg.record_error("zhipu", d2)
    assert reg.get("zhipu").level == "down"  # 鉴权是红灯

    reg.record_ok("zhipu")  # 恢复
    assert reg.get("zhipu").level == "ok"


# ---- 运行中心 ----


def test_run_center_register_update_terminal(tmp_path):
    rc = RunCenter(tmp_path / "runs.json")
    rc.register("run-1", "turn", "s1", title="测试轮", retry_text="原文")
    assert rc.get("run-1").state in RUNNING_STATES

    rc.update("run-1", state="tool_running", detail="正在执行 write_file")
    assert rc.get("run-1").state == "tool_running"

    rc.update("run-1", state="failed", error="boom")
    run = rc.get("run-1")
    assert run.state == "failed"
    assert run.error == "boom"
    assert run.retryable is True  # 带原文，可重发
    assert run.finished_at is not None


def test_run_center_crash_recovery_marks_interrupted(tmp_path):
    # 预置快照：一条「上次退出时还在跑」的记录
    p = tmp_path / "runs.json"
    p.write_text(json.dumps({
        "version": 1,
        "runs": [
            {"id": "turn-9", "kind": "turn", "session_id": "s1",
             "title": "被打断的轮", "state": "thinking",
             "retry_text": "继续做事", "retryable": True},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    rc = RunCenter(p)
    run = rc.get("turn-9")
    assert run.state == INTERRUPTED
    assert run.retryable is True
    assert run in rc.interrupted_runs()


def test_run_center_cancel_and_list(tmp_path):
    rc = RunCenter(tmp_path / "runs.json")
    rc.register("run-1", "turn", "s1")
    rc.register("run-2", "subagent", "s2")
    assert rc.cancel("run-1") is True
    assert rc.cancel("run-1") is False  # 已终态
    assert rc.cancel("nope") is False

    rows = rc.list_runs(session_id="s1")
    assert len(rows) == 1
    assert rows[0]["state"] == "cancelled"
    assert rows[0]["running"] is False
    # 只看在跑的：run-1 已终态，run-2 仍在跑
    active = rc.list_runs(include_terminal=False)
    assert len(active) == 1 and active[0]["id"] == "run-2"


def test_run_center_persists_snapshot(tmp_path):
    p = tmp_path / "runs.json"
    rc = RunCenter(p)
    rc.register("run-1", "turn", "s1", retry_text="hi")
    rc.update("run-1", state="completed")
    rc2 = RunCenter(p)
    assert rc2.get("run-1") is not None
    assert rc2.get("run-1").state == "completed"


def test_run_center_trims_terminal_keep(tmp_path):
    rc = RunCenter(tmp_path / "runs.json")
    from skysheep.core.runs import KEEP_TERMINAL

    for i in range(KEEP_TERMINAL + 30):
        rc.register(f"run-{i}", "turn", "s", retry_text="")
        rc.update(f"run-{i}", state="completed")
    rows = rc.list_runs(limit=1000)
    assert len(rows) == KEEP_TERMINAL


def test_run_to_dict_has_labels():
    r = Run(id="x", kind="turn", session_id="s")
    d = r.to_dict()
    assert "state_label" in d and "running" in d
