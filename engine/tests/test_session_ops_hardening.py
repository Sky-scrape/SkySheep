"""会话操作体系加固回归：2026-09 审计发现的问题逐条钉死。

覆盖：删项目级联清理定时任务/流水线、删会话清理检查点、检查点落盘失败的
内存兜底、blob 丢失不再误删现文件、bytes 随 meta 持久化、forget_session、
空会话角标与清理口径一致（置顶/归档不动）、LIKE 兜底转义通配符、FTS 回填
失败置脏、运行中会话拒绝分叉/回滚、删除返回契约（new_active / truncate id）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from test_server import recv_until

from skysheep.core.checkpoints import CheckpointStore
from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider


def _db(home):
    con = sqlite3.connect(home / "home" / "skysheep.db")
    con.row_factory = sqlite3.Row
    return con


# ---- 删项目级联：定时任务 / 流水线必须跟着走（否则孤儿任务挂到当前项目继续跑） ----


async def test_delete_project_cascades_cron_and_pipelines(home):
    from skysheep.session.store import SessionStore

    store = await SessionStore(home / "home" / "skysheep.db").connect()
    try:
        proj = await store.get_or_create_project(str(home / "proj"))
        pid = proj.id
        await store.add_cron_task(pid, "报告", "写周报", "interval", interval_minutes=30)
        pipe = await store.add_pipeline(pid, "整理", nodes=[
            {"title": "收集", "prompt": "收集资料"},
        ])
        s = await store.create_session(pid, "有会话")
        await store.append_message(s.id, Message.user("hi"))

        removed = await store.delete_project(pid)
        assert removed == 1
        con = _db(home)
        try:
            assert con.execute("SELECT COUNT(*) FROM cron_tasks WHERE project_id = ?",
                               (pid,)).fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM pipelines WHERE project_id = ?",
                               (pid,)).fetchone()[0] == 0
            assert con.execute(
                "SELECT COUNT(*) FROM pipeline_nodes WHERE pipeline_id = ?",
                (pipe["id"],)).fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?",
                               (pid,)).fetchone()[0] == 0
        finally:
            con.close()
    finally:
        await store.close()


# ---- 空会话：角标口径与清理一致；置顶/归档的都不清 ----


async def test_empty_session_count_matches_cleanup_scope(store, tmp_path):
    p1 = await store.create_session(None, "普通空会话")
    p2 = await store.create_session(None, "置顶空会话")
    p3 = await store.create_session(None, "归档空会话")
    await store.set_pinned(p2.id, True)
    await store.set_archived(p3.id, True)
    full = await store.create_session(None, "有消息")
    await store.append_message(full.id, Message.user("占位"))

    assert await store.count_empty_sessions(None) == 1, "置顶/归档的空会话不该计入角标"
    removed = await store.delete_empty_sessions(None, keep_id=full.id)
    assert removed == 1
    assert await store.get_session(p1.id) is None
    assert await store.get_session(p2.id) is not None, "置顶的空会话不被清理"
    assert await store.get_session(p3.id) is not None, "归档的空会话不被清理"


# ---- 搜索兜底：LIKE 通配符按字面处理 ----


async def test_search_like_escapes_wildcards(store):
    s = await store.create_session(None, "百分号")
    await store.append_message(s.id, Message.user("进度 100%完成，下一步是收尾"))
    s2 = await store.create_session(None, "不含百分号")
    await store.append_message(s2.id, Message.user("普通消息内容"))
    store.fts_ready = False  # 强制走 LIKE 兜底

    rows = await store._search_like(None, "100%", "all")
    assert len(rows) == 1, "搜「100%」只命中字面包含它的消息"
    rows = await store._search_like(None, "%", "all")
    assert len(rows) == 1, "搜「%」不再等于全表命中"
    # 正文里定位不到时片段从开头截，不产生负索引的乱码切片
    results = await store.search_messages(None, "100%", scope="all")
    assert results and "进度" in results[0]["snippet"]


# ---- FTS 回填失败必须置脏：下次启动走查漏模式把洞补回来 ----


async def test_fts_backfill_failure_marks_dirty(store, monkeypatch):
    async def boom():
        raise RuntimeError("磁盘抖动")

    monkeypatch.setattr(store, "_backfill_fts", boom)
    await store._setup_fts()
    assert store.fts_ready is False
    assert store._fts_dirty is True, "回填半途而废会留水位以下的洞，必须置脏改走查漏"


# ---- 检查点：bytes 随 meta 持久化（重启后字节上限仍有数可依） ----


def test_checkpoint_bytes_survive_reload(tmp_path):
    root = tmp_path / "cps"
    store = CheckpointStore(root=root)
    f = tmp_path / "doc.txt"
    f.write_text("old", encoding="utf-8")
    cp = store.save("s1", {str(f): b"x" * 1024})
    assert cp is not None
    meta = json.loads(
        (root / "s1" / cp["id"] / "meta.json").read_text(encoding="utf-8"))
    assert meta.get("bytes") == 1024, "bytes 要写进 meta，跨重启字节上限才有效"

    reloaded = CheckpointStore(root=root)
    assert reloaded._items[cp["id"]]["bytes"] == 1024


# ---- 检查点：blob 丢失时回滚不能把现文件删掉 ----


def test_checkpoint_missing_blob_keeps_current_file(tmp_path):
    root = tmp_path / "cps"
    store = CheckpointStore(root=root)
    f = tmp_path / "doc.txt"
    f.write_text("before", encoding="utf-8")
    cp = store.save("s1", {str(f): b"before"})
    blob = next(
        b for b in (root / "s1" / cp["id"]).iterdir() if b.suffix == ".bin")
    blob.unlink()  # 模拟 blob 被外部清理
    f.write_text("用户后来的改动", encoding="utf-8")

    store.restore(cp["id"], force=True)  # force：跳过冲突检查（签名对不上是必然）
    assert f.read_text(encoding="utf-8") == "用户后来的改动", (
        "blob 丢了只能少恢复一个文件，绝不能把它当「新建文件」删掉")


# ---- 检查点：落盘失败时内存副本不丢（同进程内回滚仍可用） ----


def test_checkpoint_persist_failure_keeps_memory_copy(tmp_path, monkeypatch):
    import skysheep.core.checkpoints as ckpt

    def broken_write(path, text, **kw):
        raise OSError("磁盘已满")

    monkeypatch.setattr(ckpt, "write_text_atomic", broken_write)
    store = CheckpointStore(root=tmp_path / "cps")
    f = tmp_path / "doc.txt"
    f.write_text("before", encoding="utf-8")
    cp = store.save("s1", {str(f): b"before"})
    assert cp is not None

    # 内存副本还在：回滚拿得到改前内容（落盘失败不再静默掏空这条检查点）
    assert store.restore(cp["id"]) == [str(f)]


# ---- 检查点：删会话要连索引带磁盘目录一起清 ----


def test_checkpoint_forget_session(tmp_path):
    root = tmp_path / "cps"
    store = CheckpointStore(root=root)
    fa = tmp_path / "a.txt"
    fa.write_text("a-old", encoding="utf-8")
    fb = tmp_path / "b.txt"
    fb.write_text("b-old", encoding="utf-8")
    cp_a = store.save("s1", {str(fa): b"a-old"})
    cp_b = store.save("s2", {str(fb): b"b-old"})

    assert store.forget_session("s1") == 1
    assert store.list_for("s1") == []
    assert not (root / "s1" / cp_a["id"]).exists(), "磁盘目录一并移除"
    assert store.list_for("s2"), "别的会话不受影响"
    fb.write_text("b-new", encoding="utf-8")
    store.restore(cp_b["id"], force=True)  # 文件被本测试改过：force 跳过冲突检查
    assert fb.read_text(encoding="utf-8") == "b-old"


# ---- 协议层：删会话清理检查点、排队轮落空、返回契约 ----


async def test_delete_session_cleans_checkpoints_and_fails_queue(home):
    provider = FakeProvider([[TextBlock(text="答")]])
    from skysheep.server import create_app

    app = create_app(working_dir=home / "proj", provider_name="fake",
                     provider_factory=lambda: provider)
    from fastapi.testclient import TestClient

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        backend = app.state.backend
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        # 直接塞一条检查点（等价于该会话某轮改过文件后的落盘结果）
        f = home / "proj" / "doc.txt"
        f.write_text("old", encoding="utf-8")
        cp = backend.checkpoints.save(sid, {str(f): b"old"})
        assert cp is not None and backend.checkpoints.list_for(sid)

        # 模拟一个排队中的轮次（Future 未落）
        from skysheep.server.backend import QueuedTurn

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        backend.runtimes[sid].queue.append(
            QueuedTurn(text="排队的消息", emit=lambda ev: asyncio.sleep(0),
                       plan_mode=False, fut=fut))

        ws.send_json({"id": "d1", "method": "session.delete", "params": {"id": sid}})
        frame = recv_until(ws, "d1")
        assert frame["ok"]
        assert frame["result"]["new_active"] is None, "删空后进入待新建态（跟踪键跟着走）"
        assert fut.done() and isinstance(fut.exception(), RuntimeError), \
            "排队轮的请求必须拿到明确失败，不能永久挂起"
        assert backend.checkpoints.list_for(sid) == [], "检查点索引随会话清空"
        assert not any((home / "home" / "backups" / "checkpoints").rglob(cp["id"])), \
            "磁盘上的检查点目录也一并移除"
        assert sid not in backend._manually_named


async def test_truncate_returns_session_id_for_tracking(home):
    provider = FakeProvider([[TextBlock(text="回答一")], [TextBlock(text="回答二")]])
    from skysheep.server import create_app

    app = create_app(working_dir=home / "proj", provider_name="fake",
                     provider_factory=lambda: provider)
    from fastapi.testclient import TestClient

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "问一"}})
        recv_until(ws, "c1")
        sid = app.state.backend.session.id
        ws.send_json({"id": "t1", "method": "session.truncate",
                      "params": {"id": sid, "mode": "regen"}})
        result = recv_until(ws, "t1")["result"]
        assert result["id"] == sid, "WS 层按返回的 id 跟踪连接会话，truncate 必须带回"


async def test_running_session_rejects_fork_and_checkpoint_restore(home):
    provider = FakeProvider([[TextBlock(text="答")]])
    from skysheep.server import create_app

    app = create_app(working_dir=home / "proj", provider_name="fake",
                     provider_factory=lambda: provider)
    from fastapi.testclient import TestClient

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        backend = app.state.backend
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        f = home / "proj" / "doc.txt"
        f.write_text("old", encoding="utf-8")
        cp = backend.checkpoints.save(sid, {str(f): b"old"})

        task = asyncio.create_task(asyncio.sleep(30))
        backend.runtimes[sid].run_task = task  # 模拟会话正在跑
        try:
            ws.send_json({"id": "f1", "method": "session.fork", "params": {"id": sid}})
            frame = recv_until(ws, "f1")
            assert not frame["ok"] and "正在运行" in frame["error"]

            ws.send_json({"id": "r1", "method": "checkpoint.restore",
                          "params": {"id": cp["id"]}})
            frame = recv_until(ws, "r1")
            assert not frame["ok"] and "正在运行" in frame["error"], \
                "运行中回滚会踩进行中的轮次（truncate 已有同款守卫）"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            backend.runtimes[sid].run_task = None

        ws.send_json({"id": "f2", "method": "session.fork", "params": {"id": sid}})
        assert recv_until(ws, "f2")["ok"], "轮次结束后分叉恢复正常"
