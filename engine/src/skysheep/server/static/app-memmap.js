// app-memmap.js —— 记忆地图（项目演化的可视化：时间线 + 热力 + 主题图谱）
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有声明与纯数据常量，零加载期执行；ResizeObserver 观察块（加载期
// 立即执行）与工具条接线仍留在 app.js 原位——RO 回调异步触发时本文件早已
// 加载完毕，引用 mapHeatWeeksFor / renderMapAside 安全。

// ---------- 记忆地图：项目演化的可视化（时间线 + 主题图谱） ----------
// 数据来自 map.get（会话/事件/文件足迹/热力/全局记忆/演化摘要一次装配）；
// 摘要生成走 map.generate（后台任务，完成经 map_updated 事件广播后重拉）。
// 渲染沿用 mountPipelineGraph 的路数：纯 HTML/SVG 自绘、配色全走主题变量、
// ResizeObserver 防抖重排；图谱布局是自写的定环初始化力导向（不引 d3）。

const mapState = {
  projectId: 0,     // 0 = 跟随当前项目（后端解析，切项目自动跟上）
  range: "90",      // 30 | 90 | 0（全部）
  view: "timeline", // timeline | graph
  data: null,       // map.get 载荷
  fileFilter: null, // 文件足迹 chip 点选后的过滤（完整路径）
  genBusy: false,   // 摘要生成中（按钮禁用转文案；事件回来后复位）
};
let mapProjects = []; // project.list 缓存（项目下拉）

function mapPad(n) { return pad2(n); }
function mapDayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${mapPad(d.getMonth() + 1)}-${mapPad(d.getDate())}`;
}
function mapMonthKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${mapPad(d.getMonth() + 1)}`;
}
function mapMonthLabel(key) {
  const [y, m] = key.split("-");
  return `${y}年${parseInt(m, 10)}月`;
}
// 会话节点按 token 量分档（圆点大小/色深随之分五档，一眼看出投入量）
function mapTokClass(s) {
  const t = (s.in_tokens || 0) + (s.out_tokens || 0);
  return t >= 50000 ? "t4" : t >= 20000 ? "t3" : t >= 5000 ? "t2" : t > 0 ? "t1" : "t0";
}
function mapIsFork(title) {
  return SUB_MARKS.some((mk) => String(title || "").startsWith(mk));
}
function mapBasename(p) {
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return i >= 0 ? p.slice(i + 1) : p;
}
// 热力图周数按底栏可用宽度算（格 9px + 缝 2px）：宽就多铺历史、窄就少铺，
// 能放几列放几列，格子恒完整不裁边。宽 - 2px 是亚像素舍入冗余（clientWidth
// 取整与设备像素换算可能让内容比可用宽多出零点几像素）；上限与后端
// MAP_HEAT_WEEKS 对齐
function mapHeatWeeksFor(width) {
  return Math.max(1, Math.min(260, Math.floor((width - 2) / 11)));
}

async function loadMemoryMap(force) {
  const tl = document.getElementById("map-timeline");
  // 记忆候选是全局的（不随项目/时间窗过滤），与地图载荷并行各拉各的；
  // 地图挂了（如还没有项目）也不影响候选区
  loadMemoryCandidates();
  try {
    if (!mapProjects.length || force) {
      const pl = await request("project.list").catch(() => ({ projects: [] }));
      mapProjects = pl.projects || [];
    }
    const params = {};
    if (mapState.projectId) params.project_id = mapState.projectId;
    // 「全部」必须显式发 start_ts=0：不发 = 后端缺省只查近 90 天；
    // 协议约定 0 = 不限起点（后端按显式 0 识别，不走 or 缺省）
    if (mapState.range === "0") params.start_ts = 0;
    else {
      const days = parseInt(mapState.range, 10);
      if (days > 0) params.start_ts = Date.now() / 1000 - days * 86400;
    }
    mapState.data = await request("map.get", params);
    renderMapChrome();
    renderMapView();
    renderMapAside();
  } catch (e) {
    if (tl) tl.innerHTML = `<div class="map-empty dim">${escapeHtml(e.message)}</div>`;
    document.getElementById("map-aside").classList.add("hidden");
  }
}

// ---------- 轮次记忆沉淀：待审候选（memory.candidates / candidate_adopt / candidate_ignore） ----------
// 候选制「不打扰」：模型每轮挑出的候选先进待审列表，用户在这里采纳（写入全局记忆）
// 或忽略（不再提出）；开关默认关。入口放记忆地图：空列表且未开沉淀时整块隐藏。

let distillState = { enabled: false, pending: [] };

async function loadMemoryCandidates() {
  const box = document.getElementById("map-distill");
  if (!box) return;
  let r;
  try {
    r = await request("memory.candidates");
  } catch (e) {
    box.classList.add("hidden"); // 方法不可用（旧后端/远端被拒）：入口不出现
    return;
  }
  distillState = { enabled: !!r.enabled, pending: r.pending || [] };
  renderMemoryCandidates();
}

