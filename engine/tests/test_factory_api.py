"""工厂一期 · WS 接口（六个 factory.* 方法）与返回形状回归。

契约见 docs/工厂设计.md §8.3 与实施任务书：state 的形状精确到字段名，
runs 带 name / project_name；run_now / toggle / line_add 的校验与既有
cron.* 同口径；六个方法全部本机专属（local_only 的整表回归在
test_local_only_methods.py，这里只抽查拒绝文案）。
"""

from __future__ import annotations

import time

from test_server import make_client, recv_until


def _call(ws, method: str, params: dict | None = None, mid: str | None = None) -> dict:
    mid = mid or method
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    return recv_until(ws, mid)


def _line_fields() -> set[str]:
    return {
        "key", "kind", "id", "project_id", "name", "status", "trigger_text",
        "next_run_at", "last_status", "last_run_at", "running_run_id", "nodes",
    }


def test_factory_state_returns_exact_contract_shape(home):
    with make_client(home, ["完成"]) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        created = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总今天的进展", "schedule_type": "daily",
            "time_of_day": "9:05",
        })
        assert created["ok"], created
        tid = created["result"]["id"]

        state = _call(ws, "factory.state")["result"]
        assert set(state) == {"projects", "counters", "attention", "runs", "config", "now"}
        assert state["now"] > 0
        assert set(state["counters"]) == {"running", "queued", "attention", "done_today"}
        assert set(state["config"]) == {
            "enabled", "demo", "ambient", "weather",
            "history_keep", "history_days", "max_packages",
        }
        assert state["config"] == {
            "enabled": True, "demo": True, "ambient": True, "weather": False,
            "history_keep": 2000, "history_days": 30, "max_packages": 60,
        }

        assert len(state["projects"]) == 1
        proj = state["projects"][0]
        assert set(proj) == {"id", "name", "root_ok", "lines"}
        assert proj["root_ok"] is True
        assert len(proj["lines"]) == 1
        line = proj["lines"][0]
        assert set(line) == _line_fields()
        assert line["key"] == f"cron:{tid}" and line["kind"] == "cron"
        assert line["id"] == tid and line["project_id"] == proj["id"]
        assert line["name"] == "日报"
        assert line["status"] == "idle"
        assert line["trigger_text"] == "每天 09:05", "time_of_day 应被规整成 HH:MM"
        assert line["next_run_at"] > 0
        assert line["last_status"] == "" and line["running_run_id"] is None
        assert line["nodes"] == []
        assert state["runs"] == []
        assert state["counters"]["running"] == 0
        assert state["counters"]["attention"] == 0


def test_factory_run_now_records_run_and_updates_state(home):
    with make_client(home, ["这一轮的产出"]) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 30,
        })["result"]["id"]

        res = _call(ws, "factory.run_now", {"task_id": tid})
        assert res["ok"], res
        assert res["result"] == {"started": True, "id": tid}

        runs = _call(ws, "factory.runs", {"task_id": tid})["result"]["runs"]
        assert len(runs) == 1
        run = runs[0]
        assert set(run) == {
            "id", "kind", "task_id", "pipeline_id", "project_id", "session_id",
            "name", "project_name", "trigger", "status", "started_at",
            "finished_at", "duration_s", "result_excerpt", "error",
        }
        assert run["kind"] == "cron" and run["task_id"] == tid
        assert run["trigger"] == "manual" and run["status"] == "done"
        assert run["name"] == "日报" and run["project_name"] == "proj"
        assert run["session_id"], "运行结束要回填本次会话 id"
        assert run["finished_at"] >= run["started_at"]
        assert "这一轮的产出" in run["result_excerpt"]

        state = _call(ws, "factory.state")["result"]
        line = state["projects"][0]["lines"][0]
        assert line["status"] == "idle" and line["last_status"] == "ok"
        assert line["last_run_at"] > 0
        assert state["counters"]["done_today"] == 1
        assert len(state["runs"]) == 1
        assert state["runs"][0]["name"] == "日报"

        # 未知任务返回空；状态 / 条数 / 偏移过滤（同一行数据，三个维度各查一次）
        assert _call(ws, "factory.runs", {"task_id": tid + 999})["result"]["runs"] == []
        assert len(_call(ws, "factory.runs", {"status": "done"})["result"]["runs"]) == 1
        assert _call(ws, "factory.runs", {"status": "error"})["result"]["runs"] == []
        assert _call(ws, "factory.runs", {"limit": 1, "offset": 5})["result"]["runs"] == []


