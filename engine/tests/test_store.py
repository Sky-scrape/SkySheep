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


async def test_rolling_backup_on_connect(tmp_path, monkeypatch):
    """连接时滚动备份（窗口外/首次），误删后可从 backups/ 恢复；
    频率窗口内不重复拷（桌面应用一天多次启动不再每次全量压一份）。"""
    import sqlite3
    import time

    # 关掉「每日一份」的频率窗口，验证备份本身的内容与时机
    monkeypatch.setattr(SessionStore, "BACKUP_MIN_INTERVAL_S", 0)
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

    # 恢复默认频率窗口（第一阶段为验证备份本身设成了 0）：
    # 最新备份很新鲜 → 窗口内的重复启动不再产生新备份
    monkeypatch.setattr(SessionStore, "BACKUP_MIN_INTERVAL_S", 20 * 3600)
    s3 = await SessionStore(db).connect()
    try:
        assert s3.backup_created is None, "频率窗口内的重复启动不应再拷一份"
    finally:
        await s3.close()


async def test_empty_session_helpers(store):
    """空会话统计与清理（保留当前会话与置顶会话）。"""
    p = await store.get_or_create_project("/tmp/empty-proj")
    with_msg = await store.create_session(p.id, title="有内容")
    await store.append_message(with_msg.id, Message.user("hi"))
    e1 = await store.create_session(p.id)
    e2 = await store.create_session(p.id)
    pinned_empty = await store.create_session(p.id)
    await store.set_pinned(pinned_empty.id, True)

    # 计数口径与 delete_empty_sessions 一致（置顶/归档的不算）：只数 e1/e2
    assert await store.count_empty_sessions(p.id) == 2
    removed = await store.delete_empty_sessions(p.id, keep_id=e1.id)
    assert removed == [e2.id]  # 只删掉 e2（e1 是当前会话、置顶会话、含内容的都保留）
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


async def test_usage_stats_session_count_and_models(store):
    """usage_stats：session_count 是去重会话数；by_provider 按 服务×模型 分组、缓存入账。"""
    p = await store.get_or_create_project("/tmp/usage-proj")
    sa = await store.create_session(p.id, title="A")
    sb = await store.create_session(p.id, title="B")
    await store.add_usage(sa.id, "prov", "m1", in_tokens=100, out_tokens=20, cached_tokens=80)
    await store.add_usage(sa.id, "prov", "m2", in_tokens=50, out_tokens=10)
    await store.add_usage(sb.id, "prov", "m1", in_tokens=30, out_tokens=0, cached_tokens=20)

    st = await store.usage_stats(14, project_id=p.id)
    # 两个会话各有过记录；by_session 只留最近 12 条，但计数是真实去重数
    assert st["session_count"] == 2
    # 同一服务两个模型拆成两行（旧实现 MAX(model) 会任意挑一个）
    rows = {(r["provider"], r["model"]): r for r in st["by_provider"]}
    assert set(rows) == {("prov", "m1"), ("prov", "m2")}
    assert rows[("prov", "m1")]["it"] == 130
    assert rows[("prov", "m1")]["cached"] == 100
    assert rows[("prov", "m2")]["cached"] == 0
    # 全局口径同样返回计数
    assert (await store.usage_stats(14))["session_count"] == 2


async def test_fts_write_failure_is_recorded_and_repaired(tmp_path, monkeypatch):
    """索引写失败要留痕并置脏；下次启动走查漏模式把洞补上（低危项）。"""
    from skysheep.session.store import SessionStore

    db = tmp_path / "s.db"
    store = await SessionStore(db).connect()
    proj = await store.get_or_create_project(str(tmp_path))
    sess = await store.create_session(proj.id, "会话")
    await store.append_message(sess.id, Message.user("第一条"))
    assert store.fts_ready is True

    # 制造一次索引写失败：让 INSERT 抛错（模拟 FTS 写入异常）
    calls = {"n": 0}
    real_execute = store._db.execute

    async def flaky(sql, *args, **kwargs):
        if "INSERT INTO messages_fts" in sql:
            calls["n"] += 1
            raise RuntimeError("fts down")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._db, "execute", flaky)
    await store.append_message(sess.id, Message.user("搜不到的那条"))
    assert calls["n"] == 1
    assert store._fts_dirty is True, "失败要置脏（旧实现静默吞掉）"
    # 该消息确实搜不到（前提成立）
    hits = await store.search_messages(proj.id, "搜不到")
    assert all("搜不到" not in (h.get("snippet") or "") for h in hits)
    monkeypatch.undo()
    await store.close()

    # 重新打开：查漏模式补齐水位以下的空洞
    store2 = await SessionStore(db).connect()
    assert store2._fts_dirty is False
    hits2 = await store2.search_messages(proj.id, "搜不到")
    assert any("搜不到" in (h.get("snippet") or "") for h in hits2), hits2
    await store2.close()


