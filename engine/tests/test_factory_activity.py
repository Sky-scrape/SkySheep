"""真实工厂 JavaScript 的装运、巡检、限流和动态画序回归。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from skysheep.core.mods_node import find_node

STATIC = Path(__file__).resolve().parents[1] / "src/skysheep/server/static"
NODE = shutil.which("node") or find_node()

DRIVER = r"""
const assert = require('node:assert/strict');
const fx = window.FX, S = fx.S;
const projects = Array.from({length: 5}, (_, i) => ({
  id: i + 1, name: '测试厂' + i, root_ok: true,
  lines: [{key: 'cron:' + i, kind: 'cron', id: i, status: 'idle'}]
}));
S.state = {projects, runs: [], counters: {}, config: {}};
fx.buildWorld(S.state);
const f = S.world.factories[0];
const trip = {pid: f.id, factory: f, t0: 1000, boxes: 2};
const phaseStarts = [0, 2800, 4200, 5800, 9400];
assert.equal(fx.TRIP_MS, 12000);
assert.equal(fx.TRIP_MAX, 4);
assert.equal(fx.TRIP_PHASES.reduce((n, p) => n + p.ms, 0), fx.TRIP_MS);
fx.TRIP_PHASES.forEach((phase, i) => {
  assert.equal(fx.tripPose(trip, 1000 + phaseStarts[i]).phase, phase.name);
});
let loaded = 0;
for (let t = 0; t <= fx.TRIP_MS; t += 25) {
  const p = fx.tripPose(trip, trip.t0 + t);
  assert.ok([p.x, p.y, p.k, p.alpha].every(Number.isFinite));
  assert.ok(p.alpha >= 0 && p.alpha <= 1);
  if (p.phase === 'load') {
    assert.ok(p.loaded >= loaded && p.loaded <= 2);
    loaded = p.loaded;
  }
  const fl = fx.forkliftPose(trip, trip.t0 + t);
  if (p.phase !== 'load') assert.equal(fl, null);
  else {
    assert.ok(fl && [fl.x, fl.y, fl.lift].every(Number.isFinite));
    assert.ok(fl.lift >= 0 && fl.lift <= 1);
  }
}
const poses = [0.1, 0.36, 0.55, 0.79, 0.94].map(k =>
  fx.forkliftPose(trip, trip.t0 + 5800 + k * 1800));
assert.equal(poses[0].carrying, false);
assert.equal(poses[2].carrying, true);
assert.equal(poses[2].lift, 1);
assert.equal(poses[4].carrying, false);
assert.ok(poses.every(p => p.x === poses[0].x));
assert.ok(poses[4].y > f.dock.y);

f.mood = 'working';
const positions = new Set(), states = new Set();
for (let t = 0; t < 60000; t += 100) {
  const p = fx.workerPose(f, t, false);
  assert.ok([p.x, p.y].every(Number.isFinite));
  positions.add(p.x.toFixed(2) + ':' + p.y.toFixed(2));
  states.add(p.state);
}
const patrolLength = f.patrol.reduce((n, p, i, points) => {
  const q = points[(i + 1) % points.length];
  return n + Math.hypot(p.x - q.x, p.y - q.y);
}, 0);
if (patrolLength > 0.1) {
  assert.ok(positions.size > 1);
  assert.ok(states.has('patrol') && states.has('pause'));
} else {
  assert.equal(positions.size, 1);
  assert.ok(states.has('pause') || states.has('idle'));
}
f.waveUntil = 2000;
assert.equal(fx.workerPose(f, 1999, false).wave, true);
assert.equal(fx.workerPose(f, 2000, false).wave, false);
assert.deepEqual(fx.workerPose(f, 1000, true), fx.workerPose(f, 10000, true));
assert.equal(fx.workerPose(f, 1000, true).wave, false);
['idle', 'trouble', 'stopped'].forEach(mood => {
  f.mood = mood;
  const p = fx.workerPose(f, 10000, false);
  assert.equal(p.state, mood === 'idle' ? 'idle' : 'gate');
  assert.deepEqual(p, fx.workerPose(f, 20000, false));
});

S.reduced = false; S.demo = null; S.trips = []; S.stats = {};
fx.spawnTrip(f); fx.spawnTrip(f);
assert.equal(S.trips.length, 1);
S.world.factories.slice(1).forEach(fx.spawnTrip);
assert.equal(S.trips.length, 4);
assert.equal(new Set(S.trips.map(t => t.pid)).size, 4);
S.trips = []; S.reduced = true;
fx.spawnTrip(f);
assert.equal(S.trips.length, 0);
S.reduced = false;
S.trips = [{...trip, t0: 0}, {...trip, t0: 12000}];
fx.updateTrips(12000);
assert.equal(S.trips.length, 1);
assert.equal(S.trips[0].t0, 12000);

S.state.runs = Array.from({length: 6}, (_, i) => ({
  id: i, project_id: f.id, status: 'done', outcome: 'succeeded',
  started_at: Date.now() / 1000, finished_at: Date.now() / 1000,
  result_excerpt: '完成'
}));
S.trips = []; S.stats = {};
fx.refreshProjectStats(true);
assert.equal(S.trips.length, 0);
assert.equal(fx.statFor(f.id).done, 6);
S.stats = {};
fx.refreshProjectStats(false);
assert.equal(S.trips.length, 1);
fx.refreshProjectStats(false);
assert.equal(S.trips.length, 1);

S.trips[0].t0 = Date.now() - 6500;
S.hits = [];
const dyn = fx.collectDynamicItems(fx.PAL_DAY);
assert.ok(dyn.length >= 3);
assert.ok(dyn.every((it, i) => !i || dyn[i - 1].d <= it.d));
assert.ok(S.hits.some(h => h.type === 'truck'));
assert.ok(S.hits.some(h => h.type === 'forklift'));
assert.ok(S.hits.some(h => h.type === 'sheep'));
assert.ok(S.hits.every(h => [h.x, h.y, h.r].every(Number.isFinite)));
assert.ok(fx.sceneDrawItems(S.world.models).every(it => !it.m.dynamic));
S.hover = {type: 'sheep', pid: f.id};
fx.onPointerLeave();
assert.equal(S.hover, null);
S.hover = {type: 'truck', pid: f.id};
fx.buildWorld(S.state);
assert.equal(S.hover, null);
assert.equal(S.trips.length, 0);
console.log('activity assertions passed');
"""


def test_factory_activity_runtime(tmp_path):
    if not NODE:
        pytest.skip("需要 Node.js 运行时")
    shim = "var window = {}; var document = {getElementById: function(){return null;}};\n"
    source = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in (
        "app-factory.js", "app-factory-scene.js", "app-factory-drawer.js", "app-factory-draw.js",
    ))
    script = tmp_path / "activity.cjs"
    script.write_text(shim + source + "\n" + DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "activity assertions passed" in result.stdout
