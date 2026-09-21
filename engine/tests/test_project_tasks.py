"""项目任务清单：与项目绑定的手动任务（增删改查 + 删项目级联清空）。"""

from __future__ import annotations

import sqlite3

from test_server import make_client, recv_until  # home fixture 在 conftest.py


def task_rows_of_project(home, pid):
    con = sqlite3.connect(home / "home" / "skysheep.db")
    try:
        return con.execute(
            "SELECT id FROM project_tasks WHERE project_id = ?", (pid,)
        ).fetchall()
    finally:
        con.close()


def test_project_task_crud_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 加两条（不传 project_id = 当前项目）
        ws.send_json({"id": "a1", "method": "project.task_add",
                      "params": {"title": "梳理目录结构"}})
        assert recv_until(ws, "a1")["ok"]
        ws.send_json({"id": "a2", "method": "project.task_add",
                      "params": {"title": "修登录报错", "detail": "先复现再修"}})
        t2 = recv_until(ws, "a2")["result"]["task"]
        assert t2["detail"] == "先复现再修"

        ws.send_json({"id": "l1", "method": "project.task_list", "params": {}})
        tasks = recv_until(ws, "l1")["result"]["tasks"]
        assert [t["title"] for t in tasks] == ["梳理目录结构", "修登录报错"]

        # 勾掉第一条 → 完成的垫底
        ws.send_json({"id": "u1", "method": "project.task_update",
                      "params": {"id": tasks[0]["id"], "done": True}})
        assert recv_until(ws, "u1")["result"]["task"]["done"] is True
        ws.send_json({"id": "l2", "method": "project.task_list", "params": {}})
        tasks = recv_until(ws, "l2")["result"]["tasks"]
        assert tasks[-1]["title"] == "梳理目录结构" and tasks[-1]["done"] is True

        # 编辑标题；删除另一条
        ws.send_json({"id": "u2", "method": "project.task_update",
                      "params": {"id": tasks[0]["id"], "title": "修登录报错（复现步骤已写）"}})
        assert "复现步骤" in recv_until(ws, "u2")["result"]["task"]["title"]
        ws.send_json({"id": "d1", "method": "project.task_delete",
                      "params": {"id": tasks[0]["id"]}})
        assert recv_until(ws, "d1")["result"]["removed"] == tasks[0]["id"]

        ws.send_json({"id": "l3", "method": "project.task_list", "params": {}})
        assert len(recv_until(ws, "l3")["result"]["tasks"]) == 1

        # 空标题拒绝
        ws.send_json({"id": "a3", "method": "project.task_add", "params": {"title": "  "}})
        assert not recv_until(ws, "a3")["ok"]


def test_deleting_project_cascades_its_tasks(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 当前项目先放一条任务
        ws.send_json({"id": "a1", "method": "project.task_add",
                      "params": {"title": "当前项目的任务"}})
        cur_task = recv_until(ws, "a1")["result"]["task"]
        cur_pid = cur_task["project_id"]

        # 建第二个项目并给它放两条任务
        proj2 = home / "proj2"
        proj2.mkdir()
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]
        for i in ("1", "2"):
            ws.send_json({"id": f"a{i}", "method": "project.task_add",
                          "params": {"title": f"proj2 的任务 {i}"}})
            assert recv_until(ws, f"a{i}")["ok"]

        # 切回当前项目，删掉 proj2
        ws.send_json({"id": "sw2", "method": "project.switch",
                      "params": {"path": str(home / "proj")}})
        assert recv_until(ws, "sw2")["ok"]
        ws.send_json({"id": "pl", "method": "project.list", "params": {}})
        proj2_id = next(
            p["id"] for p in recv_until(ws, "pl")["result"]["projects"] if not p["is_current"]
        )
        ws.send_json({"id": "del", "method": "project.delete", "params": {"id": proj2_id}})
        assert recv_until(ws, "del")["ok"]

        # proj2 的任务被级联清空；当前项目的任务原地不动
        assert task_rows_of_project(home, proj2_id) == []
        rows = task_rows_of_project(home, cur_pid)
        assert len(rows) == 1


