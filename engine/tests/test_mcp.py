"""MCP 客户端测试：配置合并、工具包装、真实 stdio 集成。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from skysheep.mcp.client import (
    MCPManager,
    MCPServerConfig,
    MCPTool,
    load_mcp_configs,
)
from skysheep.tools.base import Safety, ToolContext, ToolError


def _write_mcp_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_config_merge_project_overrides_global(tmp_path):
    gp, pp = tmp_path / "global.json", tmp_path / "proj.json"
    _write_mcp_json(gp, {
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
            "fs": {"url": "http://a:8000/mcp", "readonly": True},
        }
    })
    _write_mcp_json(pp, {
        "mcpServers": {"fs": {"url": "http://b:8000/mcp"}}  # 项目覆盖同名
    })
    cfg = load_mcp_configs(gp, pp)
    assert set(cfg) == {"fetch", "fs"}
    assert cfg["fetch"].transport == "stdio"
    assert cfg["fs"].url == "http://b:8000/mcp"
    assert cfg["fs"].readonly is False  # 项目覆盖后不再继承 readonly
    assert cfg["fetch"].readonly is False


def test_config_invalid_entries(tmp_path):
    p = tmp_path / "bad.json"
    _write_mcp_json(p, {"mcpServers": {"broken": {"foo": 1}, "ok": {"command": "x"}}})
    cfg = load_mcp_configs(None, p)
    # pydantic 默认忽略未知字段，broken 也能构造但 transport 无效
    # （connect_all 会跳过并记录警告），ok 正常保留
    assert set(cfg) == {"broken", "ok"}
    assert cfg["broken"].transport == "invalid"
    assert cfg["ok"].transport == "stdio"


class FakeSession:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self.result


def test_mcp_tool_wrapping_and_safety():
    session = FakeSession(CallToolResult(
        content=[TextContent(type="text", text="42")], is_error=False
    ))
    tool = MCPTool(
        server_name="calc", tool_name="add", description="加法",
        input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        session=session, readonly=False,
    )
    assert tool.name == "mcp__calc__add"
    assert tool.safety == Safety.WRITE
    schema = tool.to_schema()
    assert schema["input_schema"]["properties"]["a"]["type"] == "integer"
    # 宽松入参模型：schema 之外的参数也能通过校验并透传
    args = tool.args_model(a=40, b=2)
    assert args.model_dump() == {"a": 40, "b": 2}


async def test_mcp_tool_run_and_error():
    session = FakeSession(CallToolResult(
        content=[TextContent(type="text", text="42")], is_error=False
    ))
    tool = MCPTool(
        server_name="calc", tool_name="add", description="",
        input_schema={"type": "object"}, session=session, readonly=True,
    )
    assert tool.safety == Safety.READONLY
    ctx = ToolContext(working_dir=Path("."))
    args = tool.args_model(a=40, b=2)
    out = await tool.run(args, ctx)
    assert out == "42"
    assert session.calls == [("add", {"a": 40, "b": 2})]

    err_session = FakeSession(CallToolResult(
        content=[TextContent(type="text", text="boom")], is_error=True
    ))
    tool2 = MCPTool("calc", "sub", "", {"type": "object"}, err_session, False)
    try:
        await tool2.run(tool2.args_model(x=1), ctx)
        raise AssertionError("should raise")
    except ToolError as e:
        assert "boom" in str(e)


SERVER_SCRIPT = Path(__file__).parents[1] / "examples" / "mcp_demo_server.py"


async def test_unreachable_server_fails_fast_with_readable_error():
    """地址不通/命令不存在时要快速失败并给出可读提示，不能挂住界面。"""
    import time

    # 关着的端口：连不上应立即返回（预检 3 秒内），而不是等 SDK 超时
    manager = MCPManager({"dead": MCPServerConfig(url="http://127.0.0.1:1/mcp")})
    started = time.monotonic()
    tools = await manager.connect_all()
    elapsed = time.monotonic() - started
    assert tools == []
    assert elapsed < 10, f"连不上时应快速失败，实际用了 {elapsed:.1f}s"
    st = manager.statuses["dead"]
    assert st.connected is False
    assert st.error and "连不上" in st.error
    await manager.shutdown()

    # 命令不存在：也要立刻给出可读提示
    manager2 = MCPManager({"nocmd": MCPServerConfig(command="definitely-not-a-real-cmd-xyz")})
    tools2 = await manager2.connect_all()
    assert tools2 == []
    assert "启动命令不存在" in manager2.statuses["nocmd"].error
    await manager2.shutdown()


async def test_one_bad_server_does_not_break_others():
    """一个服务器连不上，不能影响另一个正常服务器注册工具。"""
    manager = MCPManager({
        "bad": MCPServerConfig(url="http://127.0.0.1:1/mcp"),
        "demo": MCPServerConfig(command=sys.executable, args=[str(SERVER_SCRIPT)]),
    })
    try:
        tools = await manager.connect_all()
        names = {t.name for t in tools}
        assert "mcp__demo__add" in names, "好的服务器仍应连上"
        assert manager.statuses["demo"].connected is True
        assert manager.statuses["bad"].connected is False
    finally:
        await manager.shutdown()
    assert manager.statuses["demo"].connected is False


async def test_real_stdio_roundtrip():
    """真实 MCP 集成：通过 stdio 启动本地 demo server 并完成一次工具调用。"""
    assert SERVER_SCRIPT.exists(), SERVER_SCRIPT
    manager = MCPManager({"demo": MCPServerConfig(command=sys.executable, args=[str(SERVER_SCRIPT)])})
    try:
        tools = await manager.connect_all()
        names = {t.name for t in tools}
        assert "mcp__demo__add" in names
        assert "mcp__demo__echo" in names
        st = manager.statuses["demo"]
        assert st.connected and len(st.tool_names) == 3

        add_tool = next(t for t in tools if t.name == "mcp__demo__add")
        args = add_tool.args_model(a=20, b=22)
        out = await add_tool.run(args, ToolContext(working_dir=Path(".")))
        assert "42" in out
    finally:
        await manager.shutdown()
    assert manager.statuses["demo"].connected is False


# ---- 内置常用 MCP 预设 ----


def test_mcp_presets_shape():
    """预设清单结构自检：字段齐全、名字合法唯一、主打项 sequential-thinking 在列。"""
    from skysheep.mcp import MCP_PRESETS, preset_by_name, presets_public
    from skysheep.mcp.installer import SERVER_NAME_RE, validate_name

    names = [p["name"] for p in MCP_PRESETS]
    assert len(names) == len(set(names)), "预设 name 不能重复"
    for p in MCP_PRESETS:
        assert SERVER_NAME_RE.match(p["name"])
        validate_name(p["name"])  # 非法名字会抛
        assert p["need"] in ("uv", "node")
        assert p["label"] and p["desc"]
        assert isinstance(p["args"], list) and all(isinstance(a, str) for a in p["args"])
        assert p["command"]
        assert isinstance(p["readonly"], bool)
    # 主打项：结构化分步思考，与内置工具互补
    st = preset_by_name("sequential-thinking")
    assert st is not None and st["need"] == "node"
    # 前端只拿元数据，不外泄 command/args
    pub = presets_public()
    assert set(pub[0]) == {"name", "label", "desc", "need", "readonly"}
    assert "command" not in pub[0]


async def test_add_mcp_preset_writes_and_substitutes_dir(tmp_path, monkeypatch):
    """点预设 → 按原文写入 mcp.json、{dir} 换成工作目录；不真连外部包。"""
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    backend = ServerBackend(working_dir=tmp_path / "proj")
    # 预设里 git/filesystem 带 {dir} 占位符，添加时应替换成真实工作目录
    called: dict = {}

    async def fake_save(name, **kw):
        called["name"] = name
        called.update(kw)
        return {"added": [name], "skipped": [], "mcp": [], "mcp_warnings": []}

    monkeypatch.setattr(backend, "save_mcp_server", fake_save)
    await backend.add_mcp_preset("git")
    assert called["name"] == "git"
    assert called["command"] == "uvx"
    assert str(tmp_path / "proj") in called["args"]  # {dir} 已替换
    assert "{dir}" not in "".join(called["args"])
    assert called["readonly"] is True
    assert called["overwrite"] is False  # 预设不覆盖已有


async def test_add_mcp_preset_unknown_name(tmp_path, monkeypatch):
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    backend = ServerBackend(working_dir=tmp_path)
    try:
        await backend.add_mcp_preset("no-such-preset-xyz")
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "内置预设" in str(e)


async def test_add_mcp_preset_already_installed_hint(tmp_path, monkeypatch):
    """同名已存在 → save 走 skipped，hint 给出「已添加过」而不是重复装。"""
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))

    async def fake_save(name, **kw):
        return {"added": [], "skipped": [name], "mcp": [], "mcp_warnings": []}

    backend = ServerBackend(working_dir=tmp_path)
    monkeypatch.setattr(backend, "save_mcp_server", fake_save)
    r = await backend.add_mcp_preset("time")
    assert r["skipped"] == ["time"]
    assert "已经添加过" in r["hint"]


def test_mcp_installed_names(tmp_path, monkeypatch):
    from skysheep.mcp.installer import mcp_config_path, save_servers
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    save_servers(mcp_config_path(tmp_path / "home"), {
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    })
    backend = ServerBackend(working_dir=tmp_path)
    backend._reload_mcp_configs()  # 正常启动里由 setup() 做；这里手动触发
    assert backend.mcp_installed_names() == ["fetch"]
