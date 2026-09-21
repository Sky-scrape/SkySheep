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
import os
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


# ---- web_search：自定义档（SearXNG GET / 通用 REST POST） ----


def _searxng_handler(request: httpx.Request) -> httpx.Response:
    """SearXNG 的 GET /search?format=json：不带鉴权，参数走查询串。"""
    assert request.method == "GET"
    assert request.url.path.endswith("/search")
    assert request.url.params.get("format") == "json"
    assert request.url.params.get("q") == "skysheep"
    return httpx.Response(200, json={"results": [
        {"title": "SearXNG 结果", "url": "https://s.example.com", "content": "自建搜索摘要"},
        {"title": "无链接应丢弃", "url": "", "content": "..."},
    ]})


async def test_web_search_custom_searxng_get():
    tool = WebSearchTool(provider="custom", base_url="http://localhost:8080",
                         transport=httpx.MockTransport(_searxng_handler))
    assert tool.configured  # 自建服务不带 Key 也算已配置
    out = await tool.run(WebSearchArgs(query="skysheep"), make_ctx(Path(".")))
    assert "SearXNG 结果" in out and "https://s.example.com" in out
    assert "无链接" not in out


def _custom_rest_handler(request: httpx.Request) -> httpx.Response:
    """通用 REST：POST JSON + Bearer，结果放在 data.results。"""
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer ck"
    import json as _json

    assert _json.loads(request.content) == {"query": "q", "max_results": 6}
    return httpx.Response(200, json={"data": {"results": [
        {"name": "自定义结果", "link": "https://c.example.com", "snippet": "摘要"},
    ]}})


async def test_web_search_custom_rest_post():
    tool = WebSearchTool(provider="custom", api_key="ck",
                         base_url="https://search.internal/api",
                         transport=httpx.MockTransport(_custom_rest_handler))
    out = await tool.run(WebSearchArgs(query="q"), make_ctx(Path(".")))
    assert "自定义结果" in out and "https://c.example.com" in out


async def test_web_search_custom_falls_back_to_other_protocol():
    """协议猜错时自动互备：地址看着像 SearXNG，实际是 POST 接口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405, text="Method Not Allowed")
        return httpx.Response(200, json={"results": [
            {"title": "互备结果", "url": "https://f.example.com", "content": "ok"},
        ]})

    tool = WebSearchTool(provider="custom", base_url="https://searx.example.com",
                         transport=httpx.MockTransport(handler))
    out = await tool.run(WebSearchArgs(query="q"), make_ctx(Path(".")))
    assert "互备结果" in out


async def test_web_search_custom_html_response_falls_back():
    """SearXNG 的 POST /search 会回 HTML 页面（不是 JSON），也要能换协议重试。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text="<!DOCTYPE html><html>...",
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, json={"results": [
            {"title": "HTML 互备结果", "url": "https://h.example.com", "content": "ok"},
        ]})

    tool = WebSearchTool(provider="custom", base_url="http://localhost:8080/search",
                         transport=httpx.MockTransport(handler))
    out = await tool.run(WebSearchArgs(query="q"), make_ctx(Path(".")))
    assert "HTML 互备结果" in out


async def test_web_search_custom_requires_base_url():
    from skysheep.tools.base import ToolError

    tool = WebSearchTool(provider="custom")
    assert not tool.configured
    with pytest.raises(ToolError, match="联网搜索未配置"):
        await tool.run(WebSearchArgs(query="x"), make_ctx(Path(".")))


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