# ---- 搜索的 SQL 层硬上限（性能：命中行不再全量拉回内存） ----


async def test_search_messages_first_hit_per_session_across_many_rows(store):
    """大量命中行下结果仍正确：每会话取最先命中、按 limit 截断。"""
    proj = await store.get_or_create_project("/tmp/demo-search-limit")
    sessions = [await store.create_session(proj.id, f"s{i}") for i in range(5)]
    for sess in sessions:
        for i in range(20):
            await store.append_message(sess.id, Message.user(f"针目标词 第{i}条"))
    results = await store.search_messages(proj.id, "针目标词", limit=3)
    assert len(results) == 3
    assert len({r["session_id"] for r in results}) == 3
    assert all("针目标词" in r["snippet"] for r in results)


async def test_search_queries_capped_at_sql_level(store, monkeypatch):
    """FTS 与 LIKE 两条查询都带 SQL 层 LIMIT（取回前截行，防大库内存峰值）。"""
    executed: list[tuple[str, tuple]] = []
    real_execute = store._db.execute

    async def spy(sql, *args, **kwargs):
        executed.append((sql, tuple(args[0]) if args else ()))
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._db, "execute", spy)
    proj = await store.get_or_create_project("/tmp/demo-search-sqlcap")
    sess = await store.create_session(proj.id, "s")
    await store.append_message(sess.id, Message.user("searchable needle text here"))
    assert await store.search_messages(proj.id, "searchable needle", limit=5)
    fts_params = [q for sql, q in executed if "messages_fts MATCH" in sql]
    assert fts_params, "FTS 查询应被执行"
    assert fts_params[0][-1] == store.SEARCH_SQL_LIMIT

    executed.clear()
    store.fts_ready = False  # 强制走 LIKE 兜底
    assert await store.search_messages(proj.id, "needle", limit=5)
    like_params = [q for sql, q in executed if "LIKE ?" in sql]
    assert like_params, "LIKE 查询应被执行"
    assert like_params[0][-1] == store.SEARCH_SQL_LIMIT


# ---- 旧库迁移：缺列才补列、重复索引清理、真实故障不再静默吞 ----


async def test_connect_drops_legacy_duplicate_index(tmp_path):
    """旧库里同列重复的 idx_messages_session 在 connect 时清理，正式索引保留。"""
    from skysheep.session.store import SessionStore

    s = await SessionStore(tmp_path / "legacy.db").connect()
    await s._db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq)"
    )
    await s._db.commit()
    await s.close()

    s2 = await SessionStore(tmp_path / "legacy.db").connect()
    try:
        cur = await s2._db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        names = {r[0] for r in await cur.fetchall()}
        assert "idx_messages_session_seq" in names
        assert "idx_messages_session" not in names
    finally:
        await s2.close()


