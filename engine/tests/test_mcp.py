"""MCP 客户端测试：配置合并、工具包装、真实 stdio 集成。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from skysheep.mcp.client import (
    MCPManager,
    MCPServerConfig,
    MCPServerStatus,
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
    cfg, _warn = load_mcp_configs(gp, pp)
    assert set(cfg) == {"fetch", "fs"}
    assert cfg["fetch"].transport == "stdio"
    assert cfg["fs"].url == "http://b:8000/mcp"
    assert cfg["fs"].readonly is False  # 项目覆盖后不再继承 readonly
    assert cfg["fetch"].readonly is False


def test_config_invalid_entries(tmp_path):
    p = tmp_path / "bad.json"
    _write_mcp_json(p, {"mcpServers": {"broken": {"foo": 1}, "ok": {"command": "x"}}})
    cfg, _warn = load_mcp_configs(None, p)
    # pydantic 默认忽略未知字段，broken 也能构造但 transport 无效
    # （connect_all 会跳过并记录警告），ok 正常保留
    assert set(cfg) == {"broken", "ok"}
    assert cfg["broken"].transport == "invalid"
    assert cfg["ok"].transport == "stdio"


def test_broken_config_json_reports_warning(tmp_path):
    """配置 JSON 解析失败必须变成可读告警：静默当空配置会让用户以为从没配过。"""
    p = tmp_path / "mcp.json"
    p.write_text('{"mcpServers": { "fetch": ', encoding="utf-8")
    configs, warnings = load_mcp_configs(p, None)
    assert configs == {}
    assert warnings and "解析失败" in warnings[0]


def test_invalid_server_entry_reports_warning(tmp_path):
    """单个服务定义不合法：跳过它，但要在告警里点名，不能无声消失。"""
    p = tmp_path / "mcp.json"
    _write_mcp_json(p, {"mcpServers": {"bad": {"args": 123}, "ok": {"command": "x"}}})
    configs, warnings = load_mcp_configs(p, None)
    assert set(configs) == {"ok"}
    assert any("bad" in w for w in warnings)


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


async def test_mcp_protocol_error_does_not_disconnect():
    """JSON-RPC 层错误（未知工具/服务端参数校验）说明连接是好的：不断线不重连。"""
    from mcp.shared.exceptions import MCPError

    class ProtocolErrorSession:
        async def call_tool(self, name, arguments=None):
            raise MCPError(404, "Tool not found: nope")

    manager = FakeManager(ProtocolErrorSession())
    tool = MCPTool(
        manager=manager, server_name="calc", tool_name="nope", description="",
        input_schema={"type": "object"}, readonly=False,
    )
    ctx = ToolContext(working_dir=Path("."))
    try:
        await tool.run(tool.args_model(), ctx)
        raise AssertionError("should raise")
    except ToolError as e:
        assert "Tool not found" in str(e)
    assert manager.failures == [], "协议错误不是断线：不该上报 note_call_failure"
    assert manager.reconnect_requests == []


async def test_image_content_becomes_context_attachment():
    """MCP 返回的图片转成 ctx.images 附件（模型真能看到），文本照常拼接。"""
    from mcp.types import ImageContent

    session = FakeSession(CallToolResult(content=[
        TextContent(type="text", text="截图好了"),
        ImageContent(type="image", data="aGk=", mimeType="image/png"),
    ], is_error=False))
    tool = make_tool(session)
    ctx = ToolContext(working_dir=Path("."))
    out = await tool.run(tool.args_model(), ctx)
    assert "截图好了" in out and "1 张图片" in out
    assert len(ctx.images) == 1
    assert ctx.images[0].media_type == "image/png"
    assert ctx.images[0].data == "aGk="


def test_per_server_timeout_passthrough():
    """每服务器可配调用上限：配了用它，没配回落全局默认。"""
    from skysheep.mcp.client import CALL_TIMEOUT_S

    fast = make_tool(None)
    assert fast._call_timeout == CALL_TIMEOUT_S
    slow = MCPTool(
        manager=FakeManager(None), server_name="s", tool_name="t",
        description="", input_schema={}, readonly=False, call_timeout=300,
    )
    assert slow._call_timeout == 300


def test_server_readonly_narrowed_by_tool_annotations():
    """注解只收窄不放宽：hint=False 收回自动放行，缺失不回收服务器级授权。"""
    from skysheep.mcp.client import _effective_readonly

    class Ann:
        def __init__(self, hint):
            self.read_only_hint = hint

    assert _effective_readonly(False, "x", Ann(False)) is False  # 服务器没授权，注解说什么都不放行
    assert _effective_readonly(False, "x", Ann(True)) is False
    assert _effective_readonly(True, "git_status", Ann(True)) is True    # 读操作保持顺滑
    assert _effective_readonly(True, "git_add", Ann(False)) is False     # 写操作回权限门
    assert _effective_readonly(True, "fetch", None) is True              # 无注解不误伤
    assert _effective_readonly(True, "x", Ann(None)) is True             # hint 缺省 None


def test_friendly_error_maps_auth_failures():
    """401/403 要翻译成「凭证可能过期」的可读提示，而不是裸 httpx 错误。"""
    from skysheep.mcp.client import _friendly_error

    assert "鉴权失败" in _friendly_error(Exception("Server error '401 Unauthorized'"))
    assert "鉴权失败" in _friendly_error(Exception("Forbidden for url https://x"))
    assert "鉴权失败" not in _friendly_error(Exception("connection refused"))


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


# ---- M10：导入 stdio 定义要显式确认 ----


def test_import_stdio_requires_confirmation(tmp_path):
    """含 command 的定义：未确认时不写盘，报错里列出将执行的命令。"""
    from skysheep.mcp import MCPInstallError, import_servers, normalize_server

    path = tmp_path / "mcp.json"
    servers = {
        "fetch": normalize_server({"command": "uvx", "args": ["mcp-server-fetch"]}),
        "remote": normalize_server({"url": "http://example.com/mcp"}),
    }
    with pytest.raises(MCPInstallError) as e:
        import_servers(servers, path)
    msg = str(e.value)
    assert "uvx" in msg and "fetch" in msg
    assert "本机" in msg
    assert not path.exists(), "未确认前不得写盘"

    # 确认后写入；纯 http 服务不受影响
    out = import_servers(servers, path, confirmed=True)
    assert sorted(out["added"]) == ["fetch", "remote"]


def test_import_http_only_needs_no_confirmation(tmp_path):
    """只有 url 的服务不执行本机程序，无需确认。"""
    from skysheep.mcp import import_servers, normalize_server

    path = tmp_path / "mcp.json"
    out = import_servers({"remote": normalize_server({"url": "http://example.com/mcp"})}, path)
    assert out["added"] == ["remote"]


def test_pending_stdio_commands_skips_names_that_will_be_skipped(tmp_path):
    """dry-run 只列「这次真的会写进去」的 stdio 定义：同名不覆盖的不算。"""
    from skysheep.mcp import (
        normalize_server,
        pending_stdio_commands,
        save_servers,
    )

    path = tmp_path / "mcp.json"
    save_servers(path, {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}})
    servers = {
        "fetch": normalize_server({"command": "uvx", "args": ["mcp-server-fetch"]}),
        "other": normalize_server({"command": "npx", "args": ["-y", "some-mcp"]}),
    }
    assert pending_stdio_commands(servers, path) == [
        {"name": "other", "command": "npx", "args": ["-y", "some-mcp"]}
    ]
    assert [p["name"] for p in pending_stdio_commands(servers, path, overwrite=True)] == [
        "fetch", "other",
    ]


# ---- M11：keeper 内 initialize/list_tools 挂死必须被收割 ----

_HANG_SERVER = """
import json
import sys
import time

