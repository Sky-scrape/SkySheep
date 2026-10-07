// app-schedule.js —— 日程 / 定时任务 / 任务编排流水线（自动化三件套，右侧面板）
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有声明与纯数据常量（含 agView/agAnchor/agCache 等顶层 let 状态，
// 纯初始化先例同 app-tools.js 的 providerUiMode），零加载期执行；按钮接线
// 仍留在 app.js 原位（运行时解引用），故 index.html 先载本文件再载 app.js。

// ---------- 日程（右侧面板：周/月历视图 + 分组列表 + 新增/编辑弹窗 + 到点提醒横幅） ----------
const AG_HOUR_H = 44;        // 周视图里 1 小时的高度
const AG_SNAP_MIN = 5;       // 周视图拖动吸附粒度（分钟）：一格 3.7px，5 分钟够细又不抖
const AG_CLICK_MIN = 30;     // 周视图单击（不拖）时的取整粒度（分钟）
const AG_DRAG_SLOP = 4;      // 位移小于此值算单击、不算拖动（布局像素）
const AG_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const AG_WNAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
let agView = "week";         // week | month | list
let agAnchor = new Date();   // 当前查看的日期（取其所在周/月）
let agCache = [];            // schedule.list（含已完成）的缓存

function agPad(n) { return pad2(n); }
// 毫秒时间戳的 "HH:MM"（agHm 收的是秒，周视图内部一律用毫秒）
function agHmMs(ms) {
  const d = new Date(ms);
  return `${agPad(d.getHours())}:${agPad(d.getMinutes())}`;
}
// 时段文案：有时段拼「起–止」，按点事件只给一个时刻
function agRangeTxt(startMs, endMs) {
  return endMs > startMs ? `${agHmMs(startMs)} – ${agHmMs(endMs)}` : agHmMs(startMs);
}
function agSameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}
function agWeekMonday(d) {
  const m = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  m.setDate(m.getDate() - ((m.getDay() + 6) % 7));
  return m;
}

async function loadAgenda() {
  try {
    agCache = await request("schedule.list", { include_done: true });
    renderAgendaView();
  } catch (e) {
    addNotice("日程加载失败：" + e.message);
  }
}

function renderAgendaView() {
  closeAgendaPop(); // 切视图/重新渲染前先把就地快建气泡收起来（它锚在上一版网格上）
  const week = document.getElementById("ag-week");
  const month = document.getElementById("ag-month");
  const list = document.getElementById("agenda-list");
  document.querySelectorAll("#ag-view-seg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === agView));
  document.getElementById("ag-list-btn").classList.toggle("active", agView === "list");
  week.classList.toggle("hidden", agView !== "week");
  month.classList.toggle("hidden", agView !== "month");
  list.classList.toggle("hidden", agView !== "list");
  document.getElementById("ag-navcluster").classList.toggle("hidden", agView === "list");
  if (agView === "week") renderWeekGrid();
  else if (agView === "month") renderMonthGrid();
  else renderAgenda(agCache.filter((i) => !i.done));
}

function renderWeekGrid() {
  const week = document.getElementById("ag-week");
  const mon = agWeekMonday(agAnchor);
  const today = new Date();
  const days = [...Array(7)].map((_, i) => {
    const d = new Date(mon); d.setDate(mon.getDate() + i); return d;
  });
  const todayIdx = days.findIndex((d) => agSameDay(d, today));
  document.getElementById("ag-range").textContent =
    `${mon.getMonth() + 1}/${mon.getDate()}${AG_WEEKDAYS[0]} — ` +
    (() => { const s = days[6]; return `${s.getMonth() + 1}/${s.getDate()}${AG_WEEKDAYS[6]}`; })();

  const head = '<div class="ag-corner"></div>' + days.map((d) => {
    const isT = agSameDay(d, today);
    return `<div class="ag-day-head${isT ? " today" : ""}"><span>${AG_WNAMES[d.getDay()]}</span><b>${d.getDate()}</b></div>`;
  }).join("");
  let hours = "";
  for (let h = 0; h < 24; h++) hours += `<div class="ag-hour">${agPad(h)}:00</div>`;

  const weekStart = mon.getTime(), weekEnd = weekStart + 7 * 86400000;
  let evts = "";
  agCache.forEach((it) => {
    const t = it.start_at * 1000;
    // 时间段可能跨天：起点或终点任一天在本周就画出来（按起点列定位）
    const endMs = it.end_at ? it.end_at * 1000 : 0;
    if (t >= weekEnd || (endMs && endMs < weekStart)) return;
    if (t < weekStart && !endMs) return;
    const d = new Date(t);
    // 起点在本周之前时钉在本周首列，否则定位到它所在的那天
    const dayIdx = t < weekStart ? 0 : Math.floor(
      (new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() - weekStart) / 86400000);
    const mins = t < weekStart ? 0 : d.getHours() * 60 + d.getMinutes();
    // 有时段就按真实持续时间撑高（最少 20 分钟，否则字都挤不下）；
    // 跨天/跨周的区间封顶到当天 24:00（完整区间在悬停提示与列表里看），不溢出网格
    const rawMin = endMs ? Math.round((endMs - t) / 60000) : 0;
    const spanMin = rawMin ? Math.max(20, Math.min(rawMin, 1440 - mins)) : 0;
    const top = Math.round(mins / 60 * AG_HOUR_H) + 1;
    const h = spanMin ? `height:${Math.max(18, Math.round(spanMin / 60 * AG_HOUR_H) - 2)}px;` : "";
    const timeTxt = it.end_at
      ? `${agPad(d.getHours())}:${agPad(d.getMinutes())}–${agHm(it.end_at)}`
      : `${agPad(d.getHours())}:${agPad(d.getMinutes())}`;
    const evtTitle = `${timeTxt} ${it.title}${it.notes ? " · " + it.notes : ""}`;
    // 时间段块带上下两条拖拽手柄（拉边缘改起止）：按点事件没有时长可拉，不给手柄
    const grips = spanMin
      ? '<i class="ag-grip up" title="拖动改开始时间"></i><i class="ag-grip down" title="拖动改结束时间"></i>'
      : "";
    evts += `<div class="ag-evt${it.done ? " done" : ""}${spanMin ? " span" : ""}" data-id="${it.id}" data-col="${dayIdx}"
      style="top:${top}px;${h}left:calc(${dayIdx} * 100% / 7 + 2px);width:calc(100% / 7 - 4px)"
      title="${escapeHtml(evtTitle)}">${grips}<span class="ag-evt-txt">${it.done ? "✓ " : ""}${escapeHtml(it.title)}</span></div>`;
  });
  let now = "";
  if (todayIdx >= 0) {
    const top = Math.round((today.getHours() * 60 + today.getMinutes()) / 60 * AG_HOUR_H);
    now = `<div class="ag-now" style="top:${top}px;left:calc(${todayIdx} * 100% / 7);width:calc(100% / 7)"><i></i></div>`;
  }
  week.innerHTML =
    `<div class="ag-grid-head">${head}</div>
     <div class="ag-scroll"><div class="ag-grid-body" style="height:${24 * AG_HOUR_H}px">
       <div class="ag-hours">${hours}</div>
       <div class="ag-canvas">${evts}${now}</div>
     </div></div>`;

  week.querySelectorAll(".ag-evt").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      if (el._agDragged) { el._agDragged = false; return; } // 刚拖过（改时长/平移）：那一下不算「打开编辑」
      const it = agCache.find((x) => x.id == el.dataset.id);
      if (it) agendaModal(it);
    };
    bindAgendaEvtDrag(el, days);
  });
  const canvas = week.querySelector(".ag-canvas");
  canvas.tabIndex = 0; // 气泡 Esc 关闭后把焦点还给网格，键盘用户不会断在空气里
  bindAgendaCanvas(canvas, days);
  const scroller = week.querySelector(".ag-scroll");
  // 滚网格就把气泡收起来：它锚在视口坐标上，内容一滚就与格子对不上了
  if (scroller) scroller.addEventListener("scroll", () => closeAgendaPop());
}

// —— 周视图拖动：空白处拖 = 圈时段、单击 = 选时点；已有块拖边缘/块身 = 改时间 ——

/** 指针位置 → { col, mins }：第几列 + 当天第几分钟（分钟吸附到 AG_SNAP_MIN）。

    缩放（#app 上的 CSS zoom）的换算不靠猜：横向比 clientWidth（布局 px）与
    getBoundingClientRect().width（物理 px）得到真实倍率，纵向直接拿
    clientHeight（那是未缩放的布局高度 24*44，且不受滚动影响）。这样无论
    zoom 是否已反映在 clientWidth 里，两边都自洽，松手时间与看到的位置一致。 */
function agPointFromEvent(e, canvas) {
  const rect = canvas.getBoundingClientRect();
  const sx = canvas.clientWidth ? rect.width / canvas.clientWidth : 1;
  const hLayout = canvas.clientHeight || 24 * AG_HOUR_H;
  const sy = hLayout ? rect.height / hLayout : sx;
  const col = Math.floor((e.clientX - rect.left) / (rect.width / 7));
  const yLayout = sy ? (e.clientY - rect.top) / sy : 0;
  const rawMin = (yLayout / AG_HOUR_H) * 60;
  return {
    col: Math.max(0, Math.min(6, col)),
    mins: Math.max(0, Math.min(1440, Math.round(rawMin / AG_SNAP_MIN) * AG_SNAP_MIN)),
  };
}
/** 列号 + 当天的分钟数 → 毫秒时间戳（列 0 是本周一） */
function agMsFromColMins(days, col, mins) {
  const day = new Date(days[col]);
  day.setHours(0, 0, 0, 0);
  return day.getTime() + mins * 60000;
}

/** 空白处：拖出一段就是时段，点一下就是单个时点（30 分钟就近取整）。

    全程用 pointer 事件 + setPointerCapture：鼠标移出网格也收得到 move/up，
    不会拖到一半丢指针。拖动中只画幽灵块、不碰后端，松手才动一次。 */
function bindAgendaCanvas(canvas, days) {
  canvas.onpointerdown = (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".ag-evt")) return; // 点在已有块上由块自己处理
    if (e.target.closest("#ag-pop")) return;  // 气泡是 canvas 的子节点，点它不算「圈时间」
    e.preventDefault();
    closeAgendaPop();
    const start = agPointFromEvent(e, canvas);
    const drag = {
      col: start.col,
      from: start.mins,
      to: start.mins,
      moved: false,
      x0: e.clientX, y0: e.clientY,
      ghost: null,
    };
    // 捕获失败不影响交互（合成事件、或在无活动指针时调用会抛）：拖动本身
    // 只靠 canvas 上的 move/up，捕获只是「移出网格也不丢」的加固
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    canvas.classList.add("ag-dragging");

    const paint = () => {
      const lo = Math.min(drag.from, drag.to), hi = Math.max(drag.from, drag.to);
      if (!drag.ghost) {
        drag.ghost = document.createElement("div");
        drag.ghost.className = "ag-draft";
        canvas.appendChild(drag.ghost);
      }
      const top = Math.round((lo / 60) * AG_HOUR_H) + 1;
      const h = Math.max(14, Math.round(((hi - lo) / 60) * AG_HOUR_H) - 2);
      drag.ghost.style.top = top + "px";
      drag.ghost.style.height = h + "px";
      drag.ghost.style.left = `calc(${drag.col} * 100% / 7 + 2px)`;
      drag.ghost.style.width = "calc(100% / 7 - 4px)";
      const s = agMsFromColMins(days, drag.col, lo), en = agMsFromColMins(days, drag.col, hi);
      drag.ghost.textContent = agRangeTxt(s, en);
    };

    const onMove = (ev) => {
      if (!drag.moved) {
        if (Math.abs(ev.clientX - drag.x0) < AG_DRAG_SLOP && Math.abs(ev.clientY - drag.y0) < AG_DRAG_SLOP) return;
        drag.moved = true;
      }
      // 列锁在按下那一列：跨列拖动会变成「跨天时段」，先不做（与 Apple 日历同取舍）
      drag.to = agPointFromEvent(ev, canvas).mins;
      paint();
    };
    const onUp = (ev) => {
      try { canvas.releasePointerCapture?.(ev.pointerId); } catch (err) { /* 忽略 */ }
      canvas.classList.remove("ag-dragging");
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointercancel", onCancel);
      if (drag.ghost) drag.ghost.remove();
      if (!drag.moved) {
        // 单击：30 分钟就近取整选一个时点（与改动前的点击行为一致）
        const mins = Math.min(1440, Math.round(drag.from / AG_CLICK_MIN) * AG_CLICK_MIN);
        const ts = agMsFromColMins(days, drag.col, Math.min(mins, 1440 - AG_CLICK_MIN));
        agendaQuickCreate(canvas, drag.col, ts, 0);
        return;
      }
      const lo = Math.min(drag.from, drag.to), hi = Math.max(drag.from, drag.to);
      if (hi - lo < AG_SNAP_MIN) return; // 拖了个寂寞：不建
      agendaQuickCreate(canvas, drag.col,
        agMsFromColMins(days, drag.col, lo), agMsFromColMins(days, drag.col, hi));
    };
    const onCancel = () => {
      canvas.classList.remove("ag-dragging");
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointercancel", onCancel);
      if (drag.ghost) drag.ghost.remove();
    };
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", onCancel);
  };
}

