"""项目删除测试：project.delete（连带会话/消息/白名单，不删磁盘文件夹）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from test_server import make_client, recv_until  # helpers（home fixture 在 conftest.py）

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider


def project_rows(home, pid):
    con = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        return con.execute("SELECT id FROM projects WHERE id = ?", (pid,)).fetchall()
    finally:
        con.close()


def session_rows_of_project(home, pid):
    con = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        return con.execute(
            "SELECT id FROM sessions WHERE project_id = ?", (pid,)
        ).fetchall()
    finally:
        con.close()


def rule_rows_of_project(home, pid):
    con = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        return con.execute(
            "SELECT id FROM whitelist_rules WHERE project_id = ?", (pid,)
        ).fetchall()
    finally:
        con.close()


def orphan_message_count(home):
    con = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        return con.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)"
        ).fetchone()[0]
    finally:
        con.close()


def test_delete_project_cascades_sessions_and_rules_keeps_disk(home):
    proj2 = home / "proj2"
    proj2.mkdir()
    (home / "proj" / "keep.txt").write_text("磁盘文件不应被删", encoding="utf-8")
    provider = FakeProvider([[TextBlock(text="你好")]])
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        # 在默认项目 proj 里产生一个会话（含消息）并置顶
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "在 proj 里聊聊"}})
        done = recv_until(ws, "c1")
        assert done["ok"]
        sid = done["result"]["session_id"]
        ws.send_json({"id": "pin", "method": "session.pin", "params": {"id": sid, "pinned": True}})
        assert recv_until(ws, "pin")["ok"]

        # 切到 proj2，proj 变成非当前
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]
        ws.send_json({"id": "pl", "method": "project.list", "params": {}})
        projects = recv_until(ws, "pl")["result"]["projects"]
        proj = next(p for p in projects if not p["is_current"])

        # 删除 proj
        ws.send_json({"id": "del", "method": "project.delete", "params": {"id": proj["id"]}})
        r = recv_until(ws, "del")
        assert r["ok"] and r["result"]["removed"] == proj["id"]

        # 列表里只剩 proj2
        ws.send_json({"id": "pl2", "method": "project.list", "params": {}})
        rest = recv_until(ws, "pl2")["result"]["projects"]
        assert [p["id"] for p in rest] == [p["id"] for p in rest if p["is_current"]]

        # 数据库级联：项目/会话/白名单全部清掉（置顶会话也不留）、无孤儿消息
        assert project_rows(home, proj["id"]) == []
        assert session_rows_of_project(home, proj["id"]) == []
        assert rule_rows_of_project(home, proj["id"]) == []
        assert orphan_message_count(home) == 0

        # 磁盘上的文件夹和文件不受影响
        assert (home / "proj").is_dir()
        assert (home / "proj" / "keep.txt").read_text(encoding="utf-8") == "磁盘文件不应被删"


def test_delete_current_project_switches_clears_and_unknown_id(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "pl", "method": "project.list", "params": {}})
        cur = next(p for p in recv_until(ws, "pl")["result"]["projects"] if p["is_current"])

        # 再加一个项目并切过去（project.switch 到新目录 = 添加项目），让它成为当前
        (home / "proj2").mkdir()
        ws.send_json({"id": "sw", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw")["ok"]

        # 删当前项目（proj2）：不重建记录，自动切到剩下的最近项目
        ws.send_json({"id": "pl2", "method": "project.list", "params": {}})
        proj2 = next(p for p in recv_until(ws, "pl2")["result"]["projects"] if p["is_current"])
        ws.send_json({"id": "del", "method": "project.delete", "params": {"id": proj2["id"]}})
        r = recv_until(ws, "del")
        assert r["ok"] and r["result"]["was_current"] is True
        assert Path(r["result"]["switched_to"]["path"]) == (home / "proj").resolve()

        # 级联：proj2 的项目记录删掉，磁盘文件夹不受影响
        assert project_rows(home, proj2["id"]) == []
        assert (home / "proj2").is_dir()

        # 删掉唯一剩下的当前项目：一个都不剩 → 进入无项目态（快聊），列表为空。
        # 无项目态持久：ui.json 记 active_project=0，重启后不自动接回
        ws.send_json({"id": "pl3", "method": "project.list", "params": {}})
        proj1 = next(p for p in recv_until(ws, "pl3")["result"]["projects"] if p["is_current"])
        assert proj1["id"] == cur["id"]
        ws.send_json({"id": "del2", "method": "project.delete", "params": {"id": proj1["id"]}})
        r2 = recv_until(ws, "del2")
        assert r2["ok"] and r2["result"]["was_current"] is True
        assert r2["result"]["switched_to"] is None
        ws.send_json({"id": "pl4", "method": "project.list", "params": {}})
        assert recv_until(ws, "pl4")["result"]["projects"] == []
        prefs = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
        assert prefs.get("active_project") == 0

        # 数据库级联：项目/会话/白名单全部清掉、无孤儿消息；磁盘文件不受影响
        assert project_rows(home, proj1["id"]) == []
        assert session_rows_of_project(home, proj1["id"]) == []
        assert rule_rows_of_project(home, proj1["id"]) == []
        assert orphan_message_count(home) == 0
        assert (home / "proj").is_dir()

        # 不存在的 id：直接报不存在
        ws.send_json({"id": "del3", "method": "project.delete", "params": {"id": 99999}})
        r3 = recv_until(ws, "del3")
        assert not r3["ok"] and "不存在" in r3["error"]
