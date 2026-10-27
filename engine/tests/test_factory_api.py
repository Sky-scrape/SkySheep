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


def _wait_runs(ws, params: dict, *, count: int = 1, timeout: float = 8.0) -> list[dict]:
    """等后台派工落地：工厂派工是后台起跑（请求立刻回执），运行行稍后才出现。

    轮询 ``factory.runs`` 直到条数到位且没有 running，比 sleep 固定时长稳。
    """
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        rows = _call(ws, "factory.runs", params)["result"]["runs"]
        if len(rows) >= count and not any(r["status"] == "running" for r in rows):
            return rows
        time.sleep(0.02)
    return rows


def _line_fields() -> set[str]:
    return {
        "key", "kind", "id", "project_id", "name", "status", "trigger_text",
        "next_run_at", "last_status", "last_run_at", "running_run_id", "nodes",
        "health",
        # 团队产线（mode=team）的只读呈现字段：cron 行两键透传 + 模板册现查
        "is_team", "team_template", "team_meta",
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
        assert set(state) == {
            "projects", "counters", "attention", "attention_total",
            "runs", "live", "config", "now",
            # 额度之外真正在等的定时任务（按到点时间升序）：前端据此按每条
            # 自己等了多久决定抖动幅度，而不是把「排队 N」画成三枚一样的包裹
            "waiting",
        }
        assert state["waiting"] == []
        assert state["now"] > 0
        assert set(state["counters"]) == {
            "running", "queued", "attention", "done_today", "tokens_today",
        }
        assert set(state["config"]) == {
            "enabled", "demo", "ambient", "weather", "weather_location",
            "history_keep", "history_days", "max_packages",
            "machine_limit", "label_limit",
            "run_timeout_minutes", "max_parallel_runs", "alert_after_failures",
        }
        assert state["config"] == {
            "enabled": True, "demo": True, "ambient": True, "weather": False,
            "weather_location": "",
            "history_keep": 2000, "history_days": 30, "max_packages": 60,
            "machine_limit": 6, "label_limit": 40,
            "run_timeout_minutes": 60, "max_parallel_runs": 3,
            "alert_after_failures": 3,
        }
        # 对话 / 子代理的只读快照：没有运行中心数据时是空壳，不是缺字段
        assert set(state["live"]) == {"chat", "subagent", "runs"}
        assert state["live"]["chat"] == 0 and state["live"]["runs"] == []

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
        assert line["health"] is None, "还没有运行就没有体检数据"
        # 普通任务：团队呈现三键在场但全空（契约对 agent 形态也稳定）
        assert line["is_team"] is False
        assert line["team_template"] == ""
        assert line["team_meta"] is None
        assert state["runs"] == []
        assert state["counters"]["running"] == 0
        assert state["counters"]["attention"] == 0
        assert state["counters"]["tokens_today"] == 0


def _save_team_template(ws, wid: str, name: str, *, members: int = 2):
    """经 WS 建一个 AI 总管团队模板（与 test_cron 同款夹具，provider 用假名——
    入口校验只看 provider 非空，不做真实构建）。"""
    ws.send_json({"id": wid, "method": "team.template_save", "params": {
        "name": name, "director_mode": "ai",
        "director": {"provider": "fa", "model": "ma"},
        "members": [{"provider": "fa", "model": "ma", "name": f"队员{i}",
                     "persona": ""} for i in range(members)],
    }})
    r = recv_until(ws, wid)
    assert r["ok"], r.get("error")


def test_factory_state_team_line_presentation(home):
    """团队产线（mode=team）在工厂里要「被看见」：cron 行两键原样透传，
    team_meta 由后端按既有团队模板册现查（成员规模 / AI 总管）；模板悬空
    （建任务后被删，§14.5 允许）时 found=False 照实标注，不臆测。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        _save_team_template(ws, "tpl", "夜班小队", members=2)
        team_id = _call(ws, "cron.add", {
            "name": "值班巡检团队", "prompt": "巡检并汇总",
            "schedule_type": "interval", "interval_minutes": 60,
            "mode": "team", "team_template": "夜班小队",
        })["result"]["id"]
        agent_id = _call(ws, "cron.add", {
            "name": "普通日报", "prompt": "汇总",
            "schedule_type": "interval", "interval_minutes": 60,
        })["result"]["id"]

        lines = {ln["id"]: ln for ln in
                 _call(ws, "factory.state")["result"]["projects"][0]["lines"]}
        team = lines[team_id]
        assert team["is_team"] is True and team["team_template"] == "夜班小队"
        assert team["team_meta"] == {
            "found": True, "members": 2, "director_ai": True, "director_model": "ma",
        }
        # 同厂区的普通任务不受影响：三键在场但全空
        agent = lines[agent_id]
        assert agent["is_team"] is False and agent["team_template"] == ""
        assert agent["team_meta"] is None

        # 模板删除（悬空引用）：team_meta.found 翻 False，team_template 原样保留
        rm = _call(ws, "team.template_remove", {"name": "夜班小队"})
        assert rm["ok"], rm
        team = next(ln for ln in
                    _call(ws, "factory.state")["result"]["projects"][0]["lines"]
                    if ln["id"] == team_id)
        assert team["is_team"] is True and team["team_template"] == "夜班小队"
        assert team["team_meta"] == {
            "found": False, "members": 0, "director_ai": False, "director_model": "",
        }


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

        # 后台派工：请求立刻回执，运行行稍后才出现
        runs = _wait_runs(ws, {"task_id": tid})
        assert len(runs) == 1
        run = runs[0]
        assert set(run) == {
            "id", "kind", "task_id", "pipeline_id", "project_id", "session_id",
            "name", "project_name", "trigger", "status", "started_at",
            "finished_at", "duration_s", "result_excerpt", "error",
            "in_tokens", "out_tokens", "cached_tokens",
            "denied_count", "denied_tools", "error_kind", "outcome", "outcome_source",
        }
        assert run["kind"] == "cron" and run["task_id"] == tid
        assert run["trigger"] == "manual" and run["status"] == "done"
        assert run["outcome"] == "ok" and run["outcome_source"] == "auto", \
            "跑完且没有被门控拦过 → 自动判定「做成」"
        assert run["denied_count"] == 0 and run["error_kind"] == ""
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
        assert len(_wait_runs(ws, {"task_id": tid})) == 1


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

        runs = _wait_runs(ws, {"task_id": tid})
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
        # 登记表是 task_id → 运行句柄（None = 拿不到句柄）；这里只验证去重语义
        backend._cron_running[tid] = None
        try:
            res = _call(ws, "factory.run_now", {"task_id": tid})
        finally:
            backend._cron_running.pop(tid, None)
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
            "weather": True, "weather_location": " 上海 ",
        })
        assert saved["ok"], saved
        cfg = saved["result"]["config"]
        assert cfg["demo"] is False and cfg["ambient"] is False
        assert cfg["history_keep"] == 500 and cfg["history_days"] == 7
        assert cfg["max_packages"] == 80
        assert cfg["weather"] is True and cfg["weather_location"] == "上海", \
            "城市名要压空白，但等首次打开天气时才联网"
        assert cfg["enabled"] is True, "未传的键保持原值"

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


def test_factory_state_counts_waiting_lines_and_live_runs(home):
    """「排队」要按并发闸算，不能只数编排节点；「对话在跑」给只读快照。

    这条锁的是两个口径：
    - 到点但没跑上的定时任务，减去空闲额度才是真的在排队（以前恒为 0）；
    - RunCenter 里在跑的对话轮 / 子代理要能在工厂里看见（以前是盲区）。
    """
    from types import SimpleNamespace

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend
        projects = ((_call(ws, "project.list")["result"]) or {}).get("projects") or []
        pid = int(projects[0]["id"])

        # 到点任务：4 条，并发上限 1 → 1 条能马上跑，3 条在排队
        async def _due(_now=None):
            return [
                {"id": 900 + i, "project_id": pid, "next_run_at": 1, "enabled": 1,
                 "name": f"线{i}"}
                for i in range(4)
            ]

        backend.store.due_cron_tasks = _due
        backend._cron_running = {}
        backend.cfg.factory.max_parallel_runs = 1

        now = time.time()
        backend.runs.running_runs = lambda: [
            SimpleNamespace(id="turn-1", kind="turn", session_id="abc123def456",
                            title="整理发布说明", state="running", detail="正在执行 read_file",
                            created_at=now - 30, updated_at=now - 2),
            SimpleNamespace(id="sub-1", kind="subagent", session_id="abc123def456",
                            title="依赖扫描", state="queued", detail="",
                            created_at=now - 90, updated_at=now - 400),
        ]

        state = _call(ws, "factory.state")["result"]
        assert state["counters"]["queued"] == 3, "4 条到点 - 1 个空闲额度 = 排队 3"
        assert state["live"]["chat"] == 1 and state["live"]["subagent"] == 1
        assert len(state["live"]["runs"]) == 2
        assert state["live"]["runs"][0]["title"] == "整理发布说明"
        # 5 分钟没有任何更新 → 疑似卡住的软判定
        stale = {r["id"]: r["stale"] for r in state["live"]["runs"]}
        assert stale["sub-1"] is True and stale["turn-1"] is False


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
        # 工序进度（phase=progress，1 次/秒节流）可能夹在开始与结束之间，
        # 这里按 phase 取首尾，不假设恰好两帧。
        for _ in range(40):
            if any(e["data"]["phase"] == "finished" for e in factory_events):
                break
            frame = ws.receive_json()
            if frame.get("event") == "factory_run":
                factory_events.append(frame)
        phases = [e["data"]["phase"] for e in factory_events]
        assert phases[0] == "started", phases
        assert phases[-1] == "finished", phases
        assert phases.count("started") == 1 and phases.count("finished") == 1, phases
        started, finished = factory_events[0], factory_events[-1]
        assert started["data"]["kind"] == "factory_run"
        for ev in factory_events:
            assert "session_id" not in ev["data"], "顶层绝不能带 session_id"
            if ev["data"]["phase"] == "progress":
                # 进度帧只带运行 id 与「在跑什么」，不带名字与结果
                assert set(ev["data"]["run"]) == {
                    "id", "kind", "task_id", "pipeline_id", "progress"}
        assert finished["data"]["run"]["session_id"]
        assert finished["data"]["run"]["name"] == "日报"
        assert finished["data"]["run"]["project_name"] == "proj"
        assert started["data"]["run"]["id"] == finished["data"]["run"]["id"]


def test_factory_methods_are_local_only_with_chinese_errors():
    from skysheep.server import app as server_app

    for method in ("state", "runs", "run_now", "toggle", "cancel", "ack", "line_add", "save"):
        entry = server_app._WS_METHODS[f"factory.{method}"]
        assert entry.local_only is True, f"factory.{method} 必须本机专属"
        assert "本机" in entry.local_error, f"factory.{method} 的拒绝文案要用中文说清"


def test_factory_methods_rejected_for_remote_client(home, monkeypatch):
    """远程连接调八个工厂方法一律被拒（与 archive.list 同口径的本机专属）。"""
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
        "factory.cancel": {"task_id": 1},
        "factory.ack": {"line_key": "cron:1"},
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
    """运行账本的「今日做成」以本地 0 点为界；最近 24h 的失败进「需留意」。"""
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


# ---------- 二期：产线体检 / 单次成本 / 需留意消化 / 叫停 ----------

def _add_run(client, *, tid, pid, status, started, duration=None, error="",
             in_tokens=0, out_tokens=0, denied_count=0, denied_tools="",
             outcome="", outcome_source="") -> dict:
    """直接落一条已结束的运行行（走 store，不经过真实模型）。"""
    import functools

    store = client.app.state.backend.store
    run = client.portal.call(functools.partial(
        store.add_automation_run, kind="cron", task_id=tid,
        project_id=pid, trigger="schedule", started_at=started))
    client.portal.call(functools.partial(
        store.finish_automation_run, run["id"], status=status,
        duration_s=duration, error=error,
        in_tokens=in_tokens, out_tokens=out_tokens,
        denied_count=denied_count, denied_tools=denied_tools,
        outcome=outcome, outcome_source=outcome_source))
    return run


def test_factory_state_health_and_tokens(home):
    """产线体检：7 天成功率 / 平均与 P95 / 最慢 / 连续失败 / 最近 N 次色条。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        now = time.time()
        # 三成功一失败：成功率 75%，耗时样本 10/20/30/45 秒
        _add_run(client, tid=tid, pid=pid, status="done", started=now - 300,
                 duration=10.0, in_tokens=1000, out_tokens=200)
        _add_run(client, tid=tid, pid=pid, status="done", started=now - 200,
                 duration=20.0, in_tokens=500, out_tokens=100)
        _add_run(client, tid=tid, pid=pid, status="done", started=now - 100,
                 duration=30.0, in_tokens=800, out_tokens=150)
        _add_run(client, tid=tid, pid=pid, status="error", started=now - 50,
                 duration=45.0, error="模型超时", in_tokens=300, out_tokens=0)
        # 7 天以外的一行不该进体检（但也不算错）
        _add_run(client, tid=tid, pid=pid, status="done", started=now - 9 * 86400,
                 duration=99.0, in_tokens=9999, out_tokens=9999)

        state = _call(ws, "factory.state")["result"]
        health = state["projects"][0]["lines"][0]["health"]
        assert health["total"] == 4, health
        assert health["done"] == 3 and health["error"] == 1
        assert health["success_rate"] == 0.75
        assert health["avg_s"] == 26.2, "耗时样本含失败的那次（10/20/30/45，保留一位小数）"
        assert health["max_s"] == 45.0
        assert health["p95_s"] == 45.0, "样本少时 P95 退化为最大值"
        assert health["streak"] == 1, "最新一次是失败"
        assert health["tail"][0] == "error" and len(health["tail"]) == 4
        # 今日 token：只算本地今天 0 点以后的行（9 天前那行不计）
        assert state["counters"]["tokens_today"] == 1000 + 200 + 500 + 100 + 800 + 150 + 300


def test_factory_health_counts_streak_and_skips_cancelled(home):
    """连续失败计数跨过 running；用户叫停不算失败，也不进成功率分母。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "巡检", "prompt": "查", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        now = time.time()
        _add_run(client, tid=tid, pid=pid, status="done", started=now - 400, duration=5.0)
        _add_run(client, tid=tid, pid=pid, status="error", started=now - 300, duration=5.0)
        _add_run(client, tid=tid, pid=pid, status="error", started=now - 200, duration=5.0)
        _add_run(client, tid=tid, pid=pid, status="cancelled", started=now - 100, duration=5.0)

        health = _call(ws, "factory.state")["result"]["projects"][0]["lines"][0]["health"]
        assert health["streak"] == 2, "连续两次失败；叫停的那轮不算失败也不打断计数"
        assert health["total"] == 3, "叫停的运行不算一次尝试"
        assert health["success_rate"] == round(1 / 3, 3)


def test_factory_partial_run_surfaces_in_attention_and_not_done_today(home):
    """跑完但被门控拦过：进需留意、面板可见、不算「今日做成」。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        now = time.time()
        _add_run(client, tid=tid, pid=pid, status="done", started=now - 30,
                 duration=12.0, denied_count=3,
                 denied_tools='{"run_command": 1, "write_file": 2}',
                 outcome="partial", outcome_source="auto")

        state = _call(ws, "factory.state")["result"]
        assert state["counters"]["done_today"] == 0, "partial 不能算进「今日做成」"
        line = state["projects"][0]["lines"][0]
        health = line["health"]
        assert health["partial"] == 1 and health["success_rate"] == 0.0
        assert health["last_outcome"] == "partial"
        assert health["tail"] == ["done"]
        assert health["tail_outcomes"] == ["partial"], "状态色条要能区分有拦截的 done"

        attn = next(a for a in state["attention"] if a["line_key"] == f"cron:{tid}")
        assert attn["kind"] == "partial"
        assert attn["project_name"] == "proj" and attn["line_name"] == "日报"
        assert "proj" in attn["text"] and "日报" in attn["text"]

        run = _call(ws, "factory.runs", {"task_id": tid})["result"]["runs"][0]
        assert run["denied_tools"] == '{"run_command": 1, "write_file": 2}'

        # 人工把这一轮改判为「算过」：成功率与今日做成跟着走
        marked = _call(ws, "factory.run_mark", {"run_id": run["id"], "outcome": "ok"})
        assert marked["ok"], marked
        state2 = _call(ws, "factory.state")["result"]
        assert state2["counters"]["done_today"] == 1
        assert state2["projects"][0]["lines"][0]["health"]["success_rate"] == 1.0


def test_factory_empty_run_counts_as_failure_without_fake_user_verdict(home):
    """空产出的自动 outcome 是 failed，但不能显示成用户「判定不算」。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "巡检", "prompt": "查", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        now = time.time()
        _add_run(client, tid=tid, pid=pid, status="empty", started=now - 30,
                 duration=5.0, outcome="failed", outcome_source="auto")

        state = _call(ws, "factory.state")["result"]
        health = state["projects"][0]["lines"][0]["health"]
        assert health["streak"] == 1, "空产出按 outcome 就是没做成，连续失败要算它"
        assert health["marked_failed"] == 0, "自动判负不是用户判负"
        assert health["last_status"] == "empty", "体检里最新一次尝试是空产出"
        attn = next(a for a in state["attention"] if a["line_key"] == f"cron:{tid}")
        assert attn["kind"] == "empty"


def test_factory_live_returns_light_snapshot(home):
    """factory.live 只回 RunCenter 的内存快照，不查库。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        res = _call(ws, "factory.live")
        assert res["ok"], res
        assert set(res["result"]) == {"live", "now"}
        assert set(res["result"]["live"]) == {"chat", "subagent", "runs"}
        assert res["result"]["live"]["chat"] == 0


def test_factory_weather_requires_opt_in_and_delegates_to_service(home):
    """默认关闭时不出网；开启并填城市后返回服务快照（这里用假服务，不联网）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        off = _call(ws, "factory.weather")
        assert off["ok"] and off["result"] == {"enabled": False, "weather": None}

        backend = client.app.state.backend

        class _FakeWeather:
            def __init__(self):
                self.calls = []

            def set_network(self, _network):
                pass

            async def get(self, city, *, force=False):
                self.calls.append((city, force))
                return {
                    "ok": True, "kind": "rain", "label": "雨",
                    "temperature_c": 19.5, "is_day": False,
                    "sunrise": "06:03", "sunset": "17:14",
                    "location": {"name": city, "country": "中国"},
                    "fetched_at": time.time(), "cached": False,
                    "stale": False, "error": "",
                }

        fake = _FakeWeather()
        backend._weather_service = fake
        saved = _call(ws, "factory.save", {
            "weather": True, "weather_location": "上海",
        })
        assert saved["ok"], saved
        out = _call(ws, "factory.weather")
        assert out["ok"] and out["result"]["enabled"] is True
        weather = out["result"]["weather"]
        assert weather["kind"] == "rain" and weather["is_day"] is False
        assert weather["sunrise"] == "06:03" and weather["location"]["name"] == "上海"
        assert fake.calls == [("上海", False)]


def test_factory_ack_hides_attention_until_next_failure(home):
    """「知道了」只压住当前这一次：更晚的失败会重新浮上来。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        now = time.time()
        _add_run(client, tid=tid, pid=pid, status="error", started=now - 120, error="超时")
        key = f"cron:{tid}"
        assert key in {a["line_key"] for a in _call(ws, "factory.state")["result"]["attention"]}

        acked = _call(ws, "factory.ack", {"line_key": key})
        assert acked["ok"] and acked["result"]["line_keys"] == [key]
        state = _call(ws, "factory.state")["result"]
        assert key not in {a["line_key"] for a in state["attention"]}
        assert state["counters"]["attention"] == 0

        # 同一产线又失败一次（更晚）：需留意重新出现
        _add_run(client, tid=tid, pid=pid, status="error", started=time.time(), error="又炸了")
        assert key in {a["line_key"] for a in _call(ws, "factory.state")["result"]["attention"]}

        bad = _call(ws, "factory.ack", {"line_key": "没有冒号"})
        assert bad["ok"] is False and "产线键" in bad["error"]


def test_factory_attention_scan_is_not_capped_at_recent_60(home):
    """24h 内的失败不会被「最近 N 条」截断：老失败照样进需留意。"""
    import os
    import sqlite3

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "日报", "prompt": "汇总", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        pid = _call(ws, "factory.state")["result"]["projects"][0]["id"]
        now = time.time()
        # 一条 23 小时前的失败 + 600 条更晚的成功：旧写法只扫最近 500 条会漏掉它
        rows = [("cron", tid, pid, "schedule", "error", now - 23 * 3600,
                 now - 23 * 3600 + 30, 30.0, "", "早就失败了")]
        rows += [("cron", tid, pid, "schedule", "done", now - 22 * 3600 + i,
                  now - 22 * 3600 + i + 1, 1.0, "ok", "")
                 for i in range(600)]
        con = sqlite3.connect(os.path.join(os.environ["SKYSHEEP_HOME"], "skysheep.db"))
        try:
            con.executemany(
                "INSERT INTO automation_runs (kind, task_id, project_id, trigger, status,"
                " started_at, finished_at, duration_s, result_excerpt, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            con.commit()
        finally:
            con.close()

        state = _call(ws, "factory.state")["result"]
        kinds = {(a["kind"], a["line_key"]) for a in state["attention"]}
        assert ("error", f"cron:{tid}") in kinds, "被截断的老失败漏了"
        assert "早就失败了" in next(
            a["text"] for a in state["attention"] if a["line_key"] == f"cron:{tid}")


def test_factory_cancel_reports_no_handle_and_rejects_request_bound_run(home):
    """叫停：没有运行 / 句柄不可取消时如实回报，不假装成功。"""
    import asyncio

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "长跑", "prompt": "干活", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        backend = client.app.state.backend

        idle = _call(ws, "factory.cancel", {"task_id": tid})
        assert idle["ok"] and idle["result"]["cancelled"] is False
        assert "没有在跑" in idle["result"]["notice"]

        # 由普通 create_task 跑起来的运行（请求内同步执行）：不给叫停
        async def _spawn_plain():
            async def _forever():
                await asyncio.sleep(30)
            return asyncio.get_running_loop().create_task(_forever())

        plain = client.portal.call(_spawn_plain)
        backend._cron_running[tid] = plain
        try:
            res = _call(ws, "factory.cancel", {"task_id": tid})
        finally:
            backend._cron_running.pop(tid, None)
            client.portal.call(lambda: plain.cancel())
        assert res["ok"] and res["result"]["cancelled"] is False
        assert "同步执行" in res["result"]["notice"]

        missing = _call(ws, "factory.cancel", {"task_id": 99999})
        assert missing["ok"] is False and "任务不存在" in missing["error"]


def test_factory_cancel_cancels_background_run(home):
    """后台派工的句柄可取消：回执 cancelled=true，协程真的被取消。"""
    import asyncio

    from skysheep.bgtasks import spawn_bg

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "长跑", "prompt": "干活", "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        backend = client.app.state.backend

        async def _spawn_bg_stub():
            async def _forever():
                await asyncio.sleep(30)
            return spawn_bg(_forever())

        task = client.portal.call(_spawn_bg_stub)
        backend._cron_running[tid] = task
        try:
            res = _call(ws, "factory.cancel", {"task_id": tid})
            assert res["ok"] and res["result"]["cancelled"] is True
            # 取消是异步投递的：让事件循环转一圈再确认
            client.portal.call(lambda: asyncio.sleep(0.05))
            assert task.cancelled() or task.done()
        finally:
            backend._cron_running.pop(tid, None)