/** 已有块：抓上下边缘拉时长、抓块身整体平移。

    拖动中只改内联 top/height（所见即所得），松手才落库一次——几十个中间态
    全写进库既没必要，也会把撤消历史弄脏。 */
function bindAgendaEvtDrag(el, days) {
  const it = agCache.find((x) => x.id == el.dataset.id);
  if (!it) return;
  const spanMin = it.end_at ? Math.round((it.end_at - it.start_at) / 60) : 0;
  // 块所在列（0–6）：渲染时就写在 data-col 上。拖动中列不变，只用来算落库日期；
  // 不靠指针位置取列——拖到别的列上方时那就不对了（与「列锁在按下那一列」一致）。
  const col = Number(el.dataset.col) || 0;
  // 拖动的「所见即所得」：块高只由起止分钟数推，块内文字同步显示新的起—止
  let spanTxt = "";
  const draw = (startMin, endMin) => {
    el.style.top = Math.round((Math.max(0, startMin) / 60) * AG_HOUR_H) + 1 + "px";
    if (endMin > startMin) {
      el.style.height = Math.max(16, Math.round(((endMin - startMin) / 60) * AG_HOUR_H) - 2) + "px";
    }
    const s = agMsFromColMins(days, col, Math.max(0, startMin));
    const en = endMin > startMin ? agMsFromColMins(days, col, endMin) : 0;
    spanTxt = agRangeTxt(s, en);
    el.title = `${spanTxt} ${it.title}`;
  };
  el.onpointerdown = (e) => {
    if (e.button !== 0) return;
    const grip = e.target.closest(".ag-grip");
    const mode = grip ? (grip.classList.contains("up") ? "resize-up" : "resize-down") : "move";
    // 按点事件没有时长可拉
    if (!spanMin && mode !== "move") return;
    e.preventDefault();
    e.stopPropagation();
    closeAgendaPop();
    el.classList.add("dragging");
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* 同 canvas：捕获不是必需 */ }
    const base = agPointFromEvent(e, el.parentElement);
    const s0 = new Date(it.start_at * 1000);
    const startMin0 = s0.getHours() * 60 + s0.getMinutes();
    let moved = false;
    let cur = { startMin: startMin0, endMin: spanMin ? startMin0 + spanMin : 0 };
    // 拖块身平移：末端的最大位移是一次算好的常量，不必在每次 move 里重算
    const maxShift = spanMin ? 1440 - (startMin0 + spanMin) : 1440 - startMin0;
    const onMove = (ev) => {
      const p = agPointFromEvent(ev, el.parentElement);
      const delta = p.mins - base.mins;
      if (!moved) {
        if (Math.abs(delta) < AG_SNAP_MIN) return;
        moved = true;
      }
      if (mode === "resize-up") {
        const endMin = startMin0 + spanMin;
        cur = { startMin: Math.min(Math.max(0, startMin0 + delta), endMin - AG_SNAP_MIN), endMin };
      } else if (mode === "resize-down") {
        cur = { startMin: startMin0, endMin: Math.max(startMin0 + AG_SNAP_MIN, startMin0 + spanMin + delta) };
      } else {
        const d = Math.max(-startMin0, Math.min(maxShift, delta));
        cur = { startMin: startMin0 + d, endMin: spanMin ? startMin0 + spanMin + d : 0 };
      }
      draw(cur.startMin, cur.endMin);
      const txtEl = el.querySelector(".ag-evt-txt");
      if (txtEl) txtEl.textContent = spanTxt;
    };
    const finish = async (ev) => {
      try { el.releasePointerCapture?.(ev.pointerId); } catch (err) { /* 忽略 */ }
      el.classList.remove("dragging");
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", finish);
      el.removeEventListener("pointercancel", cancel);
      if (!moved) return; // 没拖动=点击：交给 onclick 开编辑弹窗
      // 只吞掉紧随其后的那一次 click，而且由 click 处理器自己清标记：
      // pointerup 与 click 的先后没有可靠保证，用 setTimeout 复位会漏。
      el._agDragged = true;
      const day = new Date(days[col]); day.setHours(0, 0, 0, 0);
      const params = { id: it.id, start_at: (day.getTime() + cur.startMin * 60000) / 1000 };
      if (spanMin) params.end_at = (day.getTime() + cur.endMin * 60000) / 1000;
      try { await request("schedule.update", params); }
      catch (err) { addNotice("调整失败：" + err.message); }
      await loadAgenda();
    };
    const cancel = () => {
      el.classList.remove("dragging");
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", finish);
      el.removeEventListener("pointercancel", cancel);
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", finish);
    el.addEventListener("pointercancel", cancel);
  };
}

function renderMonthGrid() {
  const month = document.getElementById("ag-month");
  const y = agAnchor.getFullYear(), m = agAnchor.getMonth();
  document.getElementById("ag-range").textContent = `${y}年${m + 1}月`;
  const start = agWeekMonday(new Date(y, m, 1));
  const today = new Date();
  const byDay = {};
  agCache.forEach((it) => {
    const d = new Date(it.start_at * 1000);
    const k = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    (byDay[k] = byDay[k] || []).push(it);
  });
  const head = AG_WEEKDAYS.map((w) => `<span>${w}</span>`).join("");
  let cells = "";
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const inM = d.getMonth() === m;
    const isT = agSameDay(d, today);
    const items = byDay[`${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`] || [];
    const chips = items.slice(0, 2).map((it) =>
      `<span class="ag-chip${it.done ? " done" : ""}" data-id="${it.id}">${escapeHtml(it.title)}</span>`).join("") +
      (items.length > 2 ? `<span class="ag-more">还有 ${items.length - 2} 项</span>` : "");
    cells += `<div class="ag-cell${inM ? "" : " out"}${isT ? " today" : ""}" data-ts="${d.getTime() / 1000}">
      <b>${d.getDate()}</b>${chips}</div>`;
  }
  month.innerHTML = `<div class="ag-mhead">${head}</div><div class="ag-mgrid">${cells}</div>`;
  month.querySelectorAll(".ag-chip").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const it = agCache.find((x) => x.id == el.dataset.id);
      if (it) agendaModal(it);
    };
  });
  month.querySelectorAll(".ag-cell").forEach((el) => {
    el.onclick = () => agendaModal(null, Number(el.dataset.ts) + 9 * 3600);
  });
}

function shiftAgenda(dir) {
  if (agView === "month") agAnchor = new Date(agAnchor.getFullYear(), agAnchor.getMonth() + dir, 1);
  else agAnchor.setDate(agAnchor.getDate() + dir * 7);
  renderAgendaView();
}

function fmtAgendaTime(ts) {
  const d = new Date(ts * 1000);
  const p = pad2;
  const now = new Date();
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (sameDay(d, now)) return `今天 ${hm}`;
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
  if (sameDay(d, tomorrow)) return `明天 ${hm}`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
}

// "HH:MM" 形式的时刻（给时间段用：同一天的区间只重复时刻，不重写日期）
function agHm(ts) {
  const d = new Date(ts * 1000);
  return `${agPad(d.getHours())}:${agPad(d.getMinutes())}`;
}

// 日程时间的完整文案：无结束时间就是单个时刻，有时段则拼成「起—止」；
// 跨天时右侧带上日期，避免只看到 23:00–01:00 分不清哪天结束
function fmtAgendaSpan(it) {
  const head = fmtAgendaTime(it.start_at);
  if (!it.end_at) return head;
  const s = new Date(it.start_at * 1000), e = new Date(it.end_at * 1000);
  const crossDay = s.getFullYear() !== e.getFullYear()
    || s.getMonth() !== e.getMonth() || s.getDate() !== e.getDate();
  return `${head}—${crossDay ? fmtAgendaTime(it.end_at) : agHm(it.end_at)}`;
}

function agendaGroup(item, now) {
  const t = item.start_at * 1000;
  const dayStart = new Date(now); dayStart.setHours(0, 0, 0, 0);
  if (t < dayStart.getTime()) return "过期";
  const dayEnd = dayStart.getTime() + 86400000;
  if (t < dayEnd) return "今天";
  if (t < dayEnd + 86400000) return "明天";
  return "未来";
}

function renderAgenda(items) {
  const ul = document.getElementById("agenda-list");
  ul.innerHTML = "";
  const now = new Date();
  const groups = { "过期": [], "今天": [], "明天": [], "未来": [] };
  items.forEach((it) => { groups[agendaGroup(it, now)].push(it); });
  let any = false;
  for (const g of ["过期", "今天", "明天", "未来"]) {
    if (!groups[g].length) continue;
    any = true;
    const head = document.createElement("li");
    head.className = "agenda-head";
    head.textContent = g;
    ul.appendChild(head);
    groups[g].forEach((it) => {
      const li = document.createElement("li");
      li.className = "agenda-item" + (g === "过期" ? " overdue" : "");
      li.innerHTML =
        `<div class="agenda-main">
           <span class="agenda-title">${escapeHtml(it.title)}</span>
           <span class="agenda-time">${fmtAgendaSpan(it)}</span>
         </div>` +
        (it.notes ? `<div class="agenda-notes">${escapeHtml(it.notes)}</div>` : "");
      const ops = document.createElement("span");
      ops.className = "agenda-ops";
      ops.innerHTML =
        `<button class="agenda-op" data-op="done" title="标记完成">✓</button>` +
        `<button class="agenda-op" data-op="del" title="删除">✕</button>`;
      ops.querySelector('[data-op="done"]').onclick = async (e) => {
        e.stopPropagation();
        try { await request("schedule.update", { id: it.id, done: true }); }
        catch (err) { addNotice("操作失败: " + err.message); }
        await loadAgenda();
      };
      ops.querySelector('[data-op="del"]').onclick = async (e) => {
        e.stopPropagation();
        try { await request("schedule.delete", { id: it.id }); }
        catch (err) { addNotice("删除失败: " + err.message); }
        await loadAgenda();
      };
      li.appendChild(ops);
      li.onclick = () => agendaModal(it);
      ul.appendChild(li);
    });
  }
  if (!any) {
    const li = document.createElement("li");
    li.className = "dim small";
    li.style.padding = "6px 10px";
    li.textContent = "还没有日程 —— 点右上角 ＋ 新增，或直接在对话里让 Agent 记";
    ul.appendChild(li);
  }
}

// —— 就地快建气泡：拖选/单击后直接在网格上把日程写出来 ——
// 与 agendaModal（完整弹窗）分工：这里只处理「一句话就能记下的事」，需要更多
// 字段（提前量、备注展开后仍不够用的）走「更多选项…」转成完整弹窗并预填当前值。

let agPopEl = null;      // 当前打开的气泡（同时只允许一个）
let agPopDetach = null;  // 关闭时要摘掉的全局监听

function closeAgendaPop() {
  if (agPopDetach) { agPopDetach(); agPopDetach = null; }
  if (agPopEl) { agPopEl.remove(); agPopEl = null; }
  const canvas = document.querySelector("#ag-week .ag-canvas");
  if (canvas) delete canvas.dataset.popOpen;
}

