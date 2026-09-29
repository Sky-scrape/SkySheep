"""openai_compat 流式：include_usage 用量块、严格网关 400/422 降级、流的确定性关闭。

对应评审发现：流式请求须带 stream_options.include_usage（否则按规范省略
usage 的服务上 token 永远记 0）；拒收未知参数的严格网关按 400/422（或错误
消息指明参数不认识）去参重试一次，401/403/429 永不降级；流须像
anthropic_provider 一样 async-with 确定性关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from skysheep.models.base import ProviderDone, ProviderError, ProviderTextDelta
from skysheep.models.openai_compat import OpenAICompatProvider


def _chunk(content=None, finish=None):
    """普通增量块：一条 choice + delta（无 usage）。"""
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(reasoning_content=None, content=content, tool_calls=None),
            finish_reason=finish)])


def _usage_chunk(prompt=100, completion=7, cached=None):
    """include_usage 的流末纯用量块：choices 为空，只带 usage。"""
    det = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            prompt_tokens_details=det, prompt_cache_hit_tokens=None),
        choices=[])


class FakeStream:
    """模拟 openai AsyncStream：async-with 退出即 close（与 SDK __aexit__ 一致）。"""

    def __init__(self, chunks=()) -> None:
        self._chunks = list(chunks)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()

    async def close(self):
        self.closed = True


class HangingStream(FakeStream):
    """吐出一个增量后挂死在读流上：取消必然打在流内挂起点。"""

    def __aiter__(self):
        async def _gen():
            yield _chunk("a")
            await asyncio.Event().wait()  # 永不就绪

        return _gen()


class HttpishError(Exception):
    """带 status_code 的伪 HTTP 错误（openai 的 APIStatusError 形态）。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class StatusMessageError(Exception):
    """带 status_code 与自定义消息的伪 HTTP 错误（消息匹配路径要用）。"""

    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeClient:
    """create 按序消费 streams（条目为 FakeStream 或异常），并记录每次 params。"""

    def __init__(self, streams) -> None:
        self._streams = list(streams)
        self.calls: list[dict] = []
        outer = self

        class FakeCompletions:
            async def create(self, **params):
                outer.calls.append(dict(params))
                item = outer._streams.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        # openai SDK 形态：client.chat.completions.create(...)
        self.chat = SimpleNamespace(completions=FakeCompletions())


def _consume(provider) -> list:
    got: list = []

    async def go():
        async for pe in provider.stream([], []):
            got.append(pe)

    asyncio.new_event_loop().run_until_complete(go())
    return got


# ---- 发现 15：stream_options.include_usage ----


def test_stream_requests_include_usage():
    """流式请求带 stream_options={"include_usage": True}。"""
    client = FakeClient([FakeStream([_chunk("好")])])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    _consume(p)
    assert client.calls[0]["stream_options"] == {"include_usage": True}


def test_usage_terminal_chunk_fills_token_counts():
    """流末纯 usage 块（choices 为空）被正确消费成 ProviderDone 的 token 数。"""
    stream = FakeStream([_chunk("好"), _usage_chunk(prompt=123, completion=45, cached=80)])
    client = FakeClient([stream])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    events = _consume(p)
    done = next(e for e in events if isinstance(e, ProviderDone))
    assert (done.input_tokens, done.output_tokens, done.cached_tokens) == (123, 45, 80)


def test_strict_gateway_400_drops_stream_options_and_retries_once():
    """拒收未知参数的严格网关：400 时去掉 stream_options 降级重试一次。"""
    stream = FakeStream([_chunk("ok", finish="stop")])
    client = FakeClient([HttpishError(400), stream])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    events = _consume(p)
    assert len(client.calls) == 2, "400 后恰好重试一次"
    assert "stream_options" in client.calls[0]
    assert "stream_options" not in client.calls[1]
    assert [e.text for e in events if isinstance(e, ProviderTextDelta)] == ["ok"]


def test_non_400_error_is_not_retried():
    """非 400 错误（如 401）原样上抛为 ProviderError，不做无谓重试。"""
    client = FakeClient([HttpishError(401)])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    with pytest.raises(ProviderError):
        _consume(p)
    assert len(client.calls) == 1


