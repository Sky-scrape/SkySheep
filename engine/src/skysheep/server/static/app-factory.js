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
  var UPCOMING_WINDOW_S = 1800;  // 待发月台只看未来 30 分钟到点的产线
  var UPCOMING_MAX = 7;          // 月台上最多摆几枚待发包裹（再多靠标签计数）
  var QUEUED_MAX_DRAW = 3;       // 排队卡住的包裹最多画几枚（其余靠标签补足）
  // 排队等待分级（秒）：到点后等得越久抖得越急、颜色越烫。
  // 分段而不是连续：连续插值在 10fps 的像素场景里读不出差别，三段才分得清。
  var WAIT_WARM_S = 60;          // 1 分钟：从静止转为轻抖
  var WAIT_HOT_S = 180;          // 3 分钟：转快抖 + 变红
  // 抖动参数：幅度 / 频率 / 颜色键
  var WAIT_TIERS = [
    { amp: 0, freq: 0, tone: "warn" },      // < 1 分钟：不动
    { amp: 1, freq: 0.6, tone: "warn" },    // 1–3 分钟：轻抖
    { amp: 2, freq: 1.8, tone: "bad" },     // ≥ 3 分钟：快抖 + 红
  ];
  var PIPELINE_MAX_NODES = 6;    // 装配线一条最多画几个工位（其余靠抽屉列出）
  var CAM_EASE = 9;           // 镜头缓动系数
  var RUN_TAIL_MS = 6000;     // 包裹落地后停留多久再消失
  var STALE_S = 300;          // 运行多久没有任何更新算「卡住」（与 liveStale 同口径）
  var NEAR_TIMEOUT_RATIO = 0.8; // 已跑超过超时额度的这个比例，开始冒烟
  var BATCH_RUN_LIMIT = 5;    // 一次批量派工最多派几条（手滑连点不该烧掉一天额度）
  var FILTER_DEBOUNCE_MS = 120;
  var LIVE_POLL_MS = 12000;   // 「对话在跑」轻量快照的轮询间隔（只读内存）
  var WEATHER_POLL_MS = 30 * 60 * 1000; // 天气后端缓存 30 分钟，前端同频轮询

  // 状态文案（与 docs/界面术语表.md 同口径：包裹不叫「任务」）
  var RUN_STATUS = {
    running: "运行中", done: "已完成", empty: "空产出", error: "失败",
    interrupted: "中断", skipped: "跳过", orphan: "孤儿", cancelled: "已取消",
  };
  // 「做成」判定（与 status 分开）：跑完了不等于做成了。partial = 跑完但有工具
  // 被无人值守门控拦下（预授权没勾全），产出可能只是模型的绕行说明。
  var RUN_OUTCOME = { ok: "做成", partial: "有拦截", failed: "没做成" };
  // 失败归因（后端 classify_run_error 的固定几类）
  var ERROR_KIND = {
    permission: "权限被拒", model: "模型或网络", path: "目录或文件",
    budget: "步数或预算", timeout: "超时", unknown: "未知",
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
    smoke: "#ded8cc", dust: "#f0d6a8", ore: "#6b4a32", oreDark: "#3e2a1c", oreLight: "#8a6443",
    crate: "#c98a4b", crateDark: "#7d5326", crateLight: "#e0a869",
    metal: "#8b949c", metalDark: "#4c545c", metalLight: "#c3ccd2",
    belt: "#6e6a60", beltDark: "#3d3a34", beltMark: "#c9c2b0",
    sheep: "#f6f1e4", sheepShade: "#d8cfbc", sheepFace: "#33302a",
    ink: "#2b2a24", ok: "#6aa86a", warn: "#e0a33c", bad: "#cf503f", idle: "#9a958a",
    product: "#f0d58b", roofLight: "#c07a5c",
    steelMid: "#9aa1a8", panelLine: "#454b53", roofSlate: "#767d85",
    roofSlateLight: "#a7aeb5", roofSlateDark: "#4b5158",
    paint: "#d9a33c", paintDark: "#96691f", paintLight: "#f2c86e",
    mark: "#efe6d2", stain: "rgba(72, 50, 28, 0.16)", soil: "#8b6a45",
    glassGlow: "#cfe8e6", concreteWarm: "#b6a894",
    bgSteel: "#c7bda9", bgSteelDark: "#bcb19c", bgSteelLight: "#ddd3bf",
    bgRust: "#cfb098", bgRustDark: "#c2a58c", bgRustLight: "#ddc0a6",
    bgHaze: "#e8c9a4",
    shadow: "rgba(58, 40, 24, 0.34)", glow: "rgba(255, 196, 110, 0.20)",
    glowCool: "rgba(206, 232, 255, 0.22)",
  };
  // 夜盘：钢蓝暮色 + 暖黄窗光 + 浓烟
  var PAL_NIGHT = {
    skyTop: "#122a44", skyMid: "#1b3a56", skyLow: "#2b4c64", sun: "#a8c6da",
    ground: "#43443f", groundDark: "#2f302b", groundLight: "#54544c", groundHi: "#63625a",
    road: "#3f4144", roadDark: "#2c2e30", roadLight: "#5b5f65",
    steel: "#4e5c6a", steelDark: "#2e3945", steelLight: "#748696",
    stone: "#50514d", stoneDark: "#303834", stoneLight: "#71756c",
    concrete: "#555650", concreteDark: "#3a3b36", concreteLight: "#686a61",
    rust: "#7a4530", rustDark: "#48281b", rustLight: "#9c5e44",
    glass: "#4a6a7a", glassDark: "#293f4c", glassLight: "#7fa6b8",
    window: "#ffd36e", windowDark: "#1a1a18",
    smoke: "#a8adb2", dust: "#d0b98f", ore: "#4a3524", oreDark: "#2b1e14", oreLight: "#5f4630",
    crate: "#8a6138", crateDark: "#4b351d", crateLight: "#a87846",
    metal: "#4a5057", metalDark: "#2b2f34", metalLight: "#6f767e",
    belt: "#3a3831", beltDark: "#211f1b", beltMark: "#7a7364",
    sheep: "#d9d2c4", sheepShade: "#b3ac9c", sheepFace: "#1b1a18",
    ink: "#0b0c10", ok: "#5aa05a", warn: "#c9903a", bad: "#b8443f", idle: "#6a6c73",
    product: "#c9a95c", roofLight: "#8a5040",
    steelMid: "#5b6a79", panelLine: "#1f2831", roofSlate: "#3a4653",
    roofSlateLight: "#546272", roofSlateDark: "#232d37",
    paint: "#a97a2c", paintDark: "#6f4d17", paintLight: "#d3a04a",
    mark: "#b9b2a2", stain: "rgba(0, 0, 0, 0.28)", soil: "#3b3126",
    glassGlow: "#7fb0b8", concreteWarm: "#4f4c45",
    bgSteel: "#3f4f5e", bgSteelDark: "#33424f", bgSteelLight: "#4f6272",
    bgRust: "#5c4436", bgRustDark: "#4d372b", bgRustLight: "#70533f",
    bgHaze: "#2a4358",
    shadow: "rgba(0, 0, 0, 0.46)", glow: "rgba(255, 196, 110, 0.26)",
    glowCool: "rgba(176, 214, 255, 0.24)",
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
    flowingProjects: new Set(),
    runCache: new Map(),   // 抽屉里按需求回的行
    upcoming: [],          // 待发月台：未来 30 分钟到点的产线（按 eta 升序）
    cam: { x: 0, y: 0, zoom: 1, tx: 0, ty: 0, tz: 1 },
    fitZoom: 0.82,         // 全景取景的缩放：机台名只在比全景更近时出现
    focusPid: null,        // 聚焦档：点厂区头「飞过去」看机台，Esc / 全景按钮回全景
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
    lastProgressPanel: 0,  // 工序进度刷新面板的节流时间戳
    resyncTimer: 0,        // cron_updated / pipeline_updated 的防抖重拉
    liveTimer: 0,          // 「对话在跑」轻量轮询
    liveLoading: false,
    weather: null,         // 现实天气快照（/factory.weather）
    weatherTimer: 0,
    weatherLoading: false,
    weatherSeq: 0,         // 天气请求序号：关天气 / 换城市时让在途响应作废
    lastWeatherAt: 0,
    // 面板筛选：mode = all / running / attention / stopped；q 为厂区名或产线名的子串
    filter: { mode: "all", q: "" },
    filterTimer: 0,
    // 「全部运行」抽屉的筛选与分页（后端 factory.runs 早就支持，前端以前只用 60 条平铺）
    runsFilter: { status: "", project_id: 0, offset: 0 },
  };

  var dom = {};

  function $(id) { return document.getElementById(id); }
  function nowMs() { return Date.now(); }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function pad2(n) { return n < 10 ? "0" + n : String(n); }

  // ---------- 主题 / 调色板 ----------
  function activeConfig() {
    var st = S.demo || S.state;
    return (st && st.config) || {};
  }

  /** 天气种类：只有开启且拿到 ok 快照才参与绘制；关闭 / 失败按晴天处理。 */
  function weatherKind() {
    var cfg = activeConfig();
    if (!cfg || cfg.weather !== true || S.demo) return "clear";
    var w = S.weather;
    if (!w || !w.ok) return "clear";
    return String(w.kind || "unknown");
  }

  /** 昼夜：开着现实天气时按当地日出日落（服务端 is_day），否则按本机时刻。
      06:00–18:00 为白天；主题明暗不再影响场景——场景是一扇按真实时间变化的窗。 */
  function weatherIsDay() {
    var cfg = activeConfig();
    if (cfg.ambient === false) return true;
    if (cfg.weather && S.weather && S.weather.ok
        && typeof S.weather.is_day === "boolean") {
      return !!S.weather.is_day;
    }
    var h = new Date().getHours();
    return h >= 6 && h < 18;
  }

  function palette() {
    return weatherIsDay() ? PAL_DAY : PAL_NIGHT;
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
    if (!S.liveTimer) S.liveTimer = setInterval(refreshLive, LIVE_POLL_MS);
    if (!S.weatherTimer) {
      S.weatherTimer = setInterval(function () { refreshWeather(false); }, WEATHER_POLL_MS);
    }
  }

  function factoryUnmount() {
    S.mounted = false;
    if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; }
    if (S.clockTimer) { clearInterval(S.clockTimer); S.clockTimer = 0; }
    if (S.snapshotTimer) { clearInterval(S.snapshotTimer); S.snapshotTimer = 0; }
    if (S.liveTimer) { clearInterval(S.liveTimer); S.liveTimer = 0; }
    if (S.weatherTimer) { clearInterval(S.weatherTimer); S.weatherTimer = 0; }
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
    dom.centerBtn = $("factory-center");
    dom.labelToggle = $("factory-labels-toggle");
    dom.settingsBtn = $("factory-settings");
    dom.panelFold = $("factory-panel-fold");
    dom.hudRunning = $("factory-num-running");
    dom.hudQueued = $("factory-num-queued");
    dom.hudAttention = $("factory-num-attention");
    dom.hudDone = $("factory-num-done");
    dom.hudTokens = $("factory-num-tokens");
    dom.hudChat = $("factory-num-chat");
    dom.weatherChip = $("factory-weather");
    dom.hud = $("factory-hud");
    dom.demoBadge = $("factory-demo-badge");
    dom.chips = $("factory-panel-filter");
    dom.search = $("factory-search");
    dom.hudAttentionCell = document.querySelector('#factory-hud .fh-cell[data-k="attention"]');
    dom.hudChatCell = document.querySelector('#factory-hud .fh-cell[data-k="chat"]');
  }

  function bindOnce() {
    if (S.bound) return;
    S.bound = true;
    cacheDom();

    var allBtn = $("factory-all-runs");
    var addBtn = $("factory-line-add");

    if (allBtn) allBtn.onclick = function () { openAllRunsDrawer(); };
    if (addBtn) addBtn.onclick = function () { openNewLineDrawer(0); };
    if (dom.centerBtn) {
      dom.centerBtn.onclick = function () {
        fitCamera(true);
        updateLabels();
      };
    }
    if (dom.labelToggle) {
      dom.labelToggle.onclick = function () {
        S.labelsOff = !S.labelsOff;
        dom.labelToggle.setAttribute("aria-pressed", S.labelsOff ? "false" : "true");
        updateLabels();
      };
    }
    if (dom.settingsBtn) dom.settingsBtn.onclick = function () { openSettingsDrawer(); };
    if (dom.weatherChip) {
      dom.weatherChip.onclick = function () { openSettingsDrawer(); };
      dom.weatherChip.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openSettingsDrawer();
        }
      };
    }
    if (dom.panelFold) {
      dom.panelFold.onclick = function () {
        S.panelFolded = !S.panelFolded;
        dom.panel.classList.toggle("folded", S.panelFolded);
        resizeScene(false);
      };
    }
    if (dom.panelBody) {
      dom.panelBody.addEventListener("click", function (ev) {
        var clearEl = ev.target.closest("[data-clear-filter]");
        if (clearEl) { resetFilter(); return; }
        var batchRun = ev.target.closest("[data-batch-run]");
        if (batchRun && !batchRun.disabled) {
          batchRunLines(Number(batchRun.dataset.batchRun), batchRun);
          return;
        }
        var batchPause = ev.target.closest("[data-batch-pause]");
        if (batchPause && !batchPause.disabled) {
          batchPauseLines(Number(batchPause.dataset.batchPause), batchPause);
          return;
        }
        var addEl = ev.target.closest("[data-add-project]");
        if (addEl) { openNewLineDrawer(Number(addEl.dataset.addProject) || 0); return; }
        var head = ev.target.closest("[data-focus-project]");
        if (head) { focusProject(Number(head.dataset.focusProject)); return; }
        var lineEl = ev.target.closest("[data-line-key]");
        if (lineEl) { openLineDrawer(lineEl.dataset.lineKey, { focus: true }); }
      });
      // 键盘：↑↓ / Home / End 在面板内逐行走（面板是画布的等价入口，不能只靠鼠标）
      dom.panelBody.addEventListener("keydown", panelKeyNav);
    }
    bindFilter();
    var hudActions = {
      running: function () { openAllRunsDrawer({ status: "running" }); },
      queued: openQueueDrawer,
      done: function () { openAllRunsDrawer({ view: "done_today" }); },
      tokens: function () { openAllRunsDrawer({ view: "tokens_today" }); },
    };
    Object.keys(hudActions).forEach(function (key) {
      var cell = document.querySelector('#factory-hud .fh-cell[data-k="' + key + '"]');
      if (!cell) return;
      var open = function () {
        if (S.demo) { addNotice("当前是样板厂演示，没有真实运行记录"); return; }
        hudActions[key]();
      };
      cell.onclick = open;
      cell.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      };
    });
    if (dom.hudAttentionCell) {
      dom.hudAttentionCell.onclick = function () { openAttentionDrawer(); };
      dom.hudAttentionCell.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openAttentionDrawer(); }
      };
    }
    // 「对话在跑」：工厂之外还有一个盲区——我这些会话里正在跑什么、哪个卡住了
    if (dom.hudChatCell) {
      dom.hudChatCell.onclick = function () { openLiveDrawer(); };
      dom.hudChatCell.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openLiveDrawer(); }
      };
    }

    if (dom.canvas) {
      dom.canvas.addEventListener("pointerdown", onPointerDown);
      dom.canvas.addEventListener("pointermove", onPointerMove);
      dom.canvas.addEventListener("pointerup", onPointerUp);
      dom.canvas.addEventListener("pointercancel", onPointerLeave);
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
        refreshLive();
        refreshWeather(false);
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

  // 轻量实时快照：只刷新「对话在跑 / 子代理」与疑似卡住标记，不重建场景。
  // factory.state 会查项目、任务、7 天运行等多份数据，不适合十几秒轮询。
  function refreshLive() {
    if (!S.mounted || S.demo || S.liveLoading || !S.state || document.hidden) return;
    S.liveLoading = true;
    request("factory.live").then(function (r) {
      if (!S.mounted || !S.state || !r) return;
      S.state.live = r.live || { chat: 0, subagent: 0, runs: [] };
      renderCounters();
      // 抽屉正开着且用户没把焦点放进去：跟着刷新；有焦点时不动，避免抢操作
      if (S.drawer && S.drawer.type === "live" && dom.drawer
          && !dom.drawer.contains(document.activeElement)) {
        openLiveDrawer();
      }
    }).catch(function () {
      /* 实时快照失败不打扰观战：下一轮再试 */
    }).finally(function () {
      S.liveLoading = false;
    });
  }

  function refreshWeather(force) {
    if (!S.mounted || S.demo || !S.state) return;
    var cfg = S.state.config || {};
    if (cfg.weather !== true) {
      var hadWeather = !!S.weather;
      S.weatherSeq += 1;
      S.weatherLoading = false;
      S.weather = null;
      S.lastWeatherAt = 0;
      renderWeatherChip();
      if (hadWeather) render();
      return;
    }
    if (S.weatherLoading && !force) return;
    if (!force && S.weather && nowMs() - S.lastWeatherAt < 5 * 60 * 1000) return;
    var seq = ++S.weatherSeq;
    S.weatherLoading = true;
    request("factory.weather", force ? { force: true } : {}).then(function (r) {
      if (!S.mounted || !S.state || !r || seq !== S.weatherSeq) return;
      if (!(S.state.config || {}).weather) {
        S.weather = null;
        renderWeatherChip();
        render();
        return;
      }
      S.weather = r.weather || null;
      S.lastWeatherAt = nowMs();
      renderWeatherChip();
      render();
    }).catch(function () {
      /* 天气失败不打扰观战：保留上一份快照，chip 会显示不可用 */
      if (S.mounted && seq === S.weatherSeq) renderWeatherChip();
    }).finally(function () {
      if (seq === S.weatherSeq) S.weatherLoading = false;
    });
  }

  function renderWeatherChip() {
    if (!dom.weatherChip) return;
    var cfg = activeConfig();
    var on = !!(cfg && cfg.weather === true) && !S.demo;
    dom.weatherChip.classList.toggle("hidden", !on);
    if (!on) return;
    var w = S.weather;
    if (!w) {
      dom.weatherChip.textContent = "天气读取中…";
      dom.weatherChip.title = "正在按设置里的城市获取现实天气";
      dom.weatherChip.classList.remove("error");
      return;
    }
    if (!w.ok) {
      dom.weatherChip.textContent = "天气不可用";
      dom.weatherChip.title = (w.error || "请检查天气城市与网络") +
        "；点开工厂设置可改城市";
      dom.weatherChip.classList.add("error");
      return;
    }
    var loc = (w.location && w.location.name) || "";
    var temp = w.temperature_c != null ? Math.round(w.temperature_c) + "°" : "";
    dom.weatherChip.textContent = [loc, w.label, temp].filter(Boolean).join(" · ");
    var tip = "现实天气：" + (w.label || "") + (temp ? " " + temp : "");
    if (w.sunrise && w.sunset) tip += "；当地日出 " + w.sunrise + " / 日落 " + w.sunset;
    tip += "（30 分钟缓存）";
    if (w.stale) tip += "；本次更新失败，沿用上次数据";
    dom.weatherChip.title = tip;
    dom.weatherChip.classList.remove("error");
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
    renderWeatherChip();
    // 现实天气跟着配置走：开启后拉一次（后端 30 分钟缓存），关闭则清掉
    refreshWeather(false);
    // 世界重建后取景：聚焦档还在（厂区仍在）就保持推进后的镜头，否则回全景
    if (S.world && (!prevSig || prevSig !== S.worldSig)) {
      var still = S.focusPid && S.world.factories.some(function (f) { return f.id === S.focusPid; });
      if (still) applyFocusCamera(false);
      else fitCamera(false);
    }
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
  // 世界按 3×3 网格排布：调度厅占中心格，项目厂区占外层八格（不画格线）。
  // 超过 8 个项目时继续向外扩 5×5、7×7 的外圈，间距不变。
  var GRID_X = 224;
  var GRID_Y = 120;

  function gridSlots(n) {
    // 固定槽位顺序，不随项目数量重排；坐标投影与平台菱形的两条轴一致。
    var order = [[-1, -1], [1, -1], [-1, 1], [1, 1],
                 [0, -1], [-1, 0], [1, 0], [0, 1]];
    var out = order.slice(0, Math.max(0, n));
    var ring = 2;
    while (out.length < n) {
      var cells = [];
      for (var gx = -ring; gx <= ring; gx += 1) {
        for (var gy = -ring; gy <= ring; gy += 1) {
          if (Math.max(Math.abs(gx), Math.abs(gy)) !== ring) continue;
          cells.push([gx, gy]);
        }
      }
      // 从正北开始顺时针排，保证外圈顺序稳定
      cells.sort(function (a, b) {
        return Math.atan2(a[0], -a[1]) - Math.atan2(b[0], -b[1]);
      });
      for (var ci = 0; ci < cells.length && out.length < n; ci += 1) out.push(cells[ci]);
      ring += 1;
    }
    return out.map(function (g) { return { gx: g[0], gy: g[1] }; });
  }

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

  // 台地尺寸按 3×3 网格外圈算：直接取所有厂区坪的极限尖角，
  // 加一格余量，让台地恰好包住外圈厂区——中间不空一大片。
  function makePlateau(factories) {
    // 保留完整九宫格，即使部分外圈格暂时没有项目也不缩掉。
    var ring = Math.max(1, Math.ceil((Math.sqrt(factories.length + 1) - 1) / 2));
    var hw = GRID_X * (ring + 0.5);
    var hh = GRID_Y * (ring + 0.5);
    var required = 1;
    factories.forEach(function (f) {
      [[f.x - f.halfW, f.y], [f.x + f.halfW, f.y],
       [f.x, f.y - f.halfH], [f.x, f.y + f.halfH]].forEach(function (p) {
        required = Math.max(required, Math.abs(p[0]) / hw + Math.abs(p[1]) / hh);
      });
    });
    hw = Math.ceil(hw * required * 1.04);
    hh = Math.ceil(hh * required * 1.04);
    return {
      cx: 0, cy: 0,
      hw: hw, hh: hh,
      th: 0,
    };
  }

  function buildWorld(st) {
    var projects = (st && st.projects) || [];
    var hall = { x: 0, y: 0 };
    var factories = [];
    var lines = new Map();
    var keepSelected = S.selectedKey;
    // 3×3 网格：调度厅居中，项目按外层格排布（格线不画出来）。
    // 先占外围四角，再补边格；新增项目不改变已占槽位。
    var slots = gridSlots(projects.length);
    var i = 0;
    projects.forEach(function (p) {
      var slot = slots[i] || { gx: 0, gy: -1 };
      var fx = (slot.gx - slot.gy) * GRID_X / 2;
      var fy = (slot.gx + slot.gy) * GRID_Y / 2;
      var spec = plotSpec((p.lines || []).length);
      var f = {
        project: p, id: p.id, name: p.name, root_ok: p.root_ok !== false,
        x: fx, y: fy, w: spec.w, h: spec.h, spec: spec, lines: [],
        jumpStart: 0,
      };
      var n = spec.w + spec.h;
      // 菱形坪要装下后场厂房 + 附房 + 筒仓 + 前场两排机台，半宽按 104 起算；
      // 建筑与机台都收在「菱形内切矩形」附近，四角不再压出坪沿。
      f.halfW = Math.max(104, 10.5 * n);
      f.halfH = f.halfW * 0.5;
      // 入口位于调度厅与厂区中心连线的坪沿，主轨道保持一条直线。
      var edgeScale = 0.96 / (Math.abs(fx) / f.halfW + Math.abs(fy) / f.halfH || 1);
      f.entrance = { x: fx * (1 - edgeScale), y: fy * (1 - edgeScale) };
      f.hub = { x: fx + f.halfW * 0.28, y: fy + f.halfH * 0.06 };
      f.tray = { x: fx + f.halfW * 0.40, y: fy + f.halfH * 0.26 };
      f.sheep = { x: fx - f.halfW * 0.45, y: fy + f.halfH * 0.36 };
      f.belt = routeBelt(hall, f.entrance, i);
      // 机台在前场排两排，横向按场院宽度铺开；支线停在机台口，不横穿工作区。
      var gapX = Math.min(34, f.halfW * 0.32);
      var rowY = fy + f.halfH * 0.15;
      (p.lines || []).forEach(function (line, li) {
        if (line.kind === "cron" && line.status === "stopped") { /* 仍画机台：挂停工牌 */ }
        var col = li % spec.cols;
        var row = Math.floor(li / spec.cols);
        var mx = fx + (col - (spec.cols - 1) / 2) * gapX;
        var my = rowY + row * f.halfH * 0.24;
        var rec = {
          key: line.key, line: line, kind: line.kind,
          id: line.id, project: p, factory: f,
          x: mx, y: my, index: li, drawn: li < 6,
        };
        rec.path = f.belt.concat([f.hub, { x: mx, y: my - 7 }]);
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
      if (m.bg && !m.peripheral) return;
      var points = m.path || [{ x: m.x, y: m.y }];
      var hw = m.hw || m.r || m.width || 8;
      var hh = m.hh || (m.r ? m.r * 0.5 : 5);
      // 轴系零件（煤棚 / 筒仓群）的占地落在「沿公路 / 纵深」两条轴上，投影回屏幕
      // 两轴再取包围盒；轴对齐零件照旧（ax=1, ay=0 时两个 span 就是 hw / hh）。
      var ax = m.axis ? m.axis.x : 1, ay = m.axis ? m.axis.y : 0;
      var spanX = Math.abs(hw * ax) + Math.abs(hh * ay);
      var spanY = Math.abs(hw * ay) + Math.abs(hh * ax);
      // 取景包含塔顶，避免缩放和不同窗口尺寸下裁掉主体。
      var height = (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0));
      points.forEach(function (pt) {
        minX = Math.min(minX, pt.x - spanX - 8);
        maxX = Math.max(maxX, pt.x + spanX + 8);
        minY = Math.min(minY, pt.y - height - spanY - 16);
        maxY = Math.max(maxY, pt.y + spanY + 12);
      });
    });

    // 固定取景同时包含园区边缘与外围设施，避免居中后裁掉前沿道路。
    minX = Math.min(minX, plateau.cx - plateau.hw - 20);
    maxX = Math.max(maxX, plateau.cx + plateau.hw + 20);
    minY = Math.min(minY, plateau.cy - plateau.hh);
    maxY = Math.max(maxY, plateau.cy + plateau.hh + 20);

    S.world = {
      hall: hall, factories: factories, lines: lines,
      models: site.models, site: site, plateau: plateau,
      bounds: { minX: minX, maxX: maxX, minY: minY, maxY: maxY },
    };
    S.worldSig = projects.map(function (p) {
      return p.id + ":" + (p.lines || []).map(function (l) { return l.key; }).join(",");
    }).join("|");
    // 重建世界时不丢选中态：cron_updated / 运行事件会触发防抖重拉，若每次都把
    // 高亮清掉，面板会一直在用户眼皮底下「闪一下」
    S.selectedKey = lines.has(keepSelected) ? keepSelected : null;
  }

  function buildIndustrialSite(hall, factories, plateau) {
    // 台地由 buildWorld 算好传进来；单测 / 探针会只传前两个参数，
    // 而贴路布置（料场 / 罐区 / 前场支路）依赖台地尺寸，不能依赖调用方传参。
    if (!plateau) plateau = makePlateau(factories);
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
      var variant = Math.abs(hashKey("site" + f.id)) % 8;
      f.buildingVariant = variant;
      addBox(cx, cy, hw * 0.96, hh * 0.96, 5, "concrete",
             { paving: true, rail: true, curb: true, yard: f });

      var compoundStart = models.length;
      var styles = ["重工铸造厂", "物流集散仓", "垂直加工塔", "联排制造坊",
                    "数据机房", "退台研发园", "双跨装配厂", "罐区精炼站"];
      f.compoundStyle = styles[variant];
      var layouts = [
        [-0.20, -0.28, 0.25, 0.18], [-0.10, -0.37, 0.38, 0.16],
        [-0.10, -0.40, 0.14, 0.14], [-0.32, -0.20, 0.16, 0.18],
        [-0.22, -0.25, 0.24, 0.22], [-0.18, -0.32, 0.23, 0.18],
        [-0.27, -0.28, 0.19, 0.24], [-0.37, -0.12, 0.13, 0.16]
      ];
      var layout = layouts[variant];
      var mainBx = cx + hw * layout[0], mainBy = cy + hh * layout[1];
      var mainHW = hw * layout[2], mainHH = hh * layout[3];
      var heights = [38, 24, 56, 34, 27, 32, 22, 29];
      var mainH = heights[variant];
      var topZ = baseZ + mainH;
      addBox(mainBx, mainBy, mainHW, mainHH, mainH, "steel",
             { z0: baseZ, windows: variant === 4 ? 1 : 4, door: true,
               roof: variant === 0 || variant === 3 || variant === 6 ? "saw" : "flat",
               vent: variant === 2, hall: true, pipes: variant === 0 || variant === 7 });

      function wing(x, y, w, d, h, role, opts) {
        var options = { z0: baseZ, roof: "flat", windows: 2, annex: true };
        if (opts) Object.keys(opts).forEach(function (key) { options[key] = opts[key]; });
        return addBox(cx + hw * x, cy + hh * y, hw * w, hh * d, h, role, options);
      }
      function tank(x, y, r, h, role, opts) {
        var options = { z0: baseZ, bands: 3, ladder: true, silo: true };
        if (opts) Object.keys(opts).forEach(function (key) { options[key] = opts[key]; });
        return addCyl(cx + hw * x, cy + hh * y, hw * r, h, role, options);
      }
      function stack(x, y, h) {
        var chimneyX = cx + hw * x, chimneyY = cy + hh * y;
        addCyl(chimneyX, chimneyY, 2.3, h, "steel",
               { z0: baseZ, bands: 2, chimney: true, cap: true });
        smoke.push({ x: chimneyX, y: chimneyY, h: baseZ + h });
      }
      if (variant === 0) {
        models.push({ kind: "furnace", x: cx - hw * 0.56, y: cy - hh * 0.10,
                      hw: 10, hh: 6, h: 34, z0: baseZ, role: "rust", projectId: f.id });
        wing(0.25, -0.27, 0.14, 0.13, 16, "rust", { roof: "saw", pipes: true });
        tank(0.49, -0.15, 0.065, 27, "rust");
        stack(-0.27, -0.55, 53);
        stack(-0.07, -0.55, 46);
        wing(-0.20, 0.00, 0.19, 0.055, 7, "steel", { canopy: true, windows: 0 });
      } else if (variant === 1) {
        wing(0.34, -0.06, 0.12, 0.15, 11, "rust", { door: true });
        wing(-0.10, -0.13, 0.30, 0.045, 8, "steel", { canopy: true, windows: 0 });
        for (var container = 0; container < 3; container += 1) {
          wing(-0.48 + container * 0.22, 0.14, 0.085, 0.075, 6,
               container === 1 ? "steel" : "rust", { container: true, windows: 0 });
        }
        wing(-0.48, 0.14, 0.075, 0.065, 5, "steel",
             { z0: baseZ + 6, container: true, windows: 0 });
        models.push({ kind: "truck", x: cx + hw * 0.40, y: cy + hh * 0.25, z0: baseZ });
      } else if (variant === 2) {
        wing(-0.10, -0.40, 0.09, 0.09, 8, "glass", { z0: topZ, windows: 3 });
        topZ += 8;
        wing(-0.36, -0.08, 0.19, 0.12, 14, "steel", { vent: true });
        wing(0.25, -0.18, 0.13, 0.13, 21, "rust", { pipes: true });
        models.push({ kind: "transferBridge",
                      path: [{ x: cx - hw * 0.03, y: cy - hh * 0.29 },
                             { x: cx + hw * 0.20, y: cy - hh * 0.18 }],
                      h0: 20, h1: 20, width: 3 });
      } else if (variant === 3) {
        wing(0.04, -0.40, 0.15, 0.16, 26, "rust", { roof: "saw", door: true });
        wing(0.34, -0.20, 0.13, 0.17, 18, "steel", { roof: "saw", door: true });
        wing(-0.06, -0.10, 0.07, 0.07, 10, "glass");
        wing(-0.50, 0.14, 0.075, 0.06, 7, "rust", { container: true, windows: 0 });
      } else if (variant === 4) {
        wing(0.31, -0.28, 0.16, 0.18, 23, "steel", { windows: 1 });
        for (var ac = 0; ac < 3; ac += 1) {
          addBox(mainBx - mainHW * 0.60 + ac * mainHW * 0.60, mainBy,
                 mainHW * 0.20, mainHH * 0.24, 6, "steel",
                 { z0: topZ, roof: "flat", vent: true });
        }
        wing(0.01, 0.02, 0.12, 0.07, 8, "glass", { windows: 4 });
        for (var cooler = 0; cooler < 3; cooler += 1) {
          wing(-0.51 + cooler * 0.14, 0.15, 0.045, 0.06, 8, "steel", { vent: true, windows: 0 });
        }
        topZ += 6;
      } else if (variant === 5) {
        addBox(mainBx, mainBy, mainHW * 0.72, mainHH * 0.72, 12, "glass",
               { z0: topZ, windows: 5, roof: "flat" });
        topZ += 12;
        wing(0.26, -0.18, 0.15, 0.13, 17, "glass", { windows: 4 });
        wing(-0.42, 0.07, 0.10, 0.08, 11, "steel", { windows: 3 });
        for (var garden = 0; garden < 3; garden += 1) {
          var gardenX = cx + hw * (0.04 + garden * 0.15);
          var gardenY = cy + hh * 0.12;
          addBox(gardenX, gardenY, hw * 0.045, hh * 0.045, 2, "concrete", { z0: baseZ });
          models.push({ kind: "shrub", x: gardenX, y: gardenY, s: 0.8, z0: baseZ + 2 });
        }
      } else if (variant === 6) {
        wing(0.23, -0.27, 0.19, 0.24, 28, "steel", { roof: "saw", door: true });
        wing(-0.01, -0.04, 0.055, 0.075, 9, "glass");
        models.push({ kind: "transferBridge",
                      path: [{ x: cx - hw * 0.28, y: cy - hh * 0.22 },
                             { x: cx + hw * 0.24, y: cy - hh * 0.22 }],
                      h0: 32, h1: 32, width: 3.5 });
        for (var reel = 0; reel < 3; reel += 1) {
          models.push({ kind: "reel", x: cx - hw * 0.52 + reel * hw * 0.14,
                        y: cy + hh * 0.15, s: 0.8, z0: baseZ });
        }
      } else {
        [{ x: 0.04, y: -0.72, h: 48, role: "steel" },
         { x: 0.25, y: -0.55, h: 39, role: "rust" }].forEach(function (column) {
          models.push({ kind: "distillation", x: cx + hw * column.x, y: cy + hh * column.y,
                        r: 3.2, hw: 5.5, hh: 3, h: column.h, z0: baseZ,
                        platforms: 3, role: column.role });
        });
        tank(-0.06, -0.51, 0.085, 43, "rust");
        tank(0.25, -0.37, 0.10, 32, "steel");
        tank(0.48, -0.10, 0.07, 24, "rust");
        tank(0.12, -0.02, 0.065, 18, "steel");
        wing(-0.36, 0.18, 0.12, 0.075, 10, "glass");
        stack(-0.30, -0.43, 48);
        models.push({ kind: "pipeRack",
                      path: [{ x: cx - hw * 0.04, y: cy - hh * 0.34 },
                             { x: cx + hw * 0.43, y: cy - hh * 0.12 }],
                      h0: 9, h1: 9, width: 3 });
      }
      var gateSide = (f.entrance.x >= cx) ? 1 : -1;
      if (variant === 0 || variant === 1 || variant === 6 || variant === 7) {
        wing(gateSide * 0.63, 0.04, 0.045, 0.05, 7, "steel", { door: true, gatehouse: true });
      }
      models.slice(compoundStart).forEach(function (m) {
        m.projectId = f.id;
        m.compoundPart = true;
      });

      // 前场机台：围绕分配带排两排；机台自带底座与状态灯柱
      f.lines.forEach(function (rec, li) {
        models.push({ kind: "machine", x: rec.x, y: rec.y, hw: 9, hh: 4.5,
                      h: 12.5, z0: baseZ, line: rec.line, rec: rec, index: li });
      });
      // 分配带：入口 → 前场枢纽，再分短支线停在每台机台身后（低于机台，不压工作面）
      var hub = f.hub || { x: cx + hw * 0.30, y: cy + hh * 0.12 };
      models.push({ kind: "conveyor", path: [f.entrance, hub],
                    h0: 9, h1: 9, width: 2.6, ore: false, projectId: f.id });
      f.lines.forEach(function (rec) {
        models.push({ kind: "conveyor", path: [hub, { x: rec.x, y: rec.y - 7 }],
                      h0: 9, h1: 7, width: 1.6, ore: false, spur: true });
      });
      // 前场两角：成品托盘 / 料箱与卷盘；灯杆与工头放在通道边
      models.push({ kind: "pallet", x: f.tray.x, y: f.tray.y, z0: baseZ, projectId: f.id, bay: true });
      if (variant === 0 || variant === 3 || variant === 7) {
        addBox(cx - hw * 0.48, cy + hh * 0.26, 5.4, 2.7, 3, "concrete", { z0: baseZ, pad: true });
        models.push({ kind: "crate", x: cx - hw * 0.48, y: cy + hh * 0.24, s: 0.9, z0: baseZ });
        models.push({ kind: "barrel", x: cx - hw * 0.57, y: cy + hh * 0.31, s: 0.9, z0: baseZ });
        if (variant === 7) {
          models.push({ kind: "barrel", x: cx - hw * 0.45, y: cy + hh * 0.39, s: 0.8, z0: baseZ });
        }
      } else if (variant === 1 || variant === 2) {
        models.push({ kind: "crate", x: cx - hw * 0.42, y: cy + hh * 0.30, s: 1.1, z0: baseZ });
        models.push({ kind: "reel", x: cx - hw * 0.24, y: cy + hh * 0.47, s: 0.85, z0: baseZ });
      }
      var sheep = f.sheep || { x: cx - hw * 0.45, y: cy + hh * 0.36 };
      models.push({ kind: "figure", x: sheep.x, y: sheep.y, z0: baseZ });
      models.push({ kind: "lamp", x: cx - hw * 0.52, y: cy + hh * 0.16, z0: baseZ, h: 30 });
      models.push({ kind: "lamp", x: cx + hw * 0.52, y: cy - hh * 0.16, z0: baseZ, h: 26 });
      if (variant === 0) {
        models.push({ kind: "pipeRack",
                      path: [{ x: cx + hw * 0.43, y: cy - hh * 0.15 },
                             { x: cx + hw * 0.23, y: cy - hh * 0.27 }],
                      h0: 6, h1: 6, width: 3, projectId: f.id, compoundPart: true });
      }
      models.push({ kind: "conveyor", path: f.belt, h0: 12, h1: 12, width: 4, ore: true,
                    projectId: f.id });
      // 名牌按主体屋脊定位，不跟随烟囱或独立反应罐抬高。
      f.labelZ = topZ + 10;
    });

    var perimeterStart = models.length;
    var perimeterSmokeStart = smoke.length;
    function anchorFacility(start, smokeStart, name, origin, target) {
      models.slice(start).concat(smoke.slice(smokeStart)).forEach(function (m) {
        m.facility = name;
        m.facilityOrigin = origin;
        m.facilityTarget = target;
      });
    }
    var coolingStart = models.length, coolingSmokeStart = smoke.length;
    // 外围设施共用组坐标；局部零件间距不随扩圈变化。
    models.push({ kind: "cooling", x: 356, y: -233, r: 16, h: 52, z0: 0 });
    models.push({ kind: "cooling", x: 404, y: -207, r: 14, h: 46, z0: 0 });
    smoke.push({ x: 356, y: -233, h: 55, steam: true });
    smoke.push({ x: 404, y: -207, h: 49, steam: true });
    addBox(325, -250, 13, 6.5, 10, "steel", { z0: 0, windows: 2, roof: "flat", vent: true });
    models.push({ kind: "pipeRack", path: [{ x: 338, y: -246 }, { x: 352, y: -236 }], h0: 10, h1: 10, width: 6 });

    anchorFacility(coolingStart, coolingSmokeStart, "coolingPlant",
                   { x: 380, y: -220 }, { x: 155, y: -140 });

    models.push({ kind: "box", x: 480, y: -220, hw: 42, hh: 22, h: 24, role: "steel", windows: 4, roof: "saw", bg: true });
    models.push({ kind: "cyl", x: 510, y: -180, r: 16, h: 34, role: "rust", bands: 4, bg: true });
    // 远景工业带：独立物件不参与建筑组的零件位移。

    models.push({ kind: "cyl", x: 470, y: -100, r: 12, h: 26, role: "steel", bands: 3, bg: true, roadsideTank: 1 });
    models.push({ kind: "box", x: -480, y: -200, hw: 50, hh: 26, h: 22, role: "steel", windows: 3, roof: "flat", bg: true });
    models.push({ kind: "cyl", x: -460, y: -60, r: 24, h: 40, role: "rust", bands: 4, bg: true, roadsideTank: -1 });
    models.push({ kind: "pylon", x: -380, y: -160, bg: true });
    models.push({ kind: "pylon", x: 400, y: -150, bg: true });
    models.push({ kind: "pylon", x: 520, y: -140, bg: true });
    models.push({ kind: "truck", x: -360, y: 190, z0: 0, bg: true });
    models.push({ kind: "truck", x: 360, y: 200, z0: 0, bg: true });
    models.push({ kind: "ore", x: 380, y: 170, s: 2.6, bg: true });
    models.push({ kind: "ore", x: -380, y: 175, s: 2.2, bg: true });
    models.push({ kind: "rock", x: -380, y: -170, s: 2, bg: true });
    models.push({ kind: "rock", x: 400, y: 80, s: 1.6, bg: true });
    models.push({ kind: "shrub", x: -360, y: 180, s: 1.3, bg: true });
    models.push({ kind: "shrub", x: 360, y: 180, s: 1.2, bg: true });
    models.push({ kind: "conveyor", path: [{ x: 454, y: -224 }, { x: 508, y: -180 }], h0: 16, h1: 10, width: 2.6, ore: true, bg: true });
    models.push({ kind: "transferBridge", path: [{ x: 468, y: -196 }, { x: 528, y: -152 }],
                  h0: 16, h1: 12, width: 6, bg: true });
    models.push({ kind: "box", x: 466, y: -198, hw: 7, hh: 4, h: 20, role: "steel", windows: 1, roof: "flat", bg: true });
    models.push({ kind: "box", x: 530, y: -150, hw: 6, hh: 3.5, h: 18, role: "rust", roof: "flat", bg: true });
    var frontY = factories.reduce(function (y, f) { return Math.max(y, f.y + f.halfH); }, 110) + 25;
    models.push({ kind: "truck", x: -380, y: frontY + 10, z0: 0, bg: true });
    // 外围新设施：变电站、水塔和维修库；与项目厂区保持独立。
    var substationStart = models.length;
    addBox(-410, -182, 22, 11, 3, "concrete", { paving: true, bg: true, facility: "substation" });
    for (var tr = 0; tr < 2; tr += 1) {
      addBox(-418 + tr * 15, -182, 5, 2.5, 9, "steel", { bg: true, facility: "substation", vent: true });
      for (var ins = 0; ins < 3; ins += 1) {
        addCyl(-421 + tr * 15 + ins * 3, -182, 0.9, 4, "glass",
               { z0: 9, bg: true, facility: "substation" });
      }
    }
    anchorFacility(substationStart, smoke.length, "substation",
                   { x: -410, y: -182 }, { x: -210, y: -92 });
    var waterStart = models.length;
    addBox(-414, -111, 12, 6, 3, "concrete", { paving: true, bg: true, facility: "waterTower" });
    [[-6, -2], [6, -2], [0, 3]].forEach(function (leg) {
      addCyl(-414 + leg[0], -111 + leg[1], 1.2, 22, "steel", { bg: true, facility: "waterTower" });
    });
    addCyl(-414, -111, 11, 13, "steel", { z0: 22, bg: true, bands: 2, ladder: true, facility: "waterTower" });
    anchorFacility(waterStart, smoke.length, "waterTower",
                   { x: -414, y: -111 }, { x: -214, y: -21 });
    var repairStart = models.length;
    addBox(441, -255, 21, 10.5, 16, "rust",
           { windows: 2, roof: "saw", bg: true, facility: "repairDepot", door: true });
    models.push({ kind: "truck", x: 462, y: -245, z0: 0, bg: true, facility: "repairDepot" });
    anchorFacility(repairStart, smoke.length, "repairDepot",
                   { x: 441, y: -255 }, { x: 231, y: -130 });
    // 厂外建筑群：不生成项目名牌、任务机台或运行状态；整组锚在 facilityAnchor 上，
    // 项目变多（环数 +1）时按锚点整体外移，组内相对位置不变。
    // opts.near=true：摆在前场做近景——不标 bg（bg 会被 roleCols 与雾化调色板压成灰调远景）。
    // opts.plateauAligned=true：位置已按台地前缘公路算好，别再按环数位移（见 perimeterOffset）。
    function utilityGroup(name, x, y, build, opts) {
      var start = models.length;
      build(x, y);
      models.slice(start).forEach(function (m) {
        if (!(opts && opts.near)) m.bg = true;
        m.facility = name;
        if (opts && opts.plateauAligned) m.plateauAligned = true;
        else m.facilityAnchor = { x: x, y: y };
      });
    }
    utilityGroup("serviceQuarter", -170, -140, function (x, y) {
      addBox(x, y, 66, 27, 2, "concrete", { paving: true });
      addBox(x - 26, y - 8, 24, 12, 30, "steel", { z0: 2, roof: "flat", windows: 4, door: true });
      addBox(x - 26, y - 8, 17, 8, 8, "glass", { z0: 32, roof: "flat", windows: 3 });
      addBox(x + 26, y - 5, 20, 10, 19, "rust", { z0: 2, roof: "saw", windows: 3, door: true });
      addBox(x + 4, y + 14, 15, 6, 10, "steel", { z0: 2, roof: "flat", windows: 2, canopy: true });
      addBox(x - 48, y + 9, 9, 4.5, 9, "steel", { z0: 2, roof: "flat", door: true });
      models.push({ kind: "truck", x: x + 39, y: y + 15, z0: 2 });
      models.push({ kind: "lamp", x: x - 8, y: y + 25, z0: 2, h: 22 });
    });
    utilityGroup("energyQuarter", 275, -160, function (x, y) {
      addBox(x, y, 63, 27, 2, "concrete", { paving: true });
      addBox(x - 25, y - 7, 23, 11.5, 25, "rust",
             { z0: 2, roof: "saw", windows: 3, door: true, pipes: true });
      addBox(x - 26, y - 7, 9, 4.5, 6, "steel", { z0: 32, roof: "flat", vent: true });
      addBox(x + 18, y + 9, 17, 8.5, 13, "steel", { z0: 2, roof: "flat", windows: 3, vent: true });
      addCyl(x + 22, y - 15, 9, 31, "steel", { z0: 2, bands: 3, ladder: true });
      addCyl(x + 45, y - 4, 7, 23, "rust", { z0: 2, bands: 2, ladder: true });
      models.push({ kind: "pipeRack", path: [{ x: x + 17, y: y - 9 }, { x: x + 41, y: y + 4 }],
                    h0: 8, h1: 8, width: 3 });
      models.push({ kind: "lamp", x: x - 37, y: y + 19, z0: 2, h: 24 });
    });
    // 西侧物资场：填住外圈后勤带与料场之间那块空沙地，作为库房 / 工棚 / 备件场的补充
    utilityGroup("westDepot", -360, -70, function (x, y) {
      addBox(x - 20, y, 38, 25, 2, "concrete", { paving: true });
      // 主库房（左后）+ 长条工棚（右后），中间留出消防通道
      addBox(x - 25, y - 6, 18, 9, 16, "steel",
             { z0: 2, roof: "flat", windows: 4, door: true });
      addBox(x + 15, y - 21, 13, 5.5, 10, "rust",
             { z0: 2, roof: "saw", windows: 2, door: true });
      // 检修工棚（右侧）与门房（左前）
      addBox(x + 33, y - 6, 11, 6, 10, "steel",
             { z0: 2, roof: "saw", windows: 2, door: true });
      addBox(x - 41, y + 8, 6, 3.5, 7, "glass",
             { z0: 2, roof: "flat", windows: 2, door: true });
      // 备件罐（右前）与管廊
      addCyl(x + 41, y + 14, 5.5, 14, "rust", { z0: 2, bands: 2, ladder: true });
      addCyl(x + 53, y + 8, 4.5, 10, "steel", { z0: 2, bands: 1, ladder: true });
      models.push({ kind: "pipeRack", path: [{ x: x - 25, y: y + 6 }, { x: x - 5, y: y + 6 }],
                    h0: 6, h1: 6, width: 3 });
      // 前场：两辆卡车、两盏灯，拉开整组的横向跨度
      models.push({ kind: "truck", x: x - 21, y: y + 24, z0: 2 });
      models.push({ kind: "truck", x: x + 25, y: y + 24, z0: 2 });
      models.push({ kind: "lamp", x: x - 47, y: y + 24, z0: 2, h: 24 });
      models.push({ kind: "lamp", x: x + 41, y: y + 24, z0: 2, h: 22 });
      // 散料：别都堆在一角
      models.push({ kind: "crate", x: x - 46, y: y - 20, z0: 2, s: 1.1 });
      models.push({ kind: "crate", x: x + 40, y: y + 8, z0: 2, s: 0.9 });
      models.push({ kind: "barrel", x: x + 15, y: y + 22, z0: 2, s: 1 });
      models.push({ kind: "reel", x: x - 35, y: y + 22, z0: 2, s: 0.9 });
      // 小龙门吊站在场院右前缘，吊钩下留一组备件箱
      models.push({ kind: "gantry", x: x + 49, y: y + 30, w: 7, h: 18, hw: 8, hh: 3 });
      models.push({ kind: "crate", x: x + 49, y: y + 34, s: 1.2 });
    }, { near: true });
    // 前场两组近景：贴着前缘公路外侧布置（左西料场、右东罐区）。
    // 位置按当前台地算——公路走向就是台地前缘那两条边，台地变大两组跟着挪，
    // 所以它们标 plateauAligned，不再参与 perimeterOffset 的按环位移（见下）。
    // 场坪一律 2:1 菱形，与园区坪、公路同一倾斜度。
    var roadLen = Math.hypot(plateau.hw + 14, plateau.hh + 8) || 1;
    function roadsideAnchor(side, t, gap) {
      // 前缘左/右臂：侧角 → 前角；返回「路外侧 gap 处」的场地中心（gap=0 即落在公路中线上）
      var ax = side * (plateau.hw + 14), ay = 0;
      var bx = 0, by = plateau.hh + 8;
      var px = ax + (bx - ax) * t, py = ay + (by - ay) * t;
      var nx = side * (plateau.hh + 8) / roadLen, ny = (plateau.hw + 14) / roadLen;
      return { x: Math.round(px + nx * gap), y: Math.round(py + ny * gap),
               nx: nx, ny: ny };
    }
    // 间距不写死：整组建完先量一次「最靠路那面墙沿外法线到公路中线的距离」（负=压上路），
    // 再把整组沿外法线挪过去，只留一条排水沟的宽度。改建筑布局不用回来手改常数。
    var ROAD_CLEARANCE = 14;
    function alignYardToRoad(name, armCorner, n) {
      var near = Infinity;
      models.forEach(function (m) {
        if (m.facility !== name) return;
        var pts = m.path || [{ x: m.x, y: m.y }];
        var hw = m.hw || m.r || 0;
        var hh = m.hh || (m.r ? m.r * 0.5 : 0);
        // 轴系零件（煤棚 / 筒仓群）的纵深本来就沿外法线摆，支撑长度就是 hh；
        // 轴对齐零件要按两条轴在外法线上的投影合成。
        var extent = m.axis ? hh : (hw * Math.abs(n.x) + hh * Math.abs(n.y));
        pts.forEach(function (pt) {
          var d = (pt.x - armCorner.x) * n.x + (pt.y - armCorner.y) * n.y - extent;
          near = Math.min(near, d);
        });
      });
      if (!isFinite(near)) return;
      var shift = Math.round(ROAD_CLEARANCE - near);
      models.forEach(function (m) {
        if (m.facility !== name) return;
        if (m.path) {
          m.path = m.path.map(function (pt) {
            return { x: pt.x + n.x * shift, y: pt.y + n.y * shift };
          });
        } else { m.x += n.x * shift; m.y += n.y * shift; }
      });
    }
    // 两组站在沙地上、贴前缘公路外侧；位置都按当前台地算（公路走向就是台地前缘
    // 那两条边），台地随项目变多长大时跟着挪。
    var stockYard = roadsideAnchor(-1, 0.52, 0);
    var tankYard = roadsideAnchor(1, 0.52, 0);

    // 料场整组顺着公路排：u 轴取公路方向，v 轴取前缘那条边的外法线（v 越大越远离马路）。
    // 老版把「纵深」当成世界 y 轴，而公路本身是斜的，摆出来整排与马路不平行，最高体量
    // 反而被推到最靠路的一侧；现在组内一律用 (u, v) 表达，整排与马路平行。
    var stockNormal = { x: -(plateau.hh + 8) / roadLen, y: (plateau.hw + 14) / roadLen };
    var stockAlong = { x: stockNormal.y, y: -stockNormal.x };

    // 料场按两条马路的方向摆：L 沿近侧公路，R 沿另一条路；两条轴围出平行四边形场院，
    // 三排内容沿 R 方向由后往前排——后排筒仓、中排煤棚与煤堆、前排地磅与排队卡车。
    // 这样围栏的两条翼栅和场内每一排都共用同两条马路方向，不会一边斜一边正。
    utilityGroup("stockyard", stockYard.x, stockYard.y, function (x, y) {
      function at(u, v) {
        return { x: x + stockAlong.x * u + stockNormal.x * v,
                 y: y + stockAlong.y * u + stockNormal.y * v };
      }
      // 围栏后缘中点作为场院坐标原点；L 沿近路，R 沿另一条路并向排前方向
      var backC = at(0, 72);
      var cross = { x: stockAlong.x, y: -stockAlong.y };
      function site(l, d) {
        return { x: backC.x + stockAlong.x * l + cross.x * d,
                 y: backC.y + stockAlong.y * l + cross.y * d };
      }
      var W = 75, D = 95;
      // 后排：筒仓群 + 提升机塔，仓列沿 L 排开
      models.push(Object.assign({ kind: "siloBattery", axis: stockAlong,
                    count: 3, r: 6.4, h: 30, gapX: 24, towerGap: 9, role: "steel",
                    hw: 58, hh: 4 }, site(0, 12)));
      // 中排：煤棚居中，两堆煤分列左右
      models.push(Object.assign({ kind: "coalShed", axis: stockAlong,
                    hw: 34, hh: 11, h: 20, rise: 7, role: "steel" }, site(0, 45)));
      models.push(Object.assign({ kind: "coalPile", hw: 14, hh: 8, h: 10 }, site(-52, 45)));
      models.push(Object.assign({ kind: "coalPile", hw: 11, hh: 6.5, h: 8 }, site(50, 45)));
      // 运煤线：塔顶 → 煤棚脊
      models.push({ kind: "conveyor", path: [site(46, 13), site(-14, 45)],
                    h0: 40, h1: 20, width: 2.6, ore: true });
      // 前排：地磅在中间，排队车朝公路一侧收口，磅房贴在地磅左前方
      models.push(Object.assign({ kind: "weighbridge", hw: 18, hh: 8, h: 2.5 },
                                site(-20, 60)));
      models.push(Object.assign({ kind: "truck", z0: 3 }, site(-20, 60)));
      models.push(Object.assign({ kind: "truck" }, site(-20, 78)));
      models.push(Object.assign({ kind: "truck" }, site(-20, 90)));
      var booth = site(-38, 80);
      addBox(booth.x, booth.y, 5, 4, 7, "concrete", { roof: "flat", windows: 2, door: true });
      models.push(Object.assign({ kind: "crate", s: 0.9 }, site(30, 82)));
      models.push(Object.assign({ kind: "barrel", s: 0.9 }, site(45, 80)));
      models.push(Object.assign({ kind: "reel", s: 0.9 }, site(-60, 70)));
      // 灯杆：前缘两端 + 地磅入口 + 煤堆侧
      models.push(Object.assign({ kind: "lamp", h: 26 }, site(-65, 88)));
      models.push(Object.assign({ kind: "lamp", h: 22 }, site(65, 88)));
      models.push(Object.assign({ kind: "lamp", h: 20 }, site(-45, 92)));
      models.push(Object.assign({ kind: "lamp", h: 22 }, site(-65, 45)));
      // 两盏冷白投光：一盏照筒仓群，一盏照煤棚右翼
      models.push(Object.assign({ kind: "floodlight", hw: 6, hh: 3, h: 36 }, site(-65, 25)));
      models.push(Object.assign({ kind: "floodlight", hw: 6, hh: 3, h: 34 }, site(67, 30)));
      // 围栏：后缘沿 L，左右翼沿 R；前侧完全敞开，地磅入口正对公路
      models.push({ kind: "fence", role: "steel", h: 4, width: 1.5, postGap: 6,
                    path: [site(-W, D), site(-W, 0), site(W, 0), site(W, D)] });
    }, { near: true, plateauAligned: true });
    // 前场东侧罐区：北排是生产（气柜 + 循环水池 + 泵房/管廊），南排是装车（台 + 罐车），
    // 棚子必须整块落在北排的前缘以南——它在 h=15 处张开，压到气柜 / 水池底上就看着粘在一起。
    utilityGroup("tankFarm", tankYard.x, tankYard.y, function (x, y) {
      models.push({ kind: "gasholder", x: x - 46, y: y - 10, r: 15, h: 30, role: "rust" });
      models.push({ kind: "basin", x: x + 30, y: y - 14, hw: 30, hh: 15, h: 3 });
      addBox(x + 62, y - 4, 9, 4.5, 8, "steel", { roof: "flat", windows: 2, vent: true });
      models.push({ kind: "pipeRack", path: [{ x: x + 30, y: y - 4 }, { x: x + 56, y: y - 4 }],
                    h0: 7, h1: 7, width: 2.5 });
      models.push({ kind: "loadingRack", x: x - 8, y: y + 30, hw: 22, hh: 11, h: 15 });
      models.push({ kind: "truck", x: x - 8, y: y + 38 });   // 停在棚下靠外的位置
      models.push({ kind: "flare", x: x + 64, y: y + 12, hw: 7, hh: 3.5, h: 54 });
      models.push({ kind: "barrel", x: x - 58, y: y + 14, s: 1 });
      models.push({ kind: "lamp", x: x - 64, y: y + 36, h: 24 });
      models.push({ kind: "lamp", x: x + 44, y: y + 36, h: 26 });
    }, { near: true, plateauAligned: true });
    // 贴路：两组各挪到「建筑最近那面墙离公路中线 14 单位」。
    [["stockyard", stockYard, -1], ["tankFarm", tankYard, 1]].forEach(function (item) {
      var side = item[2];
      var n = { x: side * (plateau.hh + 8) / roadLen, y: (plateau.hw + 14) / roadLen };
      alignYardToRoad(item[0], { x: side * (plateau.hw + 14), y: 0 }, n);
    });
    var tracks = [];
    var perimeterRing = Math.max(1, Math.ceil((Math.sqrt(factories.length + 1) - 1) / 2));
    function perimeterOffset(m) {
      if (m.plateauAligned) return { x: 0, y: 0 };
      if (m.facilityOrigin) {
        return { x: m.facilityTarget.x * perimeterRing - m.facilityOrigin.x,
                 y: m.facilityTarget.y * perimeterRing - m.facilityOrigin.y };
      }
      if (m.roadsideTank) {
        // 罐体连同高度投影退到前缘道路外侧，不能只让底心离开路面。
        var tankX = m.roadsideTank < 0 ? -300 : 320;
        var tankY = m.roadsideTank < 0 ? 110 : 95;
        return { x: tankX * perimeterRing - m.x, y: tankY * perimeterRing - m.y };
      }
      if (m.facilityAnchor) {
        return { x: m.facilityAnchor.x * (perimeterRing - 1),
                 y: m.facilityAnchor.y * (perimeterRing - 1) };
      }
      var p = m.path ? pointAt(m.path, 0.5) : m;
      var dx, dy;
      if (!m.bg) { dx = -225; dy = 80; }
      else if (p.y < 0) {
        dx = p.x < 0 ? 200 : -210;
        dy = p.x < 0 ? 90 : 125;
        if (p.y > -110) { dx = p.x < 0 ? 200 : -180; dy = p.x < 0 ? 115 : 140; }
      } else { dx = p.x < 0 ? 170 : -170; dy = -60; }
      if (m.kind === "pylon") {
        if (p.x < 0) { dx = 170; dy = 50; }
        else if (p.x < 450) { dx = -170; dy = 75; }
        else { dx = -190; dy = 100; }
      }
      if (m.kind === "rock" && p.y < 0) { dx = 180; dy = 60; }
      if (m.kind === "rock" && p.y > 0) { dx = -150; dy = 0; }
      var x = p.x + dx, y = p.y + dy;
      if (perimeterRing > 1) {
        dx += x * (perimeterRing - 1);
        dy += y * (perimeterRing - 1);
      }
      return { x: dx, y: dy };
    }
    for (var pi = perimeterStart; pi < models.length; pi += 1) {
      var pm = models[pi], offset = perimeterOffset(pm);
      pm.peripheral = true;
      if (pm.path) {
        pm.path = pm.path.map(function (pt) { return { x: pt.x + offset.x, y: pt.y + offset.y }; });
      } else { pm.x += offset.x; pm.y += offset.y; }
    }
    for (var si = perimeterSmokeStart; si < smoke.length; si += 1) {
      var sm = smoke[si], smokeOffset = perimeterOffset(sm);
      sm.x += smokeOffset.x; sm.y += smokeOffset.y;
    }

    // 中央调度厅：石台 + 台阶 + 钢塔 + 检修环 + 玻璃冠 + 天线 + 四角灯柱
    addBox(hall.x, hall.y, 37, 18.5, 6, "concrete", { paving: true, hallBase: true });
    addBox(hall.x, hall.y, 30, 15, 2.5, "concrete", { z0: 6, step: true });
    for (var st = 0; st < 3; st += 1) {
      addBox(hall.x, hall.y + 20 + st * 2.1, 12 - st * 3, 1.1, 1 + st,
             "concrete", { z0: 0, step: true });
    }
    addBox(hall.x, hall.y, 18, 9, 40, "steel",
           { z0: 8.5, windows: 3, door: true, roof: "flat", tower: true });
    addBox(hall.x, hall.y, 20, 10, 9, "glass",
           { z0: 48.5, windows: 3, roof: "flat", crown: true });
    addCyl(hall.x, hall.y, 1.6, 10, "steel", { z0: 57.5 });
    models.push({ kind: "radar", x: hall.x, y: hall.y, z0: 67.5 });
    models.push({ kind: "lamp", x: hall.x - 42, y: hall.y + 13, z0: 6, h: 24 });
    models.push({ kind: "lamp", x: hall.x + 42, y: hall.y + 13, z0: 6, h: 24 });
    models.push({ kind: "lamp", x: hall.x - 36, y: hall.y - 14, z0: 6, h: 20 });
    models.push({ kind: "lamp", x: hall.x + 36, y: hall.y - 14, z0: 6, h: 20 });
    models.push({ kind: "flag", x: hall.x - 14, y: hall.y + 19, z0: 6, h: 26 });
    models.push({ kind: "flag", x: hall.x + 14, y: hall.y + 19, z0: 6, h: 26 });

    return { models: models, patches: patches, smoke: smoke, sparks: sparks,
             tracks: tracks, roads: roads };
  }

  function routeBelt(a, b, seed) {
    var dx = b.x - a.x, dy = b.y - a.y;
    var length = Math.hypot(dx, dy) || 1;
    // 从调度厅外墙出发；直达对应入口，不绕去另一个厂区。
    var start = Math.min(31, length * 0.25);
    return [
      { x: a.x + dx / length * start, y: a.y + dy / length * start },
      { x: b.x, y: b.y },
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
    S.selectedKey = f.lines.length ? f.lines[0].key : null;
    S.focusPid = pid;
    applyFocusCamera(true);
    renderPanel();   // 选中态要跟着变
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
    if (dom.hudTokens) dom.hudTokens.textContent = fmtTokens(c.tokens_today || 0);
    if (dom.hudAttentionCell) dom.hudAttentionCell.classList.toggle("alert", (c.attention || 0) > 0);
    // 对话 / 子代理在跑（工厂之外的盲区，只读快照）
    var live = (st && st.live) || {};
    var liveCount = (live.chat || 0) + (live.subagent || 0);
    if (dom.hudChat) dom.hudChat.textContent = String(liveCount);
    if (dom.hudChatCell) {
      var stale = (live.runs || []).filter(liveStale).length;
      dom.hudChatCell.classList.toggle("alert", stale > 0);
      dom.hudChatCell.title = liveCount
        ? ("在跑的对话轮 " + (live.chat || 0) + " 个、后台子代理 " + (live.subagent || 0) + " 个" +
           (stale ? "（其中 " + stale + " 个长时间没有更新，可能卡住）" : "") + "；点击查看")
        : "当前没有在跑的对话轮或子代理；点击查看说明";
    }
    // 样板厂：计数条整体灰掉并挂「演示」，避免把演示数字当成真的
    if (dom.hud) dom.hud.classList.toggle("demo", !!S.demo);
    if (dom.demoBadge) dom.demoBadge.classList.toggle("hidden", !S.demo);
  }

  /** 对话轮 / 子代理「疑似卡住」：服务端快照的 stale 或本地按 updated_at 推算。
      轮询间隔内也要能自己变红，不能等下一次拉取才提示。 */
  function liveStale(r) {
    if (!r) return false;
    if (r.stale) return true;
    var updated = Number(r.updated_at) || 0;
    return updated > 0 && nowMs() / 1000 - updated > 300;
  }

  /** token 数：过千折成 k，过百万折成 M（计数条一格放不下长数字）。 */
  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n <= 0) return "0";
    if (n < 1000) return String(Math.round(n));
    if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + "k";
    return (n / 1000000).toFixed(1) + "M";
  }

  /** 工序进度是否还新鲜：超过 30 秒没有新事件就当没有（避免显示上一个工具名）。 */
  function freshProgress(pr) {
    if (!pr || !pr.at) return null;
    if (nowMs() / 1000 - pr.at > 30) return null;
    return pr;
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
    if (line.last_status === "empty"
        || (line.health && line.health.last_status === "empty")) return "上次无产出";
    if (line.health && line.health.last_outcome === "partial") return "上次有拦截";
    if (line.status === "done") return "已完成";
    if (line.last_status === "cancelled") return "上次叫停";
    return "待机";
  }

  /** 产线当前这轮运行的停滞等级（机台用）。没在跑 / 取不到运行行时返回空。 */
  function lineStall(line) {
    if (!line || line.status !== "running") return "";
    var run = line.running_run_id ? findRun(line.running_run_id) : null;
    // 装配线没有「本次运行」句柄：退回用 line.progress 与 next_run_at 之外的信息判不了，
    // 这里只对定时任务给停滞标记，不猜。
    if (!run) return "";
    return runStall(run, nowMs());
  }

  function lineRunSeconds(line) {
    if (!line.running_run_id) return 0;
    var run = findRun(line.running_run_id);
    return run ? runElapsed(run) : 0;
  }

  /** 在跑的这一轮在干什么：当前工具名，没有工具时退回「第 N 步」。 */
  function runningStepText(line) {
    var pr = freshProgress(line && line.progress);
    if (!pr) return "";
    if (pr.tool) return pr.tool;
    if (pr.steps) return "第 " + pr.steps + " 步";
    return "";
  }

  function lineDotClass(line) {
    if (line.status === "running") return "running";
    if (line.status === "paused" || line.status === "stopped") return "paused";
    if (line.status === "failed" || line.last_status === "error") return "failed";
    if (line.last_status === "empty"
        || (line.health && line.health.last_status === "empty")) return "warn";
    if (line.health && line.health.last_outcome === "partial") return "warn";
    if (line.status === "done") return "ok";
    return "";
  }

  function lineStateClass(line) {
    if (line.status === "running") return "running";
    if (line.status === "failed" || line.last_status === "error") return "failed";
    if (line.last_status === "empty"
        || (line.health && line.health.last_status === "empty")) return "warn";
    if (line.health && line.health.last_outcome === "partial") return "warn";
    return "";
  }

  // ---------- 厂区级异常信号（面板之外的视觉出口） ----------
  /** 把「这座厂区有没有出事」压成一个等级，供场景在地面 / 灯柱两层画出来。

      以前只有左面板「连错 3」和机台那盏几十像素的小灯能表达故障，全景下
      根本扫不到。厂区级信号直接挂在整座场院上：一眼看过去哪座是红的。

      等级：
        0 = 正常
        1 = 黄：空产出 / 有工具被拦（partial）——预授权没勾全的高频原因
        2 = 红：上次失败，或有运行被人工判负
        3 = 严重红：连续失败达到 alert_after_failures 阈值
        4 = 停工：厂区不可用（项目目录不存在）；目录没了，别的判断都不可靠

      同一产线取等级最高：一条线「有拦截」被另一条线「失败」压住，是刻意的——
      前者提示补充预授权，后者是当下正在烧时间。 */
  function factoryAlert(f) {
    if (!f) return { level: 0 };
    if (f.root_ok === false) return { level: 4 };
    var lines = (f.project && f.project.lines) || [];
    var hasFail = false, hasWarn = false, maxStreak = 0;
    lines.forEach(function (l) {
      if (!l) return;
      var h = l.health || {};
      if (h.streak > maxStreak) maxStreak = h.streak;
      if (l.status === "failed" || l.last_status === "error"
          || h.last_outcome === "failed") hasFail = true;
      if (l.last_status === "empty" || h.last_status === "empty"
          || h.last_outcome === "partial") hasWarn = true;
    });
    var cfg = (activeState() && activeState().config) || {};
    var threshold = Number(cfg.alert_after_failures);
    if (!threshold || threshold < 1) threshold = 3;
    if (maxStreak >= threshold) return { level: 3, streak: maxStreak };
    if (hasFail) return { level: 2, streak: maxStreak };
    if (hasWarn) return { level: 1, streak: maxStreak };
    return { level: 0 };
  }

  /** 每次 render 只算一遍，两处绘制（地面 / 灯柱）共用同一份判定。
      逐帧重复遍历厂区产线虽然开销可忽略，但等级不一致会画出「地面亮红灯
      灯柱却是黄」的观感。 */
  function refreshFactoryAlerts() {
    if (!S.world) return;
    S.world.factories.forEach(function (f) { f.alert = factoryAlert(f); });
  }

  /** 等距菱形轮廓点列：一个环而非填满，用于脉冲光晕 / 停工边界。 */
  function ringPts(cx, cy, hw, hh) {
    return [{ x: cx - hw, y: cy }, { x: cx, y: cy - hh }, { x: cx + hw, y: cy },
            { x: cx, y: cy + hh }, { x: cx - hw, y: cy }];
  }

  // ---------- 面板筛选与批量操作（纯函数，便于 node 侧直接测） ----------
  /** 一条产线是否符合当前筛选模式。 */
  function matchFilterMode(line, attentionKeys, mode) {
    if (!mode || mode === "all") return true;
    if (mode === "running") return line.status === "running";
    if (mode === "stopped") return line.status === "paused" || line.status === "stopped";
    if (mode === "attention") return !!(attentionKeys && attentionKeys.has(line.key));
    return true;
  }

  /** 按「模式 + 关键词」过滤厂区分组，返回 [{project, lines}]。
      关键词命中厂区名时整座厂区放行；没有关键词且模式为「全部」时，空厂区
      仍要露出来（否则「＋ 新建一条定时产线」的入口会跟着消失）。 */
  function filterProjects(projects, attentionKeys, filter) {
    filter = filter || {};
    var mode = filter.mode || "all";
    var q = String(filter.q || "").trim().toLowerCase();
    var out = [];
    (projects || []).forEach(function (p) {
      var allLines = (p.lines || []);
      var projectHit = !!q && String(p.name || "").toLowerCase().indexOf(q) >= 0;
      var lines = allLines.filter(function (l) {
        if (!matchFilterMode(l, attentionKeys, mode)) return false;
        if (!q) return true;
        return projectHit || String(l.name || "").toLowerCase().indexOf(q) >= 0;
      });
      if (lines.length) out.push({ project: p, lines: lines });
      else if (!q && mode === "all" && !allLines.length) out.push({ project: p, lines: [] });
    });
    return out;
  }

  /** 批量派工目标：启用的定时产线，且当前不在跑 / 未停工（装配线不参与）。 */
  function batchRunTargets(lines) {
    return (lines || []).filter(function (l) {
      return l.kind === "cron" && l.status !== "running"
        && l.status !== "paused" && l.status !== "stopped";
    });
  }

  /** 批量暂停目标：还在启用中的定时产线（已暂停 / 停工的跳过）。 */
  function batchPauseTargets(lines) {
    return (lines || []).filter(function (l) {
      return l.kind === "cron" && l.status !== "paused" && l.status !== "stopped";
    });
  }

  function filterActive() {
    return (S.filter.mode && S.filter.mode !== "all") || !!String(S.filter.q || "").trim();
  }

  function bindFilter() {
    if (dom.chips) {
      dom.chips.querySelectorAll("[data-filter]").forEach(function (b) {
        b.onclick = function () {
          S.filter.mode = b.dataset.filter || "all";
          syncFilterChips();
          renderPanel();
        };
      });
    }
    if (dom.search) {
      dom.search.addEventListener("input", function () {
        S.filter.q = dom.search.value;
        if (S.filterTimer) clearTimeout(S.filterTimer);
        S.filterTimer = setTimeout(function () {
          S.filterTimer = 0;
          renderPanel();
        }, FILTER_DEBOUNCE_MS);
      });
      dom.search.addEventListener("keydown", function (ev) {
        // Esc 先清搜索框（并拦住冒泡，否则会顺手关掉整个工厂视图）
        if (ev.key === "Escape" && dom.search.value) {
          ev.stopPropagation();
          dom.search.value = "";
          S.filter.q = "";
          renderPanel();
        }
      });
    }
    syncFilterChips();
  }

  function syncFilterChips() {
    if (!dom.chips) return;
    dom.chips.querySelectorAll("[data-filter]").forEach(function (b) {
      var on = (b.dataset.filter || "all") === S.filter.mode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function resetFilter() {
    S.filter.mode = "all";
    S.filter.q = "";
    if (dom.search) dom.search.value = "";
    syncFilterChips();
    renderPanel();
  }

  /** 面板内方向键导航：在可见按钮之间移动焦点（不改选中态，选中由点击决定）。 */
  function panelKeyNav(ev) {
    var keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (keys.indexOf(ev.key) < 0 || !dom.panelBody) return;
    var items = Array.prototype.slice.call(dom.panelBody.querySelectorAll("button"))
      .filter(function (b) { return !b.disabled && b.offsetParent !== null; });
    if (!items.length) return;
    var idx = items.indexOf(document.activeElement);
    if (idx < 0) return; // 焦点不在面板里：不抢方向键（页面别处可能在用）
    var next = idx;
    if (ev.key === "ArrowDown") next = Math.min(items.length - 1, idx + 1);
    else if (ev.key === "ArrowUp") next = Math.max(0, idx - 1);
    else if (ev.key === "Home") next = 0;
    else next = items.length - 1;
    ev.preventDefault();
    items[next].focus();
  }

  function findProjectGroup(pid) {
    var st = activeState();
    var projects = (st && st.projects) || [];
    for (var i = 0; i < projects.length; i += 1) {
      if (Number(projects[i].id) === Number(pid)) return projects[i];
    }
    return null;
  }

  /** 厂区级批量派工：确认后逐条串行派（单次上限 BATCH_RUN_LIMIT 条）。 */
  function batchRunLines(pid, btn) {
    if (S.demo) { addNotice("样板厂不会真的运行；先新建一条真实产线"); return; }
    var project = findProjectGroup(pid);
    if (!project) return;
    var all = batchRunTargets(project.lines);
    if (!all.length) { addNotice("这座厂区没有待命的产线"); return; }
    var targets = all.slice(0, BATCH_RUN_LIMIT);
    var extra = all.length - targets.length;
    confirmModal(
      "会给「" + (project.name || "这座厂区") + "」的 " + targets.length + " 条产线各派一次工。"
        + (extra > 0 ? "另有 " + extra + " 条超出单次上限 " + BATCH_RUN_LIMIT + " 条，本次不派。" : "")
        + "每条都会真的跑一轮，按各自项目的白名单与权限门执行。",
      { title: "批量派工", okLabel: "派工" },
    ).then(function (ok) {
      if (!ok) return;
      if (btn) btn.disabled = true;
      var done = 0;
      var problems = [];
      var chain = Promise.resolve();
      targets.forEach(function (l) {
        chain = chain.then(function () {
          return request("factory.run_now", { task_id: l.id }).then(function (r) {
            if (r && r.started) done += 1;
            else problems.push((l.name || l.key) + "：" + ((r && r.notice) || "未派"));
          }).catch(function (e) {
            problems.push((l.name || l.key) + "：" + (e.message || "失败"));
          });
        });
      });
      chain.then(function () {
        var tail = problems.length
          ? "；" + problems.length + " 条未派（" + problems.slice(0, 2).join("；") + "）" : "";
        addNotice("批量派工：" + done + " 条已派" + tail);
      }).finally(function () {
        if (btn) btn.disabled = false;
      });
    });
  }

  /** 厂区级批量暂停：确认后逐条串行停用（只动定时产线，装配线不碰）。 */
  function batchPauseLines(pid, btn) {
    if (S.demo) { addNotice("样板厂不会真的运行；先新建一条真实产线"); return; }
    var project = findProjectGroup(pid);
    if (!project) return;
    var targets = batchPauseTargets(project.lines);
    if (!targets.length) { addNotice("这座厂区没有启用中的产线"); return; }
    confirmModal(
      "会暂停「" + (project.name || "这座厂区") + "」的 " + targets.length + " 条产线："
        + "之后不再到点派工（仍可手动跑一次）。之后可在自动化页逐条恢复。",
      { title: "批量暂停", okLabel: "全部暂停", danger: true },
    ).then(function (ok) {
      if (!ok) return;
      if (btn) btn.disabled = true;
      var done = 0;
      var failed = 0;
      var chain = Promise.resolve();
      targets.forEach(function (l) {
        chain = chain.then(function () {
          return request("factory.toggle", { task_id: l.id, enabled: false })
            .then(function () { done += 1; })
            .catch(function () { failed += 1; });
        });
      });
      chain.then(function () {
        addNotice("批量暂停：" + done + " 条已停" + (failed ? "，" + failed + " 条失败" : ""));
        factoryReload();
      }).finally(function () {
        if (btn) btn.disabled = false;
      });
    });
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
    var attentionKeys = new Set();
    (st.attention || []).forEach(function (a) {
      if (a && a.line_key) attentionKeys.add(a.line_key);
    });
    var groups = filterProjects(projects, attentionKeys, S.filter);
    if (!groups.length) {
      dom.panelBody.innerHTML = '<div class="fx-empty">没有符合条件的产线。' +
        '<button class="fx-add-line" data-clear-filter="1">清除筛选</button></div>';
      return;
    }
    var html = "";
    groups.forEach(function (g) {
      var p = g.project;
      var lines = g.lines;
      var allLines = p.lines || [];
      var busy = allLines.filter(function (l) { return l.status === "running"; }).length;
      // 批量按钮：样板厂一律禁用（不能对演示数据派工），没有目标也禁用
      var runCount = batchRunTargets(lines).length;
      var pauseCount = batchPauseTargets(lines).length;
      var demoOff = !!S.demo;
      html += '<div class="fx-proj">';
      html += '<div class="fx-proj-head">' +
        '<button class="fx-proj-title" data-focus-project="' + escapeHtml(String(p.id)) +
        '" title="镜头飞到这座厂区">' +
        "<span>" + escapeHtml(p.name || "未命名项目") + "</span>" +
        '<span class="fx-badge' + (p.root_ok === false ? " warn" : "") + '">' +
        (p.root_ok === false ? "停工" : allLines.length + " 线" + (busy ? " · " + busy + " 在忙" : "")) +
        "</span></button>" +
        '<span class="fx-proj-ops">' +
        '<button data-batch-run="' + escapeHtml(String(p.id)) + '"' +
        (demoOff || !runCount ? " disabled" : "") + " title=\"" +
        (demoOff ? "样板厂：不会真的运行"
          : "把这座厂区里 " + runCount + " 条待命产线各跑一次（单次最多 " + BATCH_RUN_LIMIT + " 条）") +
        '">▶</button>' +
        '<button data-batch-pause="' + escapeHtml(String(p.id)) + '"' +
        (demoOff || !pauseCount ? " disabled" : "") + " title=\"" +
        (demoOff ? "样板厂：不会真的运行"
          : "暂停这座厂区的 " + pauseCount + " 条启用产线") +
        '">⏸</button>' +
        "</span></div>";
      if (!lines.length) {
        html += '<div class="fx-empty">这个厂区还没有产线。' +
          '<button class="fx-add-line" data-add-project="' + escapeHtml(String(p.id)) + '">＋ 新建一条定时产线</button>' +
          "</div>";
      } else {
        lines.forEach(function (l) {
          var next = l.next_run_at ? " · 下次 " + fmtTs(l.next_run_at).slice(-5) : "";
          var sub = (l.trigger_text || "") + next;
          // 在跑的产线优先显示「在跑什么」（工序进度），其次才是排期
          if (l.status === "running") {
            var step = runningStepText(l);
            if (step) sub = "在跑 " + step;
          }
          if (l.health && l.health.streak >= 2) sub += " · 连错 " + l.health.streak;
          var stateText = lineStateText(l);
            html += '<button class="fx-line ' + lineDotClass(l) +
              (S.selectedKey === l.key ? " active" : "") +
              '" data-line-key="' + escapeHtml(l.key) + '" title="点开看最近运行与派工">' +
            '<span class="fx-dot ' + lineDotClass(l) + '"></span>' +
            '<span class="fx-line-main">' +
            '<span class="fx-line-name">' + escapeHtml(l.name || l.key) + "</span>" +
            '<span class="fx-line-sub">' + escapeHtml(sub) + "</span>" +
            "</span>" +
            '<span class="fx-line-state ' + lineStateClass(l) + '">' + escapeHtml(stateText) + "</span>" +
            "</button>";
        });
      }
      html += "</div>";
    });
    if (filterActive()) {
      var shown = groups.reduce(function (n, g) { return n + g.lines.length; }, 0);
      html += '<div class="fx-empty fx-filter-note">筛选中：' + shown + " 条产线。" +
        '<button class="fx-add-line" data-clear-filter="1">清除筛选</button></div>';
    }
    dom.panelBody.innerHTML = html;
  }

  function refreshPanelStates() {
    if (!S.mounted || !dom.panelBody) return;
    var active = document.activeElement;
    if (dom.panelBody.contains(active)) {
      // 焦点在产线行上时可以重建（方向键导航后仍会停在这一行）；焦点在
      // 厂区头的批量按钮等其它控件上时不动，免得把用户的操作目标抽走
      var key = (active && active.dataset && active.dataset.lineKey) || "";
      if (!key) return;
      renderPanel();
      var rows = dom.panelBody.querySelectorAll("[data-line-key]");
      for (var i = 0; i < rows.length; i += 1) {
        if (rows[i].dataset.lineKey === key) { rows[i].focus(); break; }
      }
      return;
    }
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
    S.focusPid = null;   // 全景 = 退出聚焦档（Esc / 重开视图都回到这里）
    var w = S.bufW, h = S.bufH;
    var b = (S.world && S.world.bounds) || { minX: -80, maxX: 80, minY: -50, maxY: 50 };
    var pad = 32;
    var z = Math.min((w - pad * 2) / Math.max(1, b.maxX - b.minX),
                     (h - pad * 2) / Math.max(1, b.maxY - b.minY));
    z = Math.max(0.01, Math.min(z, 1.45));
    S.fitZoom = z;
    var cx = (b.minX + b.maxX) / 2;
    // 天空占上方约 22%，园区中心落在剩余陆地中间；保留底部安全边距。
    var landOffset = Math.max(0, Math.min(h * 0.11,
      (h - (b.maxY - b.minY) * z) / 2 - 16));
    var cy = (b.minY + b.maxY) / 2 - landOffset / z;
    if (animate) {
      S.cam.tx = cx; S.cam.ty = cy; S.cam.tz = z;
    } else {
      S.cam.x = S.cam.tx = cx;
      S.cam.y = S.cam.ty = cy;
      S.cam.zoom = S.cam.tz = z;
      render();
    }
    updateLabels();
  }

  /** 聚焦档：把镜头推进到某个厂区。

      以前 focusProject 只改选中态、镜头被 fitCamera 锁死在全景，而机台名牌
      的显示条件是「zoom ≥ 全景缩放 × 1.15」——条件恒为假，设计稿里最核心的
      观战信息（机台名 / 在跑计时 / 正在执行哪个工具）一格都没画出来。
      聚焦档同时解决「点厂区头却没反应」的观感问题：真的飞过去。 */
  function applyFocusCamera(animate) {
    var f = S.world && S.world.factories.find(function (x) { return x.id === S.focusPid; });
    if (!f) { S.focusPid = null; fitCamera(animate); return; }
    var w = f.halfW * 2 + 90;
    var h = f.halfH * 2 + 150;
    var pad = 24;
    var z = Math.min((S.bufW - pad * 2) / Math.max(1, w),
                     (S.bufH - pad * 2) / Math.max(1, h));
    z = clamp(z, (S.fitZoom || 0.82) * 1.15, 2.6);
    var cx = f.x;
    var cy = f.y - 10 / z;   // 抬高一点，给机台上方的名牌留位置
    if (animate) {
      S.cam.tx = cx; S.cam.ty = cy; S.cam.tz = z;
    } else {
      S.cam.x = S.cam.tx = cx;
      S.cam.y = S.cam.ty = cy;
      S.cam.zoom = S.cam.tz = z;
      render();
    }
    updateLabels();
  }

  // ---------- 场景尺寸 ----------
  function resizeScene(initial) {
    if (!dom.stage || !dom.canvas) return;
    // DOM 标签使用逻辑 CSS 像素；rect 已含全局界面缩放，混用会让名牌整体漂移。
    S.cssW = Math.max(1, dom.stage.clientWidth);
    S.cssH = Math.max(1, dom.stage.clientHeight);
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
    // 聚焦档要跟着窗口尺寸重算取景，不能被一次 resize 拉回全景
    if (S.focusPid) applyFocusCamera(false);
    else fitCamera(false);
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
      refreshFactoryAlerts();
      drawShadows(ctx, pal);
      refreshUpcoming();
      drawAlertGround(ctx, pal);
      drawLightPools(ctx, pal);
      drawBerths(ctx, pal);
      drawModels(ctx, pal);
      drawUpcoming(ctx, pal);
      drawPackages(ctx, pal);
      drawEffects(ctx, pal);
      drawLighting(ctx, pal);
      drawWeather(ctx, pal);
      drawAlertBeacon(ctx, pal);
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

  function sceneHorizon() {
    var nominal = Math.max(22, Math.round(S.bufH * 0.22));
    if (!S.world) return nominal;
    var nearestRoof = S.bufH;
    S.world.models.forEach(function (m) {
      if (!m.peripheral || (!m.h && !m.h0 && !m.h1) || m.paving) return;
      var height = (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0));
      var depth = m.hh || (m.r ? m.r * 0.5 : 5);
      (m.path || [m]).forEach(function (p) {
        nearestRoof = Math.min(nearestRoof, viewPoint(p.x, p.y - height - depth).y);
      });
    });
    return Math.max(22, Math.min(nominal, nearestRoof - 14));
  }

  function drawSkyGround(ctx, pal) {
    var W = S.bufW, H = S.bufH;
    var skyH = sceneHorizon();
    var kind = weatherKind();
    var heavy = kind === "cloudy" || kind === "rain" || kind === "drizzle"
      || kind === "snow" || kind === "thunder" || kind === "fog";
    var sky = ctx.createLinearGradient(0, 0, 0, skyH + 26);
    sky.addColorStop(0, pal.skyTop);
    sky.addColorStop(0.62, pal.skyMid);
    sky.addColorStop(1, pal.skyLow);
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, W, skyH + 26);
    // 太阳/月亮放在左上：与墙面受光（左面中、右面暗）和右下投影同一方向
    var sunX = Math.round(W * 0.24);
    var sunY = Math.round(skyH * 0.30);
    var glow = ctx.createRadialGradient(sunX, sunY, 2, sunX, sunY, Math.round(W * 0.36));
    glow.addColorStop(0, pal.sun);
    glow.addColorStop(1, "rgba(255, 210, 130, 0)");
    ctx.globalAlpha = (S.night ? 0.14 : 0.38) * (heavy ? 0.25 : 1);
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, skyH + 26);
    var sunBreath = S.reduced ? 0 : Math.sin(S.ticks * 0.025) * 0.025;
    ctx.globalAlpha = ((S.night ? 0.5 : 0.42) + sunBreath) * (heavy ? 0.35 : 1);
    var orbR = Math.max(2, Math.round(W * 0.010));
    fillOval(ctx, sunX, sunY, orbR, orbR, pal.sun);
    ctx.globalAlpha = 1;
    // 夜空星点：阴雨 / 雾天被云层挡住
    if (S.night && !heavy) {
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
    if (!S.night && !heavy) {
      // 晴天 / 多云的薄云：低速漂移，给固定视角的天空一点远景呼吸感。
      ctx.globalAlpha = 0.15;
      var cloudDrift = S.reduced ? 0 : (S.ticks % 4800) * 0.025;
      for (var cl = 0; cl < 5; cl += 1) {
        var cw = Math.round(W * (0.05 + (cl % 2) * 0.03));
        var cx2 = ((cl * 211 + 70 + cloudDrift) % (W + cw * 2)) - cw;
        var cy2 = Math.round(skyH * (0.16 + (cl % 3) * 0.13));
        fillOval(ctx, cx2, cy2, cw, Math.max(2, Math.round(skyH * 0.05)), pal.sun);
        fillOval(ctx, cx2 + cw * 0.55, cy2 + 1, cw * 0.6, Math.max(2, Math.round(skyH * 0.038)), pal.sun);
      }
      ctx.globalAlpha = 1;
    }
    if (heavy) {
      // 阴雨 / 雾天：低垂的灰云压住天空，颜色用钢灰而不是暖沙
      ctx.globalAlpha = S.night ? 0.30 : 0.24;
      ctx.fillStyle = S.night ? pal.steelDark : pal.concreteLight;
      var clouds = kind === "fog" ? 5 : 8;
      for (var hc = 0; hc < clouds; hc += 1) {
        var hx = (hc * 179 + 40) % W;
        var hy = Math.round(skyH * (0.10 + (hc % 4) * 0.085));
        var hw = Math.round(W * (0.07 + (hc % 3) * 0.035));
        fillOval(ctx, hx, hy, hw, Math.max(3, Math.round(skyH * 0.085)), ctx.fillStyle);
        fillOval(ctx, hx + hw * 0.5, hy - 2, hw * 0.62,
                 Math.max(2, Math.round(skyH * 0.06)), ctx.fillStyle);
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
    var hazeAlpha = kind === "fog"
      ? (S.night ? 0.34 : 0.42)
      : (S.night ? 0.16 : 0.22) * (heavy ? 1.3 : 1);
    ctx.globalAlpha = hazeAlpha;
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
    var pl = (S.world && S.world.plateau) || { cx: 0, cy: 0, hw: 260, hh: 125 };
    var c = viewPoint(pl.cx, pl.cy);
    var hw = Math.max(120, Math.round(pl.hw * z));
    var hh = Math.max(60, Math.round(pl.hh * z));
    fillDiamond(ctx, c.x, c.y, hw, hh, pal.ground);
    // 园区边界与外围道路处于同一地面，不画侧壁或下沉投影。
    var rim = [
      { x: c.x - hw, y: c.y }, { x: c.x, y: c.y - hh },
      { x: c.x + hw, y: c.y }, { x: c.x, y: c.y + hh },
      { x: c.x - hw, y: c.y },
    ];
    linePath(ctx, rim, 1, pal.stoneLight);
    // 干道贴着台地前缘两条边，两端再各自顺走向延长出去——出图的方向感
    var armL = { x: pl.cx - pl.hw - 14, y: pl.cy };
    var armR = { x: pl.cx + pl.hw + 14, y: pl.cy };
    var face = { x: pl.cx, y: pl.cy + pl.hh + 8 };
    function extendRoad(from, toward, len) {
      var dx = toward.x - from.x, dy = toward.y - from.y;
      var l = Math.hypot(dx, dy) || 1;
      return { x: from.x - dx / l * len, y: from.y - dy / l * len };
    }
    var road = [extendRoad(armL, face, 150), armL, face, armR, extendRoad(armR, face, 150)]
      .map(function (p) { return viewPoint(p.x, p.y); });
    linePath(ctx, road, vlen(10), pal.road);
    linePath(ctx, road, 1, pal.roadLight);
  }

  function drawGroundPatches(ctx, pal) {
    var site = S.world && S.world.site;
    if (!site) return;
    var z = S.cam.zoom;
    var pl = (S.world && S.world.plateau) || { cx: 0, cy: 0, hw: 260, hh: 125 };
    // 沙地风纹：几道很淡的横向长弧，压住大面积平涂
    var wr = seeded(0x6C0FFEE);
    ctx.globalAlpha = 0.06;
    for (var w = 0; w < 7; w += 1) {
      var wx = pl.cx + (wr() * 2 - 1) * pl.hw * 0.66;
      var wy = pl.cy + (wr() * 2 - 1) * pl.hh * 0.62;
      fillOval(ctx, viewPoint(wx, wy).x, viewPoint(wx, wy).y,
               Math.max(6, Math.round(pl.hw * (0.10 + wr() * 0.16) * z)),
               Math.max(1, Math.round(2 * z)), pal.groundHi);
    }
    ctx.globalAlpha = 0.16;
    site.patches.forEach(function (b) {
      var p = viewPoint(b.x, b.y);
      fillOval(ctx, p.x, p.y, Math.max(2, Math.round(b.rx * z)),
               Math.max(1, Math.round(b.ry * z)),
               b.light ? pal.groundHi : pal.groundDark);
    });
    // 砾石点：撒在台面范围内，近处更密
    var grit = seeded(0x5A17C0);
    for (var g = 0; g < 130; g += 1) {
      var gx = pl.cx + (grit() * 2 - 1) * pl.hw * 0.94;
      var gy = pl.cy + (grit() * 2 - 1) * pl.hh * 0.94;
      if (Math.abs(gx - pl.cx) / pl.hw + Math.abs(gy - pl.cy) / pl.hh > 0.98) continue;
      var gp = viewPoint(gx, gy);
      ctx.globalAlpha = 0.12 + grit() * 0.16;
      ctx.fillStyle = grit() < 0.55 ? pal.groundDark : pal.groundHi;
      ctx.fillRect(gp.x, gp.y, 1, 1);
    }
    ctx.globalAlpha = 1;
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
      if (m.kind === "box" || m.kind === "furnace" || m.kind === "distillation") {
        var hw = m.hw * z, hh = m.hh * z;
        var off = Math.min(20, (m.h || 10) * z * 0.22);
        fillDiamond(ctx, p.x + off * 0.7, p.y + off * 0.45, hw * 1.02, hh * 1.02, pal.shadow);
      } else if (m.kind === "cyl") {
        var rx = m.r * z, ry = Math.max(1, rx * 0.55);
        var off2 = Math.min(22, m.h * z * 0.2);
        fillOval(ctx, p.x + off2 * 0.7, p.y + off2 * 0.45, rx * 1.1, ry * 1.1, pal.shadow);
      } else if (m.kind === "cooling") {
        fillOval(ctx, p.x + vlen(9), p.y + vlen(4), vlen(m.r * 1.1), vlen(m.r * 0.45), pal.shadow);
      } else if (m.kind === "coalShed" || m.kind === "loadingRack") {
        if (m.axis) {
          // 煤棚是轴系零件：影子跟着转到「沿公路 / 纵深」两条轴上
          var shedFrame = yardFrame(m, p.x + vlen(6), p.y + vlen(3), z);
          fillPoly(ctx, [shedFrame(-m.hw * 1.02, -m.hh * 1.02), shedFrame(m.hw * 1.02, -m.hh * 1.02),
                         shedFrame(m.hw * 1.02, m.hh * 1.02), shedFrame(-m.hw * 1.02, m.hh * 1.02)],
                   pal.shadow);
        } else {
          fillDiamond(ctx, p.x + vlen(6), p.y + vlen(3), m.hw * z * 1.02, m.hh * z * 1.02, pal.shadow);
        }
      } else if (m.kind === "siloBattery") {
        // 一仓一影：筒仓群是并排几只仓，用整块影子会糊成一坨（仓列沿公路方向）
        var siloCount = Math.max(1, Math.round(m.count || 3));
        var siloFrame = yardFrame(m, p.x + vlen(7), p.y + vlen(3), z);
        for (var sk = 0; sk < siloCount; sk += 1) {
          var siloPt = siloFrame((sk - (siloCount - 1) / 2) * (m.gapX || 16), 0);
          fillOval(ctx, siloPt.x, siloPt.y, vlen(m.r * 1.12), vlen(m.r * 0.56), pal.shadow);
        }
      } else if (m.kind === "gasholder") {
        fillOval(ctx, p.x + vlen(8), p.y + vlen(4), vlen(m.r * 1.14), vlen(m.r * 0.57), pal.shadow);
      } else if (m.kind === "flare") {
        fillOval(ctx, p.x + vlen(3), p.y + vlen(2), vlen(m.hw || 7), vlen(m.hh || 3.5), pal.shadow);
      } else if (m.kind === "coalPile") {
        fillOval(ctx, p.x + vlen(3), p.y + vlen(2), vlen(m.hw * 1.05), vlen(m.hh * 0.7), pal.shadow);
      } else if (m.kind === "weighbridge") {
        fillDiamond(ctx, p.x + vlen(2), p.y + vlen(1), m.hw * z * 1.02, m.hh * z * 1.02, pal.shadow);
      } else if (m.kind === "floodlight") {
        fillOval(ctx, p.x + vlen(2), p.y + vlen(1), vlen(5), vlen(2.5), pal.shadow);
      } else if (m.kind === "gantry") {
        fillOval(ctx, p.x + vlen(2), p.y + vlen(1), vlen(m.w || 8), vlen(3), pal.shadow);
      } else if (m.path) {
        var pts = m.path.map(function (q) { return viewPoint(q.x, q.y); });
        linePath(ctx, pts, vlen(m.width || 6), pal.shadow);
      }
    });
    ctx.globalAlpha = 1;
  }

  /** 厂区级异常的地面层：场院外缘一圈脉冲光晕。

      画在模型之前（和光池同一层），所以只会从场坪边缘透出来，不会盖住机台
      与建筑；停机（root_ok=false）画成静止的红环 + 斜向封锁带，不闪。
      脉冲只改透明度、不改几何：reduced-motion 下自然退化为静止描边。 */
  function drawAlertGround(ctx, pal) {
    if (!S.world) return;
    var z = S.cam.zoom;
    S.world.factories.forEach(function (f) {
      var a = f.alert || { level: 0 };
      if (!a.level) return;
      var c = viewPoint(f.x, f.y);
      var hw = Math.max(40, Math.round(f.halfW * z * 0.94));
      var hh = Math.max(20, Math.round(f.halfH * z * 0.94));
      var color = a.level === 1 ? pal.warn : pal.bad;
      var pts = ringPts(c.x, c.y, hw, hh);
      if (a.level === 4) {
        // 停工：两道静止的环，像拉了封锁带，不随时间闪（它不是「正在出事」）
        ctx.globalAlpha = 0.34;
        linePath(ctx, pts, Math.max(1, Math.round(2 * z)), color);
        ctx.globalAlpha = 0.16;
        linePath(ctx, ringPts(c.x, c.y, hw * 0.88, hh * 0.88),
                 Math.max(1, Math.round(2 * z)), color);
        ctx.globalAlpha = 1;
        return;
      }
      // 脉冲：4 秒一轮；等级越高基线越亮，让严重的那座自己跳出来
      var base = a.level >= 3 ? 0.30 : (a.level === 2 ? 0.20 : 0.13);
      var pulse = S.reduced ? 0 : Math.sin(S.ticks * 0.16) * (a.level >= 3 ? 0.18 : 0.08);
      ctx.globalAlpha = clamp(base + pulse, 0.05, 0.55);
      linePath(ctx, pts, Math.max(1, Math.round(2 * z)), color);
      // 场坪内侧再压一层很淡的填充，远看是一整块警示色而不是一条细线
      ctx.globalAlpha = clamp(base * 0.35 + pulse * 0.3, 0.02, 0.2);
      fillDiamond(ctx, c.x, c.y, hw, hh, color);
      ctx.globalAlpha = 1;
    });
  }

  /** 厂区级的警报灯柱：立在场院后沿，高出厂房，全景下也能一眼看见。

      地面光晕解决「哪一块区域有问题」，灯柱解决「问题在哪座厂区」——等距俯视
      下光晕会被厂房压住，而竖直的柱子不会被任何前景挡住（它画在模型之后）。
      这是纯装饰层：不参与命中测试，交互仍走厂区名牌与左面板。 */
  function drawAlertBeacon(ctx, pal) {
    if (!S.world) return;
    var z = S.cam.zoom;
    S.world.factories.forEach(function (f) {
      var a = f.alert || { level: 0 };
      if (!a.level) return;
      var c = viewPoint(f.x, f.y);
      var groundY = c.y - Math.round(f.halfH * z * 0.72);
      var mastH = Math.max(14, Math.round(34 * z));
      var color = a.level === 1 ? pal.warn : pal.bad;
      var x = Math.round(c.x + f.halfW * z * 0.5);
      // 桅杆
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(x, groundY - mastH, Math.max(1, Math.round(z)), mastH);
      // 灯头：等级 3 画两颗，等级 4 画成熄灭的暗红（停工不是「正在发生」）
      var lampR = Math.max(2, Math.round(3 * z));
      var on = a.level !== 4;
      var blink = on && !S.reduced ? (Math.sin(S.ticks * 0.22) > -0.2 ? 1 : 0.35) : 1;
      ctx.globalAlpha = a.level === 4 ? 0.5 : blink;
      fillCircle(ctx, x, groundY - mastH - lampR, lampR, color);
      if (on) {
        // 光晕：夜里更亮，白天靠对比度也读得出来
        var halo = Math.max(5, Math.round((S.night ? 12 : 9) * z));
        ctx.globalAlpha = (S.night ? 0.26 : 0.16) * blink;
        fillCircle(ctx, x, groundY - mastH - lampR, halo, color);
      }
      if (a.level >= 3) {
        var r2 = Math.max(1, Math.round(2 * z));
        ctx.globalAlpha = a.level === 4 ? 0.5 : blink;
        fillCircle(ctx, x, groundY - mastH - lampR * 2 - r2, r2, color);
      }
      ctx.globalAlpha = 1;
    });
  }

  /** 夜里的地面光池：先于模型画在地面上，灯光才不会盖住建筑。 */
  function drawLightPools(ctx, pal) {
    if (!S.night) return;
    var lightIndex = 0;
    S.world.models.forEach(function (m) {
      var flicker = S.reduced ? 1 : 0.94 + 0.06 * Math.sin(S.ticks * 0.075 + lightIndex * 1.7);
      lightIndex += 1;
      if (m.bg) return;
      var p = viewPoint(m.x, m.y);
      var baseY = p.y - Math.round((m.z0 || 0) * S.cam.zoom);
      if (m.kind === "lamp") {
        var arm = Math.max(3, Math.round(4 * S.cam.zoom));
        ctx.globalAlpha = 0.30 * flicker;
        fillOval(ctx, p.x - arm, baseY + vlen(1), vlen(13), vlen(5.5), pal.glow);
        ctx.globalAlpha = 0.42 * flicker;
        fillOval(ctx, p.x - arm, baseY + vlen(1), vlen(8), vlen(3.5), pal.glow);
      } else if (m.kind === "floodlight") {
        ctx.globalAlpha = 0.34 * flicker;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(24), vlen(10), pal.glowCool);
        ctx.globalAlpha = 0.48;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(15), vlen(6), pal.glowCool);
        ctx.globalAlpha = 0.62;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(8), vlen(3.4), pal.glowCool);
      } else if (m.kind === "flare") {
        ctx.globalAlpha = 0.35;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(10), vlen(4.5), pal.glow);
      } else if (m.kind === "loadingRack") {
        ctx.globalAlpha = 0.28;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(m.hw * 1.1), vlen(m.hh * 0.8), pal.glow);
      } else if (m.kind === "box" && m.door && !m.bg) {
        // 厂房门口的暖光落在场坪上
        var dhw = m.hw * S.cam.zoom, dhh = m.hh * S.cam.zoom;
        var dcx = p.x - dhw * 0.5, dcy = baseY + dhh * 0.5;
        ctx.globalAlpha = 0.18;
        fillOval(ctx, dcx, dcy, Math.max(3, dhw * 0.34), Math.max(2, dhh * 0.30), pal.glow);
        ctx.globalAlpha = 0.28;
        fillOval(ctx, dcx, dcy, Math.max(2, dhw * 0.20), Math.max(1, dhh * 0.17), pal.glow);
      }
    });
    ctx.globalAlpha = 1;
  }

  function sceneDrawItems(models) {
    var items = [];
    models.forEach(function (m) {
      if (m.kind === "conveyor") {
        var total = pathLength(m.path), acc = 0;
        for (var i = 1; i < m.path.length; i += 1) {
          var a = m.path[i - 1], b = m.path[i];
          var len = Math.hypot(b.x - a.x, b.y - a.y);
          var count = Math.max(1, Math.ceil(len / 14));
          for (var j = 0; j < count; j += 1) {
            var t0 = j / count, t1 = (j + 1) / count;
            var piece = Object.assign({}, m, {
              path: [{ x: a.x + (b.x - a.x) * t0, y: a.y + (b.y - a.y) * t0 },
                     { x: a.x + (b.x - a.x) * t1, y: a.y + (b.y - a.y) * t1 }],
              h0: m.h0 + (m.h1 - m.h0) * (total ? (acc + len * t0) / total : 0),
              h1: m.h0 + (m.h1 - m.h0) * (total ? (acc + len * t1) / total : 0),
              flowStart: acc + len * t0,
              flowEnd: acc + len * t1,
              flowTotal: total,
              support: m.support !== false && j % 2 === 0,
            });
            items.push(piece);
          }
          acc += len;
        }
      } else items.push(m);
    });
    return items.map(function (m, index) {
      var mid = m.path ? pointAt(m.path, 0.5) : m;
      return { m: m, d: mid.y, index: index };
    }).sort(function (a, b) {
      var layerA = drawLayer(a.m);
      var layerB = drawLayer(b.m);
      return layerA - layerB || a.d - b.d || (a.m.z0 || 0) - (b.m.z0 || 0) || a.index - b.index;
    });
  }

  /** 分层：铺装永远在最下，远景 bg 在中，立体件按 y 排在上面。 */
  function drawLayer(m) {
    if (m.paving) return 0;
    return m.bg ? 1 : 2;
  }

  function drawModels(ctx, pal) {
    var items = sceneDrawItems(S.world.models);
    var hasBg = items.some(function (it) { return it.m.bg; });
    var hazed = !hasBg;
    items.forEach(function (it) {
      if (!hazed && !it.m.bg) {
        // 远景画完、主体之前压一层薄雾：背景带整体退后，仍保留轮廓
        var W = S.bufW, H = S.bufH;
        var horizon = sceneHorizon();
        ctx.fillStyle = pal.bgHaze;
        // 阶梯式薄雾（不用渐变：假 Canvas / 老 WebView 都只认纯色填充）
        for (var hb = 0; hb < 6; hb += 1) {
          ctx.globalAlpha = (S.night ? 0.10 : 0.14) * (1 - hb / 6);
          ctx.fillRect(0, horizon - 18 + hb * Math.max(3, Math.round(H * 0.06)),
                       W, Math.max(3, Math.round(H * 0.06)) + 1);
        }
        ctx.globalAlpha = S.night ? 0.05 : 0.07;
        ctx.fillRect(0, 0, W, H);
        ctx.globalAlpha = 1;
        hazed = true;
      }
      drawModel(ctx, pal, it.m);
    });
  }

  function sawRoofFaces(x, y, hw, hh, rise) {
    function pt(u, v, lift) {
      return { x: x + (u - v) * hw, y: y + (u + v - 1) * hh - (lift || 0) };
    }
    var faces = [];
    for (var i = 0; i < 3; i += 1) {
      var a = i / 3, b = (i + 1) / 3;
      faces.push({ side: "slope", points: [pt(a, 0), pt(a, 1), pt(b, 1, rise), pt(b, 0, rise)] });
      faces.push({ side: "gable", points: [pt(a, 1), pt(b, 1), pt(b, 1, rise)] });
      faces.push({ side: "glazing", points: [pt(b, 0), pt(b, 0, rise), pt(b, 1, rise), pt(b, 1)] });
    }
    return faces;
  }

  function drawModel(ctx, pal, m) {
    if (m.kind === "box") drawBoxModel(ctx, pal, m);
    else if (m.kind === "cyl") drawCylinderModel(ctx, pal, m);
    else if (m.kind === "furnace") drawFurnaceModel(ctx, pal, m);
    else if (m.kind === "distillation") drawDistillationModel(ctx, pal, m);
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
    else if (m.kind === "fence") drawFenceModel(ctx, pal, m);
    else if (m.kind === "coalShed") drawCoalShedModel(ctx, pal, m);
    else if (m.kind === "siloBattery") drawSiloBatteryModel(ctx, pal, m);
    else if (m.kind === "gasholder") drawGasholderModel(ctx, pal, m);
    else if (m.kind === "basin") drawBasinModel(ctx, pal, m);
    else if (m.kind === "loadingRack") drawLoadingRackModel(ctx, pal, m);
    else if (m.kind === "flare") drawFlareModel(ctx, pal, m);
    else if (m.kind === "coalPile") drawCoalPileModel(ctx, pal, m);
    else if (m.kind === "weighbridge") drawWeighbridgeModel(ctx, pal, m);
    else if (m.kind === "floodlight") drawFloodlightModel(ctx, pal, m);
    else if (m.kind === "lamp") drawLampModel(ctx, pal, m);
    else if (m.kind === "reel") drawReelModel(ctx, pal, m);
    else if (m.kind === "flag") drawFlagModel(ctx, pal, m);
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
    if (S.night && !m.bg) {
      ctx.globalAlpha = 0.12;
      fillOval(ctx, p.x + Math.round(L * 0.66), baseY + 1,
               Math.max(3, Math.round(7 * z)), Math.max(1, Math.round(2.5 * z)), pal.window);
      ctx.globalAlpha = 1;
    }
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
    if (S.night && !m.bg) {
      ctx.fillStyle = pal.window;
      ctx.fillRect(Math.round(p.x + L * 0.5), Math.round(baseY - 4 * z),
                   Math.max(1, Math.round(2 * z)), 1);
      ctx.fillStyle = pal.bad;
      ctx.fillRect(Math.round(p.x - L * 0.44), Math.round(baseY - 4 * z),
                   Math.max(1, Math.round(1.5 * z)), 1);
    }
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
    var top = cols.top, left = cols.left, right = cols.right;
    var frame = m.bg ? pal.bgSteelDark : pal.panelLine;

    if (m.paving) {
      // 场院坪保留整洁亮面，仅用边缘表现厚度。
      drawIsoBoxAt(ctx, base.x, baseY, hw, hh, h, top, left, right);
      var ty = baseY - h;
      // 坪缘石：上缘亮线、下缘暗线，平台不再是一片平涂
      linePath(ctx, [{ x: base.x - hw, y: ty }, { x: base.x, y: ty + hh },
                    { x: base.x + hw, y: ty }], Math.max(1, Math.round(z)), pal.mark);
      linePath(ctx, [{ x: base.x - hw, y: ty + 1 }, { x: base.x, y: ty + hh + 1 },
                    { x: base.x + hw, y: ty + 1 }], Math.max(1, Math.round(z)), pal.groundDark);
      if (m.curb) {
        var postH = Math.max(2, Math.round(4 * z));
        [[base.x - hw, ty], [base.x, ty + hh], [base.x + hw, ty]].forEach(function (cp) {
          ctx.fillStyle = pal.paint;
          ctx.fillRect(Math.round(cp[0]), Math.round(cp[1] - postH), 1, postH);
          ctx.fillStyle = pal.paintDark;
          ctx.fillRect(Math.round(cp[0]) - 1, Math.round(cp[1]) - 1, 3, 1);
        });
      }
      // 坪面污渍（确定性散布，不随帧变化）
      var rand = seeded(hashKey("pave" + m.x + ":" + m.y + ":" + hw));
      for (var st = 0; st < 5; st += 1) {
        var sx = base.x + (rand() * 2 - 1) * hw * 0.62;
        var sy = ty + (rand() * 2 - 1) * hh * 0.56;
        ctx.globalAlpha = 0.10 + rand() * 0.06;
        fillOval(ctx, sx, sy, Math.max(2, hw * (0.05 + rand() * 0.08)),
                 Math.max(1, hh * (0.03 + rand() * 0.05)), pal.stain);
      }
      ctx.globalAlpha = 1;
      drawYardMarkings(ctx, pal, m, base.x, baseY - h, hw, hh);
      // 围栏：前缘高栏 + 后缘低栏，围出完整的场院边界
      if (m.rail) {
        var ph = Math.max(2, Math.round(4 * z));
        for (var ri = 0; ri <= 8; ri += 1) {
          var rt = ri / 8;
          ctx.fillStyle = pal.steelDark;
          ctx.fillRect(Math.round(base.x - hw + hw * rt),
                       Math.round(ty + hh * rt - ph), 1, ph);
          ctx.fillRect(Math.round(base.x + hw - hw * rt),
                       Math.round(ty + hh * rt - ph), 1, ph);
        }
        linePath(ctx, [{ x: base.x - hw, y: ty }, { x: base.x, y: ty + hh },
                      { x: base.x + hw, y: ty }], 1, pal.steelLight);
        // 后缘：矮护栏，压住平台后侧，不挡建筑
        ctx.globalAlpha = 0.65;
        var bh = Math.max(1, Math.round(2 * z));
        for (var bi = 0; bi <= 8; bi += 1) {
          var bt = bi / 8;
          ctx.fillStyle = pal.steelDark;
          ctx.fillRect(Math.round(base.x - hw + hw * bt),
                       Math.round(ty - hh + hh * bt - bh), 1, bh);
          ctx.fillRect(Math.round(base.x + hw - hw * bt),
                       Math.round(ty - hh + hh * bt - bh), 1, bh);
        }
        linePath(ctx, [{ x: base.x - hw, y: ty }, { x: base.x, y: ty - hh },
                      { x: base.x + hw, y: ty }], 1, pal.steel);
        ctx.globalAlpha = 1;
      }
      return;
    }

    drawIsoBoxAt(ctx, base.x, baseY, hw, hh, h, top, left, right);
    // 勒脚：墙根压暗一档，接缝处补亮线，建筑稳稳落在地坪上
    var plinth = Math.max(2, Math.round(Math.min(h * 0.14, 5 * z)));
    fillPoly(ctx, [
      { x: base.x - hw, y: baseY - plinth }, { x: base.x, y: baseY + hh - plinth },
      { x: base.x, y: baseY + hh }, { x: base.x - hw, y: baseY },
    ], m.bg ? pal.bgSteelDark : (m.role === "rust" ? pal.rustDark : m.role === "glass" ? pal.glassDark : pal.steelDark));
    fillPoly(ctx, [
      { x: base.x, y: baseY + hh - plinth }, { x: base.x + hw, y: baseY - plinth },
      { x: base.x + hw, y: baseY }, { x: base.x, y: baseY + hh },
    ], m.bg ? pal.bgSteelDark : (m.role === "rust" ? pal.rustDark : m.role === "glass" ? pal.glassDark : pal.steelDark));
    linePath(ctx, [{ x: base.x - hw, y: baseY - plinth }, { x: base.x, y: baseY + hh - plinth },
                  { x: base.x + hw, y: baseY - plinth }], 1, top);

    // 墙面分缝：竖缝按面板宽度铺，横缝两道腰线
    if (h >= 10) {
      var step = m.bg ? 0.22 : (hw > 26 ? 0.09 : 0.14);
      var seamCol = m.bg ? pal.bgSteel : (m.role === "rust" ? pal.rustDark : pal.steelDark);
      for (var u = step; u < 0.96; u += step) {
        linePath(ctx, [
          { x: base.x - hw + hw * u, y: baseY - h + hh * u },
          { x: base.x - hw + hw * u, y: baseY + hh * u },
        ], 1, seamCol);
        linePath(ctx, [
          { x: base.x + hw * u, y: baseY - h + hh * (1 - u) },
          { x: base.x + hw * u, y: baseY + hh * (1 - u) },
        ], 1, m.bg ? pal.bgSteelDark : pal.steelMid);
      }
      ctx.globalAlpha = 0.55;
      [0.34, 0.64].forEach(function (vv) {
        linePath(ctx, [
          { x: base.x - hw, y: baseY - h + h * vv },
          { x: base.x, y: baseY - h + hh + h * vv },
          { x: base.x + hw, y: baseY - h + h * vv },
        ], 1, seamCol);
      });
      ctx.globalAlpha = 1;
      // 檐口：顶边一条亮线，把屋顶和墙面分开
      linePath(ctx, [{ x: base.x - hw, y: baseY - h }, { x: base.x, y: baseY - h + hh },
                    { x: base.x + hw, y: baseY - h }], Math.max(1, Math.round(z)),
                m.bg ? pal.bgSteelLight : pal.roofSlateLight);
    }

    // 墙面窗体：左面两排、右面一排；白天是冷灰玻璃，夜里亮暖窗
    if (m.windows && h >= 12) {
      var rows = h >= 40 ? 3 : h >= 26 ? 2 : 1;
      var n = Math.max(1, m.windows);
      var glassDay = m.bg ? pal.bgSteelDark : (m.role === "rust" ? pal.rustDark : pal.glass);
      var glassLit = m.bg ? pal.bgSteelLight : pal.window;
      function facePt(side, uu, vv) {
        if (side === 0) {
          return { x: base.x - hw + hw * uu, y: baseY - h + hh * uu + h * vv };
        }
        return { x: base.x + hw * uu, y: baseY - h + hh * (1 - uu) + h * vv };
      }
      function drawWin(side, u0, v0, du, dv) {
        fillPoly(ctx, [
          facePt(side, u0, v0), facePt(side, u0 + du, v0),
          facePt(side, u0 + du, v0 + dv), facePt(side, u0, v0 + dv),
        ], frame);
        fillPoly(ctx, [
          facePt(side, u0 + 0.012, v0 + 0.02), facePt(side, u0 + du - 0.012, v0 + 0.02),
          facePt(side, u0 + du - 0.012, v0 + dv - 0.02), facePt(side, u0 + 0.012, v0 + dv - 0.02),
        ], S.night ? glassLit : glassDay);
        // 玻璃上沿反光
        linePath(ctx, [facePt(side, u0 + 0.02, v0 + dv * 0.22),
                      facePt(side, u0 + du - 0.02, v0 + dv * 0.22)], 1,
                 S.night ? pal.window : pal.glassLight);
        // 窗台与中挺
        linePath(ctx, [facePt(side, u0, v0 + dv), facePt(side, u0 + du, v0 + dv)],
                 1, m.bg ? pal.bgSteelLight : pal.steelLight);
        linePath(ctx, [facePt(side, u0 + du / 2, v0), facePt(side, u0 + du / 2, v0 + dv)],
                 1, frame);
        if (S.night && !m.bg) {
          var wc = facePt(side, u0 + du / 2, v0 + dv / 2);
          ctx.globalAlpha = 0.20;
          fillOval(ctx, wc.x, wc.y, Math.max(3, hw * 0.20), Math.max(2, hw * 0.13), glassLit);
          ctx.globalAlpha = 1;
        }
      }
      for (var row = 0; row < rows; row += 1) {
        var v0 = rows === 1 ? 0.40 : rows === 2 ? (row === 0 ? 0.24 : 0.56) : (0.16 + row * 0.24);
        var dv = rows === 1 ? 0.20 : rows === 2 ? 0.16 : 0.12;
        for (var wi = 0; wi < n; wi += 1) {
          var u0 = n === 1 ? 0.44 : 0.14 + wi * (0.68 / (n - 1));
          drawWin(0, u0, v0, Math.min(0.12, 0.42 / n), dv);
        }
      }
      // 右面一列窗，数量减半，避免两面都花
      var rn = Math.max(1, Math.round(n / 2));
      for (var rj = 0; rj < rn; rj += 1) {
        var ru = rn === 1 ? 0.52 : 0.30 + rj * (0.40 / (rn - 1));
        drawWin(1, ru, 0.38, Math.min(0.10, 0.30 / rn), 0.18);
      }
    }

    // 大门 + 雨棚 + 门灯
    if (m.door) {
      var dpt = function (uu, vv) {
        return {
          x: base.x - hw + hw * uu,
          y: baseY - h + hh * uu + h * vv,
        };
      };
      fillPoly(ctx, [
        dpt(0.40, 0.58), dpt(0.60, 0.58), dpt(0.60, 1), dpt(0.40, 1),
      ], pal.ink);
      fillPoly(ctx, [
        dpt(0.415, 0.60), dpt(0.585, 0.60), dpt(0.585, 0.985), dpt(0.415, 0.985),
      ], pal.steelDark);
      // 卷帘门横条
      for (var sl = 0.64; sl < 0.97; sl += 0.055) {
        linePath(ctx, [dpt(0.42, sl), dpt(0.58, sl)], 1, pal.steelMid);
      }
      ctx.fillStyle = pal.paint;
      ctx.fillRect(Math.round(base.x - hw * 0.28), Math.round(baseY - h + h * 0.56 - 1),
                   Math.max(2, Math.round(hw * 0.12)), 1);
      // 雨棚（薄板挑出）
      fillPoly(ctx, [
        { x: base.x - hw * 0.34, y: baseY - h * 0.995 + hh * 0.34 - hh * 0.22 },
        { x: base.x - hw * 0.06, y: baseY - h * 0.995 + hh * 0.06 + hh * 0.42 },
        { x: base.x - hw * 0.06, y: baseY - h * 0.995 + hh * 0.06 + hh * 0.50 },
        { x: base.x - hw * 0.34, y: baseY - h * 0.995 + hh * 0.34 - hh * 0.14 },
      ], pal.roofSlateLight);
      // 门槛压条 + 门灯（夜里亮一盏暖光）
      linePath(ctx, [dpt(0.38, 0.985), dpt(0.62, 0.985)], 1, pal.concreteLight);
      if (S.night && !m.bg) {
        var dl = dpt(0.50, 0.615);
        ctx.fillStyle = pal.window;
        ctx.fillRect(Math.round(dl.x), Math.round(dl.y), 2, 1);
        ctx.globalAlpha = 0.22;
        fillOval(ctx, dl.x, dl.y, Math.max(3, Math.round(hw * 0.10)),
                 Math.max(2, Math.round(hw * 0.06)), pal.window);
        ctx.globalAlpha = 1;
      }
    }

    // 屋顶：锯齿 / 平顶 + 屋顶设备
    if (m.roof === "saw") {
      var roof = sawRoofFaces(base.x, baseY - h, hw, hh, Math.max(2, vlen(5)));
      roof.forEach(function (face) {
        var fc = face.side === "slope" ? (m.bg ? pal.bgSteelLight : pal.roofSlateLight)
          : face.side === "gable" ? (m.bg ? pal.bgSteel : pal.roofSlate)
            : (m.bg ? pal.bgSteelDark : (S.night ? pal.glassDark : pal.glass));
        fillPoly(ctx, face.points, fc);
        if (face.side === "slope") {
          // 压脊线与檐口线，再补两道檩条，坡面不再是一块平色
          linePath(ctx, [face.points[3], face.points[2]], 1,
                   m.bg ? pal.bgSteelLight : pal.roofSlateLight);
          linePath(ctx, [face.points[0], face.points[1]], 1,
                   m.bg ? pal.bgSteelDark : pal.roofSlateDark);
          ctx.globalAlpha = m.bg ? 0.32 : 0.30;
          for (var pr = 1; pr <= 2; pr += 1) {
            var pt = pr / 3;
            linePath(ctx, [
              { x: face.points[0].x + (face.points[3].x - face.points[0].x) * pt,
                y: face.points[0].y + (face.points[3].y - face.points[0].y) * pt },
              { x: face.points[1].x + (face.points[2].x - face.points[1].x) * pt,
                y: face.points[1].y + (face.points[2].y - face.points[1].y) * pt },
            ], 1, m.bg ? pal.bgSteelDark : pal.roofSlateDark);
          }
          ctx.globalAlpha = 1;
        } else if (face.side === "gable") {
          linePath(ctx, face.points.concat([face.points[0]]), 1,
                   m.bg ? pal.bgSteelDark : pal.roofSlateDark);
        }
        if (face.side === "glazing") {
          var mid = { x: (face.points[0].x + face.points[2].x) / 2,
                      y: (face.points[0].y + face.points[2].y) / 2 };
          ctx.fillStyle = S.night ? pal.window : pal.glassGlow;
          ctx.globalAlpha = S.night ? 0.28 : 0.35;
          ctx.fillRect(Math.round(mid.x) - 1, Math.round(mid.y) - 1, 2, 2);
          ctx.globalAlpha = 1;
        }
      });
    } else {
      // 平顶：檐口厚度 + 女儿墙 + 天窗 + 屋顶机组
      linePath(ctx, [{ x: base.x - hw, y: baseY - h }, { x: base.x, y: baseY - h - hh },
                    { x: base.x + hw, y: baseY - h }], Math.max(1, Math.round(z)),
                m.bg ? pal.bgSteelLight : pal.roofSlateLight);
      // 前檐压暗一档，内侧再补一条女儿墙亮线，顶板有厚度
      linePath(ctx, [{ x: base.x - hw, y: baseY - h + 1 },
                    { x: base.x, y: baseY - h + hh + 1 },
                    { x: base.x + hw, y: baseY - h + 1 }], 1,
                m.bg ? pal.bgSteelDark : pal.roofSlateDark);
      linePath(ctx, [{ x: base.x - hw + 1, y: baseY - h },
                    { x: base.x, y: baseY - h + hh - 1 },
                    { x: base.x + hw - 1, y: baseY - h }], 1,
                m.bg ? pal.bgSteelLight : pal.roofSlateLight);
      if (hw > 8) {
        var skx = base.x - hw * 0.28, sky = baseY - h + hh * 0.30;
        fillDiamond(ctx, skx, sky, Math.max(2, Math.round(hw * 0.18)),
                    Math.max(1, Math.round(hh * 0.16)), m.bg ? pal.bgSteelLight : pal.glassGlow);
        if (S.night && !m.bg) {
          ctx.globalAlpha = 0.16;
          fillOval(ctx, skx, sky, Math.max(3, Math.round(hw * 0.22)),
                   Math.max(2, Math.round(hh * 0.20)), pal.window);
          ctx.globalAlpha = 1;
        }
      }
      if (!m.bg && hw > 7) {
        drawIsoBoxAt(ctx, base.x + hw * 0.22, baseY - h + hh * 0.18 - hh * 0.20,
                     Math.max(2, Math.round(hw * 0.26)), Math.max(1, Math.round(hh * 0.26)),
                     Math.max(2, Math.round(3 * z)), pal.metalLight, pal.metal, pal.metalDark);
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(base.x - hw * 0.40), Math.round(baseY - h - hh * 0.30),
                     Math.max(2, Math.round(hw * 0.16)), 1);
      }
      if (!m.bg && hw > 16) {
        // 第二台屋顶机组 + 跨顶管线
        drawIsoBoxAt(ctx, base.x - hw * 0.34, baseY - h + hh * 0.50,
                     Math.max(2, Math.round(hw * 0.12)), Math.max(1, Math.round(hh * 0.16)),
                     Math.max(2, Math.round(2.4 * z)), pal.metal, pal.metalDark, pal.metalDark);
        linePath(ctx, [{ x: base.x + hw * 0.02, y: baseY - h + hh * 0.20 },
                      { x: base.x + hw * 0.58, y: baseY - h + hh * 0.58 }], 1, pal.metalDark);
        linePath(ctx, [{ x: base.x + hw * 0.02, y: baseY - h + hh * 0.20 - 1 },
                      { x: base.x + hw * 0.58, y: baseY - h + hh * 0.58 - 1 }], 1, pal.metalLight);
      }
      if (m.bg && hw > 12) {
        // 远景大屋面也别是一块平板：补一个检修口/通风帽
        drawIsoBoxAt(ctx, base.x - hw * 0.24, baseY - h + hh * 0.24,
                     Math.max(2, Math.round(hw * 0.12)), Math.max(1, Math.round(hh * 0.12)),
                     Math.max(1, Math.round(2 * z)), pal.bgSteelLight, pal.bgSteel, pal.bgSteelDark);
      }
    }
    // 屋顶通风器 / 排气帽
    if (m.vent && !m.bg) {
      var ventCount = hw > 14 ? 3 : 2;
      for (var vv = 0; vv < ventCount; vv += 1) {
        var vx = base.x - hw * 0.36 + vv * hw * 0.34;
        var vy = baseY - h - hh * 0.42 + vv * hh * 0.30;
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(vx), Math.round(vy - Math.max(2, 3 * z)),
                     Math.max(1, Math.round(2 * z)), Math.max(1, Math.round(3 * z)));
        ctx.fillStyle = pal.metalLight;
        ctx.fillRect(Math.round(vx), Math.round(vy - Math.max(2, 3 * z)), 2, 1);
      }
    }
    // 调度塔：控制室玻璃横带 + 顶冠竖梃
    if (m.tower) {
      var bandV0 = 0.27, bandV1 = 0.35;
      fillPoly(ctx, [
        { x: base.x - hw, y: baseY - h + h * bandV0 },
        { x: base.x, y: baseY - h + hh + h * bandV0 },
        { x: base.x, y: baseY - h + hh + h * bandV1 },
        { x: base.x - hw, y: baseY - h + h * bandV1 },
      ], pal.glassDark);
      linePath(ctx, [
        { x: base.x - hw, y: baseY - h + h * bandV0 },
        { x: base.x, y: baseY - h + hh + h * bandV0 },
      ], 1, pal.glassLight);
      for (var bw = 0; bw < 5; bw += 1) {
        var bu = 0.10 + bw * 0.19;
        ctx.fillStyle = S.night ? pal.window : pal.glassGlow;
        ctx.fillRect(Math.round(base.x - hw + hw * bu),
                     Math.round(baseY - h + h * bandV0 + hh * bu + h * 0.02), 2, 1);
        if (S.night) {
          ctx.globalAlpha = 0.18;
          fillOval(ctx, base.x - hw + hw * bu, baseY - h + h * bandV0 + hh * bu, 3, 2, pal.window);
          ctx.globalAlpha = 1;
        }
      }
    }
    if (m.crown) {
      for (var cu = 0.10; cu < 0.96; cu += 0.12) {
        linePath(ctx, [
          { x: base.x - hw + hw * cu, y: baseY - h + hh * cu },
          { x: base.x - hw + hw * cu, y: baseY + hh * cu },
        ], 1, pal.glassDark);
      }
      ctx.globalAlpha = S.night ? 0.30 : 0.20;
      fillDiamond(ctx, base.x, baseY - h, hw - 1, hh - 1, S.night ? pal.window : pal.glassGlow);
      ctx.globalAlpha = 1;
    }
    // 外露管线：沿左面下到勒脚
    if (m.pipes && !m.bg) {
      var pipeX = base.x - hw * 0.72;
      linePath(ctx, [{ x: pipeX, y: baseY - h * 0.96 }, { x: pipeX, y: baseY + hh * 0.28 }],
               1, pal.metalDark);
      linePath(ctx, [{ x: pipeX + 1, y: baseY - h * 0.96 }, { x: pipeX + 1, y: baseY + hh * 0.28 }],
               1, pal.metalLight);
      [0.20, 0.52, 0.84].forEach(function (pv) {
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(pipeX - 1), Math.round(baseY - h * pv + hh * 0.10), 3, 1);
      });
    }
    // 附房雨棚 / 站台雨棚：加一道浅色挑檐
    if (m.canopy) {
      linePath(ctx, [{ x: base.x - hw, y: baseY - h + 1 }, { x: base.x, y: baseY - h + hh + 1 },
                    { x: base.x + hw, y: baseY - h + 1 }], 1, pal.mark);
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
    // 落水管 + 检修爬梯：给所有盒体建筑补一层工业立面细节
    if (h >= 9) {
      var trimDark = m.bg ? pal.bgSteelDark : pal.metalDark;
      var trimLight = m.bg ? pal.bgSteelLight : pal.metalLight;
      ctx.globalAlpha = m.bg ? 0.45 : 0.8;
      (m.bg ? [0.05] : [0.05, 0.95]).forEach(function (u) {
        var px = base.x - hw + hw * u;
        var yTop = baseY - h + hh * u + Math.max(1, Math.round(2 * z));
        var yBot = baseY + hh * u - 1;
        linePath(ctx, [{ x: px, y: yTop }, { x: px, y: yBot }], 1, trimDark);
        linePath(ctx, [{ x: px + 1, y: yTop }, { x: px + 1, y: yBot - 1 }], 1, trimLight);
        ctx.fillStyle = trimDark;
        ctx.fillRect(Math.round(px), Math.round(yBot - 1), 2, 1);
      });
      ctx.globalAlpha = 1;
      if (!m.bg && h >= 16 && hw >= 7) {
        // 高厂房右立面靠后侧：检修爬梯（压在窗列之外）
        var u0 = 0.84;
        var lx = base.x + hw * u0;
        var ins = hh * (1 - u0);
        var lt = baseY - h + ins + Math.max(1, Math.round(h * 0.09));
        var lb = baseY + ins - Math.max(1, Math.round(h * 0.02));
        ctx.fillStyle = trimDark;
        ctx.fillRect(Math.round(lx), Math.round(lt), 1, Math.round(lb - lt));
        ctx.fillRect(Math.round(lx) + 2, Math.round(lt), 1, Math.round(lb - lt));
        for (var rr = lt + 3; rr < lb; rr += Math.max(3, Math.round(4 * z))) {
          ctx.fillRect(Math.round(lx), Math.round(rr), 3, 1);
        }
        ctx.fillStyle = trimLight;
        ctx.fillRect(Math.round(lx) - 1, Math.round(lt) - 1, 5, 1);
      }
    }
  }

  // 坪面标线：安全区黄虚线、通道双黄线、轮胎痕与角落碎石。
  // 在铺装模型画完之后调用，写字楼/机台等主体随后覆盖其上，天然落在「地面」层。
  function drawYardMarkings(ctx, pal, m, baseX, topY, hw, hh) {
    var z = S.cam.zoom;
    if (m.hallBase) {
      ctx.globalAlpha = 0.30;
      linePath(ctx, [
        { x: baseX - hw * 0.88, y: topY }, { x: baseX, y: topY + hh * 0.88 },
        { x: baseX + hw * 0.88, y: topY }, { x: baseX, y: topY - hh * 0.88 },
        { x: baseX - hw * 0.88, y: topY },
      ], 1, pal.mark);
      for (var q = 0; q < 4; q += 1) {
        var ang = Math.PI / 2 * q + Math.PI / 4;
        linePath(ctx, [
          { x: baseX + Math.cos(ang) * hw * 0.62, y: topY + Math.sin(ang) * hh * 0.62 },
          { x: baseX + Math.cos(ang) * hw * 0.84, y: topY + Math.sin(ang) * hh * 0.84 },
        ], 2, pal.paint);
      }
      ctx.globalAlpha = 1;
      return;
    }
    var f = m.yard;
    if (!f) return;
    function yPoint(wx, wy) {
      var p = viewPoint(wx, wy);
      p.y -= Math.round(5 * z);
      return p;
    }
    function dashed(a, b, dash) {
      var len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      var n = Math.max(1, Math.floor(len / Math.max(3, dash)));
      for (var i = 0; i < n; i += 1) {
        var t0 = i / n, t1 = Math.min(1, (i + 0.52) / n);
        linePath(ctx, [
          { x: a.x + (b.x - a.x) * t0, y: a.y + (b.y - a.y) * t0 },
          { x: a.x + (b.x - a.x) * t1, y: a.y + (b.y - a.y) * t1 },
        ], 1, pal.paint);
      }
    }
    // 机台安全区：黄虚线框（收在菱形坪的前场内切矩形里）
    var zx0 = f.x - f.halfW * 0.42, zx1 = f.x + f.halfW * 0.42;
    var zy0 = f.y + f.halfH * 0.06, zy1 = f.y + f.halfH * 0.52;
    var c1 = yPoint(zx0, zy0), c2 = yPoint(zx1, zy0);
    var c3 = yPoint(zx1, zy1), c4 = yPoint(zx0, zy1);
    ctx.globalAlpha = 0.28;
    dashed(c1, c2, Math.max(6, 8 * z));
    dashed(c2, c3, Math.max(6, 8 * z));
    dashed(c3, c4, Math.max(6, 8 * z));
    dashed(c4, c1, Math.max(6, 8 * z));
    // 入口 → 分配枢纽：通道边线
    var e = yPoint(f.entrance.x, f.entrance.y);
    var hub = yPoint(f.hub.x, f.hub.y);
    linePath(ctx, [{ x: e.x - 1, y: e.y }, { x: hub.x - 1, y: hub.y }], 1, pal.paint);
    linePath(ctx, [{ x: e.x + 1, y: e.y }, { x: hub.x + 1, y: hub.y }], 1, pal.paint);
    // 通往主厂房的轮胎痕
    var door = yPoint(f.x - f.halfW * 0.30, f.y + f.halfH * 0.06);
    ctx.globalAlpha = 0.13;
    linePath(ctx, [{ x: e.x - 2, y: e.y }, { x: door.x - 2, y: door.y }], 1, pal.ink);
    linePath(ctx, [{ x: e.x + 2, y: e.y }, { x: door.x + 2, y: door.y }], 1, pal.ink);
    // 地井盖与排水格栅
    [[-0.34, 0.36], [0.12, 0.60]].forEach(function (o) {
      var hp = yPoint(f.x + f.halfW * o[0], f.y + f.halfH * o[1]);
      fillOval(ctx, hp.x, hp.y, Math.max(2, Math.round(3 * z)),
               Math.max(1, Math.round(2 * z)), pal.metalDark);
      ctx.fillStyle = pal.metal;
      ctx.fillRect(Math.round(hp.x - 1), Math.round(hp.y), 2, 1);
    });
    var gr = yPoint(f.x + f.halfW * 0.42, f.y + f.halfH * 0.46);
    for (var gi = 0; gi < 3; gi += 1) {
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(Math.round(gr.x - 3), Math.round(gr.y + gi), Math.max(3, Math.round(6 * z)), 1);
    }
    // 入口斑马线：黄黑相间的短斜杠
    ctx.globalAlpha = 0.55;
    for (var hz = 0; hz < 3; hz += 1) {
      linePath(ctx, [
        { x: e.x - 4 + hz * 4, y: e.y + 3 },
        { x: e.x + hz * 4, y: e.y - 3 },
      ], 1, hz % 2 ? pal.ink : pal.paint);
    }
    // 角落碎石与杂草点
    var rand = seeded(hashKey("yard" + f.id));
    ctx.globalAlpha = 0.45;
    for (var k = 0; k < 16; k += 1) {
      var wx = f.x + (rand() * 2 - 1) * f.halfW * 0.86;
      var wy = f.y + (rand() * 2 - 1) * f.halfH * 0.86;
      var pp = yPoint(wx, wy);
      ctx.fillStyle = rand() < 0.5 ? pal.soil : pal.groundDark;
      ctx.fillRect(Math.round(pp.x), Math.round(pp.y), 1, 1);
    }
    ctx.globalAlpha = 1;
  }

  function drawFurnaceModel(ctx, pal, m) {
    var z = S.cam.zoom, p = viewPoint(m.x, m.y);
    var y = p.y - (m.z0 || 0) * z, w = m.hw * z, d = m.hh * z, h = m.h * z;
    var c = roleCols(pal, m.role, m.bg);
    function at(u, v, lift) { return { x: p.x + u * w, y: y + v * d - lift * h }; }
    drawIsoBoxAt(ctx, p.x, y, w, d, h * 0.10, pal.concreteLight, pal.concrete, pal.concreteDark);
    // 各段共用同一组截面顶点，收腰炉身与上部炉喉之间不留裂缝。
    var levels = [[0.10, 0.64], [0.30, 0.86], [0.58, 0.74], [0.83, 0.38], [0.94, 0.38]];
    for (var i = 1; i < levels.length; i += 1) {
      var a = levels[i - 1], b = levels[i];
      fillPoly(ctx, [at(-a[1], 0, a[0]), at(0, a[1], a[0]),
                     at(0, b[1], b[0]), at(-b[1], 0, b[0])], c.left);
      fillPoly(ctx, [at(0, a[1], a[0]), at(a[1], 0, a[0]),
                     at(b[1], 0, b[0]), at(0, b[1], b[0])], c.right);
      linePath(ctx, [at(-b[1], 0, b[0]), at(0, b[1], b[0]), at(b[1], 0, b[0])], 1, c.top);
    }
    drawIsoBoxAt(ctx, p.x, y - h * 0.94, w * 0.40, d * 0.40, h * 0.06,
                 pal.metalLight, pal.metal, pal.metalDark);
    fillDiamond(ctx, p.x, y - h, w * 0.24, d * 0.24, pal.ink);
    [0.32, 0.65].forEach(function (height) {
      var deck = y - h * height;
      var inner = height < 0.5 ? 0.85 : 0.64;
      [-1, 1].forEach(function (side) {
        fillPoly(ctx, [{ x: p.x + side * w * 0.93, y: deck },
                       { x: p.x, y: deck + d * 0.93 },
                       { x: p.x, y: deck + d * inner },
                       { x: p.x + side * w * inner, y: deck }], pal.metalLight);
      });
      // 平台内侧炉身继续向上，前缘护栏立在走道外沿。
      for (var j = 0; j <= 4; j += 1) {
        var t = j / 4;
        var x = p.x - w * 0.93 + w * 0.93 * t, py = deck + d * 0.93 * t;
        linePath(ctx, [{ x: x, y: py }, { x: x, y: py - 3 * z }], 1, pal.metalDark);
      }
      linePath(ctx, [{ x: p.x - w * 0.93, y: deck - 3 * z },
                    { x: p.x, y: deck + d * 0.93 - 3 * z }], 1, pal.metalLight);
    });
    var lx = p.x + w * 0.80;
    linePath(ctx, [{ x: lx, y: y - h * 0.12 }, { x: lx, y: y - h * 0.72 }], 1, pal.metalDark);
    linePath(ctx, [{ x: lx + 2 * z, y: y - h * 0.12 }, { x: lx + 2 * z, y: y - h * 0.72 }], 1, pal.metalDark);
    for (var rung = 0.16; rung < 0.72; rung += 0.10) {
      linePath(ctx, [{ x: lx, y: y - h * rung }, { x: lx + 2 * z, y: y - h * rung }], 1, pal.metalLight);
    }
    linePath(ctx, [at(-0.75, 0, 0.25), at(0, 0.75, 0.25), at(0.75, 0, 0.25)],
             Math.max(1, 2 * z), pal.metalDark);
    [-0.48, 0, 0.48].forEach(function (u) {
      var nozzle = at(u, 0.64 - Math.abs(u) * 0.3, 0.23);
      fillOval(ctx, nozzle.x, nozzle.y, Math.max(1, z), Math.max(1, z), pal.metalLight);
    });
    var tap = at(-0.35, 0.35, 0.13);
    drawIsoBoxAt(ctx, tap.x, tap.y, w * 0.22, d * 0.22, h * 0.09,
                 pal.metalDark, pal.ink, pal.ink);
    // 炉口是装饰性余热，不表示任务正在运行。
    fillOval(ctx, tap.x, tap.y - h * 0.04, Math.max(1, w * 0.14), Math.max(1, h * 0.025), pal.rustLight);
    if (S.night) {
      ctx.globalAlpha = 0.24;
      fillOval(ctx, tap.x, tap.y, w * 0.45, d * 0.5, pal.window);
      ctx.globalAlpha = 1;
    }
  }

  function drawDistillationModel(ctx, pal, m) {
    var z = S.cam.zoom, p = viewPoint(m.x, m.y);
    var y = p.y - (m.z0 || 0) * z, r = m.r * z, h = m.h * z;
    var c = roleCols(pal, m.role, m.bg);
    var foot = h * 0.08, top = y - h * 0.94;
    drawIsoBoxAt(ctx, p.x, y, m.hw * z, m.hh * z, foot,
                 pal.concreteLight, pal.concrete, pal.concreteDark);
    fillOval(ctx, p.x, y - foot, r, r * 0.5, c.right);
    ctx.fillStyle = c.left;
    ctx.fillRect(Math.round(p.x - r), Math.round(top), Math.max(1, r * 2), h * 0.86);
    ctx.fillStyle = c.top;
    ctx.fillRect(Math.round(p.x - r * 0.65), Math.round(top), Math.max(1, r * 0.35), h * 0.86);
    ctx.fillStyle = c.right;
    ctx.fillRect(Math.round(p.x + r * 0.4), Math.round(top), Math.max(1, r * 0.6), h * 0.86);
    for (var seam = 0.16; seam < 0.94; seam += 0.13) {
      linePath(ctx, [{ x: p.x - r, y: y - h * seam },
                    { x: p.x, y: y - h * seam + r * 0.42 },
                    { x: p.x + r, y: y - h * seam }], 1, c.right);
    }
    fillOval(ctx, p.x, top, r, r * 0.5, c.top);
    fillOval(ctx, p.x, top - h * 0.025, r * 0.72, r * 0.34, c.top);
    linePath(ctx, [{ x: p.x, y: top - h * 0.02 }, { x: p.x, y: y - h }], Math.max(1, z), pal.metalLight);
    var count = m.platforms || 3;
    for (var i = 0; i < count; i += 1) {
      var dy = y - h * (0.28 + i * 0.25), width = m.hw * z;
      for (var arc = 0; arc < 10; arc += 1) {
        var a0 = Math.PI * arc / 10, a1 = Math.PI * (arc + 1) / 10;
        fillPoly(ctx, [
          { x: p.x + Math.cos(a0) * width, y: dy + Math.sin(a0) * width * 0.46 },
          { x: p.x + Math.cos(a1) * width, y: dy + Math.sin(a1) * width * 0.46 },
          { x: p.x + Math.cos(a1) * r, y: dy + Math.sin(a1) * r * 0.5 },
          { x: p.x + Math.cos(a0) * r, y: dy + Math.sin(a0) * r * 0.5 }
        ], pal.metalDark);
      }
      linePath(ctx, [{ x: p.x - width, y: dy }, { x: p.x, y: dy + width * 0.46 },
                    { x: p.x + width, y: dy }], Math.max(1, z), pal.metalLight);
      [-1, 0, 1].forEach(function (side) {
        var px = p.x + side * width, py = dy + (side ? 0 : width * 0.46);
        linePath(ctx, [{ x: px, y: py }, { x: px, y: py - 2.5 * z }], 1, pal.metalDark);
      });
      linePath(ctx, [{ x: p.x - width, y: dy - 2.5 * z },
                    { x: p.x, y: dy + width * 0.46 - 2.5 * z },
                    { x: p.x + width, y: dy - 2.5 * z }], 1, pal.metalLight);
    }
    var lx = p.x + r * 0.45, ly = y - h * 0.86;
    [0, 2 * z].forEach(function (off) {
      linePath(ctx, [{ x: lx + off, y: y - foot }, { x: lx + off, y: ly }], 1, pal.metalDark);
    });
    for (var step = 4; step < m.h * 0.80; step += 4) {
      linePath(ctx, [{ x: lx, y: y - step * z }, { x: lx + 2 * z, y: y - step * z }], 1, pal.metalLight);
    }
    var pipeX = p.x - r * 1.25;
    linePath(ctx, [{ x: p.x - r * 0.65, y: y - h * 0.65 },
                  { x: pipeX, y: y - h * 0.65 }, { x: pipeX, y: y - foot }], Math.max(1, z * 1.5), pal.metalDark);
    fillOval(ctx, pipeX, y - h * 0.2, Math.max(1, 1.5 * z), Math.max(1, z), pal.rustLight);
    if (S.night) {
      ctx.fillStyle = pal.window;
      ctx.fillRect(Math.round(p.x - r), Math.round(y - h * 0.78), 1, 1);
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
    var top = cols.top, left = cols.left, right = cols.right;
    // 接地弧影 + 底座环
    fillOval(ctx, base.x, baseY + 1, Math.round(rx * 1.16), Math.round(ry * 1.16), pal.shadow);
    ctx.fillStyle = m.bg ? pal.bgSteelDark : pal.stoneDark;
    ctx.fillRect(base.x - rx, baseY - 2, rx * 2, 2);
    // 筒身：左中右三段受光 + 竖瓦楞
    fillOval(ctx, base.x, baseY, rx, ry, right);
    ctx.fillStyle = left;
    ctx.fillRect(base.x - rx, baseY - h, rx * 2, h);
    ctx.fillStyle = top;
    ctx.fillRect(base.x - Math.round(rx * 0.72), baseY - h, Math.round(rx * 0.44), h);
    ctx.fillStyle = right;
    ctx.fillRect(base.x + Math.round(rx * 0.30), baseY - h,
                 Math.max(1, Math.round(rx * 0.70)), h);
    if (rx > 4 && h > 8) {
      for (var c = 1; c < 6; c += 1) {
        var cx = base.x - rx + Math.round(rx * 2 * c / 6);
        linePath(ctx, [{ x: cx, y: baseY - h }, { x: cx, y: baseY }], 1,
                 c < 3 ? top : right);
      }
    }
    // 箍圈
    if (m.bands) {
      for (var i = 1; i <= m.bands; i += 1) {
        var by = Math.round(baseY - h * i / (m.bands + 1));
        ctx.fillStyle = right;
        ctx.fillRect(base.x - rx, by, rx * 2, 1);
        ctx.fillStyle = top;
        ctx.fillRect(base.x - Math.round(rx * 0.5), by, Math.round(rx * 0.52), 1);
        ctx.fillStyle = left;
        ctx.fillRect(base.x - rx, by + 1, rx * 2, 1);
      }
    }
    // 顶盖：穹顶带环口；烟囱是开口 + 警示环
    if (m.chimney) {
      fillOval(ctx, base.x, baseY - h, rx, ry, top);
      fillOval(ctx, base.x, baseY - h, Math.max(1, rx - 1), Math.max(1, ry - 1), pal.ink);
      linePath(ctx, [{ x: base.x - rx, y: baseY - h },
                    { x: base.x, y: baseY - h + ry },
                    { x: base.x + rx, y: baseY - h }], 1, pal.steelLight);
      ctx.fillStyle = pal.bad;
      ctx.fillRect(base.x - rx, baseY - Math.round(h * 0.86), rx * 2, Math.max(1, Math.round(z)));
      ctx.fillStyle = pal.mark;
      ctx.fillRect(base.x - rx, baseY - Math.round(h * 0.80), rx * 2, Math.max(1, Math.round(z)));
    } else {
      fillOval(ctx, base.x, baseY - h, rx, ry, top);
      fillOval(ctx, base.x, baseY - h, Math.max(1, rx - 2), Math.max(1, ry - 1), left);
      linePath(ctx, [{ x: base.x - Math.round(rx * 0.78), y: baseY - h },
                    { x: base.x, y: baseY - h - Math.round(ry * 0.72) },
                    { x: base.x + Math.round(rx * 0.78), y: baseY - h }], 1, top);
      // 顶部人孔与呼吸阀
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(base.x - 1, baseY - h - 1, 2, 2);
    }
    // 爬梯 + 护笼
    if (m.ladder) {
      var lx = base.x - Math.round(rx * 0.18);
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(lx, baseY - h + 2, 1, h - 3);
      ctx.fillRect(lx + Math.max(2, Math.round(rx * 0.22)), baseY - h + 2, 1, h - 3);
      for (var l = 3; l < h - 2; l += Math.max(3, Math.round(4 * z))) {
        ctx.fillRect(lx, baseY - h + l, Math.max(2, Math.round(rx * 0.22)), 1);
      }
      if (h > 18) {
        ctx.globalAlpha = 0.7;
        for (var cg = 6; cg < h - 4; cg += 8) {
          ctx.fillRect(lx - 1, baseY - h + cg, Math.max(4, Math.round(rx * 0.30)), 1);
        }
        ctx.globalAlpha = 1;
      }
    }
    // 侧向出料管：筒仓脚边一根短管
    if (m.silo) {
      linePath(ctx, [{ x: base.x + Math.round(rx * 0.55), y: baseY - Math.round(h * 0.12) },
                    { x: base.x + rx + Math.round(5 * z), y: baseY - Math.round(h * 0.12) },
                    { x: base.x + rx + Math.round(5 * z), y: baseY + 1 }],
               Math.max(1, Math.round(2 * z)), pal.metalDark);
      ctx.fillStyle = pal.metalLight;
      ctx.fillRect(base.x + rx + Math.round(5 * z), baseY - Math.round(h * 0.12), 1, 1);
    }
    if (S.night) {
      ctx.fillStyle = pal.window;
      ctx.fillRect(base.x - 1, baseY - h - ry - 1, 2, 2);
      ctx.globalAlpha = 0.25;
      fillOval(ctx, base.x, baseY - h - ry - 1, 4, 3, pal.window);
      ctx.globalAlpha = 1;
    }
  }

  function drawConveyorModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var pts = m.path.map(function (p) { return viewPoint(p.x, p.y); });
    var total = pathLength(pts);
    if (!total) return;
    var w = Math.max(2, Math.round(m.width * z));
    var bg = !!m.bg;
    var steel = bg ? pal.bgSteel : pal.steel;
    var steelDark = bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = bg ? pal.bgSteelLight : pal.steelLight;
    var beltCol = bg ? pal.bgSteel : pal.belt;
    var beltDark = bg ? pal.bgSteelDark : pal.beltDark;
    var beltMark = bg ? pal.bgSteelLight : pal.beltMark;
    var ore = bg ? pal.bgRust : pal.ore;
    var oreDark = bg ? pal.bgRustDark : pal.oreDark;
    // 支腿：双肢 + 斜撑 + 脚板；低矮支线不画腿，避免像栅栏
    var step = Math.max(8, vlen(20));
    for (var d = 0; m.support !== false && d < total; d += step) {
      var t = d / total;
      var pt = pointAt(pts, t);
      var hh = (m.h0 + (m.h1 - m.h0) * t) * z;
      if (hh < 3) continue;
      var legW = Math.max(2, Math.round(w * 0.62));
      ctx.fillStyle = steelDark;
      ctx.fillRect(Math.round(pt.x - legW), Math.round(pt.y - hh), 1, Math.max(2, Math.round(hh)));
      ctx.fillRect(Math.round(pt.x + legW), Math.round(pt.y - hh), 1, Math.max(2, Math.round(hh)));
      ctx.fillStyle = steel;
      ctx.fillRect(Math.round(pt.x), Math.round(pt.y - hh * 0.55), 1, 1);
      // 斜撑
      linePath(ctx, [{ x: pt.x - legW, y: pt.y - 2 },
                    { x: pt.x + legW, y: pt.y - hh + 1 }], 1, steel);
      linePath(ctx, [{ x: pt.x + legW, y: pt.y - 2 },
                    { x: pt.x - legW, y: pt.y - hh + 1 }], 1, steel);
      // 脚板
      ctx.fillStyle = steelLight;
      ctx.fillRect(Math.round(pt.x - legW - 1), Math.round(pt.y - 1), 4, 1);
    }
    // 带面：侧裙 + 皮带 + 滚轮刻线 + 边梁；刻线随帧缓慢移动
    for (var i = 1; i < pts.length; i += 1) {
      var a = pts[i - 1], b = pts[i];
      var ta = (i - 1) / Math.max(1, pts.length - 1);
      var tb = i / Math.max(1, pts.length - 1);
      var ya = a.y - (m.h0 + (m.h1 - m.h0) * ta) * z;
      var yb = b.y - (m.h0 + (m.h1 - m.h0) * tb) * z;
      var dx = b.x - a.x, dy = b.y - a.y;
      var len = Math.hypot(dx, dy) || 1;
      var nx = -dy / len * w / 2, ny = dx / len * w / 2;
      // 侧裙
      linePath(ctx, [{ x: a.x, y: ya + Math.max(2, Math.round(w * 0.42)) },
                    { x: b.x, y: yb + Math.max(2, Math.round(w * 0.42)) }],
               Math.max(1, Math.round(w * 0.66)), beltDark);
      // 带面
      fillPoly(ctx, [
        { x: a.x + nx, y: ya + ny }, { x: a.x - nx, y: ya - ny },
        { x: b.x - nx, y: yb - ny }, { x: b.x + nx, y: yb + ny },
      ], beltCol);
      // 皮带中缝与移动刻线
      linePath(ctx, [{ x: a.x + nx * 0.15, y: ya + ny * 0.15 },
                    { x: b.x + nx * 0.15, y: yb + ny * 0.15 }],
               Math.max(1, Math.round(w * 0.24)), m.ore ? steelDark : beltDark);
      if (!m.spur && w >= 3) {
        var phase = ((S.ticks * 0.7 + i * 3) % 14) / 14;
        var tick = pointAt([a, b], phase);
        var ty = ya + (yb - ya) * phase;
        ctx.fillStyle = beltMark;
        ctx.fillRect(Math.round(tick.x - nx * 0.55), Math.round(ty - ny * 0.55),
                     Math.max(1, Math.round(w * 0.34)), 1);
      }
      // 矿石
      if (m.ore) {
        var orePos = pointAt([a, b], 0.45);
        ctx.fillStyle = ore;
        ctx.fillRect(Math.round(orePos.x - w * 0.22),
                     Math.round((ya + yb) / 2 - Math.max(1, w * 0.22)),
                     Math.max(1, Math.round(w * 0.44)), Math.max(1, Math.round(w * 0.34)));
        ctx.fillStyle = oreDark;
        ctx.fillRect(Math.round(orePos.x - w * 0.10),
                     Math.round((ya + yb) / 2), Math.max(1, Math.round(w * 0.22)), 1);
        ctx.fillStyle = steelLight;
        ctx.fillRect(Math.round(orePos.x + w * 0.06),
                     Math.round((ya + yb) / 2 - Math.max(1, w * 0.22)), 1, 1);
      }
      // 边梁亮线
      linePath(ctx, [{ x: a.x + nx, y: ya + ny }, { x: b.x + nx, y: yb + ny }], 1, steelLight);
      linePath(ctx, [{ x: a.x - nx, y: ya - ny }, { x: b.x - nx, y: yb - ny }], 1, steelLight);
    }
    // 流向高光：只在主干带面上跑两枚低密度像素，给「调度厅 → 厂区」的方向感。
    // flowStart / flowEnd 来自切片时的原始路径距离，保证拐弯处不会跳回起点。
    if (!S.reduced && !m.spur && m.projectId && S.flowingProjects.has(m.projectId)
        && m.flowTotal > 0 && total > 2) {
      var flowTotal = m.flowTotal;
      var flowSpeed = m.ore ? 3.6 : 2.6;
      var flowPhase = (S.ticks * flowSpeed) % flowTotal;
      [flowPhase, (flowPhase + flowTotal * 0.5) % flowTotal].forEach(function (mark) {
        if (mark < m.flowStart || mark > m.flowEnd) return;
        var mt = (mark - m.flowStart) / Math.max(1, m.flowEnd - m.flowStart);
        var mp = pointAt(pts, mt);
        var mh = (m.h0 + (m.h1 - m.h0) * mt) * z;
        var mx = Math.round(mp.x), my = Math.round(mp.y - mh - 1);
        ctx.fillStyle = m.ore ? pal.paintLight : pal.glassGlow;
        ctx.fillRect(mx, my, Math.max(1, Math.round(z)), Math.max(1, Math.round(z)));
        ctx.fillRect(mx - Math.max(1, Math.round(z * 0.5)), my + 1,
                     Math.max(1, Math.round(z * 0.5)), 1);
      });
    }
    // 夜间边灯：沿带面每隔一段一颗暖黄小灯
    if (S.night && !bg) {
      for (var nd = 0; nd <= total; nd += Math.max(10, vlen(26))) {
        var nt = total ? nd / total : 0;
        var np = pointAt(pts, nt);
        np.y -= (m.h0 + (m.h1 - m.h0) * nt) * z;
        ctx.fillStyle = pal.window;
        ctx.fillRect(Math.round(np.x), Math.round(np.y) + 1, 1, 1);
      }
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
    var skid = Math.max(2, Math.round(2 * z));
    // 滑撬底座 + 主体
    fillDiamond(ctx, base.x, baseY, hw + Math.max(1, Math.round(z)), hh + 1, pal.ink);
    drawIsoBoxAt(ctx, base.x, baseY - skid, hw, hh, skid,
                 pal.steelLight, pal.metal, pal.metalDark);
    drawIsoBoxAt(ctx, base.x, baseY - skid, hw, hh, h,
                 pal.steelLight, pal.steel, pal.steelDark);
    // 侧板与散热格栅
    ctx.fillStyle = pal.steelMid;
    for (var gr = 1; gr <= 3; gr += 1) {
      linePath(ctx, [{ x: base.x - hw + hw * 0.18, y: baseY - h + hh * 0.18 + gr * h * 0.18 },
                    { x: base.x - hw + hw * 0.70, y: baseY - h + hh * 0.70 + gr * h * 0.18 }],
               1, pal.steelDark);
    }
    // 产线色带（顶沿）+ 反光
    var tones = [pal.rust, pal.glassLight, pal.ok, pal.product];
    var tone = tones[Math.abs(hashKey(m.rec.key)) % tones.length];
    ctx.fillStyle = tone;
    ctx.fillRect(base.x - hw, baseY - skid - h, Math.max(1, hw * 2), Math.max(1, Math.round(z * 1.3)));
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(base.x - hw, baseY - skid - h, Math.max(1, hw * 2), 1);
    // 右面控制柜：屏幕 + 按钮
    var cabW = Math.max(2, Math.round(hw * 0.42));
    var cabH = Math.max(2, Math.round(h * 0.44));
    var cabX = base.x + Math.max(1, Math.round(hw * 0.28));
    var cabY = baseY - skid - Math.max(1, Math.round(h * 0.82));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(cabX, cabY, cabW, cabH);
    ctx.fillStyle = line.status === "running" ? pal.glassGlow : pal.steelDark;
    ctx.fillRect(cabX + 1, cabY + 1, Math.max(1, cabW - 2), Math.max(1, Math.round(cabH * 0.42)));
    ctx.fillStyle = line.status === "running" ? pal.ok : pal.idle;
    ctx.fillRect(cabX + 1, cabY + cabH - 2, 1, 1);
    ctx.fillStyle = pal.paint;
    ctx.fillRect(cabX + cabW - 2, cabY + cabH - 2, 1, 1);
    // 状态灯柱。运行中被判成停滞时灯从绿转暗黄：这是「在跑但不健康」，
    // 与「失败」（红）和「正常在跑」（绿）都不同——它还没失败，只是不动了。
    var stall = line.status === "running" ? lineStall(line) : "";
    var lamp = pal.idle;
    if (line.status === "paused" || line.status === "stopped") lamp = pal.warn;
    else if (line.status === "failed" || line.last_status === "error") lamp = pal.bad;
    else if (line.status === "running") lamp = stall ? pal.warn : pal.ok;
    else if (line.last_status === "empty"
             || (line.health && line.health.last_outcome === "partial")) lamp = pal.warn;
    var poleH = Math.max(4, Math.round(6 * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(base.x + hw - 1, baseY - skid - h - poleH, 1, poleH + 1);
    ctx.fillStyle = lamp;
    ctx.fillRect(base.x + hw - 1, baseY - skid - h - poleH, 2, 2);
    if (line.status === "running") {
      // 正常运行的指示灯轻轻呼吸；停滞灯保持克制，减弱动态时两者都静止。
      var glowPulse = !S.reduced && !stall ? 0.82 + 0.18 * Math.sin(S.ticks * 0.10 + m.index) : 1;
      ctx.globalAlpha = (S.night ? 0.32 : 0.16) * glowPulse;
      fillOval(ctx, base.x + hw, baseY - skid - h - poleH, Math.max(3, 4 * z),
               Math.max(2, 3 * z), lamp);
      ctx.globalAlpha = 1;
    }
    // 快超时 / 已超时：机台顶部冒烟。烟从机台正上方走，不与状态灯抢位置。
    if (stall === "nearTimeout" || stall === "timeout") {
      var puff = stall === "timeout" ? 4 : 2;
      for (var sp = 0; sp < puff; sp += 1) {
        var st2 = ((S.ticks * (stall === "timeout" ? 0.06 : 0.045) + sp * 0.25) % 1);
        var sx2 = base.x + Math.sin(S.ticks * 0.05 + sp) * Math.max(1, 2.5 * z) - st2 * 2;
        var sy2 = baseY - skid - h - st2 * Math.max(8, 18 * z);
        var sr = Math.max(1, (1.4 + st2 * 3) * z);
        ctx.globalAlpha = (1 - st2) * (stall === "timeout" ? 0.55 : 0.32);
        fillOval(ctx, Math.round(sx2), Math.round(sy2), sr, sr * 0.75,
                 stall === "timeout" ? pal.ink : pal.smoke);
      }
      ctx.globalAlpha = 1;
    }
    // 运行中的活塞臂：小工位有节奏地往复
    if (line.status === "running") {
      var arm = Math.round(Math.sin(S.ticks * 0.9) * Math.max(1, hw * 0.22));
      ctx.fillStyle = pal.metalLight;
      ctx.fillRect(base.x - hw - Math.max(1, Math.round(z)) + arm,
                   baseY - skid - Math.max(2, Math.round(h * 0.52)), Math.max(2, Math.round(z * 2)), 1);
    }
    // 出料滑槽 + 小成品箱
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(base.x - hw - Math.max(1, Math.round(2 * z)), baseY + hh - 1,
                 Math.max(2, Math.round(3 * z)), Math.max(1, Math.round(z)));
    if (hw > 4) {
      drawIsoBoxAt(ctx, base.x - hw - Math.max(1, Math.round(2 * z)), baseY + hh + Math.max(1, Math.round(z)),
                   Math.max(1, Math.round(2 * z)), Math.max(1, Math.round(z)),
                   Math.max(2, Math.round(3 * z)), pal.crateLight, pal.crate, pal.crateDark);
    }
    // 装配线：工位串在后沿短带上，带面本身就是依赖流向
    if (line.kind === "pipeline" && (line.nodes || []).length) {
      drawPipelineFlow(ctx, pal, m, base, baseY, hw, hh);
    }
    m.rec._vx = base.x;
    m.rec._vy = baseY - skid - h - poleH - Math.max(4, vlen(6));
  }

  /** 装配线的工位流向：带面 + 逐工位的料状态。

      以前只画三颗灯珠，看得出「有几个工位」，看不出「谁在等谁」——而依赖流向
      正是装配线区别于单机产线的地方。现在：
      - 带面分三段着色：上游已出料的段是亮的，被失败 / 等待卡住的段是暗的；
      - 每个工位上方站一个小料箱，只有 `done` 的工位才有料流到下一段；
      - `blocked`（等待依赖）画成空位 + 暗灯，`ready` 画成待料的小空箱，
        `running` 画成料箱 + 活塞抖动，`done` 画成成品箱 + 亮灯；
      - 任一工位 `error`：它之后的整段带面画成断开（红色缺口），下游亮「等料」。
      以上全是表现层，节点状态机仍以 nodeStatusText 为准。 */
  function drawPipelineFlow(ctx, pal, m, base, baseY, hw, hh) {
    var z = S.cam.zoom;
    var nodes = m.line.nodes || [];
    var n = Math.min(PIPELINE_MAX_NODES, nodes.length);
    if (!n) return;
    var x0 = base.x - hw * 1.15;
    var span = hw * 2.3;
    var y = baseY + hh - Math.max(1, Math.round(2 * z));
    var stepPx = n > 1 ? span / (n - 1) : 0;
    // 找到第一个失败的工位：它之后的带面全部断开
    var breakAt = -1;
    for (var b = 0; b < n; b += 1) {
      if (nodes[b].status === "error" || nodes[b].status === "cancelled") { breakAt = b; break; }
    }
    // 带面分段：上游 done 越多，亮段越长
    for (var seg = 0; seg < n; seg += 1) {
      var ax = x0 + stepPx * seg;
      var bx2 = x0 + stepPx * (seg + 1);
      var lit = seg < n - 1 && nodes[seg].status === "done" && (breakAt < 0 || seg < breakAt);
      var col = lit ? pal.beltMark : pal.beltDark;
      if (seg === breakAt) col = pal.bad;   // 断口
      linePath(ctx, [{ x: ax, y: y }, { x: bx2, y: y }], Math.max(1, Math.round(z * 1.2)), col);
      // 卡料：断口前堆一小撮，表示料过不去
      if (seg === breakAt - 1 && breakAt > 0) {
        ctx.fillStyle = pal.crateDark;
        ctx.fillRect(Math.round(bx2 - Math.max(1, z)), Math.round(y - Math.max(1, z * 1.5)),
                     Math.max(1, Math.round(z * 1.5)), Math.max(1, Math.round(z * 1.5)));
      }
    }
    for (var i = 0; i < n; i += 1) {
      var sx = Math.round(x0 + stepPx * i);
      var node = nodes[i];
      var st = String(node.status || "");
      var blockedDown = breakAt >= 0 && i > breakAt;
      // 支柱
      ctx.fillStyle = pal.steelDark;
      ctx.fillRect(sx - Math.max(1, Math.round(z)), Math.round(y - Math.max(2, 3 * z)),
                   Math.max(2, Math.round(2 * z)), Math.max(2, Math.round(3 * z)));
      // 工位料箱
      var bw = Math.max(1, Math.round(2.2 * z));
      var boxY = y - Math.max(3, 4 * z);
      if (st === "done") {
        drawIsoBoxAt(ctx, sx, boxY, bw, Math.max(1, Math.round(bw * 0.5)), bw,
                     pal.product, pal.crate, pal.crateDark);
      } else if (st === "running") {
        var shake = S.reduced ? 0 : Math.round(Math.sin(S.ticks * 0.9 + i) * Math.max(0.6, z * 0.7));
        drawIsoBoxAt(ctx, sx, boxY + shake, bw, Math.max(1, Math.round(bw * 0.5)), bw,
                     pal.crateLight, pal.crate, pal.crateDark);
      } else if (st === "ready") {
        // 待料：空箱（只有框）
        linePath(ctx, ringPts(sx, boxY, bw, Math.max(1, Math.round(bw * 0.5))),
                 Math.max(1, Math.round(z)), pal.metalDark);
      }
      // 状态灯
      var nc = st === "running" ? pal.ok
        : st === "error" || st === "cancelled" ? pal.bad
          : st === "done" ? pal.steelLight
            : blockedDown ? pal.warn : pal.idle;
      ctx.fillStyle = nc;
      ctx.fillRect(sx, boxY - Math.max(2, 3 * z), Math.max(1, Math.round(z)), Math.max(1, Math.round(z)));
      // 下游等料：灯下挂一小段红色虚线，指着断口方向
      if (blockedDown && !S.reduced) {
        ctx.globalAlpha = 0.6;
        ctx.fillStyle = pal.warn;
        for (var d = 0; d < 2; d += 1) {
          ctx.fillRect(sx - Math.round(stepPx * 0.3) - d * Math.max(1, Math.round(2 * z)),
                       boxY - Math.max(3, 4 * z), Math.max(1, Math.round(z)), 1);
        }
        ctx.globalAlpha = 1;
      }
    }
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
    var hw = Math.round(w * 0.5), hh = Math.max(1, Math.round(h * 0.5));
    // 停放框：黄漆虚线 + 四角短标
    if (m.bay) {
      ctx.globalAlpha = 0.55;
      var bayHW = hw + Math.max(4, Math.round(6 * z));
      var bayHH = hh + Math.max(2, Math.round(3 * z));
      linePath(ctx, [{ x: base.x - bayHW, y: baseY }, { x: base.x, y: baseY + bayHH },
                    { x: base.x + bayHW, y: baseY }, { x: base.x, y: baseY - bayHH },
                    { x: base.x - bayHW, y: baseY }],
               1, pal.paint);
      ctx.globalAlpha = 1;
    }
    // 木托盘：底档 + 板条
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - hw, baseY - h, w, h);
    ctx.fillStyle = pal.crate;
    ctx.fillRect(base.x - hw + 1, baseY - h + 1, Math.max(1, w - 2), Math.max(1, h - 1));
    for (var slat = 1; slat <= 4; slat += 1) {
      ctx.fillStyle = pal.crateDark;
      ctx.fillRect(Math.round(base.x - hw + w * slat / 5), baseY - h + 1, 1, Math.max(1, h - 1));
    }
    ctx.fillStyle = pal.crateLight;
    ctx.fillRect(base.x - hw + 1, baseY - h, Math.max(1, w - 2), 1);
    // 成品垛：瓦楞纸箱 + 胶带 + 标签
    var done = 0;
    var st = activeState();
    ((st && st.runs) || []).forEach(function (r) {
      if (r.project_id === m.projectId && r.status === "done" && isToday(r.finished_at)) done += 1;
    });
    var stack = Math.min(5, done);
    var bw = Math.max(2, Math.round(4 * z));
    for (var i = 0; i < stack; i += 1) {
      var col = i % 2, row = Math.floor(i / 2);
      var bx = base.x - Math.round(bw * 1.15) + col * (bw + 1);
      var by = baseY - h - row * (bw + 1);
      drawIsoBoxAt(ctx, bx, by, bw, Math.max(1, bw * 0.5), Math.max(2, bw),
                   pal.product, pal.crate, pal.crateDark);
      // 胶带十字 + 面单
      ctx.fillStyle = pal.crateLight;
      ctx.fillRect(bx - 1, by - Math.max(2, bw), Math.max(1, bw * 2 - 1), 1);
      ctx.fillStyle = pal.mark;
      ctx.fillRect(bx + Math.max(1, Math.round(bw * 0.3)),
                   by - Math.max(2, bw) + 1, Math.max(1, Math.round(bw * 0.4)), 1);
    }
    // 小旗 + 托盘编号牌
    var poleX = base.x + Math.round(w / 2) - 1;
    var poleH = Math.max(4, Math.round(9 * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(poleX, baseY - h - poleH, 1, poleH);
    ctx.fillStyle = pal.ok;
    ctx.fillRect(poleX + 1, baseY - h - poleH, Math.max(2, Math.round(3 * z)),
                 Math.max(1, Math.round(2 * z)));
    ctx.fillStyle = pal.paint;
    ctx.fillRect(base.x - hw + 1, baseY - h - Math.max(1, Math.round(2 * z)),
                 Math.max(2, Math.round(3 * z)), 1);
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
    var u = clamp(Math.round(2.8 * z), 2, 4);
    var size = 6 * u;
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
    // 反光背心：让工头在灰坪上有个暖色落点
    ctx.fillStyle = pal.paint;
    ctx.fillRect(-Math.round(size * 0.40), top + Math.round(size * 0.28),
                 Math.round(size * 0.64), Math.max(1, u));
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

  // ---------- 厂外建筑：料场与罐区（前场两组，见 buildIndustrialSite） ----------
  // 约定与冷却塔 / 管廊一致：模型只记占地（hw/hh 或 r）与高度（h / z0），形态全在这里画。
  // 前场这两组不标 bg——bg 会被 roleCols 与雾化调色板压成远景色，摆在近景会发灰。

  /** 场院轴向取点：把组内坐标 (u 沿公路, v 纵深) 折到屏幕坐标。
      `m.axis` 是「沿公路方向」的世界单位向量（缺省 = 屏幕 x 轴向右）；纵深方向取它
      逆时针 90°，`v` 越大越远离公路那一侧。返回的点以 (x, baseY) 为原点，`up` 是
      模型自身的高度方向（往上为屏幕 -y），按像素给。 */
  function yardFrame(m, x, baseY, z) {
    var ax = m.axis ? m.axis.x : 1;
    var ay = m.axis ? m.axis.y : 0;
    var nx = -ay, ny = ax;
    return function (u, v, up) {
      return {
        x: x + Math.round((u * ax + v * nx) * z),
        y: baseY + Math.round((u * ay + v * ny) * z) - (up || 0),
      };
    };
  }

  /** 矮围栏：沿 path 立柱 + 两道横杆。料场用它把三排圈成一组——不铺场坪，
      范围靠这道线标出来、底面仍是沙地；靠公路那侧留出入口，门口另立一段高门柱。 */
  function drawFenceModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var h = Math.max(2, Math.round((m.h || 4) * z));
    var cols = roleCols(pal, m.role || "steel", m.bg);
    var postW = Math.max(1, Math.round(1.4 * z));
    var gap = Math.max(4, m.postGap || 6);
    var base = m.path.map(function (q) { var p = viewPoint(q.x, q.y); return { x: p.x, y: p.y }; });
    // 横杆先画（顶杆 + 腰杆），立柱压在上面
    [[h, cols.top], [Math.round(h * 0.55), cols.right]].forEach(function (bar) {
      linePath(ctx, base.map(function (p) { return { x: p.x, y: p.y - bar[0] }; }),
               Math.max(1, Math.round(1.6 * z)), bar[1]);
    });
    // 立柱：按世界步长沿 path 均布
    for (var i = 0; i < base.length - 1; i += 1) {
      var a = base[i], b = base[i + 1];
      var n = Math.max(1, Math.round(
        Math.hypot(b.x - a.x, b.y - a.y) / Math.max(1, gap * z)));
      for (var k = 0; k <= n; k += 1) {
        var t = k / n;
        var px = Math.round(a.x + (b.x - a.x) * t);
        var py = Math.round(a.y + (b.y - a.y) * t);
        ctx.fillStyle = k % 2 ? cols.right : cols.left;
        ctx.fillRect(px, py - h, postW, h);
      }
    }
  }

  /** 拱顶煤棚：高柱 + 平檐双坡屋盖（前侧全开）+ 后缘挡煤墙 + 棚下煤堆。
      檐口要比煤堆顶高出约一个坪深，煤堆才露得出来（否则屋盖把料整个盖住）。 */
  function drawCoalShedModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = m.hw || 30, hh = m.hh || 12;
    var h = Math.max(8, Math.round(m.h * z));
    var rise = Math.max(3, Math.round((m.rise || m.h * 0.22) * z));
    var cols = roleCols(pal, m.role || "steel", m.bg);
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var postW = Math.max(1, Math.round(1.8 * z));
    var at = yardFrame(m, p.x, baseY, z);
    // 菱形四角：L / R 是沿公路的两端，B 是后缘（远离公路那侧）、F 是敞口（朝公路）
    var L = at(-hw, 0), R = at(hw, 0), B = at(0, hh), F = at(0, -hh);
    // 后缘挡煤墙：两片矮墙，遮住煤堆后侧（前侧全开，料从这儿铲出去）
    var wallH = Math.max(3, Math.round(h * 0.42));
    fillPoly(ctx, [at(-hw, 0), at(0, hh), at(0, hh, wallH), at(-hw, 0, wallH)], cols.left);
    fillPoly(ctx, [at(0, hh), at(hw, 0), at(hw, 0, wallH), at(0, hh, wallH)], cols.right);
    linePath(ctx, [at(-hw, 0, wallH), at(0, hh, wallH), at(hw, 0, wallH)], 1, steelLight);
    // 煤堆：压在棚前半部（靠公路那侧），脊线在棚内、坡脚落到前檐外，正好露在开口里
    var ridgeUp = Math.max(3, Math.round(h * 0.30));
    var crownUp = Math.max(2, Math.round(3 * z));
    fillPoly(ctx, [
      at(-hw * 0.9, -hh * 1.02), at(hw * 0.9, -hh * 1.02),
      at(hw * 0.62, -hh * 0.42, ridgeUp), at(-hw * 0.62, -hh * 0.42, ridgeUp),
    ], pal.oreDark);
    fillPoly(ctx, [
      at(-hw * 0.62, -hh * 0.42, ridgeUp), at(hw * 0.62, -hh * 0.42, ridgeUp),
      at(hw * 0.40, -hh * 0.42, ridgeUp + crownUp),
      at(-hw * 0.40, -hh * 0.42, ridgeUp + crownUp),
    ], pal.ore);
    // 散落的煤块（确定性散布，不随帧变化）
    var rand = seeded(hashKey("coal" + m.x + ":" + m.y));
    for (var lump = 0; lump < 5; lump += 1) {
      var lumpPt = at((rand() * 2 - 1) * hw * 0.86, -hh * (0.55 + rand() * 0.62));
      ctx.fillStyle = lump % 2 ? pal.oreDark : pal.ink;
      ctx.fillRect(lumpPt.x, lumpPt.y, Math.max(1, Math.round(2 * z)), 1);
      ctx.fillRect(lumpPt.x + 1, lumpPt.y - 1, 1, 1);
    }
    // 六根立柱：四个角 + 左右两侧中点；檐口齐平（平檐双坡）
    [L, at(-hw * 0.5, hh * 0.5), B, R, at(hw * 0.5, hh * 0.5), F].forEach(function (node, ni) {
      ctx.fillStyle = ni === 0 || ni === 4 || ni === 5 ? steelDark : steelLight;
      ctx.fillRect(node.x, node.y - h, postW, h);
      ctx.fillStyle = steelDark;
      ctx.fillRect(node.x - 1, node.y - 1, postW + 2, 1);
    });
    // 屋盖：双坡（脊线沿公路方向，前后檐齐平），只画可见的两片坡面 + 一道檐口厚度
    var ridgeV = hh * 0.08, eaveV = hh * 0.92, ridgeUp = h + rise;
    fillPoly(ctx, [at(-hw * 0.94, ridgeV, ridgeUp), at(hw * 0.94, ridgeV, ridgeUp),
                   at(hw, eaveV, h), at(-hw, eaveV, h)], cols.top);
    fillPoly(ctx, [at(-hw * 0.94, ridgeV, ridgeUp), at(hw * 0.94, ridgeV, ridgeUp),
                   at(hw, -eaveV, h), at(-hw, -eaveV, h)], cols.left);
    linePath(ctx, [at(-hw * 0.94, ridgeV, ridgeUp), at(hw * 0.94, ridgeV, ridgeUp)],
             Math.max(1, Math.round(z)), steelLight);
    linePath(ctx, [at(-hw, -eaveV, h), at(hw, -eaveV, h)],
             Math.max(1, Math.round(z)), steelDark);
    linePath(ctx, [at(-hw, eaveV, h), at(hw, eaveV, h)],
             Math.max(1, Math.round(z)), cols.right);
    // 屋面檩条：前后两坡各两道，压住大片屋面色
    ctx.globalAlpha = 0.32;
    [1 / 3, 2 / 3].forEach(function (pt) {
      var up = h + rise * (1 - pt);
      linePath(ctx, [at(-hw * 0.94, ridgeV + (-eaveV - ridgeV) * pt, up),
                    at(hw * 0.94, ridgeV + (-eaveV - ridgeV) * pt, up)],
               1, steelDark);
      linePath(ctx, [at(-hw * 0.94, ridgeV + (eaveV - ridgeV) * pt, up),
                    at(hw * 0.94, ridgeV + (eaveV - ridgeV) * pt, up)],
               1, cols.right);
    });
    ctx.globalAlpha = 1;
    // 屋顶采光带：两块斜置天窗，夜里透出一点暖光
    [-0.34, 0.30].forEach(function (u0) {
      function rp(uu, tt) {
        return at(uu, ridgeV + (-eaveV - ridgeV) * tt, ridgeUp + (h - ridgeUp) * tt);
      }
      var u1 = u0 + 0.13;
      fillPoly(ctx, [rp(hw * u0, 0.30), rp(hw * u1, 0.30),
                     rp(hw * u1, 0.42), rp(hw * u0, 0.42)],
               S.night ? pal.glassDark : pal.glassGlow);
      linePath(ctx, [rp(hw * u0, 0.30), rp(hw * u1, 0.30)], 1, steelLight);
      if (S.night) {
        var sg = rp(hw * (u0 + 0.065), 0.36);
        ctx.globalAlpha = 0.16;
        fillOval(ctx, sg.x, sg.y, vlen(5), vlen(2.4), pal.window);
        ctx.globalAlpha = 1;
      }
    });
    // 脊部通风帽
    var ridgePt = at(0, ridgeV, ridgeUp);
    drawIsoBoxAt(ctx, ridgePt.x, ridgePt.y,
                 Math.max(2, Math.round(4 * z)), Math.max(1, Math.round(2 * z)),
                 Math.max(1, Math.round(2 * z)), steelLight, cols.left, steelDark);
    if (S.night) {
      var glow = at(0, 0, Math.max(3, Math.round(h * 0.34)));
      ctx.globalAlpha = 0.3;
      fillOval(ctx, glow.x, glow.y, vlen(10), vlen(5), pal.window);
      ctx.globalAlpha = 1;
      // 棚口再补一层暖光，让敞开的料堆在夜里读得出来
      var mouth = at(0, -hh * 0.7, h * 0.42);
      ctx.globalAlpha = 0.22;
      fillOval(ctx, mouth.x, mouth.y, vlen(9), vlen(4.5), pal.window);
      ctx.globalAlpha = 1;
      var edge = at(-hw, -hh * 0.9, h);
      ctx.fillStyle = pal.window;
      ctx.fillRect(edge.x + 2, edge.y, 2, 1);
    }
  }

  /** 筒仓群 + 斗式提升机塔：原料/成品仓，塔顶机房与筒仓顶用一条输送管连通。 */
  function drawSiloBatteryModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var count = Math.max(1, Math.round(m.count || 3));
    var gap = m.gapX || 16;                 // 沿公路方向的仓距（世界单位，见 yardFrame）
    var r = Math.max(3, Math.round(m.r * z));
    var h = Math.max(10, Math.round(m.h * z));
    var cols = roleCols(pal, m.role || "steel", m.bg);
    var body = m.bg ? pal.bgSteel : pal.steel;
    var bodyLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var bodyDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var leg = Math.max(2, Math.round(5 * z));
    var hopper = Math.max(3, Math.round(6 * z));
    var at = yardFrame(m, p.x, baseY, z);
    // 塔（斗式提升机）立在仓列沿公路方向的右端：细高 + 顶部机房
    var towerU = ((count - 1) / 2) * gap + (m.r || 0) * 2.1 + (m.towerGap || 0);
    var towerPt = at(towerU, 0);
    var towerW = Math.max(3, Math.round(7 * z));
    var towerH = h + Math.max(6, Math.round(12 * z));
    for (var i = 0; i < count; i += 1) {
      var c = at((i - (count - 1) / 2) * gap, (i % 2) * 2);
      var cx = c.x, cy = c.y;
      // 筒仓腿 + 锥斗：仓底悬空，物料从锥口落到下面的皮带
      fillOval(ctx, cx, cy + 1, Math.round(r * 1.1), Math.max(1, Math.round(r * 0.34)), pal.shadow);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(cx - Math.round(r * 0.8), cy - leg, Math.max(1, Math.round(1.5 * z)), leg);
      ctx.fillRect(cx + Math.round(r * 0.6), cy - leg, Math.max(1, Math.round(1.5 * z)), leg);
      fillPoly(ctx, [
        { x: cx - Math.round(r * 0.9), y: cy - leg }, { x: cx + Math.round(r * 0.9), y: cy - leg },
        { x: cx + Math.round(r * 0.34), y: cy - leg - hopper },
        { x: cx - Math.round(r * 0.34), y: cy - leg - hopper },
      ], bodyDark);
      // 仓身：三段受光 + 竖瓦楞 + 箍圈
      var bodyTop = cy - leg - hopper - h;
      var rx = r;
      fillOval(ctx, cx, cy - leg - hopper, rx, Math.max(1, Math.round(rx * 0.5)), body);
      ctx.fillStyle = body;
      ctx.fillRect(cx - rx, bodyTop, rx * 2, cy - leg - hopper - bodyTop);
      ctx.fillStyle = bodyLight;
      ctx.fillRect(cx - Math.round(rx * 0.66), bodyTop, Math.max(1, Math.round(rx * 0.42)),
                   cy - leg - hopper - bodyTop);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(cx + Math.round(rx * 0.36), bodyTop, Math.max(1, Math.round(rx * 0.64)),
                   cy - leg - hopper - bodyTop);
      for (var band = 1; band <= 3; band += 1) {
        var by = Math.round(bodyTop + (cy - leg - hopper - bodyTop) * band / 4);
        ctx.fillStyle = bodyDark;
        ctx.fillRect(cx - rx, by, rx * 2, 1);
        ctx.fillStyle = bodyLight;
        ctx.fillRect(cx - Math.round(rx * 0.6), by, Math.round(rx * 0.62), 1);
      }
      // 仓顶：穹顶 + 人孔 + 除尘帽
      fillOval(ctx, cx, bodyTop, rx, Math.max(1, Math.round(rx * 0.5)), bodyLight);
      fillOval(ctx, cx, bodyTop, Math.max(1, rx - Math.max(1, Math.round(2 * z))),
               Math.max(1, Math.round(rx * 0.5) - 1), body);
      linePath(ctx, [{ x: cx - Math.round(rx * 0.7), y: bodyTop },
                    { x: cx, y: bodyTop - Math.max(2, Math.round(rx * 0.5)) },
                    { x: cx + Math.round(rx * 0.7), y: bodyTop }], 1, bodyLight);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(cx - 1, bodyTop - Math.max(2, Math.round(rx * 0.5)) - 1, 3, 2);
      if (S.night) {
        // 冷白投光在仓身左侧留一道窄高光
        ctx.globalAlpha = 0.30;
        ctx.fillStyle = pal.glassGlow;
        ctx.fillRect(cx - rx, bodyTop + 1, 1,
                     Math.max(2, Math.round((cy - leg - hopper - bodyTop) * 0.55)));
        ctx.globalAlpha = 1;
        ctx.fillStyle = pal.bad;
        ctx.fillRect(cx - 1, bodyTop - Math.max(3, Math.round(rx * 0.5)) - 2, 2, 1);
      }
      // 仓身检修梯：放大了才画，全景时保持干净
      if (rx >= 4) {
        var lx2 = cx - rx + 1;
        var lt = bodyTop + 2;
        var lb = cy - leg - hopper - 1;
        ctx.fillStyle = bodyDark;
        ctx.fillRect(lx2, lt, 1, lb - lt);
        ctx.fillRect(lx2 + 2, lt, 1, lb - lt);
        for (var lr = lt + 3; lr < lb; lr += 3) ctx.fillRect(lx2, lr, 3, 1);
      }
    }
    // 顶部输送管：机房 → 各仓顶（略高于仓顶，接在塔身上段，沿公路方向）
    var pipeUp = leg + hopper + h + Math.max(3, Math.round(6 * z));
    var pipeRoot = at(towerU, 0, pipeUp);
    var pipeEnd = at(-(count - 1) / 2 * gap, 0, pipeUp);
    linePath(ctx, [{ x: pipeRoot.x - Math.round(towerW * 0.5), y: pipeRoot.y }, pipeEnd],
             Math.max(1, Math.round(2 * z)), bodyDark);
    linePath(ctx, [{ x: pipeRoot.x - Math.round(towerW * 0.5), y: pipeRoot.y - 1 },
                  { x: pipeEnd.x, y: pipeEnd.y - 1 }],
             1, bodyLight);
    // 提升机塔
    fillOval(ctx, towerPt.x, towerPt.y + 1, Math.round(towerW * 1.3), Math.max(1, Math.round(towerW * 0.5)), pal.shadow);
    ctx.fillStyle = bodyDark;
    ctx.fillRect(towerPt.x - towerW, towerPt.y - towerH, towerW * 2, towerH);
    ctx.fillStyle = bodyLight;
    ctx.fillRect(towerPt.x - Math.round(towerW * 0.6), towerPt.y - towerH, Math.max(1, Math.round(towerW * 0.6)), towerH);
    ctx.fillStyle = body;
    ctx.fillRect(towerPt.x + Math.round(towerW * 0.4), towerPt.y - towerH, Math.max(1, Math.round(towerW * 0.6)), towerH);
    for (var seg = 1; seg <= 4; seg += 1) {
      var sy = Math.round(towerPt.y - towerH * seg / 5);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(towerPt.x - towerW, sy, towerW * 2, 1);
    }
    // 机房（顶部大一号的箱体）
    var houseH = Math.max(4, Math.round(10 * z));
    drawIsoBoxAt(ctx, towerPt.x, towerPt.y - towerH, Math.round(towerW * 1.5), Math.round(towerW * 0.75), houseH,
                 m.bg ? pal.bgSteelLight : pal.roofSlateLight, body, bodyDark);
    if (S.night) {
      // 塔顶平台灯 + 机房窗 + 航空障碍灯
      ctx.globalAlpha = 0.5;
      ctx.fillStyle = pal.glassGlow;
      ctx.fillRect(towerPt.x - towerW, towerPt.y - towerH + 1, towerW * 2, 1);
      ctx.globalAlpha = 1;
      ctx.fillStyle = pal.window;
      ctx.fillRect(towerPt.x - Math.round(towerW * 0.4), towerPt.y - towerH - houseH + 1, 2, 2);
      ctx.fillStyle = pal.bad;
      ctx.fillRect(towerPt.x + 1, towerPt.y - towerH - houseH - 2, 2, 1);
    }
    if (towerW >= 4) {
      // 塔身外梯
      var tlx = towerPt.x - towerW - 1;
      ctx.fillStyle = bodyDark;
      ctx.fillRect(tlx, towerPt.y - towerH + 4, 1, towerH - 5);
      ctx.fillRect(tlx + 2, towerPt.y - towerH + 4, 1, towerH - 5);
      for (var tly = towerPt.y - towerH + 6; tly < towerPt.y - 2;
           tly += Math.max(3, Math.round(4 * z))) {
        ctx.fillRect(tlx, tly, 3, 1);
      }
      // 塔脚控制箱
      drawIsoBoxAt(ctx, towerPt.x + towerW + 3, towerPt.y - 1,
                   Math.max(2, Math.round(3 * z)), Math.max(1, Math.round(1.6 * z)),
                   Math.max(2, Math.round(3 * z)),
                   pal.steelLight, pal.steel, pal.steelDark);
      if (S.night) {
        ctx.fillStyle = pal.window;
        ctx.fillRect(towerPt.x + towerW + 3, towerPt.y - Math.max(2, Math.round(3 * z)), 2, 1);
      }
    }
    // 侧向出料管：塔脚一根落地管
    linePath(ctx, [{ x: towerPt.x + towerW, y: towerPt.y - Math.round(towerH * 0.24) },
                  { x: towerPt.x + towerW + Math.max(2, Math.round(4 * z)), y: towerPt.y - Math.round(towerH * 0.24) },
                  { x: towerPt.x + towerW + Math.max(2, Math.round(4 * z)), y: towerPt.y + 1 }],
             Math.max(1, Math.round(2 * z)), cols.right);
  }

  /** 露天煤堆：截顶四棱锥，顶面与正面受光，散几块煤。 */
  function drawCoalPileModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(4, Math.round((m.hw || 12) * z));
    var hh = Math.max(2, Math.round((m.hh || 6) * z));
    var h = Math.max(2, Math.round((m.h || 8) * z));
    var top = Math.max(1, Math.round(hw * 0.52));
    var topH = Math.max(1, Math.round(hh * 0.52));
    var topY = baseY - h;
    var W = { x: p.x - hw, y: baseY }, N = { x: p.x, y: baseY - hh };
    var E = { x: p.x + hw, y: baseY }, SP = { x: p.x, y: baseY + hh };
    var WT = { x: p.x - top, y: topY }, NT = { x: p.x, y: topY - topH };
    var ET = { x: p.x + top, y: topY }, ST = { x: p.x, y: topY + topH };
    fillDiamond(ctx, p.x, baseY + 1, Math.round(hw * 1.04), Math.round(hh * 1.04), pal.oreDark);
    fillPoly(ctx, [W, N, NT, WT], pal.oreDark);
    fillPoly(ctx, [N, E, ET, NT], pal.oreDark);
    fillPoly(ctx, [W, SP, ST, WT], pal.ore);
    fillPoly(ctx, [SP, E, ET, ST], pal.ore);
    fillDiamond(ctx, p.x, topY, top, topH, pal.ore);
    linePath(ctx, [WT, ST, ET], 1, pal.oreLight);
    // 散落的煤块（确定性散布，不随帧变化）
    var rand = seeded(hashKey("coalpile" + m.x + ":" + m.y));
    for (var i = 0; i < 7; i += 1) {
      var lu = rand() * 2 - 1;
      var lv = rand() * 2 - 1;
      var lx = p.x + Math.round(lu * hw * 0.8);
      var ly = topY + Math.round(lv * topH * 0.8);
      ctx.fillStyle = i % 3 ? pal.ink : pal.oreLight;
      ctx.fillRect(lx, ly, Math.max(1, Math.round(2 * z)), 1);
    }
  }

  /** 地磅：低混凝土台 + 深色秤板 + 黄黑端线 + 计量立杆。 */
  function drawWeighbridgeModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(6, Math.round((m.hw || 15) * z));
    var hh = Math.max(3, Math.round((m.hh || 7) * z));
    var h = Math.max(1, Math.round((m.h || 1.6) * z));
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, h,
                 pal.concreteLight, pal.concrete, pal.concreteDark);
    fillDiamond(ctx, p.x, baseY - h, Math.round(hw * 0.88), Math.round(hh * 0.82), pal.metalDark);
    linePath(ctx, [{ x: p.x - hw + 1, y: baseY - h },
                  { x: p.x, y: baseY - h + hh - 1 },
                  { x: p.x + hw - 1, y: baseY - h }], 1, pal.warn);
    linePath(ctx, [{ x: p.x - Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.28) },
                  { x: p.x + Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.28) }],
             1, pal.metal);
    linePath(ctx, [{ x: p.x - Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.66) },
                  { x: p.x + Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.66) }],
             1, pal.metal);
    // 两端黄黑警示条：做大一档，卡车压上去后仍能从台缘读出来
    [-1, 1].forEach(function (side) {
      var ex = p.x + Math.round(hw * 0.9) * side;
      for (var s = 0; s < 4; s += 1) {
        ctx.fillStyle = s % 2 ? pal.ink : pal.warn;
        ctx.fillRect(ex - Math.round(hw * 0.18) + s * Math.max(2, Math.round(3 * z)),
                     baseY - h + Math.round(hh * 0.18), Math.max(2, Math.round(3 * z)),
                     Math.max(1, Math.round(2 * z)));
      }
    });
    // 计量立杆（台侧）：细杆 + 状态灯
    var postX = p.x + Math.round(hw * 0.94);
    var postY = baseY - h + Math.round(hh * 0.5);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(postX, postY - Math.round(8 * z), 1, Math.round(8 * z));
    ctx.fillStyle = pal.warn;
    ctx.fillRect(postX - 1, postY - Math.round(9 * z), 3, 2);
    if (S.night) {
      ctx.globalAlpha = 0.45;
      fillOval(ctx, postX, postY - Math.round(9 * z), vlen(4), vlen(2), pal.window);
      ctx.globalAlpha = 1;
    }
  }

  /** 冷白投光灯：细高杆 + 横臂双灯头，地面光池由 drawLightPools 统一画。 */
  function drawFloodlightModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(12, Math.round((m.h || 32) * z));
    var arm = Math.max(4, Math.round(6 * z));
    ctx.fillStyle = pal.stoneDark;
    ctx.fillRect(p.x - 2, baseY - 1, 4, 2);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x, baseY - h, Math.max(1, Math.round(1.6 * z)), h);
    ctx.fillStyle = pal.metal;
    ctx.fillRect(p.x - 1, baseY - Math.round(h * 0.45), Math.max(2, Math.round(2.6 * z)), 1);
    ctx.fillRect(p.x - arm, baseY - h, arm * 2 + 1, Math.max(1, Math.round(1.4 * z)));
    [-arm, arm - Math.max(2, Math.round(3 * z))].forEach(function (dx) {
      var hx = p.x + dx;
      ctx.fillStyle = pal.metal;
      ctx.fillRect(hx, baseY - h + 1, Math.max(2, Math.round(3 * z)), Math.max(1, Math.round(2 * z)));
      ctx.fillStyle = S.night ? pal.glassGlow : pal.metalLight;
      ctx.fillRect(hx, baseY - h + 2, Math.max(2, Math.round(2 * z)), 1);
      if (S.night) {
        ctx.globalAlpha = 0.28;
        fillOval(ctx, hx + 1, baseY - h + 2, vlen(4), vlen(2.5), pal.glassGlow);
        ctx.globalAlpha = 1;
      }
    });
  }

  /** 湿式气柜：三节套筒互相套着升降，外套导架 + 外梯，罐顶放散管。 */
  function drawGasholderModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var r = Math.max(4, Math.round(m.r * z));
    var h = Math.max(12, Math.round(m.h * z));
    var cols = roleCols(pal, m.role || "rust", m.bg);
    var dark = m.bg ? pal.bgSteelDark : pal.metalDark;
    var light = m.bg ? pal.bgSteelLight : pal.metalLight;
    // 基础环
    ctx.fillStyle = m.bg ? pal.bgSteelDark : pal.stoneDark;
    ctx.fillRect(p.x - Math.round(r * 1.1), baseY - 2, Math.round(r * 2.2), 2);
    fillDiamond(ctx, p.x, baseY - 2, Math.round(r * 1.16), Math.max(1, Math.round(r * 0.58)),
                m.bg ? pal.bgSteel : pal.stone);
    // 导架：两根后立柱先画（被罐体挡住一半）
    var frameH = h + Math.max(3, Math.round(5 * z));
    [-0.86, 0.86].forEach(function (side) {
      var fx = p.x + Math.round(r * side);
      ctx.fillStyle = dark;
      ctx.fillRect(fx, baseY - frameH, Math.max(1, Math.round(1.4 * z)), frameH);
    });
    // 三节套筒：底节最粗，往上逐节变细，露出上一节的上沿
    var sections = [
      { rr: 1.0, top: 0.50 }, { rr: 0.90, top: 0.70 }, { rr: 0.80, top: 0.86 },
    ];
    sections.forEach(function (sec) {
      var sr = Math.round(r * sec.rr), sh = Math.round(h * sec.top);
      var ry = Math.max(1, Math.round(sr * 0.5));
      fillOval(ctx, p.x, baseY, sr, ry, cols.right);
      ctx.fillStyle = cols.left;
      ctx.fillRect(p.x - sr, baseY - sh, sr * 2, sh);
      ctx.fillStyle = cols.top;
      ctx.fillRect(p.x - Math.round(sr * 0.70), baseY - sh, Math.max(1, Math.round(sr * 0.40)), sh);
      ctx.fillStyle = cols.right;
      ctx.fillRect(p.x + Math.round(sr * 0.32), baseY - sh, Math.max(1, Math.round(sr * 0.68)), sh);
      // 上沿环口：套筒的标志
      fillOval(ctx, p.x, baseY - sh, sr, ry, cols.top);
      fillOval(ctx, p.x, baseY - sh, Math.max(1, sr - Math.max(1, Math.round(1.6 * z))),
               Math.max(1, ry - 1), cols.right);
      linePath(ctx, [{ x: p.x - sr, y: baseY - sh }, { x: p.x, y: baseY - sh + ry },
                    { x: p.x + sr, y: baseY - sh }], 1, light);
    });
    // 罐顶：穹顶 + 放散管 + 人孔
    var topY = baseY - Math.round(h * 0.86);
    var topR = Math.round(r * 0.8);
    for (var cap = 0; cap < 3; cap += 1) {
      var cr = Math.round(topR * (1 - cap * 0.24));
      fillOval(ctx, p.x, topY - Math.round(cap * 3 * z), cr, Math.max(1, Math.round(cr * 0.5)),
               cap === 0 ? cols.top : light);
    }
    linePath(ctx, [{ x: p.x, y: topY - Math.round(9 * z) },
                  { x: p.x, y: topY - Math.round(13 * z) }], Math.max(1, Math.round(1.5 * z)), dark);
    linePath(ctx, [{ x: p.x - 1, y: topY - Math.round(13 * z) }, { x: p.x + 1, y: topY - Math.round(13 * z) }],
             1, light);
    // 前柱压在罐体上：前缘的两根导架柱（比后柱亮一档即可，别亮成白线）
    [-0.86, 0.86].forEach(function (side) {
      var fx = p.x + Math.round(r * side);
      ctx.fillStyle = m.bg ? pal.bgSteel : pal.steel;
      ctx.fillRect(fx, baseY - frameH, Math.max(1, Math.round(1.4 * z)), Math.round(frameH * 0.9));
    });
    // 顶部导架环 + 外梯
    linePath(ctx, [{ x: p.x - Math.round(r * 0.86), y: baseY - frameH },
                  { x: p.x + Math.round(r * 0.86), y: baseY - frameH }],
             Math.max(1, Math.round(1.4 * z)), light);
    var ladderX = p.x + Math.round(r * 0.34);
    linePath(ctx, [{ x: ladderX, y: baseY + 2 }, { x: ladderX, y: topY + 2 }], 1, dark);
    linePath(ctx, [{ x: ladderX + Math.max(2, Math.round(2.4 * z)), y: baseY + 2 },
                  { x: ladderX + Math.max(2, Math.round(2.4 * z)), y: topY + 2 }], 1, dark);
    for (var rung = 4; rung < h - 4; rung += Math.max(3, Math.round(4 * z))) {
      linePath(ctx, [{ x: ladderX, y: baseY - rung * z },
                    { x: ladderX + Math.max(2, Math.round(2.4 * z)), y: baseY - rung * z }], 1, light);
    }
    // 中间检修平台
    var deckY = baseY - Math.round(h * 0.50);
    linePath(ctx, [{ x: p.x - Math.round(r * 0.95), y: deckY },
                  { x: p.x, y: deckY + Math.max(1, Math.round(r * 0.48)) },
                  { x: p.x + Math.round(r * 0.95), y: deckY }], Math.max(1, Math.round(1.4 * z)), light);
    if (S.night) {
      ctx.fillStyle = pal.bad;
      ctx.fillRect(p.x - 1, topY - Math.round(15 * z), 2, 2);
      ctx.globalAlpha = 0.3;
      fillOval(ctx, p.x, topY - Math.round(15 * z), vlen(5), vlen(3), pal.bad);
      ctx.globalAlpha = 1;
    }
    // 出口管：气柜侧下方的短管接到管廊
    linePath(ctx, [{ x: p.x - Math.round(r * 0.6), y: baseY - Math.round(h * 0.14) },
                  { x: p.x - r - Math.max(2, Math.round(4 * z)), y: baseY - Math.round(h * 0.14) }],
             Math.max(1, Math.round(2 * z)), cols.right);
  }

  /** 循环水池：下沉水面 + 池壁 + 栏杆 + 溢流堰；夜里水面反灯。 */
  function drawBasinModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(8, Math.round(m.hw * z));
    var hh = Math.max(5, Math.round(m.hh * z));
    var wall = Math.max(2, Math.round((m.h || 3) * z));
    var concrete = roleCols(pal, "concrete", m.bg);
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    // 池壁：外圈台阶 + 内壁两片暗面
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, wall, concrete.top, concrete.left, concrete.right);
    var iw = Math.max(4, hw - Math.max(2, Math.round(5 * z)));
    var ih = Math.max(3, hh - Math.max(1, Math.round(2.5 * z)));
    fillDiamond(ctx, p.x, baseY - wall, iw, ih, m.bg ? pal.bgSteelDark : pal.glassDark);
    fillDiamond(ctx, p.x, baseY - wall - Math.max(1, Math.round(2 * z)),
                Math.max(2, iw - vlen(3)), Math.max(2, ih - vlen(2)), pal.glass);
    // 涟漪：两道横向淡线 + 一道高光
    ctx.globalAlpha = 0.55;
    linePath(ctx, [{ x: p.x - iw * 0.6, y: baseY - wall - vlen(4) },
                  { x: p.x + iw * 0.2, y: baseY - wall - vlen(5) }], 1, pal.glassLight);
    linePath(ctx, [{ x: p.x - iw * 0.1, y: baseY - wall - vlen(7) },
                  { x: p.x + iw * 0.62, y: baseY - wall - vlen(7) }], 1, pal.glassLight);
    ctx.globalAlpha = 1;
    // 溢流堰：一角的小方口 + 侧向排水管
    var weirX = p.x + Math.round(iw * 0.58);
    ctx.fillStyle = concrete.top;
    ctx.fillRect(weirX - vlen(4), baseY - wall - vlen(2), vlen(8), Math.max(1, Math.round(1.5 * z)));
    linePath(ctx, [{ x: p.x + hw - 1, y: baseY - wall - vlen(2) },
                  { x: p.x + hw + vlen(4), y: baseY - wall - vlen(2) },
                  { x: p.x + hw + vlen(4), y: baseY + 1 }], Math.max(1, vlen(2)), steelDark);
    // 栏杆：沿左右两缘（前缘留出检修口）
    for (var ri = 0; ri <= 5; ri += 1) {
      var t = ri / 5;
      ctx.fillStyle = steelDark;
      ctx.fillRect(Math.round(p.x - hw + hw * t), Math.round(baseY - wall + ih * t - vlen(4)), 1, vlen(4));
      ctx.fillRect(Math.round(p.x + hw - hw * t), Math.round(baseY - wall + ih * t - vlen(4)), 1, vlen(4));
    }
    ctx.globalAlpha = 0.85;
    linePath(ctx, [{ x: p.x - hw, y: baseY - wall - vlen(4) },
                  { x: p.x, y: baseY - wall + ih - vlen(4) }], 1, steelLight);
    linePath(ctx, [{ x: p.x, y: baseY - wall + ih - vlen(4) },
                  { x: p.x + hw, y: baseY - wall - vlen(4) }], 1, steelLight);
    ctx.globalAlpha = 1;
    if (S.night) {
      ctx.globalAlpha = 0.35;
      fillOval(ctx, p.x + Math.round(iw * 0.2), baseY - wall - vlen(3), vlen(10), vlen(3), pal.window);
      ctx.globalAlpha = 1;
    }
  }

  /** 装车台：混凝土台 + 四柱顶棚 + 两根鹤管 + 计量牌，棚下停罐车。 */
  function drawLoadingRackModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(8, Math.round(m.hw * z));
    var hh = Math.max(4, Math.round(m.hh * z));
    var h = Math.max(10, Math.round(m.h * z));
    var cols = roleCols(pal, "steel", m.bg);
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var postW = Math.max(1, Math.round(2 * z));
    // 装车台面
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, Math.max(1, Math.round(2 * z)),
                 pal.stoneLight, pal.stone, pal.stoneDark);
    // 四根立柱（前后各两根，前柱画在顶棚之后）
    var legs = [[-0.84, 0.72, false], [0.84, 0.72, false], [-0.84, -0.78, true], [0.84, -0.78, true]];
    legs.forEach(function (lg) {
      if (lg[2]) return;
      var lx = p.x + Math.round(hw * lg[0]), ly = baseY + Math.round(hh * lg[1]);
      ctx.fillStyle = steelDark;
      ctx.fillRect(lx, ly - h, postW, h);
      ctx.fillStyle = steelLight;
      ctx.fillRect(lx, ly - h, postW, 1);
    });
    // 顶棚：菱形板 + 檐口 + 警示斜纹
    drawIsoBoxAt(ctx, p.x, baseY - h, hw, hh, Math.max(1, Math.round(2 * z)),
                 cols.top, cols.left, cols.right);
    linePath(ctx, [{ x: p.x - hw, y: baseY - h }, { x: p.x, y: baseY - h + hh },
                  { x: p.x + hw, y: baseY - h }], Math.max(1, Math.round(z)), steelLight);
    ctx.fillStyle = pal.warn;
    for (var stripe = 0; stripe < 3; stripe += 1) {
      var sx = p.x - Math.round(hw * 0.5) + stripe * Math.max(4, Math.round(hw * 0.32));
      ctx.fillRect(sx, baseY - h + Math.round(hh * 0.82), Math.max(2, Math.round(3 * z)), 1);
      ctx.fillStyle = pal.ink;
      ctx.fillRect(sx + Math.max(2, Math.round(2 * z)), baseY - h + Math.round(hh * 0.82), 1, 1);
      ctx.fillStyle = pal.warn;
    }
    // 前柱（压在顶棚上）
    legs.forEach(function (lg) {
      if (!lg[2]) return;
      var lx = p.x + Math.round(hw * lg[0]), ly = baseY + Math.round(hh * lg[1]);
      ctx.fillStyle = steelDark;
      ctx.fillRect(lx, ly - h, postW, h);
      ctx.fillStyle = steelLight;
      ctx.fillRect(lx, ly - h, postW, 1);
    });
    // 鹤管：两根从棚底垂到罐口高度，带阀门轮与软管弯头
    [-0.34, 0.30].forEach(function (off) {
      var ax = p.x + Math.round(hw * off);
      var armTop = baseY - h + vlen(2);
      var armBottom = baseY - Math.round(h * 0.42);
      linePath(ctx, [{ x: ax, y: armTop }, { x: ax, y: armBottom }], Math.max(1, Math.round(2 * z)), steelDark);
      linePath(ctx, [{ x: ax - 1, y: armTop }, { x: ax - 1, y: armBottom }], 1, steelLight);
      linePath(ctx, [{ x: ax, y: armBottom }, { x: ax + Math.round(hw * 0.14), y: armBottom }],
               Math.max(1, Math.round(2 * z)), steelDark);
      fillOval(ctx, ax, armBottom - vlen(4), vlen(2), Math.max(1, vlen(2)), pal.rust);
      ctx.fillStyle = pal.warn;
      ctx.fillRect(ax - 1, armBottom + 1, 2, 1);
    });
    // 计量牌：台侧的立牌
    var signX = p.x + Math.round(hw * 0.92), signY = baseY + Math.round(hh * 0.4);
    ctx.fillStyle = steelDark;
    ctx.fillRect(signX, signY - vlen(7), 1, vlen(7));
    ctx.fillStyle = pal.mark;
    ctx.fillRect(signX - vlen(3), signY - vlen(9), vlen(6), vlen(4));
    ctx.fillStyle = pal.paintDark;
    ctx.fillRect(signX - vlen(2), signY - vlen(8), vlen(4), 1);
    if (S.night) {
      ctx.globalAlpha = 0.34;
      fillOval(ctx, p.x, baseY - Math.round(h * 0.6), vlen(16), vlen(7), pal.window);
      ctx.globalAlpha = 1;
      ctx.fillStyle = pal.window;
      ctx.fillRect(p.x - Math.round(hw * 0.3), baseY - h + 1, 2, 1);
    }
  }

  /** 火炬塔：细高塔身 + 塔顶燃烧头 + 三只引火管，昼夜都有火苗与烟柱。 */
  function drawFlareModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(16, Math.round(m.h * z));
    var steel = m.bg ? pal.bgSteel : pal.steel;
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var metalDark = m.bg ? pal.bgSteelDark : pal.metalDark;
    // 基础墩 + 三个地脚
    drawIsoBoxAt(ctx, p.x, baseY, vlen(m.hw || 7), vlen(m.hh || 3.5), Math.max(1, Math.round(2 * z)),
                 pal.stoneLight, pal.stone, pal.stoneDark);
    // 塔身：三段收细（下粗上细），每段一条法兰
    var segs = [
      { w: 2.2, top: 0.42 }, { w: 1.9, top: 0.74 }, { w: 1.6, top: 1.0 },
    ];
    var prev = 0;
    segs.forEach(function (sg) {
      var w = Math.max(1, Math.round(sg.w * z));
      var from = baseY - Math.round(h * prev), to = baseY - Math.round(h * sg.top);
      ctx.fillStyle = steelDark;
      ctx.fillRect(p.x - w, to, w * 2, from - to);
      ctx.fillStyle = steelLight;
      ctx.fillRect(p.x - w, to, Math.max(1, w), from - to);
      ctx.fillStyle = steelDark;
      ctx.fillRect(p.x - w - 1, to, (w + 1) * 2, Math.max(1, Math.round(z)));
      prev = sg.top;
    });
    // 检修平台 + 爬梯
    var deck = baseY - Math.round(h * 0.62);
    linePath(ctx, [{ x: p.x - vlen(5), y: deck }, { x: p.x + vlen(5), y: deck }],
             Math.max(1, Math.round(1.4 * z)), steelLight);
    linePath(ctx, [{ x: p.x + vlen(3), y: baseY }, { x: p.x + vlen(3), y: baseY - Math.round(h * 0.95) }],
             1, metalDark);
    for (var rung = 3; rung < h * 0.9; rung += Math.max(3, Math.round(4 * z))) {
      linePath(ctx, [{ x: p.x + vlen(3), y: baseY - rung * z }, { x: p.x + vlen(5), y: baseY - rung * z }],
               1, steelLight);
    }
    // 燃烧头：略宽的短节 + 三只引火管
    var tipY = baseY - h;
    var tipW = Math.max(2, Math.round(2.6 * z));
    ctx.fillStyle = steelDark;
    ctx.fillRect(p.x - tipW, tipY - Math.max(2, Math.round(3 * z)), tipW * 2, Math.max(2, Math.round(3 * z)));
    ctx.fillStyle = steelLight;
    ctx.fillRect(p.x - tipW, tipY - Math.max(2, Math.round(3 * z)), tipW, 1);
    [-1, 0, 1].forEach(function (dir) {
      ctx.fillStyle = metalDark;
      ctx.fillRect(p.x + Math.round(dir * tipW * 1.2), tipY - Math.max(2, Math.round(3 * z)) - 1, 1, 2);
    });
    // 火苗：昼夜都烧，夜里更亮更大（外层火色 → 内层亮芯，逐帧轻抖）
    var flick = 1 + Math.sin((S.ticks || 0) * 0.11 + m.x * 0.05) * 0.16;
    var flameH = Math.max(4, Math.round((S.night ? 10 : 7) * z * flick));
    var flameW = Math.max(2, Math.round((S.night ? 4.2 : 3.2) * z * flick));
    var flameY = tipY - Math.max(2, Math.round(3 * z));
    ctx.globalAlpha = S.night ? 0.55 : 0.4;
    fillOval(ctx, p.x, flameY - Math.round(flameH * 0.4), Math.round(flameW * 1.5),
             Math.round(flameH * 0.8), pal.warn);
    ctx.globalAlpha = S.night ? 0.75 : 0.5;
    fillOval(ctx, p.x, flameY - Math.round(flameH * 0.35), flameW, Math.round(flameH * 0.6), pal.rustLight);
    ctx.globalAlpha = 1;
    fillOval(ctx, p.x, flameY - Math.round(flameH * 0.3), Math.max(1, Math.round(flameW * 0.5)),
             Math.max(1, Math.round(flameH * 0.34)), S.night ? pal.window : pal.mark);
    // 烟柱：从火苗往上飘，越远越淡（不用渐变，纯色叠 alpha）
    for (var s = 1; s <= 3; s += 1) {
      ctx.globalAlpha = 0.22 / s;
      fillOval(ctx, p.x + Math.round(s * 1.6 * z), flameY - Math.round(flameH + s * 7 * z),
               Math.max(1, Math.round((2 + s * 1.6) * z)), Math.max(1, Math.round((2 + s * 1.2) * z)),
               m.bg ? pal.bgSteel : pal.smoke);
    }
    ctx.globalAlpha = 1;
    // 塔脚围栏
    ctx.fillStyle = pal.warn;
    ctx.fillRect(p.x - vlen(6), baseY - vlen(3), vlen(12), 1);
  }

  function drawLampModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(8, Math.round((m.h || 24) * z));
    var arm = Math.max(3, Math.round(4 * z));
    fillOval(ctx, p.x, baseY + 1, Math.max(2, Math.round(3 * z)), 1, pal.shadow);
    ctx.fillStyle = pal.stoneDark;
    ctx.fillRect(p.x - 1, baseY - 1, 3, 2);
    // 灯杆 + 挑臂 + 灯头
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x, baseY - h, 1, h);
    ctx.fillRect(p.x - arm, baseY - h, arm + 1, 1);
    ctx.fillStyle = S.night ? pal.window : pal.glassGlow;
    ctx.fillRect(p.x - arm, baseY - h + 1, 2, 1);
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(p.x - arm - 1, baseY - h + 1, 1, 1);
    if (S.night) {
      ctx.globalAlpha = 0.20;
      fillOval(ctx, p.x - arm, baseY - h + 1, Math.max(3, Math.round(4 * z)),
               Math.max(2, Math.round(3 * z)), pal.window);
      ctx.globalAlpha = 0.05;
      fillPoly(ctx, [
        { x: p.x - arm - 1, y: baseY - h + 2 },
        { x: p.x - arm + 2, y: baseY - h + 2 },
        { x: p.x + Math.round(5 * z), y: baseY + 1 },
        { x: p.x - Math.round(6 * z), y: baseY + 1 },
      ], pal.window);
      ctx.globalAlpha = 1;
    }
  }

  function drawReelModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var s = Math.max(3, Math.round(6 * z)) * (m.s || 1);
    var r = Math.max(2, Math.round(s * 0.55));
    fillOval(ctx, p.x, baseY + 1, Math.round(s * 1.1), Math.max(1, Math.round(s * 0.4)), pal.shadow);
    // 侧放线盘：外圈木法兰 + 内圈线缆
    fillOval(ctx, p.x, baseY - r, r, Math.round(r * 0.72), pal.crateDark);
    fillOval(ctx, p.x, baseY - r, Math.max(1, r - 2), Math.max(1, Math.round(r * 0.60)), pal.crate);
    for (var wr = 1; wr <= 3; wr += 1) {
      linePath(ctx, [{ x: p.x - Math.round(r * 0.72), y: baseY - r + wr },
                    { x: p.x + Math.round(r * 0.72), y: baseY - r + wr }], 1, pal.metalDark);
    }
    fillOval(ctx, p.x, baseY - r, Math.max(1, Math.round(r * 0.28)),
             Math.max(1, Math.round(r * 0.20)), pal.crateDark);
    ctx.fillStyle = pal.paint;
    ctx.fillRect(p.x - 1, baseY - r - 1, 2, 1);
  }

  function drawFlagModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(6, Math.round((m.h || 24) * z));
    fillOval(ctx, p.x, baseY + 1, Math.max(2, Math.round(2 * z)), 1, pal.shadow);
    ctx.fillStyle = pal.stoneDark;
    ctx.fillRect(p.x - 1, baseY - 1, 3, 2);
    ctx.fillStyle = pal.metalLight;
    ctx.fillRect(p.x, baseY - h, 1, h);
    var wave = Math.round(Math.sin(S.ticks * 0.35 + m.x) * Math.max(1, 2 * z));
    var fw = Math.max(4, Math.round(9 * z));
    var fh = Math.max(2, Math.round(3 * z));
    ctx.fillStyle = pal.paint;
    ctx.fillRect(p.x + 1, baseY - h + 1 + wave, fw, fh);
    ctx.fillStyle = pal.mark;
    ctx.fillRect(p.x + 1, baseY - h + 1 + wave, fw, 1);
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

  // ---------- 现实天气 ----------
  /** 天气层：整体压暗 / 雾 / 雨 / 雪 / 雷闪。粒子只画在 Canvas 内，
      文字标签仍是 DOM 覆盖层；reduced-motion 下只保留静态色调。 */
  function drawWeather(ctx, pal) {
    var kind = weatherKind();
    if (kind === "clear" || kind === "unknown") return;
    var W = S.bufW, H = S.bufH;
    if (kind !== "fog") {
      ctx.globalAlpha = kind === "thunder" ? 0.20 : (kind === "snow" ? 0.07 : 0.13);
      ctx.fillStyle = S.night ? "rgba(8, 16, 24, 1)" : "rgba(56, 64, 74, 1)";
      ctx.fillRect(0, 0, W, H);
      ctx.globalAlpha = 1;
    }
    if (kind === "fog") {
      var fog = ctx.createLinearGradient(0, H * 0.22, 0, H);
      fog.addColorStop(0, "rgba(200, 205, 205, 0)");
      fog.addColorStop(0.55, S.night ? "rgba(90, 105, 120, 0.20)"
        : "rgba(220, 215, 205, 0.22)");
      fog.addColorStop(1, S.night ? "rgba(70, 85, 100, 0.34)"
        : "rgba(235, 228, 215, 0.34)");
      ctx.fillStyle = fog;
      ctx.fillRect(0, H * 0.22, W, H * 0.78);
    }
    if (kind === "rain" || kind === "drizzle" || kind === "thunder") {
      drawRain(ctx, pal, kind);
    }
    if (kind === "snow") drawSnow(ctx, pal);
    if (kind === "thunder") drawLightning(ctx, pal);
  }

  function drawRain(ctx, pal, kind) {
    if (S.reduced) return;
    var W = S.bufW, H = S.bufH;
    var drizzle = kind === "drizzle";
    var count = drizzle ? 70 : 132;
    ctx.fillStyle = S.night ? pal.glassLight : pal.glassGlow;
    for (var i = 0; i < count; i += 1) {
      var seed = i * 97 + 13;
      var x = (seed * 7 + S.ticks * (drizzle ? 1.1 : 2.4)) % W;
      var y = (seed * 31 + S.ticks * (drizzle ? 3 : 7) + (i % 5) * 17) % H;
      ctx.globalAlpha = 0.14 + (i % 4) * 0.055;
      ctx.fillRect(Math.round(x), Math.round(y), 1, drizzle ? 2 : 4);
      if (!drizzle) {
        ctx.fillRect(Math.round(x) + 1, Math.round(y) + 4, 1, 1);
      }
    }
    ctx.globalAlpha = 1;
  }

  function drawSnow(ctx, pal) {
    if (S.reduced) return;
    var W = S.bufW, H = S.bufH;
    ctx.fillStyle = S.night ? pal.glassLight : "#f4f7fb";
    for (var i = 0; i < 84; i += 1) {
      var drift = Math.sin(S.ticks * 0.045 + i * 0.7) * 9;
      var x = (i * 113 + drift + 23) % W;
      var y = (i * 47 + S.ticks * 1.7) % H;
      ctx.globalAlpha = 0.30 + (i % 3) * 0.16;
      var size = (i % 2) + 1;
      ctx.fillRect(Math.round(x), Math.round(y), size, size);
    }
    ctx.globalAlpha = 1;
  }

  function drawLightning(ctx, pal) {
    var phase = S.ticks % 120;
    if (phase >= 2) return;
    var W = S.bufW, H = S.bufH;
    var skyH = sceneHorizon();
    ctx.globalAlpha = phase === 0 ? 0.22 : 0.10;
    ctx.fillStyle = "#eaf4ff";
    ctx.fillRect(0, 0, W, H);
    ctx.globalAlpha = 0.65;
    ctx.strokeStyle = pal.glassLight;
    ctx.lineWidth = 1;
    var bx = Math.round(W * (0.52 + (S.ticks % 7) * 0.045));
    var by = 0;
    ctx.beginPath();
    ctx.moveTo(bx, by);
    for (var i = 1; i <= 6; i += 1) {
      bx += ((i * 37) % 19) - 9;
      by = Math.round(skyH * (i / 6) + ((i * 13) % 7));
      ctx.lineTo(bx, by);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // ---------- 包裹 ----------
  // ---------- 待发月台（未来排期可视化） ----------
  /** 把「未来 30 分钟会到点的产线」算成一份按 eta 升序的列表。

      以前场景只表达「此刻」：包裹凭空从调度厅冒出来，看不到还没开始的那一段。
      待发月台把 `cron_tasks.next_run_at` 翻译成摆在场院上的静止包裹，于是同一幅画
      同时有「成品（过去）/ 在途（此刻）/ 待发（未来）」三段。eta 用本机时间现算：
      `next_run_at` 是服务端时间戳，用 `Date.now()/1000` 直接减，30 分钟窗口内
      几秒的钟差可以忽略；用快照里的 `state.now` 会在两次拉取之间冻住。 */
  function refreshUpcoming() {
    var st = activeState();
    var out = [];
    if (st && st.projects) {
      var now = Date.now() / 1000;
      st.projects.forEach(function (p) {
        if (p.root_ok === false) return;   // 停工的厂区不会到点派工
        (p.lines || []).forEach(function (l) {
          if (l.kind !== "cron" || l.status !== "idle") return;  // 在跑 / 停工的都不算
          var at = Number(l.next_run_at) || 0;
          if (!at) return;
          var eta = at - now;
          if (eta <= 0 || eta > UPCOMING_WINDOW_S) return;
          out.push({ key: l.key, name: l.name || l.key,
                     project: p.name || "", eta: eta });
        });
      });
    }
    out.sort(function (a, b) { return a.eta - b.eta; });
    S.upcoming = out;
  }

  /** 排队中的任务：按到点时间算「已经等了多久」。

      后端的 `waiting` 是额度之外真正在等的那些（按 next_run_at 升序），每条的
      `due_at` 是它本该开跑的时刻。等待时长用本机时间现算，与待发月台的 eta
      同一套钟口径。返回按等待时长降序（等得最久的排最前，抖得最凶的那个在
      最左边），与后端顺序一致时也稳。 */
  function waitingTier(sec) {
    if (sec >= WAIT_HOT_S) return 2;
    if (sec >= WAIT_WARM_S) return 1;
    return 0;
  }

  function queuedWaits() {
    var st = activeState();
    var rows = (st && st.waiting) || [];
    var now = Date.now() / 1000;
    return rows.map(function (w) {
      var due = Number(w.due_at) || 0;
      var waited = due > 0 ? Math.max(0, now - due) : 0;
      return { key: w.key, name: w.name || w.key, waited: waited,
               tier: waitingTier(waited) };
    }).sort(function (a, b) { return b.waited - a.waited; });
  }

  /** 待发倒计时文案：大于 1 分钟只给分，最近 60 秒精确到秒。 */
  function fmtCountdown(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + " 秒";
    var m = Math.round(sec / 60);
    return m + " 分";
  }

  function refreshFlowingProject(pid) {
    if (pid == null) return;
    var active = false;
    S.packages.forEach(function (pkg) {
      if (!pkg.finish && pkg.line && pkg.line.project
          && String(pkg.line.project.id) === String(pid)) active = true;
    });
    if (active) S.flowingProjects.add(pid);
    else S.flowingProjects.delete(pid);
  }

  function syncPackages(fromSnapshot) {
    var st = activeState();
    if (!st || !S.world) { S.packages.clear(); S.flowingProjects.clear(); return; }
    var running = new Map();
    S.flowingProjects.clear();
    (st.runs || []).forEach(function (r) {
      if (r.status === "running") running.set(r.id, r);
    });
    // 新增
    running.forEach(function (run, id) {
      var line = S.world.lines.get(runKey(run));
      if (!line || !line.drawn) return;
      if (line.project) S.flowingProjects.add(line.project.id);
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

  // ---------- 停滞判定 ----------
  /** 一条运行「卡住了没有」：返回 stale / nearTimeout / timeout 三者之一或空串。

      以前这条语义只存在于左面板的计时数字里——一条挂住的运行要用户自己盯着
      「已跑 47 分」和超时配置做减法才发现。场景本该直接说出来。

      - `stale`：进度事件超过 STALE_S 秒没有更新（与 `liveStale` 的 5 分钟同口径）；
      - `nearTimeout`：已跑超过超时额度的 NEAR_TIMEOUT_RATIO，还没到；
      - `timeout`：已跑超过 `run_timeout_minutes`（0 = 不限，永不超时）。

      进度事件的 `at` 比 `started_at` 更可信：长跑但一直在出活的运行不该被
      判成停滞。没有进度事件时退回 `started_at` 起算。 */
  function runStall(run, now) {
    if (!run || run.status !== "running") return "";
    var nowS = now / 1000;
    var started = Number(run.started_at) || 0;
    if (!started) return "";
    var elapsed = Math.max(0, nowS - started);
    var cfg = activeConfig();
    var limitMin = Number(cfg.run_timeout_minutes);
    if (Number.isFinite(limitMin) && limitMin > 0) {
      var limitS = limitMin * 60;
      if (elapsed >= limitS) return "timeout";
      if (elapsed >= limitS * NEAR_TIMEOUT_RATIO) return "nearTimeout";
    }
    // 停滞：最后一次进度 / 状态更新距今超过阈值。运行刚开始时给足缓冲，
    // 否则一启动就会被判成停滞（模型首字节还没回来）。
    var last = Number(run.progress && run.progress.at) || 0;
    var mark = last > started ? last : started;
    var quiet = Math.max(0, nowS - mark);
    if (quiet >= STALE_S && elapsed >= STALE_S) return "stale";
    return "";
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

  /** 调度厅泊位：把 \`max_parallel_runs\` 这个抽象额度画成看得见的车位。

      待发月台的排队包裹以前「无因」——它们卡在那里，但画面上没有东西解释
      为什么卡住。额度用完就是原因，而它以前只存在于计数条的一个数字里。
      泊位紧贴厅基座南沿：空位是暗色凹槽 + 灰色空闲灯，占用是亮起的凹槽 +
      脉动绿灯。数量随配置变，上限 6 个（再多就挤到月台上去了）。
      画在模型之前：它在地面上，被厅体挡住是应该的。 */
  function drawBerths(ctx, pal) {
    if (!S.world) return;
    var z = S.cam.zoom;
    var hall = S.world.hall;
    var base = viewPoint(hall.x, hall.y);
    var cfg = activeConfig();
    // 与后端 backend_parts/factory.py 的 cap = max(1, int(...)) 同口径：
    // 0 / 负数是无效配置而不是「不限」，用 || 回落 3 会和后端的排队计数对不上。
    var rawCap = Number(cfg.max_parallel_runs);
    var cap = clamp(Math.round(Number.isFinite(rawCap) ? rawCap : 3), 1, 6);
    var st = activeState();
    var running = Number((st && st.counters && st.counters.running) || 0);
    var used = Math.min(cap, running);
    var gap = Math.max(10, Math.round(15 * z));
    var laneY = base.y + Math.round(23 * z);
    var startX = base.x - (cap - 1) * gap / 2;
    var hw = Math.max(3, Math.round(4.5 * z));
    var hh = Math.max(1, Math.round(2.2 * z));
    for (var i = 0; i < cap; i += 1) {
      var x = Math.round(startX + i * gap);
      var busy = i < used;
      // 凹槽本体：占用的比空闲的深一档，边框才是状态色
      fillDiamond(ctx, x, laneY, hw, hh, busy ? pal.steelDark : pal.ink);
      linePath(ctx, ringPts(x, laneY, hw, hh),
               Math.max(1, Math.round(z)), busy ? pal.ok : pal.metalDark);
      if (busy) {
        var pulse = S.reduced ? 0.75 : 0.5 + 0.4 * Math.abs(Math.sin(S.ticks * 0.12 + i));
        ctx.globalAlpha = pulse;
        fillOval(ctx, x, laneY - Math.max(1, Math.round(hh * 0.4)),
                 Math.max(1, Math.round(1.6 * z)), Math.max(1, Math.round(1 * z)), pal.ok);
        ctx.globalAlpha = 1;
      } else {
        ctx.fillStyle = pal.idle;
        ctx.fillRect(x - 1, laneY - Math.max(1, Math.round(hh * 0.4)), 2, 1);
      }
    }
    // 排布留给标签层：厅名旁边报「额度 used/cap」，只在有运行时出现
    S.berthLane = used ? { cap: cap, used: used } : null;
  }

  /** 待发月台：调度厅前沿摆一排静止包裹，越近越靠前。

      位置以调度厅为中心向外排，避开四条主干道（那些是 belt 起点）——月台在
      厅的正下方（屏幕南侧），是等距视角下唯一不会被厅体自身挡住的方位。
      排队（到点但没抢到并发额度）的包裹画在月台出口，用警示色与轻微抖动
      表示「卡住了」，这就是计数条上「排队 N」的可视化。 */
  function drawUpcoming(ctx, pal) {
    if (!S.world) return;
    var z = S.cam.zoom;
    var hall = S.world.hall;
    var base = viewPoint(hall.x, hall.y);
    // 由厅向外分三层：泊位(23) → 排队包裹(33) → 待发包裹(44)。排队紧贴泊位外侧
    // 才读得出「额度占满、这些在等」，待发在更外面是还没轮到的。以前三者挤在
    // 26 那一层，泊位一加就全叠在一起了。
    var queuedY = base.y + Math.round(33 * z);
    var laneY = base.y + Math.round(44 * z);
    var gap = Math.max(9, Math.round(17 * z));
    var s = Math.max(2, Math.round(4 * z));
    var lift = Math.round(9 * z);
    var list = S.upcoming || [];

    // 排队数来自计数条；明细来自 waiting（额度之外真正在等的那几条）。
    // 只有计数没有明细时（旧快照 / 编排节点排队）退回按计数画，不编造等待时长。
    var st = activeState();
    var queued = Number(st && st.counters && st.counters.queued) || 0;
    var waits = queuedWaits();
    var queuedDraw = Math.min(QUEUED_MAX_DRAW, Math.max(queued, waits.length));
    var shownCount = Math.min(UPCOMING_MAX, list.length);
    var total = Math.max(shownCount, queuedDraw);
    // 先清掉上一帧的排布：月台空了还留着旧坐标，标签会画在空场院上
    S.upcomingLane = null;
    S.queuedLane = null;
    if (!total) return;
    // 整排从厅中线向两侧铺开，保证不以厅顶为中心偏左
    var startX = base.x - (total - 1) * gap / 2;

    function box(x, y, body, edge, alpha) {
      ctx.globalAlpha = alpha == null ? 1 : alpha;
      ctx.fillStyle = pal.shadow;
      fillOval(ctx, x, y + 1, Math.max(1, Math.round(s * 0.8)),
               Math.max(1, Math.round(s * 0.35)), pal.shadow);
      ctx.fillStyle = edge;
      ctx.fillRect(x - s, y - s, s * 2, s * 2);
      ctx.fillStyle = body;
      ctx.fillRect(x - s + 1, y - s + 1, Math.max(1, s * 2 - 2), Math.max(1, s * 2 - 2));
      ctx.fillStyle = pal.crateLight;
      ctx.fillRect(x - s + 1, y - s + 1, Math.max(1, s * 2 - 2), 1);
      ctx.globalAlpha = 1;
    }

    var i = 0;
    for (; i < shownCount; i += 1) {
      var item = list[i];
      var x = Math.round(startX + i * gap);
      var y = Math.round(laneY) - lift;
      // 最近的几枚偏暖，越远越淡：一眼看出先后顺序
      var warmth = i === 0 ? pal.product : (i < 3 ? pal.crate : pal.beltMark);
      box(x, y, warmth, pal.crateDark, i < 3 ? 1 : 0.72);
      S.hits.push({ type: "upcoming", key: item.key, x: x, y: y,
                    r: Math.max(5, s * 1.6) });
    }
    // 排队包裹：紧贴泊位外侧那一层，警示色 + 抖动；reduced-motion 下不抖。
    // 它们与待发包裹不在同一行：同一行会让人以为是同一批，而这里差的是
    // 「已经到点但没抢到额度」与「还没到点」两种完全不同的处境。
    for (var q = 0; q < queuedDraw; q += 1) {
      var qx = Math.round(startX + q * gap);
      var wait = waits[q];
      // 没有明细的那几枚按「刚到点」画：静止、警示色，不假装它等了很久
      var tier = wait ? WAIT_TIERS[wait.tier] : WAIT_TIERS[0];
      var wob = 0;
      if (!S.reduced && tier.amp > 0) {
        wob = Math.sin(S.ticks * tier.freq + q) * Math.max(tier.amp * 0.6, tier.amp * z);
      }
      var body = pal[tier.tone] || pal.warn;
      box(qx, Math.round(queuedY - lift + wob), body, pal.ink, 1);
      S.hits.push({ type: "queued", key: wait ? wait.key : "",
                    x: qx, y: Math.round(queuedY - lift), r: Math.max(5, s * 1.6) });
    }
    // 排队层的位置与「等最久的那条」交给标签层：标签不自己再算一遍坐标，
    // 否则有排队包裹时整排左移，标签会落在空处。
    S.queuedLane = queuedDraw ? {
      startX: startX, y: queuedY - lift,
      top: waits.length ? waits[0] : null,
    } : null;
    // 排布交给标签层复用：标签不能自己再算一遍 startX——绘制时如果有排队包裹，
    // 整排会左移，标签按另一套公式算就会与箱体错开一格。
    S.upcomingLane = {
      startX: startX, y: laneY - lift, gap: gap,
      count: shownCount,
      overflow: Math.max(0, list.length - UPCOMING_MAX) + Math.max(0, queued - queuedDraw),
    };
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
    var lift = 12 * z;
    var color = pkg.color === 0 ? pal.crate : pkg.color === 1 ? pal.steelLight : pal.product;
    var edge = pal.crateDark;
    if (pkg.finish === "error") { color = pal.bad; edge = pal.ink; }
    else if (pkg.finish === "empty") { color = pal.beltMark; edge = pal.beltDark; }
    else if (pkg.finish === "interrupted" || pkg.finish === "skipped") { color = pal.idle; edge = pal.ink; }
    else if (pkg.finish === "done") { color = pal.product; edge = pal.crateDark; }
    // 停滞：包裹还在途，但这条运行已经不对劲了。表现分三档，都是画面变化，
    // 不叠任何数字——它要说的是「这条别等了」，具体多久在抽屉里。
    var stall = pkg.finish ? "" : runStall(pkg.run, now);
    if (stall === "stale") {
      // 卡住：包裹原地打转（画成轻微左右偏），箱体压暗一档
      color = pal.idle;
      edge = pal.ink;
    } else if (stall === "nearTimeout") {
      // 快超时：箱体开始发烫，边缘转锈色
      color = pal.rustLight;
      edge = pal.rustDark;
    } else if (stall === "timeout") {
      // 已超时：焦黑
      color = pal.ink;
      edge = pal.ink;
    }
    var gy = Math.round(pos.y);
    var x = Math.round(pos.x), y = gy - Math.round(lift) - s;
    if (stall === "stale" && !S.reduced) {
      // 打转：只晃半格，看得出「卡住了」而不是「在跑」
      x += Math.round(Math.sin(S.ticks * 0.35) * Math.max(1, z));
    }
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
    // 卡住的标记：头顶一个问号，快超时冒一缕烟，已超时压一团黑烟。
    // 全是像素图形、不带文字——问号的形状本身就说明「不知道在等什么」。
    if (stall === "stale") drawStallMark(ctx, pal, x, y - s - Math.max(3, Math.round(5 * z)), z, false);
    else if (stall === "nearTimeout") drawStallMark(ctx, pal, x, y - s, z, true);
    else if (stall === "timeout") drawTimeoutSmoke(ctx, pal, x, y - s, z);
    // 记录点击热区
    S.hits.push({ type: "pkg", id: pkg.id, x: x, y: y, r: Math.max(5, s * 1.6) });
  }

  /** 停滞问号：一个 3×5 像素的问号轮廓，随时间轻微上下浮。 */
  function drawStallMark(ctx, pal, cx, cy, z, isHeat) {
    var bob = S.reduced ? 0 : Math.round(Math.sin(S.ticks * 0.22) * Math.max(0.5, z * 0.6));
    var px = Math.round(cx), py = Math.round(cy + bob);
    ctx.fillStyle = isHeat ? pal.rustLight : pal.bad;
    // 问号的四笔：顶横、右竖、中横、底点
    ctx.fillRect(px - 1, py - 3, 3, 1);
    ctx.fillRect(px + 1, py - 2, 1, 1);
    ctx.fillRect(px, py - 1, 2, 1);
    ctx.fillRect(px, py + 1, 1, 1);
  }

  /** 超时黑烟：几团上飘的暗色点，节奏比烟囱慢、颜色更沉。 */
  function drawTimeoutSmoke(ctx, pal, cx, cy, z) {
    for (var i = 0; i < 4; i += 1) {
      var t = ((S.ticks * 0.05 + i * 0.25) % 1);
      var px = cx + Math.sin(S.ticks * 0.04 + i) * Math.max(1, 3 * z) - t * 2;
      var py = cy - t * Math.max(6, 16 * z);
      var r = Math.max(1, (1.5 + t * 3) * z);
      ctx.globalAlpha = (1 - t) * 0.6;
      fillOval(ctx, Math.round(px), Math.round(py), r, r * 0.75, pal.ink);
    }
    ctx.globalAlpha = 1;
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

  /** 直接按屏幕坐标摆一个标签（不走世界坐标换算，也不参与互压检测）。

      月台上的倒计时彼此只隔一格，走 placeLabel 的碰撞检测会互相压掉；
      它们本来就排成一列，靠间距保证不重叠，不需要那套让位逻辑。 */
  function placeLabelAt(el, sx, sy, text, cls) {
    if (sx < -40 || sy < -20 || sx > S.cssW + 40 || sy > S.cssH + 20) {
      el.style.display = "none";
      return;
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
    var berth = S.berthLane;
    var hallText = "调度厅" + (running ? " · 在转 " + running + " 件" : "");
    if (berth) hallText += " · 额度 " + berth.used + "/" + berth.cap;
    placeLabel(labelFor(i++), S.world.hall.x, S.world.hall.y, hallText, "hall", 76, used);
    // 待发月台：最近三枚写倒计时，再往后的只报个数——全景下每枚都带字会糊成
    // 一排，而「还有几枚在等」本来就是这排包裹要说的话。
    // 位置直接取 drawUpcoming 算好的屏幕坐标：placeLabel 的入参是世界坐标，
    // 且会做名牌互压检测；四枚倒计时起点相同，走它会被第一枚全部压掉。
    var upc = S.upcoming || [];
    var lane = S.upcomingLane;
    if (upc.length && lane) {
      var sx2 = S.cssW / S.bufW, sy2 = S.cssH / S.bufH;
      var shown = Math.min(3, lane.count);
      for (var u = 0; u < shown; u += 1) {
        placeLabelAt(labelFor(i++), (lane.startX + u * lane.gap) * sx2,
                     lane.y * sy2, fmtCountdown(upc[u].eta), "upcoming");
      }
      if (lane.overflow > 0) {
        placeLabelAt(labelFor(i++), (lane.startX + shown * lane.gap) * sx2,
                     lane.y * sy2, "待发 +" + lane.overflow, "upcoming more");
      }
    }
    // 排队最久的那一枚标等待时长：抖动只表达「急不急」，标不出「等了多久」。
    // 只标一枚——整排都带字会把月台糊成一排，而最久的那条才是要处理的那条。
    // 坐标取自 drawUpcoming 记下的排队层，标签才不会和箱体错开。
    var qLane = S.queuedLane;
    if (qLane && qLane.top) {
      var topTier = qLane.top.tier;
      placeLabelAt(labelFor(i++), qLane.startX * (S.cssW / S.bufW),
                   qLane.y * (S.cssH / S.bufH),
                   "等 " + fmtCountdown(qLane.top.waited),
                   topTier >= 2 ? "queued hot" : "queued");
    }
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
    // 机台名：聚焦到某个厂区时一定画（这是聚焦档存在的理由），全景下缩放
    // 够大才画——默认取景里一排小字会糊在一起。
    if (S.focusPid || S.cam.zoom >= (S.fitZoom || 0.82) * 1.15) {
      factories.forEach(function (f) {
        // 聚焦档只画这一个厂区的机台：别的厂区在屏幕外，画了也只是被裁掉
        if (S.focusPid && f.id !== S.focusPid) return;
        f.lines.forEach(function (rec) {
          if (i >= MAX_LABELS - 2) return;
          if (rec._vx == null) return;
          var sx = rec._vx * (S.cssW / S.bufW);
          var sy = rec._vy * (S.cssH / S.bufH);
          if (sx < -60 || sx > S.cssW + 60 || sy < 0 || sy > S.cssH) return;
          var mlabel = rec.line.name || rec.key;
          if (rec.line.status === "running" && rec.kind === "cron") {
            mlabel += " " + fmtClockShort(lineRunSeconds(rec.line)); // 机台上方计时 mm:ss
            var step = runningStepText(rec.line);
            if (step) mlabel += " · " + step;
            var dn = rec.line.progress && Number(rec.line.progress.denied) || 0;
            if (dn) mlabel += " · 被拦 " + dn;
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
        "float" + (f.float.bad ? " bad" : "") + (f.float.warn ? " warn" : ""));
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
  var pointerPress = null;

  function canvasPoint(ev) {
    var rect = dom.canvas.getBoundingClientRect();
    var bx = (ev.clientX - rect.left) * (S.bufW / Math.max(1, rect.width));
    var by = (ev.clientY - rect.top) * (S.bufH / Math.max(1, rect.height));
    return { x: bx, y: by, cx: ev.clientX - rect.left, cy: ev.clientY - rect.top };
  }

  function onPointerDown(ev) {
    if (!dom.canvas || ev.button !== 0) return;
    dom.canvas.setPointerCapture && dom.canvas.setPointerCapture(ev.pointerId);
    pointerPress = { x: ev.clientX, y: ev.clientY, moved: 0, id: ev.pointerId };
  }

  function onPointerMove(ev) {
    if (!dom.canvas) return;
    if (pointerPress) {
      if (ev.pointerId !== pointerPress.id) return;
      var dx = ev.clientX - pointerPress.x;
      var dy = ev.clientY - pointerPress.y;
      pointerPress.moved = Math.max(pointerPress.moved, Math.abs(dx) + Math.abs(dy));
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
    if (!dom.canvas || !pointerPress || ev.pointerId !== pointerPress.id) return;
    var moved = Math.max(pointerPress.moved,
      Math.abs(ev.clientX - pointerPress.x) + Math.abs(ev.clientY - pointerPress.y));
    pointerPress = null;
    if (moved > 5) return;
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
    // 点待发包裹 = 提前看这条产线（与点机台同一个抽屉），不派工：
    // 还没到点的那一轮不该因为一次误触就提前跑起来。
    if (hit && hit.type === "upcoming") {
      openLineDrawer(hit.key, { focus: true });
      return;
    }
    // 点排队包裹 = 看这条产线为什么卡着（抽屉里有状态与排期），同样不派工：
    // 它已经在等了，再派一次只会被后端按 already_running 挡回来。
    if (hit && hit.type === "queued" && hit.key) {
      openLineDrawer(hit.key, { focus: true });
      return;
    }
  }

  function onPointerLeave() {
    if (dom.canvas) dom.canvas.title = "";
    pointerPress = null;
  }

  function onWheel(ev) {
    ev.preventDefault();
    // 场景保持固定全景；滚轮不改变比例，也不带动页面滚动。
  }

  function hitTest(x, y) {
    // 包裹优先（小目标）：在跑的与待发的都是小目标，一起按距离判
    for (var i = S.hits.length - 1; i >= 0; i -= 1) {
      var h = S.hits[i];
      if ((h.type === "pkg" || h.type === "upcoming" || h.type === "queued")
          && Math.hypot(x - h.x, y - h.y) <= h.r) return h;
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
  /** 被拦工具的快照（后端存 JSON；旧行 / 坏数据回落原文），给抽屉一句人话。 */
  function deniedToolsText(run) {
    var raw = String((run && run.denied_tools) || "");
    if (!raw) return "";
    try {
      var obj = JSON.parse(raw);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        return Object.keys(obj).map(function (name) {
          return name + " ×" + Number(obj[name] || 0);
        }).join(" / ");
      }
    } catch (e) { /* 不是 JSON：按原文显示 */ }
    return raw;
  }

  /** 单次运行的 token 用量（未记录时不占位，避免一排「0 in / 0 out」）。 */
  function tokenRow(run) {
    var tin = Number(run && run.in_tokens) || 0;
    var tout = Number(run && run.out_tokens) || 0;
    if (tin <= 0 && tout <= 0) return "";
    var cached = Number(run && run.cached_tokens) || 0;
    var text = fmtTokens(tin) + " in / " + fmtTokens(tout) + " out";
    if (cached > 0) text += "（缓存 " + fmtTokens(cached) + "）";
    return "<dt>用量</dt><dd>" + escapeHtml(text) + "</dd>";
  }

  /** 产线体检：近 7 天成功率 / 耗时 / 连续失败 + 最近 N 次状态色条。 */
  function healthBlock(line) {
    var h = line && line.health;
    if (!h || !h.total) {
      return '<div><div class="dim small">产线体检</div>' +
        '<div class="fx-hint">近 7 天还没有可统计的运行。</div></div>';
    }
    var rate = Math.round((h.success_rate || 0) * 100);
    var parts = [
      "成功率 <b>" + rate + "%</b>（7 天 " + h.total + " 次）",
      "平均 " + fmtDur(h.avg_s),
      "P95 " + fmtDur(h.p95_s),
      "最慢 " + fmtDur(h.max_s),
    ];
    // 「跑完了但被门控拦过」与「用户判定不算」单独说清：它们不拉低状态色条，
    // 但必须拉低成功率，否则这个百分比会一直是绿的
    if (h.partial) parts.push('<span class="fx-warn">有拦截 ' + h.partial + " 次</span>");
    if (h.marked_failed) parts.push('<span class="fx-bad">判定不算 ' + h.marked_failed + " 次</span>");
    if (h.streak >= 2) parts.push('<span class="fx-bad">连续失败 ' + h.streak + " 次</span>");
    var tail = h.tail || [];
    var tailOutcomes = h.tail_outcomes || [];
    var strip = tail.map(function (s, idx) {
      var oc = tailOutcomes[idx] || "";
      // done 但有拦截的格子不能染成成功绿：它是 partial，不是做成
      var cls = (s === "done" && oc === "partial") ? "partial" : s;
      var tip = (RUN_STATUS[s] || s) + (oc ? " · " + (RUN_OUTCOME[oc] || oc) : "");
      return '<i class="' + escapeHtml(cls) + '" title="' + escapeHtml(tip) + '"></i>';
    }).join("");
    return '<div><div class="dim small">产线体检</div>' +
      '<div class="fx-health">' + parts.join(" · ") + "</div>" +
      '<div class="fx-strip" title="最近 ' + tail.length + ' 次运行（新 → 旧）">' + strip + "</div></div>";
  }

  function drawerShell(title, bodyHtml) {
    return '<div class="fx-drawer-head"><h3>' + title + "</h3>" +
      '<button class="fx-drawer-close" title="关闭（Esc）">✕</button></div>' +
      '<div class="fx-drawer-body">' + bodyHtml + "</div>";
  }

  function openDrawer(html) {
    if (!dom.drawer) return;
    var wasHidden = dom.drawer.classList.contains("hidden");
    dom.drawer.innerHTML = html;
    if (wasHidden) dom.drawer.classList.remove("factory-entered");
    dom.drawer.classList.remove("hidden");
    if (wasHidden) {
      void dom.drawer.offsetWidth;
      dom.drawer.classList.add("factory-entered");
    }
    var close = dom.drawer.querySelector(".fx-drawer-close");
    if (close) {
      close.onclick = closeDrawer;
      // 键盘用户从面板 / 画布打开抽屉后，焦点要跟着进来（Esc 关闭、Tab 从关闭键开始）；
      // 程序化刷新（事件到达 / live 轮询）不抢焦点。
      if (wasHidden) close.focus();
    }
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
    // 聚焦档里 Esc 先回全景；工具栏的「全景」按钮是同一出口（键盘 / 鼠标都行）
    if (S.focusPid) { fitCamera(true); return true; }
    return false;
  }

  function openRunDrawer(id) {
    var run = findRun(id);
    if (!run) return;
    S.drawer = { type: "run", id: id };
    var status = run.status || "";
    var denied = Number(run.denied_count) || 0;
    var deniedTools = deniedToolsText(run);
    var outcome = run.outcome || "";
    var html = '<div class="fx-kv">' +
      "<dt>包裹</dt><dd>#" + escapeHtml(String(run.id)) + "</dd>" +
      "<dt>产线</dt><dd>" + escapeHtml(runTitle(run)) + "</dd>" +
      "<dt>厂区</dt><dd>" + escapeHtml(run.project_name || "—") + "</dd>" +
      "<dt>触发</dt><dd>" + escapeHtml(run.kind === "pipeline_node" && run.trigger === "schedule"
        ? "编排自动推进" : (TRIGGER_TEXT[run.trigger] || run.trigger || "—")) + "</dd>" +
      "<dt>状态</dt><dd><span class=\"fx-status " + escapeHtml(status) + "\">" +
        escapeHtml(RUN_STATUS[status] || status || "—") + "</span>" +
        (outcome
          ? " " + outcomeChip(run) + '<span class="fx-run-meta">' +
            (run.outcome_source === "user" ? "（你判定的）" : "（自动判定）") + "</span>"
          : "") + "</dd>" +
      "<dt>开始</dt><dd>" + escapeHtml(fmtTs(run.started_at)) + "</dd>" +
      "<dt>结束</dt><dd>" + escapeHtml(fmtTs(run.finished_at)) + "</dd>" +
      "<dt>耗时</dt><dd>" + escapeHtml(fmtDur(runElapsed(run))) + "</dd>" +
      (denied > 0
        ? "<dt>被拦</dt><dd class=\"fx-bad\">" + denied + " 次" +
          (deniedTools ? "：" + escapeHtml(deniedTools) : "") +
          "<br><span class=\"fx-run-meta\">无人值守门控拒绝了写入 / 执行类工具，" +
          (run.kind === "cron" ? "产出可能只是模型的绕行说明；点下方「去编辑」核对预授权工具"
            : "产出可能只是模型的绕行说明；点下方「查看编排」核对节点预授权，修改可在对话中交给 Agent 处理") + "</span></dd>"
        : "") +
      tokenRow(run) +
      (status === "running"
        ? "<dt>在跑</dt><dd id=\"fx-run-progress\">" +
          escapeHtml(progressText(run.progress) || "启动中") + "</dd>"
        : "") +
      "</div>";
    if (run.error) {
      var kind = run.error_kind ? (ERROR_KIND[run.error_kind] || run.error_kind) : "";
      html += "<div><div class=\"dim small\">错误" +
        (kind ? " · <span class=\"fx-bad\">" + escapeHtml(kind) + "</span>" : "") +
        "</div><pre class=\"fx-excerpt\">" + escapeHtml(run.error) + "</pre></div>";
    }
    if (run.result_excerpt) {
      html += "<div><div class=\"dim small\">结果摘要</div><pre class=\"fx-excerpt\">" +
        escapeHtml(run.result_excerpt) + "</pre></div>";
    }
    html += '<div class="fx-drawer-ops">';
    if (run.session_id) {
      html += '<button class="btn-primary" data-open-session="' + escapeHtml(String(run.session_id)) + '">打开会话</button>';
    }
    if (run.result_path) {
      html += '<button class="btn-ghost" data-open-result="' + escapeHtml(String(run.result_path)) +
        '" title="打开这个工位的产出全文">打开产出</button>';
    }
    if (status !== "running") {
      // 人工判定与自动判定分开记：跑完了不等于做成了，只有用户知道产出对不对
      html += '<button class="btn-ghost" data-mark="ok">这轮算过</button>';
      html += '<button class="btn-ghost" data-mark="failed">不算</button>';
      if (run.outcome_source === "user") {
        html += '<button class="btn-ghost" data-mark="">撤销判定</button>';
      }
    }
    if (run.status === "running" && run.kind === "cron") {
      html += '<button class="btn-ghost fx-danger" data-cancel-task="' +
        escapeHtml(String(run.task_id)) + '">叫停</button>';
    }
    if (run.kind === "cron" && run.project_id != null) {
      html += '<button class="btn-ghost" data-edit-task="' + escapeHtml(String(run.task_id)) +
        '" data-edit-project="' + escapeHtml(String(run.project_id)) +
        '" title="切到该厂区并打开自动化面板，直接改提示词 / 补预授权工具">去编辑</button>';
    }
    if (run.kind === "pipeline_node" && run.pipeline_id && run.project_id != null) {
      html += '<button class="btn-ghost" data-view-pipeline="1">查看编排</button>';
    }
    html += '<button class="btn-ghost" data-close-drawer="1">关闭</button></div>';
    openDrawer(drawerShell("运行详情", html));
    if (!dom.drawer) return;
    var pipeBtn = dom.drawer.querySelector("[data-view-pipeline]");
    if (pipeBtn) pipeBtn.onclick = function () {
      closeDrawer();
      openPipelineFor(run.project_id, run.pipeline_id, run.task_id);
    };
    var cancelBtn = dom.drawer.querySelector("[data-cancel-task]");
    if (cancelBtn) {
      cancelBtn.onclick = function () {
        cancelRun(Number(cancelBtn.dataset.cancelTask), run.name, cancelBtn);
      };
    }
    var resultBtn = dom.drawer.querySelector("[data-open-result]");
    if (resultBtn) {
      resultBtn.onclick = function () { openFactoryResult(resultBtn.dataset.openResult); };
    }
    var editBtn = dom.drawer.querySelector("[data-edit-task]");
    if (editBtn) {
      editBtn.onclick = function () {
        closeDrawer();
        if (typeof openAutomationFor === "function") {
          openAutomationFor(Number(editBtn.dataset.editProject),
                            Number(editBtn.dataset.editTask));
        }
      };
    }
    dom.drawer.querySelectorAll("[data-mark]").forEach(function (b) {
      b.onclick = function () { markRunOutcome(run, b.dataset.mark, b); };
    });
    var openBtn = dom.drawer.querySelector("[data-open-session]");
    if (openBtn) {
      openBtn.onclick = function () {
        // 会话 id 是 12 位十六进制串（uuid 前缀），不是数字——以前 Number() 一下
        // 变成 NaN，工厂里「打开会话」其实一直是坏的
        var sid = String(openBtn.dataset.openSession || "");
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
    renderPanel();
    S.drawer = { type: "line", key: key };
    var next = line.next_run_at ? fmtTs(line.next_run_at) : "—";
    var html = '<div class="fx-kv">' +
      "<dt>厂区</dt><dd>" + escapeHtml(rec.project.name || "—") + "</dd>" +
      "<dt>类型</dt><dd>" + (line.kind === "pipeline" ? "装配线" : "单机产线") + "</dd>" +
      "<dt>触发</dt><dd>" + escapeHtml(line.trigger_text || "—") + "</dd>" +
      "<dt>下次</dt><dd>" + escapeHtml(next) + "</dd>" +
      "<dt>状态</dt><dd>" + escapeHtml(lineStateText(line)) + "</dd>" +
      (line.status === "running"
        ? "<dt>在跑</dt><dd id=\"fx-progress\">" +
          escapeHtml(progressText(line.progress) || "启动中") + "</dd>"
        : "") +
      "</div>";
    if (line.kind === "pipeline" && (line.nodes || []).length) {
      html += "<div><div class=\"dim small\">工位</div><div class=\"fx-run-list\">";
      line.nodes.slice(0, 20).forEach(function (n) {
        html += '<div class="fx-run-row"><span class="fx-run-name">' + escapeHtml(n.title || ("工位 " + n.id)) +
          '</span><span class="fx-run-meta">' + escapeHtml(nodeStatusText(n.status)) + "</span></div>";
      });
      html += "</div></div>";
    }
    html += healthBlock(line);
    if (line.kind === "pipeline") html += '<button class="btn-ghost" data-view-pipeline="1">查看编排</button>';
    html += '<div class="fx-drawer-ops">';
    if (line.kind === "cron") {
      html += '<button class="btn-primary" data-run-now="1">立即运行</button>';
      html += '<button class="btn-ghost" data-toggle-line="1">' +
        (line.status === "paused" || line.status === "stopped" ? "启用产线" : "暂停产线") + "</button>";
      if (line.status === "running") {
        html += '<button class="btn-ghost fx-danger" data-cancel-line="1">叫停</button>';
      }
      // 看到「上次失败」之后最常见的下一步：改提示词、补预授权工具。
      // 以前要返回对话 → 切项目 → 开自动化面板 → 找任务四步，现在一步。
      html += '<button class="btn-ghost" data-edit-line="1" ' +
        'title="切到这个厂区并打开自动化面板，改提示词或预授权工具">去编辑</button>';
    }
    html += '<button class="btn-ghost" data-close-drawer="1">关闭</button></div>';
    html += '<div><div class="dim small">最近运行</div><div id="fx-line-runs" class="fx-run-list">' +
      '<div class="fx-run-empty">读取中…</div></div></div>';
    openDrawer(drawerShell(escapeHtml(line.name || key), html));
    if (!dom.drawer) return;

    var pipeBtn = dom.drawer.querySelector("[data-view-pipeline]");
    if (pipeBtn) pipeBtn.onclick = function () { closeDrawer(); openPipelineFor(rec.project.id, line.id); };
    var runBtn = dom.drawer.querySelector("[data-run-now]");
    if (runBtn) {
      runBtn.onclick = function () { runLine(line, runBtn); };
    }
    var toggleBtn = dom.drawer.querySelector("[data-toggle-line]");
    if (toggleBtn) {
      toggleBtn.onclick = function () { toggleLine(line, toggleBtn); };
    }
    var cancelBtn = dom.drawer.querySelector("[data-cancel-line]");
    if (cancelBtn) {
      cancelBtn.onclick = function () { cancelRun(line.id, line.name, cancelBtn); };
    }
    var editBtn = dom.drawer.querySelector("[data-edit-line]");
    if (editBtn) {
      editBtn.onclick = function () {
        closeDrawer();
        if (typeof openAutomationFor === "function") {
          openAutomationFor(rec.project.id, line.id);
        }
      };
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
    else { params.task_id = line.id; params.kind = "cron"; }
    request("factory.runs", params).then(function (r) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
      if (!host || host !== box) return;
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
      if (host && host === box) host.innerHTML = '<div class="fx-run-empty">读取失败：' + escapeHtml(e.message || "") + "</div>";
    });
  }

  function fmtDurationShort(run) {
    if (run.status === "running") return "运行中";
    return fmtDur(runElapsed(run));
  }

  /** 运行行的名字兜底：产线被删后 name 是空串，列表里不能渲染成「厂区 / 」。 */
  function runTitle(run) {
    return run.name || "（已删除的产线）";
  }

  /** 「做成」判定的小标签（空 = 还没有结论，不占位）。 */
  function outcomeChip(run) {
    var oc = run.outcome || "";
    if (!oc) return "";
    var cls = oc === "ok" ? "ok" : (oc === "failed" ? "bad" : "warn");
    var tip = (run.outcome_source === "user" ? "你手动判定" : "自动判定") + "：" + (RUN_OUTCOME[oc] || oc);
    return '<i class="fx-outcome ' + cls + '" title="' + escapeHtml(tip) + '">' +
      escapeHtml(RUN_OUTCOME[oc] || oc) + "</i>";
  }

  var RUN_PAGE = 30;

  /** 全部运行：状态 / 厂区筛选 + 分页 + 行内打开产出。

      后端 factory.runs 一直支持 status / project_id / offset，前端以前只拉
      固定的 60 条平铺——第 61 条以后的历史在工厂里根本翻不到。
   */
  function openAllRunsDrawer(initial) {
    S.drawer = { type: "all" };
    // 每次打开从「全部状态 / 全部厂区」开始：S.runsFilter 会跨打开保留，
    // 而下面的 select 总是重建为默认项——只重置 offset 会让列表按上次筛选
    // 查询、界面却显示「全部」，是最容易让人误判历史的一条。
    initial = initial || {};
    S.runsFilter = { status: initial.status || "", project_id: 0, offset: 0, view: initial.view || "all" };
    var st = activeState();
    var projects = (st && st.projects) || [];
    var projOpts = ['<option value="0">全部厂区</option>'].concat(projects.map(function (p) {
      return '<option value="' + escapeHtml(String(p.id)) + '">' +
        escapeHtml(p.name || ("项目 " + p.id)) + "</option>";
    })).join("");
    var statusOpts = [
      ["", "全部状态"], ["running", "运行中"], ["done", "已完成"], ["empty", "空产出"],
      ["error", "失败"], ["interrupted", "中断"], ["skipped", "跳过"], ["cancelled", "已取消"],
    ].map(function (x) {
      return '<option value="' + x[0] + '">' + x[1] + "</option>";
    }).join("");
    var html = '<div class="fx-runs-filter">' +
      '<select id="fx-runs-status" aria-label="按状态筛选">' + statusOpts + "</select>" +
      '<select id="fx-runs-project" aria-label="按厂区筛选">' + projOpts + "</select>" +
      "</div>" +
      '<div id="fx-all-runs" class="fx-run-list"><div class="fx-run-empty">读取中…</div></div>' +
      '<div class="fx-runs-pager" id="fx-runs-pager"></div>';
    var viewTitle = { all: "全部运行", done_today: "今日做成", tokens_today: "今日 token" };
    var note = S.runsFilter.view === "done_today"
      ? "本地今天零点后完成，按自动或人工判定的做成口径统计（含旧版完成记录）。"
      : (S.runsFilter.view === "tokens_today" ? "本地今天零点后开始的运行；用量为输入 + 输出，未记录用量的运行记为 0。" : "");
    openDrawer(drawerShell(viewTitle[S.runsFilter.view] || "全部运行",
      (note ? '<p class="fx-form-msg">' + note + "</p>" : "") + html));
    if (!dom.drawer) return;
    var statusSel = dom.drawer.querySelector("#fx-runs-status");
    var projSel = dom.drawer.querySelector("#fx-runs-project");
    if (statusSel) {
      statusSel.value = S.runsFilter.status;
      statusSel.onchange = function () {
        S.runsFilter.status = statusSel.value;
        S.runsFilter.offset = 0;
        loadAllRuns();
      };
    }
    if (projSel) {
      projSel.onchange = function () {
        S.runsFilter.project_id = Number(projSel.value) || 0;
        S.runsFilter.offset = 0;
        loadAllRuns();
      };
    }
    loadAllRuns();
  }

  var runsRequestId = 0;

  function loadAllRuns() {
    var f = Object.assign({}, S.runsFilter);
    var requestId = ++runsRequestId;
    var drawer = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
    var params = { limit: RUN_PAGE, offset: f.offset || 0 };
    params.view = f.view || "all";
    if (f.status) params.status = f.status;
    if (f.project_id) params.project_id = f.project_id;
    request("factory.runs", params).then(function (r) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
      if (!host || host !== drawer || requestId !== runsRequestId) return;
      var rows = (r && r.runs) || [];
      rows.forEach(function (x) { S.runCache.set(x.id, x); });
      if (!rows.length) {
        host.innerHTML = '<div class="fx-run-empty">' +
          (f.offset ? "这一页没有更多了。" : "没有符合条件的运行记录。") + "</div>";
      } else {
        host.innerHTML = rows.map(function (x) {
          var path = x.result_path || "";
          return '<div class="fx-run-row" data-run-id="' + escapeHtml(String(x.id)) + '">' +
            '<span class="fx-run-name">#' + escapeHtml(String(x.id)) + " · " +
            escapeHtml(x.project_name || "—") + " / " + escapeHtml(runTitle(x)) + "</span>" +
            outcomeChip(x) +
            '<span class="fx-run-meta">' + escapeHtml(RUN_STATUS[x.status] || x.status) +
            " · " + escapeHtml(fmtDurationShort(x)) +
            (f.view === "tokens_today" ? " · " + (Number(x.in_tokens || 0) + Number(x.out_tokens || 0)).toLocaleString() + " token" : "") + "</span>" +
            (path
              ? '<button class="fx-ack" data-open-result="' + escapeHtml(path) + '"' +
                ' title="打开这个工位的产出全文">产出</button>'
              : "") +
            "</div>";
        }).join("");
        host.querySelectorAll("[data-run-id]").forEach(function (el) {
          var open = function (ev) {
            if (ev && ev.target.closest("[data-open-result]")) return;
            openRunDrawer(Number(el.dataset.runId));
          };
          el.setAttribute("role", "button");
          el.setAttribute("tabindex", "0");
          el.onclick = open;
          el.onkeydown = function (ev) {
            if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              open(ev);
            }
          };
        });
        host.querySelectorAll("[data-open-result]").forEach(function (b) {
          b.onclick = function (ev) {
            ev.stopPropagation();
            openFactoryResult(b.dataset.openResult);
          };
        });
      }
      var pager = dom.drawer && dom.drawer.querySelector("#fx-runs-pager");
      if (!pager) return;
      var off = f.offset || 0;
      pager.innerHTML =
        '<button class="btn-ghost" data-page="prev"' + (off > 0 ? "" : " disabled") + ">上一页</button>" +
        '<span class="fx-run-meta">第 ' + (Math.floor(off / RUN_PAGE) + 1) + " 页 · "
        + rows.length + " 条</span>" +
        '<button class="btn-ghost" data-page="next"' + (rows.length >= RUN_PAGE ? "" : " disabled") + ">下一页</button>";
      pager.querySelectorAll("[data-page]").forEach(function (b) {
        b.onclick = function () {
          if (b.disabled) return;
          S.runsFilter.offset = b.dataset.page === "next"
            ? off + RUN_PAGE : Math.max(0, off - RUN_PAGE);
          loadAllRuns();
        };
      });
    }).catch(function (e) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
      if (host && host === drawer && requestId === runsRequestId) host.innerHTML = '<div class="fx-run-empty">读取失败：' + escapeHtml(e.message || "") + "</div>";
    });
  }

  /** 打开编排节点的产出全文（<项目>/.skysheep/pipeline-results/node-<id>.md）。
      路径由后端在 run.result_path 里给出，前端不拼目录。 */
  function openFactoryResult(path) {
    if (!path) return;
    closeDrawer();
    if (typeof backToChat === "function") backToChat();
    if (typeof openRightTab === "function") openRightTab("files");
    if (typeof openFile === "function") {
      Promise.resolve(openFile(path)).catch(function (e) {
        addNotice("打开产出失败：" + (e && e.message ? e.message : "未知错误"));
      });
    } else {
      addNotice("当前版本没有文件预览入口");
    }
  }

  /** 「对话在跑」抽屉：只读快照 + 跳到该会话。
      工厂只覆盖定时任务与编排，用户自己发起的对话轮 / 子代理在这里是盲区。 */
  function openLiveDrawer() {
    var st = activeState();
    var live = (st && st.live) || {};
    var rows = live.runs || [];
    S.drawer = { type: "live" };
    var html = "";
    if (!rows.length) {
      html = '<div class="fx-run-empty">当前没有在跑的对话轮或后台子代理。' +
        "工厂只统计定时任务与编排；你在对话里发起的运行会在这里出现。</div>";
    } else {
      html = '<div class="fx-run-list">' + rows.map(function (r) {
        var kind = r.kind === "turn" ? "对话" : "子代理";
        var title = r.title || (r.kind === "turn" ? "对话轮" : "后台任务");
        return '<div class="fx-run-row">' +
          '<span class="fx-run-name">' + escapeHtml(title) + "</span>" +
          '<span class="fx-run-meta">' + escapeHtml(kind) +
          (liveStale(r) ? ' · <span class="fx-bad">5 分钟无更新</span>' : "") + "</span>" +
          (r.session_id
            ? '<button class="fx-ack" data-live-session="' + escapeHtml(String(r.session_id)) +
              '" title="打开这个会话">去会话</button>'
            : "") +
          "</div>";
      }).join("") + "</div>" +
        '<p class="fx-form-msg">这里是只读观战：对话轮的中止请在会话里操作（工厂的「叫停」只中止定时任务）。</p>';
    }
    openDrawer(drawerShell("对话与子代理在跑", html));
    if (!dom.drawer) return;
    dom.drawer.querySelectorAll("[data-live-session]").forEach(function (b) {
      b.onclick = function () {
        closeDrawer();
        if (typeof backToChat === "function") backToChat();
        if (typeof openTabForSession === "function") openTabForSession(b.dataset.liveSession, "");
      };
    });
  }

  function runLine(line, btn) {
    if (btn) btn.disabled = true;
    request("factory.run_now", { task_id: line.id }).then(function (r) {
      // 后台派工：回执只表示「已起跑」，包裹稍后由 started 事件送进场景
      if (r && r.started) addNotice("已派工：" + (line.name || line.key) + "（包裹马上上带）");
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

  function openQueueDrawer() {
    var st = activeState() || {};
    var projects = st.projects || [];
    var rows = (st.waiting || []).map(function (r) {
      return { key: r.key, name: r.name, project_id: r.project_id,
        note: "定时产线 · 已等待 " + fmtDur(Math.max(0, nowMs() / 1000 - (r.due_at || 0))) };
    });
    projects.forEach(function (p) {
      (p.lines || []).filter(function (l) { return l.kind === "pipeline"; }).forEach(function (l) {
        (l.nodes || []).filter(function (n) { return n.status === "ready"; }).forEach(function (n) {
          rows.push({ key: l.key, name: l.name + " / " + n.title, project_id: p.id,
            note: "编排工位 · 依赖已满足，等待调度" });
        });
      });
    });
    S.drawer = { type: "queue" };
    var total = Number((st.counters || {}).queued) || 0;
    var html = '<p class="fx-form-msg">定时产线等待并发额度；编排工位已满足依赖，等待调度。点击查看对应产线。</p>';
    html += rows.length ? '<div class="fx-run-list">' + rows.map(function (r) {
      var p = projects.find(function (p) { return p.id === r.project_id; });
      return '<button class="fx-run-row" data-queue-line="' + escapeHtml(r.key) + '"><span class="fx-run-name">' +
        escapeHtml((p ? p.name + " · " : "") + r.name) + '</span><span class="fx-run-meta">' +
        escapeHtml(r.note) + '</span></button>';
    }).join("") + '</div>' : '<div class="fx-run-empty">当前没有等待中的产线。</div>';
    if (total > rows.length) html += '<p class="fx-form-msg">共 ' + total + ' 项，当前列出 ' + rows.length + ' 项（明细有显示上限，已删除厂区也可能无法列出）。</p>';
    openDrawer(drawerShell("排队明细", html));
    if (dom.drawer) dom.drawer.querySelectorAll("[data-queue-line]").forEach(function (b) {
      b.onclick = function () { openLineDrawer(b.dataset.queueLine, { focus: true }); };
    });
  }

  function openAttentionDrawer() {
    var st = activeState();
    var items = (st && st.attention) || [];
    S.drawer = { type: "attention" };
    var html = "";
    // 结构性问题（停工 / 孤儿）要用户去修，标记「知道了」没有意义也不生效，
    // 所以只给运行类问题（失败 / 中断 / 跳过 / 空产出 / 有拦截）一个消化按钮。
    var ackable = { error: 1, interrupted: 1, skipped: 1, empty: 1, partial: 1 };
    var keys = [];
    if (!items.length) {
      html = '<div class="fx-run-empty">没有需要留意的事。</div>';
    } else {
      var kindText = {
        error: "失败", interrupted: "中断", skipped: "跳过",
        empty: "无产出", partial: "有拦截",
        orphan: "孤儿", stopped: "停工",
      };
      html = '<div class="fx-run-list">' + items.map(function (a) {
        var key = String(a.line_key || "");
        var canAck = !!ackable[a.kind] && key;
        if (canAck) keys.push(key);
        return '<div class="fx-run-row fx-attn-row" data-attn-line="' + escapeHtml(key) +
          '" role="button" tabindex="0" title="点开对应产线">' +
          '<span class="fx-run-name">' +
          escapeHtml(a.text || a.kind || "") + '</span><span class="fx-run-meta">' +
          escapeHtml(kindText[a.kind] || a.kind || "") + "</span>" +
          (a.kind === "partial" && findLine(key)
            ? '<button class="fx-ack" data-attn-process="' + escapeHtml(key) + '">去处理</button>' : "") +
          (canAck
            ? '<button class="fx-ack" data-ack="' + escapeHtml(key) + '"' +
              ' title="这条产线的这个问题已处理；同一问题再发生会重新提示">知道了</button>'
            : '<span class="fx-run-meta">修好即消失</span>') +
          "</div>";
      }).join("") + "</div>" +
        '<p class="fx-form-msg">点任意一行打开对应产线；「知道了」只压住这次，之后的新问题会重新提示。</p>';
      // 列表只画前 50 条（服务端上限），但计数是全部：不能让人以为「只剩这些」
      var total = Number(st && st.attention_total) || items.length;
      if (total > items.length) {
        html += '<p class="fx-form-msg">共 ' + total + " 条，这里只列出前 " +
          items.length + " 条；处理掉眼前这些再打开会看到剩下的。</p>";
      }
      if (keys.length > 1) {
        html += '<div class="fx-drawer-ops">' +
          '<button class="btn-ghost" data-ack-all="1">全部知道了</button></div>';
      }
    }
    openDrawer(drawerShell("需留意", html));
    if (!dom.drawer) return;
    dom.drawer.querySelectorAll("[data-attn-line]").forEach(function (row) {
      var open = function (ev) {
        if (ev && ev.target.closest("[data-ack], [data-attn-process]")) return;
        var key = row.dataset.attnLine || "";
        if (!key) return;
        if (!findLine(key)) {
          addNotice("这条产线已不在当前视图中（可能已被删除或所属项目已移除）");
          return;
        }
        openLineDrawer(key, { focus: true });
      };
      row.addEventListener("click", open);
      row.addEventListener("keydown", function (ev) {
        if (ev.target.closest("[data-ack], [data-attn-process]")) return;
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          open(ev);
        }
      });
    });
    dom.drawer.querySelectorAll("[data-attn-process]").forEach(function (b) {
      b.onclick = function (ev) {
        ev.stopPropagation();
        var rec = findLine(b.dataset.attnProcess);
        if (!rec || S.demo) return;
        closeDrawer();
        if (rec.line.kind === "cron") openAutomationFor(rec.project.id, rec.line.id);
        else openPipelineFor(rec.project.id, rec.line.id);
      };
    });
    dom.drawer.querySelectorAll("[data-ack]").forEach(function (b) {
      b.onclick = function (ev) {
        ev.stopPropagation();
        ackAttention([b.dataset.ack]);
      };
    });
    var allBtn = dom.drawer.querySelector("[data-ack-all]");
    if (allBtn) allBtn.onclick = function () { ackAttention(keys); };
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
      '<label class="fx-check"><input id="fx-nl-notify" type="checkbox">完成后推送到已启用的聊天渠道 / Webhook</label>' +
      '<p class="fx-hint">默认不推（每天几十次会淹人）；连续失败的聚合告警不受这个开关影响。</p>' +
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
        notify_channel: !!dom.drawer.querySelector("#fx-nl-notify").checked,
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
      (cfg.enabled !== false ? " checked" : "") + ">显示工厂看板入口（左侧导航）</label>" +
      '<label class="fx-check"><input id="fx-cfg-demo" type="checkbox"' +
      (cfg.demo !== false ? " checked" : "") + ">没有真实产线时播放样板厂</label>" +
      '<label class="fx-check"><input id="fx-cfg-ambient" type="checkbox"' +
      (cfg.ambient !== false ? " checked" : "") + ">昼夜天色（按本机时间；开天气时按当地日出日落）</label>" +
      '<label class="fx-check"><input id="fx-cfg-weather" type="checkbox"' +
      (cfg.weather === true ? " checked" : "") + ">按现实天气渲染（联网，默认关）</label>" +
      "<label>天气城市（城市名，例如：上海 / Shanghai）</label>" +
      '<input id="fx-cfg-weather-location" type="text" maxlength="80" value="' +
      escapeHtml(String(cfg.weather_location || "")) + '" placeholder="留空则只用本机时间判断昼夜">' +
      '<p class="fx-hint">只把城市名交给 Open-Meteo 解析坐标，再按坐标取当前天气；不做 IP 定位、不需要 API Key，结果缓存 30 分钟。关闭时一次网络请求都不发。</p>' +
      "<label>运行历史保留条数（100–100000）</label>" +
      '<input id="fx-cfg-keep" type="number" min="100" max="100000" value="' +
      Number(cfg.history_keep || 2000) + '">' +
      "<label>运行历史保留天数（1–3650）</label>" +
      '<input id="fx-cfg-days" type="number" min="1" max="3650" value="' +
      Number(cfg.history_days || 30) + '">' +
      "<label>同屏最多包裹数（10–300）</label>" +
      '<input id="fx-cfg-max" type="number" min="10" max="300" value="' +
      Number(cfg.max_packages || 60) + '">' +
      "<label>单次运行超时（分钟，0 = 不限）</label>" +
      '<input id="fx-cfg-timeout" type="number" min="0" max="1440" value="' +
      Number(cfg.run_timeout_minutes != null ? cfg.run_timeout_minutes : 60) + '">' +
      '<p class="fx-hint">超时按失败收尾并释放占用位——没有它，一条挂在模型流式 / 网络上的运行会永远显示「运行中」。</p>' +
      "<label>同时运行的产线上限（1–20）</label>" +
      '<input id="fx-cfg-parallel" type="number" min="1" max="20" value="' +
      Number(cfg.max_parallel_runs || 3) + '">' +
      '<p class="fx-hint">到点的产线超过这个数会排队，等下一轮扫描（20 秒）再接；计数条里的「排队」按这个口径算。</p>' +
      "<label>连续失败告警阈值（0 = 不告警）</label>" +
      '<input id="fx-cfg-alert" type="number" min="0" max="50" value="' +
      Number(cfg.alert_after_failures != null ? cfg.alert_after_failures : 3) + '">' +
      '<p class="fx-hint">同一条产线连续失败达到这个次数推一条通知，每天最多一条（防打扰）。</p>' +
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
      var weatherOn = dom.drawer.querySelector("#fx-cfg-weather").checked;
      var weatherCity = dom.drawer.querySelector("#fx-cfg-weather-location").value.trim();
      if (weatherOn && !weatherCity) {
        msg.textContent = "开启现实天气需要填写城市（例如：上海）";
        msg.className = "fx-form-msg error";
        return;
      }
      msg.textContent = "正在保存…";
      msg.className = "fx-form-msg";
      saveConfig({
        enabled: dom.drawer.querySelector("#fx-cfg-enabled").checked,
        demo: dom.drawer.querySelector("#fx-cfg-demo").checked,
        ambient: dom.drawer.querySelector("#fx-cfg-ambient").checked,
        weather: weatherOn,
        weather_location: weatherCity,
        history_keep: Number(dom.drawer.querySelector("#fx-cfg-keep").value) || 2000,
        history_days: Number(dom.drawer.querySelector("#fx-cfg-days").value) || 30,
        max_packages: Number(dom.drawer.querySelector("#fx-cfg-max").value) || 60,
        run_timeout_minutes: Number(dom.drawer.querySelector("#fx-cfg-timeout").value) || 0,
        max_parallel_runs: Number(dom.drawer.querySelector("#fx-cfg-parallel").value) || 3,
        alert_after_failures: Number(dom.drawer.querySelector("#fx-cfg-alert").value) || 0,
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
      renderWeatherChip();
      renderAll();
      // 天气开关 / 城市变了：清掉旧快照，强制拉一次（后端仍会按城市缓存）
      if (Object.prototype.hasOwnProperty.call(patch, "weather")
          || Object.prototype.hasOwnProperty.call(patch, "weather_location")) {
        S.weatherSeq += 1; // 让换城市 / 关天气前发出的请求作废
        S.weather = null;
        S.lastWeatherAt = 0;
        refreshWeather(true);
      }
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

  // ---------- 事件：factory_run（开始 / 工序进度 / 结束） ----------
  /** 进度文案：工具名 + 步数；没有工具时退回「第 N 步 / 启动中」。
      被门控拦过的次数挂在后面——这是「预授权没勾全」唯一看得见的痕迹。 */
  function progressText(pr) {
    var fresh = freshProgress(pr);
    if (!fresh) return "";
    var text;
    if (fresh.tool) text = fresh.tool + (fresh.steps ? " · 第 " + fresh.steps + " 步" : "");
    else if (fresh.steps) text = "第 " + fresh.steps + " 步";
    else text = "启动中";
    var denied = Number(fresh.denied) || 0;
    if (denied > 0) text += " · 被拦 " + denied + " 次";
    return text;
  }

  /** 进度事件：只落到产线 / 运行对象上，不重建场景（包裹位置另有计时驱动）。 */
  function applyRunProgress(payload) {
    if (!payload || payload.id == null) return;
    var key = payload.kind === "pipeline_node"
      ? "pipe:" + payload.pipeline_id : "cron:" + payload.task_id;
    var pr = payload.progress || {};
    var step = {
      tool: String(pr.tool || ""),
      at: Number(pr.at) || 0,
      steps: Number(pr.steps) || 0,
      denied: Number(pr.denied) || 0,
    };
    var rec = S.world && S.world.lines.get(key);
    if (rec) rec.line.progress = step;
    var cached = findRun(payload.id);
    if (cached) cached.progress = step;
    if (!S.mounted) return;
    // 面板最多 2 次/秒重建（多枚包裹同时推进时不要每个事件都重排 DOM）
    var now = nowMs();
    if (!S.lastProgressPanel || now - S.lastProgressPanel > 500) {
      S.lastProgressPanel = now;
      refreshPanelStates();
    }
    // 抽屉正开着这条产线 / 这一轮：就地更新进度行，不重建抽屉（避免抢焦点）
    if (S.drawer && S.drawer.type === "line" && S.drawer.key === key && dom.drawer) {
      var el = dom.drawer.querySelector("#fx-progress");
      if (el) el.textContent = progressText(step) || "—";
    }
    if (S.drawer && S.drawer.type === "run" && Number(S.drawer.id) === Number(payload.id)
        && dom.drawer) {
      var el2 = dom.drawer.querySelector("#fx-run-progress");
      if (el2) el2.textContent = progressText(step) || "—";
    }
  }

  /** 叫停：只停本轮，不动启用状态与排期（后端写 cancelled 终态并重排下次）。 */
  function cancelRun(taskId, name, btn) {
    confirmModal(
      "叫停会立刻中止「" + (name || "这条产线") + "」本轮的运行，已消耗的 token 不会退回；"
        + "产线保持启用，下一轮照原排期继续。",
      { title: "叫停正在跑的运行", okLabel: "叫停", danger: true },
    ).then(function (ok) {
      if (!ok) return;
      if (btn) btn.disabled = true;
      request("factory.cancel", { task_id: taskId }).then(function (r) {
        if (r && r.cancelled) addNotice("已叫停：" + (name || ("任务 " + taskId)));
        else addNotice((r && r.notice) || "没有可叫停的运行");
        if (!(r && r.cancelled) && btn) btn.disabled = false;
      }).catch(function (e) {
        addNotice("叫停失败：" + (e.message || "未知错误"));
        if (btn) btn.disabled = false;
      });
    });
  }

  /** 「这轮算过 / 不算」：人工判定只改 outcome，不删历史、不改状态。

      自动判定（跑完且有产出 / 有没有被门控拦过）回答不了「产出对不对」，
      而成功率要能当决策依据就必须允许人推翻它。 */
  function markRunOutcome(run, outcome, btn) {
    if (btn) btn.disabled = true;
    request("factory.run_mark", { run_id: run.id, outcome: outcome }).then(function (r) {
      var row = (r && r.run) || {};
      run.outcome = row.outcome || "";
      run.outcome_source = row.outcome_source || "";
      var cached = findRun(run.id);
      if (cached && cached !== run) {
        cached.outcome = run.outcome;
        cached.outcome_source = run.outcome_source;
      }
      addNotice(outcome === "ok" ? "已标记：这轮算过"
        : outcome === "failed" ? "已标记：这轮不算" : "已撤销人工判定");
      if (S.drawer && S.drawer.type === "run") openRunDrawer(run.id);
      scheduleResync();   // 产线体检的成功率要跟着重算
    }).catch(function (e) {
      addNotice("标记失败：" + (e.message || "未知错误"));
      if (btn) btn.disabled = false;
    });
  }

  /** 「需留意」已处理：只记时间戳，不删运行历史；更晚的失败会重新浮上来。 */
  function ackAttention(keys) {
    keys = (keys || []).filter(function (k) { return !!k; });
    if (!keys.length) return;
    request("factory.ack", { line_keys: keys }).then(function () {
      addNotice("已标记为知道了（同一问题再发生会重新提示）");
      closeDrawer();
      factoryReload();
    }).catch(function (e) {
      addNotice("标记失败：" + (e.message || "未知错误"));
    });
  }

  // ---------- 配置变化重拉 ----------
  /** cron_updated / pipeline_updated 之后防抖重拉一次快照。
      产线的新建 / 删除 / 暂停、跑完后的「下次运行」，都只能从服务端拿全；
      逐字段增量同步迟早和服务端对不上，一次 200ms 防抖的整份重拉更稳。 */
  function scheduleResync() {
    if (!S.mounted) return; // 没打开工厂就不必为它查库（下次打开会整份重拉）
    if (S.resyncTimer) clearTimeout(S.resyncTimer);
    S.resyncTimer = setTimeout(function () {
      S.resyncTimer = 0;
      if (S.mounted) factoryReload();
    }, 200);
  }

  function factoryOnEvent(data) {
    if (!data || data.kind !== "factory_run" || !data.run) return;
    if (S.demo) return; // 样板厂不掺真实事件；真有真实产线时会走下一次 reload
    if (!S.state) return;
    // 工序进度：只更新「在跑什么」，不动运行行与计数（开始/结束才有那两件事）
    if (data.phase === "progress") { applyRunProgress(data.run); return; }
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
        if (rec.project) S.flowingProjects.add(rec.project.id);
      }
    } else {
      st.counters = st.counters || {};
      st.counters.running = Math.max(0, (st.counters.running || 0) - 1);
      // 「今日做成」按 outcome：done + partial（有工具被拦）不算做成，
      // 不能因为状态是 done 就给它 +1 并飘「+1 成品」。旧行没有 outcome 时
      // 仍按 done 计，避免升级当天的历史凭空少一截。
      var runOutcome = run.outcome || "";
      var made = run.status === "done"
        && runOutcome !== "partial" && runOutcome !== "failed";
      if (made && isToday(run.finished_at)) {
        st.counters.done_today = (st.counters.done_today || 0) + 1;
      }
      var rec2 = S.world && S.world.lines.get(runKey(run));
      if (rec2) {
        rec2.line.running_run_id = null;
        rec2.line.progress = null; // 这一轮结束了：机台上不该还挂着上一个工具名
        rec2.line.status = rec2.kind === "pipeline"
          ? (run.status === "error" ? "failed" : (run.status === "done" || run.status === "empty") ? "done" : "idle")
          : (rec2.line.status === "paused" || rec2.line.status === "stopped" ? rec2.line.status : "idle");
        rec2.line.last_status = statusToLast(run.status);
        rec2.line.last_run_at = run.finished_at || nowMs() / 1000;
        var attnKind = "";
        if (run.status === "error" || run.status === "interrupted"
            || run.status === "skipped" || run.status === "empty") {
          attnKind = run.status;
        } else if (run.status === "done" && runOutcome === "partial") {
          attnKind = "partial";
        }
        if (attnKind) {
          st.attention = st.attention || [];
          var lineKey = runKey(run);
          var exists = st.attention.some(function (a) { return a.line_key === lineKey; });
          if (!exists) {
            var attnLabel = attnKind === "partial" ? "跑完但有工具被拦"
              : attnKind === "empty" ? "无产出"
                : (RUN_STATUS[attnKind] || attnKind);
            var attnName = (run.project_name ? run.project_name + " · " : "")
              + (run.name || "产线");
            st.attention.unshift({
              kind: attnKind, project_id: run.project_id, line_key: lineKey,
              text: attnName + "：" + attnLabel,
            });
            st.counters.attention = (st.counters.attention || 0) + 1;
          }
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
      if (rec2 && rec2.project) refreshFlowingProject(rec2.project.id);
      // 成品 / 返工的一行浮字（DOM 覆盖层，不画进 Canvas）
      if (rec2 && !S.reduced) {
        var floatText = "";
        var floatBad = false;
        var floatWarn = false;
        if (made) {
          floatText = "+1 成品";
        } else if (run.status === "done" && runOutcome === "partial") {
          floatText = "成品 · 有拦截";
          floatWarn = true;
        } else if (run.status === "error") {
          floatText = "返工";
          floatBad = true;
        }
        rec2.factory.float = {
          text: floatText,
          bad: floatBad,
          warn: floatWarn,
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
    // 运行详情也一样：跑完的包裹不能还挂着「运行中 / 叫停」。
    // 抽屉已经可见时 openDrawer 不会重新抢焦点，所以这里不需要避开 activeElement。
    if (S.drawer && S.drawer.type === "run"
        && Number(S.drawer.id) === Number(run.id)) {
      openRunDrawer(run.id);
    }
    render();
  }

  function statusToLast(s) {
    if (s === "done") return "ok";
    if (s === "empty") return "empty";
    if (s === "cancelled") return "cancelled";
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
      // 判定类字段必须跟着事件走，否则「被拦 N 次 / 没做成 / 归因」只在重开抽屉时才有
      denied_count: Number(run.denied_count) || 0,
      denied_tools: run.denied_tools || "",
      error_kind: run.error_kind || "",
      outcome: run.outcome || "",
      outcome_source: run.outcome_source || "",
      result_path: run.result_path || "",
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
      pair[0].lines.forEach(function (l) {
        l.project_id = pair[1];
        // 样板厂的体检样本：按产线 id 稳定生成（只在内存，不写库）
        var bad = l.last_status === "error";
        l.health = demoHealth(
          l.id, 24 + (Math.abs(hashKey(String(l.id))) % 30),
          bad ? 0.62 : 0.92, 90 + (Math.abs(hashKey(l.name)) % 120), bad ? 2 : 0);
      });
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
      counters: { running: 2, queued: 1, attention: 2, done_today: 2, tokens_today: 18400 },
      attention: [
        { kind: "error", project_id: -2, line_key: "cron:-22", text: "依赖体检 · 上次失败" },
        { kind: "stopped", project_id: -1, line_key: "cron:-13", text: "素材归档 · 停工" },
      ],
      attention_total: 2,
      runs: runs,
      // 样板厂的「对话在跑」：演示一格，说明这一栏在真实环境下也看得到东西
      live: {
        chat: 1, subagent: 1,
        runs: [
          { id: "demo-turn-1", kind: "turn", session_id: "", title: "整理这周的发布说明",
            state: "running", detail: "正在执行 read_file", started_at: now - 30,
            updated_at: now - 2, stale: false },
          { id: "demo-sub-1", kind: "subagent", session_id: "", title: "依赖扫描子代理",
            state: "running", detail: "正在执行 run_command", started_at: now - 90,
            updated_at: now - 4, stale: false },
        ],
      },
      config: Object.assign({ enabled: true, demo: true, ambient: true, weather: false,
        history_keep: 2000, history_days: 30, max_packages: 60,
        run_timeout_minutes: 60, max_parallel_runs: 3, alert_after_failures: 3 }, cfg || {}),
      now: now,
    };
  }

  /** 样板厂体检样本：按产线 id 生成稳定的状态序列（只在内存）。 */
  function demoHealth(id, total, rate, avg, streak) {
    var tail = [];
    for (var i = 0; i < 20; i += 1) {
      var h = Math.abs(hashKey(String(id) + ":" + i)) % 100;
      tail.push(h < rate * 100 ? "done" : (h < rate * 100 + 6 ? "empty" : "error"));
    }
    for (var j = 0; j < Math.min(streak || 0, 20); j += 1) tail[j] = "error";
    var done = Math.round(total * rate);
    return {
      total: total, done: done, error: Math.max(0, total - done),
      succeeded: done,
      partial: Math.max(0, Math.round(total * (1 - rate) * 0.3)),
      marked_failed: 0,
      success_rate: rate, avg_s: avg,
      p95_s: Math.round(avg * 1.8), max_s: Math.round(avg * 2.6),
      streak: streak || 0, tail: tail,
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
      // 样板厂的用量也是编的：让「单次成本」这一栏在演示里也看得见
      in_tokens: 1800 + (Math.abs(hashKey(String(id))) % 900),
      out_tokens: 320 + (Math.abs(hashKey(String(id) + "o")) % 400),
      cached_tokens: 600 + (Math.abs(hashKey(String(id) + "c")) % 500),
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

  /** cron_updated / pipeline_updated 的入口：产线定义变了就防抖重拉快照。 */
  function factoryOnAutomationChanged() {
    scheduleResync();
  }

  window.factoryMount = factoryMount;
  window.factoryUnmount = factoryUnmount;
  window.factoryReload = factoryReload;
  window.factoryOnEvent = factoryOnEvent;
  window.factoryRepaint = factoryRepaint;
  window.factoryEscape = factoryEscape;
  window.factoryOnAutomationChanged = factoryOnAutomationChanged;
})();
