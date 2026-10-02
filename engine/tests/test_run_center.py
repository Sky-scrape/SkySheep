"""今日运行总览（automation.run_center_summary）：只读聚合的口径与接线。

聚合断言用种子 store（含跨天边界：昨天的运行不进今天）；字段缺失容忍用
缺键的替身 store 与缺预算属性的 cfg（前置版本容错）；WS 与前端接线沿用
test_wiring_m2_m3 的模式（make_client + 源码子串断言）。状态文件走
SKYSHEEP_HOME 隔离。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from conftest import read_app_bundle

from skysheep.server.backend_parts import daily_report as dr
from skysheep.server.backend_parts.automation import AutomationMixin
from skysheep.server.backend_parts.daily_report import (
    save_state as save_report_state,
)

# 用例可能从任意 cwd 启动：静态资源按本文件定位成绝对路径（conftest 同款）
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"


# ---- 种子小件与宿主 ----


def _day_window() -> tuple[float, float]:
    return dr._day_bounds(time.time())


async def _seed_cron(store, name: str, status: str, run_at: float, result: str = "",
                     *, project=None, enabled: bool = True,
                     next_run_at: float | None = None) -> dict:
    proj = project or await store.get_or_create_project("/tmp/run-center-proj")
    t = await store.add_cron_task(proj.id, name, "干活", "interval", interval_minutes=30)
    kw: dict = {"last_run_at": run_at, "last_status": status, "last_result": result,
                "enabled": 1 if enabled else 0}
    if next_run_at is not None:
        kw["next_run_at"] = next_run_at
    return await store.update_cron_task(t["id"], **kw)


def _host(store, cfg=None, project_id: int | None = None):
    """只背 run_center_summary 用到的方法，不背整个 backend（_Host 同款）。"""

    class _Host:
        run_center_summary = AutomationMixin.run_center_summary
        RUN_CENTER_RESULT_CHARS = AutomationMixin.RUN_CENTER_RESULT_CHARS

        def _cur_project_id(self):
            return self._pid

    h = _Host()
    h.store = store
    h.cfg = cfg if cfg is not None else SimpleNamespace(daily_token_budget=0)
    h._pid = project_id
    return h


# ---- 聚合口径：今日定时任务（含跨天边界） ----


async def test_summary_counts_today_cron_only(store):
    """跨天边界：昨天的成功/失败都不进今天；明细倒序、结果一句话。"""
    start, _end = _day_window()
    noon = start + 12 * 3600
    yesterday = start - 3600
    proj = await store.get_or_create_project("/tmp/run-center-proj")
    await _seed_cron(store, "顺利", "ok", noon, "完成 3 件事", project=proj)
    await _seed_cron(store, "无产出", "empty", noon + 60, project=proj)
    await _seed_cron(store, "失败", "error", noon + 120, "连接超时", project=proj)
    await _seed_cron(store, "昨天的成功", "ok", yesterday, "不进今天", project=proj)
    await _seed_cron(store, "昨天的失败", "error", yesterday, "不进今天", project=proj)

    d = await _host(store, project_id=proj.id).run_center_summary()

    assert d["cron"]["total"] == 3
    assert d["cron"]["ok"] == 2, "ok 与 empty 都算成功（与日报口径一致）"
    assert d["cron"]["error"] == 1
    names = [x["name"] for x in d["cron"]["items"]]
    assert "昨天的成功" not in names and "昨天的失败" not in names
    # 明细按时间倒序，最新在前
    assert [x["name"] for x in d["cron"]["items"]] == ["失败", "无产出", "顺利"]
    assert d["cron"]["items"][0]["result"] == "连接超时"
    assert d["cron"]["items"][2]["result"] == "完成 3 件事"


async def test_summary_pipeline_nodes_today(store):
    """流水线节点结果：今天到终态的才进（运行中/昨天完成的不算）。"""
    start, _end = _day_window()
    noon = start + 12 * 3600
    proj = await store.get_or_create_project("/tmp/run-center-proj")
    pipe = await store.add_pipeline(proj.id, "发布流水", nodes=[
        {"title": "构建", "prompt": "做"}, {"title": "审查", "prompt": "查"},
        {"title": "上传", "prompt": "传"}, {"title": "还在跑", "prompt": "等"},
    ])
    n = pipe["nodes"]
    await store.update_pipeline_node(
        n[0]["id"], status="done", result="产出 A", finished_at=noon)
    await store.update_pipeline_node(
        n[1]["id"], status="error", last_error="节点没有产出", finished_at=noon + 60)
    await store.update_pipeline_node(
        n[2]["id"], status="skipped", last_error="上游被跳过", finished_at=noon + 120)
    await store.update_pipeline_node(
        n[3]["id"], status="running", started_at=noon, finished_at=0)
    old = await store.add_pipeline(proj.id, "昨天的流水",
                                   nodes=[{"title": "旧节点", "prompt": "做"}])
    await store.update_pipeline_node(
        old["nodes"][0]["id"], status="done", result="昨天", finished_at=start - 3600)

    d = await _host(store, project_id=proj.id).run_center_summary()

    assert d["pipeline"]["total"] == 3
    assert d["pipeline"]["ok"] == 2, "done 与 skipped 算成功（同日报口径）"
    assert d["pipeline"]["error"] == 1
    assert [x["title"] for x in d["pipeline"]["items"]] == ["上传", "审查", "构建"]
    assert d["pipeline"]["items"][1]["result"] == "节点没有产出"
    assert all(x["pipeline"] == "发布流水" for x in d["pipeline"]["items"])


async def test_summary_scopes_to_current_project(store):
    """B14 同款：明细只带当前项目，不跨项目下发；无项目态为空但不报错。"""
    start, _end = _day_window()
    noon = start + 3600
    mine = await store.get_or_create_project("/tmp/run-center-mine")
    other = await store.get_or_create_project("/tmp/run-center-other")
    await _seed_cron(store, "我的任务", "ok", noon, project=mine)
    await _seed_cron(store, "别人的任务", "ok", noon, project=other)
    pipe_other = await store.add_pipeline(other.id, "别人的流水",
                                          nodes=[{"title": "节点", "prompt": "做"}])
    await store.update_pipeline_node(
        pipe_other["nodes"][0]["id"], status="done", result="x", finished_at=noon)

    d = await _host(store, project_id=mine.id).run_center_summary()
    assert [x["name"] for x in d["cron"]["items"]] == ["我的任务"]
    assert d["pipeline"]["total"] == 0

    empty = await _host(store, project_id=None).run_center_summary()
    assert empty["cron"]["total"] == 0 and empty["pipeline"]["total"] == 0


# ---- 下次调度与日报状态 ----


async def test_summary_next_run_and_report_state(store, home):
    start, _end = _day_window()
    proj = await store.get_or_create_project("/tmp/run-center-proj")
    soon = time.time() + 600
    await _seed_cron(store, "稍后跑", "ok", start + 3600,
                     project=proj, enabled=True, next_run_at=soon)
    await _seed_cron(store, "更晚", "ok", start + 3600,
                     project=proj, enabled=True, next_run_at=soon + 3600)
    await _seed_cron(store, "停用的", "", 0,
                     project=proj, enabled=False, next_run_at=time.time() - 60)

    d = await _host(store, project_id=proj.id).run_center_summary()
    assert abs(d["next_run_at"] - soon) < 1, "取启用人中最早的；停用任务不参与"
    assert d["daily_report"] == {"enabled": False, "time": "09:00", "last_sent_date": ""}

    save_report_state({"enabled": True, "time": "08:30"})
    d2 = await _host(store, project_id=proj.id).run_center_summary()
    assert d2["daily_report"]["enabled"] is True and d2["daily_report"]["time"] == "08:30"


# ---- usage 汇总与预算（含 F4 前置版本容错） ----


async def test_summary_usage_today_excludes_yesterday(store):
    start, _end = _day_window()
    await store.add_usage("s1", "p", "m", 3000, 700)  # 今天 +4000
    await store._db.execute("UPDATE usage_log SET ts = ?", (start - 3600,))
    await store._db.commit()

    d = await _host(store, project_id=1).run_center_summary()
    assert d["usage"]["today"] == 0, "把今天唯一一笔 usage 挪到昨天后，今日汇总归零"

    await store.add_usage("s1", "p", "m", 100, 50)  # 重新记今天一笔
    d2 = await _host(store, project_id=1).run_center_summary()
    assert d2["usage"]["today"] == 150


async def test_summary_usage_and_budget(store):
    await store.add_usage("s1", "p", "m", 3000, 700)  # 今天 in+out = 3700
    host = _host(store, cfg=SimpleNamespace(daily_token_budget=10_000), project_id=1)

    d = await host.run_center_summary()

    assert d["usage"]["today"] == 3700
    assert d["usage"]["budget"] == 10_000
    assert d["usage"]["remaining"] == 6_300


async def test_summary_budget_zero_means_unset(store):
    """预算未设（0 = 不限制）：不虚构「剩余」，前端隐藏预算段。"""
    await store.add_usage("s1", "p", "m", 100, 0)
    d = await _host(store, project_id=1).run_center_summary()
    assert d["usage"]["budget"] is None and d["usage"]["remaining"] is None
    assert d["usage"]["today"] == 100


async def test_summary_budget_field_missing_returns_null(store):
    """前置版本容错：配置层没有 daily_token_budget 字段（F4 未合拢）→ null。"""
    d = await _host(store, cfg=SimpleNamespace(), project_id=1).run_center_summary()
    assert d["usage"]["budget"] is None and d["usage"]["remaining"] is None


# ---- 字段缺失容忍（行缺键不崩） ----


class _PartialStore:
    """只带 run_center_summary 会调的口子，行故意缺可选键。"""

    async def list_cron_tasks(self, pid):
        # 无 name / result / next_run_at / enabled / last_status
        return [{"id": 1, "project_id": pid, "last_run_at": time.time(),
                 "last_status": "ok"}]

    async def list_pipelines(self, pid):
        # 无 name / title / result / last_error
        return [{"id": 1, "nodes": [{"status": "done",
                                     "finished_at": time.time()}]}]

    async def usage_today(self):
        return 7


async def test_summary_tolerates_missing_fields():
    host = _host(_PartialStore(), cfg=SimpleNamespace(daily_token_budget=50), project_id=3)
    d = await host.run_center_summary()
    assert d["cron"]["total"] == 1
    assert d["cron"]["items"][0]["name"] == "未命名任务"
    assert d["cron"]["items"][0]["result"] == ""
    assert d["pipeline"]["items"][0]["pipeline"] == "未命名流水线"
    assert d["pipeline"]["items"][0]["title"] == "未命名节点"
    assert d["usage"] == {"today": 7, "budget": 50, "remaining": 43}


async def test_summary_usage_read_failure_degrades_to_zero(store):
    """用量读不到按 0 展示：总览不能整卡崩。"""

    class _BrokenUsageStore:
        async def list_cron_tasks(self, pid):
            return []

        async def list_pipelines(self, pid):
            return []

        async def usage_today(self):
            raise RuntimeError("库坏了")

    d = await _host(_BrokenUsageStore(), project_id=1).run_center_summary()
    assert d["usage"]["today"] == 0 and d["usage"]["remaining"] is None


# ---- WS 接线 ----


def test_run_center_ws_roundtrip(home):
    """协议接通：空库返回五段齐全的总览，今日计数为 0。"""
    from test_server import make_client, recv_until

    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "rc", "method": "automation.run_center_summary", "params": {}})
        r = recv_until(ws, "rc")
        assert r["ok"], r.get("error")
        d = r["result"]
        assert d["cron"] == {"total": 0, "ok": 0, "error": 0, "items": []}
        assert d["pipeline"]["total"] == 0 and d["pipeline"]["items"] == []
        assert d["usage"]["today"] == 0
        assert d["next_run_at"] == 0
        assert d["daily_report"]["enabled"] is False


def test_run_center_ws_remote_readable(home, monkeypatch):
    """总览是只读聚合且明细已按当前项目过滤：远端可看（不设 local_only）。"""
    from test_server import make_client, recv_until

    from skysheep.server import app as server_app

    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "rc", "method": "automation.run_center_summary", "params": {}})
        r = recv_until(ws, "rc")
        assert r["ok"], r.get("error")
        assert "cron" in r["result"] and "usage" in r["result"]


# ---- 前端接线 ----


def test_run_center_frontend_wiring():
    js = read_app_bundle()
    assert "automation.run_center_summary" in js
    assert "async function loadRunCenter" in js
    assert "function renderRunCenter" in js
    # 刷新挂点：定时任务与流水线加载时顺手刷新（定义处 + 两个调用点）
    assert js.count("loadRunCenter()") >= 3
    # 请求失败保持初始 hidden（旧后端不报错打扰），展开状态跨重拉保持
    assert "runCenterOpen" in js

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for anchor in ("run-center-card", "run-center-lines", "run-center-detail",
                   "run-center-detail-btn", "run-center-line-tasks",
                   "run-center-line-usage", "run-center-line-next",
                   "run-center-line-report"):
        assert f'id="{anchor}"' in html, f"index.html 缺 #{anchor}"

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".rp-runcard" in css
    assert "#run-center-detail" in css and ".rc-list" in css
