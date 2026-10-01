"""Anthropic 流式解析协议用例：假 SDK 事件流直接驱动 stream() 主循环。

回归 2026-10 审查项 10：stream() 的 tool_use 块聚合、text/thinking/signature
delta 透出、缓存用量归一、tool 参数 JSON 解析、异常包装此前整段零执行（唯一
驱动它的假 client 流体不产任何事件）。事件用 SimpleNamespace 造，形状对齐
anthropic SDK 暴露给 stream() 的属性（type / index / content_block / delta /
usage / message），不联网、不依赖 SDK 内部。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skysheep.models.anthropic_provider import AnthropicProvider
from skysheep.models.base import (
    ProviderDone,
    ProviderError,
    ProviderReasoning,
    ProviderTextDelta,
    ProviderToolUse,
)


def _event(etype: str, **kw):
    return SimpleNamespace(type=etype, **kw)


def _full_events() -> list:
    """一次「文本 + 思考 + 签名 + 工具调用」的完整事件序列。"""
    return [
        _event("message_start", message=SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, cache_read_input_tokens=40,
            cache_creation_input_tokens=10))),
        _event("content_block_delta", index=0,
               delta=SimpleNamespace(type="text_delta", text="你好")),
        _event("content_block_delta", index=0,
               delta=SimpleNamespace(type="thinking_delta", thinking="想一下")),
        _event("content_block_delta", index=0,
               delta=SimpleNamespace(type="signature_delta", signature="sig==")),
        _event("content_block_start", index=1, content_block=SimpleNamespace(
            type="tool_use", id="tu_1", name="read_file")),
        _event("content_block_delta", index=1,
               delta=SimpleNamespace(type="input_json_delta", partial_json='{"pa')),
        _event("content_block_delta", index=1,
               delta=SimpleNamespace(type="input_json_delta", partial_json='th": "a.txt"}')),
        _event("message_delta", usage=SimpleNamespace(output_tokens=21)),
    ]


def _client_with(events, captured: dict | None = None):
    class FakeMessages:
        def stream(self, **kwargs):
            if captured is not None:
                captured.update(kwargs)

            class _Stream:
                async def __aenter__(self):
                    class _Iter:
                        def __aiter__(self):
                            async def _gen():
                                for e in events:
                                    yield e
                            return _gen()

                    return _Iter()

                async def __aexit__(self, *a):
                    return False

            return _Stream()

    class FakeClient:
        messages = FakeMessages()

    return FakeClient()


def _provider(client) -> AnthropicProvider:
    return AnthropicProvider(
        name="anthropic-test", model="claude-test", api_key="k", client=client
    )


async def _collect(provider):
    out = []
    async for ev in provider.stream([], []):
        out.append(ev)
    return out


async def test_stream_aggregates_tool_use_and_deltas():
    captured: dict = {}
    evs = await _collect(_provider(_client_with(_full_events(), captured)))
    # 文本与思考增量实时透出
    assert evs[0] == ProviderTextDelta("你好")
    assert ProviderReasoning("想一下") in evs
    # 签名是只带 signature 的空文本增量（前端折叠展示用）
    sig = [e for e in evs if isinstance(e, ProviderReasoning) and e.signature]
    assert len(sig) == 1 and sig[0].text == "" and sig[0].signature == "sig=="
    # 分片 JSON 聚合成完整入参
    tools = [e for e in evs if isinstance(e, ProviderToolUse)]
    assert len(tools) == 1
    assert tools[0].id == "tu_1" and tools[0].name == "read_file"
    assert tools[0].input == {"path": "a.txt"}
    # 缓存用量归一：input = 100 + 读 40 + 写 10；cached 只计本次真正省下的「读」
    assert evs[-1] == ProviderDone(
        stop_reason="tool_use", input_tokens=150, output_tokens=21, cached_tokens=40
    )
    # 空工具清单时不带 tools 参数（请求形状的回归锚）
    assert "tools" not in captured


async def test_stream_end_turn_without_tools_or_cache():
    events = [
        _event("message_start", message=SimpleNamespace(usage=SimpleNamespace(
            input_tokens=7, cache_read_input_tokens=0, cache_creation_input_tokens=0))),
        _event("content_block_delta", index=0,
               delta=SimpleNamespace(type="text_delta", text="plain")),
        _event("message_delta", usage=SimpleNamespace(output_tokens=2)),
    ]
    evs = await _collect(_provider(_client_with(events)))
    assert evs == [
        ProviderTextDelta("plain"),
        ProviderDone(stop_reason="end_turn", input_tokens=7, output_tokens=2, cached_tokens=0),
    ]
    # 无缓存明细时归一退化为透传（不出现 0+0 叠加 bug）
    assert evs[-1].input_tokens == 7


async def test_stream_bad_tool_json_falls_back_to_raw():
    events = [
        _event("content_block_start", index=0, content_block=SimpleNamespace(
            type="tool_use", id="tu_2", name="write_file")),
        _event("content_block_delta", index=0,
               delta=SimpleNamespace(type="input_json_delta", partial_json='{"bad')),
        _event("message_delta", usage=None),
    ]
    tools = [e for e in await _collect(_provider(_client_with(events)))
             if isinstance(e, ProviderToolUse)]
    assert len(tools) == 1
    assert tools[0].input == {"_raw_arguments": '{"bad'}


async def test_stream_wraps_transport_errors_as_provider_error():
    """传输层异常（迭代中途抛错）要包装成 ProviderError 并带上服务名。"""

    class _RaisingEvents:
        """先吐半截内容再抛连接错误的事件序列（模拟中途断流）。"""

        def __init__(self) -> None:
            self.sent = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self.sent:
                self.sent = True
                return _event(
                    "content_block_delta", index=0,
                    delta=SimpleNamespace(type="text_delta", text="半截"),
                )
            raise RuntimeError("connection reset")

    with pytest.raises(ProviderError, match="anthropic-test"):
        await _collect(_provider(_client_with(_RaisingEvents())))
