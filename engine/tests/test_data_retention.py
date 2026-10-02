"""数据保留策略：只进不出目录的过期清理（截图副本 / 隔离区 / 子代理报告）。

锁五层：① 清理语义（过期删、新文件留、0 关闭、目录缺失静默跳过、只碰
已知目录里引擎自己写入的文件形状）；② 配置解析（days 回读、损坏配置兜底、
[retention] 写入与校验）；③ 防重状态（当天去重、跨天再跑、原子写状态文件）；
④ WS 方法（retention.status / retention.save / retention.sweep 的读写回环与
非法值拒绝）；⑤ 前端接线（数据管理卡片、回调与切页钩子，test_frontend_wiring
模式）。全部走 SKYSHEEP_HOME 隔离（home fixture），不碰真实 ~/.skysheep。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import read_app_bundle
from test_server import make_client, recv_until

from skysheep.config import (
    ConfigError,
    RetentionConfig,
    SkySheepConfig,
    load_config,
    set_retention_in_config,
)
from skysheep.server.backend_parts import data_retention as dr
from skysheep.server.backend_parts.data_retention import (
    DEFAULT_DAYS,
    days_from,
    load_state,
    resolve_days,
    run_retention_pass,
    save_state,
    sweep_all,
    today_str,
    usage_all,
)

# 用例可能从任意 cwd 启动（仓库根 / engine/），静态资源一律按本文件定位成
# 绝对路径；与 test_frontend_wiring.py 的写法同源。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"

FULL_DAYS = dict(DEFAULT_DAYS)  # 30/30/30，全部启用


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def age_file(path: Path, days_ago: float) -> None:
    """把文件 mtime 拨回 N 天前（清理按 mtime 判过期）。"""
    old = time.time() - days_ago * 86400
    os.utime(path, (old, old))


def day_start(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time())
    return datetime(dt.year, dt.month, dt.day).timestamp()


def write(path: Path, content: bytes = b"x" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------- ① 清理语义 ----------


def test_sweep_deletes_expired_keeps_fresh_and_foreign_files(home):
    shots = home / "home" / "screenshots"
    old = write(shots / "20260901_090000_abc123.png", b"o" * 300)
    fresh = write(shots / "20261002_090000_def456.png")
    foreign = write(shots / "user-notes.txt")  # 非引擎写入形状，不碰
    age_file(old, 40)
    age_file(foreign, 40)

    res = sweep_all(FULL_DAYS, [])

    assert old.exists() is False, "40 天前的截图副本应被清理"
    assert fresh.exists() and foreign.exists(), "新截图与非 png 文件必须原样保留"
    assert res["screenshots"]["deleted"] == 1
    assert res["screenshots"]["bytes"] == 300


def test_sweep_quarantine_only_touches_dated_subdirs(home):
    q = home / "home" / "quarantine"
    old_hit = write(q / "2026-08-20" / "aaa.txt", b"q" * 50)
    fresh_hit = write(q / "2026-10-01" / "bbb.txt")
    foreign_dir = write(q / "自己建的" / "c.txt")
    loose = write(q / "loose.txt")
    for p, days in ((old_hit, 40), (foreign_dir, 40), (loose, 40)):
        age_file(p, days)

    res = sweep_all(FULL_DAYS, [])

    assert old_hit.exists() is False, "过期日期目录里的隔离文件应被清理"
    assert fresh_hit.exists(), "没过期的隔离文件必须保留"
    assert foreign_dir.exists() and loose.exists(), "非日期子目录与根下散文件不碰"
    assert res["quarantine"]["deleted"] == 1


async def test_sweep_reports_across_known_projects(home, store):
    proj_a = home / "projA"
    proj_b = home / "projB"
    proj_unknown = home / "projC"  # 没在库里登记过的项目目录，不碰
    await store.get_or_create_project(str(proj_a))
    await store.get_or_create_project(str(proj_b))
    old_report = write(proj_a / ".skysheep" / "reports" / "t1-worker.md", b"r" * 80)
    fresh_report = write(proj_b / ".skysheep" / "reports" / "t2-worker.md")
    unknown_report = write(proj_unknown / ".skysheep" / "reports" / "t3-worker.md")
    age_file(old_report, 40)
    age_file(unknown_report, 40)

    projects = await store.list_projects()
    res = sweep_all(FULL_DAYS, projects)

    assert old_report.exists() is False, "已知项目的过期报告应被清理"
    assert fresh_report.exists() and unknown_report.exists(), "新报告与未知项目目录不碰"
    assert res["reports"]["deleted"] == 1


def test_zero_days_disables_category(home):
    old = write(home / "home" / "screenshots" / "old.png")
    age_file(old, 400)

    res = sweep_all({"screenshots": 0, "quarantine": 0, "reports": 0}, [])

    assert old.exists(), "天数为 0 = 该类关闭清理，文件必须原样保留"
    assert res["screenshots"]["deleted"] == 0 and res["screenshots"]["days"] == 0


def test_missing_dirs_are_silent_skip(home):
    res = sweep_all(FULL_DAYS, [])
    assert all(r["deleted"] == 0 for r in res.values())
    assert all(r["bytes"] == 0 for r in res.values())
    # 静默跳过不建目录：清理不该把没产生过文件的目录凭空造出来
    assert not (home / "home" / "screenshots").exists()
    assert not (home / "home" / "quarantine").exists()
    assert usage_all([]) == {
        "screenshots": {"files": 0, "bytes": 0, "label": "截图副本"},
        "quarantine": {"files": 0, "bytes": 0, "label": "隔离区"},
        "reports": {"files": 0, "bytes": 0, "label": "子代理报告"},
    }


def test_sweep_days_clamped_to_boundaries(home):
    """天数越界（手改状态/配置层漏网）不炸也不误删：非法按关闭处理，超上限按上限。"""
    old = write(home / "home" / "screenshots" / "old.png")
    age_file(old, 4000)
    res = sweep_all({"screenshots": 999999}, [])
    assert old.exists() is False, "超过上限的天数夹到 3650 后，4000 天的旧文件仍过期"
    assert res["screenshots"]["days"] == 3650


# ---------- ② 配置解析 ----------


def test_days_from_defaults_and_zero(home):
    assert days_from(None) == DEFAULT_DAYS
    assert days_from(SkySheepConfig()) == DEFAULT_DAYS
    assert days_from(SkySheepConfig(retention=RetentionConfig(
        screenshots_days=0, quarantine_days=7, reports_days=3650,
    ))) == {"screenshots": 0, "quarantine": 7, "reports": 3650}


def _write_config(home: Path, text: str) -> None:
    d = home / "home"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(text, encoding="utf-8")


def test_resolve_days_falls_back_on_corrupt_config(home):
    assert resolve_days(None) == DEFAULT_DAYS  # 无配置文件 → 默认

    _write_config(home, "这不是 [ TOML")
    with pytest.raises(ConfigError):
        load_config()  # 损坏配置：load_config 本身要报错（可读指引）
    assert resolve_days(None) == DEFAULT_DAYS, "巡检不该跟着损坏配置一起炸"

    _write_config(home, '[retention]\nscreenshots_days = "abc"\n')
    with pytest.raises(ConfigError):
        load_config()
    assert resolve_days(None) == DEFAULT_DAYS


def test_load_config_reads_retention_section(home):
    _write_config(home, '[retention]\nscreenshots_days = 7\nquarantine_days = 0\n')
    cfg = load_config()
    assert cfg.retention.screenshots_days == 7
    assert cfg.retention.quarantine_days == 0
    assert cfg.retention.reports_days == 30  # 未写的键回落默认


def test_set_retention_in_config_roundtrip_and_validation(home):
    set_retention_in_config(screenshots_days=7, quarantine_days=0)
    cfg = load_config()
    assert (cfg.retention.screenshots_days, cfg.retention.quarantine_days, cfg.retention.reports_days) \
        == (7, 0, 30)

    # 部分保存不动其它键；全默认值时回落正常写入
    set_retention_in_config(reports_days=90)
    assert load_config().retention.reports_days == 90
    assert load_config().retention.screenshots_days == 7

    with pytest.raises(ConfigError):
        set_retention_in_config(screenshots_days=-1)
    with pytest.raises(ConfigError):
        set_retention_in_config(screenshots_days=4000)
    with pytest.raises(ConfigError):
        set_retention_in_config(screenshots_days=True)  # bool 冒充整数
    # 校验失败的写入不落盘
    assert load_config().retention.reports_days == 90


# ---------- ③ 防重状态与巡检 ----------


def test_state_roundtrip_and_garbage(home):
    assert not dr.state_path().exists()
    assert load_state() == {"last_sweep_date": ""}

    save_state({"last_sweep_date": "2026-10-02", "junk": 1})
    assert load_state() == {"last_sweep_date": "2026-10-02"}
    raw = json.loads(dr.state_path().read_text(encoding="utf-8"))
    assert raw == {"last_sweep_date": "2026-10-02"}, "未知字段不落盘"

    dr.state_path().write_text("不是 JSON", encoding="utf-8")
    assert load_state() == {"last_sweep_date": ""}, "坏状态文件回落「还没清过」"


async def test_pass_dedups_within_day_and_runs_next_day(home, store):
    shots = home / "home" / "screenshots"
    now = day_start() + 3600  # 当天 01:00，+86400 一定是明天

    first = await run_retention_pass(store, RetentionConfig(), now=now)
    assert first["ran"] is True
    assert load_state()["last_sweep_date"] == today_str(now)

    second = await run_retention_pass(store, RetentionConfig(), now=now + 60)
    assert second == {"ran": False, "reason": "already_ran", "today": today_str(now)}

    # 跨天：再放一个过期文件，第二天巡检应再跑一次并清掉
    old2 = write(shots / "20260902_090000_aaa.png", b"o" * 40)
    age_file(old2, 45)
    third = await run_retention_pass(store, RetentionConfig(), now=now + 86400)
    assert third["ran"] is True and third["deleted"] == 1
    assert old2.exists() is False
    assert load_state()["last_sweep_date"] == today_str(now + 86400)


async def test_pass_force_ignores_dedup_and_reports_usage(home, store):
    now = day_start() + 3600
    old = write(home / "home" / "screenshots" / "20260901_090000_aaa.png", b"o" * 60)
    age_file(old, 40)

    out = await run_retention_pass(store, RetentionConfig(), now=now, force=True)
    assert out["ran"] is True and out["deleted"] == 1 and out["bytes"] == 60
    assert out["usage"]["screenshots"]["files"] == 0, "清理后回报的是最新占用"

    # force=True 忽略当天防重：「立即清理」随时可再跑（没东西可删就不删）
    again = await run_retention_pass(store, RetentionConfig(), now=now + 60, force=True)
    assert again["ran"] is True and again["deleted"] == 0


async def test_pass_without_store_still_sweeps(home):
    write(home / "home" / "screenshots" / "20260901_090000_aaa.png")
    old = home / "home" / "screenshots" / "20260901_090000_aaa.png"
    age_file(old, 40)
    out = await run_retention_pass(None, RetentionConfig(), now=day_start() + 3600)
    assert out["ran"] is True and out["deleted"] == 1


# ---------- ④ WS 方法（读写回环 / 非法值 / 立即清理） ----------


def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": method, "params": params or {}})
        return recv_until(ws, "m1")


def test_ws_retention_status_and_save_roundtrip(home):
    frame = call(home, "retention.status")
    assert frame["ok"]
    d = frame["result"]
    assert d["days"] == DEFAULT_DAYS and d["defaults"] == DEFAULT_DAYS
    assert set(d["usage"]) == {"screenshots", "quarantine", "reports"}
    assert d["last_sweep_date"] == ""

    frame = call(home, "retention.save", {
        "screenshots_days": 7, "quarantine_days": 0, "reports_days": 3650,
    })
    assert frame["ok"]
    assert frame["result"]["days"] == {"screenshots": 7, "quarantine": 0, "reports": 3650}
    # 真实落进 config.toml 且热生效（后端 cfg 已重载，status 回读新值）
    assert load_config().retention.screenshots_days == 7
    frame = call(home, "retention.status")
    assert frame["result"]["days"]["screenshots"] == 7

    # 部分保存：只动一个键，其它保持
    frame = call(home, "retention.save", {"screenshots_days": 3})
    assert frame["ok"]
    assert frame["result"]["days"] == {"screenshots": 3, "quarantine": 0, "reports": 3650}


@pytest.mark.parametrize("bad", [-1, 4000, "abc", True])
def test_ws_retention_save_rejects_bad_days(home, bad):
    frame = call(home, "retention.save", {"screenshots_days": bad})
    assert not frame["ok"], f"非法保留天数 {bad!r} 必须被拒绝"


def test_ws_retention_save_partial_days_ok(home):
    frame = call(home, "retention.save", {"quarantine_days": 14})
    assert frame["ok"]
    assert frame["result"]["days"] == DEFAULT_DAYS | {"quarantine": 14}


def test_ws_retention_sweep_now(home):
    shots = home / "home" / "screenshots"
    old = write(shots / "20260901_090000_aaa.png", b"o" * 77)
    age_file(old, 40)

    frame = call(home, "retention.sweep")
    assert frame["ok"]
    r = frame["result"]
    assert r["ran"] is True and r["deleted"] == 1 and r["bytes"] == 77
    assert old.exists() is False
    assert load_state()["last_sweep_date"] == today_str(time.time())

    # 当天第二次立即清理照常执行（忽略防重），只是没东西可删
    frame = call(home, "retention.sweep")
    assert frame["ok"] and frame["result"]["ran"] is True
    assert frame["result"]["deleted"] == 0
    assert frame["result"]["usage"]["screenshots"]["files"] == 0


# ---------- ⑤ 前端接线（test_frontend_wiring 模式：锁源码里的接线存在） ----------


def test_retention_card_wired():
    html = read_static("index.html")
    assert "数据管理" in html
    for eid in (
        "retention-card", "btn-retention-save", "btn-retention-sweep",
        "retention-screenshots", "retention-quarantine", "retention-reports",
        "retention-status", "retention-usage-note",
    ):
        assert f'id="{eid}"' in html, f"数据管理卡片缺少 #{eid}"
    assert "0</b>" in html, "「0 = 关闭清理」的说明缺失"

    js = read_app_bundle()
    assert "async function renderRetentionCfg" in js
    assert "async function saveRetention" in js
    assert 'request("retention.status")' in js
    assert 'request("retention.save"' in js
    assert 'request("retention.sweep")' in js
    assert 'document.getElementById("btn-retention-save").onclick = saveRetention' in js
    assert 'document.getElementById("btn-retention-sweep").onclick' in js
    # 回调把三个天数字段都回传后端
    assert "screenshots_days: Number(q(\"retention-screenshots\").value)" in js
    assert "quarantine_days: Number(q(\"retention-quarantine\").value)" in js
    assert "reports_days: Number(q(\"retention-reports\").value)" in js
    # 「立即清理」回报清理量；切到关于页时拉一次状态
    assert "已清理" in js and "retentionBytes" in js
    assert "renderRetentionCfg().catch" in js
