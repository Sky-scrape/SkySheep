"""MCP 远程服务器的自定义请求头（带鉴权的托管 MCP）。

此前 MCPServerConfig 只有 command/args/env/url/readonly，且建连时调
streamable_http_client(url) 不传任何 header —— Notion / Linear / GitHub
官方 Remote MCP 这类需要 Authorization 的服务根本连不上。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from skysheep.mcp.client import MCPManager, MCPServerConfig, _headers_only
from skysheep.mcp.installer import MCPInstallError, normalize_server


def _write_mcp_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------- 配置层


def test_config_headers_parsed(tmp_path):
    p = tmp_path / "mcp.json"
    _write_mcp_json(p, {
        "mcpServers": {
            "remote": {
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer tok123"},
            }
        }
    })
    from skysheep.mcp.client import load_mcp_configs

    cfg = load_mcp_configs(p, None)
    assert cfg["remote"].headers == {"Authorization": "Bearer tok123"}


def test_normalize_server_accepts_headers():
    cfg = normalize_server({
        "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer a", "X-Api-Version": "2"},
    })
    assert cfg.headers["X-Api-Version"] == "2"


def test_normalize_server_rejects_bad_headers():
    # 非字符串值由 pydantic 先拦（与 env 同一行为）
    with pytest.raises(MCPInstallError):
        normalize_server({"url": "https://x/mcp", "headers": {"Auth": 123}})
    # 全是字符串但 header 名为空：pydantic 放行，靠 installer 的显式校验拦下
    with pytest.raises(MCPInstallError) as ei:
        normalize_server({"url": "https://x/mcp", "headers": {"   ": "v"}})
    assert "headers" in str(ei.value)


def test_normalize_server_headers_still_needs_transport():
    with pytest.raises(MCPInstallError):
        normalize_server({"headers": {"Authorization": "Bearer a"}})


def test_headers_only_filters_blank_names():
    cfg = MCPServerConfig(url="https://x/mcp", headers={"  ": "v", "Good": "v2", "K": ""})
    # 空名过滤；值为空串是合法 header 值（有些服务要求空值），一并保留
    assert _headers_only(cfg) == {"Good": "v2", "K": ""}


def test_stdio_ignores_headers():
    """stdio 服务不带 headers 语义；配置里留着也不该让它变成 http 传输。"""
    cfg = MCPServerConfig(command="uvx", args=["x"], headers={"Authorization": "Bearer a"})
    assert cfg.transport == "stdio"


def test_headers_default_empty():
    cfg = MCPServerConfig(url="https://x/mcp")
    assert cfg.headers == {}
    assert _headers_only(cfg) == {}


# ---------------------------------------------------------------- 端到端：头真的发出去了


class _HeaderRecorder(BaseHTTPRequestHandler):
    """记录收到的请求头；对 MCP 握手一律返回 401（我们只关心头有没有到）。"""

    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        type(self).seen.append(dict(self.headers))
        body = b'{"jsonrpc":"2.0","error":{"code":401,"message":"unauthorized"},"id":1}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture
def header_server():
    _HeaderRecorder.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HeaderRecorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()
    srv.server_close()


async def test_headers_are_sent_to_remote_server(header_server):
    cfg = MCPServerConfig(
        url=header_server,
        headers={"Authorization": "Bearer secret-token", "X-Custom": "hello"},
    )
    manager = MCPManager({"auth": cfg})
    try:
        await manager.connect_all()
    finally:
        await manager.shutdown()
    assert _HeaderRecorder.seen, "服务器应当收到至少一个请求"
    got = _HeaderRecorder.seen[0]
    # httpx 的 header 名大小写不敏感，键的规范形是 Title-Case
    assert got.get("Authorization") == "Bearer secret-token"
    assert got.get("X-Custom") == "hello"


async def test_no_headers_sent_when_not_configured(header_server):
    manager = MCPManager({"anon": MCPServerConfig(url=header_server)})
    try:
        await manager.connect_all()
    finally:
        await manager.shutdown()
    assert _HeaderRecorder.seen
    assert "Authorization" not in _HeaderRecorder.seen[0]
