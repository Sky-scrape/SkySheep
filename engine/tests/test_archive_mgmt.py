"""归档管理（设置 · 归档管理）：自动归档巡检、[archive] 配置、跨项目列表与批量操作。

锁五层：① 候选扫描（stale_session_ids 的置顶 / 已归档 / 排除名单口径）；
② 配置（[archive] 读写回环与非法天数拒绝）；③ WS 方法（archive.* 的读写
回环、跨项目列表、批量恢复 / 删除只碰已归档）；④ 自动归档 pass（开关关闭
零动作，置顶 / 运行中 / 当前打开的会话不动）；⑤ 前端接线（设置页卡片、
回调与切页钩子）。全部走 SKYSHEEP_HOME 隔离（home fixture），不碰真实
~/.skysheep。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import read_app_bundle
from test_server import make_client, recv_until

from skysheep.config import (
    ConfigError,
    SkySheepConfig,
    load_config,
    set_archive_in_config,
)
from skysheep.session.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": method, "params": params or {}})
        return recv_until(ws, "m1")


async def _age(store: SessionStore, days_by_sid: dict[str, float]) -> None:
    """把会话的 updated_at 拨回 N 天前（自动归档按它判超期）。"""
    now = time.time()
    for sid, days in days_by_sid.items():
        await store._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now - days * 86400, sid),
        )
    await store._db.commit()


class _NeverDone:
    """永不完成的假 run_task：让一个会话看起来「正在运行」。"""

    def done(self) -> bool:
        return False


# ---------- ① 存储层：自动归档候选 ----------


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "s.db").connect()
    yield s
    await s.close()


async def test_stale_session_ids_candidates(store):
    pid = (await store.get_or_create_project("/p")).id
    old = await store.create_session(pid, "旧会话")
    older = await store.create_session(pid, "更旧的会话")
    pinned = await store.create_session(pid, "置顶")
    fresh = await store.create_session(pid, "新会话")
    archived = await store.create_session(pid, "已归档")
    await _age(store, {
        old.id: 20, older.id: 30, pinned.id: 20, fresh.id: 1, archived.id: 20,
    })
    await store.set_pinned(pinned.id, True)
    await store.set_archived(archived.id, True)

    ids = await store.stale_session_ids(time.time() - 14 * 86400)
    assert ids == [older.id, old.id], "超期未置顶未归档的按最旧在前；置顶/已归档/新会话都不算"

    assert await store.stale_session_ids(time.time() - 14 * 86400, [old.id]) == [older.id], \
        "排除名单（运行期在跑/打开的会话）不进候选"


# ---------- ② 配置层 ----------


def test_archive_config_defaults():
    sec = SkySheepConfig().archive
    assert sec.auto_archive is False
    assert sec.auto_after_days == 14


def test_set_archive_in_config_roundtrip(home):
    set_archive_in_config(auto_archive=True, auto_after_days=30)
    sec = load_config().archive
    assert (sec.auto_archive, sec.auto_after_days) == (True, 30)

    # 只动一项，另一项保持
    set_archive_in_config(auto_after_days=3)
    sec = load_config().archive
    assert (sec.auto_archive, sec.auto_after_days) == (True, 3)

    # [archive] 不与其它段互相踩（段解析回归：见 config 段循环的 else 兜底坑）
    assert type(load_config().server).__name__ == "ServerConfig"


@pytest.mark.parametrize("bad", [0, -1, 4000, "abc", True])
def test_set_archive_in_config_rejects_bad_days(home, bad):
    with pytest.raises(ConfigError):
        set_archive_in_config(auto_after_days=bad)


# ---------- ③ WS 方法：状态 / 保存 / 列表 / 批量恢复删除 ----------


def test_ws_archive_status_defaults(home):
    frame = call(home, "archive.status")
    assert frame["ok"]
    assert frame["result"] == {"auto_archive": False, "auto_after_days": 14, "archived_count": 0}


def test_ws_archive_save_roundtrip(home):
    frame = call(home, "archive.save", {"auto_archive": True, "auto_after_days": 7})
    assert frame["ok"]
    assert frame["result"]["auto_archive"] is True
    assert frame["result"]["auto_after_days"] == 7
    assert load_config().archive.auto_archive is True, "真实落进 config.toml"

    # 部分保存：只动开关，天数保持
    frame = call(home, "archive.save", {"auto_archive": False})
    assert frame["ok"]
    assert frame["result"] == {"auto_archive": False, "auto_after_days": 7, "archived_count": 0}


@pytest.mark.parametrize("bad", [0, -1, 4000, "abc", True])
def test_ws_archive_save_rejects_bad_days(home, bad):
    frame = call(home, "archive.save", {"auto_after_days": bad})
    assert not frame["ok"], f"非法保留天数 {bad!r} 必须被拒绝"


async def _seed(backend) -> dict:
    """三个项目各一个已归档会话 + 一个快聊归档 + 一个未归档，返回 id 映射。"""
    store = backend.store
    pa = await store.get_or_create_project("/proj-a")
    pb = await store.get_or_create_project("/proj-b")
    a1 = await store.create_session(pa.id, "甲项目的归档")
    b1 = await store.create_session(pb.id, "乙项目的归档")
    quick = await store.create_session(None, "快聊的归档")
    keep = await store.create_session(pa.id, "未归档的不许动")
    for sid in (a1.id, b1.id, quick.id):
        await store.set_archived(sid, True)
    return {"a1": a1.id, "b1": b1.id, "quick": quick.id, "keep": keep.id,
            "pa": pa.id, "pb": pb.id}


def test_ws_archive_list_spans_projects(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ids = asyncio.run(_seed(backend))

        ws.send_json({"id": "l1", "method": "archive.list"})
        frame = recv_until(ws, "l1")
        assert frame["ok"]
        r = frame["result"]
        got = {s["id"] for s in r["sessions"]}
        assert got == {ids["a1"], ids["b1"], ids["quick"]}, "跨项目 + 快聊都要列出，未归档不在"
        names = {p["name"] for p in r["projects"]}
        assert {"proj-a", "proj-b"} <= names
        by_id = {s["id"]: s for s in r["sessions"]}
        assert by_id[ids["quick"]]["project_id"] is None
        assert by_id[ids["a1"]]["project_id"] == ids["pa"]


def test_ws_archive_restore_batch(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ids = asyncio.run(_seed(backend))

        ws.send_json({"id": "r1", "method": "archive.restore",
                      "params": {"ids": [ids["a1"], ids["quick"], "nope"]}})
        frame = recv_until(ws, "r1")
        assert frame["ok"]
        assert frame["result"]["restored"] == 2, "不存在的 id 静默跳过"

        ws.send_json({"id": "l2", "method": "archive.list"})
        left = {s["id"] for s in recv_until(ws, "l2")["result"]["sessions"]}
        assert left == {ids["b1"]}

        store = backend.store

        async def _check():
            return (
                (await store.get_session(ids["a1"])).archived,
                (await store.get_session(ids["b1"])).archived,
            )
        assert asyncio.run(_check()) == (0, 1)


def test_ws_archive_delete_only_archived_and_delete_all(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ids = asyncio.run(_seed(backend))

        # 指定清单里混进未归档 id：只删已归档的那个
        ws.send_json({"id": "d1", "method": "archive.delete",
                      "params": {"ids": [ids["a1"], ids["keep"]]}})
        frame = recv_until(ws, "d1")
        assert frame["ok"]
        assert frame["result"]["deleted"] == 1
        store = backend.store

        async def _alive(sid):
            return (await store.get_session(sid)) is not None
        assert asyncio.run(_alive(ids["keep"])), "误传的未归档 id 必须原样保留"

        # 不带 ids = 全部已归档
        ws.send_json({"id": "d2", "method": "archive.delete", "params": {}})
        frame = recv_until(ws, "d2")
        assert frame["ok"]
        assert frame["result"]["deleted"] == 2

        ws.send_json({"id": "l3", "method": "archive.list"})
        assert recv_until(ws, "l3")["result"]["sessions"] == []
        assert asyncio.run(_alive(ids["keep"])), "清空归档不得波及未归档会话"


def test_ws_archive_restore_rejects_missing_ids(home):
    frame = call(home, "archive.restore", {"ids": "不是数组"})
    assert not frame["ok"]
    frame = call(home, "archive.delete", {"ids": 42})
    assert not frame["ok"]


# ---------- ④ 自动归档 pass ----------


def test_auto_archive_pass_respects_pin_running_and_current(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ws.send_json({"id": "n1", "method": "session.new"})
        cur = recv_until(ws, "n1")["result"]["id"]

        ws.send_json({"id": "s1", "method": "archive.save",
                      "params": {"auto_archive": True, "auto_after_days": 14}})
        assert recv_until(ws, "s1")["ok"]

        async def _seed():
            store = backend.store
            pid = (await store.get_or_create_project("/p")).id
            stale = await store.create_session(pid, "该归档的")
            stale2 = await store.create_session(pid, "在跑的")
            pinned = await store.create_session(pid, "置顶的")
            fresh = await store.create_session(pid, "还新的")
            await _age(store, {
                stale.id: 20, stale2.id: 20, pinned.id: 20, fresh.id: 1, cur: 20,
            })
            await store.set_pinned(pinned.id, True)
            return {"stale": stale.id, "stale2": stale2.id,
                    "pinned": pinned.id, "fresh": fresh.id}
        ids = asyncio.run(_seed())

        # stale2 正在运行：伪造一个「未完成的 run_task」挂进 runtimes
        backend.runtimes[ids["stale2"]] = SimpleNamespace(run_task=_NeverDone(), queue=[])
        try:
            archived = asyncio.run(backend._auto_archive_pass())
        finally:
            backend.runtimes.pop(ids["stale2"], None)
        assert archived == [ids["stale"]], "只归档超期且闲置的；置顶/在跑/当前打开/未超期都不动"

        store = backend.store

        async def _flags():
            out = {}
            for k in ("stale", "stale2", "pinned", "fresh"):
                out[k] = (await store.get_session(ids[k])).archived
            out["cur"] = (await store.get_session(cur)).archived
            return out
        assert asyncio.run(_flags()) == {
            "stale": 1, "stale2": 0, "pinned": 0, "fresh": 0, "cur": 0,
        }

        # 开关关掉后 pass 零动作（还有 stale2 超期也不归档）
        asyncio.run(backend.archive_save({"auto_archive": False}))
        assert asyncio.run(backend._auto_archive_pass()) == []


def test_auto_archive_pass_limit_caps_batch(home):
    """单轮上限：候选超过 ARCHIVE_PASS_LIMIT 时分多轮消化，不一次打满广播。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        ws.send_json({"id": "s1", "method": "archive.save",
                      "params": {"auto_archive": True, "auto_after_days": 7}})
        assert recv_until(ws, "s1")["ok"]

        async def _seed():
            store = backend.store
            pid = (await store.get_or_create_project("/p")).id
            sids = []
            for i in range(backend.ARCHIVE_PASS_LIMIT + 3):
                s = await store.create_session(pid, f"旧会话{i}")
                sids.append(s.id)
            await _age(store, {sid: 30 for sid in sids})
            return sids
        sids = asyncio.run(_seed())

        first = asyncio.run(backend._auto_archive_pass())
        assert len(first) == backend.ARCHIVE_PASS_LIMIT
        rest = asyncio.run(backend._auto_archive_pass())
        assert len(rest) == 3
        assert set(first) | set(rest) == set(sids)


