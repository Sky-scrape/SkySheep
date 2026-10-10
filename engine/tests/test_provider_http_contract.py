"""Provider 层真实 HTTP 契约测试。

既有 provider 测试（test_openai_compat / test_anthropic_stream）全部用进程内
FakeClient 桩——协议序列化由桩「假设」了，SSE 字节流解析、请求头、错误码分支
从未走过真实 openai / anthropic SDK + HTTP 栈。本文件补上这一层：

- 起真实 uvicorn（随机端口，同 test_ws_e2e 的 live_server 模式），按各家
  线上协议手写 SSE / JSON 响应；
- 断言 provider 产出的事件序列（文本增量 / 工具调用 / 思考 / 用量 / 收尾）；
- 覆盖错误分支：401（鉴权，不降级不重试语义由 SDK 承担）、400 带
  stream_options 拒收（严格网关降级重试）、429（限流原样上抛）、断流。

标记为 e2e：起真实 socket，用 ``pytest -m e2e`` 单独跑。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import uvicorn

from skysheep.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from skysheep.models.anthropic_provider import AnthropicProvider
from skysheep.models.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _sse(data: dict | str) -> str:
    """一条 SSE 帧。data 为 str 时构造 [DONE] 哨兵。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n"


class _Recorder:
    """记录最近一次请求的 method/path/headers/body，供断言鉴权头与载荷。"""

    def __init__(self) -> None:
        self.method = ""
        self.path = ""
        self.headers: dict[str, str] = {}
        self.body: dict = {}

    async def __call__(self, scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        while True:
            chunk = await receive()
            body += chunk.get("body", b"")
            if not chunk.get("more_body"):
                break
        self.method = scope["method"]
        self.path = scope["path"]
        self.headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        raw = body.decode("utf-8")
        self.body = json.loads(raw) if raw else {}
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b""})


def _openai_chunks(recorder: _Recorder, *, stream_options: bool = True) -> None:
    """把「一次成功对话」的 SSE 序列挂到 recorder 上：文本增量 + 工具调用 + usage。"""
    chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"content": "，世界"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_9",
                     "type": "function", "function": {"name": "read_file", "arguments": "{\"pa"}}]},
                     "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                     "function": {"arguments": "th\": \"a.txt\"}"}}]}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [],
         "usage": {"prompt_tokens": 21, "completion_tokens": 13,
                   "prompt_tokens_details": {"cached_tokens": 8}}},
    ]
    if stream_options:
        recorder.respond = None  # 占位：见下面 _asgi_app 的统一出口
    recorder.chunks = chunks


