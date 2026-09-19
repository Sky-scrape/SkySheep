"""跨会话搜索的 FTS5 索引：中文子串命中、索引同步、LIKE 兜底、旧库回填。

此前用 `m.content LIKE '%q%'` 全表扫描，无索引、无分词，数据量上去后明显变慢。
"""

from __future__ import annotations

import json

import pytest

from skysheep.messages import Message
from skysheep.session.store import SessionStore


async def _mk(store: SessionStore, project_id: int | None, texts: list[str]) -> str:
    s = await store.create_session(project_id)
    for t in texts:
        await store.append_message(s.id, Message.user(t))
    return s.id


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "s.db").connect()
    yield s
    await s.close()


async def test_fts_is_ready_on_fresh_db(store):
    assert store.fts_ready is True


async def test_search_finds_chinese_substring(store):
    """核心诉求：中文子串能命中（FTS5 默认分词器做不到这一点）。"""
    pid = (await store.get_or_create_project("/proj")).id
    sid = await _mk(store, pid, ["这是一段中文测试内容，包含关键词"])
    results = await store.search_messages(pid, "中文测试")
    assert len(results) == 1
    assert results[0]["session_id"] == sid
    assert "中文测试" in results[0]["snippet"]


async def test_search_finds_mid_string_keyword(store):
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["数据库连接超时的排查记录"])
    assert await store.search_messages(pid, "连接超时")
    assert await store.search_messages(pid, "排查记录")


async def test_search_is_case_insensitive(store):
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["Uses PostgreSQL and Redis"])
    assert await store.search_messages(pid, "postgresql")
    assert await store.search_messages(pid, "POSTGRESQL")


async def test_search_no_match_returns_empty(store):
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["完全无关的内容"])
    assert await store.search_messages(pid, "不存在的词") == []


async def test_search_empty_query_returns_empty(store):
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["some text"])
    assert await store.search_messages(pid, "") == []
    assert await store.search_messages(pid, "   ") == []


async def test_short_query_falls_back_to_like(store):
    """1–2 字符低于 trigram 的最小片段，直接走 LIKE，结果同样正确。"""
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["ab cd"])
    assert await store.search_messages(pid, "ab")
    assert await store.search_messages(pid, "c")


async def test_fts_special_chars_do_not_break_search(store):
    """查询里的 FTS 语法字符（" * - : ^）不该抛错，也不该变成语法。"""
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["关于 a-b*c 的说明", "引号 \"x\" 测试"])
    for q in ['a-b*c', '"x"', "a - b", "x:*", "a:b", "^start"]:
        await store.search_messages(pid, q)  # 不抛即通过


async def test_search_scoped_to_project(store):
    p1 = (await store.get_or_create_project("/p1")).id
    p2 = (await store.get_or_create_project("/p2")).id
    await _mk(store, p1, ["项目一的独特词汇 alpha"])
    await _mk(store, p2, ["项目二的独特词汇 beta"])
    r1 = await store.search_messages(p1, "独特词汇", scope="project")
    assert len(r1) == 1 and r1[0]["project_id"] == p1
    r_all = await store.search_messages(p1, "独特词汇", scope="all")
    assert len(r_all) == 2


async def test_search_limits_one_hit_per_session(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    for i in range(5):
        await store.append_message(sid, Message.user(f"重复关键词 第{i}次"))
    results = await store.search_messages(pid, "重复关键词")
    assert len(results) == 1 and results[0]["session_id"] == sid


async def test_search_respects_limit(store):
    pid = (await store.get_or_create_project("/proj")).id
    for i in range(5):
        await _mk(store, pid, [f"共享词汇 in session {i}"])
    results = await store.search_messages(pid, "共享词汇", limit=3)
    assert len(results) == 3


async def test_deleted_session_leaves_no_index(store):
    """删会话要同步清 FTS，否则搜索会返回已删内容。"""
    pid = (await store.get_or_create_project("/proj")).id
    sid = await _mk(store, pid, ["待删除的独特标记 zzz"])
    assert await store.search_messages(pid, "独特标记")
    await store.delete_session(sid)
    assert await store.search_messages(pid, "独特标记") == []


async def test_truncate_clears_index(store):
    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    await store.append_message(sid, Message.user("保留的内容 aaa"))
    await store.append_message(sid, Message.user("截断的内容 bbb"))
    assert await store.search_messages(pid, "截断的内容")
    await store.truncate_from(sid, 2, include_self=True)
    assert await store.search_messages(pid, "截断的内容") == []
    assert await store.search_messages(pid, "保留的内容")


async def test_delete_project_clears_index(store):
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["项目删除测试的标记 yyy"])
    assert await store.search_messages(pid, "项目删除测试", scope="all")
    await store.delete_project(pid)
    assert await store.search_messages(None, "项目删除测试", scope="all") == []


async def test_copy_messages_indexes_duplicate(store):
    """分叉复制出来的新会话也必须可搜（否则分叉后搜索会漏）。"""
    pid = (await store.get_or_create_project("/proj")).id
    src = (await store.create_session(pid)).id
    await store.append_message(src, Message.user("分叉测试标记 xxx"))
    dst = (await store.create_session(pid)).id
    n = await store.copy_messages_between(src, dst, upto_seq=1)
    assert n == 1
    results = await store.search_messages(pid, "分叉测试标记")
    ids = {r["session_id"] for r in results}
    assert ids == {src, dst}


