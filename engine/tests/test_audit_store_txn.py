"""审计 SEC-M16 回归：多语句写方法的独立事务边界（session/store.py）。

共享 aiosqlite 连接（隐式延迟事务）上的多语句写曾没有事务边界——其他协程的
commit 会把中间状态提交落盘，方法中途失败也不回滚，留下半删除的项目、新旧
交错的演化摘要、FTS 与主表漂移。这里钉死新不变量：这些方法要么全、要么无；
失败之后任意无关提交都不允许把半成品刷成事实；并发写者穿插不破坏原子性。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.messages import Message
from skysheep.session.store import SessionStore

# ---- 注入工具 ----


class _FailAtConn:
    """事务连接代理：第 fail_at 次 execute 按配置抛 RuntimeError，其余透传。

    before=True 在真执行该条语句前抛（模拟「上一条已生效、本条失败」）；
    before=False 先真执行再抛。commit/rollback 由 _immediate_txn 在真实连接
    上做，代理不参与——计数按每次 _write_txn 调用独立。
    """

    def __init__(self, conn, fail_at: int, *, before: bool = True):
        self._conn = conn
        self._seen = 0
        self._fail_at = fail_at
        self._before = before

    async def execute(self, sql, args=()):
        self._seen += 1
        if self._seen == self._fail_at and self._before:
            raise RuntimeError(f"注入失败：第 {self._seen} 条语句执行前")
        cur = await self._conn.execute(sql, args)
        if self._seen == self._fail_at and not self._before:
            raise RuntimeError(f"注入失败：第 {self._seen} 条语句执行后")
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


def inject_txn_failure(monkeypatch, fail_at: int, *, before: bool = True) -> None:
    """把 SessionStore._write_txn 换成「第 fail_at 条 execute 失败」的版本。"""
    original = SessionStore._write_txn

    async def patched(self, fn):
        async def wrapped(conn):
            return await fn(_FailAtConn(conn, fail_at, before=before))

        return await original(self, wrapped)

    monkeypatch.setattr(SessionStore, "_write_txn", patched)


async def make_session_with_messages(store, project_id, title, texts):
    s = await store.create_session(project_id, title=title)
    for t in texts:
        await store.append_message(s.id, Message.user(t))
    return s


async def fts_ghost_rows(store) -> int:
    """FTS 里指向已不存在消息行的索引条数（>0 即索引与主表漂移）。"""
    cur = await store._db.execute(
        "SELECT COUNT(*) AS n FROM messages_fts f"
        " LEFT JOIN messages m ON m.id = f.rowid WHERE m.id IS NULL"
    )
    return int((await cur.fetchone())[0])


def require_fts(store) -> None:
    if not store.fts_ready:
        pytest.skip("本机 SQLite 无 FTS5，跳过索引一致性断言")


# ---- 失败回滚：失败 + 无关提交穿插后库状态要么全要么无 ----


async def test_delete_project_failure_rolls_back_completely(store, monkeypatch):
    proj = await store.get_or_create_project("/tmp/m16-del-proj")
    s = await make_session_with_messages(
        store, proj.id, "要删的会话", ["删除目标词甲", "删除目标词乙"]
    )
    await store.add_rule(proj.id, "run_command", "prefix", "git ")
    await store.add_project_task(proj.id, "任务清单项")
    bystander = await store.create_session(proj.id, title="旁观会话")

    # 语句序：FTS 删除(1) → messages 删除(2) → sessions 删除(3)。第 3 条前失败
    # ——正是旧实现的最坏半态（消息没了、会话/白名单还在）出现的时点。
    inject_txn_failure(monkeypatch, fail_at=3)
    with pytest.raises(RuntimeError):
        await store.delete_project(proj.id)
    # 无关协程的提交穿插：不允许把半删除刷成事实
    await store.set_title(bystander.id, "穿插标题")

    assert await store.get_session(s.id) is not None
    assert len(await store.load_messages(s.id)) == 2
    assert await store.list_rules(proj.id)
    assert await store.list_project_tasks(proj.id)
    assert any(p.id == proj.id for p in await store.list_projects())
    require_fts(store)
    assert await store.search_messages(proj.id, "删除目标词")
    assert await fts_ghost_rows(store) == 0


async def test_delete_session_failure_is_all_or_nothing(store, monkeypatch):
    require_fts(store)
    proj = await store.get_or_create_project("/tmp/m16-del-sess")
    s = await make_session_with_messages(
        store, proj.id, "待删会话", ["会删词aa one", "会删词bb two"]
    )
    bystander = await store.create_session(proj.id, title="旁观会话")

    # 第 3 条（sessions 删除）前失败：messages 已删、会话壳还在的半态时点
    inject_txn_failure(monkeypatch, fail_at=3)
    with pytest.raises(RuntimeError):
        await store.delete_session(s.id)
    await store.set_title(bystander.id, "穿插标题")

    assert await store.get_session(s.id) is not None
    assert len(await store.load_messages(s.id)) == 2
    assert await store.search_messages(proj.id, "会删词")
    assert await fts_ghost_rows(store) == 0


async def test_replace_map_digests_failure_restores_old_digests(store, monkeypatch):
    proj = await store.get_or_create_project("/tmp/m16-map")
    bystander = await store.create_session(proj.id, title="旁观会话")
    old = [
        {"kind": "overview", "title": "旧总览", "summary": "旧的总体描述",
         "highlights": [], "topics": [], "session_ids": [], "start_ts": 0, "end_ts": 10},
        {"kind": "phase", "title": "旧阶段一", "summary": "旧的第一阶段",
         "highlights": ["旧亮点"], "topics": ["旧主题"], "session_ids": [],
         "start_ts": 1, "end_ts": 9},
    ]
    await store.replace_map_digests(proj.id, old)

    # 新摘要第二条 start_ts 非法：DELETE 已执行、第一条 INSERT 已执行后自然失败
    #（无需注入）——docstring 承诺的「写失败整体回滚到旧摘要」必须成立
    new = [
        {"kind": "overview", "title": "新总览", "summary": "新的总体描述",
         "highlights": [], "topics": [], "session_ids": [], "start_ts": 0, "end_ts": 5},
        {"kind": "phase", "title": "坏行", "summary": "s", "highlights": [],
         "topics": [], "session_ids": [], "start_ts": "not-a-number", "end_ts": 6},
    ]
    with pytest.raises(ValueError):
        await store.replace_map_digests(proj.id, new)
    # 无关协程的提交穿插：不允许把「删掉旧摘要」刷成事实
    await store.set_title(bystander.id, "穿插标题")

    digests = await store.list_map_digests(proj.id)
    assert [d["title"] for d in digests] == ["旧总览", "旧阶段一"]
    assert digests[1]["highlights"] == ["旧亮点"]


async def test_truncate_from_failure_keeps_fts_in_sync(store, monkeypatch):
    require_fts(store)
    proj = await store.get_or_create_project("/tmp/m16-trunc")
    s = await make_session_with_messages(
        store, proj.id, "截断会话",
        ["开头词aa one", "开头词bb two", "截断词cc three", "截断词dd four", "截断词ee five"],
    )
    bystander = await store.create_session(proj.id, title="旁观会话")

    # 语句序：FTS 删除(1) → messages 删除(2)。在 FTS 删除已生效后、主表删除前
    # 失败——正是旧实现漂移的失败点（_fts_dirty 无机会置位）。
    inject_txn_failure(monkeypatch, fail_at=2)
    with pytest.raises(RuntimeError):
        await store.truncate_from(s.id, 3, include_self=True)
    monkeypatch.undo()  # 撤掉注入，后续走真实路径
    await store.set_title(bystander.id, "穿插标题")

    assert len(await store.load_messages(s.id)) == 5  # 一条都没删
    assert await store.search_messages(proj.id, "截断词")  # 索引没有漂移
    assert await fts_ghost_rows(store) == 0

    # 成功路径回归：真截断后，截掉的搜不到、留下的搜得到，索引无幽灵行
    n = await store.truncate_from(s.id, 3, include_self=True)
    assert n == 3
    assert len(await store.load_messages(s.id)) == 2
    assert not await store.search_messages(proj.id, "截断词")
    assert await store.search_messages(proj.id, "开头词")
    assert await fts_ghost_rows(store) == 0


async def test_copy_messages_between_failure_copies_nothing(store, monkeypatch):
    proj = await store.get_or_create_project("/tmp/m16-copy")
    src = await make_session_with_messages(
        store, proj.id, "源会话", [f"复制词{i} body" for i in range(5)]
    )
    dst = await store.create_session(proj.id, title="分叉会话")
    bystander = await store.create_session(proj.id, title="旁观会话")

    # 语句序：SELECT(1) → INSERT×5(2..6)。第 4 条前失败（已复制 2 条）
    inject_txn_failure(monkeypatch, fail_at=4)
    with pytest.raises(RuntimeError):
        await store.copy_messages_between(src.id, dst.id, upto_seq=5)
    monkeypatch.undo()  # 撤掉注入，后续走真实路径
    await store.set_title(bystander.id, "穿插标题")

    assert await store.load_messages(dst.id) == []  # 不留半批
    require_fts(store)
    assert await fts_ghost_rows(store) == 0

    # 成功路径回归
    n = await store.copy_messages_between(src.id, dst.id, upto_seq=5)
    assert n == 5
    assert [m.seq for m in await store.load_messages(dst.id)] == [1, 2, 3, 4, 5]


async def test_add_pipeline_failure_leaves_no_orphan(store, monkeypatch):
    proj = await store.get_or_create_project("/tmp/m16-pipe")
    bystander = await store.create_session(proj.id, title="旁观会话")
    nodes = [{"title": f"节点{i}", "prompt": f"做第{i}步"} for i in range(3)]

    # 语句序：INSERT pipelines(1) → INSERT 节点×3(2..4)。第 3 条前失败（节点 1 已进）
    inject_txn_failure(monkeypatch, fail_at=3)
    with pytest.raises(RuntimeError):
        await store.add_pipeline(proj.id, "会失败的流水线", nodes)
    monkeypatch.undo()  # 撤掉注入，后续走真实路径
    await store.set_title(bystander.id, "穿插标题")

    assert await store.list_pipelines(proj.id) == []  # 没有孤儿流水线
    cur = await store._db.execute("SELECT COUNT(*) AS n FROM pipeline_nodes")
    assert int((await cur.fetchone())[0]) == 0  # 也没有半批节点

    # 成功路径回归
    pipe = await store.add_pipeline(proj.id, "正常流水线", nodes)
    assert len(pipe["nodes"]) == 3


# ---- 并发穿插：其他协程的 commit 不再泄漏多语句方法的中间状态 ----


async def test_concurrent_writer_interleaves_without_partial_state(store):
    """协程 A 跑多语句复制、协程 B 在共享连接上反复 execute+commit 穿插：
    A 完成后库里必须是整批，B 的写也必须完整落盘。"""
    proj = await store.get_or_create_project("/tmp/m16-race")
    src = await make_session_with_messages(
        store, proj.id, "源会话", [f"并发词{i} body" for i in range(6)]
    )
    dst = await store.create_session(proj.id, title="分叉会话")
    bystander = await store.create_session(proj.id, title="旁观会话")

    async def writer_a():
        await asyncio.sleep(0)  # 与 B 交错启动
        return await store.copy_messages_between(src.id, dst.id, upto_seq=6)

    async def writer_b():
        await asyncio.sleep(0)
        for i in range(5):
            await store.set_title(bystander.id, f"并发标题{i}")  # 每次 execute+commit

    n, _ = await asyncio.gather(writer_a(), writer_b())
    assert n == 6
    assert [m.seq for m in await store.load_messages(dst.id)] == [1, 2, 3, 4, 5, 6]
    got = await store.get_session(bystander.id)
    assert got.title == "并发标题4"
    require_fts(store)
    assert await fts_ghost_rows(store) == 0


async def test_concurrent_writer_failure_leaves_nothing(store, monkeypatch):
    """并发穿插下 A 失败：A 半点不剩（回滚），B 的写完整落盘。"""
    proj = await store.get_or_create_project("/tmp/m16-race-fail")
    src = await make_session_with_messages(
        store, proj.id, "源会话", [f"并发词{i} body" for i in range(6)]
    )
    dst = await store.create_session(proj.id, title="分叉会话")
    bystander = await store.create_session(proj.id, title="旁观会话")

    inject_txn_failure(monkeypatch, fail_at=4)  # A 复制到第 3 条消息时失败

    async def writer_a():
        await asyncio.sleep(0)
        await store.copy_messages_between(src.id, dst.id, upto_seq=6)

    async def writer_b():
        await asyncio.sleep(0)
        await store.set_title(bystander.id, "并发标题")

    results = await asyncio.gather(writer_a(), writer_b(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert await store.load_messages(dst.id) == []
    got = await store.get_session(bystander.id)
    assert got.title == "并发标题"
    require_fts(store)
    assert await fts_ghost_rows(store) == 0


async def test_append_message_concurrent_seq_unique(store):
    """并发追加同一会话：seq 分配（MAX+1→INSERT）在独立写事务内完成，
    全部成功且无重号；UNIQUE(session_id, seq) 只是最后兜底。"""
    proj = await store.get_or_create_project("/tmp/m16-seq")
    s = await store.create_session(proj.id, title="并发追加")

    results = await asyncio.gather(
        *[store.append_message(s.id, Message.user(f"并发消息{i}")) for i in range(8)]
    )
    assert all(r is not None for r in results)
    msgs = await store.load_messages(s.id)
    assert [m.seq for m in msgs] == list(range(1, 9))


async def test_orphaned_shared_txn_does_not_starve_writers(store):
    """共享连接上的孤儿写事务（任务在 execute 与 commit 之间被取消留下的）
    不能占死写锁：独立写事务在 BEGIN 撞锁后清掉它再重试，落库照常完成。"""
    proj = await store.get_or_create_project("/tmp/m16-orphan")
    s = await store.create_session(proj.id, title="孤儿事务")
    other = await store.create_session(proj.id, title="旁观会话")

    # 复刻被取消任务留下的现场：共享连接上执行一条写语句、故意不提交
    # （等价于 touch() 在 execute 与 commit 之间被 CancelledError 打断）
    await store._db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (1.0, s.id)
    )
    assert store._db.in_transaction

    # 独立写事务要能自己走出来：清孤儿 → BEGIN 成功 → 落库生效
    await store.append_message(s.id, Message.user("孤儿之后照常落库"))
    msgs = await store.load_messages(s.id)
    assert [m.text for m in msgs] == ["孤儿之后照常落库"]
    assert not store._db.in_transaction  # 孤儿已清，共享连接不再挂着事务
    await store.set_title(other.id, "共享连接恢复正常")  # 后续写不受影响


# ---- 事务骨架本身 ----


async def test_standalone_txn_pragmas_match_main_connection(store):
    """独立事务连接的 pragmas 与主连接同口径（busy_timeout / synchronous）。"""

    async def probe(conn):
        cur = await conn.execute("PRAGMA synchronous")
        sync = int((await cur.fetchone())[0])
        cur = await conn.execute("PRAGMA busy_timeout")
        busy = int((await cur.fetchone())[0])
        return sync, busy

    sync, busy = await store._write_txn(probe)

    cur = await store._db.execute("PRAGMA synchronous")
    main_sync = int((await cur.fetchone())[0])
    cur = await store._db.execute("PRAGMA busy_timeout")
    main_busy = int((await cur.fetchone())[0])
    assert busy == 5000
    assert (sync, busy) == (main_sync, main_busy)


async def test_write_txn_propagates_return_value_and_rolls_back_on_error(store):
    """_write_txn 透传回调返回值；回调抛异常时整体回滚且异常原样上抛。"""
    proj = await store.get_or_create_project("/tmp/m16-helper")

    async def ok(conn):
        cur = await conn.execute(
            "INSERT INTO project_tasks (project_id, title, detail, created_at)"
            " VALUES (?, ?, '', ?)",
            (proj.id, " helper ", 1.0),
        )
        return cur.lastrowid

    assert await store._write_txn(ok) is not None
    assert len(await store.list_project_tasks(proj.id)) == 1

    async def boom(conn):
        await conn.execute(
            "INSERT INTO project_tasks (project_id, title, detail, created_at)"
            " VALUES (?, ?, '', ?)",
            (proj.id, "不该留下", 1.0),
        )
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await store._write_txn(boom)
    assert len(await store.list_project_tasks(proj.id)) == 1


async def test_shared_conn_retry_wrapper(store, monkeypatch):
    """共享连接重试壳：瞬时 locked 自动退避重试；放弃前回滚收场；
    非锁类错误原样上抛不重试。"""
    import sqlite3

    from skysheep.session.store import _SharedConnRetry

    class FlakyConn:
        def __init__(self, fail_times: int):
            self.fail_left = fail_times
            self.executes = 0
            self.rolled_back = 0

        async def execute(self, sql, *args, **kwargs):
            self.executes += 1
            if self.fail_left > 0:
                self.fail_left -= 1
                raise sqlite3.OperationalError("database is locked")
            return "cursor"

        async def rollback(self):
            self.rolled_back += 1

    # 前两次 locked、第三次成功：重试后返回结果
    flaky = FlakyConn(fail_times=2)
    shell = _SharedConnRetry(flaky, retries=3, delay=0.0)
    assert await shell.execute("UPDATE t SET x = 1") == "cursor"
    assert flaky.executes == 3 and flaky.rolled_back == 0

    # 一直 locked：重试耗尽后回滚收场并上抛
    flaky = FlakyConn(fail_times=99)
    shell = _SharedConnRetry(flaky, retries=2, delay=0.0)
    with pytest.raises(sqlite3.OperationalError):
        await shell.execute("UPDATE t SET x = 1")
    assert flaky.executes == 3 and flaky.rolled_back == 1

    # 非锁类错误不重试、不回滚
    flaky = FlakyConn(fail_times=0)

    async def boom(sql, *args, **kwargs):
        raise sqlite3.OperationalError("no such table: nope")

    monkeypatch.setattr(flaky, "execute", boom)
    shell = _SharedConnRetry(flaky, retries=3, delay=0.0)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await shell.execute("UPDATE nope SET x = 1")
    assert flaky.rolled_back == 0

    # 共享连接上的锁重试是端到端行为：单语句写失败一次后下一次成功，方法照常完成
    proj = await store.get_or_create_project("/tmp/m16-retry")
    s = await store.create_session(proj.id, title="重试壳")
    real_execute = store._db._conn.execute
    locked_fired = {"n": 0}

    async def one_locked(sql, *args, **kwargs):
        if sql.startswith("UPDATE sessions SET summary"):
            locked_fired["n"] += 1
            if locked_fired["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._db._conn, "execute", one_locked)
    await store.set_summary(s.id, "重试壳生效")
    assert locked_fired["n"] == 2  # 第一次撞锁、重试成功
    got = await store.get_session(s.id)
    assert got.summary == "重试壳生效"
