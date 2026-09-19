"""会话归档：archived 列、列表/最近会话/搜索排除、恢复、WS 方法。"""

from __future__ import annotations

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.messages import Message
from skysheep.session.store import SessionStore


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "s.db").connect()
    yield s
    await s.close()


# ---------------------------------------------------------------- 存储层


async def test_archive_roundtrip(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid, "要归档的会话")).id

    await store.set_archived(sid, True)
    assert (await store.get_session(sid)).archived == 1  # 记录本身还在
    assert all(s.id != sid for s in await store.list_sessions(pid))  # 侧栏列表隐藏
    archived = await store.list_archived_sessions(pid)
    assert [s.id for s in archived] == [sid]

    await store.set_archived(sid, False)
    assert (await store.get_session(sid)).archived == 0
    assert [s.id for s in await store.list_sessions(pid)] == [sid]
    assert await store.list_archived_sessions(pid) == []


async def test_archive_count_scoped_to_project(store):
    p1 = (await store.get_or_create_project("/p1")).id
    p2 = (await store.get_or_create_project("/p2")).id
    a = (await store.create_session(p1)).id
    b = (await store.create_session(p2)).id
    await store.set_archived(a, True)
    await store.set_archived(b, True)
    assert await store.count_archived_sessions(p1) == 1
    assert await store.count_archived_sessions(p2) == 1
    assert await store.count_archived_sessions(None) == 2


async def test_archived_not_latest_and_not_in_search(store):
    """启动「接着上次继续」不挑归档会话；搜索也眼不见。"""
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid, "唯一关键词会话")).id
    await store.append_message(sid, Message.user("独一无二关键词"))
    assert (await store.latest_session(pid)).id == sid
    hits = await store.search_messages(pid, "独一无二关键词")
    assert [h["session_id"] for h in hits] == [sid]

    await store.set_archived(sid, True)
    assert await store.latest_session(pid) is None  # 只有这一条且已归档 → 不续
    again = await store.create_session(pid, "新会话")
    await store.append_message(again.id, Message.user("新会话的正文内容"))
    assert (await store.latest_session(pid)).id == again.id  # 归档的不抢「最近」
    assert await store.search_messages(pid, "独一无二关键词") == []
    assert await store.search_messages(pid, "正文内容")  # 未归档照常命中


async def test_archive_survives_reconnect(tmp_path):
    path = tmp_path / "t.db"
    s1 = await SessionStore(path).connect()
    pid = (await s1.get_or_create_project("/proj")).id
    sid = (await s1.create_session(pid)).id
    await s1.set_archived(sid, True)
    await s1.close()

    s2 = await SessionStore(path).connect()
    try:
        assert (await s2.get_session(sid)).archived == 1
        assert await s2.list_sessions(pid) == []
        assert [s.id for s in await s2.list_archived_sessions(pid)] == [sid]
    finally:
        await s2.close()


# ---------------------------------------------------------------- WS 层


def test_archive_ws_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        ws.send_json({"id": "a1", "method": "session.archive",
                      "params": {"id": sid, "archived": True}})
        assert recv_until(ws, "a1")["result"]["archived"] is True

        ws.send_json({"id": "l1", "method": "session.list"})
        listed = recv_until(ws, "l1")["result"]
        assert listed["archived_count"] == 1
        assert listed["sessions"] == []

        ws.send_json({"id": "l2", "method": "session.list_archived"})
        arc = recv_until(ws, "l2")["result"]["sessions"]
        assert [s["id"] for s in arc] == [sid]
        assert arc[0]["archived"] is True

        ws.send_json({"id": "a2", "method": "session.archive",
                      "params": {"id": sid, "archived": False}})
        recv_until(ws, "a2")
        ws.send_json({"id": "l3", "method": "session.list"})
        listed = recv_until(ws, "l3")["result"]
        assert listed["archived_count"] == 0
        assert [s["id"] for s in listed["sessions"]] == [sid]


def test_archive_ws_rejects_unknown_session(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a3", "method": "session.archive",
                      "params": {"id": "nope", "archived": True}})
        frame = recv_until(ws, "a3")
    assert frame["ok"] is False
    assert "session not found" in frame["error"]
