"""工厂 · 右月台常驻待发告示牌（空态 / 倒计时 / 过期等待调度 / 样板厂排期）。

`app-factory.js` 拆成四个零构建普通 script：告示牌的计算层是纯函数
`FX.noticeboardState(st, nowSec)` 与 `FX.upcomingList(st, nowSec)`，绘制层
`drawUpcoming` 画一块固定牌并把几何留在 `S.noticeboard`。本文件用 Node 加载
真实四个文件，按生产契约逐条断言：

- 空态：state=empty、text 含「暂无待发」、计数 0；告示牌仍常驻（drawUpcoming
  照旧写 S.noticeboard）。
- soon：idle + next_run_at 在 30 分钟窗口内 → state=soon、name（截 8 字）、
  countdown=fmtCountdown(eta)、key=产线 key。
- due：idle 且 next_run_at 已到 → state=due 等待调度；**超 30 分钟仍 due**，
  不按过期下界强行隐藏（真实调度卡住时不许变空态）。
- running / 暂停 / 停工厂区 / 超窗 / 无 next_run_at / pipeline 都不待发。
- UPCOMING_MAX=1：多枚待发时 count>1、overflow=计数-1。
- refreshUpcoming 把结果落到 S.upcoming 与 S.noticeboardView。
- 样板厂：竞品巡检初始 45 秒、period_s=900；demoTick 不提前挑未来排期，
  到点才启动，完成后 next_run_at=完成时刻+period_s。

没有 Node 时整组 skip；不碰浏览器、不写用户数据。
"""

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
const NOW = 1700000000;
function cron(o) {
  return Object.assign({
    key: 'cron:' + o.id, kind: 'cron', id: o.id, status: 'idle',
    name: '任务', next_run_at: NOW + 600,
  }, o);
}
function state(lines, rootOk) {
  return { projects: [{ id: 1, name: '厂区', root_ok: rootOk !== false, lines: lines }],
           runs: [], counters: {}, config: {} };
}
function board(lines, rootOk, now) {
  return fx.noticeboardState(state(lines, rootOk), now == null ? NOW : now);
}
function list(lines, rootOk, now) {
  return fx.upcomingList(state(lines, rootOk), now == null ? NOW : now);
}

// —— 空态：暂无待发 ——
var v = board([]);
assert.equal(v.state, 'empty');
assert.equal(v.count, 0);
assert.equal(v.overflow, 0);
assert.ok(!v.key);
assert.ok(String(v.text).includes('暂无待发'), '空态文案');
assert.equal(fx.UPCOMING_MAX, 1);
assert.equal(fx.UPCOMING_NAME_MAX, 8);

// —— soon：未来 15 分钟 ——
v = board([cron({ id: -12, name: '竞品巡检', next_run_at: NOW + 900 })]);
assert.equal(v.state, 'soon');
assert.equal(v.key, 'cron:-12');
assert.equal(v.name, '竞品巡检');
assert.equal(v.count, 1);
assert.equal(v.overflow, 0);
assert.equal(v.countdown, fx.fmtCountdown(900));
assert.ok(String(v.text).includes('竞品巡检'));

// —— name 截 8 字 ——
v = board([cron({ id: -13, name: '一二三四五六七八九十', next_run_at: NOW + 600 })]);
assert.equal(v.name.length, 8);
assert.equal(v.name, '一二三四五六七八');

// —— due：过期仍等待调度（超 30 分钟也不隐藏） ——
v = board([cron({ id: -22, name: '依赖体检', next_run_at: NOW - 600 })]);
assert.equal(v.state, 'due');
assert.equal(v.key, 'cron:-22');
assert.equal(v.countdown, '', '等待调度不给倒计时');
assert.ok(String(v.text).includes('等待调度'));
v = board([cron({ id: -23, name: '久未调度', next_run_at: NOW - 3600 })]);
assert.equal(v.state, 'due', '过期 1 小时仍 due，不按 -1800 下界隐藏');
assert.equal(v.name, '久未调度', '超窗但不裁名字（due 不走上界）');

// —— 四种不待发 ——
assert.equal(board([cron({ id: -11, status: 'running' })]).state, 'empty');
assert.equal(board([cron({ id: -14, status: 'paused' })]).state, 'empty');
assert.equal(board([cron({ id: -15, status: 'stopped' })]).state, 'empty');
assert.equal(board([cron({ id: -16 })], false).state, 'empty');
assert.equal(board([cron({ id: -17, next_run_at: NOW + 1801 })]).state, 'empty');
assert.equal(board([cron({ id: -18, next_run_at: 0 })]).state, 'empty');
assert.equal(board([{ key: 'pipeline:1', kind: 'pipeline', id: 1, status: 'idle',
                      next_run_at: NOW + 600 }]).state, 'empty');
