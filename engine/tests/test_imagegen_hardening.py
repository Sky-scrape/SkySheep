"""generate_image 的 SSRF 加固（安全审查 M9）。

web_fetch 已修的两项——DNS rebinding TOCTOU（连接固定到已校验 IP）与响应体
流式限长——此前没有同步到 imagegen：

1. **DNS rebinding**：旧实现每跳只做 ``_assert_public_host``（校验时答公网
   IP），随后交给 httpx 建连时又解析一次 DNS——两次解析之间是攻击窗口。
   现在每跳用 ``_pinned_transport`` 固定到已校验 IP（``_SyncPinnedBackend``）。
2. **流式限长**：旧实现用非流式 ``client.get()``，图片与生成接口的 JSON 响应
   都是先全量进内存再检查上限——恶意/被劫持端点可用无限流吃干内存。现在边读
   边计数，超限立即中止。

内网地址（生成接口 base_url 与图片下载 URL）在建连前直接拒绝。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
import pytest
from test_new_features import _png_bytes

from skysheep.tools.base import ToolContext, ToolError
from skysheep.tools.imagegen import GenerateImageTool

REAL_GETADDRINFO = socket.getaddrinfo


def _answers(ips: list[str]):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0)) for ip in ips
    ]


def _ctx(tmp_path):
    return ToolContext(working_dir=tmp_path)


# ---- 内网地址：建连前拒绝 ----


def test_generation_endpoint_private_base_url_rejected(tmp_path):
    """生成接口指向内网时直接拒绝（base_url 可手填，不能默认可信）。"""
    tool = GenerateImageTool(provider="custom", api_key="k", base_url="http://192.168.1.9/v1")
    with pytest.raises(ToolError):
        tool._generate_sync("x")


def test_download_url_private_host_rejected(tmp_path):
    """服务商返回的图片 URL 指向内网：拒绝下载。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/images/generations" in str(request.url):
            return httpx.Response(200, json={"data": [{"url": "http://10.0.0.5/img.png"}]})
        raise AssertionError("内网地址不应进入下载阶段")

    tool = GenerateImageTool(provider="zhipu", api_key="k",
                             transport=httpx.MockTransport(handler))
    with pytest.raises(ToolError):
        tool._generate_sync("x")


# ---- DNS rebinding：建连必须用已校验 IP，不得二次解析 ----


def test_imagegen_dials_verified_ip_without_second_dns(tmp_path):
    """下载跳固定到已校验 IP 后，建连阶段不得再对被固定主机做 DNS 解析。"""
    png = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/images/generations" in str(request.url):
            return httpx.Response(200, json={"data": [{"url": "http://rebind.test/img.png"}]})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    tool = GenerateImageTool(provider="zhipu", api_key="k",
                             transport=httpx.MockTransport(handler))
    # 解析阶段答公网 IP（通过校验）；如果建连时再解析一次，explode 会炸
    with patch.object(socket, "getaddrinfo", side_effect=lambda h, *a, **k: (
        _answers(["93.184.216.34"]) if h == "rebind.test" else REAL_GETADDRINFO(h, *a, **k)
    )):
        _url, body, _mime = tool._generate_sync("x")
    assert body == png


def test_rebinding_second_lookup_answers_private_ip(tmp_path):
    """校验后改答内网 IP 的 rebinding：固定生效，建连用的仍是公网 IP。"""
    png = _png_bytes()
    lookups: list[str] = []

    def fake_getaddrinfo(host, *a, **k):
        lookups.append(host)
        if host == "rebind.test":
            # 只有第一次（校验）答公网；若实现错误地二次解析，这里答内网 127.0.0.1
            return _answers(["93.184.216.34"] if lookups.count("rebind.test") == 1
                            else ["127.0.0.1"])
        return REAL_GETADDRINFO(host, *a, **k)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/images/generations" in str(request.url):
            return httpx.Response(200, json={"data": [{"url": "http://rebind.test/img.png"}]})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    tool = GenerateImageTool(provider="zhipu", api_key="k",
                             transport=httpx.MockTransport(handler))
    with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo):
        _url, body, _mime = tool._generate_sync("x")
    assert body == png


# ---- 流式限长：超上限立即中止，不再全量进内存 ----


class _InfiniteImageHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # 无 Content-Length 的无限流：旧实现先读完整响应体再查 MAX_IMAGE_BYTES
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        blk = b"X" * 65536
        try:
            for _ in range(2000):  # ~130 MB，远超 8MB 上限
                self.wfile.write(blk)
        except Exception:
            pass

    def do_POST(self):
        import json as _json
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        # 生成接口返回一个指向本服务的无限流图片地址（127.0.0.1 仅测试桩内可达）
        payload = _json.dumps({"data": [{"url": f"http://imgstub.test:{self.server.server_address[1]}/img"}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload.encode())


def test_oversized_image_download_aborted_while_streaming(tmp_path):
    """下载无限流图片：边读边计数，超 MAX_IMAGE_BYTES 立即中止（旧实现先全量进内存）。

    测试桩只在内网可达：让解析层把桩主机名答成 127.0.0.1，真实的 pinned
    transport 会把连接固定过去（顺带覆盖同步 pinning 的建连路径）。
    """
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _InfiniteImageHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tool = GenerateImageTool(provider="custom", api_key="k",
                                 base_url=f"http://imgstub.test:{port}/v1")
        with patch("skysheep.tools.imagegen._resolve_public_ips",
                   side_effect=lambda host: ["127.0.0.1"]):
            with pytest.raises(ToolError) as e:
                tool._generate_sync("x")
        assert "上限" in str(e.value)
    finally:
        srv.shutdown()


def test_oversized_api_json_aborted_while_streaming(tmp_path):
    """生成接口返回超限 JSON（如无限流）：边读边计数，超限立即中止。"""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            blk = b"{" + b'"x":"' + b"A" * 65536 + b'"}'
            try:
                for _ in range(2000):  # ~130 MB
                    self.wfile.write(blk)
            except Exception:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tool = GenerateImageTool(provider="custom", api_key="k",
                                 base_url=f"http://jsonstub.test:{port}/v1")
        with patch("skysheep.tools.imagegen._resolve_public_ips",
                   side_effect=lambda host: ["127.0.0.1"]):
            with pytest.raises(ToolError) as e:
                tool._generate_sync("x")
        assert "上限" in str(e.value)
    finally:
        srv.shutdown()


# ---- 相对重定向（urljoin）：合法图片 CDN 常见形态 ----


def test_relative_redirect_location_is_joined(tmp_path):
    """重定向 Location 允许是相对路径：按当前 URL 补全后再校验。"""
    png = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "/images/generations" in u:
            return httpx.Response(200, json={"data": [{"url": "http://93.184.216.34/a/img.png"}]})
        if u.endswith("/a/img.png"):
            return httpx.Response(302, headers={"Location": "/b/real.png"})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    tool = GenerateImageTool(provider="zhipu", api_key="k",
                             transport=httpx.MockTransport(handler))
    _url, body, _mime = tool._generate_sync("x")
    assert body == png