mode = sys.argv[1]
for line in sys.stdin:
    try:
        req = json.loads(line)
    except Exception:
        continue
    method = req.get("method", "")
    if method == "initialize":
        if mode == "init":
            time.sleep(3600)  # 挂死：对 initialize 永不响应
        print(json.dumps({
            "jsonrpc": "2.0", "id": req["id"],
            "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "serverInfo": {"name": "hang", "version": "1"}},
        }), flush=True)
    elif method == "tools/list":
        if mode == "tools":
            time.sleep(3600)  # 挂死：initialize 已过，list_tools 永不响应
        print(json.dumps({
            "jsonrpc": "2.0", "id": req["id"], "result": {"tools": []},
        }), flush=True)
"""


async def _connect_hanging_server(tmp_path, mode, monkeypatch):
    """连接一台会挂死在握手某一步的 stdio 服务器，返回 (manager, tools)。"""
    script = tmp_path / "hang_server.py"
    script.write_text(_HANG_SERVER, encoding="utf-8")
    # 压缩超时窗口：外层 connect 超时与 keeper 内部超时共用这个常量
    monkeypatch.setattr("skysheep.mcp.client.CONNECT_TIMEOUT_S", 2.0)
    manager = MCPManager({"hang": MCPServerConfig(
        command=sys.executable, args=[str(script), mode],
    )})
    tools = await manager.connect_server("hang", MCPServerConfig(
        command=sys.executable, args=[str(script), mode],
    ))
    return manager, tools


async def test_hanging_initialize_is_collected(tmp_path, monkeypatch):
    """服务器对 initialize 永不响应：不能留下常驻 keeper 与孤儿子进程。

    旧实现只在 keeper 外包超时——超时后 set 一下 stop 事件就返回，而卡在
    initialize 里的 keeper 根本没在等这个事件，从此无人收割（直到关机）。
    """
    manager, tools = await _connect_hanging_server(tmp_path, "init", monkeypatch)
    try:
        assert tools == []
        st = manager.statuses["hang"]
        assert st.connected is False
        assert st.error and "超时" in st.error
        # 关键断言：失败连接不留常驻 keeper / stop 事件（旧实现会泄漏在这里）
        assert "hang" not in manager._keepers
        assert "hang" not in manager._stops
        task = manager._keepers.get("hang")
        assert task is None or task.done()
    finally:
        await manager.shutdown()


async def test_hanging_list_tools_is_collected(tmp_path, monkeypatch):
    """initialize 正常、tools/list 挂死：同样必须被收割，不留半开连接。"""
    manager, tools = await _connect_hanging_server(tmp_path, "tools", monkeypatch)
    try:
        assert tools == []
        st = manager.statuses["hang"]
        assert st.connected is False
        assert "hang" not in manager._keepers
        assert "hang" not in manager._stops
    finally:
        await manager.shutdown()


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
    # git 预设：服务器级标只读（查询免弹窗）——写操作由客户端按服务器自带的
    # read_only_hint=False 收回自动放行（见 _effective_readonly 的测试）
    git = preset_by_name("git")
    assert git is not None and git["readonly"] is True
    assert "确认" in git["desc"], "desc 要说清写操作会请求确认"
    # 每个预设声明探测用的运行时名（前端据此置灰缺依赖的卡片）
    for p in MCP_PRESETS:
        assert p["runtime"], f"预设 {p['name']} 缺 runtime 探测名"
    # cua-driver：操控真实桌面的预设，绝不能标只读（只读会被权限门自动放行）；
    # 命令与官方 MCP 接入方式一致（cua-driver mcp，stdio）
    cd = preset_by_name("cua-driver")
    assert cd is not None
    assert cd["readonly"] is False
    assert cd["need"] == "local"
    assert cd["command"] == "cua-driver" and cd["args"] == ["mcp"]
    # 前端只拿元数据，不外泄 command/args；available 是运行时探测结果
    pub = presets_public()
    assert set(pub[0]) == {"name", "label", "desc", "need", "readonly", "available"}
    assert "command" not in pub[0]
    assert isinstance(pub[0]["available"], bool)


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
    # git 预设不带 --repository {dir}：指向非 git 目录时服务器会启动即退出
    assert called["args"] == ["mcp-server-git"]
    assert called["readonly"] is True
    assert called["overwrite"] is False  # 预设不覆盖已有

    # {dir} 替换逻辑由 filesystem 预设覆盖（它的目录参数是允许目录，非 git 仓库也能用）
    await backend.add_mcp_preset("filesystem")
    assert called["name"] == "filesystem"
    assert str(tmp_path / "proj") in called["args"]  # {dir} 已替换
    assert "{dir}" not in "".join(called["args"])


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


async def test_setup_connects_mcp_in_background(home):
    """启动不再同步等 MCP：setup 立即返回，连接在后台完成后注入注册表。"""
    from skysheep.mcp.installer import mcp_config_path
    from skysheep.server.backend import ServerBackend

    _write_mcp_json(mcp_config_path(home / "home"), {
        "mcpServers": {"demo": {"command": sys.executable, "args": [str(SERVER_SCRIPT)]}},
    })
    be = ServerBackend(working_dir=home / "proj")
    try:
        await be.setup()
        # setup 返回即服务可就绪：连接交给后台任务，完成后才注入 mcp_tools
        assert be._mcp_connect_task is not None
        await asyncio.wait_for(be._mcp_connect_task, timeout=30)
        st = be.mcp.statuses["demo"]
        assert st.connected is True
        assert st.connecting is False  # 完成后「连接中」标记要清掉
        assert "mcp__demo__add" in {t.name for t in be.mcp_tools}
        # 注册表随后台连接重建：Agent 不等下一轮配置操作就能看到 MCP 工具
        assert "mcp__demo__add" in {t.name for t in be._base_agent.registry.all()}
        row = next(s for s in be._mcp_status_list(be.mcp) if s["name"] == "demo")
        assert row["connected"] is True and row["connecting"] is False
        assert be.mcp_warnings == []
    finally:
        await be.shutdown()


async def test_hanging_mcp_does_not_delay_boot(home, tmp_path, monkeypatch):
    """MCP 服务器挂死不再拖住启动：连接超时只在后台烧，setup 立即返回。

    2026-09-22/23 实测踩过：同步等 MCP 连接（代理拒连 + uvx 拉包重试 15~19s）
    把「服务就绪」拖过桌面端启动预算，弹了「启动失败」页——连接已后台化。
    """
    import time

    from skysheep.mcp.installer import mcp_config_path
    from skysheep.server.backend import ServerBackend

    script = tmp_path / "hang_server.py"
    script.write_text(_HANG_SERVER, encoding="utf-8")
    _write_mcp_json(mcp_config_path(home / "home"), {
        "mcpServers": {"hang": {"command": sys.executable, "args": [str(script), "init"]}},
    })
    be = ServerBackend(working_dir=home / "proj")
    try:
        started = time.monotonic()
        await be.setup()
        elapsed = time.monotonic() - started
        assert elapsed < 10, "setup 不应等 MCP 连接（超时预算 20s 只许在后台烧）"
        # 连接仍在后台进行：「连接中」已标记、任务未完成；关机时会被取消收割
        assert be._mcp_connect_task is not None and not be._mcp_connect_task.done()
        assert be.mcp.statuses["hang"].connecting is True
    finally:
        await be.shutdown()
    assert be.mcp.statuses["hang"].connecting is False


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


async def test_restart_loop_stops_after_consecutive_failures(monkeypatch):
    """回归：**连续失败**达到上限必须停手，不能以恒定间隔无限重试下去。

    旧实现只在成功时计数，失败从不累加——一台永远起不来的服务器会被
    每 2 秒拉起一次进程直到天荒地老，前端还一直显示「重连中」。
    """
    mgr = MCPManager({"srv": MCPServerConfig(command="definitely-not-a-real-cmd-xyz")})
    mgr.RESTART_BASE_DELAY_S = 0.01
    mgr.RESTART_MAX_DELAY_S = 0.02
    calls = {"n": 0}
    real = mgr._connect_one

    async def counting(name, cfg):
        calls["n"] += 1
        return await real(name, cfg)

    monkeypatch.setattr(mgr, "_connect_one", counting)
    mgr.request_reconnect("srv")
    await asyncio.wait_for(mgr._restart_tasks["srv"], 10)
    assert calls["n"] == mgr.MAX_AUTO_RESTARTS, "恰好尝试上限次，一次不多"
    assert "srv" in mgr._gave_up
    assert "已停止重试" in (mgr.statuses["srv"].error or "")
    assert mgr.statuses["srv"].reconnecting is False
    # 放弃后重复预约不再开新链（已完成的旧句柄留在字典里无妨）
    done_task = mgr._restart_tasks["srv"]
    mgr.request_reconnect("srv")
    assert mgr._restart_tasks.get("srv") is done_task, "已放弃的服务器不该再起重连任务"
    await mgr.shutdown()


async def test_successful_reconnect_resets_failure_cap():
    """成功重连清零连续失败计数：长期会话里偶尔断一次不该被历史失败误伤。"""
    mgr = MCPManager({"srv": MCPServerConfig(command="definitely-not-a-real-cmd-xyz")})
    mgr.RESTART_BASE_DELAY_S = 0.01
    mgr.RESTART_MAX_DELAY_S = 0.02
    mgr.request_reconnect("srv")
    await asyncio.wait_for(mgr._restart_tasks["srv"], 10)
    assert "srv" in mgr._gave_up
    assert mgr.statuses["srv"].attempts == mgr.MAX_AUTO_RESTARTS

    # 手动换上能连的配置：显式连接清零计数与放弃标记
    tools = await mgr.connect_server(
        "srv", MCPServerConfig(command=sys.executable, args=[str(SERVER_SCRIPT)])
    )
    assert "mcp__srv__add" in {t.name for t in tools}
    assert mgr.statuses["srv"].connected is True
    assert mgr.statuses["srv"].attempts == 0
    assert "srv" not in mgr._gave_up

    # 再断一次：自动重连应重新被允许（计数已清零）
    mgr.note_call_failure("srv")
    assert "srv" in mgr._restart_tasks
    await mgr.shutdown()
    assert mgr.statuses["srv"].connected is False


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


# ---- 停用与按服务器连接管理（配置差量同步的地基） ----


async def test_disabled_server_is_listed_but_never_connected():
    """停用的服务器保留状态条目（前端显示「已停用」）但不发起连接。"""
    mgr = MCPManager({
        "off": MCPServerConfig(command=sys.executable, args=[str(SERVER_SCRIPT)], enabled=False),
    })
    try:
        tools = await mgr.connect_all()
        assert tools == []
        st = mgr.statuses["off"]
        assert st.connected is False and st.enabled is False
        assert mgr.session_for("off") is None
        # 停用状态也不该被重连链拉起
        mgr.request_reconnect("off")
        assert "off" not in mgr._restart_tasks
    finally:
        await mgr.shutdown()


async def test_connect_server_replaces_and_forget_removes():
    """connect_server 按新配置连接（含停用→断开）；forget_server 抹掉全部痕迹。"""
    mgr = MCPManager({})
    tools = await mgr.connect_server(
        "demo", MCPServerConfig(command=sys.executable, args=[str(SERVER_SCRIPT)])
    )
    assert "mcp__demo__add" in {t.name for t in tools}
    assert mgr.tools_for("demo")  # per-server 工具存储供 backend 重建注册表

    # 改成停用：连接应断开、配置保留
    await mgr.connect_server("demo", MCPServerConfig(command="x", enabled=False))
    assert mgr.statuses["demo"].connected is False
    assert mgr.statuses["demo"].enabled is False
    assert mgr.session_for("demo") is None

    # 停用的服务器重新启用 → 换回正常配置即可恢复
    await mgr.connect_server(
        "demo", MCPServerConfig(command=sys.executable, args=[str(SERVER_SCRIPT)])
    )
    assert mgr.statuses["demo"].connected is True

    mgr.forget_server("demo")
    assert "demo" not in mgr.statuses
    assert mgr.tools_for("demo") == []
    await mgr.shutdown()


async def test_backend_sync_mcp_changes_is_incremental(tmp_path, monkeypatch):
    """配置差量同步：只连新增/变更的、断开删除的，未变更的不动。"""
    from skysheep.mcp.installer import mcp_config_path, save_servers
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    backend = ServerBackend(working_dir=tmp_path)
    backend._apply_registry_to_agents = lambda: None  # 无 setup 环境不重建注册表

    class FakeMgr:
        def __init__(self):
            self.statuses: dict = {}
            self.calls: list[tuple] = []

        async def disconnect_server(self, name):
            self.calls.append(("disconnect", name))

        def forget_server(self, name):
            self.calls.append(("forget", name))

        async def connect_server(self, name, cfg):
            self.calls.append(("connect", name))
            return []

        def tools_for(self, name):
            return []

    backend.mcp = FakeMgr()
    backend.mcp_configs = {
        "keep": MCPServerConfig(command="x"),
        "drop": MCPServerConfig(command="y"),
    }
    save_servers(mcp_config_path(tmp_path / "home"), {
        "keep": {"command": "x"},   # 未变更：不应触发连接
        "new": {"command": "z"},    # 新增：要连
    })
    warnings = await backend._sync_mcp_changes()
    assert warnings == []
    assert ("connect", "new") in backend.mcp.calls
    assert ("connect", "keep") not in backend.mcp.calls
    assert ("forget", "drop") in backend.mcp.calls
    assert set(backend.mcp_configs) == {"keep", "new"}


async def test_backend_set_mcp_enabled_flips_config(tmp_path, monkeypatch):
    """停用开关：mcp.json 里写 enabled=false，配置其余字段原样保留。"""
    from skysheep.mcp.installer import load_servers, mcp_config_path, save_servers
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    backend = ServerBackend(working_dir=tmp_path)
    backend._apply_registry_to_agents = lambda: None
    cfg_path = mcp_config_path(tmp_path / "home")
    save_servers(cfg_path, {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}})

    class FakeMgr:
        def __init__(self):
            self.statuses: dict = {}

        async def disconnect_server(self, name):
            pass

        def forget_server(self, name):
            pass

        async def connect_server(self, name, cfg):
            st = MCPServerStatus(name)
            st.enabled = cfg.enabled
            self.statuses[name] = st
            return []

        def tools_for(self, name):
            return []

    backend.mcp = FakeMgr()
    backend.mcp_configs = {"fetch": MCPServerConfig(command="uvx")}

    r = await backend.set_mcp_enabled("fetch", False)
    assert r["enabled"] is False and "已停用" in r["hint"]
    saved = load_servers(cfg_path)["fetch"]
    assert saved["enabled"] is False
    assert saved["command"] == "uvx" and saved["args"] == ["mcp-server-fetch"]

    r = await backend.set_mcp_enabled("fetch", True)
    assert r["enabled"] is True
    assert "enabled" not in load_servers(cfg_path)["fetch"], "启用即默认态，不写冗余字段"


def test_mcp_tool_exports_annotations():
    """to_schema 必须带 annotations 四布尔（与内置工具同一份契约）。

    read_only_hint 用收窄后的最终判定（= 权限门的实际口径，注解只收窄不
    放宽）；其余三项透传服务器显式声明的值，未声明落 Tool 基类的保守缺省
    ——可疑其有写、有破坏性，让外部宿主多提醒一次。
    """
    from types import SimpleNamespace

    declared = SimpleNamespace(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    )
    # 调用点（_connect_one）传的就是收窄后的 readonly：服务器标 readonly
    # 但工具自带 read_only_hint=False 时算出 False → WRITE 口径
    from skysheep.mcp.client import _effective_readonly

    narrowed = _effective_readonly(True, "t", declared)
    assert narrowed is False
    tool = MCPTool(
        manager=FakeManager(None), server_name="srv", tool_name="t",
        description="", input_schema={"type": "object"}, readonly=narrowed,
        annotations=declared,
    )
    schema = tool.to_schema()
    assert set(schema) == {"name", "description", "input_schema", "annotations"}
    assert tool.safety == Safety.WRITE
    assert schema["annotations"] == {
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": False,
    }

    # 未声明任何注解：read_only 沿用服务器级授权，其余落保守缺省
    plain_readonly = MCPTool(
        manager=FakeManager(None), server_name="srv", tool_name="t",
        description="", input_schema={"type": "object"}, readonly=True,
    ).to_schema()["annotations"]
    assert plain_readonly == {
        "readOnlyHint": True, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    }
    plain_write = MCPTool(
        manager=FakeManager(None), server_name="srv", tool_name="t",
        description="", input_schema={"type": "object"}, readonly=False,
    ).to_schema()["annotations"]
    assert plain_write["readOnlyHint"] is False
    assert plain_write["destructiveHint"] is True


async def test_backend_save_mcp_server_preserves_disabled_state(tmp_path, monkeypatch):
    """表单没有「停用」字段：覆盖已停用的同名服务时保留停用状态。

    否则用户只是改个参数保存，服务就被顺手重新启用并拉起（stdio 等于立即
    执行本机命令），超出这次操作表达的意思；停用的服务也不得发起连接。
    """
    from skysheep.mcp.installer import load_servers, mcp_config_path, save_servers
    from skysheep.server.backend import ServerBackend

    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    backend = ServerBackend(working_dir=tmp_path)
    backend._apply_registry_to_agents = lambda: None
    cfg_path = mcp_config_path(tmp_path / "home")

    class FakeMgr:
        def __init__(self):
            self.statuses: dict = {}
            self.connected: list[str] = []

        async def disconnect_server(self, name):
            pass

        def forget_server(self, name):
            pass

        async def connect_server(self, name, cfg):
            if cfg.enabled:
                self.connected.append(name)
            st = MCPServerStatus(name)
            st.enabled = cfg.enabled
            self.statuses[name] = st
            return []

        def tools_for(self, name):
            return []

    mgr = FakeMgr()
    backend.mcp = mgr

    save_servers(cfg_path, {"fetch": {"command": "uvx", "enabled": False}})
    backend.mcp_configs = {"fetch": MCPServerConfig(command="uvx", enabled=False)}
    await backend.save_mcp_server("fetch", command="uvx", args=["mcp-server-fetch", "--new"])
    saved = load_servers(cfg_path)["fetch"]
    assert saved["enabled"] is False, "改参数不能顺手把停用的服务启用"
    assert saved["args"] == ["mcp-server-fetch", "--new"], "其余字段正常更新"
    assert mgr.connected == [], "停用的服务不得被拉起"

    # 已存在且未停用的服务：保存后照常连接（编辑不等同于保留停用）
    save_servers(cfg_path, {"fetch": {"command": "uvx"}})
    backend.mcp_configs = {"fetch": MCPServerConfig(command="uvx")}
    await backend.save_mcp_server("fetch", command="uvx", args=["mcp-server-fetch"])
    assert mgr.connected == ["fetch"]
    # 落盘带的是显式默认值 enabled: true（model_dump 非 None 字段），语义即启用
    assert load_servers(cfg_path)["fetch"]["enabled"] is True


def test_mcp_status_list_flags_insecure_http_with_headers():
    """审查 S-09：http:// + 鉴权头 → insecure_http=True（不下发 headers 内容）。

    https、或没配鉴权头的 http 都不算明文风险面。
    """
    from types import SimpleNamespace

    from skysheep.mcp.client import MCPServerConfig, MCPServerStatus
    from skysheep.server.backend import ServerBackend

    def mgr(configs: dict):
        return SimpleNamespace(
            statuses={n: MCPServerStatus(n) for n in configs},
            _configs=configs,
        )

    rows = ServerBackend._mcp_status_list(mgr({
        "plain-http": MCPServerConfig(url="http://intranet.example/mcp",
                                      headers={"Authorization": "Bearer x"}),
        "tls": MCPServerConfig(url="https://ok.example/mcp",
                               headers={"Authorization": "Bearer x"}),
        "no-headers": MCPServerConfig(url="http://lan.example/mcp"),
    }))
    by_name = {r["name"]: r for r in rows}
    assert by_name["plain-http"]["insecure_http"] is True
    assert by_name["tls"]["insecure_http"] is False
    assert by_name["no-headers"]["insecure_http"] is False
    assert all("headers" not in r for r in rows), "鉴权头内容不得进前端载荷"