def test_factory_toggle_pauses_and_resumes_line(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "daily",
            "time_of_day": "09:00",
        })["result"]["id"]

        off = _call(ws, "factory.toggle", {"task_id": tid, "enabled": False})["result"]
        # 停用会顺带注销系统计划任务，响应可能多带 schtask_sync（与 cron.update 同口径）
        assert off["ok"] is True and off["id"] == tid
        assert off["enabled"] is False and off["next_run_at"] == 0
        line = _call(ws, "factory.state")["result"]["projects"][0]["lines"][0]
        assert line["status"] == "paused"

        on = _call(ws, "factory.toggle", {"task_id": tid, "enabled": True})["result"]
        assert on["ok"] is True and on["enabled"] is True and on["next_run_at"] > 0
        line = _call(ws, "factory.state")["result"]["projects"][0]["lines"][0]
        assert line["status"] == "idle"

        missing = _call(ws, "factory.toggle", {"task_id": 99999, "enabled": True})
        assert missing["ok"] is False and "任务不存在" in missing["error"]


def test_factory_line_add_validates_and_creates_task(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]

        ok = _call(ws, "factory.line_add", {
            "project_id": pid, "name": "轻量产线", "prompt": "整理待办",
            "schedule_type": "weekly", "time_of_day": "8:00", "weekday": 0,
            "allowed_tools": ["read_file", " read_file ", "", "list_dir"],
        })
        assert ok["ok"], ok
        assert ok["result"]["ok"] is True and ok["result"]["id"]

        tasks = _call(ws, "cron.list")["result"]["tasks"]
        added = next(t for t in tasks if t["id"] == ok["result"]["id"])
        assert added["name"] == "轻量产线"
        assert added["weekday"] == 0, "weekday=0（周一）是合法值"
        assert added["time_of_day"] == "08:00"
        assert added["allowed_tools"] == ["read_file", "read_file", "list_dir"]
        assert added["next_run_at"] > 0

        # 校验与 cron.add 同口径
        bad_type = _call(ws, "factory.line_add", {
            "project_id": pid, "name": "x", "prompt": "p", "schedule_type": "hourly"})
        assert bad_type["ok"] is False and "schedule_type" in bad_type["error"]
        bad_tod = _call(ws, "factory.line_add", {
            "project_id": pid, "name": "x", "prompt": "p",
            "schedule_type": "daily", "time_of_day": "25:99"})
        assert bad_tod["ok"] is False and "24 小时制" in bad_tod["error"]
        no_prompt = _call(ws, "factory.line_add", {
            "project_id": pid, "name": "x", "prompt": "  ", "schedule_type": "interval"})
        assert no_prompt["ok"] is False and "任务指令不能为空" in no_prompt["error"]
        # 目标厂区不存在：明确拒绝，不落库
        ghost = _call(ws, "factory.line_add", {
            "project_id": 99999, "name": "x", "prompt": "p",
            "schedule_type": "interval"})
        assert ghost["ok"] is False
        assert "目标厂区不可用（项目目录不存在）" in ghost["error"]

        # 项目行还在、目录已被搬走：同样 fail closed（不能把产线建到幽灵目录）
        backend = client.app.state.backend
        gone = client.portal.call(
            backend.store.get_or_create_project, str(home / "已搬走的厂"), "搬走厂")
        gone_res = _call(ws, "factory.line_add", {
            "project_id": gone.id, "name": "x", "prompt": "p",
            "schedule_type": "interval"})
        assert gone_res["ok"] is False
        assert "目标厂区不可用（项目目录不存在）" in gone_res["error"]
        assert len(_call(ws, "cron.list")["result"]["tasks"]) == 1