async def test_like_fallback_when_fts_unavailable(store):
    """FTS 不可用时（旧库没虚表 / 未编 FTS5）搜索仍有结果。"""
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["兜底路径也要能找到 中文关键词"])
    store.fts_ready = False
    results = await store.search_messages(pid, "中文关键词")
    assert len(results) == 1
    assert "中文关键词" in results[0]["snippet"]


async def test_backfill_on_upgrade(tmp_path):
    """旧库升级：messages 里已有数据、FTS 表为空时启动要自动回填。"""
    path = tmp_path / "old.db"
    s1 = await SessionStore(path).connect()
    pid = (await s1.get_or_create_project("/proj")).id
    await _mk(s1, pid, ["升级前就存在的老数据 oldstuff"])
    # 模拟旧库：清掉索引（相当于这张表当时还不存在）
    await s1._db.execute("DELETE FROM messages_fts")
    await s1._db.commit()
    await s1.close()

    s2 = await SessionStore(path).connect()
    try:
        assert s2.fts_ready is True
        cur = await s2._db.execute("SELECT count(*) AS n FROM messages_fts")
        assert (await cur.fetchone())["n"] >= 1
        results = await s2.search_messages(pid, "oldstuff")
        assert len(results) == 1
    finally:
        await s2.close()


async def test_backfill_does_not_duplicate_on_reconnect(tmp_path):
    """再次打开不应重复回填（否则索引会膨胀）。"""
    path = tmp_path / "d.db"
    s1 = await SessionStore(path).connect()
    pid = (await s1.get_or_create_project("/proj")).id
    await _mk(s1, pid, ["去重检查 dedup"])
    await s1.close()

    s2 = await SessionStore(path).connect()
    try:
        cur = await s2._db.execute("SELECT count(*) AS n FROM messages_fts")
        n = (await cur.fetchone())["n"]
        assert n == 1
    finally:
        await s2.close()


async def test_backfill_handles_corrupt_row(tmp_path):
    """坏消息行不能让整个索引建不起来。"""
    path = tmp_path / "bad.db"
    s1 = await SessionStore(path).connect()
    pid = (await s1.get_or_create_project("/proj")).id
    sid = (await s1.create_session(pid)).id
    await s1._db.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (sid, 1, "user", "{not valid json", 0.0),
    )
    await s1._db.commit()
    await s1._db.execute("DELETE FROM messages_fts")
    await s1._db.commit()
    await s1.close()

    s2 = await SessionStore(path).connect()
    try:
        assert s2.fts_ready is True
        # 坏行也能进索引（退化成原始字符串），搜到它不算错误
        results = await s2.search_messages(pid, "not valid json")
        assert len(results) == 1
    finally:
        await s2.close()


async def test_snippet_shows_context(store):
    pid = (await store.get_or_create_project("/proj")).id
    long_text = "前置无关内容" * 10 + "目标关键词" + "后置无关内容" * 10
    await _mk(store, pid, [long_text])
    results = await store.search_messages(pid, "目标关键词")
    snip = results[0]["snippet"]
    assert "目标关键词" in snip
    # 超长内容要截成片段并标省略号
    assert snip.startswith("…")


async def test_search_result_fields(store):
    pid = (await store.get_or_create_project("/proj")).id
    await _mk(store, pid, ["字段检查字段检查"])
    r = (await store.search_messages(pid, "字段检查"))[0]
    assert set(r) == {
        "session_id", "title", "snippet", "updated_at",
        "project_id", "project_name", "project_path",
    }


async def test_quick_chat_scope_all(store):
    """项目为 NULL 的快聊会话在 scope=all 里也要能找到。"""
    quick = (await store.create_session(None)).id
    await store.append_message(quick, Message.user("快聊里的独特标记 quickmark"))
    pid = (await store.get_or_create_project("/proj")).id
    results = await store.search_messages(pid, "quickmark", scope="all")
    assert len(results) == 1
    assert results[0]["session_id"] == quick
    assert results[0]["project_name"] == "快聊"


async def test_fts_indexes_tool_results(store):
    """to_plain() 覆盖 tool_result 内容，索引也应包含它们。"""
    from skysheep.messages import ToolUseBlock

    pid = (await store.get_or_create_project("/proj")).id
    sid = (await store.create_session(pid)).id
    await store.append_message(
        sid,
        Message.assistant([ToolUseBlock(id="t1", name="read_file", input={"path": "a.py"})]),
    )
    await store.append_message(
        sid,
        Message.tool_result("t1", "工具输出里的独特标记 toolout"),
    )
    assert await store.search_messages(pid, "toolout")
    assert await store.search_messages(pid, "read_file")


def test_plain_text_helper_handles_json():
    """_plain_text 是静态方法，可直接验证其容错。"""
    raw = Message.user("hello world").model_dump_json()
    assert "hello world" in SessionStore._plain_text(raw)
    assert SessionStore._plain_text("{broken") == "{broken"
    assert json.dumps(Message.user("x").model_dump())  # sanity: 可序列化