function renderMemoryCandidates() {
  const box = document.getElementById("map-distill");
  const body = document.getElementById("map-distill-body");
  if (!box || !body) return;
  const n = distillState.pending.length;
  // 空列表折叠不显眼：没开沉淀且没有候选时整块藏掉；有候选才默认展开
  if (!n && !distillState.enabled) {
    box.classList.add("hidden");
    return;
  }
  const wasHidden = box.classList.contains("hidden");
  box.classList.remove("hidden");
  // 有候选且刚从隐藏态出来才自动展开；用户已展开着就保持（清空到 0 条时
  // 不强收——开关行在里面，收起来会藏掉用户刚点过的开关）
  if (n) box.open = wasHidden || box.open;
  const title = document.getElementById("map-distill-title");
  if (title) title.textContent = n ? `记忆候选 · ${n} 条待审` : "记忆候选 · 暂无待审";
  const tgl = `<label class="toggle-row check-row map-distill-toggle"
    title="开启后每轮对话收尾，用当前模型挑出「值得长期记住」的候选（每轮至多 3 条，失败静默），采纳后才写入全局记忆。额外消耗一次模型调用；寒暄、过短的轮次不会触发。">
    <input type="checkbox" id="map-distill-toggle"${distillState.enabled ? " checked" : ""}>
    <span>轮次自动沉淀</span></label>`;
  const rows = distillState.pending.map((c) =>
    `<div class="map-distill-item" data-cid="${escapeHtml(c.id)}">
       <span class="map-distill-text" title="${escapeHtml(c.context || "（无上下文摘录）")}">${escapeHtml(c.text)}</span>
       <span class="map-distill-ops">
         <button class="rp-mini" data-op="adopt" title="写入全局记忆（与既有记忆重复会自动跳过）">采纳</button>
         <button class="rp-mini" data-op="ignore" title="丢弃这条，同类信息之后不再被提出">忽略</button>
       </span>
     </div>`).join("");
  body.innerHTML = tgl + (rows ||
    '<div class="dim small map-distill-empty">没有待审候选。开启沉淀后，对话里值得长期记住的信息会出现在这里，由你决定记不记。</div>');
  const t = document.getElementById("map-distill-toggle");
  if (t) t.onchange = async (e) => {
    const el = e.target;
    try {
      const r = await request("memory.distill_save", { enabled: el.checked });
      distillState.enabled = !!r.enabled;
      el.checked = distillState.enabled; // 以落库值为准
    } catch (err) {
      el.checked = !el.checked; // 失败回拨
      addNotice("保存失败: " + err.message);
      return;
    }
    renderMemoryCandidates();
  };
  body.querySelectorAll(".map-distill-item").forEach((el) => {
    const cid = el.dataset.cid;
    el.querySelector('[data-op="adopt"]').onclick = async () => {
      try {
        const r = await request("memory.candidate_adopt", { candidate_id: cid });
        addNotice(r.adopted ? "已写入全局记忆，之后所有会话立即生效"
          : (r.reason || "没有采纳：记忆里已有相同内容"));
      } catch (err) { addNotice("采纳失败: " + err.message); }
      loadMemoryCandidates();
    };
    el.querySelector('[data-op="ignore"]').onclick = async () => {
      try {
        await request("memory.candidate_ignore", { candidate_id: cid });
      } catch (err) { addNotice("忽略失败: " + err.message); }
      loadMemoryCandidates();
    };
  });
}

// 页头控件与项目下拉（数据回来后同步，选择器只发事件不改渲染）
function renderMapChrome() {
  const d = mapState.data;
  const sel = document.getElementById("map-project");
  sel.innerHTML = '<option value="0">当前项目</option>' + mapProjects.map((p) =>
    `<option value="${p.id}">${escapeHtml(p.name || "未命名项目")}</option>`).join("");
  sel.value = String(mapState.projectId || 0);
  // 跟随当前项目时把实际项目名露出来，用户不用猜「当前」是哪个
  if (!mapState.projectId && d) sel.options[0].textContent = "当前项目 · " + (d.project.name || "未命名");
  document.getElementById("map-range").value = mapState.range;
  document.querySelectorAll("#map-segbar button").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === mapState.view);
  });
  document.getElementById("map-timeline").classList.toggle("hidden", mapState.view !== "timeline");
  document.getElementById("map-graph").classList.toggle("hidden", mapState.view !== "graph");
  document.getElementById("map-aside").classList.toggle("hidden", mapState.view !== "timeline");
  updateMapGenBtn(d && d.generating);
  const autoBtn = document.getElementById("map-auto");
  autoBtn.classList.toggle("on", !!(d && d.config && d.config.auto_digest));
  autoBtn.title = d && d.config && d.config.auto_digest
    ? "自动生成演化摘要：已开启（项目累计足量新会话时后台自动总结；点击关闭）"
    : "自动生成演化摘要：已关闭（点击开启；每次生成都是一次真实模型调用）";
}

