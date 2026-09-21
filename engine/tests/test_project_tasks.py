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
