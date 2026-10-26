"""工厂场景 · 动态路线 / 实体占地碰撞 / 本厂菱形坪边界回归（1..9 项目 × 1..6 产线）。

`app-factory.js` 拆成四个零构建普通 script，装卸几何（车道 / 装货纵列 / 巡检环 /
门岗）全按真实占地算在 scene 层，位姿在纯函数里。本文件用 Node 加载真实四个文件，
逐场景断言：

一、**本厂菱形坪边界（硬要求）**：所有新增物流 / 人物可见路线与设施的地面脚印
    （含实际半宽，不是中心点）四角都必须在自家菱形坪内：|dx|/halfW + |dy|/halfH
    <= 1（竖向高度投影不算地面外溢）。设施含：取货托盘、成品垛（读真实绘制出的横向
    占地，含满垛向左铺开）、货车停车 / 等候 / 淡入点、叉车停靠 / 取放位、工头值守点、
    门岗两柱；路线含货车动线、叉车常驻动线、工头巡检环。禁止靠「把 palletSpot 外移」
    「truckIn 甩到坪外」腾地方。
二、动态路线不碰机台底座，也不跨主传送带及支腿。
三、货车停车与门岗两根柱子留出车身间隙（查真实两柱，不是 gate 中心）。
四、叉车常驻（不凭空出现 / 消失）；载货箱世界点 == 叉尖世界点，取货落托盘、
    放货落车斗锚点 f.dockBed（3D，升降只改 z）。
五、朝向沿路径：heading 全程恒定（进场推进方向），倒车只标 reversing，无 180° 翻面
    也不侧滑（y 恒 == f.laneY）；无瞬移（16ms 内位移有界）。
六、物体 z0 深度：动态对象按厂坪高度 YARD_Z 参与遮挡排序，静态件 z0 有限非负。

尺寸一律取生产代理口径的真实值（`FX.*` 导出 + 真实 drawPalletModel 量出的码垛占地），
绝不伪造缩小半径。失败按类别给出「最差场景 + 数值 / 时刻」一条摘要。没有 Node 时
整组 skip；不碰浏览器、不写用户数据。
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

// 真实模型尺寸（世界单位 @zoom1，取 FX 导出 / 真实绘制值，绝不缩小）
const MACH_HW = fx.MACHINE_HW, MACH_HH = fx.MACHINE_HH;   // 机台底座半宽 / 半深
const WORKER_R = fx.WORKER_R;                             // 工头占用半径
const TRUCK_HL = fx.TRUCK_HALF_L, TRUCK_HW = fx.TRUCK_HALF_W;   // 货车 26×9 的半值
const FORK_HL = 3, FORK_HW = 1.5 + fx.FORK_REACH;         // 叉车：车体 6×3 半 3/1.5 + 叉臂伸出
const TRAY_HY = fx.TRAY_HALF;                             // 托盘板厚半高（地面脚y向）
const GATE_PHL = 2, GATE_PHW = 1.5;                       // 门岗柱：drawGateModel w*2/2 与栏杆厚
const YARD_Z = fx.YARD_Z, FORK_REACH = fx.FORK_REACH, FORK_LIFT = fx.FORK_LIFT;
const STEP_MS = 16, MAX_STEP = 2.0, EPS = 0.01;

const clearance = {}, counts = {};
function closest(cat, gap, where) {
  const c = clearance[cat] || (clearance[cat] = { gap: Infinity, where: '', n: 0 });
  c.n += 1; if (gap < c.gap) { c.gap = gap; c.where = where; }
}
function flaw(cat, where) {
  const c = counts[cat] || (counts[cat] = { n: 0, where: '' });
  c.n += 1; if (!c.where) c.where = where;
}
function p2(p) { return '(' + p.x.toFixed(1) + ',' + p.y.toFixed(1) + ')'; }
// 本厂菱形坪比值：地面脚印四角最外者 <= 1 即在坪内（竖向高度不计）
function aabbRatio(f, x0, x1, y0, y1) {
  let m = 0;
  [[x0, y0], [x0, y1], [x1, y0], [x1, y1]].forEach((c) => {
    m = Math.max(m, Math.abs(c[0] - f.x) / f.halfW + Math.abs(c[1] - f.y) / f.halfH);
  });
  return m;
}
function boxRatio(f, x, y, a, b) { return aabbRatio(f, x - a, x + a, y - b, y + b); }

function rectOf(px, py, hl, hw) { return { x0: px - hl, y0: py - hw, x1: px + hl, y1: py + hw }; }
function rectGap(a, b) {   // >0 分离（距离），<0 重叠（负穿透）
  return Math.max(Math.max(b.x0 - a.x1, a.x0 - b.x1), Math.max(b.y0 - a.y1, a.y0 - b.y1));
}
function dRect(px, py, r) {
  return Math.hypot(Math.max(r.x0 - px, px - r.x1, 0), Math.max(r.y0 - py, py - r.y1, 0));
}
function dPtSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
  let t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
// 线段到 AABB 的距离（两凸集：线段端点到矩形 + 矩形四角到线段的最小值）
function segRectGap(s, r) {
  let d = Math.min(dRect(s.ax, s.ay, r), dRect(s.bx, s.by, r));
  [[r.x0, r.y0], [r.x1, r.y0], [r.x0, r.y1], [r.x1, r.y1]].forEach((c) => {
    d = Math.min(d, dPtSeg(c[0], c[1], s.ax, s.ay, s.bx, s.by));
  });
  return d;
}
// 用真实绘制函数量模型占地（相机归零，zoom=1；减 BUF/2 回世界坐标）
const BUF = 4000;
function drawn(fn, model) {
  const rects = [];
  const t = {
    fillStyle: '#000', globalAlpha: 1, fillRect: (x, y, w, h) => {
      if ([x, y, w, h].every(Number.isFinite)) rects.push({ x, y, w, h });
    },
    save() {}, restore() {}, translate() {}, setTransform() {},
    createLinearGradient: () => ({ addColorStop() {} }),
    createRadialGradient: () => ({ addColorStop() {} }),
  };
  const ctx = new Proxy(t, {
    get(o, k) { const v = o[k]; return typeof v === 'function' ? v.bind(o) : v; },
    set(o, k, v) { o[k] = v; return true; },
  });
  fn(ctx, fx.PAL_DAY, model);
  return rects;
}
// 托盘真实横向占地（含满垛向左铺开）：设满垛 done 后量绘制像素的 x 范围
function palletXRange(model, done) {
  S.stats = {};
  if (model.projectId != null) S.stats[model.projectId] = { done: done, rework: 0, busy: 0, lines: 0 };
  const rs = drawn(fx.drawModel, model);
  if (!rs.length) return null;
  let x0 = Infinity, x1 = -Infinity;
  rs.forEach((r) => { x0 = Math.min(x0, r.x); x1 = Math.max(x1, r.x + r.w); });
  return { x0: x0 - BUF / 2, x1: x1 - BUF / 2 };
}
// 门岗：真实 drawGateModel 画两根柱（停工态只有柱是高的）。柱心相对 gate 中心的
// 屏幕横移量 = 世界横移量（zoom=1），柱与其 gate 同地面 y。
function gatePosts(f) {
  const m = f.gateModel;
  if (!m) return null;
  const centerX = m.x + BUF / 2;
  const posts = drawn(fx.drawModel, m).filter((r) => r.h >= 7);
  return posts.map((r) => ({ x: m.x + (r.x + r.w / 2 - centerX), y: m.y }));
}

function projects(nP, nL) {
  return Array.from({ length: nP }, (_, i) => ({
    id: i + 1, name: '项目' + i, root_ok: true,
    lines: Array.from({ length: nL }, (_, j) => (
      { key: 'cron:' + i + ':' + j, kind: 'cron', id: i * 10 + j, status: 'idle' })),
  }));
}

let scenarios = 0;
for (let nP = 1; nP <= 9; nP += 1) {
  for (let nL = 1; nL <= 6; nL += 1) {
    const tag = '项目' + nP + '×产线' + nL;
    S.state = { projects: projects(nP, nL), runs: [], counters: {}, config: {} };
    fx.buildWorld(S.state);
    const world = S.world, tripx = fx.TRIP_MS;
    scenarios += 1;
    if (world.factories.length !== nP) { flaw('厂区数', tag + ' 实为 ' + world.factories.length); continue; }
    S.cam = { x: 0, y: 0, zoom: 1, tx: 0, ty: 0, tz: 1 };   // 量真实绘制占地用（zoom=1）
    S.bufW = BUF; S.bufH = BUF;

    world.factories.forEach((f, fi) => {
      const at = tag + ' 厂' + fi;
      f.mood = 'idle';
      const mach = f.machines.map((m) => rectOf(m.x, m.y, MACH_HW, MACH_HH));
      const belts = [];   // 传送带按「薄带」脚印：沿路径线段 + 半宽（带面与支腿取大）
      world.models.forEach((m) => {
        if (m.kind !== 'conveyor' || m.bg || String(m.projectId) !== String(f.id) || !m.path) return;
        const pad = Math.max((m.width || 3) * 0.5, 3);   // drawConveyorModel 带面/支腿外扩
        for (let i = 1; i < m.path.length; i += 1) {
          belts.push({ ax: m.path[i - 1].x, ay: m.path[i - 1].y,
                       bx: m.path[i].x, by: m.path[i].y, pad: pad });
        }
      });
      const posts = gatePosts(f);
      const palletRange = palletXRange(f.palletModel, 12);   // 满垛（2 只 × 6）
      const pickRange = palletXRange(f.pickModel, 0);

      // (一) 本厂菱形坪边界：设施（真实脚印）与路线（逐点采样）都必须在坪内
      const fac = [
        ['货车停车位', f.dock, TRUCK_HL, TRUCK_HW],
        ['货车等候点', f.waitPt, TRUCK_HL, TRUCK_HW],
        ['货车淡入点', f.truckIn, TRUCK_HL, TRUCK_HW],
        ['叉车停靠位', f.forkPark, FORK_HL, FORK_HW],
        ['叉车放货位', f.dockDrop, FORK_HL, FORK_HW],
        ['叉车取货位', f.forkPick, FORK_HL, FORK_HW],
        ['工头值守点', f.workerPost, WORKER_R, WORKER_R],
      ];
      fac.forEach((it) => {
        const r = boxRatio(f, it[1].x, it[1].y, it[2], it[3]);
        closest('坪外·' + it[0], 1 - r, at + ' ' + p2(it[1]) + ' 比值 ' + r.toFixed(2));
      });
      if (palletRange) {
        const y0 = f.palletSpot.y - TRAY_HY, y1 = f.palletSpot.y + TRAY_HY;
        const r = aabbRatio(f, palletRange.x0, palletRange.x1, y0, y1);
        closest('坪外·成品垛', 1 - r, at + ' 绘制x[' + palletRange.x0.toFixed(1) + ',' +
          palletRange.x1.toFixed(1) + '] 比值 ' + r.toFixed(2));
      }
      if (pickRange) {
        const y0 = f.tray.y - TRAY_HY, y1 = f.tray.y + TRAY_HY;
        const r = aabbRatio(f, pickRange.x0, pickRange.x1, y0, y1);
        closest('坪外·取货托盘', 1 - r, at + ' 绘制x[' + pickRange.x0.toFixed(1) + ',' +
          pickRange.x1.toFixed(1) + '] 比值 ' + r.toFixed(2));
      }
      (posts || []).forEach((q, qi) => {
        const r = boxRatio(f, q.x, q.y, GATE_PHL, GATE_PHW);
        closest('坪外·门岗柱' + qi, 1 - r, at + ' ' + p2(q) + ' 比值 ' + r.toFixed(2));
      });

      // (二) 工头巡检环：不碰机台底座，不撞主带 / 支腿；环上每点都在坪内
      for (let i = 0; i < f.patrol.length; i += 1) {
        const a = f.patrol[i], b = f.patrol[(i + 1) % f.patrol.length];
        for (let k = 0; k <= 1.0001; k += 0.02) {
          const px = a.x + (b.x - a.x) * k, py = a.y + (b.y - a.y) * k;
          const pts = p2({ x: px, y: py });
          mach.forEach((r, mi) => closest('巡检路线→机台底座', dRect(px, py, r) - WORKER_R,
            at + ' 机台' + mi + ' ' + pts));
          belts.forEach((s, bi) => closest('巡检路线→传送带',
            dPtSeg(px, py, s.ax, s.ay, s.bx, s.by) - s.pad - WORKER_R, at + ' 带' + bi + ' ' + pts));
          closest('坪外·巡检路线', 1 - boxRatio(f, px, py, WORKER_R, WORKER_R), at + ' ' + pts);
        }
      }

      // (三)(五) 货车：单轴直线动线，heading 恒定，全程在坪内
      const trip = { pid: f.id, factory: f, t0: 0, boxes: 2 };
      const dir = f.dock.x >= (f.truckIn || f.apron).x ? 1 : -1;
      let prev = null, maxStep = 0;
      for (let t = 0; t <= tripx; t += STEP_MS) {
        const p = fx.tripPose(trip, t);
        if (![p.x, p.y, p.heading, p.alpha].every(Number.isFinite)) {
          flaw('货车位姿非有限', at + ' t=' + t); continue;
        }
        if (p.heading !== dir) flaw('货车朝向非恒定', at + ' t=' + t + ' heading=' + p.heading);
        if (Math.abs(p.y - f.laneY) > EPS) {
          flaw('货车侧滑离车道', at + ' t=' + t + ' y=' + p.y.toFixed(2));
        }
        if (p.reversing && p.phase !== 'leave') {
          flaw('倒车标记出现在非离场相', at + ' t=' + t + ' ' + p.phase);
        }
        if (p.z0 !== YARD_Z) flaw('货车 z0≠厂坪', at + ' t=' + t + ' ' + p.z0);
        const where = ' t=' + t + ' ' + p.phase + ' ' + p2(p);
        if (p.alpha > 0.02) {
          closest('坪外·货车动线', 1 - boxRatio(f, p.x, p.y, TRUCK_HL, TRUCK_HW), at + where);
        }
        const box = rectOf(p.x, p.y, TRUCK_HL, TRUCK_HW);
        mach.forEach((r, mi) => closest('货车占地→机台底座', rectGap(box, r), at + ' 机台' + mi + where));
        belts.forEach((s, bi) => closest('货车占地→传送带', segRectGap(s, box) - s.pad,
          at + ' 带' + bi + where));
        if (posts && p.alpha > 0.02) {
          posts.forEach((q, qi) => closest('货车占地→门岗柱',
            rectGap(box, rectOf(q.x, q.y, GATE_PHL, GATE_PHW)), at + ' 柱' + qi + where));
        }
        if (prev) maxStep = Math.max(maxStep, Math.hypot(p.x - prev.x, p.y - prev.y));
        prev = p;
      }
      closest('货车瞬移', MAX_STEP - maxStep, at + ' 16ms 位移 ' + maxStep.toFixed(2));
      if (!posts || posts.length !== 2) flaw('门岗柱读不到两根', at + ' 实为 ' + (posts ? posts.length : 0));

      // (四)(五) 叉车：常驻不消失 + 无瞬移 + 全程在坪内
      S.trips = [{ pid: f.id, factory: f, t0: 0, boxes: 2 }];
      let flPrev = null, flStep = 0;
      for (let now = 0; now <= 2 * tripx; now += STEP_MS) {
        const pose = fx.forkliftDutyPose(f, now);
        if (!pose) { flaw('叉车中途消失', at + ' now=' + now); continue; }
        if (pose.x !== f.forkLaneX) flaw('叉车作业道漂移', at + ' now=' + now);
        if (pose.z0 !== YARD_Z) flaw('叉车 z0≠厂坪', at + ' now=' + now + ' ' + pose.z0);
        closest('坪外·叉车动线', 1 - boxRatio(f, pose.x, pose.y, FORK_HL, FORK_HW), at + ' now=' + now);
        if (flPrev) flStep = Math.max(flStep, Math.hypot(pose.x - flPrev.x, pose.y - flPrev.y));
        flPrev = pose;
      }
      S.trips = [];
      if (!fx.forkliftDutyPose(f, 12345)) flaw('叉车不在停靠位常驻', at);   // 无车时也在场
      closest('叉车瞬移', MAX_STEP - flStep, at + ' 16ms 位移 ' + flStep.toFixed(2));

      // (四) 载货箱世界点 == 叉尖；取货落托盘、放货落车斗锚点（3D）
      const tray = f.tray, bed = f.dockBed;
      let sawPick = false, sawDrop = false;
      for (let ck = 0; ck < 1; ck += 0.004) {
        const pose = fx.forkliftCycle(f, ck);
        if (!pose.carrying || !pose.cargo) continue;
        const c = pose.cargo, ckTag = at + ' ck=' + ck.toFixed(3);
        closest('货箱→叉尖 x', EPS - Math.abs(c.x - pose.x), ckTag);
        const tipY = pose.y + (pose.facing === 'north' ? -1 : 1) * FORK_REACH;
        closest('货箱→叉尖 y', EPS - Math.abs(c.y - tipY), ckTag);
        if (!(c.z >= YARD_Z - EPS && c.z <= YARD_Z + FORK_LIFT + EPS)) {
          flaw('货箱 z 越界（离叉飞行）', ckTag + ' z=' + c.z);
        }
        closest('坪外·载货箱', 1 - boxRatio(f, c.x, c.y, fx.FORK_HALF, fx.FORK_HALF), ckTag + ' ' + p2(c));
        if (pose.phase === 'pick') {
          sawPick = true;
          closest('取货点→托盘', EPS - Math.hypot(c.x - tray.x, c.y - tray.y), ckTag);
        }
        if (pose.phase === 'drop') {
          sawDrop = true;
          closest('放货点→车斗锚点', EPS - Math.hypot(c.x - bed.x, c.y - bed.y), ckTag);
        }
      }
      if (!sawPick) flaw('叉车没有取货相', at);
      if (!sawDrop) flaw('叉车没有放货相', at);
      // 放货锚点必须落在车斗占地内（叉车放下的箱与车斗码的箱共用它）
      const dockBox = rectOf(f.dock.x, f.dock.y, TRUCK_HL, TRUCK_HW);
      if (!(bed.x >= dockBox.x0 && bed.x <= dockBox.x1 && bed.y >= dockBox.y0 && bed.y <= dockBox.y1)) {
        flaw('车斗锚点落在车斗占地外', at + ' bed=' + p2(bed));
      }
      // (六) 静态件 z0 有限非负
      world.models.forEach((m, mi) => {
        if (!Number.isFinite(m.z0 || 0) || (m.z0 || 0) < 0) {
          flaw('静态件 z0 非法', at + ' 模型' + mi + ' ' + m.kind + ' ' + m.z0);
        }
      });
    });

    // (六) 动态绘制项按厂坪高度参与排序
    const f0 = world.factories[0];
    S.hits = [];
    const loadAt = fx.TRIP_PHASES[0].ms + fx.TRIP_PHASES[1].ms + fx.TRIP_PHASES[2].ms;
    S.trips = [{ pid: f0.id, factory: f0, boxes: 2, t0: Date.now() - loadAt - 60 }];
    const dyn = fx.collectDynamicItems(fx.PAL_DAY);
    if (!dyn.length) flaw('动态绘制项为空', tag);
    dyn.forEach((it, di) => {
      if (it.z0 !== YARD_Z) flaw('动态项 z0≠厂坪高度', tag + ' 项' + di + ' z0=' + it.z0);
    });
    S.trips = [];
  }
}
if (scenarios !== 54) flaw('场景数', '实跑 ' + scenarios + '（应为 54）');

const issues = [];
Object.keys(clearance).forEach((cat) => {
  const c = clearance[cat];
  if (c.gap < 0) {
    issues.push('[' + cat + '] 间隙 ' + c.gap.toFixed(2) + ' < 0（' + c.n + ' 处）最差：' + c.where);
  }
});
Object.keys(counts).forEach((cat) => {
  const c = counts[cat];
  issues.push('[' + cat + '] ' + c.n + ' 次，首例：' + c.where);
});
assert.equal(issues.length, 0, '\n' + issues.join('\n'));
console.log('collision assertions passed');
"""


def test_factory_dynamic_routes_and_footprints(tmp_path):
    if not NODE:
        pytest.skip("需要 Node.js 运行时")
    shim = "var window = {}; var document = {getElementById: function(){return null;}};\n"
    source = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in (
        "app-factory.js", "app-factory-scene.js", "app-factory-drawer.js", "app-factory-draw.js",
    ))
    script = tmp_path / "collisions.cjs"
    script.write_text(shim + source + "\n" + DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "collision assertions passed" in result.stdout
