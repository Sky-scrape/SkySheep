"""C 组 · 会话、上下文与回滚验收。

覆盖：压缩后原文可回溯、大会话分页、检查点保留上限与重启后回滚、
检查点归属、会话库备份恢复。
"""

from __future__ import annotations

import sqlite3

import pytest
from _helpers import first, make_client, rpc, run_turn

from skysheep.config import db_path
from skysheep.messages import TextBlock, ToolUseBlock

pytestmark = pytest.mark.acceptance


def test_c1_compaction_keeps_original_recoverable(home):
    """C1 上下文压缩：摘要替换旧历史，但原文落盘 + 仍能从库里召回。"""
    turns = [[TextBlock(text=f"第{i}轮回答")] for i in range(6)]
    turns.append([TextBlock(text="摘要：之前讨论过订单服务")])  # 压缩用的摘要调用
    scripts = [turns]

    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        for i, text in enumerate(
            [
                "关键路径是 E:\\data\\orders.py",
                "第二个问题",
                "第三个问题",
                "第四个问题",
                "第五个问题",
                "第六个问题",
            ]
        ):
            assert rpc(ws, f"c{i}", "chat.send", {"text": text, "session_id": sid})["ok"]

        out = rpc(ws, "cp", "chat.compact")["result"]
        assert out["compacted"] is True, out
        assert out["after"] < out["before"], "压缩后消息数必须变少"

    # 原文落盘：transcripts 下有一份含关键路径的全文
    files = list((home / "proj" / ".skysheep" / "transcripts").glob("*.md"))
    assert files, "压缩必须把原文落盘"
    assert "orders.py" in files[0].read_text(encoding="utf-8")

    # 会话库里原文仍在（摘要丢了细节也能回溯）
    db = sqlite3.connect(db_path())
    try:
        rows = db.execute("SELECT content FROM messages WHERE content LIKE '%orders.py%'").fetchall()
    finally:
        db.close()
    assert rows, "被压缩的原文必须仍可从会话库召回"


def test_c2_long_history_pages_without_overlap(home):
    """C2 大会话分页：首屏只给最近 200 条，向前翻页不重叠、不遗漏。"""
    turns = [[TextBlock(text=f"回答 {i}")] for i in range(151)]
    with make_client(home, [turns]) as client, client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        for i in range(151):
            assert rpc(ws, f"c{i}", "chat.send", {"text": f"问题 {i}", "session_id": sid})["ok"]

        peek = rpc(ws, "p1", "session.peek_messages", {"id": sid})["result"]
        assert peek["messages_total"] == 302
        assert len(peek["messages"]) == 200
        assert peek["messages_offset"] == 102, "首屏应只给最近 200 条"

        oldest_seq = peek["messages"][0]["seq"]
        page = rpc(ws, "p2", "messages.earlier",
                   {"session_id": sid, "before_seq": oldest_seq, "limit": 100})["result"]
        assert len(page["messages"]) == 100
        assert all(m["seq"] < oldest_seq for m in page["messages"]), "翻页不能重叠"
        assert page["has_more"] is True

        seqs = [m["seq"] for m in page["messages"]]
        assert seqs == sorted(seqs), "返回页必须升序"

        page2 = rpc(ws, "p3", "messages.earlier",
                    {"session_id": sid, "before_seq": seqs[0], "limit": 100})["result"]
        assert len(page2["messages"]) == 2, "最旧一页只剩 2 条"
        assert page2["has_more"] is False
        assert page2["messages_offset"] == 0
        assert not ({m["seq"] for m in page2["messages"]} & set(seqs)), "两页不能有重叠"

        all_seqs = [m["seq"] for m in peek["messages"]] + seqs + [
            m["seq"] for m in page2["messages"]
        ]
        assert len(all_seqs) == len(set(all_seqs)) == 302, "三页合起来应覆盖全部消息且不重复"


