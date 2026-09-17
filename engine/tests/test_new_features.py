"""v0.7.0 新功能测试：

- read_document 文档阅读（PDF/DOCX/XLSX 提取 + 不支持类型报错）
- web_search 联网搜索（三家服务商响应解析 + 未配置指引 + 输出格式）
- memory_write 全局记忆（append/list/delete + 去重 + 系统提示词注入）
- generate_image 画图（b64 / URL 两条路径 + 落盘 + 检查点记录）
- 检查点落盘持久化（跨实例恢复 + 新建文件回滚删除 + FIFO 淘汰）
- hooks（pre 阻断 / JSON block / post 通知 + 配置解析）
- 版本比较 + 更新检查数据结构
- 新 WS 协议：memory/websearch/imagegen/lan/market + /preview 路由与穿越防护
- config 解析：resolve_websearch / resolve_imagegen / update_config_section
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from test_server import make_client, recv_until  # noqa: F401

from skysheep.config import (
    load_config,
    resolve_imagegen,
    resolve_websearch,
    update_config_section,
)
from skysheep.core.checkpoints import MAX_CHECKPOINTS, CheckpointStore
from skysheep.core.hooks import HookRule, HookRunner, hooks_from_config
from skysheep.core.uptodate import is_newer_version
from skysheep.messages import TextBlock
from skysheep.tools import ChangeRecorder, ToolContext, ToolRegistry, default_tools
from skysheep.tools.docs import ReadDocumentArgs, ReadDocumentTool
from skysheep.tools.imagegen import GenerateImageArgs, GenerateImageTool
from skysheep.tools.memory import MemoryWriteArgs, MemoryWriteTool, render_memory_section
from skysheep.tools.web import WebSearchArgs, WebSearchTool


def make_ctx(tmp_path):
    return ToolContext(working_dir=tmp_path)


def registry_with(recorder=None):
    return ToolRegistry(default_tools(recorder=recorder))


# ---- read_document ----


def _mini_pdf(text: str) -> bytes:
    """构造一个只含一行文字的最小合法 PDF（pypdf 可提取）。"""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return out


async def test_read_document_pdf(tmp_path):
    p = tmp_path / "hello.pdf"
    p.write_bytes(_mini_pdf("Hello SkySheep"))
    result = await ReadDocumentTool().run(ReadDocumentArgs(path=str(p)), make_ctx(tmp_path))
    assert "Hello SkySheep" in result
    assert "第 1 页" in result


async def test_read_document_docx_and_xlsx(tmp_path):
    docx = __import__("docx")
    d = docx.Document()
    d.add_paragraph("第一段正文")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "甲"
    table.rows[0].cells[1].text = "乙"
    p = tmp_path / "t.docx"
    d.save(str(p))
    result = await ReadDocumentTool().run(ReadDocumentArgs(path=str(p)), make_ctx(tmp_path))
    assert "第一段正文" in result and "甲" in result and "乙" in result

    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append(["名称", "数量"])
    ws.append(["苹果", 3])
    p2 = tmp_path / "t.xlsx"
    wb.save(str(p2))
    result2 = await ReadDocumentTool().run(ReadDocumentArgs(path=str(p2)), make_ctx(tmp_path))
    assert "工作表: 数据" in result2 and "苹果 | 3" in result2


async def test_read_document_rejects_unsupported(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("plain")
    from skysheep.tools.base import ToolError

    with pytest.raises(ToolError, match="read_file"):
        await ReadDocumentTool().run(ReadDocumentArgs(path=str(p)), make_ctx(tmp_path))


# ---- web_search ----


def _tavily_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer key-tv"
    return httpx.Response(200, json={
        "results": [
            {"title": "结果一", "url": "https://a.example.com/x", "content": "内容摘要" * 5},
            {"title": "无链接的应被丢弃", "url": "", "content": "..."},
        ]
    })


async def test_web_search_tavily_parse():
    tool = WebSearchTool(provider="tavily", api_key="key-tv",
                         transport=httpx.MockTransport(_tavily_handler))
    out = await tool.run(WebSearchArgs(query="skysheep 用法"), make_ctx(Path(".")))
    assert "web_search" in out and "结果一" in out and "https://a.example.com/x" in out
    assert "无链接" not in out  # 没有 URL 的结果被过滤


async def test_web_search_unconfigured_gives_hint():
    from skysheep.tools.base import ToolError

    tool = WebSearchTool()
    with pytest.raises(ToolError, match="联网搜索未配置"):
        await tool.run(WebSearchArgs(query="x"), make_ctx(Path(".")))


async def test_web_search_bocha_and_zhipu_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        if "bochaai" in str(request.url):
            return httpx.Response(200, json={
                "code": 200,
                "data": {"webPages": {"value": [
                    {"name": "博查结果", "url": "https://b.example.com", "snippet": "摘要"},
                ]}},
            })
        assert "bigmodel" in str(request.url)
        return httpx.Response(200, json={"search_result": [
            {"title": "智谱结果", "link": "https://z.example.com", "content": "正文"},
        ]})

    bocha = WebSearchTool(provider="bocha", api_key="k", transport=httpx.MockTransport(handler))
    out1 = await bocha.run(WebSearchArgs(query="q"), make_ctx(Path(".")))
    assert "博查结果" in out1
    zhipu = WebSearchTool(provider="zhipu", api_key="k", transport=httpx.MockTransport(handler))
    out2 = await zhipu.run(WebSearchArgs(query="q"), make_ctx(Path(".")))
    assert "智谱结果" in out2


# ---- memory ----


async def test_memory_append_list_delete_dedupe(home):
    tool = MemoryWriteTool()
    ctx = make_ctx(home / "proj")
    out1 = await tool.run(MemoryWriteArgs(action="append", content="喜欢简洁回复"), ctx)
    assert "remembered" in out1
    out2 = await tool.run(MemoryWriteArgs(action="append", content="喜欢简洁回复"), ctx)
    assert "already" in out2  # 去重
    listed = await tool.run(MemoryWriteArgs(action="list"), ctx)
    assert "喜欢简洁回复" in listed
    deleted = await tool.run(MemoryWriteArgs(action="delete", match="喜欢简洁"), ctx)
    assert "forgot" in deleted
    assert "(memory is empty)" in await tool.run(MemoryWriteArgs(action="list"), ctx)


def test_memory_prompt_injection(home):
    from skysheep.tools.memory import memory_path

    memory_path().parent.mkdir(parents=True, exist_ok=True)
    memory_path().write_text("- [2026-09-15] 测试记忆\n", encoding="utf-8")
    section = render_memory_section()
    assert "测试记忆" in section and "User memory" in section


# ---- generate_image ----


def _png_bytes() -> bytes:
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (200, 80, 40)).save(buf, format="PNG")
    return buf.getvalue()


async def test_generate_image_b64_path_and_checkpoint(tmp_path):
    png = _png_bytes()
    payload = base64.b64encode(png).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/images/generations" in str(request.url)
        return httpx.Response(200, json={"data": [{"b64_json": payload}]})

    rec = ChangeRecorder()
    tool = GenerateImageTool(provider="siliconflow", api_key="k", recorder=rec,
                             transport=httpx.MockTransport(handler))
    out = await tool.run(GenerateImageArgs(prompt="一只羊", path="img/sheep.png"), make_ctx(tmp_path))
    assert "image saved" in out
    saved = tmp_path / "img" / "sheep.png"
    assert saved.read_bytes() == png
    # 检查点：新建文件改前状态为 None → 回滚时应删除
    assert rec.pre[str(saved)] is None


async def test_generate_image_url_download_path(tmp_path):
    png = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/images/generations" in str(request.url):
            # 公网 IP 字面量（通过 SSRF 主机校验；MockTransport 不真正联网）
            return httpx.Response(200, json={"data": [{"url": "http://93.184.216.34/img.png"}]})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    tool = GenerateImageTool(provider="zhipu", api_key="k",
                             transport=httpx.MockTransport(handler))
    out = await tool.run(GenerateImageArgs(prompt="x"), make_ctx(tmp_path))
    assert "image saved" in out and ".png" in out


# ---- 检查点落盘 ----


def test_checkpoint_store_persists_across_instances(tmp_path):
    root = tmp_path / "cps"
    f = tmp_path / "doc.txt"
    f.write_text("v1")
    store = CheckpointStore(root=root)
    cp = store.save("sess-1", {str(f): b"v1"})
    assert cp and cp["id"]
    # 模拟重启：全新实例从磁盘恢复
    store2 = CheckpointStore(root=root)
    listing = store2.list_for("sess-1")
    assert [c["id"] for c in listing] == [cp["id"]]
    f.write_text("v2-broken")
    restored = store2.restore(cp["id"])
    assert restored == [str(f)]
    assert f.read_text() == "v1"


def test_checkpoint_store_prunes_fifo(tmp_path):
    root = tmp_path / "cps"
    store = CheckpointStore(root=root)
    f = tmp_path / "x.txt"
    f.write_bytes(b"x")
    ids = []
    for i in range(MAX_CHECKPOINTS + 5):
        cp = store.save(f"s{i}", {str(f): None})
        ids.append(cp["id"])
    assert len(store.list_for("s0")) == 0  # 最老的被淘汰
    assert len(list(root.rglob("meta.json"))) == MAX_CHECKPOINTS


# ---- hooks ----


def _py(code: str) -> str:
    import sys

    return f'"{sys.executable}" -c "{code}"'


async def test_hooks_pre_block_and_allow(tmp_path):
    # hook 子进程的 stderr 默认随控制台区域设置（中文 Windows 是 GBK），而 hook 协议约定
    # 输出为 UTF-8——测试脚本显式 reconfigure，避免在 GBK 机器上因编码不一致而假失败
    blocker = HookRule(
        command=_py("import sys;sys.stderr.reconfigure(encoding='utf-8');"
                    "sys.stderr.write('不许删');sys.exit(2)"),
        timeout_s=15,
    )
    hooks = HookRunner(pre_rules=[blocker], working_dir=tmp_path)
    reason = await hooks.run_pre("write_file", {"path": "x"})
    assert "不许删" in reason

    allow = HookRule(command=_py("import sys;sys.exit(0)"), timeout_s=15)
    hooks2 = HookRunner(pre_rules=[allow], working_dir=tmp_path)
    assert await hooks2.run_pre("write_file", {}) == ""


async def test_hooks_pre_json_block_and_post_note(tmp_path):
    json_block = HookRule(
        command=_py("import sys;sys.stdout.reconfigure(encoding='utf-8');"
                    "print('{\\\"decision\\\": \\\"block\\\", \\\"reason\\\": \\\"禁运\\\"}')"),
        timeout_s=15,
    )
    hooks = HookRunner(pre_rules=[json_block], working_dir=tmp_path)
    assert "禁运" in await hooks.run_pre("run_command", {})

    note = HookRule(command=_py("print('done-ok')"), timeout_s=15)
    hooks2 = HookRunner(post_rules=[note], working_dir=tmp_path)
    out = await hooks2.run_post("write_file", {})
    assert "done-ok" in out and "hook" in out


def test_hooks_config_parsing():
    raw = {"hooks": {
        "pre_tool_use": [{"match": "write_file", "command": "echo hi", "timeout_s": 5}],
        "post_tool_use": [{"command": ""}],  # 空 command：跳过
        "bad": [{"match": 1}],
    }}
    pre, post = hooks_from_config(raw)
    assert len(pre) == 1 and pre[0].match == "write_file" and pre[0].timeout_s == 5
    assert post == []
    assert HookRunner([], []).has_pre is False


def test_fs_read_document_preview(home):
    """文件树点 PDF/DOCX/XLSX → 提取文本（doc: true）；真二进制仍拒绝。"""
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["月份", "新增用户"])
    ws.append(["八月", 120])
    (home / "proj" / "数据表.xlsx").parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(home / "proj" / "数据表.xlsx"))
    (home / "proj" / "blob.bin").write_bytes(b"\x00\x01binary")
    script = [[TextBlock(text="x")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "fs.read", "params": {"path": "数据表.xlsx"}})
        r = recv_until(ws, "d1")
        assert r["ok"]
        assert r["result"]["doc"] is True
        assert "八月 | 120" in r["result"]["text"]
        ws.send_json({"id": "d2", "method": "fs.read", "params": {"path": "blob.bin"}})
        r2 = recv_until(ws, "d2")
        assert not r2["ok"] and "二进制" in r2["error"]


# ---- 版本比较 / 更新检查 ----


def test_is_newer_version():
    assert is_newer_version("v0.7.0", "0.6.0")
    assert is_newer_version("0.10.0", "0.9.9")
    assert not is_newer_version("0.6.0", "0.6.0")
    assert not is_newer_version("0.5.9", "0.6.0")
    assert not is_newer_version("", "0.6.0")


# ---- config 解析 ----


def test_update_config_section_roundtrip(home):
    update_config_section("websearch", {"provider": "bocha", "api_key": "k-1"})
    cfg = load_config()
    assert cfg.websearch.provider == "bocha" and cfg.websearch.api_key == "k-1"
    # 空串 = 删除字段
    update_config_section("websearch", {"api_key": ""})
    cfg2 = load_config()
    assert cfg2.websearch.api_key == "" and cfg2.websearch.provider == "bocha"


def test_resolve_websearch(home, monkeypatch):
    cfg = load_config()
    assert resolve_websearch(cfg) is None  # 什么 Key 都没有
    monkeypatch.setenv("ZHIPUAI_API_KEY", "zk")
    got = resolve_websearch(load_config())
    assert got and got["provider"] == "zhipu"
    update_config_section("websearch", {"provider": "tavily", "api_key": "tk"})
    got2 = resolve_websearch(load_config())
    assert got2 and got2["provider"] == "tavily"


def test_resolve_imagegen(home, monkeypatch):
    cfg = load_config()
    assert resolve_imagegen(cfg) is None
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk")
    got = resolve_imagegen(load_config())
    assert got and got["provider"] == "siliconflow" and got["model"] == "Kwai-Kolors/Kolors"


# ---- WS 协议：新方法 + /preview ----


def test_ws_memory_and_toolcfg_and_lan_and_market(home):
    script = [[TextBlock(text="ok")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "memory.save", "params": {"text": "- [x] 记住测试"}})
        assert recv_until(ws, "m1")["ok"]
        ws.send_json({"id": "m2", "method": "memory.get"})
        r = recv_until(ws, "m2")["result"]
        assert "记住测试" in r["text"]

        ws.send_json({"id": "w1", "method": "websearch.get"})
        w = recv_until(ws, "w1")["result"]
        assert w["provider"] == "auto" and "bocha" in w["providers"]
        ws.send_json({"id": "w2", "method": "websearch.save",
                      "params": {"provider": "bocha", "api_key": "k9"}})
        w2 = recv_until(ws, "w2")["result"]
        assert w2["resolved_provider"] == "bocha" and w2["has_key"]

        ws.send_json({"id": "i1", "method": "imagegen.get"})
        i1 = recv_until(ws, "i1")["result"]
        assert i1["provider"] == "auto"

        ws.send_json({"id": "l1", "method": "lan.status"})
        l1 = recv_until(ws, "l1")["result"]
        assert l1["enabled"] is False
        ws.send_json({"id": "l2", "method": "lan.enable", "params": {}})
        l2 = recv_until(ws, "l2")["result"]
        assert l2["token"] and "重启" in l2["note"]

        ws.send_json({"id": "mk1", "method": "skills.market"})
        mk = recv_until(ws, "mk1")["result"]
        assert mk["source"] in ("remote", "builtin") and mk["items"]


def test_market_modal_has_keyword_search(home):
    """技能广场必须带关键词搜索（索引长了以后翻找成本高）。

    锁两件事：搜索框/计数/列表容器在弹窗里，以及过滤与高亮的实现约定——
    多关键词空格分隔且为 AND 语义，高亮必须走转义（索引内容是外部输入）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    body = js[js.index("async function openMarket()"):]
    body = body[:body.index("\ndocument.getElementById(\"btn-market\")")]

    for needle in ('id="market-q"', 'id="market-q-clear"', 'id="market-count"', 'id="market-list"'):
        assert needle in body, f"技能广场缺搜索相关节点：{needle}"
    # 输入即过滤（不需要点按钮）
    assert 'input.addEventListener("input"' in body
    # 多关键词按空格拆、全部命中（AND）
    assert "split(/\\s+/)" in body
    assert "terms.every((t) => hay.includes(t))" in body
    # 命中范围包含名称/描述/作者/地址
    for field in ("it.name", "it.description", "it.author", "it.url"):
        assert field in body, f"搜索字段缺 {field}"
    # 高亮先转义再拼标签，且合并重叠区间（不产生嵌套 mark）
    assert "function markAll" in body or "const markAll" in body
    assert "escapeHtml(text.slice(a, b))" in body
    assert "merged" in body
    # 列表自己滚（条目多了不至于把搜索框顶出视野）
    assert ".market-list {" in css
    market_list_css = css[css.index(".market-list {"):]
    market_list_css = market_list_css[:market_list_css.index("}")]
    assert "max-height" in market_list_css and "overflow-y: auto" in market_list_css