function updateMapGenBtn(busy) {
  const btn = document.getElementById("map-gen");
  const on = busy === undefined ? (mapState.genBusy || !!(mapState.data && mapState.data.generating)) : busy;
  btn.disabled = on;
  btn.textContent = on ? "⏳ 生成中…" : "✦ 生成演化摘要";
}

// 时间线：会话/事件/记忆/阶段里程碑按月分组铺开；overview 摘要卡置顶
function renderMapView() {
  if (mapState.view === "graph") return renderMapGraph();
  return renderMapTimeline();
}

function renderMapTimeline() {
  const d = mapState.data;
  const box = document.getElementById("map-timeline");
  if (!d) { box.innerHTML = ""; return; }
  const fileOk = mapState.fileFilter
    ? new Set((d.files.find((f) => f.path === mapState.fileFilter) || {}).session_ids || [])
    : null;
  const items = [];
  (d.sessions || []).forEach((s) => {
    if (fileOk && !fileOk.has(s.id)) return;
    items.push({ ts: s.created_at, type: "sess", s });
  });
  (d.events || []).forEach((e) => items.push({ ts: e.ts, type: "evt", e }));
  (d.memories || []).forEach((m) => {
    // 记忆条目只有日期（本地零点），铺到当天；全局条目标「全局」徽记
    items.push({ ts: new Date(m.date + "T00:00:00").getTime() / 1000, type: "mem", m });
  });
  (d.digests || []).forEach((dg) => {
    if (dg.kind === "overview") return;
    items.push({ ts: dg.start_ts, type: "phase", dg });
  });
  items.sort((a, b) => a.ts - b.ts);
  const overview = (d.digests || []).find((dg) => dg.kind === "overview");
  let html = "";
  if (overview && overview.summary) {
    const highs = (overview.highlights || [])
      .map((h) => `<li>${escapeHtml(h)}</li>`).join("");
    html += `<details class="map-overview"><summary>✦ ${escapeHtml(overview.title || "项目总览")}` +
      `<span class="map-dim">${escapeHtml((d.project || {}).name || "")}</span></summary>` +
      `<div class="map-phase-body"><p>${escapeHtml(overview.summary)}</p>${highs ? `<ul>${highs}</ul>` : ""}</div></details>`;
  }
  if (!items.length) {
    html += `<div class="map-empty dim">${fileOk ? "这个文件的时间窗内没有会话"
      : "时间窗内还没有会话——回去聊点什么，地图就会长出来"}</div>`;
  }
  let curMonth = "";
  items.forEach((it) => {
    const mk = mapMonthKey(it.ts);
    if (mk !== curMonth) {
      if (curMonth) html += "</div></div>";
      curMonth = mk;
      html += `<div class="map-month"><div class="map-month-name">${escapeHtml(mapMonthLabel(mk))}</div><div class="map-month-items">`;
    }
    html += mapItemHtml(it);
  });
  if (curMonth) html += "</div></div>";
  box.innerHTML = html;
  bindMapTimeline(box);
}