def test_resolve_websearch_custom(home, monkeypatch):
    """自定义档只认 base_url，Key 可留空（自建 SearXNG 默认无鉴权）。"""
    update_config_section("websearch", {"provider": "custom", "api_key": "tk"})
    assert resolve_websearch(load_config()) is None  # 没填地址 → 未配置
    update_config_section("websearch", {"base_url": "http://localhost:8080"})
    got = resolve_websearch(load_config())
    assert got and got["provider"] == "custom" and got["base_url"] == "http://localhost:8080"
    # 自定义档不参与 auto：自动档里唯一的基础设施是用户自填地址，不能悄悄生效
    update_config_section("websearch", {"provider": "auto", "api_key": ""})
    assert resolve_websearch(load_config()) is None


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
        assert w["provider"] == "auto"
        # 设置页只渲染「自动 / 自定义 / 已配置」三档；已配置服务是单独一屏的列表
        assert w["providers"] == ["auto", "custom"]
        assert "configured_services" in w
        ws.send_json({"id": "w2", "method": "websearch.save",
                      "params": {"provider": "bocha", "api_key": "k9"}})
        w2 = recv_until(ws, "w2")["result"]
        assert w2["resolved_provider"] == "bocha" and w2["has_key"]
        # 自定义档：存 base_url 后立即可用，无需 Key
        ws.send_json({"id": "w3", "method": "websearch.save",
                      "params": {"provider": "custom",
                                 "base_url": "http://localhost:8080"}})
        w3 = recv_until(ws, "w3")["result"]
        assert w3["resolved_provider"] == "custom" and w3["base_url"] == "http://localhost:8080"
        assert w3["has_key"]

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

    锁两件事：搜索框/计数/列表容器在技能页的折叠块里，以及过滤与高亮的实现约定——
    多关键词空格分隔且为 AND 语义，高亮必须走转义（索引内容是外部输入）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # 搜索渲染逻辑现在住在 renderMarketInto（技能页的折叠块展开时调用）
    body = js[js.index("async function renderMarketInto("):]
    body = body[:body.index("\n// 折叠块")]

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
    assert "const markAll" in body
    assert "escapeHtml(text.slice(a, b))" in body
    assert "merged" in body
    # 列表自己滚（条目多了不至于把搜索框顶出视野）
    assert ".market-list {" in css
    market_list_css = css[css.index(".market-list {"):]
    market_list_css = market_list_css[:market_list_css.index("}")]
    assert "max-height" in market_list_css and "overflow-y: auto" in market_list_css
    # 不再是弹窗：入口是技能页里的折叠块，展开时才拉索引
    assert 'id="skill-market"' in html and "<details" in html
    assert 'id="btn-market"' not in html
    assert "renderMarketInto(body" in js


