"""更新链出网回退：直连失败后按序试环境变量代理 → Windows 系统代理。

回归 2026-10 用户反馈：机器只有系统代理一条路（httpx 不读 Windows 系统代理、
trust_env=False 也不读环境变量），检查更新必然 ConnectTimeout。回退只改
「从哪里出去」：更新源 URL 仍是硬编码常量、TLS 照常校验，见
core/uptodate.py 的「出网回退」注。网络路径全部用 MockTransport 打真实
函数（与 test_update_download 同款手法），不打真实网络。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import types

import httpx
import pytest

from skysheep.core import uptodate

_LOCATION = "https://github.com/Sky-scrape/SkySheep/releases/tag/v2.7.0"


def _handler_302():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": _LOCATION})
    return handler


# ---- ProxyServer 取值归一 ----


def test_proxy_server_plain_host_port():
    assert uptodate._proxy_url_from_proxy_server("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert uptodate._proxy_url_from_proxy_server(" 127.0.0.1:7890/") == "http://127.0.0.1:7890"


def test_proxy_server_per_protocol_prefers_https():
    per = "http=10.0.0.1:8080;https=10.0.0.2:8443"
    assert uptodate._proxy_url_from_proxy_server(per) == "http://10.0.0.2:8443"
    assert uptodate._proxy_url_from_proxy_server("http=10.0.0.1:8080") == "http://10.0.0.1:8080"
    assert uptodate._proxy_url_from_proxy_server("socks=127.0.0.1:7891") == "socks5://127.0.0.1:7891"


def test_proxy_server_scheme_passthrough_and_garbage():
    assert uptodate._proxy_url_from_proxy_server("http://p.example.com:8443") == "http://p.example.com:8443"
    assert uptodate._proxy_url_from_proxy_server("") is None
    assert uptodate._proxy_url_from_proxy_server("   ") is None
    assert uptodate._proxy_url_from_proxy_server("=;=;") is None


# ---- 候选顺序与环境缺口 ----


def test_proxy_candidates_order_and_dedupe(monkeypatch):
    monkeypatch.setattr(uptodate, "_env_proxy_url", lambda: "http://env:1")
    monkeypatch.setattr(uptodate, "system_proxy_url", lambda: "http://sys:2")
    assert len(uptodate.proxy_candidates()) == 2

    monkeypatch.setattr(uptodate, "system_proxy_url", lambda: "http://env:1")  # 同址去重
    assert len(uptodate.proxy_candidates()) == 1


def test_proxy_candidates_empty_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(uptodate, "_env_proxy_url", lambda: None)
    monkeypatch.setattr(uptodate, "system_proxy_url", lambda: None)
    assert uptodate.proxy_candidates() == []


def test_proxy_candidates_skips_unconstructible(monkeypatch):
    """缺 socksio 之类环境缺口：只弃用那一条候选，不连累其余路径。"""
    monkeypatch.setattr(uptodate, "_env_proxy_url", lambda: "env-broken")
    monkeypatch.setattr(uptodate, "system_proxy_url", lambda: "http://sys:2")
    real_proxy = httpx.Proxy

    def flaky_proxy(url):
        if url == "env-broken":
            raise ImportError("socksio 未安装")
        return real_proxy(url)

    monkeypatch.setattr(uptodate.httpx, "Proxy", flaky_proxy)
    kept = uptodate.proxy_candidates()
    assert [str(c.url) for c in kept] == ["http://sys:2"]


# ---- Windows 系统代理读取（打桩 winreg，不碰真实注册表）----


def _install_fake_winreg(monkeypatch, values):
    """values 缺键时抛 FileNotFoundError——与真 winreg 行为一致。"""

    def _query(key, name):
        if name not in values:
            raise FileNotFoundError(2, name)
        return values[name], 0

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda *a, **k: contextlib.nullcontext(object()),
        QueryValueEx=_query,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)


def test_system_proxy_url_reads_registry(monkeypatch):
    _install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7890"})
    assert uptodate.system_proxy_url() == "http://127.0.0.1:7890"


def test_system_proxy_url_disabled_or_missing(monkeypatch):
    _install_fake_winreg(monkeypatch, {"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7890"})
    assert uptodate.system_proxy_url() is None, "系统代理未启用不算候选"
    _install_fake_winreg(monkeypatch, {"ProxyEnable": 1})  # 缺 ProxyServer 键
    assert uptodate.system_proxy_url() is None


def test_system_proxy_url_registry_error_swallowed(monkeypatch):
    import types

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda *a, **k: (_ for _ in ()).throw(PermissionError(5, "registry")),
        QueryValueEx=lambda *a, **k: (1, 0),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert uptodate.system_proxy_url() is None


# ---- 客户端构造参数：transport（测试注入）与 proxy（回退）互斥 ----


def test_client_kwargs_mutually_exclusive():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    kw = uptodate.client_kwargs(6.0, None, None)
    assert kw == {"timeout": 6.0, "trust_env": False, "follow_redirects": False}

    assert "proxy" not in uptodate.client_kwargs(6.0, transport, None)
    proxy = httpx.Proxy("http://127.0.0.1:9")
    kw = uptodate.client_kwargs(6.0, None, proxy, follow_redirects=True)
    assert kw["proxy"] is proxy and "transport" not in kw and kw["follow_redirects"] is True


# ---- fetch_latest_release 的回退链 ----


def test_fetch_falls_back_when_direct_cannot_connect():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("直连超时", request=request)
        return httpx.Response(302, headers={"Location": _LOCATION})

    seen = {"candidates": 0}

    def candidates():
        seen["candidates"] += 1
        return [httpx.Proxy("http://127.0.0.1:9")]

    info = asyncio.run(uptodate.fetch_latest_release(
        transport=httpx.MockTransport(handler), candidates_fn=candidates))
    assert info["version"] == "2.7.0"
    assert calls["n"] == 2, "直连失败后应经候选路径重试一次"
    assert seen["candidates"] == 1, "候选只在首连失败后构建"


def test_fetch_direct_success_never_builds_candidates():
    seen = {"n": 0}

    def candidates():
        seen["n"] += 1
        return []

    info = asyncio.run(uptodate.fetch_latest_release(
        transport=httpx.MockTransport(_handler_302()), candidates_fn=candidates))
    assert info["version"] == "2.7.0"
    assert seen["n"] == 0, "直连成功不碰代理候选（也不读注册表）"


def test_fetch_without_candidates_raises_last_connect_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("超时", request=request)

    with pytest.raises(RuntimeError, match="无法连接更新源：ConnectTimeout"):
        asyncio.run(uptodate.fetch_latest_release(
            transport=httpx.MockTransport(handler), candidates_fn=list))
    assert calls["n"] == 1, "没有候选路径时不重复尝试"


def test_fetch_http_status_error_does_not_fall_back():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502)

    with pytest.raises(RuntimeError, match="更新源返回 HTTP 502"):
        asyncio.run(uptodate.fetch_latest_release(
            transport=httpx.MockTransport(handler), candidates_fn=list))
    assert calls["n"] == 1, "链路已通的状态错误不属于连不上，不回退"


def test_fetch_read_timeout_does_not_fall_back():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("读到一半断了", request=request)

    with pytest.raises(RuntimeError, match="无法连接更新源：ReadTimeout"):
        asyncio.run(uptodate.fetch_latest_release(
            transport=httpx.MockTransport(handler), candidates_fn=list))
    assert calls["n"] == 1
