"""数据管理：存储占用（storage.usage）与表级清理（storage.cleanup）。

这两条 WS 方法此前没有任何测试（用户可见的「设置 · 关于 · 数据管理」卡片），
这里把占用统计、目录体量、清理校验、真删行与 VACUUM、以及「有会话在跑时
拒绝」的防线都覆盖到。走 SKYSHEEP_HOME 隔离，不碰真实 ~/.skysheep。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from test_server import make_client

from skysheep.config import db_path, skysheep_home
from skysheep.server.backend_parts.data_retention import RetentionMixin


def _rpc(ws, mid, method, params=None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    while True:
        frame = ws.receive_json()
        if frame.get("id") == mid and "event" not in frame:
            return frame


# ---- storage.usage ----


def test_storage_usage_reports_db_dirs_and_tables(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "u1", "storage.usage")
        assert r["ok"] is True
        res = r["result"]
        assert isinstance(res["db_bytes"], int)
        assert isinstance(res["backups_bytes"], int)
        assert isinstance(res["logs_bytes"], int)
        rows = {t["table"]: t for t in res["tables"]}
        assert "usage_log" in rows
        assert rows["usage_log"]["label"] == "用量记录（token 消耗历史）"
        assert isinstance(rows["usage_log"]["rows"], int)


def test_storage_usage_counts_backup_and_log_files(home):
    base = skysheep_home()
    (base / "backups").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    (base / "backups" / "a.bin").write_bytes(b"x" * 1234)
    (base / "logs" / "b.log").write_bytes(b"y" * 567)

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        res = _rpc(ws, "u1", "storage.usage")["result"]
        assert res["backups_bytes"] >= 1234
        assert res["logs_bytes"] >= 567


# ---- storage.cleanup：校验与真删 ----


def test_storage_cleanup_validation(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 既没有 targets 也没有 vacuum：拒绝
        r = _rpc(ws, "c1", "storage.cleanup", {})
        assert r["ok"] is False
        # targets 不是数组：拒绝
        r = _rpc(ws, "c2", "storage.cleanup", {"targets": "usage_log"})
        assert r["ok"] is False
        # 空数组且不 vacuum：拒绝
        r = _rpc(ws, "c3", "storage.cleanup", {"targets": []})
        assert r["ok"] is False
        # 元素不是对象 → 过滤后为空且不 vacuum：拒绝
        r = _rpc(ws, "c4", "storage.cleanup", {"targets": ["usage_log"]})
        assert r["ok"] is False


def test_storage_cleanup_deletes_rows_and_vacuums(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 直接往用量账本插几行（免跑整轮对话）
        conn = sqlite3.connect(db_path())
        try:
            for i in range(3):
                conn.execute(
                    "INSERT INTO usage_log (session_id, provider, model, ts, in_tokens,"
                    " out_tokens) VALUES (?,?,?,?,?,?)",
                    ("s1", "p", "m", time.time() - i, 1, 1),
                )
            conn.commit()
        finally:
            conn.close()

        before = {t["table"]: t["rows"] for t in _rpc(ws, "u1", "storage.usage")["result"]["tables"]}
        assert before["usage_log"] >= 3

        r = _rpc(ws, "c1", "storage.cleanup",
                 {"targets": [{"table": "usage_log", "days": 0}], "vacuum": True})
        assert r["ok"] is True
        res = r["result"]
        assert res["deleted"].get("usage_log", 0) >= 3
        assert res["total_deleted"] >= 3
        assert res["vacuumed"] is True
        assert res["freed_bytes"] >= 0
        # 返回里带最新占用快照
        assert "tables" in res

        after = {t["table"]: t["rows"] for t in _rpc(ws, "u2", "storage.usage")["result"]["tables"]}
        assert after["usage_log"] == 0


def test_storage_cleanup_ignores_unknown_table(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 不在白名单的表名：被跳过（不报错、不删）
        r = _rpc(ws, "c1", "storage.cleanup",
                 {"targets": [{"table": "messages", "days": 0}]})
        assert r["ok"] is True
        assert "messages" not in r["result"]["deleted"]


# ---- 单元级：目录体量统计的防御分支与「有会话在跑」防线 ----


class _StorageStub(RetentionMixin):
    def __init__(self, store=None, busy=None):
        self.store = store
        self.runtimes = {}
        self._base_run_task = None
        self._busy = busy

    def _backup_busy_message(self, action: str) -> str | None:
        if self._busy:
            return f"还有会话正在运行，先停止（Esc）或等它结束再{action}"
        return None


async def test_storage_cleanup_refuses_when_busy():
    stub = _StorageStub(busy=True)
    with pytest.raises(RuntimeError, match="正在运行"):
        await stub.storage_cleanup({"targets": [{"table": "usage_log", "days": 0}]})


async def test_storage_usage_without_store_is_safe(home):
    """没有 store（未连库）时不炸：只回目录体量，表计数为空。"""
    stub = _StorageStub(store=None)
    res = await stub.storage_usage()
    assert res["db_bytes"] == 0
    assert res["tables"] == []
    assert isinstance(res["backups_bytes"], int)


async def test_storage_usage_dir_size_swallows_errors(home, monkeypatch):
    """目录遍历抛 OSError 时按 0 计，不让整卡挂掉。"""
    base = skysheep_home()
    (base / "backups").mkdir(parents=True, exist_ok=True)
    real_rglob = Path.rglob

    def _boom(self, pattern):
        if self.name == "backups":
            raise OSError("unreadable")
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", _boom)
    stub = _StorageStub(store=None)
    res = await stub.storage_usage()
    assert res["backups_bytes"] == 0