def test_c3_checkpoint_cap_and_restart_restore(home):
    """C3 检查点：每会话最多 50 条；重启后仍能撤销最后一轮。"""
    proj = home / "proj"
    turns = []
    for i in range(55):
        turns.append([ToolUseBlock(
            id=f"w{i}", name="write_file",
            input={"path": f"f{i}.txt", "content": f"v{i}"},
        )])
        turns.append([TextBlock(text=f"第{i}次写完")])

    with make_client(home, [turns]) as client, client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new")["result"]["id"]
        rpc(ws, "m", "permission.set_mode", {"mode": "accept_edits"})
        for i in range(55):
            assert rpc(ws, f"c{i}", "chat.send",
                       {"text": f"写 f{i}", "session_id": sid})["ok"]
        rows = rpc(ws, "cp", "checkpoint.list", {"session_id": sid})["result"]["checkpoints"]
        assert len(rows) == 50, f"每会话应只保留最近 50 条，实际 {len(rows)}"
        newest_id = rows[-1]["id"]  # 列表按时间升序，最后一条才是本轮
        target = proj / "f54.txt"
        assert target.exists()

    # 重启：新进程读同一份磁盘库，检查点还在，仍能撤销
    with make_client(home, []) as client2, client2.websocket_connect("/ws") as ws2:
        rpc(ws2, "r", "session.resume", {"id": sid})
        rows2 = rpc(ws2, "cp", "checkpoint.list", {"session_id": sid})["result"]["checkpoints"]
        assert any(r["id"] == newest_id for r in rows2), "检查点应随磁盘持久化"
        out = rpc(ws2, "cr", "checkpoint.restore",
                  {"id": newest_id, "session_id": sid})["result"]
        assert out["restored"] == newest_id
        assert not target.exists(), "重启后撤销仍应真的删掉文件"


def test_c4_checkpoint_ownership_is_enforced(home):
    """C4 检查点归属：A 的检查点不能用 B 的会话身份恢复/查看。"""
    turns = [
        [ToolUseBlock(id="w", name="write_file", input={"path": "a.txt", "content": "A"})],
        [TextBlock(text="写好了")],
    ]
    with make_client(home, [turns]) as client, client.websocket_connect("/ws") as ws:
        sid_a = rpc(ws, "n1", "session.new")["result"]["id"]
        sid_b = rpc(ws, "n2", "session.new")["result"]["id"]
        rpc(ws, "s", "session.activate", {"id": sid_a})
        _, evs = run_turn(ws, "c1", {"text": "写 a.txt", "session_id": sid_a})
        assert first(evs, "permission_request")
        cp = rpc(ws, "cp", "checkpoint.list", {"session_id": sid_a})["result"]["checkpoints"][0]

        bad = rpc(ws, "x1", "checkpoint.restore", {"id": cp["id"], "session_id": sid_b})
        assert bad["ok"] is False and "不属于当前会话" in bad["error"]
        bad2 = rpc(ws, "x2", "checkpoint.diff", {"id": cp["id"], "session_id": sid_b})
        assert bad2["ok"] is False and "不属于当前会话" in bad2["error"]
        ok = rpc(ws, "x3", "checkpoint.diff", {"id": cp["id"], "session_id": sid_a})
        assert ok["ok"] is True


def test_c5_backup_and_restore_roundtrip(home):
    """C5 数据可救：手动备份 → 删会话 → 从备份恢复，消息回来。"""
    with make_client(home, [[TextBlock(text="备份前的回答")]]) as client, \
            client.websocket_connect("/ws") as ws:
        sid = rpc(ws, "n1", "session.new", {"title": "备份用例"})["result"]["id"]
        rpc(ws, "c1", "chat.send", {"text": "备份前的问题", "session_id": sid})
        backup = rpc(ws, "b1", "session.create_backup")["result"]
        assert backup["name"].endswith(".db")

        rpc(ws, "d1", "session.delete", {"id": sid})
        assert all(s["id"] != sid for s in rpc(ws, "l1", "session.list")["result"]["sessions"])

        restored = rpc(ws, "r1", "session.restore_backup", {"name": backup["name"]})
        assert restored["ok"], restored
        listed = rpc(ws, "l2", "session.list")["result"]["sessions"]
        assert any(s["title"] == "备份用例" for s in listed), "恢复后会话应回来"

        # 恢复动作本身会留一份「恢复前」安全备份
        backups = rpc(ws, "b2", "session.backups")["result"]["backups"]
        assert any(b.get("safety") for b in backups), "恢复前应自动留安全副本"