function agPopDateLabel(ms) {
  const d = new Date(ms);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${AG_WNAMES[d.getDay()]}`;
}

/** 在网格上就地弹一个小快建气泡。

    canvas：网格容器；col：第几列（0–6）；startMs：起点；endMs：终点（0 = 按点）。
    气泡挂在 .ag-canvas（它本身是 position:relative）里，因此跟随网格一起滚动，
    不会像视口定位的浮层那样一滚就与格子错位。 */
function agendaQuickCreate(canvas, col, startMs, endMs) {
  closeAgendaPop();
  if (!canvas) return;
  const hasEnd = endMs > startMs;
  const pop = document.createElement("div");
  pop.id = "ag-pop";
  pop.innerHTML = `
    <div class="ag-pop-when">${agPopDateLabel(startMs)}</div>
    <div class="ag-pop-row">
      <input id="ag-pop-start" class="ag-pop-time" type="time" step="300" value="${agHmMs(startMs)}" title="开始时间">
      <span class="ag-pop-dash">–</span>
      <input id="ag-pop-end" class="ag-pop-time" type="time" step="300"
        value="${hasEnd ? agHmMs(endMs) : ""}" title="结束时间（留空 = 只记一个时刻）">
    </div>
    <input id="ag-pop-title" class="modal-input" type="text" maxlength="200" placeholder="准备做什么？">
    <input id="ag-pop-notes" class="modal-input hidden" type="text" maxlength="500" placeholder="备注（可选）">
    <div class="ag-pop-err hidden"></div>
    <div class="ag-pop-foot">
      <label class="ag-pop-remind"><input id="ag-pop-remind" type="checkbox" checked> 提醒</label>
      <span class="spacer"></span>
      <button class="ag-pop-link" data-act="notes">＋ 备注</button>
      <button class="ag-pop-link" data-act="more">更多选项…</button>
    </div>
    <div class="ag-pop-foot ops">
      <button class="ag-pop-link" data-act="cancel">取消</button>
      <button class="rp-mini primary" data-act="ok">添加</button>
    </div>`;
  canvas.appendChild(pop);
  agPopEl = pop;

  // 定位：全部用网格自己的布局坐标（pop 也挂在 canvas 里，两边坐标系一致）
  const rect = canvas.getBoundingClientRect();
  const gridW = canvas.clientWidth || rect.width;
  const colW = gridW / 7;
  const loMin = (startMs - new Date(startMs).setHours(0, 0, 0, 0)) / 60000;
  const topInGrid = (loMin / 60) * AG_HOUR_H;
  const popW = pop.offsetWidth || 280;
  const popH = pop.offsetHeight || 200;
  const gridH = canvas.clientHeight || 24 * AG_HOUR_H;
  // 靠右的列向左翻，靠下的时段向上翻；再夹回网格内（右下角那一格容易差几像素）。
  // 网格比气泡还窄时（窄面板）不夹：让它从左缘开始、右侧溢出，好过压到别处
  let left = col * colW + 2;
  if (left + popW > gridW - 2) left = left + colW - popW;
  if (popW <= gridW - 4) left = Math.max(2, Math.min(left, gridW - popW - 2));
  else left = 2;
  let top = topInGrid + 4;
  if (top + popH > gridH - 4) top = topInGrid - popH - 4;
  top = popH <= gridH - 4 ? Math.max(2, Math.min(top, gridH - popH - 2)) : 2;
  pop.style.left = left + "px";
  pop.style.top = top + "px";
  canvas.dataset.popOpen = "1"; // 给网格留个记号（测试与调试用）

  const errEl = pop.querySelector(".ag-pop-err");
  const showErr = (msg) => { errEl.textContent = msg; errEl.classList.remove("hidden"); };
  const titleEl = pop.querySelector("#ag-pop-title");

  // 空标题不让提交：按钮变灰，不弹错误（未输入不是「错」）
  const okBtn = pop.querySelector('[data-act="ok"]');
  const syncOk = () => { okBtn.disabled = !titleEl.value.trim(); };
  titleEl.oninput = syncOk;
  syncOk();

  const save = async () => {
    errEl.classList.add("hidden");
    const title = titleEl.value.trim();
    if (!title) { showErr("请填写标题"); titleEl.focus(); return; }
    const sv = pop.querySelector("#ag-pop-start").value;
    if (!sv) { showErr("请填写开始时间"); return; }
    const dayStart = new Date(startMs); dayStart.setHours(0, 0, 0, 0);
    const toMs = (hhmm) => {
      const [h, mi] = hhmm.split(":").map(Number);
      return dayStart.getTime() + (h * 60 + mi) * 60000;
    };
    const s = toMs(sv);
    const ev = pop.querySelector("#ag-pop-end").value;
    let e = 0;
    if (ev) {
      e = toMs(ev);
      if (e <= s) { showErr("结束时间要晚于开始时间"); return; }
    }
    okBtn.disabled = true;
    try {
      await request("schedule.add", {
        title,
        start_at: s / 1000,
        end_at: e / 1000,
        notes: pop.querySelector("#ag-pop-notes").value.trim(),
        remind: pop.querySelector("#ag-pop-remind").checked,
        remind_before: 0,
      });
    } catch (err) {
      errEl.textContent = "✗ " + err.message;
      errEl.classList.remove("hidden");
      okBtn.disabled = false;
      return;
    }
    closeAgendaPop();
    await loadAgenda();
  };

  /** 把气泡里已填的值交给完整弹窗（新建态预填，不是编辑已有日程） */
  const toFullModal = () => {
    const sv = pop.querySelector("#ag-pop-start").value || agHmMs(startMs);
    const ev = pop.querySelector("#ag-pop-end").value;
    const dayStart = new Date(startMs); dayStart.setHours(0, 0, 0, 0);
    const toTs = (hhmm) => {
      const [h, mi] = hhmm.split(":").map(Number);
      return (dayStart.getTime() + (h * 60 + mi) * 60000) / 1000;
    };
    const preset = {
      title: titleEl.value.trim(),
      notes: pop.querySelector("#ag-pop-notes").value.trim(),
      remind: pop.querySelector("#ag-pop-remind").checked,
      remind_before: 0,
      start_at: toTs(sv),
      end_at: ev ? toTs(ev) : 0,
    };
    closeAgendaPop();
    agendaModal(null, preset.start_at, preset);
  };

  pop.querySelector('[data-act="ok"]').onclick = save;
  pop.querySelector('[data-act="cancel"]').onclick = closeAgendaPop;
  const notesBtn = pop.querySelector('[data-act="notes"]');
  notesBtn.onclick = () => {
    const n = pop.querySelector("#ag-pop-notes");
    const opening = n.classList.contains("hidden");
    n.classList.toggle("hidden", !opening);
    notesBtn.textContent = opening ? "－ 备注" : "＋ 备注";
    if (opening) n.focus();
  };
  pop.querySelector('[data-act="more"]').onclick = toFullModal;
  // 回车保存、Esc 关闭；气泡内的按键不冒泡到聊天输入框等全局处理
  // isComposing：中文输入法选词的回车不算「提交」（同聊天输入框的处理）
  pop.onkeydown = (e) => {
    if (e.isComposing) return;
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); save(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeAgendaPop(); }
  };
  // 点气泡外（含别的格子）关闭：用捕获阶段监听，先关再让网格自己处理新的拖选/单击
  const onDocDown = (e) => { if (!pop.contains(e.target)) closeAgendaPop(); };
  const t = setTimeout(() => document.addEventListener("pointerdown", onDocDown, true), 0);
  agPopDetach = () => {
    clearTimeout(t);
    document.removeEventListener("pointerdown", onDocDown, true);
  };
  titleEl.focus();
  // 气泡可能落在网格可视区之外（比如选了 23:00），滚到看得见的位置
  try { pop.scrollIntoView({ block: "nearest" }); } catch (e) { /* 老内核不支持则忽略 */ }
}

function agendaModal(existing, defaultTs, preset) {
  const isEdit = !!existing;
  // preset：给「新建」预填一份初值（就地气泡转完整弹窗时用），不改变编辑语义
  // 预填字段优先于空值默认；没有 preset 时行为与从前完全一致
  const pre = isEdit ? null : preset;
  const box = document.createElement("div");
  const d = existing ? new Date(existing.start_at * 1000)
    : defaultTs ? new Date(defaultTs * 1000)
    : new Date(Date.now() + 3600000);
  const p = pad2;
  const dtLocal = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  // 结束时间：默认 1 小时后（新建）、沿用已有值（编辑）或气泡传进来的区间；
  // 没填就是按点事件
  const hasEnd = existing ? !!existing.end_at : !!(pre && pre.end_at);
  const endD = existing && existing.end_at ? new Date(existing.end_at * 1000)
    : pre && pre.end_at ? new Date(pre.end_at * 1000)
    : existing ? new Date(existing.start_at * 1000 + 3600000)
    : new Date(d.getTime() + 3600000);
  const endLocal = `${endD.getFullYear()}-${p(endD.getMonth() + 1)}-${p(endD.getDate())}T${p(endD.getHours())}:${p(endD.getMinutes())}`;
  const titleVal = existing ? existing.title : (pre?.title ?? "");
  box.innerHTML = `
    <div class="agenda-fields">
      <label>标题</label>
      <input id="ag-title" class="modal-input" type="text" placeholder="例如：小组会 / 交房租" value="${escapeHtml(titleVal)}">
      <label>开始</label>
      <input id="ag-time" class="modal-input" type="datetime-local" value="${dtLocal}">
      <label class="agenda-remind"><input id="ag-span-on" type="checkbox" ${hasEnd ? "checked" : ""}> 设定时间段（填结束时间）</label>
      <label id="ag-end-label" class="agenda-end-label">结束
        <input id="ag-end" class="modal-input" type="datetime-local" value="${endLocal}">
      </label>
      <label>备注（可选）</label>
      <input id="ag-notes" class="modal-input" type="text" value="${escapeHtml((existing ? existing.notes : pre?.notes) || "")}">
      <label class="agenda-remind"><input id="ag-remind" type="checkbox" ${(existing ? existing.remind : (pre ? pre.remind : true)) ? "checked" : ""}> 到点在应用内提醒</label>
      <label>提前量</label>
      <select id="ag-remind-before" class="modal-input">
        <option value="0">到点才提醒</option>
        <option value="5">提前 5 分钟</option>
        <option value="10">提前 10 分钟</option>
        <option value="15">提前 15 分钟</option>
        <option value="30">提前 30 分钟</option>
        <option value="60">提前 1 小时</option>
      </select>
    </div>`;
  box.querySelector("#ag-remind-before").value =
    String((existing ? existing.remind_before : pre?.remind_before) ?? 0);
  // 未勾选「设定时间段」时藏起结束时间输入框，避免让人以为它是必填项
  const spanOn = box.querySelector("#ag-span-on");
  const endLabel = box.querySelector("#ag-end-label");
  const endInput = box.querySelector("#ag-end");
  const syncEnd = () => { endLabel.classList.toggle("hidden", !spanOn.checked); };
  syncEnd();
  spanOn.onchange = syncEnd;
  // 改动开始时间时把结束时间跟着平移，保持原来的时长（不然容易一下子变成倒置区间）
  box.querySelector("#ag-time").onchange = () => {
    if (!spanOn.checked) return;
    const sv = box.querySelector("#ag-time").value;
    const ev = endInput.value;
    if (!sv || !ev) return;
    const delta = new Date(ev).getTime() - new Date(dtLocal).getTime();
    const next = new Date(new Date(sv).getTime() + (delta > 0 ? delta : 3600000));
    endInput.value = `${next.getFullYear()}-${p(next.getMonth() + 1)}-${p(next.getDate())}T${p(next.getHours())}:${p(next.getMinutes())}`;
  };
  showModal(isEdit ? "编辑日程" : "新增日程", box, async () => {
    const title = box.querySelector("#ag-title").value.trim();
    const tstr = box.querySelector("#ag-time").value;
    if (!title) throw new Error("请填写标题");
    if (!tstr) throw new Error("请选择开始时间");
    const ts = new Date(tstr).getTime() / 1000;
    if (Number.isNaN(ts)) throw new Error("时间格式不正确");
    // 结束时间为空 = 取消时间段，显式传 0 让后端清掉旧值（编辑已有日程时）
    let endTs = 0;
    if (spanOn.checked) {
      const estr = endInput.value;
      if (!estr) throw new Error("请选择结束时间，或取消勾选「设定时间段」");
      endTs = new Date(estr).getTime() / 1000;
      if (Number.isNaN(endTs)) throw new Error("结束时间格式不正确");
      if (endTs <= ts) throw new Error("结束时间要晚于开始时间");
    }
    const params = {
      title,
      start_at: ts,
      end_at: endTs,
      notes: box.querySelector("#ag-notes").value.trim(),
      remind: box.querySelector("#ag-remind").checked,
      remind_before: Number(box.querySelector("#ag-remind-before").value) || 0,
    };
    if (isEdit) {
      await request("schedule.update", { id: existing.id, ...params });
    } else {
      await request("schedule.add", params);
    }
    await loadAgenda();
  }, isEdit ? "保存" : "添加");
}

// ---------- 每日运行日报：当天定时任务 / 任务编排终态的定时汇总（automation.daily_report_*） ----------
// 开关默认关；每天到设定时刻经已启用的渠道（聊天渠道 / Webhook）推一次，
// 应用没开着错过时刻则开机后补发当天一份，送达失败当天不重试（取舍见引擎侧
// daily_report.py 的模块注释）。行常驻自动化页两个分段之下，方法不可用时保持隐藏。

async function loadDailyReportRow() {
  const row = document.getElementById("daily-report-row");
  if (!row) return;
  let st;
  try {
    st = await request("automation.daily_report_status");
  } catch (e) {
    return; // 旧后端 / 远端被拒：维持初始 hidden，不报错打扰
  }
  const tgl = document.getElementById("daily-report-toggle");
  const t = document.getElementById("daily-report-time");
  if (tgl) tgl.checked = !!st.enabled;
  if (t) t.value = st.time || "09:00";
  row.classList.remove("hidden");
  bindDailyReportRow(); // 幂等重绑：重复 load 不叠加行为
}

// 即改即存（与归档提炼开关同一交互）；失败按后台真实值回拨
async function saveDailyReportRow(patch) {
  try {
    const r = await request("automation.daily_report_save", patch);
    const tgl = document.getElementById("daily-report-toggle");
    const t = document.getElementById("daily-report-time");
    if (tgl) tgl.checked = !!r.enabled;
    if (t) t.value = r.time || "09:00";
    return r;
  } catch (e) {
    addNotice("日报设置保存失败: " + e.message);
    await loadDailyReportRow();
    return null;
  }
}

function bindDailyReportRow() {
  const tgl = document.getElementById("daily-report-toggle");
  if (tgl) tgl.onchange = () => saveDailyReportRow({ enabled: tgl.checked });
  const t = document.getElementById("daily-report-time");
  if (t) t.onchange = () => saveDailyReportRow({ time: t.value });
}

// ---------- 今日运行总览（automation.run_center_summary，只读聚合） ----------
// 四行摘要（今日任务 / token 与预算 / 下次调度 / 日报状态）+ 可展开的今日执行
// 明细。明细已按当前项目过滤；预算未设或前置版本（配置层无预算字段）时后端
// 回 null，该行只显示用量。方法不可用（旧后端）保持初始 hidden，不报错打扰
// ——同日报开关行的姿态。

let runCenterOpen = false;  // 明细展开状态：cron/pipeline 事件高频重拉时不打断查看
let runCenterData = null;   // 最近一次聚合结果（展开/收起就地重画用，不重发请求）

async function loadRunCenter() {
  const card = document.getElementById("run-center-card");
  if (!card) return;
  let d;
  try {
    d = await request("automation.run_center_summary", {});
  } catch (e) {
    return; // 旧后端 / 远端被拒：维持初始 hidden，不报错打扰
  }
  runCenterData = d || {};
  renderRunCenter(runCenterData);
}

// 状态 → 单字符标记与颜色分组（对齐流水线面板的语义：跳过不算失败、用户停止不算失败）
function runCenterMark(status) {
  if (status === "error") return { m: "✗", cls: "bad" };
  if (status === "cancelled") return { m: "⏹", cls: "" };
  if (status === "skipped") return { m: "⏭", cls: "" };
  if (status === "done" || status === "ok" || status === "empty") return { m: "✓", cls: "ok" };
  return { m: "·", cls: "" };
}

function renderRunCenter(d) {
  const card = document.getElementById("run-center-card");
  if (!card || !d) return;
  card.classList.remove("hidden");
  const q = (id) => document.getElementById(id);
  const cron = d.cron || {}, pipe = d.pipeline || {};
  const cItems = cron.items || [], pItems = pipe.items || [];
  // 四行摘要（字段缺失容忍：一律 || 兜底，旧后端少字段也不缺行）
  q("run-center-line-tasks").textContent =
    `今日任务 ${cron.total || 0} 成功 ${cron.ok || 0} 失败 ${cron.error || 0}` +
    (pipe.total ? ` · 流水线节点 ${pipe.total} 个（未成功 ${pipe.error || 0}）` : "");
  const usage = d.usage || {};
  let usageTxt = `今日 token ${usageFmtTokens(usage.today || 0)}`;
  if (usage.budget != null && usage.remaining != null) {
    usageTxt += ` · 预算剩 ${usageFmtTokens(usage.remaining)} / ${usageFmtTokens(usage.budget)}`;
  }
  q("run-center-line-usage").textContent = usageTxt;
  q("run-center-line-next").textContent = "下次调度 " + fmtNextRun(d.next_run_at || 0);
  const report = d.daily_report || {};
  q("run-center-line-report").textContent =
    report.enabled ? `日报 开（每天 ${report.time || "09:00"}）` : "日报 关";
  // 明细：定时任务与流水线节点合并按时间倒序；今天没有任何记录给一句话空态
  const det = q("run-center-detail");
  if (!cItems.length && !pItems.length) {
    det.innerHTML = '<div class="dim small" style="padding:4px 0 2px">今天还没有定时任务或流水线的运行记录。</div>';
  } else {
    const hm = (ts) => {
      const x = new Date((ts || 0) * 1000);
      return `${pad2(x.getHours())}:${pad2(x.getMinutes())}`;
    };
    const rows = [
      ...cItems.map((it) => ({
        at: it.at,
        mark: runCenterMark(it.status),
        head: it.name || "未命名任务",
        detail: it.result || "",
      })),
      ...pItems.map((it) => ({
        at: it.at,
        mark: runCenterMark(it.status),
        head: `${it.pipeline || "流水线"} · ${it.title || "未命名节点"}`,
        detail: it.result || "",
      })),
    ].sort((a, b) => (b.at || 0) - (a.at || 0));
    det.innerHTML =
      `<ul class="rc-list">` +
      rows.map((r) =>
        `<li><span class="rc-mark ${r.mark.cls}">${r.mark.m}</span>` +
        `<span class="rc-time">${hm(r.at)}</span>` +
        `<span class="rc-txt">${escapeHtml(r.head)}` +
        (r.detail ? `<span class="dim"> — ${escapeHtml(r.detail)}</span>` : "") +
        `</span></li>`).join("") +
      `</ul>`;
  }
  det.classList.toggle("hidden", !runCenterOpen);
  const btn = q("run-center-detail-btn");
  if (btn) {
    btn.textContent = runCenterOpen ? "明细 ▴" : "明细 ▾";
    btn.onclick = () => {  // 幂等重绑：重复 load 不叠加行为（同 bindDailyReportRow）
      runCenterOpen = !runCenterOpen;
      renderRunCenter(runCenterData);
    };
  }
}

// ---------- 定时任务：无人值守的周期 Agent 任务（cron.list/add/update/delete/run_now） ----------

function fmtCronSchedule(t) {
  if (t.schedule_type === "interval") return `每 ${t.interval_minutes} 分钟`;
  if (t.schedule_type === "daily") return `每天 ${t.time_of_day}`;
  const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  return `每周${days[t.weekday] || "?"} ${t.time_of_day}`;
}

function fmtNextRun(ts) {
  if (!ts) return "未排期";
  const d = new Date(ts * 1000);
  const p = pad2;
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadCron() {
  loadDailyReportRow(); // 日报开关行与定时任务同源（automation 家族），顺手刷新
  loadRunCenter();      // 今日运行总览：定时任务跑完写回任务行，卡片跟着刷新
  // 工具勾选表要用 boot 快照里的工具清单（含权限级别），别让用户手打工具名
  if (!bootSnap || !bootSnap.tools) {
    try { bootSnap = await request("boot"); } catch (e) { /* 取不到就退化提示 */ }
  }
  let tasks = [];
  try {
    tasks = (await request("cron.list")).tasks || [];
  } catch (e) {
    document.getElementById("cron-list").innerHTML =
      `<li class="dim small" style="padding:6px 10px">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const ul = document.getElementById("cron-list");
  ul.innerHTML = "";
  if (!tasks.length) {
    ul.innerHTML = '<li class="dim small" style="padding:6px 10px">还没有定时任务 —— 点右上角 ＋ 新建，让 Agent 到点自己干活</li>';
    return;
  }
  tasks.forEach((t) => {
    const li = document.createElement("li");
    li.className = "cron-item" + (t.enabled ? "" : " off");
    const status = t.last_status === "ok" ? "✓" : t.last_status === "error" ? "✗" : "";
    li.innerHTML =
      `<div class="cron-main">
         <span class="cron-title">${t.enabled ? "" : "⏸ "}${escapeHtml(t.name)}</span>
         <span class="cron-sched">${escapeHtml(fmtCronSchedule(t))} · 下次 ${fmtNextRun(t.next_run_at)}</span>
         ${t.last_result ? `<span class="cron-last">${status} ${escapeHtml(t.last_result)}</span>` : ""}
       </div>`;
    const ops = document.createElement("span");
    ops.className = "cron-ops";
    ops.innerHTML =
      `<button class="cron-op" data-op="run" title="立即运行一次">▶</button>` +
      `<button class="cron-op" data-op="pipeline" title="纳入任务编排（复制成流水线节点）">⛓</button>` +
      `<button class="cron-op" data-op="schtask" title="系统计划任务：状态查询中…">…</button>` +
      `<button class="cron-op" data-op="toggle" title="${t.enabled ? "暂停" : "启用"}">${t.enabled ? "⏸" : "▶"}</button>` +
      `<button class="cron-op danger" data-op="del" title="删除">✕</button>`;
    ops.querySelector('[data-op="run"]').onclick = async (e) => {
      e.stopPropagation();
      try {
        const r = await request("cron.run_now", { id: t.id });
        addNotice(r && r.started === false
          ? (r.notice || `「${t.name}」正在运行中，本次未重复触发`)
          : `已开始运行「${t.name}」，结果会写回列表`);
      } catch (err) { addNotice("运行失败: " + err.message); }
    };
    ops.querySelector('[data-op="pipeline"]').onclick = async (e) => {
      e.stopPropagation();
      await pipelineImportModal({ source: "cron", cron_id: t.id });
    };
    ops.querySelector('[data-op="toggle"]').onclick = async (e) => {
      e.stopPropagation();
      try { await request("cron.update", { id: t.id, enabled: !t.enabled }); }
      catch (err) { addNotice("操作失败: " + err.message); }
      await loadCron();
    };
    ops.querySelector('[data-op="del"]').onclick = async (e) => {
      e.stopPropagation();
      if (!(await confirmModal("删除定时任务",
        `<p>确定删除定时任务 <b>${escapeHtml(t.name)}</b> 吗？</p>` +
        `<p class="dim small">到点不再运行；历史配置不可恢复。</p>`, "删除"))) return;
      try { await request("cron.delete", { id: t.id }); }
      catch (err) { addNotice("删除失败: " + err.message); }
      await loadCron();
    };
    // 系统计划任务（Windows 任务计划程序集成）：导出 / 移除 / 已导出状态
    const schBtn = ops.querySelector('[data-op="schtask"]');
    schBtn.onclick = async (e) => {
      e.stopPropagation();
      if (schBtn.dataset.exported === "1") {
        if (!(await confirmModal("移除系统计划任务",
          `<p>确定移除系统计划任务 <b>SkySheep-${t.id}</b> 吗？</p>` +
          `<p class="dim small">移除后到点不再独立执行；SkySheep 运行期间的调度不受影响。</p>`,
          "移除"))) return;
        try {
          const r = await request("cron.schtask_remove", { id: t.id });
          addNotice(r.removed ? `已移除系统计划任务 ${r.task_name}` : (r.notice || "移除失败"));
        } catch (err) { addNotice("移除失败: " + err.message); }
      } else {
        if (!(await confirmModal("导出为系统计划任务",
          `<p>将在 Windows 任务计划程序创建 <b>SkySheep-${t.id}</b>，按「${escapeHtml(fmtCronSchedule(t))}」到点用命令行独立执行，<b>SkySheep 应用不需要打开</b>。</p>` +
          `<p class="dim small">结果照常写回这个列表；开了「完成后推送到聊天渠道」的照样推送。应用内停用任务后，系统计划任务到点也会跳过执行。</p>`,
          "导出"))) return;
        try {
          const r = await request("cron.schtask_export", { id: t.id });
          addNotice(r.exported ? `已导出为系统计划任务 ${r.task_name}，到点独立执行` : (r.notice || "导出失败"));
        } catch (err) { addNotice("导出失败: " + err.message); }
      }
      await loadCron();
    };
    // 已导出状态现查现显（schtasks /Query 只读）；非 Windows / 查询不可用时藏掉按钮
    request("cron.schtask_status", { id: t.id }).then((r) => {
      if (!r.supported) { schBtn.style.display = "none"; return; }
      schBtn.dataset.exported = r.exported ? "1" : "0";
      schBtn.textContent = r.exported ? "⊟" : "⊞";
      schBtn.title = r.exported
        ? "已导出为系统计划任务（点击移除）"
        : "导出为系统计划任务（应用关闭时也能到点运行）";
    }).catch(() => { schBtn.style.display = "none"; });
    li.appendChild(ops);
    li.onclick = () => cronModal(t);
    ul.appendChild(li);
  });
}

