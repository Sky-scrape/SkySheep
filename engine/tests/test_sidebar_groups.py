"""侧栏分组视图的后端支撑：按项目取会话（store）+ session.list 的 all_projects 口径。

分组视图（sidebar_view=grouped）要在一个列表里画「项目 → 会话」两级，
依赖两点：store 能按项目各取前 N 条；跨项目会话标题只给本机（与
session.search scope=all 同一安全口径，见 test_security_hardening）。
"""

from __future__ import annotations

import pytest
from test_server import make_client, recv_until

from skysheep.messages import Message
from skysheep.session.store import SessionStore


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "s.db").connect()
    yield s
    await s.close()


async def set_time(store: SessionStore, sid: str, t: float) -> None:
    """显式安排 updated_at：Windows 时钟精度粗，连发的会话时间戳可能同值，排序断言会抖。"""
    await store._db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (t, sid))
    await store._db.commit()


# ---------------------------------------------------------------- 存储层


async def test_list_sessions_by_project_groups_quick_chat_and_orders(store):
    p1 = (await store.get_or_create_project("/p1")).id
    p2 = (await store.get_or_create_project("/p2")).id
    old = await store.create_session(p1, "p1-旧")
    await store.append_message(old.id, Message.user("x"))
    new = await store.create_session(p1, "p1-新")
    await store.append_message(new.id, Message.user("x"))
    await set_time(store, old.id, 1000.0)
    await set_time(store, new.id, 2000.0)
    await store.set_pinned(old.id, True)  # 置顶的旧会话排到最前
    b1 = await store.create_session(p2, "p2-1")
    await store.append_message(b1.id, Message.user("x"))
    quick = await store.create_session(None, "快聊会话")  # project_id NULL = 快聊
    await store.append_message(quick.id, Message.user("x"))

    grouped = await store.list_sessions_by_project()
    assert [s.title for s in grouped[p1]] == ["p1-旧", "p1-新"]
    assert [s.title for s in grouped[p2]] == ["p2-1"]
    assert [s.title for s in grouped[None]] == ["快聊会话"]


async def test_list_sessions_by_project_cap_and_archive(store):
    p1 = (await store.get_or_create_project("/p1")).id
    p2 = (await store.get_or_create_project("/p2")).id
    made = []
    for i in range(7):
        s = await store.create_session(p1, f"会话{i}")
        await store.append_message(s.id, Message.user("x"))
        await set_time(store, s.id, 1000.0 + i)
        made.append(s)
    # 会话多的项目不能把会话少的项目挤出结果（各取各的前 N）
    only = await store.create_session(p2, "p2-唯一")
    await store.append_message(only.id, Message.user("x"))

    grouped = await store.list_sessions_by_project(limit_per_project=2)
    assert len(grouped[p1]) == 2
    assert grouped[p2] and grouped[p2][0].id == only.id
    # 留下的应是最近的 2 条（置顶/时间倒序）
    assert [s.id for s in grouped[p1]] == [made[-1].id, made[-2].id]

    await store.set_archived(made[-1].id, True)
    grouped2 = await store.list_sessions_by_project(limit_per_project=2)
    assert [s.id for s in grouped2[p1]] == [made[-2].id, made[-3].id]


# ---------------------------------------------------------------- WS 层


def test_session_list_all_projects_local_only(home, monkeypatch):
    """本机：all_projects 回全部项目的会话（各带 project_id 供前端分组）；
    默认口径不变：只回当前项目。远程：all_projects 关死，默认口径照常。"""
    created = []
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        created.append(recv_until(ws, "n1")["result"]["id"])
        proj2 = home / "proj2"
        proj2.mkdir()
        ws.send_json({"id": "sw", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw")["ok"]
        ws.send_json({"id": "n2", "method": "session.new"})
        created.append(recv_until(ws, "n2")["result"]["id"])

        ws.send_json({"id": "l1", "method": "session.list"})
        plain = recv_until(ws, "l1")["result"]
        assert [s["id"] for s in plain["sessions"]] == [created[1]]

        ws.send_json({"id": "l2", "method": "session.list", "params": {"all_projects": 1}})
        grouped = recv_until(ws, "l2")["result"]
        assert sorted(s["id"] for s in grouped["sessions"]) == sorted(created)
        assert len({s["project_id"] for s in grouped["sessions"]}) == 2

    from skysheep.server import app as server_app
    from skysheep.server.app import _client_is_local

    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "session.list", "params": {"all_projects": 1}})
        frame = recv_until(ws, "r1")
        assert frame["ok"] is False and "本机" in frame["error"]
        ws.send_json({"id": "r2", "method": "session.list"})
        assert recv_until(ws, "r2")["ok"]
    # 只恢复本机判定函数本身（同 test_security_hardening 的做法，不动环境变量）
    monkeypatch.setattr(server_app, "_client_is_local", _client_is_local)
