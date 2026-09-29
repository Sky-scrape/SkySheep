"""probe：模型探测的客户端生命周期（用完即关，不泄漏 httpx 连接池）。

对应评审发现：probe_provider_models 构造的 AsyncOpenAI/AsyncAnthropic
客户端从不关闭，设置页每点一次「检测可用模型」就泄漏一个客户端。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anthropic
import openai
import pytest

from skysheep.models.probe import probe_provider_models

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