function cronModal(existing) {
  const isEdit = !!existing;
  const box = document.createElement("div");
  const t = existing || { name: "", prompt: "", schedule_type: "interval",
                          interval_minutes: 60, time_of_day: "09:00", weekday: 0,
                          allowed_tools: [], enabled: true, notify_channel: false };
  const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const allowed = new Set(t.allowed_tools || []);
  // 工具勾选表：从最近一次 boot 快照取工具清单（含权限级别），让用户勾而不是背工具名
  const tools = (bootSnap && bootSnap.tools) || [];
  const toolRows = tools.length
    ? tools.map((tool) => {
        const locked = tool.safety === "readonly";  // 只读本来就是自动放行的
        const on = locked || allowed.has(tool.name);
        return `<label class="cron-tool${locked ? " locked" : ""}" title="${escapeHtml(tool.description || "")}">
            <input type="checkbox" data-tool="${escapeHtml(tool.name)}"${on ? " checked" : ""}${locked ? " disabled" : ""}>
            <span class="ct-name">${escapeHtml(tool.name)}</span>
            <span class="chip ${tool.safety === "dangerous" ? "danger-mark" : tool.safety === "write" ? "write-mark" : "safe-mark"}">${SAFETY_LABELS[tool.safety] || tool.safety}</span>
          </label>`;
      }).join("")
    : '<div class="dim small">（暂时取不到工具清单，保存后可在任务列表里再编辑）</div>';
  box.innerHTML = `
    <div class="cron-fields">
      <label>任务名</label>
      <input id="cron-name" class="modal-input" type="text" placeholder="例如：每日项目状态汇总" value="${isEdit ? escapeHtml(t.name) : ""}">
      <label>任务指令（Agent 每次到点执行的内容）</label>
      <textarea id="cron-prompt" class="modal-input" rows="3" placeholder="例如：扫描当前目录的 TODO 标记，汇总成一段进度报告">${isEdit ? escapeHtml(t.prompt) : ""}</textarea>
      <label>频率</label>
      <select id="cron-type" class="modal-input cron-select">
        <option value="interval" ${t.schedule_type === "interval" ? "selected" : ""}>每隔 N 分钟</option>
        <option value="daily" ${t.schedule_type === "daily" ? "selected" : ""}>每天固定时间</option>
        <option value="weekly" ${t.schedule_type === "weekly" ? "selected" : ""}>每周固定时间</option>
      </select>
      <div id="cron-interval-wrap">
        <label>间隔（分钟）</label>
        <input id="cron-interval" class="modal-input" type="number" min="1" step="5" value="${t.interval_minutes}">
      </div>
      <div id="cron-time-wrap" class="hidden">
        <label>时间（HH:MM）</label>
        <input id="cron-time" class="modal-input" type="time" value="${escapeHtml(t.time_of_day)}">
        <label id="cron-weekday-label">星期几</label>
        <select id="cron-weekday" class="modal-input cron-select">
          ${days.map((d, i) => `<option value="${i}" ${t.weekday === i ? "selected" : ""}>${d}</option>`).join("")}
        </select>
      </div>
      <label>预授权工具（无人值守运行时自动放行；不勾的会被自动拒绝）</label>
      <div class="cron-tools">${toolRows}</div>
      <label class="cron-notify"><input id="cron-notify" type="checkbox"${t.notify_channel ? " checked" : ""}> 完成后推送到聊天渠道</label>
      <p class="dim small">任务跑完（成功或失败）会在飞书/微信渠道收到一条摘要：状态、耗时与结果节选；
        渠道未启用或没有绑定聊天时静默跳过。渠道在 设置 · 聊天机器人渠道 里配置。</p>
      <p class="dim small">安全说明：定时任务无人值守运行，<b>只读工具本来就放行</b>；
        写入 / 执行类必须在这里勾选，否则运行时会自动拒绝。建议先只勾必要的。
        <b>勾选 run_command 等于允许无人值守执行任意命令</b>——任务文本一旦被注入，预授权就是它的通行证，只在任务内容完全可信时勾选。</p>
      <p class="dim small">运行前提：定时任务只在 <b>SkySheep 运行期间</b>触发（关窗时选「缩到系统托盘」它就继续在后台跑）。
        彻底退出期间错过的任务，会在下次打开应用时补跑一次。想让电脑一开机就守着，
        可在 设置 · 高级 里打开「开机自动启动」；想让应用彻底关闭也照常到点跑，
        可在任务卡片上点「⊞」导出为系统计划任务（Windows）。</p>
    </div>`;
  // 与任务列表里的卡片一致：频率切换时显隐对应字段
  const syncType = () => {
    const v = box.querySelector("#cron-type").value;
    box.querySelector("#cron-interval-wrap").classList.toggle("hidden", v !== "interval");
    box.querySelector("#cron-time-wrap").classList.toggle("hidden", v === "interval");
    const wd = box.querySelector("#cron-weekday");
    box.querySelector("#cron-weekday-label").style.display = v === "weekly" ? "" : "none";
    wd.style.display = v === "weekly" ? "" : "none";
  };
  box.querySelector("#cron-type").addEventListener("change", syncType);
  syncType();
  showModal(isEdit ? "编辑定时任务" : "新建定时任务", box, async () => {
    const name = box.querySelector("#cron-name").value.trim();
    const prompt = box.querySelector("#cron-prompt").value.trim();
    if (!prompt) throw new Error("任务指令不能为空");
    const stype = box.querySelector("#cron-type").value;
    const picked = [...box.querySelectorAll('input[data-tool]:checked')]
      .map((el) => el.dataset.tool);
    const params = {
      name: name || (prompt.length > 20 ? prompt.slice(0, 20) : prompt),
      prompt,
      schedule_type: stype,
      interval_minutes: Number(box.querySelector("#cron-interval").value) || 60,
      time_of_day: box.querySelector("#cron-time").value || "09:00",
      weekday: Number(box.querySelector("#cron-weekday").value) || 0,
      allowed_tools: picked,
      notify_channel: box.querySelector("#cron-notify").checked,
    };
    if (isEdit) await request("cron.update", { id: existing.id, ...params });
    else await request("cron.add", params);
    await loadCron();
    addNotice(isEdit ? "定时任务已更新" : "定时任务已创建，到点自动运行");
  }, isEdit ? "保存" : "创建");
}


