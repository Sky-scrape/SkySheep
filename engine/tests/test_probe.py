"""probe：模型探测的客户端生命周期（用完即关，不泄漏 httpx 连接池）。

对应评审发现：probe_provider_models 构造的 AsyncOpenAI/AsyncAnthropic
客户端从不关闭，设置页每点一次「检测可用模型」就泄漏一个客户端。
后半部分（2026-10 审查项 10）：probe_context_limit 的 Anthropic 短路 /
Ollama / openai 兼容分支与 probe_ollama——此前探测主体零执行（现有用例
全是 monkeypatch 替身），用 MockTransport 打真实实现，不打真实网络。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

from skysheep.models.probe import (
    probe_context_limit,
    probe_ollama,
    probe_provider_models,
)

# 桩可按用例注入的故障（每个用例先经 fake_sdk 夹具清零）
_STATE: dict = {"list_error": None, "close_error": None}


class _FakeModels:
    async def list(self, limit=None):
        err = _STATE["list_error"]
        if err is not None:
            raise err
        return SimpleNamespace(data=[SimpleNamespace(id="m2"), SimpleNamespace(id="m1")])


class _FakeClient:
    """同时顶替 AsyncOpenAI / AsyncAnthropic（两者用法在 probe 里同构）。"""

    instances: list[_FakeClient] = []

    def __init__(self, **kw) -> None:
        self.kwargs = kw
        self.closed = False
        self.models = _FakeModels()
        self.instances.append(self)

    async def close(self):
        err = _STATE["close_error"]
        if err is not None:
            raise err
        self.closed = True


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeClient.instances.clear()
    _STATE["list_error"] = None
    _STATE["close_error"] = None
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeClient)
    return _FakeClient


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_probe_closes_openai_client(fake_sdk):
    ids = _run(probe_provider_models(kind="openai", api_key="k", base_url="http://x"))
    assert ids == ["m1", "m2"]
    (client,) = _FakeClient.instances
    assert client.closed, "探测成功后客户端必须关闭"
    assert client.kwargs["api_key"] == "k"


def test_probe_closes_anthropic_client(fake_sdk):
    _run(probe_provider_models(kind="anthropic", api_key="k", base_url="http://x"))
    (client,) = _FakeClient.instances
    assert client.closed, "探测成功后客户端必须关闭"


def test_probe_closes_client_when_list_fails(fake_sdk):
    """查询失败（如 401）也要关闭客户端，且错误照常转成可读提示。"""
    _STATE["list_error"] = _HttpError(401)
    with pytest.raises(RuntimeError, match="API Key"):
        _run(probe_provider_models(kind="openai", api_key="k", base_url="http://x"))
    (client,) = _FakeClient.instances
    assert client.closed, "失败路径也要关闭客户端"


def test_probe_close_failure_does_not_mask_result(fake_sdk):
    """收尾关闭自身的失败不掩盖成功的探测结果（成功路径行为不变）。"""
    _STATE["close_error"] = RuntimeError("close boom")
    ids = _run(probe_provider_models(kind="openai", api_key="k", base_url="http://x"))
    assert ids == ["m1", "m2"]


def test_probe_local_base_url_keeps_direct_client(fake_sdk):
    """本地地址（不走系统代理）的既有行为不回归：仍注入 trust_env=False 客户端。"""

    async def go():
        await probe_provider_models(
            kind="openai", api_key="k", base_url="http://127.0.0.1:11434/v1")
        (client,) = _FakeClient.instances
        http_client = client.kwargs.get("http_client")
        try:
            assert http_client is not None and http_client.trust_env is False
        finally:
            # 桩不替我们关 httpx 客户端，测试自己收尾
            if http_client is not None:
                await http_client.aclose()

    asyncio.new_event_loop().run_until_complete(go())


# ---- probe_context_limit / probe_ollama：探测主体（2026-10 审查项 10） ----


def _mock_async_httpx(monkeypatch, handler):
    """给 probe 内部自建的 httpx.AsyncClient 注入 MockTransport（其余参数原样）。"""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_probe_context_limit_anthropic_short_circuits_without_network():
    """Anthropic 的 models 接口不带窗口字段：短路返回提示，不发任何请求。"""
    r = await probe_context_limit(kind="anthropic", api_key="k", model="claude-x")
    assert r["limit"] is None
    assert "Anthropic" in r["note"] and "200K" in r["note"]


async def test_probe_context_limit_ollama_top_level_field(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show"
        return httpx.Response(200, json={"context_length": 8192})

    _mock_async_httpx(monkeypatch, handler)
    r = await probe_context_limit(kind="ollama", model="qwen2")
    assert r["limit"] == 8192 and "8,192" in r["note"] and "qwen2" in r["note"]


async def test_probe_context_limit_ollama_model_info_fallback(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model_info": {"qwen2.context_length": 4096}})

    _mock_async_httpx(monkeypatch, handler)
    r = await probe_context_limit(kind="ollama", model="qwen2")
    assert r["limit"] == 4096


async def test_probe_context_limit_ollama_no_window_info(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"license": "llama"})

    _mock_async_httpx(monkeypatch, handler)
    r = await probe_context_limit(kind="ollama", model="qwen2")
    assert r["limit"] is None and "没有返回" in r["note"]


async def test_probe_context_limit_ollama_http_error_is_readable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _mock_async_httpx(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="服务返回错误 500"):
        await probe_context_limit(kind="ollama", model="qwen2")


async def test_probe_context_limit_openai_finds_window_field(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"data": [
            {"id": "other", "context_length": 1},
            {"id": "m1", "context_length": 32768},
        ]})

    _mock_async_httpx(monkeypatch, handler)
    r = await probe_context_limit(
        kind="openai", api_key="sk-test", base_url="https://api.example.com/v1",
        model="m1",
    )
    assert r["limit"] == 32768 and "32,768" in r["note"] and "m1" in r["note"]


async def test_probe_context_limit_openai_openrouter_nested_field(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"id": "deep", "top_provider": {"context_length": 65536}},
        ]})

    _mock_async_httpx(monkeypatch, handler)
    # 不给 model 且列表只有一条：自动取唯一条目
    r = await probe_context_limit(
        kind="openai", api_key="k", base_url="https://openrouter.ai/api/v1",
    )
    assert r["limit"] == 65536


async def test_probe_context_limit_openai_target_missing_and_no_field(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"id": "other", "context_length": 1}, {"id": "another", "context_length": 2},
        ]})

    _mock_async_httpx(monkeypatch, handler)
    base = dict(kind="openai", api_key="k", base_url="https://api.example.com/v1")
    r = await probe_context_limit(model="ghost", **base)
    assert r["limit"] is None and "ghost" in r["note"]
    # 列表多条且未指定模型：不乱猜
    r = await probe_context_limit(**base)
    assert r["limit"] is None and "模型 ID" in r["note"]


async def test_probe_context_limit_openai_nonstandard_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})  # 没有 data 字段

    _mock_async_httpx(monkeypatch, handler)
    r = await probe_context_limit(
        kind="openai", api_key="k", base_url="https://api.example.com/v1", model="m",
    )
    assert r["limit"] is None and "标准格式" in r["note"]


async def test_probe_ollama_returns_names_and_filters_empty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [
            {"name": "qwen2:7b"}, {"name": ""}, {}, {"name": "llama3"},
        ]})

    _mock_async_httpx(monkeypatch, handler)
    assert await probe_ollama() == ["qwen2:7b", "llama3"]


async def test_probe_ollama_quiet_when_service_down(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_async_httpx(monkeypatch, handler)
    assert await probe_ollama() == []


async def test_probe_ollama_quiet_on_bad_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    _mock_async_httpx(monkeypatch, handler)
    assert await probe_ollama() == []