def test_local_skills_render_inline_not_modal(home):
    """「本机现存」：候选直接平铺在技能页按钮下方，不再弹窗。

    锁住三件事：面板节点在技能页 HTML 里；候选渲染进面板而不是 showModal；
    导入后重新探测（已装过的要转为灰显）。后端只读探测协议不变
    （skills.scan_local 覆盖在 test_skills_scan.py）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    for needle in ("btn-scan-skill", "btn-scan-skill-2", "skill-local-panel",
                   "skill-local-list", "skill-local-scope", "btn-skill-local-import"):
        assert needle in html, f"技能页缺本机候选节点：{needle}"
    # 两个入口按钮的文案都改为「本机现存」，旧的「扫描本机」不再出现
    assert html.count("本机现存") >= 4, "按钮与说明文案都要改叫「本机现存」"
    assert "扫描本机" not in html

    # 渲染函数住在 loadLocalSkills 里，且把候选写进面板容器
    body = js[js.index("async function loadLocalSkills()"):]
    body = body[:body.index("function deleteSkillModal")]
    assert 'request("skills.scan_local")' in body
    assert 'id="skill-local-list"' not in body  # 用 getElementById 取，不拼标签
    assert "skill-local-list" in body and "scan-list" in body
    # 不再是弹窗：这段代码里不该出现 showModal
    assert "showModal" not in body
    # 导入走既有 skills.install，单个失败不中断；成功后重新探测
    import_body = js[js.index("async function importLocalSkills()"):]
    import_body = import_body[:import_body.index("function deleteSkillModal")]
    assert 'request("skills.install"' in import_body
    assert "loadLocalSkills()" in import_body
    # 面板样式：候选列表自己滚，底部一行是「装到哪里 + 导入所选」
    assert "#skill-local-panel" in css and ".scan-foot" in css


def test_skills_page_has_scope_controls(home):
    """技能独立页：总览卡片可点进入，页内能设使用范围、预览指令、逛广场。"""
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # 总览 / 独立页两个视图都在，且有返回入口
    for needle in ('id="skill-list-view"', 'id="skill-manage-view"', 'id="btn-skill-back"',
                   'id="skill-summary"', 'id="settings-skill-list"'):
        assert needle in html, f"技能页缺节点：{needle}"
    assert "function openSkillManage()" in js and "function resetSkillView()" in js
    # 进入设置时回到总览（与模型服务同一套「先重置再进」约定）
    assert "resetSkillView();" in js[js.index("function openSettings("):][:600]

    # 范围控件：三档 + 项目勾选 + 保存
    for needle in ('"all"', '"projects"', '"none"', "renderScopePicker", "scope-save"):
        assert needle in js, f"范围控件缺：{needle}"
    # 三态如实呈现（生效中 / 本项目已停用 / 本项目不适用）
    assert "function skillState(" in js
    for label in ("生效中", "本项目已停用", "本项目不适用"):
        assert label in js, f"缺少状态文案：{label}"
    # 范围不适用时不显示本项目开关（否则会让人以为开关坏了）
    assert "if (s.applies)" in js
    # 技能名可点开完整指令
    assert "skills.body" in js and "previewSkill" in js
    # 指定项目但一个都没勾：前端先拦一道，不发请求
    assert "至少要勾选一个项目" in js
    # 项目列表来自 project.list（勾选项要跟着项目变化）
    assert 'request("project.list")' in js


def test_skills_scope_protocol(home):
    """skills.scope / skills.body 协议：改范围热生效、预览读原文。"""
    script = [[TextBlock(text="ok")]]
    gdir = Path(os.environ["SKYSHEEP_HOME"]) / "skills" / "pdf-tools"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "SKILL.md").write_text(
        "---\nname: pdf-tools\ndescription: 合并 PDF\n---\n\n用 pypdf 处理。\n",
        encoding="utf-8",
    )
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "boot"})
        snap = recv_until(ws, "s1")["result"]
        item = next(s for s in snap["skills"] if s["name"] == "pdf-tools")
        # 默认所有项目可用，快照带范围字段
        assert item["scope"] == "all" and item["applies"] is True
        assert item["scope_projects"] == []
        assert "scope_config" in snap["skill_dirs"]

        # 设为任何项目都不用 → 立即不生效（热生效，不用重启）
        ws.send_json({"id": "s2", "method": "skills.scope",
                      "params": {"name": "pdf-tools", "mode": "none", "projects": []}})
        r = recv_until(ws, "s2")["result"]
        assert r["mode"] == "none" and r["applies"] is False
        ws.send_json({"id": "s3", "method": "boot"})
        snap2 = recv_until(ws, "s3")["result"]
        assert next(s for s in snap2["skills"] if s["name"] == "pdf-tools")["applies"] is False

        # 预览：读 SKILL.md 原文（不受启用/范围限制）
        ws.send_json({"id": "s4", "method": "skills.body", "params": {"name": "pdf-tools"}})
        body = recv_until(ws, "s4")["result"]
        assert "name: pdf-tools" in body["text"] and "pypdf" in body["text"]

        # 指定项目但一个都没勾 → 报错（后端也要拦）
        ws.send_json({"id": "s5", "method": "skills.scope",
                      "params": {"name": "pdf-tools", "mode": "projects", "projects": []}})
        err = recv_until(ws, "s5")
        assert not err["ok"] and "至少" in err["error"]


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
    """局域网模式守卫矩阵：远端必须验令牌（缺失/错误 403，正确放行并种 cookie）；
    本机永远免令牌——桌面窗口自己不带令牌，不能被挡在门外（2026-09-18 修复）。"""
    from skysheep.server import app as server_app

    update_config_section("server", {"lan": True, "token": "tok-123"})
    script = [[TextBlock(text="x")]]
    # 本机（TestClient 按本机对待）无令牌直接放行：桌面不能被挡在门外（先于强制来源）
    with make_client(home, script) as client:
        assert client.get("/").status_code == 200
    monkeypatch.setattr(server_app, "client_origin", lambda c: "tailscale")
    with make_client(home, script) as client:
        # 远端（tailnet/局域网）无令牌：首页 403；带令牌：放行并种 cookie
        r0 = client.get("/")
        assert r0.status_code == 403 and "令牌" in r0.text
        r1 = client.get("/", params={"token": "tok-123"})
        assert r1.status_code == 200 and "skysheep_token" in r1.headers.get("set-cookie", "")
        r2 = client.get("/", headers={"X-SkySheep-Token": "tok-123"})
        assert r2.status_code == 200
        # 错误令牌
        assert client.get("/", params={"token": "wrong"}).status_code == 403


def test_tailscale_guard_http_local_exempt(home):
    """仅远程访问（Tailscale）模式：本机免令牌（桌面自己不能被挡在门外）。

    tailnet 来源必须验令牌、物理局域网来源直接拒绝——来源分类在
    test_server.test_client_origin_classification 里锁，守卫分支与其共用。
    """
    update_config_section("server", {"tailscale": True, "token": "tok-123"})
    script = [[TextBlock(text="x")]]
    with make_client(home, script) as client:
        assert client.get("/").status_code == 200


def test_tailscale_guard_rejects_nontailnet_even_with_token(home, monkeypatch):
    """仅远程访问模式：非 tailnet 来源（物理局域网等）带对令牌也 403——
    IP 段不对就没有商量余地，令牌不再是唯一门槛。"""
    from skysheep.server import app as server_app

    update_config_section("server", {"tailscale": True, "token": "tok-123"})
    script = [[TextBlock(text="x")]]
    monkeypatch.setattr(server_app, "client_origin", lambda c: "other")
    with make_client(home, script) as client:
        assert client.get("/", params={"token": "tok-123"}).status_code == 403
