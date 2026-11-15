"""文件夹变化触发器（A2）：规格解析、扫描签名、差异判定与调度停放。

纯逻辑用 watchspec 直测；入口校验与「停放不自动到点」用 store/cron_add 的
真实路径测；轮询器本身用一个最小宿主（AutomationMixin 子类）测「变化 ⇒
next_run_at 被顶到当前、防抖窗口内不重复触发」。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from skysheep.core import watchspec
from skysheep.server.backend_parts.automation import AutomationMixin
from skysheep.session.store import SessionStore

# ---------- 规格解析 / 校验 ----------

def test_parse_spec_json_and_bare_dir():
    spec = watchspec.parse_spec('{"dir": "C:/tmp", "glob": "*.pdf", "debounce_s": 12}')
    assert (spec.directory, spec.pattern, spec.debounce_s) == ("C:/tmp", "*.pdf", 12)
    # 裸路径当目录、glob 默认 *（最省事的输入）
    bare = watchspec.parse_spec("D:/下载")
    assert (bare.directory, bare.pattern, bare.debounce_s) == ("D:/下载", "*", 5)
    # 反序列化对称
    assert watchspec.parse_spec(watchspec.dump_spec(spec)) == spec


def test_parse_spec_rejects_garbage():
    for bad in ("", '{"glob": "*.pdf"}', "{bad-json"):
        with pytest.raises(watchspec.WatchSpecError):
            watchspec.parse_spec(bad)


def test_validate_spec_requires_existing_dir_and_flat_glob(tmp_path):
    watchspec.validate_spec(watchspec.WatchSpec(directory=str(tmp_path)))
    with pytest.raises(watchspec.WatchSpecError):
        watchspec.validate_spec(watchspec.WatchSpec(directory=str(tmp_path / "nope")))
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(watchspec.WatchSpecError):
        watchspec.validate_spec(watchspec.WatchSpec(directory=str(f)))
    with pytest.raises(watchspec.WatchSpecError):
        watchspec.validate_spec(
            watchspec.WatchSpec(directory=str(tmp_path), pattern="sub/*.pdf"))


# ---------- 扫描与差异 ----------

def test_scan_and_diff_detects_new_and_changed(tmp_path):
    spec = watchspec.WatchSpec(directory=str(tmp_path), pattern="*.txt")
    first = watchspec.scan(spec)
    assert first.files == {}
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    second = watchspec.scan(spec)
    assert watchspec.diff(first.files, second.files) == ["a.txt"]
    # 内容改写（size 变）算变化
    time.sleep(0.01)
    (tmp_path / "a.txt").write_text("one-two", encoding="utf-8")
    third = watchspec.scan(spec)
    assert watchspec.diff(second.files, third.files) == ["a.txt"]
    # 没有变化时不触发
    assert watchspec.diff(third.files, watchspec.scan(spec).files) == []
    # 删除不触发
    (tmp_path / "a.txt").unlink()
    assert watchspec.diff(third.files, watchspec.scan(spec).files) == []
    # 不匹配 glob 的文件看不见
    (tmp_path / "b.pdf").write_text("p", encoding="utf-8")
    assert watchspec.scan(spec).files == {}


# ---------- 调度：watch 停放，不按时间到点 ----------

def test_compute_next_run_watch_parks():
    assert SessionStore.compute_next_run({"schedule_type": "watch"}) == 0.0


def test_cron_add_watch_validates_and_stores_spec(home):
    """WS 入口：watch 规格入库成 JSON、next_run_at 停放为 0、坏目录拒绝、
    导出系统计划任务被明示拒绝（schtasks 没有文件变化维度）。"""
    from test_server import make_client, recv_until

    watch_dir = Path(home) / "inbox"
    watch_dir.mkdir(parents=True, exist_ok=True)
    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "bad", "method": "cron.add", "params": {
            "name": "缺目录", "prompt": "整理",
            "schedule_type": "watch",
            "time_of_day": json.dumps({"dir": str(Path(home) / "missing")}),
        }})
        r_bad = recv_until(ws, "bad")
        assert not r_bad["ok"] and "目录不存在" in str(r_bad.get("error"))

        ws.send_json({"id": "ca", "method": "cron.add", "params": {
            "name": "发票", "prompt": "整理新发票",
            "schedule_type": "watch",
            "time_of_day": json.dumps({
                "dir": str(watch_dir), "glob": "*.pdf", "debounce_s": 3,
            }),
        }})
        r = recv_until(ws, "ca")
        assert r["ok"], r.get("error")
        task = r["result"]
        spec = watchspec.parse_spec(task["time_of_day"])
        assert spec.directory == str(watch_dir) and spec.pattern == "*.pdf"
        assert task["next_run_at"] == 0, "watch 任务必须停放（不按时间到点）"

        ws.send_json({"id": "ex", "method": "cron.schtask_export",
                      "params": {"id": task["id"]}})
        ex = recv_until(ws, "ex")["result"]
        assert ex["exported"] is False and "文件夹变化" in ex["notice"]


# ---------- 轮询器：变化 ⇒ 顶 next_run_at；防抖窗口内不重复 ----------

class _FakeWatchHost(AutomationMixin):
    """最小宿主：只要 store / ws_emitters / _broadcast_cron 三件。"""

    def __init__(self, store) -> None:
        self.store = store
        self.emitters_seen: list[dict] = []

    @property
    def ws_emitters(self):
        return [self._emit]

    async def _emit(self, frame: dict) -> None:
        self.emitters_seen.append(frame)

    def _broadcast_cron(self, task: dict) -> None:
        self.emitters_seen.append({"kind": "cron_updated", "task": task})


@pytest.mark.asyncio
async def test_watch_pass_pokes_next_run_once_per_debounce(tmp_path):
    store = await SessionStore(tmp_path / "t.db").connect()
    try:
        proj = await store.get_or_create_project(str(tmp_path))
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        spec = json.dumps({"dir": str(inbox), "glob": "*.txt", "debounce_s": 600})
        task = await store.add_cron_task(
            proj.id, "看夹子", "处理新文件", "watch", 1, spec, -1, [],
        )
        await store.update_cron_task(task["id"], next_run_at=0)
        host = _FakeWatchHost(store)

        # 第一轮：建立基线，目录里已有文件也不触发（启动不空跑）
        (inbox / "old.txt").write_text("x", encoding="utf-8")
        await host._watch_pass()
        assert (await store.get_cron_task(task["id"]))["next_run_at"] == 0

        # 新文件出现：防抖窗口外（last_trigger=0）⇒ 顶到当前时刻
        (inbox / "new.txt").write_text("y", encoding="utf-8")
        await host._watch_pass()
        row = await store.get_cron_task(task["id"])
        assert row["next_run_at"] > 0
        assert any(f.get("kind") == "notice" for f in host.emitters_seen)

        # 落回停放后再改一次文件：防抖窗口内（debounce=600s）不触发
        await store.update_cron_task(task["id"], next_run_at=0)
        time.sleep(0.01)
        (inbox / "new.txt").write_text("y2", encoding="utf-8")
        await host._watch_pass()
        assert (await store.get_cron_task(task["id"]))["next_run_at"] == 0
        # 记了 pending：窗口过后补触发一次，不会把这次变化丢掉
        host._watch_runtime[task["id"]]["last_trigger"] = (
            time.time() - 601
        )
        await host._watch_pass()
        assert (await store.get_cron_task(task["id"]))["next_run_at"] > 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_watch_state_cleared_for_disabled_task(tmp_path):
    store = await SessionStore(tmp_path / "t.db").connect()
    try:
        proj = await store.get_or_create_project(str(tmp_path))
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        spec = json.dumps({"dir": str(inbox), "glob": "*", "debounce_s": 0})
        task = await store.add_cron_task(
            proj.id, "看夹子", "处理", "watch", 1, spec, -1, [],
        )
        await store.update_cron_task(task["id"], next_run_at=0)
        host = _FakeWatchHost(store)
        await host._watch_pass()
        assert task["id"] in host._watch_runtime
        await store.update_cron_task(task["id"], enabled=0)
        await host._watch_pass()
        assert task["id"] not in host._watch_runtime, "停用任务的基线要清掉"
    finally:
        await store.close()


def test_watch_module_has_no_os_specific_paths():
    """规格解析不做平台假设：Windows 盘符与 POSIX 路径都能当纯字符串存。"""
    assert os.path.join("a", "b")  # 仅为显式说明本用例跨平台
    assert watchspec.parse_spec("C:\\\\data\\\\in").directory == "C:\\\\data\\\\in"