@contextlib.asynccontextmanager
async def _live(handler):
    """起一个按 handler 应答的原始 ASGI 服务（uvicorn 承载真实 socket）。"""
    port = _free_port()
    config = uvicorn.Config(handler, host="127.0.0.1", port=port,
                            log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("mock 服务未能在超时内启动")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


def _openai_handler(recorder: _Recorder, *, first_status: int | None = None):
    """OpenAI 兼容端点：/v1/chat/completions。

    first_status 非 None 时第一次请求按该状态码返回 JSON 错误体（用于
    stream_options 降级重试契约），第二次请求才回 SSE 成功流。
    """

    async def handler(scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        while True:
            chunk = await receive()
            body += chunk.get("body", b"")
            if not chunk.get("more_body"):
                break
        recorder.method = scope["method"]
        recorder.path = scope["path"]
        recorder.headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        raw = body.decode("utf-8")
        recorder.body = json.loads(raw) if raw else {}
        recorder.requests = getattr(recorder, "requests", 0) + 1

        async def _send_sse(chunks):
            payload = "".join(_sse(c) for c in chunks) + _sse("[DONE]")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": payload.encode()})

        if first_status is not None and recorder.requests == 1:
            recorder.first_seen_params = recorder.body
            await send({"type": "http.response.start", "status": first_status,
                        "headers": [(b"content-type", b"application/json")]})
            msg = ("Unknown parameter: 'stream_options' is not supported by this gateway")
            await send({"type": "http.response.body",
                        "body": json.dumps({"error": {"message": msg}}).encode()})
            return
        await _send_sse(recorder.chunks)

    return handler


def _status_handler(recorder: _Recorder, status: int, message: str):
    """固定状态码错误端点（401 / 429 等）。"""

    async def handler(scope, receive, send):
        body = b""
        while True:
            chunk = await receive()
            body += chunk.get("body", b"")
            if not chunk.get("more_body"):
                break
        recorder.headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        recorder.path = scope["path"]
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": json.dumps({"error": {"message": message}}).encode()})

    return handler


def _anthropic_handler(recorder: _Recorder):
    """Anthropic /v1/messages SSE：text + thinking + tool_use 完整事件序列。"""

    async def handler(scope, receive, send):
        body = b""
        while True:
            chunk = await receive()
            body += chunk.get("body", b"")
            if not chunk.get("more_body"):
                break
        recorder.headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        recorder.path = scope["path"]
        recorder.body = json.loads(body.decode("utf-8") or "{}")
        events = [
            {"type": "message_start",
             "message": {"id": "msg_1", "type": "message", "role": "assistant",
                         "content": [], "model": "claude-test",
                         "usage": {"input_tokens": 30,
                                   "cache_read_input_tokens": 5,
                                   "cache_creation_input_tokens": 2}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "先想想"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig-abc"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "答案在这"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                               "input": {}}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"x.md\"}"}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta",
             "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 17}},
            {"type": "message_stop"},
        ]
        payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                          for e in events)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": payload.encode()})

    return handler


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


def _tool_history(with_system: bool = False) -> list[Message]:
    """带一次工具往返的最小历史：覆盖 tool_use / tool_result 的线上序列化。"""
    msgs: list[Message] = []
    if with_system:
        msgs.append(_msg("system", "你是测试助手"))
    msgs += [
        _msg("user", "看一下 a.txt"),
        Message(role="assistant", content=[
            TextBlock(text="好的"), ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"}),
        ]),
        Message(role="tool", content=[ToolResultBlock(tool_use_id="t1", content="内容")]),
    ]
    return msgs


def _events(provider, messages):
    async def run():
        out = []
        async for ev in provider.stream(messages, tool_schemas=[]):
            out.append(ev)
        return out
    return asyncio.wait_for(run(), timeout=15)


def _openai_provider(base_url: str) -> OpenAICompatProvider:
    # 代理参数传空：走 SDK 自建客户端直连 127.0.0.1
    return OpenAICompatProvider(name="t", model="m1", api_key="sk-test", base_url=base_url)


# ---- OpenAI 兼容：成功流契约 ----


async def test_openai_http_streams_text_tool_usage():
    recorder = _Recorder()
    recorder.chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"content": "，世界"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_9",
                     "type": "function", "function": {"name": "read_file", "arguments": "{\"pa"}}]},
                     "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                     "function": {"arguments": "th\": \"a.txt\"}"}}]}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [],
         "usage": {"prompt_tokens": 21, "completion_tokens": 13,
                   "prompt_tokens_details": {"cached_tokens": 8}}},
    ]
    async with _live(_openai_handler(recorder)) as base:
        events = await _events(_openai_provider(base), _tool_history())

    texts = "".join(e.text for e in events if type(e).__name__ == "ProviderTextDelta")
    assert texts == "你好，世界"
    tools = [e for e in events if type(e).__name__ == "ProviderToolUse"]
    assert len(tools) == 1 and tools[0].id == "call_9" and tools[0].name == "read_file"
    assert tools[0].input == {"path": "a.txt"}, "跨 chunk 的 arguments 分片必须拼接完整"
    done = next(e for e in events if type(e).__name__ == "ProviderDone")
    assert done.stop_reason == "tool_use"
    assert (done.input_tokens, done.output_tokens, done.cached_tokens) == (21, 13, 8)

    # 请求侧契约：鉴权头、流式开关、用量开关、历史序列化（含工具往返）
    assert recorder.path.endswith("/chat/completions")
    assert recorder.headers.get("authorization") == "Bearer sk-test"
    assert recorder.body["stream"] is True
    assert recorder.body["stream_options"] == {"include_usage": True}
    roles = [m["role"] for m in recorder.body["messages"]]
    assert roles == ["user", "assistant", "tool"], "OpenAI 兼容协议保留独立 tool 角色"


async def test_openai_strict_gateway_400_downgrades_stream_options_over_real_http():
    """严格网关按 400 拒收 stream_options：去掉参数用真实 HTTP 重试一次并成功。"""
    recorder = _Recorder()
    recorder.chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    async with _live(_openai_handler(recorder, first_status=400)) as base:
        events = await _events(_openai_provider(base), [_msg("user", "hi")])

    assert getattr(recorder, "requests", 0) == 2, "400 拒收后必须降级重试一次"
    assert "stream_options" in recorder.first_seen_params
    assert "stream_options" not in recorder.body
    assert "".join(e.text for e in events if type(e).__name__ == "ProviderTextDelta") == "ok"


async def test_openai_401_and_429_surface_without_retry():
    """401 / 429 原样上抛为 ProviderError（真实 HTTP 层，SDK max_retries=1 生效）。"""
    for status in (401, 429):
        recorder = _Recorder()
        async with _live(_status_handler(recorder, status, "nope")) as base:
            with pytest.raises(Exception, match=r"\[t\]"):
                await _events(_openai_provider(base), [_msg("user", "hi")])