function mapItemHtml(it) {
  if (it.type === "sess") {
    const s = it.s;
    const tags = (s.tags || []).slice(0, 3).map((t) => `<span class="map-tag">${escapeHtml(t)}</span>`).join("");
    const dd = new Date(it.ts * 1000);
    return `<div class="map-item map-sess ${mapTokClass(s)}${s.archived ? " archived" : ""}" data-sid="${s.id}"` +
      ` title="${escapeHtml(s.title)} · ${s.msg_count} 条消息 · 点击打开会话">` +
      `<span class="map-dot"></span>` +
      `<span class="map-item-main"><span class="map-item-title">${escapeHtml(s.title || "（未命名）")}` +
      `${mapIsFork(s.title) ? ' <span class="map-fork">分叉</span>' : ""}${s.archived ? ' <span class="map-dim">已归档</span>' : ""}</span>` +
      `<span class="map-item-meta">${mapPad(dd.getMonth() + 1)}-${mapPad(dd.getDate())} · ${s.msg_count} 条${tags ? " " + tags : ""}</span>` +
      `</span></div>`;
  }
  if (it.type === "evt") {
    const mark = { task_created: "＋", task_done: "✔", pipeline: "⛓", cron: "⏰" }[it.e.kind] || "·";
    const label = { task_created: "新建任务", task_done: "完成任务", pipeline: "流水线", cron: "定时任务" }[it.e.kind] || "";
    const dd = new Date(it.ts * 1000);
    return `<div class="map-item map-evt"><span class="map-evt-mark">${mark}</span>` +
      `<span class="map-item-main"><span class="map-item-title">${escapeHtml(it.e.title)}</span>` +
      `<span class="map-item-meta">${label} · ${mapPad(dd.getMonth() + 1)}-${mapPad(dd.getDate())}</span></span></div>`;
  }
  if (it.type === "mem") {
    return `<div class="map-item map-mem" title="全局记忆条目（memory.md，跨项目）">` +
      `<span class="map-evt-mark">📌</span><span class="map-item-main">` +
      `<span class="map-item-title">${escapeHtml(it.m.text)}</span>` +
      `<span class="map-item-meta">${escapeHtml(it.m.date)} · 全局记忆</span></span></div>`;
  }
  // 阶段里程碑：可展开看摘要与要点
  const dg = it.dg;
  const dd = new Date(it.ts * 1000);
  const highs = (dg.highlights || []).map((h) => `<li>${escapeHtml(h)}</li>`).join("");
  const topics = (dg.topics || []).map((t) => `<span class="map-tag topic">${escapeHtml(t)}</span>`).join("");
  return `<details class="map-item map-phase" data-did="${dg.id}"><summary>` +
    `<span class="map-diamond">◆</span><span class="map-item-main">` +
    `<span class="map-item-title">${escapeHtml(dg.title)}</span>` +
    `<span class="map-item-meta">阶段 · 起于 ${mapPad(dd.getMonth() + 1)}-${mapPad(dd.getDate())} · ${ (dg.session_ids || []).length } 个会话</span>` +
    `</span></summary><div class="map-phase-body"><p>${escapeHtml(dg.summary || "")}</p>` +
    `${highs ? `<ul>${highs}</ul>` : ""}${topics ? `<div class="map-topics">${topics}</div>` : ""}</div></details>`;
}

function bindMapTimeline(box) {
  box.querySelectorAll(".map-sess").forEach((el) => {
    el.onclick = () => {
      const sid = el.dataset.sid;
      const s = (mapState.data.sessions || []).find((x) => x.id === sid);
      openTabForSession(sid, s ? s.title : "");
    };
  });
  box.querySelectorAll(".map-phase summary").forEach((el) => {
    // details/summary 原生开合，这里只做「同时只展开一个」的省心处理
    el.onclick = () => {
      const me = el.parentElement;
      if (me.open) return;
      box.querySelectorAll("details.map-phase[open]").forEach((d) => { if (d !== me) d.open = false; });
    };
  });
}

