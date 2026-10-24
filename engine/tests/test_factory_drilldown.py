"""工厂计数明细、运行来源及导航的行为回归；数据使用测试 store。"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_factory_runs import _make_pipeline, _NodeRunner

from skysheep.messages import Message, TextBlock
from skysheep.server.backend_parts.factory import FactoryMixin

STATIC = Path(__file__).resolve().parents[1] / "src/skysheep/server/static"


async def test_today_drilldowns_match_counters_and_kind_isolation(store, tmp_path):
    class Host(FactoryMixin):
        async def _factory_name_maps(self):
            return {}, {}, {}, {}

    host = Host()
    host.store = store
    now = time.time()
    midnight = host._factory_local_midnight(now)
    proj = await store.get_or_create_project(str(tmp_path), "厂区")
    legacy = await store.add_automation_run(
        kind="cron", task_id=1, project_id=proj.id, started_at=midnight - 30)
    await store.finish_automation_run(legacy["id"], status="done", finished_at=now)
    partial = await store.add_automation_run(
        kind="cron", task_id=1, project_id=proj.id, started_at=now)
    await store.finish_automation_run(
        partial["id"], status="done", outcome="partial", finished_at=now,
        in_tokens=100, out_tokens=20)
    marked = await store.add_automation_run(
        kind="pipeline_node", task_id=1, pipeline_id=1, project_id=proj.id, started_at=now)
    await store.finish_automation_run(marked["id"], status="error", finished_at=now)
    await store.set_automation_run_outcome(marked["id"], "ok", source="user")
    yesterday = await store.add_automation_run(
        kind="cron", task_id=2, project_id=proj.id, started_at=midnight - 100)
    await store.finish_automation_run(
        yesterday["id"], status="done", outcome="ok", finished_at=midnight - 1)

    made = (await host.factory_runs({"view": "done_today"}))["runs"]
    assert {r["id"] for r in made} == {legacy["id"], marked["id"]}
    counters = await store.automation_run_counters(done_since=midnight)
    assert len(made) == counters["done_today"]
    tokens = (await host.factory_runs({"view": "tokens_today"}))["runs"]
    assert {r["id"] for r in tokens} == {partial["id"], marked["id"]}
    assert counters["tokens_today"] == sum(r["in_tokens"] + r["out_tokens"] for r in tokens) == 120
    cron = (await host.factory_runs({"task_id": 1, "kind": "cron"}))["runs"]
    assert {r["id"] for r in cron} == {legacy["id"], partial["id"]}
    first = await host.factory_runs({"view": "done_today", "limit": 1})
    second = await host.factory_runs({"view": "done_today", "limit": 1, "offset": 1})
    assert first["runs"][0]["id"] != second["runs"][0]["id"]
    with pytest.raises(RuntimeError, match="view"):
        await host.factory_runs({"view": "invalid"})
    with pytest.raises(RuntimeError, match="kind"):
        await host.factory_runs({"kind": "invalid"})
    assert await store.list_automation_runs(outcome="ok' OR 1=1 --") == []


@pytest.mark.parametrize("trigger", ["manual", "schedule"])
async def test_pipeline_attempt_records_explicit_source(store, tmp_path, trigger):
    proj, pipe, node = await _make_pipeline(store, tmp_path)
    host = _NodeRunner(store, tmp_path, project_id=proj.id)
    host._build_agent = lambda **_: SimpleNamespace(
        history=[Message.assistant([TextBlock(text="完成")])], set_system=lambda _: None)
    await host._run_pipeline_node(pipe, node, trigger=trigger)
    rows = await store.list_automation_runs(pipeline_id=pipe["id"])
    assert len(rows) == 1 and rows[0]["trigger"] == trigger


async def test_manual_trigger_survives_busy_session(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path), "厂区")
    sess = await store.create_session(proj.id)
    pipe = await store.add_pipeline(proj.id, "续跑", [
        {"title": "继续", "prompt": "继续", "kind": "session", "ref_id": sess.id},
    ])
    host = _NodeRunner(store, tmp_path, project_id=proj.id)
    host.runtimes[sess.id] = SimpleNamespace(run_task=SimpleNamespace(done=lambda: False))
    await host._run_pipeline_node(pipe, pipe["nodes"][0], trigger="manual")
    assert pipe["nodes"][0]["id"] in host._pipeline_manual_triggers
    assert await store.list_automation_runs(pipeline_id=pipe["id"]) == []


async def test_rerun_confirmation_and_cancel_clean_pending_sources(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path), "厂区")
    pipe = await store.add_pipeline(proj.id, "级联", [
        {"title": "一", "prompt": "一"}, {"title": "二", "prompt": "二", "depends_on": [0]},
    ])
    for node in pipe["nodes"]:
        await store.update_pipeline_node(node["id"], status="done")
    await store.update_pipeline(pipe["id"], status="done")
    host = _NodeRunner(store, tmp_path, project_id=proj.id)
    host._check_pipeline_ownership = lambda _: None

    async def no_scan():
        pass

    host._pipeline_pass = no_scan
    result = await host.pipeline_node_rerun({"id": pipe["nodes"][0]["id"]})
    assert result["needs_confirm"] and not host._pipeline_manual_triggers
    await host.pipeline_node_rerun({"id": pipe["nodes"][0]["id"], "cascade": True})
    assert host._pipeline_manual_triggers == {n["id"] for n in pipe["nodes"]}
    await host.pipeline_cancel({"id": pipe["id"]})
    assert not host._pipeline_manual_triggers
    host._pipeline_manual_triggers.update(n["id"] for n in pipe["nodes"])
    await host.pipeline_delete({"id": pipe["id"]})
    assert not host._pipeline_manual_triggers
    await asyncio.sleep(0)


def test_factory_list_presets_stale_response_and_pipeline_navigation(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = (STATIC / "app-factory.js").read_text(encoding="utf-8")
    source = source.replace("})();", (
        "window.fx = { S, dom, openAllRunsDrawer, loadAllRuns, openQueueDrawer, openAttentionDrawer };\n})();"
    ))
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    nav = app[app.index("async function openPipelineFor("):app.index("function closeRightTab(")]
    driver = r'''
const vm = require('vm');
const assert = require('assert');
const source = SOURCE;
const nav = NAV;
const pending = [];
let nodes = {};
const drawer = { classList: { contains: () => false, remove: () => {} },
  querySelector: s => nodes[s] || null, querySelectorAll: () => [],
  set innerHTML(html) {
    this.html = html;
    nodes = { '#fx-all-runs': { innerHTML: '', querySelectorAll: () => [] },
      '#fx-runs-status': {}, '#fx-runs-project': {},
      '#fx-runs-pager': { innerHTML: '', querySelectorAll: () => [] } };
  }
};
const ctx = { window: {}, document: {}, console, Map, Set, Date, Math, Number,
  escapeHtml: s => String(s), addNotice: s => {},
  request: (method, params) => new Promise((resolve, reject) =>
    pending.push({ method, params, resolve, reject })) };
vm.createContext(ctx);
vm.runInContext(source, ctx);
const fx = ctx.window.fx;
fx.dom.drawer = drawer;
fx.S.state = { projects: [] };
(async () => {
  fx.openAllRunsDrawer({status:'running'});
  assert.equal(nodes['#fx-runs-status'].value, 'running');
  assert.equal(pending[0].params.status, 'running');
  const old = pending.shift();
  fx.S.runsFilter.status = 'error'; fx.loadAllRuns();
  pending.shift().resolve({runs:[]}); await Promise.resolve();
  const html = nodes['#fx-all-runs'].innerHTML;
  old.resolve({runs:[{id:77, status:'done', name:'OLD'}]}); await Promise.resolve();
  assert.equal(nodes['#fx-all-runs'].innerHTML, html);
  fx.openAllRunsDrawer({view:'done_today'});
  assert.equal(pending.shift().params.view, 'done_today');
  fx.openAllRunsDrawer({view:'tokens_today'});
  assert.equal(pending.shift().params.view, 'tokens_today');
  fx.openAllRunsDrawer();
  assert.equal(nodes['#fx-runs-status'].value, '');
  assert.equal(pending.shift().params.view, 'all');
  fx.S.state = { counters: {queued: 2}, waiting: [{key:'cron:1',name:'日报',project_id:1,due_at:1}],
    projects:[{id:1,name:'厂区',lines:[{key:'pipe:2',kind:'pipeline',name:'汇总',
      nodes:[{title:'检查',status:'ready'}]}]}] };
  fx.openQueueDrawer();
  assert(drawer.html.includes('日报') && drawer.html.includes('检查') && drawer.html.includes('排队明细'));
  const calls = [];
  ctx.request = async (method, params) => {
    calls.push(method);
    if (method === 'project.list') return {projects:[{id:2,root_path:'target',is_current:false}]};
    if (method === 'pipeline.get') return {pipeline:{id:3}};
    return {};
  };
  ctx.fetchWorkspaceData = async () => ({});
  ctx.applyWorkspaceData = async () => calls.push('apply');
  ctx.backToChat = () => calls.push('chat');
  ctx.openRightTab = () => calls.push('panel');
  ctx.pipelineModal = (p, n) => { assert.equal(p.id,3); assert.equal(n,8); calls.push('modal'); };
  vm.runInContext(nav, ctx);
  assert(await ctx.openPipelineFor(2,3,8));
  assert.deepEqual(calls, ['project.list','project.switch','apply','pipeline.get','chat','panel','modal']);
  calls.length = 0;
  assert.equal(await ctx.openPipelineFor(999,3), false);
  assert.deepEqual(calls,['project.list']);
  console.log('ok');
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    driver = driver.replace("SOURCE", json.dumps(source)).replace("NAV", json.dumps(nav))
    script = tmp_path / "factory-ui.cjs"
    script.write_text(driver, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
