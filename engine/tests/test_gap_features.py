"""对标主流 Agent 工具补齐的三项能力测试：

- 分级权限模式（accept_edits 自动允许写入；命令仍确认；ui.json 持久化）
- 忽略文件（.skysheepignore / .gitignore / .env 内建默认 → @索引/glob/grep）
- headless 一次性运行（skysheep run：预授权门控 + 落库 + 结构化结果）
"""

from __future__ import annotations

import json
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
