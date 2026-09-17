"""会话存储测试。"""

from __future__ import annotations

from skysheep.messages import Message, ToolUseBlock
from skysheep.session.store import SessionStore


async def test_project_roundtrip(store, tmp_path):
    root = str(tmp_path / "proj")
    p1 = await store.get_or_create_project(root)
    p2 = await store.get_or_create_project(root)  # 幂等
    assert p1.id == p2.id
    assert p1.name == (tmp_path / "proj").name


async def test_session_and_messages(store):
    p = await store.get_or_create_project("/tmp/demo-s")
    s = await store.create_session(p.id, title="t")
    got = await store.get_session(s.id)
    assert got is not None and got.title == "t"

    msgs = [
        Message.user("hello"),
        Message.assistant([ToolUseBlock(id="t1", name="list_dir", input={"path": "."})]),
        Message.tool_result("t1", "(empty)"),
        Message.user("next"),
    ]
    for m in msgs:
        await store.append_message(s.id, m)
    loaded = await store.load_messages(s.id)
    assert len(loaded) == 4
    assert loaded[0].text == "hello"
    assert loaded[1].tool_uses[0].name == "list_dir"
    assert loaded[2].content[0].tool_use_id == "t1"

    # 顺序稳定
    again = await store.load_messages(s.id)
    assert [m.id for m in again] == [m.id for m in loaded]

    sessions = await store.list_sessions(p.id)
    assert any(x.id == s.id for x in sessions)


async def test_rules(store):
    p = await store.get_or_create_project("/tmp/demo-r")
    await store.add_rule(p.id, "run_command", "prefix", "git ")
    rules = await store.list_rules(p.id)
    assert len(rules) == 1
    assert rules[0]["tool"] == "run_command"
    assert rules[0]["kind"] == "prefix"
    assert rules[0]["pattern"] == "git "
    assert isinstance(rules[0]["id"], int)
    # 删除规则
    await store.remove_rule(rules[0]["id"])
    assert await store.list_rules(p.id) == []


async def test_rules_order_and_clear(store):
    p = await store.get_or_create_project("/tmp/demo-clear")
    await store.add_rule(p.id, "run_command", "prefix", "git status")
    await store.add_rule(p.id, "write_file", "always")
    await store.add_rule(p.id, "write_file", "glob", "docs/*.md")
    rules = await store.list_rules(p.id)
    # 新规则在前（id 倒序），created_at 随字段返回
    assert [r["pattern"] for r in rules] == ["docs/*.md", "", "git status"]
    assert all(isinstance(r["created_at"], float) for r in rules)
    # 按 kind 清空：只删 glob
    assert await store.clear_rules(p.id, kind="glob") == 1
    assert len(await store.list_rules(p.id)) == 2
    # 全部清空
    assert await store.clear_rules(p.id) == 2
    assert await store.list_rules(p.id) == []


async def test_quick_chat_session_without_project(store):
    s = await store.create_session(None, title="quick")
    got = await store.get_session(s.id)
    assert got.project_id is None


async def test_rolling_backup_on_connect(tmp_path):
    """每次打开数据库前滚动备份，误删后可从 backups/ 恢复。"""
    import sqlite3
    import time

    db = tmp_path / "app.db"
    s1 = await SessionStore(db).connect()
    project = await s1.get_or_create_project("/tmp/backup-proj")
    sess = await s1.create_session(project.id, title="重要会话")
    await s1.append_message(sess.id, Message.user("这条消息不能被弄丢"))
    await s1.close()
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").glob("*.db"))

    time.sleep(1.1)  # 时间戳精确到秒，确保前进一秒产生新备份文件
    s2 = await SessionStore(db).connect()
    assert s2.backup_created, "再次打开应生成备份"
    con = sqlite3.connect(s2.backup_created)
    try:
        assert con.execute("select count(*) from messages").fetchone()[0] == 1
        assert con.execute("select title from sessions").fetchone()[0] == "重要会话"
    finally:
        con.close()
    await s2.close()


async def test_empty_session_helpers(store):
    """空会话统计与清理（保留当前会话与置顶会话）。"""
    p = await store.get_or_create_project("/tmp/empty-proj")
    with_msg = await store.create_session(p.id, title="有内容")
    await store.append_message(with_msg.id, Message.user("hi"))
    e1 = await store.create_session(p.id)
    e2 = await store.create_session(p.id)
    pinned_empty = await store.create_session(p.id)
    await store.set_pinned(pinned_empty.id, True)

    assert await store.count_empty_sessions(p.id) == 3  # e1/e2/置顶空会话
    removed = await store.delete_empty_sessions(p.id, keep_id=e1.id)
    assert removed == 1  # 只删掉 e2（e1 是当前会话、置顶会话、含内容的都保留）
    assert await store.get_session(e2.id) is None
    assert await store.get_session(e1.id) is not None
    assert await store.get_session(with_msg.id) is not None
    assert await store.get_session(pinned_empty.id) is not None


async def test_usage_today(store):
    """每日 token 用量：只累计今天（本地时区零点起），昨日不计入。"""
    import time

    await store.add_usage("s1", "prov", "m", in_tokens=100, out_tokens=20)
    await store.add_usage("s2", "prov", "m", in_tokens=30, out_tokens=0)
    assert await store.usage_today() == 150
    # 昨天的记录不计入
    await store._db.execute(
        "INSERT INTO usage_log (session_id, provider, model, ts, in_tokens, out_tokens)"
        " VALUES ('s1', 'prov', 'm', ?, 9999, 9999)",
        (time.time() - 86400 * 2,),
    )
    await store._db.commit()
    assert await store.usage_today() == 150
