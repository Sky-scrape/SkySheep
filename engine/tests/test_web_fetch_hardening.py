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
import html as html_mod
import re
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
    _safe_charset,
    html_to_text,
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


# ---- 低危项：charset 与 URL 凭据 ----


def test_safe_charset_falls_back_on_bogus_name():
    """对端声明的 charset 认不出来时回 utf-8，不让 LookupError 穿透成 500。"""
    assert _safe_charset("gbk") == "gbk"
    assert _safe_charset("UTF-8") == "UTF-8"
    assert _safe_charset(None) == "utf-8"
    assert _safe_charset("") == "utf-8"
    assert _safe_charset("x-nonexistent-charset") == "utf-8"
    assert _safe_charset("utf-8" + chr(0) + "evil") == "utf-8"


class _EchoHeaderHandler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
        })
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=x-bogus")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_url_userinfo_is_stripped_and_bogus_charset_tolerated(tmp_path):
    """URL 里的 userinfo 不发给对端；伪造 charset 不炸。"""
    _EchoHeaderHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHeaderHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tool = WebFetchTool(allow_private_hosts=True)
        ctx = ToolContext(working_dir=tmp_path)
        out = asyncio.run(tool.run(
            WebFetchArgs(url=f"http://user:secret@127.0.0.1:{port}/page"), ctx,
        ))
        assert "ok" in out  # 伪造 charset 不影响取回
        assert _EchoHeaderHandler.seen, "桩服务器应收到请求"
        got = _EchoHeaderHandler.seen[0]
        assert got["auth"] is None, "URL 里的凭据不得发给对端"
        assert "secret" not in got["path"]
    finally:
        srv.shutdown()


# ---- 审查 C（2026-09-25）：翻译段地址与重定向 userinfo ----


def test_resolve_rejects_nat64_translation_address():
    """NAT64/DNS64 合成地址（64:ff9b::/96）is_global=True 但语义是访问内嵌
    的内网 IPv4——公网校验必须显式拒绝。"""

    def fake(host, port, *a, **k):
        if host == "rebind.test":
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 ("64:ff9b::7f00:1", 0, 0, 0))  # 127.0.0.1 的 NAT64 合成形式
            ]
        return REAL_GETADDRINFO(host, port, *a, **k)

    with patch.object(socket, "getaddrinfo", side_effect=fake):
        with pytest.raises(ToolError):
            _resolve_public_ips("rebind.test")


def test_resolve_rejects_6to4_address():
    """6to4（2002::/16）内嵌 IPv4，同样不得过公网校验。"""

    def fake(host, port, *a, **k):
        if host == "tunnel.test":
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 ("2002:7f00:1::", 0, 0, 0))  # 内嵌 127.0.0.1
            ]
        return REAL_GETADDRINFO(host, port, *a, **k)

    with patch.object(socket, "getaddrinfo", side_effect=fake):
        with pytest.raises(ToolError):
            _resolve_public_ips("tunnel.test")


class _RedirectorHandler(BaseHTTPRequestHandler):
    target = ""
    log_message = _EchoHeaderHandler.log_message

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.end_headers()


def test_redirect_userinfo_is_stripped_per_hop(tmp_path):
    """重定向 Location 携带的 userinfo 在下一跳被剥掉，不转成 Basic Auth 发出。

    首跳剥离只保护初始 URL；302 的 Location 里带 user:pass 时，httpx 会把它
    当 Basic Auth 发给对端——每一跳都要过同一把剪刀。
    """
    _EchoHeaderHandler.seen = []
    catcher = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHeaderHandler)
    redir = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectorHandler)
    _RedirectorHandler.target = (
        f"http://alice:secret@127.0.0.1:{catcher.server_address[1]}/cred"
    )
    for s in (catcher, redir):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        tool = WebFetchTool(allow_private_hosts=True)  # 测试后门：本机桩
        ctx = ToolContext(working_dir=tmp_path)
        out = asyncio.run(tool.run(
            WebFetchArgs(url=f"http://127.0.0.1:{redir.server_address[1]}/start"), ctx,
        ))
        assert "ok" in out
        assert _EchoHeaderHandler.seen, "第二跳桩应收到请求"
        got = _EchoHeaderHandler.seen[0]
        assert got["auth"] is None, "重定向带进来的凭据不得转成 Basic Auth"
        assert "secret" not in got["path"]
        assert "secret" not in out, "凭据也不该留在返回文本里"
    finally:
        redir.shutdown()
        catcher.shutdown()


# ---- html_to_text 线性解析（审查 P-6：正则回溯 → HTMLParser） ----


def test_html_to_text_drops_script_and_decodes_entities():
    """与旧正则实现对齐的行为：丢弃名单内标签的内容、块级标签换行、实体解码。"""
    from skysheep.tools.web import html_to_text

    page = (
        "<html><head><title>Secret Title</title></head><body>"
        "<h1>Heading</h1><p>Text &amp; more</p>"
        "<script>var evil = 1;</script><style>.x { color: red }</style>"
        "<noscript>no js</noscript><svg><circle/></svg>"
        "line1<br>line2<br/>line3"
        "</body></html>"
    )
    text = html_to_text(page)
    assert "Heading" in text and "Text & more" in text
    assert "line1\nline2\nline3" in text
    assert "evil" not in text and ".x" not in text and "no js" not in text
    assert "Secret Title" not in text
    assert "<" not in text


