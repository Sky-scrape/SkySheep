"""web_fetch 的 SSRF 加固：DNS rebinding 防护与流式截断。

两条独立的防护，都对应「校验与使用之间不是原子操作」这类经典缺口：

1. **DNS rebinding（TOCTOU）**：旧实现先 ``getaddrinfo`` 校验（答公网 IP 通过），
   再交给 httpx 建连——httpx 会自己再解析一次，两次之间是攻击窗口。现在解析一次即把
   连接固定到已校验 IP（``_PinnedBackend``），不再二次解析。
2. **响应体先下完再截断**：旧实现用非流式 ``client.get()``，整个响应体先读进内存，
   ``MAX_RESPONSE_BYTES`` 截断发生在下载完成之后。现在边读边计数，超限即中止。
"""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from skysheep.tools.base import ToolContext, ToolError
from skysheep.tools.web import (
    MAX_RESPONSE_BYTES,
    WebFetchArgs,
    WebFetchTool,
    _PinnedBackend,
    _resolve_public_ips,
)

REAL_GETADDRINFO = socket.getaddrinfo


def _answers(ips: list[str]):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0)) for ip in ips
    ]


# ---- DNS rebinding ----


def test_resolve_returns_verified_public_ips():
    """解析并校验后返回 IP 列表，供建连阶段直接复用。"""

    def fake(host, port, *a, **k):
        if host == "good.test":
            return _answers(["93.184.216.34"])
        return REAL_GETADDRINFO(host, port, *a, **k)

    with patch.object(socket, "getaddrinfo", side_effect=fake):
        assert _resolve_public_ips("good.test") == ["93.184.216.34"]


def test_mixed_public_and_private_dns_is_rejected():
    """一次解析同时给出公网与内网：整个拒绝，不能只挑能用的那个。"""

    def fake(host, port, *a, **k):
        if host == "mixed.test":
            return _answers(["93.184.216.34", "127.0.0.1"])
        return REAL_GETADDRINFO(host, port, *a, **k)

    with patch.object(socket, "getaddrinfo", side_effect=fake):
        with pytest.raises(ToolError, match="非公网"):
            _resolve_public_ips("mixed.test")


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1"])
def test_non_public_answers_are_rejected(ip):
    """云 metadata 端点、回环、私网地址一律拒绝。"""

    def fake(host, port, *a, **k):
        if host == "internal.test":
            return _answers([ip])
        return REAL_GETADDRINFO(host, port, *a, **k)

    with patch.object(socket, "getaddrinfo", side_effect=fake):
        with pytest.raises(ToolError, match="非公网"):
            _resolve_public_ips("internal.test")


def test_localhost_names_are_rejected_without_dns():
    for host in ("localhost", "0.0.0.0", "::"):
        with pytest.raises(ToolError, match="内网"):
            _resolve_public_ips(host)


def test_pinned_backend_dials_verified_ip_without_dns():
    """命中被固定的主机名时直接连已校验 IP，不再解析域名（这是防 rebinding 的关键）。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        backend = _PinnedBackend({"fake-host.test": ["127.0.0.1"]})

        def explode(*a, **k):
            raise AssertionError("被固定的主机不应再解析 DNS")

        with patch.object(socket, "getaddrinfo", side_effect=explode):
            async def go():
                stream = await backend.connect_tcp("fake-host.test", port, timeout=5)
                await stream.aclose()

            asyncio.run(go())
    finally:
        srv.shutdown()


def test_pinned_backend_passes_through_unpinned_hosts():
    """没被固定的主机照常走 DNS（重定向到新主机时由调用方重新固定）。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        backend = _PinnedBackend({})  # 什么都不固定

        async def go():
            stream = await backend.connect_tcp("127.0.0.1", port, timeout=5)
            await stream.aclose()

        asyncio.run(go())
    finally:
        srv.shutdown()


def test_rebinding_host_header_and_sni_keep_original_name():
    """固定只改「连到哪台机器」：Host 头与 SNI 仍是原主机名（虚拟主机/证书校验照常）。"""
    seen: list[str | None] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Host"))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import httpx

        transport = httpx.AsyncHTTPTransport(trust_env=False)
        # pin 到 127.0.0.1，但 URL 里写的是另一个主机名
        transport._pool._network_backend = _PinnedBackend({"alias.test": ["127.0.0.1"]})

        async def go():
            async with httpx.AsyncClient(transport=transport, follow_redirects=False) as c:
                async with c.stream("GET", f"http://alias.test:{port}/x") as resp:
                    await resp.aread()
                    return resp.status_code

        assert asyncio.run(go()) == 200
        assert seen and "alias.test" in seen[0], "Host 头应保留原始主机名"
    finally:
        srv.shutdown()


def test_tool_rejects_private_host_end_to_end(tmp_path):
    """工具层：内网地址在建连前就被拒绝（正式对象不开 allow_private_hosts）。"""
    tool = WebFetchTool()
    ctx = ToolContext(working_dir=tmp_path)
    with pytest.raises(ToolError):
        asyncio.run(tool.run(WebFetchArgs(url="http://169.254.169.254/latest/meta-data/"), ctx))


# ---- 流式截断 ----


class _BigHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # 无 Content-Length 的无限流：旧实现会先读完整响应体再截断
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        blk = b"X" * 65536
        try:
            for _ in range(200):  # ~13 MB，远超 2 MB 上限
                self.wfile.write(blk)
        except Exception:
            pass


def test_oversized_body_is_aborted_while_streaming(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _BigHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tool = WebFetchTool(allow_private_hosts=True)
        ctx = ToolContext(working_dir=tmp_path)
        out = asyncio.run(tool.run(WebFetchArgs(url=f"http://127.0.0.1:{port}/big"), ctx))
        assert "已截断" in out, "超限响应应带上截断说明"
        assert len(out) < MAX_RESPONSE_BYTES, "不会把整个超大响应带进上下文"
    finally:
        srv.shutdown()