// 窗口边界：恰好 1800 秒仍算 soon
assert.equal(board([cron({ id: -19, next_run_at: NOW + 1800 })]).state, 'soon');

// —— 计数与溢出 ——
var many = [cron({ id: -30, next_run_at: NOW + 300 }), cron({ id: -31, next_run_at: NOW + 600 })];
v = board(many);
assert.equal(v.count, 2);
assert.equal(v.overflow, 1);
assert.equal(list(many).length, 2);
assert.equal(list(many)[0].key, 'cron:-30', '列表按 eta 升序');

// —— refreshUpcoming 落库 ——
S.state = state([cron({ id: -12, name: '竞品巡检', next_run_at: Date.now() / 1000 + 900 })]);
fx.refreshUpcoming();
assert.ok(Array.isArray(S.upcoming) && S.upcoming.length === 1);
assert.equal(S.noticeboardView.state, 'soon');
assert.equal(S.noticeboardView.key, 'cron:-12');

// —— 常驻固定牌：空态也照画 ——
function stubCtx() {
  var base = { globalAlpha: 1, fillStyle: '#000', strokeStyle: '#000' };
  return new Proxy(base, {
    get: function (o, k) { return k in o ? o[k] : function () {}; },
    set: function (o, k, val) { o[k] = val; return true; },
  });
}
S.state = state([cron({ id: -12, name: '竞品巡检', next_run_at: NOW + 900 })]);
fx.buildWorld(S.state);
fx.refreshUpcoming();
S.hits = [];
fx.drawUpcoming(stubCtx(), fx.PAL_DAY);
assert.ok(S.noticeboard && Number.isFinite(S.noticeboard.x) && Number.isFinite(S.noticeboard.h));
assert.ok(S.hits.some(function (h) { return h.type === 'upcoming'; }), '告示牌要有点击热区');
S.state = state([]);
fx.refreshUpcoming();
S.hits = [];
fx.drawUpcoming(stubCtx(), fx.PAL_DAY);
assert.ok(S.noticeboard && Number.isFinite(S.noticeboard.x), '无任务仍常驻');

// —— 样板厂：排期不提前跑、到点启动、完成顺延 ——
S.state = null;
S.demo = fx.buildDemoState({});
S.world = null;
S.reduced = false;
S.ticks = 0;
var jp = S.demo.projects[0].lines.filter(function (l) { return l.id === -12; })[0];
assert.ok(jp, '样板厂应有竞品巡检');
assert.ok(Math.abs(jp.next_run_at - Date.now() / 1000 - 45) <= 2, '竞品巡检初始 45 秒');
assert.equal(jp.period_s, 900, '竞品巡检周期 900 秒');

var t0 = Date.now();
for (var i = 0; i < 12; i += 1) fx.demoTick(t0 + i * 1000);  // 到点前 ~11 秒
var jp1 = S.demo.projects[0].lines.filter(function (l) { return l.id === -12; })[0];
assert.equal(jp1.status, 'idle', '未到点不得启动');
assert.ok(!S.demo.runs.some(function (r) {
  return r.task_id === -12 && r.status === 'running';
}), '未到点不派工');

var dueMs = t0 + 50000;  // 越过 45 秒排期
for (var j = 0; j < 4; j += 1) fx.demoTick(dueMs + j * 1000);
var jp2 = S.demo.projects[0].lines.filter(function (l) { return l.id === -12; })[0];
assert.equal(jp2.status, 'running', '到点才启动');
var run = S.demo.runs.filter(function (r) { return r.task_id === -12 && r.status === 'running'; })[0];
assert.ok(run, '到点应有运行记录');

var laterMs = dueMs + 20000;  // 运行满 18 秒
for (var k = 0; k < 3; k += 1) fx.demoTick(laterMs + k * 1000);
assert.notEqual(jp2.status, 'running', '演示运行应按时收尾');
assert.ok(run.finished_at > 0, '收尾要记完成时刻');
assert.ok(Math.abs(jp2.next_run_at - (run.finished_at + 900)) <= 1,
          '完成后 next_run_at = 完成时刻 + period_s');
console.log('noticeboard assertions passed');
"""


def test_factory_noticeboard_runtime(tmp_path):
    if not NODE:
        pytest.skip("需要 Node.js 运行时")
    shim = "var window = {}; var document = {getElementById: function(){return null;}};\n"
    source = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in (
        "app-factory.js", "app-factory-scene.js", "app-factory-drawer.js", "app-factory-draw.js",
    ))
    script = tmp_path / "noticeboard.cjs"
    script.write_text(shim + source + "\n" + DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "noticeboard assertions passed" in result.stdout
