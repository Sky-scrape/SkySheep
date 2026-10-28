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

  // ---------- FX 命名空间 ----------
  // 工厂拆成四个普通 script，加载顺序：本文件（状态 / 数据 / 面板 / 循环 / 事件）
  // → app-factory-scene.js（世界布局建模）→ app-factory-drawer.js（抽屉与产线
  // 操作）→ app-factory-draw.js（场景绘制）。共享状态、常量与跨层函数统一挂
  // window.FX：先加载方在 IIFE 尾部导出，后加载方在顶部绑定活引用；先加载方
  // 调后加载方的函数一律经 FX.xxx 运行时查表（见各「转发」区块）。
  var FX = window.FX = window.FX || {};

  // ---------- 常量 ----------
  var TILE_W = 32;
  var TILE_H = 16;
  var MAX_LABELS = 40;
  var MAX_PACKAGES = 60;
  var BUFFER_MIN_W = 320;
  var BUFFER_MIN_H = 180;
  var TICK_MS = 100;          // 世界动画 10fps
  var UPCOMING_WINDOW_S = 1800;  // 待发告示牌只看未来 30 分钟到点的产线
  var UPCOMING_MAX = 1;          // 牌面不再写任务名（只留短标题 + 状态灯），常量沿用（弹窗明细用）
  var UPCOMING_OVERDUE_S = 1800; // 过期下界预留位：现在不按过期时长裁（见 refreshUpcoming）
  var UPCOMING_NAME_MAX = 8;     // 名字截断长度（弹窗里的任务名沿用；牌面已不写名字）
  // 告示牌固定像素尺寸（内部缓冲坐标，不随窗口条数或摄像机缩放变）：小立牌，
  // 只放「待发」短标题与状态灯，明细点击后在居中弹窗里看（openUpcomingDialog）。
  var UPCOMING_BOARD = { w: 28, h: 17, pillar: 8, lead: 5 };
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
  var SPARSE_PROJECTS = 8;    // 厂区少于这个数时，筛选条不出现（没有「看不过来」的问题）
  var PALLET_SIZE = 6;        // 一只托盘码几箱（2 列 × 3 层）；满一托盘就该来一辆车
  var PALLET_MAX = 2;         // 场院里最多同时摆几只成品托盘（再多就只标「满」）
  var REWORK_MAX = 6;         // 返工堆最多画几箱（再多的部分靠厂区地面的红灯说）
  var UPCOMING_GLYPH_S = 1800; // 机台上方的到点沙漏只看未来 30 分钟
  var TRIP_PHASES = [          // 出车五段：进场 → 等候 → 停靠 → 装货 → 离场
    { name: "approach", ms: 2800 },   // 从坪沿外淡入，沿场院前带开到等候点
    { name: "wait", ms: 1400 },       // 等候点熄火等装货位腾出来（表现节奏）
    { name: "dock", ms: 1600 },       // 缓动挪进装货位
    { name: "load", ms: 3600 },       // 叉车在托盘与车斗之间往返搬箱
    { name: "leave", ms: 2600 },      // 原路倒出，坪沿外淡出
  ];
  var TRIP_MS = TRIP_PHASES.reduce(function (sum, ph) { return sum + ph.ms; }, 0);
  var TRIP_MAX = 4;           // 全场同时在场的出车上限（纯表现，防扎堆）
  var WAVE_MS = 900;          // 点工头小羊之后的挥手时长（位姿函数读同一常量）
  var REPLAY_CAP_MS = 60000;  // 回放的总时长上限（每轮至少 0.9 秒、最多 2.6 秒）
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
    // 厂区今日统计（做成 / 返工 / 在跑）：只喂给场景，不当文字显示
    stats: {},
    trips: [],             // 出货卡车（装满一托盘放一辆，纯表现）
    replay: null,          // 回放今天的进度；running=false 时表示没在回放
    replayLoading: false,
    runCache: new Map(),   // 抽屉里按需求回的行
    upcoming: [],          // 告示牌数据：近 30 分钟到点（含刚过点未启动）的产线，按 eta 升序
    noticeboardView: null,    // 告示牌离散状态（noticeboardState 的结果，绘制/标签/测试同源）
    noticeboard: null,     // 告示牌本帧几何（绘制层写，标签层与点击热区共用）
    cam: { x: 0, y: 0, zoom: 1, tx: 0, ty: 0, tz: 1 },
    fitZoom: 0.82,         // 全景取景的缩放：机台名只在比全景更近时出现
    focusPid: null,        // 聚焦档：点厂区头「飞过去」看机台，Esc / 全景按钮回全景
    bufW: BUFFER_MIN_W, bufH: BUFFER_MIN_H,
    cssW: 960, cssH: 540,
    ticks: 0,
    rot: 0,
    hallBoost: 0,          // performance 时间戳：派工时转盘加速
    labelsOff: false,
    panelFolded: true,   // 目录默认折叠：工厂的主体是场景，目录按需展开
    drawer: null,
    selectedKey: null,
    hits: [],
    hover: null,           // 当前悬停的命中（{type,key/id/pid}）；离开 / 重建 / 卸载即清
    labelPool: [],
    raf: 0,
    clockTimer: 0,
    lastFrame: 0,
    lastTick: 0,
    lastPanel: 0,
    demoSeq: 0,
    reduced: false,
    hiddenAt: 0,           // 页面隐藏时刻：恢复时把出车 t0 整体平移，避免瞬跳
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

  // 产线键的唯一拼接入口：定时任务 cron:<id>、编排 pipe:<id>
  // （与后端 factory_line_key 同一口径；解析侧后端用 parse_factory_line_key）
  function lineKeyFor(kind, id) {
    return (kind === "pipeline" || kind === "pipeline_node") ? "pipe:" + id : "cron:" + id;
  }

  function runKey(run) {
    return run.kind === "pipeline_node" ? lineKeyFor("pipeline", run.pipeline_id) : lineKeyFor("cron", run.task_id);
  }

  // ---------- 数据源 ----------
  function activeState() { return S.demo || S.state; }
  // 上限按 [factory] 配置读（默认 6 台机台 / 40 个标签），越界按后端同口径夹取
  function machineCap() { return clamp(Math.round(Number(activeConfig().machine_limit) || 6), 1, 12); }
  function labelCap() { return clamp(Math.round(Number(activeConfig().label_limit) || MAX_LABELS), 10, 80); }
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
    // 目录默认折叠（状态在 S.panelFolded，折叠按钮会把它翻过来）
    if (dom.panel) dom.panel.classList.toggle("folded", S.panelFolded);
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
    S.hover = null;
    S.hoverPointer = null;
    hideHoverTip();
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
    // 顶上只剩一盏灯：在跑几件、今天做成几件已经长在场景里，不再占计数条
    dom.attentionLamp = $("factory-attention");
    dom.attnNum = $("factory-attn-num");
    dom.replayBtn = $("factory-replay");
    dom.replayBar = $("factory-replay-bar");
    dom.replayTime = $("factory-replay-time");
    dom.replayHint = $("factory-replay-hint");
    dom.replayStop = $("factory-replay-stop");
    dom.weatherChip = $("factory-weather");
    dom.hud = $("factory-hud");
    dom.demoBadge = $("factory-demo-badge");
    dom.chips = $("factory-panel-filter");
    dom.search = $("factory-search");
    // 悬停信息牌：轻量 DOM 层（中文不入 canvas），首次挂载时补建；
    // createElement 不可用（测试 shim）时保持 undefined，读写处均已判空
    dom.tip = $("factory-tip");
    if (!dom.tip && dom.stage && document.createElement) {
      var tipEl = document.createElement("div");
      tipEl.id = "factory-tip";
      tipEl.className = "fx-tip";
      tipEl.style.display = "none";
      tipEl.setAttribute("aria-hidden", "true");
      dom.stage.appendChild(tipEl);
      dom.tip = tipEl;
    }
  }

  function bindOnce() {
    if (S.bound) return;
    S.bound = true;
    cacheDom();

    var allBtn = $("factory-all-runs");

    if (allBtn) allBtn.onclick = function () { openAllRunsDrawer(); };
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
        var head = ev.target.closest("[data-focus-project]");
        if (head) { focusProject(Number(head.dataset.focusProject)); return; }
        var lineEl = ev.target.closest("[data-line-key]");
        if (lineEl) { openLineDrawer(lineEl.dataset.lineKey, { focus: true }); }
      });
      // 键盘：↑↓ / Home / End 在面板内逐行走（面板是画布的等价入口，不能只靠鼠标）
      dom.panelBody.addEventListener("keydown", panelKeyNav);
    }
    bindFilter();
    // 需留意：厂区头顶的红灯是主入口，这盏灯是键盘 / 扫视的等价入口
    if (dom.attentionLamp) dom.attentionLamp.onclick = function () { openAttentionDrawer(); };
    if (dom.replayBtn) dom.replayBtn.onclick = function () { toggleReplay(); };
    if (dom.replayStop) dom.replayStop.onclick = function () { stopReplay(); };

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
      if (document.hidden) {
        // 记下隐藏时刻：出车的 t0 是绝对时间，恢复时整体平移，
        // 避免后台挂了很久再回来时车从半路瞬移到离场
        S.hiddenAt = nowMs();
        return;
      }
      if (S.hiddenAt) {
        var drift = nowMs() - S.hiddenAt;
        S.hiddenAt = 0;
        S.trips.forEach(function (t) { t.t0 += drift; });
      }
      S.lastFrame = 0;
      S.lastTick = 0;
      renderAll();
      refreshLive();
      refreshWeather(false);
    });
    try {
      var mq = window.matchMedia("(prefers-reduced-motion: reduce)");
      S.reduced = mq.matches;
      var onChange = function (e) {
        S.reduced = e.matches;
        // 切到减少动态：在途出车立即撤场（位姿是时间函数，不清会继续跑）
        if (S.reduced) S.trips = [];
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
    // quiet：重拉快照不补放货车——否则每次配置变化都会有一队车同时涌出来
    refreshProjectStats(true);
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

  // ---------- 场景构建转发 ----------
  // 世界布局实现在 app-factory-scene.js（经 window.FX 运行时查表）。
  function buildWorld(st) { return FX.buildWorld(st); }
  function pathLength(pts) { return FX.pathLength(pts); }
  // 路径取样点：实现同样在 app-factory-scene.js（drawPackage 要用），
  // 漏掉这一行会让工厂首屏在渲染包裹时抛 ReferenceError，再被数据加载的
  // catch 误报成「读取工厂数据失败」。
  function pointAt(pts, t) { return FX.pointAt(pts, t); }

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

  /** 顶部只更新「需留意」这一盏灯。
      其余读数（在跑几件 / 今天做成几件 / 谁在返工）一律不在这里显示——
      它们已经在场景里有形状：带上的包裹、垛高的成品、地上堆的返工箱。 */
  function renderCounters() {
    var st = activeState();
    var c = (st && st.counters) || {};
    var n = Number(c.attention) || 0;
    if (dom.attnNum) dom.attnNum.textContent = String(n);
    if (dom.attentionLamp) {
      dom.attentionLamp.classList.toggle("hidden", n <= 0);
      dom.attentionLamp.title = n > 0
        ? (n + " 条产线需要看一眼（上次失败 / 有工具被拦 / 停工）；点击逐条列出")
        : "当前没有需要留意的产线";
    }
    // 样板厂：整条顶栏挂「演示」，避免把演示当成真的
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

  /** 团队产线（mode=team）的一句描述：模板名 / 成员规模 / 总管形态。
      数据是 factory.state 透传的 cron 行两键（is_team / team_template）加
      team_meta（后端按团队模板册现查的只读呈现字段）；模板悬空照实说
      「已删除」（触发期才报错，观战窗不做臆测）。非团队 / 缺 meta 返回空。 */
  function teamLineText(line) {
    if (!line || !line.is_team) return "";
    var meta = line.team_meta || null;
    var tpl = String(line.team_template || "");
    if (!meta) return tpl ? "模板「" + tpl + "」" : "团队";
    if (!meta.found) return tpl ? "模板「" + tpl + "」已删除" : "模板已删除";
    var parts = [];
    if (tpl) parts.push("模板「" + tpl + "」");
    parts.push((Number(meta.members) || 0) + " 名队员");
    parts.push(meta.director_ai ? "AI 总管" : "用户总管");
    return parts.join(" · ");
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

/** 厂区的「心情」：working / trouble / stopped / idle。

      这是场景层的总开关：大门抬不抬、工头在不在场院上走、烟多不多、窗光亮几格，
      全都读这一个值。以前这些表现各写各的判定（有的看 line.status、有的看 alert），
      于是会出现「门抬着、工头不动、烟也不冒」的拼接感。现在只在这里判一次。

      trouble 的门槛是红级（上次失败 / 被判不算），黄级（被拦 / 空产出）仍算 working：
      它还在开工，只是这轮不干净——黄级交给机台灯柱与返工堆表达，不该让整座厂区停摆。 */
  function refreshFactoryMood() {
    if (!S.world) return;
    S.world.factories.forEach(function (f) {
      var st = statFor(f.id);
      if (f.root_ok === false) f.mood = "stopped";
      else if (f.alert && f.alert.level >= 2) f.mood = "trouble";
      else if (st.busy > 0) f.mood = "working";
      else if (f.alert && f.alert.level === 1) f.mood = "working";
      else f.mood = "idle";
    });
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


  function renderPanel() {
    if (!dom.panelBody) return;
    // 全量重建会重置滚动条：重建前记住位置、写回后恢复，
    // 否则运行事件密集时监控面板会反复跳回顶部。
    var keepScroll = dom.panelBody.scrollTop;
    function setHtml(html) {
      dom.panelBody.innerHTML = html;
      dom.panelBody.scrollTop = keepScroll;
    }
    var st = activeState();
    if (!st) { showPanelMessage("正在读取工厂数据…"); return; }
    var projects = st.projects || [];
    if (!projects.length) {
      setHtml('<div class="fx-empty">还没有项目。先在侧栏添加一个项目，' +
        '工厂就会为它建一座厂区。</div>');
      return;
    }
    var attentionKeys = new Set();
    (st.attention || []).forEach(function (a) {
      if (a && a.line_key) attentionKeys.add(a.line_key);
    });
    var groups = filterProjects(projects, attentionKeys, S.filter);
    if (!groups.length) {
      setHtml('<div class="fx-empty">没有符合条件的产线。' +
        '<button class="fx-add-line" data-clear-filter="1">清除筛选</button></div>');
      return;
    }
    var html = "";
    groups.forEach(function (g) {
      var p = g.project;
      var lines = g.lines;
      var allLines = p.lines || [];
      var busy = allLines.filter(function (l) { return l.status === "running"; }).length;
      // 这里只留「这座厂区是谁、几条线、现在在忙什么」。
      // 批量派工 / 批量暂停 / 新建产线都退回了自动化页——那些是配置与调度，
      // 不是看场景时该出现在眼前的东西。派工仍可：点机台或点产线行，抽屉里一次一条。
      html += '<div class="fx-proj">';
      html += '<div class="fx-proj-head">' +
        '<button class="fx-proj-title" data-focus-project="' + escapeHtml(String(p.id)) +
        '" title="镜头飞到这座厂区">' +
        "<span>" + escapeHtml(p.name || "未命名项目") + "</span>" +
        '<span class="fx-badge' + (p.root_ok === false ? " warn" : "") + '">' +
        (p.root_ok === false ? "停工" : allLines.length + " 线" + (busy ? " · " + busy + " 在忙" : "")) +
        "</span></button></div>";
      if (!lines.length) {
        html += '<div class="fx-empty">这个厂区还没有产线。在左侧「自动化」里给它建一条。</div>';
      } else {
        lines.forEach(function (l) {
          // 面板只说「在跑什么」：下次几点、成功率、token 都进抽屉，别在目录里堆数字
          var sub = "";
          if (l.status === "running") {
            var step = runningStepText(l);
            sub = step ? "在跑 " + step : "在跑";
          } else if (l.health && l.health.streak >= 2) {
            sub = "连错 " + l.health.streak;
          } else if (l.is_team) {
            // 团队产线：非在跑且无连错时才轮到这句「模板 / 成员 / 总管」——
            // 在跑让位给「在跑 …」、连错让位给「连错 N」，先认出这是谁在值班
            sub = teamLineText(l);
          }
          var stateText = lineStateText(l);
            html += '<button class="fx-line ' + lineDotClass(l) +
              (S.selectedKey === l.key ? " active" : "") +
              '" data-line-key="' + escapeHtml(l.key) + '" title="点开看这轮在做什么，并可立即运行">' +
            '<span class="fx-dot ' + lineDotClass(l) + '"></span>' +
            '<span class="fx-line-main">' +
            '<span class="fx-line-name">' + escapeHtml(l.name || l.key) + "</span>" +
            (sub ? '<span class="fx-line-sub">' + escapeHtml(sub) + "</span>" : "") +
            "</span>" +
            (l.is_team ? '<span class="fx-line-team">团队</span>' : "") +
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
    setHtml(html);
    // 厂区少的时候筛选条不出场：它解决的是「多到看不过来」，不是默认态
    if (dom.chips) dom.chips.classList.toggle("sparse", projects.length < SPARSE_PROJECTS);
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
        // preventScroll：滚动位置已由 renderPanel 保持，焦点恢复不再把它拽走
        if (rows[i].dataset.lineKey === key) { rows[i].focus({ preventScroll: true }); break; }
      }
      return;
    }
    renderPanel();
  }

  function renderAttentionNotice(cfg) {
    var st = activeState();
    if (!st) return;
    if (S.demo) {
      // 建线入口不在工厂里：这里只说「去哪儿建」，不给表单
      showNotice("<b>样板厂</b>：本机还没有真实产线，下面是演示数据——只在内存，不会真的运行。" +
        "在左侧「自动化」里给项目建一条定时产线，这座园区就开始动起来。" +
        '<div class="fx-notice-ops"><button class="btn-ghost" data-notice-dismiss="1">知道了</button></div>', false);
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
    var dismiss = dom.notice.querySelector("[data-notice-dismiss]");
    if (dismiss) dismiss.onclick = function () { hideNotice(); };
    var enable = dom.notice.querySelector("[data-notice-enable]");
    if (enable) enable.onclick = function () { saveConfig({ enabled: true }, enable); };
  }

  // ---------- 相机 ----------
  function fitCamera(animate) {
    S.focusPid = null;   // 全景 = 退出聚焦档（Esc / 重开视图都回到这里）
    var w = S.bufW, h = S.bufH;
    // 有 focus 就用 focus：只框调度厅 + 厂区坪，不把台地前缘与远景设施算进来。
    // 场景不支持缩放，默认取景就是唯一画幅——框得越准，机台越大。
    var b = (S.world && (S.world.focus || S.world.bounds))
      || { minX: -80, maxX: 80, minY: -50, maxY: 50 };
    var pad = 32;
    var z = Math.min((w - pad * 2) / Math.max(1, b.maxX - b.minX),
                     (h - pad * 2) / Math.max(1, b.maxY - b.minY));
    z = Math.max(0.01, Math.min(z, 1.45));
    var cx = (b.minX + b.maxX) / 2;
    // 垂直取景尽量把「台地前缘前景」（油罐 / 公路 / 前沿设施）一起收进画幅：
    // 大窗口下全景缩放顶到 1.45 上限后，厂区坪只占画幅六七成，若仍只按坪
    // 居中，余量全堆在顶部成大片空天，前缘景物被底边拦腰裁断。前缘装不下
    // 时允许 zoom 最多回缩一成换完整前缘（天空仍拿 11%、底部留 16px）；
    // 再装不下（窗口小，机台优先）维持只框厂区坪，前缘沿底边自然出画。
    var frontMaxY = (S.world && S.world.bounds)
      ? Math.max(b.maxY, S.world.bounds.maxY) : b.maxY;
    var frontDepth = frontMaxY - b.maxY;
    if (frontDepth > 0) {
      var span = b.maxY - b.minY + frontDepth;
      var sky = h * 0.11;
      if (span * z + sky + 16 > h) {
        var zAlt = (h - sky - 16) / span;
        if (zAlt >= z * 0.9) z = zAlt;   // 机台缩小一成以内，换前缘完整入画
      }
    }
    S.fitZoom = z;
    var frameMaxY = (frontDepth > 0 &&
                     (b.maxY - b.minY + frontDepth) * z + h * 0.11 + 16 <= h)
      ? frontMaxY : b.maxY;
    // 天空占上方约 11% 封顶，余量上下均分且底部保 16px 安全边；机台名等
    // 「比全景更近才出现」的阈值仍按 fitZoom（取景框变化不改变全景语义）。
    var landOffset = Math.max(0, Math.min(h * 0.11,
      (h - (frameMaxY - b.minY) * z) / 2 - 16));
    var cy = (b.minY + frameMaxY) / 2 - landOffset / z;
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

  // ---------- 绘制层转发 ----------
  // 绘制实现搬入 app-factory-draw.js（经 window.FX 运行时查表）；
  // 本文件内的调用点保持裸名不变，由这组同签名转发函数承接。
  function render() { FX.render(); }
  function viewPoint(x, y) { return FX.viewPoint(x, y); }
  function drawIsoBoxAt(ctx, x, y, hw, hh, h, top, left, right) {
    return FX.drawIsoBoxAt(ctx, x, y, hw, hh, h, top, left, right);
  }
  function vlen(v) { return FX.vlen(v); }
  function fillDiamond(ctx, cx, cy, hw, hh, color) { FX.fillDiamond(ctx, cx, cy, hw, hh, color); }
  function fillOval(ctx, cx, cy, rx, ry, color) { FX.fillOval(ctx, cx, cy, rx, ry, color); }
  function linePath(ctx, pts, width, color) { FX.linePath(ctx, pts, width, color); }
  function seeded(seed) { return FX.seeded(seed); }
  function isToday(sec) { return FX.isToday(sec); }
  function roleCols(pal, role, bg) { return FX.roleCols(pal, role, bg); }

  // ---------- 包裹 ----------
  // ---------- 待发告示牌（未来排期可视化） ----------
  /** 近 30 分钟到点的 cron 产线（eta 升序；eta <= 0 表示到点还没跑起来）。 */
  function upcomingList(st, nowSec) {
    var out = [];
    if (st && st.projects) {
      st.projects.forEach(function (p) {
        if (p.root_ok === false) return;   // 停工的厂区不会到点派工
        (p.lines || []).forEach(function (l) {
          if (l.kind !== "cron" || l.status !== "idle") return;  // 在跑 / 停工的都不算
          var at = Number(l.next_run_at);
          if (!Number.isFinite(at) || at <= 0) return;  // 缺字段 / 脏时间戳不成一条待发
          var eta = at - nowSec;
          if (eta > UPCOMING_WINDOW_S) return;   // 过点的不裁：等运行事件或下轮排期带走它
          out.push({ key: l.key, name: l.name || l.key, project: p.name || "",
                     eta: eta, due: eta <= 0 });
        });
      });
    }
    out.sort(function (a, b) { return a.eta - b.eta; });
    return out;
  }

  /** 牌面任务名截断：牌子只有几格宽，长名字顶出板面就不好看了。 */
  function truncName(name) {
    var s = String(name == null ? "" : name);
    return s.length > UPCOMING_NAME_MAX ? s.slice(0, UPCOMING_NAME_MAX) : s;
  }

  /** 告示牌牌面离散状态：empty 暂无待发 / due 到点未启动（等待调度）/ soon 倒计时。 */
  function noticeboardState(st, nowSec) {
    var list = upcomingList(st, nowSec);
    if (!list.length) {
      return { state: "empty", key: "", name: "", countdown: "",
               text: "暂无待发", count: 0, overflow: 0 };
    }
    var first = list[0];
    var name = truncName(first.name);
    var countdown = first.due ? "" : fmtCountdown(first.eta);
    return {
      state: first.due ? "due" : "soon",
      key: first.key,
      name: name,
      countdown: countdown,
      text: name + (countdown ? " · " + countdown : " · 等待调度"),
      count: list.length,
      overflow: Math.max(0, list.length - UPCOMING_MAX),
    };
  }

  function refreshUpcoming() {
    var st = activeState();
    var now = Date.now() / 1000;
    S.upcoming = upcomingList(st, now);
    S.noticeboardView = noticeboardState(st, now);
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
      else {
        var p0 = S.packages.get(id);
        p0.run = run;
        // 世界重建（快照重排 / 机台数变化）后 line 是新对象：不回填的话
        // 包裹会沿着旧几何飞，直到这条运行结束
        p0.line = line;
      }
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

  // ---------- 厂区「今天干了多少」：让成品垛 / 返工堆 / 出货有数据来源 ----------
  /** 每个厂区今天的做成 / 返工件数，以及当前在跑几条线。

      这些数字不显示成文字，只喂给场景：做成几件 → 托盘上码几箱，堆满一托盘
      就叫一辆车来拉走；返工件数 → 场院角上堆几个红箱。取数口径与顶部那盏灯
      和抽屉体检一致：`done` 且不是 partial 才算做成，partial 与 error 都进返工堆。

      样本来源是快照里的 runs（服务端只回最近 60 条）。所以这是个「够看」的量：
      成品垛封顶 PALLET_SIZE × PALLET_MAX 箱、返工堆封顶 REWORK_MAX 箱，
      样本饱和时画面上本来也看不出差别。 */
  function refreshProjectStats(quiet) {
    var st = activeState();
    if (!st) return;
    var next = {};
    (st.projects || []).forEach(function (p) {
      next[p.id] = { done: 0, rework: 0, busy: 0, lines: (p.lines || []).length };
    });
    (st.runs || []).forEach(function (r) {
      var s = next[r.project_id];
      if (!s) return;
      if (r.status === "running") s.busy += 1;
      if (!isToday(r.finished_at)) return;
      var partial = r.outcome === "partial" || r.outcome === "failed";
      if (r.status === "done" && !partial) s.done += 1;
      else if (r.status === "error" || partial) s.rework += 1;
    });
    // 成色变化 → 出货：每攒满一托盘放一辆车出厂（首帧 / 重拉时不补放，
    // 否则每次切项目都会有一队车同时涌出来）。
    if (!quiet && S.world) {
      Object.keys(next).forEach(function (pid) {
        var before = (S.stats && S.stats[pid] && S.stats[pid].done) || 0;
        var after = next[pid].done;
        if (after > before
            && Math.floor(after / PALLET_SIZE) > Math.floor(before / PALLET_SIZE)) {
          spawnTrip(S.world.factories.filter(function (f) {
            return String(f.id) === String(pid);
          })[0]);
        }
      });
    }
    S.stats = next;
  }

  /** 取某个厂区的今日统计（绘制层用；没有数据时返回全零）。 */
  function statFor(pid) {
    var s = S.stats && S.stats[pid];
    return s || { done: 0, rework: 0, busy: 0, lines: 0 };
  }

  // ---------- 出货：装满一托盘就来一辆车 ----------
  /** 出车走完整动线：进场 → 等候 → 停靠装货位 → 叉车装货 → 离场。
      纯表现：不参与任何判定、不改运行状态；动线几何与位姿插值在 scene 层。 */
  function spawnTrip(f) {
    if (!f || S.reduced) return;          // 减少动态效果：不动，托盘垛仍是静态读数
    if (!f.dock || !f.apron) return;      // 旧世界没有动线几何：不放假车
    // 同厂一次只走一组装运；全场同时在场的车不超过 TRIP_MAX 组
    for (var i = 0; i < S.trips.length; i += 1) {
      if (String(S.trips[i].pid) === String(f.id)) return;
    }
    if (S.trips.length >= TRIP_MAX) return;
    var done = statFor(f.id).done || 0;
    var boxes = done % PALLET_SIZE || Math.min(2, PALLET_SIZE);  // 没读到统计按半托装
    S.trips.push({ pid: f.id, factory: f, t0: nowMs(), boxes: clamp(boxes, 1, 3) });
  }

  function updateTrips(now) {
    if (!S.trips.length) return;
    S.trips = S.trips.filter(function (t) { return now - t.t0 < TRIP_MS; });
  }

  // ---------- 回放今天 ----------
  /** 把今天跑过的每一轮按时间顺序在传送带上重演。

      为什么值得做：工厂是「此刻」的一扇窗，可生产这件事最好看的样子是它的一天——
      早上空垛、中午堆满、傍晚被车拉走、中间出过几次红箱。数据全在
      `automation_runs` 里（`factory.runs` 的 tokens_today 视图就是「今天开始的运行」），
      不需要新的执行路径，也不需要落任何东西。

      - 按轮次排队演：每轮一个固定档期（0.9–2.6 秒），一天的轮次越多总时长越长
        （上限 REPLAY_CAP_MS）；不按真实时间轴等比压缩，否则几十秒的运行会闪一下就没；
      - 进度条上写的是这轮真实的开始钟点，不是加速后的虚拟时钟；
      - 回放时实时包裹停画（避免真假同屏），Esc /「回到此刻」立刻退出。 */
  function toggleReplay() {
    if (S.replay && S.replay.running) { stopReplay(); return; }
    if (S.demo) { addNotice("样板厂没有真实运行记录，回放只在有真实产线的项目里可用"); return; }
    if (S.replayLoading) return;
    S.replayLoading = true;
    request("factory.runs", { view: "tokens_today", limit: 400 }).then(function (r) {
      var rows = ((r && r.runs) || []).filter(function (x) {
        return x.started_at && isToday(x.started_at);
      });
      if (!rows.length) {
        addNotice("今天还没有运行记录可回放");
        return;
      }
      rows.sort(function (a, b) { return a.started_at - b.started_at; });
      // 按「轮次」排队演，不按真实时间轴等比压缩：今天的运行大多几十秒，
      // 摊到 13 个小时里压成 42 秒，每一轮只会闪一下——那不是回放，是闪烁。
      // 现在每轮占一个固定档期（约 2.2 秒），一天 25 轮大约 55 秒看完，
      // 进度条上的钟点仍然写这轮真实的开始时间。
      var step = Math.max(900, Math.min(2600, Math.floor(REPLAY_CAP_MS / rows.length)));
      S.replay = {
        running: true, startedAt: nowMs(), step: step, duration: step * rows.length,
        items: rows.map(function (x, i) {
          return {
            id: x.id, run: x, key: runKey(x), slot: i,
            at: x.started_at,
            status: x.status,
          };
        }),
      };
      document.body.classList.add("factory-replaying");
      if (dom.replayBtn) { dom.replayBtn.classList.add("playing"); dom.replayBtn.textContent = "回放中"; }
      if (dom.replayBar) dom.replayBar.classList.remove("hidden");
      render();
    }).catch(function (e) {
      addNotice("回放数据读取失败：" + escapeHtml(e && e.message ? e.message : "未知错误"));
    }).finally(function () {
      S.replayLoading = false;
    });
  }

  function stopReplay() {
    S.replay = null;
    document.body.classList.remove("factory-replaying");
    if (dom.replayBtn) { dom.replayBtn.classList.remove("playing"); dom.replayBtn.textContent = "回放"; }
    if (dom.replayBar) dom.replayBar.classList.add("hidden");
    render();
  }

  /** 某条回放项此刻的进度（0..1）；还没轮到 / 已经演完都返回 -1。 */
  function replayProgress(rp, it, elapsed) {
    var local = elapsed - it.slot * rp.step;
    if (local < 0) return -1;
    // 最后 15% 的档期留给「落地」：包裹停一会儿再消失，不然看不清结果色
    var run = rp.step * 0.85;
    if (local > rp.step) return -1;
    return clamp(local / run, 0, 1);
  }

  function replayTick(now) {
    var rp = S.replay;
    if (!rp || !rp.running) return;
    var elapsed = now - rp.startedAt;
    if (dom.replayTime) {
      // 进度条上的钟点是「这轮真实的开始时间」，不是加速后的虚拟时钟
      var idx = clamp(Math.floor(elapsed / rp.step), 0, rp.items.length - 1);
      var d = new Date((rp.items[idx].at || 0) * 1000);
      dom.replayTime.textContent = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    }
    if (dom.replayHint) {
      var k = clamp(Math.floor(elapsed / rp.step) + 1, 1, rp.items.length);
      dom.replayHint.textContent = "第 " + k + "/" + rp.items.length + " 轮";
    }
    if (elapsed >= rp.duration) stopReplay();
  }

  function drawReplay(ctx, pal) {
    var rp = S.replay;
    if (!rp || !rp.running || !S.world) return;
    var elapsed = nowMs() - rp.startedAt;
    rp.items.forEach(function (it) {
      var progress = replayProgress(rp, it, elapsed);
      if (progress < 0) return;
      var line = S.world.lines.get(it.key);
      if (!line || !line.drawn) return;
      var z = S.cam.zoom;
      // 连续插值：旧版按 16px 一格取整，拉近看包裹是一顿一顿跳格的。
      // pointAt 吃的是世界坐标，画之前必须过 viewPoint 投影——漏了投影
      // 包裹会拿世界值当屏幕值，全画到画布左上角一小片区域里乱窜。
      var pos = pointAt(line.path, progress);
      var vp = viewPoint(pos.x, pos.y);
      // 贴带高度随行程缓降（主带 12 → 机台支线 7~9，取 12→8 线性近似）
      var lift = Math.round((12 - 4 * progress) * z);
      var color = pal.product;
      var edge = pal.crateDark;
      if (it.status === "error" || it.status === "interrupted" || it.status === "orphan") {
        color = pal.bad; edge = pal.ink;
      } else if (it.status === "empty" || it.status === "skipped") {
        color = pal.beltMark; edge = pal.beltDark;
      }
      // 落地后淡出：档期最后那一小段
      var alpha = progress > 0.94 ? clamp((1 - progress) / 0.06, 0, 1) : 1;
      ctx.globalAlpha = alpha;
      drawParcelBox(ctx, pal, vp.x, vp.y, lift, color, edge);
      ctx.globalAlpha = 1;
    });
  }

  function drawPackages(ctx, pal) {
    // 回放时只演历史：实时包裹停画，两种「包裹」同屏会分不清哪个是真的
    if (S.replay && S.replay.running) { S.overflow = 0; drawReplay(ctx, pal); return; }
    // 实时包裹改走动态画序（collectPackageItems）：与工头 / 车 / 叉车同一条
    // 合并通道，按地面线插进静态件之间——以前整层盖在场景顶上，包裹路过
    // 调度厅身后时糊在厅的正立面上。
  }

  /** 实时包裹进动态画序（绘制层 collectDynamicItems 逐帧调用）：上限折叠
      （「+N 枚包裹」标签读的 S.overflow）沿用原口径，位姿由 packagePose
      现算；排序键取包裹的地面世界 y，z0 与车 / 工头统一坐厂坪高度。 */
  function collectPackageItems(pal, out, now) {
    if (S.replay && S.replay.running) { S.overflow = 0; return; }
    var items = [];
    S.packages.forEach(function (pkg) {
      if (!pkg.line || !pkg.line.drawn) return;
      if (pkg.finish && now - pkg.finishedAt > RUN_TAIL_MS) return;
      items.push(pkg);
    });
    items.sort(function (a, b) { return (a.run.started_at || 0) - (b.run.started_at || 0); });
    // [factory] max_packages 控制同屏包裹上限（默认 60）；超出折叠为「+N」
    var limit = clamp(Math.round(Number(activeConfig().max_packages) || MAX_PACKAGES), 10, 300);
    S.overflow = Math.max(0, items.length - limit);
    items.slice(0, limit).forEach(function (pkg) {
      var pose = packagePose(pal, pkg, now);
      out.push({ d: pose.wy, z0: FX.YARD_Z, draw: function (ctx) {
        drawPackage(ctx, pal, pkg, now);
      } });
    });
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

  /** 待发告示牌：调度厅右前方一块固定像素的小立牌（影子 + 中柱底座 + 板面 + 状态灯）。

      位置沿用原右侧月台锚点，尺寸固定（UPCOMING_BOARD）：不随条数或缩放长大，
      不遮厅、不越月台、不占 3×3 外围。牌面只放「待发」短标题与状态灯，不写任务名 /
      倒计时 / 溢出条数——明细点击后在居中弹窗里看（openUpcomingDialog）。中文一律
      由 DOM 标签层贴，canvas 不画字。 */
  function drawNoticeboard(ctx, pal) {
    if (!S.world) return;
    var view = S.noticeboardView || noticeboardState(activeState(), Date.now() / 1000);
    var hall = S.world.hall;
    var dock = viewPoint(hall.x + 34, hall.y + 22);   // 原右侧月台锚点
    var w = UPCOMING_BOARD.w, h = UPCOMING_BOARD.h, pillar = UPCOMING_BOARD.pillar;
    var x0 = Math.round(dock.x + UPCOMING_BOARD.lead);
    var gy = Math.round(dock.y);
    var top = gy - pillar - h;
    var cx = x0 + Math.round(w / 2);
    // 影子比底座宽：牌子是「立」在月台上的，不是飘着的
    fillOval(ctx, cx, gy, Math.round(w * 0.34), 3, pal.shadow);
    // 中柱 + 底座横档
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(cx - 2, gy - pillar, 4, pillar + 1);
    ctx.fillStyle = pal.metal;
    ctx.fillRect(cx - Math.round(w * 0.16), gy - 2, Math.round(w * 0.32), 2);
    // 板面：浅框 + 深底（标题栏与状态灯压在框内）
    ctx.fillStyle = pal.mark;
    ctx.fillRect(x0, top, w, h);
    ctx.fillStyle = pal.ink;
    ctx.fillRect(x0 + 1, top + 1, w - 2, h - 2);
    // 标题栏：到点还没跑起来时转锈色——「在等」和「该跑没跑」一眼分得开
    ctx.fillStyle = view.state === "due" ? pal.rust : pal.steelDark;
    ctx.fillRect(x0 + 2, top + 2, w - 4, 7);
    // 状态灯：空态灰、倒计时绿、等待调度黄；10fps 脉冲，减动效时静止
    var lamp = view.state === "empty" ? pal.idle
      : (view.state === "due" ? pal.warn : pal.ok);
    ctx.globalAlpha = view.state === "empty" ? 0.7
      : (S.reduced ? 0.85 : 0.55 + 0.45 * Math.abs(Math.sin(S.ticks * 0.12)));
    fillOval(ctx, x0 + 7, top + 5, 2, 2, lamp);
    ctx.globalAlpha = 1;
    // 两道抽象字迹：只示意「这是一块告示牌」，不呈现任何真实内容（名字 / 倒计时都在弹窗里）
    ctx.globalAlpha = 0.35;
    ctx.fillStyle = pal.mark;
    ctx.fillRect(x0 + 4, top + 11, w - 8, 1);
    ctx.fillRect(x0 + 4, top + 14, w - 12, 1);
    ctx.globalAlpha = 1;
    // 挂牌的钉：板面下沿两枚，交代「这块是挂上去的」（压字迹之上）
    ctx.fillStyle = pal.metalLight;
    ctx.fillRect(x0 + 4, top + h - 5, 2, 2);
    ctx.fillRect(x0 + w - 6, top + h - 5, 2, 2);
    // 整块板面都是热区；点它打开待发信息弹窗（空态也开，只报「暂无待发」），
    // 也不会穿透到背后的机台。标题贴在牌面上方，明细不进牌面。
    S.noticeboard = {
      x: x0, y: top, w: w, h: h, state: view.state, key: view.key,
      titleX: cx, titleY: top - 2,
    };
    S.hits.push({ type: "upcoming", key: view.key || "",
                  x: cx, y: top + Math.round(h / 2), rw: Math.round(w / 2),
                  rh: Math.round(h / 2) });
  }

  /** 待发月台的残留半边：厅左前方那堆「到点没抢到并发额度」的排队包裹。

      待发那半边已收成告示牌；排队这半边保留包裹形态（警示色 + 轻微抖动），
      就是计数条上「排队 N」的可视化。 */
  function drawUpcoming(ctx, pal) {
    S.noticeboard = null;
    if (!S.world) return;
    var z = S.cam.zoom;
    var hall = S.world.hall;
    var leftDock = viewPoint(hall.x - 34, hall.y + 22);
    var gapX = Math.max(12, Math.round(22 * z),
      Math.ceil(44 * S.bufW / S.cssW));
    var gapY = Math.max(4, Math.round(gapX * 0.5));
    var s = Math.max(2, Math.round(7 * z));
    var hh = Math.max(1, Math.round(s * 0.5));
    var h = Math.max(2, Math.round(s * 1.4));
    var boxBackOffset = Math.max(1, Math.round(hh * 0.5));
    var boxBottomOffset = hh - boxBackOffset;
    var labelTail = Math.ceil(16 * S.bufH / S.cssH);
    var labelGap = Math.max(1, Math.round(S.bufH / S.cssH));
    var labelOffset = boxBottomOffset + labelTail + labelGap;

    drawNoticeboard(ctx, pal);

    // 排队数来自计数条：明细来自 waiting（额度之外真正在等的那几条）。
    // 只有计数没有明细时（旧快照 / 编排节点排队）退回按计数画，不编造等待时长。
    var st = activeState();
    var queued = Number(st && st.counters && st.counters.queued) || 0;
    var waits = queuedWaits();
    var queuedDraw = Math.min(QUEUED_MAX_DRAW, Math.max(queued, waits.length));
    // 先清掉上一帧的排布：场院空了还留着旧坐标，标签会画在空场院上
    S.queuedLane = null;
    var queuedPositions = [];

    /** 一只立在月台线上的包裹：接地影子 + 三面体箱 + 顶面高光。 */
    function parcel(x, groundY, top, body, side) {
      // 接地影子比箱底略宽，从两侧探出来——包裹是「摆在」月台上，不是飘着。
      fillOval(ctx, x, groundY, Math.max(2, Math.round(s * 1.1)),
               Math.max(1, Math.round(s * 0.42)), pal.shadow);
      var by = groundY - boxBackOffset;
      drawIsoBoxAt(ctx, x, by, s + 1, hh + 1, h + 1,
                   pal.crateDark, pal.crateDark, pal.crateDark);
      drawIsoBoxAt(ctx, x, by, s, hh, h, top, body, side);
      ctx.fillStyle = pal.crateLight;
      ctx.fillRect(x - s, by - h, Math.max(1, s * 2 - 1), 1);
      ctx.fillStyle = pal.crateDark;
      ctx.fillRect(x, by - h + hh, 1, h);
    }

    // 排队箱沿调度厅左下侧紧凑排列；最久一枚保留轻微抖动，标签跟随同一坐标。
    var firstQueued = null;
    for (var q = 0; q < queuedDraw; q += 1) {
      var wait = waits[q];
      var tier = wait ? WAIT_TIERS[wait.tier] : WAIT_TIERS[0];
      var wob = 0;
      if (!S.reduced && tier.amp > 0) {
        wob = Math.sin(S.ticks * tier.freq + q) * Math.max(tier.amp * 0.6, tier.amp * z);
      }
      var xq = Math.round(leftDock.x - q * gapX);
      var yq = Math.round(leftDock.y + q * gapY + wob);
      var qpos = { x: xq, y: yq };
      queuedPositions.push(qpos);
      if (q === 0) firstQueued = qpos;
      var body = pal[tier.tone] || pal.warn;
      parcel(xq, yq, body, body, pal.ink);
      S.hits.push({ type: "queued", key: wait ? wait.key : "",
                    x: xq, y: yq - Math.round(h * 0.6), r: Math.max(5, s * 1.6) });
    }
    // 标签锚点与箱体使用同一组位置。
    S.queuedLane = queuedDraw ? {
      positions: queuedPositions,
      label: firstQueued ? { x: firstQueued.x, y: firstQueued.y + labelOffset } : null,
      top: waits.length ? waits[0] : null,
    } : null;
  }

  /** 在途包裹位姿（纯函数）：沿产线路径连续插值 + 贴带高度 + 状态成色。
      旧版两处怪相在这里一并收掉：①步进按 16px 取整，拉近看包裹一顿一顿
      跳格——pointAt 本就吃小数 t，直接连续走；②挑高恒为 14，悬在带面上方
      像飘着——主带带面高 12、机台支线 7~9，取 12→8 线性缓降贴着带面滑。
      返回值带世界 y（wy），给动态画序当排序键。 */
  function packagePose(pal, pkg, now) {
    var progress = packageProgress(pkg, now);
    var step = progress;
    if (pkg.finish === "error" || pkg.finish === "cancelled") {
      // 失败 / 叫停：原路退回调度厅（4.5 秒走完），像被收回去重查
      step = 1 - clamp((now - pkg.finishedAt) / 4500, 0, 1);
    }
    var pos = pointAt(pkg.line.path, step);
    var vp = viewPoint(pos.x, pos.y);
    if (progress >= 1 && !pkg.finish) {
      // 到机台口等待加工：原地轻微呼吸，说明「活着在等」而不是丢件了
      // （屏幕像素口径——世界坐标会被投影放大，拉近时抖得不像话）
      vp.y += Math.sin(S.ticks * 0.5) * Math.max(1, vlen(1));
    }
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
      // 卡住：箱体压暗一档（打转在 drawPackage 里画）
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
    return {
      x: Math.round(vp.x), gy: Math.round(vp.y), wy: pos.y,
      lift: Math.round((12 - 4 * step) * S.cam.zoom),
      color: color, edge: edge, stall: stall,
    };
  }

  /** 在途包裹立体建模：与月台 / 排队包裹同一套三面体（勾边箱 + 顶面高光 +
      前缝 + 接地影子）。以前在途的是正面平贴方块——停着的包裹是立体的，
      一动起来变成纸片，同屏两种建模语言。返回箱顶 y，给停滞问号当锚点。 */
  function drawParcelBox(ctx, pal, x, gy, liftPx, color, edge) {
    var z = S.cam.zoom;
    var s = Math.max(2, Math.round(7 * z));
    var hh = Math.max(1, Math.round(s * 0.5));
    var h = Math.max(2, Math.round(s * 1.4));
    var by = gy - liftPx - Math.max(1, Math.round(hh * 0.5));
    // 接地影子比箱底略宽；外圈勾一档深色，三面体才从带面上立起来
    fillOval(ctx, x, gy + 1, Math.max(2, Math.round(s * 1.1)),
             Math.max(1, Math.round(s * 0.42)), pal.shadow);
    drawIsoBoxAt(ctx, x, by, s + 1, hh + 1, h + 1, pal.crateDark, pal.crateDark, pal.crateDark);
    drawIsoBoxAt(ctx, x, by, s, hh, h, color, color, edge);
    ctx.fillStyle = pal.crateLight;
    ctx.fillRect(x - s, by - h, Math.max(1, s * 2 - 1), 1);
    ctx.fillStyle = edge;
    ctx.fillRect(x, by - h + hh, 1, h);
    return by - h;
  }

  function drawPackage(ctx, pal, pkg, now) {
    var pose = packagePose(pal, pkg, now);
    var z = S.cam.zoom;
    var x = pose.x;
    if (pose.stall === "stale" && !S.reduced) {
      // 打转：只晃半格，看得出「卡住了」而不是「在跑」
      x += Math.round(Math.sin(S.ticks * 0.35) * Math.max(1, z));
    }
    var topY = drawParcelBox(ctx, pal, x, pose.gy, pose.lift, pose.color, pose.edge);
    // 卡住的标记：头顶一个问号，快超时冒一缕烟，已超时压一团黑烟。
    // 全是像素图形、不带文字——问号的形状本身就说明「不知道在等什么」。
    if (pose.stall === "stale") drawStallMark(ctx, pal, x, topY - Math.max(3, Math.round(5 * z)), z, false);
    else if (pose.stall === "nearTimeout") drawStallMark(ctx, pal, x, topY, z, true);
    else if (pose.stall === "timeout") drawTimeoutSmoke(ctx, pal, x, topY, z);
    // 记录点击热区
    S.hits.push({ type: "pkg", id: pkg.id, x: x, y: topY - Math.max(2, Math.round(5 * z)),
                  r: Math.max(5, Math.round(7 * z * 1.6)) });
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
    var maxLabels = labelCap();
    var st = activeState();
    var running = (st && st.counters && st.counters.running) || 0;
    var berth = S.berthLane;
    var hallText = "调度厅" + (running ? " · 在转 " + running + " 件" : "");
    if (berth) hallText += " · 额度 " + berth.used + "/" + berth.cap;
    placeLabel(labelFor(i++), S.world.hall.x, S.world.hall.y, hallText, "hall", 76, used);
    // 待发告示牌：只贴「待发」短标题（牌面太小，任务名 / 倒计时 / 条数一律不上面，
    // 明细点击后进弹窗）。中文一律走标签池，牌面上不画字。
    var board = S.noticeboard;
    if (board) {
      var bxs = S.cssW / S.bufW, bys = S.cssH / S.bufH;
      placeLabelAt(labelFor(i++), board.titleX * bxs, board.titleY * bys,
                   "待发", "board title");
    }
    // 排队最久的一枚位于左下侧；从绘制层复用其实际位置，抖动时标签同步移动。
    var qLane = S.queuedLane;
    if (qLane && qLane.top && qLane.label) {
      var topTier = qLane.top.tier;
      placeLabelAt(labelFor(i++), qLane.label.x * (S.cssW / S.bufW),
                   qLane.label.y * (S.cssH / S.bufH),
                   "等 " + fmtCountdown(qLane.top.waited),
                   topTier >= 2 ? "queued hot" : "queued");
    }
    // 厂区名牌优先
    var factories = S.world.factories.slice().sort(function (a, b) { return a.y - b.y; });
    factories.forEach(function (f) {
      if (i >= maxLabels - 8) return;
      var allLines = (f.project && f.project.lines) || [];
      var busy = allLines.filter(function (l) { return l.status === "running"; }).length;
      var text = f.name + (busy ? " · " + busy + " 忙碌" : "");
      var hidden = Math.max(0, allLines.length - machineCap());
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
          if (i >= maxLabels - 2) return;
          if (rec._vx == null) return;
          var sx = rec._vx * (S.cssW / S.bufW);
          var sy = rec._vy * (S.cssH / S.bufH);
          if (sx < -60 || sx > S.cssW + 60 || sy < 0 || sy > S.cssH) return;
          var mlabel = rec.line.name || rec.key;
          if (rec.line.is_team) mlabel = "团队 · " + mlabel;   // 与面板「团队」徽标同一口径
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
      if (i >= maxLabels - 1) return;
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

    // 出货卡车：纯表现层的倒计时，跑完就回收
    updateTrips(now);
    // 回放：推进虚拟时间、刷进度条、到点自动收队
    replayTick(now);

    // 运行时长文案每 5 秒刷一次面板（有焦点时不重建）
    if (now - S.lastPanel > 5000) {
      S.lastPanel = now;
      // 快照之外还有演示 / 手动补的数据：定期重算一次，成品垛不会停在旧数
      refreshProjectStats(true);
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

  /** 悬停的轻量中文提示：只说画面上真实存在的状态，不虚构关联。
      机台 / 车 / 叉车 / 羊同时会画一圈高亮（见绘制层），提示文字写回 canvas.title。 */
  function hoverTitle(hit) {
    if (!hit) return "";
    if (hit.type === "pkg") {
      var run = findRun(hit.id);
      return run ? ("包裹 #" + run.id + " · " + (run.name || "")) : "包裹";
    }
    if (hit.type === "line") {
      var rec = findLine(hit.key);
      return rec ? ("机台 · " + (rec.line.name || rec.key) + " · " + lineStateText(rec.line)) : "";
    }
    if (hit.type === "upcoming") {
      // 牌面只给短标题 + 状态灯，这里补一句「有多少项 / 点开看明细」，
      // 空态也说清能点（点了会开弹窗报「暂无待发」）。
      var boardView = S.noticeboardView;
      var n = (boardView && boardView.count) || 0;
      return n ? ("待发告示牌 · " + n + " 项（点击查看）")
               : "待发告示牌 · 暂无待发（点击查看）";
    }
    if (hit.type === "queued") {
      var recQ = findLine(hit.key);
      return recQ ? ("排队 · " + (recQ.line.name || recQ.key)) : "";
    }
    if (hit.type === "truck") {
      var trip = null;
      S.trips.forEach(function (t) { if (String(t.pid) === String(hit.pid)) trip = t; });
      var phase = trip ? FX.tripPose(trip, nowMs()).phase : "";
      return "出货卡车 · " + ({ approach: "正在进场", wait: "等候装货", dock: "停靠装货位",
        load: "装货中", leave: "离场" }[phase] || "在场内");
    }
    if (hit.type === "forklift") return "叉车 · 托盘与车斗之间搬货";
    if (hit.type === "sheep") {
      var f = factoryById(hit.pid);
      if (!f) return "";
      var pose = FX.workerPose(f, nowMs(), S.reduced);
      var what = { patrol: "巡检中", pause: "巡检停留", gate: "在门口值守", idle: "待命" }[pose.state] || "在场";
      return "工头小羊 · " + what + (S.reduced ? "" : "（点一下挥手）");
    }
    return "";
  }

  function factoryById(pid) {
    if (!S.world) return null;
    for (var i = 0; i < S.world.factories.length; i += 1) {
      if (String(S.world.factories[i].id) === String(pid)) return S.world.factories[i];
    }
    return null;
  }

  /** 悬停信息牌：跟随命中点的轻量 DOM（中文文案不入 canvas）。
      文本与 hoverTitle 同源；隐藏 / 显示都不接收指针事件。 */
  function showHoverTip(hit, ev) {
    if (!dom.tip) return;
    var text = hoverTitle(hit);
    if (!text) {
      hideHoverTip();
      return;
    }
    var rect = dom.stage.getBoundingClientRect();
    var width = dom.stage.clientWidth || rect.width;
    var height = dom.stage.clientHeight || rect.height;
    var px = (ev.clientX - rect.left) * width / (rect.width || width);
    var py = (ev.clientY - rect.top) * height / (rect.height || height);
    var x = px + 14;
    var y = py + 18;
    dom.tip.textContent = text;
    dom.tip.style.display = "";
    // 贴边收拢：信息牌不探出舞台
    var w = dom.tip.offsetWidth || 120;
    var h = dom.tip.offsetHeight || 24;
    if (x + w > width - 8) x = Math.max(4, px - w - 12);
    if (y + h > height - 8) y = Math.max(4, py - h - 10);
    dom.tip.style.left = Math.round(x) + "px";
    dom.tip.style.top = Math.round(y) + "px";
  }

  function hideHoverTip() {
    if (dom.tip && dom.tip.style.display !== "none") dom.tip.style.display = "none";
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
    // 悬停提示 + 高亮：命中记录进 S.hover（绘制层每帧读取）；reduced 模式的
    // 主循环不重绘，hover 变化时补一帧让高亮即时出现/消失。
    S.hoverPointer = { clientX: ev.clientX, clientY: ev.clientY };
    var pt = canvasPoint(ev);
    var hit = hitTest(pt.x, pt.y);
    var prev = S.hover;
    S.hover = hit || null;
    dom.canvas.title = hoverTitle(hit);
    showHoverTip(hit, ev);
    if (S.reduced && (S.hover || prev) &&
        (String(S.hover && S.hover.type) !== String(prev && prev.type) ||
         String(S.hover && S.hover.key) !== String(prev && prev.key) ||
         String(S.hover && S.hover.pid) !== String(prev && prev.pid))) {
      render();
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
    // 点告示牌 = 打开待发信息弹窗（空态也开，只报「暂无待发」）：牌面太小放不下
    // 明细，明细在居中只读弹窗里看，点其中一条才开原产线抽屉。全程不派工——
    // 还没到点的那一轮不该因为一次误触就提前跑起来。
    if (hit && hit.type === "upcoming") {
      openUpcomingDialog();
      return;
    }
    // 点排队包裹 = 看这条产线为什么卡着（抽屉里有状态与排期），同样不派工：
    // 它已经在等了，再派一次只会被后端按 already_running 挡回来。
    if (hit && hit.type === "queued" && hit.key) {
      openLineDrawer(hit.key, { focus: true });
      return;
    }
    // 点工头小羊：短挥手一下（纯表现；减少动态效果时不挥手）
    if (hit && hit.type === "sheep" && !S.reduced) {
      var f = factoryById(hit.pid);
      if (f) {
        f.waveUntil = nowMs() + WAVE_MS;
        render();
      }
    }
  }

  function refreshHover() {
    if (!S.hoverPointer || !dom.canvas) return;
    var pt = canvasPoint(S.hoverPointer);
    S.hover = hitTest(pt.x, pt.y) || null;
    dom.canvas.title = hoverTitle(S.hover);
    showHoverTip(S.hover, S.hoverPointer);
  }

  function onPointerLeave() {
    var hadHover = !!S.hover;
    S.hover = null;
    S.hoverPointer = null;
    hideHoverTip();
    if (dom.canvas) dom.canvas.title = "";
    pointerPress = null;
    if (hadHover && S.reduced) render();
  }

  function onWheel(ev) {
    ev.preventDefault();
    // 场景保持固定全景；滚轮不改变比例，也不带动页面滚动。
  }

  function hitTest(x, y) {
    // 包裹（小目标）与告示牌优先：在跑的、排队的、牌面与动态对象（车 / 叉车 / 羊）
    // 都是小目标，一起判——它们必须压过脚下的机台热区
    for (var i = S.hits.length - 1; i >= 0; i -= 1) {
      var h = S.hits[i];
      // 告示牌给的是矩形（整块板面比一枚包裹大得多，圆热区在四角会漏点）
      if (h.rw != null) {
        if (h.type === "upcoming" && Math.abs(x - h.x) <= h.rw
            && Math.abs(y - h.y) <= h.rh) return h;
        continue;
      }
      if ((h.type === "pkg" || h.type === "upcoming" || h.type === "queued"
           || h.type === "sheep" || h.type === "truck" || h.type === "forklift")
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

  // ---------- 抽屉转发 ----------
  // 抽屉与产线操作实现在 app-factory-drawer.js（经 window.FX 运行时查表）；
  // 本文件内的调用点保持裸名不变，由这组同签名转发函数承接。
  function openLineDrawer(key, opts) { FX.openLineDrawer(key, opts); }
  function openUpcomingDialog() { FX.openUpcomingDialog(); }
  function openRunDrawer(id) { FX.openRunDrawer(id); }
  function openAllRunsDrawer(initial) { FX.openAllRunsDrawer(initial); }
  function openLiveDrawer() { FX.openLiveDrawer(); }
  function openQueueDrawer() { FX.openQueueDrawer(); }
  function openAttentionDrawer() { FX.openAttentionDrawer(); }
  function openNewLineDrawer(presetProject) { FX.openNewLineDrawer(presetProject); }
  function openSettingsDrawer() { FX.openSettingsDrawer(); }
  function saveConfig(patch, btn, msgEl) { FX.saveConfig(patch, btn, msgEl); }
  function applyRunProgress(payload) { FX.applyRunProgress(payload); }
  function closeDrawer() { FX.closeDrawer(); }

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
    // 本轮结束 → 成品垛 / 返工堆重新算一遍（顺带可能放出一辆出货的车）
    refreshProjectStats(false);
    renderCounters();
    // 走 refreshPanelStates：滚动位置保持 + 焦点在面板控件上时不动面板
    refreshPanelStates();
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
  /** 演示产线：period 为演示周期（秒），0 表示无排期（手动型，可被随机轮换拉起）。 */
  function demoLineCron(id, name, trigger, next, status, last, lastAt, period, team) {
    var line = {
      key: lineKeyFor("cron", id), kind: "cron", id: id, project_id: 0, name: name,
      status: status || "idle", trigger_text: trigger, next_run_at: next || 0,
      last_status: last || "", last_run_at: lastAt || 0, running_run_id: null, nodes: [],
      period_s: period || 0,
    };
    // 团队演示线（定时团队 §14）：形状与 factory.state 透传的行一致，让样板厂
    // 也能演示「团队产线」的呈现（徽标 / 模板 / 成员 / 总管），只在内存
    if (team) {
      line.is_team = true;
      line.team_template = team.template || "";
      line.team_meta = {
        found: team.found !== false, members: team.members || 0,
        director_ai: team.directorAI !== false, director_model: "",
      };
    }
    return line;
  }
  function demoLinePipe(id, name, status, nodes) {
    return {
      key: lineKeyFor("pipeline", id), kind: "pipeline", id: id, project_id: 0, name: name,
      status: status || "idle", trigger_text: "编排", next_run_at: 0,
      last_status: "", last_run_at: 0, running_run_id: null, nodes: nodes || [],
    };
  }

  function buildDemoState(cfg) {
    var now = nowMs() / 1000;
    var p1 = {
      id: -1, name: "样板 · 内容厂", root_ok: true,
      lines: [
        demoLineCron(-11, "晨间简报", "每天 09:00", now + 3600, "running", "ok", now - 7200, 86400),
        // 指定待发：45 秒后到点（演示里几十秒就能看到「到点才启动」），
        // 之后每 15 分钟一轮，牌上稳定显示倒计时
        demoLineCron(-12, "竞品巡检", "每 15 分钟", now + 45, "idle", "ok", now - 1200, 900),
        demoLineCron(-13, "素材归档", "每隔 120 分钟", now + 1800, "paused", "ok", now - 86400, 7200),
        // 无排期（手动型）：交给随机轮换，演示的日常动静靠它们
        demoLineCron(-14, "素材转码", "手动", 0, "idle", "ok", now - 5400, 0),
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
        demoLineCron(-22, "依赖体检", "每周一 08:30", now + 86400, "idle", "error", now - 3600, 604800),
        demoLineCron(-23, "临时打包", "手动", 0, "idle", "ok", now - 4800, 0),
      ],
    };
    var p3 = {
      id: -3, name: "样板 · 资料厂", root_ok: true,
      lines: [
        demoLineCron(-31, "文档快照", "每天 18:00", now + 5400, "idle", "empty", now - 10800, 86400),
        // 团队演示线：让样板厂认得出「团队产线」（面板徽标 + 机台旗标）
        demoLineCron(-33, "值班报告团队", "每天 20:00", now + 7200, "idle", "ok", now - 43200, 86400,
                     { template: "值班小队", members: 3 }),
        // 无排期（手动型）：保留给随机轮换，演示不会因为没有近排期机台而冷场
        demoLineCron(-32, "临时批处理", "手动", 0, "idle", "ok", now - 6000, 0),
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
        { kind: "error", project_id: -2, line_key: lineKeyFor("cron", -22), text: "依赖体检 · 上次失败" },
        { kind: "stopped", project_id: -1, line_key: lineKeyFor("cron", -13), text: "素材归档 · 停工" },
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

  /** 演示产线是否「排了期」：排了期的到点才启动，随机轮换不许提前拉起。 */
  function demoReserved(line, nowSec) {
    return (Number(line.next_run_at) || 0) > nowSec;
  }

  /** 起一轮演示运行（纯内存）：随机轮换与「到点启动」走同一条路。 */
  function startDemoRun(st, line, sec) {
    var id = -2000 - (S.demoSeq += 1);
    line.status = "running";
    line.running_run_id = id;
    st.runs.unshift(demoRun(id, "cron", line.id, line.project_id, line.name,
                            demoProjectName(line.project_id), "running", sec, null, ""));
    st.counters.running += 1;
  }

  function demoTick(now) {
    var st = S.demo;
    if (!st) return;
    var sec = now / 1000;
    // 收尾：运行超过 18 秒的演示运行；排了期的产线顺延到下一轮
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
        // 排期顺延：跑完推到下一个周期（无周期的手动型保持 0，继续参与轮换）
        if (Number(line.period_s) > 0) line.next_run_at = sec + Number(line.period_s);
      }
      st.counters.running = Math.max(0, st.counters.running - 1);
      if (status === "done") {
        st.counters.done_today += 1;
        // 样板厂的出货与真实路径同一条 spawnTrip 规则：演示运行做成也放车
        var doneRec = S.world && S.world.lines.get(runKey(r));
        if (doneRec && doneRec.factory) spawnTrip(doneRec.factory);
      } else {
        st.counters.attention += 1;
      }
    });

    // 样板厂首入约 2 秒放第一组装运（别让观战干等），之后每 ~14 秒补一轮，
    // 让「进场→等候→停靠→装货→离场」在演示里随时都看得见。
    var tripEvery = st.demoTripped ? 140 : 18;
    if (S.world && S.ticks > 0 && S.ticks % tripEvery === 0) {
      var fs = S.world.factories;
      var pickF = fs[Math.abs(hashKey("trip" + Math.floor(S.ticks / tripEvery))) % fs.length];
      if (pickF) {
        spawnTrip(pickF);
        st.demoTripped = true;
      }
    }

    // 到点启动：排了期且已到点的产线此刻才起（牌上数到 0 就开跑）
    st.projects.forEach(function (p) {
      (p.lines || []).forEach(function (l) {
        if (l.kind !== "cron" || l.status !== "idle") return;
        var at = Number(l.next_run_at) || 0;
        if (!at || sec < at) return;
        startDemoRun(st, l, sec);
      });
    });

    // 每 ~6 秒起一枚新包裹：只在没排期的机台上轮换，排了期的到点才跑
    if (S.ticks % 60 === 0) {
      var candidates = [];
      st.projects.forEach(function (p) {
        (p.lines || []).forEach(function (l) {
          if (l.kind !== "cron" || l.status !== "idle") return;
          if (demoReserved(l, sec)) return;
          candidates.push(l);
        });
      });
      if (candidates.length) {
        var pick = candidates[Math.abs(hashKey(String(S.ticks))) % candidates.length];
        startDemoRun(st, pick, sec);
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

  // ---------- FX 导出（绘制层绑定 + 测试探针入口） ----------
  FX.S = S; FX.dom = dom;
  FX.PAL_DAY = PAL_DAY; FX.PAL_NIGHT = PAL_NIGHT;
  FX.PIPELINE_MAX_NODES = PIPELINE_MAX_NODES;
  // 场景侧的「痕迹」口径：托盘几箱、返工堆几箱、沙漏看多远、车跑多久
  FX.PALLET_SIZE = PALLET_SIZE; FX.PALLET_MAX = PALLET_MAX;
  FX.REWORK_MAX = REWORK_MAX; FX.UPCOMING_GLYPH_S = UPCOMING_GLYPH_S;
  FX.TRIP_MS = TRIP_MS;
  FX.statFor = statFor; FX.refreshProjectStats = refreshProjectStats;
  FX.TRIP_PHASES = TRIP_PHASES; FX.TRIP_MAX = TRIP_MAX;
  FX.WAVE_MS = WAVE_MS;
  FX.spawnTrip = spawnTrip; FX.updateTrips = updateTrips;
  FX.toggleReplay = toggleReplay; FX.stopReplay = stopReplay;
  FX.TILE_W = TILE_W; FX.TILE_H = TILE_H;
  FX.RUN_STATUS = RUN_STATUS; FX.RUN_OUTCOME = RUN_OUTCOME;
  FX.ERROR_KIND = ERROR_KIND; FX.TRIGGER_TEXT = TRIGGER_TEXT;
  FX.activeConfig = activeConfig; FX.activeState = activeState;
  FX.hashKey = hashKey; FX.lineStall = lineStall; FX.clamp = clamp; FX.ringPts = ringPts;
  // 场景构建层与抽屉层的绑定源
  FX.nowMs = nowMs; FX.factoryReload = factoryReload;
  FX.findLine = findLine; FX.findRun = findRun; FX.fitCamera = fitCamera;
  FX.fmtDur = fmtDur; FX.fmtTs = fmtTs; FX.freshProgress = freshProgress;
  FX.hasRealLines = hasRealLines; FX.lineStateText = lineStateText; FX.liveStale = liveStale;
  FX.refreshPanelStates = refreshPanelStates; FX.refreshWeather = refreshWeather;
  FX.renderAll = renderAll; FX.renderAttentionNotice = renderAttentionNotice;
  FX.renderPanel = renderPanel; FX.renderWeatherChip = renderWeatherChip;
  FX.runElapsed = runElapsed; FX.scheduleResync = scheduleResync;
  FX.applyNavVisibility = applyNavVisibility;
  FX.drawBerths = drawBerths; FX.drawPackages = drawPackages; FX.drawUpcoming = drawUpcoming;
  FX.collectPackageItems = collectPackageItems;   // 实时包裹进动态画序（绘制层合并）
  FX.drawNoticeboard = drawNoticeboard;
  FX.refreshFactoryAlerts = refreshFactoryAlerts; FX.refreshUpcoming = refreshUpcoming;
  // 待发告示牌：口径常量 + 纯函数（绘制层、标签层与测试同源）
  FX.UPCOMING_WINDOW_S = UPCOMING_WINDOW_S; FX.UPCOMING_MAX = UPCOMING_MAX;
  FX.UPCOMING_OVERDUE_S = UPCOMING_OVERDUE_S; FX.UPCOMING_NAME_MAX = UPCOMING_NAME_MAX;
  FX.UPCOMING_BOARD = UPCOMING_BOARD; FX.truncName = truncName;
  FX.noticeboardState = noticeboardState; FX.upcomingList = upcomingList;
  FX.fmtCountdown = fmtCountdown;
  // 样板厂（只存在内存）：demoTick 的到点启动 / 顺延排期用纯函数口径，测试可直调
  FX.buildDemoState = buildDemoState; FX.demoTick = demoTick;
  FX.demoLineCron = demoLineCron; FX.demoLinePipe = demoLinePipe;
  FX.demoReserved = demoReserved; FX.startDemoRun = startDemoRun;
  FX.refreshFactoryMood = refreshFactoryMood;
  FX.updateLabels = updateLabels;
  // 测试探针（test_factory_scene_models / test_factory_panel_logic）经 FX 取内部函数
  FX.resizeScene = resizeScene; FX.fitCamera = fitCamera;
  FX.onPointerDown = onPointerDown; FX.onPointerMove = onPointerMove;
  FX.onPointerUp = onPointerUp; FX.onPointerLeave = onPointerLeave; FX.onWheel = onWheel;
  FX.refreshHover = refreshHover;
  FX.filterProjects = filterProjects; FX.matchFilterMode = matchFilterMode;
  FX.filterActive = filterActive; FX.fmtTokens = fmtTokens; FX.lineKeyFor = lineKeyFor;
  FX.teamLineText = teamLineText;
  FX.machineCap = machineCap; FX.labelCap = labelCap;

  window.factoryMount = factoryMount;
  window.factoryUnmount = factoryUnmount;
  window.factoryReload = factoryReload;
  window.factoryOnEvent = factoryOnEvent;
  window.factoryRepaint = factoryRepaint;
  window.factoryOnAutomationChanged = factoryOnAutomationChanged;
})();
