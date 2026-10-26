"""工厂一期 · 运行账本（automation_runs）与三处埋点的回归。

覆盖契约（docs/工厂设计.md §8）：
- store 的五个方法与「running 永不清理」「finished_at 已有值不动」；
- RunLedger 的 start / finish / skipped / mark_interrupted / maybe_prune，
  以及 factory_run 事件形状（顶层绝无 session_id，run 自带 name / project_name）；
- run_cron_task_core 的首尾各写一行（ok→done、空产出→empty、异常→error，触发方式
  按 force 区分 manual / schedule）；
- _cron_pass 对「上一轮还在跑又到点」按 (task_id, next_run_at) 去重写 skipped；
- _run_pipeline_node 每次尝试恰好一行（成功 done / 失败 error），不双写。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from skysheep.core.run_ledger import RunLedger
from skysheep.messages import Message, TextBlock
from skysheep.server.backend_parts.automation import (
    AutomationMixin,
    alert_repeated_failures,
    run_cron_task_core,
)


def _runtime(text: str = "完成"):
    history = [Message.assistant([TextBlock(text=text)])] if text else []
    return SimpleNamespace(agent=SimpleNamespace(history=history))


# ---------- store：五个方法 ----------

async def test_automation_runs_roundtrip_and_caps(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(
        proj.id, "日报", "干活", "interval", interval_minutes=5)

    run = await store.add_automation_run(
        kind="cron", task_id=task["id"], project_id=proj.id, trigger="manual")
    assert run["status"] == "running"
    assert run["finished_at"] is None and run["duration_s"] is None
    assert run["result_excerpt"] == "" and run["error"] == ""

    fin = await store.finish_automation_run(
        run["id"], status="done", result_excerpt="摘" * 260,
        session_id="abc123def456")
    assert fin["status"] == "done"
    assert fin["finished_at"] is not None
    assert fin["duration_s"] is not None and fin["duration_s"] >= 0
    assert len(fin["result_excerpt"]) == 200, "结果摘要需截到 200 字"
    # 会话 id 在本项目是 TEXT（uuid4().hex[:12]），必须原样保留
    assert fin["session_id"] == "abc123def456"

    long_err = await store.finish_automation_run(
        run["id"], status="error", error="错" * 500)
    assert len(long_err["error"]) == 300, "错误摘要需截到 300 字"
    assert long_err["session_id"] == "abc123def456", "session_id 不能被 None 抹掉"

    assert await store.finish_automation_run(99999, status="done") is None


async def test_automation_runs_list_filters_order_and_paging(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(
        proj.id, "日报", "干活", "interval", interval_minutes=5)
    now = time.time()
    older = await store.add_automation_run(
        kind="cron", task_id=task["id"], project_id=proj.id, started_at=now - 10)
    newer = await store.add_automation_run(
        kind="cron", task_id=task["id"], project_id=proj.id, started_at=now)
    node = await store.add_automation_run(
        kind="pipeline_node", task_id=77, pipeline_id=3, project_id=proj.id,
        started_at=now + 10)

    rows = await store.list_automation_runs()
    assert [r["id"] for r in rows] == [node["id"], newer["id"], older["id"]]
    assert [r["id"] for r in await store.list_automation_runs(task_id=task["id"])] == [
        newer["id"], older["id"]]
    assert [r["id"] for r in await store.list_automation_runs(pipeline_id=3)] == [node["id"]]
    assert [r["id"] for r in await store.list_automation_runs(project_id=proj.id)] == [
        node["id"], newer["id"], older["id"]]
    assert await store.list_automation_runs(status="done") == []
    assert len(await store.list_automation_runs(limit=1)) == 1
    assert await store.list_automation_runs(limit=10, offset=99) == []
    # 过滤参数一律参数绑定：畸形值只会是普通字符串，不会改变 SQL 结构
    assert await store.list_automation_runs(status="done' OR '1'='1") == []


async def test_mark_interrupted_touches_only_running_and_keeps_finished_at(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    running = await store.add_automation_run(
        kind="cron", task_id=1, project_id=proj.id, status="running")
    done = await store.add_automation_run(
        kind="cron", task_id=2, project_id=proj.id, status="running")
    before = await store.finish_automation_run(done["id"], status="done")

    changed = await store.mark_interrupted_runs()
    assert changed == 1

    after_done = await store.get_automation_run(done["id"])
    assert after_done["status"] == "done"
    assert after_done["finished_at"] == before["finished_at"]
    after_running = await store.get_automation_run(running["id"])
    assert after_running["status"] == "interrupted"
    assert after_running["finished_at"] is None, "中断行不能伪造结束时间"
    assert await store.mark_interrupted_runs() == 0


async def test_prune_never_deletes_running_and_respects_keep_and_days(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    old_running = await store.add_automation_run(
        kind="cron", task_id=1, project_id=proj.id, status="running",
        started_at=time.time() - 400 * 86400)
    old_done = await store.add_automation_run(
        kind="cron", task_id=2, project_id=proj.id, status="done",
        started_at=time.time() - 400 * 86400)
    fresh = await store.add_automation_run(
        kind="cron", task_id=3, project_id=proj.id, status="done")

    removed = await store.prune_automation_runs(keep=100, days=30)
    assert removed == 1, "超过 30 天的旧行应被清理"
    left = {r["id"] for r in await store.list_automation_runs(limit=100)}
    assert old_done["id"] not in left
    assert fresh["id"] in left
    assert old_running["id"] in left, "running 行永不清理"

    # 条数上限：只留最近 keep 条（running 不计入被删集合）
    await store.add_automation_run(kind="cron", task_id=4, project_id=proj.id)
    await store.prune_automation_runs(keep=1, days=3650)
    left = {r["id"] for r in await store.list_automation_runs(limit=100)}
    assert old_running["id"] in left
    assert len(left) == 2, f"keep=1 应只留最近 1 条 + running：{left}"


async def test_automation_run_counters_aggregate(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    r1 = await store.add_automation_run(kind="cron", task_id=1, project_id=proj.id)
    skipped = await store.add_automation_run(kind="cron", task_id=2, project_id=proj.id)
    await store.finish_automation_run(skipped["id"], status="skipped")
    done = await store.add_automation_run(kind="cron", task_id=3, project_id=proj.id)
    await store.finish_automation_run(done["id"], status="done")
    stale = await store.add_automation_run(kind="cron", task_id=4, project_id=proj.id)
    await store.finish_automation_run(
        stale["id"], status="done", finished_at=time.time() - 3 * 86400)

    counters = await store.automation_run_counters(done_since=time.time() - 86400)
    assert counters["running"] == 1
    assert counters["done_today"] == 1
    assert r1["id"] < done["id"]


# ---------- RunLedger ----------

async def test_run_ledger_emits_started_and_finished_with_names(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "daily")
    events: list[dict] = []
    ledger = RunLedger(store, emit=events.append)

    run = await ledger.start("cron", task["id"], trigger="manual")
    assert run["name"] == "日报"
    assert run["project_name"] == "一号厂"
    assert events[0]["kind"] == "factory_run"
    assert events[0]["phase"] == "started"
    assert events[0]["run"]["id"] == run["id"]
    assert "session_id" not in events[0], "事件顶层绝不能带 session_id"

    fin = await ledger.finish(run["id"], status="done", result_excerpt="好",
                              session_id="abc123def456")
    assert fin["status"] == "done" and fin["session_id"] == "abc123def456"
    assert events[-1]["phase"] == "finished"
    assert "session_id" not in events[-1]
    assert events[-1]["run"]["name"] == "日报"

    # emit 抛错不能反噬记账
    ledger_bad = RunLedger(store, emit=lambda ev: (_ for _ in ()).throw(RuntimeError("x")))
    run2 = await ledger_bad.start("cron", task["id"])
    assert (await ledger_bad.finish(run2["id"], status="done"))["status"] == "done"


async def test_run_ledger_skipped_writes_finished_row(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "interval", interval_minutes=5)
    events: list[dict] = []
    ledger = RunLedger(store, emit=events.append)

    run = await ledger.skipped(task)
    assert run["status"] == "skipped"
    assert run["finished_at"] is not None and run["duration_s"] == 0.0
    assert run["name"] == "日报" and run["project_name"] == "一号厂"
    assert [e["phase"] for e in events] == ["finished"], "跳过只发结束事件"

    rows = await store.list_automation_runs(status="skipped")
    assert [r["id"] for r in rows] == [run["id"]]


async def test_run_ledger_prune_and_interrupted(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    ledger = RunLedger(store, keep=1, days=30)
    a = await store.add_automation_run(
        kind="cron", task_id=1, project_id=proj.id, status="done",
        started_at=time.time() - 400 * 86400)
    b = await store.add_automation_run(
        kind="cron", task_id=2, project_id=proj.id, status="running")
    assert await ledger.maybe_prune() == 1
    left = {r["id"] for r in await store.list_automation_runs(limit=100)}
    assert a["id"] not in left and b["id"] in left
    assert await ledger.mark_interrupted() == 1


async def test_run_ledger_prunes_every_n_starts(store, tmp_path, monkeypatch):
    """不必等满 100 次：清理挂在 start 的计数上，到点自动清历史。"""
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    old = await store.add_automation_run(
        kind="cron", task_id=1, project_id=proj.id, status="done",
        started_at=time.time() - 400 * 86400)
    ledger = RunLedger(store, keep=100, days=30)
    monkeypatch.setattr(RunLedger, "PRUNE_EVERY", 2)

    await ledger.start("cron", 1, project_id=proj.id)
    assert old["id"] in {r["id"] for r in await store.list_automation_runs(limit=100)}
    await ledger.start("cron", 1, project_id=proj.id)
    assert old["id"] not in {r["id"] for r in await store.list_automation_runs(limit=100)}


# ---------- 埋点：定时任务 ----------

async def test_run_cron_task_core_writes_done_empty_and_error_rows(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "interval", interval_minutes=10)
    events: list[dict] = []

    async def ok_execute(_task):
        return _runtime("完成"), "abc123def456", False

    final = await run_cron_task_core(
        store, task["id"], force=True, execute=ok_execute, emit_run=events.append)
    assert final["last_status"] == "ok"
    rows = await store.list_automation_runs()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["trigger"] == "manual", "force=True 记为 manual"
    assert rows[0]["session_id"] == "abc123def456"
    assert rows[0]["result_excerpt"] == "完成"
    assert [e["phase"] for e in events] == ["started", "finished"]

    async def empty_execute(_task):
        return _runtime(""), "sid-empty", False

    # 上一轮收尾把 next_run_at 重排到了未来：SEC-M17 起，非 force 触发要先
    # 到点才能通过库级认领（读到占位/未来值按「未到点」放弃），这里重新到点
    await store.update_cron_task(task["id"], next_run_at=time.time() - 1)
    await run_cron_task_core(
        store, task["id"], execute=empty_execute, emit_run=events.append)
    rows = await store.list_automation_runs()
    assert rows[0]["status"] == "empty"
    assert rows[0]["trigger"] == "schedule", "到点触发记为 schedule"

    async def bad_execute(_task):
        raise RuntimeError("系统提示词炸了")

    await store.update_cron_task(task["id"], next_run_at=time.time() - 1)
    await run_cron_task_core(
        store, task["id"], execute=bad_execute, emit_run=events.append)
    rows = await store.list_automation_runs()
    assert rows[0]["status"] == "error"
    assert "系统提示词炸了" in rows[0]["error"]

    # 任务不存在 / 已停用（非 force）：不产生运行行
    await run_cron_task_core(store, 99999, execute=ok_execute, emit_run=events.append)
    await store.update_cron_task(task["id"], enabled=0)
    await run_cron_task_core(store, task["id"], execute=ok_execute, emit_run=events.append)
    assert len(await store.list_automation_runs(limit=100)) == 3


async def test_cron_pass_records_skipped_once_per_due_batch(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "长跑", "干活", "interval", interval_minutes=5)
    await store.update_cron_task(task["id"], next_run_at=time.time() - 5)

    class _Pass(AutomationMixin):
        pass

    dummy = _Pass()
    dummy.store = store
    dummy._cron_running = {task["id"]: None}
    dummy._cron_skip_mark = {}
    dummy.run_ledger = RunLedger(store)

    await dummy._cron_pass()
    await dummy._cron_pass()
    rows = await store.list_automation_runs()
    assert len(rows) == 1, "同一到点批次只记一条 skipped"
    assert rows[0]["status"] == "skipped"
    assert rows[0]["finished_at"] is not None

    # 下一批次（next_run_at 变了）再记一条
    await store.update_cron_task(task["id"], next_run_at=time.time() - 1)
    await dummy._cron_pass()
    assert len(await store.list_automation_runs(limit=100)) == 2


# ---------- 埋点：编排节点 ----------

class _NodeRunner(AutomationMixin):
    """最小可运行的节点执行宿主：只实现 _run_pipeline_node 用到的协作面。"""

    def __init__(self, store, root: Path, *, project_id: int, fail: bool = False):
        self.store = store
        self.root = Path(root)
        self.project_id = int(project_id)
        self.fail = fail
        self.provider = object()
        self.runtimes: dict = {}
        self._pipeline_running: set[int] = set()
        self._pipeline_node_tasks: dict = {}
        self._pipeline_busy_until: dict = {}
        self._pipeline_manual_triggers: set[int] = set()
        self.run_ledger = RunLedger(store)
        self.pushed: list = []

    async def _cron_execution_context(self, _task):
        return self.project_id, self.root

    def compose_system_for(self, _workdir):
        return ""

    def _build_agent(self, **_kw):
        agent = SimpleNamespace(history=[], set_system=lambda _s: None)
        return agent

    async def _run_turn_pipeline(self, *_a, **_kw):
        if self.fail:
            raise RuntimeError("模型崩了")
        return {"stopped": False}

    def _broadcast_pipeline(self, _pipe):
        pass

    async def _write_node_result_file(self, _node, _text):
        pass

    def _spawn_pipeline_node_push(self, *args, **_kw):
        self.pushed.append(args)

    async def notify(self, _params):
        pass

    def _node_run_duration(self, _node):
        return 1.0

    async def _pipeline_kick(self):
        pass


async def _make_pipeline(store, tmp_path, name="发布"):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    pipe = await store.add_pipeline(proj.id, name, [{"title": "构建", "prompt": "构建一下"}])
    pipe = await store.update_pipeline(pipe["id"], status="running")
    return proj, pipe, pipe["nodes"][0]


async def test_pipeline_node_writes_one_done_row(store, tmp_path):
    proj, pipe, node = await _make_pipeline(store, tmp_path)
    runner = _NodeRunner(store, tmp_path / "厂区", project_id=proj.id)

    # 产出：_run_turn_pipeline 跑完后从 agent.history 取最后一条助手文本
    runtime_agent = SimpleNamespace(
        history=[Message.assistant([TextBlock(text="构建完成")])],
        set_system=lambda _system: None,
    )
    runner._build_agent = lambda **_kw: runtime_agent

    await runner._run_pipeline_node(pipe, node)
    rows = await store.list_automation_runs(pipeline_id=pipe["id"])
    assert len(rows) == 1, "一次尝试恰好一行"
    row = rows[0]
    assert row["kind"] == "pipeline_node"
    assert row["task_id"] == node["id"]
    assert row["project_id"] == proj.id
    assert row["trigger"] == "schedule"
    assert row["status"] == "done"
    assert row["result_excerpt"] == "构建完成"
    assert row["session_id"], "节点运行要回填本次会话"


async def test_pipeline_node_records_denied_tools_and_partial(store, tmp_path):
    """装配线节点和定时任务同一口径：被门控拦过 → denied_count / partial / 工具名。"""
    proj, pipe, node = await _make_pipeline(store, tmp_path, name="装配")
    runner = _NodeRunner(store, tmp_path / "厂区", project_id=proj.id)
    runtime_agent = SimpleNamespace(
        history=[Message.assistant([TextBlock(text="写不了，改成总结")])],
        set_system=lambda _system: None,
    )
    runner._build_agent = lambda **_kw: runtime_agent

    async def emit_denials(_prompt, emit, **_kw):
        for i in range(2):
            rid = f"r{i}"
            await emit({"kind": "permission_request", "request_id": rid,
                        "tool_name": "write_file" if i == 0 else "run_command"})
            await emit({"kind": "permission_resolved", "request_id": rid,
                        "decision": "deny"})
        return {"stopped": False}

    runner._run_turn_pipeline = emit_denials

    await runner._run_pipeline_node(pipe, node)
    rows = await store.list_automation_runs(pipeline_id=pipe["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "done", "状态仍是跑完，不能把 done 改成 error"
    assert row["denied_count"] == 2
    assert row["outcome"] == "partial", "被拦过就不算做成"
    assert '"write_file": 1' in row["denied_tools"]
    assert '"run_command": 1' in row["denied_tools"]


def test_factory_run_view_only_exposes_existing_result_file(tmp_path):
    """编排节点的产出文件不存在时不给按钮，避免「打开产出」打开空路径。"""
    from skysheep.server.backend_parts.factory import FactoryMixin

    root = tmp_path / "厂区"
    root.mkdir()
    run = {"id": 1, "kind": "pipeline_node", "task_id": 77, "project_id": 1}
    out = FactoryMixin._factory_run_view(
        run, {}, {77: "构建"}, {1: "一号厂"}, {1: str(root)})
    assert "result_path" not in out

    target = root / ".skysheep" / "pipeline-results" / "node-77.md"
    target.parent.mkdir(parents=True)
    target.write_text("完成", encoding="utf-8")
    out = FactoryMixin._factory_run_view(
        run, {}, {77: "构建"}, {1: "一号厂"}, {1: str(root)})
    assert out["result_path"] == str(target)


async def test_alert_repeated_failures_covers_pipeline_and_dedupes_daily(store, tmp_path):
    """装配线连续失败也走同一聚合告警；同一条线每天只推一次。"""
    proj, pipe, node = await _make_pipeline(store, tmp_path, name="发布")
    for i in range(2):
        run = await store.add_automation_run(
            kind="pipeline_node", task_id=node["id"], pipeline_id=pipe["id"],
            project_id=proj.id, trigger="manual",
            started_at=time.time() - 100 + i,
        )
        await store.finish_automation_run(
            run["id"], status="error", error=f"节点炸了 {i}")

    notices: list[dict] = []

    async def notify(payload):
        notices.append(payload)

    kwargs = dict(
        store=store, notify=notify, channels=None,
        task={"id": pipe["id"], "name": "发布"},
        error="节点炸了", threshold=2,
        kind="pipeline_node", pipeline_id=pipe["id"],
    )
    assert await alert_repeated_failures(**kwargs) is True
    assert notices and "装配线" in notices[0]["title"]
    assert "发布" in notices[0]["body"]
    assert await alert_repeated_failures(**kwargs) is False, "同一产线同一天不重复打扰"


async def test_pipeline_node_failure_writes_exactly_one_error_row(store, tmp_path):
    proj, pipe, node = await _make_pipeline(store, tmp_path, name="出事")
    runner = _NodeRunner(store, tmp_path / "厂区", project_id=proj.id, fail=True)

    await runner._run_pipeline_node(pipe, node)
    rows = await store.list_automation_runs(pipeline_id=pipe["id"])
    assert len(rows) == 1, "失败也只写一行（不能双写）"
    assert rows[0]["status"] == "error"
    assert "模型崩了" in rows[0]["error"]

    # 节点行仍是既有错误语义（不被账本改动影响）
    after = await store.get_pipeline_node(node["id"])
    assert after["status"] == "error"


async def test_pipeline_busy_precheck_does_not_create_run_row(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    sess = await store.create_session(proj.id, title="忙碌会话")
    pipe = await store.add_pipeline(
        proj.id, "续跑", [{"title": "接着干", "prompt": "继续", "kind": "session",
                          "ref_id": sess.id}])
    pipe = await store.update_pipeline(pipe["id"], status="running")
    node = pipe["nodes"][0]
    runner = _NodeRunner(store, tmp_path / "厂区", project_id=proj.id)
    runner.runtimes[sess.id] = SimpleNamespace(
        run_task=SimpleNamespace(done=lambda: False), queue=[])

    await runner._run_pipeline_node(pipe, node)
    assert await store.list_automation_runs(pipeline_id=pipe["id"]) == [], (
        "会话忙而退避重排不是一次真实尝试，不记运行行")
    after = await store.get_pipeline_node(node["id"])
    assert after["status"] == "blocked", "退避期间节点保持原状态，等下一轮重试"


# ---------- 二期：工序进度 / 单次成本 / 叫停 ----------

async def test_run_ledger_progress_emits_throttled_shape(store, tmp_path):
    """工序进度：1 次/秒节流，帧里只有运行 id 与「在跑什么」，不带会话 id。"""
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "daily")
    events: list[dict] = []
    ledger = RunLedger(store, emit=events.append)

    # 还没开跑：没有可归属的运行，不发
    assert ledger.progress("cron", task["id"], tool="shell") is False

    run = await ledger.start("cron", task["id"])
    assert ledger.progress("cron", task["id"], tool="shell", steps=1) is True
    assert ledger.progress("cron", task["id"], tool="fs.read", steps=2) is False, (
        "1 秒内的第二次进度要被节流掉")

    ledger.MIN_PROGRESS_INTERVAL = 0  # 放开节流看形状
    assert ledger.progress("cron", task["id"], tool="fs.read", steps=2) is True
    prog = [e for e in events if e["phase"] == "progress"]
    assert len(prog) == 2
    assert set(prog[-1]) == {"kind", "phase", "run"}
    assert set(prog[-1]["run"]) == {"id", "kind", "task_id", "pipeline_id", "progress"}
    assert prog[-1]["run"]["id"] == run["id"]
    assert prog[-1]["run"]["progress"]["tool"] == "fs.read"
    assert prog[-1]["run"]["progress"]["steps"] == 2
    assert prog[-1]["run"]["progress"]["at"] > 0

    await ledger.finish(run["id"], status="done")
    assert ledger.progress("cron", task["id"], tool="shell") is False, "收尾后不再推进度"


async def test_run_ledger_finish_backfills_session_tokens(store, tmp_path):
    """收尾按「会话 + 本次运行时间窗」回填 token；别的会话的用量不算进来。"""
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "daily")
    ledger = RunLedger(store)
    run = await ledger.start("cron", task["id"])

    await store.add_usage("sid-run", "fake", "m1", 1200, 300, 400)
    await store.add_usage("sid-other", "fake", "m1", 9999, 9999, 0)
    fin = await ledger.finish(run["id"], status="done", session_id="sid-run")
    assert fin["in_tokens"] == 1200
    assert fin["out_tokens"] == 300
    assert fin["cached_tokens"] == 400

    # 没有会话 / 查不到用量：写 0，不报错
    run2 = await ledger.start("cron", task["id"])
    fin2 = await ledger.finish(run2["id"], status="empty")
    assert fin2["in_tokens"] == 0 and fin2["out_tokens"] == 0


async def test_run_cron_task_core_cancel_writes_cancelled_and_reschedules(store, tmp_path):
    """叫停：写 cancelled 终态 + 重排下次运行 + 退出在跑登记表，异常继续上抛。"""
    import asyncio

    import pytest

    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "长跑", "干活", "interval", interval_minutes=10)
    started = asyncio.Event()

    async def slow_execute(_task):
        started.set()
        await asyncio.sleep(30)
        return _runtime("迟到的产出"), "sid-late", False

    events: list[dict] = []
    running: dict = {}
    handle = asyncio.create_task(run_cron_task_core(
        store, task["id"], execute=slow_execute, emit_run=events.append,
        running=running,
    ))
    await started.wait()
    assert task["id"] in running, "开跑就要登记句柄（叫停靠它）"

    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle

    row = await store.get_cron_task(task["id"])
    assert row["last_status"] == "cancelled", "叫停不是失败也不是空产出"
    assert row["next_run_at"] > 0, "叫停后照常排下一轮"
    runs = await store.list_automation_runs()
    assert len(runs) == 1 and runs[0]["status"] == "cancelled"
    assert "叫停" in runs[0]["error"]
    assert task["id"] not in running, "取消后登记表要清干净（否则这任务永远派不动）"
    assert [e["phase"] for e in events] == ["started", "finished"]


# ---------- 批次二：停工口径 / 并发闸 / 超时 / 被拦计数 / 做成判定 ----------

class _PassHost(AutomationMixin):
    """最小扫描宿主：只提供 _cron_pass 用到的协作面，绝不真的跑 Agent。"""

    def __init__(self, store, *, cap: int = 3, timeout_minutes: int = 60):
        self.store = store
        self._cron_running: dict = {}
        self._cron_skip_mark: dict = {}
        self.run_ledger = RunLedger(store)
        self.cfg = SimpleNamespace(factory=SimpleNamespace(
            max_parallel_runs=cap, run_timeout_minutes=timeout_minutes,
            alert_after_failures=0,
        ))
        self.dispatched: list[int] = []

    async def _run_cron_task(self, task_id: int, force: bool = False):
        self.dispatched.append(int(task_id))
        return None

    def _broadcast_cron(self, task: dict) -> None:
        pass


async def test_cron_pass_marks_stopped_zone_instead_of_dispatching(store, tmp_path):
    """厂区目录没了：不派工，记一条 error（归因 path）并照常排下一轮。

    以前 `due_cron_tasks` 不看目录，`_cron_execution_context` 又会回退到当前
    项目——工厂挂着「停工」牌，任务照样跑，还跑进别的项目目录。
    """
    import asyncio
    import shutil

    root = tmp_path / "厂区"
    root.mkdir(parents=True, exist_ok=True)
    proj = await store.get_or_create_project(str(root), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "interval", interval_minutes=5)
    await store.update_cron_task(task["id"], next_run_at=time.time() - 5)

    host = _PassHost(store)
    await host._cron_pass()
    await asyncio.sleep(0)   # spawn_bg 是后台起跑：让出一次循环才能看到它执行
    assert host.dispatched == [task["id"]], "目录还在时照常派工"

    # 删掉目录，再造一个到点任务
    shutil.rmtree(root)
    await store.update_cron_task(task["id"], next_run_at=time.time() - 5)
    host.dispatched.clear()
    await host._cron_pass()
    await asyncio.sleep(0)

    assert host.dispatched == [], "厂区不可用时绝不能派工（会跑进别的项目目录）"
    row = await store.get_cron_task(task["id"])
    assert row["last_status"] == "error"
    assert "厂区不可用" in (row["last_result"] or "")
    assert row["next_run_at"] > time.time(), "要重排下一轮，否则每 20 秒刷一条错误"

    runs = await store.list_automation_runs(limit=10)
    assert runs[0]["status"] == "error"
    assert runs[0]["error_kind"] == "path"
    assert runs[0]["outcome"] == "failed"


async def test_cron_pass_respects_global_parallel_cap(store, tmp_path):
    """并发闸：到点任务超过上限就排队，等下一轮扫描再接。"""
    import asyncio

    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    Path(tmp_path / "厂区").mkdir(parents=True, exist_ok=True)
    ids = []
    for i in range(4):
        t = await store.add_cron_task(proj.id, f"线{i}", "干活", "interval", interval_minutes=5)
        await store.update_cron_task(t["id"], next_run_at=time.time() - 10 + i)
        ids.append(t["id"])

    host = _PassHost(store, cap=2)
    await host._cron_pass()
    await asyncio.sleep(0)
    assert host.dispatched == ids[:2], "只接前两条（due 按 next_run_at 升序）"

    # 额度被占满后，剩下的继续留在队列里（仍是 due 状态）
    host._cron_running[ids[0]] = None
    host._cron_running[ids[1]] = None
    await host._cron_pass()
    await asyncio.sleep(0)
    assert host.dispatched == ids[:2], "没额度就不派工，也不记账"


async def test_run_cron_task_core_times_out_and_releases_slot(store, tmp_path):
    """挂住的运行按超时收尾为 error，并释放在跑登记（否则永远显示运行中）。"""
    import asyncio

    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "卡住", "干活", "interval", interval_minutes=10)
    running: dict = {}

    async def hang(_task):
        await asyncio.sleep(30)
        return _runtime("迟到的产出"), "sid-late", False

    final = await run_cron_task_core(
        store, task["id"], force=True, execute=hang, running=running,
        timeout_s=0.05,
    )
    assert final["last_status"] == "error"
    assert "超时" in final["last_result"]
    assert task["id"] not in running, "超时也必须释放占用位"

    rows = await store.list_automation_runs()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error_kind"] == "timeout"
    assert rows[0]["outcome"] == "failed" and rows[0]["outcome_source"] == "auto"


async def test_run_cron_task_core_records_denied_tools(store, tmp_path):
    """被门控拦过 → denied_count 落库，自动判定降级为 partial（不算成功）。"""
    from skysheep.server.backend_parts.factory import factory_line_health

    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "写文件", "干活", "interval", interval_minutes=10)

    async def ok_execute(_task):
        return _runtime("已跳过写文件，改用总结"), "sid-denied", False, {
            "denied": 3, "denied_tools": {"write_file": 2, "run_command": 1},
        }

    await run_cron_task_core(store, task["id"], force=True, execute=ok_execute)
    rows = await store.list_automation_runs()
    assert rows[0]["status"] == "done", "跑完了就是 done（状态不改）"
    assert rows[0]["denied_count"] == 3
    assert rows[0]["outcome"] == "partial", "被拦过就不算做成"
    health = factory_line_health(rows)[f"cron:{task['id']}"]
    assert health["success_rate"] == 0.0, "partial 不该计入成功率"
    assert health["partial"] == 1


async def test_factory_run_mark_overrides_outcome_and_health(store, tmp_path):
    """人工判定优先于自动判定：改完 outcome，成功率跟着变。"""
    from skysheep.server.backend_parts.factory import FactoryMixin, factory_line_health

    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(proj.id, "日报", "干活", "interval", interval_minutes=10)
    run = await store.add_automation_run(
        kind="cron", task_id=task["id"], project_id=proj.id, trigger="schedule")
    await store.finish_automation_run(
        run["id"], status="done", outcome="ok", outcome_source="auto")

    class _Factory(FactoryMixin):
        pass

    host = _Factory()
    host.store = store
    out = await host.factory_run_mark({"run_id": run["id"], "outcome": "failed"})
    assert out["ok"] and out["run"]["outcome"] == "failed"
    assert out["run"]["outcome_source"] == "user"
    health = factory_line_health(await store.list_automation_runs())[f"cron:{task['id']}"]
    assert health["success_rate"] == 0.0
    assert health["marked_failed"] == 1

    # 撤销标记：回到自动口径（行本身没有 outcome 了，成功率按 status 算）
    out = await host.factory_run_mark({"run_id": run["id"], "outcome": ""})
    assert out["run"]["outcome"] == "" and out["run"]["outcome_source"] == ""

    import pytest

    with pytest.raises(RuntimeError):
        await host.factory_run_mark({"run_id": run["id"], "outcome": "乱写"})