async def test_connect_restores_dropped_column(tmp_path):
    """旧库缺列（用 DROP COLUMN 模拟升级前旧库）在 connect 时补回默认值。"""
    from skysheep.session.store import SessionStore

    s = await SessionStore(tmp_path / "oldcol.db").connect()
    await s._db.execute("ALTER TABLE snippets DROP COLUMN enabled")
    await s._db.commit()
    await s.close()

    s2 = await SessionStore(tmp_path / "oldcol.db").connect()
    try:
        cur = await s2._db.execute("PRAGMA table_info(snippets)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "enabled" in cols
    finally:
        await s2.close()


async def test_migrate_logs_real_failures(store, monkeypatch, caplog):
    """非「列已存在」的迁移故障记 warning，不再静默吞掉。"""
    import logging

    # 先制造真实缺列，否则预检后没有 ALTER 可发
    await store._db.execute("ALTER TABLE snippets DROP COLUMN enabled")
    await store._db.commit()

    real_execute = store._db.execute

    async def flaky(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("ALTER TABLE"):
            raise RuntimeError("disk I/O error")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._db, "execute", flaky)
    with caplog.at_level(logging.WARNING, logger="skysheep.store"):
        await store._migrate_legacy()
    assert any("旧库迁移失败" in r.getMessage() for r in caplog.records)


# ---- F18：全新库 schema 直连即含全部迁移列（不再依赖 ALTER 补列） ----


async def test_fresh_schema_contains_all_migration_columns(tmp_path):
    """SCHEMA 与 _COLUMN_MIGRATIONS 必须逐列一致：sessions.tags（及
    snippets.enabled）曾只在迁移清单里，全新安装全靠 ALTER 补列——迁移一旦
    失败（仅记 warning），set_tags/list_all_tags 等运行时才炸 no such column。"""
    import sqlite3

    from skysheep.session.store import _COLUMN_MIGRATIONS, SCHEMA

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(SCHEMA)
        for table, ddl in _COLUMN_MIGRATIONS:
            column = ddl.split(None, 1)[0]
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            assert column in cols, f"SCHEMA 缺列 {table}.{column}"
        # tags 列定义与迁移补列语句一致：TEXT NOT NULL DEFAULT ''
        info = {r[1]: r for r in con.execute("PRAGMA table_info(sessions)")}
        tags = info["tags"]
        assert (tags[2], tags[3], tags[4]) == ("TEXT", 1, "''")
    finally:
        con.close()

    # 运行时链路：全新库直连后标签读写可用（不再依赖迁移补列）
    s = await SessionStore(tmp_path / "fresh.db").connect()
    try:
        sess = await s.create_session(None, title="t")
        await s.set_tags(sess.id, ["a", "b"])
        assert (await s.get_session(sess.id)).tags == "a,b"
        assert [x["tag"] for x in await s.list_all_tags(None)] == ["a", "b"]
    finally:
        await s.close()


# ---- F3：备份走 SQLite backup API 一致性快照；恢复先校验再原子换库 ----


async def test_restore_backup_roundtrip_and_safety_copy(tmp_path, monkeypatch):
    """恢复走通：换回旧备份后消息回退到备份时刻，当前库先留「恢复前」安全副本。"""
    monkeypatch.setattr(SessionStore, "BACKUP_MIN_INTERVAL_S", 20 * 3600)
    from pathlib import Path

    db = tmp_path / "app.db"
    s = await SessionStore(db).connect()
    try:
        p = await s.get_or_create_project("/tmp/restore-proj")
        sess = await s.create_session(p.id, title="t")
        await s.append_message(sess.id, Message.user("第一轮"))
        result = await s.backup_now()
        await s.append_message(sess.id, Message.user("第二轮"))

        done = await s.restore_backup(result["name"])
        assert done["restored"] == result["name"]
        assert done["safety_copy"], "恢复前要给当前库留安全副本"
        assert Path(done["safety_copy"]).exists()
        msgs = await s.load_messages(sess.id)
        assert [m.text for m in msgs] == ["第一轮"], "恢复后应回到备份时刻的消息状态"
    finally:
        await s.close()


async def test_restore_backup_rejects_bad_backup_and_keeps_main_db(tmp_path):
    """坏备份（撕裂/垃圾字节）恢复被拒且回可读错误，主库原样完好可用。

    旧实现仅校验文件名后 copy2 照单全收，主库换坏后 DatabaseError 使全部
    会话功能瘫痪。"""
    from pathlib import Path

    import pytest

    db = tmp_path / "app.db"
    s = await SessionStore(db).connect()
    try:
        p = await s.get_or_create_project("/tmp/restore-bad")
        sess = await s.create_session(p.id, title="t")
        await s.append_message(sess.id, Message.user("不能丢的消息"))
        good = await s.backup_now()

        # ① 撕裂备份：把备份文件截掉一半（模拟拷到一半的半截库）
        torn = s.backup_dir() / "app-torn.db"
        data = Path(good["path"]).read_bytes()
        torn.write_bytes(data[: len(data) // 2])
        # ② 垃圾字节备份
        garbage = s.backup_dir() / "app-garbage.db"
        garbage.write_bytes(b"definitely not a database" * 100)

        for bad in (torn.name, garbage.name):
            with pytest.raises(ValueError) as ei:
                await s.restore_backup(bad)
            assert "校验未通过" in str(ei.value)
            # 主库完好：消息还能读，store 连接仍可用
            assert [m.text for m in await s.load_messages(sess.id)] == ["不能丢的消息"]

        # 恢复动作留下的「恢复前」安全副本在场（人工救回的入口）
        assert any("恢复前" in f.name for f in s.backup_dir().glob("*.db"))
    finally:
        await s.close()


async def test_backup_now_is_consistent_snapshot_under_concurrent_writes(
    tmp_path, monkeypatch
):
    """备份走 SQLite backup API：并发写进行中拷出的也是一致性快照。

    旧实现 wal_checkpoint(TRUNCATE) 后无锁 copy2，拷贝窗口内并发写触发
    auto-checkpoint 搬页会拷出撕裂库。同时锚定备份路径不再用 shutil.copy2。
    """
    import asyncio
    import sqlite3

    import skysheep.session.store as store_mod

    db = tmp_path / "app.db"
    s = await SessionStore(db).connect()
    try:
        p = await s.get_or_create_project("/tmp/backup-proj")
        sess = await s.create_session(p.id, title="t")
        await s.append_message(sess.id, Message.user("第一轮"))

        copy2_calls = []
        real_copy2 = store_mod.shutil.copy2

        def spy_copy2(*a, **kw):
            copy2_calls.append(a[0])
            return real_copy2(*a, **kw)

        monkeypatch.setattr(store_mod.shutil, "copy2", spy_copy2)

        stop = asyncio.Event()

        async def writer():
            i = 0
            while not stop.is_set():
                await s.append_message(sess.id, Message.user(f"并发写 {i}"))
                i += 1

        wt = asyncio.create_task(writer())
        await asyncio.sleep(0.01)  # 让并发写先落几条
        result = await s.backup_now()
        stop.set()
        await wt

        assert not copy2_calls, "备份快照不该走 copy2（应走 SQLite backup API）"
        con = sqlite3.connect(result["path"])
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
            assert n >= 1, "备份至少包含开始备份前已提交的消息"
        finally:
            con.close()
    finally:
        await s.close()


# ---- F20：启动清扫接线（connect 回收数据目录内 mkstemp 模式的 .tmp 残留） ----


async def test_connect_sweeps_stale_tmp_files(tmp_path):
    import os
    import time as _time

    db = tmp_path / "app.db"
    stale = tmp_path / "ui.json.ab12cd9_.tmp"
    stale.write_bytes(b"leftover")
    old = _time.time() - 3600
    os.utime(stale, (old, old))
    keep = tmp_path / "笔记.tmp"
    keep.write_bytes(b"user file")

    s = await SessionStore(db).connect()
    try:
        assert not stale.exists(), "connect 应回收 mkstemp 模式的 .tmp 残留"
        assert keep.exists(), "非 mkstemp 模式的用户文件不能动"
    finally:
        await s.close()


# ---- 团队频道消息落库（team_messages，三期） ----


async def test_team_messages_roundtrip(store):
    """频道消息逐条落库、按 team_id 归组、seq 升序回放；team_id 是唯一归组键
    （不同团队互不可见），session_id 只作归属注记不参与查询。"""
    await store.add_team_message(
        "team-a", seq=1, from_member="user", to_member="all",
        msg_kind="ruling", text="开工", session_id="sess-1", created_at=100.0,
    )
    await store.add_team_message(
        "team-a", seq=3, from_member="小研", to_member="director",
        msg_kind="report", task_ref="T1", text="做完了", session_id="sess-1",
    )
    await store.add_team_message(
        "team-a", seq=2, from_member="director", to_member="小研",
        msg_kind="assign", task_ref="T1", text="派工",
    )
    await store.add_team_message(
        "team-b", seq=1, from_member="user", to_member="all", text="另一队",
    )

    rows = await store.list_team_messages("team-a")
    assert [r["seq"] for r in rows] == [1, 2, 3]  # 写入乱序，读出按 seq 升序
    assert rows[0]["from_member"] == "user" and rows[0]["text"] == "开工"
    assert rows[1]["task_ref"] == "T1" and rows[1]["msg_kind"] == "assign"
    assert rows[0]["created_at"] == 100.0 and rows[1]["created_at"] > 0  # 缺省取当下
    assert all(r["team_id"] == "team-a" for r in rows)
    assert await store.list_team_messages("no-such-team") == []


async def test_team_messages_table_in_schema(store):
    """team_messages 随 connect() 建表（第 15 张业务表），旧库升级路径由
    CREATE TABLE IF NOT EXISTS 直接补齐——手动建一个没有此表的旧库再 connect
    也能查。"""
    import sqlite3

    assert store._db is not None
    cur = await store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {r[0] for r in await cur.fetchall()}
    assert "team_messages" in tables

    # 模拟旧库（没有 team_messages）：connect 后补建，写入读取照常。
    # sessions 用旧版本就有的原始列形状（project_id 是建表列、不在补列清单，
    # idx_sessions_project 靠它建）——缺新表才是要验证的升级场景
    old_db = store.path.parent / "legacy.db"
    con = sqlite3.connect(old_db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id INTEGER,"
        " title TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    con.commit()
    con.close()
    from skysheep.session.store import SessionStore

    s = await SessionStore(old_db).connect()
    try:
        await s.add_team_message("team-x", seq=1, from_member="user", to_member="all")
        assert len(await s.list_team_messages("team-x")) == 1
    finally:
        await s.close()
