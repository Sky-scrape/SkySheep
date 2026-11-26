"""回归测试：自动化、项目记忆与记忆地图后端优化与边界修复。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from skysheep.bgtasks import is_bg_task
from skysheep.messages import Message, TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.server.backend_parts import memory as memory_mod
from skysheep.server.backend_parts import run_inspect
from skysheep.tools import memory_embed
from skysheep.tools.memory import MAINTAIN_MIN_PROJECT_CHARS, load_maintenance_state


def _make_client(home, script=(), provider=None):
    provider = provider or FakeProvider(list(script))
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def _call(ws, method: str, params: dict | None = None, req_id: str = "1") -> dict:
    ws.send_json({"id": req_id, "method": method, "params": params or {}})
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            continue
        if frame.get("id") == req_id:
            return frame


# ---- 1. 存储层与查询优化 ----


async def test_delete_project_clears_map_digests(store, tmp_path):
    """删除项目同时清理 map_digests，避免残留孤儿摘要行。"""
    proj = await store.get_or_create_project(str(tmp_path / "p"), "地图项目")
    await store.replace_map_digests(
        proj.id,
        [{"kind": "overview", "title": "总览", "summary": "项目概况"}],
    )
    assert len(await store.list_map_digests(proj.id)) == 1

    await store.delete_project(proj.id)
    assert await store.list_map_digests(proj.id) == []
    cur = await store._db.execute(
        "SELECT COUNT(*) AS cnt FROM map_digests WHERE project_id = ?", (proj.id,)
    )
    row = await cur.fetchone()
    assert int(row["cnt"]) == 0


async def test_list_pipelines_status_filter_and_batch_nodes(store, tmp_path):
    """list_pipelines(status=...) 在 SQL 过滤状态并批量装配节点，与全量扫描等价。"""
    proj = await store.get_or_create_project(str(tmp_path / "p"), "编排项目")
    nodes = [
        {"title": "步骤一", "prompt": "a"},
        {"title": "步骤二", "prompt": "b", "depends_on": [0]},
    ]
    p_run = await store.add_pipeline(proj.id, "运行中1", nodes=nodes)
    p_done = await store.add_pipeline(proj.id, "已完成", nodes=nodes)
    p_run2 = await store.add_pipeline(proj.id, "运行中2", nodes=nodes)
    await store.update_pipeline(p_run["id"], status="running")
    await store.update_pipeline(p_done["id"], status="done")
    await store.update_pipeline(p_run2["id"], status="running")

    filtered = await store.list_pipelines(status="running")
    expected = [p for p in await store.list_pipelines() if p["status"] == "running"]
    assert filtered == expected
    assert [p["id"] for p in filtered] == [p_run2["id"], p_run["id"]]
    assert filtered[0]["nodes"][1]["depends_on"] == [filtered[0]["nodes"][0]["id"]]


async def test_map_sessions_with_stats_limit_chronological_and_exact(store, tmp_path):
    """map_sessions_with_stats(limit=N) 截取最近 N 条并按时间升序返回准确统计。"""
    proj = await store.get_or_create_project(str(tmp_path / "p"), "统计项目")
    sids: list[str] = []
    for i in range(4):
        sess = await store.create_session(proj.id, title=f"会话 {i}")
        await store._db.execute(
            "UPDATE sessions SET created_at = ? WHERE id = ?", (100.0 * (i + 1), sess.id)
        )
        sids.append(sess.id)
    await store._db.commit()

    # 给旧会话 s0 也记两条消息和用量，确认截断与聚合不会串行
    await store.append_message(sids[0], Message(role="user", content=[TextBlock(text="old")]))
    await store.add_usage(sids[0], "fake", "m", 999, 999, 999)

    await store.append_message(sids[2], Message(role="user", content=[TextBlock(text="u2")]))
    await store.append_message(sids[2], Message(role="assistant", content=[TextBlock(text="a2")]))
    await store.add_usage(sids[2], "fake", "m", 100, 50, 20)

    await store.append_message(sids[3], Message(role="user", content=[TextBlock(text="u3")]))
    await store.add_usage(sids[3], "fake", "m", 300, 40, 10)

    rows = await store.map_sessions_with_stats(proj.id, 0.0, 1000.0, limit=2)
    assert [r["id"] for r in rows] == [sids[2], sids[3]]
    assert (rows[0]["msg_count"], rows[0]["in_tokens"], rows[0]["out_tokens"]) == (2, 100, 50)
    assert (rows[1]["msg_count"], rows[1]["in_tokens"], rows[1]["out_tokens"]) == (1, 300, 40)


# ---- 2. 自动化：流水线重跑传递下游 / 缺目录隔离 / 独立 Provider 释放 / 工厂单跑去重 ----


def test_pipeline_node_rerun_resets_transitive_dep_fail_chain(home, monkeypatch):
    """A -> B -> C 传递依赖失败链在重跑 A 时全部退回 blocked，但不误伤仍受失败父节点 D 阻塞的分支。"""
    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend

        async def _noop_pass():
            return None

        monkeypatch.setattr(backend, "_pipeline_pass", _noop_pass)

        async def _check():
            pid = backend.project.id
            pipe = await backend.store.add_pipeline(
                pid,
                "传递依赖",
                nodes=[
                    {"title": "A", "prompt": "a"},
                    {"title": "D", "prompt": "d"},
                    {"title": "B", "prompt": "b", "depends_on": [0]},
                    {"title": "C", "prompt": "c", "depends_on": [1, 2]},
                ],
            )
            a, d, b, c = pipe["nodes"]
            await backend.store.update_pipeline_node(a["id"], status="error", last_error="root err")
            await backend.store.update_pipeline_node(d["id"], status="done")
            await backend.store.update_pipeline_node(
                b["id"], status="error", last_error=backend.DEP_FAIL_MARK + "A 失败",
            )
            await backend.store.update_pipeline_node(
                c["id"], status="error", last_error=backend.DEP_FAIL_MARK + "B 失败",
            )
            await backend.store.update_pipeline(pipe["id"], status="failed")

            await backend.pipeline_node_rerun({"id": a["id"]})
            pipe_after = await backend.store.get_pipeline(pipe["id"])
            statuses = {n["title"]: n["status"] for n in pipe_after["nodes"]}
            errors = {n["title"]: n["last_error"] for n in pipe_after["nodes"]}
            assert statuses == {"A": "blocked", "D": "done", "B": "blocked", "C": "blocked"}
            assert errors["B"] == "" and errors["C"] == ""

            # D 也失败时只重跑 A：B 退回 blocked，但 C 因 D 仍 error 不得提前解封
            await backend.store.update_pipeline_node(a["id"], status="error", last_error="root err")
            await backend.store.update_pipeline_node(d["id"], status="error", last_error="d err")
            await backend.store.update_pipeline_node(
                b["id"], status="error", last_error=backend.DEP_FAIL_MARK + "A 失败",
            )
            await backend.store.update_pipeline_node(
                c["id"], status="error", last_error=backend.DEP_FAIL_MARK + "上游失败",
            )
            await backend.store.update_pipeline(pipe["id"], status="failed")
            await backend.pipeline_node_rerun({"id": a["id"]})
            pipe_partial = await backend.store.get_pipeline(pipe["id"])
            statuses2 = {n["title"]: n["status"] for n in pipe_partial["nodes"]}
            assert statuses2["A"] == "blocked"
            assert statuses2["B"] == "blocked"
            assert statuses2["C"] == "error"

        client.portal.call(_check)


def test_run_pipeline_node_fails_closed_when_project_dir_missing(home):
    """目标项目目录被移除时 fail closed，绝不回落到当前打开的另一个项目目录执行。"""
    provider = FakeProvider(["不应该被执行"])
    with _make_client(home, provider=provider) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend
        proj_a_dir = home / "proj_a"
        proj_a_dir.mkdir()

        async def _check():
            proj_a = await backend.store.get_or_create_project(str(proj_a_dir), "项目A")
            pipe = await backend.store.add_pipeline(
                proj_a.id,
                "跨项目编排",
                nodes=[{"title": "检查", "prompt": "ping"}],
            )
            proj_a_dir.rmdir()
            await backend._run_pipeline_node(pipe, pipe["nodes"][0])
            node = await backend.store.get_pipeline_node(pipe["nodes"][0]["id"])
            assert node["status"] == "error"
            assert "不存在" in node["last_error"]
            assert provider.calls == []

        client.portal.call(_check)


def test_execute_cron_turn_releases_detached_provider(home, monkeypatch):
    """指定模型产线的独立 provider 无论成功还是中途失败都在 finally 释放。"""
    with _make_client(home, ["主"]) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "独立模型产线",
            "prompt": "ok",
            "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        backend = client.app.state.backend
        released: list[list[object]] = []
        orig_release = backend._release_turn_providers

        def _spy_release(providers):
            released.append(list(providers))
            return orig_release(providers)

        monkeypatch.setattr(backend, "_release_turn_providers", _spy_release)

        async def _check():
            task = await backend.store.get_cron_task(tid)
            detached1 = FakeProvider(["独立模型完成"])
            monkeypatch.setattr(backend, "_cron_line_provider", lambda _t: detached1)
            await backend._execute_cron_turn(task)
            assert any(detached1 in batch for batch in released)

            # 失败分支同样必须释放 detached2
            released.clear()
            detached2 = FakeProvider(["不会用到"])
            monkeypatch.setattr(backend, "_cron_line_provider", lambda _t: detached2)
            bad_task = dict(task, project_id=999999)
            with pytest.raises(RuntimeError):
                await backend._execute_cron_turn(bad_task)
            assert any(detached2 in batch for batch in released)

        client.portal.call(_check)


def test_factory_run_now_registers_task_and_dedups_before_start(home, monkeypatch):
    """factory.run_now 派发时立即占位 _cron_running[tid]，重入不重跑，收尾清理。"""
    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        tid = _call(ws, "cron.add", {
            "name": "并发防重",
            "prompt": "go",
            "schedule_type": "interval",
            "interval_minutes": 10,
        })["result"]["id"]
        backend = client.app.state.backend

        async def _check():
            entered = asyncio.Event()
            release = asyncio.Event()
            runs = 0

            async def _fake_run(task_id: int, *, force: bool = False):
                nonlocal runs
                cur = asyncio.current_task()
                assert backend._cron_running.get(task_id) is cur
                runs += 1
                entered.set()
                await release.wait()
                if backend._cron_running.get(task_id) is cur:
                    backend._cron_running.pop(task_id, None)

            monkeypatch.setattr(backend, "_run_cron_task", _fake_run)
            first = await backend.factory_run_now({"task_id": tid})
            assert first["started"] is True
            handle = backend._cron_running.get(tid)
            assert handle is not None and is_bg_task(handle)

            second = await backend.factory_run_now({"task_id": tid})
            assert second["started"] is False

            await entered.wait()
            release.set()
            await handle
            assert runs == 1
            assert tid not in backend._cron_running

        client.portal.call(_check)


# ---- 3. 项目记忆：动态发现 / 并发冲突 / 异步切项目隔离 ----


def test_project_instructions_discovery_and_conflict_guards(home):
    """中途新建 AGENTS.md 实时可读取；base_mtime=0.0 不得静默覆盖已新建文件；无项目态报清晰错。"""
    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        init_res = _call(ws, "project.instructions", req_id="r1")
        assert init_res["ok"] and init_res["result"]["path"] is None
        assert init_res["result"]["mtime"] == 0.0

        agents_path = home / "proj" / "AGENTS.md"
        agents_path.write_text("# 项目规则\n- 用中文回复\n", encoding="utf-8")

        reloaded = _call(ws, "project.instructions", req_id="r2")
        assert reloaded["ok"]
        assert reloaded["result"]["path"] == str(agents_path)
        assert "用中文回复" in reloaded["result"]["text"]
        assert reloaded["result"]["mtime"] > 0.0

        conflict = _call(
            ws,
            "project.save_instructions",
            {"text": "# 覆盖内容\n", "base_mtime": 0.0},
            req_id="r3",
        )
        assert conflict["ok"] is False
        assert "重读" in conflict["error"]
        assert "用中文回复" in agents_path.read_text(encoding="utf-8")

        backend = client.app.state.backend

        async def _no_project_save():
            saved_dir, saved_file = backend.working_dir, backend.instructions_file
            backend.working_dir = None
            backend.instructions_file = None
            try:
                with pytest.raises(RuntimeError, match="当前没有项目"):
                    await backend.save_instructions("content")
            finally:
                backend.working_dir, backend.instructions_file = saved_dir, saved_file

        client.portal.call(_no_project_save)


def test_maintain_memory_project_respects_midflight_project_switch(home, monkeypatch):
    """定期整理项目记忆期间切换项目，不污染新项目的 instructions_text 并按原目录记账。"""
    proj_a = home / "proj"
    proj_b = home / "proj_b"
    proj_b.mkdir()
    agents_a = proj_a / "AGENTS.md"
    old_lines = "\n".join(
        f"- 规则 {i}：请遵守开发规范与代码质量要求，修改后运行相关单元测试并保持文档同步。"
        for i in range(25)
    )
    assert len(old_lines) >= MAINTAIN_MIN_PROJECT_CHARS
    agents_a.write_text(old_lines, encoding="utf-8")
    new_lines = "\n".join(
        f"- 精简规则 {i}：改后跑测试并保持文档一致。"
        for i in range(12)
    )

    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend
        refresh_calls = 0
        orig_refresh = backend._refresh_system_prompts

        def _spy_refresh():
            nonlocal refresh_calls
            refresh_calls += 1
            return orig_refresh()

        monkeypatch.setattr(backend, "_refresh_system_prompts", _spy_refresh)

        async def _fake_stream(_provider, _prompt):
            # 模拟等待模型回复期间用户切换到了 proj_b
            backend.working_dir = proj_b
            backend.instructions_file = str(proj_b / "AGENTS.md")
            backend.instructions_text = "# B 项目规则"
            return new_lines

        monkeypatch.setattr(memory_mod, "collect_stream_text", _fake_stream)

        async def _check():
            status = await backend._maintain_memory("project", force=True)
            assert status == "changed"
            assert "精简规则 0" in agents_a.read_text(encoding="utf-8")
            # 新项目的内存状态不能被旧项目整理结果覆盖
            assert backend.instructions_text == "# B 项目规则"
            assert refresh_calls == 1
            state = load_maintenance_state()
            assert str(proj_a) in (state.get("project_last") or {})
            assert str(proj_b) not in (state.get("project_last") or {})

        client.portal.call(_check)


# ---- 4. 记忆地图 & 巡检 & 向量缓存 ----


def test_map_auto_digest_cooldown_and_deleted_project_abort(home, monkeypatch):
    """演化摘要生成失败触发冷却防止高频空转；等待模型期间项目被删则不落孤儿行。"""
    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend

        async def _check():
            await backend.map_save_config({"auto_digest": True})
            pid = backend.project.id
            for i in range(backend.MAP_AUTO_MIN_SESSIONS):
                await backend.store.create_session(pid, title=f"历史会话 {i}")

            calls = 0

            async def _bad_digest(_provider, _prompt):
                nonlocal calls
                calls += 1
                return "not valid json"

            monkeypatch.setattr(memory_mod, "collect_stream_text", _bad_digest)
            await backend._map_generate_run(pid)
            assert calls == 1
            assert pid in backend._map_auto_failed_at

            spawned: list[object] = []
            monkeypatch.setattr(memory_mod, "spawn_bg", lambda coro: (spawned.append(coro), coro.close())[0])
            await backend._map_auto_digest_tick()
            assert spawned == []  # 冷却期内不重复触发

            # 若在生成期间项目被删除，必须放弃 replace_map_digests 防止写回孤儿行
            proj2 = await backend.store.get_or_create_project(str(home / "proj_del"), "待删项目")
            await backend.store.create_session(proj2.id, title="会话1")
            await backend.store.create_session(proj2.id, title="会话2")

            valid_json = json.dumps({
                "overview": "总览",
                "phases": [{"title": "阶段一", "summary": "初始搭建", "from": 1, "to": 2}],
            }, ensure_ascii=False)

            async def _delete_during_llm(_provider, _prompt):
                await backend.store.delete_project(proj2.id)
                return valid_json

            monkeypatch.setattr(memory_mod, "collect_stream_text", _delete_during_llm)
            await backend._map_generate_run(proj2.id)
            assert await backend.store.list_map_digests(proj2.id) == []

        client.portal.call(_check)


async def test_run_inspect_rejects_demo_provider(store):
    """运行巡检遇到 demo_mode=True 的演示模型时直接跳过，不产出虚假巡检报告。"""

    for i in range(run_inspect.MIN_RUNS):
        run = await store.add_automation_run(
            kind="cron", task_id=i + 1, project_id=None, trigger="schedule",
        )
        await store.finish_automation_run(
            int(run["id"]), status="ok", duration_s=30.0,
            result_excerpt=f"run {i}", outcome="ok",
        )

    class _DemoProvider(FakeProvider):
        demo_mode = True

    demo = _DemoProvider(["不应该被当成巡检结论"])
    res = await run_inspect.inspect_runs(store, demo)
    assert res["ok"] is False
    assert res["reason"] == "no_provider"
    assert demo.calls == []


def test_memory_embed_lru_eviction(monkeypatch):
    """词条向量缓存满时按 LRU 淘汰最久未用条，而非清空整张表。"""
    monkeypatch.setattr(memory_embed, "_state", {"available": True, "checked_at": time.monotonic()})
    monkeypatch.setattr(memory_embed, "_vec_cache", {})
    monkeypatch.setattr(memory_embed, "_VEC_CACHE_MAX", 3)
    monkeypatch.setattr(
        memory_embed,
        "_http_post_json",
        lambda _url, payload: {"embedding": [float(len(payload["prompt"])), 1.0]},
    )

    assert memory_embed._embedded(["k1", "k2", "k3"]) is not None
    assert len(memory_embed._vec_cache) == 3

    # 命中 k1：把 k1 刷新为最近使用，使 k2 成为最久未用（LRU）
    assert memory_embed._embedded(["k1"]) is not None
    # 插入 k4：应只淘汰 k2，保留 k3、k1、k4
    assert memory_embed._embedded(["k4"]) is not None

    assert len(memory_embed._vec_cache) == 3
    assert memory_embed._vec_key("k1") in memory_embed._vec_cache
    assert memory_embed._vec_key("k3") in memory_embed._vec_cache
    assert memory_embed._vec_key("k4") in memory_embed._vec_cache
    assert memory_embed._vec_key("k2") not in memory_embed._vec_cache


# ---- 5. 项目记忆与记忆地图合并后行为 ----


def test_ui_save_persists_legacy_map_and_memory_views(home):
    """合并后 map 与 memory 同受 RIGHT_TAB_IDS 接纳，老偏好 right_tabs=['map'] 可持久化恢复。"""
    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        res = _call(ws, "ui.save", {
            "prefs": {"right_tabs": ["map", "memory"], "right_active": "map"},
        })
        assert res["ok"]
        assert res["result"]["prefs"]["right_tabs"] == ["map", "memory"]
        assert res["result"]["prefs"]["right_active"] == "map"

        got = _call(ws, "ui.get")["result"]["prefs"]
        assert got["right_tabs"] == ["map", "memory"]
        assert got["right_active"] == "map"


def test_merged_memory_map_ui_wiring():
    """项目记忆与记忆地图的容器合并、跨项目清理与演化→约定沉淀接线完整。"""
    from conftest import read_app_bundle

    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = read_app_bundle()
    assert 'id="rp-page-memory"' in html and 'id="rp-page-map"' not in html
    assert 'id="rp-segs-memory"' in html
    assert 'id="rp-view-memory"' in html and 'id="rp-view-map"' in html
    assert 'data-act="memory"' in html and 'data-act="map"' not in html
    assert 'data-rail-act="memory"' in html and 'data-rail-act="map"' not in html
    assert "resetMemoryMapForProject()" in js
    assert "function promoteToProjectInstructions(" in js
    assert 'data-op="to-project"' in js and 'data-op="promote"' in js


def test_map_get_local_false_omits_global_memories(home):
    """远程只读视图（local=False）返回项目演化数据时，不得附带本机跨项目 ~/.skysheep/memory.md 条目。"""
    mem_file = home / "home" / "memory.md"
    mem_file.parent.mkdir(parents=True, exist_ok=True)
    mem_file.write_text("- [2026-11-20] 本机个人习惯：优先用 uv\n", encoding="utf-8")

    with _make_client(home) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        backend = client.app.state.backend

        async def _check():
            local_res = await backend.map_get({"start_ts": 0}, local=True)
            assert len(local_res["memories"]) == 1
            remote_res = await backend.map_get({"start_ts": 0}, local=False)
            assert remote_res["memories"] == []
            assert remote_res["project"]["name"]

        client.portal.call(_check)