// 文件足迹 + 热力图（时间线视图的常驻底栏）
function renderMapAside() {
  const d = mapState.data;
  const filesBox = document.getElementById("map-files");
  const heatBox = document.getElementById("map-heat");
  const aside = document.getElementById("map-aside");
  if (!d) { aside.classList.add("hidden"); return; }
  aside.classList.toggle("hidden", mapState.view !== "timeline");
  // 文件足迹 chips：basename 为主、次数徽记；title 带完整路径。无记录时整行
  // 收起——一行加粗 label 说「没有」比不显示更抢眼；过滤指向的文件掉出
  // Top N 后 chip 没了，残留的过滤一并清掉，免得时间线被悄悄过滤成空
  const chips = (d.files || []).map((f) =>
    `<button class="map-file-chip${mapState.fileFilter === f.path ? " on" : ""}" data-path="${escapeHtml(f.path)}"` +
    ` title="${escapeHtml(f.path)} · ${f.count} 次改动 · 点击过滤时间线">${escapeHtml(mapBasename(f.path))}<i>${f.count}</i></button>`).join("");
  if (!chips) {
    if (mapState.fileFilter) {
      mapState.fileFilter = null;
      renderMapTimeline();
    }
    filesBox.innerHTML = "";
    filesBox.classList.add("hidden");
  } else {
    filesBox.classList.remove("hidden");
    filesBox.innerHTML = `<span class="map-aside-label">文件足迹</span>${chips}${mapState.fileFilter ? '<button class="map-file-clear" data-clear="1">✕ 清除过滤</button>' : ""}`;
    filesBox.querySelectorAll(".map-file-chip").forEach((b) => {
      b.onclick = () => {
        mapState.fileFilter = mapState.fileFilter === b.dataset.path ? null : b.dataset.path;
        renderMapTimeline();
        renderMapAside();
      };
    });
    const clearBtn = filesBox.querySelector(".map-file-clear");
    if (clearBtn) clearBtn.onclick = () => { mapState.fileFilter = null; renderMapTimeline(); renderMapAside(); };
  }

  // 热力图：GitHub 贡献图式（列=周，行=周一..周日），周数按底栏实际宽度
  // 自适应（拉宽窗口多铺历史，富余宽度不空在中间）；强度按当日 token 分档，
  // 悬停给明细，点击跳时间线对应月份
  const byDay = new Map((d.days || []).map((x) => [x.day, x]));
  const paintHeat = (budget) => {   // budget：底栏放得下的总列数（列=周）
    const now = new Date();
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    // 从当前周的周一开始往前铺 budget 整周：每列恒 7 天，本周没到的几天补
    // 空格——整图是规矩矩形，既不缺角也不会超宽被裁
    const endWeek = end.getTime() - ((end.getDay() + 6) % 7) * 86400000;
    let start = endWeek - (budget - 1) * 7 * 86400000;
    // 项目诞生晚于窗口起点时，起点裁到诞生所在周并收窄列数：不满宽就少铺，
    // 不拿一排「无活动」的空格凑满底栏（右侧留白比假数据诚实）；data-weeks
    // 仍记宽度对应的列数，拉伸窗口的防抖比较不受裁剪影响
    let weeks = budget;
    const bornTs = Number((d.project || {}).created_at) || 0;
    if (bornTs > 0) {
      const b = new Date(bornTs * 1000);
      const bornWeek = new Date(b.getFullYear(), b.getMonth(), b.getDate()).getTime()
        - ((b.getDay() + 6) % 7) * 86400000;
      if (bornWeek > start) {
        start = Math.min(bornWeek, endWeek);
        weeks = Math.max(1, Math.round((endWeek - start) / (7 * 86400000)) + 1);
      }
    }
    let cells = "";
    for (let i = 0; i < weeks * 7; i++) {
      const t = start + i * 86400000;
      const key = mapDayKey(t / 1000);
      const st = byDay.get(key);
      const tok = st ? st.tokens : 0;
      const future = t > end.getTime();
      const lv = future ? "l0" : tok >= 50000 ? "l4" : tok >= 10000 ? "l3" : tok >= 2000 ? "l2" : tok > 0 ? "l1" : "l0";
      const tip = future ? `${key}：还没到`
        : st ? `${key}：${st.sessions} 个会话 · ${tok >= 1000 ? Math.round(tok / 1000) + "k" : tok} tokens`
        : `${key}：无活动`;
      cells += `<i class="${lv}" data-day="${key}" title="${escapeHtml(tip)}"></i>`;
    }
    heatBox.innerHTML = `<span class="map-aside-label">活跃</span>` +
      `<div class="map-heat-grid" data-weeks="${budget}">${cells}</div>` +
      `<span class="map-heat-legend">少<i class="l0"></i><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i>多</span>`;
    heatBox.querySelectorAll("[data-day]").forEach((el) => {
      el.onclick = () => {
        const day = el.dataset.day;
        mapState.view = "timeline";
        renderMapChrome();
        renderMapView();
        renderMapAside();
        const target = Array.from(document.querySelectorAll("#map-timeline .map-item"))
          .find((n) => n.textContent.includes(day.slice(5).replace("-", "-")));
        if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
      };
    });
  };
  const prevGrid = heatBox.querySelector(".map-heat-grid");
  const budget = prevGrid && aside.clientWidth > 0 ? mapHeatWeeksFor(prevGrid.clientWidth) : 26;
  paintHeat(budget);
  if (!prevGrid && aside.clientWidth > 0) {
    // 首帧没有旧格可量：先按最小列数铺，再按 flex:1 撑开的实际宽度补铺一次
    //（网格宽度只随容器走、与格数无关，一轮即稳，不会来回抖）
    const fit = mapHeatWeeksFor(heatBox.querySelector(".map-heat-grid").clientWidth);
    if (fit !== budget) paintHeat(fit);
  }
}

// ---- 主题图谱：阶段-主题-文件-记忆的关联网络 ----
// 节点上限与边剪枝：面板就几百像素宽，节点超量按权重截断，边超量保权重高的。

const MAP_GRAPH_NODE_CAP = 44;
const MAP_GRAPH_EDGE_CAP = 110;
const MAP_NODE_TYPE = {
  phase: { color: "var(--dm-c1)", label: "阶段" },
  topic: { color: "var(--dm-c2)", label: "主题" },
  file: { color: "var(--dm-c3)", label: "文件" },
  mem: { color: "var(--dm-c4)", label: "记忆" },
};

