/* ============================================================
 * 工厂（第三顶层视图）：跨项目自动化观战台
 *
 * 零构建普通 script，在 app.js 之后加载；只通过全局函数与 app.js 互调：
 *   factoryMount() / factoryUnmount() / factoryReload() / factoryOnEvent(data) /
 *   factoryRepaint() / factoryEscape()
 *
 * 数据：factory.state（打开时一次） + factory_run 事件（开始 / 结束各一次）。
 * 场景：固定斜 45° 等距，32×16 瓦片；内部缓冲按 CSS 尺寸 1/3 渲染（最小 320×180），
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
    ground: "#c9a06a", groundDark: "#a87b4e", groundLight: "#e0bd8a", groundHi: "#f0d6a8",
    road: "#5a5148", roadDark: "#3e3830", roadLight: "#8a7d6b",
    steel: "#8a8f96", steelDark: "#545a62", steelLight: "#c2c8ce",
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
    ground: "#3a3b37", groundDark: "#282925", groundLight: "#4c4c45", groundHi: "#5c5b51",
    road: "#2c2e31", roadDark: "#1a1c1f", roadLight: "#45484d",
    steel: "#4e5c6a", steelDark: "#2e3945", steelLight: "#748696",
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

    var site = buildIndustrialSite(hall, factories);

    var minX = hall.x - 70, maxX = hall.x + 70;
    var minY = hall.y - 50, maxY = hall.y + 50;
    var minX = hall.x - 70, maxX = hall.x + 70;
    var minY = hall.y - 50, maxY = hall.y + 50;
    factories.forEach(function (f) {
      minX = Math.min(minX, f.x - f.halfW - 20);
      maxX = Math.max(maxX, f.x + f.halfW + 20);
      minY = Math.min(minY, f.y - f.halfH - 26);
      maxY = Math.max(maxY, f.y + f.halfH + 26);
    });

    S.world = {
      hall: hall, factories: factories, lines: lines,
      models: site.models, site: site,
      bounds: { minX: minX, maxX: maxX, minY: minY, maxY: maxY },
    };
    S.worldSig = projects.map(function (p) {
      return p.id + ":" + (p.lines || []).map(function (l) { return l.key; }).join(",");
    }).join("|");
    S.selectedKey = null;
  }

  function buildIndustrialSite(hall, factories) {
    var rand = seeded(0x1D05721A);
    var models = [];
    var patches = [];
    var smoke = [];
    var sparks = [];
    var extentX = 380, extentY = 310;

    for (var b = 0; b < 44; b += 1) {
      var bx = (rand() * 2 - 1) * extentX;
      var by = (rand() * 2 - 1) * extentY;
      var br = 14 + rand() * 32;
      patches.push({ x: bx, y: by, rx: br, ry: br * (0.32 + rand() * 0.2), light: rand() < 0.5 });
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
      addBox(cx, cy, hw * 0.96, hh * 0.96, 5, "sand", { paving: true, rail: true });

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
               { z0: baseZ, windows: 5, door: true, roof: "flat" });
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
      models.push({ kind: "conveyor", path: f.belt, h0: 12, h1: 22, width: 3.6, ore: true });
      f.labelZ = topZ + 6;
    });

    // 远景工业带：储罐、仓库、输电塔、铁路、矿堆与卡车
    models.push({ kind: "box", x: 300, y: -150, hw: 58, hh: 30, h: 28, role: "steel", windows: 4, roof: "saw", bg: true });
    models.push({ kind: "cyl", x: 330, y: -60, r: 22, h: 44, role: "rust", bands: 4, bg: true });
    models.push({ kind: "cyl", x: 300, y: 10, r: 16, h: 34, role: "steel", bands: 3, bg: true });
    models.push({ kind: "box", x: -320, y: -100, hw: 50, hh: 26, h: 22, role: "steel", windows: 3, roof: "flat", bg: true });
    models.push({ kind: "cyl", x: -300, y: 40, r: 24, h: 40, role: "rust", bands: 4, bg: true });
    models.push({ kind: "pylon", x: -240, y: -230, bg: true });
    models.push({ kind: "pylon", x: 220, y: -240, bg: true });
    models.push({ kind: "pylon", x: 360, y: -40, bg: true });
    models.push({ kind: "rail", path: [{ x: -380, y: 170 }, { x: -120, y: 190 }, { x: 140, y: 200 }, { x: 380, y: 185 }], bg: true });
    models.push({ kind: "truck", x: -60, y: 175, z0: 0, bg: true });
    models.push({ kind: "truck", x: 80, y: 190, z0: 0, bg: true });
    models.push({ kind: "ore", x: 250, y: 200, s: 2.6, bg: true });
    models.push({ kind: "ore", x: -200, y: 210, s: 2.2, bg: true });
    models.push({ kind: "rock", x: -180, y: -160, s: 2, bg: true });
    models.push({ kind: "rock", x: 180, y: 60, s: 1.6, bg: true });
    models.push({ kind: "shrub", x: -100, y: 260, s: 1.3, bg: true });
    models.push({ kind: "shrub", x: 260, y: 240, s: 1.2, bg: true });
    models.push({ kind: "conveyor", path: [{ x: 270, y: -150 }, { x: 330, y: -60 }], h0: 16, h1: 10, width: 2.6, ore: true, bg: true });
    models.push({ kind: "box", x: -210, y: 140, hw: 26, hh: 14, h: 14, role: "steel", windows: 2, roof: "saw", bg: true });
    models.push({ kind: "cyl", x: -245, y: 155, r: 8, h: 18, role: "rust", bands: 2, bg: true });
    models.push({ kind: "box", x: 220, y: 150, hw: 22, hh: 12, h: 12, role: "steel", windows: 2, roof: "flat", bg: true });
    models.push({ kind: "cyl", x: 250, y: 165, r: 7, h: 16, role: "steel", bands: 2, bg: true });
    models.push({ kind: "truck", x: -150, y: 185, z0: 0, bg: true });
    models.push({ kind: "truck", x: 120, y: 198, z0: 0, bg: true });
    models.push({ kind: "rock", x: -90, y: 130, s: 1.4, bg: true });
    models.push({ kind: "shrub", x: 60, y: 150, s: 1.2, bg: true });
    models.push({ kind: "ore", x: -40, y: 195, s: 1.6, bg: true });
    var tracks = [
      { path: [{ x: -360, y: 210 }, { x: -160, y: 250 }, { x: 60, y: 275 }, { x: 300, y: 290 }] },
      { path: [{ x: -260, y: -40 }, { x: -120, y: 20 }, { x: 20, y: 90 }] },
    ];

    // 中央调度塔：石台 + 钢塔 + 玻璃顶 + 天线
    addBox(hall.x, hall.y, 46, 23, 8, "sand");
    addBox(hall.x, hall.y, 22, 11, 46, "steel",
           { z0: 8, windows: 4, door: true, roof: "flat" });
    addBox(hall.x, hall.y - 5, 13, 6.5, 58, "glass",
           { z0: 8, windows: 0, roof: "flat" });
    addCyl(hall.x, hall.y - 5, 2.6, 72, "steel", { z0: 8, bands: 1 });
    models.push({ kind: "radar", x: hall.x, y: hall.y - 5, z0: 66 });
    sparks.push({ x: hall.x, y: hall.y, h: 18 });

    return { models: models, patches: patches, smoke: smoke, sparks: sparks, tracks: tracks };
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
    var pad = 34;
    var z = Math.min((w - pad * 2) / Math.max(1, b.maxX - b.minX),
                     (h - pad * 2) / Math.max(1, b.maxY - b.minY));
    z = clamp(z * 1.28, 0.22, 1.45);
    var cx = (b.minX + b.maxX) / 2;
    var cy = (b.minY + b.maxY) / 2;
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
    var bw = Math.max(BUFFER_MIN_W, Math.round(S.cssW / 3));
    var bh = Math.max(BUFFER_MIN_H, Math.round(S.cssH / 3));
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
    // 沙地
    var ground = ctx.createLinearGradient(0, skyH, 0, H);
    ground.addColorStop(0, pal.groundLight);
    ground.addColorStop(0.24, pal.ground);
    ground.addColorStop(1, pal.groundDark);
    ctx.fillStyle = ground;
    ctx.fillRect(0, skyH, W, H - skyH);
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

  function drawGroundPatches(ctx, pal) {
    var site = S.world && S.world.site;
    if (!site) return;
    var z = S.cam.zoom;
    site.patches.forEach(function (b) {
      var p = viewPoint(b.x, b.y);
      fillOval(ctx, p.x, p.y, Math.max(2, Math.round(b.rx * z)),
               Math.max(1, Math.round(b.ry * z)),
               b.light ? pal.groundHi : pal.groundDark);
    });
    // 传送带下方的土路与车辙
    S.world.factories.forEach(function (f) {
      var pts = f.belt.map(function (p) { return viewPoint(p.x, p.y); });
      linePath(ctx, pts, vlen(14), pal.roadDark);
      linePath(ctx, pts, vlen(11), pal.road);
      linePath(ctx, pts, vlen(2), pal.roadLight);
      var total = pathLength(pts);
      for (var d = 0; d < total; d += Math.max(5, vlen(16))) {
        var pt = pointAt(pts, total ? d / total : 0);
        ctx.fillStyle = pal.roadDark;
        ctx.fillRect(Math.round(pt.x) - 3, Math.round(pt.y) - 1, 2, 1);
        ctx.fillRect(Math.round(pt.x) + 1, Math.round(pt.y) + 1, 2, 1);
      }
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
      if (m.bg) return;
      var p = viewPoint(m.x, m.y);
      if (m.kind === "box") {
        var hw = m.hw * z, hh = m.hh * z;
        var off = Math.min(20, (m.h || 10) * z * 0.22);
        fillDiamond(ctx, p.x + off * 0.7, p.y + off * 0.45, hw * 1.02, hh * 1.02, pal.shadow);
      } else if (m.kind === "cyl") {
        var rx = m.r * z, ry = Math.max(1, rx * 0.55);
        var off2 = Math.min(22, m.h * z * 0.2);
        fillOval(ctx, p.x + off2 * 0.7, p.y + off2 * 0.45, rx * 1.1, ry * 1.1, pal.shadow);
      } else if (m.kind === "conveyor") {
        var pts = m.path.map(function (q) { return viewPoint(q.x, q.y); });
        linePath(ctx, pts, vlen(6), pal.shadow);
      }
    });
  }

  function drawModels(ctx, pal) {
    var bg = [], main = [];
    S.world.models.forEach(function (m) {
      var mx = m.x, my = m.y;
      if (m.kind === "conveyor" && m.path && m.path.length) {
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
  }

  function drawTruckModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var L = Math.max(10, Math.round(26 * z));
    var W = Math.max(4, Math.round(9 * z));
    var steel = m.bg ? pal.bgSteel : steel;
    var steelDark = m.bg ? pal.bgSteelDark : steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : steelLight;
    var rust = m.bg ? pal.bgRust : rust;
    var rustDark = m.bg ? pal.bgRustDark : rustDark;
    var rustLight = m.bg ? pal.bgRustLight : rustLight;
    var wheel = m.bg ? pal.bgSteelDark : wheel;
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
        var wy = Math.round(baseY - h + hh * (1 - u));
        ctx.fillRect(wx, wy, 1, Math.max(1, Math.round(h - hh * (1 - u))));
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
    var steel = m.bg ? pal.bgSteel : steel;
    var steelDark = m.bg ? pal.bgSteelDark : steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : steelLight;
    var ore = m.bg ? pal.bgRust : ore;
    var oreDark = m.bg ? pal.bgRustDark : oreDark;
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
    var ore = m.bg ? pal.bgRust : ore;
    var oreDark = m.bg ? pal.bgRustDark : oreDark;
    var oreHi = m.bg ? pal.bgRustLight : oreHi;
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
        ctx.globalAlpha = (1 - t) * 0.32;
        fillOval(ctx, px, py, r, r * 0.72, pal.smoke);
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

  function placeLabel(el, wx, wy, text, cls, z) {
    var p = viewPoint(wx, wy);
    p.y -= Math.round((z || 0) * S.cam.zoom);
    var sx = p.x * (S.cssW / S.bufW);
    var sy = p.y * (S.cssH / S.bufH);
    if (sx < -40 || sy < -20 || sx > S.cssW + 40 || sy > S.cssH + 20) { el.style.display = "none"; return; }
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
    var st = activeState();
    var running = (st && st.counters && st.counters.running) || 0;
    placeLabel(labelFor(i++), S.world.hall.x, S.world.hall.y, "调度厅" + (running ? " · 在转 " + running + " 件" : ""), "hall", 92);
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
      placeLabel(labelFor(i++), f.x, f.y, text, "project", f.labelZ || 44);
    });
    // 机台名：放大到能读时才出现
    if (S.cam.zoom >= 0.55) {
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
          placeLabel(labelFor(i++), rec.x, rec.y, mlabel, "machine", 20);
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