def test_new_task_chat_creates_projectless_session(home):
    """侧栏「任务」分组的 ＋：建一个不绑定任何文件夹的会话。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "session.new_task", "params": {}})
        r = recv_until(ws, "t1")
        assert r["ok"] and r["result"]["id"]
        con = sqlite3.connect(home / "home" / "skysheep.db")
        try:
            row = con.execute(
                "SELECT project_id FROM sessions WHERE id = ?", (r["result"]["id"],)
            ).fetchone()
        finally:
            con.close()
        assert row is not None and row[0] is None


def test_projectless_session_operations_work(home):
    """快聊会话（project_id IS NULL）要能像普通会话一样操作。

    回归：归属校验只按当前项目查（get_session_for_project(id, current_pid)），
    而快聊的 project_id 是 NULL，永远匹配不上——列表里看得见、点开与删除
    全报「session not found」。实际表现是侧栏「快聊」分组 ＋ 建出的对话完全
    用不了。
    """
    from skysheep.messages import TextBlock

    script = [[TextBlock(text="ok")], [TextBlock(text="ok2")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "session.new_task", "params": {}})
        sid = recv_until(ws, "t1")["result"]["id"]

        # 打开标签（resume）、发消息、改元数据、导出、删除：一条都不能报 not found
        for mid, method, params in [
            ("r1", "session.resume", {"id": sid}),
            ("c1", "chat.send", {"text": "你好", "session_id": sid}),
            ("rn", "session.rename", {"id": sid, "title": "改名后的快聊"}),
            ("pn", "session.pin", {"id": sid, "pinned": True}),
            ("tg", "session.tags", {"id": sid, "tags": ["快聊标签"]}),
            ("ac", "session.activate", {"id": sid}),
            ("ex", "session.export", {"id": sid, "fmt": "md"}),
        ]:
            ws.send_json({"id": mid, "method": method, "params": params})
            frame = recv_until(ws, mid)
            assert frame["ok"], f"{method}: {frame.get('error')}"

        ws.send_json({"id": "d1", "method": "session.delete", "params": {"id": sid}})
        assert recv_until(ws, "d1")["ok"]

        # 库里确实没了
        con = sqlite3.connect(home / "home" / "skysheep.db")
        try:
            assert con.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is None
        finally:
            con.close()


def test_session_moved_to_quick_stays_usable(home):
    """移入快聊的会话同样不能再变成「删也删不掉」的僵尸。"""
    from skysheep.messages import TextBlock

    script = [[TextBlock(text="ok")], [TextBlock(text="再来一句")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "chat.send", "params": {"text": "hi"}})
        sid = recv_until(ws, "t1")["result"]["session_id"]
        ws.send_json({"id": "m1", "method": "session.move",
                      "params": {"id": sid, "project_id": None}})
        assert recv_until(ws, "m1")["ok"]

        ws.send_json({"id": "c1", "method": "chat.send",
                      "params": {"text": "移进快聊后再聊", "session_id": sid}})
        assert recv_until(ws, "c1")["ok"]
        ws.send_json({"id": "d1", "method": "session.delete", "params": {"id": sid}})
        assert recv_until(ws, "d1")["ok"]


def test_quick_session_archive_is_recoverable(home):
    """快聊会话归档后能在归档弹窗里找回：它不属于任何项目，若弹窗漏掉就永久消失。"""
    from skysheep.messages import TextBlock

    with make_client(home, [[TextBlock(text="ok")]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "session.new_task", "params": {}})
        sid = recv_until(ws, "t1")["result"]["id"]
        ws.send_json({"id": "ar", "method": "session.archive",
                      "params": {"id": sid, "archived": True}})
        assert recv_until(ws, "ar")["ok"]

        # 角标与列表同一口径：角标不出现，入口都点不开
        ws.send_json({"id": "sl", "method": "session.list", "params": {"all_projects": 1}})
        assert recv_until(ws, "sl")["result"]["archived_count"] >= 1
        ws.send_json({"id": "la", "method": "session.list_archived", "params": {}})
        assert sid in {s["id"] for s in recv_until(ws, "la")["result"]["sessions"]}

        # 能恢复回去，也能直接删掉
        ws.send_json({"id": "ur", "method": "session.archive",
                      "params": {"id": sid, "archived": False}})
        assert recv_until(ws, "ur")["ok"]
        ws.send_json({"id": "d1", "method": "session.delete", "params": {"id": sid}})
        assert recv_until(ws, "d1")["ok"]


def test_archive_modal_keeps_project_isolation(home):
    """归档弹窗带上快聊，但绝不放别的项目的会话进来（安全边界不被稀释）。"""
    from skysheep.messages import TextBlock

    (home / "proj2").mkdir()
    script = [[TextBlock(text="a")], [TextBlock(text="b")], [TextBlock(text="c")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "session.new_task", "params": {}})
        quick = recv_until(ws, "t1")["result"]["id"]
        ws.send_json({"id": "t2", "method": "chat.send", "params": {"text": "A 的会话"}})
        sid_a = recv_until(ws, "t2")["result"]["session_id"]

        # 项目 B 的会话归档后，在项目 A 的归档弹窗里不该出现
        ws.send_json({"id": "sw", "method": "project.switch",
                      "params": {"path": str(home / "proj2")}})
        assert recv_until(ws, "sw")["ok"]
        ws.send_json({"id": "t3", "method": "chat.send", "params": {"text": "B 的会话"}})
        sid_b = recv_until(ws, "t3")["result"]["session_id"]
        ws.send_json({"id": "ab", "method": "session.archive",
                      "params": {"id": sid_b, "archived": True}})
        assert recv_until(ws, "ab")["ok"]
        ws.send_json({"id": "sw2", "method": "project.switch",
                      "params": {"path": str(home / "proj")}})
        assert recv_until(ws, "sw2")["ok"]

        for mid, tid in (("aa", sid_a), ("aq", quick)):
            ws.send_json({"id": mid, "method": "session.archive",
                          "params": {"id": tid, "archived": True}})
            assert recv_until(ws, mid)["ok"]

        ws.send_json({"id": "la", "method": "session.list_archived", "params": {}})
        ids = {s["id"] for s in recv_until(ws, "la")["result"]["sessions"]}
        assert sid_a in ids and quick in ids
        assert sid_b not in ids