def test_factory_run_now_rejects_missing_project_dir(home):
    with make_client(home, ["完成"]) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        proj_dir = home / "proj"
        moved = home / "proj-moved"
        proj_dir.rename(moved)
        try:
            res = _call(ws, "factory.run_now", {"task_id": tid})
        finally:
            moved.rename(proj_dir)
        assert res["ok"] is False
        assert "目标厂区不可用（项目目录不存在）" in res["error"]
        assert _call(ws, "factory.runs", {"task_id": tid})["result"]["runs"] == []

        # 项目目录恢复后同一任务仍可派工
        res = _call(ws, "factory.run_now", {"task_id": tid})
        assert res["ok"] and res["result"]["started"] is True


def test_factory_run_now_from_other_project_uses_task_project(home):
    """在项目 B 的界面给项目 A 的产线派工：运行与会话都归属 A。

    工厂是跨项目观战台，但执行上下文不能跟着「当前项目」走——否则 B 的
    工作目录与白名单会套到 A 的任务上（_cron_execution_context 的既有语义）。
    """
    with make_client(home, ["A 的产出"]) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        pid_a = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]

        b_dir = home / "proj-b"
        b_dir.mkdir()
        switched = _call(ws, "project.switch", {"path": str(b_dir)})
        assert switched["ok"], switched
        assert switched["result"]["switched"] is True
        backend = client.app.state.backend
        pid_b = backend.project.id
        assert pid_b != pid_a

        res = _call(ws, "factory.run_now", {"task_id": tid})
        assert res["ok"], res
        assert res["result"]["started"] is True

        runs = _call(ws, "factory.runs", {"task_id": tid})["result"]["runs"]
        assert len(runs) == 1
        run = runs[0]
        assert run["project_id"] == pid_a, "派工应按任务所属项目解析，不跟随当前项目"
        assert run["project_name"] == "proj"
        assert run["status"] == "done"

        # 会话也落在 A 名下：工作目录 / 白名单 / 权限门都由 A 决定
        sess = client.portal.call(backend.store.get_session, run["session_id"])
        assert sess is not None and sess.project_id == pid_a

        # B 的运行历史里查不到这条（按项目过滤）
        assert _call(ws, "factory.runs", {"project_id": pid_b})["result"]["runs"] == []
        by_name = {p["name"]: p for p in _call(ws, "factory.state")["result"]["projects"]}
        assert {p["id"] for p in by_name.values()} == {pid_a, pid_b}
        assert by_name["proj"]["lines"][0]["last_status"] == "ok"