async def test_openai_midstream_disconnect_raises_provider_error():
    """服务端中途断流：SSE 半途关连接，provider 包装成 ProviderError 而不是静默结束。"""

    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": _sse(
            {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
             "choices": [{"index": 0, "delta": {"content": "半"}, "finish_reason": None}]}
        ).encode()})
        # 半途关闭：不发 [DONE] 也不发更多帧

    async with _live(handler) as base:
        got: list = []

        async def consume():
            async for ev in _openai_provider(base).stream([_msg("user", "hi")], []):
                got.append(ev)

        # SDK 对截断流可能抛异常，也可能按结束处理；无论哪条路径都不许挂死
        with contextlib.suppress(Exception):
            await asyncio.wait_for(consume(), timeout=15)
    assert got, "断流前收到的增量必须保留"


async def test_openai_connection_refused_surfaces_readable_error():
    """端口无服务（服务未启动/断网）：不挂死，快速抛 ProviderError 带可读文案。"""
    refused = "http://127.0.0.1:9"  # 丢弃端口；连接必被拒
    provider = _openai_provider(refused + "/v1")
    with pytest.raises(Exception, match=r"\[t\]"):
        await _events(provider, [_msg("user", "hi")])


async def test_openai_read_timeout_surfaces_error():
    """服务端迟迟不响应（超过客户端超时）：抛 ProviderError 而不是无限等。"""
    from openai import AsyncOpenAI

    async def handler(scope, receive, send):
        while True:
            chunk = await receive()
            if not chunk.get("more_body"):
                break
        await asyncio.sleep(3)  # 持住不答，触发客户端读超时（限 3s 以免拖慢 shutdown）

    async with _live(handler) as base:
        client = AsyncOpenAI(api_key="sk-test", base_url=base + "/v1",
                             timeout=0.5, max_retries=0)
        provider = OpenAICompatProvider(name="t", model="m1", api_key="sk-test",
                                        base_url=base + "/v1", client=client)
        with pytest.raises(Exception, match=r"\[t\]"):
            await _events(provider, [_msg("user", "hi")])


async def test_anthropic_connection_refused_surfaces_error():
    """Anthropic 同样：连不上时 ProviderError 而非僵尸等待。"""
    provider = AnthropicProvider(name="ta", model="claude-test",
                                 api_key="sk-ant-test",
                                 base_url="http://127.0.0.1:9", max_tokens=100)
    with pytest.raises(Exception, match=r"\[ta\]"):
        await _events(provider, [_msg("user", "hi")])


async def test_anthropic_midstream_error_event_wrapped():
    """服务端在流中发 error 事件：SDK 上抛，provider 包装为 ProviderError。"""

    async def handler(scope, receive, send):
        events = [
            {"type": "message_start",
             "message": {"id": "m1", "type": "message", "role": "assistant",
                         "content": [], "model": "claude-test",
                         "usage": {"input_tokens": 1}}},
            {"type": "error", "error": {"type": "overloaded_error",
                                        "message": "overloaded"}},
        ]
        payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": payload.encode()})

    async with _live(handler) as base:
        provider = AnthropicProvider(name="ta", model="claude-test",
                                     api_key="sk-ant-test", base_url=base,
                                     max_tokens=100)
        with pytest.raises(Exception, match=r"\[ta\]"):
            await _events(provider, [_msg("user", "hi")])


# ---- Anthropic：成功流契约 ----


async def test_anthropic_http_streams_thinking_text_tool_and_cache():
    recorder = _Recorder()
    async with _live(_anthropic_handler(recorder)) as base:
        provider = AnthropicProvider(name="ta", model="claude-test",
                                     api_key="sk-ant-test", base_url=base,
                                     max_tokens=1000)
        events = await _events(provider, _tool_history(with_system=True))

    reasoning = "".join(e.text for e in events
                        if type(e).__name__ == "ProviderReasoning" and e.text)
    assert reasoning == "先想想"
    texts = "".join(e.text for e in events if type(e).__name__ == "ProviderTextDelta")
    assert texts == "答案在这"
    tools = [e for e in events if type(e).__name__ == "ProviderToolUse"]
    assert len(tools) == 1 and tools[0].name == "read_file" and tools[0].input == {"path": "x.md"}
    done = next(e for e in events if type(e).__name__ == "ProviderDone")
    assert done.stop_reason == "tool_use"
    # 缓存归一：input=30 + read 5 + write 2 = 37，cached 只计 read
    assert done.input_tokens == 37 and done.cached_tokens == 5
    assert done.output_tokens == 17

    # 请求侧契约：版本头、系统提示 cache_control、工具往返历史
    assert recorder.path.endswith("/v1/messages")
    assert recorder.headers.get("x-api-key") == "sk-ant-test"
    assert recorder.headers.get("anthropic-version")
    assert recorder.body["max_tokens"] == 1000
    assert recorder.body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in recorder.body["messages"]] == ["user", "assistant", "user"], \
        "tool 结果应按 Anthropic 协议并入 user 角色"