def test_html_to_text_unclosed_script_does_not_leak_body():
    """未闭合的 <script>：其后内容按脚本文本处理整段丢弃（浏览器语义），
    而不是像旧正则那样失效后把脚本源码当正文吐给模型。"""
    from skysheep.tools.web import html_to_text

    text = html_to_text("<p>keep</p><script>alert(document.cookie)<p>hidden</p>")
    assert "keep" in text
    assert "alert" not in text and "hidden" not in text


def test_html_to_text_unclosed_head_recovers_at_body():
    """容错：head 未闭合的页面从 <body> 起恢复取文（浏览器对 head 隐式收口）。"""
    from skysheep.tools.web import html_to_text

    text = html_to_text("<html><head><meta charset=utf-8><title>T</title>"
                        "<body><p>content</p></body></html>")
    assert "content" in text


def test_html_to_text_linear_time_on_unclosed_open_tags():
    """性能回归（审查 P-6）：旧正则实现对大量未闭合 <script> 呈平方级回溯，
    256KB 实测 40 秒以上（2MB 按曲线外推约 45 分钟），web_fetch 是 READONLY
    自动放行工具，同步执行发生在事件循环上——一次抓取即卡死整个引擎。
    HTMLParser 实现是线性状态机：同样输入必须在远低于此的时限内完成。"""
    import time

    payload = "ok" + "<script>" * 32_768  # 256KB，旧实现 40s+ 的形态
    start = time.perf_counter()
    text = html_to_text(payload)
    elapsed = time.perf_counter() - start
    assert text == "ok"
    assert elapsed < 5.0, f"html_to_text 对 256KB 未闭合标签耗时 {elapsed:.2f}s，疑似平方级回溯回归"


# ---- 发现 8：与旧正则实现（git HEAD 逐字提取）的对照 ----


def _old_regex_html_to_text(html: str) -> str:
    """旧实现逐字照抄自 ``git show HEAD:engine/src/skysheep/tools/web.py``
    的 html_to_text（185-193 行，含四条 re.sub 与折叠逻辑）。

    只在对照测试里存在：正式实现绝不允许退回这个平方级回溯的正则形态
    （性能护栏见 test_html_to_text_linear_time_on_unclosed_open_tags）。
    """
    html = re.sub(r"(?is)<(script|style|noscript|svg|head|iframe)[^>]*>.*?</\1\s*>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(
        r"(?i)</(p|div|li|tr|h[1-6]|section|article|blockquote|pre|table)>", "\n", html
    )
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_mod.unescape(html)
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


@pytest.mark.parametrize(
    "page",
    [
        # 表格单元格（验证员对照里黏连最重的形态：旧病是输出 "AB"）
        "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>",
        # 行内标签：相邻 span、强调混排、文本后接块级标签
        "<span>Total</span><span>$100</span>",
        "<b>加粗</b>正文<i>斜体</i>",
        "Hello<p>World</p>",
        # 导航链接
        "<nav><a href='/'>首页</a><a href='/about'>关于</a></nav>",
        # 注释节点夹在文本中间
        "价格<!-- TODO: 补充 -->合计",
        # 块级形态：块间换行、列表
        "<p>Hello</p><p>World</p>",
        "<ul><li>One</li><li>Two</li></ul>",
        # 完整文档（含闭合 head）与空白折叠
        "<html><head><title>T</title></head><body><h1>A</h1><p>B&amp;C</p></body></html>",
        "<div>\n  spaced   text\n</div>",
        # 丢弃块夹在文本之间：整块恰好替换为一个空格
        "A<script>var x=1</script>B",
        # DOCTYPE（旧正则按 <[^>]+> 整体换一个空格）
        "<!DOCTYPE html><p>X</p>",
        # <br> 的三种写法
        "line1<br>line2<br/>line3<br >line4",
    ],
)
def test_html_matches_old_regex_implementation(page):
    """行内/表格/导航/注释/块级各形态：新实现与旧正则逐字一致（发现 8：
    非丢弃标签必须各补一个空格分隔，行内不黏连）。"""
    assert html_to_text(page) == _old_regex_html_to_text(page)