function mapBuildGraphData() {
  const d = mapState.data;
  const nodes = [];
  const phases = (d.digests || []).filter((x) => x.kind === "phase");
  phases.forEach((dg) => nodes.push({
    id: "p" + dg.id, type: "phase", label: dg.title, weight: (dg.session_ids || []).length + 2,
    sessions: dg.session_ids || [], topics: dg.topics || [], start: dg.start_ts, end: dg.end_ts,
  }));
  // 主题节点：阶段 topics（归属该阶段的会话）∪ 会话 tags（归属带标签的会话），同名合并
  const topics = new Map();
  const bumpTopic = (name, sids) => {
    const key = String(name).trim();
    if (!key) return;
    let t = topics.get(key);
    if (!t) { t = { id: "t·" + key, type: "topic", label: key, weight: 0, sessions: new Set() }; topics.set(key, t); }
    (sids || []).forEach((sid) => t.sessions.add(sid));
    t.weight = t.sessions.size + 1;
  };
  phases.forEach((dg) => (dg.topics || []).forEach((tp) => bumpTopic(tp, dg.session_ids)));
  (d.sessions || []).forEach((s) => (s.tags || []).forEach((tg) => bumpTopic(tg, [s.id])));
  topics.forEach((t) => nodes.push({ ...t, sessions: [...t.sessions] }));
  // 文件节点（Top 12）与记忆节点（近 10 条）
  (d.files || []).slice(0, 12).forEach((f) => nodes.push({
    id: "f·" + f.path, type: "file", label: mapBasename(f.path), weight: f.count + 1,
    sessions: f.session_ids || [], path: f.path,
  }));
  (d.memories || []).slice(-10).forEach((m, i) => nodes.push({
    id: "m·" + i, type: "mem", label: m.text.slice(0, 16) + (m.text.length > 16 ? "…" : ""),
    weight: 1, sessions: [], date: m.date, full: m.text,
  }));
  // 超量裁剪：阶段全保，其余按权重留
  const keep = nodes.filter((n) => n.type === "phase");
  const rest = nodes.filter((n) => n.type !== "phase").sort((a, b) => b.weight - a.weight);
  const graphNodes = keep.concat(rest.slice(0, Math.max(0, MAP_GRAPH_NODE_CAP - keep.length)));
  const byId = new Map(graphNodes.map((n) => [n.id, n]));
  // 边：阶段–主题（阶段列出该主题）；主题–文件（同一会话既带标签又改过该文件）；
  // 记忆–阶段（条目日期落在阶段窗口内）。带权重，超量保高权。
  const edges = [];
  const addEdge = (a, b, w) => {
    if (!byId.has(a) || !byId.has(b) || a === b) return;
    edges.push({ a, b, w });
  };
  graphNodes.forEach((n) => {
    if (n.type !== "phase") return;
    (n.topics || []).forEach((tp) => addEdge(n.id, "t·" + String(tp).trim(), 2));
  });
  graphNodes.forEach((n) => {
    if (n.type !== "topic" || !n.sessions.length) return;
    const sset = new Set(n.sessions);
    graphNodes.forEach((f) => {
      if (f.type !== "file") return;
      const overlap = (f.sessions || []).filter((sid) => sset.has(sid)).length;
      if (overlap > 0) addEdge(n.id, f.id, 1 + overlap);
    });
  });
  graphNodes.forEach((m) => {
    if (m.type !== "mem") return;
    let linked = 0;
    graphNodes.forEach((p) => {
      if (p.type !== "phase" || linked >= 2) return;
      if (m.date >= mapDayKey(p.start) && m.date <= mapDayKey(p.end)) { addEdge(m.id, p.id, 1); linked += 1; }
    });
  });
  edges.sort((x, y) => y.w - x.w);
  return { nodes: graphNodes, edges: edges.slice(0, MAP_GRAPH_EDGE_CAP) };
}