def test_static_assets_send_no_store(home):
    """手写前端资源必须带 Cache-Control: no-store。

    前端零构建、文件名不带指纹：WebView 沿用缓存里的 app.js 会让我改了前端
    却看不到效果，只能靠用户手动强刷。vendor/ 不在此列（mermaid 单文件 3.3MB，
    靠 ETag 协商即可，否则局域网手机访问每次重下）。
    """
    with make_client(home, []) as client:
        for path in ("/", "/static/app.js", "/static/app.css", "/static/index.html"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "no-store" in r.headers.get("cache-control", ""), path
        # vendor 仍然可缓存（ETag 协商）
        v = client.get("/static/vendor/qrcode.min.js")
        assert v.status_code == 200
        assert "no-store" not in v.headers.get("cache-control", "")


def test_preview_route_and_traversal_guard(home):
    (home / "proj" / "page.html").write_text("<h1>hello preview</h1>", encoding="utf-8")
    (home / "secret.txt").write_text("outside", encoding="utf-8")
    with make_client(home, []) as client:
        resp = client.get("/preview", params={"p": "page.html"})
        assert resp.status_code == 200 and "hello preview" in resp.text
        assert client.get("/preview", params={"p": "../secret.txt"}).status_code == 403
        assert client.get("/preview", params={"p": "nope.html"}).status_code == 404
        assert client.get("/preview").status_code == 400


def test_lan_token_guard_http(home, monkeypatch):
    update_config_section("server", {"lan": True, "token": "tok-123"})
    script = [[TextBlock(text="x")]]
    with make_client(home, script) as client:
        # 无令牌：首页 403；带令牌：放行并种 cookie
        r0 = client.get("/")
        assert r0.status_code == 403 and "令牌" in r0.text
        r1 = client.get("/", params={"token": "tok-123"})
        assert r1.status_code == 200 and "skysheep_token" in r1.headers.get("set-cookie", "")
        r2 = client.get("/", headers={"X-SkySheep-Token": "tok-123"})
        assert r2.status_code == 200
        # 错误令牌
        assert client.get("/", params={"token": "wrong"}).status_code == 403