# ---------- ⑤ 前端接线（锁源码里的接线存在） ----------


def test_archive_page_wired():
    html = read_static("index.html")
    assert '<li data-target="archive">归档管理</li>' in html, "设置导航缺少「归档管理」入口"
    assert 'id="settings-page-archive"' in html
    for eid in (
        "archive-auto-toggle", "archive-auto-days", "archive-auto-status",
        "btn-archive-purge", "archive-mgr-search", "archive-mgr-sort",
        "archive-mgr-project", "archive-mgr-list",
    ):
        assert f'id="{eid}"' in html, f"归档管理页缺少 #{eid}"
    assert 'src="/static/app-archive.js"' in html

    js = read_app_bundle()
    assert "async function loadArchivePage" in js
    assert 'if (target === "archive") loadArchivePage();' in js
    for m in ("archive.status", "archive.save", "archive.list", "archive.restore", "archive.delete"):
        assert f'request("{m}"' in js, f"前端没有调用 {m}"
    # 删除动作必须过确认弹窗（单发 / 整组 / 全部三处）
    assert js.count("删除后无法恢复") >= 3
    # 配置即改即存；失败回读真实配置
    assert 'document.getElementById("archive-auto-toggle").onchange = () => saveArchiveAuto()' in js

    css = read_static("app.css")
    for cls in (".archive-mgr-tools", ".archive-mgr-list", ".arch-grp-head", ".arch-row"):
        assert cls in css, f"app.css 缺少 {cls}"
