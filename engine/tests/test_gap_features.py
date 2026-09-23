"""对标主流 Agent 工具补齐的三项能力测试：

- 分级权限模式（confirm/accept_edits/full_access 三档；ui.json 持久化；远端拦截见 test_permission_hardening）
- 忽略文件（.skysheepignore / .gitignore / .env 内建默认 → @索引/glob/grep）
- headless 一次性运行（skysheep run：预授权门控 + 落库 + 结构化结果）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from conftest import FakeProvider
from test_server import make_client, recv_until  # noqa: F401

from skysheep.cli.app import run_headless
from skysheep.config import db_path
from skysheep.core.ignore import IgnoreRules, _Rule
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolContext
from skysheep.tools.fs import GlobArgs, GlobTool
from skysheep.tools.search import GrepArgs, GrepTool

# ---- 后台任务强引用与节流状态（安全审查低危项） ----


async def test_spawn_bg_holds_reference_until_done():
    """spawn_bg 在任务结束前持强引用（asyncio 只持弱引用，会被 GC 掉）。"""
    import asyncio
    import gc

    from skysheep.bgtasks import pending_count, spawn_bg

    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(0.05)

    before = pending_count()
    task = spawn_bg(work())
    await started.wait()
    gc.collect()  # 旧实现下未持引用的任务可能在这里消失
    assert not task.done()
    assert pending_count() >= before + 1
    await task
    for _ in range(50):
        if pending_count() < before + 1:
            break
        await asyncio.sleep(0.01)
    assert pending_count() == before, "任务结束后应从登记表移除"


def test_token_throttle_reset_keeps_blocks():
    """令牌轮换：失败计数清零，但已生效的封锁保留（换锁不等于放人进来）。"""
    from skysheep.server.backend import TokenThrottle

    t = TokenThrottle(threshold=2, base_delay=60.0)
    assert t.note_failure("1.2.3.4") == 0.0
    assert t.note_failure("1.2.3.4") > 0  # 达阈值 → 封锁
    assert t.blocked("1.2.3.4")
    t.reset_failures(keep_blocks=True)
    assert t.blocked("1.2.3.4"), "已生效的封锁应保留"
    t.reset_failures(keep_blocks=False)
    assert not t.blocked("1.2.3.4")


# ---- CLI 渲染剥离终端控制序列（安全审查 M14） ----


def test_cli_sanitize_strips_terminal_control_sequences():
    """模型/工具输出里的 ANSI 转义不得打到终端（清屏/改标题/隐藏光标）。"""
    from skysheep.cli.render import sanitize_terminal_text

    dirty = (
        "正常文本"
        "\x1b[2J"          # 清屏
        "\x1b]0;被改的标题\x07"  # 改窗口标题（OSC）
        "\x1b[?25l"        # 隐藏光标
        "\x1b7"            # 存游标（ESC 7）
        "\x1b(B"           # 选字符集（ESC ( B）
        "\x07\x08"         # BEL / BS
        "\x9b31m"          # C1 CSI
        "结尾"
    )
    got = sanitize_terminal_text(dirty)
    assert "\x1b" not in got and "\x07" not in got and "\x9b" not in got
    assert got == "正常文本结尾", repr(got)
    # 换行与制表符保留（多行输出不能被压成一行）
    assert sanitize_terminal_text("a\nb\tc") == "a\nb\tc"
    # 孤立 ESC（截断的序列）也要清掉，不能留在终端里当控制字符
    assert sanitize_terminal_text("x\x1b") == "x"


def test_cli_render_sanitizes_tool_and_permission_text():
    """渲染路径上：工具结果预览与权限面板正文都过了剥离。"""
    import io

    from rich.console import Console

    from skysheep.cli.render import Renderer
    from skysheep.events import PermissionRequest, ToolCallFinished

    buf = io.StringIO()
    r = Renderer(Console(file=buf, force_terminal=False, width=200))
    r.handle(ToolCallFinished(
        tool_call_id="t1", name="read_file", preview="内容\x1b[2J还在",
        is_error=False, duration_ms=1,
    ))
    r.handle(PermissionRequest(
        request_id="p1", tool_name="run_command", input={"command": "echo \x1b[31mhi"},
        safety="dangerous", detail="", diff="", note="注意\x1b]0;标题\x07",
        rule_kind="prefix", rule_pattern="echo\x1b[2J",
    ))
    r.close()
    out = buf.getvalue()
    assert "\x1b" not in out, "渲染后的输出里不得残留 ESC"


# ---- 子进程环境剥离（安全审查 M6） ----


def test_child_env_strips_secret_like_vars(monkeypatch):
    """密钥形态的环境变量不进子进程；PATH / HOME 这类跑命令必需的原样留着。"""
    from skysheep.tools.shell import _child_env, _is_secret_env_name

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-1")
    monkeypatch.setenv("MY_RELAY_TOKEN", "tok-2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-3")
    monkeypatch.setenv("DB_PASSWORD", "pw-4")
    monkeypatch.setenv("PGPASSWORD", "pw-5")
    monkeypatch.setenv("SIGNING_PRIVATE_KEY", "pk-6")
    monkeypatch.setenv("SOME_CREDENTIALS", "cred-7")
    monkeypatch.setenv("PLAIN_SETTING", "not-a-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/usr/lib/git-askpass")  # 名字带 pass 但是助手脚本路径

    assert _is_secret_env_name("OPENAI_API_KEY")
    assert _is_secret_env_name("PGPASSWORD")
    assert not _is_secret_env_name("GIT_ASKPASS")
    assert not _is_secret_env_name("PLAIN_SETTING")

    env = _child_env()
    for name in ("OPENAI_API_KEY", "MY_RELAY_TOKEN", "AWS_SECRET_ACCESS_KEY",
                 "DB_PASSWORD", "PGPASSWORD", "SIGNING_PRIVATE_KEY", "SOME_CREDENTIALS"):
        assert name not in env, name
    assert env["PLAIN_SETTING"] == "not-a-secret"
    assert env["GIT_ASKPASS"] == "/usr/lib/git-askpass"
    # 剥完之后命令还得能跑：Windows 的 cmd.exe 依赖 SystemRoot/ComSpec，POSIX 依赖 PATH。
    # os.environ 在 Windows 上大小写不敏感（键被规范成大写），_child_env() 返回的是
    # 普通 dict，所以按小写比对。
    lowered = {k.lower(): v for k, v in env.items()}
    for essential in ("PATH", "SystemRoot" if sys.platform == "win32" else "HOME"):
        if essential.lower() in {k.lower() for k in os.environ}:
            assert lowered[essential.lower()] == os.environ[essential], essential
    # 不动本进程自己的 os.environ：config.resolve_api_key 读密钥照常
    assert os.environ["OPENAI_API_KEY"] == "sk-secret-1"


async def test_run_command_cannot_echo_api_key(tmp_path, monkeypatch):
    """端到端：被确认执行的命令也读不到密钥类变量（「完全访问」档下同样读不到）。"""
    from skysheep.tools.shell import IS_WINDOWS, RunCommandArgs, RunCommandTool

    monkeypatch.setenv("SKYSHEEP_TEST_API_KEY", "sk-leak-me")
    monkeypatch.setenv("SKYSHEEP_TEST_PLAIN", "visible-ok")
    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path)
    quote = (lambda v: f"%{v}%") if IS_WINDOWS else (lambda v: f"${v}")

    out = await tool.run(
        RunCommandArgs(command=f"echo {quote('SKYSHEEP_TEST_API_KEY')}"), ctx)
    assert "sk-leak-me" not in out
    out = await tool.run(
        RunCommandArgs(command=f"echo {quote('SKYSHEEP_TEST_PLAIN')}"), ctx)
    assert "visible-ok" in out


# ---- 分级权限模式 ----


async def test_gate_auto_accept_write(home, tmp_path):
    from skysheep.tools.base import Safety
    from skysheep.tools.fs import WriteFileTool
    from skysheep.tools.shell import RunCommandTool

    # 工作目录是「自动允许写入」档的判定基准：只放行能确认落在目录内的写入
    gate = PermissionGate(store=None, project_id=None, working_dir=tmp_path)
    write_tool = WriteFileTool()
    cmd_tool = RunCommandTool()
    # 默认档：写入需确认
    assert await gate.authorize(write_tool, {"path": "a", "content": "b"}) is not None
    # accept_edits：目录内写入自动放行，高危仍需确认
    gate.auto_accept_write = True
    assert await gate.authorize(write_tool, {"path": "a", "content": "b"}) is None
    assert await gate.authorize(
        write_tool, {"path": str(tmp_path / "sub" / "a.txt"), "content": "b"}
    ) is None
    # 目录之外的写入不回落到自动档（以前这里会被无条件放行）
    outside = tmp_path.parent / "outside.txt"
    assert await gate.authorize(
        write_tool, {"path": str(outside), "content": "b"}
    ) is not None
    assert cmd_tool.safety == Safety.DANGEROUS
    assert await gate.authorize(cmd_tool, {"command": "ls"}) is not None


async def test_auto_accept_write_requires_known_target():
    """不知道工作目录、或工具说不清写到哪里时，自动档一律不生效。"""
    from skysheep.tools.fs import WriteFileTool

    write_tool = WriteFileTool()
    # 没给工作目录（如 CLI 早期路径、测试直连）：无从判定边界 → 仍需确认
    gate = PermissionGate(store=None, project_id=None)
    gate.auto_accept_write = True
    assert await gate.authorize(write_tool, {"path": "a", "content": "b"}) is not None


def test_permission_mode_ws_persisted(home):
    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "auto.txt", "content": "hi"})],
        [TextBlock(text="写好了")],
        [ToolUseBlock(id="t2", name="write_file", input={"path": "auto2.txt", "content": "hi2"})],
        [TextBlock(text="又写好了")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        # 切到 accept_edits
        ws.send_json({"id": "m1", "method": "permission.set_mode",
                      "params": {"mode": "accept_edits"}})
        assert recv_until(ws, "m1")["result"]["mode"] == "accept_edits"
        ws.send_json({"id": "m2", "method": "permission.mode"})
        assert recv_until(ws, "m2")["result"]["mode"] == "accept_edits"
        # 写文件一轮：不应出现任何权限请求事件
        events = []
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写个文件"}})
        recv_until(ws, "c1", events)
        kinds = [e["event"] for e in events]
        assert "permission_request" not in kinds
        assert (home / "proj" / "auto.txt").read_text(encoding="utf-8") == "hi"
        # 持久化到 ui.json
        prefs = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
        assert prefs.get("accept_edits") == 1
        # 切回确认档：再次写文件应出现权限请求
        ws.send_json({"id": "m3", "method": "permission.set_mode",
                      "params": {"mode": "confirm"}})
        recv_until(ws, "m3")
        # 切回确认档：再次写文件应出现权限请求；拒绝它让回合收尾
        ws.send_json({"id": "c2", "method": "chat.send", "params": {"text": "再写一次"}})
        kinds2 = []
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                kinds2.append(frame["event"])
                if frame["event"] == "permission_request":
                    ws.send_json({"id": "pr", "method": "permission.respond",
                                  "params": {"request_id": frame["data"]["request_id"],
                                             "decision": "deny"}})
                continue
            if frame.get("id") == "c2":
                break
        assert "permission_request" in kinds2
        # 拒绝后 ui.json 回落到 0
        prefs2 = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
        assert prefs2.get("accept_edits") == 0


async def test_gate_full_access(home, tmp_path):
    """完全访问档：写入（含目录外）与执行命令都自动放行；收回后恢复确认。"""
    from skysheep.tools.base import Safety
    from skysheep.tools.fs import WriteFileTool
    from skysheep.tools.shell import RunCommandTool

    gate = PermissionGate(store=None, project_id=None, working_dir=tmp_path)
    write_tool = WriteFileTool()
    cmd_tool = RunCommandTool()
    assert cmd_tool.safety == Safety.DANGEROUS
    # 只开自动编辑：命令仍需确认
    gate.auto_accept_write = True
    assert await gate.authorize(cmd_tool, {"command": "echo hi"}) is not None
    # 开完全访问：写入（连目录外）与命令都放行
    gate.auto_accept_all = True
    assert await gate.authorize(write_tool, {"path": "a", "content": "b"}) is None
    assert await gate.authorize(
        write_tool, {"path": str(tmp_path.parent / "outside.txt"), "content": "b"}
    ) is None
    assert await gate.authorize(cmd_tool, {"command": "echo hi"}) is None
    # 收回完全访问：命令回到确认
    gate.auto_accept_all = False
    assert await gate.authorize(cmd_tool, {"command": "echo hi"}) is not None


def test_full_access_mode_ws_and_restart(home):
    """完全访问档走 WS 通路：命令类高危也不再弹确认；档位持久化且重启后保持。"""
    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "auto.txt", "content": "hi"})],
        [TextBlock(text="写好了")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "permission.set_mode",
                      "params": {"mode": "full_access"}})
        assert recv_until(ws, "m1")["result"]["mode"] == "full_access"
        # 非法档位直接报错，档位不变
        ws.send_json({"id": "mx", "method": "permission.set_mode",
                      "params": {"mode": "yolo"}})
        assert recv_until(ws, "mx")["ok"] is False
        ws.send_json({"id": "m2", "method": "permission.mode"})
        assert recv_until(ws, "m2")["result"]["mode"] == "full_access"
        # 写文件一轮：不应出现任何权限请求事件
        events = []
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写个文件"}})
        recv_until(ws, "c1", events)
        assert "permission_request" not in [e["event"] for e in events]
        prefs = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
        assert prefs.get("accept_edits") == 2
    # 重启（新 client）：完全访问档从 ui.json 恢复
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m3", "method": "permission.mode"})
        assert recv_until(ws, "m3")["result"]["mode"] == "full_access"


# ---- 忽略文件 ----


def _write_ignore(proj: Path) -> None:
    (proj / ".skysheepignore").write_text(
        "# 注释行\nsecret*\nlogs/\n!keep.log\n/data/\n",
        encoding="utf-8",
    )


def test_ignore_rules_semantics(tmp_path):
    _write_ignore(tmp_path)
    rules = IgnoreRules.load(tmp_path)
    # 名字模式：任意层级
    assert rules.matches("secret_key.txt")
    assert rules.matches("src/secret_key.txt")
    # 取反：先忽略后取反 → keep.log 可见
    tmp_rules = IgnoreRules([_Rule("*.log"), _Rule("!keep.log")])
    assert tmp_rules.matches("a.log")
    assert not tmp_rules.matches("keep.log")
    # 目录规则：目录本体与内容一并忽略（目录条目需 is_dir=True）
    assert rules.matches("logs", is_dir=True)
    assert rules.matches("logs/x.txt")
    assert rules.matches("logs/sub/y.txt")
    # 锚定目录 /data/：只忽略根下 data，不忽略 src/data
    assert rules.matches("data/a.csv")
    assert not rules.matches("src/data/a.csv")
    # 内建默认：.env 永远忽略
    assert rules.matches(".env")
    assert rules.matches("src/.env.local")
    assert not rules.matches("notes.md")


def test_glob_and_grep_respect_ignore(tmp_path):
    _write_ignore(tmp_path)
    (tmp_path / "a.py").write_text("x=1", encoding="utf-8")
    (tmp_path / "secret_key.txt").write_text("k=1", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "l.txt").write_text("log line", encoding="utf-8")
    ctx = ToolContext(working_dir=tmp_path)
    import asyncio

    g = asyncio.run(GlobTool().run(GlobArgs(pattern="**/*"), ctx))
    assert "a.py" in g
    assert "secret_key.txt" not in g
    assert "logs" not in g
    assert ".env" not in g
    grep = asyncio.run(GrepTool().run(GrepArgs(pattern="."), ctx))
    assert "a.py" in grep
    assert "secret_key.txt" not in grep
    assert ".env" not in grep


def test_fs_files_respects_ignore(home):
    (home / "proj" / "visible.txt").write_text("ok", encoding="utf-8")
    (home / "proj" / ".env").write_text("TOKEN=x", encoding="utf-8")
    (home / "proj" / ".skysheepignore").write_text("hidden*\n", encoding="utf-8")
    (home / "proj" / "hidden.txt").write_text("x", encoding="utf-8")
    script = [[TextBlock(text="x")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "f1", "method": "fs.files"})
        r = recv_until(ws, "f1")["result"]
    assert "visible.txt" in r["files"]
    assert "hidden.txt" not in r["files"]
    assert ".env" not in r["files"]
    # read_file 显式点名不受 ignore 影响（只约束发现类操作）——fs.files 是索引层


# ---- headless 一次性运行 ----


async def test_run_headless_text_and_persist(home, capsys):
    provider = FakeProvider([[TextBlock(text="任务完成：42")]])
    result = await run_headless(
        "算出答案", directory=str(home / "proj"),
        provider_factory=lambda: provider, output="text",
    )
    assert result["stop_reason"] == "end_turn"
    assert result["reply"] == "任务完成：42"
    assert result["session_id"]
    out = capsys.readouterr().out
    assert "任务完成：42" in out
    # 会话落库可审计
    from skysheep.session import SessionStore

    store = await SessionStore(db_path()).connect()
    try:
        sess = await store.get_session(result["session_id"])
        assert sess is not None and sess.title.startswith("▶ ")
        msgs = await store.load_messages(result["session_id"])
        assert any(m.role == "assistant" and "42" in m.text for m in msgs)
    finally:
        await store.close()


async def test_run_headless_tool_gating(home):
    # 未预授权 write_file：工具被自动拒绝，文件不落盘
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file",
                      input={"path": "out.txt", "content": "data"})],
        [TextBlock(text="好的，写不了就算了")],
    ])
    await run_headless(
        "写个文件", directory=str(home / "proj"),
        provider_factory=lambda: provider, output="json",
    )
    assert not (home / "proj" / "out.txt").exists()

    # 预授权后：写入放行
    provider2 = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file",
                      input={"path": "out.txt", "content": "data"})],
        [TextBlock(text="写好了")],
    ])
    result2 = await run_headless(
        "写个文件", directory=str(home / "proj"),
        provider_factory=lambda: provider2, output="json",
        allow_tools=["write_file"],
    )
    assert (home / "proj" / "out.txt").read_text(encoding="utf-8") == "data"
    assert result2["reply"] == "写好了"
    json.loads(json.dumps(result2))  # 可 JSON 序列化（CI 管道友好）
