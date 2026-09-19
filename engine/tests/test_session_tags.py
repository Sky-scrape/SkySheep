"""会话标签与分组：sessions.tags 列、存储层归一化、WS 方法与列表输出。"""

from __future__ import annotations

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.session.store import SessionStore


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "s.db").connect()
    yield s
    await s.close()


# ---------------------------------------------------------------- 存储层


async def test_set_tags_roundtrip(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    value = await store.set_tags(sid, ["工作", "待办"])
    assert value == "工作,待办"
    sess = await store.get_session(sid)
    assert sess.tags == "工作,待办"


async def test_set_tags_normalizes(store):
    """去空白、去重、保序；中文逗号也当分隔符。"""
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    value = await store.set_tags(sid, [" 工作 ", "工作", "待办,重要", ""])
    assert value == "工作,待办,重要"


async def test_set_tags_accepts_comma_string(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    assert await store.set_tags(sid, "a，b, c") == "a,b,c"


async def test_set_tags_limits_count(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    value = await store.set_tags(sid, [f"t{i}" for i in range(30)])
    assert len(value.split(",")) == 12


async def test_set_tags_truncates_long_tag(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    value = await store.set_tags(sid, ["x" * 100])
    assert len(value) == 24


async def test_set_tags_empty_clears(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    await store.set_tags(sid, ["a"])
    assert await store.set_tags(sid, []) == ""
    assert (await store.get_session(sid)).tags == ""


async def test_list_all_tags_counts(store):
    pid = (await store.get_or_create_project("/proj")).id
    s1 = (await store.create_session(pid)).id
    s2 = (await store.create_session(pid)).id
    s3 = (await store.create_session(pid)).id
    await store.set_tags(s1, ["工作", "重要"])
    await store.set_tags(s2, ["工作"])
    await store.set_tags(s3, [])
    tags = await store.list_all_tags(pid)
    by = {t["tag"]: t["count"] for t in tags}
    assert by == {"工作": 2, "重要": 1}
    # 会话数多的排前面
    assert tags[0]["tag"] == "工作"


async def test_list_all_tags_scoped_to_project(store):
    p1 = (await store.get_or_create_project("/p1")).id
    p2 = (await store.get_or_create_project("/p2")).id
    a = (await store.create_session(p1)).id
    b = (await store.create_session(p2)).id
    await store.set_tags(a, ["甲"])
    await store.set_tags(b, ["乙"])
    assert [t["tag"] for t in await store.list_all_tags(p1)] == ["甲"]
    assert [t["tag"] for t in await store.list_all_tags(p2)] == ["乙"]


async def test_list_sessions_includes_tags(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    await store.set_tags(sid, ["项目A"])
    sessions = await store.list_sessions(pid)
    assert sessions[0].tags == "项目A"


async def test_tags_survive_reconnect(tmp_path):
    """标签是持久化字段，重开库仍在。"""
    path = tmp_path / "t.db"
    s1 = await SessionStore(path).connect()
    pid = (await s1.get_or_create_project("/proj")).id
    sid = (await s1.create_session(pid)).id
    await s1.set_tags(sid, ["持久"])
    await s1.close()

    s2 = await SessionStore(path).connect()
    try:
        assert (await s2.get_session(sid)).tags == "持久"
    finally:
        await s2.close()


# ---------------------------------------------------------------- WS 层


def test_session_tags_ws_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]
        ws.send_json({"id": "t1", "method": "session.tags", "params": {"id": sid, "tags": ["工作", "周报"]}})
        frame = recv_until(ws, "t1")
        assert frame["ok"], frame
        assert frame["result"]["tags"] == ["工作", "周报"]
        assert {t["tag"] for t in frame["result"]["all_tags"]} == {"工作", "周报"}

        ws.send_json({"id": "l1", "method": "session.list"})
        sessions = recv_until(ws, "l1")["result"]["sessions"]
        assert sessions[0]["tags"] == ["工作", "周报"]


def test_session_tags_rejects_unknown_session(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t2", "method": "session.tags", "params": {"id": "nope", "tags": ["x"]}})
        frame = recv_until(ws, "t2")
    assert frame["ok"] is False
    assert "session not found" in frame["error"]


def test_session_tags_clear(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n2", "method": "session.new"})
        sid = recv_until(ws, "n2")["result"]["id"]
        ws.send_json({"id": "t3", "method": "session.tags", "params": {"id": sid, "tags": ["x"]}})
        recv_until(ws, "t3")
        ws.send_json({"id": "t4", "method": "session.tags", "params": {"id": sid, "tags": []}})
        assert recv_until(ws, "t4")["result"]["tags"] == []


def test_session_tags_list_ws(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n3", "method": "session.new"})
        sid = recv_until(ws, "n3")["result"]["id"]
        ws.send_json({"id": "t5", "method": "session.tags", "params": {"id": sid, "tags": ["汇总"]}})
        recv_until(ws, "t5")
        ws.send_json({"id": "l2", "method": "session.tags_list"})
        frame = recv_until(ws, "l2")
    assert frame["result"]["tags"] == [{"tag": "汇总", "count": 1}]


def test_session_list_tags_default_empty(home):
    """没打过标签的会话返回空数组（前端据此走「不分组」路径）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n4", "method": "session.new"})
        recv_until(ws, "n4")
        ws.send_json({"id": "l3", "method": "session.list"})
        sessions = recv_until(ws, "l3")["result"]["sessions"]
    assert sessions[0]["tags"] == []
