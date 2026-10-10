"""CLI REPL 斜杠命令与主循环契约。

ChatApp 的打断路径已有 test_cli_interrupt.py 覆盖；此前 REPL 的日常面——
斜杠命令分发、主循环的标题落库 / 权限决策回传 / 会话持久化——只能靠手动
验证。本文件补上这一层，全部绕过 setup() 的完整装配（同 _make_app 模式），
只装被测面需要的部件。
"""

from __future__ import annotations

import asyncio
import io
import types

from conftest import FakeProvider
from pydantic import BaseModel
from rich.console import Console

from skysheep.cli.app import ChatApp
from skysheep.cli.render import Renderer
from skysheep.core.agent import Agent
from skysheep.events import PermissionRequest
from skysheep.messages import Message, TextBlock, ToolUseBlock
from skysheep.security.gate import Decision, PermissionGate
from skysheep.session.store import SessionStore
from skysheep.tools import ToolRegistry
from skysheep.tools.base import Safety, Tool

# ---- 测试桩（与 test_cli_interrupt 同源，按本文件需要裁剪） ----


class _FakePrompt:
    """按队列回放的 PromptSession 桩：队列耗尽后抛 EOFError（= Ctrl-D 退出）。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def prompt_async(self, *args, **kwargs) -> str:
        if not self.replies:
            raise EOFError
        return self.replies.pop(0)


class _EchoWriteArgs(BaseModel):
    path: str = "out.txt"


class _EchoWriteTool(Tool):
    name = "echo_write"
    description = "测试用写工具：需要确认后执行"
    safety = Safety.WRITE
    args_model = _EchoWriteArgs

    async def run(self, args, ctx):
        return "written:" + args.path


class _PermStreamProvider(FakeProvider):
    """第一轮请求一次写工具，权限放行后 FakeProvider 的脚本消费完按默认收尾。

    注意脚本只有一组：放行后 history 里的工具结果进入第二轮 stream() 调用，
    此时脚本耗尽、scripted_default 为空 → 纯 end_turn 收尾，不会再次请求权限。
    """

    def __init__(self) -> None:
        super().__init__([[
            TextBlock(text="我要写文件"),
            ToolUseBlock(id="tu-perm", name="echo_write", input={"path": "out.txt"}),
        ]])


async def _make_app(tmp_path, provider, replies, tools=(), gate=None) -> ChatApp:
    (tmp_path / "proj").mkdir(exist_ok=True)
    app = object.__new__(ChatApp)
    app.console = Console(file=io.StringIO(), force_terminal=False, width=200)
    app.renderer = Renderer(app.console)
    app.store = await SessionStore(tmp_path / "cli.db").connect()
    app.project = await app.store.get_or_create_project(str(tmp_path / "proj"))
    app.session = await app.store.create_session(app.project.id)
    app.working_dir = tmp_path / "proj"
    app.args = types.SimpleNamespace(session=None)
    app.cfg = None  # 斜杠命令按需补；/model 用例单独构造
    app.provider = provider
    app.provider_name = "fake"
    app.agent = Agent(
        provider=provider,
        registry=ToolRegistry(list(tools)),
        gate=gate or PermissionGate(store=None, project_id=None),
        working_dir=tmp_path / "proj",
    )
    app.agent.set_system("system")
    app.prompt = _FakePrompt(replies)
    app.mcp = None
    app.skills = None
    app.tasks = None
    return app


async def _load_db(tmp_path, session_id):
    """run() 收尾会关掉 app.store，读库一律开新连接。"""
    store = await SessionStore(tmp_path / "cli.db").connect()
    try:
        return await store.load_messages(session_id)
    finally:
        await store.close()


async def _read_session(tmp_path, session_id):
    store = await SessionStore(tmp_path / "cli.db").connect()
    try:
        return await store.get_session(session_id)
    finally:
        await store.close()


# ---- 斜杠命令分发 ----


async def test_slash_exit_variants_quit(tmp_path):
    provider = FakeProvider([])
    for cmd in ("/exit", "/quit", "/q", "/EXIT"):
        app = await _make_app(tmp_path, provider, [cmd])
        assert await app.handle_command(cmd) is True, f"{cmd} 应退出 REPL"
        assert "bye" in app.console.file.getvalue()
        await app.store.close()


async def test_slash_help_prints_commands(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/help"])
    assert await app.handle_command("/help") is False
    out = app.console.file.getvalue()
    for word in ("/help", "/new", "/sessions", "/resume", "/model", "/tools",
                 "/whitelist", "/skills", "/mcp", "/cwd", "/export"):
        assert word in out, f"帮助里应出现 {word}"
    await app.store.close()


async def test_slash_unknown_command_hints_help(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/nope"])
    assert await app.handle_command("/nope") is False
    assert "unknown command" in app.console.file.getvalue()
    await app.store.close()


async def test_slash_new_creates_session(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/new"])
    old_id = app.session.id
    app._compose_system = lambda: "sys"  # 避开 skills/memory 依赖
    await app.handle_command("/new")
    assert app.session.id != old_id
    await app.store.close()


async def test_slash_sessions_lists_and_marks_current(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/sessions"])
    await app.handle_command("/sessions")
    out = app.console.file.getvalue()
    assert app.session.id in out and "(untitled)" in out
    assert "*" in out, "当前会话应带 * 标记"
    await app.store.close()


async def test_slash_cwd_prints_working_dir(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/cwd"])
    await app.handle_command("/cwd")
    assert str(tmp_path / "proj") in app.console.file.getvalue()
    await app.store.close()


async def test_slash_tools_lists_registry_with_safety(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/tools"], tools=[_EchoWriteTool()])
    await app.handle_command("/tools")
    out = app.console.file.getvalue()
    assert "echo_write" in out and "write" in out
    await app.store.close()


async def test_slash_resume_restores_history(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), [])
    other = await app.store.create_session(app.project.id)
    await app.store.append_message(other.id, Message.user("旧对话内容"))
    await app.store.append_message(other.id, Message.assistant([TextBlock(text="旧回答")]))

    app2 = await _make_app(tmp_path, FakeProvider([]), [])
    await app2.handle_command(f"/resume {other.id}")
    assert app2.session.id == other.id
    assert "resumed" in app2.console.file.getvalue()
    assert any("旧对话内容" in m.text for m in app2.agent.history)
    await app.store.close()
    await app2.store.close()


async def test_slash_resume_without_arg_shows_usage(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/resume"])
    await app.handle_command("/resume")
    assert "usage" in app.console.file.getvalue()
    await app.store.close()


async def test_slash_resume_missing_session_reports_error(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/resume no-such"])
    await app.handle_command("/resume no-such")
    assert "session not found" in app.console.file.getvalue()
    await app.store.close()


async def test_slash_whitelist_shows_project_rules(tmp_path):
    app = await _make_app(tmp_path, FakeProvider([]), ["/whitelist"])
    await app.store.add_rule(app.project.id, "run_command", "prefix", "git status")
    await app.handle_command("/whitelist")
    out = app.console.file.getvalue()
    assert "run_command" in out and "git status" in out
    assert "拼接" in out, "prefix 型 run_command 规则要带拼接提示"
    await app.store.close()


async def test_slash_export_writes_relative_to_workdir(tmp_path):
    """相对路径相对工作目录（/cwd 口径）解析，不落进程启动目录。"""
    provider = FakeProvider([[TextBlock(text="导出这段")]])
    app = await _make_app(tmp_path, provider, ["问一句", "/export out.md"])
    await asyncio.wait_for(app.run(), timeout=10)
    exported = tmp_path / "proj" / "out.md"
    assert exported.exists(), f"导出文件应落在 {exported}"
    body = exported.read_text(encoding="utf-8")
    assert "USER" in body.upper() and "导出这段" in body
    await app.store.close()


async def test_slash_export_default_writes_session_md(tmp_path):
    provider = FakeProvider([[TextBlock(text="默认导出")]])
    app = await _make_app(tmp_path, provider, ["问", "/export"])
    await asyncio.wait_for(app.run(), timeout=10)
    assert (tmp_path / "proj" / "skysheep-session.md").exists()
    await app.store.close()


async def test_slash_model_switches_provider(tmp_path):
    from skysheep.config import ProviderConfig
    from skysheep.models.fake import FakeProvider as FP
    app = await _make_app(tmp_path, FakeProvider([]), [])
    second = FP([])
    app.cfg = type("C", (), {"providers": {"fake": ProviderConfig(kind="fake"),
                                           "beta": ProviderConfig(kind="fake")}})()

    def _build(name):
        if name == "beta":
            return second, "beta"
        raise SystemExit("nope")
    app._build_provider = _build
    await app.handle_command("/model")  # 无参：列出
    out = app.console.file.getvalue()
    assert "fake" in out and "beta" in out
    await app.handle_command("/model beta")
    assert app.provider is second and app.provider_name == "beta"
    assert app.agent.provider is second, "切换后 agent 必须用新 provider"
    await app.store.close()


async def test_slash_model_unknown_reports_not_switch(tmp_path):
    from skysheep.config import ProviderConfig
    app = await _make_app(tmp_path, FakeProvider([]), [])
    app.cfg = type("C", (), {"providers": {"fake": ProviderConfig(kind="fake")}})()
    before = app.provider
    await app.handle_command("/model ghost")
    assert "unknown provider" in app.console.file.getvalue()
    assert app.provider is before
    await app.store.close()


# ---- 主循环行为 ----


async def test_main_loop_persists_turn_and_sets_title(tmp_path):
    provider = FakeProvider([[TextBlock(text="回答内容")]])
    app = await _make_app(tmp_path, provider, ["帮我看看这个很有意思的问题"])
    await asyncio.wait_for(app.run(), timeout=10)
    msgs = await _load_db(tmp_path, app.session.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert "帮我看看" in msgs[0].text and "回答内容" in msgs[1].text
    fresh = await _read_session(tmp_path, app.session.id)
    assert fresh.title == "帮我看看这个很有意思的问题"


async def test_main_loop_blank_line_and_commands_do_not_turn(tmp_path):
    """空行不进入轮次；斜杠命令只走命令分发，不产生对话消息。"""
    provider = FakeProvider([[TextBlock(text="答")]])
    app = await _make_app(tmp_path, provider, ["", "   ", "/help", "/cwd"])
    await asyncio.wait_for(app.run(), timeout=10)
    assert provider.calls == [], "空行与斜杠命令都不应触发模型"
    msgs = await _load_db(tmp_path, app.session.id)
    assert msgs == []
    await app.store.close()


async def test_main_loop_title_only_set_once(tmp_path):
    """标题只在首条消息设置：第二轮不应覆盖首条定的标题。"""
    provider = FakeProvider([
        [TextBlock(text="第一轮")],
        [TextBlock(text="第二轮")],
    ])
    app = await _make_app(tmp_path, provider, ["第一问标题", "第二问跟标题无关"])
    await asyncio.wait_for(app.run(), timeout=10)
    fresh = await _read_session(tmp_path, app.session.id)
    assert fresh.title == "第一问标题"


async def test_permission_flow_allow_once_runs_tool_and_persists(tmp_path):
    """真实门 + 真实决策回传：允许一次 → 工具执行 → 全链落库。"""
    gate = PermissionGate(store=None, project_id=None, working_dir=tmp_path / "proj")
    tool = _EchoWriteTool()
    provider = _PermStreamProvider()
    app = await _make_app(tmp_path, provider, ["y"], tools=[tool], gate=gate)

    async def scripted_permission(ev):
        assert isinstance(ev, PermissionRequest)
        assert ev.tool_name == "echo_write"
        return Decision.ALLOW_ONCE
    app._ask_permission = scripted_permission
    await asyncio.wait_for(app.run(), timeout=10)

    msgs = await _load_db(tmp_path, app.session.id)
    roles = [m.role for m in msgs]
    assert roles[0] == "user"
    assert "assistant" in roles and "tool" in roles
    tool_msg = next(m for m in msgs if m.role == "tool")
    tool_text = "".join(getattr(b, "content", "") or "" for b in tool_msg.content)
    assert "written:out.txt" in tool_text


async def test_permission_flow_deny_records_denial(tmp_path):
    """拒绝 → 不执行工具，工具结果落库为错误说明。"""
    gate = PermissionGate(store=None, project_id=None, working_dir=tmp_path / "proj")
    tool = _EchoWriteTool()
    provider = _PermStreamProvider()
    app = await _make_app(tmp_path, provider, ["n"], tools=[tool], gate=gate)

    async def scripted_permission(ev):
        return Decision.DENY
    app._ask_permission = scripted_permission
    await asyncio.wait_for(app.run(), timeout=10)

    msgs = await _load_db(tmp_path, app.session.id)
    tool_msg = next(m for m in msgs if m.role == "tool")
    tool_text = "".join(getattr(b, "content", "") or "" for b in tool_msg.content)
    assert "denied" in tool_text.lower() or "拒绝" in tool_text


async def test_permission_decision_values_flow_through_gate(tmp_path):
    """ALLOW_ALWAYS 走 persist_rule（store 缺席时尽力而为）；乱值按拒绝处理。"""
    gate = PermissionGate(store=None, project_id=None, working_dir=tmp_path / "proj")
    tool = _EchoWriteTool()
    provider = _PermStreamProvider()
    app = await _make_app(tmp_path, provider, ["a"], tools=[tool], gate=gate)
    persisted: list = []

    async def fake_persist(rule):
        persisted.append(rule)
    gate.persist_rule = fake_persist

    async def scripted_permission(ev):
        return Decision.ALLOW_ALWAYS
    app._ask_permission = scripted_permission
    await asyncio.wait_for(app.run(), timeout=10)
    assert persisted, "ALLOW_ALWAYS 应触发规则持久化"
    msgs = await _load_db(tmp_path, app.session.id)
    tool_msg = next(m for m in msgs if m.role == "tool")
    tool_text = "".join(getattr(b, "content", "") or "" for b in tool_msg.content)
    assert "written:out.txt" in tool_text


async def test_decision_map_covers_prompt_keys():
    """DECISION_MAP：y/a/n 与空串的映射契约（空回车=拒绝是安全默认）。"""
    from skysheep.cli.app import DECISION_MAP
    assert DECISION_MAP["y"] == Decision.ALLOW_ONCE
    assert DECISION_MAP["a"] == Decision.ALLOW_ALWAYS
    assert DECISION_MAP["n"] == Decision.DENY
    assert DECISION_MAP[""] == Decision.DENY
