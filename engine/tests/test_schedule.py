"""日程功能测试：存储层 CRUD / schedule_write 工具 / WS 分发 / 到点提醒扫描。"""

from __future__ import annotations

import time

import pytest

from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend
from skysheep.tools import ScheduleWriteTool, ToolContext, ToolError

# ---- 存储层 ----

async def test_schedule_store_crud(store):
    row = await store.add_schedule("小组会", time.time() + 3600, notes="带笔记本", remind=True)
    assert row["id"] and row["title"] == "小组会" and row["remind"] and not row["done"]

    rows = await store.list_schedules()
    assert [r["id"] for r in rows] == [row["id"]]

    upd = await store.update_schedule(row["id"], title="小组会（改）", start_at=time.time() + 7200)
    assert upd["title"] == "小组会（改）" and upd["reminded"] is False

    # 改时间会重置已提醒标记
    await store.mark_schedule_reminded(row["id"])
    assert (await store.get_schedule(row["id"]))["reminded"] is True
    upd2 = await store.update_schedule(row["id"], start_at=time.time() + 9000)
    assert upd2["reminded"] is False

    # 完成后默认列表不再返回，include_done 才返回
    await store.update_schedule(row["id"], done=True)
    assert await store.list_schedules() == []
    assert len(await store.list_schedules(include_done=True)) == 1

    assert await store.delete_schedule(row["id"]) is True
    assert await store.delete_schedule(row["id"]) is False


async def test_schedule_store_due_filter(store):
    now = time.time()
    past = await store.add_schedule("过期件", now - 100)
    future = await store.add_schedule("未来件", now + 10000)
    done = await store.add_schedule("已完成", now - 50)
    await store.update_schedule(done["id"], done=True)
    no_remind = await store.add_schedule("不提醒", now - 60, remind=False)

    due = await store.due_schedules()
    assert [r["id"] for r in due] == [past["id"]]  # 未来/完成/关提醒都不算到期

    await store.mark_schedule_reminded(past["id"])
    assert await store.due_schedules() == []
    assert (await store.get_schedule(future["id"]))["reminded"] is False
    assert (await store.get_schedule(no_remind["id"]))["reminded"] is False


async def test_schedule_remind_before(store):
    now = time.time()
    # 开始时间在 5 分钟后，提前 10 分钟提醒 → 现在就算到期
    early = await store.add_schedule("早会", now + 300, remind_before=10)
    due = await store.due_schedules(now=now)
    assert [r["id"] for r in due] == [early["id"]]
    assert early["remind_before"] == 10

    # 提前量改为 1 分钟后不再到期；负数按 0 处理
    await store.update_schedule(early["id"], remind_before=1)
    assert await store.due_schedules(now=now) == []
    assert (await store.get_schedule(early["id"]))["remind_before"] == 1
    await store.update_schedule(early["id"], remind_before=-5)
    assert (await store.get_schedule(early["id"]))["remind_before"] == 0

    # 到点提醒（remind_before=0）不受影响
    ontime = await store.add_schedule("准点", now + 60)
    assert await store.due_schedules(now=now) == []
    due2 = await store.due_schedules(now=now + 61)
    assert due2 and due2[0]["id"] == ontime["id"]


# ---- schedule_write 工具 ----

async def test_schedule_tool_roundtrip(store, tmp_path):
    tool = ScheduleWriteTool(store)
    ctx = ToolContext(working_dir=tmp_path)

    out = await tool.run(
        tool.args_model(action="add", title="交房租", start_at=time.time() + 86400),
        ctx,
    )
    assert "交房租" in out and "到点提醒" in out

    out = await tool.run(
        tool.args_model(
            action="add", title="早会", start_at=time.time() + 86400, remind_before=15
        ),
        ctx,
    )
    assert "提前15分钟提醒" in out

    rows = await store.list_schedules()
    assert len(rows) == 2
    sid = rows[0]["id"]

    out = await tool.run(tool.args_model(action="list"), ctx)
    assert f"#{sid}" in out

    out = await tool.run(
        tool.args_model(action="update", id=sid, done=True), ctx
    )
    assert "已完成" in out
    assert (await store.get_schedule(sid))["done"] is True

    out = await tool.run(tool.args_model(action="list", include_done=True), ctx)
    assert f"#{sid}" in out

    out = await tool.run(tool.args_model(action="delete", id=sid), ctx)
    assert "deleted" in out
    # 清掉另一条（早会）后列表应为空
    other = await store.list_schedules(include_done=True)
    for r in other:
        await store.delete_schedule(r["id"])
    assert await store.list_schedules(include_done=True) == []