// ---------- 任务编排：流水线（pipeline.list/create/start/cancel/delete/node_rerun） ----------
// 多个任务排成依赖图：并行开发 → 最后一个汇总审查。与定时任务同一套无人值守
// 门控（预授权名单外的写/执行自动拒绝），差别是按「依赖完成」触发而非时间。

const PL_NODE_STATUS = {
  blocked: "等待依赖", ready: "待运行", running: "运行中",
  done: "已完成", error: "失败", cancelled: "已取消", skipped: "已跳过",
};
const PL_NODE_MARK = { blocked: "◌", ready: "▸", running: "●", done: "✓", error: "✗", cancelled: "⏹", skipped: "–" };
// 控制流节点（control 字段）：改变运行逻辑本身，而不是执行一个任务步骤
const PL_CONTROL = {
  gate: { label: "条件门", mark: "◇", hint: "产出首行 PASS = 放行下游，FAIL = 下游跳过" },
  stop: { label: "终止", mark: "⏹", hint: "依赖满足时在此截停整条流水线" },
  loop: { label: "迭代", mark: "↻", hint: "反复执行直到产出首行出现 DONE，或达到轮数上限" },
};
const PL_PIPE_STATUS = {
  draft: "草稿（未启动）", running: "运行中", done: "已完成",
  failed: "有失败", cancelled: "已停止",
};
const pipelineSeenStatus = {}; // pipeline_updated 判状态迁移用（只报完成/失败一次）

function plToolPicker(selected) {
  // 工具勾选表：与定时任务同源（boot 快照），只读工具天然放行不用勾
  const allowed = new Set(selected || []);
  const tools = (bootSnap && bootSnap.tools) || [];
  if (!tools.length) return '<div class="dim small">（暂时取不到工具清单，默认仅只读运行）</div>';
  return tools.map((tool) => {
    const locked = tool.safety === "readonly";
    const on = locked || allowed.has(tool.name);
    return `<label class="cron-tool${locked ? " locked" : ""}" title="${escapeHtml(tool.description || "")}">
        <input type="checkbox" data-tool="${escapeHtml(tool.name)}"${on ? " checked" : ""}${locked ? " disabled" : ""}>
        <span class="ct-name">${escapeHtml(tool.name)}</span>
        <span class="chip ${tool.safety === "dangerous" ? "danger-mark" : tool.safety === "write" ? "write-mark" : "safe-mark"}">${SAFETY_LABELS[tool.safety] || tool.safety}</span>
      </label>`;
  }).join("");
}

