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


class FakeManager:
    """只实现 MCPTool 需要的那部分契约（会话解析 + 断线上报）。"""

    def __init__(self, session=None) -> None:
        self.session = session
        self.failures: list[str] = []
        self.reconnect_requests: list[str] = []

    def session_for(self, name):
        return self.session

    def note_call_failure(self, name):
        # 镜像真实 MCPManager 的契约：断线后既记录失败也预约重连
        self.failures.append(name)
        self.request_reconnect(name)

    def request_reconnect(self, name):
        self.reconnect_requests.append(name)


class ExplodingSession:
    """模拟传输层断开的会话（连接错，不是工具层 is_error）。"""

    async def call_tool(self, name, arguments=None):
        raise ConnectionError("server gone")


def make_tool(session, readonly=False, server="calc", tool="add"):
    return MCPTool(
        manager=FakeManager(session), server_name=server, tool_name=tool,
        description="", input_schema={"type": "object"}, readonly=readonly,
    )


def test_mcp_tool_wrapping_and_safety():
    session = FakeSession(CallToolResult(
        content=[TextContent(type="text", text="42")], is_error=False
    ))
    tool = MCPTool(
        manager=FakeManager(session), server_name="calc", tool_name="add",
        description="加法",
        input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        readonly=False,
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
    tool = make_tool(session, readonly=True)
    assert tool.safety == Safety.READONLY
    ctx = ToolContext(working_dir=Path("."))
    args = tool.args_model(a=40, b=2)
    out = await tool.run(args, ctx)
    assert out == "42"
    assert session.calls == [("add", {"a": 40, "b": 2})]

    err_session = FakeSession(CallToolResult(
        content=[TextContent(type="text", text="boom")], is_error=True
    ))
    tool2 = make_tool(err_session, tool="sub")
    try:
        await tool2.run(tool2.args_model(x=1), ctx)
        raise AssertionError("should raise")
    except ToolError as e:
        assert "boom" in str(e)
    # 工具层语义错误（is_error）不是断线：不应误触发重连
    assert tool2._manager.failures == []
    assert tool2._manager.reconnect_requests == []


async def test_mcp_tool_resolves_session_at_call_time():
    """工具不固持 session：重连后换上新会话，已注册的工具实例照常可用。"""
    manager = FakeManager(None)
    tool = MCPTool(
        manager=manager, server_name="calc", tool_name="add", description="",
        input_schema={"type": "object"}, readonly=True,
    )
    ctx = ToolContext(working_dir=Path("."))
    # 服务器未连接：给可读错误，并预约重连，而不是让调用卡到超时
    try:
        await tool.run(tool.args_model(), ctx)
        raise AssertionError("should raise")
    except ToolError as e:
        assert "未连接" in str(e)
    assert manager.reconnect_requests == ["calc"]

    # 重连成功（manager 换上新会话）后，同一个工具对象直接可用
    session = FakeSession(CallToolResult(
        content=[TextContent(type="text", text="ok")], is_error=False
    ))
    manager.session = session
    assert await tool.run(tool.args_model(), ctx) == "ok"


async def test_mcp_tool_transport_error_reports_failure_without_replay():
    """传输层断开：上报断线并预约重连，但**不重放**本次调用（副作用可能已发生）。"""
    manager = FakeManager(ExplodingSession())
    tool = MCPTool(
        manager=manager, server_name="calc", tool_name="write", description="",
        input_schema={"type": "object"}, readonly=False,
    )
    ctx = ToolContext(working_dir=Path("."))
    try:
        await tool.run(tool.args_model(x=1), ctx)
        raise AssertionError("should raise")
    except ToolError as e:
        assert "MCP call failed" in str(e)
    assert manager.failures == ["calc"]
    assert manager.reconnect_requests == ["calc"]


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
        assert p["need"] in ("uv", "node", "local")
        assert p["label"] and p["desc"]
        assert isinstance(p["args"], list) and all(isinstance(a, str) for a in p["args"])
        assert p["command"]
        assert isinstance(p["readonly"], bool)
    # 主打项：结构化分步思考，与内置工具互补
    st = preset_by_name("sequential-thinking")
    assert st is not None and st["need"] == "node"
    # cua-driver：操控真实桌面的预设，绝不能标只读（只读会被权限门自动放行）；
    # 命令与官方 MCP 接入方式一致（cua-driver mcp，stdio）
    cd = preset_by_name("cua-driver")
    assert cd is not None
    assert cd["readonly"] is False
    assert cd["need"] == "local"
    assert cd["command"] == "cua-driver" and cd["args"] == ["mcp"]
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


# ---- 工具描述的形状约束：协议不限制长度/内容，这里做通用缓解 ----


def test_long_mcp_description_is_truncated():
    """超长描述被截断并附提示，避免挤占上下文、也避免藏进长串指令。"""
    from skysheep.mcp.client import MAX_MCP_DESCRIPTION_CHARS, _sanitize_description

    out = _sanitize_description("x" * (MAX_MCP_DESCRIPTION_CHARS + 500))
    assert len(out) <= MAX_MCP_DESCRIPTION_CHARS + 20
    assert "已截断" in out


def test_mcp_description_blank_lines_are_collapsed():
    """大量空行会被压掉：否则可把注入内容推到看不见的位置。"""
    from skysheep.mcp.client import _sanitize_description

    out = _sanitize_description("normal\n" + "\n" * 50 + "hidden instruction")
    assert "\n\n\n" not in out
    assert "normal" in out and "hidden instruction" in out


def test_empty_mcp_description_has_placeholder():
    from skysheep.mcp.client import _sanitize_description

    assert _sanitize_description("") == "(no description)"


# ---- 断连重连：有界、去重、不重放 ----


async def test_manager_reconnect_is_deduplicated():
    """同一台服务器同时只允许一条重连链，重复预约不叠加。"""
    mgr = MCPManager({"srv": MCPServerConfig(command="definitely-not-a-real-cmd-xyz")})
    mgr.request_reconnect("srv")
    first = mgr._restart_tasks.get("srv")
    mgr.request_reconnect("srv")
    assert mgr._restart_tasks.get("srv") is first, "重复预约不该开出第二条重连链"
    await mgr.shutdown()
    assert not mgr._restart_tasks


async def test_manager_gives_up_after_restart_cap():
    """达到重连上限后停止自动重试，并把结论写进 status 交给用户手动处理。"""
    mgr = MCPManager({"srv": MCPServerConfig(command="definitely-not-a-real-cmd-xyz")})
    st = mgr.statuses["srv"]
    st.restarts = mgr.MAX_AUTO_RESTARTS
    mgr.request_reconnect("srv")
    assert "srv" in mgr._gave_up
    assert mgr._restart_tasks.get("srv") is None, "已放弃的服务器不该再起重连任务"
    assert "已停止重试" in (st.error or "")
    await mgr.shutdown()


async def test_manager_note_call_failure_marks_disconnected_and_schedules():
    """调用失败要把连接标记为断开、清掉旧会话，并预约一次后台重连。"""
    mgr = MCPManager({"srv": MCPServerConfig(command="definitely-not-a-real-cmd-xyz")})
    st = mgr.statuses["srv"]
    st.connected = True
    mgr._sessions["srv"] = object()  # 假装有个会话
    mgr.note_call_failure("srv")
    assert st.connected is False
    assert mgr.session_for("srv") is None
    assert "srv" in mgr._restart_tasks
    await mgr.shutdown()


async def test_session_for_returns_none_when_not_connected():
    """未连接的服务器不给会话：工具据此走可读错误而不是拿死会话去调。"""
    mgr = MCPManager({"srv": MCPServerConfig(command="x")})
    assert mgr.session_for("srv") is None
    st = mgr.statuses["srv"]
    st.connected = True
    fake = object()
    mgr._sessions["srv"] = fake
    assert mgr.session_for("srv") is fake
    await mgr.shutdown()
