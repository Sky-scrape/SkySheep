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


async def test_schedule_store_span(store):
    """结束时间：不传 = 按点事件（end_at 为 0），传了就是一个时间段。"""
    now = time.time()
    point = await store.add_schedule("交房租", now + 3600)
    assert point["end_at"] == 0

    span = await store.add_schedule("午休", now + 7200, end_at=now + 10800)
    assert span["end_at"] == pytest.approx(now + 10800)

    # 部分更新：只改结束时间，开始时间不动
    upd = await store.update_schedule(span["id"], end_at=now + 12600)
    assert upd["end_at"] == pytest.approx(now + 12600)
    assert upd["start_at"] == pytest.approx(now + 7200)

    # 传 0 清除结束时间，回到按点事件
    cleared = await store.update_schedule(span["id"], end_at=0)
    assert cleared["end_at"] == 0

    # 非法值（负数）归一到 0，不写入负时间戳
    neg = await store.update_schedule(span["id"], end_at=-100)
    assert neg["end_at"] == 0


async def test_schedule_end_at_migration(tmp_path):
    """旧库升级：建表时没有 end_at 列的 schedules 表补上该列，旧数据不丢。"""
    import aiosqlite

    from skysheep.session import SessionStore

    db = tmp_path / "old.db"
    conn = await aiosqlite.connect(db)
    await conn.executescript(
        "CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " notes TEXT NOT NULL DEFAULT '', start_at REAL NOT NULL, remind INTEGER NOT NULL DEFAULT 1,"
        " remind_before INTEGER NOT NULL DEFAULT 0, reminded INTEGER NOT NULL DEFAULT 0,"
        " done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
    )
    old_ts = time.time() + 3600
    await conn.execute(
        "INSERT INTO schedules (title, notes, start_at, created_at, updated_at)"
        " VALUES ('旧日程', '', ?, ?, ?)",
        (old_ts, time.time(), time.time()),
    )
    await conn.commit()
    await conn.close()

    s = await SessionStore(db).connect()
    try:
        rows = await s.list_schedules()
        assert [r["title"] for r in rows] == ["旧日程"]
        assert rows[0]["end_at"] == 0  # 补列默认 0 = 按点事件
        # 新列可立即写入
        upd = await s.update_schedule(rows[0]["id"], end_at=old_ts + 1800)
        assert upd["end_at"] == pytest.approx(old_ts + 1800)
    finally:
        await s.close()


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

    # 时间段：add 带 end_at 后输出「起—止」；只写一个时刻时不拼区间
    base = time.time() + 90000
    out = await tool.run(
        tool.args_model(action="add", title="周会", start_at=base, end_at=base + 5400),
        ctx,
    )
    assert "周会" in out
    assert "-" in out.split("周会")[0]  # 日期与时刻拼成区间
    span_id = (await store.list_schedules())[-1]["id"]
    span_row = await store.get_schedule(span_id)
    assert span_row["end_at"] == pytest.approx(base + 5400)

    # 只改结束时间：不传 start_at 也不能被误判成倒置区间
    out = await tool.run(
        tool.args_model(action="update", id=span_id, end_at=base + 7200), ctx
    )
    assert "updated" in out
    assert (await store.get_schedule(span_id))["end_at"] == pytest.approx(base + 7200)

    # 传 0 清除结束时间，回到按点事件
    await tool.run(tool.args_model(action="update", id=span_id, end_at=0), ctx)
    assert (await store.get_schedule(span_id))["end_at"] == 0

    rows = await store.list_schedules()
    assert len(rows) == 3
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
    # 结束时间必须晚于开始时间；区间倒置一律拦下
    now = time.time()
    with pytest.raises(ToolError):
        await tool.run(
            tool.args_model(action="add", title="倒置", start_at=now, end_at=now - 60), ctx
        )
    with pytest.raises(ToolError):
        await tool.run(
            tool.args_model(action="add", title="同时刻", start_at=now, end_at=now), ctx
        )
    # 编辑时把已有日程的结束时间改到开始之前，同样要拦
    row = await store.add_schedule("正会", now + 3600, end_at=now + 5400)
    with pytest.raises(ToolError):
        await tool.run(
            tool.args_model(action="update", id=row["id"], end_at=now + 3000), ctx
        )


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
        assert frame["result"]["end_at"] == 0

        ws.send_json({"id": "l1", "method": "schedule.list", "params": {}})
        frame = recv_frame(ws, "l1")
        assert [r["title"] for r in frame["result"]] == ["牙医"]
        assert frame["result"][0]["end_at"] == 0

        # 时间段：带 end_at 新增 → 能读回；改回 0 即取消区间
        t0 = time.time() + 7200
        ws.send_json({"id": "a2", "method": "schedule.add",
                      "params": {"title": "午休", "start_at": t0, "end_at": t0 + 5400}})
        frame = recv_frame(ws, "a2")
        assert frame["ok"], frame.get("error")
        span_id = frame["result"]["id"]
        assert frame["result"]["end_at"] == pytest.approx(t0 + 5400)

        ws.send_json({"id": "u2", "method": "schedule.update",
                      "params": {"id": span_id, "end_at": 0}})
        assert recv_frame(ws, "u2")["result"]["end_at"] == 0
        ws.send_json({"id": "d2", "method": "schedule.delete", "params": {"id": span_id}})
        recv_frame(ws, "d2")

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