// 流水线依赖图：分层布局（layer = 1 + 最深上游）+ SVG 贝塞尔连线。
// 纯 HTML/SVG 自绘，不引 mermaid——节点量小、要融入面板配色、同步渲染即可。
// 宽度在挂载后实测（行卡/弹窗宽度不定），ResizeObserver 兜底重排（面板拖宽/收展）。
function mountPipelineGraph(el, nodes, opts = {}) {
  if (!el) return;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const layerOf = new Map();
  const depth = (n) => {
    if (layerOf.has(n.id)) return layerOf.get(n.id);
    layerOf.set(n.id, 0); // 先占位防环（后端保证无环，这里只防手滑）
    const ups = (n.depends_on || []).map((id) => byId.get(id)).filter(Boolean);
    const l = ups.length ? Math.max(...ups.map(depth)) + 1 : 0;
    layerOf.set(n.id, l);
    return l;
  };
  nodes.forEach(depth);
  const layers = [];
  nodes.forEach((n) => {
    const l = layerOf.get(n.id) || 0;
    (layers[l] = layers[l] || []).push(n);
  });
  const rows = layers.filter(Boolean);
  const W = Math.max(el.clientWidth || 0, 180);
  const LAYER_H = 48, NODE_H = 26, GAP = 8;
  const maxRow = Math.max(...rows.map((r) => r.length));
  const nodeW = Math.max(64, Math.min(150, Math.floor((W - GAP) / maxRow) - GAP));
  const H = rows.length * LAYER_H;
  const pos = new Map();
  rows.forEach((layer, li) => {
    const slot = W / layer.length;
    layer.forEach((n, i) => {
      pos.set(n.id, {
        x: slot * i + (slot - nodeW) / 2,
        y: li * LAYER_H + (LAYER_H - NODE_H) / 2,
      });
    });
  });
  let edges = "";
  nodes.forEach((n) => {
    (n.depends_on || []).forEach((id) => {
      const up = pos.get(id), me = pos.get(n.id);
      if (!up || !me) return;
      const x1 = up.x + nodeW / 2, y1 = up.y + NODE_H;
      const x2 = me.x + nodeW / 2, y2 = me.y;
      const mid = (y1 + y2) / 2;
      edges += `<path class="pl-edge" marker-end="url(#pl-arrow)" d="M${x1} ${y1} C${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}"/>`;
    });
  });
  let boxes = "";
  nodes.forEach((n) => {
    const p = pos.get(n.id);
    const ctl = PL_CONTROL[n.control];
    const mark = ctl ? ctl.mark : (PL_NODE_MARK[n.status] || "◌");
    const loopTag = n.control === "loop" && (n.max_runs || 0) > 1
      ? ` ${n.runs || 0}/${n.max_runs}` : "";
    boxes += `<div class="pl-gnode pl-st-${n.status}${ctl ? " pl-ctl-" + n.control : ""}" style="left:${p.x}px;top:${p.y}px;width:${nodeW}px"` +
      (opts.onPick ? ` data-gid="${n.id}"` : "") +
      ` title="${escapeHtml(n.title)} · ${PL_NODE_STATUS[n.status] || n.status}${ctl ? " · " + ctl.label : ""}">` +
      `<span>${mark} ${escapeHtml(n.title)}${loopTag}</span></div>`;
  });
  el.innerHTML = `<div class="pl-graph" style="height:${H}px">` +
    `<svg width="${W}" height="${H}">` +
    `<defs><marker id="pl-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">` +
    `<path d="M0 0L8 4L0 8" fill="none" stroke="currentColor" stroke-width="1.2"/></marker></defs>${edges}</svg>` +
    `${boxes}</div>`;
  if (opts.onPick) {
    el.querySelectorAll("[data-gid]").forEach((b) => {
      b.onclick = () => opts.onPick(b.dataset.gid);
    });
  }
  // 宽度变化超过阈值才重排一次：innerHTML 重写自身也会触发 RO，靠门槛断掉循环；
  // 重排挪进 rAF（下一帧再动布局），否则 RO 回调内同步改尺寸会刷
  // 「ResizeObserver loop completed」告警、被全局错误横幅接住吓到用户
  let raf = 0;
  const ro = new ResizeObserver(() => {
    const w = el.clientWidth;
    if (!w || Math.abs(w - (+el.dataset.w || 0)) < 24) return;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      el.dataset.w = String(el.clientWidth);
      mountPipelineGraph(el, nodes, opts);
    });
  });
  el.dataset.w = String(W);
  // 同一元素重复挂载前先摘掉旧 observer（重排递归重挂、列表刷新重渲染都会
  // 再进本函数）：旧实现每次 new 一个且从不 disconnect，observer 在元素上
  // 无上界累积，之后每次拖宽全部回调、成倍全量重渲染
  if (el._plRO) el._plRO.disconnect();
  el._plRO = ro;
  ro.observe(el);
}

async function loadPipelines() {
  loadDailyReportRow(); // 日报汇总口径含流水线终态：任务编排分段打开时同样刷新
  loadRunCenter();      // 今日运行总览：流水线节点到终态写回节点行，卡片跟着刷新
  if (!bootSnap || !bootSnap.tools) {
    try { bootSnap = await request("boot"); } catch (e) { /* 取不到就退化提示 */ }
  }
  let pipes = [];
  try {
    pipes = (await request("pipeline.list")).pipelines || [];
  } catch (e) {
    // 无项目态是正常状态不是故障：给引导而不是「加载失败」
    document.getElementById("pipeline-list").innerHTML = String(e.message || "").startsWith("当前没有项目")
      ? '<li class="dim small" style="padding:6px 10px">先在侧栏「项目」区添加项目，再创建流水线（流水线按项目的工作目录运行）</li>'
      : `<li class="dim small" style="padding:6px 10px">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  pipes.forEach((p) => { pipelineSeenStatus[p.id] = p.status; });
  const ul = document.getElementById("pipeline-list");
  ul.innerHTML = "";
  if (!pipes.length) {
    ul.innerHTML = '<li class="dim small" style="padding:6px 10px">还没有流水线 —— 点右上角 ＋ 新建，把几个任务排成「并行开发 → 汇总审查」</li>';
    return;
  }
  pipes.forEach((p) => {
    const done = (p.nodes || []).filter((n) => n.status === "done").length;
    const li = document.createElement("li");
    li.className = "cron-item pipeline-item pl-pipe-" + p.status;
    li.innerHTML =
      `<div class="cron-main">
         <span class="cron-title">${escapeHtml(p.name)}</span>
         <span class="cron-sched">${PL_PIPE_STATUS[p.status] || p.status} · 节点 ${done}/${(p.nodes || []).length} · 并发 ${p.concurrency}</span>
         <div class="pl-graph-mount"></div>
       </div>`;
    const ops = document.createElement("span");
    ops.className = "cron-ops";
    if (p.status === "running") {
      ops.innerHTML = `<button class="cron-op" data-op="stop" title="停止流水线">⏹</button>` +
        `<button class="cron-op" data-op="dup" title="复制为草稿">⧉</button>` +
        `<button class="cron-op danger" data-op="del" title="删除">✕</button>`;
    } else {
      // 草稿/已停止/已完成/有失败都可启动：延展了新节点或重跑失败节点后要能再跑
      //（没有可运行节点时后端会拒绝并提示，不会空转）
      ops.innerHTML = `<button class="cron-op" data-op="start" title="启动">▶</button>` +
        `<button class="cron-op" data-op="dup" title="复制为草稿">⧉</button>` +
        `<button class="cron-op danger" data-op="del" title="删除">✕</button>`;
    }
    ops.querySelector("[data-op='dup']").onclick = async (e) => {
      e.stopPropagation();
      try {
        const r = await request("pipeline.duplicate", { id: p.id });
        addNotice(`已复制为草稿「${r.pipeline.name}」，结构与预授权保持一致`);
      } catch (err) { addNotice("复制失败: " + err.message); }
      await loadPipelines();
    };
    const start = ops.querySelector('[data-op="start"]');
    if (start) start.onclick = async (e) => {
      e.stopPropagation();
      try {
        await request("pipeline.start", { id: p.id });
        addNotice(`流水线「${p.name}」已启动，依赖满足的节点会自动运行`);
      } catch (err) { addNotice("启动失败: " + err.message); }
      await loadPipelines();
    };
    const stop = ops.querySelector('[data-op="stop"]');
    if (stop) stop.onclick = async (e) => {
      e.stopPropagation();
      try { await request("pipeline.cancel", { id: p.id }); }
      catch (err) { addNotice("停止失败: " + err.message); }
      await loadPipelines();
    };
    ops.querySelector('[data-op="del"]').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除流水线「${p.name}」？运行中的节点也会停止，记录不可恢复。`)) return;
      try { await request("pipeline.delete", { id: p.id }); }
      catch (err) { addNotice("删除失败: " + err.message); }
      await loadPipelines();
    };
    li.onclick = () => pipelineModal(p);
    ul.appendChild(li);
    const gbox = li.querySelector(".pl-graph-mount");
    if (gbox && (p.nodes || []).length) mountPipelineGraph(gbox, p.nodes);
  });
}

