"""模型诊断（models/diagnostics.py）与运行中心（core/runs.py）。

覆盖：错误分类与中文建议、测试连接的失败面（不联网）、运行中心的状态机
与崩溃恢复（上次在跑 → interrupted）、终态裁剪、子代理状态镜像。
"""

from __future__ import annotations

import json

import httpx
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
    capability_summary,
    diagnose_error,
    probe_capabilities,
    test_connection,
)
from skysheep.models.probe import http_client_module as _http_client_module

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


# ---- 能力探测（不联网：本地无效端口 / MockTransport） ----


@pytest.mark.asyncio
async def test_probe_capabilities_down_service_all_none():
    """连不上的服务：四项都是「没测成」（None），不判不支持也不报错。"""
    r = await probe_capabilities(
        kind="openai",
        base_url="http://127.0.0.1:9",  # discard 端口，连接必然失败
        api_key="sk-x",
        model="m",
        timeout_s=2.0,
    )
    for key in ("streaming", "tools", "vision", "reasoning"):
        assert r[key]["supported"] is None
        assert r[key]["note"]
    assert r["summary"]
    assert r["kind"] == "openai"


def _openai_transport(handler):
    """用 MockTransport 拦截 openai SDK 的 HTTP 调用（协议序列与真实线上一致）。"""
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_probe_capabilities_openai_all_supported():
    """openai 兼容协议全支持路径：流式 SSE / tool_calls / 图片 / reasoning_effort。"""
    seen_params: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: F821
        body = json.loads(request.content.decode() or "{}")
        seen_params.update({"stream": body.get("stream"), "tools": bool(body.get("tools")),
                            "content": body.get("messages", [{}])[0].get("content")})
        if body.get("stream"):
            # 最小 SSE 流：一帧 role + [DONE]
            text = (
                'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
                '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, text=text,
                                  headers={"content-type": "text/event-stream"})
        if body.get("tools"):
            tool_args = '{"city": "北京"}'
            message = {"role": "assistant", "content": None,
                       "tool_calls": [{"id": "t1", "type": "function",
                                       "function": {"name": "get_weather",
                                                    "arguments": tool_args}}]}
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls"}],
            })
        content = body.get("messages", [{}])[0].get("content")
        if isinstance(content, list):
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "收到"},
                             "finish_reason": "stop"}],
            })
        # 其余（reasoning_effort 探测）：接受即成功
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
        })

    r = await probe_capabilities(
        kind="openai", base_url="https://mock.test/v1", api_key="sk-x", model="m1",
        transport=_openai_transport(handler),
    )
    assert r["streaming"]["supported"] is True
    assert r["tools"]["supported"] is True
    assert r["vision"]["supported"] is True
    assert r["reasoning"]["supported"] is True
    assert "支持：" in r["summary"]
    assert isinstance(seen_params, dict)


