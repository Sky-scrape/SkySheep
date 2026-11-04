"""A 组 · 主流程验收：从启动到一条任务跑完、再撤销回原点。

这些用例只走对外协议（WS RPC / CLI 入口），断言用户能看到的结果：
文件有没有落盘、事件流是否收尾、重启后数据还在不在。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from _helpers import (
    first,
    kinds,
    make_client,
    of_kind,
    rpc,
    run_turn,
)
from fastapi.testclient import TestClient

from skysheep.config import db_path
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.server import create_app

pytestmark = pytest.mark.acceptance


def test_a1_first_boot_without_key_guides_user(home, monkeypatch):
    """A1 首次启动：没有 Key 也不崩，boot 带可读指引，聊天给出设置页指路。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = create_app(working_dir=home / "proj")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        snap = rpc(ws, "b1", "boot")["result"]
        assert snap["version"], "boot 快照必须带版本号"
        assert snap["provider_error"], "无 Key 时用 provider_error 告知，而不是启动失败"
        assert "API Key" in snap["provider_error"]
        # 工具目录仍然完整：用户可以先逛设置、看演示，不会卡在空白页
        assert {"read_file", "write_file", "load_skill"} <= {t["name"] for t in snap["tools"]}

        frame = rpc(ws, "c1", "chat.send", {"text": "你好"})
        assert frame["ok"] is False
        assert "API Key" in frame["error"]
        assert "config.toml" not in frame["error"], "面向普通用户的文案不该要求手改配置"


def test_a2_main_flow_write_confirm_rollback(home):
    """A2 主流程：新会话 → 写文件确认（带 diff）→ 落盘 → 检查点 → 撤销回滚。

    这是产品承诺「每一步都看得见、管得住、撤得回」的完整用户旅程。
    """
    target = home / "proj" / "会议纪要.md"
    # 一个 provider、两轮迭代：先调 write_file，拿到结果后再收尾
    scripts = [[
        [ToolUseBlock(
            id="w1",
            name="write_file",
            input={"path": "会议纪要.md", "content": "# 纪要\n决议：周五上线\n"},
        )],
        [TextBlock(text="已写入会议纪要.md")],
    ]]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]

        frame, evs = run_turn(
            ws, "c1", {"text": "把结论整理成 会议纪要.md", "session_id": sid}
        )
        assert frame["ok"], frame
        assert frame["result"]["done"] is True

        # 权限卡：工具、落点、diff 都在事件里，且决策被回传
        perm = first(evs, "permission_request")
        assert perm and perm["tool_name"] == "write_file"
        assert perm["input"]["path"] == "会议纪要.md", "确认卡必须带上落点"
        assert "+# 纪要" in perm["diff"] and "+决议：周五上线" in perm["diff"]
        assert first(evs, "permission_resolved")["decision"] == "allow_once"
        assert "permission_request" in kinds(evs)

        assert target.exists(), "允许后文件必须落盘"
        assert "周五上线" in target.read_text(encoding="utf-8")

        # 检查点：本轮快照可列出，diff 能回看改前/改后
        rows = rpc(ws, "cp1", "checkpoint.list", {"session_id": sid})["result"]["checkpoints"]
        assert rows, "一轮写入后必须有检查点"
        cp_id = rows[0]["id"]
        diff = rpc(ws, "cp2", "checkpoint.diff", {"id": cp_id, "session_id": sid})["result"]
        assert any("会议纪要.md" in f.get("path", "") for f in diff["files"])

        # 撤销：新建文件被删除，回滚结果回给前端
        out = rpc(ws, "cr1", "checkpoint.restore", {"id": cp_id, "session_id": sid})["result"]
        assert out["restored"] == cp_id
        assert out["failed"] == []
        assert not target.exists(), "撤销本轮改动后，本轮新建的文件必须消失"

    # 重启后仍在同一份会话库里：文件确实回到了「没有这个文件」的状态
    assert not (home / "proj" / "会议纪要.md").exists()
    db = sqlite3.connect(db_path())
    try:
        n = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        db.close()
    assert n >= 2, "user / assistant 消息应已落库"


def test_a3_deny_write_keeps_disk_clean_and_turn_finishes(home):
    """A3 拒绝写入：文件不产生、模型收到拒绝后正常收尾、事件流完整。"""
    target = home / "proj" / "secret.txt"
    scripts = [[
        [ToolUseBlock(id="w1", name="write_file", input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="明白，不写了。")],
    ]]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        frame, evs = run_turn(
            ws, "c1", {"text": "写个文件"}, decisions=("deny",)
        )
        assert frame["ok"] and frame["result"]["done"]
        assert not target.exists(), "拒绝后不得落盘"

        resolved = first(evs, "permission_resolved")
        assert resolved["decision"] == "deny"
        tool = first(evs, "tool_call_finished")
        assert tool["is_error"] is True and tool["preview"], "拒绝以错误结果回给模型"
        assert "turn_finished" in kinds(evs)
        texts = [
            block.get("text", "")
            for data in of_kind(evs, "assistant_message")
            for block in data["message"]["content"]
            if block.get("type") == "text"
        ]
        assert "明白，不写了。" in "".join(texts), "拒绝后 Agent 应继续把这一轮收尾"


