"""定时任务：store CRUD/next_run 计算、WS 协议、headless 权限门控。"""

from __future__ import annotations

import time as _time

from skysheep.messages import TextBlock, ToolUseBlock

# ---- store：CRUD + next_run ----


async def test_cron_store_crud_and_next_run(store):
    project = await store.get_or_create_project("/tmp/cron-proj")
    t = await store.add_cron_task(project.id, "汇总", "看看 TODO", "interval",
                                  interval_minutes=30)
    assert t["id"] > 0 and t["interval_minutes"] == 30
    tasks = await store.list_cron_tasks(project.id)
    assert len(tasks) == 1 and tasks[0]["name"] == "汇总"

    # next_run：interval 基于 last_run_at
    row = {**t, "last_run_at": 1000.0}
    assert abs(store.compute_next_run(row) - 1000.0 - 1800) < 1

    # daily：今天已过点 → 明天
    lt = _time.localtime()
    row_d = {**t, "schedule_type": "daily",
             "time_of_day": f"{lt.tm_hour}:{lt.tm_min:02d}",
             "last_run_at": 0}
    next_d = store.compute_next_run(row_d, now=_time.time())
    # 构造 now 让今天的时刻已过，得到的一定是 > now 且 < now+24h
    assert next_d > _time.time() and next_d - _time.time() < 24 * 3600 + 60

    # weekly 也落在未来
    row_w = {**t, "schedule_type": "weekly", "time_of_day": "09:00", "weekday": 0}
    assert store.compute_next_run(row_w) > _time.time()

    # update / delete
    up = await store.update_cron_task(t["id"], enabled=0, allowed_tools=["read_file"])
    assert up["enabled"] is False and up["allowed_tools"] == ["read_file"]
    assert await store.delete_cron_task(t["id"]) is True
    assert await store.get_cron_task(t["id"]) is None


async def test_due_cron_only_enabled_and_scheduled(store):
    project = await store.get_or_create_project("/tmp/cron-proj2")
    t = await store.add_cron_task(project.id, "到点", "干活", "interval", interval_minutes=10)
    # 未排期（next_run_at=0）不算到期
    assert await store.due_cron_tasks() == []
    await store.update_cron_task(t["id"], next_run_at=_time.time() - 5)
    due = await store.due_cron_tasks()
    assert len(due) == 1 and due[0]["id"] == t["id"]
    await store.update_cron_task(t["id"], enabled=0)
    assert await store.due_cron_tasks() == []


# ---- WS：创建 / 立即运行 / headless 权限门控 ----


def test_cron_add_list_update_via_ws(home):
    from test_server import make_client, recv_until

    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "每日报告", "prompt": "汇总今天的进展",
            "schedule_type": "daily", "time_of_day": "09:00",
            "allowed_tools": ["read_file"],
        }})
        r = recv_until(ws, "ca")
        assert r["ok"], r.get("error")
        task = r["result"]
        assert task["name"] == "每日报告" and task["enabled"] is True
        assert task["next_run_at"] > 0, "创建后应立刻排期"

        ws.send_json({"id": "cl", "method": "cron.list", "params": {}})
        lst = recv_until(ws, "cl")["result"]["tasks"]
        assert len(lst) == 1 and lst[0]["allowed_tools"] == ["read_file"]

        # 非法频率拒绝
        ws.send_json({"id": "bad", "method": "cron.add", "params": {
            "name": "x", "prompt": "y", "schedule_type": "monthly"}})
        bad = recv_until(ws, "bad")
        assert not bad["ok"]

        ws.send_json({"id": "cu", "method": "cron.update", "params": {
            "id": task["id"], "enabled": False}})
        up = recv_until(ws, "cu")["result"]
        assert up["enabled"] is False and up["next_run_at"] == 0, "停用后不排期"


def test_cron_run_now_headless_gate(home):
    """无人值守门控：未预授权的 write_file 被自动拒绝；预授权后放行。"""
    from pathlib import Path

    from test_server import make_client, recv_until

    script = [
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "secret.txt", "content": "x"})],
        [TextBlock(text="结果一")],
        [ToolUseBlock(id="w2", name="write_file",
                      input={"path": "ok.txt", "content": "y"})],
        [TextBlock(text="结果二")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "无人值守", "prompt": "写个文件",
            "schedule_type": "interval", "interval_minutes": 10,
            "allowed_tools": [],
        }})
        task = recv_until(ws, "ca")["result"]

        # 运行 1：write_file 未预授权 → 拒绝，文件不产生
        ws.send_json({"id": "r1", "method": "cron.run_now", "params": {"id": task["id"]}})
        recv_until(ws, "r1")
        secret = Path(home) / "proj" / "secret.txt"
        assert not secret.exists(), "未预授权写入必须被 headless 门控拒绝"

        ws.send_json({"id": "gl", "method": "cron.list", "params": {}})
        lst = recv_until(ws, "gl")["result"]["tasks"]
        assert lst[0]["last_status"] == "ok"
        assert lst[0]["last_result"] == "结果一"
        assert lst[0]["next_run_at"] > _time.time() - 1, "运行后重算下次时间"

        # 预授权 write_file → 放行落盘
        ws.send_json({"id": "cu", "method": "cron.update", "params": {
            "id": task["id"], "allowed_tools": ["write_file"]}})
        recv_until(ws, "cu")
        ws.send_json({"id": "r2", "method": "cron.run_now", "params": {"id": task["id"]}})
        recv_until(ws, "r2")
        okf = Path(home) / "proj" / "ok.txt"
        assert okf.exists() and okf.read_text(encoding="utf-8") == "y"

        ws.send_json({"id": "gl2", "method": "cron.list", "params": {}})
        lst2 = recv_until(ws, "gl2")["result"]["tasks"]
        assert lst2[0]["last_result"] == "结果二"