@pytest.mark.asyncio
async def test_probe_capabilities_openai_feature_rejected():
    """服务点名拒绝某项能力：判 False 并给出该能力的结论；其他项互不影响。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        if body.get("stream"):
            return httpx.Response(400, json={
                "error": {"message": "this model does not support streaming output"}})
        if body.get("tools"):
            return httpx.Response(400, json={
                "error": {"message": "tool calling is not supported by this model"}})
        content = body.get("messages", [{}])[0].get("content")
        if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in content):
            return httpx.Response(400, json={
                "error": {"message": "image input is not supported by this model"}})
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
        })

    r = await probe_capabilities(
        kind="openai", base_url="https://mock.test/v1", api_key="sk-x", model="m1",
        transport=httpx.MockTransport(handler),
    )
    assert r["streaming"]["supported"] is False
    assert r["tools"]["supported"] is False
    assert r["vision"]["supported"] is False
    # reasoning 探测的请求不带特殊标记，走成功分支
    assert r["reasoning"]["supported"] is True
    assert "只适合纯对话" in r["summary"]
    assert "不支持图片输入" in r["summary"]


def test_capability_summary_all_none():
    """全部没测成时给兑底提醒，不空结论。"""
    from skysheep.models.diagnostics import CapabilityItem

    items = {k: CapabilityItem(None, "") for k in
             ("streaming", "tools", "vision", "reasoning")}
    s = capability_summary(items)
    assert "未测出" in s


def test_probe_capabilities_unknown_kind_raises():
    import asyncio

    with pytest.raises(RuntimeError):
        asyncio.run(probe_capabilities(kind="fake", api_key="x", model="m"))


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


# ---- 测试连接的成功/失败分支（MockTransport，不联网） ----


def _openai_conn_app(*, list_status=200, chat_status=200, model="m1", ctx=128000):
    """openai 兼容端点：/models 给列表（含窗口字段）+ /chat/completions 给对话。"""
    import httpx

    async def handler(request):
        path = request.url.path
        if path.endswith("/models"):
            if list_status != 200:
                return httpx.Response(list_status, json={"error": {"message": "no list"}})
            entry = {"id": model, "object": "model", "created": 1}
            if ctx:
                entry["context_length"] = ctx
            return httpx.Response(200, json={"object": "list", "data": [entry]})
        if path.endswith("/chat/completions"):
            if chat_status != 200:
                return httpx.Response(chat_status,
                                      json={"error": {"message": "invalid api key"}})
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "created": 1, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}],
            })
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return handler


def _anthropic_message(model="m1", text="hi", stop_reason="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 2}}


# anthropic SDK 1.5+ 走独立的 httpx2 包：MockTransport 与 Response 都必须来自它，
# 混用 httpx 的同名类会被拒（Invalid `http_client` argument / 响应类型不符）
_HX2 = _http_client_module("anthropic")


def _anthropic_transport(handler):
    """anthropic 用的 MockTransport（httpx2 版；httpx 的会被 SDK 拒绝）。"""
    return _HX2.MockTransport(handler)


def _anthropic_conn_app(*, list_status=200, chat_status=200, model="m1", ctx=200000):
    async def handler(request):
        path = request.url.path
        if path.endswith("/models"):
            if list_status != 200:
                return _HX2.Response(list_status, json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            entry = {"id": model, "type": "model", "display_name": "M",
                     "created_at": "2024-01-01T00:00:00Z"}
            if ctx:
                entry["context_length"] = ctx
            return _HX2.Response(200, json={"data": [entry], "has_more": False,
                                             "first_id": model, "last_id": model})
        if path.endswith("/messages"):
            if chat_status != 200:
                return _HX2.Response(chat_status, json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            return _HX2.Response(200, json=_anthropic_message(model))
        return _HX2.Response(404, json={
            "type": "error", "error": {"type": "not_found_error", "message": "no"}})

    return handler


@pytest.mark.asyncio
async def test_test_connection_openai_success_reports_capabilities():
    """成功路径：连通 + 模型在列表 + 窗口字段一并回报（真 HTTP 栈，MockTransport）。"""
    r = await test_connection(
        kind="openai", base_url="https://mock.test/v1", api_key="sk-x", model="m1",
        transport=httpx.MockTransport(_openai_conn_app()),
    )
    assert r.ok is True
    assert r.model_listed is True
    assert r.context_limit == 128000
    assert r.latency_ms >= 0
    assert r.error == ""


@pytest.mark.asyncio
async def test_test_connection_anthropic_success():
    r = await test_connection(
        kind="anthropic", base_url="https://mock.test", api_key="sk-x", model="m1",
        transport=_anthropic_transport(_anthropic_conn_app()),
    )
    assert r.ok is True
    assert r.model_listed is True
    assert r.context_limit == 200000


@pytest.mark.asyncio
async def test_test_connection_list_fails_but_chat_ok():
    """模型列表接口不可用（404）不判死：对话调用成功仍算连通。"""
    r = await test_connection(
        kind="openai", base_url="https://mock.test/v1", api_key="sk-x", model="m1",
        transport=httpx.MockTransport(_openai_conn_app(list_status=404)),
    )
    assert r.ok is True
    assert r.model_listed is None  # 列表没跑成：无法判定是否在列表里


@pytest.mark.asyncio
async def test_test_connection_chat_auth_failure_classified():
    """对话调用 401：判不通，并给出 auth 分类与中文建议。"""
    r = await test_connection(
        kind="openai", base_url="https://mock.test/v1", api_key="sk-bad", model="m1",
        transport=httpx.MockTransport(_openai_conn_app(chat_status=401)),
    )
    assert r.ok is False
    assert r.category == "auth"
    assert r.title and r.advice


@pytest.mark.asyncio
async def test_test_connection_anthropic_auth_failure_short_circuits():
    """anthropic 列表阶段就 401：直接返回（不再打对话调用）。"""
    r = await test_connection(
        kind="anthropic", base_url="https://mock.test", api_key="sk-bad", model="m1",
        transport=_anthropic_transport(_anthropic_conn_app(list_status=401)),
    )
    assert r.ok is False
    assert r.category == "auth"


# ---- 能力探测：anthropic 分支（MockTransport，不联网） ----

_ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    '"role":"assistant","model":"m1","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"ok"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    '"stop_sequence":null},"usage":{"output_tokens":2}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


def _anthropic_cap_app():
    async def handler(request):
        body = json.loads(request.content.decode() or "{}")
        if body.get("stream"):
            return _HX2.Response(200, text=_ANTHROPIC_SSE,
                                  headers={"content-type": "text/event-stream"})
        if body.get("tools"):
            return _HX2.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "get_weather",
                             "input": {"city": "北京"}}],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5}})
        return _HX2.Response(200, json=_anthropic_message("m1", "收到"))

    return handler


@pytest.mark.asyncio
async def test_probe_capabilities_anthropic_all_supported():
    r = await probe_capabilities(
        kind="anthropic", base_url="https://mock.test", api_key="sk-x", model="m1",
        transport=_anthropic_transport(_anthropic_cap_app()),
    )
    assert r["streaming"]["supported"] is True
    assert r["tools"]["supported"] is True
    assert r["vision"]["supported"] is True
    assert r["reasoning"]["supported"] is True
    assert "支持：" in r["summary"]
