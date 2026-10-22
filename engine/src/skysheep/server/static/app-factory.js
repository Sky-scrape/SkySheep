/* ============================================================
 * 工厂（第三顶层视图）：跨项目自动化观战台
 *
 * 零构建普通 script，在 app.js 之后加载；只通过全局函数与 app.js 互调：
 *   factoryMount() / factoryUnmount() / factoryReload() / factoryOnEvent(data) /
 *   factoryRepaint() / factoryEscape()
 *
 * 数据：factory.state（打开时一次） + factory_run 事件（开始 / 结束各一次）。
 * 场景：固定斜 45° 等距，32×16 瓦片；内部缓冲按 CSS 尺寸 1/2 渲染（最小 320×180），
 *       最近邻放大；世界动画 10fps，镜头平移 / 缩放走 rAF。
 * 全部图形程序化绘制（矩形 / 菱形 / 折线），不引入图片、库或出网请求。
 * 文字一律 DOM 覆盖层（中文像素字体不可分发），最多约 40 个标签。
 * 场景不跟随六套主题换色：昼夜两套调色板在下方 JS 常量里，外壳仍走 CSS 主题变量。
 * ============================================================ */
(function () {
  "use strict";

  // ---------- 常量 ----------
  var TILE_W = 32;
  var TILE_H = 16;
  var MAX_LABELS = 40;
  var MAX_PACKAGES = 60;
  var BUFFER_MIN_W = 320;
  var BUFFER_MIN_H = 180;
  var TICK_MS = 100;          // 世界动画 10fps
  var CAM_EASE = 9;           // 镜头缓动系数
  var RUN_TAIL_MS = 6000;     // 包裹落地后停留多久再消失

  // 状态文案（与 docs/界面术语表.md 同口径：包裹不叫「任务」）
  var RUN_STATUS = {
    running: "运行中", done: "已完成", empty: "空产出", error: "失败",
    interrupted: "中断", skipped: "跳过", orphan: "孤儿", cancelled: "已取消",
  };
  var TRIGGER_TEXT = { schedule: "定时", manual: "手动", link: "供货" };
  var WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

  // 昼盘：黄金时刻的工业厂区（灰钢 / 锈色筒仓 / 沙地 / 暖窗）
  var PAL_DAY = {
    skyTop: "#f7d488", skyMid: "#e8a35a", skyLow: "#c97a45", sun: "#ffe6a8",
    ground: "#c3a176", groundDark: "#a17d55", groundLight: "#dcc198", groundHi: "#efdab4",
    road: "#8a7a63", roadDark: "#6f6049", roadLight: "#a8977d",
    steel: "#8a8f96", steelDark: "#545a62", steelLight: "#c2c8ce",
    stone: "#9e8063", stoneDark: "#655342", stoneLight: "#d0b592",
    concrete: "#a89f92", concreteDark: "#7c7365", concreteLight: "#c4bbae",
    rust: "#b4623a", rustDark: "#7a3d24", rustLight: "#dc8a54",
    glass: "#6f8f9a", glassDark: "#3f5660", glassLight: "#a8c8d0",
    window: "#ffcf6b", windowDark: "#3a2f28",
    smoke: "#ded8cc", dust: "#f0d6a8", ore: "#6b4a32", oreDark: "#3e2a1c",
    crate: "#c98a4b", crateDark: "#7d5326", crateLight: "#e0a869",
    metal: "#8b949c", metalDark: "#4c545c", metalLight: "#c3ccd2",
    belt: "#6e6a60", beltDark: "#3d3a34", beltMark: "#c9c2b0",
    sheep: "#f6f1e4", sheepShade: "#d8cfbc", sheepFace: "#33302a",
    ink: "#2b2a24", ok: "#6aa86a", warn: "#e0a33c", bad: "#cf503f", idle: "#9a958a",
    product: "#f0d58b", roofLight: "#c07a5c",
    bgSteel: "#b9b0a2", bgSteelDark: "#a79e90", bgSteelLight: "#d2c9ba",
    bgRust: "#c39a7c", bgRustDark: "#b08a6e", bgRustLight: "#d9b394",
    bgHaze: "#e8c9a4",
    shadow: "rgba(58, 40, 24, 0.34)", glow: "rgba(255, 196, 110, 0.20)",
  };
  // 夜盘：钢蓝暮色 + 暖黄窗光 + 浓烟
  var PAL_NIGHT = {
    skyTop: "#122a44", skyMid: "#1b3a56", skyLow: "#2b4c64", sun: "#a8c6da",
    ground: "#43443f", groundDark: "#2f302b", groundLight: "#54544c", groundHi: "#63625a",
    road: "#3f4144", roadDark: "#2c2e30", roadLight: "#5b5f65",
    steel: "#4e5c6a", steelDark: "#2e3945", steelLight: "#748696",
    stone: "#50514d", stoneDark: "#303834", stoneLight: "#71756c",
    concrete: "#494a46", concreteDark: "#30312d", concreteLight: "#5b5c55",
    rust: "#7a4530", rustDark: "#48281b", rustLight: "#9c5e44",
    glass: "#4a6a7a", glassDark: "#293f4c", glassLight: "#7fa6b8",
    window: "#ffd36e", windowDark: "#1a1a18",
    smoke: "#a8adb2", dust: "#d0b98f", ore: "#4a3524", oreDark: "#2b1e14",
    crate: "#8a6138", crateDark: "#4b351d", crateLight: "#a87846",
    metal: "#4a5057", metalDark: "#2b2f34", metalLight: "#6f767e",
    belt: "#3a3831", beltDark: "#211f1b", beltMark: "#7a7364",
    sheep: "#d9d2c4", sheepShade: "#b3ac9c", sheepFace: "#1b1a18",
    ink: "#0b0c10", ok: "#5aa05a", warn: "#c9903a", bad: "#b8443f", idle: "#6a6c73",
    product: "#c9a95c", roofLight: "#8a5040",
    bgSteel: "#3f4f5e", bgSteelDark: "#33424f", bgSteelLight: "#4f6272",
    bgRust: "#5c4436", bgRustDark: "#4d372b", bgRustLight: "#70533f",
    bgHaze: "#2a4358",
    shadow: "rgba(0, 0, 0, 0.46)", glow: "rgba(255, 196, 110, 0.26)",
  };

  // ---------- 模块状态 ----------
  var S = {
    mounted: false,        // 视图可见 + 循环在跑
    loaded: false,
    loading: false,
    state: null,           // 最近一次 factory.state（真实数据）
    demo: null,            // 样板厂（没有真实产线时）
    world: null,
    packages: new Map(),   // run.id -> 包裹
    runCache: new Map(),   // 抽屉里按需求回的行
    cam: { x: 0, y: 0, zoom: 1, tx: 0, ty: 0, tz: 1 },
    bufW: BUFFER_MIN_W, bufH: BUFFER_MIN_H,
    cssW: 960, cssH: 540,
    ticks: 0,
    rot: 0,
    hallBoost: 0,          // performance 时间戳：派工时转盘加速
    labelsOff: false,
    panelFolded: false,
    drawer: null,
    selectedKey: null,
    hits: [],
    labelPool: [],
    raf: 0,
    clockTimer: 0,
    lastFrame: 0,
    lastTick: 0,
    lastPanel: 0,
    demoSeq: 0,
    reduced: false,
    bound: false,
    worldSig: "",
    snapshotTimer: 0,
    night: false,
  };

  var dom = {};

  function $(id) { return document.getElementById(id); }
  function nowMs() { return Date.now(); }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function pad2(n) { return n < 10 ? "0" + n : String(n); }

  // ---------- 主题 / 调色板 ----------
  function themeIsDark() {
    try {
      if (typeof resolvedThemeId === "function" && typeof THEMES !== "undefined") {
        var id = resolvedThemeId();
        return !!(THEMES[id] && THEMES[id].dark);
      }
    } catch (e) { /* 主题表还没就绪时按浅色处理 */ }
    return false;
  }

  function activeConfig() {
    var st = S.demo || S.state;
    return (st && st.config) || {};
  }

  /** 昼夜盘：22:00–06:00 一律夜盘；其余时刻按 resolvedThemeId() 的明暗；
      关闭 [factory] ambient 时固定昼盘（一期不做天气）。 */
  function palette() {
    var cfg = activeConfig();
    if (cfg.ambient === false) return PAL_DAY;
    var h = new Date().getHours();
    var night = h >= 22 || h < 6 || themeIsDark();
    return night ? PAL_NIGHT : PAL_DAY;
  }

  // ---------- 时间文案 ----------
  function fmtClock(d) {
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
  function fmtDate(d) {
    return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + WEEKDAYS[d.getDay()];
  }
  function fmtTs(sec) {
    if (!sec) return "—";
    var d = new Date(sec * 1000);
    return (d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }
  function fmtAgo(sec) {
    if (!sec) return "从未";
    var diff = Math.max(0, nowMs() / 1000 - sec);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    return fmtTs(sec);
  }
  function fmtClockShort(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    return Math.floor(sec / 60) + ":" + pad2(sec % 60);
  }
  function fmtDur(sec) {
    if (sec == null || isNaN(sec)) return "—";
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + " 秒";
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    if (m < 60) return m + " 分" + (s ? " " + s + " 秒" : "");
    return Math.floor(m / 60) + " 时 " + (m % 60) + " 分";
  }
  function runElapsed(run) {
    if (!run) return 0;
    if (run.status !== "running") {
      if (run.duration_s != null) return run.duration_s;
      if (run.finished_at && run.started_at) return run.finished_at - run.started_at;
      return 0;
    }
    return Math.max(0, nowMs() / 1000 - (run.started_at || 0));
  }

  function runKey(run) {
    return run.kind === "pipeline_node" ? "pipe:" + run.pipeline_id : "cron:" + run.task_id;
  }

  // ---------- 数据源 ----------
  function activeState() { return S.demo || S.state; }
  function allRuns() {
    var st = activeState();
    return (st && st.runs) || [];
  }
  function allProjects() {
    var st = activeState();
    return (st && st.projects) || [];
  }
  function findLine(key) {
    if (!S.world) return null;
    return S.world.lines.get(key) || null;
  }
  function findRun(id) {
    var r = allRuns().find(function (x) { return x.id === id; });
    return r || S.runCache.get(id) || null;
  }

  function hasRealLines(st) {
    if (!st || !st.projects) return false;
    return st.projects.some(function (p) { return (p.lines || []).length > 0; });
  }

  // ---------- 挂载 / 卸载 ----------
  function factoryMount() {
    if (S.mounted) return;
    S.mounted = true;
    cacheDom();
    bindOnce();
    updateClock();
    if (!S.clockTimer) S.clockTimer = setInterval(updateClock, 1000);
    // 「减少动态效果」：场景是静态快照，每分钟刷一次（设计 §4.5）
    if (!S.snapshotTimer) {
      S.snapshotTimer = setInterval(function () {
        if (S.mounted && S.reduced) render();
      }, 60000);
    }
    resizeScene(true);
    if (S.loaded) renderAll(); // 先用缓存画一帧，再向服务端要最新（打开时拉一次）
    else showPanelMessage("正在读取工厂数据…");
    factoryReload();
    startLoop();
  }

  function factoryUnmount() {
    S.mounted = false;
    if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; }
    if (S.clockTimer) { clearInterval(S.clockTimer); S.clockTimer = 0; }
    if (S.snapshotTimer) { clearInterval(S.snapshotTimer); S.snapshotTimer = 0; }
    closeDrawer();
    clearLabels();
  }

  function cacheDom() {
    dom.view = $("view-factory");
    dom.stage = $("factory-stage");
    dom.canvas = $("factory-canvas");
    dom.labels = $("factory-labels");
    dom.panel = $("factory-panel");
    dom.panelBody = $("factory-panel-body");
    dom.drawer = $("factory-drawer");
    dom.notice = $("factory-notice");
    dom.clockTime = $("factory-clock-time");
    dom.clockDate = $("factory-clock-date");
    dom.toolbar = $("factory-toolbar");
    dom.labelToggle = $("factory-labels-toggle");
    dom.settingsBtn = $("factory-settings");
    dom.panelFold = $("factory-panel-fold");
    dom.hudRunning = $("factory-num-running");
    dom.hudQueued = $("factory-num-queued");
    dom.hudAttention = $("factory-num-attention");
    dom.hudDone = $("factory-num-done");
    dom.hudAttentionCell = document.querySelector('#factory-hud .fh-cell[data-k="attention"]');
  }

  function bindOnce() {
    if (S.bound) return;
    S.bound = true;
    cacheDom();

    var fit = $("factory-fit");
    var zin = $("factory-zoom-in");
    var zout = $("factory-zoom-out");
    var allBtn = $("factory-all-runs");
    var addBtn = $("factory-line-add");

    if (fit) fit.onclick = function () { fitCamera(true); };
    if (zin) zin.onclick = function () { zoomBy(1.25); };
    if (zout) zout.onclick = function () { zoomBy(0.8); };
    if (allBtn) allBtn.onclick = function () { openAllRunsDrawer(); };
    if (addBtn) addBtn.onclick = function () { openNewLineDrawer(0); };
    if (dom.labelToggle) {
      dom.labelToggle.onclick = function () {
        S.labelsOff = !S.labelsOff;
        dom.labelToggle.setAttribute("aria-pressed", S.labelsOff ? "false" : "true");
        updateLabels();
      };
    }
    if (dom.settingsBtn) dom.settingsBtn.onclick = function () { openSettingsDrawer(); };
    if (dom.panelFold) {
      dom.panelFold.onclick = function () {
        S.panelFolded = !S.panelFolded;
        dom.panel.classList.toggle("folded", S.panelFolded);
        resizeScene(false);
      };
    }
    if (dom.panelBody) {
      dom.panelBody.addEventListener("click", function (ev) {
        var addEl = ev.target.closest("[data-add-project]");
        if (addEl) { openNewLineDrawer(Number(addEl.dataset.addProject) || 0); return; }
        var head = ev.target.closest("[data-focus-project]");
        if (head) { focusProject(Number(head.dataset.focusProject)); return; }
        var lineEl = ev.target.closest("[data-line-key]");
        if (lineEl) { openLineDrawer(lineEl.dataset.lineKey, { focus: true }); }
      });
    }
    if (dom.hudAttentionCell) {
      dom.hudAttentionCell.onclick = function () { openAttentionDrawer(); };
      dom.hudAttentionCell.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openAttentionDrawer(); }
      };
    }

    if (dom.canvas) {
      dom.canvas.addEventListener("pointerdown", onPointerDown);
      dom.canvas.addEventListener("pointermove", onPointerMove);
      dom.canvas.addEventListener("pointerup", onPointerUp);
      dom.canvas.addEventListener("pointercancel", onPointerUp);
      dom.canvas.addEventListener("pointerleave", onPointerLeave);
      dom.canvas.addEventListener("wheel", onWheel, { passive: false });
    }

    if (typeof ResizeObserver === "function" && dom.stage) {
      try {
        new ResizeObserver(function () { resizeScene(false); }).observe(dom.stage);
      } catch (e) { /* 老 WebView 没有 ResizeObserver：退回 window resize */ }
    }
    window.addEventListener("resize", function () { if (S.mounted) resizeScene(false); });
    document.addEventListener("visibilitychange", function () {
      if (!S.mounted) return;
      if (!document.hidden) {
        S.lastFrame = 0;
        S.lastTick = 0;
        renderAll();
      }
    });
    try {
      var mq = window.matchMedia("(prefers-reduced-motion: reduce)");
      S.reduced = mq.matches;
      var onChange = function (e) {
        S.reduced = e.matches;
        if (S.mounted) render();
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    } catch (e) { /* 没有 matchMedia 就按正常动效 */ }
  }

  function updateClock() {
    var d = new Date();
    if (dom.clockTime) dom.clockTime.textContent = fmtClock(d);
    if (dom.clockDate) dom.clockDate.textContent = fmtDate(d);
    // 运行中的计时按本地时钟每秒推算（不轮询、不整块重绘面板）
    tickPanelTimers();
  }

  function tickPanelTimers() {
    if (!S.mounted || !dom.panelBody) return;
    var st = activeState();
    if (!st) return;
    var byKey = {};
    (st.projects || []).forEach(function (p) {
      (p.lines || []).forEach(function (l) { byKey[l.key] = l; });
    });
    var rows = dom.panelBody.querySelectorAll(".fx-line[data-line-key]");
    for (var i = 0; i < rows.length; i += 1) {
      var line = byKey[rows[i].dataset.lineKey];
      if (!line || line.status !== "running") continue;
      var el = rows[i].querySelector(".fx-line-state");
      if (!el) continue;
      var txt = line.kind === "pipeline" ? "运行中" : "运行中 " + fmtDur(lineRunSeconds(line));
      if (el.textContent !== txt) el.textContent = txt;
    }
  }

  // ---------- 数据加载 ----------
  function factoryReload() {
    if (S.loading) return;
    S.loading = true;
    // project.list 用来把厂区排成侧栏的 project_order（factory.state 按建库序返回）；
    // 取不到就沿用服务端顺序，不因为排序失败挡住工厂首屏。
    Promise.all([
      request("factory.state"),
      request("project.list").catch(function () { return null; }),
    ]).then(function (pair) {
      S.loading = false;
      var st = pair[0] || {};
      var order = ((pair[1] && pair[1].projects) || []).map(function (p) { return Number(p.id); });
      if (order.length && Array.isArray(st.projects)) {
        var rank = new Map();
        order.forEach(function (id, idx) { rank.set(id, idx); });
        st.projects.sort(function (a, b) {
          var ra = rank.has(a.id) ? rank.get(a.id) : 1e9;
          var rb = rank.has(b.id) ? rank.get(b.id) : 1e9;
          return ra - rb;
        });
      }
      applyState(st);
    }).catch(function (e) {
      S.loading = false;
      S.loaded = true;
      showNotice("<b>读取工厂数据失败</b><br>" + escapeHtml(e && e.message ? e.message : "未知错误") +
        "<div class=\"fx-notice-ops\"><button class=\"btn-ghost\" data-notice-retry=\"1\">重试</button></div>", true);
      bindNoticeOps();
      showPanelMessage("读取失败（" + escapeHtml(e && e.message ? e.message : "未知错误") + "）");
    });
  }

  function applyState(st) {
    var prevSig = S.worldSig;
    S.loaded = true;
    S.state = st;
    var cfg = st.config || {};
    applyNavVisibility(cfg.enabled !== false);

    // 没有真实产线 → 播放样板厂（只在内存；config.demo 默认 true）
    if (!hasRealLines(st) && cfg.demo !== false) {
      S.demo = buildDemoState(cfg);
    } else {
      S.demo = null;
    }

    var src = activeState();
    buildWorld(src);
    syncPackages(true);
    renderCounters();
    renderPanel();
    renderAttentionNotice(cfg);
    if (S.world && (!prevSig || prevSig !== S.worldSig)) fitCamera(false);
    renderAll();
  }

  function applyNavVisibility(v) {
    document.querySelectorAll('[data-act="factory"], [data-rail-act="factory"]').forEach(function (el) {
      el.classList.toggle("hidden", !v);
    });
  }

  function showPanelMessage(text) {
    if (dom.panelBody) dom.panelBody.innerHTML = '<div class="fx-empty">' + text + "</div>";
  }

  // ---------- 世界 ----------
  function capForRing(ring) { return 6 * (ring + 1); }

  function plotSpec(n) {
    if (n <= 2) return { w: 4, h: 3, cols: 2, rows: 1 };
    if (n <= 5) return { w: 5, h: 4, cols: 3, rows: 2 };
    return { w: 6, h: 5, cols: 3, rows: 2 };
  }

  function toTile(x, y) {
    return { x: x / TILE_W + y / TILE_H, y: y / TILE_H - x / TILE_W };
  }
  function iso(x, y) {
    return { x: (x - y) * (TILE_W / 2), y: (x + y) * (TILE_H / 2) };
  }

  // 台地尺寸按厂区平台外缘反推：菱形台面在四个斜方向上都必须兜住每一块厂区坪，
  // 否则厂区会悬在台地边缘外。k 取所有厂区坪尖角的 |x|/hw + |y|/hh 最大值。
  function makePlateau(factories) {
    var k = 1;
    factories.forEach(function (f) {
      [
        { x: f.x - f.halfW, y: f.y }, { x: f.x + f.halfW, y: f.y },
        { x: f.x, y: f.y - f.halfH }, { x: f.x, y: f.y + f.halfH },
      ].forEach(function (p) {
        k = Math.max(k, Math.abs(p.x) / 260 + Math.abs(p.y) / 125);
      });
    });
    k = clamp(k * 1.06, 1.05, 3.2);
    var hh = Math.round(125 * k);
    return {
      cx: 0, cy: Math.round(hh * 0.2),
      hw: Math.round(260 * k), hh: hh,
      th: Math.round(52 * Math.min(1.18, k)),
    };
  }

  function buildWorld(st) {
    var projects = (st && st.projects) || [];
    var hall = { x: 0, y: 0 };
    var factories = [];
    var lines = new Map();
    // 每环实际有几个厂区：只按真实数量均分一圈，别让 3 个项目挤在 6 槽的右半圈
    var ringCounts = [];
    var remainingProjects = projects.length;
    var rr = 0;
    while (remainingProjects > 0) {
      var take = Math.min(capForRing(rr), remainingProjects);
      ringCounts.push(take);
      remainingProjects -= take;
      rr += 1;
    }
    var i = 0;
    var ring = 0;
    var ringOffset = 0;
    projects.forEach(function (p) {
      while (ring < ringCounts.length && i - ringOffset >= ringCounts[ring]) {
        ringOffset += ringCounts[ring];
        ring += 1;
      }
      var count = ringCounts[ring] || 1;
      var slot = i - ringOffset;
      var angle = -Math.PI / 2 + (slot + 0.5) * Math.PI * 2 / count;
      var rx = 165 + ring * 145;
      var ry = 105 + ring * 95;
      var fx = Math.cos(angle) * rx;
      var fy = Math.sin(angle) * ry;
      var spec = plotSpec((p.lines || []).length);
      var f = {
        project: p, id: p.id, name: p.name, root_ok: p.root_ok !== false,
        x: fx, y: fy, w: spec.w, h: spec.h, lines: [],
        jumpStart: 0,
      };
      var n = spec.w + spec.h;
      f.halfW = 8 * n;
      f.halfH = 4 * n;
      f.entrance = { x: fx + (spec.w - spec.h) * 8, y: fy + (spec.w + spec.h) * 4 };
      f.tray = { x: fx + f.halfW * 0.72, y: fy + f.halfH * 0.10 };
      f.sheep = { x: fx - f.halfW * 0.62, y: fy + f.halfH * 0.28 };
      f.belt = routeBelt(hall, f.entrance, i);
      (p.lines || []).forEach(function (line, li) {
        if (line.kind === "cron" && line.status === "stopped") { /* 仍画机台：挂停工牌 */ }
        var col = li % spec.cols;
        var row = Math.floor(li / spec.cols);
        var mx = fx + (col - (spec.cols - 1) / 2) * 38;
        var my = fy + (row - (spec.rows - 1) / 2) * 26 - 2;
        var rec = {
          key: line.key, line: line, kind: line.kind,
          id: line.id, project: p, factory: f,
          x: mx, y: my, index: li, drawn: li < 6,
        };
        rec.path = f.belt.concat([{ x: mx, y: my }]);
        rec.transit = transitSeconds(rec.path);
        // 单厂最多画 6 台机台；其余只在面板 / 抽屉里列出（名牌标 +N）
        if (li < 6) f.lines.push(rec);
        lines.set(rec.key, rec);
      });
      factories.push(f);
      i += 1;
    });

    var plateau = makePlateau(factories);
    var site = buildIndustrialSite(hall, factories, plateau);

    var minX = hall.x - 70, maxX = hall.x + 70;
    var minY = hall.y - 50, maxY = hall.y + 50;
    factories.forEach(function (f) {
      minX = Math.min(minX, f.x - f.halfW - 20);
      maxX = Math.max(maxX, f.x + f.halfW + 20);
      minY = Math.min(minY, f.y - f.halfH - 26);
      maxY = Math.max(maxY, f.y + f.halfH + 26);
    });

    site.models.forEach(function (m) {
      if (m.bg) return;
      var points = m.path || [{ x: m.x, y: m.y }];
      var hw = m.hw || m.r || m.width || 8;
      var hh = m.hh || (m.r ? m.r * 0.5 : 5);
      // 高塔只算占地，不把塔顶算进取景：否则一座冷却塔就把整个场景压小一圈
      var height = Math.min(30, (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0)));
      points.forEach(function (pt) {
        minX = Math.min(minX, pt.x - hw - 8);
        maxX = Math.max(maxX, pt.x + hw + 8);
        minY = Math.min(minY, pt.y - height - hh - 16);
        maxY = Math.max(maxY, pt.y + hh + 12);
      });
    });

    // 台地左右与前沿都可以溢出画面，只把后缘压在地平线以下；全塞进取景会把厂区压成一小团
    minY = Math.min(minY, plateau.cy - plateau.hh);

    S.world = {
      hall: hall, factories: factories, lines: lines,
      models: site.models, site: site, plateau: plateau,
      bounds: { minX: minX, maxX: maxX, minY: minY, maxY: maxY },
    };
    S.worldSig = projects.map(function (p) {
      return p.id + ":" + (p.lines || []).map(function (l) { return l.key; }).join(",");
    }).join("|");
    S.selectedKey = null;
  }

  function buildIndustrialSite(hall, factories, plateau) {
    var rand = seeded(0x1D05721A);
    var models = [];
    var patches = [];
    var smoke = [];
    var sparks = [];
    var roads = [];
    var extentX = 300, extentY = 150;

    for (var b = 0; b < 64; b += 1) {
      var bx = 0, by = 0;
      for (var attempt = 0; attempt < 8; attempt += 1) {
        bx = (rand() * 2 - 1) * extentX;
        by = (rand() * 2 - 1) * extentY;
        if (!plateau) break;
        if (Math.abs(bx - plateau.cx) / plateau.hw + Math.abs(by - plateau.cy) / plateau.hh <= 0.96) break;
      }
      var br = 8 + rand() * 26;
      patches.push({ x: bx, y: by, rx: br, ry: br * (0.32 + rand() * 0.2), light: rand() < 0.5 });
    }

    // 台地表面的干裂纹：短折线，跟着沙地的走向散开
    var cracks = [];
    for (var ck = 0; ck < 7; ck += 1) {
      var cxp = (rand() * 2 - 1) * 240;
      var cyp = 20 + (rand() * 2 - 1) * 110;
      if (plateau && Math.abs(cxp - plateau.cx) / plateau.hw + Math.abs(cyp - plateau.cy) / plateau.hh > 0.86) continue;
      var seg = [{ x: cxp, y: cyp }];
      var ang = rand() * Math.PI * 2;
      for (var sp = 0; sp < 4; sp += 1) {
        ang += (rand() - 0.5) * 1.1;
        seg.push({ x: seg[sp].x + Math.cos(ang) * (9 + rand() * 12),
                   y: seg[sp].y + Math.sin(ang) * (4 + rand() * 7) });
      }
      cracks.push({ path: seg });
    }

    function addBox(x, y, hw, hh, h, role, opts) {
      var m = { kind: "box", x: x, y: y, hw: hw, hh: hh, h: h, role: role, z0: 0 };
      if (opts) for (var k in opts) if (Object.prototype.hasOwnProperty.call(opts, k)) m[k] = opts[k];
      models.push(m);
      return m;
    }
    function addCyl(x, y, r, h, role, opts) {
      var m = { kind: "cyl", x: x, y: y, r: r, h: h, role: role, z0: 0 };
      if (opts) for (var k in opts) if (Object.prototype.hasOwnProperty.call(opts, k)) m[k] = opts[k];
      models.push(m);
      return m;
    }

    factories.forEach(function (f) {
      var cx = f.x, cy = f.y, hw = f.halfW, hh = f.halfH;
      var baseZ = 5;
      var variant = Math.abs(hashKey("site" + f.id)) % 4;
      addBox(cx, cy, hw * 0.96, hh * 0.96, 5, "concrete", { paving: true, rail: true });

      var mainBx = cx - hw * 0.18, mainBy = cy - hh * 0.12;
      var topZ = baseZ + 38;

      if (variant === 1) {
        // 仓储型：两条长仓 + 小罐区 + 低办公楼
        mainBx = cx - hw * 0.34; mainBy = cy - hh * 0.20;
        addBox(mainBx, mainBy, hw * 0.19, hh * 0.58, 24, "steel",
               { z0: baseZ, windows: 2, door: true, roof: "saw" });
        addBox(cx + hw * 0.06, cy - hh * 0.26, hw * 0.17, hh * 0.48, 18, "steel",
               { z0: baseZ, windows: 2, roof: "flat" });
        addCyl(cx + hw * 0.30, cy - hh * 0.18, 2.0, 44, "steel", { z0: baseZ, bands: 2 });
        smoke.push({ x: cx + hw * 0.30, y: cy - hh * 0.18, h: baseZ + 44 });
        for (var ti = 0; ti < 3; ti += 1) {
          addCyl(cx + hw * (0.40 + ti * 0.10), cy + hh * (-0.06 + ti * 0.14),
                 hw * 0.06, 18, "rust", { z0: baseZ, bands: 2 });
        }
        addBox(cx + hw * 0.26, cy + hh * 0.26, hw * 0.12, hh * 0.24, 12, "glass",
               { z0: baseZ, windows: 2, roof: "flat" });
        topZ = baseZ + 44;
      } else if (variant === 2) {
        // 塔楼型：高层厂房 + 双烟囱 + 单筒仓 + 低附楼
        mainBx = cx - hw * 0.10; mainBy = cy - hh * 0.15;
        addBox(mainBx, mainBy, hw * 0.26, hh * 0.40, 56, "steel",
               { z0: baseZ, windows: 5, door: true, roof: "flat", vent: true });
        addCyl(mainBx - hw * 0.10, mainBy - hh * 0.02, 2.2, 70, "steel", { z0: baseZ, bands: 2 });
        addCyl(mainBx + hw * 0.10, mainBy - hh * 0.02, 2.2, 70, "steel", { z0: baseZ, bands: 2 });
        smoke.push({ x: mainBx - hw * 0.10, y: mainBy - hh * 0.02, h: baseZ + 70 });
        smoke.push({ x: mainBx + hw * 0.10, y: mainBy - hh * 0.02, h: baseZ + 70 });
        addBox(cx - hw * 0.52, cy + hh * 0.02, hw * 0.14, hh * 0.30, 18, "steel",
               { z0: baseZ, windows: 2, roof: "flat" });
        addCyl(cx + hw * 0.42, cy - hh * 0.05, hw * 0.11, 46, "rust",
               { z0: baseZ, bands: 3, ladder: true });
        addBox(cx + hw * 0.26, cy + hh * 0.28, hw * 0.12, hh * 0.22, 14, "glass",
               { z0: baseZ, windows: 2, roof: "flat" });
        topZ = baseZ + 70;
      } else if (variant === 3) {
        // 联排型：主厂房 + 侧翼 + 高筒仓 + 小罐
        mainBx = cx - hw * 0.20; mainBy = cy - hh * 0.14;
        addBox(mainBx, mainBy, hw * 0.28, hh * 0.50, 34, "steel",
               { z0: baseZ, windows: 3, door: true, roof: "saw" });
        addBox(cx + hw * 0.18, cy - hh * 0.04, hw * 0.22, hh * 0.28, 20, "steel",
               { z0: baseZ, windows: 2, roof: "saw" });
        addCyl(cx + hw * 0.46, cy - hh * 0.20, hw * 0.10, 58, "rust",
               { z0: baseZ, bands: 4, ladder: true });
        smoke.push({ x: cx + hw * 0.46, y: cy - hh * 0.20, h: baseZ + 58 });
        addCyl(cx + hw * 0.50, cy + hh * 0.15, hw * 0.06, 20, "rust",
               { z0: baseZ, bands: 2 });
        addCyl(mainBx - hw * 0.06, mainBy - hh * 0.02, 2.0, 50, "steel",
               { z0: baseZ, bands: 1 });
        smoke.push({ x: mainBx - hw * 0.06, y: mainBy - hh * 0.02, h: baseZ + 50 });
        topZ = baseZ + 58;
      } else {
        // 锯齿厂房型：主厂房 + 副仓 + 双筒仓
        mainBx = cx - hw * 0.18; mainBy = cy - hh * 0.12;
        addBox(mainBx, mainBy, hw * 0.31, hh * 0.52, 38, "steel",
               { z0: baseZ, windows: 3, door: true, roof: "saw" });
        addBox(cx - hw * 0.62, cy - hh * 0.02, hw * 0.14, hh * 0.34, 15, "steel",
               { z0: baseZ, windows: 1, roof: "saw" });
        addCyl(mainBx + hw * 0.13, mainBy - hh * 0.04, 2.4, 56, "steel",
               { z0: baseZ, bands: 2 });
        smoke.push({ x: mainBx + hw * 0.13, y: mainBy - hh * 0.04, h: baseZ + 56 });
        addCyl(cx + hw * 0.30, cy - hh * 0.10, hw * 0.11, 54, "rust",
               { z0: baseZ, bands: 4, ladder: true });
        smoke.push({ x: cx + hw * 0.30, y: cy - hh * 0.10, h: baseZ + 54 });
        addCyl(cx + hw * 0.47, cy + hh * 0.10, hw * 0.10, 48, "rust",
               { z0: baseZ, bands: 3, ladder: true });
        topZ = baseZ + 56;
      }

      // 处理机组（所有厂型共用）
      var px = cx + hw * 0.02, py = cy + hh * 0.30;
      addBox(px, py, hw * 0.17, hh * 0.46, 22, "rust",
             { z0: baseZ, windows: 1, roof: "flat", pipes: true });
      sparks.push({ x: px, y: py, h: baseZ + 22 });
      models.push({ kind: "conveyor",
        path: [{ x: mainBx + hw * 0.14, y: mainBy + hh * 0.24 }, { x: px, y: py }],
        h0: baseZ + 26, h1: baseZ + 16, width: 3.2, ore: true });
      f.lines.forEach(function (rec) {
        models.push({ kind: "machine", x: rec.x, y: rec.y, hw: 7.5, hh: 3.8,
                      h: 13, z0: baseZ, line: rec.line, rec: rec });
        models.push({ kind: "conveyor",
          path: [f.belt[f.belt.length - 1], { x: rec.x, y: rec.y }],
          h0: 12, h1: 12, width: 2.4, ore: false });
      });
      models.push({ kind: "pallet", x: f.tray.x, y: f.tray.y, z0: baseZ, projectId: f.id });
      models.push({ kind: "crate", x: cx - hw * 0.72, y: cy + hh * 0.45, s: 1, z0: baseZ });
      models.push({ kind: "barrel", x: cx + hw * 0.62, y: cy + hh * 0.52, s: 1, z0: baseZ });
      models.push({ kind: "ore", x: cx + hw * 0.22, y: cy + hh * 0.58, s: 1.1 });
      models.push({ kind: "worker", x: cx - hw * 0.55, y: cy + hh * 0.60, z0: baseZ });
      models.push({ kind: "truck", x: cx - hw * 0.24, y: cy + hh * 0.70, z0: baseZ });
      models.push({ kind: "figure", x: cx + hw * 0.34, y: cy + hh * 0.40, z0: baseZ });
      if (variant === 0 || variant === 2) {
        models.push({ kind: "gantry", x: cx + hw * 0.12, y: cy + hh * 0.62,
                      w: hw * 0.16, h: 26, z0: baseZ });
      }
      models.push({ kind: "conveyor", path: f.belt, h0: 16, h1: 27, width: 7, ore: true });
      f.labelZ = topZ + 6;
    });

    // 公用设施独立于项目产线，不表示跨项目供料或新的执行状态。
    // 冷却塔放在调度厅背后：两根蒸汽柱给中央天际线立起垂直参照，泵房与管廊把机组接回地面。
    addBox(-16, -64, 82, 16, 3, "concrete", { paving: true, rail: true });
    models.push({ kind: "cooling", x: -78, y: -64, r: 20, h: 62, z0: 3 });
    models.push({ kind: "cooling", x: 46, y: -72, r: 17, h: 52, z0: 3 });
    smoke.push({ x: -78, y: -64, h: 65, steam: true });
    smoke.push({ x: 46, y: -72, h: 55, steam: true });
    addBox(-52, -50, 13, 7, 11, "steel", { windows: 2, roof: "flat", vent: true });
    addCyl(68, -58, 6, 16, "rust", { bands: 2, ladder: true });
    models.push({ kind: "pipeRack", path: [{ x: -40, y: -53 }, { x: -62, y: -60 }], h0: 9, h1: 9, width: 7 });
    models.push({ kind: "pipeRack", path: [{ x: 62, y: -63 }, { x: 48, y: -71 }], h0: 8, h1: 8, width: 6 });

    // 台地左右两翼的物流堆场：集装箱、值班房与货车，填住厂区外圈的空地
    [-1, 1].forEach(function (side) {
      var yx = side * 232, yy = 62;
      addBox(yx, yy, 42, 22, 2, "concrete", { paving: true });
      addBox(yx - side * 8, yy - 6, 20, 9, 9, "rust", { windows: 1, roof: "flat" });
      addBox(yx + side * 20, yy + 10, 16, 8, 7, "steel", { roof: "flat" });
      addBox(yx - side * 24, yy + 12, 14, 7, 13, "steel", { windows: 1, roof: "flat", vent: true });
      addBox(yx + side * 2, yy + 16, 10, 5, 6, "rust", { roof: "flat" });
      models.push({ kind: "truck", x: yx + side * 12, y: yy - 14, z0: 2 });
      models.push({ kind: "crate", x: yx - side * 30, y: yy - 10, s: 1.2 });
      models.push({ kind: "barrel", x: yx + side * 28, y: yy - 2, s: 1 });
    });

    // 台地上的零散植被与碎石：让大块空地有尺度参照
    [
      { x: -250, y: -10, s: 2.2 }, { x: -205, y: 78, s: 1.7 }, { x: -120, y: 86, s: 1.3 },
      { x: 250, y: -6, s: 2.0 }, { x: 206, y: 82, s: 1.6 }, { x: 118, y: 92, s: 1.2 },
      { x: -8, y: 128, s: 1.4 }, { x: 56, y: 118, s: 1.1 },
    ].forEach(function (r) { models.push({ kind: "rock", x: r.x, y: r.y, s: r.s }); });
    [
      { x: -270, y: 40 }, { x: -212, y: -32 }, { x: -160, y: 96 }, { x: 268, y: 44 },
      { x: 214, y: -26 }, { x: 168, y: 98 }, { x: -30, y: 142 }, { x: 80, y: 130 },
    ].forEach(function (r) { models.push({ kind: "shrub", x: r.x, y: r.y, s: 1.3 }); });

    // 调度厅前场：地磅房与停在道边的货车
    addBox(24, 46, 30, 12, 2, "concrete", { paving: true });
    addBox(38, 44, 9, 6, 10, "steel", { windows: 1, roof: "flat" });
    models.push({ kind: "truck", x: 14, y: 52, z0: 2 });
    models.push({ kind: "crate", x: -4, y: 42, s: 1.1 });
    models.push({ kind: "barrel", x: 8, y: 40, s: 1 });

    // 前沿两座装卸场：给台地南半部一点重量，别让厂区下面空一大片
    [-1, 1].forEach(function (side) {
      var yx = side * 112, yy = 112;
      addBox(yx, yy, 34, 16, 2, "concrete", { paving: true });
      addBox(yx - side * 14, yy - 2, 16, 8, 8, "steel", { windows: 1, roof: "flat", vent: true });
      addBox(yx + side * 16, yy + 4, 11, 5, 6, "rust", { roof: "flat" });
      addBox(yx + side * 2, yy + 9, 9, 4, 5, "rust", { roof: "flat" });
      models.push({ kind: "gantry", x: yx + side * 6, y: yy - 8, w: 12, h: 20, z0: 2 });
      models.push({ kind: "truck", x: yx - side * 22, y: yy - 10, z0: 2 });
      models.push({ kind: "crate", x: yx + side * 26, y: yy - 6, s: 1.1 });
      models.push({ kind: "barrel", x: yx - side * 4, y: yy + 11, s: 1 });
    });
    // 台地前沿的货运站台：长条仓库 + 集装箱位，把画面下半部压住
    addBox(0, 148, 45, 10, 11, "steel", { windows: 3, roof: "flat", vent: true });
    addBox(0, 158, 40, 4, 3, "concrete", { paving: true });
    models.push({ kind: "gantry", x: -34, y: 154, w: 12, h: 18, z0: 3 });
    models.push({ kind: "gantry", x: 30, y: 154, w: 12, h: 18, z0: 3 });
    [-32, -12, 10, 30].forEach(function (cx, ci) {
      addBox(cx, 160, 8, 3.5, 7, ci % 2 ? "rust" : "steel", { roof: "flat" });
    });
    models.push({ kind: "truck", x: 8, y: 142, z0: 3 });
    models.push({ kind: "crate", x: -18, y: 142, s: 1.2 });

    roads.push(
      { path: [{ x: -112, y: 112 }, { x: -62, y: 104 }, { x: -34, y: 116 }, { x: -30, y: 146 }] },
      { path: [{ x: 112, y: 112 }, { x: 62, y: 104 }, { x: 34, y: 116 }, { x: 30, y: 146 }] },
      { path: [{ x: -50, y: 130 }, { x: -14, y: 140 }, { x: 22, y: 140 }] }
    );

    // 从台地边缘通到调度厅的两条厂区大道（从厂区之间的空档穿过去）
    roads.push(
      { path: [{ x: -300, y: 20 }, { x: -210, y: 16 }, { x: -120, y: 10 }, { x: -54, y: 4 }] },
      { path: [{ x: 300, y: 20 }, { x: 210, y: 16 }, { x: 120, y: 10 }, { x: 54, y: 4 }] }
    );

    // 远景工业带：储罐、仓库、输电塔、铁路、矿堆与卡车
    models.push({ kind: "box", x: 300, y: -96, hw: 58, hh: 30, h: 28, role: "steel", windows: 4, roof: "saw", bg: true });
    models.push({ kind: "cyl", x: 330, y: -60, r: 22, h: 44, role: "rust", bands: 4, bg: true });
    models.push({ kind: "cyl", x: 300, y: 10, r: 16, h: 34, role: "steel", bands: 3, bg: true });
    models.push({ kind: "box", x: -320, y: -100, hw: 50, hh: 26, h: 22, role: "steel", windows: 3, roof: "flat", bg: true });
    models.push({ kind: "cyl", x: -300, y: 40, r: 24, h: 40, role: "rust", bands: 4, bg: true });
    models.push({ kind: "pylon", x: -240, y: -95, bg: true });
    models.push({ kind: "pylon", x: 270, y: -100, bg: true });
    models.push({ kind: "pylon", x: 360, y: -40, bg: true });
    models.push({ kind: "rail", path: [{ x: -380, y: 100 }, { x: -120, y: 112 }, { x: 140, y: 122 }, { x: 380, y: 110 }], bg: true });
    models.push({ kind: "truck", x: -60, y: 108, z0: 0, bg: true });
    models.push({ kind: "truck", x: 80, y: 118, z0: 0, bg: true });
    models.push({ kind: "ore", x: 250, y: 112, s: 2.6, bg: true });
    models.push({ kind: "ore", x: -200, y: 114, s: 2.2, bg: true });
    models.push({ kind: "rock", x: -180, y: -110, s: 2, bg: true });
    models.push({ kind: "rock", x: 180, y: 60, s: 1.6, bg: true });
    models.push({ kind: "shrub", x: -100, y: 108, s: 1.3, bg: true });
    models.push({ kind: "shrub", x: 260, y: 100, s: 1.2, bg: true });
    models.push({ kind: "conveyor", path: [{ x: 280, y: -100 }, { x: 334, y: -56 }], h0: 16, h1: 10, width: 2.6, ore: true, bg: true });
    // 远景转运桥：接在储罐与仓库之间，只作天际线的横向层次，不参与厂区环线
    models.push({ kind: "transferBridge", path: [{ x: 292, y: -74 }, { x: 352, y: -30 }],
                  h0: 20, h1: 15, width: 8, bg: true });
    models.push({ kind: "box", x: 290, y: -76, hw: 10, hh: 6, h: 26, role: "steel", windows: 1, roof: "flat", bg: true });
    models.push({ kind: "box", x: 354, y: -28, hw: 9, hh: 5, h: 22, role: "rust", roof: "flat", bg: true });
    models.push({ kind: "box", x: -210, y: 92, hw: 26, hh: 14, h: 14, role: "steel", windows: 2, roof: "saw", bg: true });
    models.push({ kind: "cyl", x: -245, y: 104, r: 8, h: 18, role: "rust", bands: 2, bg: true });
    models.push({ kind: "box", x: 220, y: 96, hw: 22, hh: 12, h: 12, role: "steel", windows: 2, roof: "flat", bg: true });
    models.push({ kind: "cyl", x: 250, y: 108, r: 7, h: 16, role: "steel", bands: 2, bg: true });
    models.push({ kind: "truck", x: -150, y: 116, z0: 0, bg: true });
    models.push({ kind: "truck", x: 120, y: 124, z0: 0, bg: true });
    models.push({ kind: "rock", x: -90, y: 88, s: 1.4, bg: true });
    models.push({ kind: "shrub", x: 60, y: 98, s: 1.2, bg: true });
    models.push({ kind: "ore", x: -40, y: 120, s: 1.6, bg: true });
    var tracks = [
      { path: [{ x: -360, y: 102 }, { x: -160, y: 112 }, { x: 60, y: 120 }, { x: 300, y: 126 }] },
      { path: [{ x: -260, y: -40 }, { x: -120, y: 20 }, { x: 20, y: 90 }] },
    ];

    // 中央调度塔：石台 + 钢塔 + 玻璃顶 + 天线
    addBox(hall.x, hall.y, 46, 23, 8, "sand");
    addBox(hall.x, hall.y, 22, 11, 46, "steel",
           { z0: 8, windows: 4, door: true, roof: "flat", vent: true });
    addBox(hall.x, hall.y - 5, 13, 6.5, 58, "glass",
           { z0: 8, windows: 0, roof: "flat" });
    addCyl(hall.x, hall.y - 5, 2.6, 72, "steel", { z0: 8, bands: 1 });
    models.push({ kind: "radar", x: hall.x, y: hall.y - 5, z0: 66 });
    models.push({ kind: "catwalk", x: hall.x, y: hall.y, hw: 26, hh: 13, z0: 26 });
    models.push({ kind: "catwalk", x: hall.x, y: hall.y, hw: 26, hh: 13, z0: 44 });
    models.push({ kind: "figure", x: hall.x - 20, y: hall.y - 2, z0: 28 });
    models.push({ kind: "figure", x: hall.x + 16, y: hall.y + 2, z0: 46 });
    models.push({ kind: "figure", x: hall.x - 4, y: hall.y + 6, z0: 10 });
    sparks.push({ x: hall.x, y: hall.y, h: 18 });

    return { models: models, patches: patches, cracks: cracks, smoke: smoke, sparks: sparks,
             tracks: tracks, roads: roads };
  }

  function routeBelt(a, b, seed) {
    var at = toTile(a.x, a.y);
    var bt = toTile(b.x, b.y);
    var mid = iso(bt.x, at.y);
    // 同源多带错开 2px，避免完全重叠
    var off = ((seed || 0) % 3) * 2;
    return [
      { x: a.x, y: a.y },
      { x: mid.x + off, y: mid.y + off },
      { x: b.x + off, y: b.y + off },
    ];
  }

  function transitSeconds(pts) {
    var len = 0;
    for (var i = 1; i < pts.length; i += 1) {
      len += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    }
    return clamp(len / 34, 3, 12);
  }

  function pathLength(pts) {
    var len = 0;
    for (var i = 1; i < pts.length; i += 1) {
      len += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    }
    return len;
  }

  function pointAt(pts, t) {
    if (!pts || !pts.length) return { x: 0, y: 0 };
    if (t <= 0) return pts[0];
    if (t >= 1) return pts[pts.length - 1];
    var total = pathLength(pts);
    var want = total * t;
    var acc = 0;
    for (var i = 1; i < pts.length; i += 1) {
      var seg = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
      if (acc + seg >= want) {
        var k = seg ? (want - acc) / seg : 0;
        return {
          x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * k,
          y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * k,
        };
      }
      acc += seg;
    }
    return pts[pts.length - 1];
  }

  function focusProject(pid) {
    if (!S.world) return;
    var f = S.world.factories.find(function (x) { return x.id === pid; });
    if (!f) return;
    S.cam.tx = f.x;
    S.cam.ty = f.y;
    S.cam.tz = clamp(Math.max(S.cam.zoom, 0.9), 0.25, 2.4);
  }

  // ---------- 计数器 / 面板 ----------
  function renderAll() {
    renderCounters();
    renderPanel();
    render();
  }

  function renderCounters() {
    var st = activeState();
    var c = (st && st.counters) || {};
    if (dom.hudRunning) dom.hudRunning.textContent = String(c.running || 0);
    if (dom.hudQueued) dom.hudQueued.textContent = String(c.queued || 0);
    if (dom.hudAttention) dom.hudAttention.textContent = String(c.attention || 0);
    if (dom.hudDone) dom.hudDone.textContent = String(c.done_today || 0);
    if (dom.hudAttentionCell) dom.hudAttentionCell.classList.toggle("alert", (c.attention || 0) > 0);
  }

  function lineStateText(line) {
    if (line.status === "running") {
      // 装配线没有「本次运行」的句柄：只报运行中，不编造计时
      return line.kind === "pipeline" ? "运行中" : "运行中 " + fmtDur(lineRunSeconds(line));
    }
    if (line.kind === "pipeline") {
      var ready = (line.nodes || []).filter(function (n) { return n.status === "ready"; }).length;
      if (ready) return "排队 " + ready;
    }
    if (line.status === "paused") return "停工";
    if (line.status === "stopped") return "停工";
    if (line.status === "draft") return "草稿";
    if (line.status === "failed" || line.last_status === "error") return "上次失败";
    if (line.status === "done") return "已完成";
    return "待机";
  }

  function lineRunSeconds(line) {
    if (!line.running_run_id) return 0;
    var run = findRun(line.running_run_id);
    return run ? runElapsed(run) : 0;
  }

  function lineDotClass(line) {
    if (line.status === "running") return "running";
    if (line.status === "paused" || line.status === "stopped") return "paused";
    if (line.status === "failed" || line.last_status === "error") return "failed";
    if (line.status === "done") return "ok";
    return "";
  }

  function lineStateClass(line) {
    if (line.status === "running") return "running";
    if (line.status === "failed" || line.last_status === "error") return "failed";
    return "";
  }

  function renderPanel() {
    if (!dom.panelBody) return;
    var st = activeState();
    if (!st) { showPanelMessage("正在读取工厂数据…"); return; }
    var projects = st.projects || [];
    if (!projects.length) {
      dom.panelBody.innerHTML = '<div class="fx-empty">还没有项目。先在侧栏添加一个项目，' +
        '工厂就会为它建一座厂区。</div>';
      return;
    }
    var html = "";
    projects.forEach(function (p) {
      var lines = p.lines || [];
      var busy = lines.filter(function (l) { return l.status === "running"; }).length;
      html += '<div class="fx-proj">';
      html += '<button class="fx-proj-head" data-focus-project="' + escapeHtml(String(p.id)) + '" title="镜头飞到这座厂区">' +
        "<span>" + escapeHtml(p.name || "未命名项目") + "</span>" +
        '<span class="fx-badge' + (p.root_ok === false ? " warn" : "") + '">' +
        (p.root_ok === false ? "停工" : lines.length + " 线" + (busy ? " · " + busy + " 在忙" : "")) +
        "</span></button>";
      if (!lines.length) {
        html += '<div class="fx-empty">这个厂区还没有产线。' +
          '<button class="fx-add-line" data-add-project="' + escapeHtml(String(p.id)) + '">＋ 新建一条定时产线</button>' +
          "</div>";
      } else {
        lines.forEach(function (l) {
          var next = l.next_run_at ? " · 下次 " + fmtTs(l.next_run_at).slice(-5) : "";
          var stateText = lineStateText(l);
          html += '<button class="fx-line' + (S.selectedKey === l.key ? " active" : "") +
            '" data-line-key="' + escapeHtml(l.key) + '" title="点开看最近运行与派工">' +
            '<span class="fx-dot ' + lineDotClass(l) + '"></span>' +
            '<span class="fx-line-main">' +
            '<span class="fx-line-name">' + escapeHtml(l.name || l.key) + "</span>" +
            '<span class="fx-line-sub">' + escapeHtml(l.trigger_text || "") + escapeHtml(next) + "</span>" +
            "</span>" +
            '<span class="fx-line-state ' + lineStateClass(l) + '">' + escapeHtml(stateText) + "</span>" +
            "</button>";
        });
      }
      html += "</div>";
    });
    dom.panelBody.innerHTML = html;
  }

  function refreshPanelStates() {
    if (!S.mounted || !dom.panelBody) return;
    // 面板里正在输入（目前没有输入框）或焦点在面板上时不重建，避免抢焦点
    if (dom.panelBody.contains(document.activeElement)) return;
    renderPanel();
  }

  function renderAttentionNotice(cfg) {
    var st = activeState();
    if (!st) return;
    if (S.demo) {
      showNotice("<b>样板厂</b>：本机还没有真实产线，下面是演示数据——只在内存，不会真的运行。" +
        (hasRealLines(S.state) ? "" : "") +
        '<div class="fx-notice-ops"><button class="btn-ghost" data-notice-new="1">＋ 新建一条真实产线</button>' +
        '<button class="btn-ghost" data-notice-dismiss="1">知道了</button></div>', false);
      bindNoticeOps();
      return;
    }
    if (cfg && cfg.enabled === false) {
      showNotice("<b>工厂已在配置里关闭</b>：左侧导航入口已隐藏。重新启用后入口会回来。" +
        '<div class="fx-notice-ops"><button class="btn-ghost" data-notice-enable="1">重新启用</button></div>', false);
      bindNoticeOps();
      return;
    }
    hideNotice();
  }

  function showNotice(html, isError) {
    if (!dom.notice) return;
    dom.notice.classList.remove("hidden");
    dom.notice.classList.toggle("error", !!isError);
    dom.notice.innerHTML = html;
  }
  function hideNotice() {
    if (!dom.notice) return;
    dom.notice.classList.add("hidden");
    dom.notice.innerHTML = "";
  }
  function bindNoticeOps() {
    if (!dom.notice) return;
    var retry = dom.notice.querySelector("[data-notice-retry]");
    if (retry) retry.onclick = function () { hideNotice(); factoryReload(); };
    var add = dom.notice.querySelector("[data-notice-new]");
    if (add) add.onclick = function () { openNewLineDrawer(0); };
    var dismiss = dom.notice.querySelector("[data-notice-dismiss]");
    if (dismiss) dismiss.onclick = function () { hideNotice(); };
    var enable = dom.notice.querySelector("[data-notice-enable]");
    if (enable) enable.onclick = function () { saveConfig({ enabled: true }, enable); };
  }

  // ---------- 相机 ----------
  function fitCamera(animate) {
    var w = S.bufW, h = S.bufH;
    var b = (S.world && S.world.bounds) || { minX: -80, maxX: 80, minY: -50, maxY: 50 };
    var pad = 32;
    // 世界后缘要落在地平线以下：天空带的高度不参与取景，台地才不会顶进天里
    var skyH = Math.max(26, Math.round(h * 0.22) + 6);
    var z = Math.min((w - pad * 2) / Math.max(1, b.maxX - b.minX),
                     (h - skyH - 16) / Math.max(1, b.maxY - b.minY));
    z = clamp(z, 0.22, 1.45);
    var cx = (b.minX + b.maxX) / 2;
    var cy = b.minY + (h / 2 - skyH) / z;
    if (animate) {
      S.cam.tx = cx; S.cam.ty = cy; S.cam.tz = z;
    } else {
      S.cam.x = S.cam.tx = cx;
      S.cam.y = S.cam.ty = cy;
      S.cam.zoom = S.cam.tz = z;
      render();
    }
  }

  function zoomBy(k) {
    S.cam.tz = clamp(S.cam.tz * k, 0.22, 2.6);
  }

  // ---------- 场景尺寸 ----------
  function resizeScene(initial) {
    if (!dom.stage || !dom.canvas) return;
    var rect = dom.stage.getBoundingClientRect();
    S.cssW = Math.max(1, Math.round(rect.width));
    S.cssH = Math.max(1, Math.round(rect.height));
    var bw = Math.max(BUFFER_MIN_W, Math.round(S.cssW / 2));
    var bh = Math.max(BUFFER_MIN_H, Math.round(S.cssH / 2));
    if (dom.canvas.width !== bw || dom.canvas.height !== bh) {
      dom.canvas.width = bw;
      dom.canvas.height = bh;
    }
    S.bufW = bw;
    S.bufH = bh;
    var ctx = dom.canvas.getContext("2d");
    if (ctx) ctx.imageSmoothingEnabled = false;
    if (initial || !S.world) fitCamera(false);
    else render();
  }

  // ---------- 绘制基础 ----------
  function fillDiamond(ctx, cx, cy, hw, hh, color) {
    cx = Math.round(cx); cy = Math.round(cy);
    hw = Math.max(1, Math.round(hw)); hh = Math.max(1, Math.round(hh));
    ctx.fillStyle = color;
    for (var dy = -hh; dy < hh; dy += 1) {
      var t = 1 - Math.abs(dy + 0.5) / hh;
      var w = Math.max(1, Math.round(hw * t));
      ctx.fillRect(cx - w, cy + dy, w * 2, 1);
    }
  }

  function fillCircle(ctx, cx, cy, r, color) {
    cx = Math.round(cx); cy = Math.round(cy);
    r = Math.max(1, Math.round(r));
    ctx.fillStyle = color;
    for (var dy = -r; dy <= r; dy += 1) {
      var w = Math.round(Math.sqrt(Math.max(0, r * r - dy * dy)));
      ctx.fillRect(cx - w, cy + dy, Math.max(1, w * 2), 1);
    }
  }

  // 像素椭圆：逐行扫，边缘保持硬像素
  function fillOval(ctx, cx, cy, rx, ry, color) {
    cx = Math.round(cx); cy = Math.round(cy);
    rx = Math.max(1, Math.round(rx)); ry = Math.max(1, Math.round(ry));
    ctx.fillStyle = color;
    for (var dy = -ry; dy <= ry; dy += 1) {
      var t = 1 - (dy * dy) / (ry * ry);
      if (t <= 0) continue;
      var w = Math.max(1, Math.round(rx * Math.sqrt(t)));
      ctx.fillRect(cx - w, cy + dy, w * 2, 1);
    }
  }

  // 像素多边形（屋顶、树冠三角）
  function fillPoly(ctx, pts, color) {
    if (!pts || pts.length < 3) return;
    ctx.fillStyle = color;
    var minY = Infinity, maxY = -Infinity;
    for (var i = 0; i < pts.length; i += 1) {
      minY = Math.min(minY, pts[i].y);
      maxY = Math.max(maxY, pts[i].y);
    }
    for (var y = Math.floor(minY); y <= Math.ceil(maxY); y += 1) {
      var xs = [];
      for (var j = 0; j < pts.length; j += 1) {
        var a = pts[j], b = pts[(j + 1) % pts.length];
        if ((y >= a.y && y < b.y) || (y >= b.y && y < a.y)) {
          xs.push(a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y));
        }
      }
      xs.sort(function (p, q) { return p - q; });
      for (var k = 0; k + 1 < xs.length; k += 2) {
        var x0 = Math.round(xs[k]), x1 = Math.round(xs[k + 1]);
        ctx.fillRect(x0, y, Math.max(1, x1 - x0), 1);
      }
    }
  }

  function seeded(seed) {
    // 装饰布局用确定性伪随机（非安全用途）：每次刷新同一座工厂长得一样
    var s = seed >>> 0;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  function distToSeg(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay;
    var len2 = dx * dx + dy * dy;
    if (!len2) return Math.hypot(px - ax, py - ay);
    var t = clamp(((px - ax) * dx + (py - ay) * dy) / len2, 0, 1);
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  function linePath(ctx, pts, width, color) {
    if (!pts || pts.length < 2) return;
    ctx.fillStyle = color;
    var w = Math.max(1, Math.round(width));
    for (var i = 1; i < pts.length; i += 1) {
      var a = pts[i - 1], b = pts[i];
      var steps = Math.max(1, Math.round(Math.hypot(b.x - a.x, b.y - a.y)));
      for (var s = 0; s <= steps; s += 1) {
        var k = s / steps;
        ctx.fillRect(Math.round(a.x + (b.x - a.x) * k) - (w >> 1),
                     Math.round(a.y + (b.y - a.y) * k) - (w >> 1), w, w);
      }
    }
  }

  // 世界坐标 → 视口（内部缓冲）坐标；全部取整，避免半像素抖动
  function viewPoint(x, y) {
    var z = S.cam.zoom;
    return {
      x: Math.round((x - S.cam.x) * z + S.bufW / 2),
      y: Math.round((y - S.cam.y) * z + S.bufH / 2),
    };
  }
  function vlen(v) { return Math.max(1, Math.round(v * S.cam.zoom)); }

  // ---------- 渲染 ----------
  function render() {
    if (!S.mounted || !dom.canvas) return;
    // 窄屏（≤860px）画布被 CSS 隐藏：不白画，面板与抽屉仍是完整信息源
    if (S.cssW <= 1 || S.cssH <= 1) return;
    var cv = dom.canvas;
    var ctx = cv.getContext("2d");
    if (!ctx) return;
    var pal = palette();
    var W = S.bufW, H = S.bufH;
    var z = S.cam.zoom;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    S.night = pal === PAL_NIGHT;

    drawSkyGround(ctx, pal);
    drawTerrain(ctx, pal);
    drawGroundPatches(ctx, pal);

    S.hits = [];
    if (S.world) {
      drawShadows(ctx, pal);
      drawModels(ctx, pal);
      drawPackages(ctx, pal);
      drawEffects(ctx, pal);
      drawLighting(ctx, pal);
    }
    updateLabels();
  }

  // ---------- 工业场景：三维模型渲染 ----------
  // 所有建筑/筒仓/传送带都是三维体：给出底面中心、半宽、半深、高度，按「顶面 + 左面 + 右面」
  // 三块面着色；渲染时按屏幕底线排序（画家算法），再叠投影、烟雾、粉尘与火花。
  function roleCols(pal, role, bg) {
    if (bg) {
      if (role === "rust") return { top: pal.bgRustLight, left: pal.bgRust, right: pal.bgRustDark };
      if (role === "glass") return { top: pal.bgSteelLight, left: pal.bgSteel, right: pal.bgSteelDark };
      if (role === "sand") return { top: pal.bgHaze, left: pal.bgHaze, right: pal.bgHaze };
      return { top: pal.bgSteelLight, left: pal.bgSteel, right: pal.bgSteelDark };
    }
    if (role === "rust") return { top: pal.rustLight, left: pal.rust, right: pal.rustDark };
    if (role === "glass") return { top: pal.glassLight, left: pal.glass, right: pal.glassDark };
    if (role === "concrete") return { top: pal.concreteLight, left: pal.concrete, right: pal.concreteDark };
    if (role === "sand") return { top: pal.groundLight, left: pal.ground, right: pal.groundDark };
    return { top: pal.steelLight, left: pal.steel, right: pal.steelDark };
  }

  function drawSkyGround(ctx, pal) {
    var W = S.bufW, H = S.bufH;
    var skyH = Math.max(22, Math.round(H * 0.22));
    var sky = ctx.createLinearGradient(0, 0, 0, skyH + 26);
    sky.addColorStop(0, pal.skyTop);
    sky.addColorStop(0.62, pal.skyMid);
    sky.addColorStop(1, pal.skyLow);
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, W, skyH + 26);
    // 太阳光晕
    var sunX = Math.round(W * 0.76);
    var sunY = Math.round(skyH * 0.32);
    var glow = ctx.createRadialGradient(sunX, sunY, 2, sunX, sunY, Math.round(W * 0.34));
    glow.addColorStop(0, pal.sun);
    glow.addColorStop(1, "rgba(255, 210, 130, 0)");
    ctx.globalAlpha = S.night ? 0.14 : 0.34;
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, skyH + 26);
    ctx.globalAlpha = 1;
    // 夜空星点
    if (S.night) {
      ctx.fillStyle = pal.glassLight;
      for (var st = 0; st < 30; st += 1) {
        var stx = (st * 97 + 31) % W;
        var sty = (st * 53 + 17) % Math.max(4, skyH - 4);
        ctx.globalAlpha = 0.22 + (st % 4) * 0.12;
        ctx.fillRect(stx, sty, 1, 1);
      }
      ctx.globalAlpha = 1;
    }
    drawSkyline(ctx, pal, skyH, W);
    // 白天薄云：几团软边的暖色雾，给渐变天空一点层次
    if (!S.night) {
      ctx.globalAlpha = 0.15;
      for (var cl = 0; cl < 5; cl += 1) {
        var cx2 = (cl * 211 + 70) % W;
        var cy2 = Math.round(skyH * (0.16 + (cl % 3) * 0.13));
        var cw = Math.round(W * (0.05 + (cl % 2) * 0.03));
        fillOval(ctx, cx2, cy2, cw, Math.max(2, Math.round(skyH * 0.05)), pal.sun);
        fillOval(ctx, cx2 + cw * 0.55, cy2 + 1, cw * 0.6, Math.max(2, Math.round(skyH * 0.038)), pal.sun);
      }
      ctx.globalAlpha = 1;
    }
    // 沙地
    var ground = ctx.createLinearGradient(0, skyH, 0, H);
    ground.addColorStop(0, pal.groundLight);
    ground.addColorStop(0.24, pal.ground);
    ground.addColorStop(1, pal.groundDark);
    ctx.fillStyle = ground;
    ctx.fillRect(0, skyH, W, H - skyH);
    // 远景沙丘：屏幕空间、只压在地平线以下，给远处的仓库与储罐一个地平面
    for (var dune = 0; dune < 9; dune += 1) {
      var dx2 = (dune * 137 + 53) % W;
      var dy2 = skyH + 16 + ((dune * 71) % Math.max(8, Math.round(H * 0.14)));
      var dw2 = Math.round(W * (0.05 + (dune % 4) * 0.012));
      ctx.globalAlpha = 0.10 + (dune % 3) * 0.03;
      fillOval(ctx, dx2, dy2, dw2, Math.max(2, Math.round(H * 0.012)), pal.groundDark);
    }
    ctx.globalAlpha = 1;
    // 地平线雾带
    var haze = ctx.createLinearGradient(0, skyH - 4, 0, skyH + 28);
    haze.addColorStop(0, pal.bgHaze);
    haze.addColorStop(1, "rgba(0,0,0,0)");
    ctx.globalAlpha = S.night ? 0.16 : 0.22;
    ctx.fillStyle = haze;
    ctx.fillRect(0, skyH - 4, W, 32);
    ctx.globalAlpha = 1;
    // 远处沙丘剪影
    ctx.globalAlpha = S.night ? 0.30 : 0.22;
    for (var i = 0; i < 7; i += 1) {
      var mx = Math.round((i + 0.5) * W / 7);
      var mw = Math.round(W / 12 + (i % 3) * 6);
      var mh = Math.round(5 + (i % 4) * 4);
      fillPoly(ctx, [
        { x: mx - mw, y: skyH + 2 },
        { x: mx, y: skyH - mh },
        { x: mx + mw, y: skyH + 2 },
      ], pal.groundDark);
    }
    ctx.globalAlpha = 1;
  }

  function drawTerrain(ctx, pal) {
    var z = S.cam.zoom;
    var pl = (S.world && S.world.plateau) || { cx: 0, cy: 0, hw: 260, hh: 125, th: 48 };
    var c = viewPoint(pl.cx, pl.cy);
    var hw = Math.max(120, Math.round(pl.hw * z));
    var hh = Math.max(60, Math.round(pl.hh * z));
    var th = Math.max(20, Math.round(pl.th * z));
    fillDiamond(ctx, c.x + Math.round(12 * z), c.y + th + Math.round(8 * z), hw, hh, pal.shadow);
    // 崖壁：左右两面
    fillPoly(ctx, [
      { x: c.x - hw, y: c.y }, { x: c.x, y: c.y + hh },
      { x: c.x, y: c.y + hh + th }, { x: c.x - hw, y: c.y + th },
    ], pal.stoneDark);
    fillPoly(ctx, [
      { x: c.x, y: c.y + hh }, { x: c.x + hw, y: c.y },
      { x: c.x + hw, y: c.y + th }, { x: c.x, y: c.y + hh + th },
    ], pal.groundDark);
    // 岩层横纹
    for (var i = 1; i <= 3; i += 1) {
      var dy = Math.round(th * i / 4);
      linePath(ctx, [{ x: c.x - hw, y: c.y + dy }, { x: c.x, y: c.y + hh + dy }], 1, pal.stone);
      linePath(ctx, [{ x: c.x, y: c.y + hh + dy }, { x: c.x + hw, y: c.y + dy }], 1, pal.groundLight);
    }
    // 台地顶面
    fillDiamond(ctx, c.x, c.y, hw, hh, pal.ground);
    linePath(ctx, [{ x: c.x - hw, y: c.y }, { x: c.x, y: c.y - hh }], 1, pal.groundHi);
    linePath(ctx, [{ x: c.x, y: c.y - hh }, { x: c.x + hw, y: c.y }], 1, pal.groundHi);
    linePath(ctx, [{ x: c.x - hw, y: c.y }, { x: c.x, y: c.y + hh }], 2, pal.stone);
    linePath(ctx, [{ x: c.x, y: c.y + hh }, { x: c.x + hw, y: c.y }], 2, pal.stone);
    // 崖壁裂缝
    ctx.fillStyle = pal.roadDark;
    for (var ck = 0; ck < 5; ck += 1) {
      var ct = (ck + 1) / 6;
      ctx.fillRect(Math.round(c.x - hw + hw * ct), Math.round(c.y + hh * ct + th * 0.18),
                   1, Math.round(th * 0.5));
      ctx.fillRect(Math.round(c.x + hw * (1 - ct)), Math.round(c.y + hh * ct + th * 0.2),
                   1, Math.round(th * 0.42));
    }
    // 崖底碎石
    for (var r = 0; r < 12; r += 1) {
      var t = (r + 0.5) / 12;
      var side = r % 2 ? 1 : -1;
      var bx = c.x + side * (hw - hw * t) - Math.round(t * 8);
      var by = c.y + hh * t + th + Math.round((r % 3) * 2 * z);
      fillOval(ctx, bx, by, Math.max(2, Math.round(4 * z)), Math.max(1, Math.round(2 * z)), pal.groundDark);
      ctx.fillStyle = pal.stoneLight;
      ctx.fillRect(bx, by - 1, 1, 1);
    }
  }

  function drawGroundPatches(ctx, pal) {
    var site = S.world && S.world.site;
    if (!site) return;
    var z = S.cam.zoom;
    ctx.globalAlpha = 0.16;
    site.patches.forEach(function (b) {
      var p = viewPoint(b.x, b.y);
      fillOval(ctx, p.x, p.y, Math.max(2, Math.round(b.rx * z)),
               Math.max(1, Math.round(b.ry * z)),
               b.light ? pal.groundHi : pal.groundDark);
    });
    ctx.globalAlpha = 1;
    (site.cracks || []).forEach(function (ck) {
      var pts = ck.path.map(function (q) { return viewPoint(q.x, q.y); });
      linePath(ctx, pts, 1, pal.groundDark);
      linePath(ctx, pts.map(function (q) { return { x: q.x, y: q.y - 1 }; }), 1, pal.groundHi);
    });
    // 传送带下方的土路与车辙
    S.world.factories.forEach(function (f) {
      var pts = f.belt.map(function (p) { return viewPoint(p.x, p.y); });
      linePath(ctx, pts, vlen(8), pal.roadDark);
      linePath(ctx, pts, vlen(6), pal.road);
      linePath(ctx, pts, vlen(1), pal.roadLight);
      var total = pathLength(pts);
      for (var d = 0; d < total; d += Math.max(5, vlen(16))) {
        var pt = pointAt(pts, total ? d / total : 0);
        ctx.fillStyle = pal.roadDark;
        ctx.fillRect(Math.round(pt.x) - 3, Math.round(pt.y) - 1, 2, 1);
        ctx.fillRect(Math.round(pt.x) + 1, Math.round(pt.y) + 1, 2, 1);
      }
    });
    (site.roads || []).forEach(function (rd) {
      var pts = rd.path.map(function (q) { return viewPoint(q.x, q.y); });
      linePath(ctx, pts, vlen(7), pal.roadDark);
      linePath(ctx, pts, vlen(5), pal.road);
      linePath(ctx, pts.map(function (q) { return { x: q.x - 1, y: q.y }; }),
               Math.max(1, vlen(1)), pal.roadLight);
      linePath(ctx, pts.map(function (q) { return { x: q.x + 1, y: q.y }; }),
               Math.max(1, vlen(1)), pal.roadLight);
    });
    (site.tracks || []).forEach(function (tr) {
      var pts = tr.path.map(function (q) { return viewPoint(q.x, q.y); });
      linePath(ctx, pts, vlen(3), pal.roadDark);
      linePath(ctx, pts, Math.max(1, vlen(1)), pal.roadLight);
    });
  }

  function drawShadows(ctx, pal) {
    var z = S.cam.zoom;
    S.world.models.forEach(function (m) {
      // 远景模型也要有接地影，否则雾里一排白色方块看着像浮在天上
      ctx.globalAlpha = m.bg ? 0.45 : 1;
      var p = viewPoint(m.x, m.y);
      if (m.kind === "box") {
        var hw = m.hw * z, hh = m.hh * z;
        var off = Math.min(20, (m.h || 10) * z * 0.22);
        fillDiamond(ctx, p.x + off * 0.7, p.y + off * 0.45, hw * 1.02, hh * 1.02, pal.shadow);
      } else if (m.kind === "cyl") {
        var rx = m.r * z, ry = Math.max(1, rx * 0.55);
        var off2 = Math.min(22, m.h * z * 0.2);
        fillOval(ctx, p.x + off2 * 0.7, p.y + off2 * 0.45, rx * 1.1, ry * 1.1, pal.shadow);
      } else if (m.kind === "cooling") {
        fillOval(ctx, p.x + vlen(9), p.y + vlen(4), vlen(m.r * 1.1), vlen(m.r * 0.45), pal.shadow);
      } else if (m.path) {
        var pts = m.path.map(function (q) { return viewPoint(q.x, q.y); });
        linePath(ctx, pts, vlen(m.width || 6), pal.shadow);
      }
    });
    ctx.globalAlpha = 1;
  }

  function drawModels(ctx, pal) {
    var bg = [], main = [];
    S.world.models.forEach(function (m) {
      var mx = m.x, my = m.y;
      if (m.path && m.path.length) {
        var mid = pointAt(m.path, 0.5);
        mx = mid.x; my = mid.y;
      }
      var item = { m: m, d: viewPoint(mx, my).y };
      if (m.bg) bg.push(item); else main.push(item);
    });
    bg.sort(function (a, b) { return a.d - b.d; });
    main.sort(function (a, b) { return a.d - b.d; });
    bg.forEach(function (it) { drawModel(ctx, pal, it.m); });
    main.forEach(function (it) { drawModel(ctx, pal, it.m); });
  }

  function drawModel(ctx, pal, m) {
    if (m.kind === "box") drawBoxModel(ctx, pal, m);
    else if (m.kind === "cyl") drawCylinderModel(ctx, pal, m);
    else if (m.kind === "conveyor") drawConveyorModel(ctx, pal, m);
    else if (m.kind === "machine") drawMachineModel(ctx, pal, m);
    else if (m.kind === "pallet") drawPalletModel(ctx, pal, m);
    else if (m.kind === "crate") drawCrateModel(ctx, pal, m);
    else if (m.kind === "barrel") drawBarrelModel(ctx, pal, m);
    else if (m.kind === "ore") drawOreModel(ctx, pal, m);
    else if (m.kind === "worker") drawWorkerModel(ctx, pal, m);
    else if (m.kind === "truck") drawTruckModel(ctx, pal, m);
    else if (m.kind === "radar") drawRadarModel(ctx, pal, m);
    else if (m.kind === "pylon") drawPylonModel(ctx, pal, m);
    else if (m.kind === "rail") drawRailModel(ctx, pal, m);
    else if (m.kind === "rock") drawRockModel(ctx, pal, m);
    else if (m.kind === "shrub") drawShrubModel(ctx, pal, m);
    else if (m.kind === "gantry") drawGantryModel(ctx, pal, m);
    else if (m.kind === "catwalk") drawCatwalkModel(ctx, pal, m);
    else if (m.kind === "figure") drawFigureModel(ctx, pal, m);
    else if (m.kind === "cooling") drawCoolingTowerModel(ctx, pal, m);
    else if (m.kind === "pipeRack") drawPipeRackModel(ctx, pal, m);
    else if (m.kind === "transferBridge") drawTransferBridgeModel(ctx, pal, m);
  }

  function drawTruckModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var L = Math.max(10, Math.round(26 * z));
    var W = Math.max(4, Math.round(9 * z));
    var steel = m.bg ? pal.bgSteel : pal.steel;
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var rust = m.bg ? pal.bgRust : pal.rust;
    var rustDark = m.bg ? pal.bgRustDark : pal.rustDark;
    var rustLight = m.bg ? pal.bgRustLight : pal.rustLight;
    var wheel = m.bg ? pal.bgSteelDark : pal.ink;
    fillOval(ctx, p.x, baseY + 1, L * 0.5, W * 0.4, pal.shadow);
    ctx.fillStyle = wheel;
    for (var i = 0; i < 3; i += 1) {
      fillOval(ctx, p.x - L * 0.3 + i * L * 0.28, baseY,
               Math.max(1, W * 0.28), Math.max(1, W * 0.16), wheel);
    }
    drawIsoBoxAt(ctx, p.x - L * 0.08, baseY - 2, L * 0.34, W * 0.3,
                 Math.max(5, Math.round(9 * z)), steelLight, steel, steelDark);
    drawIsoBoxAt(ctx, p.x + L * 0.38, baseY - 2, L * 0.15, W * 0.28,
                 Math.max(4, Math.round(7 * z)), rustLight, rust, rustDark);
    ctx.fillStyle = pal.windowDark;
    ctx.fillRect(Math.round(p.x + L * 0.45), Math.round(baseY - 2 - 6 * z),
                 Math.max(1, Math.round(3 * z)), Math.max(1, Math.round(2 * z)));
  }

  function drawRadarModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(4, Math.round(7 * z));
    ctx.fillStyle = pal.steelDark;
    ctx.fillRect(p.x, baseY - h, 1, h);
    var a = S.rot * 2;
    var dx = Math.round(Math.cos(a) * 3 * z);
    var dy = Math.round(Math.sin(a) * 1.5 * z);
    var rx = Math.max(2, Math.round(4 * z));
    var ry = Math.max(1, Math.round(2 * z));
    fillOval(ctx, p.x + dx, baseY - h - ry, rx, ry, pal.steelLight);
    ctx.fillStyle = pal.steelDark;
    ctx.fillRect(p.x + dx, baseY - h - ry, Math.max(1, Math.abs(dx) + 1), 1);
    if (S.night) {
      ctx.fillStyle = pal.bad;
      ctx.fillRect(p.x - 1, baseY - h - ry * 2 - 2, 2, 2);
    }
  }

  function drawIsoBoxAt(ctx, x, y, hw, hh, h, top, left, right) {
    fillPoly(ctx, [
      { x: x - hw, y: y - h }, { x: x, y: y - h + hh },
      { x: x, y: y + hh }, { x: x - hw, y: y },
    ], left);
    fillPoly(ctx, [
      { x: x, y: y - h + hh }, { x: x + hw, y: y - h },
      { x: x + hw, y: y }, { x: x, y: y + hh },
    ], right);
    fillDiamond(ctx, x, y - h, hw, hh, top);
  }

  function drawBoxModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(2, Math.round(m.hw * z));
    var hh = Math.max(1, Math.round(m.hh * z));
    var h = Math.max(2, Math.round(m.h * z));
    var cols = roleCols(pal, m.role, m.bg);
    drawIsoBoxAt(ctx, base.x, baseY, hw, hh, h, cols.top, cols.left, cols.right);
    // 瓦楞竖线（只画左面）
    if (m.role === "steel") {
      ctx.fillStyle = cols.right;
      for (var u = 0.12; u < 0.95; u += 0.12) {
        var wx = Math.round(base.x - hw + hw * u);
        var wy = Math.round(baseY - h + hh * u);
        ctx.fillRect(wx, wy, 1, Math.max(1, h));
      }
    }
    if (!m.paving && m.h >= 14) {
      linePath(ctx, [{ x: base.x - hw, y: baseY - h }, { x: base.x, y: baseY - h + hh },
                    { x: base.x + hw, y: baseY - h }], 1, cols.top);
      linePath(ctx, [{ x: base.x - hw, y: baseY - 2 }, { x: base.x, y: baseY + hh - 2 },
                    { x: base.x + hw, y: baseY - 2 }], Math.max(1, vlen(2)), cols.right);
      for (var seam = 1; seam < 5; seam += 1) {
        var st = seam / 5;
        linePath(ctx, [{ x: base.x + hw * st, y: baseY - h + hh * (1 - st) },
                      { x: base.x + hw * st, y: baseY + hh * (1 - st) }], 1, cols.left);
      }
      if (!m.bg && hw > 9) {
        var ventX = base.x + hw * 0.50, ventY = baseY - h * 0.5 + hh * 0.50;
        for (var grille = 0; grille < 3; grille += 1) {
          linePath(ctx, [{ x: ventX, y: ventY + grille * 2 },
                        { x: ventX + hw * 0.24, y: ventY - hh * 0.24 + grille * 2 }], 1, pal.metalDark);
        }
      }
    }
    // 暖窗：左面平行四边形
    if (m.windows) {
      var n = m.windows;
      for (var i = 0; i < n; i += 1) {
        var u0 = n === 1 ? 0.48 : 0.2 + i * (0.6 / (n - 1)) - 0.05;
        var v0 = 0.3, du = 0.1, dv = 0.16;
        var wpt = function (uu, vv) {
          return {
            x: base.x - hw + hw * uu,
            y: baseY - h + hh * uu + h * vv,
          };
        };
        fillPoly(ctx, [
          wpt(u0, v0), wpt(u0 + du, v0), wpt(u0 + du, v0 + dv), wpt(u0, v0 + dv),
        ], S.night ? pal.window : pal.windowDark);
        if (S.night) {
          var wc = wpt(u0 + du / 2, v0 + dv / 2);
          ctx.globalAlpha = 0.24;
          fillOval(ctx, wc.x, wc.y, Math.max(3, hw * 0.22), Math.max(2, hw * 0.14), pal.window);
          ctx.globalAlpha = 1;
        }
      }
    }
    // 大门
    if (m.door) {
      var dpt = function (uu, vv) {
        return {
          x: base.x - hw + hw * uu,
          y: baseY - h + hh * uu + h * vv,
        };
      };
      fillPoly(ctx, [
        dpt(0.46, 0.56), dpt(0.63, 0.56), dpt(0.63, 0.98), dpt(0.46, 0.98),
      ], pal.windowDark);
    }
    // 屋顶：锯齿 / 平顶
    if (m.vent) {
      for (var vv = 0; vv < 3; vv += 1) {
        var vx = base.x - hw * 0.4 + vv * hw * 0.4;
        var vy = baseY - h - hh * 0.35 + vv * hh * 0.2;
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(vx), Math.round(vy), Math.max(1, Math.round(3 * z)), Math.max(1, Math.round(2 * z)));
        ctx.fillStyle = pal.metalLight;
        ctx.fillRect(Math.round(vx), Math.round(vy), Math.max(1, Math.round(2 * z)), 1);
      }
    }
    if (m.roof === "saw") {
      for (var r = 0; r < 3; r += 1) {
        var rx0 = base.x - hw + hw * (0.1 + r * 0.28);
        var ry0 = baseY - h - hh * 0.55;
        var rise = Math.max(2, Math.round(h * 0.2));
        fillPoly(ctx, [
          { x: rx0, y: ry0 },
          { x: rx0 + hw * 0.48, y: ry0 + hh * 0.48 },
          { x: rx0 + hw * 0.48, y: ry0 + hh * 0.48 - rise },
          { x: rx0, y: ry0 - rise },
        ], r % 2 ? cols.top : cols.left);
      }
    } else {
      ctx.fillStyle = cols.right;
      ctx.fillRect(base.x - hw, baseY - h - 1, hw * 2, 1);
    }
    // 外露管线
    if (m.pipes) {
      ctx.fillStyle = pal.metalLight;
      ctx.fillRect(base.x - Math.round(hw * 0.7), baseY - h - Math.round(hh * 0.2),
                   Math.max(1, Math.round(hw * 1.4)), 1);
      ctx.fillRect(base.x + Math.round(hw * 0.3), baseY - h - Math.round(hh * 0.6),
                   1, Math.max(2, Math.round(hh * 0.6)));
    }
    // 场院铺装：顶面石缝
    if (m.paving) {
      var ty = baseY - h;
      ctx.globalAlpha = 0.5;
      for (var pi = -1; pi <= 1; pi += 1) {
        linePath(ctx, [
          { x: base.x - hw * 0.72, y: ty + pi * hh * 0.42 - hh * 0.08 },
          { x: base.x + hw * 0.72, y: ty + pi * hh * 0.42 + hh * 0.08 },
        ], 1, pal.groundDark);
      }
      ctx.globalAlpha = 1;
    }
    // 站台围栏：两条前缘立短柱 + 横杆
    if (m.rail) {
      var railTop = baseY - h;
      var ph = Math.max(2, Math.round(4 * z));
      for (var ri = 0; ri <= 6; ri += 1) {
        var rt = ri / 6;
        var lx = base.x - hw + hw * rt;
        var ly = railTop + hh * rt;
        ctx.fillStyle = pal.steelDark;
        ctx.fillRect(Math.round(lx), Math.round(ly - ph), 1, ph);
        var rx2 = base.x + hw - hw * rt;
        ctx.fillRect(Math.round(rx2), Math.round(ly - ph), 1, ph);
      }
      linePath(ctx, [{ x: base.x - hw, y: railTop }, { x: base.x, y: railTop + hh }],
               1, pal.steelLight);
      linePath(ctx, [{ x: base.x, y: railTop + hh }, { x: base.x + hw, y: railTop }],
               1, pal.steelLight);
    }
  }

  function drawCylinderModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var rx = Math.max(2, Math.round(m.r * z));
    var ry = Math.max(1, Math.round(rx * 0.5));
    var h = Math.max(3, Math.round(m.h * z));
    var cols = roleCols(pal, m.role, m.bg);
    // 底部弧、筒身、左右明暗
    fillOval(ctx, base.x, baseY, rx, ry, cols.right);
    ctx.fillStyle = cols.left;
    ctx.fillRect(base.x - rx, baseY - h, rx * 2, h);
    ctx.fillStyle = cols.top;
    ctx.fillRect(base.x - Math.round(rx * 0.55), baseY - h, Math.round(rx * 0.7), h);
    ctx.fillStyle = cols.right;
    ctx.fillRect(base.x + Math.round(rx * 0.55), baseY - h,
                 Math.max(1, Math.round(rx * 0.45)), h);
    // 箍圈
    if (m.bands) {
      for (var i = 1; i <= m.bands; i += 1) {
        var by = Math.round(baseY - h * i / (m.bands + 1));
        ctx.fillStyle = cols.right;
        ctx.fillRect(base.x - rx, by, rx * 2, 1);
        ctx.fillStyle = cols.top;
        ctx.fillRect(base.x - Math.round(rx * 0.5), by, Math.round(rx * 0.4), 1);
      }
    }
    // 顶盖
    fillOval(ctx, base.x, baseY - h, rx, ry, cols.top);
    fillOval(ctx, base.x, baseY - h, Math.max(1, rx - 2), Math.max(1, ry - 1), cols.left);
    // 爬梯
    if (m.ladder) {
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(base.x - Math.round(rx * 0.2), baseY - h + 2, 1, h - 3);
      for (var l = 2; l < h - 2; l += 4) {
        ctx.fillRect(base.x - Math.round(rx * 0.2), baseY - h + l,
                     Math.max(2, Math.round(rx * 0.35)), 1);
      }
    }
    if (S.night) {
      ctx.fillStyle = pal.window;
      ctx.fillRect(base.x - 1, baseY - h - ry, 2, 2);
    }
  }

  function drawConveyorModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var pts = m.path.map(function (p) { return viewPoint(p.x, p.y); });
    var total = pathLength(pts);
    if (!total) return;
    var w = Math.max(2, Math.round(m.width * z));
    var steel = m.bg ? pal.bgSteel : pal.steel;
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var ore = m.bg ? pal.bgRust : pal.ore;
    var oreDark = m.bg ? pal.bgRustDark : pal.oreDark;
    // 桁架支腿
    var step = Math.max(8, vlen(20));
    for (var d = 0; d < total; d += step) {
      var t = d / total;
      var pt = pointAt(pts, t);
      var hh = (m.h0 + (m.h1 - m.h0) * t) * z;
      ctx.fillStyle = steelDark;
      ctx.fillRect(Math.round(pt.x), Math.round(pt.y - hh), 1, Math.max(2, Math.round(hh)));
      ctx.fillStyle = steel;
      ctx.fillRect(Math.round(pt.x) - 2, Math.round(pt.y - hh * 0.55), 1, 1);
    }
    // 带面
    for (var i = 1; i < pts.length; i += 1) {
      var a = pts[i - 1], b = pts[i];
      var ta = (i - 1) / Math.max(1, pts.length - 1);
      var tb = i / Math.max(1, pts.length - 1);
      var ya = a.y - (m.h0 + (m.h1 - m.h0) * ta) * z;
      var yb = b.y - (m.h0 + (m.h1 - m.h0) * tb) * z;
      var dx = b.x - a.x, dy = b.y - a.y;
      var len = Math.hypot(dx, dy) || 1;
      var nx = -dy / len * w / 2, ny = dx / len * w / 2;
      // 侧面
      linePath(ctx, [{ x: a.x, y: ya + 2 }, { x: b.x, y: yb + 2 }],
               Math.max(1, Math.round(w * 0.5)), steelDark);
      fillPoly(ctx, [
        { x: a.x + nx, y: ya + ny }, { x: a.x - nx, y: ya - ny },
        { x: b.x - nx, y: yb - ny }, { x: b.x + nx, y: yb + ny },
      ], steel);
      // 矿石与高光
      var mid = pointAt([a, b], 0.5);
      if (m.ore) {
        ctx.fillStyle = ore;
        ctx.fillRect(Math.round(mid.x - w * 0.3), Math.round((ya + yb) / 2 - 1),
                     Math.max(1, Math.round(w * 0.6)), 1);
        ctx.fillStyle = oreDark;
        ctx.fillRect(Math.round(mid.x - w * 0.1), Math.round((ya + yb) / 2), 1, 1);
      }
      ctx.fillStyle = steelLight;
      ctx.fillRect(Math.round(mid.x + nx * 0.5), Math.round((ya + yb) / 2 + ny * 0.5), 1, 1);
      // 两侧护栏亮线：让产线从底下的土路里跳出来
      linePath(ctx, [{ x: a.x + nx, y: ya + ny }, { x: b.x + nx, y: yb + ny }], 1, steelLight);
      linePath(ctx, [{ x: a.x - nx, y: ya - ny }, { x: b.x - nx, y: yb - ny }], 1, steelLight);
    }
  }

  function drawMachineModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(3, Math.round(m.hw * z));
    var hh = Math.max(1, Math.round(m.hh * z));
    var h = Math.max(4, Math.round(m.h * z));
    var line = m.line;
    var cols = roleCols(pal, "steel");
    fillDiamond(ctx, base.x, baseY, hw, hh, pal.steelDark);
    drawIsoBoxAt(ctx, base.x, baseY, hw, hh, h, cols.top, cols.left, cols.right);
    // 产线色带
    var tones = [pal.rust, pal.glassLight, pal.ok, pal.product];
    ctx.fillStyle = tones[Math.abs(hashKey(m.rec.key)) % tones.length];
    ctx.fillRect(base.x - hw, baseY - h, Math.max(1, hw * 2), Math.max(1, Math.round(z)));
    // 状态灯
    var lamp = pal.idle;
    if (line.status === "paused" || line.status === "stopped") lamp = pal.warn;
    else if (line.status === "failed" || line.last_status === "error") lamp = pal.bad;
    else if (line.status === "running") lamp = pal.ok;
    ctx.fillStyle = lamp;
    ctx.fillRect(base.x - 1, baseY - h - 2, 2, 2);
    if (line.status === "running" && S.night) {
      ctx.globalAlpha = 0.25;
      fillOval(ctx, base.x, baseY - h - 2, 6, 4, lamp);
      ctx.globalAlpha = 1;
    }
    // 小烟囱
    ctx.fillStyle = pal.steelDark;
    ctx.fillRect(base.x + hw - 1, baseY - h - Math.round(4 * z), 1, Math.round(4 * z));
    // 装配线：三个小工位
    if (line.kind === "pipeline" && (line.nodes || []).length) {
      var n = Math.min(3, line.nodes.length);
      for (var i = 0; i < n; i += 1) {
        var sx = base.x - hw * 1.4 + (hw * 2.8) * i / Math.max(1, n - 1);
        var sy = baseY + hh;
        ctx.fillStyle = pal.steelDark;
        ctx.fillRect(Math.round(sx - 2), Math.round(sy - 4), 4, 4);
        var nc = line.nodes[i].status === "running" ? pal.ok
          : line.nodes[i].status === "error" ? pal.bad
            : line.nodes[i].status === "done" ? pal.steelLight : pal.idle;
        ctx.fillStyle = nc;
        ctx.fillRect(Math.round(sx - 1), Math.round(sy - 6), 2, 2);
      }
    }
    m.rec._vx = base.x;
    m.rec._vy = baseY - h - Math.max(4, vlen(6));
  }

  function isToday(sec) {
    if (!sec) return false;
    var d = new Date(sec * 1000);
    var n = new Date();
    return d.getFullYear() === n.getFullYear() &&
      d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  }

  function drawPalletModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var w = Math.max(8, Math.round(22 * z));
    var h = Math.max(2, Math.round(3 * z));
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - w / 2, baseY - h, w, h);
    ctx.fillStyle = pal.crate;
    ctx.fillRect(base.x - w / 2 + 1, baseY - h + 1, Math.max(1, w - 2), Math.max(1, h - 1));
    // 成品垛
    var done = 0;
    var st = activeState();
    ((st && st.runs) || []).forEach(function (r) {
      if (r.project_id === m.projectId && r.status === "done" && isToday(r.finished_at)) done += 1;
    });
    var stack = Math.min(4, done);
    var bw = Math.max(2, Math.round(4 * z));
    for (var i = 0; i < stack; i += 1) {
      var col = i % 2, row = Math.floor(i / 2);
      var bx = base.x - Math.round(bw * 1.1) + col * (bw + 1);
      var by = baseY - h - row * (bw + 1);
      drawIsoBoxAt(ctx, bx, by, bw, Math.max(1, bw * 0.5), Math.max(2, bw),
                   pal.product, pal.crate, pal.crateDark);
    }
    // 小旗
    var poleX = base.x + Math.round(w / 2) - 1;
    var poleH = Math.max(4, Math.round(9 * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(poleX, baseY - h - poleH, 1, poleH);
    ctx.fillStyle = pal.ok;
    ctx.fillRect(poleX + 1, baseY - h - poleH, Math.max(2, Math.round(3 * z)),
                 Math.max(1, Math.round(2 * z)));
  }

  function drawCrateModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var s = Math.max(2, Math.round(4 * z)) * (m.s || 1);
    fillOval(ctx, base.x, baseY + 1, s + 1, Math.max(1, Math.round(2 * z)), pal.shadow);
    drawIsoBoxAt(ctx, base.x, baseY, s, Math.max(1, s * 0.5), s * 2,
                 pal.crateLight, pal.crate, pal.crateDark);
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - 1, baseY - s * 2, 1, s * 2);
  }

  function drawBarrelModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var s = Math.max(2, Math.round(3 * z)) * (m.s || 1);
    var h = Math.max(4, Math.round(7 * z));
    fillOval(ctx, base.x, baseY + 1, s + 1, Math.max(1, Math.round(2 * z)), pal.shadow);
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - s, baseY - h, s * 2, h);
    ctx.fillStyle = pal.crate;
    ctx.fillRect(base.x - s + 1, baseY - h + 1, Math.max(1, s * 2 - 2), Math.max(1, h - 2));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(base.x - s, baseY - Math.round(h * 0.7), s * 2, 1);
    ctx.fillRect(base.x - s, baseY - Math.round(h * 0.25), s * 2, 1);
    fillOval(ctx, base.x, baseY - h, s, Math.max(1, s * 0.5), pal.crateLight);
  }

  function drawOreModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var s = Math.max(3, Math.round(7 * z)) * (m.s || 1);
    var ore = m.bg ? pal.bgRust : pal.ore;
    var oreDark = m.bg ? pal.bgRustDark : pal.oreDark;
    var oreHi = m.bg ? pal.bgRustLight : pal.rustLight;
    fillOval(ctx, p.x, p.y, s * 1.4, s * 0.6, pal.shadow);
    fillOval(ctx, p.x, p.y - 1, s, s * 0.6, oreDark);
    fillOval(ctx, p.x - s * 0.3, p.y - 2, s * 0.7, s * 0.45, ore);
    ctx.fillStyle = oreHi;
    ctx.fillRect(Math.round(p.x - s * 0.2), Math.round(p.y - s * 0.7), 1, 1);
    ctx.fillRect(Math.round(p.x + s * 0.3), Math.round(p.y - s * 0.4), 1, 1);
  }

  function drawWorkerModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var u = clamp(Math.round(2.2 * z), 2, 3);
    var size = 5 * u;
    fillOval(ctx, p.x, baseY + 1, Math.round(size * 0.5), Math.max(1, u), pal.shadow);
    ctx.save();
    ctx.translate(Math.round(p.x), Math.round(baseY));
    var top = -size;
    ctx.fillStyle = pal.sheepFace;
    ctx.fillRect(-Math.round(size * 0.30), -Math.round(size * 0.40), u, Math.round(size * 0.40));
    ctx.fillRect(Math.round(size * 0.18), -Math.round(size * 0.40), u, Math.round(size * 0.40));
    ctx.fillStyle = pal.sheep;
    ctx.fillRect(-Math.round(size * 0.52), top, Math.round(size * 0.9), Math.round(size * 0.5));
    ctx.fillStyle = pal.sheepShade;
    ctx.fillRect(-Math.round(size * 0.52), top + Math.round(size * 0.42),
                 Math.round(size * 0.9), Math.max(1, u));
    ctx.fillStyle = pal.sheepFace;
    ctx.fillRect(Math.round(size * 0.26), top + Math.round(size * 0.06),
                 Math.round(size * 0.42), Math.round(size * 0.34));
    ctx.fillStyle = pal.warn;
    ctx.fillRect(Math.round(size * 0.08), top - Math.round(u * 0.6),
                 Math.round(size * 0.62), Math.max(1, u));
    ctx.fillRect(Math.round(size * 0.18), top - Math.round(u * 1.6),
                 Math.round(size * 0.4), Math.max(1, u));
    ctx.restore();
  }

  function drawPylonModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var h = Math.max(10, Math.round(34 * z));
    var w = Math.max(3, Math.round(9 * z));
    var steel = pal.bgSteel, dark = pal.bgSteelDark;
    linePath(ctx, [{ x: p.x - w, y: p.y }, { x: p.x - Math.round(w * 0.35), y: p.y - h }], 1, dark);
    linePath(ctx, [{ x: p.x + w, y: p.y }, { x: p.x + Math.round(w * 0.35), y: p.y - h }], 1, dark);
    for (var i = 1; i <= 4; i += 1) {
      var yy = p.y - Math.round(h * i / 5);
      var ww = Math.round(w * (1 - i / 5) + 1);
      ctx.fillStyle = steel;
      ctx.fillRect(p.x - ww, yy, ww * 2, 1);
    }
    ctx.fillStyle = steel;
    ctx.fillRect(p.x - Math.round(w * 1.6), p.y - h + Math.round(h * 0.12), Math.round(w * 3.2), 1);
    ctx.fillStyle = pal.bgRust;
    ctx.fillRect(p.x - Math.round(w * 1.2), p.y - h + Math.round(h * 0.12) + 1, 1, 2);
    ctx.fillRect(p.x + Math.round(w * 1.2), p.y - h + Math.round(h * 0.12) + 1, 1, 2);
  }

  function drawRailModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var pts = m.path.map(function (q) { return viewPoint(q.x, q.y); });
    linePath(ctx, pts, Math.max(1, Math.round(2 * z)), pal.bgSteelDark);
    var total = pathLength(pts);
    for (var d = 0; d < total; d += Math.max(4, Math.round(8 * z))) {
      var pt = pointAt(pts, total ? d / total : 0);
      ctx.fillStyle = pal.bgSteel;
      ctx.fillRect(Math.round(pt.x - 2), Math.round(pt.y), 4, 1);
    }
  }

  function drawRockModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var s = Math.max(2, Math.round((m.s || 1) * 4 * z));
    fillOval(ctx, p.x, p.y + 1, s, Math.max(1, Math.round(s * 0.45)), pal.shadow);
    ctx.fillStyle = pal.bgSteelDark;
    ctx.fillRect(p.x - s, p.y - s, s * 2, s);
    ctx.fillStyle = pal.bgSteel;
    ctx.fillRect(p.x - s + 1, p.y - s, Math.max(1, s), Math.max(1, s - 1));
  }

  function drawShrubModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var s = Math.max(1, Math.round((m.s || 1) * 3 * z));
    ctx.fillStyle = pal.bgRustDark;
    ctx.fillRect(p.x - s, p.y - 1, s * 2, 1);
    ctx.fillStyle = pal.bgRust;
    ctx.fillRect(p.x - s + 1, p.y - 2, Math.max(1, s), 1);
    ctx.fillRect(p.x, p.y - 3, Math.max(1, s), 1);
  }

  function drawSkyline(ctx, pal, skyH, W) {
    var rand = seeded(0x51C1E);
    ctx.globalAlpha = S.night ? 0.45 : 0.38;
    ctx.fillStyle = pal.bgHaze;
    for (var i = 0; i < 16; i += 1) {
      var x = Math.round(i * W / 15 + rand() * 10);
      var w = Math.round(10 + rand() * 26);
      var h = Math.round(5 + rand() * 14);
      var shape = rand();
      if (shape > 0.8) {
        fillPoly(ctx, [
          { x: x, y: skyH }, { x: x + Math.round(w * 0.28), y: skyH - h },
          { x: x + Math.round(w * 0.72), y: skyH - h }, { x: x + w, y: skyH },
        ], pal.bgHaze);
      } else if (shape > 0.62) {
        fillPoly(ctx, [
          { x: x, y: skyH - h }, { x: x + Math.round(w / 2), y: skyH - h - 5 },
          { x: x + w, y: skyH - h },
        ], pal.bgHaze);
        ctx.fillRect(x, skyH - h, w, h);
      } else {
        ctx.fillRect(x, skyH - h, w, h);
      }
      if (rand() > 0.55) ctx.fillRect(x + Math.round(w * 0.3), skyH - h - 7, 1, 7);
    }
    ctx.globalAlpha = S.night ? 0.6 : 0.5;
    ctx.fillStyle = pal.bgSteel;
    for (var j = 0; j < 10; j += 1) {
      var x2 = Math.round(j * W / 9 + rand() * 16);
      var w2 = Math.round(14 + rand() * 34);
      var h2 = Math.round(8 + rand() * 20);
      ctx.fillRect(x2, skyH - h2, w2, h2);
      if (rand() > 0.55) {
        fillOval(ctx, x2 + Math.round(w2 * 0.68), skyH - h2 - 3,
                 Math.round(w2 * 0.16), 3, pal.bgRust);
      }
      if (S.night) {
        ctx.fillStyle = pal.window;
        for (var k = 0; k < 3; k += 1) ctx.fillRect(x2 + 4 + k * 6, skyH - h2 + 4, 1, 1);
        ctx.fillStyle = pal.bgSteel;
      }
    }
    ctx.globalAlpha = 1;
  }

  function drawGantryModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var w = Math.max(6, Math.round(m.w * z));
    var h = Math.max(8, Math.round(m.h * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x - w, baseY - h, 2, h);
    ctx.fillRect(p.x + w - 2, baseY - h, 2, h);
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(p.x - w, baseY - h, w * 2, 2);
    ctx.fillStyle = pal.metal;
    for (var i = 1; i < 4; i += 1) {
      ctx.fillRect(p.x - w, baseY - h + Math.round(h * i / 4), w * 2, 1);
    }
    ctx.fillStyle = pal.warn;
    ctx.fillRect(p.x - 3, baseY - h + 2, 6, 2);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x, baseY - h + 4, 1, Math.round(h * 0.4));
    ctx.fillStyle = pal.warn;
    ctx.fillRect(p.x - 1, baseY - h + 4 + Math.round(h * 0.4), 2, 2);
  }

  function drawCatwalkModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(6, Math.round(m.hw * z));
    var hh = Math.max(3, Math.round(m.hh * z));
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, Math.max(1, Math.round(2 * z)),
                 pal.steelLight, pal.steel, pal.steelDark);
    var ph = Math.max(2, Math.round(4 * z));
    for (var k = 0; k <= 4; k += 1) {
      var t = k / 4;
      ctx.fillStyle = pal.steelDark;
      ctx.fillRect(Math.round(p.x - hw + hw * t), Math.round(baseY + hh * t - ph), 1, ph);
      ctx.fillRect(Math.round(p.x + hw - hw * t), Math.round(baseY + hh * t - ph), 1, ph);
    }
    linePath(ctx, [{ x: p.x - hw, y: baseY }, { x: p.x, y: baseY + hh }], 1, pal.steelLight);
    linePath(ctx, [{ x: p.x, y: baseY + hh }, { x: p.x + hw, y: baseY }], 1, pal.steelLight);
  }

  function drawCoolingTowerModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - (m.z0 || 0) * z;
    var r = m.r * z, h = m.h * z;
    var cols = roleCols(pal, "concrete", m.bg);
    var levels = [
      { t: 0, r: 1 }, { t: 0.12, r: 0.90 }, { t: 0.30, r: 0.69 },
      { t: 0.52, r: 0.55 }, { t: 0.69, r: 0.53 }, { t: 0.84, r: 0.59 }, { t: 1, r: 0.72 },
    ];
    drawIsoBoxAt(ctx, p.x, baseY + 3 * z, r * 1.18, r * 0.52, 4 * z,
                 pal.stoneLight, pal.stone, pal.stoneDark);
    fillOval(ctx, p.x, baseY, r, r * 0.35, cols.right);
    for (var leg = 0; leg < 10; leg += 1) {
      var a = Math.PI * leg / 9;
      var lx = p.x + Math.cos(a) * r * 0.88;
      var ly = baseY + Math.sin(a) * r * 0.3;
      linePath(ctx, [{ x: lx, y: ly }, { x: lx - Math.cos(a) * 2 * z, y: ly - 8 * z }],
               Math.max(1, vlen(2)), cols.top);
    }
    var shellBase = baseY - 7 * z;
    // 每个圆环分成受光、过渡与背光面，轮廓收腰而非直筒。
    for (var band = 0; band < levels.length - 1; band += 1) {
      var lo = levels[band], hi = levels[band + 1];
      for (var face = 0; face < 12; face += 1) {
        var a0 = Math.PI * face / 12, a1 = Math.PI * (face + 1) / 12;
        function shellPoint(level, angle) {
          return { x: p.x + Math.cos(angle) * r * level.r,
                   y: shellBase - level.t * (h - 7 * z) + Math.sin(angle) * r * level.r * 0.32 };
        }
        fillPoly(ctx, [shellPoint(lo, a0), shellPoint(lo, a1), shellPoint(hi, a1), shellPoint(hi, a0)],
                 face < 3 ? cols.right : face < 7 ? cols.left : cols.top);
      }
    }
    for (var rib = 1; rib < 12; rib += 1) {
      var angle = Math.PI * rib / 12;
      var pts = levels.map(function (lv) {
        return { x: p.x + Math.cos(angle) * r * lv.r,
                 y: shellBase - lv.t * (h - 7 * z) + Math.sin(angle) * r * lv.r * 0.32 };
      });
      ctx.globalAlpha = rib % 3 === 0 ? 0.55 : 0.25;
      linePath(ctx, pts, 1, rib < 6 ? cols.right : cols.left);
    }
    ctx.globalAlpha = 1;
    var topY = baseY - h, rimR = r * 0.72;
    fillOval(ctx, p.x, topY, rimR, rimR * 0.34, cols.top);
    fillOval(ctx, p.x, topY, Math.max(1, rimR - 2 * z), Math.max(1, rimR * 0.34 - z), pal.ink);
    linePath(ctx, [{ x: p.x - rimR, y: topY }, { x: p.x - rimR * 0.6, y: topY + rimR * 0.24 },
                  { x: p.x + rimR * 0.6, y: topY + rimR * 0.24 }, { x: p.x + rimR, y: topY }], 1, cols.top);
    var ladderX = p.x + r * 0.28;
    linePath(ctx, [{ x: ladderX, y: shellBase + 2 * z }, { x: ladderX, y: topY + 3 * z }], 1, pal.metalDark);
    linePath(ctx, [{ x: ladderX + 3 * z, y: shellBase + 2 * z }, { x: ladderX + 3 * z, y: topY + 3 * z }], 1, pal.metalDark);
    for (var rung = 5; rung < m.h - 6; rung += 5) {
      linePath(ctx, [{ x: ladderX, y: baseY - rung * z }, { x: ladderX + 3 * z, y: baseY - rung * z }], 1, pal.metalLight);
    }
    if (S.night) fillOval(ctx, p.x - rimR * 0.8, topY, Math.max(1, z), Math.max(1, z), pal.bad);
  }

  function elevatedPoint(m, t, offset, lift) {
    var a = m.path[0], b = m.path[m.path.length - 1];
    var dx = b.x - a.x, dy = b.y - a.y;
    var length = Math.hypot(dx, dy) || 1;
    var p = viewPoint(a.x + dx * t - dy / length * offset, a.y + dy * t + dx / length * offset);
    p.y -= ((m.h0 + (m.h1 - m.h0) * t) + (lift || 0)) * S.cam.zoom;
    return p;
  }

  // 远景公用设施用雾化色，别让远处的桥和管廊比近景还抢眼
  function mutedPalette(pal) {
    var pc = Object.create(pal);
    pc.steel = pal.bgSteel; pc.steelDark = pal.bgSteelDark; pc.steelLight = pal.bgSteelLight;
    pc.rust = pal.bgRust; pc.rustDark = pal.bgRustDark; pc.rustLight = pal.bgRustLight;
    pc.metal = pal.bgSteel; pc.metalDark = pal.bgSteelDark; pc.metalLight = pal.bgSteelLight;
    return pc;
  }

  function drawRackSupports(ctx, pal, m) {
    var length = pathLength(m.path);
    var bays = Math.max(1, Math.ceil(length / 24));
    for (var i = 0; i <= bays; i += 1) {
      var t = i / bays;
      var left = elevatedPoint(m, t, -m.width * 0.58, 0);
      var right = elevatedPoint(m, t, m.width * 0.58, 0);
      var height = (m.h0 + (m.h1 - m.h0) * t) * S.cam.zoom;
      [left, right].forEach(function (pt) {
        linePath(ctx, [{ x: pt.x, y: pt.y + height }, pt], Math.max(1, vlen(2)), pal.steelDark);
        drawIsoBoxAt(ctx, pt.x, pt.y + height + 1, vlen(3), vlen(1.5), vlen(2),
                     pal.stoneLight, pal.stone, pal.stoneDark);
      });
      linePath(ctx, [left, right], Math.max(1, vlen(2)), pal.steelLight);
      if (i < bays) {
        var next = elevatedPoint(m, (i + 1) / bays, m.width * 0.58, 0);
        linePath(ctx, [{ x: right.x, y: right.y + height * 0.75 }, next], 1, pal.steel);
        linePath(ctx, [right, { x: next.x, y: next.y + height * 0.75 }], 1, pal.steelDark);
      }
    }
    return bays;
  }

  function drawPipeRackModel(ctx, pal, m) {
    var pc = m.bg ? mutedPalette(pal) : pal;
    var bays = drawRackSupports(ctx, pc, m);
    for (var pipe = 0; pipe < 3; pipe += 1) {
      var offset = (pipe - 1) * m.width * 0.30;
      var pts = [elevatedPoint(m, 0, offset, 3), elevatedPoint(m, 1, offset, 3)];
      linePath(ctx, pts, vlen(3), pipe === 1 ? pc.rustDark : pc.metalDark);
      linePath(ctx, pts.map(function (p) { return { x: p.x, y: p.y - 1 }; }),
               Math.max(1, vlen(1)), pipe === 1 ? pc.rustLight : pc.metalLight);
      for (var flange = 0; flange <= bays; flange += 1) {
        var pt = elevatedPoint(m, flange / bays, offset, 3);
        linePath(ctx, [{ x: pt.x - 1, y: pt.y - 2 }, { x: pt.x + 1, y: pt.y + 2 }], 1, pc.steelLight);
      }
    }
    var elbow = elevatedPoint(m, 1, 0, 3);
    var down = elevatedPoint(m, 1, 0, -m.h1 + 4);
    linePath(ctx, [elbow, down], vlen(3), pc.rustDark);
    linePath(ctx, [{ x: elbow.x - 1, y: elbow.y }, { x: down.x - 1, y: down.y }], 1, pc.rustLight);
    var valve = elevatedPoint(m, 0.52, 0, 5);
    linePath(ctx, [valve, { x: valve.x, y: valve.y - vlen(5) }], 1, pc.metalDark);
    fillOval(ctx, valve.x, valve.y - vlen(5), vlen(3), vlen(1.5), pc.rust);
  }

  function drawTransferBridgeModel(ctx, pal, m) {
    var pc = m.bg ? mutedPalette(pal) : pal;
    var bays = drawRackSupports(ctx, pc, m) * 2;
    var a = elevatedPoint(m, 0, 0, 0), b = elevatedPoint(m, 1, 0, 0);
    linePath(ctx, [{ x: a.x, y: a.y + vlen(3) }, { x: b.x, y: b.y + vlen(3) }], vlen(m.width), pc.steelDark);
    fillPoly(ctx, [elevatedPoint(m, 0, -m.width / 2, 0), elevatedPoint(m, 1, -m.width / 2, 0),
                   elevatedPoint(m, 1, m.width / 2, 0), elevatedPoint(m, 0, m.width / 2, 0)], pc.metalDark);
    for (var side = -1; side <= 1; side += 2) {
      var off = side * m.width / 2;
      linePath(ctx, [elevatedPoint(m, 0, off, 0), elevatedPoint(m, 1, off, 0)], 1, pc.steelLight);
      linePath(ctx, [elevatedPoint(m, 0, off, 11), elevatedPoint(m, 1, off, 11)], 1, pc.steelLight);
      for (var i = 0; i < bays; i += 1) {
        var t0 = i / bays, t1 = (i + 1) / bays;
        linePath(ctx, [elevatedPoint(m, t0, off, 0), elevatedPoint(m, t1, off, 11)], 1, pc.rust);
        linePath(ctx, [elevatedPoint(m, t0, off, 11), elevatedPoint(m, t1, off, 0)], 1, pc.steel);
      }
    }
    fillPoly(ctx, [elevatedPoint(m, 0, -m.width * 0.64, 12), elevatedPoint(m, 1, -m.width * 0.64, 12),
                   elevatedPoint(m, 1, 0, 15), elevatedPoint(m, 0, 0, 15)], pc.steelLight);
    fillPoly(ctx, [elevatedPoint(m, 0, 0, 15), elevatedPoint(m, 1, 0, 15),
                   elevatedPoint(m, 1, m.width * 0.64, 12), elevatedPoint(m, 0, m.width * 0.64, 12)], pc.steel);
    for (var panel = 1; panel < bays; panel += 1) {
      linePath(ctx, [elevatedPoint(m, panel / bays, -m.width * 0.64, 12),
                    elevatedPoint(m, panel / bays, 0, 15), elevatedPoint(m, panel / bays, m.width * 0.64, 12)], 1, pc.steelDark);
    }
    if (S.night) {
      for (var lamp = 0; lamp <= bays; lamp += 2) {
        var lp = elevatedPoint(m, lamp / bays, m.width * 0.58, 8);
        fillOval(ctx, lp.x, lp.y, 1, 1, pc.window);
      }
    }
  }

  function drawFigureModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(2, Math.round(4 * z));
    ctx.fillStyle = pal.ink;
    ctx.fillRect(p.x, baseY - h, 1, h);
    ctx.fillRect(p.x, baseY - h - 1, 1, 1);
    if (S.night) {
      ctx.fillStyle = pal.warn;
      ctx.fillRect(p.x, baseY - h + 1, 1, 1);
    }
  }

  function drawEffects(ctx, pal) {
    var site = S.world && S.world.site;
    if (!site) return;
    var z = S.cam.zoom;
    var W = S.bufW, H = S.bufH;
    // 烟囱/筒仓的烟
    site.smoke.forEach(function (s) {
      var p = viewPoint(s.x, s.y);
      var top = p.y - s.h * z;
      for (var i = 0; i < 5; i += 1) {
        var t = ((S.ticks * 0.055 + i * 0.2) % 1);
        var px = p.x + Math.sin((S.ticks * 0.03 + i) * 2) * 3 - t * 9;
        var py = top - t * Math.max(10, 26 * z);
        var r = Math.max(2, (2 + t * 6) * z);
        if (s.steam) {
          px = p.x + Math.sin(i * 1.7 + t * 4) * (3 + t * 5) * z - t * 10 * z;
          py = top - t * 37 * z;
          r = Math.max(2, (4 + t * 10) * z);
        }
        ctx.globalAlpha = (1 - t) * (s.steam ? 0.27 : 0.32);
        fillOval(ctx, px, py, r, r * 0.72, s.steam ? pal.steelLight : pal.smoke);
      }
      ctx.globalAlpha = 1;
    });
    // 处理机组的火星
    site.sparks.forEach(function (s) {
      var p = viewPoint(s.x, s.y);
      var top = p.y - s.h * z;
      for (var i = 0; i < 6; i += 1) {
        var t = ((S.ticks * 0.11 + i * 0.15) % 1);
        var px = p.x + Math.sin(i * 2.1 + S.ticks * 0.05) * (4 + t * 10);
        var py = top + t * t * 16 * z - t * 5;
        ctx.fillStyle = i % 2 ? pal.sun : pal.rustLight;
        ctx.fillRect(Math.round(px), Math.round(py), 1, 1);
      }
    });
    // 空气里的粉尘
    for (var i = 0; i < 60; i += 1) {
      var seed = i * 97 + 13;
      var dx = ((seed * 37) % Math.max(1, W) + S.ticks * 0.12 * (1 + i % 3)) % Math.max(1, W);
      var dy = Math.round(H * 0.22) + ((seed * 53) % Math.max(1, Math.round(H * 0.7)));
      ctx.globalAlpha = 0.10 + (i % 5) * 0.035;
      ctx.fillStyle = pal.dust;
      ctx.fillRect(Math.round(dx), Math.round(dy + Math.sin(S.ticks * 0.02 + i) * 2), 1, 1);
    }
    ctx.globalAlpha = 1;
  }

  function drawLighting(ctx, pal) {
    var W = S.bufW, H = S.bufH;
    var g = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.34,
                                     W / 2, H / 2, Math.max(W, H) * 0.72);
    g.addColorStop(0, "rgba(0,0,0,0)");
    g.addColorStop(1, pal.shadow);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);
  }

  // ---------- 包裹 ----------
  function syncPackages(fromSnapshot) {
    var st = activeState();
    if (!st || !S.world) { S.packages.clear(); return; }
    var running = new Map();
    (st.runs || []).forEach(function (r) {
      if (r.status === "running") running.set(r.id, r);
    });
    // 新增
    running.forEach(function (run, id) {
      var line = S.world.lines.get(runKey(run));
      if (!line || !line.drawn) return;
      if (!S.packages.has(id)) S.packages.set(id, makePackage(run, line));
      else S.packages.get(id).run = run;
    });
    // 收尾：快照里已不在 running，但包裹还在场景里 → 落定并进终态
    S.packages.forEach(function (pkg, id) {
      if (pkg.finish) return;
      if (!running.has(id)) {
        var r = findRun(id);
        pkg.run = r || pkg.run;
        pkg.finish = (r && r.status) || "interrupted";
        pkg.finishedAt = nowMs();
        if (fromSnapshot) pkg.progress = 1;
      }
    });
  }

  function makePackage(run, line) {
    return {
      id: run.id, run: run, key: runKey(run), line: line,
      color: Math.abs(hashKey(runKey(run))) % 3,
      progress: 0, arrived: false, finish: null, finishedAt: 0,
    };
  }

  function hashKey(s) {
    var h = 2166136261;
    for (var i = 0; i < String(s).length; i += 1) {
      h ^= String(s).charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h;
  }

  function packageProgress(pkg, now) {
    if (pkg.finish) return 1;
    var run = pkg.run;
    if (!run) return 0;
    var elapsed = Math.max(0, now / 1000 - (run.started_at || 0));
    var transit = pkg.line.transit || 6;
    return clamp(elapsed / transit, 0, 1);
  }

  function drawPackages(ctx, pal) {
    var now = nowMs();
    var items = [];
    S.packages.forEach(function (pkg) {
      if (!pkg.line || !pkg.line.drawn) return;
      if (pkg.finish && now - pkg.finishedAt > RUN_TAIL_MS) return;
      items.push(pkg);
    });
    items.sort(function (a, b) { return (a.run.started_at || 0) - (b.run.started_at || 0); });
    // [factory] max_packages 控制同屏包裹上限（默认 60）；超出折叠为「+N」
    var limit = clamp(Math.round(Number(activeConfig().max_packages) || MAX_PACKAGES), 10, 300);
    var drawn = 0;
    var overflow = 0;
    for (var i = 0; i < items.length; i += 1) {
      if (drawn >= limit) { overflow += items.length - i; break; }
      drawPackage(ctx, pal, items[i], now);
      drawn += 1;
    }
    if (overflow > 0) {
      S.overflow = overflow;
    } else {
      S.overflow = 0;
    }
  }

  function drawPackage(ctx, pal, pkg, now) {
    var pts = pkg.line.path.map(function (p) { return viewPoint(p.x, p.y); });
    var progress = packageProgress(pkg, now);
    var steps = Math.max(3, Math.round(pathLength(pts) / Math.max(8, vlen(16))));
    var step = Math.round(progress * steps) / steps;
    var dir = 1;
    if (pkg.finish === "error" || pkg.finish === "cancelled") {
      dir = -1;
      var back = clamp((now - pkg.finishedAt) / 4500, 0, 1);
      step = 1 - back;
    }
    var pos = pointAt(pts, step);
    if (dir === 1 && progress >= 1 && !pkg.finish) {
      pos.y += Math.sin(S.ticks * 0.5) * Math.max(1, vlen(1));
    }
    var z = S.cam.zoom;
    var s = Math.max(2, Math.round(5 * z));
    var lift = (12 + 10 * progress) * z;
    var color = pkg.color === 0 ? pal.crate : pkg.color === 1 ? pal.steelLight : pal.product;
    var edge = pal.crateDark;
    if (pkg.finish === "error") { color = pal.bad; edge = pal.ink; }
    else if (pkg.finish === "empty") { color = pal.beltMark; edge = pal.beltDark; }
    else if (pkg.finish === "interrupted" || pkg.finish === "skipped") { color = pal.idle; edge = pal.ink; }
    else if (pkg.finish === "done") { color = pal.product; edge = pal.crateDark; }
    var gy = Math.round(pos.y);
    var x = Math.round(pos.x), y = gy - Math.round(lift) - s;
    // 地面影子 + 箱体 + 顶面高光 + 十字胶带
    fillOval(ctx, x, gy + 1, Math.max(1, Math.round(s * 0.8)),
             Math.max(1, Math.round(s * 0.35)), pal.shadow);
    ctx.fillStyle = edge;
    ctx.fillRect(x - s, y - s, s * 2, s * 2);
    ctx.fillStyle = color;
    ctx.fillRect(x - s + 1, y - s + 1, Math.max(1, s * 2 - 2), Math.max(1, s * 2 - 2));
    ctx.fillStyle = pal.crateLight;
    ctx.fillRect(x - s + 1, y - s + 1, Math.max(1, s * 2 - 2), 1);
    ctx.fillStyle = edge;
    ctx.fillRect(x - 1, y - s + 1, 1, Math.max(1, s * 2 - 2));
    ctx.fillRect(x - s + 1, y - 1, Math.max(1, s * 2 - 2), 1);
    ctx.fillStyle = pal.crateLight;
    ctx.fillRect(x - s + 2, y - s + 2, Math.max(1, s - 1), 1);
    // 记录点击热区
    S.hits.push({ type: "pkg", id: pkg.id, x: x, y: y, r: Math.max(5, s * 1.6) });
  }

  // ---------- 标签覆盖层 ----------
  function clearLabels() {
    if (dom.labels) dom.labels.innerHTML = "";
    S.labelPool = [];
  }

  function labelFor(i) {
    if (S.labelPool[i]) return S.labelPool[i];
    var el = document.createElement("div");
    el.className = "fx-label";
    if (dom.labels) dom.labels.appendChild(el);
    S.labelPool[i] = el;
    return el;
  }

  function placeLabel(el, wx, wy, text, cls, z, used) {
    var p = viewPoint(wx, wy);
    p.y -= Math.round((z || 0) * S.cam.zoom);
    var sx = p.x * (S.cssW / S.bufW);
    var sy = p.y * (S.cssH / S.bufH);
    if (sx < -40 || sy < -20 || sx > S.cssW + 40 || sy > S.cssH + 20) { el.style.display = "none"; return; }
    // 名牌互相压字比少显示几个更难看：按 hall → project → machine 的优先级让位
    if (used) {
      var w = Math.max(26, String(text).length * 11 + 12);
      var h = cls === "machine" ? 18 : 21;
      var box = { l: sx - w / 2, r: sx + w / 2, t: sy - h, b: sy };
      for (var ui = 0; ui < used.length; ui += 1) {
        var o = used[ui];
        if (box.l < o.r && box.r > o.l && box.t < o.b && box.b > o.t) {
          el.style.display = "none";
          return;
        }
      }
      used.push(box);
    }
    el.style.display = "";
    el.style.left = Math.round(sx) + "px";
    el.style.top = Math.round(sy) + "px";
    el.className = "fx-label " + (cls || "");
    if (el.textContent !== text) el.textContent = text;
  }

  function updateLabels() {
    if (!dom.labels || !S.world || S.labelsOff) {
      S.labelPool.forEach(function (el) { el.style.display = "none"; });
      return;
    }
    var i = 0;
    var used = [];
    var st = activeState();
    var running = (st && st.counters && st.counters.running) || 0;
    placeLabel(labelFor(i++), S.world.hall.x, S.world.hall.y, "调度厅" + (running ? " · 在转 " + running + " 件" : ""), "hall", 92, used);
    // 厂区名牌优先
    var factories = S.world.factories.slice().sort(function (a, b) { return a.y - b.y; });
    factories.forEach(function (f) {
      if (i >= MAX_LABELS - 8) return;
      var allLines = (f.project && f.project.lines) || [];
      var busy = allLines.filter(function (l) { return l.status === "running"; }).length;
      var text = f.name + (busy ? " · " + busy + " 忙碌" : "");
      var hidden = Math.max(0, allLines.length - 6);
      if (hidden) text += " · +" + hidden + " 线";
      if (f.root_ok === false) text += " · 停工";
      placeLabel(labelFor(i++), f.x, f.y, text, "project", f.labelZ || 44, used);
    });
    // 机台名：放大到能读时才出现；默认取景下只留厂区名牌，避免一排小字糊在一起
    if (S.cam.zoom >= 0.82) {
      factories.forEach(function (f) {
        f.lines.forEach(function (rec) {
          if (i >= MAX_LABELS - 2) return;
          if (rec._vx == null) return;
          var sx = rec._vx * (S.cssW / S.bufW);
          var sy = rec._vy * (S.cssH / S.bufH);
          if (sx < -60 || sx > S.cssW + 60 || sy < 0 || sy > S.cssH) return;
          var mlabel = rec.line.name || rec.key;
          if (rec.line.status === "running" && rec.kind === "cron") {
            mlabel += " " + fmtClockShort(lineRunSeconds(rec.line)); // 机台上方计时 mm:ss
          }
          placeLabel(labelFor(i++), rec.x, rec.y, mlabel, "machine", 20 + (rec.index % 2) * 8, used);
        });
      });
    }
    // 成品 / 返工浮字（2.2 秒后自然消失）
    var now = nowMs();
    factories.forEach(function (f) {
      if (!f.float || now >= f.float.until || !f.float.text) return;
      if (i >= MAX_LABELS - 1) return;
      placeLabel(labelFor(i++), f.float.x, f.float.y - 8, f.float.text,
        "float" + (f.float.bad ? " bad" : ""));
    });
    if (S.overflow) {
      placeLabel(labelFor(i++), S.world.hall.x, S.world.hall.y + 30, "+" + S.overflow + " 枚包裹", "overflow");
    }
    for (var k = i; k < S.labelPool.length; k += 1) S.labelPool[k].style.display = "none";
  }

  // ---------- 主循环 ----------
  function startLoop() {
    if (S.raf) return;
    S.lastFrame = 0;
    S.lastTick = 0;
    S.raf = requestAnimationFrame(loop);
  }

  function visible() {
    // 只看视图是否可见：窄屏（≤860px）不画 Canvas，但面板里的运行时长仍要按秒推进
    return !!(dom.view && !dom.view.classList.contains("hidden"));
  }

  function loop(ts) {
    if (!S.mounted) { S.raf = 0; return; }
    S.raf = requestAnimationFrame(loop);
    if (document.hidden || !visible()) return;
    var dt = Math.min(0.25, (ts - (S.lastFrame || ts)) / 1000);
    S.lastFrame = ts;

    var k = 1 - Math.exp(-dt * CAM_EASE);
    var moved = false;
    var nx = S.cam.x + (S.cam.tx - S.cam.x) * k;
    var ny = S.cam.y + (S.cam.ty - S.cam.y) * k;
    var nz = S.cam.zoom + (S.cam.tz - S.cam.zoom) * k;
    if (Math.abs(nx - S.cam.x) > 0.05 || Math.abs(ny - S.cam.y) > 0.05) moved = true;
    if (Math.abs(nz - S.cam.zoom) > 0.001) moved = true;
    S.cam.x = nx; S.cam.y = ny; S.cam.zoom = nz;
    if (Math.abs(S.cam.tx - S.cam.x) < 0.1) S.cam.x = S.cam.tx;
    if (Math.abs(S.cam.ty - S.cam.y) < 0.1) S.cam.y = S.cam.ty;
    if (Math.abs(S.cam.tz - S.cam.zoom) < 0.002) S.cam.zoom = S.cam.tz;

    if (S.reduced) {
      if (moved) render();
      return;
    }
    if (!S.lastTick || ts - S.lastTick >= TICK_MS) {
      S.lastTick = ts;
      tick();
      render();
    } else if (moved) {
      render();
    }
  }

  function tick() {
    S.ticks += 1;
    var now = nowMs();
    if (!S.world) return;
    S.rot = (S.rot + (S.hallBoost && now < S.hallBoost ? 0.34 : 0.032)) % (Math.PI * 2);

    // 包裹到达 → 工头跳起转一圈；跑完的包裹到点移除
    S.packages.forEach(function (pkg, id) {
      var progress = packageProgress(pkg, now);
      if (!pkg.arrived && progress >= 1) {
        pkg.arrived = true;
        var f = pkg.line.factory;
        if (f && !S.reduced) f.jumpStart = now;
      }
      if (pkg.finish && now - pkg.finishedAt > RUN_TAIL_MS) S.packages.delete(id);
    });

    // 演示模式：每 3 秒推一次内存里的运行，永不写库
    if (S.demo) demoTick(now);

    // 运行时长文案每 5 秒刷一次面板（有焦点时不重建）
    if (now - S.lastPanel > 5000) {
      S.lastPanel = now;
      refreshPanelStates();
      renderCounters();
    }
  }

  // ---------- 交互 ----------
  var drag = null;

  function canvasPoint(ev) {
    var rect = dom.canvas.getBoundingClientRect();
    var bx = (ev.clientX - rect.left) * (S.bufW / Math.max(1, rect.width));
    var by = (ev.clientY - rect.top) * (S.bufH / Math.max(1, rect.height));
    return { x: bx, y: by, cx: ev.clientX - rect.left, cy: ev.clientY - rect.top };
  }

  function onPointerDown(ev) {
    if (!dom.canvas) return;
    dom.canvas.setPointerCapture && dom.canvas.setPointerCapture(ev.pointerId);
    dom.canvas.classList.add("dragging");
    drag = { x: ev.clientX, y: ev.clientY, moved: 0, camX: S.cam.tx, camY: S.cam.ty };
  }

  function onPointerMove(ev) {
    if (!dom.canvas) return;
    if (drag) {
      var dx = ev.clientX - drag.x;
      var dy = ev.clientY - drag.y;
      drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
      var rect = dom.canvas.getBoundingClientRect();
      var scale = S.bufW / Math.max(1, rect.width) / Math.max(0.01, S.cam.zoom);
      S.cam.tx = drag.camX - dx * scale;
      S.cam.ty = drag.camY - dy * scale;
      S.cam.x = S.cam.tx = clamp(S.cam.tx, -4000, 4000);
      S.cam.y = S.cam.ty = clamp(S.cam.ty, -4000, 4000);
      return;
    }
    // 悬停提示：包裹编号 / 机台名（Canvas 没有 title，写回元素上）
    var pt = canvasPoint(ev);
    var hit = hitTest(pt.x, pt.y);
    if (hit && hit.type === "pkg") {
      var run = findRun(hit.id);
      dom.canvas.title = run ? ("包裹 #" + run.id + " · " + (run.name || "")) : "包裹";
    } else if (hit && hit.type === "line") {
      var rec = findLine(hit.key);
      dom.canvas.title = rec ? (rec.line.name || rec.key) : "";
    } else {
      dom.canvas.title = "";
    }
  }

  function onPointerUp(ev) {
    if (!dom.canvas) return;
    dom.canvas.classList.remove("dragging");
    var wasDrag = drag && drag.moved > 5;
    drag = null;
    if (wasDrag) return;
    var pt = canvasPoint(ev);
    var hit = hitTest(pt.x, pt.y);
    if (hit && hit.type === "pkg") {
      openRunDrawer(hit.id);
      return;
    }
    if (hit && hit.type === "line") {
      openLineDrawer(hit.key, { focus: true });
      return;
    }
  }

  function onPointerLeave() {
    if (dom.canvas) { dom.canvas.classList.remove("dragging"); dom.canvas.title = ""; }
    drag = null;
  }

  function onWheel(ev) {
    ev.preventDefault();
    var k = ev.deltaY < 0 ? 1.12 : 0.89;
    S.cam.tz = clamp(S.cam.tz * k, 0.22, 2.6);
  }

  function hitTest(x, y) {
    // 包裹优先（小目标）
    for (var i = S.hits.length - 1; i >= 0; i -= 1) {
      var h = S.hits[i];
      if (h.type === "pkg" && Math.hypot(x - h.x, y - h.y) <= h.r) return h;
    }
    if (!S.world) return null;
    var best = null;
    S.world.lines.forEach(function (rec) {
      if (!rec.drawn) return;
      var p = viewPoint(rec.x, rec.y);
      var r = Math.max(8, 14 * S.cam.zoom);
      if (Math.abs(x - p.x) <= r && y >= p.y - r * 1.6 && y <= p.y + r) {
        best = { type: "line", key: rec.key };
      }
    });
    return best;
  }

  // ---------- 抽屉 ----------
  function drawerShell(title, bodyHtml) {
    return '<div class="fx-drawer-head"><h3>' + title + "</h3>" +
      '<button class="fx-drawer-close" title="关闭（Esc）">✕</button></div>' +
      '<div class="fx-drawer-body">' + bodyHtml + "</div>";
  }

  function openDrawer(html) {
    if (!dom.drawer) return;
    dom.drawer.innerHTML = html;
    dom.drawer.classList.remove("hidden");
    var close = dom.drawer.querySelector(".fx-drawer-close");
    if (close) close.onclick = closeDrawer;
  }

  function closeDrawer() {
    S.drawer = null;
    if (dom.drawer) {
      dom.drawer.classList.add("hidden");
      dom.drawer.innerHTML = "";
    }
  }

  function factoryEscape() {
    if (S.drawer) { closeDrawer(); return true; }
    return false;
  }

  function openRunDrawer(id) {
    var run = findRun(id);
    if (!run) return;
    S.drawer = { type: "run", id: id };
    var status = run.status || "";
    var html = '<div class="fx-kv">' +
      "<dt>包裹</dt><dd>#" + escapeHtml(String(run.id)) + "</dd>" +
      "<dt>产线</dt><dd>" + escapeHtml(run.name || "（已删除的产线）") + "</dd>" +
      "<dt>厂区</dt><dd>" + escapeHtml(run.project_name || "—") + "</dd>" +
      "<dt>触发</dt><dd>" + escapeHtml(TRIGGER_TEXT[run.trigger] || run.trigger || "—") + "</dd>" +
      "<dt>状态</dt><dd><span class=\"fx-status " + escapeHtml(status) + "\">" +
        escapeHtml(RUN_STATUS[status] || status || "—") + "</span></dd>" +
      "<dt>开始</dt><dd>" + escapeHtml(fmtTs(run.started_at)) + "</dd>" +
      "<dt>结束</dt><dd>" + escapeHtml(fmtTs(run.finished_at)) + "</dd>" +
      "<dt>耗时</dt><dd>" + escapeHtml(fmtDur(runElapsed(run))) + "</dd>" +
      "</div>";
    if (run.error) {
      html += "<div><div class=\"dim small\">错误</div><pre class=\"fx-excerpt\">" +
        escapeHtml(run.error) + "</pre></div>";
    }
    if (run.result_excerpt) {
      html += "<div><div class=\"dim small\">结果摘要</div><pre class=\"fx-excerpt\">" +
        escapeHtml(run.result_excerpt) + "</pre></div>";
    }
    html += '<div class="fx-drawer-ops">';
    if (run.session_id) {
      html += '<button class="btn-primary" data-open-session="' + escapeHtml(String(run.session_id)) + '">打开会话</button>';
    }
    html += '<button class="btn-ghost" data-close-drawer="1">关闭</button></div>';
    openDrawer(drawerShell("运行详情", html));
    if (!dom.drawer) return;
    var openBtn = dom.drawer.querySelector("[data-open-session]");
    if (openBtn) {
      openBtn.onclick = function () {
        var sid = Number(openBtn.dataset.openSession);
        closeDrawer();
        if (typeof backToChat === "function") backToChat();
        if (typeof openTabForSession === "function") openTabForSession(sid, "");
      };
    }
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;
  }

  function openLineDrawer(key, opts) {
    var rec = findLine(key);
    if (!rec) return;
    opts = opts || {};
    var line = rec.line;
    S.selectedKey = key;
    if (opts.focus) {
      S.cam.tx = rec.factory.x;
      S.cam.ty = rec.factory.y;
      S.cam.tz = clamp(Math.max(S.cam.tz, 0.95), 0.25, 2.4);
    }
    renderPanel();
    S.drawer = { type: "line", key: key };
    var next = line.next_run_at ? fmtTs(line.next_run_at) : "—";
    var html = '<div class="fx-kv">' +
      "<dt>厂区</dt><dd>" + escapeHtml(rec.project.name || "—") + "</dd>" +
      "<dt>类型</dt><dd>" + (line.kind === "pipeline" ? "装配线" : "单机产线") + "</dd>" +
      "<dt>触发</dt><dd>" + escapeHtml(line.trigger_text || "—") + "</dd>" +
      "<dt>下次</dt><dd>" + escapeHtml(next) + "</dd>" +
      "<dt>状态</dt><dd>" + escapeHtml(lineStateText(line)) + "</dd>" +
      "</div>";
    if (line.kind === "pipeline" && (line.nodes || []).length) {
      html += "<div><div class=\"dim small\">工位</div><div class=\"fx-run-list\">";
      line.nodes.slice(0, 20).forEach(function (n) {
        html += '<div class="fx-run-row"><span class="fx-run-name">' + escapeHtml(n.title || ("工位 " + n.id)) +
          '</span><span class="fx-run-meta">' + escapeHtml(nodeStatusText(n.status)) + "</span></div>";
      });
      html += "</div></div>";
    }
    html += '<div class="fx-drawer-ops">';
    if (line.kind === "cron") {
      html += '<button class="btn-primary" data-run-now="1">立即运行</button>';
      html += '<button class="btn-ghost" data-toggle-line="1">' +
        (line.status === "paused" || line.status === "stopped" ? "启用产线" : "暂停产线") + "</button>";
    }
    html += '<button class="btn-ghost" data-close-drawer="1">关闭</button></div>';
    html += '<div><div class="dim small">最近运行</div><div id="fx-line-runs" class="fx-run-list">' +
      '<div class="fx-run-empty">读取中…</div></div></div>';
    openDrawer(drawerShell(escapeHtml(line.name || key), html));
    if (!dom.drawer) return;

    var runBtn = dom.drawer.querySelector("[data-run-now]");
    if (runBtn) {
      runBtn.onclick = function () { runLine(line, runBtn); };
    }
    var toggleBtn = dom.drawer.querySelector("[data-toggle-line]");
    if (toggleBtn) {
      toggleBtn.onclick = function () { toggleLine(line, toggleBtn); };
    }
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;

    loadLineRuns(line);
  }

  function nodeStatusText(s) {
    return {
      blocked: "等待依赖", ready: "待运行", running: "运行中",
      done: "已完成", error: "失败", cancelled: "已取消", skipped: "已跳过",
    }[s] || s || "—";
  }

  function loadLineRuns(line) {
    var box = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
    if (!box) return;
    var params = { limit: 20 };
    if (line.kind === "pipeline") params.pipeline_id = line.id;
    else params.task_id = line.id;
    request("factory.runs", params).then(function (r) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
      if (!host) return;
      var rows = (r && r.runs) || [];
      rows.forEach(function (x) { S.runCache.set(x.id, x); });
      if (!rows.length) {
        host.innerHTML = '<div class="fx-run-empty">还没有运行记录</div>';
        return;
      }
      host.innerHTML = rows.map(function (x) {
        return '<button class="fx-run-row" data-run-id="' + escapeHtml(String(x.id)) + '">' +
          '<span class="fx-run-name">#' + escapeHtml(String(x.id)) + " · " + escapeHtml(x.name || "") + "</span>" +
          '<span class="fx-run-meta">' + escapeHtml(RUN_STATUS[x.status] || x.status) + " · " +
          escapeHtml(fmtDurationShort(x)) + "</span></button>";
      }).join("");
      host.querySelectorAll("[data-run-id]").forEach(function (b) {
        b.onclick = function () { openRunDrawer(Number(b.dataset.runId)); };
      });
    }).catch(function (e) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
      if (host) host.innerHTML = '<div class="fx-run-empty">读取失败：' + escapeHtml(e.message || "") + "</div>";
    });
  }

  function fmtDurationShort(run) {
    if (run.status === "running") return "运行中";
    return fmtDur(runElapsed(run));
  }

  function runLine(line, btn) {
    if (btn) btn.disabled = true;
    request("factory.run_now", { task_id: line.id }).then(function (r) {
      if (r && r.started) addNotice("已派工：" + (line.name || line.key) + "（运行 #" + r.id + "）");
      else addNotice((r && r.notice) || "该工位正在运行中，这次没有重复派工");
    }).catch(function (e) {
      addNotice("派工失败：" + (e.message || "未知错误"));
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  function toggleLine(line, btn) {
    var enabled = line.status === "paused" || line.status === "stopped";
    if (btn) btn.disabled = true;
    request("factory.toggle", { task_id: line.id, enabled: enabled }).then(function (r) {
      line.status = enabled ? "idle" : "paused";
      if (r && r.next_run_at != null) line.next_run_at = r.next_run_at;
      addNotice(enabled ? "产线已启用" : "产线已暂停（不再到点派工；仍可手动运行一次）");
      renderPanel();
      render();
      if (S.drawer && S.drawer.type === "line") openLineDrawer(line.key, { focus: false });
    }).catch(function (e) {
      addNotice("操作失败：" + (e.message || "未知错误"));
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  function openAllRunsDrawer() {
    S.drawer = { type: "all" };
    openDrawer(drawerShell("全部运行", '<div id="fx-all-runs" class="fx-run-list"><div class="fx-run-empty">读取中…</div></div>'));
    request("factory.runs", { limit: 60 }).then(function (r) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
      if (!host) return;
      var rows = (r && r.runs) || [];
      rows.forEach(function (x) { S.runCache.set(x.id, x); });
      if (!rows.length) { host.innerHTML = '<div class="fx-run-empty">还没有运行记录</div>'; return; }
      host.innerHTML = rows.map(function (x) {
        return '<button class="fx-run-row" data-run-id="' + escapeHtml(String(x.id)) + '">' +
          '<span class="fx-run-name">#' + escapeHtml(String(x.id)) + " · " +
          escapeHtml(x.project_name || "") + " / " + escapeHtml(x.name || "") + "</span>" +
          '<span class="fx-run-meta">' + escapeHtml(RUN_STATUS[x.status] || x.status) + "</span></button>";
      }).join("");
      host.querySelectorAll("[data-run-id]").forEach(function (b) {
        b.onclick = function () { openRunDrawer(Number(b.dataset.runId)); };
      });
    }).catch(function (e) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
      if (host) host.innerHTML = '<div class="fx-run-empty">读取失败：' + escapeHtml(e.message || "") + "</div>";
    });
  }

  function openAttentionDrawer() {
    var st = activeState();
    var items = (st && st.attention) || [];
    S.drawer = { type: "attention" };
    var html = "";
    if (!items.length) {
      html = '<div class="fx-run-empty">没有需要留意的事。</div>';
    } else {
      var kindText = {
        error: "失败", interrupted: "中断", skipped: "跳过",
        orphan: "孤儿", stopped: "停工",
      };
      html = '<div class="fx-run-list">' + items.map(function (a) {
        return '<div class="fx-run-row"><span class="fx-run-name">' +
          escapeHtml(a.text || a.kind || "") + '</span><span class="fx-run-meta">' +
          escapeHtml(kindText[a.kind] || a.kind || "") + "</span></div>";
      }).join("") + "</div>";
    }
    openDrawer(drawerShell("需留意", html));
  }

  // ---------- 新建产线 ----------
  function openNewLineDrawer(presetProject) {
    var st = S.state || activeState();
    var projects = (st && st.projects) || [];
    if (!projects.length) { addNotice("先在侧栏添加一个项目，再给它的厂区建产线"); return; }
    var tools = [];
    try {
      if (typeof bootSnap !== "undefined" && bootSnap && bootSnap.tools) tools = bootSnap.tools;
    } catch (e) { tools = []; }
    var projectOpts = projects.map(function (p) {
      return '<option value="' + escapeHtml(String(p.id)) + '"' +
        (presetProject && p.id === presetProject ? " selected" : "") + ">" +
        escapeHtml(p.name || ("项目 " + p.id)) + (p.root_ok === false ? "（停工）" : "") + "</option>";
    }).join("");
    var toolRows = tools.length ? tools.map(function (t) {
      var locked = t.safety === "readonly";
      return '<label title="' + escapeHtml(t.description || "") + '"><input type="checkbox" data-tool="' +
        escapeHtml(t.name) + '"' + (locked ? " checked disabled" : "") + ">" + escapeHtml(t.name) + "</label>";
    }).join("") : '<span class="fx-hint">暂时取不到工具清单；保存后可在自动化页再编辑。</span>';
    var weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    var html = '<form class="fx-form" id="fx-new-line">' +
      "<label>厂区（目标项目）</label><select id=\"fx-nl-project\">" + projectOpts + "</select>" +
      "<label>产线名</label><input id=\"fx-nl-name\" type=\"text\" placeholder=\"例如：每日项目状态汇总\">" +
      "<label>任务指令（到点由 Agent 执行的内容）</label>" +
      "<textarea id=\"fx-nl-prompt\" rows=\"3\" placeholder=\"例如：扫描当前目录的 TODO，汇总成一段进度报告\"></textarea>" +
      "<label>频率</label><select id=\"fx-nl-type\">" +
      '<option value="interval">每隔 N 分钟</option>' +
      '<option value="daily">每天固定时间</option>' +
      '<option value="weekly">每周固定时间</option></select>' +
      '<div id="fx-nl-interval-wrap"><label>间隔（分钟）</label>' +
      '<input id="fx-nl-interval" type="number" min="1" max="525600" step="5" value="60"></div>' +
      '<div id="fx-nl-time-wrap" class="hidden"><label>时间（HH:MM）</label>' +
      '<input id="fx-nl-time" type="time" value="09:00">' +
      '<label id="fx-nl-weekday-label" class="hidden">星期几</label>' +
      '<select id="fx-nl-weekday" class="hidden">' +
      weekdays.map(function (d, i) { return '<option value="' + i + '">' + d + "</option>"; }).join("") +
      "</select></div>" +
      "<label>预授权工具（无人值守时自动放行；不勾的会被拒绝）</label>" +
      '<div class="fx-tools">' + toolRows + "</div>" +
      '<p class="fx-hint">只读工具本来就放行；写入 / 执行类必须显式勾选。勾选 run_command 等于允许无人值守执行任意命令，建议只在任务内容完全可信时勾。</p>' +
      '<div class="fx-form-msg" id="fx-nl-msg"></div>' +
      '<div class="fx-drawer-ops"><button type="submit" class="btn-primary">创建产线</button>' +
      '<button type="button" class="btn-ghost" data-close-drawer="1">取消</button></div>' +
      "</form>";
    openDrawer(drawerShell("新建轻量定时产线", html));
    if (!dom.drawer) return;
    var form = dom.drawer.querySelector("#fx-new-line");
    var typeSel = dom.drawer.querySelector("#fx-nl-type");
    var syncType = function () {
      var v = typeSel.value;
      dom.drawer.querySelector("#fx-nl-interval-wrap").classList.toggle("hidden", v !== "interval");
      dom.drawer.querySelector("#fx-nl-time-wrap").classList.toggle("hidden", v === "interval");
      dom.drawer.querySelector("#fx-nl-weekday-label").classList.toggle("hidden", v !== "weekly");
      dom.drawer.querySelector("#fx-nl-weekday").classList.toggle("hidden", v !== "weekly");
    };
    typeSel.addEventListener("change", syncType);
    syncType();
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;
    form.onsubmit = function (ev) {
      ev.preventDefault();
      var msg = dom.drawer.querySelector("#fx-nl-msg");
      var prompt = dom.drawer.querySelector("#fx-nl-prompt").value.trim();
      if (!prompt) { msg.textContent = "任务指令不能为空"; msg.className = "fx-form-msg error"; return; }
      var name = dom.drawer.querySelector("#fx-nl-name").value.trim();
      var params = {
        project_id: Number(dom.drawer.querySelector("#fx-nl-project").value),
        name: name || (prompt.length > 20 ? prompt.slice(0, 20) : prompt),
        prompt: prompt,
        schedule_type: typeSel.value,
        interval_minutes: Number(dom.drawer.querySelector("#fx-nl-interval").value) || 60,
        time_of_day: dom.drawer.querySelector("#fx-nl-time").value || "09:00",
        weekday: Number(dom.drawer.querySelector("#fx-nl-weekday").value) || 0,
        allowed_tools: Array.prototype.map.call(
          dom.drawer.querySelectorAll("input[data-tool]:checked:not(:disabled)"),
          function (el) { return el.dataset.tool; }
        ),
      };
      msg.textContent = "正在创建…";
      msg.className = "fx-form-msg";
      request("factory.line_add", params).then(function (r) {
        addNotice("产线已创建（#" + r.id + "），到点自动运行");
        closeDrawer();
        factoryReload();
      }).catch(function (e) {
        msg.textContent = e.message || "创建失败";
        msg.className = "fx-form-msg error";
      });
    };
  }

  // ---------- 工厂设置 ----------
  function openSettingsDrawer() {
    var st = S.state || activeState();
    var cfg = (st && st.config) || {};
    S.drawer = { type: "settings" };
    var html = '<form class="fx-form" id="fx-settings">' +
      '<label class="fx-check"><input id="fx-cfg-enabled" type="checkbox"' +
      (cfg.enabled !== false ? " checked" : "") + ">显示工厂入口（左侧导航）</label>" +
      '<label class="fx-check"><input id="fx-cfg-demo" type="checkbox"' +
      (cfg.demo !== false ? " checked" : "") + ">没有真实产线时播放样板厂</label>" +
      '<label class="fx-check"><input id="fx-cfg-ambient" type="checkbox"' +
      (cfg.ambient !== false ? " checked" : "") + ">昼夜天色（按本机时刻与主题明暗）</label>" +
      '<label class="fx-hint">天气默认关，一期不联网，没有开关。</label>' +
      "<label>运行历史保留条数（100–100000）</label>" +
      '<input id="fx-cfg-keep" type="number" min="100" max="100000" value="' +
      Number(cfg.history_keep || 2000) + '">' +
      "<label>运行历史保留天数（1–3650）</label>" +
      '<input id="fx-cfg-days" type="number" min="1" max="3650" value="' +
      Number(cfg.history_days || 30) + '">' +
      "<label>同屏最多包裹数（10–300）</label>" +
      '<input id="fx-cfg-max" type="number" min="10" max="300" value="' +
      Number(cfg.max_packages || 60) + '">' +
      '<div class="fx-form-msg" id="fx-cfg-msg"></div>' +
      '<div class="fx-drawer-ops"><button type="submit" class="btn-primary">保存</button>' +
      '<button type="button" class="btn-ghost" data-close-drawer="1">取消</button></div></form>';
    openDrawer(drawerShell("工厂设置", html));
    if (!dom.drawer) return;
    var form = dom.drawer.querySelector("#fx-settings");
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;
    form.onsubmit = function (ev) {
      ev.preventDefault();
      var msg = dom.drawer.querySelector("#fx-cfg-msg");
      msg.textContent = "正在保存…";
      msg.className = "fx-form-msg";
      saveConfig({
        enabled: dom.drawer.querySelector("#fx-cfg-enabled").checked,
        demo: dom.drawer.querySelector("#fx-cfg-demo").checked,
        ambient: dom.drawer.querySelector("#fx-cfg-ambient").checked,
        history_keep: Number(dom.drawer.querySelector("#fx-cfg-keep").value) || 2000,
        history_days: Number(dom.drawer.querySelector("#fx-cfg-days").value) || 30,
        max_packages: Number(dom.drawer.querySelector("#fx-cfg-max").value) || 60,
      }, null, msg);
    };
  }

  function saveConfig(patch, btn, msgEl) {
    if (btn) btn.disabled = true;
    return request("factory.save", patch).then(function (r) {
      var cfg = (r && r.config) || patch;
      if (S.state) S.state.config = Object.assign({}, S.state.config || {}, cfg);
      if (S.demo) S.demo.config = Object.assign({}, S.demo.config || {}, cfg);
      applyNavVisibility(cfg.enabled !== false);
      addNotice("工厂设置已保存");
      if (msgEl) { msgEl.textContent = "已保存"; msgEl.className = "fx-form-msg"; }
      renderAttentionNotice((S.state && S.state.config) || cfg);
      renderAll();
      // 样板厂开关变了：立即重取一次，进入 / 退出演示状态
      if ((S.demo && cfg.demo === false) || (!S.demo && cfg.demo === true && !hasRealLines(S.state))) {
        factoryReload();
      }
      if (S.drawer && S.drawer.type === "settings" && msgEl) {
        // 留在设置抽屉里，让用户看到结果
      } else if (S.drawer && S.drawer.type === "settings") {
        closeDrawer();
      }
    }).catch(function (e) {
      if (msgEl) { msgEl.textContent = e.message || "保存失败"; msgEl.className = "fx-form-msg error"; }
      else addNotice("保存失败：" + (e.message || "未知错误"));
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  // ---------- 事件：factory_run ----------
  function factoryOnEvent(data) {
    if (!data || data.kind !== "factory_run" || !data.run) return;
    if (S.demo) return; // 样板厂不掺真实事件；真有真实产线时会走下一次 reload
    if (!S.state) return;
    var st = S.state;
    var run = normalizeRun(data.run);
    mergeRun(st, run);
    if (data.phase === "started") {
      st.counters = st.counters || {};
      st.counters.running = Math.max(0, (st.counters.running || 0) + 1);
      S.hallBoost = nowMs() + 1500;
      var rec = S.world && S.world.lines.get(runKey(run));
      if (rec) {
        // 停工的产线手动派工也显示「运行中」，启用态本身不变（由 toggle 管）
        rec.line.status = "running";
        rec.line.running_run_id = run.id;
      }
      if (S.world && rec && rec.drawn) {
        if (!S.packages.has(run.id)) S.packages.set(run.id, makePackage(run, rec));
      }
    } else {
      st.counters = st.counters || {};
      st.counters.running = Math.max(0, (st.counters.running || 0) - 1);
      if (run.status === "done" && isToday(run.finished_at)) {
        st.counters.done_today = (st.counters.done_today || 0) + 1;
      }
      var rec2 = S.world && S.world.lines.get(runKey(run));
      if (rec2) {
        rec2.line.running_run_id = null;
        rec2.line.status = rec2.kind === "pipeline"
          ? (run.status === "error" ? "failed" : (run.status === "done" || run.status === "empty") ? "done" : "idle")
          : (rec2.line.status === "paused" || rec2.line.status === "stopped" ? rec2.line.status : "idle");
        rec2.line.last_status = statusToLast(run.status);
        rec2.line.last_run_at = run.finished_at || nowMs() / 1000;
        if (run.status === "error" || run.status === "interrupted" || run.status === "skipped") {
          st.attention = st.attention || [];
          st.attention.unshift({
            kind: run.status, project_id: run.project_id, line_key: runKey(run),
            text: (run.name || "产线") + " · " + (RUN_STATUS[run.status] || run.status),
          });
          st.counters.attention = (st.counters.attention || 0) + 1;
        }
      }
      var pkg = S.packages.get(run.id);
      if (pkg) {
        pkg.run = run;
        if (!pkg.finish) {
          pkg.finish = run.status === "running" ? null : run.status;
          pkg.finishedAt = nowMs();
        }
      }
      // 成品 / 返工的一行浮字（DOM 覆盖层，不画进 Canvas）
      if (rec2 && !S.reduced) {
        rec2.factory.float = {
          text: run.status === "done" ? "+1 成品" : run.status === "error" ? "返工" : "",
          bad: run.status === "error",
          until: nowMs() + 2200,
          x: rec2.factory.tray.x,
          y: rec2.factory.tray.y,
        };
      }
    }
    if (!S.mounted) return;
    renderCounters();
    renderPanel();
    // 抽屉正开着这条产线时同步刷新：状态与「最近运行」不能停在事件之前的快照
    if (S.drawer && S.drawer.type === "line" && S.drawer.key === runKey(run)) {
      openLineDrawer(S.drawer.key, { focus: false });
    }
    render();
  }

  function statusToLast(s) {
    if (s === "done") return "ok";
    if (s === "empty") return "empty";
    if (s === "error" || s === "interrupted" || s === "orphan") return "error";
    return "";
  }

  function normalizeRun(run) {
    return {
      id: run.id, kind: run.kind, task_id: run.task_id, pipeline_id: run.pipeline_id,
      project_id: run.project_id, session_id: run.session_id,
      name: run.name || "", project_name: run.project_name || "",
      trigger: run.trigger || "schedule", status: run.status || "running",
      started_at: run.started_at || nowMs() / 1000, finished_at: run.finished_at || null,
      duration_s: run.duration_s != null ? run.duration_s : null,
      result_excerpt: run.result_excerpt || "", error: run.error || "",
    };
  }

  function mergeRun(st, run) {
    st.runs = st.runs || [];
    var idx = st.runs.findIndex(function (x) { return x.id === run.id; });
    if (idx >= 0) st.runs[idx] = run;
    else st.runs.unshift(run);
    st.runs.sort(function (a, b) {
      var ar = a.status === "running" ? 1 : 0;
      var br = b.status === "running" ? 1 : 0;
      if (ar !== br) return br - ar;
      return (b.started_at || 0) - (a.started_at || 0);
    });
    if (st.runs.length > 60) st.runs.length = 60;
  }

  // ---------- 样板厂（只存在内存里） ----------
  function demoLineCron(id, name, trigger, next, status, last, lastAt) {
    return {
      key: "cron:" + id, kind: "cron", id: id, project_id: 0, name: name,
      status: status || "idle", trigger_text: trigger, next_run_at: next || 0,
      last_status: last || "", last_run_at: lastAt || 0, running_run_id: null, nodes: [],
    };
  }
  function demoLinePipe(id, name, status, nodes) {
    return {
      key: "pipe:" + id, kind: "pipeline", id: id, project_id: 0, name: name,
      status: status || "idle", trigger_text: "编排", next_run_at: 0,
      last_status: "", last_run_at: 0, running_run_id: null, nodes: nodes || [],
    };
  }

  function buildDemoState(cfg) {
    var now = nowMs() / 1000;
    var p1 = {
      id: -1, name: "样板 · 内容厂", root_ok: true,
      lines: [
        demoLineCron(-11, "晨间简报", "每天 09:00", now + 3600, "running", "ok", now - 7200),
        demoLineCron(-12, "竞品巡检", "每 30 分钟", now + 900, "idle", "ok", now - 1200),
        demoLineCron(-13, "素材归档", "每隔 120 分钟", now + 1800, "paused", "ok", now - 86400),
      ],
    };
    var p2 = {
      id: -2, name: "样板 · 工具厂", root_ok: true,
      lines: [
        demoLinePipe(-21, "发布装配线", "running", [
          { id: 1, title: "构建", status: "done" },
          { id: 2, title: "测试", status: "running" },
          { id: 3, title: "打包", status: "ready" },
          { id: 4, title: "发布", status: "blocked" },
        ]),
        demoLineCron(-22, "依赖体检", "每周一 08:30", now + 86400, "idle", "error", now - 3600),
      ],
    };
    var p3 = {
      id: -3, name: "样板 · 资料厂", root_ok: true,
      lines: [
        demoLineCron(-31, "文档快照", "每天 18:00", now + 5400, "idle", "empty", now - 10800),
      ],
    };
    [[p1, -1], [p2, -2], [p3, -3]].forEach(function (pair) {
      pair[0].lines.forEach(function (l) { l.project_id = pair[1]; });
    });
    var runs = [
      demoRun(-1001, "cron", -11, -1, "晨间简报", "样板 · 内容厂", "running", now - 8, null, ""),
      demoRun(-1002, "pipeline_node", 2, -2, "发布装配线 / 测试", "样板 · 工具厂", "running", now - 6, null, ""),
      demoRun(-1003, "cron", -12, -1, "竞品巡检", "样板 · 内容厂", "done", now - 1300, now - 1270, "抓到 12 条更新"),
      demoRun(-1004, "cron", -22, -2, "依赖体检", "样板 · 工具厂", "error", now - 3700, now - 3650, ""),
      demoRun(-1005, "cron", -31, -3, "文档快照", "样板 · 资料厂", "empty", now - 10900, now - 10880, ""),
      demoRun(-1006, "cron", -12, -1, "竞品巡检", "样板 · 内容厂", "done", now - 3100, now - 3060, "抓到 9 条更新"),
    ];
    p1.lines[0].running_run_id = -1001;
    p2.lines[0].running_run_id = -1002;
    return {
      demo: true,
      projects: [p1, p2, p3],
      counters: { running: 2, queued: 1, attention: 2, done_today: 2 },
      attention: [
        { kind: "error", project_id: -2, line_key: "cron:-22", text: "依赖体检 · 上次失败" },
        { kind: "stopped", project_id: -1, line_key: "cron:-13", text: "素材归档 · 停工" },
      ],
      runs: runs,
      config: Object.assign({ enabled: true, demo: true, ambient: true, weather: false,
        history_keep: 2000, history_days: 30, max_packages: 60 }, cfg || {}),
      now: now,
    };
  }

  function demoRun(id, kind, taskId, pid, name, projectName, status, started, finished, excerpt) {
    return {
      id: id, kind: kind, task_id: taskId,
      pipeline_id: kind === "pipeline_node" ? -21 : null,
      project_id: pid, session_id: null, name: name, project_name: projectName,
      trigger: "schedule", status: status, started_at: started, finished_at: finished,
      duration_s: finished ? Math.max(1, finished - started) : null,
      result_excerpt: excerpt || "", error: status === "error" ? "演示：凭据缺失（不会真的执行）" : "",
    };
  }

  function demoTick(now) {
    var st = S.demo;
    if (!st) return;
    var sec = now / 1000;
    // 收尾：运行超过 18 秒的演示运行
    st.runs.forEach(function (r) {
      if (r.status !== "running") return;
      if (sec - r.started_at < 18) return;
      var roll = Math.abs(hashKey(String(r.id) + Math.floor(sec / 18))) % 10;
      var status = roll === 0 ? "error" : roll === 1 ? "empty" : "done";
      r.status = status;
      r.finished_at = sec;
      r.duration_s = Math.max(1, sec - r.started_at);
      r.result_excerpt = status === "done" ? "演示运行完成（只在内存）" : "";
      r.error = status === "error" ? "演示错误（只在内存）" : "";
      var line = findDemoLine(st, runKey(r));
      if (line) {
        line.running_run_id = null;
        line.status = "idle";
        line.last_status = status === "error" ? "error" : status === "empty" ? "empty" : "ok";
        line.last_run_at = sec;
      }
      st.counters.running = Math.max(0, st.counters.running - 1);
      if (status === "done") st.counters.done_today += 1;
      else st.counters.attention += 1;
    });

    // 每 ~6 秒起一枚新包裹（挑一条空闲的启用产线）
    if (S.ticks % 60 === 0) {
      var candidates = [];
      st.projects.forEach(function (p) {
        (p.lines || []).forEach(function (l) {
          if (l.kind !== "cron" || l.status === "paused" || l.status === "stopped" || l.status === "running") return;
          candidates.push(l);
        });
      });
      if (candidates.length) {
        var pick = candidates[Math.abs(hashKey(String(S.ticks))) % candidates.length];
        var id = -2000 - (S.demoSeq += 1);
        pick.status = "running";
        pick.running_run_id = id;
        st.runs.unshift(demoRun(id, "cron", pick.id, pick.project_id, pick.name, demoProjectName(pick.project_id), "running", sec, null, ""));
        st.counters.running += 1;
      }
    }
    if (st.runs.length > 30) {
      st.runs = st.runs.filter(function (r) { return r.status === "running"; })
        .concat(st.runs.filter(function (r) { return r.status !== "running"; }).slice(0, 20));
    }
  }

  function demoProjectName(pid) {
    return pid === -1 ? "样板 · 内容厂" : pid === -2 ? "样板 · 工具厂" : "样板 · 资料厂";
  }

  function findDemoLine(st, key) {
    for (var i = 0; i < st.projects.length; i += 1) {
      var ls = st.projects[i].lines || [];
      for (var j = 0; j < ls.length; j += 1) if (ls[j].key === key) return ls[j];
    }
    return null;
  }

  // ---------- 公共接口（暴露给 app.js） ----------
  function factoryRepaint() {
    if (!S.mounted) return;
    S.lastTick = 0;
    render();
  }

  window.factoryMount = factoryMount;
  window.factoryUnmount = factoryUnmount;
  window.factoryReload = factoryReload;
  window.factoryOnEvent = factoryOnEvent;
  window.factoryRepaint = factoryRepaint;
  window.factoryEscape = factoryEscape;
})();