// 力导向布局：定环初始化（阶段内圈 → 主题中圈 → 文件/记忆外圈，角度按序号
// 均分，确定性可复现），~90 轮斥力 + 弹簧 + 向心，节点量 ≤44 同步算毫无压力。
function mapLayoutGraph(nodes, edges, W, H) {
  const R = [Math.min(W, H) * 0.17, Math.min(W, H) * 0.34, Math.min(W, H) * 0.47];
  const ring = { phase: 0, topic: 1, file: 2, mem: 2 };
  const cx = W / 2, cy = H / 2;
  const counters = { phase: 0, topic: 0, file: 0, mem: 0 };
  const typeCount = {};
  nodes.forEach((n) => { typeCount[n.type] = (typeCount[n.type] || 0) + 1; });
  nodes.forEach((n) => {
    const r = R[ring[n.type]];
    const i = counters[n.type]++;
    const ang = (2 * Math.PI * i) / Math.max(1, typeCount[n.type]) + (ring[n.type] * 0.7);
    n.x = cx + Math.cos(ang) * r;
    n.y = cy + Math.sin(ang) * r;
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const adj = new Map();
  edges.forEach((e) => {
    if (!adj.has(e.a)) adj.set(e.a, []);
    if (!adj.has(e.b)) adj.set(e.b, []);
    adj.get(e.a).push(e.b);
    adj.get(e.b).push(e.a);
  });
  const REPULSE = 4200, SPRING = 0.012, CENTER = 0.015;
  for (let step = 0; step < 90; step++) {
    const fx = new Map(), fy = new Map();
    nodes.forEach((n) => { fx.set(n.id, 0); fy.set(n.id, 0); });
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = (i % 3) - 1; dy = (j % 3) - 1; d2 = 2; }
        const f = REPULSE / d2;
        const dd = Math.sqrt(d2);
        fx.set(a.id, fx.get(a.id) + (dx / dd) * f);
        fy.set(a.id, fy.get(a.id) + (dy / dd) * f);
        fx.set(b.id, fx.get(b.id) - (dx / dd) * f);
        fy.set(b.id, fy.get(b.id) - (dy / dd) * f);
      }
    }
    edges.forEach((e) => {
      const a = byId.get(e.a), b = byId.get(e.b);
      if (!a || !b) return;
      const dx = b.x - a.x, dy = b.y - a.y;
      const rest = 110 + 45 * e.w;
      const dd = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const f = SPRING * (dd - rest);
      fx.set(a.id, fx.get(a.id) + (dx / dd) * f * dd);
      fy.set(a.id, fy.get(a.id) + (dy / dd) * f * dd);
      fx.set(b.id, fx.get(b.id) - (dx / dd) * f * dd);
      fy.set(b.id, fy.get(b.id) - (dy / dd) * f * dd);
    });
    const damp = 1 - step / 100;
    nodes.forEach((n) => {
      n.x += Math.max(-14, Math.min(14, fx.get(n.id))) * damp + (cx - n.x) * CENTER;
      n.y += Math.max(-14, Math.min(14, fy.get(n.id))) * damp + (cy - n.y) * CENTER;
      n.x = Math.max(30, Math.min(W - 30, n.x));
      n.y = Math.max(18, Math.min(H - 18, n.y));
    });
  }
  return { byId, adj };
}

function renderMapGraph() {
  const d = mapState.data;
  const box = document.getElementById("map-graph");
  if (!d) { box.innerHTML = ""; return; }
  const { nodes, edges } = mapBuildGraphData();
  if (!nodes.length) {
    box.innerHTML = '<div class="map-empty dim">还没有可画的内容——先聊出一些会话，或点「生成演化摘要」</div>';
    return;
  }
  mountMapGraph(box, nodes, edges);
}