function pipelineModal(p) {
  const box = document.createElement("div");
  const KIND_BADGE = { task: "⛓ 挂接任务", session: "💬 会话续跑" };
  const timeoutTxt = (n) => n.kind !== "task"
    ? (n.timeout_s > 0 ? ` · 超时 ${Math.round(n.timeout_s / 60)} 分钟` : " · 不限时") : "";
  const rows = (p.nodes || []).map((n) => {
    const dep = (n.depends_on || []).map((d) => {
      const dn = (p.nodes || []).find((x) => x.id === d);
      return dn ? dn.title : "#" + d;
    }).join("、");
    const badge = KIND_BADGE[n.kind]
      ? `<span class="chip">${KIND_BADGE[n.kind]}</span>` : "";
    const rerun = n.kind !== "task" && n.status !== "running"
      ? `<button class="rp-mini" data-rerun="${n.id}">↻ 重跑这个节点${n.status === "done" ? "（含下游）" : ""}</button>` : "";
    const taskNote = n.kind === "task"
      ? `<div class="dim small">跟随原任务的状态与产出，不占流水线并发；任务本身在「任务」页管理。</div>` : "";
    const ctl = PL_CONTROL[n.control];
    const ctlNote = ctl
      ? `<div class="dim small">${ctl.mark} ${ctl.label}：${ctl.hint}${n.control === "loop" ? `（当前第 ${n.runs || 0}/${n.max_runs} 轮）` : ""}</div>` : "";
    return `<div class="pl-node-view pl-st-${n.status}" data-nid="${n.id}">
        <div class="pl-node-head">
          <b>${ctl ? ctl.mark : (PL_NODE_MARK[n.status] || "◌")} ${escapeHtml(n.title)}</b>
          ${badge}
          ${ctl ? `<span class="chip">${ctl.label}</span>` : ""}
          <span class="chip ${n.status === "done" ? "safe-mark" : n.status === "error" ? "danger-mark" : n.status === "running" ? "write-mark" : ""}">${PL_NODE_STATUS[n.status] || n.status}</span>
        </div>
        ${ctlNote}
        ${taskNote}
        ${dep ? `<div class="dim small">依赖：${escapeHtml(dep)}（${n.dep_mode === "any" ? "任一完成即可开始" : "全部完成才开始"}）</div>` : ""}
        ${n.allowed_tools && n.allowed_tools.length ? `<div class="dim small">预授权：${escapeHtml(n.allowed_tools.join("、"))}</div>` : ""}
        ${n.kind !== "task" ? `<div class="dim small">运行方式：无人值守${escapeHtml(timeoutTxt(n))} · 已尝试 ${n.runs || 0} 次</div>` : ""}
        ${n.last_error ? `<div class="pl-node-err">✗ ${escapeHtml(n.last_error)}</div>` : ""}
        ${n.result ? `<div class="pl-node-result">${escapeHtml(n.result.length > 400 ? n.result.slice(0, 400) + "…" : n.result)}</div>` : ""}
        ${rerun}
      </div>`;
  }).join("");
  box.innerHTML = `
    <div class="cron-fields">
      <p class="dim small">流水线「${escapeHtml(p.name)}」· ${PL_PIPE_STATUS[p.status] || p.status} ·
        每个节点是一次无人值守运行（预授权名单外的写/执行自动拒绝）；上游完成后下游自动开始，产出会注入下游的指令里。</p>
      <div class="dim small" id="pl-usage" style="display:flex;gap:12px;align-items:center">
        <span>用量统计中…</span>
        <span class="spacer"></span>
        <button class="rp-mini" data-op="export">⇩ 导出 JSON</button>
      </div>
      <label class="cron-notify"><input id="pl-notify" type="checkbox"${p.notify_channel ? " checked" : ""}> 节点完成后推送到聊天渠道</label>
      <p class="dim small">每个节点到终态都会推一条紧凑进度（失败必推）；整条流水线收尾时照常再推一条汇总。渠道在 设置 · 聊天机器人渠道 里配置。</p>
      <div id="pl-graph-wrap" class="pl-graph-wrap"></div>
      ${rows || '<div class="dim small">（没有节点）</div>'}
      <p class="dim small">要改节点/指令：删除后重建（Agent 对话里说一句也能帮你重排）。</p>
    </div>`;
  // 推送开关：改完即存（pipeline.update），热生效
  box.querySelector("#pl-notify").onchange = async (e) => {
    try {
      await request("pipeline.update", { id: p.id, notify_channel: e.target.checked });
      addNotice(e.target.checked
        ? "已开启节点推送：每个节点到终态都会在聊天渠道收到进度"
        : "已关闭节点推送");
    } catch (err) {
      addNotice("保存失败: " + err.message);
      e.target.checked = !e.target.checked;
    }
  };
  // 用量汇总 + 导出：现拉一次详情（列表接口不带用量，避免每次刷新都聚合）
  request("pipeline.get", { id: p.id }).then((r) => {
    const u = r.usage || {};
    const el = box.querySelector("#pl-usage");
    if (el) el.firstElementChild.textContent =
      `已用 tokens：输入 ${(u.in_tokens || 0).toLocaleString()} / 输出 ${(u.out_tokens || 0).toLocaleString()}`;
  }).catch(() => {
    const el = box.querySelector("#pl-usage");
    if (el) el.firstElementChild.textContent = "用量统计不可用";
  });
  box.querySelector("[data-op='export']").onclick = async () => {
    try {
      const r = await request("pipeline.export", { id: p.id });
      const blob = new Blob([JSON.stringify(r.export, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${p.name.replace(/[\\/:*?"<>|]/g, "_")}.pipeline.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { addNotice("导出失败: " + err.message); }
  };
  // 依赖图：点节点跳到对应详情卡并高亮一下
  const graphBox = box.querySelector("#pl-graph-wrap");
  mountPipelineGraph(graphBox, p.nodes || [], {
    onPick: (id) => {
      const card = box.querySelector(`.pl-node-view[data-nid="${id}"]`);
      if (!card) return;
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 1200);
    },
  });
  box.querySelectorAll("[data-rerun]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        const r = await request("pipeline.node_rerun", { id: Number(btn.dataset.rerun) });
        if (r.needs_confirm) {
          // done 节点重跑：下游产出基于旧结果，明确问一句再级联
          const names = (r.downstream || []).join("、");
          if (!confirm(`下游 ${r.downstream.length} 个节点（${names}）的产出基于它的旧结果。\n一并重跑这些下游吗？（取消 = 仅重跑本节点，下游保持旧产出）`)) return;
          await request("pipeline.node_rerun", { id: Number(btn.dataset.rerun), cascade: true });
          addNotice("节点与全部下游已重新排队，依赖满足后自动运行");
        } else {
          addNotice("节点已重新排队，依赖满足后自动运行");
        }
      } catch (err) { addNotice("重跑失败: " + err.message); }
      hideModal();
      await loadPipelines();
    };
  });
  // 弹窗只有一个确认键：按状态给主操作（草稿/已停止 → 启动；运行中 → 停止；其余 → 关闭）
  let okLabel = "关闭";
  let onOk = async () => {};
  if (p.status !== "running") {
    okLabel = "▶ 启动";
    onOk = async () => {
      await request("pipeline.start", { id: p.id });
      addNotice(`流水线「${p.name}」已启动，依赖满足的节点会自动运行`);
      await loadPipelines();
    };
  } else if (p.status === "running") {
    okLabel = "⏹ 停止";
    onOk = async () => {
      await request("pipeline.cancel", { id: p.id });
      addNotice(`流水线「${p.name}」已停止`);
      await loadPipelines();
    };
  }
  showModal(`任务编排 · ${p.name}`, box, onOk, okLabel);
}

function pipelineCreateModal() {
  const box = document.createElement("div");
  box.innerHTML = `
    <div class="cron-fields">
      <label>流水线名</label>
      <input id="pl-name" class="modal-input" type="text" placeholder="例如：登录模块开发">
      <label>同时运行的节点数（1~4）</label>
      <input id="pl-conc" class="modal-input" type="number" min="1" max="4" value="2">
      <label class="cron-notify"><input id="pl-notify-create" type="checkbox"> 节点完成后推送到聊天渠道</label>
      <div id="pl-nodes"></div>
      <button id="pl-add-node" class="rp-mini" type="button">＋ 添加节点</button>
      <p class="dim small">节点按依赖自动排序：勾选「依赖前面的节点」，被依赖的全部完成后才会开始。
        最后一个节点勾选依赖它前面的全部节点，就是汇总审查。</p>
      <p class="dim small">安全说明：每个节点无人值守运行，<b>只读工具本来就放行</b>；
        写入/执行类必须在该节点勾选，否则运行时自动拒绝。启动前请再检查一遍各节点的预授权。
        <b>勾选 run_command 等于允许无人值守执行任意命令</b>，只在节点指令完全可信时勾选。</p>
    </div>`;
  const nodesBox = box.querySelector("#pl-nodes");
  const addNodeRow = () => {
    const idx = nodesBox.children.length;
    const row = document.createElement("div");
    row.className = "pl-node-edit";
    const priorDeps = [];
    for (let i = 0; i < idx; i++) {
      const t = nodesBox.children[i].querySelector(".pl-node-title").value.trim();
      priorDeps.push(t || `节点 ${i + 1}`);
    }
    row.innerHTML = `
      <div class="pl-node-edit-head">
        <b>节点 ${idx + 1}</b>
        <select class="pl-node-type" title="节点的运行方式：普通=跑一步任务；条件门=判断走不走；终止=到此截停；迭代=反复打磨">
          <option value="">普通任务</option>
          <option value="gate">条件门（判断走不走）</option>
          <option value="stop">终止（到此截停）</option>
          <option value="loop">迭代（反复打磨）</option>
        </select>
        <span class="spacer"></span>
        ${idx > 0 ? '<button class="rp-mini pl-del-node" type="button" title="删除这个节点">✕</button>' : ""}
      </div>
      <input class="modal-input pl-node-title" type="text" placeholder="节点名，例如：实现登录接口">
      <textarea class="modal-input pl-node-prompt" rows="2" placeholder="这一步的完整指令（自包含，例如：在 src/auth 下实现登录接口并写测试）"></textarea>
      <label class="pl-retry-row dim small">失败自动重试 <input type="number" class="pl-node-retries" min="0" max="3" value="0"> 次</label>
      <label class="pl-loop-row dim small" hidden>最多迭代 <input type="number" class="pl-node-loopmax" min="2" max="10" value="3"> 轮（完成时把回复第一行写成 DONE）</label>
      <label class="pl-timeout-row dim small">超时 <input type="number" class="pl-node-timeout" min="0" max="1440" value="60"> 分钟（0 = 不限时；超时按失败重试处理，防卡死）</label>
      ${idx > 0 ? `<div class="pl-deps">依赖（完成后才运行本节点）：
        ${priorDeps.map((t, i) => `<label class="pl-dep"><input type="checkbox" value="${i}" checked>${escapeHtml(t)}</label>`).join("")}
        <label class="pl-dep pl-dep-mode">满足方式
          <select class="pl-node-depmode">
            <option value="all">全部完成</option>
            <option value="any">任一完成</option>
          </select>
        </label>
      </div>` : ""}
      <details class="pl-tools"><summary>预授权工具（默认仅只读）</summary>
        <div class="pl-tools-list">${plToolPicker([])}</div>
      </details>`;
    const typeSel = row.querySelector(".pl-node-type");
    const promptEl = row.querySelector(".pl-node-prompt");
    const promptPh = "这一步的完整指令（自包含，例如：在 src/auth 下实现登录接口并写测试）";
    typeSel.onchange = () => {
      const t = typeSel.value;
      promptEl.hidden = t === "stop"; // 终止节点不执行任务，不需要指令
      row.querySelector(".pl-retry-row").hidden = t !== "";
      row.querySelector(".pl-loop-row").hidden = t !== "loop";
      row.querySelector(".pl-timeout-row").hidden = t === "stop"; // 终止节点不派 Agent，无超时
      promptEl.placeholder = t === "gate"
        ? "描述要检查的条件。Agent 会在回复第一行输出 PASS（放行下游）或 FAIL（下游跳过），例如：检查 dist 目录是否存在"
        : t === "loop"
          ? "描述要反复打磨的任务。每轮结束会带着产出继续；完成时让 Agent 把回复第一行写成 DONE"
          : t === "stop"
            ? "（终止节点不执行任务：上游完成后即在此截停整条流水线）"
            : promptPh;
    };
    const del = row.querySelector(".pl-del-node");
    if (del) del.onclick = () => { row.remove(); rebuildTitles(); };
    nodesBox.appendChild(row);
  };
  const rebuildTitles = () => {
    // 删节点后重排序号与依赖勾选标签（依赖索引始终指向当前列表位置）
    [...nodesBox.children].forEach((row, i) => {
      row.querySelector("b").textContent = `节点 ${i + 1}`;
    });
  };
  addNodeRow();
  box.querySelector("#pl-add-node").onclick = addNodeRow;
  showModal("新建流水线", box, async () => {
    const name = box.querySelector("#pl-name").value.trim();
    const concurrency = Math.max(1, Math.min(4, Number(box.querySelector("#pl-conc").value) || 2));
    const nodes = [...nodesBox.children].map((row, i) => {
      const title = row.querySelector(".pl-node-title").value.trim();
      const control = row.querySelector(".pl-node-type").value;
      const prompt = row.querySelector(".pl-node-prompt").hidden
        ? "" : row.querySelector(".pl-node-prompt").value.trim();
      if (!prompt && control !== "stop") throw new Error(`节点 ${i + 1} 的指令不能为空`);
      const after = [...row.querySelectorAll(".pl-dep input:checked")].map((el) => Number(el.value));
      const dep_mode = row.querySelector(".pl-node-depmode")?.value === "any" ? "any" : "all";
      const allowed_tools = [...row.querySelectorAll("input[data-tool]:checked")].map((el) => el.dataset.tool);
      let max_runs = 1;
      if (control === "loop") {
        max_runs = Math.max(2, Math.min(10, Number(row.querySelector(".pl-node-loopmax").value) || 3));
      } else if (!control) {
        max_runs = Math.max(1, Math.min(4, (Number(row.querySelector(".pl-node-retries").value) || 0) + 1));
      }
      // 超时分钟转秒；终止节点不派 Agent，固定 0（不限时也无意义）
      const timeoutMin = control === "stop"
        ? 0 : Math.max(0, Math.min(1440, Number(row.querySelector(".pl-node-timeout").value) || 0));
      return { title: title || `节点 ${i + 1}`, prompt, after, dep_mode, allowed_tools, control, max_runs,
        timeout_s: timeoutMin * 60 };
    });
    await request("pipeline.create", { name: name || "未命名流水线", concurrency, nodes,
      notify_channel: box.querySelector("#pl-notify-create").checked });
    await loadPipelines();
    addNotice("流水线已创建（草稿）。检查各节点的预授权后点 ▶ 启动。");
  }, "创建");
}

async function pipelineImportModal(preset) {
  const source = preset.source || "cron";
  // 三类来源的候选一次性并行拉取（选中的那个才显示，失败静默降级为空）
  const [crons, tasks, sessions, pipes] = await Promise.all([
    request("cron.list").then((r) => r.tasks || []).catch(() => []),
    request("tasks.list").then((r) => (r.tasks || []).slice(0, 30)).catch(() => []),
    request("session.list").then((r) => (r.sessions || []).slice(0, 30)).catch(() => []),
    // 已结束的流水线也可以纳入新节点做延展（启动时按依赖继续调度）
    request("pipeline.list").then((r) => r.pipelines || []).catch(() => []),
  ]);
  const box = document.createElement("div");
  const opt = (v, label) => `<option value="${escapeHtml(String(v))}">${escapeHtml(label)}</option>`;
  const cronOptions = crons.map((c) =>
    opt(c.id, `${c.enabled ? "" : "（已停用）"}${c.name} · ${c.prompt.slice(0, 30)}`)).join("");
  const srcOptions = {
    cron: cronOptions,
    task: tasks.map((t) => opt(t.id, `[${t.status === "done" ? "已完成" : t.status === "running" ? "运行中" : t.status === "error" ? "失败" : "已取消"}] ${t.prompt.slice(0, 42)}`)).join(""),
    session: sessions.map((s) => opt(s.id, (s.title || s.id).slice(0, 42))).join(""),
  };
  box.innerHTML = `
    <div class="cron-fields">
      <label>纳入什么</label>
      <div class="pl-src">
        <label class="pl-dep"><input type="radio" name="pl-src" value="newtask" ${source === "newtask" ? "checked" : ""}> 新建任务（直接写指令，就地排进流水线）</label>
        <label class="pl-dep"><input type="radio" name="pl-src" value="cron" ${source === "cron" ? "checked" : ""}> 定时任务（复制指令与预授权）</label>
        <label class="pl-dep"><input type="radio" name="pl-src" value="task" ${source === "task" ? "checked" : ""}> 任务簿任务（跟随它在跑的状态与产出）</label>
        <label class="pl-dep"><input type="radio" name="pl-src" value="session" ${source === "session" ? "checked" : ""}> 已有会话（在原会话里续跑）</label>
        <label class="pl-dep"><input type="radio" name="pl-src" value="file" ${source === "file" ? "checked" : ""}> 导出文件（从 JSON 恢复整条流水线）</label>
      </div>
      <label id="pl-src-label">选定时任务</label>
      <select id="pl-src-obj" class="modal-input cron-select"></select>
      <div id="pl-file-wrap" class="hidden">
        <label>选择导出的 JSON 文件</label>
        <input id="pl-file-input" class="modal-input" type="file" accept=".json,application/json">
        <div id="pl-file-name" class="dim small">恢复为草稿，启动前请检查各节点的预授权。</div>
      </div>
      <label id="pl-target-label">放进哪条流水线</label>
      <select id="pl-target" class="modal-input cron-select">
        <option value="0">（新建流水线）</option>
        ${pipes.map((p) => opt(p.id, `${p.name}（${PL_PIPE_STATUS[p.status] || p.status}，${(p.nodes || []).length} 节点）`)).join("")}
      </select>
      <div id="pl-newname-wrap" class="hidden">
        <label>新流水线名</label>
        <input id="pl-newname" class="modal-input" type="text" placeholder="例如：登录模块开发">
      </div>
      <div id="pl-prompt-wrap" class="hidden">
        <label id="pl-prompt-label">这一轮要它做什么（在该会话已有的上下文里继续）</label>
        <textarea id="pl-session-prompt" class="modal-input" rows="2" placeholder="例如：结合刚才的实现，把接口文档补全"></textarea>
      </div>
      <div id="pl-deps-wrap" class="hidden">
        <label>等哪些节点完成后再跑（不勾 = 立即可跑）</label>
        <div id="pl-deps-box" class="pl-deps"></div>
      </div>
      <label class="pl-dep" id="pl-disable-wrap"><input type="checkbox" id="pl-disable-cron"> 导入后停用原定时任务（不再按周期独立运行）</label>
      <!-- 安全说明写成单行：模板里换行缩进会被 HTML 折叠成一个空格，
           恰好落在段落自动换行处时左缘就参差了（看着像没对齐） -->
      <p class="dim small">安全说明：纳入的节点同样无人值守执行——只读工具放行，其余按节点预授权名单，名单外自动拒绝。挂接节点跟随原任务、不占并发额度；会话节点在该会话正被使用时会等空闲。</p>
    </div>`;
  const q = (sel) => box.querySelector(sel);
  const srcObjs = { cron: srcOptions.cron, task: srcOptions.task, session: srcOptions.session };
  const srcLabels = { cron: "选定时任务", task: "选任务簿任务", session: "选会话" };
  // 「新建任务」与「已有会话」共用同一条指令输入框，只换标签与示例
  const promptHints = {
    session: ["这一轮要它做什么（在该会话已有的上下文里继续）", "例如：结合刚才的实现，把接口文档补全"],
    newtask: ["任务指令（启动后无人值守执行，可在面板里按节点补预授权）", "例如：把 docs 目录里的接口文档全部补全"],
  };
  const curSource = () => box.querySelector('input[name="pl-src"]:checked').value;
  const curObjVal = () => q("#pl-src-obj").value;
  let fileExport = null; // 「导出文件」来源：解析后的 JSON（提交时交给 pipeline.import）
  const renderSrc = () => {
    const s = curSource();
    // 「导出文件」恢复的是整条流水线：不选对象、不选目标流水线，只选文件；
    // 「新建任务」同样不选对象，但要选目标流水线并写指令
    const isFile = s === "file";
    const isNewTask = s === "newtask";
    q("#pl-src-label").classList.toggle("hidden", isFile || isNewTask);
    q("#pl-src-obj").classList.toggle("hidden", isFile || isNewTask);
    q("#pl-file-wrap").classList.toggle("hidden", !isFile);
    q("#pl-target-label").classList.toggle("hidden", isFile);
    q("#pl-target").classList.toggle("hidden", isFile);
    if (isFile) {
      q("#pl-newname-wrap").classList.add("hidden");
      q("#pl-deps-wrap").classList.add("hidden");
      q("#pl-prompt-wrap").classList.add("hidden");
      q("#pl-disable-wrap").classList.add("hidden");
      return;
    }
    if (!isNewTask) {
      q("#pl-src-obj").innerHTML = srcObjs[s];
      q("#pl-src-label").textContent = srcLabels[s];
      q("#pl-src-obj").disabled = !srcObjs[s];
      if (!srcObjs[s]) q("#pl-src-obj").innerHTML = `<option value="">（暂时没有可纳入的${srcLabels[s].slice(1)}）</option>`;
      // 预设来源对象（从任务簿/定时任务行的 ⛓ 进来）
      if (preset.cron_id && s === "cron") q("#pl-src-obj").value = String(preset.cron_id);
      if (preset.task_id && s === "task") q("#pl-src-obj").value = String(preset.task_id);
      if (preset.session_id && s === "session") q("#pl-src-obj").value = String(preset.session_id);
    }
    const [pLabel, pPh] = promptHints[s] || promptHints.session;
    q("#pl-prompt-label").textContent = pLabel;
    q("#pl-session-prompt").placeholder = pPh;
    q("#pl-prompt-wrap").classList.toggle("hidden", !(s === "session" || isNewTask));
    q("#pl-disable-wrap").classList.toggle("hidden", s !== "cron");
    syncTargetUi();
  };
  const renderDeps = () => {
    const target = q("#pl-target").value;
    const wrap = q("#pl-deps-wrap");
    const p = pipes.find((x) => String(x.id) === target);
    if (!p || !(p.nodes || []).length) { wrap.classList.add("hidden"); return; }
    wrap.classList.remove("hidden");
    q("#pl-deps-box").innerHTML = p.nodes
      .map((n) => `<label class="pl-dep"><input type="checkbox" value="${n.id}">${escapeHtml(n.title.slice(0, 24))}</label>`)
      .join("");
  };
  const syncTargetUi = () => {
    const isNew = q("#pl-target").value === "0";
    q("#pl-newname-wrap").classList.toggle("hidden", !isNew);
    if (isNew) q("#pl-deps-wrap").classList.add("hidden");
    else renderDeps();
  };
  box.querySelectorAll('input[name="pl-src"]').forEach((el) => el.addEventListener("change", renderSrc));
  q("#pl-target").addEventListener("change", syncTargetUi);
  q("#pl-file-input").addEventListener("change", async () => {
    const file = q("#pl-file-input").files && q("#pl-file-input").files[0];
    const nameEl = q("#pl-file-name");
    fileExport = null;
    if (!file) { nameEl.textContent = "恢复为草稿，启动前请检查各节点的预授权。"; return; }
    try {
      const parsed = JSON.parse(await file.text());
      if (!parsed || parsed.format !== "skysheep-pipeline") throw new Error("不是 SkySheep 流水线导出文件");
      fileExport = parsed;
      nameEl.textContent = `✓ ${file.name}：${(parsed.nodes || []).length} 个节点`;
    } catch (err) {
      nameEl.textContent = "✗ " + err.message;
    }
  });
  renderSrc();

  showModal("导入到任务编排", box, async () => {
    const s = curSource();
    if (s === "file") {
      // 从导出 JSON 恢复整条流水线（草稿态，启动仍由用户在面板确认预授权）
      if (!fileExport) throw new Error("先选择一个 SkySheep 导出的 JSON 文件");
      const r = await request("pipeline.import", { export: fileExport });
      await loadPipelines();
      addNotice(`已导入流水线「${r.pipeline.name}」（草稿）。检查各节点的预授权后点 ▶ 启动。`);
      return;
    }
    const objId = curObjVal();
    // 「新建任务」没有来源对象可选，只要求指令非空（分支里各自校验）
    if (s !== "newtask" && !objId) throw new Error("先选一个要纳入的对象");
    const targetId = Number(q("#pl-target").value);
    const dependsOn = [...box.querySelectorAll("#pl-deps-box input:checked")].map((el) => Number(el.value));
    let targetLabel;
    if (targetId === 0) {
      // 新建流水线：直接 create 一个带该节点的流水线（create 支持 task_id/session_id）
      const name = q("#pl-newname").value.trim();
      let node;
      if (s === "cron") {
        const c = crons.find((x) => String(x.id) === objId) || {};
        if (!c.prompt) throw new Error("该定时任务没有指令内容");
        node = {
          title: `定时任务：${c.name || ""}`.trim(),
          prompt: c.prompt, allowed_tools: c.allowed_tools || [],
        };
      } else if (s === "task") {
        node = { task_id: String(objId) };
      } else if (s === "newtask") {
        const prompt = q("#pl-session-prompt").value.trim();
        if (!prompt) throw new Error("新任务要写清要做什么");
        node = { title: `新任务：${prompt.slice(0, 40)}`, prompt };
      } else {
        const prompt = q("#pl-session-prompt").value.trim();
        if (!prompt) throw new Error("会话节点要写清这一轮要做什么");
        node = { session_id: String(objId), prompt };
      }
      await request("pipeline.create", { name: name || "未命名流水线", nodes: [node] });
      targetLabel = name || "未命名流水线";
    } else if (s === "newtask") {
      const prompt = q("#pl-session-prompt").value.trim();
      if (!prompt) throw new Error("新任务要写清要做什么");
      const r = await request("pipeline.add_task", {
        id: targetId, prompt, depends_on: dependsOn,
      });
      targetLabel = r.pipeline.name;
    } else if (s === "cron") {
      const r = await request("pipeline.import_cron", {
        id: targetId, cron_id: Number(objId), depends_on: dependsOn,
        disable_source: q("#pl-disable-cron").checked,
      });
      targetLabel = r.pipeline.name;
    } else if (s === "task") {
      const r = await request("pipeline.attach", {
        id: targetId, task_id: String(objId), depends_on: dependsOn,
      });
      targetLabel = r.pipeline.name;
    } else {
      const prompt = q("#pl-session-prompt").value.trim();
      if (!prompt) throw new Error("会话节点要写清这一轮要做什么");
      const r = await request("pipeline.add_session", {
        id: targetId, session_id: String(objId), prompt, depends_on: dependsOn,
      });
      targetLabel = r.pipeline.name;
    }
    await loadPipelines();
    addNotice(`已纳入流水线「${targetLabel}」（草稿/运行中都会按依赖自动调度）`);
  }, "纳入");
}

// 到点提醒横幅（后台循环推送 schedule_reminder 事件）
function showAgendaReminder(item) {
  const bar = document.createElement("div");
  bar.className = "agenda-reminder";
  const remainMin = Math.ceil((item.start_at * 1000 - Date.now()) / 60000);
  const remainTxt = remainMin > 0 ? ` · 约 ${remainMin} 分钟后开始` : "";
  bar.innerHTML =
    `<span class="ar-ico">⏰</span>
     <div class="ar-body">
       <b>${escapeHtml(item.title)}</b>
       <span class="small dim">${fmtAgendaSpan(item)}${remainTxt}${item.notes ? " · " + escapeHtml(item.notes) : ""}</span>
     </div>
     <button class="btn-ghost" data-a="done">完成</button>
     <button class="btn-ghost" data-a="snooze">稍后提醒</button>
     <button class="ar-close" title="关闭">✕</button>`;
  const close = () => bar.remove();
  bar.querySelector('[data-a="done"]').onclick = async () => {
    await request("schedule.update", { id: item.id, done: true });
    if (rightViewVisible("agenda")) await loadAgenda();
    close();
  };
  bar.querySelector('[data-a="snooze"]').onclick = async () => {
    await request("schedule.update", { id: item.id, start_at: Date.now() / 1000 + 600 });
    if (rightViewVisible("agenda")) await loadAgenda();
    close();
  };
  bar.querySelector(".ar-close").onclick = close;
  document.body.appendChild(bar);
}