async def test_schedule_tool_validation(store, tmp_path):
    tool = ScheduleWriteTool(store)
    ctx = ToolContext(working_dir=tmp_path)

    with pytest.raises(ToolError):
        await tool.run(tool.args_model(action="add", title="缺时间"), ctx)
    with pytest.raises(ToolError):
        await tool.run(tool.args_model(action="update"), ctx)  # 缺 id
    with pytest.raises(ToolError):
        await tool.run(tool.args_model(action="update", id=999), ctx)  # 不存在
    with pytest.raises(ToolError):
        await tool.run(tool.args_model(action="update", id=1), ctx)  # 无修改字段
    with pytest.raises(ToolError):
        await tool.run(tool.args_model(action="delete"), ctx)


# ---- WS 分发 + 提醒扫描 ----

def test_schedule_ws_roundtrip(home):
    from fastapi.testclient import TestClient

    from skysheep.server import create_app

    app = create_app(working_dir=home / "proj", provider_name="fake",
                     provider_factory=lambda: FakeProvider([]))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        while True:
            frame = ws.receive_json()
            if frame.get("id") == "b":
                tool_names = {t["name"] for t in frame["result"]["tools"]}
                break
        assert "schedule_write" in tool_names

        ws.send_json({"id": "a1", "method": "schedule.add",
                      "params": {"title": "牙医", "start_at": time.time() + 3600, "notes": "周二"}})
        frame = recv_frame(ws, "a1")
        assert frame["ok"], frame.get("error")
        sid = frame["result"]["id"]

        ws.send_json({"id": "l1", "method": "schedule.list", "params": {}})
        frame = recv_frame(ws, "l1")
        assert [r["title"] for r in frame["result"]] == ["牙医"]

        ws.send_json({"id": "u1", "method": "schedule.update",
                      "params": {"id": sid, "title": "牙医（改期）"}})
        assert recv_frame(ws, "u1")["result"]["title"] == "牙医（改期）"

        ws.send_json({"id": "d1", "method": "schedule.delete", "params": {"id": sid}})
        assert recv_frame(ws, "d1")["result"]["deleted"] == sid

        ws.send_json({"id": "l2", "method": "schedule.list", "params": {}})
        assert recv_frame(ws, "l2")["result"] == []


def recv_frame(ws, wanted_id):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            continue
        if frame.get("id") == wanted_id:
            return frame


async def test_reminder_pass_broadcasts_and_marks(home, store):
    b = ServerBackend(working_dir=home / "proj", store=store)
    b.store = store  # 跳过 setup()，直接注入
    seen: list[dict] = []

    async def fake_emit(ev: dict) -> None:
        seen.append(ev)

    b.ws_emitters.append(fake_emit)

    now = time.time()
    due = await store.add_schedule("开会", now - 30)
    await store.add_schedule("还没到点", now + 9999)

    await b._reminder_pass()
    assert len(seen) == 1
    assert seen[0]["kind"] == "schedule_reminder"
    assert seen[0]["title"] == "开会"
    assert (await store.get_schedule(due["id"]))["reminded"] is True

    # 二轮不再重复提醒
    await b._reminder_pass()
    assert len(seen) == 1

    # 无在线连接也不报错、照常标记
    b.ws_emitters.clear()
    await store.update_schedule(due["id"], start_at=now - 60)  # 改时间重置 reminded
    await b._reminder_pass()
    assert (await store.get_schedule(due["id"]))["reminded"] is True