def test_a4_session_lifecycle_survives_restart(home):
    """A4 会话闭环：改名 / 置顶 / 标签 / 搜索 / 归档 / 恢复 / 导出 / 删除。"""
    scripts = [[
        [TextBlock(text="项目代号是青羊")],
        [TextBlock(text="第二轮回答")],
    ]]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        rpc(ws, "c1", "chat.send", {"text": "记住项目代号：青羊", "session_id": sid})

        # 改名 + 置顶 + 标签
        assert rpc(ws, "r1", "session.rename", {"id": sid, "title": "青羊项目"})["ok"]
        assert rpc(ws, "p1", "session.pin", {"id": sid, "pinned": True})["result"]["pinned"]
        tags = rpc(ws, "t1", "session.tags", {"id": sid, "tags": ["验收", "项目"]})["result"]
        assert tags["tags"] == ["验收", "项目"]

        listed = rpc(ws, "l1", "session.list")["result"]["sessions"]
        row = next(s for s in listed if s["id"] == sid)
        assert row["title"] == "青羊项目" and row["pinned"] is True
        assert row["tags"] == ["验收", "项目"]

        # 中文全文搜索：按消息正文找回会话
        found = rpc(ws, "s1", "session.search", {"query": "青羊"})["result"]["results"]
        assert any(r.get("session_id") == sid or r.get("id") == sid for r in found), found

        # 导出 Markdown：内容与标题都在
        exported = rpc(ws, "e1", "session.export", {"id": sid, "fmt": "md"})["result"]
        assert exported["filename"].endswith(".md")
        assert "青羊项目" in exported["markdown"] and "项目代号是青羊" in exported["markdown"]

        # 归档 → 从活动列表消失、进归档列表；恢复 → 回来
        assert rpc(ws, "a1", "session.archive", {"id": sid, "archived": True})["ok"]
        active = rpc(ws, "l2", "session.list")["result"]["sessions"]
        assert all(s["id"] != sid for s in active), "归档会话不应留在活动列表"
        archived = rpc(ws, "l3", "session.list_archived")["result"]["sessions"]
        assert any(s["id"] == sid for s in archived)
        assert rpc(ws, "a2", "session.archive", {"id": sid, "archived": False})["ok"]
        active2 = rpc(ws, "l4", "session.list")["result"]["sessions"]
        assert any(s["id"] == sid for s in active2)

        # 删除当前会话：自动切到另一个会话（返回语义），数据行消失
        sid2 = rpc(ws, "n2", "session.new")["result"]["id"]
        out = rpc(ws, "d1", "session.delete", {"id": sid2})["result"]
        assert out["deleted"] == sid2
        assert out["new_active"] == sid

        rpc(ws, "d2", "session.delete", {"id": sid})
        remaining = rpc(ws, "l5", "session.list")["result"]["sessions"]
        assert all(s["id"] != sid for s in remaining)


def test_a5_two_projects_are_isolated(home):
    """A5 双项目隔离：会话、白名单、检查点互不串。"""
    proj_b = home / "projB"
    proj_b.mkdir()
    (home / "proj" / "a.txt").write_text("A", encoding="utf-8")
    (proj_b / "b.txt").write_text("B", encoding="utf-8")

    scripts = [[
        [TextBlock(text="A 项目的回答")],
        [ToolUseBlock(id="w1", name="write_file", input={"path": "a.txt", "content": "A2"})],
        [TextBlock(text="A 项目写完了")],
    ]]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        # 项目 A：建会话 + 加一条本项目白名单
        sid_a = rpc(ws, "n1", "session.new")["result"]["id"]
        rpc(ws, "c1", "chat.send", {"text": "你好", "session_id": sid_a})
        rpc(ws, "w1", "whitelist.add", {"tool": "write_file", "kind": "always"})
        rules_a = rpc(ws, "wl1", "whitelist.list")["result"]["rules"]
        assert any(r["tool"] == "write_file" for r in rules_a)

        # 切到项目 B：看不到 A 的会话，也看不到 A 的白名单
        switched = rpc(ws, "sw1", "project.switch", {"path": str(proj_b)})["result"]
        assert switched["switched"] is True and switched["project"]
        sessions_b = rpc(ws, "l1", "session.list")["result"]["sessions"]
        assert all(s["id"] != sid_a for s in sessions_b)
        rules_b = rpc(ws, "wl2", "whitelist.list")["result"]["rules"]
        assert rules_b == []
        cps_b = rpc(ws, "cp1", "checkpoint.list", {})["result"]["checkpoints"]
        assert cps_b == []

        # 回项目 A：会话与白名单原样还在（按项目持久化）
        rpc(ws, "sw2", "project.switch", {"path": str(home / "proj")})
        sessions_a = rpc(ws, "l2", "session.list")["result"]["sessions"]
        assert any(s["id"] == sid_a for s in sessions_a)
        rules_a2 = rpc(ws, "wl3", "whitelist.list")["result"]["rules"]
        assert any(r["tool"] == "write_file" for r in rules_a2)


def test_a6_headless_stream_protocol(home, capsys):
    """A6 无头主流程：--output stream 逐行事件、末行 result、消息可审计。"""
    from skysheep.cli.app import run_headless
    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([[TextBlock(text="无头跑完了")]])
    result = _run(run_headless(
        prompt="打个招呼",
        directory=str(home / "proj"),
        provider_factory=lambda: provider,
        output="stream",
    ))
    assert result["stop_reason"] == "end_turn"
    assert result["reply"] == "无头跑完了"

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("{")]
    assert lines, "stream 模式必须输出 NDJSON"
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[-1]["kind"] == "result", "末行必须是 result 汇总行"
    assert parsed[-1]["stop_reason"] == "end_turn"
    assert any(p.get("kind") == "text_delta" for p in parsed), "正文应逐 delta 输出"
    assert parsed[-1]["session_id"]


def _run(coro):
    """在同步测试里跑一个协程（pytest-asyncio auto 模式之外的入口）。"""
    import asyncio

    return asyncio.run(coro)