function mountMapGraph(box, nodes, edges) {
  const W = Math.max(box.clientWidth || 0, 260);
  const H = Math.max(box.clientHeight || 0, 260);
  const { byId, adj } = mapLayoutGraph(nodes, edges, W, H);
  let lines = "";
  edges.forEach((e) => {
    const a = byId.get(e.a), b = byId.get(e.b);
    if (!a || !b) return;
    // --w 驱动 CSS 里的连线不透明度（权重越高越实）
    lines += `<line class="map-edge" data-a="${e.a}" data-b="${e.b}" style="--w:${Math.min(4, e.w)}"` +
      ` x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"/>`;
  });
  let legend = Object.entries(MAP_NODE_TYPE).map(([k, v]) =>
    `<span class="map-legend-item"><i style="background:${v.color}"></i>${v.label}</span>`).join("");
  let boxes = "";
  nodes.forEach((n) => {
    // 不设 title：原生系统提示与悬停浮层会双份出现，浮层信息更全（类型/会话数/直达）
    boxes += `<div class="map-gnode ty-${n.type}" data-nid="${n.id}"` +
      ` style="left:${n.x.toFixed(1)}px;top:${n.y.toFixed(1)}px"` +
      `><span>${escapeHtml(n.label)}</span></div>`;
  });
  box.innerHTML = `<div class="map-graph-canvas" style="width:${W}px;height:${H}px">` +
    `<svg width="${W}" height="${H}">${lines}</svg>${boxes}` +
    `<div class="map-graph-legend">${legend}</div></div>` +
    `<div id="map-pop" class="map-pop hidden"></div>`;
  const svg = box.querySelector("svg");
  const pop = box.querySelector("#map-pop");

  const closePop = () => {
    clearTimeout(popTimer);
    clearTimeout(popHideTimer);
    popTimer = 0;
    pop.classList.add("hidden");
  };
  // 悬停出浮层：进节点稍候即现（扫过不闪），离开快速收起（留 100ms 余量——
  // 就靠这点空档把鼠标挪进紧挨着的浮层取消关闭，浮层里的会话链接照常可点；
  // 再长就有「赖着不走」的卡顿感）
  let popTimer = 0, popHideTimer = 0, dragging = false;
  const showPop = (n) => {
    clearTimeout(popHideTimer);
    const sids = (n.sessions || []).slice(0, 12);
    const sMap = new Map((mapState.data.sessions || []).map((s) => [s.id, s]));
    const rows = sids.map((sid) => {
      const s = sMap.get(sid);
      return s ? `<button class="map-pop-sess" data-sid="${sid}">${escapeHtml(s.title || "（未命名）")}</button>` : "";
    }).join("");
    pop.innerHTML = `<b>${escapeHtml(n.label)}</b>` +
      `<span class="map-dim small">${MAP_NODE_TYPE[n.type].label} · ${sids.length} 个会话${sids.length ? "，点击直达" : ""}</span>` +
      (rows || (n.full ? `<p class="map-pop-full">${escapeHtml(n.full)}</p>` : "<span class='dim small'>无关联会话</span>"));
    pop.classList.remove("hidden");
    const pw = pop.offsetWidth || 200, ph = pop.offsetHeight || 80;
    pop.style.left = Math.max(4, Math.min(W - pw - 4, n.x - pw / 2)) + "px";
    pop.style.top = Math.max(4, Math.min(H - ph - 4, n.y + 18)) + "px";
    pop.querySelectorAll(".map-pop-sess").forEach((b) => {
      b.onclick = () => {
        const s = sMap.get(b.dataset.sid);
        openTabForSession(b.dataset.sid, s ? s.title : "");
        closePop();
      };
    });
  };
  pop.addEventListener("mouseenter", () => clearTimeout(popHideTimer));
  pop.addEventListener("mouseleave", () => {
    clearTimeout(popTimer);
    popHideTimer = setTimeout(closePop, 100);
  });
  box.querySelector(".map-graph-canvas").addEventListener("click", (e) => {
    if (e.target === svg || e.target.classList.contains("map-graph-canvas")) closePop();
  });
  // 悬停高亮邻域：节点与相邻边加 .hl（相邻查 O(度)）
  const neighbors = (nid) => {
    const out = new Set();
    edges.forEach((e) => {
      if (e.a === nid) out.add(e.b);
      if (e.b === nid) out.add(e.a);
    });
    return out;
  };
  box.querySelectorAll(".map-gnode").forEach((el) => {
    const nid = el.dataset.nid;
    el.addEventListener("mouseenter", () => {
      const nb = neighbors(nid);
      el.classList.add("hl");
      box.querySelectorAll(".map-edge").forEach((ln) => {
        if (ln.dataset.a === nid || ln.dataset.b === nid) ln.classList.add("hl");
      });
      box.querySelectorAll(".map-gnode").forEach((o) => {
        if (nb.has(o.dataset.nid)) o.classList.add("hl-soft");
      });
      if (!dragging) {
        clearTimeout(popTimer);
        popTimer = setTimeout(() => showPop(byId.get(nid)), 180);
      }
    });
    el.addEventListener("mouseleave", () => {
      box.querySelectorAll(".hl,.hl-soft").forEach((x) => x.classList.remove("hl", "hl-soft"));
      clearTimeout(popTimer);
      popHideTimer = setTimeout(closePop, 100);
    });
    // 拖拽：pointer 事件改 x/y，重画该节点与相邻边（布局结果就地更新）。
    // 拖拽期间抑制悬停浮层（指针被捕获也不会再触发 mouseenter），松手后
    // 挪开再悬停即可再看。
    el.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      dragging = true;
      closePop();
      el.setPointerCapture(ev.pointerId);
      const n = byId.get(nid);
      const canvas = box.querySelector(".map-graph-canvas");
      // 记下抓取点相对节点中心的偏移：left/top 锚的是中心（CSS 平移回正），
      // 不补偏移的话，起手瞬间中心会被直接搬到指针下，节点像自己跳了半个身位
      const rect0 = canvas.getBoundingClientRect();
      const offX = ev.clientX - rect0.left - n.x;
      const offY = ev.clientY - rect0.top - n.y;
      const move = (m) => {
        const rect = canvas.getBoundingClientRect();
        n.x = Math.max(30, Math.min(W - 30, m.clientX - rect.left - offX));
        n.y = Math.max(18, Math.min(H - 18, m.clientY - rect.top - offY));
        el.style.left = n.x + "px";
        el.style.top = n.y + "px";
        box.querySelectorAll(`.map-edge[data-a="${nid}"],.map-edge[data-b="${nid}"]`).forEach((ln) => {
          const o = byId.get(ln.dataset.a === nid ? ln.dataset.b : ln.dataset.a);
          if (!o) return;
          if (ln.dataset.a === nid) { ln.setAttribute("x1", n.x); ln.setAttribute("y1", n.y); }
          else { ln.setAttribute("x2", n.x); ln.setAttribute("y2", n.y); }
        });
      };
      const up = () => {
        dragging = false;
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });
  });
  // 宽度变化超阈值才整体重排（防 RO 循环），与 mountPipelineGraph 同款门槛
  let raf = 0;
  const ro = new ResizeObserver(() => {
    const w = box.clientWidth;
    if (!w || Math.abs(w - (+box.dataset.w || 0)) < 24) return;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      box.dataset.w = String(box.clientWidth);
      renderMapGraph();
    });
  });
  box.dataset.w = String(W);
  ro.observe(box);
}