def test_factory_run_now_reports_already_running(home):
    """在跑去重：同一工位重复派工返回 started=false + 中文提示，不产生第二行。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        backend = client.app.state.backend
        backend._cron_running.add(tid)
        try:
            res = _call(ws, "factory.run_now", {"task_id": tid})
        finally:
            backend._cron_running.discard(tid)
        assert res["ok"], res
        assert res["result"]["started"] is False
        assert res["result"]["id"] == tid
        assert "正在运行" in res["result"]["notice"]
        assert _call(ws, "factory.runs", {"task_id": tid})["result"]["runs"] == []


def test_factory_save_config_roundtrip_and_bounds(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]

        saved = _call(ws, "factory.save", {
            "demo": False, "ambient": False, "history_keep": 500,
            "history_days": 7, "max_packages": 80,
        })
        assert saved["ok"], saved
        cfg = saved["result"]["config"]
        assert cfg["demo"] is False and cfg["ambient"] is False
        assert cfg["history_keep"] == 500 and cfg["history_days"] == 7
        assert cfg["max_packages"] == 80
        assert cfg["enabled"] is True and cfg["weather"] is False, "未传的键保持原值"

        state = _call(ws, "factory.state")["result"]
        assert state["config"] == cfg, "保存后热生效，state 立即回传新配置"

        for bad, needle in (
            ({"history_keep": 50}, "100"),
            ({"history_days": 0}, "1"),
            ({"max_packages": 999}, "300"),
            ({"history_days": "abc"}, "整数"),
        ):
            res = _call(ws, "factory.save", bad)
            assert res["ok"] is False, bad
            assert needle in res["error"], (bad, res["error"])


def test_factory_state_survives_empty_project_list(home, monkeypatch):
    """无项目态（快聊）也是合法状态：state 给空 projects / 全零计数，不炸。"""
    from skysheep.server import app as server_app

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend

        async def _no_projects():
            return []

        # 只替换快照读的项目面：无项目态下工厂仍要能画出「空厂区」
        original = backend.store.list_projects
        backend.store.list_projects = _no_projects
        try:
            state = _call(ws, "factory.state")["result"]
        finally:
            backend.store.list_projects = original
        assert state["projects"] == []
        assert state["counters"]["running"] == 0
        assert state["runs"] == []
        assert server_app.LOCAL_ONLY_METHODS  # 冒烟：方法表仍可读


def test_factory_run_event_shape_is_flat_and_has_no_top_level_session_id(home):
    """factory_run 的 WS 帧：event=factory_run，data 里是 kind/phase/run。"""
    with make_client(home, ["完成"]) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        ws.send_json({"id": "rn", "method": "factory.run_now", "params": {"task_id": tid}})
        events = []
        r = recv_until(ws, "rn", events=events)
        assert r["ok"] and r["result"]["started"] is True
        factory_events = [e for e in events if e.get("event") == "factory_run"]
        # 广播走 spawn_bg（与 cron_updated 同一姿态）：结束事件可能在回执之后才到。
        # 最多再读 20 帧（事件按创建顺序排队，开始必在结束之前）。
        for _ in range(20):
            if len(factory_events) >= 2:
                break
            frame = ws.receive_json()
            if frame.get("event") == "factory_run":
                factory_events.append(frame)
        assert len(factory_events) == 2, f"开始/结束各一次：{factory_events}"
        started, finished = factory_events
        assert started["data"]["kind"] == "factory_run"
        assert started["data"]["phase"] == "started"
        assert finished["data"]["phase"] == "finished"
        assert "session_id" not in started["data"], "顶层绝不能带 session_id"
        assert "session_id" not in finished["data"]
        assert finished["data"]["run"]["session_id"]
        assert finished["data"]["run"]["name"] == "日报"
        assert finished["data"]["run"]["project_name"] == "proj"
        assert started["data"]["run"]["id"] == finished["data"]["run"]["id"]


def test_factory_methods_are_local_only_with_chinese_errors():
    from skysheep.server import app as server_app

    for method in ("state", "runs", "run_now", "toggle", "line_add", "save"):
        entry = server_app._WS_METHODS[f"factory.{method}"]
        assert entry.local_only is True, f"factory.{method} 必须本机专属"
        assert "本机" in entry.local_error, f"factory.{method} 的拒绝文案要用中文说清"


def test_factory_methods_rejected_for_remote_client(home, monkeypatch):
    """远程连接调六个工厂方法一律被拒（与 archive.list 同口径的本机专属）。"""
    from starlette.testclient import TestClient

    from skysheep.models.fake import FakeProvider
    from skysheep.server import app as server_app

    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    app = server_app.create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: FakeProvider([]),
    )
    params = {
        "factory.state": {},
        "factory.runs": {},
        "factory.run_now": {"task_id": 1},
        "factory.toggle": {"task_id": 1, "enabled": True},
        "factory.line_add": {
            "project_id": 1, "name": "x", "prompt": "p", "schedule_type": "interval"},
        "factory.save": {"demo": False},
    }
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        boot = _call(ws, "boot")
        assert boot["ok"], "远程连接仍可完成 boot"
        for method, p in params.items():
            reply = _call(ws, method, p)
            assert reply["ok"] is False, f"远程客户端竟能调用 {method}"
            assert "本机" in reply["error"], (method, reply["error"])


def test_factory_state_aggregates_multiple_projects_and_stopped_zones(home):
    """跨项目聚合：三座厂区、停工牌、排队计数与在途运行各就各位。"""
    import functools

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend
        store = backend.store
        pid1 = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        t1 = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]

        # 二号厂目录存在；三号厂只有项目行、目录不存在（停工）
        p2dir = home / "proj2"
        p2dir.mkdir()
        proj2 = client.portal.call(store.get_or_create_project, str(p2dir), "二号厂")
        t2 = client.portal.call(
            store.add_cron_task, proj2.id, "夜班", "干活", "interval", 5, "", -1, [])
        proj3 = client.portal.call(
            store.get_or_create_project, str(home / "ghost"), "三号厂")
        t3 = client.portal.call(
            store.add_cron_task, proj3.id, "幽灵", "干活", "interval", 5, "", -1, [])
        pipe = client.portal.call(
            store.add_pipeline, proj2.id, "发布", [{"title": "构建", "prompt": "构建"}])
        client.portal.call(functools.partial(
            store.update_pipeline_node, pipe["nodes"][0]["id"], status="ready"))
        client.portal.call(functools.partial(
            store.add_automation_run, kind="cron", task_id=t1, project_id=pid1))

        state = _call(ws, "factory.state")["result"]
        by_name = {p["name"]: p for p in state["projects"]}
        assert set(by_name) == {"proj", "二号厂", "三号厂"}
        assert by_name["proj"]["root_ok"] is True
        assert by_name["三号厂"]["root_ok"] is False

        line1 = by_name["proj"]["lines"][0]
        assert line1["id"] == t1 and line1["status"] == "running"
        assert line1["running_run_id"] is not None

        line2 = next(x for x in by_name["二号厂"]["lines"] if x["id"] == t2["id"])
        assert line2["status"] == "idle"
        pipeline_line = next(x for x in by_name["二号厂"]["lines"] if x["kind"] == "pipeline")
        assert pipeline_line["key"] == f"pipe:{pipe['id']}"
        assert pipeline_line["status"] == "draft"
        assert pipeline_line["nodes"] == [
            {"id": pipe["nodes"][0]["id"], "title": "构建", "status": "ready"}]

        line3 = by_name["三号厂"]["lines"][0]
        assert line3["id"] == t3["id"] and line3["status"] == "stopped"

        assert state["counters"]["running"] == 1
        assert state["counters"]["queued"] == 1
        stopped = [a for a in state["attention"] if a["line_key"] == f"cron:{t3['id']}"]
        assert stopped and stopped[0]["kind"] == "stopped"
        assert stopped[0]["project_id"] == proj3.id


def test_factory_state_recency_window_and_counts(home):
    """运行账本的「今日完成」以本地 0 点为界；最近 24h 的失败进「需留意」。"""
    import os
    import sqlite3

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]

        # 直接写账本行（后端 store 的 aiosqlite 连接绑在应用事件循环上，
        # 测试线程不能借新循环复用；单独连接走参数绑定即可）
        now = time.time()
        con = sqlite3.connect(os.path.join(os.environ["SKYSHEEP_HOME"], "skysheep.db"))
        try:
            con.execute(
                "INSERT INTO automation_runs (kind, task_id, project_id, trigger, status,"
                " started_at, finished_at, duration_s, result_excerpt, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("cron", tid, pid, "schedule", "error", now - 60, now, 60.0, "", "模型超时"),
            )
            con.commit()
        finally:
            con.close()

        state = _call(ws, "factory.state")["result"]
        assert state["counters"]["done_today"] == 0
        kinds = {(a["kind"], a["line_key"]) for a in state["attention"]}
        assert ("error", f"cron:{tid}") in kinds
        assert all(a["project_id"] == pid for a in state["attention"])
        assert all(a["text"] for a in state["attention"])