def test_plain_error_without_status_code_is_not_retried():
    """无 status_code 的异常（网络断开等）不触发降级重试。"""
    client = FakeClient([RuntimeError("connection reset")])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    with pytest.raises(ProviderError):
        _consume(p)
    assert len(client.calls) == 1


# ---- 审查发现 9：422 / 错误消息匹配也可降级；401/403/429 永不降级 ----


def test_strict_gateway_422_drops_stream_options_and_retries_once():
    """Mistral 等严格网关对未知参数回 422 而非 400：同样去参降级重试一次
    （HEAD 版本没有 stream_options 参数，422 必失败是本批引入的回归）。"""
    stream = FakeStream([_chunk("ok", finish="stop")])
    client = FakeClient([HttpishError(422), stream])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    events = _consume(p)
    assert len(client.calls) == 2, "422 后恰好重试一次"
    assert "stream_options" in client.calls[0]
    assert "stream_options" not in client.calls[1]
    assert [e.text for e in events if isinstance(e, ProviderTextDelta)] == ["ok"]


def test_unknown_parameter_message_triggers_downgrade_without_status_code():
    """无 status_code 但错误消息明确指明参数不认识：同样降级重试一次。"""
    stream = FakeStream([_chunk("ok", finish="stop")])
    client = FakeClient([
        StatusMessageError(None, "Error: unknown parameter 'stream_options'"),
        stream,
    ])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    events = _consume(p)
    assert len(client.calls) == 2, "消息匹配 unknown parameter 后恰好重试一次"
    assert "stream_options" not in client.calls[1]
    assert [e.text for e in events if isinstance(e, ProviderTextDelta)] == ["ok"]


def test_message_with_unrelated_error_is_not_retried():
    """无 status_code 且消息与参数无关（网络/服务端错误）：不降级。"""
    client = FakeClient([StatusMessageError(None, "upstream connect error")])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    with pytest.raises(ProviderError):
        _consume(p)
    assert len(client.calls) == 1


def test_auth_permission_ratelimit_never_downgrade_even_if_message_matches():
    """401/403/429 与参数无关：即使消息碰巧含 unknown parameter 也不降级重试
    （排除条款优先于消息匹配——重试同样失败，只会白付一次请求）。"""
    for status in (401, 403, 429):
        client = FakeClient([
            StatusMessageError(status, f"Error code: {status} - unknown parameter"),
        ])
        p = OpenAICompatProvider("x", "m", "k", client=client)
        with pytest.raises(ProviderError):
            _consume(p)
        assert len(client.calls) == 1, f"HTTP {status} 不得触发去参重试"


# ---- 发现 16：流的确定性关闭 ----


def test_stream_closed_after_normal_completion():
    stream = FakeStream([_chunk("好")])
    client = FakeClient([stream])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    _consume(p)
    assert stream.closed, "正常消费完也要关闭流"


def test_stream_closed_when_consumer_cancelled():
    """消费任务在读流挂起点被取消：流必须立即确定性关闭（不靠 GC 兜底）。"""
    stream = HangingStream()
    client = FakeClient([stream])
    p = OpenAICompatProvider("x", "m", "k", client=client)

    async def go():
        async for _ in p.stream([], []):
            pass

    async def runner():
        task = asyncio.ensure_future(go())
        await asyncio.sleep(0.05)  # 让消费任务推进到挂起点
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.new_event_loop().run_until_complete(runner())
    assert stream.closed, "取消路径也要确定性关闭流"


def test_stream_closed_on_error_mid_stream():
    """流中途抛错（服务端断流等）：异常包装为 ProviderError 且流已关闭。"""
    class ExplodingStream(FakeStream):
        def __aiter__(self):
            async def _gen():
                yield _chunk("a")
                raise HttpishError(500)

            return _gen()

    stream = ExplodingStream()
    client = FakeClient([stream])
    p = OpenAICompatProvider("x", "m", "k", client=client)
    with pytest.raises(ProviderError):
        _consume(p)
    assert stream.closed, "异常路径也要确定性关闭流"