@pytest.mark.parametrize(
    ("page", "leak", "keep"),
    [
        # 未闭合 head：旧正则匹配不到 </head>，title 元信息当正文泄漏
        ("<html><head><title>Secret</title><meta charset=utf-8><body>正文",
         "Secret", "正文"),
        # 属性值含 >：旧正则把标签撕成两半，属性残片 b"> 当正文泄漏
        ('<a title="a>b">text</a>', 'b">', "text"),
        # 未闭合 script：旧正则失效后把脚本源码当正文吐给模型
        ("<p>keep</p><script>alert(document.cookie)</p>", "alert(", "keep"),
    ],
)
def test_html_is_explicitly_better_than_old_regex(page, leak, keep):
    """三处刻意优于旧正则的行为（发现 8 指明不得退回）：新实现不泄漏，
    且对照确认旧实现在同一形态确实泄漏。"""
    new_text = html_to_text(page)
    old_text = _old_regex_html_to_text(page)
    assert leak in old_text, "对照预期失效：旧正则本应在此形态泄漏（选例有误）"
    assert leak not in new_text, "新实现不得把元信息/属性残片/脚本文本当正文泄漏"
    assert keep in new_text


# ---- 发现 10：非文本编解码器（base64 等）不得穿透到 decode ----


@pytest.mark.parametrize(
    "name",
    ["base64", "hex", "zlib_codec", "bz2_codec", "uu_codec", "quopri_codec", "rot_13"],
)
def test_safe_charset_rejects_non_text_codecs(name):
    """codecs.lookup 查得到的字节变换编解码器并不是文本编码：bytes.decode
    照样抛 LookupError（"'base64' is not a text encoding"），整次抓取报工具
    错误（发现 10，验证员实测全链路）。_is_text_encoding=False 一律回 utf-8，
    且回退值必须真能被 bytes.decode 接受。"""
    assert _safe_charset(name) == "utf-8"
    "内容".encode().decode(_safe_charset(name), errors="replace")


@pytest.mark.parametrize("name", ["gbk", "latin-1", "shift_jis", "big5", "utf-16", "UTF-8"])
def test_safe_charset_passes_real_text_encodings_through(name):
    """真文本编码不受影响，原样返回（大小写照旧）。"""
    assert _safe_charset(name) == name


class _CharsetEchoHandler(BaseHTTPRequestHandler):
    """按类属性回 Content-Type；默认 charset=base64（发现 10 的全链路形态）。"""

    content_type = "text/html; charset=base64"
    body = b"<html><body><p>hello</p></body></html>"
    log_message = _EchoHeaderHandler.log_message

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)


def test_fetch_with_non_text_codec_charset_still_returns_text(tmp_path):
    """全链路：对端返回 charset=base64 这类字节变换编解码器时回 utf-8 兜底
    取回文本，而不是 LookupError 穿透成整体工具错误。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CharsetEchoHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tool = WebFetchTool(allow_private_hosts=True)
        ctx = ToolContext(working_dir=tmp_path)
        out = asyncio.run(tool.run(WebFetchArgs(url=f"http://127.0.0.1:{port}/b64"), ctx))
        assert "hello" in out
    finally:
        srv.shutdown()


# ---- 钉连安装的 fail-closed 哨兵（审查 P-17） ----


def test_install_pinned_backend_installs_and_fails_closed():
    """`_pool` / `_network_backend` 都是 httpx/httpcore 私有字段：
    `_pool` 没了旧代码会响亮 AttributeError；仅 `_network_backend` 改名时
    普通赋值会静默变成死属性、钉连退化成单次 getaddrinfo 校验——必须显式
    getattr 检查，装不上就拒绝发请求。"""
    import httpx

    from skysheep.tools.web import _install_pinned_backend

    backend = _PinnedBackend({})
    # 当前版本结构（httpx 0.28.x / httpcore 1.0.x）：正常安装
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    _install_pinned_backend(transport, backend, "web_fetch")
    assert transport._pool._network_backend is backend

    # 模拟上游把 `_network_backend` 改名：池还在、字段没了 → 必须响亮失败
    transport2 = httpx.AsyncHTTPTransport(trust_env=False)

    class _RenamedPool:  # 模拟 httpcore 1.x+ 改名后的连接池
        pass

    transport2._pool = _RenamedPool()
    with pytest.raises(RuntimeError, match="钉连"):
        _install_pinned_backend(transport2, backend, "web_fetch")

    # 模拟 httpx 把 `_pool` 整个移走 → 同样 fail closed
    class _NoPoolTransport:
        _pool = None

    with pytest.raises(RuntimeError, match="钉连"):
        _install_pinned_backend(_NoPoolTransport(), backend, "web_fetch")


def test_fetch_once_installs_pinning_end_to_end(tmp_path):
    """工具路径哨兵：_fetch_once 对主机名 URL 的请求必须经钉连后端直连
    已校验 IP、不再二次解析 DNS——httpx/httpcore 私有结构变化（或钉连被
    静默绕过）时本用例先红。"""
    from urllib.parse import urlparse

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"pinned-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tool = WebFetchTool(allow_private_hosts=True)
        url = f"http://alias.test:{port}/x"

        def explode(*a, **k):
            raise AssertionError("钉连生效时不应再解析 DNS")

        with patch.object(socket, "getaddrinfo", side_effect=explode):
            status, _headers, body = asyncio.run(
                tool._fetch_once(url, urlparse(url), ["127.0.0.1"])
            )
        assert status == 200
        assert body == b"pinned-ok"
    finally:
        srv.shutdown()
