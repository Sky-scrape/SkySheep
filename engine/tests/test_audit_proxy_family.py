"""出网代理族审计修复回归（SEC-M11 / M12 / M13 / M19）。

覆盖点（全部打桩/参数断言 + 本地 socket，不发真实外网请求）：
- M11：web_fetch 代理出网形态分流（direct 完整钉连 / socks5 本地把已校验 IP
  交给代理建连 / http·socks5h 代理解析无法钉连并在结果注记与审计日志如实标注）；
- M12：远程 MCP 钉连 fail-closed——preflight 解析批次直接钉给建连（回环钉死
  回环批次、公网目标钉死公网批次），钉连安装失败时公网目标拒绝建连；
- M13：自签 CA 全路径生效——代理分支附加 verify、web_fetch 直连 transport 携带
  CA（make_async_transport）、直连工厂自建 transport 附加 verify；
- M19：模型流量绕行——provider 传 url=base_url 让 no_proxy 生效、本地地址显式
  直连、服务级代理三态（None=跟随全局 / ""=显式直连 / 非空=该代理）不再被
  `proxy or None` 塌缩。

home 夹具按需隔离 SKYSHEEP_HOME（run() 会写信誉存储等），本文件不触真实 home。
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import skysheep.mcp.client as mcp_client
import skysheep.network as network_mod
from skysheep.config import ProviderConfig
from skysheep.mcp.client import MCPManager, MCPServerConfig
from skysheep.network import (
    NetworkConfig,
    is_local_base_url,
    make_async_httpx2_client,
    make_async_httpx_client,
    make_async_transport,
)
from skysheep.tools import web as web_mod
from skysheep.tools.base import ToolContext, ToolError
from skysheep.tools.web import WebFetchArgs, WebFetchTool

# ---------------- 公共小工具 ----------------


def _make_ca_and_leaf_cert(tmp_path):
    """生成自签 CA + 由它签发、带 127.0.0.1 IP SAN 的叶证书。

    返回 (ca_pem, leaf_cert_pem, leaf_key_pem)——叶证书给本地 TLS 服务端用，
    CA 给 NetworkConfig.ca_cert 用。cryptography 缺席则整组跳过。
    """
    pytest.importorskip("cryptography")
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "audit-proxy-root")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca.pem"
    leaf_pem = tmp_path / "leaf.pem"
    leaf_key_pem = tmp_path / "leaf-key.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    leaf_pem.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    leaf_key_pem.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, leaf_pem, leaf_key_pem


def _proxy_mount(client):
    """取客户端的全站代理 mount（httpx 0.28：proxy 以 mount 实现）。"""
    mounts = getattr(client, "_mounts", {})
    assert mounts, "配了代理时应存在代理 mount"
    return next(iter(mounts.values()))


def _mount_proxy_url(client) -> str:
    mount = _proxy_mount(client)
    url = getattr(mount._pool, "_proxy_url", None)
    assert url is not None, "代理 mount 应携带代理地址"
    return url.host.decode() + (f":{url.port}" if url.port else "")


# ---------------- M13：自签 CA 全路径生效（工厂参数级 + 本地 TLS 实测） ----------------


def test_proxy_path_attaches_ca_verify(monkeypatch):
    """代理 + 自签 CA 同时生效：代理 mount 的连接池拿到 CA 上下文（M13 核心）。

    用打桩的 _verify 固定 SSLContext 身份，做参数级断言（OpenSSL 3.5 起
    get_ca_certs 不再列出 load_verify_locations 的证书，内容比对不可靠）。
    """
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(network_mod, "_verify", lambda net: sentinel)
    net = NetworkConfig(proxy="http://127.0.0.1:7890", ca_cert="stub.pem")

    async def go():
        async with make_async_httpx_client(net, url="https://api.example.com") as c:
            return _proxy_mount(c)._pool._ssl_context is sentinel

    assert asyncio.run(go()) is True

    async def go2():
        async with make_async_httpx2_client(net, url="https://api.example.com") as c:
            return _proxy_mount(c)._pool._ssl_context is sentinel

    assert asyncio.run(go2()) is True, "httpx2 工厂（anthropic 路径）同样要在代理上附加 CA"


def test_direct_factory_without_transport_attaches_ca(monkeypatch):
    """直连 + 工厂自建 transport：verify 附加到客户端（CA 生效）。"""
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(network_mod, "_verify", lambda net: sentinel)
    net = NetworkConfig(ca_cert="stub.pem")

    async def go():
        async with make_async_httpx_client(net) as c:
            return c._transport._pool._ssl_context is sentinel

    assert asyncio.run(go()) is True


def test_direct_transport_keeps_identity_and_baked_ca(monkeypatch):
    """直连 + 调用方 transport：transport 原样使用，CA 由 make_async_transport
    烧进 transport（web_fetch 直连路径的 M13 修法），工厂不再叠加 verify。"""
    sentinel = ssl.create_default_context()
    monkeypatch.setattr(network_mod, "_verify", lambda net: sentinel)
    net = NetworkConfig(ca_cert="stub.pem")
    transport = make_async_transport(net)
    assert transport._pool._ssl_context is sentinel, (
        "make_async_transport 必须把 CA 烧进 transport"
    )

    async def go():
        async with make_async_httpx_client(net, transport=transport) as c:
            return c._transport is transport

    assert asyncio.run(go()) is True


def test_web_fetch_https_direct_with_custom_ca_end_to_end(tmp_path):
    """M13 实测：web_fetch 直连抓取「仅由自签 CA 签发证书」的 https 站点——
    配了 ca_cert 能过 TLS 校验，没配则失败（此前 web_fetch 恒传无 verify 的
    裸 transport，CA 对 web_fetch 全路径断链）。全程本地 socket。"""
    ca_pem, leaf_pem, leaf_key_pem = _make_ca_and_leaf_cert(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"ca-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(leaf_pem), str(leaf_key_pem))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.socket = server_ctx.wrap_socket(srv.socket, server_side=True)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"https://127.0.0.1:{port}/x"
        tool = WebFetchTool(
            network=NetworkConfig(ca_cert=str(ca_pem)), allow_private_hosts=True
        )
        status, _h, body = asyncio.run(
            tool._fetch_once(url, urlparse(url), ["127.0.0.1"])
        )
        assert status == 200 and body == b"ca-ok", "自签 CA 必须对 web_fetch 生效"

        # 对照组：不配 CA（只用系统库）→ TLS 校验失败（fail-closed 包装为 ToolError）
        plain = WebFetchTool(allow_private_hosts=True)
        with pytest.raises(ToolError) as ei:
            asyncio.run(plain._fetch_once(url, urlparse(url), ["127.0.0.1"]))
        assert "CERTIFICATE_VERIFY_FAILED" in str(ei.value)
    finally:
        srv.shutdown()


def test_proxy_override_wins_and_bypass_stays_direct(monkeypatch):
    """三态 proxy_override 在工厂层的形态：None=跟随全局 / ""=直连 / 非空=该代理。"""
    net = NetworkConfig(proxy="http://global:7890")

    async def mounted():
        async with make_async_httpx_client(net, url="https://api.example.com") as c:
            return bool(c._mounts)

    assert asyncio.run(mounted()) is True, "None=跟随全局代理"

    async def direct():
        async with make_async_httpx_client(net, url="https://api.example.com", proxy_override="") as c:
            return bool(c._mounts)

    assert asyncio.run(direct()) is False, '空串=显式直连（不吃全局代理）'

    async def svc():
        async with make_async_httpx_client(
            net, url="https://api.example.com", proxy_override="http://svc:1080"
        ) as c:
            return _mount_proxy_url(c)

    assert asyncio.run(svc()) == "svc:1080", "非空=用该服务自己的代理"


# ---------------- M11：web_fetch 代理出网形态分流 ----------------


def test_egress_mode_classification():
    tool = WebFetchTool(network=NetworkConfig())
    assert tool._egress_mode("https://example.com/x") == "direct"

    tool = WebFetchTool(network=NetworkConfig(proxy="socks5://127.0.0.1:1080"))
    assert tool._egress_mode("https://example.com/x") == "socks5"
    tool = WebFetchTool(network=NetworkConfig(proxy="socks5h://127.0.0.1:1080"))
    assert tool._egress_mode("https://example.com/x") == "proxy", (
        "socks5h 由代理解析：无法钉连，按 proxy 形态如实标注"
    )
    tool = WebFetchTool(network=NetworkConfig(proxy="http://127.0.0.1:7890"))
    assert tool._egress_mode("https://example.com/x") == "proxy"
    # no_proxy 绕行 = 直连，完整钉连
    tool = WebFetchTool(
        network=NetworkConfig(proxy="http://127.0.0.1:7890", no_proxy="example.com")
    )
    assert tool._egress_mode("https://example.com/x") == "direct"
    assert tool._egress_mode("https://other.example/x") == "proxy"


def test_ip_form_url_keeps_port_and_wraps_ipv6():
    assert (
        WebFetchTool._ip_form_url("http://example.com:8080/x?a=1", "203.0.113.9")
        == "http://203.0.113.9:8080/x?a=1"
    )
    assert (
        WebFetchTool._ip_form_url("https://example.com/", "2001:db8::1")
        == "https://[2001:db8::1]/"
    )
    assert (
        WebFetchTool._ip_form_url("http://example.com", "198.51.100.7")
        == "http://198.51.100.7"
    )


def test_socks5_pin_hands_verified_ip_to_proxy():
    """M11 核心：socks5 场景把已校验 IP 交给代理建连（ATYP=IPv4、目标即校验 IP），
    隧道内 HTTP 请求保留原主机名 Host——钉连语义完整恢复，SNI/虚拟主机不丢。"""
    pytest.importorskip("socksio")
    captured: list[tuple] = []

    async def _handle(reader, writer):
        # 最小 SOCKS5 服务端：握手 → 回成功 → 读隧道内 HTTP 请求 → 回固定响应
        ver, nmethods = await reader.readexactly(2)
        assert ver == 5
        await reader.readexactly(nmethods)
        writer.write(b"\x05\x00")
        await writer.drain()
        hdr = await reader.readexactly(4)
        atyp = hdr[3]
        if atyp == 1:
            addr = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            addr = (await reader.readexactly(ln)).decode()
        else:  # pragma: no cover - 本用例只产生 IPv4 目标
            addr = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        port = int.from_bytes(await reader.readexactly(2), "big")
        captured.append((atyp, addr, port))
        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 4 + b"\x00\x00")  # 成功应答
        await writer.drain()
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = await reader.read(4096)
            if not chunk:
                break
            req += chunk
        captured.append(req)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    async def go():
        server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            tool = WebFetchTool(network=NetworkConfig(proxy=f"socks5://127.0.0.1:{port}"))
            url = "http://example.com/x"
            status, headers, body = await tool._fetch_once(
                url, urlparse(url), ["203.0.113.9"]
            )
            return status, headers, body
        finally:
            server.close()
            await server.wait_closed()

    status, _headers, body = asyncio.run(go())
    assert status == 200
    assert body == b"ok"
    atyp, addr, port_seen = captured[0]
    assert atyp == 1, "IP 形态目标应以 ATYP=IPv4 交给代理（而不是把主机名交去代理解析）"
    assert addr == "203.0.113.9" and port_seen == 80, "交给代理建连的必须是已校验 IP"
    req = captured[1]
    assert b"Host: example.com" in req, "原主机名必须保留在 Host 头（虚拟主机不丢）"


def test_web_fetch_proxy_mode_annotates_result(home, monkeypatch):
    """代理解析场景（http/socks5h）：结果注记 + 审计日志如实标注「代理解析未做
    DNS 钉连校验」，不再静默降级。"""
    tool = WebFetchTool(network=NetworkConfig(proxy="http://127.0.0.1:7890"))
    monkeypatch.setattr(tool, "_pin", lambda host: ["203.0.113.9"])

    async def fake_fetch_once(url, parsed, ips):
        return 200, {"content-type": "text/plain"}, b"plain body"

    monkeypatch.setattr(tool, "_fetch_once", fake_fetch_once)
    logs: list[tuple] = []
    monkeypatch.setattr(
        web_mod.obs, "info", lambda ev, msg, **kw: logs.append((ev, kw))
    )
    out = asyncio.run(
        tool.run(WebFetchArgs(url="https://docs.example.com/guide"),
                 ToolContext(working_dir=None))
    )
    assert "plain body" in out
    assert "代理解析未做 DNS 钉连校验" in out, "结果注记必须点明代理解析的残余风险"
    audit = [kw for ev, kw in logs if ev == "web_fetch"]
    assert audit and audit[-1].get("proxy_unpinned") is True
    assert audit[-1].get("egress") == "proxy"


def test_web_fetch_socks5_mode_no_unpinned_note(home, monkeypatch):
    """socks5（本地解析 + IP 交给代理）场景：钉连语义完整，不附加降级注记。"""
    tool = WebFetchTool(network=NetworkConfig(proxy="socks5://127.0.0.1:1080"))
    monkeypatch.setattr(tool, "_pin", lambda host: ["203.0.113.9"])

    async def fake_fetch_once(url, parsed, ips):
        return 200, {"content-type": "text/plain"}, b"plain body"

    monkeypatch.setattr(tool, "_fetch_once", fake_fetch_once)
    logs: list[tuple] = []
    monkeypatch.setattr(
        web_mod.obs, "info", lambda ev, msg, **kw: logs.append((ev, kw))
    )
    out = asyncio.run(
        tool.run(WebFetchArgs(url="https://docs.example.com/guide"),
                 ToolContext(working_dir=None))
    )
    assert "代理解析未做 DNS 钉连校验" not in out
    audit = [kw for ev, kw in logs if ev == "web_fetch"]
    assert audit and audit[-1].get("egress") == "socks5"
    assert audit[-1].get("proxy_unpinned") is False


# ---------------- M12：远程 MCP 钉连 fail-closed ----------------


async def _start_noop_server() -> asyncio.AbstractServer:
    async def _noop(reader, writer) -> None:
        writer.close()

    return await asyncio.start_server(_noop, "127.0.0.1", 0)


class _FakeSession:
    """SDK ClientSession 替身：initialize 即停（钉连相关断言在此之前完成）。"""

    def __init__(self, read, write, message_handler=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        raise RuntimeError("sentinel-stop")


def _patch_mcp_sdk(monkeypatch, pool_holder):
    """把 mcp SDK 的建链组件换成桩：httpx2 客户端带可观测 pool、流客户端直通。"""
    import mcp as mcp_pkg
    import mcp.client.streamable_http as mcp_streamable

    class _FakeClient:
        def __init__(self, **kw):
            self._transport = SimpleNamespace(_pool=pool_holder["pool"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    @asynccontextmanager
    async def fake_streamable(url, http_client=None):
        yield object(), object()

    monkeypatch.setattr(mcp_pkg, "ClientSession", _FakeSession)
    monkeypatch.setattr(mcp_streamable, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(
        mcp_client, "make_async_httpx2_client",
        lambda network, **kw: _FakeClient(),
    )


async def test_mcp_pins_preflight_batch_without_second_resolution(monkeypatch):
    """M12：建连钉死 preflight 解析并探测过的同一批 IP，且不再做第二次解析。"""
    pins = ["203.0.113.9"]

    async def fake_pf(cfg):
        return None, list(pins)

    def explode(*a, **k):  # 若建连侧再次解析 DNS，本用例立刻红
        raise AssertionError("建连侧不得进行第二次 DNS 解析")

    monkeypatch.setattr(mcp_client, "_preflight_with_pins", fake_pf)
    monkeypatch.setattr(mcp_client, "_resolve_public_ips", explode)

    class _CapPool:
        _network_backend = None

    pool_holder = {"pool": _CapPool()}
    _patch_mcp_sdk(monkeypatch, pool_holder)

    manager = MCPManager({"srv": MCPServerConfig(url="http://example.com/mcp")})
    tools = await manager.connect_all()
    assert tools == []  # initialize 桩即停：连接不成立，但钉连已装
    st = manager.statuses["srv"]
    assert "sentinel-stop" in (st.error or ""), "不应有其他异常路径介入"
    backend = pool_holder["pool"]._network_backend
    assert isinstance(backend, mcp_client._MCPPinnedBackend)
    assert backend._host == "example.com"
    assert backend._ips == pins, "钉给 SDK 建连的必须是 preflight 那批已校验 IP"
    await manager.shutdown()


async def test_mcp_loopback_pins_preflight_loopback_batch(monkeypatch):
    """M12：回环目标（本机 MCP）把建连钉死在 preflight 解析出的回环批次上，
    保住本机 MCP 可用，同时不回退默认解析。"""
    server = await _start_noop_server()
    port = server.sockets[0].getsockname()[1]
    try:
        class _CapPool:
            _network_backend = None

        pool_holder = {"pool": _CapPool()}
        _patch_mcp_sdk(monkeypatch, pool_holder)

        manager = MCPManager({"srv": MCPServerConfig(url=f"http://localhost:{port}/mcp")})
        tools = await manager.connect_all()
        assert tools == []
        assert "sentinel-stop" in (manager.statuses["srv"].error or "")
        backend = pool_holder["pool"]._network_backend
        assert isinstance(backend, mcp_client._MCPPinnedBackend)
        assert backend._host == "localhost"
        assert backend._ips, "回环批次必须非空（来自 preflight 的解析）"
        import ipaddress

        assert all(ipaddress.ip_address(ip).is_loopback for ip in backend._ips)
        await manager.shutdown()
    finally:
        server.close()
        await server.wait_closed()


async def test_mcp_public_target_pin_install_failure_fails_closed(monkeypatch):
    """M12：公网目标钉连安装失败时拒绝建连（fail-closed），不再退回默认解析。"""

    async def fake_pf(cfg):
        return None, ["203.0.113.9"]

    monkeypatch.setattr(mcp_client, "_preflight_with_pins", fake_pf)
    # pool 没有 _network_backend 属性：模拟 httpx2 结构变化 → 安装失败
    pool_holder = {"pool": object()}
    _patch_mcp_sdk(monkeypatch, pool_holder)

    manager = MCPManager({"srv": MCPServerConfig(url="http://example.com/mcp")})
    tools = await manager.connect_all()
    assert tools == []
    st = manager.statuses["srv"]
    assert st.connected is False
    assert "无法固定已校验地址" in (st.error or ""), "公网目标必须 fail-closed"
    await manager.shutdown()


async def test_mcp_loopback_pin_install_failure_keeps_local_usable(monkeypatch):
    """M12：回环目标钉连安装失败时放行（默认解析仍是回环类别，本机 MCP 不被
    误伤），但必须留结构化告警。"""

    async def fake_pf(cfg):
        return None, ["127.0.0.1"]

    monkeypatch.setattr(mcp_client, "_preflight_with_pins", fake_pf)
    pool_holder = {"pool": object()}
    _patch_mcp_sdk(monkeypatch, pool_holder)
    warnings: list[tuple] = []
    monkeypatch.setattr(
        mcp_client.obs, "warning", lambda ev, msg, **kw: warnings.append((ev, msg))
    )

    manager = MCPManager({"srv": MCPServerConfig(url="http://localhost:8000/mcp")})
    tools = await manager.connect_all()
    assert tools == []
    st = manager.statuses["srv"]
    assert "无法固定已校验地址" not in (st.error or ""), "本机目标不该被 fail-closed 误伤"
    assert "sentinel-stop" in (st.error or "")
    assert any(ev == "mcp_pin_unavailable" for ev, _msg in warnings)
    await manager.shutdown()


async def test_mcp_proxied_connection_logs_honest_annotation(monkeypatch):
    """M11：走代理的远程 MCP 不再宣称「威胁面不存在」，留结构化日志标注。"""
    manager = MCPManager(
        {"srv": MCPServerConfig(url="http://example.com/mcp")},
        network=NetworkConfig(proxy="http://127.0.0.1:7890"),
    )
    infos: list[tuple] = []
    monkeypatch.setattr(
        mcp_client.obs, "info", lambda ev, msg, **kw: infos.append((ev, kw))
    )

    async def fake_pf(cfg):
        return None, ["203.0.113.9"]

    monkeypatch.setattr(mcp_client, "_preflight_with_pins", fake_pf)
    pool_holder = {"pool": object()}
    _patch_mcp_sdk(monkeypatch, pool_holder)

    tools = await manager.connect_all()
    assert tools == []
    assert any(ev == "mcp_proxy_egress" for ev, _kw in infos), "代理路径必须留标注日志"
    assert "sentinel-stop" in (manager.statuses["srv"].error or "")
    await manager.shutdown()


# ---------------- M19：模型流量绕行 / 三态语义 ----------------


def test_provider_config_proxy_default_is_none():
    """三态默认：ProviderConfig.proxy 默认 None（跟随全局）；显式空串保持 ""。"""
    assert ProviderConfig().proxy is None
    assert ProviderConfig(proxy="").proxy == ""
    assert ProviderConfig(proxy="http://127.0.0.1:7890").proxy == "http://127.0.0.1:7890"


def test_openai_provider_follows_global_proxy_by_default():
    """服务级 proxy=None（TOML 无键）：对话路径跟随全局代理。"""
    from skysheep.models.openai_compat import OpenAICompatProvider

    net = NetworkConfig(proxy="http://global:7890")
    p = OpenAICompatProvider(
        "x", "m", "k", base_url="http://api.example.com/v1", network=net
    )
    assert _mount_proxy_url(p._owned_http) == "global:7890"


def test_openai_provider_empty_string_forces_direct():
    """服务级 proxy=""：显式直连，不吃全局代理（`proxy or None` 塌缩已修）。"""
    from skysheep.models.openai_compat import OpenAICompatProvider

    net = NetworkConfig(proxy="http://global:7890")
    p = OpenAICompatProvider(
        "x", "m", "k", base_url="http://api.example.com/v1", proxy="", network=net
    )
    assert not p._owned_http._mounts, "空串必须是「直连」而不是「跟随全局」"


def test_openai_provider_service_proxy_overrides_global():
    from skysheep.models.openai_compat import OpenAICompatProvider

    net = NetworkConfig(proxy="http://global:7890")
    p = OpenAICompatProvider(
        "x", "m", "k", base_url="http://api.example.com/v1",
        proxy="http://svc:1080", network=net,
    )
    assert _mount_proxy_url(p._owned_http) == "svc:1080"


def test_openai_provider_no_proxy_list_now_applies_to_model_traffic():
    """M19①：provider 传 url=base_url 后，no_proxy 绕行名单对模型流量生效。"""
    from skysheep.models.openai_compat import OpenAICompatProvider

    net = NetworkConfig(proxy="http://global:7890", no_proxy="api.example.com")
    p = OpenAICompatProvider(
        "x", "m", "k", base_url="http://api.example.com/v1", network=net
    )
    assert not p._owned_http._mounts, "绕行名单命中的模型服务必须直连"
    # 对照：不在名单上的服务仍走全局代理
    p2 = OpenAICompatProvider(
        "x", "m", "k", base_url="http://other.example.com/v1", network=net
    )
    assert p2._owned_http._mounts


def test_openai_provider_local_base_url_is_explicitly_direct():
    """M19②：本地模型服务（LM Studio / Ollama）显式直连，与探测路径同口径。"""
    from skysheep.models.openai_compat import OpenAICompatProvider

    net = NetworkConfig(proxy="http://global:7890")  # 不配 no_proxy 也要直连
    for base in ("http://127.0.0.1:1234/v1", "http://localhost:11434/v1"):
        p = OpenAICompatProvider("x", "m", "k", base_url=base, network=net)
        assert not p._owned_http._mounts, f"{base} 必须直连"


def test_anthropic_provider_three_state_and_local_direct():
    """anthropic 路径（httpx2 工厂）与 openai 路径同口径。"""
    from skysheep.models.anthropic_provider import AnthropicProvider

    net = NetworkConfig(proxy="http://global:7890")
    p_follow = AnthropicProvider(
        "a", "claude", "k", base_url="http://api.example.com", network=net
    )
    assert _mount_proxy_url(p_follow._owned_http) == "global:7890"
    p_direct = AnthropicProvider(
        "a", "claude", "k", base_url="http://api.example.com", proxy="", network=net
    )
    assert not p_direct._owned_http._mounts
    p_local = AnthropicProvider(
        "a", "claude", "k", base_url="http://127.0.0.1:5678", network=net
    )
    assert not p_local._owned_http._mounts


def test_is_local_judgement_shared_with_probe():
    """判定单一事实来源：probe._is_local 委托 network.is_local_base_url。"""
    from skysheep.models.probe import _is_local

    for url in ("http://localhost:1234/v1", "http://127.0.0.1:11434",
                "http://box.local:8000", "http://[::1]:9000"):
        assert _is_local(url) is True and is_local_base_url(url) is True
    for url in ("https://api.example.com", None, ""):
        assert _is_local(url) is False and is_local_base_url(url) is False


# ---------------- M19 前端半场：设置页保存链路的三态往返 ----------------
# 独立复核发现的语义回退：backend 的 ProviderConfig.proxy 已三态化，但前端保存
# 链路总回传 proxy:""（detail 还把 None 塌缩成 ""），配了全局代理的用户在设置页
# 改任意字段保存，该服务就被静默切成「强制直连」。修复后：未改动不携带该字段，
# detail 原样回传三态。以下用例锁住「缺键=不动盘面」这一契约，防回归。


def test_update_provider_proxy_none_keeps_existing_key(home):
    """config 层：proxy=None 表示「不动该键」，不是清除也不是写空串。"""
    import tomllib

    from skysheep.config import config_path, load_config, update_provider_in_config

    # 跟随全局（无键）→ 不带 proxy 保存别的字段：键仍不出现
    update_provider_in_config("deepseek", model="deepseek-chat")
    assert load_config().providers["deepseek"].proxy is None
    raw = tomllib.loads(config_path().read_text(encoding="utf-8"))
    assert "proxy" not in raw["providers"]["deepseek"], "未显式提供时不得写 proxy 键"

    # 已有专属代理 → 不带 proxy 保存别的字段：原值保留
    update_provider_in_config("deepseek", proxy="http://p:1")
    update_provider_in_config("deepseek", model="deepseek-chat")
    assert load_config().providers["deepseek"].proxy == "http://p:1"

    # 显式空串 = 强制直连（写空串，不删键）
    update_provider_in_config("deepseek", proxy="")
    assert load_config().providers["deepseek"].proxy == ""


def test_proxy_three_state_save_roundtrip_via_ws(home):
    """设置页保存链路三态往返（模拟修复后前端的携带策略）：

    - 不带 proxy 键保存 → 配置文件里不出现该键（跟随全局保持），detail 回 null；
    - 显式 proxy="" 保存 → 落盘空串（强制直连）；
    - proxy="http://…" 保存 → 原样落盘；
    - 之后不带键再保存任意字段 → 已有的直连/代理设置不被改写。
    """
    import tomllib

    from test_server import make_client, recv_until

    from skysheep.config import config_path

    def raw_proxy():
        raw = tomllib.loads(config_path().read_text(encoding="utf-8"))
        return (raw.get("providers").get("deepseek") or {}).get("proxy")

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 1) 跟随全局：不带 proxy 键保存（前端「未改动不携带」的行为）
        ws.send_json({"id": "t1", "method": "config.save_provider", "params": {
            "name": "deepseek", "model": "deepseek-chat"}})
        r1 = recv_until(ws, "t1")
        assert r1["ok"], r1.get("error")
        assert raw_proxy() is None, "未显式提供 proxy 时配置文件里不得出现该键"

        ws.send_json({"id": "t2", "method": "config.providers", "params": {}})
        d = recv_until(ws, "t2")["result"]["providers"]["deepseek"]
        assert d["proxy"] is None, "detail 必须以 null 表达「跟随全局」，不得塌缩成空串"

        # 2) 显式空串 = 强制直连（前端把改动后清空的框回传为 ""）
        ws.send_json({"id": "t3", "method": "config.save_provider", "params": {
            "name": "deepseek", "proxy": ""}})
        r3 = recv_until(ws, "t3")
        assert r3["ok"], r3.get("error")
        assert raw_proxy() == ""

        # 3) 非空 = 该服务专属代理，原样落盘
        ws.send_json({"id": "t4", "method": "config.save_provider", "params": {
            "name": "deepseek", "proxy": "http://127.0.0.1:7890"}})
        r4 = recv_until(ws, "t4")
        assert r4["ok"], r4.get("error")
        assert raw_proxy() == "http://127.0.0.1:7890"

        ws.send_json({"id": "t5", "method": "config.providers", "params": {}})
        d5 = recv_until(ws, "t5")["result"]["providers"]["deepseek"]
        assert d5["proxy"] == "http://127.0.0.1:7890"

        # 4) 不带键再保存任意字段：已有设置不被改写
        ws.send_json({"id": "t6", "method": "config.save_provider", "params": {
            "name": "deepseek", "model": "deepseek-chat"}})
        r6 = recv_until(ws, "t6")
        assert r6["ok"], r6.get("error")
        assert raw_proxy() == "http://127.0.0.1:7890", "缺键保存不得动已有 proxy"
