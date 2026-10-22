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
from skysheep.server.backend_parts.automation import AutomationMixin, run_cron_task_core


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

    await run_cron_task_core(
        store, task["id"], execute=empty_execute, emit_run=events.append)
    rows = await store.list_automation_runs()
    assert rows[0]["status"] == "empty"
    assert rows[0]["trigger"] == "schedule", "到点触发记为 schedule"

    async def bad_execute(_task):
        raise RuntimeError("系统提示词炸了")

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
    dummy._cron_running = {task["id"]}
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
    assert row["trigger"] == "manual"
    assert row["status"] == "done"
    assert row["result_excerpt"] == "构建完成"
    assert row["session_id"], "节点运行要回填本次会话"


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
