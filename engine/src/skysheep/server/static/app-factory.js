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

  // 昼盘（约 24 色）
  var PAL_DAY = {
    grass: "#7ba05b", plot: "#9cc47c", plotEdge: "#4f6b3c",
    roof: "#c7d3d8", roofDark: "#8b9aa1", wall: "#cbb694", wallDark: "#9a8768",
    window: "#f2d36b", belt: "#8a8378", beltDark: "#565049", beltMark: "#cfc7b5",
    crate: "#c98a4b", crateDark: "#7d5326",
    hall: "#dfe8ea", hallDark: "#93a3a8", hallGlow: "#f7e6a2",
    sheep: "#f4efe2", sheepFace: "#33302a",
    ink: "#2c2a25", ok: "#5aa85a", warn: "#d9a03c", bad: "#c94f47", idle: "#8d8a80",
    product: "#e6cd83",
  };
  // 夜盘（同槽位，整体压暗、窗子点亮）
  var PAL_NIGHT = {
    grass: "#1e2a33", plot: "#26382c", plotEdge: "#121c15",
    roof: "#3d4657", roofDark: "#2a3140", wall: "#3a3730", wallDark: "#262420",
    window: "#f2c96b", belt: "#4a4640", beltDark: "#2b2824", beltMark: "#6a6458",
    crate: "#8a5f35", crateDark: "#52371c",
    hall: "#4a5a6d", hallDark: "#2f3b49", hallGlow: "#f2d98a",
    sheep: "#d9d2c4", sheepFace: "#1b1a18",
    ink: "#0c0d12", ok: "#4f9a5a", warn: "#c9903a", bad: "#b8443f", idle: "#6a6c73",
    product: "#c9a95c",
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
      bounds: { minX: minX, maxX: maxX, minY: minY, maxY: maxY },
    };
    // 厂区集合 / 产线集合变了才重新取景；运行事件刷新不动镜头
    S.worldSig = projects.map(function (p) {
      return p.id + ":" + (p.lines || []).map(function (l) { return l.key; }).join(",");
    }).join("|");
    S.selectedKey = null;
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
    z = clamp(z, 0.22, 1.4);
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
    ctx.fillStyle = pal.grass;
    ctx.fillRect(0, 0, W, H);

    drawGroundSpecks(ctx, pal);

    S.hits = [];
    if (S.world) {
      drawBelts(ctx, pal);
      drawHall(ctx, pal);
      var factories = S.world.factories.slice().sort(function (a, b) {
        return viewPoint(a.x, a.y).y - viewPoint(b.x, b.y).y;
      });
      factories.forEach(function (f) { drawFactory(ctx, pal, f); });
      drawPackages(ctx, pal);
    }
    updateLabels();
  }

  function drawGroundSpecks(ctx, pal) {
    var seed = 1337;
    var W = S.bufW, H = S.bufH;
    ctx.fillStyle = pal.plotEdge;
    for (var i = 0; i < 130; i += 1) {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      var px = seed % W;
      seed = (seed * 1664525 + 1013904223) >>> 0;
      var py = seed % H;
      ctx.fillRect(px, py, 1, 1);
    }
    ctx.fillStyle = pal.grass;
    for (var j = 0; j < 80; j += 1) {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      var qx = seed % W;
      seed = (seed * 1664525 + 1013904223) >>> 0;
      var qy = seed % H;
      ctx.fillRect(qx, qy, 2, 1);
    }
  }

  function drawBelts(ctx, pal) {
    S.world.factories.forEach(function (f) {
      var pts = f.belt.map(function (p) { return viewPoint(p.x, p.y); });
      linePath(ctx, pts, Math.max(2, vlen(5)), pal.beltDark);
      linePath(ctx, pts, Math.max(1, vlen(3)), pal.belt);
      // 两帧滚动的横向刻度
      var total = pathLength(pts);
      var step = Math.max(6, vlen(14));
      for (var d = (S.ticks % 2) * step * 0.5; d < total; d += step) {
        var pt = pointAt(pts, total ? d / total : 0);
        ctx.fillStyle = pal.beltMark;
        ctx.fillRect(Math.round(pt.x) - 1, Math.round(pt.y) - 1, 2, 2);
      }
    });
  }

  function drawHall(ctx, pal) {
    var h = S.world ? S.world.hall : { x: 0, y: 0 };
    var p = viewPoint(h.x, h.y);
    var z = S.cam.zoom;
    var base = vlen(8 * (5 + 3));
    var height = vlen(4 * (5 + 3));
    // 台基
    fillDiamond(ctx, p.x, p.y + vlen(4), base, height, pal.hallDark);
    fillDiamond(ctx, p.x, p.y, base, height, pal.hall);
    // 玻璃圆顶
    var r = Math.max(4, vlen(20));
    fillCircle(ctx, p.x, p.y - vlen(4), r, pal.hallDark);
    fillCircle(ctx, p.x, p.y - vlen(6), Math.max(3, r - 2), pal.hall);
    ctx.fillStyle = pal.window;
    ctx.fillRect(p.x - 2, p.y - vlen(10), 4, 2);
    // 转盘：8 颗珠子 + 中心轴；转角在 tick 里按 10fps 推进（派工时加速）
    var ringR = Math.max(5, vlen(13));
    for (var i = 0; i < 8; i += 1) {
      var a = S.rot + i * Math.PI / 4;
      var bx = Math.round(p.x + Math.cos(a) * ringR);
      var by = Math.round(p.y - vlen(3) + Math.sin(a) * ringR * 0.42);
      ctx.fillStyle = i % 2 ? pal.hallGlow : pal.beltMark;
      ctx.fillRect(bx - 1, by - 1, 2, 2);
    }
    fillCircle(ctx, p.x, p.y - vlen(3), Math.max(1, vlen(2)), pal.hallGlow);
  }

  function drawFactory(ctx, pal, f) {
    var p = viewPoint(f.x, f.y);
    var z = S.cam.zoom;
    var hw = 8 * (f.w + f.h) * z;
    var hh = 4 * (f.w + f.h) * z;
    var edge = Math.max(1, Math.round(2 * z));
    // 地块：外描边 + 草地 + 内院
    fillDiamond(ctx, p.x, p.y, hw, hh, pal.plotEdge);
    fillDiamond(ctx, p.x, p.y, Math.max(2, hw - edge), Math.max(1, hh - edge), pal.plot);
    fillDiamond(ctx, p.x, p.y, Math.max(2, hw * 0.78), Math.max(1, hh * 0.78), pal.plot);

    // 停工牌（红/黄）：厂区不可用或整组产线都没启用
    var allLines = (f.project && f.project.lines) || f.lines;
    var stopped = f.root_ok === false || (allLines.length && allLines.every(function (l) {
      return l.status === "paused" || l.status === "stopped";
    }));
    if (stopped) {
      var bx = p.x + Math.round(hw * 0.62);
      var by = p.y - Math.round(hh * 0.55);
      ctx.fillStyle = pal.ink;
      ctx.fillRect(bx - 1, by, 2, Math.max(3, Math.round(8 * z)));
      ctx.fillStyle = pal.bad;
      ctx.fillRect(bx + 1, by - 2, Math.max(6, Math.round(12 * z)), Math.max(4, Math.round(7 * z)));
    }

    f.lines.forEach(function (rec) { drawMachine(ctx, pal, rec, f); });
    drawTray(ctx, pal, f);
    drawSheep(ctx, pal, f);
  }

  function lineEnabled(line) {
    if (line.kind === "pipeline") return line.status !== "draft";
    return line.status !== "paused" && line.status !== "stopped";
  }
  function lineSmoking(line) {
    if (line.kind === "pipeline") return line.status === "running" || line.status === "idle";
    return line.status !== "paused" && line.status !== "stopped";
  }

  function drawMachine(ctx, pal, rec, f) {
    var line = rec.line;
    var p = viewPoint(rec.x, rec.y);
    var z = S.cam.zoom;
    var w = Math.max(4, Math.round(22 * z));
    var topH = Math.max(2, Math.round(10 * z));
    var bodyH = Math.max(2, Math.round(7 * z));
    var x = p.x, y = p.y;

    // 选中高亮：机台下的亮圈
    if (S.selectedKey === rec.key) {
      fillDiamond(ctx, x, y + bodyH, w * 0.9, topH * 0.6, pal.hallGlow);
    }
    // 阴影
    ctx.fillStyle = pal.ink;
    ctx.fillRect(x - w / 2 - 1, y + bodyH - 1, w + 2, Math.max(1, Math.round(2 * z)));
    // 机身：左右两片 + 顶面
    ctx.fillStyle = pal.wallDark;
    ctx.fillRect(x - w / 2, y - bodyH, w, bodyH);
    ctx.fillStyle = pal.wall;
    ctx.fillRect(x - w / 2, y - bodyH, Math.max(1, Math.round(w * 0.62)), bodyH);
    fillDiamond(ctx, x, y - bodyH - Math.round(topH / 2), w / 2, topH / 2, pal.roof);
    fillDiamond(ctx, x, y - bodyH - Math.round(topH / 2), w / 2 - Math.max(1, Math.round(2 * z)),
                Math.max(1, topH / 2 - Math.max(1, Math.round(z))), pal.roofDark);
    // 窗（运行时亮）
    var winOn = line.status === "running";
    ctx.fillStyle = winOn ? pal.window : pal.roofDark;
    var ww = Math.max(2, Math.round(6 * z));
    ctx.fillRect(x - Math.round(w / 4), y - bodyH + Math.round(bodyH * 0.25), ww, Math.max(1, Math.round(2 * z)));

    // 状态灯：待机 / 运行 / 故障 / 停工
    var lamp = pal.idle;
    if (line.status === "paused" || line.status === "stopped") lamp = pal.warn;
    else if (line.status === "failed" || line.last_status === "error") lamp = pal.bad;
    else if (line.status === "running") lamp = pal.ok;
    var blink = line.status === "running" && !S.reduced && (S.ticks % 6 < 4);
    if (!blink || line.status !== "running") {
      ctx.fillStyle = lamp;
      var ls = Math.max(2, Math.round(3 * z));
      ctx.fillRect(x - 1, y - bodyH - topH - ls - 1, ls, ls);
    }

    // 烟囱：只有启用的产线冒烟
    if (lineEnabled(line)) {
      var cx = x + Math.round(w / 2) - Math.max(2, Math.round(3 * z));
      var cyTop = y - bodyH - topH;
      var chH = Math.max(3, Math.round(8 * z));
      ctx.fillStyle = pal.wallDark;
      ctx.fillRect(cx, cyTop - chH, Math.max(1, Math.round(2 * z)), chH);
      if (lineSmoking(line) && !S.reduced) {
        for (var i = 0; i < 3; i += 1) {
          var t = ((S.ticks * 0.4 + i * 2.2) % 6.6);
          var sy = cyTop - chH - Math.round(t * 2 * Math.max(0.6, z));
          var sx = cx + Math.round(Math.sin(t * 1.4) * 2 * Math.max(0.6, z));
          ctx.fillStyle = i === 2 ? pal.beltDark : pal.beltMark;
          var ss = Math.max(1, Math.round((1 + (t > 3 ? 1 : 0)) * Math.max(0.6, z)));
          ctx.fillRect(sx, sy, ss, ss);
        }
      }
    }

    // 机台名牌的锚点（DOM 覆盖层用）
    rec._vx = x;
    rec._vy = y - bodyH - topH - Math.max(4, vlen(6));
  }

  function drawTray(ctx, pal, f) {
    var p = viewPoint(f.tray.x, f.tray.y);
    var z = S.cam.zoom;
    var w = Math.max(6, Math.round(24 * z));
    var h = Math.max(2, Math.round(5 * z));
    ctx.fillStyle = pal.ink;
    ctx.fillRect(p.x - w / 2, p.y, w, h);
    ctx.fillStyle = pal.wallDark;
    ctx.fillRect(p.x - w / 2 + 1, p.y + 1, w - 2, Math.max(1, h - 2));
    // 今日成品码垛（最多 4 件，按项目今日 done 数）
    var st = activeState();
    var done = 0;
    ((st && st.runs) || []).forEach(function (r) {
      if (r.project_id === f.id && r.status === "done" && isToday(r.finished_at)) done += 1;
    });
    var stack = Math.min(4, done);
    var bw = Math.max(3, Math.round(6 * z));
    for (var i = 0; i < stack; i += 1) {
      var col = i % 2, row = Math.floor(i / 2);
      var bx = p.x - bw + col * (bw + 1);
      var by = p.y - Math.max(1, Math.round(2 * z)) - row * (bw + 1);
      ctx.fillStyle = pal.crateDark;
      ctx.fillRect(bx, by, bw, bw);
      ctx.fillStyle = pal.product;
      ctx.fillRect(bx + 1, by + 1, Math.max(1, bw - 2), Math.max(1, bw - 2));
    }
  }

  function isToday(sec) {
    if (!sec) return false;
    var d = new Date(sec * 1000);
    var n = new Date();
    return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  }

  function drawSheep(ctx, pal, f) {
    var p = viewPoint(f.sheep.x, f.sheep.y);
    var z = S.cam.zoom;
    // 固定 16×16：中文名牌之外的第二视觉锚点，缩小后也认得出
    var u = clamp(Math.round(2 * z), 1, 2);
    var size = 8 * u;
    var x = p.x;
    var baseY = p.y + Math.round(size * 0.2);
    var jump = 0;
    var flip = 1;
    if (!S.reduced && f.jumpStart) {
      var ph = (nowMs() - f.jumpStart) / 900;
      if (ph < 1) {
        jump = Math.round(Math.sin(ph * Math.PI) * 10 * u * 0.6);
        flip = Math.cos(ph * Math.PI * 2) >= 0 ? 1 : -1;
      }
    }
    var frame = S.reduced ? 0 : (Math.floor(S.ticks / 3) % 4);
    var breathe = frame === 1 || frame === 3 ? 1 : 0;

    ctx.save();
    ctx.translate(Math.round(x), Math.round(baseY - jump));
    ctx.scale(flip, 1);
    var bodyTop = -size + breathe * u;
    // 羊毛身体
    ctx.fillStyle = pal.sheep;
    ctx.fillRect(-Math.round(size * 0.55), bodyTop, Math.round(size * 0.9), Math.round(size * 0.55));
    ctx.fillRect(-Math.round(size * 0.45), bodyTop - u, Math.round(size * 0.65), u);
    // 头部（深色）与耳朵
    ctx.fillStyle = pal.sheepFace;
    ctx.fillRect(Math.round(size * 0.2), bodyTop + Math.round(size * 0.12), Math.round(size * 0.42), Math.round(size * 0.4));
    ctx.fillRect(Math.round(size * 0.1), bodyTop + Math.round(size * 0.08), u, Math.round(u * 2));
    // 眼睛
    ctx.fillStyle = pal.sheep;
    ctx.fillRect(Math.round(size * 0.5), bodyTop + Math.round(size * 0.2), Math.max(1, u), Math.max(1, u));
    // 尾巴
    ctx.fillStyle = pal.sheep;
    ctx.fillRect(-Math.round(size * 0.68), bodyTop + Math.round(size * 0.22), u, Math.round(u * 2));
    // 四条腿
    ctx.fillStyle = pal.sheepFace;
    ctx.fillRect(-Math.round(size * 0.42), -Math.round(size * 0.45), Math.max(1, u), Math.round(size * 0.45));
    ctx.fillRect(Math.round(size * 0.18), -Math.round(size * 0.45), Math.max(1, u), Math.round(size * 0.45));
    ctx.restore();
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
    var color = pkg.color === 0 ? pal.crate : pkg.color === 1 ? pal.wall : pal.product;
    var edge = pal.crateDark;
    if (pkg.finish === "error") { color = pal.bad; edge = pal.ink; }
    else if (pkg.finish === "empty") { color = pal.beltMark; edge = pal.beltDark; }
    else if (pkg.finish === "interrupted" || pkg.finish === "skipped") { color = pal.idle; edge = pal.ink; }
    else if (pkg.finish === "done") { color = pal.product; edge = pal.crateDark; }
    var x = Math.round(pos.x), y = Math.round(pos.y) - s;
    ctx.fillStyle = edge;
    ctx.fillRect(x - s, y - s, s * 2, s * 2);
    ctx.fillStyle = color;
    ctx.fillRect(x - s + 1, y - s + 1, Math.max(1, s * 2 - 2), Math.max(1, s * 2 - 2));
    ctx.fillStyle = edge;
    ctx.fillRect(x - 1, y - s + 1, 1, Math.max(1, s * 2 - 2));
    // 记录点击热区
    S.hits.push({ type: "pkg", id: pkg.id, x: pos.x, y: pos.y - s, r: Math.max(5, s * 1.6) });
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

  function placeLabel(el, wx, wy, text, cls) {
    var p = viewPoint(wx, wy);
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
    placeLabel(labelFor(i++), S.world.hall.x, S.world.hall.y - 42, "调度厅" + (running ? " · 在转 " + running + " 件" : ""), "hall");
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
      placeLabel(labelFor(i++), f.x, f.y - f.halfH - 6, text, "project");
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
          placeLabel(labelFor(i++), rec.x, rec.y - 18, mlabel, "machine");
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
