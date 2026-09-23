/* SkySheep 桌面前端逻辑：WebSocket 协议客户端 + 聊天 UI。无外部依赖。 */
"use strict";

/* ── 目录（按分区注释 "// ----------" 检索；新增代码请挂在最接近的分区下）────
 *
 *  ① 基础设施：迷你 Markdown 渲染 / WebSocket 客户端（request、事件路由）
 *  ② 聊天渲染：会话标签、消息卡、思考块、圆桌卡、事件处理、图片附件、
 *     消息操作（复制/编辑/分叉/回退/重生成）、输入浮层（/ 命令、@ 提及、历史）
 *  ③ 侧栏与导航：项目列表、会话搜索、会话菜单、功能导航、日程/定时任务面板
 *  ④ 顶栏：模型菜单、思考强度、通知中心、系统通知
 *  ⑤ 右侧面板：浏览器/辅助对话/审查/文件/任务/宠物；终端独立成底部多标签面板（顶栏入口）
 *  ⑥ 设置页：模型服务、技能、MCP、子代理、记忆、局域网、用量、高级、关于
 *  ⑦ 其他：帮助、主题、缩放、拖拽调宽、复制、查找（Ctrl+F）、引导向导
 *
 *  维护约定：仍是单文件零构建（无工具链约束）；顶层 function 声明会挂到
 *  window（HTML 内联事件依赖这一点），顶层 let/const 不会——新增跨分区
 *  共享的状态时注意。若未来体量逼着拆分，按上面的分区边界整体搬移，
 *  搬前先给前端补冒烟测试（当前前端无自动化测试，拆分属高危操作）。
 * ─────────────────────────────────────────────────────────────────────── */

// ---------- 迷你 Markdown 渲染（先转义再渲染，安全） ----------
function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
// ---------- 迷你 Markdown 的块级解析辅助 ----------
// 表格 / 列表按行扫描：先把整段切成行，再整块替换成 HTML，最后用空行把块级元素
// 与相邻段落隔开（否则 <table> 会被塞进 <p> 里，浏览器解析出来的结构不可预期）。
const MD_BLOCK_RE = /^<(h\d|ul|ol|pre|blockquote|table|hr)[\s>]/;

function mdSplitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

/** GFM 分隔行：只由 - : | 与空白组成，且每格至少有连字符。 */
function mdIsTableSep(line) {
  const s = line.trim();
  if (!s.includes("-") || !/^\|?[\s:|-]+\|?$/.test(s)) return false;
  const cells = mdSplitRow(s);
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
}

function mdAlign(cell) {
  const left = cell.startsWith(":"), right = cell.endsWith(":");
  if (left && right) return "ta-c";
  if (right) return "ta-r";
  if (left) return "ta-l";
  return "";
}

/** 把「表头 + 分隔行 + 连续数据行」整块转成 table；无表格则原样返回。 */
function mdTables(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.includes("|") && i + 1 < lines.length && mdIsTableSep(lines[i + 1])) {
      const head = mdSplitRow(line);
      const aligns = mdSplitRow(lines[i + 1]).map(mdAlign);
      const rows = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim() !== "" && lines[j].includes("|")
             && !mdIsTableSep(lines[j])) {
        rows.push(mdSplitRow(lines[j]));
        j++;
      }
      const cell = (tag, txt, k) =>
        aligns[k] ? `<${tag} class="${aligns[k]}">${txt}</${tag}>` : `<${tag}>${txt}</${tag}>`;
      const thead = "<tr>" + head.map((c, k) => cell("th", c, k)).join("") + "</tr>";
      const tbody = rows.map((r) => {
        // 行内单元格数少于表头时补空，多于表头时忽略多余列（列宽以表头为准）
        const cells = head.map((_, k) => cell("td", r[k] === undefined ? "" : r[k], k));
        return "<tr>" + cells.join("") + "</tr>";
      }).join("");
      out.push("", `<table class="md-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`, "");
      i = j - 1;
      continue;
    }
    out.push(line);
  }
  return out.join("\n");
}

/** 无序/有序列表，支持一层缩进嵌套；块级元素用空行隔开。 */
function mdLists(text) {
  const lines = text.split("\n");
  const out = [];
  const item = (l) => /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(l);
  for (let i = 0; i < lines.length; i++) {
    const m = item(lines[i]);
    if (!m) { out.push(lines[i]); continue; }
    const items = [];
    while (i < lines.length) {
      const mm = item(lines[i]);
      if (!mm) break;
      const indent = mm[1].replace(/\t/g, "  ").length;
      items.push({
        depth: indent >= 2 ? 1 : 0,
        ordered: !/^[-*]$/.test(mm[2]),
        text: mm[3],
      });
      i++;
    }
    i--;
    // 首项总是顶层：空行会把「缩进子项」单独切成一块，否则会生成没有父项的嵌套列表
    if (items.length) items[0].depth = 0;
    // 每一层用自己那一层的标记类型（有序列表里嵌无序子项时，子列表该是 ul 而不是 ol）
    const topTag = items[0].ordered ? "ol" : "ul";
    const sub = items.find((x) => x.depth === 1);
    const subTag = sub && sub.ordered ? "ol" : "ul";
    let html = "";
    let openTop = false, openNested = false;
    for (const it of items) {
      if (it.depth === 0) {
        if (openNested) { html += `</${subTag}>`; openNested = false; }
        if (openTop) html += "</li>";
        else { html += `<${topTag}>`; openTop = true; }
        html += `<li>${it.text}`;
      } else {
        if (!openNested) { html += `<${subTag}>`; openNested = true; }
        else html += "</li>";
        html += `<li>${it.text}`;
      }
    }
    if (openNested) html += `</${subTag}>`;
    if (openTop) html += `</li></${topTag}>`;
    out.push("", html, "");
  }
  return out.join("\n");
}

function renderMarkdown(src) {
  const codeBlocks = [];
  let text = escapeHtml(src || "");
  // 围栏代码块（mermaid 图表：交给 mermaid.run 渲染成 SVG，见 renderMermaidIn；
  // 普通代码块带语言标记，收尾时由 highlightCodeIn 语法高亮，流式期间保持原文）
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const l = (lang || "").toLowerCase();
    const html = l === "mermaid"
      ? `<div class="mermaid">${code.replace(/\n$/, "")}</div>`
      : `<pre><code${l ? ` class="language-${l}"` : ""}>${code.replace(/\n$/, "")}</code></pre>`;
    const i = codeBlocks.push(html) - 1;
    return `\u0000CODE${i}\u0000`;
  });
  // 行内代码
  text = text.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  // 表格必须在标题/分隔线/列表之前处理：否则 | 与 - 会被别的规则吃掉
  text = mdTables(text);
  // 标题
  text = text.replace(/^#### (.*)$/gm, "<h4>$1</h4>")
             .replace(/^### (.*)$/gm, "<h3>$1</h3>")
             .replace(/^## (.*)$/gm, "<h2>$1</h2>")
             .replace(/^# (.*)$/gm, "<h1>$1</h1>");
  // 分隔线（独占一行）
  text = text.replace(/^ {0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$/gm, "\n<hr>\n");
  // 引用块：连续的 > 行合并成一个 blockquote（逐行各包一个会把多行引用碎成 N 段）
  text = text.replace(/(?:^&gt; ?[^\n]*\n?)+/gm, (run) => {
    const inner = run.split("\n")
      .filter((l) => l !== "")
      .map((l) => l.replace(/^&gt; ?/, ""))
      .join("<br>");
    return "<blockquote>" + inner + "</blockquote>\n";
  });
  text = mdLists(text);
  // 粗体 / 斜体 / 删除线 / 链接
  text = text.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  text = text.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  // 外链一律 rel="noopener noreferrer"（安全审查低危项）：模型输出里的链接
  // 打开后，新窗口拿不到 window.opener，也带不走 Referer
  text = text.replace(
    /\[([^\]]+)\]\((https?:[^)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  // 段落（代码块此时还是占位符，同样不能再包一层 <p>）
  const parts = text.split(/\n{2,}/).map((p) => {
    const t = p.trim();
    return MD_BLOCK_RE.test(t) || /^\u0000CODE\d+\u0000$/.test(t)
      ? p
      : `<p>${p.replace(/\n/g, "<br>")}</p>`;
  });
  text = parts.join("");
  // 还原代码块
  text = text.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => codeBlocks[i]);
  return text;
}

// ---------- WebSocket 客户端 ----------
let ws = null;
let reqSeq = 0;
const pendingReplies = new Map();

// ---------- 连接（WS 协议客户端） ----------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event) { handleEvent(msg.event, msg.data); return; }
    const p = pendingReplies.get(msg.id);
    if (p) {
      pendingReplies.delete(msg.id);
      if (msg.ok) p.resolve(msg.result);
      else p.reject(new Error(msg.error || "request failed"));
    }
  };
}

function request(method, params = {}) {
  const id = "r" + (++reqSeq);
  return new Promise((resolve, reject) => {
    pendingReplies.set(id, { resolve, reject });
    const doSend = () => {
      try {
        ws.send(JSON.stringify({ id, method, params }));
      } catch (e) {
        pendingReplies.delete(id);
        reject(e);
      }
    };
    if (ws.readyState === WebSocket.OPEN) doSend();
    else ws.addEventListener("open", doSend, { once: true }); // 连接就绪再发，避免首屏 boot 静默失败
  });
}

function setConn(on) {
  document.getElementById("conn-status").className = "dot " + (on ? "on" : "off");
  document.getElementById("status-text").textContent = on ? "已连接" : "重连中…";
}

// ---------- 聊天渲染 ----------
const chatBox = document.getElementById("chat");

// ---------- 会话标签：多会话并行，每个标签一条独立聊天流 ----------
// 事件带 session_id 路由（routeTab），流式状态挂在各标签对象上；
// 后台标签继续接收事件，切回时内容与滚动位置都在。
let chatTabs = [];
let activeTab = null;
let routeTab = null;
let sessionMeta = {}; // sid -> {title}（refreshSessions / snapshot 维护，标签标题用）

function newTabObj(sid, title) {
  const logEl = document.createElement("div");
  logEl.className = "chat-log";
  // role=log（隐含 aria-live=polite）：新消息插入时屏幕阅读器播报，流式增量不打断
  logEl.setAttribute("role", "log");
  logEl.setAttribute("aria-label", "对话消息");
  return {
    sid: sid || null, title: title || "", logEl, running: false,
    streamingEl: null, streamingText: "", rtCard: null, rtMemberEls: [],
    lastAssistantText: "", usage: null, needsPerm: false, permData: null,
    needHistory: false, eta: null, etaEl: null,
    thinkMs: 0, thinkT0: 0,
  };
}
function tabFor(sid) { return sid ? chatTabs.find((t) => t.sid === sid) || null : null; }
function curTab() { return routeTab || activeTab; }
function curLog() { const t = curTab(); return t ? t.logEl : chatBox; }
function scrollLog() {
  const t = curTab();
  if (t && t !== activeTab) return; // 后台标签追加内容不抢滚动
  // 滚动发生在当前标签的 .chat-log 上（#chat 本体 overflow:hidden，滚它无效）
  const el = (t && t.logEl) || chatBox.querySelector(".chat-log");
  if (el) el.scrollTop = el.scrollHeight;
}
function withTab(tab, fn) { routeTab = tab; try { fn(); } finally { routeTab = null; } }

function renderTabs() {
  const bar = document.getElementById("chat-tabs");
  // 常驻：单会话也显示（浏览器式页签，右侧带 ＋ 新建），不再「一个标签就藏起整条栏」
  bar.setAttribute("role", "tablist");
  bar.setAttribute("aria-label", "会话标签");
  bar.innerHTML = "";
  // 页签展示序：用户拖过（tab_order）按偏好序，没记过的按创建序追加在后面；
  // 空标签（无 sid，尚未落库）没有稳定 id、不参与排序，始终垫底
  const blanks = chatTabs.filter((t) => !t.sid);
  const bound = chatTabs.filter((t) => t.sid);
  const ordered = [];
  const seen = new Set();
  tabOrderPrefs.forEach((sid) => {
    const t = bound.find((x) => x.sid === sid);
    if (t) { ordered.push(t); seen.add(t.sid); }
  });
  bound.forEach((t) => { if (!seen.has(t.sid)) ordered.push(t); });
  ordered.push(...blanks);
  ordered.forEach((t) => {
    const el = document.createElement("div");
    el.className = "chat-tab" + (t === activeTab ? " active" : "") +
      (t.running ? " running" : "") + (t.needsPerm ? " needs-perm" : "");
    el.setAttribute("role", "tab");
    el.setAttribute("aria-selected", t === activeTab ? "true" : "false");
    el.tabIndex = t === activeTab ? 0 : -1; // roving tabindex：只有当前标签在 Tab 链上
    const title = t.title || (t.sid ? (sessionMeta[t.sid] || {}).title || "会话" : "新会话");
    el.innerHTML = `<span class="tab-title">${escapeHtml(title)}</span>` +
      (t.running ? '<span class="tab-dot" title="运行中"></span>' : "") +
      (t.needsPerm ? '<span class="tab-perm" title="等待你确认">🔒</span>' : "") +
      `<button class="tab-close" title="关闭标签${t.running ? "（运行中的会话会一并停止）" : ""}">✕</button>`;
    el.title = title + "（双击重命名）";
    el.onclick = (e) => {
      if (e.target.closest(".tab-close")) return;
      activateTab(t);
    };
    // 双击标签：就地改名（与侧栏会话行的行内改名同一套手感）。
    // 正在跑的也可以改：改名只写标题，不碰运行。
    el.ondblclick = (e) => {
      e.preventDefault();
      if (e.target.closest(".tab-close")) return;
      startTabRename(t, el);
    };
    el.oncontextmenu = (e) => {
      e.preventDefault();
      showTabMenu(t, { x: e.clientX, y: e.clientY });
    };
    el.querySelector(".tab-close").onclick = (e) => {
      e.stopPropagation();
      closeTab(t);
    };
    t.el = el; // 右键菜单/改名要找回这张标签的 DOM（renderTabs 每次重建，随渲染刷新）
    if (t.sid) wireTabDrag(el, t);
    bar.appendChild(el);
  });
  // 标签栏尾部的 ＋ 新建：浏览器式页签的固定收尾。与侧栏「新建会话」/ Ctrl+N
  // 同一个动作（startNewTab），只是把入口放到用户视线所在的标签栏上
  let plus = bar.querySelector(".tab-new");
  if (!plus) {
    plus = document.createElement("button");
    plus.className = "tab-new";
    plus.type = "button";
    plus.title = "新建会话（Ctrl+N）";
    plus.setAttribute("aria-label", "新建会话");
    plus.textContent = "＋";
    plus.onclick = () => startNewTab();
    bar.appendChild(plus);
  }
  // 方向键在标签间切换（激活即聚焦）；div 无原生键盘激活，Enter/空格补上。
  // bar 元素常驻，委托只绑一次
  if (!bar._navBound) {
    bar._navBound = true;
    bar.addEventListener("keydown", (e) => {
      const tabs = [...bar.querySelectorAll('[role="tab"]')];
      if (!tabs.length) return;
      const idx = tabs.indexOf(document.activeElement);
      if (e.key === "Enter" || e.key === " ") {
        if (idx >= 0) { e.preventDefault(); tabs[idx].click(); }
        return;
      }
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (idx === -1) return;
      e.preventDefault();
      const next = e.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
      tabs[next].focus();
      tabs[next].click();
    });
  }
  // 启动恢复：把当前标签集合与激活态写回 ui.json（下次启动回到同一现场）。
  // 放在 renderTabs 末尾是因为所有标签变动（开/关/接管/懒创建）最终都会走到这；
  // 只收有 sid 的标签，空标签（欢迎页）不进恢复列表。偏好写失败静默。
  // 初次 boot 的恢复流程自身也会触发 renderTabs——与写回同一份偏好，无副作用。
  const tabSids = ordered.filter((t) => t.sid).map((t) => t.sid);
  const activeSid = activeTab && activeTab.sid ? activeTab.sid : null;
  const lastKey = (persistSessionTabs._last = persistSessionTabs._last || "");
  const sig = tabSids.join(",") + "|" + (activeSid || "");
  if (sig !== lastKey) {
    persistSessionTabs._last = sig;
    saveUiPrefs({ session_tabs: tabSids, session_active: activeSid || "" });
  }
}

/** 上次写回的签名：标签集合或激活态变了才再写（renderTabs 高频调用，
    不去重的话每次重画都落一次盘）。 */
persistSessionTabs._last = "";

function persistSessionTabs() { /* 由 renderTabs 内联执行（见上），保留名字供测试锢定 */ }

// ---------- 消息导航条（minimap）：右侧一列小圆点，一颗对应一条消息 ----------
// 平时半透明圆点，当前视口所在消息的那颗拉长成实色亮条，相邻圆点按距离
// 渐变（近长远短，4 步外回到圆点）；点它平滑跳到对应消息，
// 悬停提示消息开头文字。消息太多匀不下时等距抽样（一颗代表一段，跳到段首）。
// 只在内容可滚动且不止一条消息时出现；随 attachTabLog 绑定当前标签的聊天流。
const chatMinimap = document.createElement("div");
chatMinimap.id = "chat-minimap";
chatMinimap.className = "hidden";
chatBox.appendChild(chatMinimap);
const MINI_ITEM_H = 10, MINI_GAP = 4; // 与 app.css 的圆点热区高 / 列间距保持一致
let miniLog = null, miniMut = null, miniRO = null, miniTargets = [], miniTops = [];
let miniTimer = 0, miniRaf = 0, miniRemap = 0, miniGen = 0;

// 消息在聊天流内容坐标系（= scrollTop / scrollTo 所在的坐标系）里的纵位。
// 两个坑：
//  1) getBoundingClientRect 返回物理像素，而 #app 上有 zoom（--ui-zoom），
//     不除回 uiScale 会与 scrollTop 差一个缩放倍率（0.9 缩放时偏小 10%）；
//     长对话里偏差累积，点圆点会落到错误位置、高亮也跟着错位。
//  2) 入场动画 rise 带 translateY transform，会被 rect 算进去，
//     所以必须在动画结束后校准（见 miniRebuild 里的 miniRemap）。
function miniTopOf(el, logRect) {
  const r = logRect || miniLog.getBoundingClientRect();
  return (el.getBoundingClientRect().top - r.top) / uiScale + miniLog.scrollTop;
}

function miniBind(log) {
  if (miniLog === log) { miniSchedule(); return; }
  if (miniMut) { miniMut.disconnect(); miniMut = null; }
  if (miniRO) { miniRO.disconnect(); miniRO = null; }
  // 旧流不再可见前先解绑它的 scroll 监听：来回切标签每次都重新 bind，
  // 不解绑的话同一个 log 会越积越多的 scroll 回调（内存慢性泄漏）
  if (miniLog) miniLog.removeEventListener("scroll", miniOnScroll);
  clearTimeout(miniRemap);
  miniGen++; // 换流后旧流的校准定时器作废
  miniLog = log;
  chatMinimap.textContent = "";
  chatMinimap.classList.add("hidden");
  if (!log) return;
  miniMut = new MutationObserver(miniSchedule);
  // 只观察 childList：流式渲染走 innerHTML 整树替换（childList 命中），
  // 高度变化还有 ResizeObserver 兜底；characterData 会在流式期间被
  // 逐字变更轰炸，防抖形同虚设
  miniMut.observe(log, { childList: true, subtree: true });
  miniRO = new ResizeObserver(miniSchedule);
  miniRO.observe(log);
  log.addEventListener("scroll", miniOnScroll, { passive: true });
  miniSchedule();
}

function miniSchedule() {
  clearTimeout(miniTimer);
  miniTimer = setTimeout(miniRebuild, 160); // 流式输出逐字长高，防抖后统一重排
}

function miniMsgs() {
  if (!miniLog) return [];
  const out = [];
  for (const el of miniLog.children) {
    // 只收真正的消息卡片：user / assistant / error。
    // 不能用 classList.contains("msg") 粗筛——.msg.notice（系统提示胶囊，如
    // 「已切换到规划模式」）与 .msg.plan-actions（计划执行按钮条）也带 msg 前缀，
    // 会被当成消息，多出圆点、与对话对不上（welcome 卡同理，它连 msg 都没有）
    if (!el.classList.contains("msg")) continue;
    if (el.classList.contains("notice") || el.classList.contains("plan-actions")) continue;
    out.push(el);
  }
  return out;
}

// 一个圆点必须对应一个「真能滚到」的位置：目标位置钳到可滚动范围内。
// 不钳制的话，末尾几条消息的目标会超出 maxScroll，点了只能滚到底、
// 高亮一律落到最后一颗（「点不动第 N 个圆点」的成因）。
function miniScrollTarget(el, logRect) {
  const maxScroll = Math.max(0, miniLog.scrollHeight - miniLog.clientHeight);
  return Math.min(maxScroll, Math.max(0, miniTopOf(el, logRect) - 10));
}

function miniRebuild() {
  if (!miniLog || !miniLog.isConnected) { chatMinimap.classList.add("hidden"); return; }
  const msgs = miniMsgs();
  const logH = miniLog.clientHeight;
  const scrollable = msgs.length > 1 && miniLog.scrollHeight > logH + 4;
  chatMinimap.classList.toggle("hidden", !scrollable);
  if (!scrollable) { chatMinimap.textContent = ""; return; }
  // 容器 rect 只读一次：循环里每条消息各读一次 getBoundingClientRect
  // 会强制布局多次（长会话 rebuild 期间每秒多次全量布局的来源之一）
  const logRect = miniLog.getBoundingClientRect();
  // 目标位置相同的连续消息合并成一颗（保留最后一条——滚到底时看到的正是末尾那几条）。
  // 内容只比视口高一点点时，末尾好几条的目标会被一起钳到底部；
  // 不合并就会出现多颗点了没反应、高亮全落最后一颗的死圆点。
  const slots = [];
  for (const el of msgs) {
    const target = miniScrollTarget(el, logRect);
    const last = slots[slots.length - 1];
    if (last && Math.abs(last.target - target) < 1) last.el = el;
    else slots.push({ el, target });
  }
  const availH = Math.max(120, Math.min(logH - 130, 560));
  const maxDots = Math.max(1, Math.floor((availH + MINI_GAP) / (MINI_ITEM_H + MINI_GAP)));
  const n = Math.min(slots.length, maxDots);
  const step = slots.length / n;
  miniTargets = []; miniTops = [];
  // 就地复用已有圆点，只在数量变化时增删——不能像以前那样每次 textContent=""
  // 重建整列：新插入的元素首帧就带着目标宽度，浏览器没有「起始值」可插值，
  // transition 被直接跳过，于是滚动时宽度是一档一档跳变而不是渐变（实测复现）。
  // 复用同一批元素后，宽度从旧值平滑变到新值，--mini-w 的 transition 才真正生效。
  const bars = chatMinimap.children;
  for (let i = 0; i < n; i++) {
    const el = slots[Math.floor(i * step)].el;
    let d = bars[i];
    if (!d) {
      d = document.createElement("div");
      d.className = "chat-minimap-dot";
      chatMinimap.appendChild(d);
    }
    // title 只在消息定稿（有 seq）后算一次并缓存；流式中的消息内容未定，
    // 每次重算（几千字的消息一次 rebuild 就是几千字符的扫描）
    let txt;
    if (el.dataset.seq) {
      txt = el._miniTitle;
      if (txt === undefined) {
        txt = (el.textContent || "").trim().replace(/\s+/g, " ");
        txt = txt.length > 80 ? txt.slice(0, 80) + "…" : txt;
        el._miniTitle = txt;
      }
    } else {
      txt = (el.textContent || "").trim().replace(/\s+/g, " ");
      txt = txt.length > 80 ? txt.slice(0, 80) + "…" : txt;
    }
    d.title = txt;
    // onclick 每轮重绑：闭包捕获的是本条消息的滚动目标，复用元素时必须跟着换
    d.onclick = () => miniLog.scrollTo({ top: miniScrollTarget(el), behavior: "smooth" });
    miniTargets.push(el);
    miniTops.push(miniTopOf(el, logRect));
  }
  // 多余的圆点从尾部移除（消息变少 / 容器变矮时）
  while (chatMinimap.children.length > n) chatMinimap.removeChild(chatMinimap.lastChild);
  miniUpdateActive();
  // 入场动画（320ms）结束的位置才作数：等它播完再校准一遍缓存的纵位与高亮
  const gen = ++miniGen;
  clearTimeout(miniRemap);
  miniRemap = setTimeout(() => {
    if (gen !== miniGen || !miniLog || !miniLog.isConnected) return;
    // 必须包一层箭头函数：.map(miniTopOf) 会把数组索引当第二个参数传进去，
    // 而第二参是 logRect（矩形对象）——拿到索引后 r.top 是 undefined，
    // 整列纵位全变 NaN，miniUpdateActive 里 NaN <= mark 恒为假，
    // 高亮就永远卡在第一颗（滑动时看着像「从最后一个点直接跳到第一个点」）。
    miniTops = miniTargets.map((el) => miniTopOf(el));
    miniUpdateActive();
  }, 480);
}

function miniUpdateActive() {
  if (!miniLog || chatMinimap.classList.contains("hidden")) return;
  const atBottom = miniLog.scrollTop + miniLog.clientHeight >= miniLog.scrollHeight - 4;
  const mark = miniLog.scrollTop + 24; // 视口顶缘略下取当前消息：点哪颗亮哪颗，滚动时跟随最上可见消息
  let on = 0;
  for (let i = 0; i < miniTops.length; i++) if (miniTops[i] <= mark) on = i;
  if (atBottom) on = miniTops.length - 1; // 贴底时末尾短消息到不了顶缘，强制亮最后一根
  // 从亮条向上下渐变：紧邻的略长、越远越短，4 步外回到圆点；
  // 宽度写在 --mini-w 上（CSS 的 width: var(--mini-w) 接住），transition 负责平滑
  const bars = chatMinimap.children;
  for (let i = 0; i < bars.length; i++) {
    const dist = Math.abs(i - on);
    bars[i].classList.toggle("on", i === on);
    bars[i].style.setProperty("--mini-w", (dist ? 10 + 22 * Math.max(0, 1 - dist / 4) : 32).toFixed(1) + "px");
  }
}

function miniOnScroll() {
  if (miniRaf) return;
  miniRaf = requestAnimationFrame(() => { miniRaf = 0; miniUpdateActive(); });
}

function attachTabLog(tab) {
  // 只挂当前激活标签的聊天流；其余保留在内存里（各自滚动位置天然保留）
  if (!tab) return;
  if (tab.logEl.parentElement !== chatBox) {
    chatBox.querySelectorAll(":scope > .chat-log").forEach((el) => {
      if (el !== tab.logEl) el.remove();
    });
    chatBox.appendChild(tab.logEl);
  }
  miniBind(tab.logEl);
}

async function activateTab(tab) {
  if (!tab) return;
  if (tab === activeTab) { attachTabLog(tab); return; }
  // 查找高亮挂在旧标签的 DOM 上：切标签先收掉，避免 mark 残留计数错乱
  if (findHits.length || !document.getElementById("find-bar").classList.contains("hidden")) {
    closeFindBar();
  }
  activeTab = tab;
  attachTabLog(tab);
  renderTabs();
  hidePermission();
  currentSessionId = tab.sid;
  // 后台标签从未渲染过历史（事件创建的）→ 拉一次历史；否则轻量激活。
  // 两种情形互斥：正在跑的标签不能去拉历史（会把流式内容盖掉），只做轻量激活。
  if (tab.needHistory && tab.sid && !tab.running) {
    await loadTabHistory(tab);
  } else if (tab.sid) {
    request("session.activate", { id: tab.sid }).catch(() => {});
  }
  if (tab.usage) setContextUsage(tab.usage.tokens, tab.usage.limit, tab);
  else setContextUsage(0, 0, tab); // 该标签还没发过消息：清掉读数并隐藏环，避免残留上一标签的数值
  if (tab.needsPerm && tab.permData) showPermission(tab.permData);
  petPrevRunning = !!tab.running;
  petRefresh();
  refreshSessions();
}

function openTabForSession(sid, title, opts = {}) {
  let t = tabFor(sid);
  let adopted = false;
  if (!t) {
    // 先接管一张空占位标签，再考虑新建。启动时没有历史会话、标签全关、刚点过
    //「新建会话」都会留下一张空标签（欢迎页）——不接管就会在旁边再叠一张，
    // 界面上看起来就是「点了一下 ＋，却冒出两个会话」。
    // 优先接管当前激活的那张：接管后台那张的话，激活的空标签还会赖在栏上。
    // 与 handleEvent 里「懒创建会话直接认领」是同一套做法。
    const blank = (activeTab && isBlankTab(activeTab)) ? activeTab : chatTabs.find(isBlankTab);
    if (blank) {
      t = blank;
      t.sid = sid;
      t.title = title || "";
      t.titleFixed = false; // 空标签上预命名的名字随标签换绑丢弃：它现在代表另一个会话
      adopted = true;
    } else {
      t = newTabObj(sid, title);
      chatTabs.push(t);
    }
    t.needHistory = !opts.withMessages;
  }
  if (opts.withMessages) {
    renderHistory(t, opts.withMessages);
    if (!t.logEl.children.length) withTab(t, showWelcome); // 空会话回到欢迎页
  }
  if (!opts.background) {
    // 接管来的标签常常就是当前激活那张，而 activateTab 对「已经是当前标签」
    // 会早退（它的语义是切标签）——这里补上接管必需的记账，否则标签名会一直
    // 停在「新会话」、currentSessionId 也不跟着走。
    if (adopted && t === activeTab) {
      attachTabLog(t);
      currentSessionId = t.sid;
      renderTabs();
    }
    activateTab(t);
  } else renderTabs();
  // 历史同样要在这里补拉（原因同上）
  if (t.needHistory && t === activeTab) loadTabHistory(t);
  return t;
}

/** 空占位标签：没有绑定会话、也没在跑（欢迎页那张）。可以被新会话接管。
    跑着的无会话标签不算——它正懒创建会话，抢过来会劫持别人的运行。 */
function isBlankTab(t) { return !!t && !t.sid && !t.running; }

/** 拉一张标签的历史并渲染（needHistory 先消费掉，避免重入重复请求）。 */
async function loadTabHistory(tab) {
  if (!tab || !tab.sid || tab.running || !tab.needHistory) return;
  tab.needHistory = false;
  try {
    const info = await request("session.resume", { id: tab.sid });
    renderHistory(tab, info.messages || []);
  } catch (e) { addNotice("加载会话内容失败: " + e.message); }
}

// ---------- 页签拖动排序（水平）：与侧栏拖拽同一套手势，落点是左右半分 ----------

/** 给页签接上水平拖拽。item 用 sid 当 id（空标签无 sid，不接拖——垫底占位）。 */
function wireTabDrag(el, t) {
  wireListDrag(el, { id: t.sid }, {
    // 落点按水平中线分左右：拖到页签左半=插到它前面，右半=后面
    over: (e) => ({ id: t.sid, pos: dragHalfPosX(e, el) }),
    commit: (dst) => commitTabOrder(t.sid, dst.id, dst.pos),
    rerender: renderTabs,
  });
}

function dragHalfPosX(e, el) {
  const rect = el.getBoundingClientRect();
  return e.clientX < rect.left + rect.width / 2 ? "before" : "after";
}

/** 页签新序提交：从标签栏 DOM 收集当前序（只收有 sid 的），与保存的偏好合并
    后落 ui.json。没拖过的标签（不在偏好里）按 DOM 现序一并记录，避免下次
    渲染时新旧两段顺序错乱。 */
function commitTabOrder(srcSid, dstSid, pos) {
  const keys = [...document.querySelectorAll("#chat-tabs .chat-tab")]
    .map((el) => {
      const t = chatTabs.find((x) => x.el === el);
      return t && t.sid ? t.sid : null;
    })
    .filter((x) => x);
  const from = keys.indexOf(srcSid);
  let to = keys.indexOf(dstSid);
  if (from < 0 || to < 0 || from === to) return Promise.resolve();
  keys.splice(from, 1);
  to = keys.indexOf(dstSid); // 抽走 src 后下标可能前移，重找
  if (pos === "after") to += 1;
  keys.splice(to, 0, srcSid);
  tabOrderPrefs = keys;
  return request("ui.save", { prefs: { tab_order: keys } }).catch(() => {});
}

function closeTab(tab) {
  const idx = chatTabs.indexOf(tab);
  if (idx < 0) return;
  if (tab.running && tab.sid) request("stop", { session_id: tab.sid }).catch(() => {});
  tab.logEl.remove();
  chatTabs.splice(idx, 1);
  if (activeTab === tab) {
    activeTab = null;
    hidePermission();
    const next = chatTabs[idx] || chatTabs[idx - 1] || null;
    if (next) activateTab(next);
    else { // 全关了：回到一张新会话标签
      const t = newTabObj(null, "");
      chatTabs.push(t);
      activeTab = t;
      attachTabLog(t);
      currentSessionId = null;
      showWelcome();
      renderTabs();
    }
  } else renderTabs();
}

function startNewTab() {
  // 已经站在一张空标签上就不再叠一张：连点「新建会话」/ Ctrl+N 之前会一直往上
  // 堆「新会话」标签，每个都只是同一张欢迎页。直接复用它。
  // 正在跑的无会话标签不算「空」——那是在办正事，不能把它的标签抽走。
  if (isBlankTab(activeTab)) {
    attachTabLog(activeTab);
    currentSessionId = null;
    clearTodoPanel();
    withTab(activeTab, showWelcome); // 重申欢迎页（用户可能在上面写过东西）
    renderTabs();
    return activeTab;
  }
  const t = newTabObj(null, "");
  chatTabs.push(t);
  activeTab = t;
  attachTabLog(t);
  currentSessionId = null;
  clearTodoPanel();
  showWelcome();
  renderTabs();
  return t;
}

function showWelcome() {
  const host = curLog();
  host.innerHTML = `
    <div class="welcome">
      <div class="w-title"><i class="w-logo" aria-hidden="true"></i>欢迎使用 SkySheep</div>
      <div class="w-sub">一个跑在你电脑上的 AI Agent 工作台。试着给它一个完整任务，比如：</div>
      <div class="w-samples">
        <button class="w-sample" data-q="帮我在这个目录里创建一个贪吃蛇网页游戏，写完自己打开测试一下">🎮 写一个贪吃蛇网页并测试</button>
        <button class="w-sample" data-q="看看当前项目的结构，给我一份架构总结">🗂 总结当前项目结构</button>
        <button class="w-sample" data-q="把目录下所有文件按类型整理进子文件夹，并列出你做了什么">🧹 整理当前目录文件</button>
      </div>
      <div class="w-note">写文件、执行命令等敏感操作都会先征求你的确认。</div>
    </div>`;
  host.querySelectorAll(".w-sample").forEach((btn) => {
    btn.onclick = () => {
      document.getElementById("input").value = btn.dataset.q;
      send();
    };
  });
}

function addNotice(text) {
  const d = document.createElement("div");
  d.className = "msg notice" + (text.includes("\n") ? " multiline" : "");
  d.textContent = text;
  curLog().appendChild(d);
  scrollLog();
}

// 历史图片按需加载：boot/切会话只收占位（_msg_brief 不再带 base64），
// 图片滚到视口附近（提前 300px）才向後端取真身，取回前显示灰底占位
const _lazyImgs = new IntersectionObserver((ents) => {
  for (const en of ents) {
    if (!en.isIntersecting) continue;
    const el = en.target;
    _lazyImgs.unobserve(el);
    loadMsgImage(el);
  }
}, { rootMargin: "300px" });

function lazyImgObserve(img) {
  if (img instanceof Element) _lazyImgs.observe(img);
}

async function loadMsgImage(img) {
  try {
    const r = await request("session.image", {
      session_id: img.dataset.imgSid || "",
      seq: Number(img.dataset.imgSeq || 0),
      index: Number(img.dataset.imgIndex || 0),
    });
    img.src = `data:${r.media_type};base64,${r.data}`;
    img.classList.remove("img-pending");
  } catch (e) {
    img.classList.remove("img-pending");
    img.classList.add("img-missing");
    img.alt = "图片加载失败";
    img.title = String((e && e.message) || e);
  }
}

function addUser(text, images, refs) {
  const d = document.createElement("div");
  d.className = "msg user";
  // 蓝色气泡画在内层 .user-bubble 上：操作按钮行要常驻占位在气泡下方（外层不再有底色）
  const bubble = document.createElement("div");
  bubble.className = "user-bubble";
  if (text) {
    // 行首的「> 」引用块（选中回答片段引用进来）渲染成样式化引用段，其余照旧纯文本
    const lines = String(text).split("\n");
    let qEnd = 0;
    while (qEnd < lines.length && (lines[qEnd] === ">" || lines[qEnd].startsWith("> "))) qEnd++;
    if (qEnd > 0) {
      const q = document.createElement("div");
      q.className = "user-quote";
      q.textContent = lines.slice(0, qEnd).map((l) => (l === ">" ? "" : l.slice(2))).join("\n");
      bubble.appendChild(q);
      const rest = lines.slice(qEnd).join("\n").replace(/^\n+/, "");
      if (rest) {
        const body = document.createElement("div");
        body.className = "user-text";
        body.textContent = rest;
        bubble.appendChild(body);
      }
    } else {
      bubble.textContent = text;
    }
  }
  (images || []).forEach((im) => {
    const img = document.createElement("img");
    img.className = "user-image";
    img.alt = "图片附件";
    if (im.data) {
      img.src = `data:${im.media_type};base64,${im.data}`;
    } else {
      // 历史图片是占位（boot/切会话不再整包 base64，见 session.image）：
      // 进入视口前后再按需拉取真身
      const t = curTab();
      img.dataset.imgSid = String(im.session_id || (t ? t.sid : "") || "");
      img.dataset.imgSeq = String(im.seq || "");
      img.dataset.imgIndex = String(im.index || 0);
      img.classList.add("img-pending");
      lazyImgObserve(img);
    }
    img.onclick = () => { if (img.src) window.open(img.src, "_blank"); };
    bubble.appendChild(img);
  });
  if (refs && refs.length) {
    const line = document.createElement("div");
    line.className = "ref-line";
    line.title = "被引用对话的记录已随消息注入 Agent 上下文";
    line.textContent = "🔗 引用对话：" + refs.map((r) => r.title || "未命名会话").join("、");
    bubble.appendChild(line);
  }
  if (!text && images && images.length) d.classList.add("image-only");
  d.appendChild(bubble);
  curLog().appendChild(d);
  scrollLog();
}

function beginAssistant() {
  const t = curTab();
  t.streamingEl = document.createElement("div");
  t.streamingEl.className = "msg assistant";
  t.streamingEl.innerHTML = '<div class="md"><p></p></div>';
  curLog().appendChild(t.streamingEl);
  t.streamingText = "";
  t.thinkEl = null;
  t.thinkText = "";
  t.thinkMs = 0;
  t.thinkT0 = 0;
  t.thinkT1 = 0;
}

function appendStream(txt) {
  const t = curTab();
  if (!t.streamingEl) beginAssistant();
  // 正文开始 → 思考块自动折叠（思考先于正文产出）
  if (t.thinkText && t.thinkEl && !t.thinkEl.classList.contains("folded")) {
    t.thinkEl.classList.add("folded");
    const sum = t.thinkEl.querySelector(".think-sum");
    if (sum) sum.textContent = thinkSummary(t.thinkEl, true);
  }
  t.streamingText += txt;
  // 节流渲染：delta 频率远高于人眼需要，逐条对全量累积文本重跑 markdown
  // 正则 + 整树 innerHTML 是 O(n²)，长回答越吐越卡（圆桌成员流 80ms 方案，
  // 这里同样处理）；流结束由 finishAssistant 全量渲染兜底
  if (!t._streamRenderTimer) {
    t._streamRenderTimer = setTimeout(() => {
      t._streamRenderTimer = null;
      if (!t.streamingEl) return;
      t.streamingEl.firstElementChild.innerHTML = renderMarkdown(t.streamingText) + "<p>▍</p>";
      scrollLog();
    }, 80);
  }
}

// ---------- 思考过程块（思考型模型：流式灰显，正文开始后折叠，可展开回看） ----------
// 摘要文案统一走 thinkSummary：思考耗时优先用引擎实测值（历史恢复时带上），
// 流式期间用本地起止时刻估算，都没有就退到只显示字数。
function fmtThinkMs(ms) {
  if (!ms || ms < 0) return "";
  const s = ms / 1000;
  return s < 10 ? `${s.toFixed(1)} 秒` : `${Math.round(s)} 秒`;
}

function thinkSummary(el, folded) {
  // 数据存在元素自身上（el._think）而不是 tab 状态：历史恢复后 tab 状态
  // 已清空，点击展开时才能继续显示字数与耗时。
  const data = (el && el._think) || {};
  const text = data.text || "";
  const n = Math.round(text.length / 10) * 10;
  const parts = [];
  if (n) parts.push(`${n} 字`);
  const dur = fmtThinkMs(data.ms || 0);
  if (dur) parts.push(`思考 ${dur}`);
  const tail = folded ? "点击展开" : "点击折叠";
  return `💭 思考过程${parts.length ? "（" + parts.join(" · ") + "）" : ""} · ${tail}`;
}

function ensureThinkEl(t) {
  if (!t) return null;
  if (!t.thinkEl || !t.thinkEl.isConnected) {
    const el = document.createElement("div");
    el.className = "think-block";
    el.innerHTML =
      '<button type="button" class="think-sum">💭 思考中…</button>' +
      '<div class="think-body"><div class="md"></div></div>';
    el.querySelector(".think-sum").onclick = () => {
      const folded = el.classList.toggle("folded");
      const sum = el.querySelector(".think-sum");
      if (sum) sum.textContent = thinkSummary(el, folded);
    };
    const log = curLog();
    if (t.streamingEl) log.insertBefore(el, t.streamingEl);
    else log.appendChild(el);
    t.thinkEl = el;
    t.thinkText = "";
  }
  return t.thinkEl;
}

function appendThinking(txt) {
  const t = curTab();
  const el = ensureThinkEl(t);
  if (!el) return;
  // 首个思考增量记起点：思考耗时 = 首个增量 → 最后一个增量的跨度
  if (!t.thinkT0) t.thinkT0 = Date.now();
  t.thinkT1 = Date.now();
  t.thinkText += txt;
  // 元素自持一份摘要数据（见 thinkSummary）：历史恢复清空 tab 状态后仍可展开回看
  el._think = { text: t.thinkText, ms: t.thinkMs || (t.thinkT0 && t.thinkT1 ? t.thinkT1 - t.thinkT0 : 0) };
  // 节流渲染（同 appendStream）：思考文本往往比正文还长
  if (!t._thinkRenderTimer) {
    t._thinkRenderTimer = setTimeout(() => {
      t._thinkRenderTimer = null;
      if (!t.thinkEl || !t.thinkText) return;
      const body = t.thinkEl.querySelector(".think-body .md");
      if (body) body.innerHTML = renderMarkdown(t.thinkText);
      scrollLog();
    }, 80);
  }
}

function finishThinking(t) {
  if (!t || !t.thinkEl || !t.thinkText) return;
  // 补上节流攒下的最后一次渲染，再更新折叠摘要
  if (t._thinkRenderTimer) {
    clearTimeout(t._thinkRenderTimer);
    t._thinkRenderTimer = null;
    const body = t.thinkEl.querySelector(".think-body .md");
    if (body) body.innerHTML = renderMarkdown(t.thinkText);
  }
  const folded = t.thinkEl.classList.contains("folded");
  const sum = t.thinkEl.querySelector(".think-sum");
  if (sum && !folded) {
    sum.textContent = thinkSummary(t.thinkEl, false);
  }
}

// 历史恢复：按消息携带的 thinking 文本渲染折叠块
function addThinkingDone(text, tab, ms) {
  const t = tab || curTab();
  if (!text) return;
  const el = ensureThinkEl(t);
  t.thinkText = text;
  // 思考耗时由后端随消息下发（引擎实测）；旧消息没有则只显示字数
  t.thinkMs = ms || 0;
  t.thinkT0 = 0;
  t.thinkT1 = 0;
  el.querySelector(".think-body .md").innerHTML = renderMarkdown(text);
  // 历史渲染默认折叠，不占空间
  el.classList.add("folded");
  // 摘要数据挂在元素上（tab 状态马上会被清空，点击展开时还要用）
  el._think = { text: text, ms: ms || 0 };
  const sum = el.querySelector(".think-sum");
  if (sum) sum.textContent = thinkSummary(el, true);
  // 历史恢复的思考块是一次性的：随消息渲染后断开流式关联
  t.thinkEl = null;
  t.thinkText = "";
  t.thinkMs = 0;
  return el;
}

function finishAssistant(rtMeta, seq) {
  const t = curTab();
  if (!t || !t.streamingEl) return;
  finishThinking(t);
  t.thinkEl = null;
  t.thinkText = "";
  if (t._streamRenderTimer) { clearTimeout(t._streamRenderTimer); t._streamRenderTimer = null; }
  t.streamingEl.firstElementChild.innerHTML = renderMarkdown(t.streamingText);
  if (rtMeta) {
    addRtBadge(t.streamingEl, rtMeta);
    t.streamingEl._rtMeta = rtMeta; // 重新生成时据此重跑同样的圆桌配置
    // 融合结论已就位：自动折叠成员草稿卡（此前一直展开，占着大块空白）；标题栏随时可展开回看
    const rtEls = t.rtMemberEls || [];
    const allSettled = rtEls.length &&
      rtEls.every((x) => x.card.classList.contains("ok") || x.card.classList.contains("err")
        || x.card.classList.contains("skipped"));
    if (rtMeta.members && allSettled && t.rtCard && !t.rtCard.classList.contains("folded")) {
      t.rtCard.classList.add("folded");
      const foldBtn = t.rtCard.querySelector(".rt-fold");
      if (foldBtn) foldBtn.textContent = "展开";
      const sub = t.rtCard.querySelector(".rt-sub");
      if (sub) sub.textContent = `${rtEls.length} 个模型 · 已折叠 · 点击标题栏展开查看各成员草稿`;
    }
  }
  t.lastAssistantText = t.streamingText;
  const finalEl = t.streamingEl;
  t.streamingEl = null;
  t.streamingText = "";
  if (seq) finalEl.dataset.seq = String(seq);
  renderMermaidIn(finalEl);
  highlightCodeIn(finalEl);
  decorateFinalMessage(finalEl, t, () => t.lastAssistantText, seq);
}

// 圆桌融合徽标：标记这条最终回答由多模型共同思考得出
function addRtBadge(el, meta) {
  const members = meta.members || [];
  const okCount = members.filter((m) => m.status === "done").length;
  const badge = document.createElement("div");
  badge.className = "rt-badge";
  badge.title = members
    .map((m) => {
      const tk = (m.input_tokens || 0) + (m.output_tokens || 0);
      return `${m.provider}/${m.model}：${m.status === "done" ? "已参与" : "失败"}`
        + (tk ? `（≈${fmtTokens(tk)} tokens）` : "");
    })
    .join("\n");
  if (meta.mode === "compare" && members.length === 1) {
    // A/B 对比（含融合失败降级）：每条回答只属于一个成员
    const m = members[0];
    badge.textContent = meta.degraded
      ? `◆ 融合失败 · 保留 ${m.provider}/${m.model} 的草稿`
      : `◆ A/B 对比 · ${m.provider}/${m.model}`;
  } else {
    const rounds = meta.rounds || 1;
    badge.textContent = `◆ 圆桌融合 · ${okCount} 个成员 + 主席`
      + (rounds > 1 ? ` · 辩论 ${rounds - 1} 轮` : "");
  }
  el.prepend(badge);
}

// 用量数字缩写：1200 → 1.2k
function fmtTokens(n) {
  if (!n) return "0";
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
}

// 引用成员草稿追问：把草稿以引用块形式填进输入框（截断防超长）
function quoteRoundtableDraft(provider, model, text) {
  const raw = (text || "").trim();
  if (!raw) { addNotice("这份草稿还没有内容"); return; }
  const clipped = raw.length > 1500 ? raw.slice(0, 1500) + "\n…（草稿过长，已截断）" : raw;
  const quoted = clipped.split("\n").map((line) => "> " + line).join("\n");
  const input = document.getElementById("input");
  const existing = input.value.trim();
  input.value = `【引用圆桌成员 ${provider}/${model} 的草稿】\n${quoted}\n\n我的追问：`
    + (existing ? "\n" + existing : "");
  autoGrowInput();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

// 一批成员卡片的公共 DOM：head + grid；返回 {card, grid, sub}
function buildRtCardShell(members, subText) {
  const card = document.createElement("div");
  card.className = "roundtable";
  const head = document.createElement("div");
  head.className = "rt-head";
  const sub = document.createElement("span");
  sub.className = "rt-sub";
  sub.textContent = subText;
  const title = document.createElement("span");
  title.className = "rt-title";
  title.textContent = "👥 圆桌讨论";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "折叠";
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    sub.textContent = folded
      ? `${members.length} 个模型 · 已折叠 · 点击展开查看各成员草稿`
      : subText;
  };
  fold.onclick = (e) => { e.stopPropagation(); toggleFold(); };
  head.onclick = (e) => { if (e.target !== fold) toggleFold(); };
  head.append(title, sub, fold);
  const grid = document.createElement("div");
  grid.className = "rt-grid";
  // 按成员数定列数：1/2/3 各成一列排一行，4 及以上 2×2——auto-fit 会排出 3+1 的孤行，视觉很乱
  grid.style.setProperty("--rt-cols", members.length >= 4 ? 2 : Math.max(1, members.length));
  card.append(head, grid);
  return { card, grid, sub };
}

// 成员卡的公共 DOM：head + body + foot（用量 / 引用追问）
// 返回 {el, mcard, statusEl, roundEl, tokensEl, footEl, bodyEl, quoteBtn}
function buildRtMemberCard(m) {
  const mcard = document.createElement("div");
  mcard.className = "rt-member";
  mcard.innerHTML =
    `<div class="rt-m-head"><span class="rt-m-name">${escapeHtml(m.provider)}</span>` +
    `<span class="rt-m-model">${escapeHtml(m.model)}</span>` +
    `<span class="rt-m-role"${m.role ? "" : " hidden"}>${m.role ? escapeHtml(rtRoleLabel(m.role)) : ""}</span>` +
    '<span class="rt-m-round" hidden></span>' +
    '<span class="rt-m-status">⋯</span></div>' +
    '<div class="rt-m-body"><div class="md"></div></div>' +
    '<div class="rt-m-foot" hidden><span class="rt-m-tokens"></span>' +
    '<button class="rt-m-quote" type="button" title="把这份草稿引用进输入框，继续追问">引用追问</button></div>';
  return {
    card: mcard,
    status: mcard.querySelector(".rt-m-status"),
    roundEl: mcard.querySelector(".rt-m-round"),
    tokensEl: mcard.querySelector(".rt-m-tokens"),
    foot: mcard.querySelector(".rt-m-foot"),
    body: mcard.querySelector(".rt-m-body .md"),
    quoteBtn: mcard.querySelector(".rt-m-quote"),
  };
}

// ---------- 圆桌卡片：成员草稿并列展示 + 融合进度（状态挂标签） ----------
function beginRoundtable(members, rounds) {
  const t = curTab();
  const total = rounds || 1;
  t.rtRounds = total;
  const { card, grid, sub } = buildRtCardShell(
    members,
    `${members.length} 个模型正在并行思考${total > 1 ? `（共 ${total} 轮）` : ""}…`
  );
  t.rtCard = card;
  t.rtMemberEls = members.map((m) => {
    const u = buildRtMemberCard(m);
    grid.appendChild(u.card);
    u.text = "";
    u.round = 0;
    u.finishedRound = -1;
    u.tokens = 0;
    u.quoteBtn.onclick = (e) => {
      e.stopPropagation();
      quoteRoundtableDraft(m.provider, m.model, u.text);
    };
    return u;
  });
  curLog().appendChild(card);
  scrollLog();
}

// 成员轮标记（多轮辩论时显示「第 N 轮」/「已收敛」）
function setMemberRound(el, t, r, label) {
  if ((t.rtRounds || 1) <= 1) return;
  el.roundEl.textContent = label || `第 ${r + 1} 轮`;
  el.roundEl.hidden = false;
}

function rtMemberDelta(data) {
  const t = curTab();
  const m = t.rtMemberEls && t.rtMemberEls[data.member_index];
  if (!m) return;
  const r = data.round || 0;
  if (r > m.round) {
    // 进入新一轮修订：旧草稿清空，整段换成修订版（不被上一轮文本拼接污染）
    m.round = r;
    m.text = "";
    m.card.classList.remove("ok", "err", "skipped");
    m.status.textContent = "⋯";
    setMemberRound(m, t, r, r > 0 ? `第 ${r + 1} 轮修订` : `第 ${r + 1} 轮`);
    m.body.innerHTML = "";
  } else if (r === 0 && (t.rtRounds || 1) > 1) {
    setMemberRound(m, t, 0);
  }
  m.text += data.text || "";
  // 节流渲染：多成员并行流式时逐 delta 全量重渲 markdown 很吃性能（长草稿卡顿）
  if (!m._renderTimer) {
    m._renderTimer = setTimeout(() => {
      m._renderTimer = null;
      if (m.text) m.body.innerHTML = renderMarkdown(m.text);
      scrollLog();
    }, 80);
  }
}

function rtMemberFinished(data) {
  const t = curTab();
  const m = t.rtMemberEls && t.rtMemberEls[data.member_index];
  if (!m || !t.rtCard) return;
  if (m._renderTimer) { clearTimeout(m._renderTimer); m._renderTimer = null; }
  const r = data.round || 0;
  if (r > m.round) {
    // 防御：没收到（或没来得及收）delta 就直接来了结束事件
    m.round = r;
    m.text = "";
    m.card.classList.remove("ok", "err", "skipped");
    m.body.innerHTML = "";
  }
  m.finishedRound = r;
  if (data.skipped) {
    // 已收敛：上一轮修订没有改动，本轮跳过
    m.card.classList.add("skipped");
    m.card.classList.remove("ok", "err");
    m.status.textContent = "≡";
    setMemberRound(m, t, r, "已收敛");
  } else if (data.status === "error") {
    m.card.classList.add("err");
    m.card.classList.remove("ok", "skipped");
    m.status.textContent = "✗";
    if (!m.text) m.body.innerHTML = `<p class="dim">✗ ${escapeHtml(data.error || "作答失败")}</p>`;
  } else {
    m.card.classList.add("ok");
    m.card.classList.remove("err", "skipped");
    m.status.textContent = "✓";
    // 成员草稿收尾：重渲一遍并高亮代码块（流式期间的最后一次 renderMarkdown 留下的是原文）
    m.body.innerHTML = renderMarkdown(m.text);
    renderMermaidIn(m.body);
    highlightCodeIn(m.body);
  }
  // 用量累计显示 + 引用按钮（有草稿才能引用）
  m.tokens += (data.output_tokens || 0) + (data.input_tokens || 0);
  if (m.tokens > 0) m.tokensEl.textContent = `≈${fmtTokens(m.tokens)} tokens`;
  if (m.text.trim()) m.foot.hidden = false;
  // 按轮结算：本轮所有成员的结束事件到齐（含 skipped）→ 更新进度文案
  const total = t.rtRounds || 1;
  const allNow = t.rtMemberEls.every((x) => x.finishedRound >= r);
  if (allNow) {
    const sub = t.rtCard.querySelector(".rt-sub");
    if (sub) {
      sub.textContent = (r + 1 < total)
        ? `第 ${r + 1} / ${total} 轮完成，进入修订…`
        : "草稿完成，主席融合中…";
    }
  }
}

// 历史回放：从持久化的元数据重建圆桌卡片（默认折叠，可展开回看草稿）
function buildRtReplayCard(meta) {
  const members = meta.members || [];
  const { card, grid, sub } = buildRtCardShell(
    members, `${members.length} 个模型 · 已折叠 · 点击展开查看各成员草稿`
  );
  card.classList.add("folded");
  card.querySelector(".rt-fold").textContent = "展开";
  const rounds = meta.rounds || 1;
  if (rounds > 1) sub.textContent += ` · 辩论 ${rounds - 1} 轮`;
  members.forEach((m) => {
    const u = buildRtMemberCard(m);
    const draft = m.draft || "";
    u.card.classList.add(m.status === "done" ? "ok" : "err");
    u.status.textContent = m.status === "done" ? "✓" : "✗";
    const tk = (m.input_tokens || 0) + (m.output_tokens || 0);
    if (tk) u.tokensEl.textContent = `≈${fmtTokens(tk)} tokens`;
    if (draft.trim()) {
      u.body.innerHTML = renderMarkdown(draft);
      u.foot.hidden = false;
      u.quoteBtn.onclick = (e) => {
        e.stopPropagation();
        quoteRoundtableDraft(m.provider, m.model, draft);
      };
    } else {
      u.body.innerHTML = `<p class="dim">✗ ${escapeHtml(m.error || "作答失败")}</p>`;
    }
    grid.appendChild(u.card);
  });
  return card;
}

let curSpawnCard = null; // 运行中的 spawn_agent 卡片：接收 subagent_event 直播

function addToolCard(data) {
  finishAssistant();
  const card = document.createElement("div");
  card.className = "tool-card";
  card.dataset.callId = data.tool_call_id;
  card._toolName = data.name;
  card._toolInput = data.input || {};
  card.innerHTML = `
    <div class="t-head">
      <span class="t-name">▶ ${escapeHtml(data.name)}</span>
      <span class="t-args">${escapeHtml(oneLine(data.input))}</span>
      <span class="t-status">⋯</span>
    </div>
    <div class="t-body"><pre></pre></div>`;
  card.querySelector(".t-head").onclick = () => card.classList.toggle("open");
  // 子代理派生：自动展开卡片，直播内容进来直接可见
  if (data.name === "spawn_agent") {
    curSpawnCard = card;
    card._spawnTaskId = "";
    card.classList.add("open");
  }
  // 登记进在途表：finishToolCard 直接 O(1) 取，不在整个聊天流里
  // 属性选择器扫描（一轮几十上百个工具调用时是 O(n²)）
  const t = curTab();
  if (!t._toolCards) t._toolCards = new Map();
  t._toolCards.set(data.tool_call_id, card);
  curLog().appendChild(card);
  scrollLog();
}

function finishToolCard(data) {
  finishAssistant();
  const t = curTab();
  let card = t._toolCards ? t._toolCards.get(data.tool_call_id) : null;
  if (card) t._toolCards.delete(data.tool_call_id); // 完成即出表，不持已分离节点
  if (!card || !card.isConnected) {
    // 兜底：历史渲染/重渲染路径的卡片没登记过，退回选择器查找
    card = curLog().querySelector(`[data-call-id="${data.tool_call_id}"]`);
  }
  if (!card) return;
  if (card === curSpawnCard) curSpawnCard = null;
  card.classList.add(data.is_error ? "err" : "ok");
  card.querySelector(".t-status").textContent = data.is_error ? "✗" : `✓ ${data.duration_ms}ms`;
  // spawn_agent 卡片已直播过报告流（.sub-report 有内容）时不再重复贴预览
  const subRep = card._toolName === "spawn_agent" ? card.querySelector(".sub-report") : null;
  if (subRep && subRep.textContent.trim()) {
    card.querySelector(".t-body pre").style.display = "none";
  } else {
    card.querySelector(".t-body pre").textContent =
      (data.is_error ? "[错误] " : "") + (data.preview || "(无输出)");
  }
  // 写出的 HTML 页面 / 生成的图片：给「预览」按钮，在浏览器标签里直接看效果
  const tname = card._toolName || "";
  const tpath = String((card._toolInput || {}).path || "");
  const written = !data.is_error && tpath && (tname === "write_file" || tname === "generate_image");
  // Agent 落了文件：文件树缓存失效，防抖后刷新（连续写多个文件只在最后一次刷新），
  // 用户切过去就能看到 Agent 刚写的文件，不用再手动点「刷新」
  if (!data.is_error && (tname === "write_file" || tname === "edit_file")) {
    scheduleFilesRefresh();
  }
  if (written && /\.(html?|png|jpe?g|webp|svg)$/i.test(tpath)) {
    const btn = document.createElement("button");
    btn.className = "preview-btn";
    btn.textContent = "👁 在浏览器面板预览";
    btn.onclick = () => openHtmlPreview(tpath);
    card.querySelector(".t-body").appendChild(btn);
  }
  if (data.diff) {
    const pre = document.createElement("pre");
    pre.className = "tool-diff";
    pre.innerHTML = data.diff
      .split("\n")
      .map((l) => {
        const cls = l.startsWith("+") && !l.startsWith("+++") ? "add"
          : l.startsWith("-") && !l.startsWith("---") ? "del"
          : l.startsWith("@@") ? "meta" : "";
        return `<span class="${cls}">${escapeHtml(l) || " "}</span>`;
      })
      .join("\n");
    card.querySelector(".t-body").appendChild(pre);
    card.classList.add("open"); // 有文件变更时自动展开 diff
  }
  // 截屏类工具附带的图片（screenshot）：内联展示，点击放大/还原
  if (Array.isArray(data.images) && data.images.length && !data.is_error) {
    card.classList.add("open");
    for (const img of data.images) {
      const el = document.createElement("img");
      el.className = "tool-image";
      el.alt = "工具截图";
      el.src = `data:${img.media_type || "image/png"};base64,${img.data}`;
      el.onclick = () => el.classList.toggle("zoom");
      card.querySelector(".t-body").appendChild(el);
    }
  }
}

function oneLine(obj) {
  const s = Object.entries(obj || {})
    .map(([k, v]) => {
      const val = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);
      return `${k}=${val}`;
    })
    .join(" ");
  return s.length > 90 ? s.slice(0, 90) + "…" : s;
}

// ---------- 事件处理 ----------
let running = false; // 活动标签运行态镜像（顶栏按钮用；真值在 activeTab.running）
let permRequest = null;
let usageIn = 0, usageOut = 0;

function renderUsage() {
  if (usageIn || usageOut) {
    const el = document.getElementById("usage");
    el.textContent =
      `tokens ↑${usageIn.toLocaleString()} ↓${usageOut.toLocaleString()}`;
    el.title =
      `本会话累计消耗 tokens：↑ 输入 ${usageIn.toLocaleString()} / ↓ 输出 ${usageOut.toLocaleString()}（全部会话的统计见 设置 · 用量）`;
  }
}

function setContextUsage(tokens, limit, tab) {
  const t = tab || activeTab;
  if (t) t.usage = limit ? { tokens, limit } : null;
  if (t && t !== activeTab) return; // 后台标签只记数，不改输入栏
  const ring = document.getElementById("ctx-ring");
  if (!limit) {
    // 延迟 300ms 再隐藏：切项目/新会话会先清零、紧跟着 chat.status 又填回真实值，
    // 立藏立显会让环仪表闪一下；真要隐藏的场景晚 300ms 无感
    if (!ctxRingHideTimer) {
      ctxRingHideTimer = setTimeout(() => {
        ctxRingHideTimer = null;
        ring.classList.add("hidden");
      }, 300);
    }
    return;
  }
  clearTimeout(ctxRingHideTimer);
  ctxRingHideTimer = null;
  ring.classList.remove("hidden");
  const pct = Math.min(100, Math.round((100 * tokens) / limit));
  const C = 2 * Math.PI * 9; // 环半径 r=9（viewBox 24）
  ring.querySelector(".ring-val").style.strokeDashoffset = String(C * (1 - pct / 100));
  ring.querySelector(".ring-txt").textContent = String(pct);
  ring.classList.toggle("warn", pct >= 70 && pct < 90);
  ring.classList.toggle("bad", pct >= 90);
}
let ctxRingHideTimer = null;

// ---------- 上下文容量详情弹层（环形仪表悬停/点按展开） ----------
// 数据来自 chat.send / chat.status / chat.compact 响应携带的 context_detail
//（后端分桶估算 + 会话累计平均缓存命中率），随标签存取、悬停即弹。
const ctxRingEl = document.getElementById("ctx-ring");
const ctxPop = document.getElementById("ctx-pop");
const CTX_DOT_COLORS = {
  "消息": "var(--blue)", "系统工具": "var(--ai)", "技能": "var(--gold)",
  "系统提示词": "var(--down)", "MCP 工具": "#0f7f78", "其他": "var(--dim)",
};
// 1 万以上按「万」折算（对标 CLI 风格：20.4万 / 100万），其余千分位
function fmtWan(n) {
  n = Math.max(0, Math.round(n || 0));
  if (n >= 10000) return parseFloat((n / 10000).toFixed(1)) + "万";
  return n.toLocaleString();
}
function fmtCtxPct(p) {
  p = Number(p) || 0;
  if (p >= 1) return Math.round(p) + "%";
  if (p > 0) return p.toFixed(1) + "%";
  return "0%";
}
function renderCtxPop() {
  const d = activeTab && activeTab.ctxDetail;
  if (!d) return false;
  const pct = d.limit ? Math.min(100, (100 * d.tokens) / d.limit) : 0;
  const barCls = pct >= 90 ? " bad" : pct >= 70 ? " warn" : "";
  const rows = (d.rows || []).map((r) =>
    `<div class="ctx-row">` +
    `<i class="ctx-dot" style="background:${CTX_DOT_COLORS[r.label] || "var(--dim)"}"></i>` +
    `<span class="ctx-label">${escapeHtml(r.label)}</span>` +
    `<span class="ctx-val">${fmtCtxPct(r.pct)}</span></div>`
  ).join("");
  ctxPop.innerHTML =
    `<div class="ctx-head"><span class="ctx-cap">上下文容量</span>` +
    `<span class="ctx-nums">${fmtWan(d.tokens)}/${fmtWan(d.limit)}（${parseFloat(pct.toFixed(1))}%）</span></div>` +
    `<div class="ctx-bar"><i class="ctx-bar-fill${barCls}" style="width:${pct}%"></i></div>` +
    rows +
    `<div class="ctx-foot"><span>平均缓存命中率</span>` +
    `<span class="ctx-val">${d.cache_rate == null ? "—" : d.cache_rate + "%"}</span></div>`;
  return true;
}
function placeCtxPop() {
  const r = ctxRingEl.getBoundingClientRect();
  if (!r.width && !r.height) {
    // 环此刻不可见（启动读数未到/切标签清零中）：无处可贴，收起弹层
    ctxPop.classList.add("hidden");
    ctxPopOpen = false;
    return false;
  }
  const w = ctxPop.offsetWidth;
  let left = r.left + r.width / 2 - w / 2;
  left = Math.max(10, Math.min(left, window.innerWidth - w - 10));
  ctxPop.style.left = Math.round(left) + "px";
  ctxPop.style.bottom = Math.round(window.innerHeight - r.top + 8) + "px";
  // 页面被整体缩放时（WebView2 DPI / 宿主 fit），rect 是视觉像素而 style 是布局
  // 像素，直接赋值会偏移。量一次实际渲染位置，按比例换算差值校正一次即收敛。
  const pr = ctxPop.getBoundingClientRect();
  const k = (ctxPop.offsetWidth && pr.width) ? pr.width / ctxPop.offsetWidth : 1;
  if (k > 0 && Math.abs(k - 1) > 0.01) {
    const dx = (r.left + r.width / 2) - (pr.left + pr.width / 2);
    const needUp = pr.bottom - (r.top - 8);
    if (Math.abs(dx) > 1 || Math.abs(needUp) > 1) {
      ctxPop.style.left = Math.round(parseFloat(ctxPop.style.left) + dx / k) + "px";
      ctxPop.style.bottom = Math.round(parseFloat(ctxPop.style.bottom) + needUp / k) + "px";
    }
  }
  return true;
}
let ctxPopHideTimer = null;
let ctxPopOpen = false;
function openCtxPop() {
  clearTimeout(ctxPopHideTimer);
  ctxPopOpen = true;
  if (renderCtxPop()) { ctxPop.classList.remove("hidden"); placeCtxPop(); return; }
  // 还没有明细数据（本轮尚未发过消息也没拉过状态）：拉一次，回来时仍悬停着就补弹
  request("chat.status").then((st) => {
    if (st && st.context_detail) {
      setContextDetail(st.context_detail, activeTab);
      if (ctxPopOpen && renderCtxPop()) { ctxPop.classList.remove("hidden"); placeCtxPop(); }
    }
  }).catch(() => {});
}
function closeCtxPop(now = false) {
  clearTimeout(ctxPopHideTimer);
  if (now) { ctxPopOpen = false; ctxPop.classList.add("hidden"); return; }
  ctxPopHideTimer = setTimeout(() => { ctxPopOpen = false; ctxPop.classList.add("hidden"); }, 150);
}
if (ctxRingEl && ctxPop) {
  ctxRingEl.addEventListener("mouseenter", openCtxPop);
  ctxRingEl.addEventListener("mouseleave", () => closeCtxPop());
  // 点按切换：触屏 / 手机遥控端没有 hover，点环即开、再点或点别处即收
  ctxRingEl.addEventListener("click", () => {
    if (ctxPop.classList.contains("hidden")) openCtxPop(); else closeCtxPop(true);
  });
  ctxPop.addEventListener("mouseenter", () => clearTimeout(ctxPopHideTimer));
  ctxPop.addEventListener("mouseleave", () => closeCtxPop());
  window.addEventListener("resize", () => closeCtxPop(true));
  document.addEventListener("click", (e) => {
    if (ctxPopOpen && !ctxPop.contains(e.target) && !ctxRingEl.contains(e.target)) closeCtxPop(true);
  });
}
function setContextDetail(detail, tab) {
  const t = tab || activeTab;
  if (!t || !detail) return;
  t.ctxDetail = detail;
  if (t === activeTab && ctxPopOpen && renderCtxPop()) placeCtxPop();
}

// 任务清单面板（todo_write 工具驱动；渲染进右侧面板「任务清单」标签）
function renderTodoPanel(items) {
  const ul = document.getElementById("todo-list");
  const empty = document.getElementById("todo-empty");
  const has = items && items.length;
  if (empty) empty.classList.toggle("hidden", !!has);
  ul.innerHTML = "";
  if (!has) return;
  items.forEach((t) => {
    const li = document.createElement("li");
    const icon = { pending: "□", in_progress: "▶", completed: "✓" }[t.status] || "□";
    li.innerHTML = `<span class="todo-ico ${t.status}">${icon}</span>
      <span class="todo-txt ${t.status}">${escapeHtml(t.content)}</span>`;
    li.title = { pending: "待办", in_progress: "进行中", completed: "已完成" }[t.status] || "";
    ul.appendChild(li);
  });
}
function clearTodoPanel() {
  document.getElementById("todo-list").innerHTML = "";
  const empty = document.getElementById("todo-empty");
  if (empty) empty.classList.remove("hidden");
}

// ---------- 项目任务（右侧「项目任务」页签；与项目绑定，删项目一并清掉） ----------

let ptasksLoadedFor = null; // 记上次加载的项目名，切项目后强制重取

async function loadProjectTasks(force) {
  const ul = document.getElementById("ptask-list");
  if (!ul) return;
  const projName = currentProjectName();
  if (!force && ptasksLoadedFor === projName) return; // 同一项目不反复拉
  ptasksLoadedFor = projName;
  const projLabel = document.getElementById("ptasks-project");
  if (projLabel) projLabel.textContent = projName || "当前项目";
  let tasks = [];
  try {
    const r = await request("project.task_list", {});
    tasks = r.tasks || [];
  } catch (e) {
    ul.innerHTML = `<li class="empty-hint">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  renderProjectTasks(tasks);
}

function renderProjectTasks(tasks) {
  const ul = document.getElementById("ptask-list");
  const empty = document.getElementById("ptask-empty");
  if (!ul) return;
  const has = tasks && tasks.length;
  if (empty) empty.classList.toggle("hidden", !!has);
  ul.innerHTML = "";
  tasks.forEach((t) => {
    const li = document.createElement("li");
    li.className = "ptask-row" + (t.done ? " done" : "");
    li.innerHTML = `
      <input type="checkbox" class="ptask-check" ${t.done ? "checked" : ""} title="${t.done ? "重新打开" : "标记完成"}">
      <span class="ptask-main">
        <span class="ptask-title">${escapeHtml(t.title)}</span>
        ${t.detail ? `<span class="ptask-detail">${escapeHtml(t.detail)}</span>` : ""}
      </span>
      <button class="ptask-del" title="删除这条任务">✕</button>`;
    li.querySelector(".ptask-check").onchange = async (e) => {
      try {
        await request("project.task_update", { id: t.id, done: e.target.checked });
        loadProjectTasks(true);
      } catch (err) {
        e.target.checked = !e.target.checked;
        addNotice("更新任务失败：" + err.message);
      }
    };
    li.querySelector(".ptask-del").onclick = async () => {
      try {
        await request("project.task_delete", { id: t.id });
        loadProjectTasks(true);
      } catch (err) {
        addNotice("删除任务失败：" + err.message);
      }
    };
    li.querySelector(".ptask-main").onclick = () => ptaskModal(t);
    ul.appendChild(li);
  });
}

function ptaskModal(t) {
  const isNew = !t;
  const box = document.createElement("div");
  box.innerHTML = `
    <div class="form-grid">
      <label class="wide">要做什么
        <input data-f="title" type="text" maxlength="200" value="${t ? escapeHtml(t.title) : ""}"
               placeholder="一句话说清要做什么">
      </label>
      <label class="wide">完成标准 / 详细要求（可选）
        <textarea data-f="detail" rows="3" maxlength="2000"
                  placeholder="做到什么程度算完成；有什么约束或偏好">${t ? escapeHtml(t.detail || "") : ""}</textarea>
      </label>
    </div>`;
  showModal(isNew ? "新建任务" : "编辑任务", box, async () => {
    const title = box.querySelector('[data-f="title"]').value.trim();
    if (!title) throw new Error("任务内容不能为空");
    const detail = box.querySelector('[data-f="detail"]').value;
    if (isNew) {
      await request("project.task_add", { title, detail });
    } else {
      await request("project.task_update", { id: t.id, title, detail });
    }
    loadProjectTasks(true);
  }, isNew ? "添加" : "保存");
}

function wireProjectTasksPanel() {
  const input = document.getElementById("ptask-input");
  const addBtn = document.getElementById("btn-ptask-add");
  if (!input || input.dataset.wired) return;
  input.dataset.wired = "1";
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const title = input.value.trim();
    if (!title) return;
    input.disabled = true;
    request("project.task_add", { title })
      .then(() => { input.value = ""; loadProjectTasks(true); })
      .catch((err) => addNotice("添加任务失败：" + err.message))
      .finally(() => { input.disabled = false; });
  });
  if (addBtn && !addBtn.dataset.wired) {
    addBtn.dataset.wired = "1";
    addBtn.onclick = () => ptaskModal(null);
  }
}

function setRunning(on, tab) {
  const t = tab || activeTab;
  if (t) t.running = on;
  if (t && t !== activeTab) { renderTabs(); return; } // 后台标签只更新徽标
  running = on;
  // 排队机制下发送永远可用：运行中点击 = 排队，按钮文案如实反馈
  const sendBtn = document.getElementById("btn-send");
  sendBtn.disabled = false;
  sendBtn.textContent = on ? "排队" : "发送";
  document.getElementById("btn-stop").hidden = !on;
  renderTabs();
  // 宠物：开始干活换蹦跶动画；一轮跑完撒个花
  petRefresh();
  if (petPrevRunning && !on) petCelebrate();
  petPrevRunning = on;
}

let pendingTurns = 0; // 兼容保留：排队提示用

// ---------- 任务耗时预估：接手任务时「预计 X~Y 分钟 · 已用时」对照条 ----------
// 后端每轮开工先推 task_estimate（启发式 + 本项目历史实测校准）；芯片先于
// 回复出现在日志里，运行中每秒对照已用时，结束时定格成一条弱化的用时记录。
let etaTimer = null;

function fmtEtaDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " 秒";
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return sec ? `${m} 分 ${sec} 秒` : `${m} 分钟`;
  const h = Math.floor(m / 60);
  return (m % 60) ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
}

function fmtEtaRange(lo, hi) {
  if (hi < 60) return lo === hi ? `${lo} 秒` : `${lo}~${hi} 秒`;
  if (hi < 3600) {
    const a = Math.max(1, Math.round(lo / 60)), b = Math.max(1, Math.round(hi / 60));
    return a === b ? `${a} 分钟` : `${a}~${b} 分钟`;
  }
  const a = Math.max(1, Math.round(lo / 3600)), b = Math.max(1, Math.round(hi / 3600));
  return a === b ? `${a} 小时` : `${a}~${b} 小时`;
}

function showTaskEstimate(data) {
  const t = curTab();
  if (!t) return;
  const el = document.createElement("div");
  el.className = "eta-chip";
  if (data.basis) el.title = "预估依据：" + data.basis;
  t.etaEl = el; // 旧芯片留在原地，作为上一轮的用时记录
  t.eta = { lo: data.min_seconds || 0, hi: data.max_seconds || 0, start: Date.now(), done: false, actual: 0 };
  t.logEl.appendChild(el);
  updateEtaChip(t);
  scrollLog();
  if (!etaTimer) etaTimer = setInterval(tickEtaChips, 1000);
}

function updateEtaChip(t) {
  if (!t || !t.eta || !t.etaEl || !t.etaEl.isConnected) return;
  const e = t.eta, el = t.etaEl;
  if (e.done) {
    el.classList.remove("over");
    el.classList.add("done");
    el.textContent = `⏱ 用时 ${fmtEtaDur(e.actual)} · 预估 ${fmtEtaRange(e.lo, e.hi)}`;
    return;
  }
  const gone = (Date.now() - e.start) / 1000;
  if (e.hi && gone > e.hi) {
    el.classList.add("over");
    el.textContent = `⏱ 已 ${fmtEtaDur(gone)} · 超出预估的 ${fmtEtaRange(e.lo, e.hi)}`;
  } else {
    el.textContent = `⏱ 预计 ${fmtEtaRange(e.lo, e.hi)} · 已 ${fmtEtaDur(gone)}`;
  }
}

function tickEtaChips() {
  let live = false;
  for (const t of chatTabs) {
    if (t.eta && !t.eta.done) { updateEtaChip(t); live = true; }
  }
  if (!live && etaTimer) { clearInterval(etaTimer); etaTimer = null; }
}

function finishEta(t, engineMs) {
  if (!t || !t.eta || t.eta.done) return;
  t.eta.done = true;
  // 优先用引擎实测值（与落库后历史恢复显示的完全一致）；
  // 没有则退到前端本地计时（旧引擎 / 中断路径）
  t.eta.actual = engineMs ? engineMs / 1000 : (Date.now() - t.eta.start) / 1000;
  updateEtaChip(t);
}

function handleEvent(kind, data) {
  // 多会话路由：事件带 session_id → 找到（或后台创建）对应标签再渲染
  if (data && data.session_id) {
    routeTab = tabFor(data.session_id);
    if (!routeTab) {
      // 无绑定会话的运行中标签：说明这是它懒创建的会话，直接认领，避免重复建标签
      const adopt = chatTabs.find((t) => !t.sid && t.running);
      if (adopt) {
        adopt.sid = data.session_id;
        currentSessionId = data.session_id;
        routeTab = adopt;
        renderTabs();
      } else {
        routeTab = openTabForSession(data.session_id, "", { background: true });
        refreshSessions();
      }
    }
  } else {
    routeTab = activeTab;
  }
  switch (kind) {
    case "thinking_delta": appendThinking(data.text); break;
    case "text_delta": appendStream(data.text); break;
    case "assistant_message": {
      // 有流式元素（普通轮/主席融合）→ 收尾；无流式元素（对比模式的成员回答）→ 直接渲染完整气泡
      const t = curTab();
      const meta = data.message && data.message.roundtable;
      const seq = data.message && data.message.seq;
      if (t && t.streamingEl) {
        finishAssistant(meta, seq);
      } else {
        const text = messageText(data.message);
        if (text) addAssistantDone(text, meta, t, seq);
      }
      break;
    }
    case "roundtable_started": beginRoundtable(data.members || [], data.rounds || 1); break;
    case "roundtable_member_delta": rtMemberDelta(data); break;
    case "roundtable_member_finished": rtMemberFinished(data); break;
    case "tool_call_started": addToolCard(data); break;
    case "tool_call_finished": finishToolCard(data); break;
    case "subagent_spawned": onSubagentSpawned(data); break;
    case "subagent_event": onSubagentEvent(data); break;
    case "task_finished": onTaskFinished(data); break;
    case "permission_request":
      showPermission(data); // 通知统一在 showPermission 里发（活动/后台各一份）
      break;
    case "permission_resolved":
      hidePermission();
      if (routeTab) {
        routeTab.needsPerm = false;
        routeTab.permData = null;
        renderTabs();
      }
      break;
    case "usage":
      usageIn += data.input_tokens || 0;
      usageOut += data.output_tokens || 0;
      renderUsage();
      break;
    case "todo_updated": renderTodoPanel(data.items); break;
    case "task_estimate": showTaskEstimate(data); break;
    case "schedule_updated": {
      if (rightTabs.includes("agenda") && !rightCollapsed) loadAgenda();
      break;
    }
    case "schedule_reminder":
      pushNotice("⏰ 日程提醒", data.title || "");
      showAgendaReminder(data);
    case "session_updated": {
      if (data.session_id) {
        sessionMeta[data.session_id] = { title: data.title || "" };
        const t = tabFor(data.session_id);
        if (t) { t.title = data.title || t.title; renderTabs(); }
        refreshSessions();
      }
      break;
    }
    case "user_message": {
      // 其他窗口 / 手机端发来的用户消息：轮次事件只发往发起连接，本连接
      // 不会经过 send() 的本地渲染路径，没有这条气泡就会「只见回复不见人话」。
      // 服务端已按连接排除发送方，这里不会与本地气泡重复。
      const t = tabFor(data.session_id);
      if (t && (data.text || (data.images || []).length)) {
        withTab(t, () => addUser(data.text || "", data.images));
        const el = t.logEl.lastElementChild;
        if (el) attachMsgOps(el, t, "user", () => data.text || "");
        if (t === activeTab) scrollLog();
      }
      break;
    }
    case "cron_updated": {
      if (data.task) {
        pushNotice(`⏰ ${data.task.name}`,
          data.task.last_status === "error" ? "运行失败：" + (data.task.last_result || "") :
          data.task.last_result || "已更新");
      }
      if (rightTabs.includes("cron") && !rightCollapsed) {
        loadCron();
      }
      break;
    }
    case "pipeline_updated": {
      if (data.pipeline) {
        const p = data.pipeline;
        const prev = pipelineSeenStatus[p.id];
        pipelineSeenStatus[p.id] = p.status;
        if (prev === "running" && (p.status === "done" || p.status === "failed")) {
          const bad = (p.nodes || []).filter((n) => n.status !== "done" && n.status !== "skipped").length;
          const skipped = (p.nodes || []).filter((n) => n.status === "skipped").length;
          const skippedTxt = skipped ? `，${skipped} 个被条件门跳过` : "";
          pushNotice(p.status === "done" ? `✅ 流水线完成：${p.name}${skippedTxt}`
            : `⚠️ 流水线结束（${bad} 个节点未成功${skippedTxt}）：${p.name}`);
        }
        if (rightTabs.includes("pipeline") && !rightCollapsed) loadPipelines();
      }
      break;
    }
    case "notice": addNotice("⏳ " + data.message); break;
    case "memory_digest": addNotice("🧠 " + data.message); break;
    case "memory_maintain": addNotice("🧹 " + data.message); break;
    case "compaction":
      addNotice(`🗜 上下文压缩：${data.before_messages} → ${data.after_messages} 条消息`);
      break;
    case "error": {
      finishAssistant();
      const d = document.createElement("div");
      d.className = "msg error";
      d.innerHTML = `<div class="md"><p>✗ ${escapeHtml(data.message)}</p></div>`;
      curLog().appendChild(d);
      scrollLog();
      break;
    }
    case "queue_updated":
      // 每轮结束都会推；pending>0 = 还有排队轮（含刚接棒的）在跑，保持运行态
      setRunning((data.pending || 0) > 0, routeTab);
      if (!(data.pending > 0)) {
        finishEta(routeTab); // 预估条定格（错误/中断路径没有 turn_finished，这里兜底）
        const doneTab = routeTab;
        if (doneTab && doneTab.lastAssistantText.trim()) {
          maybeNotify("任务完成", "本轮任务已结束，回来看看结果", "done",
            { sid: doneTab.sid });
        }
        if (doneTab && doneTab !== activeTab) renderTabs(); // 运行点熄灭
      }
      break;
    case "term_data": {
      // PTY 持续输出按 term_id 路由进对应 xterm；标签已关闭则丢弃
      const t = termTabById(data.term_id);
      if (t && t.term) t.term.write(data.text || "");
      break;
    }
    case "term_exit": {
      const t = termTabById(data.term_id);
      if (t && t.term) t.term.write("\r\n\x1b[90m[shell 已退出，输入任意命令重启]\x1b[0m\r\n");
      break;
    }
    case "aux_delta":
      if (auxStreamingEl) {
        auxStreamingText += data.text || "";
        auxThinkingPhase = false;
        auxScheduleRender();
      }
      break;
    case "aux_thinking":
      auxThinkingDelta(data.text);
      break;
    case "turn_finished": {
      finishAssistant();
      // 用引擎实测耗时定格芯片：前端本地计时从收到预估事件算起，与引擎的
      // 墙钟起点差一个网络往返，两个数字对不上（刷新后又变成引擎值）。
      finishEta(routeTab, data.duration_ms);
      refreshSessions();
      // 实时轮的用户气泡没有 seq：挂上操作（后端会按「最后一条 user」回退）
      if (routeTab && routeTab === activeTab) {
        const users = routeTab.logEl.querySelectorAll(".msg.user:not([data-seq])");
        const last = users[users.length - 1];
        if (last) attachMsgOps(last, routeTab, "user");
      }
      break; // 通知在 queue_updated（pending=0，队列清空）时发，避免多轮排队连响
    }
  }
}

// 工具安全级展示名：权限条/勾选表等面向用户的界面统一用它，不露内部枚举值
const SAFETY_LABELS = { readonly: "只读", write: "写入", dangerous: "高危" };

function showPermission(data) {
  if (routeTab && routeTab !== activeTab) {
    // 后台会话的确认请求：标到标签上，切过去再展示
    routeTab.needsPerm = true;
    routeTab.permData = data;
    maybeNotify("需要你确认", `${data.tool_name}（后台会话）正在等待决定`, "perm",
      { sid: routeTab.sid });
    renderTabs();
    return;
  }
  permRequest = data.request_id;
  maybeNotify("需要你确认", `${data.tool_name} 正在等待你的决定`, "perm",
    { sid: routeTab && routeTab.sid });
  document.getElementById("perm-tool").textContent =
    `${data.tool_name} · ${SAFETY_LABELS[data.safety] || data.safety}`;
  const argsEl = document.getElementById("perm-args");
  const noteEl = document.getElementById("perm-note");
  if (data.diff) {
    // 写入类操作：展示改前→改后 diff，确认时有真实依据
    argsEl.innerHTML = renderDiffText(data.diff);
    argsEl.classList.add("is-diff");
  } else {
    argsEl.textContent = data.detail || JSON.stringify(data.input, null, 2);
    argsEl.classList.remove("is-diff");
  }
  // 后端附带的额外说明（如「这条命令带 shell 拼接，前缀白名单不覆盖」）
  if (noteEl) {
    noteEl.textContent = data.note || "";
    noteEl.hidden = !data.note;
  }
  // 「总是允许」将写入的规则范围（由 gate.rule_for 生成后随事件下发）：
  // 点按钮前就能看到它会放行多大范围，而不是事后到设置里才发现
  const ruleEl = document.getElementById("perm-rule");
  if (ruleEl) {
    if (data.rule_kind) {
      const kind = RULE_KIND_LABEL[data.rule_kind] || data.rule_kind;
      const raw = data.rule_pattern || "";
      const pat = raw ? (raw.length > 80 ? raw.slice(0, 80) + "…" : raw) : "（全部）";
      ruleEl.textContent = `「总是允许」将添加规则：${data.tool_name} · ${kind} ${pat}`;
      ruleEl.title = raw;
      ruleEl.hidden = false;
    } else {
      ruleEl.hidden = true;
    }
  }
  document.getElementById("permission-bar").hidden = false;
  petRefresh();
  if (Math.random() < 0.6) petSay(petPick(PET_LINES.perm));
}

function hidePermission() {
  permRequest = null;
  document.getElementById("permission-bar").hidden = true;
  petRefresh();
}

document.querySelectorAll("#permission-bar [data-decision]").forEach((btn) => {
  btn.onclick = () => {
    if (!permRequest) return;
    request("permission.respond", { request_id: permRequest, decision: btn.dataset.decision });
  };
});

// ---------- 侧栏 ----------
let sessionSearchActive = false; // 搜索结果展示期间，禁止列表刷新覆盖
let activeSessionSid = null; // 侧栏里被点开的会话 id（两种视图共用）：列表因折叠/快照等重画后，选中高亮照它恢复

async function refreshSessions(prefetched) {
  if (sessionSearchActive) return;
  // 分组视图：项目与合到一起，渲染走另一条路（见 refreshSessionsGrouped）
  if (sidebarView === "grouped") return refreshSessionsGrouped();
  // prefetched：站内切换项目时已先取回，直接渲染（不经过网络等待，避免列表先清空）
  const { sessions, empty_count, archived_count, quick_sessions } =
    prefetched || await request("session.list");
  const ul = document.getElementById("session-list");
  ul.innerHTML = "";
  // 经典视图同样按家族块（└/⑂ 子跟随父）+ 保存序排（键 = 当前项目 id，
  // 与分组视图共用 session_order）；classic 列表即当前项目，直接取
  const classicGk = bootSnap && bootSnap.project_id != null ? String(bootSnap.project_id) : null;
  const classicList = classicGk != null ? orderedSessionList(sessions, classicGk) : sessions;
  if (classicGk != null) lastGroupedListByGroup.set(classicGk, classicList);
  // 按标签分组：有标签的会话归入对应组（可属多组），无标签的在「未分组」；
  // 全部会话都没标签时不分组，列表与从前完全一致（空分组头只是噪声）
  const tagMap = new Map();
  sessions.forEach((s) => {
    (s.tags || []).forEach((t) => {
      if (!tagMap.has(t)) tagMap.set(t, []);
      tagMap.get(t).push(s);
    });
  });
  const grouped = tagMap.size > 0;
  const untagged = classicList.filter((s) => !(s.tags || []).length);
  const groups = [];
  if (grouped) {
    for (const [tag, list] of tagMap) groups.push({ tag, list });
    if (untagged.length) groups.push({ tag: "未分组", list: untagged });
  } else {
    groups.push({ tag: null, list: classicList });
  }
  groups.forEach(({ tag, list }) => {
    if (tag) {
      const head = document.createElement("li");
      head.className = "s-group";
      head.innerHTML = `<span class="s-group-name">${escapeHtml(tag)}</span>`;
      head.onclick = () => {
        // 点分组头 = 只看这一组（再点一次取消过滤）
        activeTagFilter = activeTagFilter === tag ? null : tag;
        refreshSessions(prefetched);
      };
      if (activeTagFilter === tag) head.classList.add("active");
      ul.appendChild(head);
    }
    if (activeTagFilter && tag !== activeTagFilter) return;
    list.forEach((s) => {
      const li = renderSessionItem(s, ul);
      if (classicGk != null) {
        wireSessionDrag(li, s, classicList, classicGk, () => refreshSessions());
      }
      ul.appendChild(li);
    });
  });
  // 快聊区块放最后：它不受标签分组过滤影响，也不参与空列表的判断。
  // 远程客户端拿不到 quick_sessions（字段都不下发）：不显示一个永远为空的区块
  if (Array.isArray(quick_sessions)) renderQuickSection(ul, quick_sessions);
  renderSessionExtras({ empty_count, archived_count });
  renderRailSessions(sessions);
}

/** 经典视图底部的常驻「快聊」区块。

    快聊会话的 project_id 是 NULL，而经典视图的会话列表只取当前项目，所以
    它们在这里原本一个都看不到——只在分组视图与「全部项目」搜索里露面。
    单开一节常驻列表末尾：不绑定文件夹、只想聊一句的对话有固定去处，与分组
    视图的「快聊」组共用同一份数据与折叠状态（键都是 "quick"）。 */
function renderQuickSection(ul, list) {
  const st = groupState("quick"); // 与分组视图的快聊组共用折叠 / 显示更多记忆
  const head = document.createElement("li");
  // 空快聊时多一个 .is-empty：那时组头的 ＋ 是唯一的建会话入口，
  // 必须常显（平时仍靠悬停才现，避免常驻列表里一直闪一个按钮）
  head.className = "s-group s-quick" + (list.length ? "" : " is-empty");
  head.innerHTML = '<span class="s-group-name">快聊</span>' +
    '<button class="s-quick-add" title="新建快聊（不需要文件夹，随时能聊）">＋</button>';
  head.title = "快聊 —— 不绑定任何文件夹的对话，点击展开/折叠";
  head.querySelector(".s-quick-add").onclick = async (e) => {
    e.stopPropagation();
    const r = await request("session.new_task", {});
    await openTabForSession(r.id, r.title);
    refreshSessions();
  };
  head.onclick = () => {
    st.open = !st.open;
    refreshSessions();
  };
  ul.appendChild(head);
  if (!st.open) return;
  const ordered = orderedSessionList(list, "quick");
  lastGroupedListByGroup.set("quick", ordered);
  // 没快聊时只留组头（＋ 就在上面），不再补一行空提示
  if (!ordered.length) return;
  const visible = st.all ? ordered : ordered.slice(0, GROUP_PREVIEW);
  // 不接拖动排序：经典视图的 commitSessionOrder 按整张列表收集会话 id，
  // 快聊行混进去会把当前项目与快聊两个区块的顺序互相污染（分组视图里的
  // 快聊组仍可拖，那份顺序在这里照常生效——orderedSessionList 会读偏好）
  visible.forEach((s) => ul.appendChild(renderSessionItem(s, ul)));
  if (ordered.length > visible.length) {
    const more = document.createElement("li");
    more.className = "pgroup-more";
    more.textContent = st.all ? "收起" : `显示更多 ${ordered.length - visible.length} 个`;
    more.onclick = () => { st.all = !st.all; refreshSessions(); };
    ul.appendChild(more);
  }
}

// 空会话清理入口与归档入口：经典/分组两种视图共用（都只看当前项目）
function renderSessionExtras({ empty_count = 0, archived_count = 0 } = {}) {
  // 空会话清理入口（仅当确实存在空会话时出现）
  const footer = document.getElementById("session-footer");
  if (footer) {
    if (empty_count > 0) {
      footer.classList.remove("hidden");
      footer.innerHTML = `<button class="link-btn">🧹 清理 ${empty_count} 个空会话</button>`;
      footer.querySelector(".link-btn").onclick = async () => {
        const r = await request("session.cleanup_empty");
        addNotice(`已清理 ${r.removed} 个空会话`);
        refreshSessions();
      };
    } else {
      footer.classList.add("hidden");
    }
  }

  // 归档入口（有归档会话时出现；点开弹窗查看/恢复/删除）
  const arc = document.getElementById("session-archive");
  if (arc) {
    if ((archived_count || 0) > 0) {
      arc.classList.remove("hidden");
      arc.innerHTML = `<button class="link-btn">${svgIcon("archive")}<span>归档会话（${archived_count}）</span></button>`;
      arc.querySelector(".link-btn").onclick = openArchiveModal;
    } else {
      arc.classList.add("hidden");
      arc.innerHTML = "";
    }
  }
}

// 当前生效的标签过滤（点分组头切换）；只在内存里，不写偏好
let activeTagFilter = null;

// ---------- 折叠态窄栏（mini rail）：最近会话的首字快捷列 ----------
// 折叠后侧栏整条退场，窄栏承接「扫一眼、快速切」：列最近的会话，每项是标题
// 首字（中文单字 / 英文首字母），当前会话高亮。数据复用侧栏那次请求，不额外发 WS。
const RAIL_SESSION_MAX = 12; // 上限：再多也没人挨个认

function railInitial(title) {
  const t = String(title || "").trim();
  if (!t) return "·";
  return [...t][0].toUpperCase(); // 迭代器取字符：emoji / 代理对不会被截半
}

function renderRailSessions(sessions) {
  const box = document.getElementById("rail-sessions");
  if (!box) return;
  const list = (sessions || []).slice(0, RAIL_SESSION_MAX);
  box.innerHTML = "";
  if (!list.length) {
    const empty = document.createElement("span");
    empty.className = "rail-sess-empty";
    empty.textContent = "空";
    box.appendChild(empty);
    return;
  }
  list.forEach((s) => {
    const b = document.createElement("button");
    const active = s.id === currentSessionId;
    b.className = "rail-sess" + (active ? " active" : "");
    b.textContent = railInitial(s.title);
    b.title = (s.title || "(未命名)") + (active ? "（当前会话）" : "") + " · 点击切换";
    b.onclick = () => {
      if (settingsOpen) backToChat();
      openTabForSession(s.id, s.title);
    };
    box.appendChild(b);
  });
}

function renderSessionItem(s, ul) {
  const li = document.createElement("li");
  const tagChips = (s.tags || [])
    .map((t) => `<span class="s-tag">${escapeHtml(t)}</span>`).join("");
  const stamp = s.updated_at ? new Date(s.updated_at * 1000).toLocaleString() : "";
  li.innerHTML = `
      ${s.pinned ? '<span class="pin" title="已置顶">📌</span>' : ""}
      <span class="s-title">${escapeHtml(s.title || "(未命名)")}</span>
      <span class="s-time" title="最近活跃：${escapeHtml(stamp)}">${fmtRuleAge(s.updated_at)}</span>
      <button class="s-archive" title="归档">${svgIcon("archive")}</button>
      <button class="s-more" title="更多操作">⋯</button>` +
    (tagChips ? `<span class="s-tags">${tagChips}</span>` : "");
  li.dataset.sid = s.id; // 组内拖动排序：commitSessionOrder 沿 DOM 收集会话 id
  // 恢复「当前会话」高亮：列表会因折叠/快照/切项目等反复重画，选中态不能丢
  if (activeSessionSid != null && String(s.id) === String(activeSessionSid)) {
    li.classList.add("sess-active");
  }
  // 悬浮时时间让位：右侧出现归档与 ⋯
  li.querySelector(".s-archive").onclick = (e) => {
    e.stopPropagation();
    archiveSession(s);
  };
  li.querySelector(".s-more").onclick = (e) => {
    e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    showSessionMenu(s, li, { x: r.right - 175, y: r.bottom + 4 });
  };
  // 右键同样唤出会话管理菜单
  li.oncontextmenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    showSessionMenu(s, li, { x: e.clientX, y: e.clientY });
  };
  li.onclick = () => {
    // 只清会话行（带 data-sid 的行）的选中态：组头（项目组/标签组）的 active 是
    // 「当前项目/标签过滤」的蓝色高亮，点会话不能把它抹掉——项目高亮保留，
    // 会话行自己换用 .sess-active 的另一种高亮（见 app.css）
    ul.querySelectorAll("li[data-sid]").forEach((x) => x.classList.remove("sess-active"));
    activeSessionSid = s.id;
    li.classList.add("sess-active");
    const t = openTabForSession(s.id, s.title);
    clearTodoPanel();
    addNotice(`已恢复会话 ${s.title || s.id}`);
    if (t && !t.running) t.needHistory = false;
  };
  return li;
}

// ---------- 分组视图：会话按项目折叠收拢（项目一多，经典两区挤不下会话） ----------
// 与经典视图并存，sidebar_view 偏好记住选择（classic=原两区样式，保留不删）
let sidebarView = "classic";
let groupSeq = 0; // 渲染序号：两次并发的分组渲染，慢的那个回来后直接丢弃
const projGroupState = new Map(); // 分组 key -> { open, all }：折叠与「显示更多」记忆，重渲染不丢
let lastGroupedCurrentKey = null; // 上一次分组渲染时的当前项目 key：项目切换时自动展开新组（取代旧「当前置顶」）
// 组内会话的自定义顺序（ui.json 的 session_order，偏好回包后由 initUiPrefs 填充）。
// 键 = 组 key（字符串化项目 id），值 = 会话 id 数组（只记手动拖过的，新的照时间追加）
let sessionOrderPrefs = {};
// 标签栏页签的拖动序（ui.json 的 tab_order，sid 数组，只记有 sid 的会话标签；
// 偏好回包后由 initUiPrefs 填充，renderTabs 按它排，空标签始终垫底）
let tabOrderPrefs = [];
const lastGroupedListByGroup = new Map(); // 组 key -> 该组本次渲染的会话列表（家族归并要用，渲染前写入）
const GROUP_PREVIEW = 5; // 每个项目默认露出的会话条数，其余收进「显示更多」
const SUB_MARK = "└"; // session.fork 生成的分支会话标题前缀（「└ 父标题」），即子会话标记。
// 旧标题用 ⑂（OCR fork 字符，中文字体下笔画过细看不清），2.0 后新分叉统一用 └；
// 兼容期两个都认，见 SUB_MARKS

const SUB_MARKS = [SUB_MARK, "⑂"]; // 兼容：旧版本分叉的会话标题仍是 ⑂ 前缀

function subParentTitleOf(title) {
  const t = title || "";
  for (const mark of SUB_MARKS) {
    if (t.startsWith(mark)) return t.slice(mark.length).trimStart();
  }
  return null;
}

/** 在组列表里找子会话的父会话（fork 标题截断到 40 字符，子标题去前缀后可能只
    是父标题的前缀，所以双向 startsWith 兜底）；找不到父时子会话按顶层对待 */
function findParentInGroup(list, s) {
  const pt = subParentTitleOf(s.title);
  if (!pt) return null;
  return list.find((p) => p.id !== s.id && !subParentTitleOf(p.title) &&
    (p.title === pt || p.title.startsWith(pt) || pt.startsWith(p.title))) || null;
}

/** 组内会话的展示序：保存过的 id 按保存序排前，其余（没拖过的/新会话）按
    后端默认序（置顶在前、新在前）接着排——旧偏好不藏新会话，同 project_order。
    之后做家族聚拢：⑂ 子会话强制紧跟其父之后——它们是分支关系不是平级，
    不能因 updated_at 更新就跑到父上面，也不能被手动排序漂进别的会话之间 */
function orderedSessionList(list, gkey) {
  const saved = sessionOrderPrefs[String(gkey)];
  let base = list;
  if (saved && saved.length) {
    const rank = new Map(saved.map((id, i) => [id, i]));
    base = [...list.filter((s) => rank.has(s.id)).sort((a, b) => rank.get(a.id) - rank.get(b.id)),
            ...list.filter((s) => !rank.has(s.id))];
  }
  const subsByParent = new Map(); // 父 id -> 子数组（保持 base 相对序）
  const isSub = new Set();
  for (const s of base) {
    if (!subParentTitleOf(s.title)) continue;
    const parent = findParentInGroup(base, s);
    if (parent) {
      if (!subsByParent.has(parent.id)) subsByParent.set(parent.id, []);
      subsByParent.get(parent.id).push(s);
      isSub.add(s.id);
    }
  }
  const out = [];
  for (const s of base) {
    if (isSub.has(s.id)) continue; // 子已跟随父输出
    out.push(s);
    const subs = subsByParent.get(s.id);
    if (subs) out.push(...subs);
  }
  return out;
}

function groupState(key) {
  // 项目 id 从 JSON 来是数字、dataset.gkey 读回是字符串，同一组会存成两个键——
  // 折叠全部（走 dataset）就会与渲染（走原始 id）各写各的。入口统一成字符串
  key = String(key);
  if (!projGroupState.has(key)) projGroupState.set(key, { open: true, all: false });
  return projGroupState.get(key);
}

function applySidebarView() {
  // 单按钮切换：图标显示当前所在视图（同原先分段控件高亮图标的含义），
  // 点击切换视图、图标随之翻到另一个
  const vt = document.getElementById("view-toggle");
  if (vt) {
    vt.dataset.current = sidebarView;
    vt.title = sidebarView === "classic"
      ? "经典视图（点击切换到分组视图：会话收进各自项目下，项目多时省空间）"
      : "分组视图（点击切换到经典视图：项目、会话分成两区）";
  }
  // 按钮组随视图搬移（单实例 DOM 移动，状态/监听天然保留）：
  //   经典视图：[☰][＋] 在「项目」标题行右端（第一条横线右方，用户指定）；
  //   分组视图：项目区隐藏，[⌄][📁][＋] 回「会话」标题行，⌄ 在 📁 左侧。
  const addBtn = document.getElementById("session-add-project");
  const projOps = document.getElementById("project-head-ops");
  const sessOps = document.getElementById("session-head-ops");
  if (vt && addBtn && projOps && sessOps) {
    const dst = sidebarView === "classic" ? projOps : sessOps;
    dst.appendChild(vt);
    dst.appendChild(addBtn);
  }
  const sec = document.getElementById("project-section");
  if (sec) sec.classList.toggle("hidden", sidebarView === "grouped");
  const sec2 = document.getElementById("session-section");
  if (sec2) sec2.classList.toggle("grouped", sidebarView === "grouped");
  const ul = document.getElementById("session-list");
  if (ul) ul.classList.toggle("by-project", sidebarView === "grouped");
  syncFoldAllBtn();
}

document.getElementById("view-toggle").onclick = () => {
  sidebarView = sidebarView === "grouped" ? "classic" : "grouped";
  applySidebarView();
  saveUiPrefs({ sidebar_view: sidebarView });
  // 两个视图的列表渲染不同：切过去就重画；搜索词还在就按新视图重搜
  const q = searchEl.value.trim();
  if (q) renderSessionList(q);
  else refreshSessions();
};

// 「折叠全部项目」（分组视图专属）：项目一多时一键全收，全收后变成「展开全部项目」。
// 开合记忆与单组折叠同源（projGroupState）；经典视图与搜索结果没有可折叠组，按钮隐藏
function groupHeads() {
  return [...document.querySelectorAll("#session-list .pgroup-head")];
}

function syncFoldAllBtn() {
  const btn = document.getElementById("btn-fold-all");
  if (!btn) return;
  const heads = groupHeads();
  const expand = heads.length > 0 && heads.every((h) => !groupState(h.dataset.gkey).open);
  btn.classList.toggle("hidden",
    sidebarView !== "grouped" || sessionSearchActive || !heads.length);
  btn.classList.toggle("expand", expand);
  const tip = expand ? "展开全部项目" : "折叠全部项目";
  btn.title = tip;
  btn.setAttribute("aria-label", tip);
}

const foldAllBtn = document.getElementById("btn-fold-all");
if (foldAllBtn) foldAllBtn.onclick = () => {
  const heads = groupHeads();
  const expand = heads.length > 0 && heads.every((h) => !groupState(h.dataset.gkey).open);
  heads.forEach((h) => { groupState(h.dataset.gkey).open = expand; });
  refreshSessionsGrouped(); // 尾部的 syncFoldAllBtn 会把按钮翻成展开态
};

async function refreshSessionsGrouped() {
  const seq = ++groupSeq;
  const [listRes, projRes] = await Promise.all([
    request("session.list", { all_projects: 1 }).catch(() => null),
    request("project.list").catch(() => ({ projects: [] })),
  ]);
  if (seq !== groupSeq) return; // 已有更新的一轮在后面，别拿旧数据盖新
  if (!listRes) {
    // 远程客户端拿不到跨项目列表（安全口径同 scope=all）：退回经典视图，
    // 偏好一并还原，免得远程端每次进来都要退一次
    sidebarView = "classic";
    applySidebarView();
    saveUiPrefs({ sidebar_view: null });
    addNotice("分组视图只在本机桌面端可用，已切回经典视图");
    refreshSessions();
    return;
  }
  const projects = projRes.projects || [];
  const sessions = listRes.sessions || [];
  // 按 project_id 分桶；不属于任何已列项目的（含 NULL 的快聊）收进结尾的兜底组
  const byProject = new Map();
  sessions.forEach((s) => {
    const k = s.project_id == null ? "quick" : s.project_id;
    if (!byProject.has(k)) byProject.set(k, []);
    byProject.get(k).push(s);
  });
  const ul = document.getElementById("session-list");
  const frag = document.createDocumentFragment();
  // 组内会话应用拖动保存的自定义序（orderedSessionList：保存过的排前，新会话按时间追加）
  byProject.forEach((list, k) => {
    byProject.set(k, orderedSessionList(list, k));
    lastGroupedListByGroup.set(String(k), byProject.get(k));
  });
  // 组序保持 project.list 原序：项目在侧栏的位置稳定，不因「对话页正显示哪个
  // 项目的会话」而上跳下蹿；当前项目改用「变化时自动展开它的组」来指示——
  // 只在切到新项目那一刻展开一次，手动折叠的组不会被反复顶开。
  // 上一次的当前项目记在模块级 lastGroupedCurrentKey，跨渲染对比才知道「变化」
  const curProj = projects.find((p) => p.is_current);
  const currentKey = curProj ? curProj.id : null;
  if (currentKey != null && String(currentKey) !== String(lastGroupedCurrentKey)) {
    groupState(currentKey).open = true;
  }
  lastGroupedCurrentKey = currentKey;
  projects.forEach((p) => {
    renderProjectGroup(frag, {
      key: p.id, name: p.name, list: byProject.get(p.id) || [],
      isCurrent: !!p.is_current, rootPath: p.root_path || "", project: p,
    });
    byProject.delete(p.id);
  });
  const loose = [];
  byProject.forEach((list, key) => {
    if (key !== "quick") loose.push(...list);
  });
  {
    // 「快聊」分组常驻：不绑定文件夹的对话都在这里（有时候只是想聊一句、
    // 做点小任务，不需要工作目录）。组头 ＋ 一键新建快聊对话。
    renderProjectGroup(frag, {
      key: "quick", name: "快聊", list: byProject.get("quick") || [],
      isCurrent: false, rootPath: "", project: null,
      headPlus: async () => {
        const r = await request("session.new_task", {});
        await openTabForSession(r.id, r.title);
        refreshSessionsGrouped();
      },
    });
  }
  if (loose.length) {
    renderProjectGroup(frag, {
      key: "loose", name: "其他", list: loose,
      isCurrent: false, rootPath: "", project: null,
    });
  }
  ul.innerHTML = "";
  ul.appendChild(frag);
  renderSessionExtras(listRes);
  renderRailSessions(sessions);
  syncFoldAllBtn();
}

function renderProjectGroup(frag, { key, name, list, isCurrent, rootPath, project, headPlus }) {
  const ul = document.getElementById("session-list");
  const st = groupState(key);
  const head = document.createElement("li");
  // 空组头的 ＋ 常显（is-empty）：空的时候它是唯一的建会话入口；
  // 有会话时仍靠悬停才现（与 .s-quick-add 同一套手势）
  head.className = "pgroup-head" + (isCurrent ? " active" : "") +
    (st.open ? " open" : "") + (list.length ? "" : " is-empty");
  head.dataset.gkey = key; // 折叠全部/展开全部要靠它找回各组的状态
  head.innerHTML = FOLDER_SVG +
    `<span class="pg-name">${escapeHtml(name)}</span>` +
    (headPlus
      ? '<button class="pg-add" title="新建快聊（不需要文件夹，随时能聊）">＋</button>' : "") +
    // 「远程连接」固定项目（无真实目录）不提供删除
    (project && rootPath
      ? `<button class="pg-del" title="${isCurrent ? "重置这个项目（清空会话与记录）" : "从列表中移除这个项目"}">✕</button>` : "") +
    '<span class="pg-chev"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M6 4l4 4-4 4"/></svg></span>';
  head.title = `${name} —— 点击展开/折叠该项目下的会话`;
  const addBtn = head.querySelector(".pg-add");
  if (addBtn) addBtn.onclick = (e) => { e.stopPropagation(); headPlus(); };
  // 整行点击都只做展开/折叠（切换项目走「点该项目下的会话」或经典视图）
  head.onclick = () => {
    st.open = !st.open;
    refreshSessionsGrouped();
  };
  head.querySelector(".pg-chev").onclick = (e) => {
    e.stopPropagation();
    st.open = !st.open;
    refreshSessionsGrouped();
  };
  const del = head.querySelector(".pg-del");
  if (del) del.onclick = (e) => { e.stopPropagation(); deleteProjectModal(project); };
  if (project) wireGroupDrag(head, key);
  frag.appendChild(head);
  if (!st.open) return;
  const visible = st.all ? list : list.slice(0, GROUP_PREVIEW);
  visible.forEach((s) => {
    const li = renderSessionItem(s, ul);
    if (rootPath && !isCurrent) {
      // 其他项目的会话不能在当前项目里直接激活：先切工作项目再打开
      // （同「全部项目」搜索命中的流程），覆盖 renderSessionItem 的默认点击
      li.title = `属于项目「${name}」—— 点击切换过去并打开`;
      li.onclick = async () => {
        // 先记住目标会话再切项目：切完的列表重画会照它恢复选中高亮
        activeSessionSid = s.id;
        try {
          await request("project.switch", { path: rootPath });
          await applyWorkspaceData(await fetchWorkspaceData());
        } catch (e) {
          addNotice("切换到该项目失败：" + e.message);
          return;
        }
        openTabForSession(s.id, s.title);
        clearTodoPanel();
        addNotice(`已打开会话 ${s.title || s.id}`);
      };
    }
    wireSessionDrag(li, s, list, key);
    frag.appendChild(li);
  });
  if (list.length > visible.length || (st.all && list.length > GROUP_PREVIEW)) {
    const more = document.createElement("li");
    more.className = "pgroup-more";
    if (st.all) {
      more.textContent = "收起";
      more.onclick = () => { st.all = false; refreshSessionsGrouped(); };
    } else {
      more.textContent = `显示更多 ${list.length - visible.length} 个`;
      more.onclick = () => { st.all = true; refreshSessionsGrouped(); };
    }
    frag.appendChild(more);
  }
}

// ---- 拖动排序（分组视图组头 / 组内会话行 / 经典视图项目行、会话行共用）----
// 顺序存 ui.json 的 project_order / session_order，拖拽只改顺序不改数据本身；
// 两种视图共享同一份顺序，分组视图调好序经典视图同享。

/** 通用拖拽接线：手势部分（start/over/leave/end）全在这，落点重定向与
    提交由 opts 决定：
      over(e, item, src) -> 悬停时的落点 { id, pos } 或 null（不允许落/不响应）；
                            不传则默认上/下缘对半判定；id 默认 item.id
      commit(dst, pos)   -> dragend 里真正落库，返回 promise，resolve 后 rerender()
      rerender()         -> 保存成功后的重渲染
    进行态存 wireListDrag.active（dataTransfer 只在 drop 可读，跨事件传状态用模块级） */
function wireListDrag(el, item, opts) {
  el.draggable = true;
  el.addEventListener("dragstart", (e) => {
    wireListDrag.active = { item, over: null };
    el.classList.add("dragging", "sess-dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(item.id ?? "")); // Firefox：不 setData 不起拖
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging", "sess-dragging");
    const st = wireListDrag.active;
    wireListDrag.active = null;
    clearDragVisual();
    clearSessionDragVisual();
    if (!st || !st.over) return;
    if (st.over.id !== item.id && opts.commit) {
      Promise.resolve(opts.commit(st.over, st.over.pos || "before")).then(() => {
        if (opts.rerender) opts.rerender();
      });
    }
  });
  el.addEventListener("dragover", (e) => {
    const st = wireListDrag.active;
    if (!st || st.item.id === item.id) return;
    const t = opts.over ? opts.over(e, item, st.item)
      : { id: item.id, pos: dragHalfPos(e, el) };
    if (!t || t.id === st.item.id) return;
    e.preventDefault(); // 允许作为落点
    e.dataTransfer.dropEffect = "move";
    if (st.over && st.over.id === t.id && st.over.pos === t.pos) return;
    clearDragVisual();
    clearSessionDragVisual();
    st.over = t;
    el.classList.add(t.pos === "before" ? "drop-above" : "drop-below");
  });
  el.addEventListener("dragleave", () => {
    const st = wireListDrag.active;
    if (st && st.over && st.over.id === item.id) {
      el.classList.remove("drop-above", "drop-below");
      st.over = null;
    }
  });
  el.addEventListener("drop", (e) => e.preventDefault()); // 提交统一在 dragend
}
wireListDrag.active = null;

function dragHalfPos(e, el) {
  const rect = el.getBoundingClientRect();
  return e.clientY < rect.top + rect.height / 2 ? "before" : "after";
}

/** 给组头接上拖拽（分组视图）。head 的点击行为（折叠）不受影响：拖拽与点击是两套手势。 */
function wireGroupDrag(head, key) {
  wireListDrag(head, { id: String(key) }, {
    commit: (dst, pos) => commitProjectOrder(dst.id, pos),
    rerender: refreshSessionsGrouped,
  });
}

function clearDragVisual() {
  document.querySelectorAll("#session-list .pgroup-head.drop-above, #session-list .pgroup-head.drop-below, #project-list li.drop-above, #project-list li.drop-below, #chat-tabs .chat-tab.drop-left, #chat-tabs .chat-tab.drop-right")
    .forEach((el) => el.classList.remove("drop-above", "drop-below", "drop-left", "drop-right"));
}

/** 项目新序提交：listId 决定从哪个列表读当前 DOM 序（分组视图从组头、
    经典视图从项目行），两种视图保存同一份 project_order */
function commitProjectOrder(srcKey, dstKey, pos, listId = "session-list", rowSel = ".pgroup-head", attr = "gkey") {
  const keys = [...document.querySelectorAll(`#${listId} ${rowSel}`)]
    .map((h) => String(h.dataset[attr]))
    .filter((k) => k !== "quick" && k !== "loose"); // 伪组不进偏好
  const from = keys.indexOf(srcKey);
  let to = keys.indexOf(dstKey);
  if (from < 0 || to < 0 || from === to) return Promise.resolve();
  keys.splice(from, 1);
  to = keys.indexOf(dstKey); // 抽走 src 后下标可能前移，重找
  if (pos === "after") to += 1;
  keys.splice(to, 0, srcKey);
  return request("ui.save", {
    prefs: { project_order: keys.map((k) => parseInt(k, 10)).filter((n) => n > 0) },
  }).catch(() => {});
}

/** 给会话行接上组内拖拽（分组/经典两种视图共用）。同级限制：└/⑂ 子会话跟随父
    （作为一整个家族块）参与排序；拖到子行上时落点重定向到它的父（即插到该
    家族块的前/后），不会插进父与子之间 */
function wireSessionDrag(li, s, list, gkey, rerender) {
  const parent = findParentInGroup(list, s);
  const srcFamily = parent ? parent.id : s.id;
  wireListDrag(li, s, {
    over: (e, item) => {
      // 同级判定：拖到子行上重定向到父；同家族块不响应
      const overRow = findParentInGroup(list, item);
      const overFamily = overRow ? overRow.id : item.id;
      if (overFamily === srcFamily) return null;
      return { id: overRow ? overRow.id : item.id, pos: dragHalfPos(e, li) };
    },
    commit: (dst, pos) => commitSessionOrder(String(gkey), s.id, dst.id, pos),
    rerender: rerender || refreshSessionsGrouped,
  });
}

function clearSessionDragVisual() {
  document.querySelectorAll("#session-list li.drop-above, #session-list li.drop-below")
    .forEach((el) => el.classList.remove("drop-above", "drop-below"));
}

/** 会话新序提交（同级限制，分组/经典两种视图共用）：先按家族块（└/⑂ 子跟随父）
    把可见行归并为顶层块序，再按落点移动整个块；子行永远跟在父后，不会被拖散。
    经典视图传 view="classic"：整张 session-list 就是当前项目的会话（中间可能夹
    标签分组头，按 data-sid 收集即可），gkey 用 bootSnap.project_id。
    返回保存 promise。 */
function commitSessionOrder(gkey, srcId, dstId, pos, view = "grouped") {
  const gk = String(gkey);
  const listSel = view === "classic" ? "#session-list li[data-sid]"
                                     : "#session-list .pgroup-head";
  let visibleIds;
  if (view === "classic") {
    visibleIds = [...document.querySelectorAll(listSel)].map((el) => el.dataset.sid);
  } else {
    visibleIds = [...document.querySelectorAll("#session-list .pgroup-head")]
      .filter((h) => String(h.dataset.gkey) === gk)
      .flatMap((h) => {
        const out = [];
        let el = h.nextElementSibling;
        while (el && !el.classList.contains("pgroup-head")) {
          if (el.dataset && el.dataset.sid) out.push(el.dataset.sid);
          el = el.nextElementSibling;
        }
        return out;
      });
  }
  if (!visibleIds.includes(srcId) || !visibleIds.includes(dstId)) return Promise.resolve();
  // 用渲染时的组列表做家族归并（DOM 无标题信息）；lastGroupedListByGroup 由两种
  // 视图各自的渲染函数在渲染前写入（经典视图键 = 当前项目 id）
  const list = lastGroupedListByGroup.get(gk) || [];
  const byId = new Map(list.map((s) => [s.id, s]));
  const blockOf = new Map(); // 会话 id -> 块代表（顶层父）id
  const blockMembers = new Map(); // 块代表 id -> [父, ...子]（渲染序）
  for (const id of visibleIds) {
    const row = byId.get(id);
    if (!row || blockOf.has(id)) continue;
    blockOf.set(id, id);
    blockMembers.set(id, [id]);
    for (const cand of list) {
      if (blockOf.has(cand.id) || cand.id === id) continue;
      const pp = findParentInGroup(list, cand);
      if (pp && pp.id === id && visibleIds.includes(cand.id)) {
        blockOf.set(cand.id, id);
        blockMembers.get(id).push(cand.id);
      }
    }
  }
  const srcBlock = blockOf.get(srcId);
  const dstBlock = blockOf.get(dstId);
  if (srcBlock == null || dstBlock == null || srcBlock === dstBlock) return Promise.resolve();
  const heads = visibleIds.filter((id) => blockOf.get(id) === id);
  const from = heads.indexOf(srcBlock);
  let to = heads.indexOf(dstBlock);
  if (from < 0 || to < 0) return Promise.resolve();
  heads.splice(from, 1);
  to = heads.indexOf(dstBlock);
  if (pos === "after") to += 1;
  heads.splice(to, 0, srcBlock);
  // 块序展开回会话序（每块：父在前、子随后）；隐藏行（不在本次可见名单的）补尾
  const orderedIds = heads.flatMap((h) => blockMembers.get(h) || [h]);
  const merged = [...new Set([...orderedIds, ...(sessionOrderPrefs[gk] || [])])];
  sessionOrderPrefs[gk] = merged;
  return request("ui.save", { prefs: { session_order: sessionOrderPrefs } }).catch(() => {});
}

// 「引用此会话」：挂进输入框的引用托盘，随下一条消息把该会话内容注入上下文
function quoteThisSession(s) {
  if (activeTab && activeTab.sid === s.id) {
    addNotice("这就是当前正在对话的会话，直接提问即可");
    return;
  }
  if (pendingRefs.some((r) => r.id === s.id)) {
    addNotice("已引用该会话");
    return;
  }
  pendingRefs.push({ id: s.id, title: s.title || "未命名会话" });
  renderRefTray();
  inputEl.focus();
  addNotice(`已引用「${s.title || "未命名会话"}」，随下一条消息发给 Agent`);
}

// 归档当前会话：侧栏列表、搜索、启动续聊三处同时隐藏，可在归档弹窗恢复
async function archiveSession(s) {
  await request("session.archive", { id: s.id, archived: true });
  addNotice(`已归档「${s.title || "(未命名)"}」，点侧栏底部「归档会话」可找回`);
  refreshSessions();
}

function exportSession(s) {
  return request("session.export", { id: s.id })
    .then((r) => downloadText(r.filename, r.markdown))
    .catch((e) => addNotice("导出失败: " + e.message));
}

// —— 归档弹窗：查看已归档会话，恢复或彻底删除（删除需二次确认） ——
async function openArchiveModal() {
  const r = await request("session.list_archived");
  const list = r.sessions || [];
  const box = document.createElement("div");
  box.className = "archive-list";
  if (!list.length) {
    box.innerHTML = '<p class="dim small">没有已归档的会话。右键会话（或点 ⋯）选「归档」，它就会从侧栏消失并收进这里。</p>';
  }
  const closeIfEmpty = () => {
    if (!box.querySelector(".archive-row")) { hideModal(); refreshSessions(); }
  };
  list.forEach((s) => {
    const row = document.createElement("div");
    row.className = "archive-row";
    row.innerHTML =
      `<div class="archive-main"><b>${escapeHtml(s.title || "(未命名)")}</b>` +
      `<span class="archive-time">最近活跃：${fmtRuleAge(s.updated_at)}</span></div>`;
    const ops = document.createElement("div");
    ops.className = "archive-ops";
    const restore = document.createElement("button");
    restore.className = "rp-mini";
    restore.textContent = "恢复";
    restore.onclick = async () => {
      await request("session.archive", { id: s.id, archived: false });
      addNotice(`已恢复「${s.title || "(未命名)"}」`);
      row.remove();
      closeIfEmpty();
    };
    const del = document.createElement("button");
    del.className = "rp-mini danger";
    del.textContent = "删除";
    let armed = false;
    del.onclick = async () => {
      if (!armed) { armed = true; del.textContent = "确认删除"; return; }
      try { await request("session.delete", { id: s.id }); }
      catch (e) { addNotice("删除失败: " + e.message); return; }
      addNotice(`已删除「${s.title || "(未命名)"}」`);
      row.remove();
      closeIfEmpty();
    };
    ops.appendChild(restore);
    ops.appendChild(del);
    row.appendChild(ops);
    box.appendChild(row);
  });
  showModal("归档会话", box, async () => {}, "关闭");
}

// ---------- 会话搜索（标题 + 消息全文，对标 Claude Code /resume 检索） ----------
const searchEl = document.getElementById("session-search");
let searchTimer = null;
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderSessionList(searchEl.value.trim()), 200);
});

// 搜索命中高亮：先转义再逐段包 <mark>，大小写不敏感
function highlightSnippet(text, q) {
  text = String(text || "");
  if (!q) return escapeHtml(text);
  const low = text.toLowerCase();
  const ql = q.toLowerCase();
  let out = "";
  let i = 0;
  for (;;) {
    const p = low.indexOf(ql, i);
    if (p < 0) {
      out += escapeHtml(text.slice(i));
      break;
    }
    out += escapeHtml(text.slice(i, p)) +
      "<mark>" + escapeHtml(text.slice(p, p + q.length)) + "</mark>";
    i = p + q.length;
  }
  return out;
}

async function renderSessionList(query) {
  const ul = document.getElementById("session-list");
  if (!query) {
    sessionSearchActive = false;
    refreshSessions();
    return;
  }
  sessionSearchActive = true;
  // 搜索固定跨全部项目（含快聊）：一个范围少一个开关，跨项目命中自带项目名标注、
  // 点击自动切过去；切换器已删（2026-09-22），分组视图此前本就固定 all，两视图统一。
  const r = await request("session.search", { query, scope: "all" })
    .catch(() => ({ results: [] }));
  ul.innerHTML = "";
  syncFoldAllBtn(); // 搜索结果是平铺列表，没有组可折，「折叠全部」随之隐藏
  if (!r.results.length) {
    ul.innerHTML = '<li class="empty-hint">所有项目里都没有匹配的会话或消息</li>';
    return;
  }
  r.results.forEach((s) => {
    const li = document.createElement("li");
    li.className = "has-snippet";
    li.innerHTML =
      `<span class="s-title">${escapeHtml(s.title || "(未命名)")}</span>` +
      // 搜索跨全部项目，标出来自哪个项目，否则一堆同名标题分不清
      `<span class="s-project dim small" title="${escapeHtml(s.project_path || "")}">📁 ${escapeHtml(s.project_name || "")}</span>` +
      `<span class="s-snippet">${highlightSnippet(s.snippet, query)}</span>`;
    li.title = "点击打开这个会话";
    li.onclick = async () => {
      // 跨项目命中：先切工作项目再打开，否则会话在当前项目列表里找不到
      if (s.project_path &&
          s.project_path.toLowerCase() !== (bootSnap?.working_dir || "").toLowerCase()) {
        try {
          await request("project.switch", { path: s.project_path });
          await applyWorkspaceData(await fetchWorkspaceData());
        } catch (e) {
          addNotice("切换到该项目失败：" + e.message);
          return;
        }
      }
      searchEl.value = "";
      openTabForSession(s.session_id, s.title);
      clearTodoPanel();
      addNotice(`已打开会话 ${s.title || s.session_id}`);
      refreshSessions();
    };
    ul.appendChild(li);
  });
}

// ---------- 会话操作菜单（⋯ 按钮或右键唤出） ----------
const menuEl = document.getElementById("session-menu");

// 线条风小图标（stroke 继承文字色，与界面线稿风格一致）
function svgIcon(name) {
  const P = {
    quote: '<path d="M13.9 3.2H2.1a.9.9 0 0 0-.9.9v6.3a.9.9 0 0 0 .9.9h2.6v2.6l3.3-2.6h5.9a.9.9 0 0 0 .9-.9V4.1a.9.9 0 0 0-.9-.9z"/>',
    pin: '<path d="M5.5 2.5h5v3l1.5 2.5H4l1.5-2.5v-3z"/><path d="M8 9v4.5"/>',
    move: '<path d="M2.5 5H12m0 0L9.5 2.5M12 5 9.5 7.5"/><path d="M13.5 11H4m0 0 2.5-2.5M4 11l2.5 2.5"/>',
    rename: '<path d="M11.2 2.4l2.4 2.4L6 12.4l-3.2.8.8-3.2 7.6-7.6z"/>',
    trash: '<path d="M2.5 4h11M6.5 4V2.8h3V4M4.2 4l.6 9h6.4l.6-9"/><path d="M6.6 6.5v4.5M9.4 6.5v4.5"/>',
    archive: '<path d="M2.5 2.8h11v2.4h-11z"/><path d="M3.6 5.2v7.1a.8.8 0 0 0 .8.8h7.2a.8.8 0 0 0 .8-.8V5.2"/><path d="M6.3 8.1h3.4"/>',
  };
  return '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + P[name] + "</svg>";
}

function showSessionMenu(s, li, pos) {
  menuEl.innerHTML = "";
  const items = [
    { icon: "quote", label: "引用此会话", act: () => quoteThisSession(s) },
    {
      icon: "pin",
      label: s.pinned ? "取消置顶" : "置顶会话",
      act: async () => {
        await request("session.pin", { id: s.id, pinned: !s.pinned });
        addNotice(s.pinned ? `已取消置顶` : `已置顶「${s.title || "(未命名)"}」`);
        refreshSessions();
      },
    },
    { icon: "move", label: "迁移到其他项目", act: () => moveSessionModal(s) },
    { icon: "rename", label: "重命名", act: () => startInlineRename(s, li) },
    { icon: "trash", label: "删除会话", danger: true, act: () => deleteSessionModal(s) },
  ];
  items.forEach((it) => {
    const b = document.createElement("button");
    b.innerHTML = svgIcon(it.icon) + `<span>${escapeHtml(it.label)}</span>`;
    if (it.danger) b.classList.add("danger");
    b.onclick = () => {
      menuEl.classList.add("hidden");
      it.act();
    };
    menuEl.appendChild(b);
  });
  menuEl.classList.remove("hidden");
  // pos 与 window 尺寸是物理像素，菜单定位在缩放布局坐标系，先除回 uiScale
  menuEl.style.top = Math.min(pos.y / uiScale, window.innerHeight / uiScale - 240) + "px";
  menuEl.style.left = Math.max(8, Math.min(pos.x / uiScale, window.innerWidth / uiScale - 190)) + "px";
}
document.addEventListener("click", (e) => {
  if (!menuEl.contains(e.target)) menuEl.classList.add("hidden");
});
document.addEventListener("scroll", () => menuEl.classList.add("hidden"), true);

// ---------- 会话标签的命名（双击 / 右键菜单） ----------

/** 标签栏右键菜单：与侧栏会话行同一套菜单外观（共用 #session-menu）。
    只放与这张标签直接相关的动作；置顶/迁移等会话级管理仍回侧栏的 ⋯ 菜单。 */
function showTabMenu(t, pos) {
  menuEl.innerHTML = "";
  const items = [
    { icon: "rename", label: "重命名", act: () => startTabRename(t, t.el) },
    { icon: "trash", label: "关闭标签", danger: true, act: () => closeTab(t) },
  ];
  items.forEach((it) => {
    const b = document.createElement("button");
    b.innerHTML = svgIcon(it.icon) + `<span>${escapeHtml(it.label)}</span>`;
    if (it.danger) b.classList.add("danger");
    b.onclick = () => {
      menuEl.classList.add("hidden");
      it.act();
    };
    menuEl.appendChild(b);
  });
  menuEl.classList.remove("hidden");
  // 与 showSessionMenu 同一套缩放坐标换算
  menuEl.style.top = Math.min(pos.y / uiScale, window.innerHeight / uiScale - 140) + "px";
  menuEl.style.left = Math.max(8, Math.min(pos.x / uiScale, window.innerWidth / uiScale - 190)) + "px";
}

/** 标签改名：就地把标题换成输入框（Enter 保存 / Esc 取消 / 失焦保存）。
    有会话就落库；还没落库的空标签先记在标签上，首轮发消息创建会话时一并落库
    （见 send()）。没有会话可改也不是错误——预命名是合法需求。 */
function startTabRename(t, el) {
  if (!el || !el.isConnected) return;
  const titleEl = el.querySelector(".tab-title");
  if (!titleEl) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-inline tab-rename";
  input.value = t.title || "";
  input.placeholder = "会话名称";
  // 页签可拖动排序（draggable）：输入框里拖选文本会被当成拖页签，改名期间先关掉
  const wasDraggable = el.draggable;
  el.draggable = false;
  const restoreDrag = () => { if (el.isConnected) el.draggable = wasDraggable; };
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    restoreDrag();
    if (save && title) {
      try {
        await renameTabSession(t, title);
        addNotice(`已重命名为「${title}」`);
      } catch (e) {
        addNotice("重命名失败: " + e.message);
      }
    }
    renderTabs();
    refreshSessions();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  };
  input.onblur = () => finish(true);
  // 别让输入框上的点击触发切标签/再进改名
  input.onclick = (e) => e.stopPropagation();
  input.ondblclick = (e) => e.stopPropagation();
  input.onpointerdown = (e) => e.stopPropagation();
  input.oncontextmenu = (e) => e.stopPropagation();
}

/** 改名落库：有会话直接 session.rename；空标签只记本地（创建时再落）。
    同时更新 sessionMeta（标签标题的回退源）并刷新侧栏列表。 */
async function renameTabSession(t, title) {
  t.title = title;
  t.titleFixed = true; // 用户起过名：首轮自动标题不再覆盖（见 send()）
  if (t.sid) {
    await request("session.rename", { id: t.sid, title });
    sessionMeta[t.sid] = { title };
  }
  refreshSessions();
}

// 会话标签编辑：自由文本，逗号分隔；已有标签做成可点小片方便复用
function editSessionTags(s) {
  const existing = s.tags || [];
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">用逗号分隔多个标签（如 <code>工作, 待办</code>）。侧栏会按标签分组，
    点分组头可只看那组；留空则取消分组。最多 12 个，每个不超过 24 字。</p>
    <input id="s-tags-input" class="modal-input" autocomplete="off"
      placeholder="工作, 学习" value="${escapeHtml(existing.join(", "))}">
    <div id="s-tags-known" class="s-tags-known"></div>`;
  // 已用过的标签（来自当前项目全部会话）：点一下追加，不用每次手敲
  request("session.tags_list").then((r) => {
    const el = box.querySelector("#s-tags-known");
    const input = box.querySelector("#s-tags-input");
    const pool = (r.tags || []).map((t) => t.tag).filter((t) => !existing.includes(t));
    if (!pool.length) return;
    el.innerHTML = '<span class="dim small">已有标签：</span>' +
      pool.map((t) => `<button class="s-tag-pick">${escapeHtml(t)}</button>`).join("");
    el.querySelectorAll(".s-tag-pick").forEach((b) => {
      b.onclick = () => {
        const cur = input.value.trim();
        const sep = cur && !cur.endsWith(",") ? ", " : "";
        input.value = cur + sep + b.textContent;
        b.remove();
      };
    });
  }).catch(() => {});
  showModal("编辑标签", box, async () => {
    const raw = box.querySelector("#s-tags-input").value;
    const tags = raw.split(/[,，]/).map((t) => t.trim()).filter(Boolean);
    const r = await request("session.tags", { id: s.id, tags });
    addNotice(r.tags.length ? `已设置标签：${r.tags.join("、")}` : "已清除标签");
    refreshSessions();
  }, "保存");
}

// 行内重命名：直接在列表项上变成输入框
function startInlineRename(s, li) {
  if (!li || !li.isConnected) return;
  const titleEl = li.querySelector(".s-title");
  if (!titleEl) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-inline";
  input.value = s.title || "";
  input.placeholder = "会话名称";
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (save && title && title !== (s.title || "")) {
      try {
        await request("session.rename", { id: s.id, title });
        addNotice(`已重命名为「${title}」`);
      } catch (e) {
        addNotice("重命名失败: " + e.message);
      }
    }
    refreshSessions();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  };
  input.onblur = () => finish(true);
  input.onclick = (e) => e.stopPropagation();
  input.ondblclick = (e) => e.stopPropagation();
}

let modalTriggerEl = null; // 打开弹窗时的焦点来源，关闭时归还

// 弹层内可聚焦元素（Tab 循环用）：过滤禁用/隐藏/不可见
function modalFocusables() {
  const box = document.querySelector("#modal .modal-box");
  if (!box) return [];
  return [...box.querySelectorAll("button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])")]
    .filter((x) => !x.disabled && !x.hidden && x.offsetParent !== null);
}

function showModal(title, contentEl, onOk, okLabel = "确定") {
  modalTriggerEl = document.activeElement; // 记录触发源（可能是 null，hideModal 会判断）
  document.getElementById("modal-title").textContent = title;
  const content = document.getElementById("modal-content");
  content.innerHTML = "";
  content.appendChild(contentEl);
  // 行内报错：设置页里对话区是隐藏的，addNotice 用户看不到
  const err = document.createElement("div");
  err.className = "modal-err";
  err.hidden = true;
  content.appendChild(err);
  const okBtn = document.getElementById("modal-ok");
  okBtn.textContent = okLabel;
  okBtn.disabled = false;
  document.getElementById("modal").classList.remove("hidden");
  // 焦点闭环入口：等一拍再接管——不少调用方打开后会自行聚焦输入框，
  // 焦点仍留在弹层外（纯确认框）时才聚焦「取消」，不与调用方抢
  setTimeout(() => {
    if (modalOpen() && !modalFocusables().includes(document.activeElement)) {
      document.getElementById("modal-cancel").focus();
    }
  }, 0);
  okBtn.onclick = async () => {
    err.hidden = true;
    okBtn.disabled = true;
    try {
      await onOk();
      hideModal();
    } catch (e) {
      err.textContent = "✗ " + e.message;
      err.hidden = false;
    } finally {
      okBtn.disabled = false;
    }
  };
}
function hideModal() {
  document.getElementById("modal").classList.add("hidden");
  // 焦点归还触发源：元素可能已被重渲染移除（contains 判断），失败则静默
  if (modalTriggerEl && document.contains(modalTriggerEl)) {
    try { modalTriggerEl.focus(); } catch (e) {}
  }
  modalTriggerEl = null;
}
document.getElementById("modal-cancel").onclick = hideModal;
// Esc = 点「取消」：走 cancel 按钮的当前语义（首启向导的跳过会记 onboarded 标记），
// 不做隐藏弹层之外的事；菜单/查找条有自己的 Esc 分支，互不影响。
// Tab 在弹层内循环：焦点不会漏到被遮罩的背景内容上
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modalOpen()) {
    e.preventDefault();
    document.getElementById("modal-cancel").click();
    return;
  }
  if (e.key === "Tab" && modalOpen()) {
    const items = modalFocusables();
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    const inBox = items.includes(document.activeElement);
    if (e.shiftKey && (!inBox || document.activeElement === first)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (!inBox || document.activeElement === last)) {
      e.preventDefault();
      first.focus();
    }
  }
});

async function moveSessionModal(s) {
  const { projects } = await request("project.list");
  const box = document.createElement("div");
  const options = projects
    .filter((p) => !p.is_current)
    .map(
      (p) =>
        `<label class="move-opt"><input type="radio" name="move-target" value="${p.id}"> <b>${escapeHtml(p.name)}</b> <span class="dim small">${escapeHtml(p.root_path)}</span></label>`
    )
    .join("");
  box.innerHTML = `
    <p class="dim small">把会话移动到其他项目（移走后可在对应项目的会话列表中找到）：</p>
    <div class="move-list">${options || '<p class="dim small">暂无其他项目——先在其他目录启动一次 SkySheep 即可创建。</p>'}</div>`;
  showModal("移动会话", box, async () => {
    const checked = box.querySelector("input[name=move-target]:checked");
    if (!checked) throw new Error("请先选择目标项目");
    const r = await request("session.move", { id: s.id, project_id: Number(checked.value) });
    if (r.switched_to) {
      addNotice(`会话已移动，已切换到「${r.switched_to.title || "(未命名)"}」`);
    } else {
      addNotice("会话已移动");
    }
    refreshSessions();
  }, "移动");
}

function deleteSessionModal(s) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除会话 <b>「${escapeHtml(s.title || "(未命名)")}」</b> 吗？</p>
    <p class="dim small">该会话的全部消息将被移除，不可恢复。</p>`;
  showModal("删除会话", box, async () => {
    const name = s.title || "(未命名)";
    const r = await request("session.delete", { id: s.id });
    const deadTab = tabFor(s.id);
    if (deadTab) closeTab(deadTab);
    if (r.switched_to) {
      addNotice(`已删除会话「${name}」，已切换到「${r.switched_to.title || "(未命名)"}」`);
    } else if (r.new_active) {
      addNotice(`已删除会话「${name}」，并新建了一个空白会话`);
    } else {
      addNotice(`已删除会话「${name}」`);
    }
    await refreshSessions();
  }, "删除");
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function downloadDataZip(filename, b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const blob = new Blob([bytes], { type: "application/zip" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function setModelChip(text) {
  document.getElementById("model-chip").textContent = text;
}

// ---------- 思考强度：顶栏控件（自动 / 低 / 中 / 高）----------
// 只对声明支持的服务显示；档位存 config.toml 的 provider 字段，切换立即对下一轮生效。
let reasoningState = { supported: false, effort: "auto", labels: {}, efforts: [] };

function setReasoningChip(text) {
  document.getElementById("reasoning-chip").textContent = text;
}

function renderReasoningChip(state) {
  reasoningState = state || { supported: false, effort: "auto", labels: {}, efforts: [] };
  const chip = document.getElementById("reasoning-chip");
  const supported = !!reasoningState.supported;
  chip.classList.toggle("hidden", !supported);
  if (!supported) return;
  const label = (reasoningState.labels || {})[reasoningState.effort] || reasoningState.effort;
  // 徽章只显示档位（不再拼「思考」前缀）；完整语义走 title 悬停提示
  chip.textContent = label;
  chip.title = reasoningState.effort === "auto"
    ? "当前思考强度：自动 · 每轮按任务复杂度实时调整（简单更快更省、复杂加深）· 点击调整"
    : `当前思考强度：${label} · 点击调整（自动 / 低 / 中 / 高）`;
  chip.classList.toggle("weak", reasoningState.effort === "auto");
}

async function refreshReasoning() {
  const r = await request("model.reasoning").catch(() => null);
  if (r) renderReasoningChip(r.reasoning);
}

const reasoningMenu = document.getElementById("reasoning-menu");

function hideReasoningMenu() {
  reasoningMenu.classList.add("hidden");
}

async function toggleReasoningMenu(e) {
  if (e) e.stopPropagation();
  if (!reasoningMenu.classList.contains("hidden")) return hideReasoningMenu();
  const chip = document.getElementById("reasoning-chip");
  const r = chip.getBoundingClientRect();
  reasoningMenu.innerHTML = "";
  const efforts = reasoningState.efforts && reasoningState.efforts.length
    ? reasoningState.efforts : ["auto", "low", "medium", "high"];
  const desc = {
    auto: "按任务复杂度实时调整",
    low: "更快、更省，适合简单问答与格式整理",
    medium: "兼顾速度与深度，适合多数开发任务",
    high: "深入推敲，适合复杂调试与方案设计",
  };
  efforts.forEach((eff) => {
    const b = document.createElement("button");
    b.className = "rp-mini" + (eff === reasoningState.effort ? " primary" : "");
    b.style.display = "block";
    b.style.width = "100%";
    b.textContent = `${(reasoningState.labels || {})[eff] || eff} · ${desc[eff] || ""}`;
    b.onclick = async () => {
      hideReasoningMenu();
      try {
        const res = await request("model.set_reasoning", { effort: eff });
        renderReasoningChip(res.reasoning);
        const label = (res.reasoning.labels || {})[eff] || eff;
        addNotice(`思考强度已设为「${label}」，下一轮对话生效`);
      } catch (err) {
        addNotice("设置失败: " + err.message);
      }
    };
    reasoningMenu.appendChild(b);
  });
  reasoningMenu.classList.remove("hidden");
  // 徽章在屏幕底部的输入区工具条里：向上弹（顺带补上与模型菜单同一套 uiScale 换算）
  reasoningMenu.style.top = "auto";
  reasoningMenu.style.bottom = (window.innerHeight / uiScale - r.top / uiScale + 6) + "px";
  reasoningMenu.style.left = Math.max(8, r.left / uiScale) + "px";
}

document.getElementById("reasoning-chip").onclick = toggleReasoningMenu;
document.addEventListener("click", (e) => {
  if (!reasoningMenu.classList.contains("hidden") &&
      !reasoningMenu.contains(e.target) &&
      e.target.id !== "reasoning-chip") hideReasoningMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideReasoningMenu();
});
window.addEventListener("resize", hideReasoningMenu);
const modelChip = document.getElementById("model-chip");
let curProviderName = "", curModelName = ""; // 当前使用的服务/模型，boot() 与切换成功后更新
// 当前模型能不能看图。False 时贴图/截图会先给提示，避免发出去才收到上游报错
let curSupportsVision = true;

// ---------- 顶栏模型菜单：对话页直接切换已启用的模型（对标 ChatGPT/Claude 模型选择器） ----------
const modelMenu = document.getElementById("model-menu");
let modelMenuTab = "preset"; // 菜单当前页签：preset=默认（内置预设）、custom=自定义（用户新增）
let defaultModelPref = { provider: "", model: "", label: "" }; // 新会话默认模型（Shift+点击行设置）

function hideModelMenu() {
  modelMenu.classList.add("hidden");
  modelMenu.innerHTML = "";
}

function buildModelRows(detail) {
  // 已启用的服务 × 各自已启用的模型 = 可切换清单（disabled 名单里的不会出现在 detail.providers）
  const rows = [];
  for (const [name, p] of Object.entries(detail.providers)) {
    for (const m of p.models || []) {
      rows.push({
        name, model: m, hasKey: p.has_key,
        label: p.label || name, // 界面显示名（预设如「智谱」「小米 Mimo」）
        active: p.is_active && m === (p.active_model || p.model),
        preset: !!p.is_preset, // 内置预设归「默认」，用户新增的归「自定义」
      });
    }
  }
  return rows;
}

async function toggleModelMenu(e) {
  if (e) e.stopPropagation(); // 别让随后冒泡到 document 的 click 立刻把菜单关掉
  if (!modelMenu.classList.contains("hidden")) return hideModelMenu();
  // 先同步展开「加载中」，再等数据填充——点击立刻有反馈，也避免异步间隙被关掉
  modelMenu.innerHTML = '<div class="mm-empty">加载中…</div>';
  modelMenu.classList.remove("hidden");
  const chipR = modelChip.getBoundingClientRect(); // 物理像素，下同
  // 徽章现在住在屏幕底部的输入区工具条里：菜单向上弹（top 置 auto，用 bottom 贴徽章上沿）
  const vh = window.innerHeight / uiScale;
  modelMenu.style.top = "auto";
  modelMenu.style.bottom = (vh - chipR.top / uiScale + 6) + "px";
  modelMenu.style.left = Math.max(8, chipR.right / uiScale - 250) + "px";
  const detail = await request("config.providers").catch(() => null);
  if (!detail || modelMenu.classList.contains("hidden")) return; // 请求失败或期间已被关掉
  // 新会话默认模型（★ 行标记用）：拉取失败不阻塞菜单
  defaultModelPref = await request("default_model.get").catch(() => defaultModelPref) || defaultModelPref;
  const rows = buildModelRows(detail);
  // 两页：内置预设 = 默认，用户新增 = 自定义（页签沿用侧栏搜索范围的分段控件样式）
  const groups = [
    {
      tab: "preset", label: "默认", rows: rows.filter((r) => r.preset),
      empty: "内置服务都已停用，可在管理页恢复",
    },
    {
      tab: "custom", label: "自定义", rows: rows.filter((r) => !r.preset),
      empty: "还没有自定义服务，点下方「管理模型服务」添加",
    },
  ];
  const addRow = (row) => {
    const b = document.createElement("button");
    b.className = "mm-item" + (row.active ? " active" : "");
    b.innerHTML =
      `<span class="mm-model">${row.active ? "✓ " : ""}${escapeHtml(row.model)}</span>` +
      `<span class="mm-prov${row.hasKey ? "" : " no-key"}">${row.hasKey ? "" : "⚠ "}${escapeHtml(row.label)}</span>` +
      (defaultModelPref.provider === row.name && defaultModelPref.model === row.model
        ? `<span class="mm-def" title="新会话将默认使用这个模型">★ 新会话默认</span>` : "");
    b.title = row.hasKey
      ? `切换到 ${row.label} / ${row.model}（点击）；Shift+点击 设为新会话默认`
      : `「${row.label}」还没配置 API Key，切换过去会失败`;
    b.onclick = async (ev) => {
      // Shift+点击：把这一行设为「新会话默认模型」（不改当前对话）
      if (ev.shiftKey) {
        try {
          const r = await request("default_model.set", { name: row.name, model: row.model });
          defaultModelPref = r;
          addNotice(`✓ 已设为新会话默认：${r.label}（当前对话不变）`);
          paint();
        } catch (err) {
          addNotice("设置失败: " + err.message);
        }
        return;
      }
      hideModelMenu();
      try {
        const r = await request("model.switch", { name: row.name, model: row.model });
        curProviderName = r.provider;
        curModelName = r.model;
        if (r.supports_vision !== undefined) curSupportsVision = r.supports_vision !== false;
        hideBanner();
        setModelChip(`${r.provider}/${r.model}`);
        addNotice(`✓ 已切换到「${r.provider} / ${r.model}」，当前对话立即生效`);
        if (pendingImages.length && !curSupportsVision) {
          addNotice(`提示：该模型不支持图片输入，输入框里已贴的 ${pendingImages.length} 张图发不出去，` +
            "请换一个多模态模型或先移除图片。");
        }
        refreshReasoning();
      } catch (err) {
        addNotice("切换失败: " + err.message);
      }
    };
    modelMenu.appendChild(b);
  };
  // 整页重画（页签 + 当前列表 + 管理入口），切页时数据已在手，无需重新请求
  const paint = () => {
    modelMenu.innerHTML = "";
    const seg = document.createElement("div");
    seg.className = "seg-row seg-mini mm-seg";
    for (const g of groups) {
      const t = document.createElement("button");
      t.textContent = g.label;
      t.classList.toggle("active", modelMenuTab === g.tab);
      t.onclick = (ev) => {
        ev.stopPropagation(); // 别让冒泡到 document 的 click 把菜单关掉
        if (modelMenuTab === g.tab) return;
        modelMenuTab = g.tab;
        paint();
      };
      seg.appendChild(t);
    }
    modelMenu.appendChild(seg);
    const cur = groups.find((g) => g.tab === modelMenuTab) || groups[0];
    if (!cur.rows.length) {
      const hint = document.createElement("div");
      hint.className = "mm-empty";
      hint.textContent = cur.empty;
      modelMenu.appendChild(hint);
    }
    cur.rows.forEach(addRow);
    const manage = document.createElement("button");
    manage.className = "mm-manage";
    manage.textContent = "⚙ 管理模型服务…";
    manage.title = "到设置里添加服务、启用/停用模型、配置 API Key";
    manage.onclick = () => {
      hideModelMenu();
      openSettings("providers");
    };
    modelMenu.appendChild(manage);
    // 内容替换后按实际宽度重新定位：贴着模型徽章上方，右缘对齐徽章（不够放时往左收）
    const mw = modelMenu.offsetWidth; // 布局像素，无需换算
    modelMenu.style.bottom = (window.innerHeight / uiScale - chipR.top / uiScale + 6) + "px";
    modelMenu.style.left =
      Math.max(8, Math.min(chipR.right / uiScale - mw, window.innerWidth / uiScale - mw - 8)) + "px";
  };
  paint();
}
modelChip.style.cursor = "pointer";
modelChip.title = "当前模型 · 点击切换已启用的模型";
modelChip.onclick = toggleModelMenu;
document.addEventListener("click", (e) => {
  if (!modelMenu.classList.contains("hidden") &&
      !modelMenu.contains(e.target) && !modelChip.contains(e.target)) hideModelMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideModelMenu();
});
window.addEventListener("resize", hideModelMenu);

// actions: [{ label, onClick }]，最多两个——横幅是复用单例，按钮位固定、
// 标签与回调每次随调用重写，用不到的位隐藏（不能把上一次的回调留在 DOM 上）
function showBanner(text, actions) {
  document.getElementById("cfg-banner-text").textContent = text;
  const box = document.getElementById("cfg-banner-actions");
  const slots = [
    document.getElementById("banner-trust"),
    document.getElementById("banner-details"),
  ];
  const list = Array.isArray(actions) ? actions : [];
  list.slice(0, slots.length).forEach((a, i) => {
    slots[i].textContent = a.label;
    slots[i].onclick = a.onClick;
    slots[i].hidden = false;
  });
  slots.slice(list.length).forEach((b) => {
    b.hidden = true;
    b.onclick = null;
  });
  box.hidden = !list.length;
  document.getElementById("cfg-banner").hidden = false;
}
function hideBanner() {
  document.getElementById("cfg-banner").hidden = true;
  document.getElementById("cfg-banner-actions").hidden = true;
}
document.getElementById("cfg-banner-close").onclick = hideBanner;

// ---------- workspace trust：项目自带配置在确认前不生效 ----------
// 打开一个带 .skysheep/ 的目录时，横幅提示「这个项目想执行什么」；信任按项目记忆，
// 配置内容一变（指纹不同）会重新询问。后端在未信任时不读取项目级 mcp.json、
// 也不发现项目级技能（见 security/trust.py）。
function renderTrustBanner(trust) {
  if (!trust || trust.state !== "pending") return false;
  const items = trust.items || [];
  const names = items.slice(0, 3).map((i) => i.name).join("、");
  const more = items.length > 3 ? ` 等 ${items.length} 项` : "";
  const lead = trust.changed
    ? "⚠ 本项目自带的配置有变化，需要重新确认："
    : "⚠ 本项目自带了会被自动执行的配置：";
  showBanner(
    `${lead}${names}${more}（确认前不会启用）`,
    [
      {
        label: "信任本项目",
        onClick: async () => {
          try {
            await request("trust.grant");
            hideBanner();
            boot();
          } catch (e) {
            showBanner("✗ 信任失败：" + e.message);
          }
        },
      },
      { label: "看详情", onClick: () => showTrustDetails(trust) },
    ],
  );
  return true;
}

function showTrustDetails(trust) {
  const rows = (trust.items || []).map((i) => {
    const label = i.kind === "mcp" ? "MCP 服务器" : "技能";
    const ro = i.readonly ? "（其工具会自动放行）" : "";
    return `<div class="ob-row"><span class="ob-name">${escapeHtml(i.name)}</span>`
         + `<span class="dim small ob-url">${escapeHtml(label)}：${escapeHtml(i.detail)}${ro}</span></div>`;
  }).join("");
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">这些内容来自当前项目目录下的 <b>.skysheep/</b>，随仓库一起分发。
    信任后它们会自动生效：MCP 服务器会被启动（其中的本地命令会立即执行），
    技能描述会拼进系统提示词。如果这个仓库不是你自己的、也不是你确认过来源的，建议先不要信任。</p>
    <div class="ob-list">${rows || '<div class="dim small">（没有可展示的条目）</div>'}</div>
    <p class="dim small">信任按项目路径记忆；这些文件的内容一旦变化，需要重新确认。</p>`;
  // 已经信任时只作展示；待确认时给出确认按钮（两处入口：顶部横幅、设置页）
  if (!trust || trust.state !== "pending") {
    showModal("本项目自带的配置", box, async () => {});
    return;
  }
  // 直接用弹窗自带的确认按钮，不另外加一个同义的按钮（截图里两个蓝按钮会让人不知道该点哪个）
  showModal("本项目自带的配置", box, async () => {
    await request("trust.grant");
    renderTrustState();
    boot();
  }, "信任本项目");
}

/** 设置 · 安全与后台里的信任状态行：当前项目已信任/待确认，并提供收回/确认入口。 */
async function renderTrustState() {
  const el = document.getElementById("adv-trust-state");
  if (!el) return;
  let trust;
  try {
    trust = await request("trust.status");
  } catch (e) {
    el.textContent = "读取失败：" + e.message;
    return;
  }
  if (!trust || trust.state === "clean") {
    el.textContent = "本项目没有自带配置（无可信任之物）";
    return;
  }
  if (trust.state === "trusted") {
    el.innerHTML = '<span style="color:var(--ok,#2e7d32)">✓ 已信任本项目</span> ';
    const un = document.createElement("button");
    un.className = "btn-ghost";
    un.textContent = "收回信任";
    un.title = "收回后项目自带的 MCP 服务器会被断开，项目级技能不再生效";
    un.onclick = async () => {
      try {
        await request("trust.revoke");
        renderTrustState();
      } catch (e) {
        el.textContent = "✗ " + e.message;
      }
    };
    el.appendChild(un);
    return;
  }
  // pending：项目带配置但未确认
  el.innerHTML = '<span style="color:var(--gold)">⚠ 待确认</span> ';
  const go = document.createElement("button");
  go.className = "btn-ghost";
  go.textContent = "看详情并信任";
  go.onclick = () => showTrustDetails(trust);
  el.appendChild(go);
}

// ---------- 用量统计（设置页 · 仪表盘布局：统计磁贴 / Token 活动柱状图 / 模型用量环形图 / 会话排行） ----------
let usageRangeDays = 14; // 范围 tabs 记忆在本窗口内
let usageChartType = "bar"; // 图表类型：bar=柱状（默认）| line=折线；偏好存 ui.json 的 usage_chart

function usageFmtTokens(n) {
  n = n || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function usageTiles(r) {
  const days = r.by_day || [];
  const total = (r.total_in || 0) + (r.total_out || 0);
  const peak = Math.max(0, ...days.map((d) => (d.it || 0) + (d.ot || 0)));
  // 峰值落在哪天：多天窗口里「峰值=累计」没有信息量，日期才有
  const peakDay = days.reduce(
    (best, d) => (!best || (d.it || 0) + (d.ot || 0) > (best.it || 0) + (best.ot || 0) ? d : best),
    null,
  );
  const tile = (num, label, sub) =>
    `<div class="usage-tile"><div class="ut-num">${num}</div>` +
    `<div class="ut-label">${label}</div>` +
    (sub ? `<div class="ut-sub">${sub}</div>` : "") + `</div>`;
  const totalSub = `输入 ${usageFmtTokens(r.total_in)} · 输出 ${usageFmtTokens(r.total_out)}` +
    ((r.total_cached || 0) > 0 ? ` · 缓存命中 ${usageFmtTokens(r.total_cached)}` : "");
  // 真实去重会话数（后端 COUNT(DISTINCT session_id)）：无项目态 by_session
  // 被隐藏、项目态它被截到 12 条，这两种情况 length 都不是准确口径
  const sessionCount = r.session_count != null ? r.session_count : (r.by_session || []).length;
  return (
    tile(usageFmtTokens(total), "累计 Token 数", totalSub) +
    tile(usageFmtTokens(peak), "单日峰值 Token", peakDay ? `峰值在 ${escapeHtml(String(peakDay.day).slice(5))}` : "") +
    tile(String(days.reduce((s, d) => s + ((d.it || 0) + (d.ot || 0) > 0 ? 1 : 0), 0)) + " 天", "有记录天数", `统计窗口 ${usageRangeDays} 天`) +
    tile(String(sessionCount), "参与会话", (r.by_session || []).length ? "按 tokens 排行见下方" : "")
  );
}

function usageTrendChart(r) {
  const days = (r.by_day || []).slice().reverse(); // 按时间正序
  const lg = document.getElementById("usage-trend-legend");
  if (!days.length) {
    if (lg) lg.innerHTML = "";
    return '<p class="dim small" style="padding:12px">统计窗口内还没有用量记录——对话几轮后回来看。</p>';
  }
  const maxDay = Math.max(1, ...days.map((d) => (d.it || 0) + (d.ot || 0)));
  // 日期标签最多显示 10 个（窄卡不挤）
  const step = Math.max(1, Math.ceil(days.length / 10));
  const showDate = (i) => i % step === 0 || i === days.length - 1;
  let body;
  if (usageChartType === "line") {
    // 折线图：SVG 只画线与点（preserveAspectRatio=none 拉满容器宽），
    // 日期标签用 HTML 行均分——SVG 内文字会被非等比拉伸变形，不能放里面。
    // 点与标签都取「居中均分」坐标 (i+0.5)/n，与柱状图的列位置一致
    const W = 1000, H = 200, PAD = 12;
    const n = days.length;
    const px = (i) => ((i + 0.5) / n) * W;
    const py = (v) => H - PAD - (v / maxDay) * (H - 2 * PAD);
    const poly = (key) => days.map((d, i) => `${px(i).toFixed(1)},${py(d[key] || 0).toFixed(1)}`).join(" ");
    // 只有一个数据点时没有线可画：把孤点放大，别让窗口里只剩两粒看不清的点
    const dotR = days.length === 1 ? 8 : 3.5;
    const dots = (key, label) => days.map((d, i) =>
      `<circle class="ut-pt ${key === "it" ? "in" : "out"}" cx="${px(i).toFixed(1)}" cy="${py(d[key] || 0).toFixed(1)}" r="${dotR}">` +
      `<title>${escapeHtml(d.day)} · ${label} ${usageFmtTokens(d[key] || 0)}</title></circle>`).join("");
    body =
      '<div class="ut-line">' +
      `<svg class="ut-line-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">` +
      `<line class="ut-grid" x1="0" y1="${py(maxDay).toFixed(1)}" x2="${W}" y2="${py(maxDay).toFixed(1)}"></line>` +
      `<line class="ut-grid" x1="0" y1="${py(maxDay / 2).toFixed(1)}" x2="${W}" y2="${py(maxDay / 2).toFixed(1)}"></line>` +
      `<polyline class="ut-line-in" points="${poly("it")}"></polyline>` +
      `<polyline class="ut-line-out" points="${poly("ot")}"></polyline>` +
      dots("it", "输入") + dots("ot", "输出") +
      "</svg>" +
      '<div class="ut-line-dates">' +
      days.map((d, i) =>
        `<span class="ut-date">${showDate(i) ? escapeHtml(String(d.day).slice(5)) : ""}</span>`).join("") +
      "</div></div>";
  } else {
    const bars = days.map((d, i) => {
      const it = d.it || 0, ot = d.ot || 0;
      const inH = Math.round((it / maxDay) * 100), outH = Math.round((ot / maxDay) * 100);
      const label = showDate(i)
        ? `<span class="ut-date">${escapeHtml(String(d.day).slice(5))}</span>`
        : '<span class="ut-date"></span>';
      return `<div class="ut-col" title="${escapeHtml(d.day)} · 输入 ${usageFmtTokens(it)} · 输出 ${usageFmtTokens(ot)}">` +
        `<div class="ut-bar"><i class="ut-in" style="height:${inH}%"></i><i class="ut-out" style="height:${outH}%"></i></div>${label}</div>`;
    }).join("");
    body = `<div class="ut-chart">${bars}</div>`;
  }
  if (lg) {
    lg.innerHTML =
      '<span class="lg-dot in"></span>输入 tokens' +
      '<span class="lg-dot out"></span>输出 tokens' +
      `<span class="lg-total">合计 ${usageFmtTokens((r.total_in || 0) + (r.total_out || 0))}</span>`;
  }
  return body;
}

function usageDonut(r) {
  const rows = (r.by_provider || []).filter((p) => (p.it || 0) + (p.ot || 0) > 0);
  const el = document.getElementById("usage-models");
  if (!rows.length) {
    el.innerHTML = '<p class="dim small" style="padding:12px">还没有按服务的用量记录。</p>';
    document.getElementById("usage-cost").textContent = "";
    return;
  }
  const total = rows.reduce((s, p) => s + (p.it || 0) + (p.ot || 0), 0) || 1;
  // 环形图分段（stroke-dasharray）；色板走 CSS 变量 --dm-c1..8（app.css 定义、
  // 各主题各自调色——深色主题要提亮，写死色值会在夜墨下看不清）
  const color = (i) => `var(--dm-c${(i % 8) + 1})`;
  const R = 54, C = 2 * Math.PI * R;
  let offset = 0;
  const segs = rows.map((p, i) => {
    const frac = ((p.it || 0) + (p.ot || 0)) / total;
    const seg = `<circle class="dm-seg" cx="70" cy="70" r="${R}" fill="none"
      style="stroke:${color(i)}" stroke-width="20"
      stroke-dasharray="${(frac * C).toFixed(2)} ${(C - frac * C).toFixed(2)}"
      stroke-dashoffset="${(-offset * C).toFixed(2)}"></circle>`;
    offset += frac;
    return seg;
  }).join("");
  const legend = rows.map((p, i) => {
    const t = (p.it || 0) + (p.ot || 0);
    const pct = Math.round((t / total) * 100);
    const name = (p.model || p.provider || "?");
    return `<div class="dm-row"><span class="lg-dot" style="background:${color(i)}"></span>` +
      `<span class="dm-name" title="${escapeHtml(p.provider || "")} / ${escapeHtml(name)}">${escapeHtml(name)}</span>` +
      `<span class="dm-val">${usageFmtTokens(t)} tokens</span>` +
      `<span class="dm-pct">${pct}%</span></div>`;
  }).join("");
  el.innerHTML =
    `<div class="dm-chart"><svg viewBox="0 0 140 140" role="img">` +
    `<circle cx="70" cy="70" r="${R}" fill="none" stroke="var(--paper-sunken)" stroke-width="20"></circle>${segs}` +
    `</svg><div class="dm-center"><b>${usageFmtTokens(total)}</b><span>tokens</span></div></div>` +
    `<div class="dm-legend">${legend}</div>`;
  document.getElementById("usage-cost").textContent =
    r.has_price ? `估算费用 ¥${(r.cost || 0).toFixed(2)}` : "";
}

// 每日预算与当日用量（预算在 设置 · 高级 · 运行参数）：用量页给出现在离上限多远，
// 达到上限时标黄点破「为什么 Agent 不发新消息」
function renderUsageBudget(r) {
  const el = document.getElementById("usage-budget");
  if (!el) return;
  const today = r.today || 0;
  if (r.budget > 0) {
    const over = today >= r.budget;
    // 「今日已用」是全局口径（预算护栏是全局设置），页面上其余数字在项目态
    // 只算当前项目——不标注的话两个数并排会被当成同一口径比较
    el.textContent = `今日已用 ${usageFmtTokens(today)} tokens（全局）· 预算 ${usageFmtTokens(r.budget)}` +
      (over ? " · 已达上限，Agent 暂停发送新消息（明天自动重置）" : "") +
      " · 在 高级 · 运行参数 里调整";
    el.classList.toggle("warn", over);
  } else {
    el.textContent = `今日已用 ${usageFmtTokens(today)} tokens（全局）· 未设每日预算（可在 高级 · 运行参数 里设置上限）`;
    el.classList.remove("warn");
  }
  el.hidden = false;
}

async function loadUsage() {
  try {
    const r = await request("usage.stats", { days: usageRangeDays });
    document.getElementById("usage-tiles").innerHTML = usageTiles(r);
    renderUsageBudget(r);
    // 空态不要撑出 220px 的死空间：有记录才保留图表高度
    const trendEl = document.getElementById("usage-trend");
    trendEl.classList.toggle(
      "empty",
      !(r.by_day || []).some((d) => (d.it || 0) + (d.ot || 0) > 0),
    );
    trendEl.innerHTML = usageTrendChart(r);
    usageDonut(r);
    document.getElementById("usage-sessions").innerHTML = (r.by_session || [])
      .map((s) => {
        const total = (s.it || 0) + (s.ot || 0);
        // sid 为空的行（子代理用量归属未知时记空串）点不开，不加 link 样式
        const cls = s.sid ? "usage-row link" : "usage-row";
        return `<div class="${cls}"${s.sid ? ` data-sid="${escapeHtml(s.sid)}"` : ""}>` +
          `<span class="ud" title="${escapeHtml(s.title || "未命名")}">${escapeHtml((s.title || "未命名").slice(0, 22))}</span>` +
          `<span class="un">${usageFmtTokens(total)} tokens</span></div>`;
      }).join("") || "<p class='dim small'>暂无数据</p>";
  } catch (e) {
    document.getElementById("usage-tiles").innerHTML =
      `<p class="dim small">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}
document.getElementById("usage-refresh").onclick = () => loadUsage();
// 会话排行点击下钻：打开对应会话（委托绑定，列表随刷新整体重画）
document.getElementById("usage-sessions").addEventListener("click", (e) => {
  const row = e.target.closest(".usage-row.link");
  if (!row || !row.dataset.sid) return;
  const label = row.querySelector(".ud")?.textContent || "";
  openTabForSession(row.dataset.sid, label);
});
document.querySelectorAll("#usage-range-tabs button").forEach((b) => {
  b.onclick = () => {
    usageRangeDays = Number(b.dataset.days) || 14;
    document.querySelectorAll("#usage-range-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    loadUsage();
  };
});
// 图表类型切换（柱状 / 折线）：即时重渲染，偏好落 ui.json（usage_chart）
document.querySelectorAll("#usage-chart-tabs button").forEach((b) => {
  b.onclick = () => {
    usageChartType = b.dataset.chart === "line" ? "line" : "bar";
    document.querySelectorAll("#usage-chart-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    saveUiPrefs({ usage_chart: usageChartType });
    loadUsage();
  };
});

// ---------- 提示词：自定义提示词模板（输入 ~ 时展示；原名「快捷指令」，2.0 起更名并换触发符号） ----------
let snippetsCache = [];

// 内置示例模板（内容定义在后端 BUILTIN_SNIPPETS，首启已落成真实记录、可在设置页
// 编辑/删除）：只在用户把指令删光时给 / 菜单兜底，随 snippets.list 响应下发。
let builtinSnippetsCache = [];

function builtinSnippets() {
  return snippetsCache.length ? [] : builtinSnippetsCache;
}

async function loadSnippets() {
  try {
    const r = await request("snippets.list");
    snippetsCache = r.snippets || [];
    builtinSnippetsCache = r.builtin || builtinSnippetsCache;
  } catch (e) { snippetsCache = []; }
  if (document.getElementById("snippets-list")) renderSnippets();
}

function renderSnippets() {
  const ul = document.getElementById("snippets-list");
  const st = document.getElementById("snippets-status");
  ul.innerHTML = "";
  // 状态行：条数 + 累计使用次数（还没有任何使用记录时省略后半句）
  if (st) {
    if (snippetsCache.length) {
      const used = snippetsCache.reduce((n, s) => n + (s.use_count || 0), 0);
      st.textContent = `共 ${snippetsCache.length} 条` + (used ? ` · 累计使用 ${used} 次` : "");
      st.hidden = false;
    } else {
      st.hidden = true;
    }
  }
  if (!snippetsCache.length) {
    ul.innerHTML = '<li class="dim small" style="padding:6px 10px">还没有自定义提示词 —— 点右上角「＋ 新建」，' +
      '或点「恢复示例」把内置示例加回来；输入 ~ 时可先用内置示例模板。</li>';
    return;
  }
  snippetsCache.forEach((s) => {
    const li = document.createElement("li");
    li.draggable = true;
    li.dataset.sid = String(s.id);
    li.title = "点行编辑；按住行首 ⋮⋮ 拖动调整顺序（列表顺序就是 ~ 候选顺序）";
    const handle = document.createElement("span");
    handle.className = "s-drag";
    handle.textContent = "⋮⋮";
    li.appendChild(handle);
    const title = document.createElement("span");
    title.className = "s-title";
    title.textContent = s.name;
    const sub = document.createElement("span");
    sub.className = "s-sub";
    sub.textContent = (s.content || "").replace(/\s+/g, " ").slice(0, 60) +
      (s.use_count ? ` · 用过 ${s.use_count} 次` : "");
    li.append(title, sub);
    const ops = document.createElement("span");
    ops.className = "cron-ops";
    ops.innerHTML = `<button class="cron-op" title="复制模板原文">⧉</button>` +
      `<button class="cron-op" title="编辑">✎</button>` +
      `<button class="cron-op danger" title="删除">✕</button>`;
    const [copy, edit, del] = ops.querySelectorAll("button");
    copy.onclick = async (e) => {
      e.stopPropagation();
      await copyTextToClipboard(s.content || "");
      copy.textContent = "✓";
      setTimeout(() => { copy.textContent = "⧉"; }, 1200);
    };
    edit.onclick = (e) => { e.stopPropagation(); snippetModal(s); };
    del.onclick = async (e) => {
      e.stopPropagation();
      try { await request("snippets.delete", { id: s.id }); }
      catch (err) { addNotice("删除失败: " + err.message); }
      await loadSnippets(); // 失败也重拉一次，列表与后端保持一致
    };
    li.appendChild(ops);
    li.onclick = () => snippetModal(s);
    ul.appendChild(li);
  });
  enableSnippetDrag(ul);
}

// 拖拽排序：拖动行插到目标行前/后（按中线判定），松手后把新顺序整体上报。
// 顺序落库后，~ 候选顺序与设置页列表一致。
function enableSnippetDrag(ul) {
  let dragEl = null;
  ul.querySelectorAll("li[data-sid]").forEach((li) => {
    li.ondragstart = (e) => {
      dragEl = li;
      li.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", li.dataset.sid || "");
      }
    };
    li.ondragover = (e) => {
      if (!dragEl || dragEl === li) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      const r = li.getBoundingClientRect();
      ul.insertBefore(dragEl, e.clientY < r.top + r.height / 2 ? li : li.nextSibling);
    };
    li.ondrop = (e) => e.preventDefault();
    li.ondragend = () => {
      li.classList.remove("dragging");
      dragEl = null;
      const ids = [...ul.querySelectorAll("li[data-sid]")]
        .map((x) => Number(x.dataset.sid)).filter(Boolean);
      // 本地缓存同步成新顺序，避免下次重渲染回跳
      const byId = new Map(snippetsCache.map((s) => [String(s.id), s]));
      snippetsCache = ids.map((i) => byId.get(String(i))).filter(Boolean);
      request("snippets.reorder", { ids }).catch(() => {});
    };
  });
}

function snippetModal(existing, presetContent) {
  const box = document.createElement("div");
  const initial = existing ? existing.content : (presetContent || "");
  box.innerHTML = `
    <div class="cron-fields">
      <label>名称（输入 ~ 时显示）</label>
      <input id="sn-name" class="modal-input" type="text" maxlength="40" placeholder="例如：代码审查" value="${existing ? escapeHtml(existing.name) : ""}">
      <label>内容（占位符在插入时替换）</label>
      <textarea id="sn-content" class="modal-input" rows="5" placeholder="请审查以下代码，关注正确性与边界情况：&#10;{{clipboard}}">${escapeHtml(initial)}</textarea>
      <div class="sn-chips">
        <span class="dim small">点按插入占位符：</span>
        <button type="button" class="sn-chip" data-ph="{{clipboard}}">{{clipboard}} 剪贴板</button>
        <button type="button" class="sn-chip" data-ph="{{date}}">{{date}} 日期</button>
        <button type="button" class="sn-chip" data-ph="{{time}}">{{time}} 时间</button>
        <button type="button" class="sn-chip" data-ph="{{project}}">{{project}} 项目名</button>
      </div>
    </div>`;
  // 占位符 chips：插到光标处（不替换已有内容）
  const ta = box.querySelector("#sn-content");
  box.querySelectorAll(".sn-chip").forEach((b) => {
    b.onclick = () => {
      const ph = b.dataset.ph;
      const start = ta.selectionStart ?? ta.value.length;
      const end = ta.selectionEnd ?? start;
      ta.value = ta.value.slice(0, start) + ph + ta.value.slice(end);
      ta.focus();
      ta.setSelectionRange(start + ph.length, start + ph.length);
    };
  });
  showModal(existing ? "编辑提示词" : "新建提示词", box, async () => {
    const name = box.querySelector("#sn-name").value.trim();
    const content = box.querySelector("#sn-content").value.trim();
    if (!name || !content) throw new Error("名称与内容不能为空");
    if (existing) await request("snippets.update", { id: existing.id, name, content });
    else await request("snippets.add", { name, content });
    await loadSnippets();
  }, existing ? "保存" : "创建");
}
document.getElementById("btn-snippet-add").onclick = () => snippetModal(null);

// 消息「存为提示词」入口：用消息文本预填新建弹窗（名称留空，由用户起名）
function saveAsSnippet(text) {
  const t = (text || "").trim();
  if (!t) { addNotice("这条消息没有可保存的文本"); return; }
  snippetModal(null, t.slice(0, 8000));
}

// 导出：与白名单导出同一套下载方式（Blob 触发浏览器/窗口下载）
document.getElementById("btn-snippets-export").onclick = async () => {
  try {
    const data = await request("snippets.export");
    if (!(data.snippets || []).length) { addNotice("还没有可导出的提示词"); return; }
    downloadJson("skysheep-snippets.json", data);
  } catch (e) { addNotice("导出失败: " + e.message); }
};

// 导入：原生窗口弹文件选择框；浏览器模式退回粘贴导出 JSON
document.getElementById("btn-snippets-import").onclick = async () => {
  const done = async (res) => {
    addNotice(`已导入 ${res.added} 条提示词` +
      (res.skipped ? `，跳过 ${res.skipped} 条（重复或无效）` : ""));
    await loadSnippets();
  };
  if (nativePickerAvailable()) {
    const r = await pickPath("json");
    if (r.error) { addNotice("打开文件选择器失败: " + r.error); return; }
    const path = (r.paths || [])[0];
    if (!path) return; // 用户取消
    try { await done(await request("snippets.import", { path })); }
    catch (e) { addNotice("导入失败: " + e.message); }
    return;
  }
  const box = document.createElement("div");
  box.innerHTML = `<p>粘贴提示词导出文件（<code>skysheep-snippets.json</code>）的内容。</p>
    <textarea id="sn-import-text" class="rule-import-text" rows="6"
      placeholder='{"format":"skysheep-snippets","version":1,"snippets":[{"name":"…","content":"…"}]}'></textarea>
    <p class="dim small">合并导入：名称与内容都相同的条目会跳过，不影响现有提示词。</p>`;
  showModal("导入提示词", box, async () => {
    const raw = box.querySelector("#sn-import-text").value.trim();
    if (!raw) throw new Error("请先粘贴 JSON 内容");
    await done(await request("snippets.import", { data: raw }));
  }, "导入");
};

// 恢复内置示例：按名称去重加回（删过的示例可主动找回）
document.getElementById("btn-snippets-restore").onclick = async () => {
  try {
    const r = await request("snippets.restore_builtin");
    addNotice(r.added ? `已恢复 ${r.added} 条内置示例` : "内置示例都已在列表里，无需恢复");
    await loadSnippets();
  } catch (e) { addNotice("恢复失败: " + e.message); }
};

// 插入到输入框：占位符替换（剪贴板不可用时原样保留）
async function insertSnippet(s) {
  let content = s.content || "";
  if (content.includes("{{clipboard}}")) {
    try {
      const t = await navigator.clipboard.readText();
      if (t) content = content.split("{{clipboard}}").join(t);
    } catch (e) { /* 剪贴板不可用：保留占位符 */ }
  }
  // 本地占位符：日期 / 时间 / 当前项目名（同步替换）
  if (content.includes("{{date}}") || content.includes("{{time}}") || content.includes("{{project}}")) {
    const now = new Date();
    const p2 = (n) => String(n).padStart(2, "0");
    content = content.split("{{date}}").join(
      `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())}`);
    content = content.split("{{time}}").join(`${p2(now.getHours())}:${p2(now.getMinutes())}`);
    content = content.split("{{project}}").join(currentProjectName() || "当前项目");
  }
  const input = document.getElementById("input");
  input.value = (input.value ? input.value + "\n" : "") + content;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  autoGrowInput();
  hideInputMenu();
  // 使用统计（设置页展示「用过 N 次」）；内置兜底项没有 id，不上报；失败无碍
  if (s.id) request("snippets.used", { id: s.id }).catch(() => {});
}

// 全局热键落点：Ctrl+Alt+Space 唤起窗口后，聚焦输入框并预填剪贴板文本（desktop.py 调用）
window.__quickFocus = (text) => {
  const input = document.getElementById("input");
  if (!input) return;
  const q = String(text || "");
  if (q) input.value = (input.value ? input.value + "\n" : "") + q + "\n";
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  autoGrowInput();
};

// ---------- 引导 ----------
let providerBroken = false; // 上次 boot 是否「无可用模型」；send() 预检用（applyWorkspaceData 刷新）

/** 工作区数据预取：站内切换项目时先拿齐数据，再动界面。
 *
 *  分开取数/渲染的目的很具体：列表 DOM 的清空与填充必须在同一个同步块里完成，
 *  否则中间会渲染出一帧空列表（"内容不见了再出现"的闪）。 */
async function fetchWorkspaceData() {
  const [snap, sessions, projects, snippets] = await Promise.all([
    request("boot"),
    request("session.list").catch(() => ({ sessions: [], empty_count: 0 })),
    request("project.list").catch(() => ({ projects: [] })),
    request("snippets.list").catch(() => ({ snippets: [] })),
  ]);
  return { snap, sessions, projects, snippets };
}

/** 把预取到的数据一次性画上去（同步为主）。 */
async function applyWorkspaceData({ snap, sessions, projects, snippets }) {
  currentSessionId = snap.session ? snap.session.id : null;
  // 同步 boot 快照：classicGk（当前项目 id）等字段要跟手，项目切换后旧值会过期；
  // 保留其它分区已读入的字段（模板直接读 bootSnap.tools 等）
  bootSnap = bootSnap ? { ...bootSnap, ...snap } : snap;
  // 后端最后一次报告的「无可用模型」状态：send() 的预检依据（每次 boot 刷新）
  providerBroken = !snap.provider || !!snap.provider_error;
  const projPath = document.getElementById("project-path"); // 侧栏已移除工作目录框
  if (projPath) {
    projPath.textContent = snap.working_dir;
    projPath.title = snap.working_dir;
  }
  if (snap.provider_error) {
    showBanner("⚠ " + snap.provider_error.split("\n")[0] + " —— 配置好模型后即可开始对话。", [
      { label: "去配置", onClick: () => { hideBanner(); openSettings("providers"); } },
    ]);
    setModelChip("未配置模型");
  } else if (!renderTrustBanner(snap.workspace_trust)) {
    // 模型报错优先于信任提示：两个横幅共用一个位置，先让用户看到「用不了」的原因
    hideBanner();
    setModelChip(`${snap.provider}/${snap.model}`);
  } else {
    setModelChip(`${snap.provider}/${snap.model}`);
  }
  curProviderName = snap.provider || "";
  curModelName = snap.model || "";
  curSupportsVision = snap.supports_vision !== false; // 贴图前的前置提示用
  refreshReasoning(); // 思考强度控件按当前服务的支持情况显示
  sessionMeta = {};
  (snap.sessions || []).forEach((s) => { sessionMeta[s.id] = { title: s.title }; });
  if (snap.session) {
    // 启动恢复：snapshot 带了上次开着的标签列表（归属校验后）就按序重建，
    // 激活那张（snap.session）带消息；其余后台打开、进标签时再拉历史。
    // 没有记录（首次启动/全关过）保持原路径：只开当前会话或欢迎页。
    const openTabs = Array.isArray(snap.open_tabs) ? snap.open_tabs : [];
    if (openTabs.length > 1 || (openTabs.length === 1 && !snap.session)) {
      for (const s of openTabs) {
        if (snap.session && s.id === snap.session.id) continue; // 激活那张由下方打开
        openTabForSession(s.id, s.title, { background: true });
      }
    }
    openTabForSession(snap.session.id, snap.session.title,
                      { withMessages: snap.session.messages || [] });
  } else {
    startNewTab();
  }
  // 会话列表与项目列表一起画（数据已在手，不会出现空列表帧）
  await Promise.allSettled([
    refreshSessions(sessions),
    refreshProjects(projects),
  ]);
  snippetsCache = snippets.snippets || [];
  builtinSnippetsCache = snippets.builtin || builtinSnippetsCache;
  if (document.getElementById("snippets-list")) renderSnippets();
  // 输入栏的上下文环形仪表：启动即用当前会话的占用初始化（此前只在发过消息后才出现，用户会以为没有这个功能）
  request("chat.status").then((st) => {
    if (st) {
      setContextUsage(st.context_tokens || 0, st.context_limit || 0);
      if (st.context_detail) setContextDetail(st.context_detail);
    }
  }).catch(() => {});
  // 记忆标签若正开着（启动恢复 / 切项目回来），按当前项目重读
  if (rightActive === "memory" && !rightPanel.classList.contains("hidden")) loadMemoryPanel();
  // 启动后台已查过更新：有新版本时进通知中心（设置 · 关于里可手动再查）
  if (snap.update) {
    pushNotice(`🆕 新版本 v${snap.update.version} 可用`, "设置 · 关于 里可前往下载");
  }
  if (snap.crashed_last_run) {
    pushNotice("⚠ 上次 SkySheep 未正常关闭（可能崩溃或被强杀）",
      "会话数据有滚动备份，一般无碍；如遇异常可到 设置 · 关于 导出诊断包反馈");
  }
  // 窄屏 = 大概率是从手机/平板连进来的遥控端（桌面窗口不会这么窄）
  if (!window.__phoneHintShown && window.innerWidth <= 860) {
    window.__phoneHintShown = true;
    pushNotice("📱 手机控制模式", "你正在遥控电脑上的 SkySheep——聊天、批准确认、查看任务都在这里完成");
  }
  maybeOnboard(snap);
}

async function boot() {
  await applyWorkspaceData(await fetchWorkspaceData());
}

// ---------- 首次启动配置向导：没有可用模型时 3 步引导（选服务 → 填 Key → 开始用） ----------
// 无 Key 用户还有两条免注册出路：本机 Ollama（自动检测）与内置演示模式。
function maybeOnboard(snap) {
  const hasWorkingProvider = !snap.provider_error && snap.provider;
  // Ollama 预设出厂自带占位 Key（本机服务不校验 Key），其 has_key 恒为 true——
  // 「用户真的配过 Key」必须排除 localhost 服务，否则全新机器也会被误判成
  // 已配置，向导一次都不弹、还倒写 onboarded 把自己永久关掉
  const isLocalSvc = (u) =>
    /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\]|::1)(:\d+)?(\/|$)/i.test(String(u || ""));
  const anyKeyed = Object.values(snap.providers || {}).some(
    (p) => p.has_key && !isLocalSvc(p.base_url)
  );
  request("ui.get").then((ui) => {
    if (ui && ui.prefs && ui.prefs.onboarded) return; // 用户之前已跳过/完成
    if (hasWorkingProvider || anyKeyed) {
      saveUiPrefs({ onboarded: 1 }); // 已有可用模型，补个标记不再打扰
      return;
    }
    showOnboardWizard(snap);
  }).catch(() => {});
}

function showOnboardWizard(snap) {
  const presets = snap.provider_presets || [];
  if (!presets.length) return;
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">三步就好：选一个模型服务 → 粘贴 API Key → 开始对话。
    Key 保存在本机 ~/.skysheep/config.toml，不会上传。也可以先跳过，之后在 ⚙ 设置 里随时配置。</p>
    <div class="ob-step"><b>① 选择模型服务</b></div>
    <div class="ob-list">${
      presets.map((p, i) => `
        <label class="ob-row"><input type="radio" name="ob-preset" value="${escapeHtml(p.name)}"${i === 0 ? " checked" : ""}>
        <span class="ob-name">${escapeHtml(p.name)}</span>
        <span class="dim small ob-url">${escapeHtml(p.base_url || "官方接口")}</span>
        ${p.signup ? `<a class="ob-signup" href="${escapeHtml(p.signup)}" target="_blank" rel="noopener" data-signup="${escapeHtml(p.name)}">注册获取 Key ↗</a>` : ""}</label>`).join("")
    }</div>
    <div class="ob-step"><b>② 粘贴 API Key</b></div>
    <label class="ob-keyline">
      <input data-f="ob-key" type="password" autocomplete="off" placeholder="粘贴 API Key（点上方「注册获取 Key」去官网创建）">
      <button class="eye" type="button" title="显示 / 隐藏">👁</button>
    </label>
    <div class="form-status"></div>
    <div class="ob-alt">
      <div class="ob-alt-title dim small">没有 Key？两条免注册的出路：</div>
      <div class="ob-alt-row">
        <button type="button" class="btn-ghost" data-f="ob-demo">🎬 先看演示模式</button>
        <button type="button" class="btn-ghost" data-f="ob-ollama" hidden>🦙 启用本机 Ollama</button>
      </div>
      <div class="dim small" data-f="ob-ollama-hint">正在检测本机是否装了 Ollama（本地模型，无需 Key）…</div>
    </div>`;
  const val = (f) => box.querySelector(`[data-f="${f}"]`);
  const status = box.querySelector(".form-status");
  const finish = (msg) => {
    saveUiPrefs({ onboarded: 1 });
    hideModal();
    boot();
    providerStatus(msg);
  };
  box.querySelector(".eye").onclick = () => {
    const k = val("ob-key");
    k.type = k.type === "password" ? "text" : "password";
  };
  // 免注册出路 ①：演示模式（内置脚本回放一轮真实工具调用，零配置零费用）
  val("ob-demo").onclick = async () => {
    status.textContent = "正在进入演示模式…";
    try {
      await request("demo.enable");
      finish("✓ 演示模式已开启——发送任意消息看 Agent 怎么干活；正式使用前请在 ⚙ 设置 里配一个模型服务");
    } catch (e) {
      status.textContent = "✗ " + e.message;
    }
  };
  // 免注册出路 ②：检测到本机 Ollama 时亮出「一键启用」
  request("ollama.detect").then((det) => {
    const hint = val("ob-ollama-hint");
    if (!det || !det.available) {
      hint.textContent = "没检测到本机 Ollama——装了它就能完全离线用本地模型（不装也没关系，云服务都有免费额度）";
      return;
    }
    hint.textContent = `检测到本机 Ollama（${det.models.length} 个模型：${det.models.slice(0, 3).join("、")}${det.models.length > 3 ? "…" : ""}），可以直接用`;
    const btn = val("ob-ollama");
    btn.hidden = false;
    btn.onclick = async () => {
      status.textContent = "正在启用 Ollama…";
      try {
        const r = await request("ollama.enable", { model: det.models[0] });
        finish(`✓ 已切换到本机 Ollama（${r.model}）——无需 Key，直接开始对话吧`);
      } catch (e) {
        status.textContent = "✗ " + e.message;
      }
    };
  }).catch(() => {});
  showModal("👋 欢迎使用 SkySheep", box, async () => {
    const name = box.querySelector("input[name=ob-preset]:checked").value;
    const key = val("ob-key").value.trim();
    if (!key) throw new Error("请先粘贴 API Key（或点「跳过」之后再配置）");
    status.textContent = "正在保存并验证…";
    const r = await request("config.save_provider", { name, api_key: key });
    if (r.provider_error) throw new Error(r.provider_error);
    await request("model.switch", { name }).catch(() => {});
    finish(`✓ 「${name}」配置完成，开始对话吧！`);
  }, "保存并开始");
  // 向导底部加「跳过」入口（modal 本身的取消按钮即跳过：点了也不阻塞使用）
  const cancel = document.getElementById("modal-cancel");
  const origCancel = cancel.onclick;
  cancel.onclick = () => {
    saveUiPrefs({ onboarded: 1 });
    cancel.onclick = origCancel;
    hideModal();
    addNotice("已跳过配置——顶栏会提示「未配置模型」，点 ⚙ 设置 随时可配。");
  };
}

// ---------- 项目列表（侧栏直接展示，点击即切换，对标 Obsidian 项目列表） ----------
const FOLDER_SVG = '<svg class="p-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

/** 切换工作项目：同一套流程给侧栏列表与「工作项目」弹窗用。
 *
 *  站内切换，不整页刷新：旧界面全程留在屏幕上，后端切好 + 新项目数据拉齐后
 *  一次性替换。此前走 location.reload()，整页重载必然先画出一屏空 DOM，
 *  不管拿什么遮罩去盖，用户看到的都是一屏「加载中」。
 *
 *  失败（有任务在跑 / 目录不存在）时界面完全不动，只弹提示。 */
let switchingProject = false;
async function switchProject(path) {
  if (switchingProject) return;
  switchingProject = true;
  setSwitchBusy(true);
  try {
    // 1) 后端先切：这一步失败就什么都没发生，界面不受影响
    await request("project.switch", { path });
    // 2) 先取数（此时旧界面还在屏幕上），取不到就报错回退，不动界面
    const data = await fetchWorkspaceData().catch((e) => {
      throw new Error("新项目数据加载失败：" + e.message);
    });
    // 3) 数据到手 → 清旧状态 → 同步重画，清与画之间不经过网络，不会出现空列表帧
    resetWorkspaceState();
    await applyWorkspaceData(data);
    // 4) 右侧面板里与项目绑定的页按需重拉（boot 不管这些）
    reloadProjectPanels();
    addNotice(`已切换到项目「${currentProjectName() || path}」`);
    // 5) 新内容整体淡入 180ms：欢迎卡/提示条/列表从「瞬间弹入」变成平滑过渡，
    //    消除切换时大块内容突然出现又（下次切换时）突然消失的「弹窗感」
    for (const el of [
      document.querySelector("#chat > .chat-log"),
      document.getElementById("session-list"),
      document.getElementById("project-list"),
    ]) {
      if (!el) continue;
      el.classList.remove("swap-in");
      void el.offsetWidth; // 强制重排，让同一元素在连续切换时也能重放动画
      el.classList.add("swap-in");
    }
  } catch (e) {
    // 右面板可能已被 resetWorkspaceState 压暗：失败路径必须撤掉，别把变暗留在屏上
    const body = document.getElementById("rp-body");
    if (body) body.classList.remove("reloading");
    setSwitchBusy(false);
    switchingProject = false;
    throw e;
  }
  setSwitchBusy(false);
  switchingProject = false;
}

/** 切换期间只做轻量提示（顶栏状态 + 项目列表置灰），不遮挡界面。
 *
 *  指示延迟 250ms 才亮出：本地项目切换通常几十毫秒完成，指示器跟着一闪
 *  而过反而像界面在抖（用户报的「切换项目时页面闪烁」主要来源之一）。 */
function setSwitchBusy(on) {
  const list = document.getElementById("project-list");
  const text = document.getElementById("status-text");
  if (on) {
    clearTimeout(switchBusyTimer);
    switchBusyTimer = setTimeout(() => {
      switchBusyTimer = null;
      switchBusyShown = true;
      if (list) list.classList.add("busy");
      switchStatusPrev = text ? text.textContent : null;
      if (text) text.textContent = "正在切换项目…";
    }, 250);
  } else {
    if (switchBusyTimer) { clearTimeout(switchBusyTimer); switchBusyTimer = null; }
    if (switchBusyShown) {
      switchBusyShown = false;
      if (list) list.classList.remove("busy");
      if (text && switchStatusPrev != null) text.textContent = switchStatusPrev;
    }
    switchStatusPrev = null;
  }
}
let switchStatusPrev = null;
let switchBusyTimer = null;
let switchBusyShown = false;

function currentProjectName() {
  const el = document.querySelector("#project-list li.active .s-title");
  return el ? el.textContent : "";
}

/** 清空一切与"当前项目"绑定的前端状态。
 *
 *  会话标签、消息 DOM、权限条、运行态、搜索态、@ 文件索引、右侧面板缓存、
 *  设置页缓存全部作废；清完由 boot() 重建。宠物位置（petPos / pet_x）是界面偏好，
 *  跨项目保留，不能动。 */
function resetWorkspaceState() {
  // —— 会话标签与聊天流 ——
  chatTabs.forEach((t) => t.logEl.remove());
  chatTabs = [];
  activeTab = null;
  routeTab = null;
  sessionMeta = {};
  currentSessionId = null;
  chatBox.querySelectorAll(":scope > .chat-log").forEach((el) => el.remove());

  // —— 运行态 / 权限 / 用量 ——
  hidePermission();
  running = false;
  pendingTurns = 0;
  usageIn = 0;
  usageOut = 0;
  renderUsage();
  setContextUsage(0, 0);
  document.getElementById("btn-send").textContent = "发送";
  document.getElementById("btn-stop").hidden = true;

  // —— 会话列表与搜索 ——
  sessionSearchActive = false;
  searchEl.value = "";
  document.getElementById("session-list").innerHTML = "";

  // —— 任务清单（旧项目的 todo 属于旧项目）——
  clearTodoPanel();

  // —— @ 文件索引（30 秒缓存，指向旧项目目录树）——
  fileIndex = null;

  // —— 右侧面板：与项目绑定的缓存全部作废 ——
  resetProjectPanels();

  // —— 设置页缓存（白名单、项目记忆、技能/MCP 都跟项目走）——
  providerCfg = null;
  bootSnap = null;
  availCacheModels = null;
  availNoteState = null;
}

/** 右面板里跟项目绑定的数据缓存作废（DOM 不清空，由各自 loader 拉到新数据后整块替换）。
 *
 *  此前这里先把文件树/任务等 innerHTML 清空、再等 loader 异步回填，中间隔着
 *  一段网络等待——面板先变白再「啪」地出现内容，正是切换项目时右侧闪烁的来源。
 *  现在旧内容留屏，新数据到手后一次性换上；加载变暗由 reloadProjectPanels 延迟
 *  亮出（本函数不加点，快速切换连变暗都不出现）。 */
function resetProjectPanels() {
  filesLoaded = false;
  // 切项目时待发起的文件树防抖刷新一并作废：它请求的是旧项目的 fs.files，
  // 不取消就会在新项目里把旧目录树画上来（随后被新项目的刷新覆盖，但会先闪一下）
  if (filesRefreshTimer) { clearTimeout(filesRefreshTimer); filesRefreshTimer = 0; }
  agCache = [];
  // 项目记忆属于旧项目：内容与"已加载"标记一并作废，激活时由 loader 重读。
  // 文本框是可编辑的，不能留旧项目的 AGENTS.md 在屏上（Ctrl+S 会写进新项目），
  // 所以唯独这里仍然清空；只读列表才走「留屏 + 变暗」策略
  memoryLoaded = false;
  const memText = document.getElementById("rp-memory-text");
  if (memText) memText.value = "";
  const memStatus = document.getElementById("memory-status");
  if (memStatus) memStatus.textContent = "";
  if (tasksTimer) { clearTimeout(tasksTimer); tasksTimer = null; }
  const reviewDiff = document.getElementById("review-diff");
  if (reviewDiff) reviewDiff.classList.add("hidden");
  const preview = document.getElementById("files-preview");
  if (preview) preview.classList.add("hidden");
  // 终端：命令是在项目目录里跑的，旧项目的输出与运行态一并作废
  resetTermTabs();
}

/** 切换后重拉右侧面板里与项目绑定的页（boot 不管这些）。

  面板收起时也照拉：拉的是数据、不占屏幕，拉完再展开时就是一屏满的；
  以前这里早退，收起状态切项目再展开会看到上一个项目的内容。

  变暗指示延迟 200ms：本地项目切换大多几十毫秒完成，立刻变暗再复原
  会形成一次灰色脉冲（也是闪烁）；真的慢（大目录/网络盘）才压暗提示。 */
async function reloadProjectPanels() {
  const body = document.getElementById("rp-body");
  if (!rightTabs.length) return;
  // 面板收起时不压暗（用户看不见，压了也白压，还会在展开那一瞬闪一下）
  const dimTimer = rightCollapsed ? 0 : setTimeout(() => {
    if (body) body.classList.add("reloading");
  }, 200);
  try {
    await Promise.allSettled(
      rightTabs.map((id) => Promise.resolve().then(() => loadRightTab(id, true))),
    );
  } finally {
    clearTimeout(dimTimer);
    // 无论 loaders 是否跑过都要撤（面板收起等早退也不能把变暗留在屏上）
    if (body) body.classList.remove("reloading");
  }
}

async function refreshProjects(prefetched) {
  const ul = document.getElementById("project-list");
  if (!ul) return;
  const { projects } = prefetched || await request("project.list").catch(() => ({ projects: [] }));
  ul.innerHTML = "";
  // 「项目」区：标题 + 右端 ＋ 常显，列表两态只体现在 ul 有没有行。
  // 空态不再留「标题 + 空提示」的死区，加号常在标题右端。
  const sec = document.getElementById("project-section");
  if (sec) sec.classList.toggle("empty", !projects.length);
  if (!projects.length) {
    applySidebarView(); // 会话区的空态文案也跟着变，与项目区同帧
    return;
  }
  projects.forEach((p) => {
    const li = document.createElement("li");
    if (p.is_current) li.classList.add("active");
    li.innerHTML = FOLDER_SVG + `<span class="s-title">${escapeHtml(p.name)}</span>`;
    // 「远程连接」固定项目：无真实目录（root_path 报空），不可切换/删除，
    // 它名下是飞书/微信等渠道的对话——点名称不切项目
    const remote = !p.root_path;
    li.title = remote
      ? "远程连接 —— 飞书/微信等渠道的对话都归在这里（固定项目，不可切换）；点击打开最近的对话"
      : p.root_path + (p.is_current ? "（当前项目）" : "—— 点击切换到这个项目");
    if (remote) li.classList.add("remote-fixed");
    // 拖动排序：与分组视图共用 project_order（project.list 已按它返回）
    wireListDrag(li, { id: String(p.id) }, {
      commit: (dst, pos) => commitProjectOrder(String(p.id), dst.id, pos, "project-list", "li", "pid"),
      rerender: () => refreshProjects(),
    });
    li.dataset.pid = p.id; // commitProjectOrder 从 DOM 收集当前序
    // 移除按钮：当前项目也能删（清空会话与记录后用同一目录重置重新开始）；
    // 远程连接是固定项目，不提供删除
    if (!remote) {
      const del = document.createElement("button");
      del.className = "p-del";
      del.textContent = "✕";
      del.title = p.is_current ? "重置这个项目（清空会话与记录）" : "从列表中移除这个项目";
      del.onclick = (e) => {
        e.stopPropagation();
        deleteProjectModal(p);
      };
      li.appendChild(del);
    }
    li.onclick = async () => {
      if (p.is_current) return;
      if (remote) {
        // 「远程连接」不可切换工作目录：点它打开该项目里最近的会话
        // （没有就提示去渠道里发一条——飞书/微信来的对话会出现在这里）
        const r = await request("session.list", { all_projects: 1 }).catch(() => null);
        const list = ((r && r.sessions) || [])
          .filter((s) => s.project_id === p.id)
          .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
        if (!list.length) {
          addNotice("「远程连接」还没有对话——在飞书/微信里给机器人发条消息就会出现在这里");
          return;
        }
        await openTabForSession(list[0].id, list[0].title);
        return;
      }
      try {
        await switchProject(p.root_path);
      } catch (e) {
        addNotice("切换失败: " + e.message);
      }
    };
    ul.appendChild(li);
  });
}

// 删除项目：确认后连带删掉它的会话与白名单（磁盘文件夹不动）
function deleteProjectModal(p) {
  const box = document.createElement("div");
  const cur = !!p.is_current;
  box.innerHTML =
    `<p>确定从列表中移除项目 <b>${escapeHtml(p.name)}</b> 吗？</p>` +
    (cur
      ? `<p class="dim small">这是<b>当前正在使用的项目</b>：它的全部会话、白名单与任务会被删除，` +
        `然后切换到其他项目（没有其他项目就进入快聊）。</p>`
      : `<p class="dim small">该项目下的<b>会话记录与白名单会一并删除</b>；电脑上的文件夹和文件` +
        `<b>不受影响</b>，以后随时可以重新添加回来。</p>`) +
    `<span class="mono-path">${escapeHtml(p.root_path)}</span>`;
  showModal("删除项目", box, async () => {
    const r = await request("project.delete", { id: p.id });
    await applyWorkspaceData(await fetchWorkspaceData());
    if (r.switched_to) {
      addNotice(`已删除项目「${p.name}」，已切换到「${r.switched_to.name}」。`);
    } else {
      addNotice(`已删除项目「${p.name}」；文件夹仍保留在电脑上，当前处于快聊。`);
    }
  }, "移除");
}

// ---------- 发送（规划 / 执行两种模式） ----------
let workMode = "execute"; // "execute" | "plan"

// 功能开关收成小图标后，按钮只随状态换图标/底色（线稿 SVG，描边取 currentColor）
const CP_ICON_EXECUTE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.8 13.2h5.7L10.2 22l8.9-11.6h-5.6L13 2z"/></svg>';
const CP_ICON_PLAN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5 5-2z"/></svg>';
const MODE_TITLE = {
  execute: "当前：执行模式（直接动手改文件）· 点击切到规划模式",
  plan: "当前：规划模式（只调研出计划，不动文件）· 点击切到执行模式",
};

// 输入框占位的技巧提示：&/@/斜杠与图片入口，两种模式共用，切模式不丢
const INPUT_HINT = "输入 & 引用对话 · @ 引用文件 · ~ 提示词 · / 命令 · 可粘贴/拖入图片";

function setWorkMode(mode) {
  workMode = mode;
  const btn = document.getElementById("mode-switch");
  btn.innerHTML = mode === "plan" ? CP_ICON_PLAN : CP_ICON_EXECUTE;
  btn.title = MODE_TITLE[mode];
  btn.classList.toggle("plan", mode === "plan");
  document.getElementById("input").placeholder = mode === "plan"
    ? `规划模式：描述目标，我只调研并产出实施计划… ${INPUT_HINT}`
    : `描述你的任务… ${INPUT_HINT}`;
}
document.getElementById("mode-switch").onclick = () =>
  setWorkMode(workMode === "plan" ? "execute" : "plan");

// ---------- 圆桌：多模型共同思考，融合成更好的答案 ----------
let rtOn = false; // 一次性开关：发送后自动复位
let rtMembers = (() => {
  try { return JSON.parse(localStorage.getItem("skysheep.rt.members") || "null"); }
  catch { return null; }
})();
// 辩论修订轮数（0-2）与主席是否出草稿：弹层里的本轮控制，默认值首次拉配置时对齐
let rtDebate = (() => {
  const v = Number(localStorage.getItem("skysheep.rt.debate"));
  return v >= 1 && v <= 2 ? v : 0;
})();
let rtChair = localStorage.getItem("skysheep.rt.chair") !== "0";
// 圆桌成员身份预设（与后端 core/roundtable.py 的 MEMBER_ROLES 一一对应，id 随元数据持久化勿改）
const RT_ROLES = [
  ["", "普通"],
  ["critic", "批评者"],
  ["factcheck", "事实核查员"],
  ["concise", "简洁派"],
  ["practitioner", "实干者"],
];
const rtRoleLabel = (id) => (RT_ROLES.find((r) => r[0] === id) || RT_ROLES[0])[1];

function setRtOn(on) {
  rtOn = on;
  document.getElementById("rt-switch").classList.toggle("on", on);
}

function updateRtHint() {
  const btn = document.getElementById("rt-switch");
  const n = rtMembers ? rtMembers.length : 0;
  btn.title = (n
    ? `圆桌（已指定 ${n} 个成员）：本条消息由多个模型并行思考，融合成更好的答案`
    : "圆桌：自动挑选已配置 Key 的模型共同思考，融合成更好的答案（点 ▾ 可指定成员）");
}
updateRtHint();

async function showRtMenu(e) {
  e.stopPropagation(); // 别让随后冒泡到 document 的 click 立刻把菜单关掉
  const menu = document.getElementById("rt-menu");
  if (!menu.classList.contains("hidden")) return menu.classList.add("hidden");
  menu.innerHTML = '<div class="rt-menu-tip">加载中…</div>';
  menu.classList.remove("hidden");
  positionRtMenu(menu);
  const cfg = await request("config.providers").catch(() => null);
  if (!cfg || menu.classList.contains("hidden")) return;
  menu.innerHTML = "";
  const tip = document.createElement("div");
  tip.className = "rt-menu-tip";
  tip.textContent = "选择圆桌成员（不选 = 自动挑选已配置 Key 的模型）：";
  menu.appendChild(tip);
  const list = document.createElement("div");
  list.className = "rt-menu-list";
  const selected = new Set((rtMembers || []).map((m) => m.provider + "/" + m.model));
  const roleOf = new Map((rtMembers || []).map((m) => [m.provider + "/" + m.model, m.role || ""]));
  let entries = 0;
  Object.entries(cfg.providers || {}).forEach(([name, p]) => {
    // 只列已配置 Key 的模型：缺 Key 的服务选了也跑不了，列出来只是噪声
    if (!p.key_mask) return;
    (p.models || []).forEach((m) => {
      entries += 1;
      const key = name + "/" + m;
      const label = document.createElement("label");
      label.className = "rt-menu-item";
      label.innerHTML =
        `<input type="checkbox" value="${escapeHtml(key)}" ${selected.has(key) ? "checked" : ""}>` +
        `<span class="mi-cmd">${escapeHtml(name)}</span>` +
        `<span class="mi-model">${escapeHtml(m)}</span>`;
      {
        // 身份下拉：给这个成员注入不同视角的作答提示词（普通 = 无附加身份）
        const cb = label.querySelector("input");
        const sel = document.createElement("select");
        sel.className = "mi-role";
        sel.title = "成员身份：让不同成员带着不同视角作答，辩论更有分歧";
        RT_ROLES.forEach(([id, text]) => {
          const o = document.createElement("option");
          o.value = id;
          o.textContent = text;
          sel.appendChild(o);
        });
        sel.value = roleOf.get(key) || "";
        sel.hidden = !cb.checked;
        sel.onclick = (e) => e.stopPropagation();
        sel.onchange = () => roleOf.set(key, sel.value);
        cb.addEventListener("change", () => { sel.hidden = !cb.checked; });
        label.appendChild(sel);
      }
      list.appendChild(label);
    });
  });
  if (!entries) {
    list.innerHTML = '<div class="rt-menu-tip">还没有已配置 Key 的模型——先到「设置 · 模型服务」配置 API Key，再回来挑选成员。</div>';
  }
  menu.appendChild(list);
  // 辩论修订 / 主席出草稿：本轮生效（也随 localStorage 记住选择）
  const extra = document.createElement("div");
  extra.className = "rt-menu-extra";
  const debRow = document.createElement("label");
  debRow.className = "rt-menu-row";
  debRow.innerHTML = '<span class="rt-menu-row-label">辩论修订</span>' +
    '<select id="rt-debate">' +
    '<option value="0">关闭 · 只独立作答</option>' +
    '<option value="1">1 轮 · 看彼此草稿后修订</option>' +
    '<option value="2">2 轮 · 修订两次</option></select>';
  debRow.title = "成员先独立作答，再看到彼此草稿修订一轮后交主席融合；成本约翻倍，已收敛的成员自动跳过";
  const debSel = debRow.querySelector("select");
  debSel.value = String(rtDebate);
  debSel.onchange = () => {
    rtDebate = Number(debSel.value) || 0;
    try { localStorage.setItem("skysheep.rt.debate", String(rtDebate)); } catch {}
  };
  const chairRow = document.createElement("label");
  chairRow.className = "rt-menu-row";
  chairRow.title = "关闭后本轮只有成员作答，主席只负责融合";
  chairRow.innerHTML = `<input type="checkbox" id="rt-chair" ${rtChair ? "checked" : ""}>` +
    "<span>主席也出一份草稿参与融合</span>";
  const chairCb = chairRow.querySelector("input");
  chairCb.onchange = () => {
    rtChair = chairCb.checked;
    try { localStorage.setItem("skysheep.rt.chair", rtChair ? "1" : "0"); } catch {}
  };
  const cmpRow = document.createElement("label");
  cmpRow.className = "rt-menu-row";
  cmpRow.title = "不融合各成员回答，而是并排保留每个模型的原始回答（A/B 对比）";
  cmpRow.innerHTML = `<input type="checkbox" id="rt-compare">` +
    "<span>对比模式（不融合，保留各自回答）</span>";
  extra.append(debRow, chairRow, cmpRow);
  menu.appendChild(extra);
  const actions = document.createElement("div");
  actions.className = "rt-menu-actions";
  const clear = document.createElement("button");
  clear.className = "link-btn";
  clear.textContent = "恢复自动";
  clear.onclick = () => list.querySelectorAll("input:checked").forEach((i) => (i.checked = false));
  const ok = document.createElement("button");
  ok.className = "btn-primary";
  ok.textContent = "确定";
  ok.onclick = () => {
    const picked = [...list.querySelectorAll("input:checked")].map((i) => {
      const idx = i.value.indexOf("/");
      const item = { provider: i.value.slice(0, idx), model: i.value.slice(idx + 1) };
      const role = roleOf.get(i.value) || "";
      return role ? Object.assign(item, { role }) : item;
    });
    rtMembers = picked.length ? picked : null;
    try { localStorage.setItem("skysheep.rt.members", JSON.stringify(rtMembers)); } catch {}
    updateRtHint();
    menu.classList.add("hidden");
  };
  actions.append(clear, ok);
  menu.appendChild(actions);
}
document.getElementById("rt-switch").onclick = () => setRtOn(!rtOn);
document.getElementById("rt-pick").onclick = showRtMenu;

/** 成员选择浮层定位：底边贴在输入栏上沿，左缘与圆桌按钮对齐。
 *
 *  用按钮的实时位置算（而不是写死 left），这样界面缩放、窗口宽度、
 *  输入栏高度变化都不会错位；再向容器内收敛，窄窗口下不会溢出或被裁切。 */
function positionRtMenu(menu) {
  const composer = document.getElementById("composer");
  const host = document.getElementById("chat-main");
  if (!composer || !host) return;
  const hostRect = host.getBoundingClientRect();
  const anchor = document.getElementById("rt-switch") || document.getElementById("rt-group");
  menu.style.bottom = (composer.offsetHeight + 8) + "px";
  if (!anchor) return;
  // getBoundingClientRect 是物理像素，除以 uiScale 换算成布局坐标
  const rawLeft = (anchor.getBoundingClientRect().left - hostRect.left) / uiScale;
  const hostW = hostRect.width / uiScale;
  const menuW = menu.offsetWidth || 350;
  const maxLeft = Math.max(8, hostW - menuW - 8);
  menu.style.left = Math.round(Math.max(8, Math.min(rawLeft, maxLeft))) + "px";
}
document.addEventListener("click", (e) => {
  const menu = document.getElementById("rt-menu");
  if (!menu.classList.contains("hidden") && !menu.contains(e.target) &&
      e.target.id !== "rt-pick") menu.classList.add("hidden");
});

let currentSessionId = null; // 当前会话（/export、检查点列表用）

// ---------- Windows 系统通知：失焦时才弹，前台不打扰；开关存 ui.json（notify） ----------
let uiNotifyOn = true;
let ctrlEnterSend = false; // 发送键：false = Enter 发送（默认），true = Ctrl+Enter 发送
let uiNotifySound = false; // 提示音（默认关）：任务完成/等确认时合成短音，设置页开关
let uiNotifySoundFocus = true; // 仅失焦时提示音（默认开）：前台人已在看，响一声反而吵
let uiNotifyKindDone = true; // 任务完成通知开关（关掉不弹系统通知、不响提示音）
let uiNotifyKindPerm = true; // 等待确认通知开关（同上）

/** WebAudio 合成两音短提声（不引资源文件：零构建约束下最轻的实现）。
    无 AudioContext（老 WebView）静默跳过；页面未交互前被浏览器禁止也静默。 */
function playNotifySound(kind = "done") {
  if (!uiNotifySound) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = playNotifySound._ctx || (playNotifySound._ctx = new AC());
    if (ctx.state === "suspended") { ctx.resume().catch(() => {}); }
    // 完成 = 上行双音；等确认 = 下行双音（更「需要你」的语感）
    const notes = kind === "perm" ? [[660, 0], [520, 0.16]] : [[520, 0], [660, 0.16]];
    for (const [freq, delay] of notes) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const t0 = ctx.currentTime + delay;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.12, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.28);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.3);
    }
  } catch (e) { /* 提示音不能打断正事 */ }
}

function maybeNotify(title, body, kind = "done", meta = null) {
  // 通知中心永远记录（它是「错过提醒」的聚合日志，不该有死角）；类型开关只挡提醒本身
  pushNotice(title, body, kind, meta);
  const kindOn = kind === "perm" ? uiNotifyKindPerm : uiNotifyKindDone;
  if (!kindOn) return;
  const focusGone = !document.hasFocus() || document.hidden;
  if (uiNotifySound && (!uiNotifySoundFocus || focusGone)) playNotifySound(kind);
  if (!uiNotifyOn) return;
  if (!focusGone) return;
  request("app.notify", { title, body }).catch(() => {});
}

// ---------- 通知中心：聚合错过的提醒（cron 结果 / 权限请求 / 日程 / 错误） ----------
const notifLog = []; // {ts, title, body, kind, sid?}；sid = 相关会话标签，点击条目可跳
let notifUnread = 0;
const notifPanel = document.getElementById("notif-panel");
let notifSaveTimer = null;
// 类型图标：done=完成（蓝）、perm=等确认（金）；其余 kind / 无 kind 用圆点
const NOTIF_ICONS = { done: "✓", perm: "✋" };

function pushNotice(title, body, kind = "", meta = null) {
  const entry = { ts: Date.now() / 1000, title, body, kind };
  if (meta && meta.sid) entry.sid = String(meta.sid);
  notifLog.unshift(entry);
  if (notifLog.length > 60) notifLog.pop();
  notifUnread += 1;
  renderBell();
  if (!notifPanel.classList.contains("hidden")) renderNotifPanel();
  scheduleNotifSave(); // 持久化到 ui.json（防抖合并一轮里的多条）
}

// 通知中心历史落 ui.json（localStorage 在 pywebview 私密模式下每次启动清空，
// 挂机一夜攒下的 cron 结果不能靠它）；传 null = 清空后删键
function saveNotifLog() {
  clearTimeout(notifSaveTimer);
  notifSaveTimer = null;
  saveUiPrefs({
    notif_log: notifLog.length
      ? notifLog.map((n) => ({ ts: n.ts, title: n.title, body: n.body, kind: n.kind }))
      : null,
  });
}

function scheduleNotifSave() {
  clearTimeout(notifSaveTimer);
  notifSaveTimer = setTimeout(saveNotifLog, 800);
}

function renderBell() {
  const badge = document.getElementById("bell-badge");
  const n = notifPanel.classList.contains("hidden") ? notifUnread : 0;
  badge.classList.toggle("hidden", n <= 0);
  badge.textContent = n > 9 ? "9+" : String(n);
  const bell = document.getElementById("btn-bell");
  bell.classList.toggle("has-unread", n > 0);
  bell.setAttribute("aria-label", n > 0 ? `通知中心，${n} 条未读` : "通知中心");
}

function fmtNotifTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  // 跨天的通知带上日期，不然挂机一夜后全是「HH:MM」，分不清是昨天还是上周
  if (d.toDateString() === new Date().toDateString()) return hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

function renderNotifPanel() {
  notifPanel.innerHTML = "";
  const head = document.createElement("div");
  head.className = "notif-head";
  head.innerHTML = `<b>通知中心</b><button class="link-btn">清空</button>`;
  head.querySelector(".link-btn").onclick = (e) => {
    e.stopPropagation();
    notifLog.length = 0;
    notifUnread = 0;
    renderNotifPanel();
    renderBell();
    saveNotifLog(); // 清空也删掉 ui.json 里的历史
  };
  notifPanel.appendChild(head);
  if (!notifLog.length) {
    const empty = document.createElement("div");
    empty.className = "dim small notif-empty";
    empty.textContent = "没有通知。定时任务结果、权限请求、日程提醒和错误会出现在这里。";
    notifPanel.appendChild(empty);
    return;
  }
  notifLog.forEach((n) => {
    const item = document.createElement("div");
    item.className = "notif-item kind-" + (n.kind || "info");
    // 带会话且该标签还开着才可跳：点击切过去（后台权限请求会就地亮出确认卡）
    const tab = n.sid ? chatTabs.find((t) => String(t.sid) === n.sid) : null;
    if (tab) item.classList.add("jumpable");
    item.innerHTML = `<span class="notif-ico">${NOTIF_ICONS[n.kind] || "•"}</span>` +
      `<span class="notif-time">${fmtNotifTime(n.ts)}</span>` +
      `<span class="notif-body"><b>${escapeHtml(n.title)}</b>` +
      (n.body ? `<span>${escapeHtml(n.body)}</span>` : "") + `</span>`;
    if (tab) {
      item.title = "点击前往对应会话";
      item.onclick = () => {
        notifPanel.classList.add("hidden");
        if (settingsOpen) backToChat(); // 在设置页里点的：先回对话视图再切标签
        activateTab(tab);
      };
    }
    notifPanel.appendChild(item);
  });
}

document.getElementById("btn-bell").onclick = (e) => {
  e.stopPropagation();
  if (!notifPanel.classList.contains("hidden")) {
    notifPanel.classList.add("hidden");
    renderBell();
    return;
  }
  const r = document.getElementById("btn-bell").getBoundingClientRect();
  notifPanel.classList.remove("hidden");
  // getBoundingClientRect 是物理像素，弹层在缩放布局坐标系，除回 uiScale 才贴得住按钮
  notifPanel.style.top = (r.bottom / uiScale + 8) + "px";
  notifPanel.style.left = Math.max(8, r.right / uiScale - 340) + "px";
  notifUnread = 0;
  renderNotifPanel();
  renderBell();
};
document.addEventListener("click", (e) => {
  if (!notifPanel.classList.contains("hidden") && !notifPanel.contains(e.target) &&
      e.target.id !== "btn-bell") {
    notifPanel.classList.add("hidden");
    renderBell();
  }
});

function renderNotifyToggle() {
  const t = document.getElementById("notify-toggle");
  if (t) t.checked = uiNotifyOn;
}
const notifyToggle = document.getElementById("notify-toggle");
if (notifyToggle) {
  notifyToggle.onchange = () => {
    uiNotifyOn = notifyToggle.checked;
    saveUiPrefs({ notify: uiNotifyOn ? 1 : 0 });
  };
}

function renderNotifySoundToggle() {
  const t = document.getElementById("notify-sound-toggle");
  if (t) t.checked = uiNotifySound;
}
const notifySoundToggle = document.getElementById("notify-sound-toggle");
if (notifySoundToggle) {
  notifySoundToggle.onchange = () => {
    uiNotifySound = notifySoundToggle.checked;
    saveUiPrefs({ notify_sound: uiNotifySound ? 1 : 0 });
    if (uiNotifySound) playNotifySound("done"); // 打开即试听一声
  };
}

// 通知类型与提示音作用域的开关：保存即生效，偏好自动保存
function renderNotifyKindToggles() {
  const d = document.getElementById("notify-kind-done");
  const p = document.getElementById("notify-kind-perm");
  const f = document.getElementById("notify-sound-focus");
  if (d) d.checked = uiNotifyKindDone;
  if (p) p.checked = uiNotifyKindPerm;
  if (f) f.checked = uiNotifySoundFocus;
}
const notifyKindDone = document.getElementById("notify-kind-done");
if (notifyKindDone) {
  notifyKindDone.onchange = () => {
    uiNotifyKindDone = notifyKindDone.checked;
    saveUiPrefs({ notify_kind_done: uiNotifyKindDone ? 1 : 0 });
  };
}
const notifyKindPerm = document.getElementById("notify-kind-perm");
if (notifyKindPerm) {
  notifyKindPerm.onchange = () => {
    uiNotifyKindPerm = notifyKindPerm.checked;
    saveUiPrefs({ notify_kind_perm: uiNotifyKindPerm ? 1 : 0 });
  };
}
const notifySoundFocus = document.getElementById("notify-sound-focus");
if (notifySoundFocus) {
  notifySoundFocus.onchange = () => {
    uiNotifySoundFocus = notifySoundFocus.checked;
    saveUiPrefs({ notify_sound_focus: uiNotifySoundFocus ? 1 : 0 });
  };
}

// 自动检查更新开关（关于页）：保存即生效，下次启动不再请求 GitHub
const updateCheckToggle = document.getElementById("update-check-toggle");
if (updateCheckToggle) {
  updateCheckToggle.onchange = () => {
    saveUiPrefs({ update_check: updateCheckToggle.checked ? 1 : 0 });
  };
}

function renderSendKeyToggle() {
  const t = document.getElementById("send-key-toggle");
  if (t) t.checked = ctrlEnterSend;
}
const sendKeyToggle = document.getElementById("send-key-toggle");
if (sendKeyToggle) {
  sendKeyToggle.onchange = () => {
    ctrlEnterSend = sendKeyToggle.checked;
    saveUiPrefs({ ctrl_enter_send: ctrlEnterSend ? 1 : 0 });
  };
}

// ---------- 对话区宠物：云朵小羊（状态联动 + 点击互动；开关存 ui.json 的 pet 键） ----------
const petEl = document.getElementById("chat-pet");
const petBubble = document.getElementById("pet-bubble");
let petOn = true;            // 设置页开关，默认显示
let petBubbleTimer = null;
let petPrevRunning = false;  // 活动标签运行态的上一帧，用于触发「完成」一次性动画

const PET_LINES = {
  idle: ["咩～", "今天也要加油呀", "点我干嘛，嘿嘿", "在云朵上打个盹…", "有任务尽管丢过来"],
  running: ["冲冲冲！", "我帮你盯着呢", "加油加油～"],
  perm: ["等你拍板呢", "这个能做吗？", "点下面的按钮告诉我"],
  done: ["搞定啦！", "任务完成，咩～", "收工收工！"],
  land: ["落地咯～", "稳稳当当", "咩～到家了", "软着陆成功！"],
};

function petPick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function petSay(text, ms = 2600) {
  if (!petOn || !petBubble) return;
  petBubble.textContent = text;
  petBubble.classList.remove("hidden");
  clearTimeout(petBubbleTimer);
  petBubbleTimer = setTimeout(() => petBubble.classList.add("hidden"), ms);
}

// 状态优先级：等确认 > 运行中 > 待机
function petSync(state) {
  if (!petEl) return;
  petEl.classList.toggle("needs-perm", state === "perm");
  petEl.classList.toggle("running", state === "running");
}

function petCelebrate() {
  if (!petEl || !petOn) return;
  petEl.classList.remove("done");
  void petEl.offsetWidth; // 重启动画
  petEl.classList.add("done");
  setTimeout(() => petEl.classList.remove("done"), 850);
  petSay(petPick(PET_LINES.done));
}

// setRunning / showPermission / hidePermission / activateTab 都会调这里
function petRefresh() {
  if (!petEl) return;
  const tab = activeTab;
  if (permRequest || (tab && tab.needsPerm)) petSync("perm");
  else if (tab && tab.running) petSync("running");
  else petSync("idle");
}

// 拖拽移动 + 云朵重力：横向位置存 ui.json（pet_x）；纵向有重力——
// 松手后小羊以「云朵的方式」缓缓飘落（加速度小、终端速度低、带摇曳），
// 软着陆在对话区底部。有重力在，纵向位置便不持久化，每次都落到底。
// #chat 是定位容器；界面缩放下物理像素与布局坐标差 uiScale 倍，先除回。
let petPos = null; // 最近应用的 {x, y}；null = 默认右下角（CSS 自动跟随容器）
function petApplyPos(x, y) {
  if (!petEl) return;
  if (x == null || y == null) {
    petEl.style.left = petEl.style.top = petEl.style.right = petEl.style.bottom = "";
    petPos = null;
    return;
  }
  petEl.style.left = Math.round(x) + "px";
  petEl.style.top = Math.round(y) + "px";
  petEl.style.right = "auto";
  petEl.style.bottom = "auto";
  petPos = { x, y };
}

// 容器边界与「地面」：地面 = #chat 底部（宠物自身宽度随 pet_scale 变，留一点边距）
// ok=false 表示容器当前不可见（切到设置页时 #view-chat display:none，尺寸塌成 0）——
// 此时任何「按地面归位」的计算都必须跳过，否则地面会算成负数、把小羊顶到顶部。
function petBounds() {
  const r = document.getElementById("chat").getBoundingClientRect();
  const cw = r.width / uiScale, ch = r.height / uiScale;
  const pw = (petEl && petEl.offsetWidth) || 76;
  return {
    maxX: Math.max(4, cw - pw - 6),
    floor: Math.max(4, ch - pw - 4),
    ok: cw > 20 && ch > 20,
  };
}

function petClampPos(x, y) {
  const b = petBounds();
  return [Math.max(4, Math.min(x, b.maxX)), Math.max(4, Math.min(y, b.floor))];
}

// —— 云朵重力引擎 ——
const PET_G = 300;     // 重力加速度 px/s²：云朵体质，比真实世界温柔一个量级
const PET_TERM = 140;  // 终端速度 px/s：空气阻力大，飘着下而不是砸下去
const PET_SWAY = 26;   // 横向摇曳速度峰值 px/s：像被风轻轻托着摆

let petFall = null;    // {x, y, vy, t}
let petFallRaf = null;

function petStopFall() {
  petFall = null;
  if (petFallRaf) { cancelAnimationFrame(petFallRaf); petFallRaf = null; }
  if (petEl) petEl.classList.remove("falling");
}

function petStartFall(x, y, opts = {}) {
  if (!petEl) return;
  petStopFall();
  const [cx, cy] = petClampPos(x, y);
  petFall = { x: cx, y: cy, vy: 10, t: 0 };
  petEl.classList.add("falling");
  if (opts.quiet !== true) petSay("松手啦，我飘下去～", 3600);
  let last = performance.now();
  const step = (now) => {
    if (!petFall) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    petFall.t += dt;
    petFall.vy = Math.min(PET_TERM, petFall.vy + PET_G * dt);
    petFall.y += petFall.vy * dt;
    // 摇曳：低频正弦横摆（不改变朝向，纯平移）
    const b = petBounds(); // 逐帧取：权限条弹出等会改变容器高度
    if (!b.ok) {
      // 容器不可见（切到设置页）：暂停下落，位置原地冻结，切回来继续飘
      last = now;
      petFallRaf = requestAnimationFrame(step);
      return;
    }
    petFall.x = Math.max(4, Math.min(petFall.x + Math.sin(petFall.t * 2.2) * PET_SWAY * dt, b.maxX));
    if (petFall.y >= b.floor) { petLand(petFall.x, b); return; }
    petApplyPos(petFall.x, petFall.y);
    petFallRaf = requestAnimationFrame(step);
  };
  petFallRaf = requestAnimationFrame(step);
}

// 软着陆：底部 Q 弹挤一下 + 落地台词；只有横向位置值得存档
function petLand(x, b) {
  petStopFall();
  const lx = Math.max(4, Math.min(Math.round(x), b.maxX));
  petApplyPos(lx, b.floor);
  petEl.classList.remove("land");
  void petEl.offsetWidth;
  petEl.classList.add("land");
  setTimeout(() => petEl && petEl.classList.remove("land"), 520);
  petSay(petPick(PET_LINES.land));
  saveUiPrefs({ pet_x: lx, pet_y: null });
}

// —— 地面归位：#chat 尺寸变化（拖输入栏高度 / 侧栏宽 / 右面板 / 权限条弹出）时地面会移动 ——
// 地面升高（对话区变矮）→ 把小羊托上新的地面；地面降低（对话区变高）→ 小羊悬空了，
// 等拖动停稳后用云朵重力再飘落一次补位。拖拽中 / 下落中不管（它们逐帧取边界，天然自适应）。
// 容器不可见（切成设置页）时一律跳过——位置原样保留，切回对话页再归位。
function petSettle() {
  if (!petEl || !petOn || petDrag || petFall || !petPos) return;
  const b = petBounds();
  if (!b.ok) return;
  const x = Math.max(4, Math.min(Math.round(petPos.x), b.maxX));
  if (petPos.y > b.floor + 1) {
    petApplyPos(x, b.floor); // 地面托上来了：直接贴上新地面
  } else if (petPos.y < b.floor - 2) {
    petStartFall(petPos.x, petPos.y, { quiet: true }); // 悬空了：云朵补落一次
  } else {
    petApplyPos(x, petPos.y); // 仅横向收敛（侧栏拖动改宽度）
  }
}

if (petEl && typeof ResizeObserver === "function") {
  let petSettleTimer = null;
  new ResizeObserver(() => {
    if (!petOn || petDrag || petFall || !petPos) return;
    const b = petBounds();
    if (!b.ok) return; // 不可见（设置页）：地面无意义，别把小羊顶到顶部
    if (petPos.y < b.floor - 2) {
      // 地面降下去了：拖动期间原地等待，停稳后再校验一次（方向可能反转）并云朵飘落补位
      clearTimeout(petSettleTimer);
      petSettleTimer = setTimeout(() => {
        clearTimeout(petSettleTimer);
        petSettle();
      }, 240);
    } else {
      clearTimeout(petSettleTimer);
      petSettle();
    }
  }).observe(document.getElementById("chat"));
}

let petDrag = null; // {sx, sy, dx, dy, moved}
if (petEl) {
  petEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !petOn) return;
    petStopFall(); // 下落途中可以一把抓住
    const rect = petEl.getBoundingClientRect();
    petDrag = {
      sx: e.clientX, sy: e.clientY,
      dx: (e.clientX - rect.left) / uiScale,
      dy: (e.clientY - rect.top) / uiScale,
      moved: false,
    };
    petEl.setPointerCapture(e.pointerId);
  });
  petEl.addEventListener("pointermove", (e) => {
    if (!petDrag) return;
    if (!petDrag.moved && Math.hypot(e.clientX - petDrag.sx, e.clientY - petDrag.sy) < 5) return;
    petDrag.moved = true;
    petEl.classList.add("dragging");
    const cr = document.getElementById("chat").getBoundingClientRect();
    const [x, y] = petClampPos(
      e.clientX / uiScale - cr.left / uiScale - petDrag.dx,
      e.clientY / uiScale - cr.top / uiScale - petDrag.dy
    );
    petDrag.x = x; petDrag.y = y;
    petApplyPos(x, y);
  });
  const petPointerUp = () => {
    if (!petDrag) return;
    const d = petDrag;
    petDrag = null;
    petEl.classList.remove("dragging");
    if (d.moved) {
      petStartFall(d.x, d.y); // 云朵重力：从松手处缓缓飘落
      return;
    }
    // 没拖动 = 原地点击：挤脸 + 搭话
    petEl.classList.remove("poke");
    void petEl.offsetWidth;
    petEl.classList.add("poke");
    setTimeout(() => petEl.classList.remove("poke"), 360);
    petSay(petPick(PET_LINES.idle));
  };
  petEl.addEventListener("pointerup", petPointerUp);
  petEl.addEventListener("pointercancel", () => { petDrag = null; petEl.classList.remove("dragging"); });
  // 双击回到默认位置（对话区右下角）
  petEl.addEventListener("dblclick", () => {
    petStopFall();
    petApplyPos(null, null);
    saveUiPrefs({ pet_x: null, pet_y: null });
    petSay("回家咯～");
  });
  // 待机时偶尔冒个泡（约 75s 一次、30% 概率，不吵）
  setInterval(() => {
    if (!petOn || document.hidden || running || permRequest) return;
    if (Math.random() < 0.3) petSay(petPick(PET_LINES.idle));
  }, 75000);
}

function renderPetToggle() {
  const t = document.getElementById("pet-toggle");
  if (t) t.checked = petOn;
}
const petToggle = document.getElementById("pet-toggle");
if (petToggle) {
  petToggle.onchange = () => {
    petOn = petToggle.checked;
    petEl.classList.toggle("hidden", !petOn);
    if (!petOn) { petStopFall(); petBubble.classList.add("hidden"); }
    saveUiPrefs({ pet: petOn ? 1 : 0 });
  };
}

// ---------- 宠物大小（pet_scale，60–140%，100 = 默认）：±步进 + 双击回默认 ----------
const PET_SCALE_DEFAULT = 100, PET_SCALE_MIN = 60, PET_SCALE_MAX = 140, PET_SCALE_STEP = 10;
let petScale = PET_SCALE_DEFAULT;

function applyPetScale(pct) {
  petScale = Math.round(Math.min(PET_SCALE_MAX, Math.max(PET_SCALE_MIN, pct)));
  // CSS 基准宽 76px（#chat-pet），#pet-img 宽 100% 跟着缩，动画/阴影都不受影响
  if (petEl) petEl.style.width = Math.round(76 * petScale / 100) + "px";
  const label = document.getElementById("pet-scale-label");
  if (label) label.textContent = petScale + "%";
  const out = document.getElementById("btn-pet-out");
  const inn = document.getElementById("btn-pet-in");
  if (out) out.disabled = petScale <= PET_SCALE_MIN;
  if (inn) inn.disabled = petScale >= PET_SCALE_MAX;
}

function changePetScale(dir) {
  applyPetScale(petScale + dir * PET_SCALE_STEP);
  // 100% 是默认：删键而非存 100
  saveUiPrefs({ pet_scale: petScale === PET_SCALE_DEFAULT ? null : petScale });
}

const petScaleOut = document.getElementById("btn-pet-out");
if (petScaleOut) petScaleOut.onclick = () => changePetScale(-1);
const petScaleIn = document.getElementById("btn-pet-in");
if (petScaleIn) petScaleIn.onclick = () => changePetScale(1);
const petScaleLabel = document.getElementById("pet-scale-label");
if (petScaleLabel) {
  petScaleLabel.title = "双击回默认（100%）";
  petScaleLabel.ondblclick = () => {
    applyPetScale(PET_SCALE_DEFAULT);
    saveUiPrefs({ pet_scale: null });
  };
}

async function send() {
  const input = document.getElementById("input");
  const text = input.value.trim();
  const images = pendingImages.slice();
  const refs = pendingRefs.slice();
  if (!text && !images.length) return;
  // 切换项目进行中禁止发送：切换会整体重建会话标签与日志，此时发消息
  // 气泡会落进已被抛弃的旧日志里（用户看不到），回复也可能路由错乱
  if (switchingProject) {
    addNotice("正在切换项目，请等切换完成再发送");
    return;
  }
  if (images.length && !curSupportsVision) {
    addNotice(`当前模型「${curProviderName}/${curModelName}」标记为不支持图片输入，` +
      "图片发不出去。请先在输入框右侧切换成多模态模型；" +
      "若该模型其实支持看图，可到 设置 · 模型服务 里打开「模型支持图片输入」。");
    return; // 留在输入框里，别把用户贴的图清掉
  }
  // 无模型的机器先把人引去配置，而不是入队一轮注定失败的发送（产生会话、
  // 气泡、运行态一整套残留）。本地缓存可能过期：拦截前先向后端要一次最新判定，
  // 刚在设置页/别的窗口配好 Key 的场景不会被旧状态挡住。
  if (providerBroken) {
    try {
      const fresh = await request("boot");
      providerBroken = !fresh.provider || !!fresh.provider_error;
    } catch (e) { /* 后端够不着时按本地缓存判定 */ }
    if (providerBroken) {
      addNotice("还没有可用的模型服务——打开 ⚙ 设置 · 模型服务，选一个服务粘贴 API Key 后再发送。");
      return;
    }
  }
  recordInputHistory(text); // 输入历史：供空输入按 ↑ 回看
  const tab = activeTab;
  // 新标签（无会话）：先向后端申请一个全新会话，避免消息落进旧的活动会话。
  // 用户已经在标签上预命名过的（titleFixed），名字随创建一起落库，并且首轮
  // 不再让模型自动生成标题——不然用户起的名字一发消息就被冲掉。
  if (tab && !tab.sid) {
    const preNamed = tab.titleFixed && tab.title ? tab.title : "";
    try {
      const s = await request("session.new", preNamed ? { title: preNamed } : {});
      tab.sid = s.id;
      tab.title = tab.title || text.slice(0, 20) || "新会话";
      currentSessionId = s.id;
      if (tab.logEl.querySelector(".welcome")) tab.logEl.innerHTML = ""; // 清掉欢迎页
      renderTabs();
      tab.firstSend = !preNamed; // 预命名过：首轮不自动起标题（尊重用户命名）
    } catch (e) {
      addNotice("新建会话失败: " + e.message);
      return;
    }
  }
  input.value = "";
  autoGrowInput();
  clearPendingImages();
  clearPendingRefs();
  hideInputMenu();
  addUser(text, images, refs);
  // 实时用户消息也挂操作（复制/编辑）；seq 未知，编辑走「最后一条用户消息」的后端兜底
  attachMsgOps(curLog().lastElementChild, tab, "user", () => text);
  if (tab.running) {
    // 对标 Claude Code 消息队列：工作中继续发消息会排队，本轮结束自动执行
    pendingTurns += 1;
    const chip = document.createElement("div");
    chip.className = "queue-chip";
    chip.textContent = `⏳ 已排队（当前轮结束后自动执行）`;
    curLog().appendChild(chip);
    scrollLog();
  } else {
    setRunning(true);
  }
  try {
    const rtThisTurn = rtOn;
    let compareThisTurn = false;
    if (rtThisTurn) {
      const cb = document.getElementById("rt-compare");
      compareThisTurn = !!(cb && cb.checked);
    }
    const membersThisTurn = rtThisTurn ? rtMembers : null;
    // 圆桌是一次性开关：发送后复位
    setRtOn(false);
    // 辩论轮数 / 主席出草稿：弹层里的本轮值优先于配置（config 作默认值）
    const r = await request("chat.send", {
      text,
      session_id: tab.sid || undefined,
      wants_title: tab.firstSend === true,
      plan_mode: workMode === "plan",
      roundtable: rtThisTurn,
      members: membersThisTurn || undefined,
      compare: compareThisTurn || undefined,
      debate_rounds: rtThisTurn ? rtDebate : undefined,
      chair_answers: rtThisTurn ? rtChair : undefined,
      images: images.length ? images : undefined,
      refs: refs.length ? refs : undefined,
    });
    setContextUsage(r.context_tokens || 0, r.context_limit || 0, tab);
    if (r.context_detail) setContextDetail(r.context_detail, tab);
    tab.firstSend = false;
    if (r.session_id && !tab.sid) {
      tab.sid = r.session_id;
      currentSessionId = r.session_id;
      renderTabs();
    }
    if (!tab.title) {
      tab.title = (sessionMeta[r.session_id] || {}).title || text.slice(0, 20);
      renderTabs();
    }
    if (r.plan_mode && tab.lastAssistantText.trim()) addPlanActions(tab.lastAssistantText);
    if (r.checkpoint) addCheckpointBar(r.checkpoint);
  } catch (e) {
    addNotice("出错: " + e.message);
    finishEta(tab);
    setRunning(false, tab);
  }
}

// ---------- 图片附件：粘贴 / 拖拽 / 托盘预览，随下一条消息发送 ----------
const IMAGE_TYPES = { "image/png": 1, "image/jpeg": 1, "image/webp": 1, "image/gif": 1 };
const IMAGE_MAX_BYTES = 4 * 1024 * 1024;
const IMAGE_MAX_COUNT = 4;
let pendingImages = []; // [{ media_type, data(base64) }]

function renderImageTray() {
  const tray = document.getElementById("image-tray");
  tray.classList.toggle("hidden", !pendingImages.length);
  tray.innerHTML = "";
  pendingImages.forEach((im, i) => {
    const cell = document.createElement("div");
    cell.className = "image-thumb";
    cell.innerHTML = `<img src="data:${im.media_type};base64,${im.data}" alt="附件${i + 1}">` +
      `<button class="thumb-del" title="移除">✕</button>`;
    cell.querySelector(".thumb-del").onclick = () => {
      pendingImages.splice(i, 1);
      renderImageTray();
    };
    tray.appendChild(cell);
  });
}

function clearPendingImages() {
  pendingImages = [];
  renderImageTray();
}

function addPendingImageFile(file) {
  if (!IMAGE_TYPES[file.type]) {
    addNotice("仅支持 PNG / JPEG / WebP / GIF 图片");
    return;
  }
  if (file.size > IMAGE_MAX_BYTES) {
    addNotice(`图片太大（${(file.size / 1048576).toFixed(1)}MB），最大 4MB`);
    return;
  }
  if (pendingImages.length >= IMAGE_MAX_COUNT) {
    addNotice("一条消息最多带 4 张图片");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const url = String(reader.result || "");
    const m = /^data:([^;]+);base64,(.+)$/.exec(url);
    if (!m) return;
    pendingImages.push({ media_type: m[1], data: m[2] });
    renderImageTray();
  };
  reader.readAsDataURL(file);
}

const composerInput = document.getElementById("input");
composerInput.addEventListener("paste", (e) => {
  for (const item of e.clipboardData?.items || []) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const f = item.getAsFile();
      if (f) {
        e.preventDefault();
        addPendingImageFile(f);
      }
    }
  }
});
const composerEl = document.getElementById("composer");
["dragover", "drop"].forEach((evt) => {
  composerEl.addEventListener(evt, (e) => {
    e.preventDefault();
    if (evt !== "drop") return;
    const dropped = [...(e.dataTransfer?.files || [])];
    if (!dropped.length) return;
    // 图片进附件托盘；其他文件转 @ 引用（浏览器 Drop 事件只给文件名不给路径，
    // 故按文件名匹配项目文件索引，命中即插入 @项目内相对路径）
    dropped.filter((f) => f.type.startsWith("image/")).forEach((f) => addPendingImageFile(f));
    const others = dropped.filter((f) => !f.type.startsWith("image/"));
    if (others.length) attachDroppedAsMentions(others);
  });
});
async function attachDroppedAsMentions(files) {
  const index = await updateFileIndex();
  for (const f of files) {
    const name = f.name.toLowerCase();
    const hits = index.filter((p) => {
      const lp = p.toLowerCase();
      return lp === name || lp.endsWith("/" + name);
    });
    if (hits.length === 1) {
      const cur = inputEl.value;
      const sep = cur && !/\s$/.test(cur) ? " " : "";
      inputEl.value = cur + sep + "@" + hits[0] + " ";
    } else if (hits.length > 1) {
      addNotice(`「${f.name}」在项目里有 ${hits.length} 个同名文件，请输入 @ 手动选择要引用哪一个`);
    } else {
      addNotice(`「${f.name}」不在当前项目里——拖拽拿不到文件真实路径，` +
        "请点输入框左侧的「添加文件」按钮选它（支持项目外的文件与多选）。");
    }
  }
  autoGrowInput();
  inputEl.focus();
}

// 历史消息渲染：会话恢复/后台标签首次激活时用
function messageText(message) {
  if (!message || !Array.isArray(message.content)) return "";
  return message.content.filter((b) => b.type === "text").map((b) => b.text || "").join("");
}

function addAssistantDone(text, rtMeta, tab, seq, thinking, thinkingMs, durationMs, estimate) {
  const t = tab || curTab();
  const d = document.createElement("div");
  d.className = "msg assistant";
  if (thinking) addThinkingDone(thinking, t, thinkingMs);
  // 历史恢复的用时芯片：重建在回答上方（与流式期间的芯片位置一致）。
  // 旧消息没有 duration_ms（0）时不渲染，不给历史凭空造一条用时记录。
  if (durationMs) {
    const chip = document.createElement("div");
    chip.className = "eta-chip done";
    const lo = (estimate && estimate.min_seconds) || 0;
    const hi = (estimate && estimate.max_seconds) || 0;
    if (estimate && estimate.basis) chip.title = "预估依据：" + estimate.basis;
    chip.textContent = hi
      ? `⏱ 用时 ${fmtEtaDur(durationMs / 1000)} · 预估 ${fmtEtaRange(lo, hi)}`
      : `⏱ 用时 ${fmtEtaDur(durationMs / 1000)}`;
    curLog().appendChild(chip);
  }
  d.innerHTML = `<div class="md">${renderMarkdown(text || "")}</div>`;
  if (rtMeta) {
    d._rtMeta = rtMeta; // 重新生成时据此重跑同样的圆桌配置
    if (rtMeta.members) addRtBadge(d, rtMeta);
  }
  curLog().appendChild(d);
  renderMermaidIn(d);
  highlightCodeIn(d);
  decorateFinalMessage(d, tab, () => text || "", seq);
}

// ---------- 复制：助手消息整段复制 + 代码块一键复制 ----------
function copyTextToClipboard(text) {
  const legacy = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* 尽力而为 */ }
    ta.remove();
    return Promise.resolve();
  };
  // http://局域网IP 不是安全上下文，navigator.clipboard 不存在 → 走 execCommand 兜底
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).catch(legacy);
  }
  return legacy();
}

// ---------- 消息级操作：统一放消息下方，简笔图标（助手：复制/分叉/回退/重新生成；用户：复制/编辑） ----------
const MSG_OPS_ICONS = {
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15.5 4.5l4 4L8.5 19.5l-5 1.2 1.2-5L15.5 4.5z"/></svg>',
  fork: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.2"/><circle cx="6" cy="18" r="2.2"/><circle cx="18" cy="12" r="2.2"/><path d="M6 8.2v7.6"/><path d="M8.1 6.9c4.2.5 7.5 2 8.8 4.2"/></svg>',
  rollback: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9h10a5 5 0 0 1 0 10h-4"/><path d="M8 5 4 9l4 4"/></svg>',
  regen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-1.7 6.2"/><path d="M20 5v6h-6"/></svg>',
  snip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 3.5h11V21l-5.5-4-5.5 4V3.5z"/></svg>',
};

function attachMsgOps(el, tab, kind, getText) {
  if (el.querySelector(".msg-ops")) return;
  const seq = el.dataset.seq ? Number(el.dataset.seq) : null;
  const ops = document.createElement("span");
  ops.className = "msg-ops";
  ops.addEventListener("click", (e) => e.stopPropagation());
  const mk = (icon, title, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.innerHTML = MSG_OPS_ICONS[icon];
    b.title = title;
    b.onclick = async () => {
      await fn();
      if (icon === "copy") {
        b.textContent = "✓";
        setTimeout(() => { b.innerHTML = MSG_OPS_ICONS.copy; }, 1200);
      }
    };
    ops.appendChild(b);
  };
  if (kind === "user") {
    mk("copy", "复制这条消息", async () => {
      await copyTextToClipboard((getText && getText()) || el.textContent.trim());
    });
    mk("edit", "编辑这条消息并重新发送", () => msgEditResend(tab, el, seq));
    mk("snip", "存为提示词（存进设置 · 提示词，输入 ~ 可调用）",
      () => saveAsSnippet((getText && getText()) || el.textContent.trim()));
  } else {
    mk("copy", "复制整条回答（Markdown 原文）", async () => {
      await copyTextToClipboard(getText());
    });
    mk("fork", "从这里分叉出新会话（不影响本会话）", () => msgFork(tab, seq));
    mk("rollback", "回退到提问前（删除这条回答及之后的内容）", () => msgRollback(tab, el, seq));
    mk("regen", "重新生成这条回答", () => msgRegenerate(tab, el, seq));
  }
  el.appendChild(ops);
}

function attachCodeCopy(el) {
  el.querySelectorAll(".md pre").forEach((pre) => {
    if (pre.querySelector(".code-copy")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy";
    btn.textContent = "⧉";
    btn.title = "复制代码";
    btn.onclick = async (e) => {
      e.stopPropagation();
      await copyTextToClipboard(pre.querySelector("code")?.textContent ?? pre.textContent);
      btn.textContent = "✓";
      setTimeout(() => { btn.textContent = "⧉"; }, 1500);
    };
    pre.appendChild(btn);
  });
}

function decorateFinalMessage(el, tab, getText, seq) {
  if (!el) return;
  if (seq) el.dataset.seq = String(seq);
  attachMsgOps(el, tab || curTab(), "assistant", getText);
  attachCodeCopy(el);
}

// 长会话分批渲染的阀值与每帧份量。
// 一次同步画完数千条消息（每条还要跑 markdown / mermaid / 代码高亮）会把主线程
// 占满，切到大会话时就是一段白屏。分批把工作切成多帧，让浏览器有机会先画出首屏。
// 阀值以下的短会话仍一次画完（不多付一帧的延迟），行为与以前完全一致。
const HISTORY_SYNC_LIMIT = 200;
const HISTORY_CHUNK = 100;

function renderHistory(tab, messages) {
  const all = messages || [];
  // 渲染代际：期间又渲染过同一条聊天流（切标签 / 重进会话）时，旧的批处理作废
  const gen = (tab._historyGen = (tab._historyGen || 0) + 1);
  tab.logEl.innerHTML = "";
  tab._toolCards = new Map(); // 在途工具卡随流清空，避免持已分离节点

  if (all.length <= HISTORY_SYNC_LIMIT) {
    paintHistorySlice(tab, all, 0, all.length);
    finishHistoryRender(tab, true);
    return;
  }

  // 先同步画头一段：openTabForSession 会在 renderHistory 返回后马上看
  // children.length 判断是否空会话，零节点会被误当成空会话而错误地弹欢迎页
  const first = Math.min(HISTORY_CHUNK, all.length);
  paintHistorySlice(tab, all, 0, first);
  tab.logEl.scrollTop = tab.logEl.scrollHeight;
  let i = first;
  const step = () => {
    if (gen !== tab._historyGen) return; // 已被更新的渲染取代
    // 追加前先看用户是不是在底部：在底部就继续跟随最新内容，
    // 用户已经往上翻了就不抢他的位置（分批期间尤其重要）
    const atBottom = tab.logEl.scrollTop + tab.logEl.clientHeight >= tab.logEl.scrollHeight - 4;
    const end = Math.min(i + HISTORY_CHUNK, all.length);
    paintHistorySlice(tab, all, i, end);
    if (atBottom) tab.logEl.scrollTop = tab.logEl.scrollHeight;
    i = end;
    if (i < all.length) requestAnimationFrame(step);
    else finishHistoryRender(tab, atBottom);
  };
  requestAnimationFrame(step);
}

// 把 messages[start:end) 画进当前聊天流（调用方负责 withTab 路由与滚动位置）。
function paintHistorySlice(tab, messages, start, end) {
  withTab(tab, () => {
    for (let i = start; i < end; i++) {
      const m = messages[i];
      let el = null;
      if (m.role === "user") {
        addUser(m.text, m.images);
        el = curLog().lastElementChild;
      } else if (m.role === "assistant") {
        // 圆桌融合消息：先重建成员草稿卡（默认折叠），再放回答本体
        if (m.roundtable && m.roundtable.mode === "roundtable" && (m.roundtable.members || []).length) {
          const rtCard = buildRtReplayCard(m.roundtable);
          curLog().appendChild(rtCard);
          renderMermaidIn(rtCard);
          highlightCodeIn(rtCard);
        }
        addAssistantDone(
          m.text, m.roundtable, tab, m.seq, m.thinking,
          m.thinking_ms, m.duration_ms, m.estimate,
        );
        el = curLog().lastElementChild;
      }
      if (el && m.seq) el.dataset.seq = String(m.seq);
    }
  });
}

// forceBottom：定稿后是否强制滚到底。短会话（一次画完）保持原行为；
// 长会话分批画时，用户很可能在分批期间往上翻了，这时不能把他拉回底部。
function finishHistoryRender(tab, forceBottom) {
  attachHistoryOps(tab);
  // 滚动容器是 .chat-log（#chat 外层 overflow:hidden），滚 chatBox 从不生效——
  // 历史渲染完其实一直没滚到底
  if (forceBottom) tab.logEl.scrollTop = tab.logEl.scrollHeight;
}

// ---------- 消息级操作：历史恢复后给用户消息挂（复制/编辑）；助手消息在渲染时已挂 ----------
function attachHistoryOps(tab) {
  tab.logEl.querySelectorAll(".msg.user[data-seq]").forEach((el) =>
    attachMsgOps(el, tab, "user"));
}

async function msgEditResend(tab, el, seq) {
  if (tab.running) { addNotice("等当前轮结束再操作"); return; }
  try {
    const r = await request("session.truncate", {
      id: tab.sid, mode: "edit", seq: seq || undefined,
    });
    let drop = false;
    [...tab.logEl.children].forEach((node) => {
      if (node === el) drop = true;
      if (drop) node.remove();
    });
    document.getElementById("input").value = r.text || el.textContent.trim();
    document.getElementById("input").focus();
    addNotice("已回退这条消息，编辑后直接发送");
  } catch (e) {
    addNotice("回退失败: " + e.message);
  }
}

async function msgRegenerate(tab, el, seq) {
  if (tab.running) { addNotice("等当前轮结束再操作"); return; }
  const rtMeta = el && el._rtMeta;
  try {
    await request("session.truncate", { id: tab.sid, mode: "regen", seq: seq || undefined });
    // 清掉这条回答对应的旧圆桌卡（历史回放里卡片紧挨在消息前面）
    const prevEl = el && el.previousElementSibling;
    if (prevEl && prevEl.classList && prevEl.classList.contains("roundtable")) prevEl.remove();
    let drop = false;
    [...tab.logEl.children].forEach((node) => {
      // 与 msgRollback 同一语义：旧回答及之后的内容都要从 DOM 移除
      //（此前 `return` 跳过了 el 本身的移除，重新生成后会重复显示新旧两条）
      if (node === el) drop = true;
      if (drop) node.remove();
    });
    setRunning(true, tab);
    const params = {
      text: "", session_id: tab.sid, regenerate: true,
      plan_mode: workMode === "plan",
    };
    // 圆桌回答的重新生成：沿用原配置重跑同样的圆桌（成员/融合或对比/辩论轮数）
    if (rtMeta && (rtMeta.members || []).length) {
      params.roundtable = true;
      params.members = rtMeta.members.map((m) => {
        const item = { provider: m.provider, model: m.model };
        return m.role ? Object.assign(item, { role: m.role }) : item;
      });
      if (rtMeta.mode === "compare") {
        params.compare = true;
        params.chair_answers = false; // 只重跑这一个成员，别把主席再拉进来
      } else {
        params.chair_answers = rtMeta.chair_answers !== false;
      }
      params.debate_rounds = rtMeta.debate_rounds || 0;
    }
    await request("chat.send", params);
    setContextUsage(0, 0, tab);
  } catch (e) {
    addNotice("重新生成失败: " + e.message);
    setRunning(false, tab);
  }
}

async function msgFork(tab, seq) {
  if (tab.running) { addNotice("等当前轮结束再操作"); return; }
  try {
    const r = await request("session.fork", { id: tab.sid, seq: seq || undefined });
    openTabForSession(r.id, r.title, { withMessages: r.messages || [] });
    addNotice(`已分叉新会话（复制 ${r.copied} 条消息），原会话不受影响`);
    refreshSessions();
  } catch (e) {
    addNotice("分叉失败: " + e.message);
  }
}

// 回退：删掉这条回答及之后的内容，保留提问（不自动重发；想重跑用「重新生成」）
async function msgRollback(tab, el, seq) {
  if (tab.running) { addNotice("等当前轮结束再操作"); return; }
  try {
    await request("session.truncate", { id: tab.sid, mode: "regen", seq: seq || undefined });
    let drop = false;
    [...tab.logEl.children].forEach((node) => {
      if (node === el) drop = true;
      if (drop) node.remove();
    });
    addNotice("已回退：这条回答及之后的内容已删除，提问保留");
  } catch (e) {
    addNotice("回退失败: " + e.message);
  }
}

// 回滚检查点；文件在快照保存后又被改过（如其他并行会话/任务写了同一文件）时，
// 后端不动磁盘先返回 conflict，这里把冲突文件列出来请用户确认，再强制恢复
async function restoreCheckpoint(id) {
  let r = await request("checkpoint.restore", { id });
  if (r.conflict) {
    const names = (r.files || []).map(fileName).join("、");
    const ok = confirm(
      `以下 ${(r.files || []).length} 个文件在保存该检查点之后又被修改过` +
      `（可能是其他并行任务写入）：\n${names}\n\n` +
      "强制恢复会用当时的旧内容覆盖这些改动，确定吗？"
    );
    if (!ok) throw new Error("已取消：文件有后续改动，未恢复");
    r = await request("checkpoint.restore", { id, force: true });
  }
  return r;
}

// 检查点条：本轮改动过文件时出现，可一键回滚到改前状态
function addCheckpointBar(cp) {
  const bar = document.createElement("div");
  bar.className = "checkpoint-bar";
  const label = document.createElement("span");
  label.innerHTML = `💾 本轮改动 <b>${cp.paths.length}</b> 个文件：`;
  const files = document.createElement("span");
  files.className = "cp-files";
  files.textContent = cp.paths.map((p) => p.split(/[\\/]/).pop()).join("、");
  files.title = cp.paths.join("\n");
  const btn = document.createElement("button");
  btn.className = "btn-ghost";
  btn.textContent = "↩ 撤销本轮改动";
  btn.title = "把这些文件恢复到本轮改动前的状态（run_command 造成的改动不在追踪范围）";
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "回滚中…";
    try {
      const r = await restoreCheckpoint(cp.id);
      bar.classList.add("done");
      btn.remove();
      label.innerHTML = `↩ 已回滚 ${r.files.length} 个文件到改前状态`;
      addNotice("已撤销本轮文件改动；如需继续任务，Agent 会重新读取最新文件。");
      // 磁盘被回滚：文件树缓存失效（开着文件标签就直接刷新，与 Agent 写文件后的联动一致）
      filesLoaded = false;
      if (rightActive === "files" && !rightPanel.classList.contains("hidden")) loadFiles(true);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "↩ 撤销本轮改动";
      addNotice("回滚失败: " + e.message);
    }
  };
  bar.append(label, files, btn);
  curLog().appendChild(bar);
  scrollLog();
}

// 规划模式结果：在计划下方放「按此计划执行」按钮
function addPlanActions(plan) {
  const bar = document.createElement("div");
  bar.className = "msg plan-actions";
  const btn = document.createElement("button");
  btn.className = "btn-primary";
  btn.textContent = "▶ 按此计划执行";
  btn.onclick = () => {
    bar.remove();
    setWorkMode("execute"); // 批准计划 = 回到执行模式
    document.getElementById("input").value =
      "请严格按以下计划执行：\n\n" + plan;
    send();
  };
  const hint = document.createElement("span");
  hint.className = "dim small";
  hint.textContent = " 满意就一键交给 Agent 执行；或继续补充需求。";
  bar.appendChild(btn);
  bar.appendChild(hint);
  curLog().appendChild(bar);
  scrollLog();
}

document.getElementById("btn-send").onclick = send;
document.getElementById("btn-stop").onclick = () =>
  request("stop", activeTab && activeTab.sid ? { session_id: activeTab.sid } : {});

// ---------- 输入浮层：/ 命令 与 @ 文件提及（对标 Claude Code / Codex 输入体验） ----------
const inputEl = document.getElementById("input");

// 输入框随内容自动增高：最多 6 行，超出内部滚动；拖了 --cp-h 时输入区仍由 flex-grow 填满
const INPUT_MAX_LINES = 6;
function autoGrowInput() {
  const cs = getComputedStyle(inputEl);
  const lh = parseFloat(cs.lineHeight) || 21;
  const cap = Math.ceil(lh * INPUT_MAX_LINES +
    parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + 3); // +上下边框
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, cap) + "px";
  inputEl.style.overflowY = inputEl.scrollHeight > cap + 1 ? "auto" : "hidden";
}
inputEl.addEventListener("input", autoGrowInput);
autoGrowInput();

// ---------- 「& 引用对话」：输入 & 唤起会话选择器，选中的对话随消息发给后端注入上下文 ----------
const refMenu = document.getElementById("ref-menu");
const refTray = document.getElementById("ref-tray");
let pendingRefs = [];   // 已选引用 [{ id, title }]，随下一条消息发送
let refCandidates = []; // 弹层候选（session.list，已排除当前会话）
let refMatches = [];    // 按 & 后的查询串过滤后的候选
let refPick = 0;        // 键盘高亮行
let refLoaded = false;  // 候选已拉取（弹层开着时继续输入只做本地过滤）

function renderRefTray() {
  refTray.classList.toggle("hidden", !pendingRefs.length);
  refTray.innerHTML = "";
  pendingRefs.forEach((r, i) => {
    const chip = document.createElement("span");
    chip.className = "ref-chip";
    chip.title = "引用对话的内容会随这条消息一起给 Agent";
    chip.textContent = `🔗 ${r.title || "未命名会话"}`;
    const del = document.createElement("button");
    del.className = "ref-chip-del";
    del.textContent = "✕";
    del.title = "移除引用";
    del.onclick = () => { pendingRefs.splice(i, 1); renderRefTray(); };
    chip.appendChild(del);
    refTray.appendChild(chip);
  });
}
function clearPendingRefs() { pendingRefs = []; renderRefTray(); }

// ---------- 选中回答片段 → 引用进输入框 ----------
// 在聊天流里选中一段文字后，选区旁浮出「❝ 引用」按钮；点击把选中内容以
// 「> 」引用块的形式追加进输入框（markdown blockquote，Agent 与气泡都能识别）。
const QUOTE_MAX_CHARS = 1200;   // 选区引用上限
const selQuoteBtn = document.createElement("button");
selQuoteBtn.id = "sel-quote-btn";
selQuoteBtn.type = "button";
selQuoteBtn.innerHTML = "❝ 引用到输入框";
selQuoteBtn.title = "把选中的内容以引用块插入输入框，再针对它提问";
selQuoteBtn.classList.add("hidden");
document.body.appendChild(selQuoteBtn);
// mousedown 阻止默认行为，否则点击按钮的瞬间浏览器会清掉选区、拿不到文本
selQuoteBtn.addEventListener("mousedown", (e) => e.preventDefault());
selQuoteBtn.addEventListener("click", () => {
  const sel = window.getSelection();
  const text = sel ? sel.toString() : "";
  hideSelQuoteBtn();
  if (!text.trim()) return;
  appendQuoteToInput(text, QUOTE_MAX_CHARS);
  if (sel) sel.removeAllRanges();
});

function hideSelQuoteBtn() { selQuoteBtn.classList.add("hidden"); }

/** 把一段文字转成 markdown 引用块并追加进输入框；超长截断并提示。返回是否写入。 */
function appendQuoteToInput(text, max = QUOTE_MAX_CHARS) {
  let t = String(text || "").replace(/\u00a0/g, " ").trim();
  if (!t) return false;
  let truncated = false;
  if (t.length > max) { t = t.slice(0, max).trimEnd() + "…"; truncated = true; }
  const block = t.split("\n").map((l) => (l.trim() ? "> " + l.trimEnd() : ">")).join("\n");
  const cur = inputEl.value;
  const sep = !cur ? "" : (cur.endsWith("\n\n") ? "" : (cur.endsWith("\n") ? "\n" : "\n\n"));
  inputEl.value = cur + sep + block + "\n\n";
  inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length);
  autoGrowInput();
  inputEl.focus();
  if (truncated) addNotice("引用内容过长，已截断到 " + max + " 字");
  return true;
}

function maybeShowSelQuoteBtn() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) { hideSelQuoteBtn(); return; }
  const node = sel.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  // 只对聊天流里的助手消息生效（流式中的回复内容还在变，不引用）
  if (!el || !chatBox.contains(el)) { hideSelQuoteBtn(); return; }
  const msg = el.closest ? el.closest(".msg") : null;
  if (!msg || !msg.classList.contains("assistant") || (activeTab && msg === activeTab.streamingEl)) {
    hideSelQuoteBtn();
    return;
  }
  const text = sel.toString().replace(/\u00a0/g, " ").trim();
  if (!text) { hideSelQuoteBtn(); return; }
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) { hideSelQuoteBtn(); return; }
  // 浮层与其它菜单同款：元素带 zoom，物理像素 rect 先除回 uiScale 再定位
  const layoutW = 118;
  const left = Math.min(
    Math.max(8, (rect.left + rect.width / 2) / uiScale - layoutW / 2),
    window.innerWidth / uiScale - layoutW - 8,
  );
  const top = Math.max(8, rect.top / uiScale - 42);
  selQuoteBtn.style.left = left + "px";
  selQuoteBtn.style.top = top + "px";
  selQuoteBtn.classList.remove("hidden");
}
document.addEventListener("selectionchange", () => {
  clearTimeout(selQuoteHideTimer);
  selQuoteHideTimer = setTimeout(maybeShowSelQuoteBtn, 120);
});
let selQuoteHideTimer = null;
// 聊天流滚动后选区位置失准：立即收掉，等下一次 selectionchange 再定位
chatBox.addEventListener("scroll", hideSelQuoteBtn, true);
window.addEventListener("resize", hideSelQuoteBtn);

// 光标前是否有未完成的 &token（& 需在行首或空白后，token 内不含空白与 &）
function refTokenAt() {
  const pos = inputEl.selectionStart ?? inputEl.value.length;
  const m = inputEl.value.slice(0, pos).match(/(?:^|\s)&([^&\s]*)$/);
  return m ? { start: pos - m[1].length - 1, query: m[1].toLowerCase() } : null;
}

function closeRefMenu() {
  refMenu.classList.add("hidden");
  refMenu.innerHTML = "";
  refLoaded = false;
  refCandidates = [];
}

function positionRefMenu() {
  const r = inputEl.getBoundingClientRect(); // 物理像素；弹层带 zoom，除回 uiScale
  refMenu.style.top = "auto";
  refMenu.style.bottom = (window.innerHeight / uiScale - r.top / uiScale + 6) + "px";
  refMenu.style.left = (r.left / uiScale) + "px";
}

function highlightRefPick() {
  const items = refMenu.querySelectorAll(".mm-item");
  items.forEach((el, i) => el.classList.toggle("active", i === refPick));
  if (items[refPick]) items[refPick].scrollIntoView({ block: "nearest" });
}

function paintRefMenu() {
  const tok = refTokenAt();
  const q = tok ? tok.query : "";
  refMatches = refCandidates.filter((s) => (s.title || "").toLowerCase().includes(q));
  refPick = 0;
  refMenu.innerHTML = "";
  if (!refMatches.length) {
    const empty = document.createElement("div");
    empty.className = "mm-empty";
    empty.textContent = refCandidates.length ? "没有匹配的对话" : "当前项目还没有其他对话";
    refMenu.appendChild(empty);
    return;
  }
  refMatches.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "mm-item";
    b.innerHTML = `<span class="mm-model">🔗 ${escapeHtml(s.title || "未命名会话")}</span>` +
      `<span class="mm-prov">${new Date(s.updated_at || Date.now()).toLocaleDateString()}</span>`;
    b.title = "把这条对话的记录引用给 Agent 参考";
    b.onmouseenter = () => { refPick = i; highlightRefPick(); };
    b.onclick = () => pickRef(s);
    refMenu.appendChild(b);
  });
  highlightRefPick();
}

async function ensureRefMenu() {
  if (!refLoaded) {
    try {
      const r = await request("session.list");
      const cur = activeTab && activeTab.sid;
      refCandidates = (r.sessions || []).filter((s) => s.id !== cur);
    } catch {
      refCandidates = [];
    }
    refLoaded = true;
  }
  positionRefMenu();
  refMenu.classList.remove("hidden");
  paintRefMenu();
}

function pickRef(s) {
  if (!pendingRefs.some((r) => r.id === s.id)) {
    pendingRefs.push({ id: s.id, title: s.title || "未命名会话" });
    renderRefTray();
  }
  const tok = refTokenAt();
  if (tok) {
    const pos = inputEl.selectionStart ?? inputEl.value.length;
    inputEl.value = inputEl.value.slice(0, tok.start) + inputEl.value.slice(pos);
    inputEl.setSelectionRange(tok.start, tok.start);
    autoGrowInput();
  }
  closeRefMenu();
  inputEl.focus();
}

inputEl.addEventListener("input", () => {
  if (refTokenAt()) ensureRefMenu();
  else closeRefMenu();
});

// 键盘劫持挂在 document 捕获阶段：同一元素的监听按注册顺序执行，
// 捕获阶段才能抢在既有「Enter 发送」监听之前把引用态下的按键吃掉
document.addEventListener("keydown", (e) => {
  if (refMenu.classList.contains("hidden") || e.target !== inputEl) return;
  if (e.key === "Escape" && !e.isComposing) {
    closeRefMenu();
    e.preventDefault(); e.stopPropagation();
  } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && refMatches.length) {
    refPick = e.key === "ArrowDown"
      ? (refPick + 1) % refMatches.length
      : (refPick - 1 + refMatches.length) % refMatches.length;
    highlightRefPick();
    e.preventDefault(); e.stopPropagation();
  } else if (e.key === "Enter" && !e.isComposing && refMatches.length) {
    pickRef(refMatches[refPick]);
    e.preventDefault(); e.stopPropagation();
  }
}, true);

document.addEventListener("click", (e) => {
  if (!refMenu.classList.contains("hidden") &&
      !refMenu.contains(e.target) && e.target !== inputEl) closeRefMenu();
});
window.addEventListener("resize", closeRefMenu);

// ---------- 输入历史召回（空输入按 ↑ 逐条回看已发消息，↓ 前进，Esc/编辑退出） ----------
// idx === list.length 表示不在历史态；进入历史态前记住草稿，Esc/↓ 走到底可还原。
// 输入框非空且不在历史态时，方向键保持原生光标行为（多行内容可上下移动）。
const inputHistory = { list: [], idx: 0, draft: "" };
function histActive() { return inputHistory.idx !== inputHistory.list.length; }
function recordInputHistory(text) {
  const t = (text || "").trim();
  if (!t) return;
  if (inputHistory.list[inputHistory.list.length - 1] !== t) {
    inputHistory.list.push(t);
    if (inputHistory.list.length > 100) inputHistory.list.shift();
  }
  inputHistory.idx = inputHistory.list.length;
  inputHistory.draft = "";
}
function recallHistory(delta) {
  const { list } = inputHistory;
  if (!list.length) return;
  if (!histActive()) inputHistory.draft = inputEl.value;
  inputHistory.idx = Math.min(list.length, Math.max(0, inputHistory.idx + delta));
  inputEl.value = inputHistory.idx === list.length
    ? inputHistory.draft
    : list[inputHistory.idx];
  const end = inputEl.value.length;
  inputEl.setSelectionRange(end, end);
  autoGrowInput();
}
// 手动编辑或点击定位光标 → 退出历史态（方向键回到原生行为）
inputEl.addEventListener("input", () => {
  if (histActive()) inputHistory.idx = inputHistory.list.length;
});
inputEl.addEventListener("click", () => {
  if (histActive()) inputHistory.idx = inputHistory.list.length;
});

const inputMenu = document.getElementById("input-menu");
let menuItems = [];
let menuActive = 0;
let fileIndex = null; // { files, ts } 30 秒缓存

function hideInputMenu() {
  inputMenu.classList.add("hidden");
  inputMenu.innerHTML = "";
  menuItems = [];
}
function showInputMenu(items, emptyHint) {
  menuItems = items;
  menuActive = 0;
  if (!items.length) {
    inputMenu.innerHTML = `<div class="menu-empty">${escapeHtml(emptyHint || "（无匹配项）")}</div>`;
  } else {
    inputMenu.innerHTML = items.map((it, i) =>
      `<button class="menu-item${i === 0 ? " active" : ""}" data-i="${i}">` +
      `<span class="${it.path ? "mi-path" : "mi-cmd"}">${escapeHtml(it.label)}</span>` +
      (it.desc ? `<span class="mi-desc">${escapeHtml(it.desc)}</span>` : "") +
      `</button>`).join("");
    inputMenu.querySelectorAll(".menu-item").forEach((el) => {
      el.onclick = () => pickMenuItem(+el.dataset.i);
    });
  }
  const composer = document.getElementById("composer");
  inputMenu.style.bottom = (composer.offsetHeight + 8) + "px";
  inputMenu.classList.remove("hidden");
}
function pickMenuItem(i) {
  const it = menuItems[i];
  hideInputMenu();
  if (it) it.onPick();
  inputEl.focus();
}
function moveMenuActive(delta) {
  if (!menuItems.length) return;
  menuActive = (menuActive + delta + menuItems.length) % menuItems.length;
  const els = inputMenu.querySelectorAll(".menu-item");
  els.forEach((el, i) => el.classList.toggle("active", i === menuActive));
  if (els[menuActive]) els[menuActive].scrollIntoView({ block: "nearest" });
}
// 按下鼠标不抢走输入框焦点，保证菜单项 click 能触发
inputMenu.addEventListener("mousedown", (e) => e.preventDefault());

const SLASH_COMMANDS = [
  { cmd: "/help", desc: "查看所有命令" },
  { cmd: "/new", desc: "新建会话（Ctrl+N）" },
  { cmd: "/compact", desc: "压缩上下文：旧历史替换为摘要，释放窗口空间" },
  { cmd: "/model", desc: "打开设置 · 模型服务" },
  { cmd: "/status", desc: "查看模型、上下文占用、工具数" },
  { cmd: "/todos", desc: "查看当前任务清单" },
  { cmd: "/export", desc: "导出当前会话为 Markdown" },
];

async function execSlash(cmd) {
  switch (cmd) {
    case "/help":
      addNotice("可用命令：\n" + SLASH_COMMANDS.map((c) => `${c.cmd} — ${c.desc}`).join("\n") +
        "\n提示：输入 @ 可以引用项目里的文件（支持文件夹）；项目外的文件用输入框左侧的文件按钮选。\n" +
        "快捷键：\n" +
        "Ctrl+N 新建会话 · Ctrl+F 在本对话里查找 · Ctrl+Shift+F 搜索会话（可跨项目）\n" +
        "Ctrl+K 切换模型 · Ctrl+W 关闭标签 · Esc 停止运行 · Enter 发送 · Shift+Enter 换行\n" +
        "Ctrl+Alt+Space 全局热键（唤起窗口并预填剪贴板）\n" +
        "更多说明点右上角「？」看帮助。");
      break;
    case "/new": {
      startNewTab();
      refreshSessions();
      break;
    }
    case "/compact": {
      if (activeTab && activeTab.running) { addNotice("当前轮还没结束，结束后再压缩。"); break; }
      const r = await request("chat.compact").catch((e) => { addNotice("压缩失败: " + e.message); return null; });
      if (!r) break;
      if (r.compacted) {
        addNotice(`🗜 已压缩：${r.before} → ${r.after} 条消息（摘要 ${r.summary_chars} 字符）`);
        setContextUsage(r.context_tokens, r.context_limit);
        if (r.context_detail) setContextDetail(r.context_detail);
      } else addNotice("当前上下文还很短，不需要压缩。");
      break;
    }
    case "/model":
      openSettings("providers");
      break;
    case "/status": {
      const r = await request("chat.status").catch((e) => { addNotice("出错: " + e.message); return null; });
      if (!r) break;
      const pct = r.context_limit ? Math.round((r.context_tokens / r.context_limit) * 100) : 0;
      addNotice(
        `📊 状态\n模型：${r.provider || "（未配置）"} / ${r.model || "-"}\n` +
        `上下文：${r.context_tokens} / ${r.context_limit} tokens（${pct}%）\n` +
        `消息：${r.history_messages} 条 · 工具：${r.tool_count} 个\n` +
        `工作目录：${r.working_dir}`
      );
      break;
    }
    case "/todos": {
      const r = await request("chat.status").catch(() => null);
      if (!r) break;
      if (!r.todos.length) {
        addNotice("当前没有任务清单。给 Agent 一个多步任务，它会用 todo_write 维护步骤。");
        break;
      }
      const icon = { pending: "○", in_progress: "◐", done: "●" };
      addNotice("任务清单：\n" + r.todos.map((t) => `${icon[t.status] || "○"} ${t.content}`).join("\n"));
      break;
    }
    case "/export":
      if (!currentSessionId) { addNotice("当前还没有会话可导出。"); break; }
      request("session.export", { id: currentSessionId })
        .then((r) => downloadText(r.filename, r.markdown))
        .catch((e) => addNotice("导出失败: " + e.message));
      request("session.export", { id: currentSessionId, fmt: "html" })
        .then((r) => downloadText(r.filename, r.html))
        .catch(() => {}); // HTML 版失败不打扰（MD 版已成功）
      break;
  }
}

async function updateFileIndex() {
  if (fileIndex && Date.now() - fileIndex.ts < 30000) return fileIndex.files;
  const r = await request("fs.files").catch(() => ({ files: [] }));
  fileIndex = { files: r.files || [], ts: Date.now() };
  return fileIndex.files;
}

function updateInputMenu() {
  const caret = inputEl.selectionStart;
  const before = inputEl.value.slice(0, caret);
  // ~ 唤起提示词菜单（原「快捷指令」，自带内置示例兜底）。
  // 查询段用 \S*：提示词名是中文，\w 匹配不到 CJK；空格视为结束（收起菜单）
  const tilde = before.match(/^~(\S*)$/);
  if (tilde) {
    const q = tilde[1].toLowerCase();
    const items = snippetsCache.concat(builtinSnippets())
      .filter((s) => !q || s.name.toLowerCase().includes(q))
      .slice(0, 8)
      .map((s) => ({
        label: "◆ " + s.name,
        desc: snippetsCache.includes(s) ? "提示词 · 选中即插入" : "内置示例 · 选中即插入",
        onPick: () => {
          // ~ 查询串只是唤起器，不属于消息内容：插入前把它从输入框里去掉
          inputEl.value = inputEl.value.replace(/^~[^\n]*/, "");
          insertSnippet(s);
        },
      }));
    showInputMenu(items, items.length
      ? "（回车或点击插入到输入框，~ 后接着输入可过滤）"
      : "（没有匹配的提示词；在设置 · 提示词里可以新建）");
    return;
  }
  const slash = before.match(/^\/([\w-]*)$/);
  if (slash) {
    const q = slash[1].toLowerCase();
    const items = SLASH_COMMANDS.filter((c) => c.cmd.startsWith("/" + q))
      .map((c) => ({
        label: c.cmd,
        desc: c.desc,
        onPick: () => { inputEl.value = ""; autoGrowInput(); execSlash(c.cmd); },
      }));
    showInputMenu(items, "（没有匹配的命令，回车仍会作为消息发送）");
    return;
  }
  const at = before.match(/@([^\s@\\/]*)$/);
  if (at) {
    updateFileIndex().then((files) => {
      if (!/@([^\s@\\/]*)$/.test(inputEl.value.slice(0, inputEl.selectionStart))) return;
      const q = at[1].toLowerCase();
      const starts = [], contains = [];
      for (const f of files) {
        const low = f.toLowerCase();
        if (q && low.startsWith(q)) starts.push(f);
        else if (q && low.includes(q)) contains.push(f);
        if (starts.length >= 10) break;
      }
      const matches = (q ? starts.concat(contains) : files).slice(0, 10);
      const range = [before.length - at[0].length, before.length];
      showInputMenu(matches.map((f) => {
        const isDir = f.endsWith("/");
        return {
          label: isDir ? "📁 " + f : f, path: true,
          onPick: () => {
            inputEl.setRangeText("@" + f + " ", range[0], range[1], "end");
            autoGrowInput();
          },
        };
      }), "（项目里没有匹配的文件）");
    });
    return;
  }
  hideInputMenu();
}
inputEl.addEventListener("input", updateInputMenu);
inputEl.addEventListener("blur", () => setTimeout(hideInputMenu, 120));

document.getElementById("input").addEventListener("keydown", (e) => {
  const menuOpen = !inputMenu.classList.contains("hidden");
  if (menuOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault();
    moveMenuActive(e.key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (menuOpen && (e.key === "Enter" || e.key === "Tab")) {
    e.preventDefault();
    pickMenuItem(menuActive);
    return;
  }
  if (e.key === "Escape" && menuOpen) { e.preventDefault(); hideInputMenu(); return; }
  // 输入历史召回：空输入 ↑ 进入回看；历史态内 ↑↓ 无条件导航（点击/编辑已退出历史态后，
  // 方向键恢复原生光标行为），↓ 走到底还原草稿，Esc 直接还原
  // isComposing：输入法选词过程（拼音候选上屏前的回车/方向键）不能被这里劫持
  if (!menuOpen && !e.isComposing && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (e.key === "ArrowUp" && (inputEl.value === "" || histActive())) {
      e.preventDefault();
      recallHistory(-1);
      return;
    }
    if (e.key === "ArrowDown" && histActive()) {
      e.preventDefault();
      recallHistory(1);
      return;
    }
    if (e.key === "Escape" && histActive()) {
      e.preventDefault();
      inputHistory.idx = inputHistory.list.length;
      inputEl.value = inputHistory.draft;
      autoGrowInput();
      return;
    }
  }
  if (e.key === "Enter" && !e.isComposing) {
    // 发送键可配（ui.json 的 ctrl_enter_send）：默认 Enter 发送 / Shift+Enter 换行；
    // 开启后 Ctrl+Enter 发送 / Enter 一律换行（不再吞 Shift 语义）。
    if (ctrlEnterSend) {
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); send(); }
      return; // 其余组合（含裸 Enter）交给浏览器换行
    }
    if (!e.shiftKey) { e.preventDefault(); send(); }
  }
});
document.getElementById("btn-new").onclick = () => { startNewTab(); refreshSessions(); };
window.addEventListener("keydown", (e) => {
  if (!e.ctrlKey && !e.metaKey) {
    // Esc 停止运行（设置页/弹窗打开时不劫持——它们有自己的 Esc 语义）
    if (e.key === "Escape" && !settingsOpen && !modalOpen() && routeTab && routeTab.running) {
      e.preventDefault();
      const sid = routeTab.sid;
      if (sid) request("stop", { session_id: sid }).catch(() => {});
    }
    return;
  }
  const k = String(e.key).toLowerCase();
  // 正在输入框/可编辑元素里打字时不触发会抢焦点/丢草稿的动作
  //（Ctrl+N 新会话、Ctrl+K 模型菜单、Ctrl+W 关标签）；按键本身仍吞掉，
  // 交给浏览器默认行为在 WebView2 里没有意义，放行只会引入意外
  const tgt = e.target;
  const typing = !!(tgt && (tgt.tagName === "INPUT" || tgt.tagName === "TEXTAREA" || tgt.isContentEditable));
  // Ctrl+` 终端面板开关（对标 VS Code 的集成终端）
  if (k === "`") {
    e.preventDefault();
    toggleTermDock();
    return;
  }
  // Ctrl+Shift+F 搜索会话（全项目 / 历史）
  if (k === "f" && e.shiftKey && !e.altKey) {
    e.preventDefault();
    if (!settingsOpen) openSettingsSearch();
    return;
  }
  if (e.shiftKey || e.altKey) return;
  // Ctrl+N 新建会话
  if (k === "n") {
    e.preventDefault();
    if (!typing && !settingsOpen) document.getElementById("btn-new").click();
    return;
  }
  // Ctrl+F 在本对话里查找（所有编辑类软件的惯例；会话搜索见 Ctrl+Shift+F）
  if (k === "f" && !settingsOpen) {
    e.preventDefault();
    if (!settingsOpenFlag()) openFindBar();
    return;
  }
  // Ctrl+K 模型切换菜单
  if (k === "k" && !settingsOpen) {
    e.preventDefault();
    if (!typing) toggleModelMenu();
    return;
  }
  // Ctrl+B 左侧栏折叠/展开（对标 VS Code 的侧栏开关；输入框聚焦时也响应）
  if (k === "b") {
    e.preventDefault();
    toggleLeftSidebar();
    return;
  }
  // Ctrl+W 关闭当前会话标签
  if (k === "w" && !settingsOpen) {
    e.preventDefault();
    if (!typing && activeTab) closeTab(activeTab);
    return;
  }
});

function modalOpen() {
  return !document.getElementById("modal").classList.contains("hidden");
}
function settingsOpenFlag() { return settingsOpen; }

function openSettingsSearch() {
  const inp = document.getElementById("session-search");
  if (!inp) return;
  inp.focus();
  inp.select();
}

// ---------- 对话内查找（Ctrl+F）：在当前会话的消息里找词并高亮跳转 ----------
// 实现方式：把每条消息里的文本节点按关键词切开、包 <mark class="find-hit">，
// 关闭时再把 mark 还原成纯文本（不动 innerHTML，避免破坏已渲染的代码高亮/图表）。
let findHits = [];
let findIdx = -1;

function findEls() {
  const bar = document.getElementById("find-bar");
  return {
    bar,
    input: document.getElementById("find-input"),
    count: document.getElementById("find-count"),
  };
}

function openFindBar() {
  const { bar, input } = findEls();
  bar.classList.remove("hidden");
  input.focus();
  input.select();
  if (!input.value) return;
  runFind(input.value);
}

function closeFindBar() {
  clearFindMarks();
  const { bar, count } = findEls();
  bar.classList.add("hidden");
  count.textContent = "0/0";
  if (inputEl) inputEl.focus();
}

function clearFindMarks() {
  const log = activeTab && activeTab.logEl ? activeTab.logEl : null;
  if (!log) { findHits = []; findIdx = -1; return; }
  log.querySelectorAll("mark.find-hit").forEach((m) => {
    const parent = m.parentNode;
    if (!parent) return;
    parent.replaceChild(document.createTextNode(m.textContent), m);
    parent.normalize();
  });
  findHits = [];
  findIdx = -1;
}

function runFind(query) {
  clearFindMarks();
  const { count } = findEls();
  const log = activeTab && activeTab.logEl ? activeTab.logEl : null;
  if (!query || !log) { count.textContent = "0/0"; return; }
  const q = query.toLowerCase();
  // 逐条消息处理；只碰文本节点，跳过 <pre>/<code>（里面的高亮 span 不能拆）
  log.querySelectorAll(".msg").forEach((msg) => {
    const walker = document.createTreeWalker(msg, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(q)) return NodeFilter.FILTER_REJECT;
        const p = node.parentNode;
        if (!p) return NodeFilter.FILTER_REJECT;
        const tag = p.nodeName.toLowerCase();
        if (tag === "pre" || tag === "code" || tag === "script" || tag === "style") return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const targets = [];
    while (walker.nextNode()) targets.push(walker.currentNode);
    targets.forEach((node) => {
      const text = node.nodeValue;
      const low = text.toLowerCase();
      const frag = document.createDocumentFragment();
      let i = 0;
      for (;;) {
        const p = low.indexOf(q, i);
        if (p < 0) { frag.appendChild(document.createTextNode(text.slice(i))); break; }
        if (p > i) frag.appendChild(document.createTextNode(text.slice(i, p)));
        const mark = document.createElement("mark");
        mark.className = "find-hit";
        mark.textContent = text.slice(p, p + query.length);
        frag.appendChild(mark);
        findHits.push(mark);
        i = p + query.length;
      }
      node.parentNode.replaceChild(frag, node);
    });
  });
  findIdx = findHits.length ? 0 : -1;
  paintFindHit();
}

function paintFindHit() {
  const { count } = findEls();
  findHits.forEach((m, i) => m.classList.toggle("cur", i === findIdx));
  if (!findHits.length) { count.textContent = "0/0"; return; }
  count.textContent = `${findIdx + 1}/${findHits.length}`;
  const cur = findHits[findIdx];
  cur.scrollIntoView({ block: "center", behavior: "smooth" });
}

function stepFind(delta) {
  if (!findHits.length) return;
  findIdx = (findIdx + delta + findHits.length) % findHits.length;
  paintFindHit();
}

document.getElementById("find-input").addEventListener("input", (e) => {
  // 200ms 防抖：每次击键都是「拆掉上一轮全部 mark + 全消息 TreeWalker 扫描
  // + 逐命中 DOM 替换」，长会话里直通会每键卡几百毫秒（与会话搜索框同一配方）
  clearTimeout(runFind._t);
  runFind._t = setTimeout(() => runFind(e.target.value), 200);
});
document.getElementById("find-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1); }
  if (e.key === "Escape") { e.preventDefault(); closeFindBar(); }
});
document.getElementById("find-prev").onclick = () => stepFind(-1);
document.getElementById("find-next").onclick = () => stepFind(1);
document.getElementById("find-close").onclick = () => closeFindBar();

// ---------- 帮助：快速上手 / 快捷键 / 常见问题（顶栏「？」） ----------
const HELP_KEYS = [
  ["Ctrl + N", "新建会话"],
  ["Ctrl + F", "在本对话里查找（上下跳转）"],
  ["Ctrl + Shift + F", "搜索会话与消息（可切「全部项目」）"],
  ["Ctrl + K", "切换模型"],
  ["Ctrl + W", "关闭当前会话标签"],
  ["Ctrl + `", "开关底部终端面板（多标签）"],
  ["Esc", "停止正在运行的任务"],
  ["Enter / Shift + Enter", "发送 / 换行"],
  ["Ctrl + Alt + Space", "全局热键：唤起窗口并预填剪贴板内容"],
  ["输入 ~", "提示词菜单：选中即插入模板（在设置 · 提示词里维护）"],
  ["输入 /", "命令菜单（/help /new /compact /model /status /todos /export）"],
  ["输入 @", "引用项目文件或文件夹"],
  ["空输入按 ↑", "回看之前发过的消息"],
];

function showHelp() {
  const box = document.createElement("div");
  box.className = "help-body";
  box.innerHTML = `
    <div class="help-sec">
      <h4>① 先配好模型</h4>
      <p>第一次打开时点「⚙ 设置 → 模型服务」，挑一个服务粘贴 API Key 就能用。
      Key 只存在本机 <code>~/.skysheep/config.toml</code>，不会上传。</p>
    </div>
    <div class="help-sec">
      <h4>② 让它干活</h4>
      <p>在输入框直接说人话，比如：</p>
      <ul>
        <li>「把 <code>data.csv</code> 整理成一份带汇总表的 Excel」</li>
        <li>「读一下这个文件夹里的合同，列出到期时间」</li>
        <li>「帮我把这个报错修好，改完跑一遍测试」</li>
      </ul>
      <p>涉及写文件、执行命令时它会先弹确认条（<b>允许一次 / 本项目总是允许 / 拒绝</b>）；
      只读操作自动放行。想少点确认，可点输入框工具条上的权限按钮（盾牌图标）在三档间切换：
      「自动编辑」只自动放行工作目录内的文件写入，目录外的写入与执行命令仍会征询；
      「完全访问」连执行命令也不再征询，仅在完全信任任务的环境里使用。</p>
    </div>
    <div class="help-sec">
      <h4>③ 常用开关</h4>
      <ul>
        <li><b>⚡ 规划模式</b>：先出方案再动手，适合大改动</li>
        <li><b>👥 圆桌</b>：一条消息让多个模型同时作答再融合，难题更稳</li>
        <li><b>＋ 文件</b>：选本机任意文件（含项目外）让 Agent 读；图片请直接粘贴或拖入</li>
        <li><b>底部终端</b>：多开 PowerShell 标签，真终端（提示符 / 颜色 / Ctrl+C / 交互程序，Ctrl+&#96; 开关）</li>
        <li><b>右侧面板</b>：文件树、浏览器预览、审查看每轮改了哪些文件</li>
      </ul>
    </div>
    <div class="help-sec">
      <h4>快捷键</h4>
      <div class="help-keys">
        ${HELP_KEYS.map(([k, v]) => `<span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(v)}</span>`).join("")}
      </div>
    </div>
    <div class="help-sec">
      <h4>常见问题</h4>
      <div class="faq-q">贴了图片，它说看不到 / 报错？</div>
      <div class="faq-a">当前模型是纯文本模型。切到多模态模型（如 GLM-4V、Kimi、GPT-4o），
      或到 设置 · 模型服务 里确认该服务的「模型支持图片输入」是否开着。</div>
      <div class="faq-q">长对话越来越慢、提示上下文满了？</div>
      <div class="faq-a">输入 <code>/compact</code> 压缩历史，或在 设置 · 高级 里把
      「上下文上限」调成模型真实的窗口大小（默认 1000000）。</div>
      <div class="faq-q">定时任务到点没执行？</div>
      <div class="faq-a">定时任务只在 SkySheep 运行期间触发。关窗时选「缩到系统托盘」它就继续在后台跑；
      彻底退出期间错过的任务会在下次打开时补跑。想让它常驻，可在 设置 · 高级 打开「开机自动启动」。</div>
      <div class="faq-q">Agent 老是先停下来问我？</div>
      <div class="faq-a">它会先出计划／只做了只读调研时，多半是任务太模糊。把目标、文件、验证方式写清楚；
      或在 设置 · 高级 里把「最多执行轮数」调大（默认 40）。</div>
      <div class="faq-q">误删了会话 / 数据不对劲？</div>
      <div class="faq-a">设置 · 关于 →「会话库备份」里可以恢复到一个较早的版本；
      同一页还能导出诊断包发给开发者排障。</div>
      <div class="faq-q">日志在哪？</div>
      <div class="faq-a">设置 · 关于 →「打开日志文件夹」（<code>~/.skysheep/logs/desktop.log</code>）。</div>
    </div>`;
  showModal("帮助", box, async () => {}, "知道了");
}

document.getElementById("btn-help").onclick = () => showHelp();

// ---------- 左栏功能导航（任务清单 / 日程 / 项目记忆 / 定时任务 / MCP·Skills） ----------
const navPending = {}; // 已全部上线，保留结构便于扩展

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.onclick = () => {
    const act = btn.dataset.act;
    // 窄屏上侧栏是抽屉：点了导航就收回（任务清单等打开的是右侧面板标签，
    // 不收回的话侧栏（z 更高）正好盖住刚打开的面板）
    document.body.classList.remove("sidebar-open");
    // 任务清单/日程/定时任务：右侧面板标签（不再用侧栏折叠区）
    if (act === "todo") return openRightTab("todo");
    if (act === "agenda") return openRightTab("agenda");
    if (act === "cron") return openRightTab("cron");
    if (act === "pipeline") return openRightTab("pipeline");
    if (act === "memory") return openRightTab("memory");
    if (act === "project") return projectModal();
    if (act === "ext") return openRightTab("ext");
    if (navPending[act]) addNotice(`「${navPending[act]}」开发中，即将上线`);
  };
});

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

function agPad(n) { return String(n).padStart(2, "0"); }
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
  const p = (n) => String(n).padStart(2, "0");
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
  const p = (n) => String(n).padStart(2, "0");
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
document.getElementById("btn-agenda-add").onclick = () => agendaModal(null);

// 视图切换与周/月导航（周网格 / 月网格 / 分组列表）
document.querySelectorAll("#ag-view-seg button").forEach((b) => {
  b.onclick = () => { agView = b.dataset.v; renderAgendaView(); };
});
document.getElementById("ag-list-btn").onclick = () => { agView = "list"; renderAgendaView(); };
document.getElementById("ag-prev").onclick = () => shiftAgenda(-1);
document.getElementById("ag-next").onclick = () => shiftAgenda(1);
document.getElementById("ag-today").onclick = () => { agAnchor = new Date(); renderAgendaView(); };

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
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadCron() {
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
      `<button class="cron-op" data-op="toggle" title="${t.enabled ? "暂停" : "启用"}">${t.enabled ? "⏸" : "▶"}</button>` +
      `<button class="cron-op danger" data-op="del" title="删除">✕</button>`;
    ops.querySelector('[data-op="run"]').onclick = async (e) => {
      e.stopPropagation();
      try {
        await request("cron.run_now", { id: t.id });
        addNotice(`已开始运行「${t.name}」，结果会写回列表`);
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
      try { await request("cron.delete", { id: t.id }); }
      catch (err) { addNotice("删除失败: " + err.message); }
      await loadCron();
    };
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
                          allowed_tools: [], enabled: true };
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
      <p class="dim small">安全说明：定时任务无人值守运行，<b>只读工具本来就放行</b>；
        写入 / 执行类必须在这里勾选，否则运行时会自动拒绝。建议先只勾必要的。</p>
      <p class="dim small">运行前提：定时任务只在 <b>SkySheep 运行期间</b>触发（关窗时选「缩到系统托盘」它就继续在后台跑）。
        彻底退出期间错过的任务，会在下次打开应用时补跑一次。想让电脑一开机就守着，
        可在 设置 · 高级 里打开「开机自动启动」。</p>
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
    };
    if (isEdit) await request("cron.update", { id: existing.id, ...params });
    else await request("cron.add", params);
    await loadCron();
    addNotice(isEdit ? "定时任务已更新" : "定时任务已创建，到点自动运行");
  }, isEdit ? "保存" : "创建");
}

document.getElementById("btn-cron-add").onclick = () => cronModal(null);

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
  ro.observe(el);
}

async function loadPipelines() {
  if (!bootSnap || !bootSnap.tools) {
    try { bootSnap = await request("boot"); } catch (e) { /* 取不到就退化提示 */ }
  }
  let pipes = [];
  try {
    pipes = (await request("pipeline.list")).pipelines || [];
  } catch (e) {
    document.getElementById("pipeline-list").innerHTML =
      `<li class="dim small" style="padding:6px 10px">加载失败：${escapeHtml(e.message)}</li>`;
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
        ${dep ? `<div class="dim small">依赖：${escapeHtml(dep)}</div>` : ""}
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
      <div id="pl-graph-wrap" class="pl-graph-wrap"></div>
      ${rows || '<div class="dim small">（没有节点）</div>'}
      <p class="dim small">要改节点/指令：删除后重建（Agent 对话里说一句也能帮你重排）。</p>
    </div>`;
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
      <div id="pl-nodes"></div>
      <button id="pl-add-node" class="rp-mini" type="button">＋ 添加节点</button>
      <p class="dim small">节点按依赖自动排序：勾选「依赖前面的节点」，被依赖的全部完成后才会开始。
        最后一个节点勾选依赖它前面的全部节点，就是汇总审查。</p>
      <p class="dim small">安全说明：每个节点无人值守运行，<b>只读工具本来就放行</b>；
        写入/执行类必须在该节点勾选，否则运行时自动拒绝。启动前请再检查一遍各节点的预授权。</p>
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
      return { title: title || `节点 ${i + 1}`, prompt, after, allowed_tools, control, max_runs,
        timeout_s: timeoutMin * 60 };
    });
    await request("pipeline.create", { name: name || "未命名流水线", concurrency, nodes });
    await loadPipelines();
    addNotice("流水线已创建（草稿）。检查各节点的预授权后点 ▶ 启动。");
  }, "创建");
}

document.getElementById("btn-pipeline-add").onclick = () => pipelineCreateModal();
document.getElementById("btn-pipeline-import").onclick = () => pipelineImportModal({});
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
        <label>这一轮要它做什么（在该会话已有的上下文里继续）</label>
        <textarea id="pl-session-prompt" class="modal-input" rows="2" placeholder="例如：结合刚才的实现，把接口文档补全"></textarea>
      </div>
      <div id="pl-deps-wrap" class="hidden">
        <label>等哪些节点完成后再跑（不勾 = 立即可跑）</label>
        <div id="pl-deps-box" class="pl-deps"></div>
      </div>
      <label class="pl-dep" id="pl-disable-wrap"><input type="checkbox" id="pl-disable-cron"> 导入后停用原定时任务（不再按周期独立运行）</label>
      <p class="dim small">安全说明：纳入的节点同样无人值守执行——只读工具放行，其余按节点预授权名单，
        名单外自动拒绝。挂接节点跟随原任务、不占并发额度；会话节点在该会话正被使用时会等空闲。</p>
    </div>`;
  const q = (sel) => box.querySelector(sel);
  const srcObjs = { cron: srcOptions.cron, task: srcOptions.task, session: srcOptions.session };
  const srcLabels = { cron: "选定时任务", task: "选任务簿任务", session: "选会话" };
  const curSource = () => box.querySelector('input[name="pl-src"]:checked').value;
  const curObjVal = () => q("#pl-src-obj").value;
  let fileExport = null; // 「导出文件」来源：解析后的 JSON（提交时交给 pipeline.import）
  const renderSrc = () => {
    const s = curSource();
    // 「导出文件」恢复的是整条流水线：不选对象、不选目标流水线，只选文件
    const isFile = s === "file";
    q("#pl-src-label").classList.toggle("hidden", isFile);
    q("#pl-src-obj").classList.toggle("hidden", isFile);
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
    q("#pl-src-obj").innerHTML = srcObjs[s];
    q("#pl-src-label").textContent = srcLabels[s];
    q("#pl-src-obj").disabled = !srcObjs[s];
    if (!srcObjs[s]) q("#pl-src-obj").innerHTML = `<option value="">（暂时没有可纳入的${srcLabels[s].slice(1)}）</option>`;
    // 预设来源对象（从任务簿/定时任务行的 ⛓ 进来）
    if (preset.cron_id && s === "cron") q("#pl-src-obj").value = String(preset.cron_id);
    if (preset.task_id && s === "task") q("#pl-src-obj").value = String(preset.task_id);
    if (preset.session_id && s === "session") q("#pl-src-obj").value = String(preset.session_id);
    q("#pl-prompt-wrap").classList.toggle("hidden", s !== "session");
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
    if (!objId) throw new Error("先选一个要纳入的对象");
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
      } else {
        const prompt = q("#pl-session-prompt").value.trim();
        if (!prompt) throw new Error("会话节点要写清这一轮要做什么");
        node = { session_id: String(objId), prompt };
      }
      await request("pipeline.create", { name: name || "未命名流水线", nodes: [node] });
      targetLabel = name || "未命名流水线";
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
    if (rightTabs.includes("agenda") && !rightCollapsed) await loadAgenda();
    close();
  };
  bar.querySelector('[data-a="snooze"]').onclick = async () => {
    await request("schedule.update", { id: item.id, start_at: Date.now() / 1000 + 600 });
    if (rightTabs.includes("agenda") && !rightCollapsed) await loadAgenda();
    close();
  };
  bar.querySelector(".ar-close").onclick = close;
  document.body.appendChild(bar);
}

// ---------- 项目记忆：右侧面板编辑（AGENTS.md，保存即注入每一轮对话） ----------
// 原来是弹窗；改成与日程/定时任务同款的右面板标签——编辑约定时还能同时看着对话。
const rpMemoryText = document.getElementById("rp-memory-text");
let memoryLoaded = false; // 与项目绑定：切项目后由 resetProjectPanels 置回 false
let rpMemoryMtime = 0; // 加载时的文件 mtime：保存时带回比对，防覆盖后台重写的 AGENTS.md

function memoryStatus(text, ok = true) {
  const el = document.getElementById("memory-status");
  el.textContent = text;
  el.className = "rp-memory-status" + (ok ? "" : " bad");
}

async function loadMemoryPanel(force = false) {
  if (memoryLoaded && !force) return;
  try {
    const r = await request("project.instructions");
    rpMemoryText.value = r.text || "";
    rpMemoryMtime = r.mtime || 0;
    memoryLoaded = true;
    memoryStatus(r.path
      ? `记忆文件：${r.path}`
      : "本项目还没有记忆文件，保存时会自动创建 AGENTS.md");
  } catch (e) {
    memoryStatus("✗ 读取失败：" + e.message, false);
  }
  // 项目记忆的定期整理开关（就是设置页移过来的那个）：跟随后台配置回填
  try {
    const m = (await request("memory.get")).maintain || {};
    maintainState = {
      global_enabled: m.global_enabled !== false,
      project_enabled: m.project_enabled !== false,
      interval_hours: m.interval_hours || 168,
    };
    const t = document.getElementById("rp-memory-maintain");
    if (t) t.checked = maintainState.project_enabled;
  } catch (e) { /* 拉不到就保持默认勾选，失败在切换时才暴露 */ }
}

async function saveMemoryPanel() {
  try {
    const res = await request("project.save_instructions", {
      text: rpMemoryText.value, base_mtime: rpMemoryMtime,
    });
    rpMemoryMtime = res.mtime || 0;
    memoryStatus(`✓ 已保存（${res.chars} 字）→ ${res.path}，下一轮对话即生效`);
    addNotice(`项目记忆已保存（${res.chars} 字）→ ${res.path}`);
  } catch (e) {
    memoryStatus("✗ 保存失败：" + e.message, false);
  }
}
document.getElementById("memory-save").onclick = () => { saveMemoryPanel(); };
// 项目记忆定期整理开关（设置页移入）：切换即保存；全局开关与周期仍归设置页管，
// 这里透传后台现值，避免单独切这一个开关时覆盖其他配置
const rpMemoryMaintain = document.getElementById("rp-memory-maintain");
if (rpMemoryMaintain) {
  rpMemoryMaintain.addEventListener("change", async (e) => {
    const el = e.target;
    try {
      const r = await request("memory.maintain_save", {
        global_enabled: maintainState.global_enabled,
        project_enabled: el.checked,
        interval_hours: maintainState.interval_hours,
      });
      maintainState = { global_enabled: r.global_enabled, project_enabled: r.project_enabled,
        interval_hours: r.interval_hours };
      el.checked = !!r.project_enabled; // 以落库值为准
      memoryStatus(`✓ 项目记忆定期整理已${r.project_enabled ? "开启" : "关闭"}（周期跟全局设置）`);
    } catch (err) {
      el.checked = !el.checked; // 失败回拨
      memoryStatus("✗ 保存失败：" + err.message, false);
    }
  });
}
document.getElementById("memory-reload").onclick = () => { loadMemoryPanel(true); };
// Ctrl+S 在面板内直接保存（不劫持全局）
rpMemoryText.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "s") {
    e.preventDefault();
    saveMemoryPanel();
  }
});

// ---------- 工作项目：查看与切换（应用内设定工作目录） ----------
async function projectModal() {
  const [snap, pl] = await Promise.all([request("boot"), request("project.list")]);
  const others = pl.projects.filter((p) => !p.is_current);
  const canPick = nativePickerAvailable();
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small" style="margin:0 0 8px">
      当前：<b>${escapeHtml(snap.project || "(未命名)")}</b>
      <span class="mono-path">${escapeHtml(snap.working_dir)}</span></p>
    <p class="dim small">Agent 的文件读写、命令执行都发生在这个文件夹里。切换后白名单、项目记忆、项目技能也会跟着切换到新项目。</p>
    ${others.length ? '<div class="section"><h3>最近的项目</h3><ul class="list" id="proj-list"></ul></div>' : ""}
    <div class="pr-fields">
      <label>切换到其他文件夹</label>
      <div class="model-line">
        <input id="proj-path-input" autocomplete="off" spellcheck="false"
          placeholder="${canPick ? "点右侧按钮选择文件夹，或直接粘贴完整路径" : "粘贴文件夹完整路径，如 D:\\\\works\\\\demo"}">
        ${canPick ? '<button id="proj-pick" class="btn-ghost">' + FOLDER_SVG + '选择</button>' : ""}
      </div>
    </div>`;
  const input = () => box.querySelector("#proj-path-input").value.trim();
  const doSwitch = async (path) => {
    if (!path) throw new Error("请先选择或粘贴一个文件夹路径");
    await switchProject(path);
  };
  const ul = box.querySelector("#proj-list");
  if (ul) {
    others.forEach((p) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="s-title">${escapeHtml(p.name)}</span>` +
        `<span class="s-snippet">${escapeHtml(p.root_path)}</span>`;
      li.title = `${p.root_path}（点击切换到这个项目）`;
      li.onclick = () => doSwitch(p.root_path).catch((e) => addNotice("切换失败: " + e.message));
      ul.appendChild(li);
    });
  }
  const pickBtn = box.querySelector("#proj-pick");
  if (pickBtn) {
    pickBtn.onclick = async () => {
      const r = await pickPath("dir");
      if (r.paths && r.paths[0]) box.querySelector("#proj-path-input").value = r.paths[0];
    };
  }
  showModal("工作项目", box, () => doSwitch(input()), "切换到该文件夹");
}

// 侧栏「项目」区右上角 ＋：添加/切换项目（选文件夹或粘贴路径）
document.getElementById("session-add-project").onclick = projectModal;
// 添加项目唯一入口：两种视图共用，常驻「会话」标题行右端（分组视图下排在折叠钮之后）

// ---------- 设置页：视图切换（侧栏同步切换为设置子项目） ----------
const viewChat = document.getElementById("view-chat");
const viewSettings = document.getElementById("view-settings");
const sideChat = document.getElementById("side-chat");
const sideSettings = document.getElementById("side-settings");
const btnSettings = document.getElementById("btn-settings");
let settingsOpen = false;

function openSettings(page = "providers") {
  closeExtPanel(); // MCP/Skills 卡片若正被右面板借用，先搬回来，技能/MCP 两页才不会是空的
  settingsOpen = true;
  viewChat.classList.add("hidden");
  viewSettings.classList.remove("hidden");
  // 窄屏上侧栏是抽屉：切到设置页必须收回它，否则抽屉盖在设置页上、
  // 而"点一下收回"的监听挂在 #chat 上（设置页里点不到）→ 卡死
  document.body.classList.remove("sidebar-open");
  sideChat.classList.add("hidden");
  sideSettings.classList.remove("hidden");
  btnSettings.textContent = "← 返回对话";
  resetProviderView(); // 每次进设置都从服务列表开始，不停在上次打开的详情页
  resetSkillView();    // 技能页同理：每次都从总览开始
  showSettingsPage(page);
  renderSettings().catch((e) => addNotice("加载设置失败: " + e.message));
}
function backToChat() {
  settingsOpen = false;
  viewSettings.classList.add("hidden");
  viewChat.classList.remove("hidden");
  document.body.classList.remove("sidebar-open"); // 手机：返回对话同样先收回抽屉
  sideSettings.classList.add("hidden");
  sideChat.classList.remove("hidden");
  btnSettings.textContent = "⚙ 设置";
  // 对话区重新可见：地面回来了，小羊按新地面归位（设置页期间位置一直冻结着）
  petSettle();
}
btnSettings.onclick = () => (settingsOpen ? backToChat() : openSettings());

// 设置子项目：点击导航切换独立页面
function showSettingsPage(target) {
  document.querySelectorAll("#settings-nav li").forEach((x) =>
    x.classList.toggle("active", x.dataset.target === target)
  );
  document.querySelectorAll(".settings-page").forEach((pg) =>
    pg.classList.toggle("hidden", pg.id !== "settings-page-" + target)
  );
  if (target === "usage") loadUsage();
  if (target === "memory") loadMemoryPage();
  if (target === "snippets") loadSnippets();
  if (target === "remote") { loadLanPanel(); loadTsPanel(); loadChannelPanel(); }
  if (target === "skills") loadToolControl();
  if (target === "subagents") renderSubagentCfg().catch(() => {});
  if (target === "advanced") { renderAdvancedCfg().catch(() => {}); renderHooksCfg(); }
  if (target === "about") loadBackups().catch(() => {});
}
document.querySelectorAll("#settings-nav li[data-target]").forEach((li) => {
  li.onclick = () => {
    document.body.classList.remove("sidebar-open"); // 手机：切换子页前先收回侧栏抽屉
    showSettingsPage(li.dataset.target);
  };
});

// ---------- 模型服务：列表视图 ----------
let providerCfg = null;   // 最近一次拉取到的服务清单
let bootSnap = null;      // 最近一次 boot 快照（模板里要用工作目录等）
let availCacheModels = null; // 详情页探测到的可用模型（切视图不丢）
let availNoteState = null;   // 可用模型面板的提示状态
let availListCollapsed = false; // 检测到的模型列表是否收起（上百行时给页面让地方）
let detailOpen = false;   // 是否正停在某个服务的配置页
let detailName = "";
let providerTab = "preset"; // 列表页签：preset=默认（内置预设）、custom=自定义（用户新增）

const AVATAR_COLORS = ["#1257c4", "#8a6410", "#0f6b3a", "#8f1d1d", "#4f35a8", "#0e6b6b"];

// 内置预设的品牌外观：logo 文件在 static/logos/（按配置键命名），color 是头像底色。
// plain=true 表示彩色图标（ico/多色 png），铺白底原样显示，不做白色滤镜。
// 没收录 logo 的（xai）回落字母头像。
const PROVIDER_META = {
  anthropic: { logo: "logos/anthropic.svg", color: "#191919" },
  openai: { logo: "logos/openai.svg", color: "#10a37f" },
  google: { logo: "logos/google.svg", color: "#4285f4" },
  xai: { color: "#1c1c1e" },
  minimax: { logo: "logos/minimax.svg", color: "#f03e3e" },
  deepseek: { logo: "logos/deepseek.svg", color: "#4d6bfe" },
  zhipu: { logo: "logos/zhipu.png", plain: true },
  moonshot: { logo: "logos/moonshot.svg", color: "#16191e" },
  qwen: { logo: "logos/qwen.svg", color: "#615ced" },
  mimo: { logo: "logos/mimo.svg", color: "#ff6900" },
  ollama: { logo: "logos/ollama.svg", color: "#3f3f46" },
};

function providerAvatar(name) {
  const meta = PROVIDER_META[name];
  if (meta && meta.logo) {
    const tileStyle = meta.plain
      ? "background:#fff"
      : `background:${meta.color}`;
    return `<span class="svc-avatar" style="${tileStyle}">` +
      `<img class="svc-logo${meta.plain ? " svc-logo-plain" : ""}" src="/static/${meta.logo}" alt="">` +
      `</span>`;
  }
  const color = (meta && meta.color) || avatarColor(name);
  return `<span class="svc-avatar" style="background:${color}">${escapeHtml(name.slice(0, 1).toUpperCase())}</span>`;
}

function providerLabel(name, p) {
  return (p && p.label) || name; // 后端给的显示名（预设如「智谱」「小米 Mimo」）
}

function avatarColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 997;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function providerRow([name, p]) {
  const row = document.createElement("div");
  row.className = "svc-row" + (p.is_active ? " cur" : "");
  row.dataset.name = name;
  const keyState = p.has_key ? "Key 已配置" + (p.key_from_env ? "（环境变量）" : "") : "未配置 Key";
  const badges =
    (p.is_active ? '<span class="chip chip-blue">使用中</span>' : "") +
    (p.is_default ? '<span class="chip">★ 默认</span>' : "");
  row.innerHTML = `
    ${providerAvatar(name)}
    <span class="svc-main">
      <span class="svc-name">${escapeHtml(providerLabel(name, p))}<span class="svc-key${p.has_key ? "" : " svc-warn"}">· ${escapeHtml(keyState)}</span>${badges}</span>
      <span class="svc-sub">${escapeHtml(p.kind)} · ${escapeHtml(p.model || "未设置模型")}</span>
    </span>
    <button class="svc-go" type="button" title="进入配置">›</button>`;
  // 整行可点：进该服务的配置页
  row.onclick = () => openProviderDetail(name);
  return row;
}

function renderProviderList() {
  const list = document.getElementById("provider-list");
  const tabs = document.getElementById("provider-tabs");
  const entries = Object.entries(providerCfg.providers);
  // 页签：默认（内置预设）/ 自定义（用户新增），与顶栏模型菜单的分页口径一致
  tabs.hidden = !entries.length;
  tabs.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === providerTab);
    b.onclick = () => {
      providerTab = b.dataset.tab;
      renderProviderList();
    };
  });
  list.innerHTML = "";
  if (!entries.length) {
    list.innerHTML = '<p class="empty-hint">还没有可用的模型服务，点「＋ 添加自定义服务」新建一个，或在下方「已停用的服务」里恢复。</p>';
    return;
  }
  const wantPreset = providerTab === "preset";
  const rows = entries.filter(([, p]) => p.is_preset === wantPreset);
  if (!rows.length) {
    list.innerHTML = wantPreset
      ? '<p class="empty-hint">内置服务都停用了？在下方「已停用的服务」里点「恢复」即可找回。</p>'
      : '<p class="empty-hint">还没有自定义服务，点右上角「＋ 添加自定义服务」新建一个。</p>';
    return;
  }
  rows.forEach((e) => list.appendChild(providerRow(e)));

  // —— 已停用的服务（内置 + 自定义都在这里，可恢复） ——
  const hiddenBox = document.getElementById("provider-hidden");
  const hidden = providerCfg.disabled || [];
  hiddenBox.innerHTML = "";
  hiddenBox.hidden = !hidden.length;
  if (hidden.length) {
    hiddenBox.innerHTML = `
      <h4>已停用的服务</h4>
      <p class="settings-hint">停用只是从列表里隐藏：内置服务恢复时按最新出厂默认重建（默认模型可能已更新），已配置的 API Key 会保留；自定义服务配置原样保留，恢复后原样回来。</p>
      <div class="hidden-list">${hidden
        .map(
          (n) =>
            `<span class="hidden-item"><span class="item-name">${escapeHtml(n)}</span>
             <button class="btn-ghost restore" data-name="${escapeHtml(n)}">恢复</button></span>`
        )
        .join("")}</div>`;
    hiddenBox.querySelectorAll(".restore").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        try {
          await request("config.restore_provider", { name: btn.dataset.name });
        } catch (err) {
          providerStatus(`✗ 恢复失败：${err.message}`, false);
          return;
        }
        await renderSettings();
        boot();
        // 恢复的服务若不在当前页签的分组里，自动切过去，让它在列表中立即可见
        const restored = providerCfg.providers[btn.dataset.name];
        if (restored && restored.is_preset !== (providerTab === "preset")) {
          providerTab = restored.is_preset ? "preset" : "custom";
          renderProviderList();
        }
        providerStatus(`✓ 已恢复「${btn.dataset.name}」，按最新出厂默认重建（API Key 保留）`);
      };
    });
  }
}

// ---------- 模型服务：详情视图（点列表某一行进入） ----------
function openProviderDetail(name) {
  detailOpen = true;
  detailName = name;
  availListCollapsed = false; // 每次进详情默认展开
  document.getElementById("provider-list-view").hidden = true;
  document.getElementById("provider-detail").hidden = false;
  document.getElementById("btn-add-provider").hidden = true;
  document.getElementById("provider-card-title").textContent = "模型服务";
  renderProviderDetail(name);
}

function closeProviderDetail() {
  resetProviderView();
  renderProviderList();
}

// 只切视图、不重新拉数据（删除后由 renderSettings 统一刷新）
function resetProviderView() {
  detailOpen = false;
  detailName = "";
  document.getElementById("provider-detail").hidden = true;
  document.getElementById("provider-list-view").hidden = false;
  document.getElementById("btn-add-provider").hidden = false;
  const ds = document.getElementById("provider-detail-status");
  ds.hidden = true;
  ds.textContent = "";
}

function renderProviderDetail(name) {
  const p = providerCfg.providers[name];
  if (!p) return closeProviderDetail();
  document.getElementById("provider-detail-title").textContent = "编辑模型配置";

  const box = document.getElementById("provider-detail-body");
  box.dataset.kind = p.kind;
  const keyLabel = p.has_key ? "已配置，留空保持不变" : "尚未配置";
  box.innerHTML = `
    <h4 class="sec-title">基本信息</h4>
    <div class="flat-panel">
      <div class="field">
        <label>供应商类型</label>
        <select data-f="kind">
          <option value="openai">openai（OpenAI 兼容协议）</option>
          <option value="anthropic">anthropic（Anthropic 原生协议）</option>
        </select>
      </div>
      <div class="field">
        <label>供应商名称</label>
        <input data-f="name" value="${escapeHtml(name)}" disabled title="名称是配置文件里的标识，暂不支持修改">
      </div>
      <div class="field">
        <label>接口地址 base_url</label>
        <div class="url-preview" id="detail-url-preview"></div>
        <input data-f="base_url" value="${escapeHtml(p.base_url || "")}" placeholder="https://…/v1" autocomplete="off">
      </div>
      <div class="field">
        <div class="label-row">
          <label>API Key（${keyLabel}）</label>
        </div>
        <span class="key-line">
          <input data-f="api_key" type="password" autocomplete="off" placeholder="${p.has_key ? "留空保持不变" : "粘贴 API Key"}">
          <button class="eye" type="button" title="显示 / 隐藏">👁</button>
        </span>
      </div>
      <div class="toggle-row">
        <div>
          <div class="toggle-title">启用此服务</div>
          <div class="toggle-desc">关闭后它会从模型列表里消失（配置保留，随时可恢复）</div>
        </div>
        <label class="switch"><input type="checkbox" data-f="enabled" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div>
          <div class="toggle-title">提供思考强度调节</div>
          <div class="toggle-desc">在顶栏显示「思考」档位控件（自动 / 低 / 中 / 高）。「自动」不发送任何参数；若该服务的模型不认识推理参数，请关掉</div>
        </div>
        <label class="switch"><input type="checkbox" data-f="supports_reasoning" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div>
          <div class="toggle-title">模型支持图片输入（多模态）</div>
          <div class="toggle-desc">关掉后：贴图、截图会先给出可读提示，而不是把图片塞给纯文本模型换来一句上游报错。纯文本模型（如 deepseek-chat）建议关掉</div>
        </div>
        <label class="switch"><input type="checkbox" data-f="supports_vision" checked><span class="slider"></span></label>
      </div>
      <div class="field">
        <div class="label-row">
          <label>上下文上限（tokens）</label>
          <button class="btn-ghost ctx-detect" type="button"
            title="向该服务查询此模型的上下文窗口。部分服务不提供该信息时会说明原因">🔍 检测</button>
        </div>
        <input data-f="context_limit" type="number" min="0" step="1000"
               value="${p.context_limit ? p.context_limit : ""}"
               placeholder="留空 = 用全局默认 ${p.global_context_limit || 1000000}"
               title="该服务模型真实的上下文窗口；填对了才能在撑爆之前自动压缩历史">
        <div class="field-tip">按官方文档填模型的上下文窗口（如 64k 模型填 64000，128k 填 128000）。
          留空则用全局默认（当前 ${(p.global_context_limit || 1000000).toLocaleString()}，可在 设置 · 高级 里改）；
          当前生效值 ${(p.effective_context_limit || p.global_context_limit || 0).toLocaleString()} tokens</div>
        <div class="field-tip ctx-note hidden"></div>
      </div>
      <div class="field">
        <label>温度 temperature（可选）</label>
        <input data-f="temperature" type="number" min="0" max="2" step="0.1"
               value="${p.temperature === null || p.temperature === undefined ? "" : p.temperature}"
               placeholder="留空 = 用服务默认"
               title="越低越稳定保守，越高越发散有创意；多数服务默认 0.7 左右">
        <div class="field-tip">写作、头脑风暴可以调高（1.0+）；改代码、查资料建议留空或 0.2–0.5</div>
      </div>
      <div class="field">
        <label>代理地址（可选）</label>
        <input data-f="proxy" value="${escapeHtml(p.proxy || "")}" placeholder="http://127.0.0.1:7890" autocomplete="off"
          title="仅该服务的 API 请求走此代理；留空直连">
        <div class="field-tip">访问境外服务需要代理时填写；留空直连。支持 http(s)://（socks5:// 需额外依赖，推荐用本地代理软件的 http 端口）</div>
      </div>
      <div class="field">
        <label>每百万 tokens 单价（元，可选）</label>
        <div class="price-row">
          <input data-f="price_in" type="number" min="0" step="0.1" value="${p.price_in || ""}"
            placeholder="输入价" title="每百万输入 tokens 的单价（元）" autocomplete="off">
          <input data-f="price_out" type="number" min="0" step="0.1" value="${p.price_out || ""}"
            placeholder="输出价" title="每百万输出 tokens 的单价（元）" autocomplete="off">
          <input data-f="price_cache" type="number" min="0" step="0.1" value="${p.price_cache || ""}"
            placeholder="缓存命中价" title="命中提示词缓存的输入单价（元）；留空 = 不区分，输入全部按输入价计" autocomplete="off">
        </div>
        <div class="field-tip">填了输入 / 输出价，设置 · 用量 会按实际 tokens 估算费用。
          缓存命中价给对提示词缓存打折的服务用（如 DeepSeek 命中部分约为输入价的 1/10），
          填了它费用才拆得准；留空 = 不区分缓存。三个都留空 = 不计价。</div>
      </div>
    </div>

    <h4 class="sec-title">已启用模型</h4>
    <div class="flat-panel">
      <div class="panel-note">${(p.models || []).length || 1} 个模型 · 点模型切换使用，✕ 从列表删除</div>
      <div id="enabled-models"></div>
    </div>

    <div class="sec-title-row">
      <h4 class="sec-title">可用模型</h4>
      <button class="btn-ghost fetch" type="button" title="用上面填的地址和 Key 检测连接并拉取模型列表；地址或 Key 不对时在这里给出原因">⬇ 检测并获取模型</button>
    </div>
    <div class="flat-panel">
      <div class="avail-note-row">
        <div class="panel-note" id="avail-note">还没有拉取：点右上角「检测并获取模型」，或在下面手动填写模型 ID。</div>
        <button id="avail-toggle" class="link-btn hidden" type="button"></button>
      </div>
      <div id="avail-list"></div>
      <div class="add-row">
        <input data-f="manual_model" autocomplete="off" title="手动填写模型 ID（如 deepseek-chat / glm-5.3），回车或点 ＋ 添加">
        <button class="btn-ghost add-manual" type="button" title="把它设为该服务的启用模型">＋</button>
      </div>
    </div>

    <div class="pr-ops">
      <button class="btn-primary save">保存</button>
      <button class="btn-ghost use"${p.is_active ? ' disabled title="正在使用"' : ""}>${p.is_active ? "使用中" : "切换使用"}</button>
      <button class="btn-ghost setdef"${p.is_default ? ' disabled title="已是默认"' : ""}>设为默认 ★</button>
      <button class="btn-ghost danger del">${p.is_preset ? "停用" : "删除"}</button>
    </div>
    <div class="pr-msg"></div>`;

  const q = (sel) => box.querySelector(sel);
  q("select[data-f='kind']").value = p.kind;
  q("input[data-f='supports_reasoning']").checked = p.supports_reasoning !== false;
  q("input[data-f='supports_vision']").checked = p.supports_vision !== false;

  // 接口地址预览行（对齐参考图的「预览：完整端点」）
  const preview = () => {
    const base = q('input[data-f="base_url"]').value.trim().replace(/\/+$/, "");
    const kind = q("select[data-f='kind']").value;
    document.getElementById("detail-url-preview").textContent = base
      ? "预览：" + base + (kind === "anthropic" ? "/v1/messages" : "/chat/completions")
      : "";
  };
  preview();
  q('input[data-f="base_url"]').addEventListener("input", preview);
  q("select[data-f='kind']").addEventListener("change", preview);

  // Key 显示/隐藏
  q(".eye").onclick = () => {
    const k = q('input[data-f="api_key"]');
    k.type = k.type === "password" ? "text" : "password";
  };

  const renderAvailPanel = () => {
    const p2 = providerCfg.providers[detailName];
    const enabled = new Set(p2 ? p2.models || [] : []);
    const list = document.getElementById("avail-list");
    list.innerHTML = (availCacheModels || [])
      .map((m) => {
        const on = enabled.has(m);
        return `<div class="avail-model ${on ? "on" : ""}" data-model="${escapeHtml(m)}">
          <span class="plus">${on ? "✓" : "＋"}</span><span>${escapeHtml(m)}</span>
          ${on ? '<span class="chip chip-blue">已启用</span>' : ""}</div>`;
      })
      .join("");
    list.querySelectorAll(".avail-model:not(.on)").forEach((el) => {
      el.onclick = () => setEnabledModel(el.dataset.model);
    });
    // 收起 / 展开同步：有检测结果才给按钮；收起时列表整块隐藏（重新拉取也保持）
    const toggleBtn = document.getElementById("avail-toggle");
    if (toggleBtn) {
      const has = (availCacheModels || []).length > 0;
      toggleBtn.classList.toggle("hidden", !has);
      toggleBtn.textContent = availListCollapsed ? "展开 ▾" : "收起 ▴";
      toggleBtn.title = availListCollapsed ? "展开检测到的模型列表" : "收起检测到的模型列表";
    }
    list.classList.toggle("hidden", availListCollapsed);
    if (availNoteState) {
      const el = document.getElementById("avail-note");
      el.textContent = availNoteState.text;
      el.className = "panel-note " + (availNoteState.ok ? "ok" : "bad");
    }
  };

  const note = (text, ok) => {
    availNoteState = { text, ok };
    const el = document.getElementById("avail-note");
    if (el) {
      el.textContent = text;
      el.className = "panel-note " + (ok ? "ok" : "bad");
    }
  };

  // —— 已启用模型列表：✓ 当前使用，✕ 删除，点行切换 ——
  const enabledModels = p.models && p.models.length ? [...p.models] : (p.model ? [p.model] : []);
  const currentModel = p.is_active ? p.active_model || p.model : null;
  const enabledBox = document.getElementById("enabled-models");
  enabledBox.innerHTML = "";
  enabledModels.forEach((m) => {
    const isCur = p.is_active && currentModel === m;
    const li = document.createElement("div");
    li.className = "enabled-model";
    li.innerHTML = `
      <span class="ok-mark">✓</span>
      <span class="em-name">${escapeHtml(m)}</span>
      ${isCur ? '<span class="chip chip-blue">使用中</span>' : ""}
      <button class="em-del" type="button" title="从已启用列表删除">✕</button>`;
    li.querySelector(".em-name").onclick = async () => {
      if (isCur) return;
      try {
        await request("model.switch", { name, model: m });
        await renderSettings();
        boot();
        providerStatus(`✓ 已切换到「${m}」，当前对话立即生效`);
      } catch (e) {
        addNotice("切换失败: " + e.message);
      }
    };
    li.querySelector(".em-del").onclick = async (e) => {
      e.stopPropagation();
      try {
        const r = await request("config.remove_provider_model", { name, model: m });
        await renderSettings();
        boot();
        providerStatus(
          `✓ 已删除「${m}」` + (r.switched_to ? `，已切换使用「${r.switched_to}」` : "")
        );
      } catch (err) {
        addNotice("删除失败: " + err.message);
      }
    };
    enabledBox.appendChild(li);
  });
  if (!enabledModels.length)
    enabledBox.innerHTML = '<div class="empty-hint">还没有启用的模型，从下面的可用模型里添加，或手动填写模型 ID。</div>';

  const setEnabledModel = async (model) => {
    if (!model) return;
    try {
      const r = await request("config.add_provider_model", { name, model });
      await renderSettings();
      boot();
      note(
        r.added === false
          ? `「${model}」已在启用列表里`
          : `✓ 已启用「${model}」` + (r.activated ? "，并已切换使用" : ""),
        true
      );
    } catch (e) {
      note("✗ " + e.message, false);
    }
  };

  // 检测连接 / 拉取模型是同一个动作：向该服务查询可用模型列表，顺带验证地址与 Key
  const probeFill = async () => {
    const btn = q(".fetch");
    const params = {
      name,
      kind: q("select[data-f='kind']").value,
      base_url: q('input[data-f="base_url"]').value.trim(),
      api_key: q('input[data-f="api_key"]').value.trim(),
    };
    btn.disabled = true;
    note("检测中…正在向该服务查询模型列表");
    try {
      const r = await request("config.probe_models", params);
      if (!r.count) {
        availCacheModels = [];
        note("该服务没有返回任何模型（可能是接口地址不对，或该服务不提供模型列表）", false);
        renderAvailPanel();
        return;
      }
      availCacheModels = [...r.models];
      renderAvailPanel();
      note(`✓ 检测到 ${r.count} 个可用模型，点「＋」即可启用`, true);
    } catch (e) {
      note("✗ " + e.message, false);
    } finally {
      btn.disabled = false;
    }
  };
  q(".fetch").onclick = () => probeFill();

  // 上下文窗口检测：查询到窗口值就填进输入框（保存后生效），查不到给出原因
  const ctxBtn = q(".ctx-detect");
  const ctxNote = q(".ctx-note");
  ctxBtn.onclick = async () => {
    ctxBtn.disabled = true;
    ctxNote.classList.remove("hidden");
    ctxNote.textContent = "检测中…正在向该服务查询上下文窗口";
    try {
      const r = await request("config.probe_context", {
        name,
        kind: q("select[data-f='kind']").value,
        base_url: q('input[data-f="base_url"]').value.trim(),
        api_key: q('input[data-f="api_key"]').value.trim(),
      });
      if (r.limit) {
        q('input[data-f="context_limit"]').value = r.limit;
        ctxNote.textContent = `✓ ${r.note}（已填入，点「保存」后生效）`;
      } else {
        ctxNote.textContent = "✗ " + r.note;
      }
    } catch (e) {
      ctxNote.textContent = "✗ " + e.message;
    } finally {
      ctxBtn.disabled = false;
    }
  };
  q(".add-manual").onclick = () => {
    const input = q('input[data-f="manual_model"]');
    const v = input.value.trim();
    if (v) setEnabledModel(v);
    input.value = "";
  };
  // 回车 = 点 ＋：手动填模型的输入框不再只认鼠标
  q('input[data-f="manual_model"]').addEventListener("keydown", (e) => {
    if (e.key === "Enter") q(".add-manual").click();
  });
  // 收起 / 展开：检测列表可能上百行，收起给页面让地方（每次进详情默认展开）
  q("#avail-toggle").onclick = () => {
    availListCollapsed = !availListCollapsed;
    renderAvailPanel(); // 复用同一处同步逻辑（列表显隐 + 按钮文案）
  };

  // 启用开关：关闭 = 从列表隐藏（配置保留），自动回到服务列表
  q('input[data-f="enabled"]').onchange = async (e) => {
    const on = e.target.checked;
    try {
      await request("config.set_provider_enabled", { name, enabled: on });
    } catch (err) {
      e.target.checked = !on;
      addNotice("操作失败: " + err.message);
      return;
    }
    resetProviderView();
    await renderSettings();
    boot();
    providerStatus(`✓ 已${on ? "启用" : "停用"}「${name}」` + (on ? "" : "，可在下方「已停用的服务」里恢复"));
  };

  box.querySelector(".save").onclick = () =>
    saveProvider(box, name, false, q("select[data-f='kind']").value);
  box.querySelector(".use").onclick = () => switchProvider(box, name);
  box.querySelector(".setdef").onclick = () =>
    saveProvider(box, name, true, q("select[data-f='kind']").value);
  box.querySelector(".del").onclick = () => deleteProviderModal(name, p.is_preset);
  if (detailName === name && availCacheModels) renderAvailPanel(); // 还原上次拉取的可用模型
}

document.getElementById("btn-provider-back").onclick = closeProviderDetail;

// ---------- 白名单（设置页）：手动添加 / 撤销 / 清空 / 测试 / 导入导出 ----------
// 规则类型的中文标签：设置页列表、确认弹窗的「将添加规则」共用
const RULE_KIND_LABEL = { always: "整个工具", prefix: "前缀", exact: "仅此一条", glob: "通配" };

let rulesStatusTimer = null;

function ruleLabelText(r) {
  const kind = RULE_KIND_LABEL[r.kind] || r.kind;
  return `${r.tool} · ${kind} ${r.pattern || "（全部）"}`;
}

// 操作反馈统一走卡片里的状态行（设置页里对话区是隐藏的，addNotice 看不到）；
// 带「撤销」按钮的提示停留更久
function showRulesStatus(text, action) {
  const el = document.getElementById("rules-status");
  if (!el) return;
  el.innerHTML = "";
  el.appendChild(document.createTextNode(text));
  if (action) {
    const btn = document.createElement("button");
    btn.className = "btn-ghost rule-undo";
    btn.textContent = action.label;
    btn.onclick = action.onClick;
    el.appendChild(btn);
  }
  el.hidden = false;
  if (rulesStatusTimer) clearTimeout(rulesStatusTimer);
  rulesStatusTimer = setTimeout(() => {
    el.hidden = true;
    el.innerHTML = "";
  }, action ? 12000 : 6000);
}

function fmtRuleAge(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

// exact 规则的来源（轻量推断：三类调用走 exact——含拼接的命令、键盘内容、剪贴板内容，
// 以及 window 的 close 按标题固化）
function ruleOriginNote(r) {
  if (r.kind !== "exact") return "";
  if (r.tool === "keyboard") return "来源：键盘输入内容";
  if (r.tool === "clipboard_write") return "来源：剪贴板写入内容";
  if (r.tool === "window") return "来源：关闭窗口标题";
  if (r.tool === "run_command" && /[;|&<>`$\r\n]/.test(r.pattern || "")) return "来源：含拼接的命令";
  return "来源：单次调用参数";
}

// 手动添加的警示（不阻塞；danger 级需要在表单里勾选确认）
function ruleAddWarning(tool, kind) {
  if (!tool) return null;
  if (kind === "always" && tool === "run_command")
    return {
      danger: true,
      text: "「整个工具」放行 run_command 等于所有命令不再询问（包括删除类命令）。"
        + "建议改用「前缀」；仍要添加请先勾选下方确认。",
    };
  if (kind !== "exact" && (tool === "keyboard" || tool === "clipboard_write"))
    return {
      danger: false,
      text: "键盘 / 剪贴板不按动作整类放行：整类放行等于允许向任意焦点窗口输入任意内容 / "
        + "把任意内容写进剪贴板，建议改用「仅此一条」。",
    };
  if (kind === "prefix" && tool === "window")
    return {
      danger: false,
      text: "窗口动作按动作词放行；其中「关闭窗口」不会整类放行（只按标题匹配）。",
    };
  const safety = ((bootSnap && bootSnap.tools) || []).find((t) => t.name === tool)?.safety;
  if (kind === "always" && (safety === "write" || safety === "dangerous"))
    return { danger: false, text: `「整个工具」将放行 ${tool} 的全部调用（不限路径 / 目标）。` };
  return null;
}

function updateRuleAddFormState() {
  const tool = document.getElementById("rule-tool").value.trim();
  const kind = document.getElementById("rule-kind").value;
  const patternInput = document.getElementById("rule-pattern");
  const hint = document.getElementById("rule-add-hint");
  const warnEl = document.getElementById("rule-add-warn");
  const ackRow = document.getElementById("rule-add-ack-row");
  const ack = document.getElementById("rule-add-ack");
  patternInput.disabled = kind === "always";
  if (kind === "always") {
    patternInput.value = "";
    patternInput.placeholder = "「整个工具」无需填写";
    hint.textContent = "整个工具：该工具的所有调用都会直接放行。";
  } else if (kind === "prefix") {
    patternInput.placeholder = "如 git status / click";
    hint.textContent = "前缀：以这段文本开头的调用都放行（命令类工具不含带 shell 拼接的命令）。";
  } else if (kind === "exact") {
    patternInput.placeholder = "与调用文本完全一致的内容";
    hint.textContent = "仅此一条：与这段文本完全一致的调用才放行。";
  } else {
    patternInput.placeholder = "如 docs/*.md";
    hint.textContent = "通配：按 * ? 通配符匹配（Windows 下大小写不敏感）。";
  }
  const warn = ruleAddWarning(tool, kind);
  if (warn) {
    warnEl.textContent = "⚠ " + warn.text;
    warnEl.classList.toggle("danger", !!warn.danger);
    warnEl.hidden = false;
  } else {
    warnEl.hidden = true;
  }
  const needAck = !!(warn && warn.danger);
  ackRow.classList.toggle("hidden", !needAck);
  if (!needAck) ack.checked = false;
}

function resetRuleAddForm() {
  document.getElementById("rule-tool").value = "";
  document.getElementById("rule-pattern").value = "";
  document.getElementById("rule-add-ack").checked = false;
  document.getElementById("rule-add-warn").hidden = true;
  document.getElementById("rule-add-ack-row").classList.add("hidden");
  updateRuleAddFormState();
}

async function submitRuleAdd() {
  const tool = document.getElementById("rule-tool").value.trim();
  const kind = document.getElementById("rule-kind").value;
  const pattern = document.getElementById("rule-pattern").value.trim();
  if (!tool) {
    showRulesStatus("请先填写工具名", null);
    return;
  }
  const warn = ruleAddWarning(tool, kind);
  if (warn && warn.danger && !document.getElementById("rule-add-ack").checked) {
    showRulesStatus("这条规则影响较大：请先勾选「我了解这条规则的影响」", null);
    return;
  }
  try {
    await request("whitelist.add", { tool, kind, pattern });
    document.getElementById("rule-add-form").classList.add("hidden");
    resetRuleAddForm();
    await renderSettings();
    showRulesStatus(
      `已添加规则：${tool} · ${RULE_KIND_LABEL[kind] || kind}${pattern ? " " + pattern : "（全部）"}`,
      null
    );
  } catch (err) {
    showRulesStatus("添加失败：" + err.message, null);
  }
}

function downloadJson(filename, obj) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

document.getElementById("btn-rule-add").onclick = () => {
  const form = document.getElementById("rule-add-form");
  form.classList.toggle("hidden");
  if (!form.classList.contains("hidden")) {
    updateRuleAddFormState();
    document.getElementById("rule-tool").focus();
  }
};
document.getElementById("rule-add-cancel").onclick = () => {
  document.getElementById("rule-add-form").classList.add("hidden");
  resetRuleAddForm();
};
document.getElementById("rule-add-save").onclick = submitRuleAdd;
document.getElementById("rule-kind").onchange = updateRuleAddFormState;
document.getElementById("rule-tool").oninput = updateRuleAddFormState;

document.getElementById("btn-rules-clear").onclick = async () => {
  let rules = [];
  try {
    rules = ((await request("whitelist.list")).rules) || [];
  } catch (err) {
    showRulesStatus("读取规则失败：" + err.message, null);
    return;
  }
  if (!rules.length) {
    showRulesStatus("当前没有规则可清空", null);
    return;
  }
  const box = document.createElement("div");
  box.innerHTML = `<p>确定清空本项目的全部 <b>${rules.length}</b> 条白名单规则吗？</p>
    <p class="dim small">清空后，对应的写入 / 执行 / 操作会重新逐次询问。删除后可以点状态栏的「撤销」恢复。</p>`;
  showModal("清空白名单", box, async () => {
    const snapshot = rules.map((r) => ({ tool: r.tool, kind: r.kind, pattern: r.pattern || "" }));
    const resp = await request("whitelist.clear", {});
    await renderSettings();
    showRulesStatus(`已清空 ${resp.removed} 条规则`, {
      label: "撤销",
      onClick: async () => {
        try {
          for (const item of snapshot) await request("whitelist.add", item);
          await renderSettings();
          showRulesStatus(`已恢复 ${snapshot.length} 条规则`, null);
        } catch (err) {
          showRulesStatus("恢复失败：" + err.message, null);
        }
      },
    });
  }, "清空");
};

document.getElementById("btn-rules-export").onclick = async () => {
  try {
    const data = await request("whitelist.export");
    const safeName = (data.project || "project").replace(/[\\/:*?"<>|\s]+/g, "_");
    downloadJson(`skysheep-whitelist-${safeName}.json`, data);
    showRulesStatus(`已导出 ${(data.rules || []).length} 条规则`, null);
  } catch (err) {
    showRulesStatus("导出失败：" + err.message, null);
  }
};

document.getElementById("btn-rules-import").onclick = () => {
  const box = document.createElement("div");
  box.innerHTML = `<p>粘贴白名单 JSON（导出文件 <code>skysheep-whitelist-*.json</code> 的内容）。</p>
    <textarea id="rule-import-text" class="rule-import-text" rows="6"
      placeholder='{"version":1,"rules":[{"tool":"run_command","kind":"prefix","pattern":"git status"}]}'></textarea>
    <p class="dim small">合并导入：已有的相同规则会跳过，不影响现有规则。</p>`;
  showModal("导入白名单", box, async () => {
    const raw = box.querySelector("#rule-import-text").value.trim();
    if (!raw) throw new Error("请先粘贴 JSON 内容");
    let data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      throw new Error("内容不是合法 JSON");
    }
    const rules = Array.isArray(data) ? data : data.rules;
    if (!Array.isArray(rules)) throw new Error("JSON 里没有 rules 数组");
    const resp = await request("whitelist.import", { rules });
    await renderSettings();
    showRulesStatus(
      `已导入 ${resp.added} 条规则`
        + (resp.skipped ? `，跳过 ${resp.skipped} 条（重复或格式不符）` : ""),
      null
    );
  }, "导入");
};

const ruleCheckRun = async () => {
  const tool = document.getElementById("rule-check-tool").value.trim();
  const text = document.getElementById("rule-check-text").value;
  const out = document.getElementById("rule-check-result");
  if (!tool) {
    out.textContent = "请先填写工具名";
    out.className = "rule-check-result warn";
    out.hidden = false;
    return;
  }
  try {
    const r = await request("whitelist.check", { tool, text });
    if (r.allowed) {
      const hit = r.hit || {};
      out.textContent = `✓ 会直接放行 —— 命中规则：${hit.tool} · ${RULE_KIND_LABEL[hit.kind] || hit.kind} ${hit.pattern || "（全部）"}`;
      out.className = "rule-check-result ok";
    } else {
      out.textContent = `✗ 需要确认 —— ${r.reason}`;
      out.className = "rule-check-result warn";
    }
    out.hidden = false;
  } catch (err) {
    out.textContent = "测试失败：" + err.message;
    out.className = "rule-check-result warn";
    out.hidden = false;
  }
};
document.getElementById("btn-rule-check").onclick = ruleCheckRun;
document.getElementById("rule-check-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    ruleCheckRun();
  }
});

async function renderSettings() {
  const cfg = await request("config.providers");
  const [{ rules }, snap] = await Promise.all([
    request("whitelist.list"),
    request("boot"),
  ]);
  bootSnap = snap;

  // —— 模型服务：列表视图 / 详情视图 ——
  providerCfg = cfg;
  if (detailOpen && cfg.providers[detailName]) renderProviderDetail(detailName);
  else if (detailOpen) closeProviderDetail(); // 配置项已被删掉 → 退回列表
  else renderProviderList();

  // —— 白名单 ——
  const ul = document.getElementById("rules-list");
  ul.innerHTML = "";
  const safetyByName = {};
  (snap.tools || []).forEach((t) => { safetyByName[t.name] = t.safety; });
  // 工具名候选：现役工具 ∪ 已有规则里出现过的工具（MCP 暂时掉线的名字也应能预置）
  const toolDl = document.getElementById("rule-tool-options");
  if (toolDl) {
    const names = new Set((snap.tools || []).map((t) => t.name));
    (rules || []).forEach((r) => names.add(r.tool));
    toolDl.innerHTML = [...names].sort()
      .map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
  }
  // 摘要：规则总数 + 整工具放行数量（后者是风险最直观的信号）
  const summaryEl = document.getElementById("rules-summary");
  const wholeCount = (rules || []).filter((r) => r.kind === "always").length;
  if (rules && rules.length) {
    summaryEl.textContent = `共 ${rules.length} 条规则`
      + (wholeCount ? `；其中 ${wholeCount} 条为「整个工具」放行` : "");
    summaryEl.classList.toggle("risk", wholeCount > 0);
    summaryEl.hidden = false;
  } else {
    summaryEl.hidden = true;
  }
  (rules || []).forEach((r) => {
    const li = document.createElement("li");
    // 规则语义说清楚，否则用户会以为前缀规则能放行任何以它开头的调用：
    // 命令的前缀规则不覆盖 shell 拼接（`a; b` 要重新确认）；键盘 / 剪贴板 / 关窗口
    // 的「总是允许」只固化那一次的内容，不按动作整类放行。
    const kind = RULE_KIND_LABEL[r.kind] || r.kind;
    const extra = r.kind !== "prefix" ? ""
      : r.tool === "run_command" ? "（命令拼接不会命中）"
      : r.tool === "browser" ? "（同动作均放行：打开任意网址）"
      : r.tool === "mouse" ? "（同动作均放行：点任意位置）"
      : "（同动作均放行）";
    // 整工具放行且属写 / 高危工具：金色点提示（工具未知时不区分）
    const risky = r.kind === "always"
      && (safetyByName[r.tool] === "write" || safetyByName[r.tool] === "dangerous");
    const origin = ruleOriginNote(r);
    const age = fmtRuleAge(r.created_at);
    const tip = [kind + extra, origin].filter(Boolean).join("；");
    // 命中统计：帮用户看出哪些规则还在用、哪些可以清理
    const hits = r.hit_count > 0
      ? `<span class="rule-age" title="白名单放行累计 ${r.hit_count} 次，最近一次 ${fmtRuleAge(r.last_hit_at)}">命中 ${r.hit_count} 次</span>`
      : "";
    const enabled = r.enabled !== false;
    li.innerHTML = `<span class="dot ${risky ? "warn" : "on"}"${risky ? ' title="整工具放行（写 / 高危）"' : ""}></span>
      <span class="rule-text${enabled ? "" : " rule-off"}" title="${escapeHtml(tip)}">${escapeHtml(r.tool)} · ${escapeHtml(kind)}${escapeHtml(extra)} ${escapeHtml(r.pattern || "(全部)")}${origin ? ` <span class="rule-origin">· ${escapeHtml(origin)}</span>` : ""}${enabled ? "" : ' <span class="rule-origin">· 已停用</span>'}</span>
      ${hits}
      ${age ? `<span class="rule-age">${escapeHtml(age)}</span>` : ""}
      <label class="rule-toggle" title="${enabled ? "停用后保留配置，不再参与放行" : "重新启用这条规则"}">
        <input type="checkbox" data-rule-toggle="${r.id}" ${enabled ? "checked" : ""}>
      </label>
      <button class="rule-del">删除</button>`;
    li.querySelector("[data-rule-toggle]").onchange = async (e) => {
      try {
        await request("whitelist.enable", { id: r.id, enabled: e.target.checked });
        await renderSettings();
        showRulesStatus(e.target.checked ? "已启用规则" : "已停用规则（配置保留）", null);
      } catch (err) {
        showRulesStatus("操作失败：" + err.message, null);
        e.target.checked = !e.target.checked;
      }
    };
    li.querySelector(".rule-del").onclick = async () => {
      try {
        const label = ruleLabelText(r);
        await request("whitelist.remove", { id: r.id });
        await renderSettings();
        showRulesStatus(`已删除：${label}`, {
          label: "撤销",
          onClick: async () => {
            try {
              await request("whitelist.add", {
                tool: r.tool, kind: r.kind, pattern: r.pattern || "",
              });
              await renderSettings();
              showRulesStatus("已恢复规则", null);
            } catch (err) {
              showRulesStatus("恢复失败：" + err.message, null);
            }
          },
        });
      } catch (err) {
        showRulesStatus("删除失败：" + err.message, null);
      }
    };
    ul.appendChild(li);
  });
  if (!rules || !rules.length) {
    ul.innerHTML = '<li class="empty-hint">暂无规则（对话中选「总是允许」后会出现在这里，也可以点上方「＋ 添加规则」手动预设）</li>';
  }

  // —— 技能 ——
  // 列表渲染抽到 renderSkillList（技能页要重复用）；这里只负责总览摘要
  renderSkillSummary(snap.skills || []);
  if (skillManageOpen) renderSkillList(snap.skills || []);

  // —— 内置工具 ——
  const tul = document.getElementById("settings-tool-list");
  tul.innerHTML = "";
  const safetyMeta = {
    readonly: ["只读", "safe-mark"],
    write: ["写入", "write-mark"],
    dangerous: ["高危", "danger-mark"],
  };
  (snap.tools || []).forEach((t) => {
    const [label, cls] = safetyMeta[t.safety] || [t.safety, ""];
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="item-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</span>
      <span class="chip ${cls}">${escapeHtml(label)}</span>
      <span class="item-desc" title="${escapeHtml(t.description)}">${escapeHtml(t.description)}</span>`;
    tul.appendChild(li);
  });
  if (!(snap.tools || []).length) tul.innerHTML = '<li class="empty-hint">无工具</li>';
  // 折叠标题带上工具数（清单默认收起，展开查看全部）
  const toolFoldCap = document.getElementById("tool-list-fold-cap");
  if (toolFoldCap) toolFoldCap.textContent = `全部内置工具（${(snap.tools || []).length} 个）——点此展开 / 收起`;

  // —— MCP 服务 ——
  const mul = document.getElementById("settings-mcp-list");
  mul.innerHTML = "";
  renderMcpPresets(snap);
  (snap.mcp || []).forEach((m) => {
    const li = document.createElement("li");
    if (m.enabled === false) li.classList.add("mcp-disabled");
    const toolChips = (m.tools || [])
      .map((t) => `<span class="chip">${escapeHtml(t)}</span>`)
      .join("");
    // 停用是「配置保留、只是不连接」：与删除（配置也删）区分开
    const stateText = m.enabled === false
      ? "已停用"
      : (m.connected ? `已连接 · ${m.tools.length} 个工具` : escapeHtml(m.error || "未连接"));
    li.innerHTML = `
      <div class="mcp-head">
        <span class="dot ${m.connected ? "on" : "off"}"></span>
        <span class="item-name">${escapeHtml(m.name)}</span>
        <span class="${m.connected ? "mcp-ok" : "mcp-bad"}">${stateText}</span>
        <button class="btn-ghost mcp-toggle" title="${m.enabled === false ? "重新连接这个服务" : "停用：配置保留，工具从 Agent 移除"}">${m.enabled === false ? "启用" : "停用"}</button>
        <button class="btn-ghost danger mcp-del" title="删除这个 MCP 服务">删除</button>
      </div>
      ${toolChips ? `<div class="mcp-tools">${toolChips}</div>` : ""}`;
    li.querySelector(".mcp-del").onclick = () => deleteMcpModal(m.name);
    li.querySelector(".mcp-toggle").onclick = async () => {
      const enable = m.enabled === false;
      try {
        const r = await request("mcp.set_enabled", { name: m.name, enabled: enable });
        mcpStatus(r.hint || (enable ? "已启用" : "已停用"), enable);
      } catch (e) {
        mcpStatus((enable ? "启用" : "停用") + "失败：" + e.message, false);
      }
      await renderSettings();
      boot();
    };
    mul.appendChild(li);
  });
  if (!(snap.mcp || []).length)
    mul.innerHTML = '<li class="empty-hint">还没有 MCP 服务：点右上角「＋ 导入配置」粘贴一段配置，或「＋ 手动添加」逐个填</li>';

  // —— 关于 ——
  // 版本号只由下方更新面板展示（这里是项目身份与开源链接）；无项目态 working_dir
  // 为空串，要给可读回退而不是渲染出「工作目录：」后面一片空白
  document.getElementById("about-info").innerHTML = `
    <span>SkySheep 开源项目（${snap.frozen ? "安装版" : "源码版"}）·
      <a href="${REPO_PAGE}" target="_blank">项目主页</a> ·
      <a href="${REPO_PAGE}/blob/main/CHANGELOG.md" target="_blank">更新日志</a> ·
      <a href="${REPO_PAGE}/blob/main/LICENSE" target="_blank">MIT License</a></span>
    <span>工作目录：${snap.working_dir
      ? `<b class="copyable-path" title="点击复制">${escapeHtml(snap.working_dir)}</b>`
      : '<span class="dim">未设置（快聊模式，不读写文件）</span>'}</span>
    <span>配置文件：<b class="copyable-path" title="点击复制">${escapeHtml(cfg.config_path)}</b>（设置页保存的就是这个文件）</span>`;
  renderUpdatePanel(snap);
  renderWebsearchCfg().catch(() => {});
  renderImagegenCfg().catch(() => {});
  renderSpeechCfg().catch(() => {});
  renderRoundtableCfg().catch(() => {});
  renderSubagentCfg().catch(() => {});

  // —— 数据与隐私 ——
  document.getElementById("about-privacy").innerHTML = `
    <span>会话数据库：<b class="copyable-path" title="点击复制">${escapeHtml(cfg.db_path)}</b>（所有对话记录都存在这里）</span>
    <span>配置与技能：<b class="copyable-path" title="点击复制">${escapeHtml(cfg.home_dir)}</b>（config.toml、skills\\、mcp.json）</span>
    <span>API Key 只写入本机 config.toml，界面里也只显示打码后的片段。</span>`;

  // —— 设置导出 / 导入（换机迁移）——
  const io = document.getElementById("about-io");
  if (io && !io.dataset.bound) {
    io.dataset.bound = "1";
    const expBtn = io.querySelector("#btn-settings-export");
    const impBtn = io.querySelector("#btn-settings-import");
    const ioMsg = io.querySelector(".io-msg");
    expBtn.onclick = async () => {
      ioMsg.textContent = "打包中…";
      try {
        const r = await request("settings.export");
        downloadDataZip(r.filename, r.b64);
        ioMsg.textContent = "✓ 已导出：" + (r.included || []).join("、") +
          "。zip 内含明文 API Key，请妥善保管";
        ioMsg.className = "io-msg ok";
      } catch (e) {
        ioMsg.textContent = "✗ " + e.message;
        ioMsg.className = "io-msg bad";
      }
    };
    impBtn.onclick = () => {
      const inp = document.createElement("input");
      inp.type = "file";
      inp.accept = ".zip";
      inp.onchange = async () => {
        const f = inp.files[0];
        if (!f) return;
        ioMsg.textContent = "导入中…";
        try {
          const b64 = await new Promise((res, rej) => {
            const fr = new FileReader();
            fr.onload = () => res(String(fr.result).split(",")[1] || "");
            fr.onerror = () => rej(new Error("读取文件失败"));
            fr.readAsDataURL(f);
          });
          const r = await request("settings.import", { b64 });
          ioMsg.textContent = "✓ 已恢复：" + (r.restored || []).join("、") +
            (r.skipped && r.skipped.length ? "（跳过：" + r.skipped.join("、") + "）" : "") +
            "。配置已重载。";
          ioMsg.className = "io-msg ok";
          boot();
        } catch (e) {
          ioMsg.textContent = "✗ " + e.message;
          ioMsg.className = "io-msg bad";
        }
      };
      inp.click();
    };
  }
}

async function saveProvider(row, name, setDefault, kind) {
  const msg = row.querySelector(".pr-msg");
  msg.textContent = "保存中…";
  msg.className = "pr-msg";
  const get = (f) => {
    const el = row.querySelector(`input[data-f="${f}"]`);
    return el ? el.value.trim() : "";
  };
  const params = { name, set_default: setDefault };
  const model = get("model");
  const baseUrl = get("base_url");
  const apiKey = get("api_key");
  if (model) params.model = model;
  if (baseUrl) params.base_url = baseUrl;
  if (apiKey) params.api_key = apiKey;
  if (kind) params.kind = kind;
  const reasoningToggle = row.querySelector('input[data-f="supports_reasoning"]');
  if (reasoningToggle) params.supports_reasoning = reasoningToggle.checked;
  const visionToggle = row.querySelector('input[data-f="supports_vision"]');
  if (visionToggle) params.supports_vision = visionToggle.checked;
  // 上下文上限 / 温度：空值也要传（表示"清除该项，回到默认"）
  if (row.querySelector('input[data-f="context_limit"]')) {
    const cl = get("context_limit");
    params.context_limit = cl === "" ? 0 : Number(cl);
  }
  if (row.querySelector('input[data-f="temperature"]')) {
    params.temperature = get("temperature");  // 空串 = 清除
  }
  // 代理：始终传（字符串可为空 = 清除），后端校验格式
  const proxyInput = row.querySelector('input[data-f="proxy"]');
  if (proxyInput) params.proxy = proxyInput.value.trim();
  // 单价：表单里始终带着当前值，整组回传（空 = 0 不计价）；详情页之外的
  // 紧凑行没有这三格，不传即保持原值
  if (row.querySelector('input[data-f="price_in"]')) {
    for (const f of ["price_in", "price_out", "price_cache"]) {
      const v = get(f);
      params[f] = v === "" ? 0 : Number(v);
    }
  }
  try {
    const r = await request("config.save_provider", params);
    msg.textContent = "✓ 已保存" + (r.rebuilt ? "，使用中的模型已热更新" : "");
    msg.className = "pr-msg ok";
    if (!r.provider_error && r.rebuilt) {
      hideBanner();
      curProviderName = name;
      curModelName = r.model;
      setModelChip(`${name}/${r.model}`);
      refreshReasoning();
    }
    if (setDefault) {
      // 默认标记在标题上，必须重渲染才能看到；反馈放到状态行
      await renderSettings();
      providerStatus(`✓ 「${name}」已设为默认，下次启动就用它`);
    } else {
      // 重渲染：Key 掩码/徽章/协议等展示信息跟着更新（输入框里的明文 Key 也随之清空）
      await renderSettings();
      providerStatus(`✓ 已保存「${name}」` + (r.rebuilt ? "，使用中的模型已热更新" : ""));
    }
  } catch (e) {
    msg.textContent = "✗ " + e.message;
    msg.className = "pr-msg bad";
  }
}

// 切换当前对话使用的模型（立即生效，写进 agent.provider）
async function switchProvider(row, name) {
  const msg = row.querySelector(".pr-msg");
  msg.textContent = "切换中…";
  msg.className = "pr-msg";
  try {
    const r = await request("model.switch", { name });
    hideBanner();
    curProviderName = r.provider;
    curModelName = r.model;
    if (r.supports_vision !== undefined) curSupportsVision = r.supports_vision !== false;
    setModelChip(`${r.provider}/${r.model}`);
    await renderSettings();
    providerStatus(`✓ 已切换到「${name}」，当前对话立即生效`);
  } catch (e) {
    msg.textContent = "✗ " + e.message;
    msg.className = "pr-msg bad";
  }
}

// 添加自定义模型服务（表单在弹窗内，校验失败就地报错、不关窗）
function providerStatus(text, ok = true) {
  // 详情视图打开时状态要显示在详情里（列表视图此时是隐藏的）
  const el = detailOpen
    ? document.getElementById("provider-detail-status")
    : document.getElementById("provider-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = false;
}

function addProviderModal() {
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">接入内置列表之外的服务：自建中转、公司内网网关、或其他厂商的 OpenAI 兼容接口。
    保存后写入本机 config.toml，可随时在列表里改或删除。</p>
    <div class="form-grid">
      <label>名称（英文标识，用于 config 与日志）
        <input data-f="name" placeholder="my-relay" autocomplete="off">
      </label>
      <label>协议类型
        <select data-f="kind">
          <option value="openai">openai（OpenAI 兼容，多数服务选这个）</option>
          <option value="anthropic">anthropic（Anthropic 原生协议）</option>
        </select>
      </label>
      <label class="wide">接口地址 base_url
        <input data-f="base_url" placeholder="https://your-relay.example.com/v1" autocomplete="off">
      </label>
      <label class="wide model-label">模型名（当前使用；点「检测」后可勾选多个一并启用）
        <span class="model-line">
          <input data-f="model" placeholder="glm-5.3 / deepseek-chat / …" autocomplete="off">
          <button class="btn-ghost detect" type="button" title="用上面填的地址和 Key 查询可用模型">检测</button>
        </span>
      </label>
      <div class="ap-pick hidden"></div>
      <label class="wide">API Key（可留空，用环境变量 &lt;名称大写&gt;_API_KEY 提供）
        <input data-f="api_key" type="password" autocomplete="off" placeholder="留空表示稍后配置">
      </label>
      <div class="form-status"></div>
      <label class="wide inline-check">
        <input data-f="use_now" type="checkbox" checked> 添加后立即切换到它（当前对话马上生效）
      </label>
      <label class="wide inline-check">
        <input data-f="set_default" type="checkbox"> 同时设为默认模型（下次启动也用它）
      </label>
    </div>`;
  const val = (f) => {
    const el = box.querySelector(`[data-f="${f}"]`);
    return el.type === "checkbox" ? el.checked : el.value.trim();
  };
  // 检测可用模型：勾选要一并启用的（可多选），上面的模型名 = 当前使用的那个
  const formStatus = box.querySelector(".form-status");
  const detectBtn = box.querySelector(".detect");
  const pickWrap = box.querySelector(".ap-pick");
  const checkedModels = () =>
    [...pickWrap.querySelectorAll("input:checked")].map((c) => c.value);
  detectBtn.onclick = async () => {
    detectBtn.disabled = true;
    formStatus.textContent = "检测中…正在向该服务查询模型列表";
    formStatus.className = "form-status";
    try {
      const r = await request("config.probe_models", {
        kind: val("kind"),
        base_url: val("base_url"),
        api_key: val("api_key"),
      });
      if (!r.count) {
        formStatus.textContent = "该服务没有返回任何模型（可能是接口地址不对，或该服务不提供模型列表）";
        formStatus.className = "form-status bad";
      } else {
        const cur = val("model");
        pickWrap.classList.remove("hidden");
        pickWrap.innerHTML =
          `<div class="ap-pick-head"><span>检测到 ${r.count} 个可用模型，勾选要一并启用的</span>` +
          `<span><button class="link-btn" type="button" data-a="all">全选</button>` +
          `<button class="link-btn" type="button" data-a="none">清空</button></span></div>` +
          `<div class="ap-list">` +
          r.models.map((m) =>
            `<label class="ap-row"><input type="checkbox" value="${escapeHtml(m)}"${m === cur ? " checked" : ""}>` +
            `<span class="mono">${escapeHtml(m)}</span></label>`).join("") +
          `</div>`;
        pickWrap.querySelector('[data-a="all"]').onclick = () =>
          pickWrap.querySelectorAll("input").forEach((c) => (c.checked = true));
        pickWrap.querySelector('[data-a="none"]').onclick = () =>
          pickWrap.querySelectorAll("input").forEach((c) => (c.checked = false));
        formStatus.textContent = `✓ 检测到 ${r.count} 个可用模型，勾选后随服务一并启用（当前模型始终启用）`;
        formStatus.className = "form-status ok";
      }
    } catch (e) {
      formStatus.textContent = "✗ " + e.message;
      formStatus.className = "form-status bad";
    } finally {
      detectBtn.disabled = false;
    }
  };
  showModal("添加自定义模型服务", box, async () => {
    const primary = val("model");
    // 一并启用 = 主输入框的当前模型 + 勾选项（去重保序，主模型在首位）
    const models = [primary, ...checkedModels().filter((m) => m !== primary)]
      .filter((m, i, a) => m && a.indexOf(m) === i);
    const r = await request("config.add_provider", {
      name: val("name"),
      kind: val("kind"),
      base_url: val("base_url"),
      model: primary,
      api_key: val("api_key"),
      set_default: val("set_default"),
      models: models.length > 1 ? models : undefined,
    });
    let switched = false;
    let switchErr = "";
    providerTab = "custom"; // 添加的是自定义服务：切到自定义页签，让新服务立即可见
    if (val("use_now")) {
      try {
        const s = await request("model.switch", { name: r.added });
        switched = true;
        hideBanner();
        curProviderName = s.provider;
        curModelName = s.model;
        setModelChip(`${s.provider}/${s.model}`);
      } catch (e) {
        switchErr = e.message;
      }
    }
    await renderSettings();
    boot();
    const cnt = `已启用 ${models.length} 个模型`;
    if (switched) providerStatus(`✓ 已添加「${r.added}」并切换使用，当前对话立即生效（${cnt}）`);
    else if (switchErr) providerStatus(`✓ 已添加「${r.added}」，但切换失败：${switchErr}（${cnt}）`, false);
    else if (r.activated) providerStatus(`✓ 已添加「${r.added}」，原先没有可用模型，已自动启用它（${cnt}）`);
    else providerStatus(`✓ 已添加「${r.added}」，${cnt}；需要用时点开它，在里面点「切换使用」`);
  }, "添加");
}

// 从列表里移除模型服务：自定义彻底删除；内置（及停用自定义）只是停用，可在下方恢复
function deleteProviderModal(name, isPreset) {
  const verb = isPreset ? "停用" : "删除";
  const box = document.createElement("div");
  box.innerHTML = isPreset
    ? `<p>确定停用内置服务 <b>${escapeHtml(name)}</b> 吗？</p>
       <p class="dim small">它会从列表里消失（配置保留，本机 config.toml 不再加载它），
       之后可以在列表下方的「已停用的服务」里点「恢复」找回来；恢复时按最新出厂默认重建，
       API Key 会保留。</p>`
    : `<p>确定删除自定义模型服务 <b>${escapeHtml(name)}</b> 吗？</p>
       <p class="dim small">只会从本机 config.toml 里移除这一项；内置服务与历史会话不受影响。</p>`;
  showModal(isPreset ? "停用内置服务" : "删除模型服务", box, async () => {
    const r = await request("config.delete_provider", { name });
    if (detailOpen && detailName === name) resetProviderView(); // 被删的就是当前详情 → 回列表
    await renderSettings();
    boot();
    const suffix = r.was_active
      ? "，请在列表里另选一个模型使用"
      : r.hidden
        ? "，可在下方「已停用的服务」里恢复"
        : "";
    providerStatus(`✓ 已${verb}「${name}」${suffix}`, !r.was_active);
  }, verb);
}

document.getElementById("btn-add-provider").onclick = addProviderModal;

// ---------- 技能：导入 / 删除 ----------

// 桌面窗口里能弹原生选择框（pywebview 桥）；浏览器模式下退回手动粘贴路径
function nativePickerAvailable() {
  return !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick);
}

async function pickPath(kind) {
  if (!nativePickerAvailable()) return { paths: [], error: "no-picker" };
  try {
    return await window.pywebview.api.pick(kind);
  } catch (e) {
    return { paths: [], error: e.message };
  }
}

function skillStatus(text, ok = true) {
  const el = document.getElementById("skill-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

function mcpStatus(text, ok = true) {
  const el = document.getElementById("mcp-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

// 内置常用 MCP 预设：每张卡点「＋ 添加」即写入配置并即时连接、工具立刻可用。
// 数据来自 boot 快照的 mcp_presets（后端 presets.py 单一事实源）。
function renderMcpPresets(snap) {
  const box = document.getElementById("settings-mcp-presets");
  if (!box) return;
  const presets = snap.mcp_presets || [];
  const installed = new Set(snap.mcp_installed || []);
  box.innerHTML = "";
  if (!presets.length) return;
  const head = document.createElement("div");
  head.className = "mcp-preset-head";
  head.textContent = "常用预设 · 点「＋ 添加」一键接入";
  box.appendChild(head);
  presets.forEach((p) => {
    const needMap = { uv: "需 uv（随 SkySheep 自带）", node: "需 Node.js", local: "需先安装本机程序" };
    // available 是后端启动时对本机运行时（npx/uvx/独立程序）的探测结果：
    // 缺运行时的卡直接置灰并说明缺什么，而不是点了添加才收到「启动命令不存在」
    const missing = p.available === false;
    const need = missing
      ? { uv: "未检测到 uv", node: "未检测到 Node.js", local: "未检测到本机程序" }[p.need]
      : (needMap[p.need] || "需 Node.js");
    const has = installed.has(p.name);
    const card = document.createElement("div");
    card.className = "mcp-preset-card" + (has ? " added" : "") + (missing ? " unavailable" : "");
    card.innerHTML = `
      <div class="mpc-top">
        <span class="mpc-label">${escapeHtml(p.label)}</span>
        ${p.readonly ? '<span class="chip safe-mark">只读</span>' : ""}
      </div>
      <div class="mpc-desc" title="${escapeHtml(p.desc)}">${escapeHtml(p.desc)}</div>
      <div class="mpc-foot">
        <span class="mpc-need" title="${escapeHtml(p.desc)}">${escapeHtml(need)}</span>
        <button type="button" class="btn-ghost mpc-add"${has || missing ? " disabled" : ""}>${has ? "✓ 已添加" : (missing ? "缺运行时" : "＋ 添加")}</button>
      </div>`;
    const btn = card.querySelector(".mpc-add");
    if (!has && !missing) {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "添加中…";
        let r;
        try {
          r = await request("mcp.add_preset", { name: p.name });
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "＋ 添加";
          mcpStatus("添加失败：" + e.message, false);
          return;
        }
        await renderSettings();
        boot();
        const bad = (r.mcp_warnings || []).length > 0;
        mcpStatus(r.hint || (bad ? "已添加，但有服务没连上，详见列表" : "已添加"), !bad);
      };
    }
    box.appendChild(card);
  });
}

// 导入技能：优先弹系统选择框；浏览器里手动粘贴路径；也支持直接贴网址下载安装
function importSkillModal(prefill = "") {
  const box = document.createElement("div");
  const canPick = nativePickerAvailable();
  box.innerHTML = `
    <p class="dim small">三种装法任选其一：点「选择…」挑一个技能文件夹或 .zip 技能包；
    直接粘贴本机路径；或粘贴 <b>GitHub / Gitee 仓库链接</b>（也认 .zip 直链），程序会下载后自动安装。</p>
    <div class="form-grid">
      <label class="wide">技能文件夹 / .zip 路径
        <span class="model-line">
          <input data-f="source" placeholder="${canPick ? "点右边按钮选择，或直接粘贴路径" : "把技能文件夹或 .zip 的完整路径粘贴到这里"}" value="${escapeHtml(prefill)}" autocomplete="off">
          <button class="btn-ghost browse" type="button">选择…</button>
        </span>
      </label>
      <label class="wide">或从网址安装（GitHub / Gitee 仓库链接、.zip 直链）
        <input data-f="url" placeholder="https://github.com/用户名/仓库" autocomplete="off">
      </label>
      <label>装到哪里
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <div class="form-status"></div>
    </div>`;
  const input = box.querySelector('input[data-f="source"]');
  const urlInput = box.querySelector('input[data-f="url"]');
  const status = box.querySelector(".form-status");
  const browse = box.querySelector(".browse");
  if (!canPick) browse.title = "当前是浏览器模式，请手动粘贴路径";
  browse.onclick = async () => {
    const r = await pickPath("skill_dir");
    if (r.error) {
      // 原生框打不开时再试 .zip（有些环境选文件夹的实现不一样）
      const z = await pickPath("skill_zip");
      if (z.paths && z.paths.length) {
        input.value = z.paths[0];
        status.textContent = "已选择：" + z.paths[0];
        status.className = "form-status ok";
        return;
      }
      status.textContent = "打不开系统选择框，请手动粘贴路径（" + r.error + "）";
      status.className = "form-status bad";
      input.focus();
      return;
    }
    if (!r.paths || !r.paths.length) return; // 用户取消
    input.value = r.paths[0];
    status.textContent = "已选择：" + r.paths[0];
    status.className = "form-status ok";
  };
  showModal("导入技能", box, async () => {
    const source = input.value.trim();
    const url = urlInput.value.trim();
    if (source && url) throw new Error("两种方式选一种：本地路径 或 网址，别都填");
    if (!source && !url) throw new Error("请选择/粘贴技能路径，或粘贴要安装的网址");
    if (url) {
      status.textContent = "正在下载技能包，请稍候…";
      status.className = "form-status";
    }
    const r = await request("skills.install", {
      source: url || source,
      scope: box.querySelector('select[data-f="scope"]').value,
    });
    await renderSettings();
    boot();
    const where = r.scope === "project" ? "本项目" : "全局";
    skillStatus(`✓ 已导入 ${r.count} 个技能到${where}：${r.installed.join("、")}（已启用，可直接使用）`);
  }, "导入");
}

// 本机现存技能：只读探测 Claude Code / agents / Codex / 本项目 .claude 里已有的技能，
// 结果直接平铺在技能页按钮下方的面板里（不再弹窗），勾选后复用既有导入逻辑
// （skills.install 复制安装），探测本身不动任何文件
async function loadLocalSkills() {
  const panel = document.getElementById("skill-local-panel");
  if (!panel) return;
  panel.hidden = false;
  const list = document.getElementById("skill-local-list");
  const note = document.getElementById("skill-local-note");
  list.innerHTML = '<li class="empty-hint">正在探测本机技能目录（Claude Code / agents / Codex / 本项目 .claude）…</li>';
  note.textContent = "";
  let r;
  try {
    r = await request("skills.scan_local");
  } catch (e) {
    list.innerHTML = `<li class="empty-hint">✗ 探测失败：${escapeHtml(e.message)}</li>`;
    skillStatus("✗ 读取本机技能失败: " + e.message, false);
    return;
  }
  const cands = r.candidates || [];
  if (!cands.length) {
    list.innerHTML = `<li class="empty-hint">本机常见技能目录里没有找到技能。
      探测位置：~/.claude/skills、~/.agents/skills、~/.codex/skills、本项目 .claude/skills。
      技能在别处的话，用「＋ 导入技能」选文件夹，或粘贴路径 / GitHub·Gitee 链接安装。</li>`;
    note.textContent = "";
    return;
  }
  const doneCnt = cands.filter((c) => c.installed).length;
  note.textContent = `共 ${cands.length} 个${doneCnt ? `，其中 ${doneCnt} 个已装过（灰显不可选）` : "，均可导入"}`;
  list.innerHTML = cands.map((c) => `
    <li><label class="${c.installed ? "done" : ""}" title="${escapeHtml(c.path)}">
      <input type="checkbox" data-path="${escapeHtml(c.path)}" ${c.installed ? "disabled" : "checked"}>
      <span class="scan-name">${escapeHtml(c.name)}</span>
      <span class="scan-desc">${escapeHtml(c.description || "")}</span>
      <span class="scan-origin">${escapeHtml(c.origin)}</span>
    </label></li>`).join("");
}

// 面板里的全选 / 全不选：只作用于可选（未装过）的勾选框
function bindLocalSkillToolbar() {
  const panel = document.getElementById("skill-local-panel");
  if (!panel) return;
  panel.querySelectorAll(".scan-toolbar button").forEach((b) => {
    b.onclick = () => {
      panel.querySelectorAll(".scan-list input[type=checkbox]:not(:disabled)").forEach(
        (cb) => { cb.checked = b.dataset.act === "all"; });
    };
  });
}

// 导入面板里勾选的候选：逐个走 skills.install，单个失败不中断其余
async function importLocalSkills() {
  const panel = document.getElementById("skill-local-panel");
  const scope = document.getElementById("skill-local-scope").value;
  const picked = [...panel.querySelectorAll(".scan-list input[type=checkbox]:checked")]
    .map((x) => x.dataset.path);
  if (!picked.length) {
    skillStatus("✗ 先勾选要导入的技能", false);
    return;
  }
  const btn = document.getElementById("btn-skill-local-import");
  btn.disabled = true;
  const ok = [];
  const bad = [];
  for (const p of picked) {
    try {
      const res = await request("skills.install", { source: p, scope });
      ok.push(...res.installed);
    } catch (e) {
      bad.push(`${p.split(/[\\/]/).filter(Boolean).pop()}（${e.message}）`);
    }
  }
  btn.disabled = false;
  await renderSettings();
  boot();
  const where = scope === "project" ? "本项目" : "全局";
  if (ok.length) {
    skillStatus(`✓ 已导入 ${ok.length} 个技能到${where}：${ok.join("、")}（已启用，可直接使用）`);
    loadLocalSkills();  // 重新探测：刚装过的转为灰显，剩余候选一眼可见
  } else {
    skillStatus("✗ " + (bad[0] || "导入失败"), false);
  }
}

function deleteSkillModal(s) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除技能 <b>${escapeHtml(s.name)}</b> 吗？</p>
    <p class="dim small">会把技能文件夹从磁盘上删除（${s.source === "project" ? "本项目" : "全局"}技能目录），不可恢复。
    只是想临时停用的话，点它右边的「已启用」按钮即可。</p>`;
  showModal("删除技能", box, async () => {
    await request("skills.delete", { name: s.name });
    await renderSettings();
    boot();
    skillStatus(`✓ 已删除技能「${s.name}」`);
  }, "删除");
}

function batchDeleteSkillsModal(names) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除选中的 <b>${names.length}</b> 个技能吗？</p>
    <p class="dim small">会把这些技能文件夹从磁盘上删除，不可恢复：
    ${names.map((n) => `<span class="mono-path">${escapeHtml(n)}</span>`).join("、")}</p>`;
  showModal("批量删除技能", box, async () => {
    const bad = [];
    for (const n of names) {
      try {
        await request("skills.delete", { name: n });
      } catch (e) {
        bad.push(`${n}（${e.message}）`);
      }
    }
    await renderSettings();
    boot();
    skillStatus(bad.length ? `✗ 部分删除失败：${bad.join("、")}` : `✓ 已删除 ${names.length} 个技能`);
  }, "全部删除");
}

document.getElementById("btn-import-skill").onclick = () => importSkillModal();
document.getElementById("btn-import-skill-2").onclick = () => importSkillModal();
document.getElementById("btn-scan-skill").onclick = () => {
  // 总览卡片的按钮：先进入技能页再拉取，候选始终只在技能页内平铺
  openSkillManage();
  loadLocalSkills();
};
document.getElementById("btn-scan-skill-2").onclick = () => loadLocalSkills();
document.getElementById("btn-skill-local-import").onclick = () => importLocalSkills();
bindLocalSkillToolbar();

// ---------- MCP：导入 / 手动添加 / 删除 ----------

// 导入 MCP：粘贴配置片段（或选一个 .json 文件）
function importMcpModal(prefill = "") {
  const box = document.createElement("div");
  const canPick = nativePickerAvailable();
  box.innerHTML = `
    <p class="dim small">粘贴一段 MCP 配置即可接入。支持 Claude Desktop 的
    <b>{"mcpServers": {...}}</b> 格式，也支持单个服务（记得加 <b>"name"</b> 字段）。</p>
    <div class="form-grid">
      <label class="wide">MCP 配置（JSON）
        <textarea data-f="snippet" rows="8" placeholder='{
  "mcpServers": {
    "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
    "remote": { "url": "http://localhost:8000/mcp", "readonly": true }
  }
}'></textarea>
      </label>
      <label class="wide">或从文件导入
        <span class="model-line">
          <input data-f="path" placeholder="${canPick ? "点右边按钮选一个 .json 文件" : "粘贴 .json 配置文件的完整路径"}" value="${escapeHtml(prefill)}" autocomplete="off">
          <button class="btn-ghost browse" type="button">选择…</button>
        </span>
      </label>
      <label>存到哪个配置
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <label class="wide inline-check">
        <input data-f="overwrite" type="checkbox"> 覆盖同名服务（不勾选则跳过已存在的）
      </label>
      <div class="form-status"></div>
    </div>`;
  const pathInput = box.querySelector('input[data-f="path"]');
  const status = box.querySelector(".form-status");
  box.querySelector(".browse").onclick = async () => {
    const r = await pickPath("json");
    if (r.error) {
      status.textContent = "打不开系统选择框，请手动粘贴路径（" + r.error + "）";
      status.className = "form-status bad";
      return;
    }
    if (!r.paths || !r.paths.length) return;
    pathInput.value = r.paths[0];
    status.textContent = "已选择：" + r.paths[0];
    status.className = "form-status ok";
  };
  showModal("导入 MCP 服务", box, async () => {
    const snippet = box.querySelector('textarea[data-f="snippet"]').value.trim();
    const path = pathInput.value.trim();
    if (!snippet && !path) throw new Error("请粘贴 MCP 配置，或选择一个 .json 文件");
    const baseParams = {
      snippet,
      path,
      scope: box.querySelector('select[data-f="scope"]').value,
      overwrite: box.querySelector('input[data-f="overwrite"]').checked,
    };
    let r = await request("mcp.import", baseParams);
    // M10：含 stdio 定义时后端先回命令清单，确认后才写盘——连接即执行本机命令，
    // 必须让人看清楚要跑什么再继续
    if (r.needs_confirm) {
      const lines = (r.pending || []).map((d) => {
        const full = [d.command, ...(d.args || [])].join(" ");
        return `  ${d.name}: ${full}`;
      }).join("\n");
      if (!confirm(
        "以下 MCP 服务会在连接时执行本机命令：\n" + lines +
        "\n\n请确认它们来自可信来源（继续 = 允许在本机运行上述程序）。"
      )) return;
      r = await request("mcp.import", { ...baseParams, confirmed: true });
    }
    await renderSettings();
    boot();
    const where = r.scope === "project" ? "本项目" : "全局";
    if (r.added.length) {
      const bad = (r.mcp_warnings || []).length;
      mcpStatus(
        `✓ 已导入到${where}：${r.added.join("、")}${bad ? "；有服务没连上，详情见列表里的红色提示" : "，已连接可直接使用"}`,
        !bad
      );
    } else {
      mcpStatus(`没有新增：${r.skipped.join("、")} 已存在。勾选「覆盖同名服务」再试即可替换。`, false);
    }
  }, "导入");
}

// 手动添加：分字段填写一个服务（常用服务请直接用上面的「常用预设」一键添加）
// 请求头文本 → 对象。格式约定为每行 `名称: 值`（冒号或中文冒号都认，
// 值里的冒号原样保留——Bearer token 里出现冒号不该被截断）。
function parseHeaderLines(text) {
  const out = {};
  for (const raw of (text || "").split(/\n+/)) {
    const line = raw.trim();
    if (!line) continue;
    const i = line.search(/[:：]/);
    if (i <= 0) continue;
    const name = line.slice(0, i).trim();
    const value = line.slice(i + 1).trim();
    if (name && value) out[name] = value;
  }
  return out;
}

function addMcpModal() {
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">逐个填写一个 MCP 服务：本地命令填「启动命令 + 参数」，远程服务填「服务地址」，二选一。
    常用服务（网页抓取、Git 仓库等）已做成一键预设，在设置页上方的预设卡里添加即可。</p>
    <div class="form-grid">
      <label>服务名（英文标识）
        <input data-f="name" placeholder="fetch" autocomplete="off">
      </label>
      <label class="wide">启动命令 command（本地服务填这个）
        <input data-f="command" placeholder="uvx" autocomplete="off">
      </label>
      <label class="wide">命令参数 args（每行一个）
        <textarea data-f="args" rows="3" placeholder="mcp-server-fetch"></textarea>
      </label>
      <label class="wide">服务地址 url（远程服务填这个）
        <input data-f="url" placeholder="http://localhost:8000/mcp" autocomplete="off">
      </label>
      <label class="wide">请求头 headers（仅远程服务；每行一个 <code>名称: 值</code>）
        <textarea data-f="headers" rows="3" placeholder="Authorization: Bearer xxxxx"></textarea>
      </label>
      <label>单次调用超时（秒，留空默认 120）
        <input data-f="timeout" type="number" min="1" step="1" placeholder="120" autocomplete="off">
      </label>
      <label>存到哪个配置
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <label class="wide inline-check">
        <input data-f="readonly" type="checkbox"> 这个服务的工具自动放行（只读类服务可勾选，跳过每次确认；服务自己声明会改数据的工具仍会逐次确认）
      </label>
      <p class="dim small" style="margin:4px 0 0">远程服务的 token（如 Bearer）过期时会报「鉴权失败」：回到这里更新 headers 重新保存即可。详见 docs/mcp-远程服务鉴权.md。</p>
      <div class="form-status"></div>
    </div>`;
  showModal("手动添加 MCP 服务", box, async () => {
    const val = (f) => box.querySelector(`[data-f="${f}"]`).value.trim();
    const args = val("args").split(/\n+/).map((s) => s.trim()).filter(Boolean);
    const headers = parseHeaderLines(val("headers"));
    const timeoutNum = Number(val("timeout"));
    const r = await request("mcp.save_server", {
      name: val("name"),
      command: val("command"),
      args,
      url: val("url"),
      headers,
      timeout: Number.isFinite(timeoutNum) && timeoutNum > 0 ? timeoutNum : 0,
      scope: box.querySelector('select[data-f="scope"]').value,
      readonly: box.querySelector('input[data-f="readonly"]').checked,
    });
    await renderSettings();
    boot();
    const st = (r.mcp || []).find((m) => m.name === val("name"));
    const bad = st && !st.connected;
    mcpStatus(
      bad
        ? `已保存「${val("name")}」，但没连上：${st.error || "未知错误"}`
        : `✓ 已添加「${val("name")}」${st ? `，已连接 ${st.tools.length} 个工具` : ""}`,
      !bad
    );
  }, "添加");
}

function deleteMcpModal(name) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除 MCP 服务 <b>${escapeHtml(name)}</b> 吗？</p>
    <p class="dim small">会从本机 mcp.json 里移除它并断开连接；它的工具会立即从 Agent 可用工具里消失。</p>`;
  showModal("删除 MCP 服务", box, async () => {
    const r = await request("mcp.delete", { name });
    await renderSettings();
    boot();
    mcpStatus(`✓ 已删除「${name}」并断开连接`);
  }, "删除");
}

document.getElementById("btn-import-mcp").onclick = () => importMcpModal();
document.getElementById("btn-add-mcp").onclick = addMcpModal;

// ---------- 右侧标签页面板（对标「打开标签页」：辅助对话/审查/终端/浏览器） ----------

const RP_ICONS = {
  aux: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H10l-4.5 3.5V16H6a2 2 0 0 1-2-2z"/><path d="M8 9.5h8M8 12.5h5"/></svg>',
  review: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3.5" width="14" height="17" rx="2"/><path d="M9 8h6M9 12.5l2 2 4-4.5"/></svg>',
  browser: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.8 2.4 2.8 14.6 0 17-2.8-2.4-2.8-14.6 0-17z"/></svg>',
  files: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M9 13h6"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/></svg>',
  todo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 6h11M9.5 12h11M9.5 18h11"/><path d="m3.5 6 1.2 1.2L7 4.9M3.5 12l1.2 1.2L7 10.9M4 18h.01"/></svg>',
  ptasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8.5 4.5h9a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2h.5"/><path d="M9 2.5h4.5v4H9z"/><path d="m9 13 2 2 4.5-4.5M9 17.5h6"/></svg>',
  agenda: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5.5" width="16" height="15" rx="2"/><path d="M8 3.5v4M16 3.5v4M4 10.5h16"/></svg>',
  cron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  pipeline: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5.5" cy="6" r="2.1"/><circle cx="5.5" cy="18" r="2.1"/><circle cx="18.5" cy="12" r="2.1"/><path d="M7.4 6.9 16.6 11.1M7.4 17.1 16.6 12.9"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3.5" width="14" height="17" rx="2"/><path d="M9 3.5v17"/><path d="M12.5 8h4M12.5 12h4"/></svg>',
  ext: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3.5V8M15 3.5V8"/><path d="M6.5 8h11v2.5a5.5 5.5 0 0 1-5.5 5.5 5.5 5.5 0 0 1-5.5-5.5z"/><path d="M12 16v4.5"/></svg>',
  // 面板开关：箭头指明点击后的动作——收起时 `>`（向右展开）、展开时 `<`（向左收起），
  // 展开态把面板列填色提示"此刻是开着的"
  panelClosed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/><path d="m8.5 9.5 2.5 2.5-2.5 2.5"/></svg>',
  panelOpen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/><path d="M15.5 6h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-3z" fill="currentColor" stroke="none" opacity=".3"/><path d="m11.5 9.5-2.5 2.5 2.5 2.5"/></svg>',
};
const TAB_META = {
  aux: { title: "辅助对话" },
  review: { title: "审查" },
  browser: { title: "浏览器" },
  files: { title: "文件" },
  tasks: { title: "任务" },
  todo: { title: "任务清单" },
  ptasks: { title: "项目任务" },
  agenda: { title: "日程" },
  cron: { title: "定时任务" },
  pipeline: { title: "任务编排" },
  memory: { title: "项目记忆" },
  ext: { title: "MCP / Skills" },
};
let rightTabs = [];    // 打开的标签 id（有序）
let rightActive = null;

const rightPanel = document.getElementById("right-panel");
const rpTabs = document.getElementById("rp-tabs");
const rpResizer = document.getElementById("rp-resizer");
const btnTabs = document.getElementById("btn-tabs");

// 标签头是真正的 tablist：左右方向键在标签间移动焦点并激活（roving focus，
// 焦点不在标签上时从端头进）；＋按钮不参与 tab 语义，仍用 Tab 键到达
rpTabs.setAttribute("role", "tablist");
rpTabs.setAttribute("aria-label", "右侧面板标签");
rpTabs.addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const tabs = [...rpTabs.querySelectorAll(".rp-tab")];
  if (!tabs.length) return;
  const idx = tabs.indexOf(document.activeElement);
  const next = idx === -1
    ? (e.key === "ArrowRight" ? 0 : tabs.length - 1)
    : e.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
  e.preventDefault();
  tabs[next].focus();
  activateRightTab(tabs[next].dataset.tabId);
});
btnTabs.innerHTML = RP_ICONS.panelClosed;

/** 右面板各标签的数据加载入口（一处定义，多处复用）。

    以前 openRightTab / activateRightTab / reloadProjectPanels 各抄一份 if 链，
    而 initUiPrefs 恢复上次打开的标签时一份都没调——启动后面板里那几个页
    只是一层空壳（日程页连日期范围都是空的），得先点一下别的标签再点回来才有内容。
    统一成这张表后「打开标签」「激活标签」「恢复标签」「切项目重拉」走同一条路，
    不会再各写各的、也不会再漏掉某条路径。 */
const RIGHT_TAB_LOADERS = {
  files: (force) => loadFiles(force),
  tasks: () => loadTasks(),
  agenda: () => loadAgenda(),
  ptasks: (force) => loadProjectTasks(force),
  cron: () => loadCron(),
  pipeline: () => loadPipelines(),
  review: () => refreshReview(),
  memory: () => loadMemoryPanel(),
  ext: () => openExtPanel(),
};

/** 按需加载某个标签的数据；未知 id（如 aux 辅助对话，没有远端数据）静默跳过。 */
function loadRightTab(id, force) {
  const fn = RIGHT_TAB_LOADERS[id];
  return fn ? fn(force) : undefined;
}

function renderRightPanel() {
  const open = rightTabs.length > 0 && !rightCollapsed;
  document.body.classList.toggle("right-open", open); // 窄屏抽屉背衬的显示依据
  rightPanel.classList.toggle("hidden", !open);
  rpResizer.classList.toggle("hidden", !open);
  btnTabs.classList.toggle("on", open);
  btnTabs.innerHTML = open ? RP_ICONS.panelOpen : RP_ICONS.panelClosed;
  btnTabs.title = open ? "收起右侧面板" : "展开右侧面板";
  rpTabs.innerHTML = "";
  rightTabs.forEach((id) => {
    const b = document.createElement("button");
    b.className = "rp-tab" + (id === rightActive ? " active" : "");
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", id === rightActive ? "true" : "false");
    b.dataset.tabId = id;
    b.title = TAB_META[id].title;
    b.setAttribute("aria-label", TAB_META[id].title); // 图标档藏文字后无障碍名不丢
    b.innerHTML = RP_ICONS[id] + "<span>" + TAB_META[id].title + '</span><span class="rp-x" title="关闭标签">✕</span>';
    b.querySelector(".rp-x").onclick = (e) => { e.stopPropagation(); closeRightTab(id); };
    b.onclick = () => activateRightTab(id);
    rpTabs.appendChild(b);
  });
  // 行尾「＋」：唤出标签小菜单（不弹窗）
  const addBtn = document.createElement("button");
  addBtn.className = "rp-add";
  addBtn.title = "打开其它标签";
  addBtn.textContent = "＋";
  addBtn.onclick = (e) => { e.stopPropagation(); toggleTabsMenu(addBtn); };
  rpTabs.appendChild(addBtn);
  // 压缩降级档位：均分宽度不足时逐级降级，保证标签再多也不出横向滚动
  // （容器 overflow 已 hidden，这里是视觉降级）；判定抽成 applyRpTabDensity，
  // 容器宽度变化时由 ResizeObserver 走同一条路
  applyRpTabDensity();
  document.querySelectorAll("#rp-body .rp-page").forEach((p) => p.classList.add("hidden"));
  if (rightActive && !rightCollapsed) document.getElementById("rp-page-" + rightActive).classList.remove("hidden");
}

/** 标签栏压缩档位：按均分到每个标签的宽度逐级降级（CSS 规则见 app.css 的
    rp-tight / rp-cramped / rp-icon）。renderRightPanel 与容器宽度变化
    （ResizeObserver）都调它——拖面板宽度、缩放窗口后档位即时跟上。
    档位无变化时不动 DOM：RO 回调里每次写 class 会重新触发尺寸观察，
    形成「ResizeObserver loop completed」告警循环。 */
let _rpDensityKey = "";
function applyRpTabDensity() {
  const tabCount = rightTabs.length;
  const avail = rpTabs.clientWidth - 34; // 扣「＋」钮与内边距
  const per = tabCount > 0 ? avail / tabCount : 999;
  // 图标档（不足 56px/个：四字中文标签已只能露一两个字）与其它两档互斥：
  // 整格只剩图标，文字藏起后 tight / cramped 的规则没有意义
  const iconOnly = per < 56;
  const key = iconOnly ? "icon" : per < 64 ? "cramped" : per < 92 ? "tight" : "";
  if (key === _rpDensityKey) return; // 档位未变：不写 DOM，断掉 RO 循环
  _rpDensityKey = key;
  rpTabs.classList.toggle("rp-icon", iconOnly);
  rpTabs.classList.toggle("rp-tight", !iconOnly && per < 92);
  rpTabs.classList.toggle("rp-cramped", !iconOnly && per < 64);
}
// 容器宽度变化（拖面板 / 窗口缩放）时重算档位：此前只在重渲染时算一次，
// 拖窄后文字挤着也不降级、拖宽后仍卡在紧凑档。
// 回调里改布局要挪进 rAF（与行卡 RO 同一套做法，见 mountPipelineGraph）：
// RO 回调内同步改尺寸会刷「ResizeObserver loop completed」告警，
// 被全局错误横幅接住吓到用户
if (typeof ResizeObserver === "function") {
  let rpDensityRaf = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(rpDensityRaf);
    rpDensityRaf = requestAnimationFrame(() => applyRpTabDensity());
  }).observe(rpTabs);
}

function openRightTab(id) {
  if (!TAB_META[id]) return;
  document.body.classList.remove("sidebar-open"); // 手机互斥：开右抽屉就收侧栏
  if (!rightTabs.includes(id)) rightTabs.push(id);
  rightActive = id;
  rightCollapsed = false;
  renderRightPanel();
  const openPrefs = { right_tabs: [...rightTabs], right_active: id };
  if (!NARROW_MQ.matches) openPrefs.right_collapsed = 0;
  saveUiPrefs(openPrefs);
  loadRightTab(id);
}

function activateRightTab(id) {
  rightActive = id;
  renderRightPanel();
  saveUiPrefs({ right_active: id });
  loadRightTab(id);
}

function closeRightTab(id) {
  rightTabs = rightTabs.filter((t) => t !== id);
  if (rightActive === id) rightActive = rightTabs[rightTabs.length - 1] || null;
  if (!rightTabs.length) rightCollapsed = true; // 最后一个标签关掉 → 面板收起
  renderRightPanel();
  const closePrefs = { right_tabs: [...rightTabs], right_active: rightActive };
  if (!NARROW_MQ.matches) closePrefs.right_collapsed = rightCollapsed ? 1 : 0;
  saveUiPrefs(closePrefs);
}

// —— MCP / Skills 标签：技能与 MCP 卡片只有一份，靠 DOM 搬家在「设置页 ↔ 右面板」
// 之间共享（appendChild 移动节点时事件监听器随节点走，按 ID 绑定/查找不受位置影响）——
const extPanelPage = document.getElementById("rp-page-ext");

function openExtPanel() {
  const skillsPage = document.getElementById("settings-page-skills");
  const mcpPage = document.getElementById("settings-page-mcp");
  extPanelPage.appendChild(mcpPage); // 顺序对齐入口名：MCP 在上、技能在下
  extPanelPage.appendChild(skillsPage);
  skillsPage.classList.remove("hidden");
  mcpPage.classList.remove("hidden");
  resetSkillView(); // 每次都从总览开始，与进设置页的行为一致
  renderSettings().catch((e) => addNotice("加载设置失败: " + e.message));
}

function closeExtPanel() {
  const skillsPage = document.getElementById("settings-page-skills");
  const mcpPage = document.getElementById("settings-page-mcp");
  if (skillsPage.parentElement !== extPanelPage) return; // 卡片本来就在设置页
  const body = document.querySelector(".settings-body");
  body.insertBefore(skillsPage, document.getElementById("settings-page-subagents"));
  body.insertBefore(mcpPage, document.getElementById("settings-page-advanced"));
  if (rightTabs.includes("ext")) closeRightTab("ext"); // 内容回设置页，面板不留空壳标签
}

// 面板收起/展开由顶栏按钮直接切换；「＋」菜单选要打开的标签（小浮层，不弹窗）
let rightCollapsed = false;
let tabsMenuEl = null;
// 窄屏（手机，与 app.css 的 @media max-width:860px 同一断点）：右面板是覆盖抽屉。
// 展开/收起在那里是会话内的临时状态——启动一律收起（不照搬桌面 ui.json 的
// right_collapsed，否则手机一进来就被抽屉盖住），操作也不回写偏好（桌面端不受手机影响）。
const NARROW_MQ = window.matchMedia("(max-width: 860px)");

function persistRightCollapsed() {
  if (NARROW_MQ.matches) return;
  saveUiPrefs({ right_collapsed: rightCollapsed ? 1 : 0 });
}

function hideTabsMenu() {
  if (tabsMenuEl) { tabsMenuEl.remove(); tabsMenuEl = null; }
}

function toggleTabsMenu(anchor) {
  if (tabsMenuEl) { hideTabsMenu(); return; }
  tabsMenuEl = document.createElement("div");
  tabsMenuEl.className = "tabs-menu";
  Object.keys(TAB_META).forEach((id) => {
    const b = document.createElement("button");
    b.className = "tabs-menu-item";
    b.innerHTML = RP_ICONS[id] + "<span>" + TAB_META[id].title + "</span>" +
      (rightTabs.includes(id) ? '<span class="tm-state">已打开</span>' : "");
    b.onclick = () => { hideTabsMenu(); openRightTab(id); };
    tabsMenuEl.appendChild(b);
  });
  document.body.appendChild(tabsMenuEl);
  const r = anchor.getBoundingClientRect(); // 物理像素，除回缩放布局坐标
  const w = 184;
  tabsMenuEl.style.top = Math.min(r.bottom / uiScale + 6, window.innerHeight / uiScale - 210) + "px";
  tabsMenuEl.style.left = Math.max(8, Math.min(r.right / uiScale - w, window.innerWidth / uiScale - w - 8)) + "px";
}
document.addEventListener("click", hideTabsMenu);
window.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTabsMenu(); });

btnTabs.onclick = () => {
  if (!rightTabs.length) { openRightTab("aux"); return; } // 一个标签都没有 → 展开默认辅助对话
  rightCollapsed = !rightCollapsed;
  if (!rightCollapsed) document.body.classList.remove("sidebar-open"); // 手机互斥
  renderRightPanel();
  persistRightCollapsed();
  // 展开时重新拉一遍当前标签：面板收起期间的数据变化（日程被 Agent 改过等）
  // 在这里补齐，不至于展开就是旧的。ext 除外：它的卡片早就搬在面板里、
  // 内容没被清过，而 openExtPanel 会整个重读设置页，展开一次就白跑一趟。
  if (!rightCollapsed && rightActive && rightActive !== "ext") loadRightTab(rightActive);
};

// —— 左侧栏折叠：标题行右缘的「«」收起，折叠后由窄栏（mini rail）的「»」展开（Ctrl+B 同效）。
// 收起状态落盘 ui.json 的 left_collapsed（后端白名单已加）；手机上侧栏是覆盖抽屉，
// 由顶栏 ☰ 管理，折叠钮在窄屏 CSS 里隐藏，快捷键与恢复逻辑同样不参与——
// 强制 leftCollapsed = false，避免抽屉被折叠态吞掉。
let leftCollapsed = false;
function applyLeftCollapsed() {
  document.body.classList.toggle("left-collapsed", leftCollapsed);
  // 滑出的一侧不参与焦点与点击：inert 比 display:none 温和，过渡期间不会点到看不见的按钮
  const rail = document.getElementById("sb-rail");
  if (rail) rail.inert = !leftCollapsed;
  const sidebar = document.getElementById("sidebar");
  if (sidebar) sidebar.inert = leftCollapsed;
}
function persistLeftCollapsed() {
  if (NARROW_MQ.matches) return;
  saveUiPrefs({ left_collapsed: leftCollapsed ? 1 : 0 });
}
function toggleLeftSidebar() {
  if (NARROW_MQ.matches) return; // 手机抽屉有自己的开关（☰）
  leftCollapsed = !leftCollapsed;
  applyLeftCollapsed();
  persistLeftCollapsed();
}
document.getElementById("btn-left-collapse").onclick = toggleLeftSidebar;
// 窄栏：展开钮与折叠钮同一对；新建 / 搜索照侧栏同名功能走；会话快捷在
// renderRailSessions 里各自接管。停在设置页时先回对话再看结果。
document.getElementById("rail-expand").onclick = toggleLeftSidebar;
document.getElementById("rail-new").onclick = () => {
  if (settingsOpen) backToChat();
  startNewTab();
  refreshSessions();
};
document.getElementById("rail-search").onclick = () => {
  if (settingsOpen) backToChat();
  if (leftCollapsed) toggleLeftSidebar();
  requestAnimationFrame(() => {
    const el = document.getElementById("session-search");
    if (el) { el.focus(); el.select(); }
  });
};
// 窄栏功能导航六件套：与侧栏导航项同一套动作（openRightTab 打开右侧面板标签）；
// 停在设置页时先回对话，别让面板开在看不见的地方。
document.querySelectorAll("#sb-rail [data-rail-act]").forEach((btn) => {
  btn.onclick = () => {
    if (settingsOpen) backToChat();
    openRightTab(btn.dataset.railAct);
  };
});

// —— 终端：底部停靠面板，每个标签一个常驻 PowerShell（ConPTY 真会话）——
// 前端用 xterm.js（vendor/）渲染：提示符、ANSI 颜色、Ctrl+C、交互程序都真实可用；
// 标签序号取「最小未占用」，关闭中间的标签后新建的补上空号，不再一直涨。
const termDock = document.getElementById("term-dock");
const tdTabs = document.getElementById("td-tabs");
const termHost = document.getElementById("term-host");
const btnTerm = document.getElementById("btn-term");

let termTabs = [];     // [{ id, num, title, term, fit, screen }]
let activeTermId = "";

function termTabById(id) { return termTabs.find((t) => t.id === id) || null; }
function termActive() { return termTabById(activeTermId) || termTabs[0] || null; }

function nextTermNumber() {
  const used = new Set(termTabs.map((t) => t.num));
  let n = 1;
  while (used.has(n)) n += 1;
  return n;
}

function newTermTab(activate) {
  const num = nextTermNumber();
  const t = {
    id: "term-" + Date.now().toString(36) + "-" + num,
    num,
    title: "终端 " + num,
    term: null, fit: null, screen: null,
  };
  termTabs.push(t);
  if (activate !== false) switchTermTab(t.id);
  renderTermTabbar();
  return t;
}

// xterm 配色跟六套主题走：底/字取 CSS 变量，ANSI 十六色按明暗各配一套保证可读
function xtermTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n, fb) => (cs.getPropertyValue(n) || "").trim() || fb;
  const info = THEMES[resolvedThemeId()] || {};
  const dark = !!info.dark;
  const base = {
    background: v("--bg", dark ? "#1D1A16" : "#E8DFC7"),
    foreground: v("--text", dark ? "#F4ECD8" : "#1D1A16"),
    cursor: v("--text", dark ? "#F4ECD8" : "#1D1A16"),
    cursorAccent: v("--bg", dark ? "#1D1A16" : "#E8DFC7"),
    selectionBackground: dark ? "#453d31" : "#cdbf9d",
  };
  const ansi = dark ? {
    black: "#3a352c", red: v("--reg", "#e07a7a"), green: "#7ec98a", yellow: "#d9b96a",
    blue: "#7aa7e8", magenta: "#c58ad9", cyan: "#6cc4c4", white: "#d8d2c2",
    brightBlack: "#8a8172", brightWhite: "#f4ecd8",
  } : {
    black: "#1d1a16", red: v("--reg", "#8f1d1d"), green: "#1a6b2a", yellow: "#8a6410",
    blue: v("--blue", "#1257c4"), magenta: "#7a2d7a", cyan: "#0f6a6a", white: "#6b6255",
    brightBlack: "#5a5245", brightWhite: "#faf6ec",
  };
  return Object.assign(base, ansi);
}

async function ensureTermScreen(t) {
  if (t.term) return t.term;
  // xterm 按需加载（首次开终端才拉双文件库），失败按提示兜底
  try {
    await ensureXterm();
  } catch (e) {
    addNotice("xterm 组件没有加载成功，终端不可用（vendor/xterm.js 缺失？）");
    return null;
  }
  const screen = document.createElement("div");
  screen.className = "term-screen";
  termHost.appendChild(screen);
  const term = new window.Terminal({
    fontFamily: '"Cascadia Mono", Consolas, "Courier New", monospace',
    fontSize: 13,
    cursorBlink: true,
    scrollback: 5000,
    theme: xtermTheme(),
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(screen);
  t.term = term;
  t.fit = fit;
  t.screen = screen;
  // 键盘直通：xterm 里的每个按键（含 Enter / Ctrl+C / ↑↓）原样进 PowerShell
  term.onData((data) => {
    request("term.input", { term_id: t.id, data }).catch(() => {});
  });
  term.onResize(({ cols, rows }) => {
    request("term.resize", { term_id: t.id, rows, cols }).catch(() => {});
  });
  termFit(t);
  // 后端按 term_id 起常驻 shell；输出经 term_data 事件路由回来写入对应 xterm
  request("term.spawn", { term_id: t.id, rows: term.rows, cols: term.cols }).catch((e) => {
    term.write("终端启动失败：" + (e && e.message ? e.message : e) + "\r\n");
  });
  return term;
}

function termFit(t) {
  if (!t || !t.fit || !t.screen || t.screen.classList.contains("hidden")) return;
  try {
    const c0 = t.term.cols, r0 = t.term.rows;
    t.fit.fit();
    if (t.term.cols !== c0 || t.term.rows !== r0) {
      request("term.resize", { term_id: t.id, rows: t.term.rows, cols: t.term.cols }).catch(() => {});
    }
  } catch (e) { /* 容器不可见等：跳过 */ }
}
function termFitAll() { termTabs.forEach(termFit); }
window.addEventListener("resize", termFitAll);

function switchTermTab(id) {
  const t = termTabById(id);
  if (!t) return;
  activeTermId = id;
  for (const tab of termTabs) {
    if (tab.screen) tab.screen.classList.toggle("hidden", tab.id !== id);
  }
  renderTermTabbar();
  ensureTermScreen(t);
  termFit(t);
  if (t.term && !termDock.classList.contains("hidden") && document.hasFocus()) t.term.focus();
}

function closeTermTab(id) {
  const idx = termTabs.findIndex((t) => t.id === id);
  if (idx < 0) return;
  const [gone] = termTabs.splice(idx, 1);
  request("term.close", { term_id: gone.id }).catch(() => {}); // 关标签一并结束它的 shell
  try { if (gone.term) gone.term.dispose(); } catch (e) { /* 已销毁 */ }
  if (gone.screen) gone.screen.remove();
  if (!termTabs.length) { newTermTab(true); return; } // 面板至少留一个标签
  if (activeTermId === id) switchTermTab(termTabs[Math.max(0, idx - 1)].id);
  else renderTermTabbar();
}

function resetTermTabs() {
  // 切项目：shell 是在项目目录里起的，旧会话全部关闭作废
  for (const t of termTabs) {
    request("term.close", { term_id: t.id }).catch(() => {});
    try { if (t.term) t.term.dispose(); } catch (e) { /* 已销毁 */ }
    if (t.screen) t.screen.remove();
  }
  termTabs = [];
  activeTermId = "";
  // 只在终端面板正开着时才立刻重建 shell：每次 spawn 都会创建一个 ConPTY
  // 宿主进程（conhost），其窗口在创建瞬间可能闪一下——面板收着时白闪一次。
  // 收起时留空，等真正展开面板时 toggleTermDock 会自动建标签。
  if (!termDock.classList.contains("hidden")) newTermTab(true);
}

function renderTermTabbar() {
  tdTabs.innerHTML = "";
  termTabs.forEach((t) => {
    const tab = document.createElement("span");
    tab.className = "td-tab" + (t.id === activeTermId ? " active" : "");
    tab.title = t.title + " · PowerShell（点击切换）";
    const label = document.createElement("span");
    label.textContent = t.title;
    tab.appendChild(label);
    const x = document.createElement("button");
    x.className = "td-x";
    x.textContent = "✕";
    x.title = "关闭这个终端（会话一并结束）";
    x.onclick = (e) => { e.stopPropagation(); closeTermTab(t.id); };
    tab.appendChild(x);
    tab.onclick = () => switchTermTab(t.id);
    tdTabs.appendChild(tab);
  });
}

function toggleTermDock(force) {
  const show = force !== undefined ? force : termDock.classList.contains("hidden");
  termDock.classList.toggle("hidden", !show);
  btnTerm.classList.toggle("on", show);
  if (show) {
    if (!termTabs.length) newTermTab(true);
    const t = termActive();
    if (t) {
      ensureTermScreen(t);
      termFit(t);
      if (t.term && document.hasFocus()) t.term.focus();
    }
  }
}

btnTerm.onclick = () => toggleTermDock();
document.getElementById("td-add").onclick = () => { newTermTab(true); };
document.getElementById("td-close").onclick = () => toggleTermDock(false);
document.getElementById("term-clear").onclick = () => {
  const t = termActive();
  if (t && t.term) { t.term.clear(); t.term.focus(); }
};

// 把当前终端最近输出带入主输入框（引用块），供 Agent 分析
document.getElementById("term-to-agent").onclick = () => {
  const t = termActive();
  if (!t || !t.term) { addNotice("终端还没有内容，先展开面板运行一条命令"); return; }
  const buf = t.term.buffer.active;
  const lines = [];
  for (let i = Math.max(0, buf.length - 40); i < buf.length; i++) {
    const line = buf.getLine(i);
    lines.push(line ? line.translateToString(true) : "");
  }
  const text = lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  if (!text) { addNotice("终端还没有输出，先运行一条命令"); return; }
  const input = document.getElementById("input");
  input.value = (input.value ? input.value + "\n" : "") +
    "终端最近输出：\n```\n" + text + "\n```\n";
  autoGrowInput(); // 程序化赋值不触发 input 事件，必须手动长高，否则多行内容溢出错乱
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  addNotice("已把终端输出带入输入框，补充你的问题后发送");
};

// —— 辅助对话：独立小问答（不进主会话、不落库） ——
const auxLog = document.getElementById("aux-log");
const auxInput = document.getElementById("aux-input");
const AUX_EMPTY = '<div class="rp-empty">在这里问点小问题，不会进入主对话，也不会写进会话记录。</div>';
let auxStreamingEl = null;
let auxStreamingText = "";
let auxThinkingText = "";
let auxBusy = false;
// 流式渲染节流（同主聊天 appendStream 的 80ms 方案）：思考/正文两阶段共用
// 一个定时器，按最新阶段决定渲染形态；auxSend 的最终全量渲染兜底
let auxRenderTimer = null;
let auxThinkingPhase = false;
function auxScheduleRender() {
  if (auxRenderTimer) return;
  auxRenderTimer = setTimeout(() => {
    auxRenderTimer = null;
    if (!auxStreamingEl) return;
    auxStreamingEl.innerHTML = auxThinkingPhase
      ? '<div class="think-inline">💭 思考中…<br>' +
        escapeHtml(auxThinkingText.slice(-400)) +
        "</div><p>▍</p>"
      : renderMarkdown(auxStreamingText);
    auxLog.scrollTop = auxLog.scrollHeight;
  }, 80);
}

// 思考模型的推理增量：面板内灰显（收尾后被正文渲染覆盖），不进历史
function auxThinkingDelta(txt) {
  auxThinkingText += txt || "";
  auxThinkingPhase = !auxStreamingText;
  auxScheduleRender();
}

function auxAddUser(text) {
  const empty = auxLog.querySelector(".rp-empty");
  if (empty) empty.remove();
  const d = document.createElement("div");
  d.className = "aux-msg user";
  d.textContent = text;
  auxLog.appendChild(d);
  auxLog.scrollTop = auxLog.scrollHeight;
}

async function auxSend() {
  const text = auxInput.value.trim();
  if (!text || auxBusy) return;
  auxBusy = true;
  auxInput.value = "";
  auxAddUser(text);
  const stream = document.createElement("div");
  stream.className = "aux-msg ai";
  stream.innerHTML = "<p>▍</p>";
  auxLog.appendChild(stream);
  auxLog.scrollTop = auxLog.scrollHeight;
  auxStreamingEl = stream;
  auxStreamingText = "";
  auxThinkingText = "";
  try {
    const r = await request("chat.aux", { text });
    stream.innerHTML = renderMarkdown(r.text || auxStreamingText);
    renderMermaidIn(stream);
    highlightCodeIn(stream);
  } catch (e) {
    stream.innerHTML = '<p class="dim">✗ ' + escapeHtml(e.message) + "</p>";
  } finally {
    if (auxRenderTimer) { clearTimeout(auxRenderTimer); auxRenderTimer = null; }
    auxStreamingEl = null;
    auxStreamingText = "";
    auxThinkingText = "";
    auxBusy = false;
    auxLog.scrollTop = auxLog.scrollHeight;
  }
}

function auxClear() {
  request("aux.clear").catch(() => {});
  auxLog.innerHTML = AUX_EMPTY;
  auxStreamingEl = null;
  auxStreamingText = "";
  auxThinkingText = "";
}
document.getElementById("aux-send").onclick = auxSend;
document.getElementById("aux-clear").onclick = auxClear;
auxInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); auxSend(); }
});

// —— 审查：本会话的文件改动轮次 + 改前→现在 diff ——
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderDiffText(diff) {
  return diff.split("\n").map((l) => {
    const cls = l.startsWith("+") && !l.startsWith("+++") ? "add"
      : l.startsWith("-") && !l.startsWith("---") ? "del"
      : l.startsWith("@@") ? "meta" : "";
    return `<span class="${cls}">${escapeHtml(l) || " "}</span>`;
  }).join("\n");
}

async function refreshReview() {
  const list = document.getElementById("review-list");
  document.getElementById("review-diff").classList.add("hidden");
  list.classList.remove("hidden");
  let cps = [];
  try {
    cps = (await request("checkpoint.list")).checkpoints || [];
  } catch (e) {
    list.innerHTML = `<div class="rp-empty">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!cps.length) {
    list.innerHTML = '<div class="rp-empty">本轮会话还没有文件改动。Agent 用写入/编辑工具改文件后，这里会按轮列出改动。</div>';
    return;
  }
  list.innerHTML = "";
  [...cps].reverse().forEach((cp) => {
    const item = document.createElement("div");
    item.className = "review-item";
    const chips = (cp.paths || []).map((p) =>
      `<button class="review-file" title="查看差异" data-cp="${cp.id}">${escapeHtml(fileName(p))}</button>`
    ).join("");
    item.innerHTML = `<div class="rv-head"><b>${fmtClock(cp.ts)}</b><span>改动 ${cp.paths.length} 个文件 · ${cp.id}</span>` +
      `<button class="rv-restore" title="把这些文件恢复到这一轮改动前的状态（覆盖之后的修改）">↩ 恢复</button></div>` +
      `<div class="review-files">${chips}</div>`;
    item.querySelectorAll(".review-file").forEach((btn) => {
      btn.onclick = () => showReviewDiff(btn.dataset.cp);
    });
    // 历史轮次的恢复入口：两击确认，防止误触覆盖后续改动
    const rb = item.querySelector(".rv-restore");
    rb.onclick = async () => {
      if (rb.dataset.arm !== "1") {
        rb.dataset.arm = "1";
        rb.textContent = "确认恢复？";
        setTimeout(() => {
          rb.dataset.arm = "";
          rb.textContent = "↩ 恢复";
        }, 2500);
        return;
      }
      rb.disabled = true;
      rb.textContent = "恢复中…";
      try {
        const r = await restoreCheckpoint(cp.id);
        addNotice(`已把 ${r.files.length} 个文件恢复到 ${cp.id} 改动前的状态`);
        refreshReview();
        // 磁盘被回滚：文件树缓存失效（开着文件标签就直接刷新）
        filesLoaded = false;
        if (rightActive === "files" && !rightPanel.classList.contains("hidden")) loadFiles(true);
      } catch (e) {
        rb.disabled = false;
        rb.textContent = "↩ 恢复";
        addNotice("恢复失败: " + e.message);
      }
    };
    list.appendChild(item);
  });
}

function fileName(p) {
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return i >= 0 ? p.slice(i + 1) : p;
}

async function showReviewDiff(cpId) {
  const wrap = document.getElementById("review-diff");
  const body = document.getElementById("review-diff-body");
  let r;
  try {
    r = await request("checkpoint.diff", { id: cpId });
  } catch (e) {
    addNotice("审查加载失败：" + e.message);
    return;
  }
  document.getElementById("review-diff-title").textContent =
    `${cpId} · ${fmtClock(r.ts)} · 改前 → 现在`;
  body.innerHTML = "";
  r.files.forEach((f) => {
    const head = document.createElement("div");
    const stText = { created: "新建", modified: "修改", deleted: "删除", gone: "新建后已删", unchanged: "无变化", binary: "二进制" }[f.status] || f.status;
    head.innerHTML = `<div class="rv-head"><span class="review-file" style="cursor:default">${escapeHtml(f.path)}</span><span>${stText}</span></div>`;
    body.appendChild(head);
    if (f.diff) {
      const pre = document.createElement("pre");
      pre.className = "tool-diff";
      pre.innerHTML = renderDiffText(f.diff);
      body.appendChild(pre);
    }
  });
  document.getElementById("review-list").classList.add("hidden");
  wrap.classList.remove("hidden");
}
document.getElementById("review-refresh").onclick = refreshReview;
document.getElementById("review-diff-close").onclick = refreshReview;

// —— 文件树：工作区只读浏览 + 文件预览 ——
let filesLoaded = false;

// 文件树的「被 Agent 改动」刷新：尾部防抖。
//
// Agent 一轮里连续写多个文件时，每次都直接 loadFiles(true) 会变成
// 「一次 fs.files 全量 os.walk + 一次整树重渲染」× N——写 10 个文件就是 10 轮，
// 全部挤在流式事件之间。改成最后一次改动后统一刷一次：中间状态本就没人看，
// 最终树才是用户要的。手动「刷新」按钮仍走立即路径（用户的点击要有即时反馈）。
const FILES_REFRESH_DEBOUNCE_MS = 400;
let filesRefreshTimer = 0;

function scheduleFilesRefresh() {
  filesLoaded = false;
  if (filesRefreshTimer) clearTimeout(filesRefreshTimer);
  filesRefreshTimer = setTimeout(() => {
    filesRefreshTimer = 0;
    if (rightActive === "files" && !rightPanel.classList.contains("hidden")) loadFiles(true);
  }, FILES_REFRESH_DEBOUNCE_MS);
}

// 当前编辑器状态：{ path, baseMtime, origText, editable, sizeText, isNew }
// baseMtime 是打开时的磁盘 mtime（ns），保存时回传做冲突检测：Agent 或外部
// 程序若在编辑期间改过文件，后端拒绝落盘并返回 conflict，由用户决定覆盖与否。
let fileEdit = null;

async function loadFiles(force) {
  if (filesLoaded && !force) return;
  const tree = document.getElementById("files-tree");
  // 树里已有真实内容（目录/文件行）时不先清成「加载中…」：切项目/手动刷新都改为
  // 拉到新列表后一次性替换，旧的目录树全程留屏——先清后填会在面板里闪一段空白。
  // 首次打开（没有内容可留）才显示占位。
  if (!tree.querySelector(".ft-dir, .ft-file")) {
    tree.innerHTML = '<div class="dim small" style="padding:8px">加载中…</div>';
  }
  try {
    const r = await request("fs.files");
    const root = buildFileTree(r.files || []);
    tree.innerHTML = renderFileNode(root, "", 0) ||
      '<div class="dim small" style="padding:8px">工作区还没有文件，点「＋ 新建」建一个</div>';
    filesLoaded = true;
  } catch (e) {
    // 已有内容时加载失败保留旧树（右面板压暗态会提示正在加载），只在空树时报错
    if (!tree.querySelector(".ft-dir, .ft-file")) {
      tree.innerHTML = `<div class="dim small" style="padding:8px">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }
}

function buildFileTree(files) {
  const root = { dirs: {}, files: [] };
  for (const f of files) {
    const parts = f.split("/");
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      if (i === parts.length - 1) { node.files.push(parts[i]); break; }
      const d = parts[i];
      node.dirs[d] = node.dirs[d] || { dirs: {}, files: [] };
      node = node.dirs[d];
    }
  }
  return root;
}

function renderFileNode(node, path, depth) {
  const out = [];
  for (const d of Object.keys(node.dirs).sort()) {
    out.push(`<details class="ft-dir" ${depth < 1 ? "open" : ""}><summary>${escapeHtml(d)}/</summary>` +
      renderFileNode(node.dirs[d], path + d + "/", depth + 1) + "</details>");
  }
  for (const f of node.files.sort()) {
    out.push(`<div class="ft-file" data-path="${escapeHtml(path + f)}">${escapeHtml(f)}</div>`);
  }
  return out.join("");
}

function fileEditorBody() {
  return document.getElementById("files-editor");
}

function isFileDirty() {
  return !!(fileEdit && fileEdit.editable &&
    fileEditorBody().value !== fileEdit.origText);
}

function fileSizeText(bytes) {
  return bytes < 1024 ? ` · ${bytes} B` : ` · ${(bytes / 1024).toFixed(1)}KB`;
}

// 标题行：脏标记 ● / 只读说明 / 编码与新行符 / 保存按钮显隐与只读态，每次输入后都会重画
function renderFileHead() {
  if (!fileEdit) return;
  const dirty = isFileDirty();
  const meta = fileEdit.encodingText && fileEdit.encodingText !== "UTF-8"
    ? " · " + fileEdit.encodingText + (fileEdit.crlf ? " · CRLF" : "")
    : (fileEdit.crlf ? " · CRLF" : "");
  document.getElementById("files-preview-title").textContent =
    (dirty ? "● " : "") + fileEdit.path + fileEdit.sizeText +
    (fileEdit.isNew ? " · 新文件" : "") + meta +
    (fileEdit.editable ? "" : " · 只读（" + (fileEdit.readonlyWhy || "改了存不回去") + "）");
  const saveBtn = document.getElementById("files-save");
  saveBtn.classList.toggle("hidden", !fileEdit.editable);
  saveBtn.textContent = dirty ? "● 保存" : "保存";
  fileEditorBody().readOnly = !fileEdit.editable;
}

// 有未保存修改时的放弃确认：确定 → true（走 onOk），取消 → false
function confirmDiscard() {
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.innerHTML = "<p>有未保存的修改，关闭后会丢失。</p>";
    const cancelBtn = document.getElementById("modal-cancel");
    cancelBtn.onclick = () => { hideModal(); resolve(false); };
    showModal("未保存的修改", box, () => resolve(true), "放弃修改并关闭");
  });
}

async function openFile(path, opts = {}) {
  // 切换文件前脏检查（同一文件重复点不重复确认）
  if (isFileDirty() && fileEdit && fileEdit.path !== path) {
    if (!(await confirmDiscard())) return;
  }
  const wrap = document.getElementById("files-preview");
  try {
    const r = await request("fs.read", { path });
    // textarea 会把 \r\n 规范化成 \n（HTML 标准），基准文本必须做同样规范化，
    // 否则 Windows 的 CRLF 文件一打开就误报「已修改」（保存时后端会按原行尾符写回）
    const norm = r.text.replace(/\r\n/g, "\n");
    fileEdit = {
      path: r.path,
      baseMtime: r.mtime,
      origText: norm,
      editable: !!r.editable,
      sizeText: fileSizeText(r.size),
      isNew: !!opts.isNew,
      encodingText: r.encoding_text || "UTF-8",
      crlf: r.newline === "\r\n",
      // 提取文本 / 截断 / 编码不明的三类只读原因分别说清
      readonlyWhy: r.doc ? "提取文本" : (r.truncated ? "文件过大已截断" : "编码无法识别"),
    };
    const ed = fileEditorBody();
    ed.value = norm;
    wrap.classList.remove("hidden");
    wrap.classList.toggle("expanded", fileEdit.editable);
    renderFileHead();
    document.getElementById("files-preview-open").dataset.path = r.path;
    if (fileEdit.editable) ed.focus();
  } catch (e) {
    addNotice("打开失败: " + e.message);
  }
}

function closeFileEditor() {
  document.getElementById("files-preview").classList.add("hidden");
  document.getElementById("files-preview").classList.remove("expanded");
  fileEdit = null;
}

async function saveFile(force = false) {
  if (!fileEdit || !fileEdit.editable) return;
  const ed = fileEditorBody();
  try {
    const r = await request("fs.write", {
      path: fileEdit.path,
      text: ed.value,
      base_mtime: fileEdit.baseMtime,
      force,
    });
    if (r.conflict) {
      // 后端拒绝落盘：磁盘上的文件比打开时新（Agent / 外部程序改过）
      const box = document.createElement("div");
      box.innerHTML =
        "<p>这份文件在打开之后被其他程序改过（可能是 Agent 或外部编辑器）。</p>" +
        "<p class='dim small'>「覆盖保存」用你看到的内容覆盖对方的改动；「取消」后点工具栏「刷新」可重新加载最新内容。</p>";
      showModal("文件已被外部修改", box, () => saveFile(true), "覆盖保存");
      return;
    }
    const wasNew = fileEdit.isNew;
    fileEdit.baseMtime = r.mtime;
    fileEdit.origText = ed.value;
    fileEdit.isNew = false;
    renderFileHead();
    fileIndex = null; // @ 文件提及的 30s 缓存失效，新建的文件立刻能被 @ 到
    if (wasNew) { filesLoaded = false; loadFiles(true); }
    addNotice("已保存 " + fileEdit.path);
  } catch (e) {
    addNotice("保存失败: " + e.message);
  }
}

document.getElementById("files-refresh").onclick = () => { filesLoaded = false; loadFiles(true); };
document.getElementById("files-preview-open").onclick = async () => {
  const p = document.getElementById("files-preview-open").dataset.path || "";
  if (!p) return;
  try {
    const res = await request("fs.open", { path: p });
    addNotice(res.action === "opened"
      ? `已用系统默认程序打开 ${p}`
      : `已在文件管理器里定位 ${p}（脚本/可执行文件不直接运行）`);
  } catch (e) {
    addNotice("打开失败: " + e.message);
  }
};
document.getElementById("files-preview-close").onclick = async () => {
  if (isFileDirty() && !(await confirmDiscard())) return;
  closeFileEditor();
};
document.getElementById("files-save").onclick = () => saveFile();
document.getElementById("files-editor").addEventListener("input", renderFileHead);
document.getElementById("files-editor").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveFile();
    return;
  }
  // Tab 插入两个空格（保持文本缩进习惯；只读时交给浏览器默认焦点移动）
  if (e.key === "Tab" && !e.target.readOnly) {
    e.preventDefault();
    e.target.setRangeText("  ", e.target.selectionStart, e.target.selectionEnd, "end");
    e.target.dispatchEvent(new Event("input", { bubbles: true }));
  }
});
document.getElementById("files-new").onclick = () => {
  const box = document.createElement("div");
  box.innerHTML =
    '<p class="dim small">在工作目录里新建文本文件（可带子目录，如 docs/笔记.md）：</p>' +
    '<input id="files-new-path" class="modal-input" style="width:100%" placeholder="例如：notes/计划.md">';
  showModal("新建文件", box, async () => {
    const raw = document.getElementById("files-new-path").value.trim().replace(/\\/g, "/");
    if (!raw) throw new Error("文件名不能为空");
    // base_mtime 传 0：已存在同名文件时必然冲突，把「覆盖已有文件」挡在确认框外
    const r = await request("fs.write", { path: raw, text: "", base_mtime: 0 });
    if (r.conflict) throw new Error("同名文件已存在，换个名字或直接在树里点开它");
    addNotice("已创建 " + raw);
    filesLoaded = false;
    loadFiles(true);
    fileIndex = null;
    await openFile(raw, { isNew: true });
  }, "创建并打开");
  setTimeout(() => document.getElementById("files-new-path")?.focus(), 0);
};
document.getElementById("files-tree").addEventListener("click", (e) => {
  const el = e.target.closest(".ft-file");
  if (!el) return;
  const p = el.dataset.path || "";
  // 网页/图片文件直接在浏览器标签里打开真实渲染效果
  if (/\.(html?|png|jpe?g|webp|svg|gif)$/i.test(p)) { openHtmlPreview(p); return; }
  openFile(p);
});

// —— 任务：子代理任务簿（running 优先，支持全部取消） ——
let tasksTimer = null;

async function loadTasks() {
  const ul = document.getElementById("tasks-list");
  try {
    const tasks = (await request("tasks.list")).tasks || [];
    ul.innerHTML = "";
    if (!tasks.length) {
      ul.innerHTML = '<div class="rp-empty">还没有子代理任务。Agent 拆解出的并行任务会出现在这里。</div>';
      return;
    }
    tasks.forEach((t) => {
      const item = document.createElement("div");
      item.className = "task-item st-" + t.status;
      const stMap = {
        running: "▶ 运行中",
        queued: "⏳ 排队中",
        done: "✓ 完成",
        cancelled: "◦ 已取消",
        error: "✗ " + (t.error || "失败"),
      };
      const st = stMap[t.status] || t.status;
      const tk = (t.tokens_in || 0) + (t.tokens_out || 0);
      const dur = t.duration_s > 0
        ? `<span class="task-tokens">⏱${fmtTaskDur(t.duration_s)}</span>` : "";
      const live = t.status === "running" || t.status === "queued";
      item.innerHTML =
        `<div class="task-head"><b>${escapeHtml(t.agent_type)}</b>` +
        (tk ? `<span class="task-tokens">≈${fmtTokens(tk)}</span>` : "") + dur +
        `<span class="task-status">${escapeHtml(st)}</span>` +
        (live ? `<button class="cron-op" data-op="cancel" title="取消此任务">✕</button>` : "") +
        `<button class="cron-op" data-op="pipeline" title="纳入任务编排（挂接为流水线节点）">⛓</button></div>` +
        `<div class="task-prompt">${escapeHtml(t.prompt)}</div>` +
        (t.result ? `<div class="task-result">${escapeHtml(t.result.slice(0, 300))}</div>` : "");
      item.querySelector('[data-op="pipeline"]').onclick = async (e) => {
        e.stopPropagation();
        await pipelineImportModal({ source: "task", task_id: t.id });
      };
      const cbtn = item.querySelector('[data-op="cancel"]');
      if (cbtn) cbtn.onclick = async (e) => {
        e.stopPropagation();
        try { await request("tasks.cancel", { task_id: t.id }); } catch (err) {}
        loadTasks();
      };
      item.onclick = () => openTaskDetail(t.id);
      ul.appendChild(item);
    });
  } catch (e) {
    ul.innerHTML = `<div class="rp-empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

// 任务详情弹窗：完整 prompt / 结果报告 / 错误（列表里是截断版）
async function openTaskDetail(taskId) {
  let r;
  try {
    r = await request("tasks.get", { task_id: taskId });
  } catch (e) {
    addNotice("任务详情加载失败：" + e.message);
    return;
  }
  const t = r.task || {};
  const box = document.createElement("div");
  box.className = "task-detail";
  const tk = (t.tokens_in || 0) + (t.tokens_out || 0);
  const meta = document.createElement("div");
  meta.className = "task-detail-meta";
  meta.textContent =
    `${t.agent_type || ""} · ${t.status}` +
    (t.provider ? ` · ${t.provider}` : "") +
    (tk ? ` · ≈${fmtTokens(tk)} tokens` : "") +
    (t.duration_s > 0 ? ` · 耗时 ${fmtTaskDur(t.duration_s)}` : "");
  box.appendChild(meta);
  const mk = (label, text, isErr) => {
    const h = document.createElement("div");
    h.className = "task-detail-label";
    h.textContent = label;
    const body = document.createElement("div");
    body.className = "task-detail-body" + (isErr ? " err" : "");
    body.textContent = text;
    box.appendChild(h);
    box.appendChild(body);
  };
  mk("任务", t.prompt || "（空）");
  if (t.result) mk("结果报告", t.result);
  if (t.error) mk("错误", t.error, true);
  if (t.report_path) mk("报告文件", t.report_path);
  showModal("子代理任务详情", box, async () => {}, "关闭");
}

// 任务耗时展示：<60s 显示秒，否则 分+秒
function fmtTaskDur(s) {
  if (s < 60) return Math.round(s) + "s";
  return Math.floor(s / 60) + "m" + Math.round(s % 60) + "s";
}

// —— 子代理直播：subagent_event 渲染进运行中的 spawn_agent 卡片 ——
function onSubagentSpawned(data) {
  // 卡片与任务的绑定只认 subagent_spawned（后端在派生瞬间发、必早于该任务
  // 的任何过程事件）——不靠「第一个到达的事件」，否则并发中其他后台任务
  // 的事件会在同步卡片绑定前抢先串台
  if (!curSpawnCard || curSpawnCard._spawnTaskId) return;
  curSpawnCard._spawnTaskId = data.task_id;
  if (data.queued) renderSubQueued(curSpawnCard);
}

function renderSubQueued(card) {
  const { live } = subLiveAreas(card);
  const line = document.createElement("div");
  line.className = "sub-line";
  line.textContent = "⏳ 并发已满，已进入排队（空位出来自动开跑）";
  live.appendChild(line);
}

function onSubagentEvent(data) {
  if (!curSpawnCard || !curSpawnCard._spawnTaskId) return;
  if (curSpawnCard._spawnTaskId === data.task_id) renderSubEvent(curSpawnCard, data.event || {});
}

function subLiveAreas(card) {
  let live = card.querySelector(".sub-live");
  if (!live) {
    const body = card.querySelector(".t-body");
    live = document.createElement("div");
    live.className = "sub-live";
    const report = document.createElement("div");
    report.className = "sub-report";
    body.appendChild(live);
    body.appendChild(report);
  }
  return { live, report: card.querySelector(".sub-report") };
}

function renderSubEvent(card, ev) {
  const { live, report } = subLiveAreas(card);
  if (ev.kind === "tool_call_started") {
    const line = document.createElement("div");
    line.className = "sub-line";
    line.dataset.cid = ev.tool_call_id || "";
    line.textContent = "▶ " + (ev.name || "") + " " + oneLine(ev.input || {});
    live.appendChild(line);
    while (live.children.length > 100) live.removeChild(live.firstChild);
  } else if (ev.kind === "tool_call_finished") {
    let line = ev.tool_call_id ? live.querySelector(`[data-cid="${CSS.escape(ev.tool_call_id)}"]`) : null;
    if (!line) {
      line = document.createElement("div");
      line.className = "sub-line";
      live.appendChild(line);
    }
    line.classList.add(ev.is_error ? "err" : "ok");
    line.textContent = (ev.is_error ? "✗ " : "✓ ") + (ev.name || "") + ` (${ev.duration_ms || 0}ms)`;
  } else if (ev.kind === "text_delta") {
    // 增量追加：textContent += 每条 delta 都要整体重写字符串节点，长报告 O(n²)
    report.insertAdjacentText("beforeend", ev.text || "");
    if (!card._subScrollTimer) {
      card._subScrollTimer = setTimeout(() => {
        card._subScrollTimer = null;
        report.scrollTop = report.scrollHeight;
      }, 80);
    }
  } else if (ev.kind === "assistant_message") {
    const text = messageText(ev.message);
    if (text) report.textContent = text; // 用最终完整文本覆盖增量拼接
  }
}

// —— 后台子代理任务终态通知 ——
function onTaskFinished(data) {
  const label = { done: "完成", cancelled: "已取消", error: "失败" }[data.status] || data.status;
  addNotice(`后台子代理任务${label}（${data.agent_type || ""}）：${data.prompt || ""}`);
  offerTaskSummary(data);
  if (rightActive === "tasks" && !rightPanel.classList.contains("hidden")) loadTasks();
}

// 任务做完且属于当前打开的会话 → 给「现在汇总」快捷按钮，点了以固定话术
// 发一轮让 Agent 用 check_task 取报告。空闲会话的兜底由引擎负责：下一轮
// 开始时自动注入「任务已完成」提示（pop_turn_note），不依赖用户记得来取。
function offerTaskSummary(data) {
  if (data.status !== "done" && data.status !== "error") return;
  const tab = activeTab;
  if (!tab || !tab.sid || tab.sid !== data.session_id || tab.running) return;
  const d = document.createElement("div");
  d.className = "msg notice";
  const btn = document.createElement("button");
  btn.className = "btn-ghost";
  btn.style.marginLeft = "6px";
  btn.textContent = "让 Agent 现在汇总 →";
  btn.onclick = () => {
    const input = document.getElementById("input");
    input.value =
      `后台子代理任务 ${data.task_id}（${data.agent_type || ""}）已结束，` +
      "请用 check_task 获取它的报告，并向我汇总要点。";
    send();
  };
  d.appendChild(document.createTextNode("后台任务已结束，"));
  d.appendChild(btn);
  curLog().appendChild(d);
  scrollLog();
}

document.getElementById("tasks-refresh").onclick = () => loadTasks();
document.getElementById("tasks-cancel").onclick = async () => {
  await request("tasks.cancel_all");
  addNotice("已请求取消全部运行中的子任务");
  loadTasks();
};
// 面板打开期间轻量轮询（3s），关闭即停
setInterval(() => {
  if (rightActive === "tasks" && !rightPanel.classList.contains("hidden")) loadTasks();
}, 3000);

// —— 浏览器：iframe 预览（本地开发服务器 / 可内嵌网页） ——
const browserUrl = document.getElementById("browser-url");
const browserFrame = document.getElementById("browser-frame");
const browserEmpty = document.getElementById("browser-empty");

function browserGo() {
  let u = browserUrl.value.trim();
  if (!u) return;
  if (!/^https?:\/\//i.test(u)) u = "http://" + u;
  browserUrl.value = u;
  browserFrame.src = u;
  browserFrame.classList.remove("hidden");
  browserEmpty.classList.add("hidden");
}
document.getElementById("browser-go").onclick = browserGo;
document.getElementById("browser-reload").onclick = () => {
  const u = browserFrame.src;
  if (!u) return;
  browserFrame.src = "about:blank";
  setTimeout(() => { browserFrame.src = u; }, 30);
};
browserUrl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") browserGo();
});

// ---------- 界面缩放：整个页面内容（字体随内容）按百分比缩放 ----------
// 实现：CSS zoom 打在 #app 与 body 级浮层上；默认 90%（略缩小），设置 · 界面与通知页的
// −/+ 步进调整，偏好存后端 ui.json（ui_scale，70–120）。缩放后物理像素与缩放布局坐标
// 相差 uiScale 倍，凡依赖鼠标坐标 / 视口尺寸的定位与拖拽计算都需要除回 uiScale。
const UI_SCALE_DEFAULT = 90, UI_SCALE_MIN = 70, UI_SCALE_MAX = 120, UI_SCALE_STEP = 10;
let uiScale = UI_SCALE_DEFAULT / 100;

function applyUiScale(pct) {
  pct = Math.round(Math.min(UI_SCALE_MAX, Math.max(UI_SCALE_MIN, pct)));
  uiScale = pct / 100;
  document.documentElement.style.setProperty("--ui-zoom", String(uiScale));
  const label = document.getElementById("zoom-label");
  if (label) label.textContent = pct + "%";
  document.getElementById("btn-zoom-out").disabled = pct <= UI_SCALE_MIN;
  document.getElementById("btn-zoom-in").disabled = pct >= UI_SCALE_MAX;
}

// 缩放偏好防抖落盘：Ctrl+滚轮一格接一格地滚，不能每格都写一次 ui.json
let uiScaleSaveTimer = null;
function saveUiScaleLater() {
  clearTimeout(uiScaleSaveTimer);
  uiScaleSaveTimer = setTimeout(() => saveUiScaleNow(), 400);
}
function saveUiScaleNow() {
  clearTimeout(uiScaleSaveTimer);
  uiScaleSaveTimer = null;
  // 90% 是默认：删键而非存 90（缺省即 CSS 里的 --ui-zoom: .9）
  saveUiPrefs({ ui_scale: Math.round(uiScale * 100) === UI_SCALE_DEFAULT ? null : Math.round(uiScale * 100) });
}

function changeUiScale(dir) {
  const pct = Math.round(uiScale * 100) + dir * UI_SCALE_STEP;
  applyUiScale(pct);
  saveUiScaleLater();
}

// Ctrl+滚轮缩放（桌面惯例）。passive:false 才能 preventDefault 拦下 WebView2
// 自带的原生缩放——不然 CSS zoom 和浏览器 zoom 叠加，界面忽大忽小。
let zoomWheelAccum = 0;
window.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  zoomWheelAccum += e.deltaY;
  if (Math.abs(zoomWheelAccum) >= 40) {
    changeUiScale(zoomWheelAccum < 0 ? 1 : -1);
    zoomWheelAccum = 0;
  }
}, { passive: false });

// Ctrl＋/－/0：放大 / 缩小 / 回默认。终端（.xterm）里不拦——按键要原样进 PTY
document.addEventListener("keydown", (e) => {
  if (!e.ctrlKey || e.altKey || e.metaKey || e.key === "Control") return;
  const t = e.target;
  if (t && t.closest && t.closest(".xterm")) return;
  if (e.key === "=" || e.key === "+") {
    e.preventDefault();
    changeUiScale(1);
  } else if (e.key === "-") {
    e.preventDefault();
    changeUiScale(-1);
  } else if (e.key === "0") {
    e.preventDefault();
    applyUiScale(UI_SCALE_DEFAULT);
    saveUiScaleNow();
  }
});

document.getElementById("btn-zoom-out").onclick = () => changeUiScale(-1);
document.getElementById("btn-zoom-in").onclick = () => changeUiScale(1);
// 双击数值回默认（与阅读行宽同一手势）
const zoomLabelEl = document.getElementById("zoom-label");
if (zoomLabelEl) {
  zoomLabelEl.title = "双击回默认（90%）";
  zoomLabelEl.ondblclick = () => {
    applyUiScale(UI_SCALE_DEFAULT);
    saveUiScaleNow();
  };
}

// ---------- 阅读行宽（--chat-max-w）：±40px 步进，双击标签回默认 ----------
function renderReadWidth() {
  const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--chat-max-w"), 10) || 880;
  const label = document.getElementById("readwidth-label");
  if (label) label.textContent = cur + "px";
  // 到边界禁用按钮（与界面缩放一致），免得点了没反应像坏了
  const lim = UI_LIMITS.read_width;
  const out = document.getElementById("btn-readwidth-out");
  const inn = document.getElementById("btn-readwidth-in");
  if (out) out.disabled = cur <= lim.min;
  if (inn) inn.disabled = cur >= lim.max;
}
function renderReadWidthSafe() { try { renderReadWidth(); } catch (e) { /* 设置页未挂载时不阻塞启动 */ } }
function changeReadWidth(dir) {
  const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--chat-max-w"), 10) || 880;
  const v = Math.min(UI_LIMITS.read_width.max, Math.max(UI_LIMITS.read_width.min, cur + dir * 40));
  setUiVar("read_width", v);
  renderReadWidth();
  saveUiPrefs({ read_width: v === 880 ? null : v }); // 880 是默认：删键而非存 880
}
const rwOut = document.getElementById("btn-readwidth-out");
const rwIn = document.getElementById("btn-readwidth-in");
if (rwOut) rwOut.onclick = () => changeReadWidth(-1);
if (rwIn) rwIn.onclick = () => changeReadWidth(1);
const rwRow = document.getElementById("readwidth-row");
if (rwRow) {
  rwRow.ondblclick = () => {
    setUiVar("read_width", null);
    renderReadWidth();
    saveUiPrefs({ read_width: null });
  };
}

// ---------- 手动调整区域大小：拖侧栏右缘改宽度、拖输入区上缘改高度 ----------
// 偏好存后端 ui.json（pywebview 私密模式下 localStorage 每次启动都会清空）。
const UI_LIMITS = {
  sidebar_w: { css: "--sb-w", min: 200, max: 460 },
  composer_h: { css: "--cp-h", min: 74, max: 520 },
  right_w: { css: "--rp-w", min: 240, max: 720 },
  read_width: { css: "--chat-max-w", min: 680, max: 1400 }, // 阅读行宽：消息卡最大宽度
};

function setUiVar(key, val) {
  const lim = UI_LIMITS[key];
  if (val == null) document.documentElement.style.removeProperty(lim.css);
  else document.documentElement.style.setProperty(lim.css, val + "px");
}

function clampUi(key, v) {
  const lim = UI_LIMITS[key];
  // 动态上限：侧栏至少给主区留 420px；右面板给主区（扣除侧栏后）留 380px；
  // 输入区至少给聊天流留 240px。window 尺寸与 getBoundingClientRect 是物理像素，除回 uiScale
  const dynMax = key === "sidebar_w" ? window.innerWidth / uiScale - 420
    : key === "right_w"
      ? (window.innerWidth - document.getElementById("sidebar").getBoundingClientRect().width) / uiScale - 380
      : window.innerHeight / uiScale - 240;
  const max = Math.max(lim.min, Math.min(lim.max, dynMax));
  return Math.round(Math.min(max, Math.max(lim.min, v)));
}

function saveUiPrefs(patch) {
  request("ui.save", { prefs: patch }).catch(() => {});
}

async function initUiPrefs() {
  const r = await request("ui.get").catch(() => null);
  const prefs = (r && r.prefs) || {};
  // 界面缩放：未存偏好时保持默认 90%（CSS 与初始状态一致）
  const sc = Number(prefs.ui_scale);
  if (Number.isFinite(sc) && sc > 0) applyUiScale(sc);
  for (const key of Object.keys(UI_LIMITS)) {
    if (prefs[key] != null) setUiVar(key, prefs[key]);
  }
  renderReadWidthSafe();
  // 系统通知开关（默认开）
  uiNotifyOn = prefs.notify == null ? true : prefs.notify === 1;
  renderNotifyToggle();
  // 提示音与发送键（默认关 / Enter 发送）；提示音默认只在失焦时响
  uiNotifySound = prefs.notify_sound === 1;
  uiNotifySoundFocus = prefs.notify_sound_focus == null ? true : prefs.notify_sound_focus === 1;
  ctrlEnterSend = prefs.ctrl_enter_send === 1;
  // 通知类型开关（默认都开）
  uiNotifyKindDone = prefs.notify_kind_done == null ? true : prefs.notify_kind_done === 1;
  uiNotifyKindPerm = prefs.notify_kind_perm == null ? true : prefs.notify_kind_perm === 1;
  renderNotifySoundToggle();
  renderNotifyKindToggles();
  renderSendKeyToggle();
  // 通知中心历史回填（恢复的条目不计未读；sid 跨进程无意义，启动恢复的条目不可跳）
  if (Array.isArray(prefs.notif_log)) {
    for (const n of prefs.notif_log.slice(0, 60)) {
      if (n && n.title) {
        notifLog.push({
          ts: Number(n.ts) || 0,
          title: String(n.title),
          body: String(n.body || ""),
          kind: String(n.kind || ""),
        });
      }
    }
  }
  // 分级权限模式（0=安全执行 1=自动编辑 2=完全访问；后端 setup 已把档位应用到 gate，这里只同步界面）
  renderAcceptSwitch(prefs.accept_edits === 2 ? "full_access" : prefs.accept_edits === 1 ? "accept_edits" : "confirm");
  // 主题（auto | 主题 id，默认 auto 跟随系统；旧版 light/dark 映射到纸墨/夜墨）。
  // auto 的深浅落点也在这里回填（要在 applyThemeMode 之前，首帧解析用得上）
  themeAutoLight = THEMES[prefs.theme_auto_light] ? prefs.theme_auto_light : "paper";
  themeAutoDark = THEMES[prefs.theme_auto_dark] ? prefs.theme_auto_dark : "night";
  renderThemeAutoRow();
  applyThemeMode(normalizeThemePref(prefs.theme), false);
  // 侧栏呈现形式（classic=原两区 | grouped=按项目分组）。boot 的首渲染可能已按
  // 默认的 classic 画过一帧，是分组就再补渲染一次
  sidebarView = prefs.sidebar_view === "grouped" ? "grouped" : "classic";
  // 用量页图表类型（bar=柱状默认 | line=折线）：渲染前回填，控件状态同步
  usageChartType = prefs.usage_chart === "line" ? "line" : "bar";
  document.querySelectorAll("#usage-chart-tabs button").forEach((x) =>
    x.classList.toggle("active", x.dataset.chart === usageChartType));
  // 组内会话的自定义拖动序（分组视图）：渲染前先填好缓存
  sessionOrderPrefs = (prefs.session_order && typeof prefs.session_order === "object")
    ? prefs.session_order : {};
  // 标签栏页签的拖动序：sid 数组（渲染前先填好，renderTabs 按它排）
  tabOrderPrefs = Array.isArray(prefs.tab_order) ? prefs.tab_order : [];
  applySidebarView();
  if (sidebarView === "grouped") refreshSessions();
  // 对话区宠物开关（默认显示）+ 大小（pet_scale，缺省 100%）+ 横向落点
  // （pet_x；纵向有重力，总是落在底部）
  petOn = prefs.pet == null ? true : prefs.pet === 1;
  renderPetToggle();
  applyPetScale(Number.isFinite(Number(prefs.pet_scale)) ? Number(prefs.pet_scale) : 100);
  if (petEl) {
    petEl.classList.toggle("hidden", !petOn);
    if (Number.isFinite(prefs.pet_x)) {
      const b = petBounds();
      // 容器还没铺开（异常启动路径）就先不落位，等 ResizeObserver 兜底
      if (b.ok) petApplyPos(...petClampPos(prefs.pet_x, b.floor));
    }
  }
  // 恢复右侧面板：上次打开了哪些标签、激活的是哪个、面板是否收起
  rightTabs = (prefs.right_tabs || []).filter((t) => TAB_META[t]);
  rightActive = rightTabs.includes(prefs.right_active)
    ? prefs.right_active
    : (rightTabs[rightTabs.length - 1] || null);
  rightCollapsed = prefs.right_collapsed === 1;
  if (NARROW_MQ.matches) rightCollapsed = true; // 手机启动一律收起覆盖抽屉，别让面板盖住对话区
  renderRightPanel();
  // 上次开着的标签挨个把数据拉回来。以前只处理了 ext（卡片要搬进面板），
  // 其余标签又画了空壳——启动后日程页只见空白网格、连日期范围都是空的，
  // 得先点别的标签再点回来才有内容。这里统一走 loadRightTab。
  // 面板收起时也照常拉：拉的是数据，不占屏幕，展开时立刻是满的（与 renderRightPanel
  // 只负责显隐的分工一致）；手机上一律收起不代表不该有内容。
  for (const id of rightTabs) loadRightTab(id);
  // 恢复左侧栏折叠态（手机不参与：抽屉由 ☰ 管理，折叠恒为展开）。
  // 恢复在无过渡下完成：启动直接呈现目标状态，不重播「侧栏滑出」动画。
  document.body.classList.add("ui-anim-off");
  leftCollapsed = prefs.left_collapsed === 1 && !NARROW_MQ.matches;
  applyLeftCollapsed();
  void document.body.offsetWidth; // 强制回流：让折叠态在禁过渡下立即定型
  requestAnimationFrame(() => document.body.classList.remove("ui-anim-off"));
}

function setupResizer(el, key, compute) {
  let start = null;
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    start = { x: e.clientX, y: e.clientY, base: compute.base() };
    el.classList.add("active");
    document.body.classList.add("resizing", key === "sidebar_w" ? "resizing-x" : "resizing-y");
  });
  window.addEventListener("pointermove", (e) => {
    if (!start) return;
    setUiVar(key, clampUi(key, compute.value(start, e)));
  });
  window.addEventListener("pointerup", (e) => {
    if (!start) return;
    const v = clampUi(key, compute.value(start, e));
    start = null;
    el.classList.remove("active");
    document.body.classList.remove("resizing", "resizing-x", "resizing-y");
    saveUiPrefs({ [key]: v });
  });
  el.addEventListener("dblclick", () => {
    setUiVar(key, null);
    saveUiPrefs({ [key]: null });
  });
}

setupResizer(document.getElementById("sb-resizer"), "sidebar_w", {
  base: () => document.getElementById("sidebar").getBoundingClientRect().width / uiScale,
  value: (s, e) => s.base + (e.clientX - s.x) / uiScale,
});
setupResizer(document.getElementById("cp-resizer"), "composer_h", {
  base: () => document.getElementById("composer").getBoundingClientRect().height / uiScale,
  value: (s, e) => s.base - (e.clientY - s.y) / uiScale, // 向上拖 = 输入区变高
});
setupResizer(rpResizer, "right_w", {
  base: () => rightPanel.getBoundingClientRect().width / uiScale,
  value: (s, e) => s.base - (e.clientX - s.x) / uiScale, // 向左拖 = 面板变宽
});

// ============================================================
// 新功能区块：主题 / Mermaid / 预览 / 记忆页 / 局域网 /
// 联网搜索与画图配置 / 技能广场 / 更新检查 / 窄屏适配
// （函数声明提升，boot/initUiPrefs 在文件末尾调用时均已可用）
// ============================================================

// ---------- 主题：跟随系统 + 六套配色（存 ui.json 的 theme 键） ----------
// 浅色：纸墨 paper / 青瓷 celadon / 秋柿 kaki；深色：夜墨 night / 黛夜 indigo / 松烟 pine。
// 每套主题是 app.css 文件尾部的一个完整 [data-theme=…] 变量组，切换 = 换 <html> 的 data-theme；
// auto 时浅色落纸墨、深色落夜墨。light/dark 是旧版两档值，读取时映射回 paper/night。
// 与 backend.py 的 THEME_PREFS 白名单、index.html 的主题卡片一一对应，改主题列表要同步。
const THEMES = {
  paper: { name: "纸墨", dark: false },
  celadon: { name: "青瓷", dark: false },
  kaki: { name: "秋柿", dark: false },
  night: { name: "夜墨", dark: true },
  indigo: { name: "黛夜", dark: true },
  pine: { name: "松烟", dark: true },
};
function normalizeThemePref(v) {
  if (v === "light") return "paper";
  if (v === "dark") return "night";
  return Object.prototype.hasOwnProperty.call(THEMES, v) ? v : "auto";
}
let themePref = "auto"; // auto | 主题 id
// 「跟随系统」的深浅落点（设置页主题卡下方的两个下拉；默认纸墨/夜墨）
let themeAutoLight = "paper";
let themeAutoDark = "night";
const themeMql = window.matchMedia("(prefers-color-scheme: dark)");

function resolvedThemeId() {
  if (themePref !== "auto") return themePref;
  return themeMql.matches ? themeAutoDark : themeAutoLight;
}

function applyThemeMode(mode, save = true) {
  themePref = normalizeThemePref(mode);
  const id = resolvedThemeId();
  document.documentElement.setAttribute("data-theme", id);
  // 首帧脚本（index.html）靠 data-theme-mode 判断 auto 该跟系统深还是浅，跟着同步
  document.documentElement.setAttribute("data-theme-mode", themePref);
  // 标题栏（wintheme）与 Mermaid 配色只认深浅两档，按主题的明暗折算
  const dark = THEMES[id].dark;
  request("app.apply_theme", { resolved: dark ? "dark" : "light", theme: id }).catch(() => {});
  mermaidSetTheme(dark ? "dark" : "default");
  // 已开的 xterm 终端跟着换配色（背景/前景/ANSI 十六色）
  for (const t of termTabs) if (t.term) t.term.options.theme = xtermTheme();
  renderThemePicker();
  renderThemeSwatches();
  if (save) saveUiPrefs({ theme: themePref === "auto" ? null : themePref });
}

function renderThemePicker() {
  document.querySelectorAll("#theme-grid .theme-card").forEach((b) =>
    b.classList.toggle("active", b.dataset.themePref === themePref));
}

// ---------- 主题色卡：运行时从 CSS 变量取色 ----------
// 内联在 index.html 里的色块只是首帧兜底；真色板从这里来，改主题色不会两处失真。
// 技巧：变量定义在 [data-theme=…] 选择器上，挂一个同属性隐藏元素即可读任意主题
// 的变量，不必真的切全局主题（切了会整页闪）。
function themeSwatchColors(id) {
  const probe = document.createElement("div");
  probe.setAttribute("data-theme", id);
  probe.style.display = "none";
  document.body.appendChild(probe);
  try {
    const cs = getComputedStyle(probe);
    return [
      cs.getPropertyValue("--card").trim(), // 卡纸
      cs.getPropertyValue("--blue").trim(), // 主色
      cs.getPropertyValue("--bg").trim(), // 桌面底
    ];
  } finally {
    probe.remove();
  }
}

function renderThemeSwatches() {
  const paint = (cardId, srcId) => {
    const card = document.querySelector(`.theme-card[data-theme-pref="${cardId}"]`);
    if (!card) return;
    const [c, b, g] = themeSwatchColors(srcId);
    const blocks = card.querySelectorAll(".theme-swatch i");
    if (blocks.length < 3 || !c || !b || !g) return;
    blocks[0].style.background = c;
    blocks[1].style.background = b;
    blocks[2].style.background = g;
  };
  for (const id of Object.keys(THEMES)) paint(id, id);
  paint("auto", resolvedThemeId()); // 跟随系统卡显示当前解析落点的色板
}

// 「跟随系统时」的深浅落点下拉：改了立即按新落点重解析（当前是 auto 才有视觉效果）
function renderThemeAutoRow() {
  const l = document.getElementById("theme-auto-light");
  const d = document.getElementById("theme-auto-dark");
  if (l) l.value = themeAutoLight;
  if (d) d.value = themeAutoDark;
}
const autoLightSel = document.getElementById("theme-auto-light");
if (autoLightSel) {
  autoLightSel.onchange = () => {
    themeAutoLight = THEMES[autoLightSel.value] ? autoLightSel.value : "paper";
    saveUiPrefs({ theme_auto_light: themeAutoLight });
    renderThemeSwatches();
    if (themePref === "auto") applyThemeMode("auto", false);
  };
}
const autoDarkSel = document.getElementById("theme-auto-dark");
if (autoDarkSel) {
  autoDarkSel.onchange = () => {
    themeAutoDark = THEMES[autoDarkSel.value] ? autoDarkSel.value : "night";
    saveUiPrefs({ theme_auto_dark: themeAutoDark });
    renderThemeSwatches();
    if (themePref === "auto") applyThemeMode("auto", false);
  };
}
document.querySelectorAll("#theme-grid .theme-card").forEach((b) => {
  b.onclick = () => applyThemeMode(b.dataset.themePref);
});
themeMql.addEventListener("change", () => {
  if (themePref === "auto") applyThemeMode("auto", false);
});

// ---------- 第三方大库按需加载 ----------
// mermaid 单文件 3.3MB、xterm 双文件约 400KB，此前页面一打开就同步解析，
// 冷启动白屏几百毫秒到秒级，而绝大多数会话一张图不画、底部终端默认关着。
// 首次用到时才注入 <script>；加载失败由调用方按纯文本兜底。
const _libLoads = {};
function loadLib(name, srcs) {
  if (!(_libLoads[name] instanceof Promise)) {
    _libLoads[name] = new Promise((resolve, reject) => {
      let i = 0;
      const next = () => {
        if (i >= srcs.length) { reject(new Error("库加载失败: " + name)); return; }
        const s = document.createElement("script");
        s.src = srcs[i++];
        s.onload = () => (i < srcs.length ? next() : resolve());
        s.onerror = () => reject(new Error("库加载失败: " + s.src));
        document.head.appendChild(s);
      };
      next();
    });
  }
  return _libLoads[name];
}
function ensureMermaid() {
  if (window.mermaid) return Promise.resolve();
  return loadLib("mermaid", ["/static/vendor/mermaid.min.js"]);
}
function ensureXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  return loadLib("xterm", ["/static/vendor/xterm.js", "/static/vendor/xterm-fit.js"]);
}

// ---------- Mermaid：```\mermaid 代码块 → SVG（vendor 本地库，零构建） ----------
let mermaidThemeCurrent = "default";

function mermaidSetTheme(resolved) {
  mermaidThemeCurrent = resolved === "dark" ? "dark" : "default";
  if (window.mermaid) {
    try {
      window.mermaid.initialize({
        startOnLoad: false, securityLevel: "strict", theme: mermaidThemeCurrent,
      });
    } catch (e) { /* 老浏览器/初始化失败：按纯代码块展示 */ }
  }
}
async function renderMermaidIn(container) {
  if (!container) return;
  const nodes = container.querySelectorAll(".mermaid:not([data-processed])");
  if (!nodes.length) return;
  try {
    await ensureMermaid();
    // 懒加载后第一次使用：按当前主题初始化（mermaidSetTheme 在库缺席时只记录）
    window.mermaid.initialize({
      startOnLoad: false, securityLevel: "strict", theme: mermaidThemeCurrent,
    });
    await window.mermaid.run({ nodes });
  } catch (e) {
    // 加载失败 / 语法有误的图：退回普通代码块展示原文，不吞掉用户内容
    nodes.forEach((n) => {
      n.setAttribute("data-processed", "true");
      const err = n.querySelector(".error-text");
      if (err) err.textContent = "图表语法有误，原始内容：" + n.textContent.slice(0, 200);
    });
  }
}

// ---------- 代码块语法高亮（vendor 本地 highlight.js，零构建） ----------
// 与 Mermaid 同套路：流式期间保持原文，只在消息收尾/历史渲染时高亮一次；
// data-hl 标记防重复。带语言标记且库不认识的语言 → 保持原文（不做自动探测，
// 避免短片段误判出一身花色）；无语言标记的块交给 hljs 自动探测。
function highlightCodeIn(container) {
  if (!window.hljs || !container) return;
  container.querySelectorAll("pre code:not([data-hl])").forEach((el) => {
    el.setAttribute("data-hl", "1");
    const m = /language-([\w+#.-]+)/.exec(el.className);
    if (m && !window.hljs.getLanguage(m[1])) return;
    try { window.hljs.highlightElement(el); } catch (e) { /* 保持原文 */ }
  });
}

// ---------- 本地 HTML / 图片一键预览：浏览器标签 iframe ----------
function openHtmlPreview(relPath) {
  openRightTab("browser");
  const url = location.origin + "/preview?p=" + encodeURIComponent(relPath);
  browserUrl.value = url;
  browserGo();
}

// ---------- 设置 · 全局记忆 ----------
// 定期整理的后台状态缓存：设置页只保留全局开关与周期，项目开关已移到右面板；
// 保存时 project_enabled 必须传「当前真实值」而不是 undefined，否则会把项目整理误关
let maintainState = { global_enabled: true, project_enabled: true, interval_hours: 168 };
let memoryPageMtime = 0; // 打开页面时的文件 mtime：保存时带回比对，防覆盖后台新写的记忆

// 注入上限提示：文件超过注入上限时，页面必须明说 Agent 只看到了前一段（默认是静默截断）
function memoryInjectNote(fullLen, injectLen) {
  const el = document.getElementById("memory-inject-note");
  if (!el) return;
  if (fullLen > injectLen && injectLen > 0) {
    el.textContent = `⚠ 记忆共 ${fullLen} 字，超过单轮注入上限：每轮只注入前 ${injectLen} 字（按行截断），` +
      "超出部分 Agent 看不到；可精简表述或删掉过时条目。";
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// (自动) 条目审阅提醒：归档提炼是无确认的后台写入，提醒用户定期扫一眼
function memoryAutoNote(text) {
  const el = document.getElementById("memory-auto-note");
  if (!el) return;
  const n = (text.match(/^\s*-\s*\[[^\]]*\]\s*\(自动\)/gm) || []).length;
  el.hidden = n === 0;
  if (n) el.textContent = `有 ${n} 条带 (自动) 标记的记忆由归档提炼自动写入，建议偶尔审阅、清理不要的条目。`;
}

async function loadMemoryPage() {
  try {
    const r = await request("memory.get");
    document.getElementById("memory-text").value = r.text || "";
    memoryPageMtime = r.mtime || 0;
    memoryInjectNote((r.text || "").length, r.inject_chars || 0);
    memoryAutoNote(r.text || "");
    const t = document.getElementById("memory-digest-toggle");
    if (t) t.checked = !!r.digest_enabled;
    const m = r.maintain || {};
    maintainState = {
      global_enabled: m.global_enabled !== false,
      project_enabled: m.project_enabled !== false,
      interval_hours: m.interval_hours || 168,
    };
    const g = document.getElementById("memory-maintain-global");
    if (g) g.checked = maintainState.global_enabled;
    const sel = document.getElementById("memory-maintain-interval");
    if (sel) {
      const v = String(m.interval_hours || 168);
      if (!sel.querySelector(`option[value="${v}"]`)) {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = `每 ${v} 小时`;
        sel.appendChild(opt);
      }
      sel.value = v;
    }
    const last = document.getElementById("memory-maintain-last");
    if (last) {
      const fmt = (ts) => ts > 0 ? new Date(ts * 1000).toLocaleString() : "还没有整理过";
      last.textContent = `上次整理 · 全局：${fmt(m.global_last)}，项目：${fmt(m.project_last)}`;
      last.hidden = false;
    }
  } catch (e) {
    addNotice("记忆加载失败: " + e.message);
  }
}
document.getElementById("btn-memory-save").onclick = async () => {
  // 注意：这里必须用全局记忆自己的状态行（memory-global-status）——右面板「项目记忆」
  // 也有一处记忆状态行，两者 id 重名时 getElementById 只会拿到文档里靠前的那个，
  // 保存结果就显示到右边的面板上去了（本页看着毫无反应）。
  const status = document.getElementById("memory-global-status");
  try {
    const res = await request("memory.save", {
      text: document.getElementById("memory-text").value,
      base_mtime: memoryPageMtime,
    });
    memoryPageMtime = res.mtime || 0;
    const val = document.getElementById("memory-text").value;
    memoryInjectNote(val.length, res.inject_chars || 0);
    memoryAutoNote(val);
    status.textContent = "✓ 已保存，之后所有会话立即生效";
    status.hidden = false;
  } catch (e) {
    status.textContent = "✗ 保存失败：" + e.message;
    status.hidden = false;
  }
};
// 归档自动记忆总闸：切换即保存（失败回拨，与电脑控制开关同一交互）
const memoryDigestToggle = document.getElementById("memory-digest-toggle");
if (memoryDigestToggle) {
  memoryDigestToggle.addEventListener("change", async (e) => {
    const el = e.target;
    const st = document.getElementById("memory-digest-status");
    try {
      const r = await request("memory.digest_save", { enabled: el.checked });
      el.checked = !!r.enabled; // 以落库值为准
      if (st) st.hidden = true;
    } catch (err) {
      el.checked = !el.checked;
      if (st) {
        st.textContent = "✗ 保存失败：" + err.message;
        st.className = "card-status bad";
        st.hidden = false;
      }
    }
  });
}
// 定期整理：开关与周期即改即存（项目开关在右面板，这里只透传现值）；
// 「立即整理」无视周期手动跑一轮（仍守最短规模阈值）
async function saveMaintainSettings(patch) {
  const st = document.getElementById("memory-maintain-status");
  try {
    const r = await request("memory.maintain_save", {
      global_enabled: "global_enabled" in patch ? patch.global_enabled : maintainState.global_enabled,
      project_enabled: maintainState.project_enabled,
      interval_hours: "interval_hours" in patch ? patch.interval_hours : maintainState.interval_hours,
    });
    maintainState = { global_enabled: r.global_enabled, project_enabled: r.project_enabled,
      interval_hours: r.interval_hours };
    if (st) st.hidden = true;
    return r;
  } catch (e) {
    if (st) {
      st.textContent = "✗ 保存失败：" + e.message;
      st.className = "card-status bad";
      st.hidden = false;
    }
    return null;
  }
}
const memoryMaintainGlobal = document.getElementById("memory-maintain-global");
if (memoryMaintainGlobal) {
  memoryMaintainGlobal.addEventListener("change", async (e) => {
    const el = e.target;
    const r = await saveMaintainSettings({ global_enabled: el.checked });
    if (!r) el.checked = !el.checked; // 失败回拨
  });
}
const memoryMaintainInterval = document.getElementById("memory-maintain-interval");
if (memoryMaintainInterval) {
  memoryMaintainInterval.addEventListener("change", async (e) => {
    const el = e.target;
    const r = await saveMaintainSettings({ interval_hours: parseInt(el.value, 10) });
    if (!r) loadMemoryPage(); // 失败回拨：按后台真实值重填（含动态选项）
  });
}
document.getElementById("memory-maintain-now").onclick = async (e) => {
  const btn = e.target;
  const st = document.getElementById("memory-maintain-status");
  btn.disabled = true;
  btn.textContent = "🧹 整理中…";
  try {
    const r = await request("memory.maintain_now");
    if (r.ran) {
      const done = [r.global && "全局", r.project && "项目"].filter(Boolean).join("、");
      st.textContent = `✓ 已整理：${done}（原件已备份），系统提示词已刷新`;
    } else {
      st.textContent = r.message;
    }
    st.className = "card-status";
    st.hidden = false;
    loadMemoryPage(); // 刷新「上次整理」时间
  } catch (err) {
    st.textContent = "✗ 整理失败：" + err.message;
    st.className = "card-status bad";
    st.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "🧹 立即整理";
  }
};

// ---------- 设置 · 局域网访问 ----------
// 复制访问地址：局域网 / Tailscale 地址都是 http 明文，非安全上下文里
// navigator.clipboard 不存在，统一走 execCommand 兜底
async function copyAccessUrl(url, msgId) {
  if (!url) return;
  let ok = false;
  try {
    await navigator.clipboard.writeText(url);
    ok = true;
  } catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = url;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      ta.remove();
    } catch (e2) { ok = false; }
  }
  const msg = document.getElementById(msgId);
  if (msg) {
    msg.textContent = ok ? "✓ 已复制，发到手机后用浏览器打开" : "✗ 复制失败，请手动长按地址复制";
    msg.className = "io-msg " + (ok ? "ok" : "bad");
  }
}

// 令牌验证失败的概览（lan / ts 共用一把令牌，展示同一条）
function tokenFailureNote(failures) {
  if (!failures || !failures.total) return "";
  const recent = (failures.recent || []).map((f) =>
    f.ip ? `${escapeHtml(f.ip)}（${new Date(f.ts * 1000).toLocaleString()}）` : ""
  ).filter(Boolean).join("、");
  return `<div class="lan-note bad">⚠ 自启动以来有 ${failures.total} 次令牌验证失败` +
    (recent ? `，最近：${recent}` : "") +
    "。不是你的操作的话，说明有人在试你的令牌——建议重新生成令牌。</div>";
}

// 重新生成访问令牌：立即生效，旧地址/二维码/已登录的手机全部作废
async function rotateAccessToken() {
  if (!confirm("重新生成访问令牌？所有已发出的地址和二维码立即作废，手机需要用新地址重新打开。")) return;
  try {
    const r = await request("lan.rotate_token", {});
    addNotice(r.note || "令牌已更换");
    loadLanPanel();
    loadTsPanel();
  } catch (e) {
    addNotice("操作失败: " + e.message);
  }
}

async function loadLanPanel() {
  let st;
  try { st = await request("lan.status"); } catch (e) { return; }
  const info = document.getElementById("lan-info");
  const toggle = document.getElementById("lan-toggle");
  toggle.checked = !!st.enabled;
  const urls = (st.ips || []).map((ip) => `http://${ip}:${location.port}/?token=${st.token || "你的令牌"}`);
  if (!st.enabled) {
    info.innerHTML = `<div class="lan-note">${escapeHtml(st.note || "")}</div>`;
    return;
  }
  info.innerHTML = `
    <div class="lan-note">开启后<b>重启 SkySheep 生效</b>。同一 Wi-Fi 下的手机 / 平板用下面的地址访问
      （首次打开自动记住令牌）：</div>
    <div class="lan-url">${urls.map((u) => `<code>${escapeHtml(u)}</code>`).join("")}</div>
    <div class="lan-body">
      <div id="lan-qr" class="qr-box"></div>
      <div class="lan-note">手机扫码直达，即可用完整界面遥控这台电脑上的 SkySheep
        （聊天 / 批准确认 / 看任务进度）。二维码含访问令牌，<b>请勿截图外传</b>；地址是 http 明文传输，
        只建议在可信网络使用——跨网络或不可信 Wi-Fi 请用「远程访问（Tailscale）」，链路端到端加密。<br>
        手机连不上时先看 Windows 防火墙：首次弹出的授权要点「允许」，错过的话在防火墙设置里放行 SkySheep。<br>
        <button id="lan-copy" class="btn-ghost" style="margin-top:6px">⧉ 复制地址</button>
        <button id="lan-rotate" class="btn-ghost" style="margin-top:6px">⟳ 重新生成令牌</button>
        <span id="lan-copy-msg" class="io-msg"></span>
      </div>
    </div>
    ${tokenFailureNote(st.token_failures)}`;
  const copyBtn = document.getElementById("lan-copy");
  if (copyBtn) copyBtn.onclick = () => copyAccessUrl(urls[0] || "", "lan-copy-msg");
  const rotateBtn = document.getElementById("lan-rotate");
  if (rotateBtn) rotateBtn.onclick = () => rotateAccessToken();
  if (urls.length && st.token && window.QRCode) {
    try {
      new QRCode(document.getElementById("lan-qr"), {
        text: urls[0], width: 132, height: 132, correctLevel: QRCode.CorrectLevel.M,
      });
    } catch (e) { /* 二维码失败不影响地址文本 */ }
  }
}
document.getElementById("lan-toggle").addEventListener("change", async (e) => {
  try {
    const r = e.target.checked ? await request("lan.enable", {}) : await request("lan.disable");
    addNotice(r.note || "局域网访问设置已更新");
    loadLanPanel();
  } catch (err) {
    addNotice("操作失败: " + err.message);
    e.target.checked = !e.target.checked;
  }
});

// ---------- 设置 · 远程访问（Tailscale）：不在同一网络也能连回家 ----------
async function loadTsPanel() {
  let st;
  try { st = await request("remote.status"); } catch (e) { return; }
  const info = document.getElementById("ts-info");
  const toggle = document.getElementById("ts-toggle");
  toggle.checked = !!st.enabled;
  const urls = (st.ips || []).map((ip) => `http://${ip}:${location.port}/?token=${st.token || "你的令牌"}`);
  if (!st.enabled) {
    info.innerHTML = `<div class="lan-note">${escapeHtml(st.note || "")}</div>`;
    return;
  }
  if (!urls.length) {
    info.innerHTML = `<div class="lan-note">⚠ 没检测到 Tailscale 地址（100.64 开头的虚拟网卡）。
      请确认电脑上 Tailscale 已安装、已登录且正在运行，然后重启 SkySheep 再试。</div>`;
    return;
  }
  info.innerHTML = `
    <div class="lan-note">开启后<b>重启 SkySheep 生效</b>。手机登录同一 Tailscale 账号后，
      在<b>任意网络</b>（含手机流量）用下面的地址访问，功能与同一 Wi-Fi 时完全一样：</div>
    <div class="lan-url">${urls.map((u) => `<code>${escapeHtml(u)}</code>`).join("")}</div>
    <div class="lan-body">
      <div id="ts-qr" class="qr-box"></div>
      <div class="lan-note">手机扫码直达。二维码含访问令牌，<b>请勿截图外传</b>；
        只有你 Tailscale 账号里的设备能连进来，同一 Wi-Fi 下的陌生设备反而进不来。<br>
        <button id="ts-copy" class="btn-ghost" style="margin-top:6px">⧉ 复制地址</button>
        <button id="ts-rotate" class="btn-ghost" style="margin-top:6px">⟳ 重新生成令牌</button>
        <span id="ts-copy-msg" class="io-msg"></span>
      </div>
    </div>
    ${tokenFailureNote(st.token_failures)}`;
  const copyBtn = document.getElementById("ts-copy");
  if (copyBtn) copyBtn.onclick = () => copyAccessUrl(urls[0] || "", "ts-copy-msg");
  const rotateBtn = document.getElementById("ts-rotate");
  if (rotateBtn) rotateBtn.onclick = () => rotateAccessToken();
  if (urls.length && st.token && window.QRCode) {
    try {
      new QRCode(document.getElementById("ts-qr"), {
        text: urls[0], width: 132, height: 132, correctLevel: QRCode.CorrectLevel.M,
      });
    } catch (e) { /* 二维码失败不影响地址文本 */ }
  }
}
document.getElementById("ts-toggle").addEventListener("change", async (e) => {
  try {
    const r = e.target.checked ? await request("remote.enable", {}) : await request("remote.disable");
    addNotice(r.note || "远程访问设置已更新");
    loadTsPanel();
  } catch (err) {
    addNotice("操作失败: " + err.message);
    e.target.checked = !e.target.checked;
  }
});

// ---------- 一键重启：拉起新实例后旧进程优雅退出（LOCAL_ONLY，仅桌面本机可调） ----------
document.getElementById("btn-app-restart").onclick = async () => {
  if (!confirm("重启 SkySheep？未完成的对话轮会被中断，重启后窗口自动恢复。")) return;
  const msg = document.getElementById("app-restart-msg");
  try {
    await request("app.restart", {});
    if (msg) {
      msg.textContent = "⏳ 正在重启，稍候窗口会自动恢复…";
      msg.className = "io-msg";
    }
  } catch (e) {
    if (msg) {
      msg.textContent = "✗ 重启失败：" + e.message;
      msg.className = "io-msg bad";
    }
  }
};

// ---------- 设置 · 技能与工具：电脑控制 / 浏览器控制（工具总开关，开关即保存） ----------
// 这两个开关决定对应工具是否注册给 Agent（打开即热生效）。它们是「Agent 能用哪些工具」，
// 不是「远程接入」能力，所以放在「内置工具」列表处；advanced.save 支持只提交
// 要改的字段，其余高级参数保持不变。
async function loadToolControl() {
  let d;
  try { d = await request("advanced.get"); } catch (e) { return; }
  const c = document.getElementById("tool-computer-control");
  const b = document.getElementById("tool-browser-control");
  if (c) c.checked = !!d.computer_control;
  if (b) b.checked = !!d.browser_control;
}
function toolControlStatus(statusId, text, ok = true) {
  const st = document.getElementById(statusId);
  if (!st) return;
  st.textContent = text;
  st.className = "card-status " + (ok ? "ok" : "bad");
  st.hidden = !text;
}
async function saveToolControl(el, key, statusId, label) {
  try {
    const d = await request("advanced.save", { [key]: el.checked });
    // 用返回值同步两个开关（避免再发一次请求）
    const c = document.getElementById("tool-computer-control");
    const b = document.getElementById("tool-browser-control");
    if (c) c.checked = !!d.computer_control;
    if (b) b.checked = !!d.browser_control;
    // 成功不提示：开关本身就是状态（此前会显一行绿色「已开启…」，太吵）；
    // 失败才需要说明原因，比如配置写入被拒
    toolControlStatus(statusId, "");
    // 开关改变了 Agent 的工具清单：重渲染设置页，让「内置工具」列表同步
    try { await renderSettings(); } catch (e) { /* 列表刷新失败不影响保存结果 */ }
  } catch (e) {
    el.checked = !el.checked;
    toolControlStatus(statusId, "✗ 保存失败：" + e.message, false);
  }
}
const toolComputerToggle = document.getElementById("tool-computer-control");
if (toolComputerToggle) {
  toolComputerToggle.addEventListener("change", (e) =>
    saveToolControl(e.target, "computer_control", "tool-computer-status", "电脑控制"));
}
const toolBrowserToggle = document.getElementById("tool-browser-control");
if (toolBrowserToggle) {
  toolBrowserToggle.addEventListener("change", (e) =>
    saveToolControl(e.target, "browser_control", "tool-browser-status", "浏览器控制"));
}

// ---------- 设置 · 聊天机器人渠道（Bot Channel） ----------
// 渠道是「受限遥控端」：能对话与审批，但不能改降低防护的开关（后端 dispatch 层也拦，
// 这里不提供入口）。默认关闭，启用前必须填允许名单。
const CHANNEL_LABEL = { feishu: "飞书", weixin: "微信" };
// 微信的凭据来自扫码（不是手填 Token），且会失效需重登
let wxLoginQrcode = "";
let wxLoginTimer = null;

// 渠道卡片的操作结果就地显示，不走 addNotice。
// 原因：设置页打开时对话区（#view-chat）是 hidden，而 addNotice 写进的是对话日志
// ——在渠道页点「启用」「保存」「发测试消息」，提示全落在看不见的地方，用户只看到
// 开关弹回去、按钮像没反应。这里把结果留在卡片上方，并把状态存进模块变量，
// 使 loadChannelPanel() 重渲染后提示不丢。
let channelMsgState = null; // {text, kind: "ok"|"bad"}

function channelMsg(text, kind) {
  channelMsgState = text ? { text, kind: kind || "" } : null;
  renderChannelMsg();
}

function renderChannelMsg() {
  const el = document.getElementById("channel-msg");
  if (!el) return;
  const st = channelMsgState;
  el.hidden = !st;
  el.textContent = st ? st.text : "";
  el.className = "channel-msg" + (st && st.kind ? " " + st.kind : "");
}

async function loadChannelPanel() {
  const box = document.getElementById("channel-panel");
  if (!box) return;
  let st;
  try { st = await request("channel.status"); } catch (e) { return; }
  const rows = st.channels || [];
  if (!rows.length) {
    box.innerHTML = `<div id="channel-msg" class="channel-msg" hidden></div>
      <div class="channel-hint">尚未配置任何渠道。</div>`;
    renderChannelMsg();
    bindChannelEvents();
    return;
  }
  box.innerHTML = `<div id="channel-msg" class="channel-msg" hidden></div>
    <div class="channel-list">${rows.map((c) => channelCard(c)).join("")}</div>
    <div class="channel-footer">
      <span>审批等待：超时未回复自动拒绝</span>
      <input id="channel-timeout" class="channel-input" type="number"
        min="10" max="3600" value="${st.approve_timeout || 120}">
      <span>秒</span>
      <button id="channel-timeout-save" class="btn-ghost">保存</button>
    </div>`;
  renderChannelMsg();
  bindChannelEvents();
}

// 微信的登录区：未登录展示二维码入口，已登录展示状态与重登/退出
function weixinLoginBlock(c) {
  const logged = !!c.has_login;
  const needRelogin = c.extra && c.extra.need_relogin;
  const status = logged
    ? (needRelogin ? `<span class="channel-badge bad">登录已失效</span>`
                   : `<span class="channel-badge on">已登录</span>`)
    : `<span class="channel-badge">未登录</span>`;
  return `<div class="channel-field">登录状态 ${status}</div>
    <div class="channel-hint">${logged
      ? "凭据保存在本机配置里。在手机微信里给机器人发消息即可开始对话。"
      : "微信不用填 Token：点下方按钮生成二维码，用<b>手机微信扫描并在手机上确认</b>即可。"}</div>
    <div class="channel-ops">
      ${logged
        ? `<button class="btn-ghost channel-wx-logout">退出登录</button>`
        : `<button class="btn-ghost channel-wx-login">生成登录二维码</button>`}
    </div>
    <div id="wx-qr-area"></div>`;
}

function channelCard(c) {
  const label = CHANNEL_LABEL[c.name] || c.name;
  // 状态做成一枚小徽标：已关闭 / 运行中 / 已启用但没跑起来。
  // 适配器报 extra.connected === false 时（飞书长连接的“任务在跑、连接未建立”
  // 这种中间态），不能只说「运行中」——那就又变成用户看不到原因的状态。
  const connDown = c.extra && c.extra.connected === false;
  const badge = c.running
    ? (connDown
      ? `<span class="channel-badge warn">已启用但未连上</span>`
      : `<span class="channel-badge on">运行中</span>`)
    : (c.enabled
      ? `<span class="channel-badge warn">已启用但未运行</span>`
      : `<span class="channel-badge">已关闭</span>`);
  const err = c.error
    ? `<div class="channel-hint bad">⚠ ${escapeHtml(c.error)}</div>`
    : "";
  const seen = (c.seen_sources || []).map((s) =>
    `<div class="channel-src">
       <span class="channel-hint">见过的来源</span>
       <code>${escapeHtml(s.chat_id)}</code>
       <span class="channel-hint">出现 ${s.count || 1} 次</span>
       <button class="btn-ghost channel-claim" data-name="${c.name}"
         data-id="${escapeHtml(s.chat_id)}">加入允许名单</button>
     </div>`
  ).join("");
  const ids = (c.allowed_ids || []).join("\n");
  // 凭据区按平台分流：飞书是手填 App ID + App Secret，微信是扫码登录
  const credBlock = c.name === "weixin"
    ? weixinLoginBlock(c)
    : `<div class="channel-field">App ID</div>
       <input class="channel-input channel-appid" data-name="${c.name}" type="text"
         placeholder="${c.has_app_id ? "已保存（留空则不修改）" : "cli_xxxxxxxxxxxxxxxx"}">
       <div class="channel-field">App Secret</div>
       <input class="channel-input channel-appsecret" data-name="${c.name}" type="password"
         placeholder="${c.has_app_secret ? "已保存（留空则不修改）" : "从开发者后台复制 App Secret"}">`;
  // 首次配置的顺序指引：启用不需要先填名单（机器人跑起来才能发现来源），
  // 但名单为空时它对一切消息保持沉默，所以要提示用户「启用 → 发消息 → 回来认领」
  const needClaim = c.enabled && !(c.allowed_ids || []).length;
  const hint = c.name === "weixin"
    ? (needClaim
      ? "已启用但名单还是空的：现在给机器人发一句话，下方出现「见过的来源」后点「加入允许名单」，它才会开始回复。"
      : "登录后给机器人发一句话，然后点上方「加入允许名单」，就能拿到你的 OpenID。")
    : (needClaim
      ? "已启用但名单还是空的：现在在飞书里给机器人发一句话，下方出现「见过的来源」后点「加入允许名单」，它才会开始回复。"
      : "在飞书里搜到你的机器人，给它发一句话，再点上方「加入允许名单」拿到你的 OpenID。");
  // 允许名单的例值与叫法按平台区分：飞书与微信的 id 都是 OpenID 形态，写成纯数字会误导
  const isWx = c.name === "weixin";
  const idLabel = isWx ? "OpenID" : "OpenID / chat id";
  const idPlaceholder = isWx ? "例如 oABC123xyz@im.wechat" : "例如 ou_7d8a6e6df7621556ce0d21922b676706ccs";
  return `<div class="channel-card">
    <div class="channel-head">
      <span class="channel-title">${escapeHtml(label)}</span>
      ${badge}
      <label class="toggle-row"><input type="checkbox" class="channel-toggle" data-name="${c.name}"
        ${c.enabled ? "checked" : ""}><span>启用</span></label>
    </div>
    ${err}
    ${needClaim ? '<div class="channel-hint bad">⚠ 名单还是空的：现在机器人对一切消息保持沉默。先给它发一句话，再回来点「加入允许名单」。</div>' : ""}
    ${credBlock}
    <div class="channel-field">允许名单（每行一个 ${idLabel}；<b>空名单 = 拒绝一切</b>）</div>
    <textarea class="channel-ids" data-name="${c.name}" rows="3"
      placeholder="${idPlaceholder}">${escapeHtml(ids)}</textarea>
    ${seen}
    <label class="channel-opt">
      <input type="checkbox" class="channel-approve" data-name="${c.name}"
        ${c.approve_enabled ? "checked" : ""}>
      <span>允许机器人改文件 / 跑命令（每次在聊天窗口确认）</span>
    </label>
    ${(c.tools_warning || "")
      ? `<div class="channel-hint bad">⚠ ${escapeHtml(c.tools_warning)}</div>`
      : ""}
    ${(c.allowed_tools || []).length
      ? `<div class="channel-hint">预授权：${escapeHtml((c.allowed_tools || []).join("、"))}</div>`
      : ""}
    <div class="channel-ops">
      <button class="btn-ghost channel-save" data-name="${c.name}">保存</button>
      <button class="btn-ghost channel-test" data-name="${c.name}">发测试消息</button>
    </div>
    <div class="channel-hint">${hint}</div>
  </div>`;
}

// ---------- 微信扫码登录 ----------
// 微信接口的 qrcode_img_content 是「要编码进二维码的链接」（扫码后打开的授权页），
// 不是图片地址：直接塞进 <img src> 会因为返回的是网页而破图。这里用页面已加载的
// vendor/qrcode.min.js 渲染（与局域网/Tailscale 二维码同一套），并保留链接兑底。
function renderQrInto(elId, text) {
  const box = document.getElementById(elId);
  if (!box) return;
  box.innerHTML = "";
  if (!window.QRCode) {
    box.innerHTML = '<div class="channel-hint bad">二维码组件未加载，请刷新页面重试</div>';
    return;
  }
  try {
    new QRCode(box, { text, width: 168, height: 168, correctLevel: QRCode.CorrectLevel.M });
  } catch (e) {
    box.innerHTML = `<div class="channel-hint bad">二维码生成失败：${escapeHtml(e.message)}</div>`;
  }
}

async function startWeixinLogin() {
  const area = document.getElementById("wx-qr-area");
  if (area) area.innerHTML = `<div class="channel-hint">正在获取二维码…</div>`;
  let info;
  try {
    info = await request("channel.weixin_login_start");
  } catch (e) {
    if (area) area.innerHTML = `<div class="channel-hint bad">获取二维码失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  wxLoginQrcode = info.qrcode || "";
  const link = info.url || "";
  if (area) {
    if (!link) {
      area.innerHTML = `<div class="channel-hint">没能取到二维码内容，请点「生成登录二维码」重试。</div>`;
    } else {
      area.innerHTML = `
      <div class="wx-login">
        <div id="wx-qr-box" class="wx-qr"></div>
        <div class="wx-login-side">
          <div class="channel-hint">用<b>手机微信</b>扫描左侧二维码，并在手机上确认登录。</div>
          <div class="channel-hint" id="wx-qr-status">等待扫描…</div>
          <div class="channel-hint">扫不出来？在手机微信里打开这个链接也可以继续：
            <a href="${escapeHtml(link)}" target="_blank" rel="noreferrer noopener">打开微信登录链接</a></div>
        </div>
      </div>`;
      renderQrInto("wx-qr-box", link);
    }
  }
  pollWeixinLogin();
}

// 轮询扫码状态：服务器侧是长轮询（hold 最多 35 秒），间隔设小也不会空转
function pollWeixinLogin() {
  if (wxLoginTimer) clearTimeout(wxLoginTimer);
  const tick = async () => {
    if (!wxLoginQrcode) return;
    let st;
    try {
      st = await request("channel.weixin_login_poll", { qrcode: wxLoginQrcode });
    } catch (e) {
      const el = document.getElementById("wx-qr-status");
      if (el) el.textContent = "轮询失败：" + e.message;
      wxLoginTimer = setTimeout(tick, 3000);
      return;
    }
    const el = document.getElementById("wx-qr-status");
    if (st.status === "confirmed") {
      wxLoginQrcode = "";
      channelMsg("微信登录成功", "ok");
      loadChannelPanel();
      return;
    }
    if (st.status === "expired") {
      if (el) el.textContent = "二维码已过期，请重新生成。";
      wxLoginQrcode = "";
      return;
    }
    if (el) el.textContent = st.status === "scaned" ? "已扫描，请在手机上确认…" : "等待扫描…";
    wxLoginTimer = setTimeout(tick, 1000);
  };
  tick();
}

function bindChannelEvents() {
  const wxLogin = document.querySelector(".channel-wx-login");
  if (wxLogin) wxLogin.onclick = () => startWeixinLogin();

  const wxLogout = document.querySelector(".channel-wx-logout");
  if (wxLogout) wxLogout.onclick = async () => {
    if (!confirm("退出登录后需要重新扫码才能使用微信渠道，继续吗？")) return;
    try {
      await request("channel.weixin_logout");
      channelMsg("已退出微信登录", "ok");
    } catch (e) { channelMsg("退出失败: " + e.message, "bad"); }
    loadChannelPanel();
  };

  const timeoutSave = document.getElementById("channel-timeout-save");
  if (timeoutSave) timeoutSave.onclick = async () => {
    const v = parseInt(document.getElementById("channel-timeout").value, 10) || 120;
    try {
      await request("channel.set_timeout", { approve_timeout: v });
      channelMsg("审批等待时间已保存", "ok");
    } catch (e) { channelMsg("保存失败: " + e.message, "bad"); }
    loadChannelPanel();
  };

  document.querySelectorAll(".channel-claim").forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      const ta = document.querySelector(`.channel-ids[data-name="${name}"]`);
      const cur = (ta?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
      if (!cur.includes(btn.dataset.id)) cur.push(btn.dataset.id);
      if (ta) ta.value = cur.join("\n");
      await saveChannel(name);
    };
  });

  document.querySelectorAll(".channel-save").forEach((btn) => {
    btn.onclick = () => saveChannel(btn.dataset.name);
  });

  document.querySelectorAll(".channel-test").forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      const ta = document.querySelector(`.channel-ids[data-name="${name}"]`);
      const first = (ta?.value || "").split("\n").map((s) => s.trim()).filter(Boolean)[0];
      if (!first) { channelMsg("先在允许名单里填一个 chat id", "bad"); return; }
      try {
        const r = await request("channel.test", { name, chat_id: first });
        channelMsg(r.ok ? "测试消息已发出，去聊天窗口看看" : ("发送失败: " + (r.error || "未知原因")),
          r.ok ? "ok" : "bad");
      } catch (e) { channelMsg("发送失败: " + e.message, "bad"); }
    };
  });

  document.querySelectorAll(".channel-toggle").forEach((el) => {
    el.onchange = async (e) => {
      const name = e.target.dataset.name;
      const label = CHANNEL_LABEL[name] || name;
      // 启用前先把本卡片上的 Token / 名单落盘：常见操作是在输入框粘完 Token 就直接
      // 勾「启用」，不点「保存」的话后端看到的还是没 Token 的旧配置，只会回「还没填
      // Bot Token」——用户看到的是开关自动弹回去，以为功能坏了（“启用不了”）。
      // 既然用户已经把凭据填在眼前，勾启用就默认他要用这份，替他存一下。
      if (e.target.checked && !(await saveChannel(name, { silent: true }))) {
        e.target.checked = false;
        return;
      }
      // 名单是否为空按用户眼前输入框里的值算（重渲染后就取不到了）
      const idsEl = document.querySelector(`.channel-ids[data-name="${name}"]`);
      const idsEmpty = !(idsEl?.value || "").trim();
      try {
        if (e.target.checked) {
          await request("channel.enable", { name });
          channelMsg(`「${label}」已启用` + (idsEmpty
            ? "。名单还是空的：先在聊天窗口给它发一句话，再回来点「加入允许名单」" : ""), "ok");
        } else {
          await request("channel.disable", { name });
          channelMsg(`「${label}」已关闭`, "ok");
        }
      } catch (err) {
        channelMsg(`「${label}」启用失败：` + err.message, "bad");
        e.target.checked = !e.target.checked;
      }
      loadChannelPanel();
    };
  });
}

async function saveChannel(name, opts = {}) {
  const tokenEl = document.querySelector(`.channel-token[data-name="${name}"]`);
  const appIdEl = document.querySelector(`.channel-appid[data-name="${name}"]`);
  const appSecretEl = document.querySelector(`.channel-appsecret[data-name="${name}"]`);
  const idsEl = document.querySelector(`.channel-ids[data-name="${name}"]`);
  const approveEl = document.querySelector(`.channel-approve[data-name="${name}"]`);
  const payload = {
    name,
    allowed_ids: idsEl ? idsEl.value : "",
    approve_enabled: !!(approveEl && approveEl.checked),
  };
  // 留空 = 不修改已存的凭据（避免把密码框里的占位文本当成真凭据写回去）
  if (tokenEl && tokenEl.value.trim()) payload.token = tokenEl.value.trim();
  if (appIdEl && appIdEl.value.trim()) payload.app_id = appIdEl.value.trim();
  if (appSecretEl && appSecretEl.value.trim()) payload.app_secret = appSecretEl.value.trim();
  try {
    await request("channel.save", payload);
  } catch (e) {
    channelMsg("保存失败: " + e.message, "bad");
    if (!opts.silent) loadChannelPanel();
    return false;
  }
  // silent：供「勾启用时顺手落盘」用——调用方紧接着还要启停并自己重渲染，
  // 这里就不再闪一次「已保存」、也不重复拉一次面板。
  if (!opts.silent) {
    channelMsg("渠道配置已保存", "ok");
    loadChannelPanel();
  }
  return true;
}

// ---------- 设置 · 联网搜索 / AI 画图 ----------
// 服务商选择：只保留「自动 / 自定义 / 已配置」三档。
// 「已配置」不是下拉选项，而是单独一屏列出你已在「模型服务」里配好的服务，
// 点选即用它的地址与 Key（复用同一套凭据）——搜索 / 画图 / 语音都跑在
// OpenAI 兼容接口上，没必要再抄一遍地址和 Key。
//
// 注意：「已配置」只是查看态，选中某个服务前不改动配置，因此不能从 provider
// 反推档位（切过去时 provider 还是旧的），要用下面这个显式状态记住。
const providerUiMode = {};

function providerModeOf(formId, d) {
  if (providerUiMode[formId]) return providerUiMode[formId];
  const isSvc = (d.configured_services || []).some((s) => s.name === d.provider);
  return isSvc ? "configured" : (d.provider === "custom" ? "custom" : "auto");
}

function providerPicker(d, labels, mode) {
  const configured = d.configured_services || [];
  const seg = `
    <div class="seg-row seg-mini provider-seg">
      <button type="button" data-mode="auto" class="${mode === "auto" ? "active" : ""}">自动</button>
      <button type="button" data-mode="custom" class="${mode === "custom" ? "active" : ""}">自定义</button>
      <button type="button" data-mode="configured" class="${mode === "configured" ? "active" : ""}"
        ${configured.length ? "" : "disabled title=\"还没有配好的模型服务\""}>已配置</button>
    </div>`;
  if (mode !== "configured") return seg;
  if (!configured.length) {
    return seg + `<p class="dim small">还没有已配置的模型服务。先去「模型服务」里添加并填入 Key，再回这里选。</p>`;
  }
  const items = configured.map((s) => `
    <button type="button" class="svc-item${s.name === d.provider ? " active" : ""}" data-svc="${escapeHtml(s.name)}">
      <span class="svc-name">${escapeHtml(s.name)}</span>
      <span class="svc-meta">${escapeHtml(s.base_url || "")}${s.key_mask ? " · " + escapeHtml(s.key_mask) : ""}</span>
    </button>`).join("");
  return seg + `<div class="svc-list">${items}</div>`;
}

// 「已配置」屏点选一个服务：它本身没有独立表单，直接把 provider 存成服务名
async function pickConfiguredService(box, saveMethod, extraParams, done) {
  box.querySelectorAll(".svc-item").forEach((btn) => {
    btn.onclick = async () => {
      try {
        const params = Object.assign({ provider: btn.dataset.svc }, extraParams());
        await request(saveMethod, params);
        if (done) done();
      } catch (e) {
        addNotice("保存失败: " + e.message);
      }
    };
  });
}

async function renderWebsearchCfg() {
  let d;
  try { d = await request("websearch.get"); } catch (e) { return; }
  document.getElementById("websearch-hint").textContent = d.config_hint;
  const form = document.getElementById("websearch-form");
  // 当前档位：服务商是服务名 → 「已配置」；custom → 自定义；其余（含 auto）→ 自动
  const mode = providerModeOf("websearch-form", d);
  const labels = { auto: "自动（优先复用已配置的 Key）", custom: "自定义（自建搜索服务）" };
  const keyHint = d.key_mask && mode === "custom"
    ? "已配置（" + d.key_mask + "），留空不修改"
    : (mode === "custom" ? "可留空（自建 SearXNG 等无需鉴权）" : "粘贴服务商的 API Key");
  const detail = mode === "custom" ? `
    <div class="toolcfg-row"><label>API Key</label>
      <input type="password" data-f="key" autocomplete="new-password" placeholder="${keyHint}">
    </div>
    <div class="toolcfg-row"><label>接口地址</label>
      <input type="text" data-f="base_url" value="${escapeHtml(d.base_url || "")}"
             placeholder="自建搜索服务地址（如 http://localhost:8080 或 .../search?format=json）">
    </div>` : "";
  form.innerHTML = `
    <div class="toolcfg-row"><label>服务商</label>${providerPicker(d, labels, mode)}</div>
    ${detail}
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + (labels[d.resolved_provider] || d.resolved_provider) + "」"
        : "○ 未配置：Agent 联网搜索时会给出配置指引"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelectorAll(".provider-seg button").forEach((btn) => {
    btn.onclick = () => saveSwitch("websearch-form", "websearch.save", btn.dataset.mode);
  });
  if (mode === "configured") {
    pickConfiguredService(form, "websearch.save", () => ({}), () => {
      addNotice("已选用该服务作搜索服务商");
      renderWebsearchCfg();
    });
  }
  const saveBtn = form.querySelector('[data-act="save"]');
  if (saveBtn) saveBtn.onclick = async () => {
    const params = { provider: form.querySelector('[data-f="provider"]').value };
    const addr = form.querySelector('[data-f="base_url"]');
    if (addr) params.base_url = addr.value;
    const keyEl = form.querySelector('[data-f="key"]');
    const key = keyEl ? keyEl.value.trim() : "";
    if (key) params.api_key = key;
    try {
      await request("websearch.save", params);
      addNotice("联网搜索配置已保存并生效");
      renderWebsearchCfg();
    } catch (e) {
      addNotice("保存失败: " + e.message);
    }
  };
}

// 分段切换：自动 / 自定义 / 已配置。切到「已配置」只需重画（不写盘），
// 切到自动 / 自定义则立即保存，避免用户以为切了却没生效。
async function saveSwitch(formId, method, mode) {
  const rerender = () => {
    if (formId === "websearch-form") renderWebsearchCfg();
    else if (formId === "imagegen-form") renderImagegenCfg();
    else if (formId === "speech-form") renderSpeechCfg();
  };
  if (mode === "configured") {
    // 只是查看已配置服务列表：记住档位并重画，不动配置
    providerUiMode[formId] = "configured";
    rerender();
    return;
  }
  delete providerUiMode[formId];
  try {
    await request(method, { provider: mode === "custom" ? "custom" : "auto" });
    providerUiMode[formId] = mode;
    rerender();
  } catch (e) {
    addNotice("切换失败: " + e.message);
  }
}

async function renderImagegenCfg() {
  let d;
  try { d = await request("imagegen.get"); } catch (e) { return; }
  document.getElementById("imagegen-hint").textContent = d.config_hint;
  const form = document.getElementById("imagegen-form");
  const mode = providerModeOf("imagegen-form", d);
  const labels = { auto: "自动（优先复用已配置的 Key）", custom: "自定义 OpenAI 兼容" };
  const detail = mode === "custom" ? `
    <div class="toolcfg-row"><label>接口地址</label>
      <input type="text" data-f="base_url" value="${escapeHtml(d.base_url || "")}"
             placeholder="OpenAI 兼容 /images/generations 地址">
    </div>
    <div class="toolcfg-row"><label>API Key</label>
      <input type="password" data-f="key" autocomplete="new-password"
             placeholder="${d.has_key ? "已配置（" + d.key_mask + "），留空不修改" : "粘贴服务的 API Key"}">
    </div>
    <div class="toolcfg-row"><label>模型</label>
      <input type="text" data-f="model" value="${escapeHtml(d.model || "")}"
             placeholder="留空用服务商默认模型">
    </div>` : "";
  form.innerHTML = `
    <div class="toolcfg-row"><label>服务商</label>${providerPicker(d, labels, mode)}</div>
    ${detail}
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + (labels[d.resolved_provider] || d.resolved_provider) + " / " + d.resolved_model + "」"
        : "○ 未配置：配好任一服务的 Key 即可零配置使用"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelectorAll(".provider-seg button").forEach((btn) => {
    btn.onclick = () => saveSwitch("imagegen-form", "imagegen.save", btn.dataset.mode);
  });
  if (mode === "configured") {
    pickConfiguredService(
      form, "imagegen.save",
      () => {
        const m = form.querySelector('[data-f="model"]');
        return m && m.value.trim() ? { model: m.value.trim() } : {};
      },
      () => { addNotice("已选用该服务作画图服务商"); renderImagegenCfg(); }
    );
  }
  const saveBtn = form.querySelector('[data-act="save"]');
  if (saveBtn) saveBtn.onclick = async () => {
    const params = { provider: form.querySelector('[data-f="provider"]').value };
    for (const f of ["base_url", "model"]) {
      const el = form.querySelector(`[data-f="${f}"]`);
      if (el) params[f] = el.value;
    }
    const keyEl = form.querySelector('[data-f="key"]');
    const key = keyEl ? keyEl.value.trim() : "";
    if (key) params.api_key = key;
    try {
      await request("imagegen.save", params);
      addNotice("AI 画图配置已保存并生效");
      renderImagegenCfg();
    } catch (e) {
      addNotice("保存失败: " + e.message);
    }
  };
}

async function renderSpeechCfg() {
  let d;
  try { d = await request("speech.get"); } catch (e) { return; }
  document.getElementById("speech-hint").textContent = d.config_hint;
  const form = document.getElementById("speech-form");
  const mode = providerModeOf("speech-form", d);
  const labels = { auto: "自动（优先复用已配置的 Key）", custom: "自定义 OpenAI 兼容" };
  const detail = mode === "custom" ? `
    <div class="toolcfg-row"><label>接口地址</label>
      <input type="text" data-f="base_url" value="${escapeHtml(d.base_url || "")}"
             placeholder="OpenAI 兼容 /audio/transcriptions 地址">
    </div>
    <div class="toolcfg-row"><label>API Key</label>
      <input type="password" data-f="key" autocomplete="new-password"
             placeholder="${d.has_key ? "已配置（" + d.key_mask + "），留空不修改" : "本地服务可留空；云端服务填对应 Key"}">
    </div>
    <div class="toolcfg-row"><label>模型</label>
      <input type="text" data-f="model" value="${escapeHtml(d.model || "")}"
             placeholder="留空用服务商默认模型">
    </div>
    <div class="toolcfg-row"><label>识别语种</label>
      <input type="text" data-f="language" value="${escapeHtml(d.language || "")}"
             placeholder="zh / en / 留空自动判断">
    </div>` : "";
  form.innerHTML = `
    <div class="toolcfg-row"><label>服务商</label>${providerPicker(d, labels, mode)}</div>
    ${detail}
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + (labels[d.resolved_provider] || d.resolved_provider) + " / " + d.resolved_model + "」"
        : "○ 未配置：麦克风按钮会提示先来这里配置"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelectorAll(".provider-seg button").forEach((btn) => {
    btn.onclick = () => saveSwitch("speech-form", "speech.save", btn.dataset.mode);
  });
  if (mode === "configured") {
    pickConfiguredService(
      form, "speech.save",
      () => {
        const m = form.querySelector('[data-f="model"]');
        return m && m.value.trim() ? { model: m.value.trim() } : {};
      },
      () => { addNotice("已选用该服务作语音转写服务商"); renderSpeechCfg(); }
    );
  }
  const saveBtn = form.querySelector('[data-act="save"]');
  if (saveBtn) saveBtn.onclick = async () => {
    const params = { provider: form.querySelector('[data-f="provider"]').value };
    for (const f of ["base_url", "model", "language"]) {
      const el = form.querySelector(`[data-f="${f}"]`);
      if (el) params[f] = el.value;
    }
    const keyEl = form.querySelector('[data-f="key"]');
    const key = keyEl ? keyEl.value.trim() : "";
    if (key) params.api_key = key;
    try {
      await request("speech.save", params);
      speechStatus("✓ 已保存，麦克风按钮立即可用", true);
      renderSpeechCfg();
    } catch (e) {
      speechStatus("✗ 保存失败：" + e.message, false);
    }
  };
}

function speechStatus(text, ok = true) {
  const el = document.getElementById("speech-status");
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

// ---------- 圆桌设置：成员上限 / 超时 / 辩论轮数 / 主席出草稿 ----------
async function renderRoundtableCfg() {
  let d;
  try { d = await request("roundtable.get"); } catch (e) { return; }
  document.getElementById("roundtable-hint").textContent = d.config_hint;
  const form = document.getElementById("roundtable-form");
  if (!form) return;
  form.innerHTML = `
    <div class="toolcfg-row"><label>成员上限</label>
      <input type="number" data-f="max_members" min="1" max="8" class="num-sm" value="${d.max_members}">
      <span class="dim small">个（不含主席）</span>
    </div>
    <div class="toolcfg-row"><label>单成员超时</label>
      <input type="number" data-f="member_timeout_s" min="10" class="num-sm" value="${d.member_timeout_s}">
      <span class="dim small">秒（超过按作答失败处理，不阻断其他成员）</span>
    </div>
    <div class="toolcfg-row"><label>辩论修订</label>
      <select data-f="debate_rounds" class="sel-md">
        <option value="0">关闭 · 只独立作答</option>
        <option value="1">1 轮 · 看彼此草稿后修订</option>
        <option value="2">2 轮 · 修订两次</option>
      </select>
    </div>
    <div class="toolcfg-row"><label>成员历史上下文</label>
      <input type="number" data-f="member_history_turns" min="0" class="num-sm" value="${d.member_history_turns}">
      <span class="dim small">轮（0 = 全量；只带最近 N 轮能省不少 token，主席融合始终吃全量）</span>
    </div>
    <label class="toggle-row adv-toggle"><input type="checkbox" data-f="chair_answers"
      ${d.chair_answers ? "checked" : ""}><span>主席出草稿：当前主模型也作为成员先答一份</span></label>
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.configured_services ? "ok" : ""}">${d.configured_services
        ? `● ${d.configured_services} 个已配置 Key 的服务可作成员`
        : "○ 还没有已配置 Key 的服务：圆桌成员来自「模型服务」页配好的服务"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  const sel = form.querySelector('[data-f="debate_rounds"]');
  sel.value = String(d.debate_rounds || 0);
  // 输入框的本轮弹层默认值：用户没在弹层里手动改过时，跟随配置
  if (!localStorage.getItem("skysheep.rt.debate")) rtDebate = d.debate_rounds || 0;
  if (!localStorage.getItem("skysheep.rt.chair")) rtChair = d.chair_answers !== false;
  form.querySelector('[data-act="save"]').onclick = async () => {
    const params = {
      max_members: Number(form.querySelector('[data-f="max_members"]').value),
      member_timeout_s: Number(form.querySelector('[data-f="member_timeout_s"]').value),
      debate_rounds: Number(sel.value),
      member_history_turns: Number(form.querySelector('[data-f="member_history_turns"]').value),
      chair_answers: form.querySelector('[data-f="chair_answers"]').checked,
    };
    try {
      await request("roundtable.save", params);
      addNotice("圆桌设置已保存并生效");
      renderRoundtableCfg();
    } catch (e) {
      addNotice("保存失败: " + e.message);
    }
  };
}

// ---------- 语音输入：麦克风按钮（录音 → 转写 → 填入输入框） ----------
// 录音走浏览器 MediaRecorder（WebView2 与普通浏览器都支持，opus/webm）；
// 转写交给后端配置的服务（设置 · 语音输入）。不做 WebView2 内置 SpeechRecognition：
// 实测它在 WebView2 里能 onstart 但永远拿不到结果（无 Google 服务）。
const voiceBtn = document.getElementById("voice-btn");
let voiceRecorder = null;
let voiceChunks = [];
let voiceTimer = null;
let voiceSeconds = 0;
let voiceBusy = false;

function voiceReset(keepBusy = false) {
  if (voiceTimer) { clearInterval(voiceTimer); voiceTimer = null; }
  voiceSeconds = 0;
  if (!keepBusy) voiceBusy = false;
  if (voiceBtn) {
    voiceBtn.classList.remove("recording", "busy");
    voiceBtn.title = "语音输入：点击开始录音，再点一下结束并转成文字";
  }
}

async function voiceStart() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    addNotice("当前环境不支持录音（需要较新的浏览器内核）");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    const hint = e && e.name === "NotAllowedError"
      ? "麦克风权限被拒绝：在系统设置 · 隐私与安全 · 麦克风里允许桌面应用访问"
      : "麦克风不可用：" + (e && e.message ? e.message : e);
    addNotice(hint);
    return;
  }
  let mime = "audio/webm;codecs=opus";
  try {
    if (!MediaRecorder.isTypeSupported(mime)) {
      mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    }
  } catch (e) { mime = ""; }
  voiceChunks = [];
  try {
    voiceRecorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  } catch (e) {
    addNotice("无法开始录音：" + (e && e.message ? e.message : e));
    stream.getTracks().forEach((t) => t.stop());
    return;
  }
  voiceRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size) voiceChunks.push(e.data);
  };
  voiceRecorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    voiceUpload(voiceRecorder ? voiceRecorder.mimeType : mime);
  };
  voiceRecorder.start();
  voiceSeconds = 0;
  voiceBusy = true;
  voiceBtn.classList.add("recording");
  voiceBtn.title = "正在录音：点击结束并转成文字";
  const tick = () => {
    voiceSeconds += 1;
    voiceBtn.title = `正在录音 ${voiceSeconds}s：点击结束并转成文字`;
    if (voiceSeconds >= 120) voiceStop(); // 兜底上限，防止忘停
  };
  voiceTimer = setInterval(tick, 1000);
}

function voiceStop() {
  if (voiceTimer) { clearInterval(voiceTimer); voiceTimer = null; }
  try {
    if (voiceRecorder && voiceRecorder.state !== "inactive") voiceRecorder.stop();
  } catch (e) { /* 已停 */ }
}

async function voiceUpload(mime) {
  if (!voiceChunks.length) {
    voiceReset();
    addNotice("没有录到声音（可能麦克风静音），再试一次");
    return;
  }
  const blob = new Blob(voiceChunks, { type: mime || "audio/webm" });
  voiceChunks = [];
  voiceBtn.classList.remove("recording");
  voiceBtn.classList.add("busy");
  voiceBtn.title = "正在识别…";
  try {
    const buf = await blob.arrayBuffer();
    let bin = "";
    const bytes = new Uint8Array(buf);
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    const audio = btoa(bin);
    const r = await request("speech.transcribe", { audio, mime: blob.type });
    const input = document.getElementById("input");
    const text = (r.text || "").trim();
    if (text) {
      input.value = input.value ? input.value.replace(/\s*$/, " ") + text : text;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      input.dispatchEvent(new Event("input"));
      addNotice("已转成文字（" + r.provider + "）");
    } else {
      addNotice("没有识别到文字，再试一次");
    }
  } catch (e) {
    addNotice("语音识别失败：" + (e && e.message ? e.message : e));
  } finally {
    voiceReset();
  }
}

if (voiceBtn) {
  voiceBtn.onclick = () => {
    if (voiceBusy) {
      if (voiceRecorder && voiceRecorder.state === "recording") {
        voiceStop();
      } else {
        addNotice("正在识别上一条录音，稍等一下…");
      }
      return;
    }
    voiceStart();
  };
}

// ---------- 设置 · 子代理（独立页：基础设置 + 自定义子代理 + 内置子代理） ----------
async function renderSubagentCfg() {
  let d;
  try { d = await request("subagent.get"); } catch (e) { return; }
  const toggle = document.getElementById("subagent-toggle");
  const form = document.getElementById("subagent-form");
  toggle.checked = !!d.enabled;
  form.innerHTML = `
    <div class="toolcfg-row"><label>每个子代理最多执行轮数</label>
      <input type="number" data-f="max_iterations" min="1" max="100" step="1"
             value="${d.max_iterations}" ${d.enabled ? "" : "disabled"}>
    </div>
    <div class="toolcfg-row"><label>同时运行的分身上限</label>
      <input type="number" data-f="max_concurrent" min="1" max="8" step="1"
             value="${d.max_concurrent ?? 3}" ${d.enabled ? "" : "disabled"}>
    </div>
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.enabled ? "ok" : ""}">${d.enabled
        ? "● 已启用：Agent 会在合适的任务里派出子代理并行干活"
        : "○ 已关闭：Agent 只在主对话里逐步处理"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  toggle.onchange = () => {
    form.querySelector('[data-f="max_iterations"]').disabled = !toggle.checked;
    form.querySelector('[data-f="max_concurrent"]').disabled = !toggle.checked;
  };
  form.querySelector('[data-act="save"]').onclick = async () => {
    const maxIters = form.querySelector('[data-f="max_iterations"]').value.trim();
    const maxConc = form.querySelector('[data-f="max_concurrent"]').value.trim();
    try {
      const r = await request("subagent.save", {
        enabled: toggle.checked,
        max_iterations: maxIters === "" ? null : Number(maxIters),
        max_concurrent: maxConc === "" ? null : Number(maxConc),
      });
      subagentStatus(r.enabled
        ? `✓ 已保存：子代理已启用，最多 ${r.max_iterations} 轮 / 同时 ${r.max_concurrent} 个`
        : "✓ 已保存：子代理已关闭", true);
      renderSubagentCfg();
    } catch (e) {
      subagentStatus("✗ 保存失败：" + e.message, false);
    }
  };
  renderCustomSubagents(d);
  renderBuiltinSubagents(d);
}

function subagentStatus(text, ok = true, which = "base") {
  const el = document.getElementById(
    which === "custom" ? "custom-subagent-status"
      : which === "builtin" ? "builtin-subagent-status"
      : "subagent-status"
  );
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

function modelOptions(d, selectedProvider, selectedModel) {
  // 一个下拉同时选服务与模型：value = "provider|model"；空值 = 跟随主对话。
  // 只列出配了 Key 的服务（Ollama 等本机服务出厂即视为已配）；当前选中的服务
  // 即使未配 Key 也保留并标注，避免已有配置被静默丢弃、用户却找不到原因
  const sel = `${(selectedProvider || "").trim()}|${(selectedModel || "").trim()}`;
  let html = `<option value="">跟随主对话</option>`;
  (d.providers || []).forEach((p) => {
    if (!p.has_key && p.name !== (selectedProvider || "").trim()) return;
    const models = p.models.length ? p.models : [p.model];
    html += `<optgroup label="${escapeHtml(p.name)}${p.has_key ? "" : "（未配 Key）"}">` +
      models.map((m) => {
        const v = `${p.name}|${m}`;
        return `<option value="${escapeHtml(v)}"${v === sel ? " selected" : ""}>${escapeHtml(p.name + " · " + m)}</option>`;
      }).join("") + `</optgroup>`;
  });
  return html;
}

function parseModelValue(v) {
  const s = (v || "").trim();
  if (!s) return { provider: "", model: "" };
  const i = s.indexOf("|");
  return i < 0 ? { provider: s, model: "" } : { provider: s.slice(0, i), model: s.slice(i + 1) };
}

const SUBAGENT_TYPE_DESC = {
  task: "多步通用任务：可以拆步骤、尝试写文件（写入仍会被自动拒绝）。",
  explore: "只读调研：在代码与文件里广泛搜集信息，不改任何东西。",
  reviewer: "审查员：细读代码 / 文档，按严重度输出审查清单（不改文件）。",
  researcher: "调研员：联网搜索与抓取公开资料，结论注明来源。",
  writer: "写手：产出可直接使用的文档 / 报告 / README 成稿。",
  planner: "规划师：调研现状并拆解成分步计划（含验证方式与风险）。",
};

function renderBuiltinSubagents(d) {
  const ul = document.getElementById("builtin-subagent-list");
  ul.innerHTML = "";
  (d.builtin_display ? Object.keys(d.builtin_display) : []).forEach((type) => {
    const ov = (d.builtin || {})[type] || { provider: "", model: "", reasoning: "", description: "", prompt: "" };
    const effDesc = (d.builtin_desc || {})[type] || SUBAGENT_TYPE_DESC[type] || "";
    const customized = !!(ov.description || ov.prompt);
    const display = d.builtin_display[type] || type;
    const li = document.createElement("li");
    li.className = "subagent-row";
    li.innerHTML = `
      <div class="skill-main">
        <span class="item-name" title="${escapeHtml(display)}">${escapeHtml(display)}</span>
        <span class="tag-cell"><span class="chip">内置</span>${customized ? '<span class="chip">已定制</span>' : ""}</span>
        <span class="item-desc" title="${escapeHtml(effDesc)}">${escapeHtml(effDesc)}</span>
      </div>
      <span class="subagent-ops">
        <select data-f="model" title="这个子代理用哪个模型">${modelOptions(d, ov.provider, ov.model)}</select>
        <select data-f="reasoning" title="思考强度（留空 = 跟随全局设置）">
          <option value="">思考：跟随全局</option>
          ${(d.reasoning_efforts || []).map((r) =>
            `<option value="${r.value}"${r.value === ov.reasoning ? " selected" : ""}>思考：${escapeHtml(r.label)}</option>`).join("")}
        </select>
        <button class="btn-ghost subagent-bedit" title="编辑它的职责描述与角色提示词">编辑</button>
      </span>`;
    const save = async () => {
      const mv = parseModelValue(li.querySelector('[data-f="model"]').value);
      try {
        await request("subagent.save_builtin", {
          agent_type: type,
          provider: mv.provider,
          model: mv.model,
          reasoning: li.querySelector('[data-f="reasoning"]').value,
          description: ov.description || "",  // 行内改模型不能抹掉已编辑的说明/指令
          prompt: ov.prompt || "",
        });
        subagentStatus(`✓ 已保存「${display}」的设置`, true, "builtin");
      } catch (e) {
        subagentStatus("✗ 保存失败：" + e.message, false, "builtin");
      }
    };
    li.querySelector('[data-f="model"]').onchange = save;
    li.querySelector('[data-f="reasoning"]').onchange = save;
    li.querySelector(".subagent-bedit").onclick = () => editBuiltinModal(d, type, ov);
    ul.appendChild(li);
  });
}

function editBuiltinModal(d, type, ov) {
  const display = (d.builtin_display || {})[type] || type;
  const defDesc = (d.builtin_desc || {})[type] || SUBAGENT_TYPE_DESC[type] || "";
  const defPrompt = (d.builtin_default_prompt || {})[type] || "";
  const box = document.createElement("div");
  box.innerHTML = `
    <div class="form-grid">
      <label class="wide">职责描述（一句话说明它负责什么，会展示给 Agent；留空恢复内置默认）
        <input data-f="description" autocomplete="off" placeholder="${escapeHtml(defDesc)}"
               value="${escapeHtml(ov.description || "")}">
      </label>
      <label class="wide">角色提示词（注入它的系统提示词，写清工作方法与输出要求；留空恢复内置默认）
        <textarea data-f="prompt" rows="7" placeholder="${escapeHtml(defPrompt)}">${escapeHtml(ov.prompt || "")}</textarea>
      </label>
      <p class="dim small">占位文字是内置默认值。工具集按内置型固定（安全边界不变）；模型与思考强度在列表行里改；改完点「保存」，想回到内置默认就清空两栏再保存。</p>
    </div>`;
  const resetBtn = document.createElement("button");
  resetBtn.className = "btn-ghost";
  resetBtn.textContent = "恢复内置默认文案";
  resetBtn.onclick = () => {
    box.querySelector('[data-f="description"]').value = "";
    box.querySelector('[data-f="prompt"]').value = "";
  };
  box.querySelector(".form-grid").appendChild(resetBtn);
  showModal(`编辑内置子代理：${display}`, box, async () => {
    await request("subagent.save_builtin", {
      agent_type: type,
      provider: ov.provider || "",
      model: ov.model || "",
      reasoning: ov.reasoning || "",
      description: box.querySelector('[data-f="description"]').value.trim(),
      prompt: box.querySelector('[data-f="prompt"]').value.trim(),
    });
    subagentStatus(`✓ 已保存「${display}」的定制`, true, "builtin");
    renderSubagentCfg();
  }, "保存");
}

function renderCustomSubagents(d) {
  const ul = document.getElementById("custom-subagent-list");
  ul.innerHTML = "";
  (d.custom || []).forEach((c) => {
    const li = document.createElement("li");
    li.className = "subagent-row";
    const toolLabel = c.tools === "all" ? "全部工具"
      : c.tools === "readonly" ? "仅只读"
      : `自定义 · ${Array.isArray(c.tools) ? c.tools.length : 0} 个工具`;
    li.innerHTML = `
      <div class="skill-main">
        <span class="item-name" title="${escapeHtml(c.name)}">${escapeHtml(c.name)}</span>
        <span class="tag-cell">
          <span class="chip">${c.enabled ? "已启用" : "已停用"}</span>
          <span class="chip">${escapeHtml(toolLabel)}</span>
        </span>
        <span class="item-desc" title="${escapeHtml(c.description)}">${escapeHtml(c.description || "（没有描述）")}</span>
      </div>
      <span class="subagent-ops">
        <button class="btn-ghost subagent-edit">编辑</button>
        <button class="btn-ghost danger subagent-del">删除</button>
      </span>`;
    li.querySelector(".subagent-edit").onclick = () => subagentEditorModal(d, c);
    li.querySelector(".subagent-del").onclick = () => {
      const box = document.createElement("div");
      box.innerHTML = `<p>确定删除自定义子代理 <b>${escapeHtml(c.name)}</b> 吗？</p>
        <p class="dim small">只删除这份定义，不影响会话记录；删除后 Agent 不再派它出场。</p>`;
      showModal("删除子代理", box, async () => {
        await request("subagent.delete_custom", { name: c.name });
        subagentStatus(`✓ 已删除「${c.name}」`, true, "custom");
        renderSubagentCfg();
      }, "删除");
    };
    ul.appendChild(li);
  });
  if (!(d.custom || []).length)
    ul.innerHTML = `<li class="empty-hint">还没有自定义子代理：点右上角「＋ 新建子代理」，起个名字、写好职责、挑一套工具和模型即可。</li>`;
}

function subagentEditorModal(d, existing = null) {
  const box = document.createElement("div");
  const isEdit = !!existing;
  const policy = isEdit ? (Array.isArray(existing.tools) ? "custom" : existing.tools) : "readonly";
  const checked = isEdit && Array.isArray(existing.tools) ? existing.tools : [];
  box.innerHTML = `
    <div class="form-grid">
      <label>名称（英文标识，Agent 靠它点名）
        <input data-f="name" placeholder="repo-auditor" autocomplete="off"
               value="${isEdit ? escapeHtml(existing.name) : ""}" ${isEdit ? "disabled" : ""}>
      </label>
      <label class="wide">职责描述（一句话说明它负责什么，会展示给 Agent）
        <input data-f="description" placeholder="审计仓库结构并输出风险清单" autocomplete="off"
               value="${isEdit ? escapeHtml(existing.description) : ""}">
      </label>
      <label class="wide">专项指令（会注入它的系统提示词，写清工作方法与输出要求）
        <textarea data-f="prompt" rows="6" placeholder="1. 先列出目录结构…&#10;2. 重点检查…&#10;3. 输出一份包含文件路径的报告">${isEdit ? escapeHtml(existing.prompt) : ""}</textarea>
      </label>
      <label>工具范围
        <select data-f="policy">
          <option value="readonly"${policy === "readonly" ? " selected" : ""}>仅只读（推荐：查文件/搜索）</option>
          <option value="all"${policy === "all" ? " selected" : ""}>全部工具（不含电脑控制；写入/执行仍会被自动拒绝）</option>
          <option value="custom"${policy === "custom" ? " selected" : ""}>自定义勾选</option>
        </select>
      </label>
      <label class="wide" data-f="tools-pick" ${policy === "custom" ? "" : "hidden"}>
        <span class="dim small">勾选要给它的工具（可多选）：</span>
        <span class="tool-pick-grid">${(d.tools || []).map((t) => `
          <label class="tool-pick"><input type="checkbox" value="${escapeHtml(t.name)}"${checked.includes(t.name) ? " checked" : ""}> ${escapeHtml(t.name)}</label>`).join("")}
        </span>
      </label>
      <label>使用模型
        <select data-f="model">${modelOptions(d, existing?.provider, existing?.model)}</select>
      </label>
      <label>思考强度
        <select data-f="reasoning">
          <option value="">跟随全局</option>
          ${(d.reasoning_efforts || []).map((r) => `
            <option value="${escapeHtml(r.value)}"${isEdit && existing.reasoning === r.value ? " selected" : ""}>${escapeHtml(r.label)}</option>`).join("")}
        </select>
      </label>
      <label class="wide inline-check">
        <input data-f="enabled" type="checkbox" ${!isEdit || existing.enabled ? "checked" : ""}> 启用（关闭后保留定义但 Agent 不会派它）
      </label>
      <div class="form-status"></div>
    </div>`;
  const policySel = box.querySelector('[data-f="policy"]');
  policySel.onchange = () => {
    box.querySelector('[data-f="tools-pick"]').hidden = policySel.value !== "custom";
  };
  showModal(isEdit ? `编辑子代理「${existing.name}」` : "新建子代理", box, async () => {
    const val = (f) => box.querySelector(`[data-f="${f}"]`);
    const name = isEdit ? existing.name : val("name").value.trim();
    if (!isEdit && !name) throw new Error("请先给子代理起个名字");
    const p = policySel.value;
    const tools = p === "custom"
      ? [...box.querySelectorAll(".tool-pick input:checked")].map((x) => x.value)
      : p;
    const mv = parseModelValue(val("model").value);
    await request("subagent.save_custom", {
      name,
      description: val("description").value,
      prompt: val("prompt").value,
      tools,
      provider: mv.provider,
      model: mv.model,
      reasoning: val("reasoning").value,
      enabled: val("enabled").checked,
    });
    subagentStatus(`✓ 已保存子代理「${name}」`, true, "custom");
    renderSubagentCfg();
  }, "保存");
}

document.getElementById("btn-subagent-add").onclick = async () => {
  let d;
  try { d = await request("subagent.get"); } catch (e) { d = { tools: [] }; }
  subagentEditorModal(d, null);
};

// ---------- 设置 · 技能广场（内嵌到技能页的可折叠区块） ----------
// 把渲染逻辑抽成函数：技能广场不再是独立弹窗，而是技能页里的一个折叠块，
// 展开时才拉取索引（不展开就不发请求）。搜索/过滤/高亮逻辑与之前一致；
// 在此之上叠加：分类筛选标签、安装范围选择、手动刷新、详情预览（拉远端
// SKILL.md）、已安装/可更新标注（后端按安装来源标记与版本号并进条目）。
async function renderMarketInto(container, opts = {}) {
  container.innerHTML = '<p class="dim small">正在获取技能索引…</p>';
  let r;
  try { r = await request("skills.market", { refresh: !!opts.refresh }); } catch (e) {
    container.innerHTML = `<p>获取失败：${escapeHtml(e.message)}</p>`;
    return;
  }
  const items = r.items || [];
  // 分类筛选标签：按索引里首次出现的顺序去重（索引没写 category 时整行隐藏）
  const cats = [];
  for (const it of items) {
    const c = String(it.category || "").trim();
    if (c && !cats.includes(c)) cats.push(c);
  }
  let activeCat = "";
  container.innerHTML =
    (r.note ? `<p class="market-note">${escapeHtml(r.note)}</p>` : "") +
    (r.official === false
      ? '<p class="market-note">当前用的是第三方自建索引（SKYSHEEP_MARKET_URL）——条目非官方精选，安装前请自行确认来源可信。</p>'
      : "") +
    `<div class="market-toolbar">
      <label class="market-scope">安装到
        <select id="market-scope">
          <option value="global">全局技能</option>
          <option value="project">当前项目</option>
        </select>
      </label>
      <button id="market-refresh" class="btn-ghost" type="button"
        title="重新拉取索引：刚发布的新技能立即可见（平时有 60 秒缓存）">↻ 刷新</button>
    </div>
    <div class="market-search">
      <input id="market-q" class="modal-input" type="search" autocomplete="off"
        placeholder="搜索技能：名称、描述、作者" title="输入关键词筛选；多个关键词用空格分隔（全部命中才算匹配）；匹配范围含名称、描述、作者与地址；分类用下方标签筛">
      <button id="market-q-clear" class="btn-ghost" type="button" title="清空搜索" hidden>✕</button>
    </div>
    <div class="market-chips" id="market-chips"${cats.length ? "" : " hidden"}>
      <button class="market-chip active" type="button" data-cat="">全部</button>` +
    cats.map((c) => `<button class="market-chip" type="button" data-cat="${escapeHtml(c)}">${escapeHtml(c)}</button>`).join("") +
    `</div>
    <p class="market-count" id="market-count"></p>
    <div class="market-list" id="market-list"></div>`;

  const list = container.querySelector("#market-list");
  const countEl = container.querySelector("#market-count");
  const input = container.querySelector("#market-q");
  const clearBtn = container.querySelector("#market-q-clear");
  const scopeSel = container.querySelector("#market-scope");
  const terms = [];

  // 安装范围：没打开项目时「当前项目」不可选（后端同样会拒绝）
  if (!bootSnap || !bootSnap.working_dir) {
    scopeSel.querySelector('option[value="project"]').disabled = true;
    scopeSel.title = "没有打开的项目；技能将装进全局";
  }

  // 命中高亮：先转义再逐段包 <mark>（索引内容不可信，不能直接拼 HTML）。
  // 命中区间先合并重叠再统一包裹，避免关键词互相嵌套导致标签错乱。
  const markAll = (text, ts) => {
    text = String(text == null ? "" : text);
    if (!ts.length) return escapeHtml(text);
    const low = text.toLowerCase();
    const spans = [];
    for (const t of ts) {
      let i = 0;
      for (;;) {
        const p = low.indexOf(t, i);
        if (p < 0) break;
        spans.push([p, p + t.length]);
        i = p + t.length;
      }
    }
    if (!spans.length) return escapeHtml(text);
    spans.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const merged = [];
    for (const s of spans) {
      const last = merged[merged.length - 1];
      if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1]);
      else merged.push([s[0], s[1]]);
    }
    let out = "";
    let cursor = 0;
    for (const [a, b] of merged) {
      out += escapeHtml(text.slice(cursor, a)) +
        "<mark>" + escapeHtml(text.slice(a, b)) + "</mark>";
      cursor = b;
    }
    return out + escapeHtml(text.slice(cursor));
  };

  // 按钮文案与行为由安装状态决定：没装 → 安装；索引有新版 → 更新（覆盖重装）；
  // 已是最新 → 置灰；装过但两边都拿不到版本号 → 重装（覆盖重装）
  function installButton(it) {
    if (!it.installed) return '<button class="btn-ghost" data-mode="new">安装</button>';
    if (it.update_available) return '<button class="btn-ghost mi-update" data-mode="update">更新</button>';
    if (it.version || it.installed_version) {
      return '<button class="btn-ghost" disabled title="已安装，且不比索引上的版本旧">✓ 已安装</button>';
    }
    return '<button class="btn-ghost" data-mode="re" title="已安装过；重装会覆盖同名技能">重装</button>';
  }

  function renderItems() {
    const hit = items
      .map((it, i) => ({ it, i }))
      .filter(({ it }) => {
        if (activeCat && String(it.category || "") !== activeCat) return false;
        if (!terms.length) return true;
        const hay = [it.name, it.description, it.author, it.url]
          .map((s) => String(s == null ? "" : s).toLowerCase())
          .join(" ");
        return terms.every((t) => hay.includes(t));
      });
    // 搜索时按名称相关性排（前缀命中 > 包含命中 > 只在描述/作者/地址命中），
    // 同档保持索引原序——名字就是用户想搜的那个词的技能，应该出现在第一屏
    if (terms.length) {
      const score = (it) => {
        const name = String(it.name || "").toLowerCase();
        if (terms.some((t) => name.startsWith(t))) return 0;
        if (terms.some((t) => name.includes(t))) return 1;
        return 2;
      };
      hit.sort((a, b) => score(a.it) - score(b.it));
    }
    list.innerHTML = hit.map(({ it, i }) => {
      const meta = [it.category || "", it.version ? "v" + it.version : "", it.updated_at || ""]
        .filter(Boolean).join(" · ");
      return `
      <div class="market-item" data-i="${i}">
        <div class="mi-main">
          <span class="mi-name">${markAll(it.name, terms)}</span>
          ${meta ? `<span class="mi-meta">${escapeHtml(meta)}</span>` : ""}
          <span class="mi-desc" title="${escapeHtml(it.description || it.url)}">${markAll(it.description || it.url, terms)}</span>
        </div>
        <span class="mi-author">${markAll(it.author || "", terms)}</span>
        <span class="mi-btns">
          <button class="btn-ghost mi-detail" data-url="${escapeHtml(it.url)}" data-name="${escapeHtml(it.name || "")}">详情</button>
          ${installButton(it)}
        </span>
      </div>`;
    }).join("") ||
      (terms.length || activeCat
        ? '<p class="dim small">没有匹配的技能——换个关键词、换个分类，或点 ✕ 清空搜索看全部。</p>'
        : '<p class="dim small">索引是空的。</p>');
    countEl.textContent = !items.length
      ? ""
      : (terms.length || activeCat ? `匹配 ${hit.length} / ${items.length} 个技能` : `共 ${items.length} 个技能`);
    countEl.hidden = !countEl.textContent;

    // 详情：拉该技能 SKILL.md 原文（只读），安装前先看清楚它会让 Agent 做什么
    list.querySelectorAll(".market-item .mi-detail").forEach((btn) => {
      btn.onclick = async () => {
        const url = btn.dataset.url;
        const box = document.createElement("div");
        box.innerHTML = '<p class="dim small">正在拉取 SKILL.md …</p>';
        showModal("技能详情 · " + (btn.dataset.name || "未命名"), box, async () => {}, "关闭");
        let d;
        try { d = await request("skills.market_detail", { url }); } catch (e) {
          box.innerHTML = `<p>拉取失败：${escapeHtml(e.message)}</p>`;
          return;
        }
        box.innerHTML =
          `<p class="dim small">这是该技能 SKILL.md 的原文${d.truncated ? "（过长，已截断）" : ""}。
           安装后 Agent 平时只看名称与描述，需要时才读取下面这段指令——装之前先确认它是你愿意让它做的事。</p>
           <pre class="market-detail-pre">${escapeHtml(d.content || "（空）")}</pre>
           <p class="dim small market-detail-src" title="${escapeHtml(url)}">来源：${escapeHtml(url)}</p>`;
      };
    });
    list.querySelectorAll(".market-item button[data-mode]").forEach((btn) => {
      btn.onclick = async () => {
        const mode = btn.dataset.mode; // new | update | re
        const entry = items[+btn.closest(".market-item").dataset.i];
        const verb = mode === "new" ? "安装" : mode === "update" ? "更新" : "重装";
        btn.disabled = true;
        btn.textContent = verb + "中…";
        try {
          const res = await request("skills.install", {
            source: entry.url,
            scope: scopeSel.value,
            overwrite: mode !== "new",
          });
          skillStatus(
            `✓ 已${verb} ${res.count} 个技能（${scopeSel.value === "project" ? "当前项目" : "全局"}）：${res.installed.join("、")}`
          );
          // 就地刷新这一条的安装状态（安装结果带回技能真实版本），不重置搜索与筛选
          const names = new Set((res.installed || []).map((n) => String(n).toLowerCase()));
          const verHit = (res.skills || [])
            .find((s) => names.has(String(s.name || "").toLowerCase()) && s.version);
          entry.installed = true;
          entry.installed_version = verHit ? verHit.version : (entry.version || entry.installed_version || "");
          entry.update_available = false;
          renderItems();
          boot(); // 技能清单与系统提示词已变，后台刷新全局快照
        } catch (e) {
          btn.disabled = false;
          btn.textContent = verb;
          skillStatus("✗ " + e.message, false);
        }
      };
    });
  }

  // 多个关键词用空格分隔、全部命中才算匹配；顺序无关、大小写不敏感
  input.addEventListener("input", () => {
    terms.length = 0;
    input.value.trim().toLowerCase().split(/\s+/).filter(Boolean).forEach((t) => terms.push(t));
    clearBtn.hidden = !input.value;
    renderItems();
  });
  clearBtn.onclick = () => {
    input.value = "";
    terms.length = 0;
    clearBtn.hidden = true;
    renderItems();
    input.focus();
  };
  container.querySelector("#market-chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".market-chip");
    if (!chip) return;
    activeCat = chip.dataset.cat || "";
    container.querySelectorAll("#market-chips .market-chip")
      .forEach((b) => b.classList.toggle("active", b === chip));
    renderItems();
  });
  container.querySelector("#market-refresh").onclick = () => renderMarketInto(container, { refresh: true });
  renderItems();
  if (opts.focus) input.focus(); // 展开就聚焦，直接打关键词
}

// 折叠块：首次展开才拉索引；展开状态变化时同步右上角计数
const skillMarketFold = document.getElementById("skill-market");
skillMarketFold.addEventListener("toggle", () => {
  const body = document.getElementById("skill-market-body");
  if (skillMarketFold.open && !body.dataset.loaded) {
    body.dataset.loaded = "1";
    renderMarketInto(body, { focus: true });
  }
});

// ---------- 设置 · 技能页：列表与使用范围 ----------
let skillProjects = [];     // 最近一次拉取到的项目列表（范围勾选用）
let skillManageOpen = false; // 是否停在独立技能页

// 路径归一化：与后端 normcase 对齐（Windows 大小写不敏感）
function normPathJs(p) {
  return String(p == null ? "" : p).replace(/\//g, "\\").toLowerCase().replace(/\\+$/, "");
}

// 三态：生效中 / 本项目已停用 / 本项目不适用（范围未包含当前项目）
function skillState(s) {
  if (!s.applies) return { cls: "chip-warn", label: "本项目不适用" };
  if (!s.enabled) return { cls: "", label: "本项目已停用" };
  return { cls: "chip-blue", label: "生效中" };
}

function openSkillManage() {
  skillManageOpen = true;
  document.getElementById("skill-list-view").hidden = true;
  document.getElementById("skill-manage-view").hidden = false;
  // 范围勾选需要项目列表（切项目后路径会变，每次都重新拉）
  request("project.list")
    .then((r) => {
      skillProjects = r.projects || [];
      if (skillManageOpen && bootSnap) renderSkillList(bootSnap.skills || []);
    })
    .catch(() => {});
  if (bootSnap) {
    renderSkillList(bootSnap.skills || []);
    return;
  }
  // 极快点击（renderSettings 还没回来）：自己拉一次快照再画，避免空白页
  request("boot")
    .then((snap) => {
      bootSnap = snap;
      if (skillManageOpen) renderSkillList(snap.skills || []);
    })
    .catch((e) => addNotice("加载技能失败: " + e.message));
}

function resetSkillView() {
  skillManageOpen = false;
  document.getElementById("skill-manage-view").hidden = true;
  document.getElementById("skill-list-view").hidden = false;
  // 返回总览时收起本机候选面板：留着上次的结果容易让人以为它就是当前状态
  const local = document.getElementById("skill-local-panel");
  if (local) local.hidden = true;
}

document.getElementById("skill-summary").onclick = () => openSkillManage();
document.getElementById("btn-skill-back").onclick = () => {
  resetSkillView();
  renderSkillSummary(bootSnap ? bootSnap.skills || [] : []);
};

function renderSkillSummary(skills) {
  const el = document.getElementById("skill-summary-text");
  if (!el) return;
  const active = skills.filter((s) => s.enabled && s.applies).length;
  el.textContent = skills.length
    ? `${skills.length} 个技能 · 本项目生效 ${active} 个 — 点这里查看、设使用范围、逛技能广场`
    : "还没有技能：点右上角「＋ 导入技能」选文件夹 / 粘贴链接，或点「本机现存」查看本机已装的技能";
}

// 查看技能完整指令（停用的也能看，否则无从判断该不该启用）
async function previewSkill(name) {
  const box = document.createElement("div");
  box.innerHTML = '<p class="dim small">正在读取…</p>';
  showModal("技能指令 · " + name, box, async () => {}, "关闭");
  let r;
  try {
    r = await request("skills.body", { name });
  } catch (e) {
    box.innerHTML = `<p>读取失败：${escapeHtml(e.message)}</p>`;
    return;
  }
  box.innerHTML =
    `<p class="dim small">这是 SKILL.md 原文。Agent 平时只看得到名称与描述；
     真正需要时它才用 load_skill 读取下面这段指令。</p>` +
    `<pre class="skill-body">${escapeHtml(r.text || "")}</pre>`;
}

// 项目勾选面板：范围为「指定项目」时展开，勾选是暂存的，点「保存」才提交
function renderScopePicker(li, s) {
  const panel = document.createElement("div");
  panel.className = "scope-picker";
  const known = skillProjects.map((p) => p.root_path);
  const extra = (s.scope_projects || []).filter(
    (p) => !known.some((k) => normPathJs(k) === normPathJs(p))
  );
  const options = [
    ...skillProjects.map((p) => ({ path: p.root_path, label: p.name, current: p.is_current })),
    // 配置里引用了已不在项目列表里的路径：保留展示，避免静默丢掉用户设置
    ...extra.map((p) => ({ path: p, label: p, missing: true })),
  ];
  if (!options.length) {
    panel.innerHTML = '<span class="dim small">还没有其他项目——先在侧栏切换/新建一个项目，它就会出现在这里。</span>';
    return panel;
  }
  const chosen = new Set((s.scope_projects || []).map(normPathJs));
  panel.innerHTML =
    `<div class="scope-picker-title">这个技能在哪些项目里可用：</div>` +
    options
      .map((o) => {
        const on = chosen.has(normPathJs(o.path));
        const badge = o.current ? '<span class="chip chip-blue">当前</span>' : "";
        const miss = o.missing ? '<span class="chip chip-warn">路径已不存在</span>' : "";
        return `<label class="scope-opt">
          <input type="checkbox" value="${escapeHtml(o.path)}"${on ? " checked" : ""}>
          <span class="scope-opt-name">${escapeHtml(o.label)}</span>${badge}${miss}
        </label>`;
      })
      .join("") +
    `<div class="scope-picker-ops">
      <button class="btn-ghost scope-save">保存范围</button>
      <span class="scope-msg dim small"></span>
    </div>`;
  const msg = panel.querySelector(".scope-msg");
  panel.querySelector(".scope-save").onclick = async () => {
    const picked = [...panel.querySelectorAll("input:checked")].map((i) => i.value);
    if (!picked.length) {
      msg.textContent = "至少要勾选一个项目；一个都不想用的话请选「任何项目都不用」。";
      msg.className = "scope-msg bad";
      return;
    }
    msg.textContent = "保存中…";
    msg.className = "scope-msg dim small";
    try {
      await request("skills.scope", { name: s.name, mode: "projects", projects: picked });
      skillStatus(`✓ 「${s.name}」的使用范围已更新（指定 ${picked.length} 个项目）`);
      boot();
      await renderSettings();
    } catch (e) {
      msg.textContent = "✗ " + e.message;
      msg.className = "scope-msg bad";
    }
  };
  return panel;
}

function renderSkillList(skills) {
  const sul = document.getElementById("settings-skill-list");
  if (!sul) return;
  sul.innerHTML = "";
  // 批量删除工具条：全选 / 删除所选。勾选状态不跨渲染保留（重渲染即清零）。
  let syncPicks = null;
  if (skills.length) {
    const bar = document.createElement("li");
    bar.className = "skill-toolbar";
    bar.innerHTML = `
      <label class="pick-all"><input type="checkbox"> 全选</label>
      <button class="btn-ghost danger" disabled>删除所选</button>
      <span class="dim small"></span>`;
    const pickAll = bar.querySelector(".pick-all input");
    const delBtn = bar.querySelector("button");
    const note = bar.querySelector("span.dim");
    const picked = () => [...sul.querySelectorAll(".skill-pick:checked")];
    syncPicks = () => {
      const all = [...sul.querySelectorAll(".skill-pick")];
      const boxes = picked();
      delBtn.disabled = !boxes.length;
      delBtn.textContent = boxes.length ? `删除所选（${boxes.length}）` : "删除所选";
      note.textContent = boxes.length ? "" : "勾选要删除的技能，可一次删多个";
      pickAll.checked = all.length > 0 && boxes.length === all.length;
    };
    pickAll.onchange = () => {
      sul.querySelectorAll(".skill-pick").forEach((cb) => { cb.checked = pickAll.checked; });
      syncPicks();
    };
    delBtn.onclick = () => batchDeleteSkillsModal(picked().map((cb) => cb.dataset.name));
    sul.appendChild(bar);
  }
  skills.forEach((s) => {
    const st = skillState(s);
    const li = document.createElement("li");
    li.className = "skill-row";
    li.innerHTML =
      `<div class="skill-top">
        <input type="checkbox" class="skill-pick" data-name="${escapeHtml(s.name)}" title="勾选以批量删除">
        <button class="skill-name" type="button" title="查看 SKILL.md 完整指令">${escapeHtml(s.name)}</button>
        <span class="chip">${s.source === "project" ? "本项目" : "全局"}</span>
        <span class="chip ${st.cls}">${st.label}</span>
        <span class="skill-del-cell"></span>
      </div>
      <div class="skill-bottom">
        <span class="skill-desc" title="${escapeHtml(s.description)}">${escapeHtml(s.description)}</span>
        <span class="skill-ops"></span>
      </div>`;
    li.querySelector(".skill-pick").onchange = () => syncPicks && syncPicks();
    li.querySelector(".skill-name").onclick = () => previewSkill(s.name);

    const ops = li.querySelector(".skill-ops");
    if (s.source === "project") {
      ops.innerHTML = '<span class="dim small" title="项目技能只在本项目生效">仅本项目</span>';
    } else {
      const sel = document.createElement("select");
      sel.className = "skill-scope";
      sel.title = "这个技能在哪些项目里可用";
      sel.innerHTML =
        '<option value="all">所有项目</option>' +
        '<option value="projects">指定项目…</option>' +
        '<option value="none">任何项目都不用</option>';
      sel.value = s.scope === "projects" || s.scope === "none" ? s.scope : "all";
      sel.onchange = async () => {
        if (sel.value === "projects") {
          // 指定项目：先展开勾选面板，不立即提交（后端要求至少一个项目）
          const old = li.querySelector(".scope-picker");
          if (old) old.remove();
          li.appendChild(renderScopePicker(li, s));
          return;
        }
        const old = li.querySelector(".scope-picker");
        if (old) old.remove();
        try {
          await request("skills.scope", { name: s.name, mode: sel.value, projects: [] });
          skillStatus(
            sel.value === "none"
              ? `✓ 「${s.name}」已设为任何项目都不使用`
              : `✓ 「${s.name}」已设为所有项目可用`
          );
          boot();
          await renderSettings();
        } catch (e) {
          skillStatus("✗ " + e.message, false);
          await renderSettings();
        }
      };
      ops.appendChild(sel);
    }

    // 本项目开关：范围不适用时不显示（此时它没有意义，只会让人以为开关坏了）
    if (s.applies) {
      const toggle = document.createElement("button");
      toggle.className = "btn-ghost skill-toggle";
      toggle.textContent = s.enabled ? "🟢 已启用" : "⚪ 已停用";
      toggle.title = "只影响当前项目";
      toggle.onclick = async () => {
        toggle.disabled = true;
        try {
          await request("skills.toggle", { name: s.name, enabled: !s.enabled });
        } catch (e) {
          toggle.disabled = false;
          toggle.textContent = "✗ 切换失败";
          toggle.title = e.message;
          return;
        }
        boot();
        await renderSettings();
      };
      ops.appendChild(toggle);
    } else {
      const hint = document.createElement("span");
      hint.className = "dim small";
      hint.textContent = "本项目不适用";
      hint.title = "当前项目的路径不在这个技能的使用范围里；改上面的范围即可让它在这里生效";
      ops.appendChild(hint);
    }

    const del = document.createElement("button");
    del.className = "btn-ghost danger skill-del";
    del.textContent = "删除";
    del.title = "删除这个技能（从磁盘移除）";
    del.onclick = () => deleteSkillModal(s);
    li.querySelector(".skill-del-cell").appendChild(del);

    // 范围已是「指定项目」时，直接把勾选面板摊开（不用再点一次下拉）
    if (s.source === "global" && s.scope === "projects") {
      li.appendChild(renderScopePicker(li, s));
    }
    sul.appendChild(li);
  });
  if (!skills.length)
    sul.innerHTML = '<li class="empty-hint">还没有技能：点右上角「＋ 导入技能」选文件夹 / 粘贴链接，或点「本机现存」查看本机已装的技能</li>';
}

// ---------- 设置 · 关于：更新检查 ----------
const REPO_PAGE = "https://github.com/Sky-scrape/SkySheep";
const RELEASES_PAGE = REPO_PAGE + "/releases";
function renderUpdatePanel(snap) {
  const box = document.getElementById("about-update");
  if (!box) return;
  const cur = snap.version || "";
  const frozen = !!snap.frozen; // 安装版=可在应用内静默更新；源码版提示 git pull
  const edition = frozen ? "安装版" : "源码版"; // 排障时的关键信息，随版本号一起亮出来
  const upd = snap.update || null;
  box.innerHTML = `
    <button class="btn-ghost" id="btn-check-update">检查更新</button>
    <span class="upd-state ${upd ? "new" : ""}" id="upd-state">${
      upd
        ? `🆕 发现新版本 v${escapeHtml(upd.version)}（当前 v${escapeHtml(cur)} · ${edition}）`
        : `当前版本 v${escapeHtml(cur)} · ${edition}`
    }</span>
    <span id="upd-actions"></span>`;
  const state = box.querySelector("#upd-state");
  const actions = box.querySelector("#upd-actions");

  // 一键更新：下载最新安装包 → 退出并静默安装（仅安装版；源码版提示 git pull）
  const setUpdButtons = (version) => {
    actions.innerHTML = "";
    const link = `<a href="${RELEASES_PAGE}" target="_blank">前往下载页</a>`;
    if (!frozen) {
      actions.innerHTML = `<span class="dim small">源码版请在仓库里 git pull 后重启更新，或${link}</span>`;
      return;
    }
    const b = document.createElement("button");
    b.className = "btn-ghost";
    b.textContent = `⬇ 一键更新到 v${escapeHtml(version)}`;
    b.onclick = async () => {
      b.disabled = true;
      state.textContent = "正在下载更新包…";
      try {
        const r = await request("app.install_update");
        if (!r.update_available) {
          state.textContent = `已是最新版本（v${escapeHtml(r.current)}）`;
          actions.innerHTML = "";
          return;
        }
        state.textContent = "下载完成。点「退出并安装」后应用会自动退出并静默安装，装完自动重新打开。若弹出「用户账户控制」提示请点「是」。";
        const b2 = document.createElement("button");
        b2.className = "btn-ghost";
        b2.textContent = "退出并安装";
        b2.onclick = async () => {
          b2.disabled = true;
          state.textContent = "正在退出并启动安装程序…装完会自动重新打开 SkySheep。";
          try {
            await request("app.apply_update");
          } catch (e) {
            state.textContent = "启动安装失败：" + e.message;
            b2.disabled = false;
          }
        };
        actions.innerHTML = "";
        actions.appendChild(b2);
      } catch (e) {
        state.textContent = "更新失败：" + e.message;
        b.disabled = false;
      }
    };
    actions.appendChild(b);
    actions.insertAdjacentHTML("beforeend", `<span class="dim small">或${link}</span>`);
  };

  if (upd && upd.version) setUpdButtons(upd.version);

  box.querySelector("#btn-check-update").onclick = async () => {
    state.textContent = "正在检查更新…";
    state.className = "upd-state";
    actions.innerHTML = "";
    try {
      const r = await request("app.check_update");
      if (r.available) {
        state.innerHTML = `🆕 发现新版本 v${escapeHtml(r.version)}（当前 v${escapeHtml(r.current)} · ${edition}）`;
        state.className = "upd-state new";
        setUpdButtons(r.version);
      } else if (r.error) {
        state.textContent = "检查失败：" + r.error;
      } else {
        state.textContent = `已是最新版本（v${escapeHtml(r.current)} · ${edition}）`;
      }
    } catch (e) {
      state.textContent = "检查失败：" + e.message;
    }
  };
}

// ---------- 窄屏适配：侧栏抽屉开关（局域网手机访问用） ----------
// 窄屏互斥：两个抽屉（侧栏 / 右面板）同时开着会把屏幕盖满、彼此挡住收回入口——
// 所以开其中一个时自动收起另一个。
function openSidebarDrawer() {
  const wasOpen = document.body.classList.contains("sidebar-open");
  document.body.classList.toggle("sidebar-open");
  if (!wasOpen && NARROW_MQ.matches && rightTabs.length && !rightCollapsed) {
    rightCollapsed = true;
    renderRightPanel();
  }
}
const btnMenu = document.createElement("button");
btnMenu.id = "btn-menu";
btnMenu.className = "tb-icon";
btnMenu.title = "打开菜单";
btnMenu.textContent = "☰";
btnMenu.onclick = openSidebarDrawer;
document.getElementById("topbar").prepend(btnMenu);
// 设置页的抽屉开关：设置模式隐藏整个 #view-chat，对话区顶栏的 ☰ 一起消失——
// 手机上没有它就无法唤出侧栏，而设置子页导航与「← 返回对话」全在侧栏里（进去就出不来）
const btnMenuSettings = document.createElement("button");
btnMenuSettings.id = "btn-menu-settings";
btnMenuSettings.className = "tb-icon";
btnMenuSettings.title = "打开菜单";
btnMenuSettings.textContent = "☰";
btnMenuSettings.onclick = openSidebarDrawer;
document.getElementById("settings-topbar").prepend(btnMenuSettings);
// 抽屉背衬：抽屉打开时盖住其余区域（暗色），点它 = 收回所有抽屉回到对话。
// 这样无论抽屉多宽，屏幕上总有可点的地方能出去。
const drawerBackdrop = document.createElement("div");
drawerBackdrop.id = "drawer-backdrop";
drawerBackdrop.onclick = () => {
  document.body.classList.remove("sidebar-open");
  if (NARROW_MQ.matches && rightTabs.length && !rightCollapsed) {
    rightCollapsed = true;
    renderRightPanel();
  }
};
document.body.appendChild(drawerBackdrop);
// 侧栏自带的关闭钮：有的内核把抽屉渲染得过宽时背衬可能被挤没，得有内部出口
const sidebarClose = document.createElement("button");
sidebarClose.id = "sidebar-close";
sidebarClose.title = "收起菜单";
sidebarClose.textContent = "✕";
sidebarClose.onclick = () => document.body.classList.remove("sidebar-open");
document.getElementById("sidebar").appendChild(sidebarClose);
document.getElementById("chat").addEventListener("click", () => {
  if (document.body.classList.contains("sidebar-open")) {
    document.body.classList.remove("sidebar-open");
  }
  // 窄屏上右面板是覆盖抽屉：点一下对话区就收回（抽屉打开时顶栏开关被它压着）
  if (NARROW_MQ.matches && !rightCollapsed && rightTabs.length) {
    rightCollapsed = true;
    renderRightPanel();
  }
});

connect();
boot();
// 必须在 connect() 之后：ws 尚未创建时 request() 会抛错（被 catch 吞掉、偏好悄悄不生效）
initUiPrefs();

// ---------- 分级权限模式：安全执行 / 自动编辑 / 完全访问（对标 Codex / Claude Code 三档） ----------
// 点击循环切换：安全执行（写入/执行都征询）→ 自动编辑（工作目录内写入放行，命令仍征询）
// → 完全访问（写入与命令都不再征询）→ 回到安全执行。档位存 ui.json 的 accept_edits
// （0/1/2），自动编辑只对工作目录内的写入生效（目录外仍逐次确认）。
let acceptMode = "confirm"; // 当前档位，与后端 permission.mode 同步
const ACCEPT_ICONS = {
  confirm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  accept_edits: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15.5 4.5l4 4L8.5 19.5l-5 1.2 1.2-5L15.5 4.5z"/><path d="M13.5 6.5l4 4"/></svg>',
  full_access: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 7.9-1"/></svg>',
};
const ACCEPT_TITLE = {
  confirm: "当前：安全执行 · 每次写入/执行都会征询。点击切到「自动编辑」（工作目录内写入放行）",
  accept_edits: "当前：自动编辑 · 工作目录内的文件写入自动放行，执行命令仍会征询。点击切到「完全访问」",
  full_access: "当前：完全访问 · 写入与命令执行都自动放行。点击切回「安全执行」",
};
const ACCEPT_NOTICE = {
  confirm: "已切到安全执行模式：所有写入/执行都会先征询",
  accept_edits: "✎ 已切到自动编辑模式：工作目录内的文件写入不再逐次确认，目录外写入与执行命令仍会征询",
  full_access: "🔓 已切到完全访问模式：写入与命令执行都不再逐次确认，请确保当前任务可信",
};
function renderAcceptSwitch(mode) {
  const b = document.getElementById("accept-switch");
  if (!b) return;
  acceptMode = ACCEPT_ICONS[mode] ? mode : "confirm";
  b.innerHTML = ACCEPT_ICONS[acceptMode];
  b.title = ACCEPT_TITLE[acceptMode];
  b.classList.toggle("on", acceptMode === "accept_edits");
  b.classList.toggle("full", acceptMode === "full_access");
}
document.getElementById("accept-switch").onclick = async () => {
  const order = ["confirm", "accept_edits", "full_access"];
  const next = order[(order.indexOf(acceptMode) + 1) % order.length];
  try {
    const r = await request("permission.set_mode", { mode: next });
    renderAcceptSwitch(r.mode);
    addNotice(ACCEPT_NOTICE[r.mode] || ACCEPT_NOTICE.confirm);
  } catch (e) {
    addNotice("切换失败: " + e.message);
  }
};

// ---------- 添加文件：原生选择框选本机任意文件（含项目外、可多选） ----------
// 拖拽进窗口只能拿到文件名（浏览器 Drop 事件不给真实路径），项目外的文件连
// 索引都匹配不上；原生对话框能拿到真实路径，是「拖进来没反应」的兜底通道。
const IMAGE_EXT_RE = /\.(png|jpe?g|webp|gif|bmp)$/i;

/** 在光标处插入文本（前后补空格，避免和已有内容粘连）。 */
function insertAtCursor(text) {
  const cur = inputEl.value;
  const start = inputEl.selectionStart == null ? cur.length : inputEl.selectionStart;
  const end = inputEl.selectionEnd == null ? start : inputEl.selectionEnd;
  const payload = (start > 0 && !/\s/.test(cur[start - 1]) ? " " : "") + text + " ";
  inputEl.setRangeText(payload, start, end, "end");
  autoGrowInput();
  inputEl.focus();
}

function manualFilePrompt() {
  const box = document.createElement("div");
  box.innerHTML = `<div class="cron-fields">
      <label>文件绝对路径</label>
      <input id="manual-file-path" class="modal-input" type="text"
             placeholder="例如 D:\\文档\\季度报告.pdf">
      <p class="dim small">浏览器模式弹不出系统选择框，直接粘贴路径即可（桌面窗口里点「添加文件」可直接选）。</p>
    </div>`;
  showModal("添加文件", box, async () => {
    const v = box.querySelector("#manual-file-path").value.trim().replace(/^@+/, "");
    if (!v) throw new Error("请填写文件路径");
    insertAtCursor("@" + v);
  }, "添加");
}

async function attachPickFiles() {
  const r = await pickPath("file");
  if (r.error === "no-picker") { manualFilePrompt(); return; }
  if (r.error) { addNotice("打开文件选择框失败：" + r.error); return; }
  const paths = (r.paths || []).filter(Boolean);
  if (!paths.length) return;
  insertAtCursor(paths.map((p) => "@" + p).join(" "));
  const imgs = paths.filter((p) => IMAGE_EXT_RE.test(p));
  if (imgs.length) {
    addNotice(`已引用 ${paths.length} 个文件路径。图片这里只作「文件」引用——` +
      "要让模型直接看到图片内容，请把它粘贴或拖进输入框。");
  } else {
    addNotice(`已引用 ${paths.length} 个文件（项目外的文件也可以）`);
  }
}

document.getElementById("file-attach").onclick = () => { attachPickFiles(); };

// ---------- 设置 · 高级：Hooks 钩子面板 ----------
// 规则状态存在内存里，编辑后点「保存」一次性提交（与运行参数同一交互习惯）
let hooksState = { pre: [], post: [], stop: [], tool_names: [] };
const HOOK_KIND_LABEL = { pre: "调用前", post: "调用后", stop: "任务完成" };

function hooksStatus(text, ok = true) {
  const el = document.getElementById("hooks-status");
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

// 工具名候选：来自后端实际注册的工具清单，写 match 时不用凭记忆
function hookToolOptions() {
  return hooksState.tool_names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
}

function fillHookToolDatalist() {
  const dl = document.getElementById("hook-tools");
  if (dl) dl.innerHTML = hookToolOptions();
}

function renderHooksList(kind) {
  const box = document.getElementById(`hooks-${kind}-list`);
  if (!box) return;
  const items = hooksState[kind] || [];
  if (!items.length) {
    box.innerHTML = `<div class="rp-empty">还没有${HOOK_KIND_LABEL[kind] || kind}钩子。</div>`;
    return;
  }
  box.innerHTML = "";
  items.forEach((rule, i) => {
    const row = document.createElement("div");
    row.className = "hook-row";
    // stop 钩子不按工具过滤，没有 match 输入；每条钩子带启停开关（停用保留配置）
    const matchField = kind === "stop"
      ? `<span class="hook-match-static dim small" title="任务完成（最终回答、不再调工具）时触发，不按工具过滤">最终回答结束时</span>`
      : `<input class="hook-match" list="hook-tools" value="${escapeHtml(rule.match || "*")}"
          placeholder="*" title="工具名通配，如 write_file / run_command / *">`;
    row.innerHTML = `
      <label class="hook-enable" title="停用后保留配置，不再执行；重新勾选即恢复">
        <input type="checkbox" class="hook-enabled" ${rule.enabled === false ? "" : "checked"}>启用
      </label>
      ${matchField}
      <input class="hook-cmd" value="${escapeHtml(rule.command || "")}"
        placeholder="python check.py" title="要执行的命令（Windows 走 cmd，其它平台走 bash）">
      <input class="hook-timeout" type="number" min="1" max="600" step="1"
        value="${Number(rule.timeout_s) || 10}" title="超时秒数（1–600）">
      <button class="btn-ghost rp-mini" title="删除这条钩子">✕</button>`;
    if (kind !== "stop") {
      row.querySelector(".hook-match").oninput = (e) => { rule.match = e.target.value.trim() || "*"; };
    }
    row.querySelector(".hook-enabled").onchange = (e) => { rule.enabled = e.target.checked; };
    row.querySelector(".hook-cmd").oninput = (e) => { rule.command = e.target.value; };
    row.querySelector(".hook-timeout").oninput = (e) => { rule.timeout_s = Number(e.target.value) || 10; };
    row.querySelector("button").onclick = () => {
      hooksState[kind].splice(i, 1);
      renderHooksList(kind);
    };
    box.appendChild(row);
  });
}

function addHookRule(kind) {
  hooksState[kind].push(kind === "stop"
    ? { command: "", timeout_s: 10, enabled: true }
    : { match: "*", command: "", timeout_s: 10, enabled: true });
  renderHooksList(kind);
  const box = document.getElementById(`hooks-${kind}-list`);
  const last = box.querySelector(".hook-row:last-child .hook-cmd");
  if (last) last.focus();
}

async function renderHooksCfg() {
  renderHooksList("pre");
  renderHooksList("post");
  renderHooksList("stop");
  try {
    const d = await request("hooks.get");
    hooksState = {
      pre: d.pre || [], post: d.post || [], stop: d.stop || [],
      tool_names: d.tool_names || [],
    };
    renderHooksList("pre");
    renderHooksList("post");
    renderHooksList("stop");
    renderHooksCounts(d.active_pre, d.active_post, d.active_stop);
    renderHookRecentRuns(d.recent || []);
    const pathEl = document.getElementById("hooks-config-path");
    if (pathEl) pathEl.textContent = `存于配置文件：${d.config_path || ""}`;
    fillHookToolDatalist();
    hooksStatus("");
  } catch (e) {
    hooksStatus("加载失败：" + e.message, false);
  }
}

function renderHooksCounts(activePre, activePost, activeStop) {
  const parts = [
    ["hooks-active-pre", activePre],
    ["hooks-active-post", activePost],
    ["hooks-active-stop", activeStop],
  ];
  for (const [id, n] of parts) {
    const el = document.getElementById(id);
    if (el) el.textContent = n ? `已生效 ${n} 条` : "未配置";
  }
}

// 最近执行（含钩子自身故障）：非 0 非 2 退出码按设计放行，但必须看得见
function renderHookRecentRuns(runs) {
  const ul = document.getElementById("hooks-recent-list");
  if (!ul) return;
  if (!runs.length) {
    ul.innerHTML = '<li class="dim small">还没有执行记录（配置钩子并在对话中触发后出现在这里）</li>';
    return;
  }
  const statusMeta = {
    ok: ["✓ 正常", "safe-mark"],
    blocked: ["⛔ 阻止", "write-mark"],
    error: ["✗ 故障", "danger-mark"],
  };
  ul.innerHTML = runs.slice().reverse().map((r) => {
    const [label, cls] = statusMeta[r.status] || [r.status || "?", ""];
    const when = new Date((r.time || 0) * 1000).toLocaleTimeString();
    const head = `${escapeHtml(HOOK_KIND_LABEL[r.kind] || r.kind)} · ${escapeHtml(r.tool || "—")}`;
    const out = r.output ? escapeHtml(r.output) : "";
    return `<li class="hook-run st-${escapeHtml(r.status || "ok")}">
      <span class="chip ${cls}">${label}</span>
      <span class="hook-run-head" title="${head}">${head}</span>
      <span class="hook-run-out" title="${out}">${out}</span>
      <span class="dim small hook-run-meta">${when} · ${Number(r.code) || 0} · ${Number(r.duration_ms) || 0}ms</span>
    </li>`;
  }).join("");
}

async function saveHooks() {
  // 前端先拦一道：空命令会让保存直接失败，不如就地指出是哪一行
  for (const kind of ["pre", "post", "stop"]) {
    const bad = (hooksState[kind] || []).findIndex((r) => !String(r.command || "").trim());
    if (bad >= 0) {
      hooksStatus(`✗ ${HOOK_KIND_LABEL[kind]}第 ${bad + 1} 条钩子还没填命令`, false);
      return;
    }
  }
  try {
    const d = await request("hooks.save", {
      pre: hooksState.pre, post: hooksState.post, stop: hooksState.stop,
    });
    hooksState = {
      pre: d.pre || [], post: d.post || [], stop: d.stop || [],
      tool_names: d.tool_names || hooksState.tool_names,
    };
    renderHooksList("pre");
    renderHooksList("post");
    renderHooksList("stop");
    // 生效计数也要跟着刷新，否则刚保存完还显示「未配置」，看起来像没生效
    renderHooksCounts(d.active_pre, d.active_post, d.active_stop);
    renderHookRecentRuns(d.recent || []);
    hooksStatus(`✓ 已保存并立即生效（前 ${d.active_pre} / 后 ${d.active_post} / 完成 ${d.active_stop}）`);
  } catch (e) {
    hooksStatus("✗ 保存失败：" + e.message, false);
  }
}

document.getElementById("btn-hook-add-pre").onclick = () => addHookRule("pre");
document.getElementById("btn-hook-add-post").onclick = () => addHookRule("post");
document.getElementById("btn-hook-add-stop").onclick = () => addHookRule("stop");
document.getElementById("btn-hooks-save").onclick = () => saveHooks();

// 钩子测试器：拿示例参数把命令实跑一遍（不保存配置、不进执行记录）
const hookTestRun = async () => {
  const kind = document.getElementById("hook-test-kind").value;
  const tool = document.getElementById("hook-test-tool").value.trim();
  const command = document.getElementById("hook-test-cmd").value.trim();
  const timeout = Number(document.getElementById("hook-test-timeout").value) || 10;
  const out = document.getElementById("hook-test-result");
  const show = (text, cls) => {
    out.textContent = text;
    out.className = "rule-check-result " + (cls || "");
    out.hidden = false;
  };
  if (!command) return show("请先填写要测试的命令", "warn");
  if (kind !== "stop" && !tool) return show("请填写工具名（如 write_file）", "warn");
  let input = {};
  const raw = document.getElementById("hook-test-input").value.trim();
  if (raw) {
    try {
      input = JSON.parse(raw);
    } catch (e) {
      return show("参数不是合法 JSON：" + e.message, "warn");
    }
  }
  show("运行中…", "");
  try {
    const r = await request("hooks.test", { kind, tool, command, timeout_s: timeout, input });
    const verdict = kind === "pre"
      ? (r.blocked ? "⛔ 这次调用会被阻止" : "✓ 这次调用会放行")
      : "✓ 已执行（仅通知，不影响任务）";
    const detail = [
      `${verdict}（退出码 ${r.code}，耗时 ${r.duration_ms}ms）`,
      r.stdout ? `stdout：${r.stdout}` : "",
      r.stderr ? `stderr：${r.stderr}` : "",
    ].filter(Boolean).join("\n");
    show(detail, r.blocked ? "warn" : "ok");
  } catch (err) {
    show("测试失败：" + err.message, "warn");
  }
};
document.getElementById("btn-hook-test").onclick = hookTestRun;
// stop 类型没有工具名/参数：藏起不相干输入
document.getElementById("hook-test-kind").onchange = (e) => {
  const isStop = e.target.value === "stop";
  document.getElementById("hook-test-tool").style.display = isStop ? "none" : "";
  document.getElementById("hook-test-input-row").style.display = isStop ? "none" : "";
};

// ---------- 设置 · 高级：运行参数 / 目录限制 / 开机自启 ----------
function advancedStatus(text, ok = true) {
  const el = document.getElementById("advanced-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

// 加载时记下的热键：保存后判断是否变了——热键要重启才生效，提示不能撒谎
let advancedLoadedHotkey = "";

async function renderAdvancedCfg() {
  let d;
  try { d = await request("advanced.get"); } catch (e) {
    advancedStatus("加载失败：" + e.message, false);
    return;
  }
  const q = (id) => document.getElementById(id);
  q("adv-max-iterations").value = d.max_iterations;
  q("adv-context-limit").value = d.context_limit_tokens;
  q("adv-keep-recent").value = d.compaction_keep_recent;
  // 触发比例以百分数编辑（50–98），存的是 0.5–0.98 的比例
  q("adv-compaction-trigger").value = Math.round((d.compaction_trigger ?? 0.9) * 100);
  q("adv-restrict-workdir").checked = !!d.restrict_to_workdir;
  renderTrustState();
  renderTrustList();
  q("adv-daily-budget").value = d.daily_token_budget || "";
  // 全局唤起热键（仅 Windows 桌面版生效；其它平台置灰提示）
  const hk = q("adv-hotkey");
  advancedLoadedHotkey = d.hotkey || "";
  if (hk) {
    hk.value = d.hotkey || "";
    hk.placeholder = d.hotkey || "Ctrl+Alt+Space";
    hk.disabled = !d.hotkey_supported;
  }
  const hint = q("adv-context-hint");
  const src = d.current_provider_context_limit
    ? `当前服务「${d.current_provider}」自带设置 ${d.current_provider_context_limit.toLocaleString()}`
    : "当前服务没单独设置，用这个全局值";
  hint.textContent = `默认 1000000。${src}；当前实际生效 ${d.context_limit_tokens_effective.toLocaleString()} tokens`;
  // 开机自启（仅 Windows 桌面）
  const auto = q("adv-autostart");
  const autoHint = q("adv-autostart-hint");
  const st = d.autostart || {};
  auto.disabled = !st.supported;
  auto.checked = !!st.enabled;
  if (!st.supported) {
    autoHint.textContent = "当前系统不支持（仅 Windows 桌面版可用）";
  } else if (st.error) {
    autoHint.textContent = "读取注册表失败：" + st.error;
  } else if (st.enabled && st.stale) {
    autoHint.textContent = `已开启，但指向的是旧路径（可能换过安装位置）：${st.current}。重新保存一次即可更新。`;
  } else if (st.enabled) {
    autoHint.textContent = `已开启：开机后自动在后台启动（关闭窗口时选「缩到系统托盘」即持续运行，定时任务才会按点触发）。`;
  } else {
    autoHint.textContent = "开启后开机自动启动，定时任务与日程提醒才能真正无人值守";
  }
  advancedStatus("");
}

async function saveAdvanced() {
  const q = (id) => document.getElementById(id);
  const newHotkey = (q("adv-hotkey").value || "").trim();
  const oldHotkey = advancedLoadedHotkey; // renderAdvancedCfg 会刷新它，先记旧值
  try {
    const r = await request("advanced.save", {
      max_iterations: Number(q("adv-max-iterations").value),
      context_limit_tokens: Number(q("adv-context-limit").value),
      compaction_keep_recent: Number(q("adv-keep-recent").value),
      compaction_trigger: Number(q("adv-compaction-trigger").value) / 100,
      daily_token_budget: Number(q("adv-daily-budget").value) || 0,
      restrict_to_workdir: q("adv-restrict-workdir").checked,
      autostart: q("adv-autostart").checked,
      hotkey: newHotkey,
    });
    // 先重渲染（会把状态行清掉）再写成功提示，否则"✓ 已保存"一闪就没
    await renderAdvancedCfg();
    // 热键随窗口创建注册，改键要重启才生效——与「已立即生效」分开说，别误导
    advancedStatus(newHotkey !== oldHotkey
      ? "✓ 已保存；全局唤起热键在重启应用后生效"
      : "✓ 已保存并立即生效");
    const st = r.autostart || {};
    if (q("adv-autostart").checked && st.enabled) {
      addNotice("✓ 已设置开机自启；定时任务从此可以无人值守");
    }
  } catch (e) {
    advancedStatus("✗ 保存失败：" + e.message, false);
  }
}
document.getElementById("btn-advanced-save").onclick = saveAdvanced;

// 恢复默认：填回默认值并立即保存（40 轮 / 1,000,000 / 保留 8 / 触发 90% / 预算不限）
document.getElementById("btn-advanced-reset").onclick = async () => {
  const q = (id) => document.getElementById(id);
  q("adv-max-iterations").value = 40;
  q("adv-context-limit").value = 1000000;
  q("adv-keep-recent").value = 8;
  q("adv-compaction-trigger").value = 90;
  q("adv-daily-budget").value = "";
  await saveAdvanced();
};

// 已信任项目清单：查看 + 按路径撤销（撤当前项目会同步断开其 MCP、重载技能）
async function renderTrustList() {
  const ul = document.getElementById("trust-list");
  if (!ul) return;
  let d;
  try { d = await request("trust.list"); } catch (e) {
    ul.innerHTML = `<li class="dim small">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const items = d.items || [];
  if (!items.length) {
    ul.innerHTML = '<li class="dim small">还没有已信任的项目（首次打开带 .skysheep/ 配置的项目并确认后出现在这里）</li>';
    return;
  }
  ul.innerHTML = "";
  items.forEach((it) => {
    const li = document.createElement("li");
    li.className = "trust-row";
    const when = it.trusted_at ? fmtBackupTime(it.trusted_at) : "";
    li.innerHTML = `
      <span class="item-name" title="${escapeHtml(it.path)}">${escapeHtml(it.path)}</span>
      ${it.is_current ? '<span class="backup-tag">当前</span>' : ""}
      <span class="dim small rule-age">${when}</span>
      <span class="spacer"></span>
      <button class="btn-ghost danger">撤销信任</button>`;
    li.querySelector("button").onclick = () => {
      const box = document.createElement("div");
      box.innerHTML = `<p>撤销对 <b>${escapeHtml(it.path)}</b> 的信任？</p>
        <p class="dim small">撤销后，下次打开该项目时其中的 .skysheep/ 配置（MCP 与技能）会重新询问确认。</p>`;
      showModal("撤销信任", box, async () => {
        try {
          await request("trust.revoke_path", { path: it.path });
          await renderTrustList();
          renderTrustState();
          addNotice(`已撤销信任：${it.path}`);
        } catch (err) {
          addNotice("撤销失败：" + err.message);
        }
      }, "撤销");
    };
    ul.appendChild(li);
  });
}

// ---------- 关于：打开目录 / 诊断包 / 会话库备份恢复 ----------
async function openFixedPath(kind, label) {
  try {
    const r = await request("app.open_path", { kind });
    addNotice(`已打开${label}：${r.path}`);
  } catch (e) {
    addNotice(`打开${label}失败：` + e.message);
  }
}
document.getElementById("btn-open-data").onclick = () => openFixedPath("home", "数据文件夹");
document.getElementById("btn-open-logs").onclick = () => openFixedPath("logs", "日志文件夹");
document.getElementById("btn-open-backups").onclick = () => openFixedPath("backups", "备份目录");

// 关于页的长路径（配置文件 / 数据库 / 工作目录）点击复制：报障时直接粘给开发者。
// 设置页里看不到会话流的 notice，反馈就地做在元素上（变色 + 标题短暂切换）。
document.addEventListener("click", async (e) => {
  const el = e.target.closest(".copyable-path");
  if (!el || el.dataset.copying) return;
  el.dataset.copying = "1";
  await copyTextToClipboard(el.textContent);
  el.classList.add("copied");
  const prev = el.title;
  el.title = "已复制";
  setTimeout(() => {
    el.classList.remove("copied");
    el.title = prev;
    delete el.dataset.copying;
  }, 1200);
});

document.getElementById("btn-diag").onclick = async () => {
  const msg = document.getElementById("diag-msg");
  msg.textContent = "打包中…";
  try {
    const r = await request("app.export_diagnostics");
    msg.textContent = `✓ 已生成诊断包（${(r.size / 1024).toFixed(0)} KB）并打开所在文件夹`;
    msg.className = "io-msg ok";
  } catch (e) {
    msg.textContent = "✗ " + e.message;
    msg.className = "io-msg bad";
  }
};

// 反馈问题：诊断包出口——打包脱敏日志后打开 GitHub 反馈页，用户把 zip 拖进附件即可
document.getElementById("btn-feedback").onclick = async () => {
  const msg = document.getElementById("diag-msg");
  msg.textContent = "正在打包并打开反馈页…";
  try {
    await request("app.export_diagnostics");
  } catch (e) { /* 诊断包失败不阻塞打开反馈页 */ }
  try {
    await request("app.open_external", {
      target: REPO_PAGE + "/issues/new?template=bug_report.md",
    });
    msg.textContent = "✓ 已生成诊断包并打开反馈页——把诊断包 zip 拖进附件，描述问题即可";
    msg.className = "io-msg ok";
  } catch (e) {
    msg.textContent = "✗ 打开反馈页失败：" + e.message + "（可手动访问：" + REPO_PAGE + "/issues）";
    msg.className = "io-msg bad";
  }
};

function fmtBackupTime(ts) {
  // 精确到秒：与备份行（文件名时间戳）同一粒度，列表里两种时间才对得上
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// 备份列表：默认收起只列「当前数据 + 最近几份」，其余收进末尾的展开按钮

// 备份名里的时间戳（20260917-084701 → 2026-09-17 08:47:01）；「恢复前」副本只看前缀
function fmtBackupStamp(stamp, fallbackTs) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(String(stamp || ""));
  if (!m) return fmtBackupTime(fallbackTs);
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

let backupsExpanded = false; // 展开状态在本页内存里保持；重新加载后回到默认收起
const BACKUP_PREVIEW = 4; // 收起时展示的最近备份数

function backupRowHtml(b) {
  // 时间取 b.taken（解析自文件名的备份时刻）：备份文件的 mtime 是复制时带过来的源库
  // 修改时间，20 份备份会显示成同一个时刻。当前数据没有备份时刻，只能标它的写入时间。
  const size = `${(b.size / 1024).toFixed(0)} KB`;
  const when = b.current ? "当前数据" : fmtBackupStamp(b.stamp, b.taken || b.mtime);
  const meta = b.current ? `最后写入 ${fmtBackupTime(b.mtime)} · ${size}` : size;
  const tag = b.safety ? '<span class="backup-tag">恢复前</span>' : "";
  return `<li class="backup-row${b.current ? " cur" : ""}">
      <span class="item-name">${when}</span>${tag}
      <span class="dim small">${meta}</span>
      <span class="spacer"></span>
      ${b.current ? "" : `<button class="btn-ghost" data-restore="${escapeHtml(b.name)}" ` +
        `data-when="${escapeHtml(when)}">恢复到此版本</button>` +
        `<button class="btn-ghost danger" data-del="${escapeHtml(b.name)}" ` +
        `data-when="${escapeHtml(when)}" title="删除这份备份（不影响当前会话数据）">删除</button>`}
    </li>`;
}

async function loadBackups() {
  // 自动检查更新开关：进关于页时从偏好回填（保存即生效，下次启动不再请求）
  request("ui.get").then((ui) => {
    const t = document.getElementById("update-check-toggle");
    if (t) t.checked = !ui || ui.prefs.update_check == null ? true : ui.prefs.update_check === 1;
  }).catch(() => {});
  const ul = document.getElementById("backup-list");
  let d;
  try { d = await request("session.backups"); } catch (e) {
    ul.innerHTML = `<li class="dim small">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const items = d.backups || [];
  const cur = items.filter((b) => b.current);
  const hist = items.filter((b) => !b.current);
  // 默认收起：只列「当前数据 + 最近几份」，卡片不再被 20 行撑满；
  // 其余收进末尾的展开/收起按钮（样式在 .backup-more）
  const shown = backupsExpanded ? hist : hist.slice(0, BACKUP_PREVIEW);
  const rows = cur.concat(shown).map(backupRowHtml);
  const hidden = hist.length - shown.length;
  if (hidden > 0) {
    rows.push(`<li class="backup-more"><button class="btn-ghost" data-more="1">▾ 展开其余 ${hidden} 份备份</button></li>`);
  } else if (backupsExpanded && hist.length > BACKUP_PREVIEW) {
    rows.push(`<li class="backup-more"><button class="btn-ghost" data-more="1">▴ 收起备份列表</button></li>`);
  }
  ul.className = "list" + (backupsExpanded ? " expanded" : "");
  ul.innerHTML = rows.join("") || '<li class="dim small">还没有备份（应用下次启动时会自动生成一份）</li>';
  ul.querySelectorAll("[data-restore]").forEach((btn) => {
    btn.onclick = () => restoreBackupConfirm(btn.dataset.restore, btn.dataset.when, btn);
  });
  ul.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => deleteBackupConfirm(btn.dataset.del, btn.dataset.when, btn);
  });
  ul.querySelectorAll("[data-more]").forEach((btn) => {
    btn.onclick = () => { backupsExpanded = !backupsExpanded; loadBackups().catch(() => {}); };
  });
  const bs = document.getElementById("backup-status");
  const totalKB = hist.reduce((s, b) => s + (b.size || 0), 0) / 1024;
  const totalTxt = totalKB >= 1024 ? (totalKB / 1024).toFixed(2) + " MB" : totalKB.toFixed(0) + " KB";
  bs.textContent = `共 ${hist.length} 份备份 · 共占 ${totalTxt} · 备份目录：${d.dir}（保留最近 ${d.keep} 份）`;
  bs.className = "card-status ok";
  bs.hidden = false;
}

function restoreBackupConfirm(name, when, btn) {
  const box = document.createElement("div");
  box.innerHTML = `<p>要用 <b>${escapeHtml(when || name)}</b> 这份备份覆盖当前会话数据吗？</p>
    <p class="dim small">备份文件：${escapeHtml(name)}<br>
    当前数据会先自动另存为一份「恢复前」备份，所以还能再换回来；
    恢复后需要刷新界面（会话列表会回到那个时间点的样子）。</p>`;
  showModal("恢复会话库备份", box, async () => {
    btn.disabled = true;
    try {
      const r = await request("session.restore_backup", { name });
      addNotice(`✓ 已恢复到 ${r.restored}` + (r.safety_copy ? "（恢复前的数据已另存一份）" : ""));
      await boot(); // 会话/项目列表按恢复后的库重画
      await loadBackups();
    } catch (e) {
      addNotice("恢复失败：" + e.message);
      btn.disabled = false;
    }
  }, "恢复");
}

function deleteBackupConfirm(name, when, btn) {
  const box = document.createElement("div");
  box.innerHTML = `<p>要删除 <b>${escapeHtml(when || name)}</b> 这份备份吗？</p>
    <p class="dim small">备份文件：${escapeHtml(name)}<br>
    删除后不可找回；当前会话数据不受影响。</p>`;
  showModal("删除会话库备份", box, async () => {
    btn.disabled = true;
    try {
      await request("session.delete_backup", { name });
      await loadBackups();
    } catch (e) {
      addNotice("删除失败：" + e.message);
      btn.disabled = false;
    }
  }, "删除");
}

document.getElementById("btn-backups-refresh").onclick = () => loadBackups().catch(() => {});

document.getElementById("btn-backup-now").onclick = async () => {
  const btn = document.getElementById("btn-backup-now");
  const bs = document.getElementById("backup-status");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "备份中…";
  try {
    const r = await request("session.create_backup");
    await loadBackups();
    bs.textContent = `✓ 已手动备份：${r.name}`;
    bs.className = "card-status ok";
    bs.hidden = false;
    btn.textContent = label;
  } catch (e) {
    bs.textContent = "✗ 手动备份失败：" + e.message;
    bs.className = "card-status bad";
    bs.hidden = false;
    btn.textContent = label;
  } finally {
    btn.disabled = false;
  }
};

// __ERR_TRAP_BEGIN（临时错误陷阱，测试后删除）
(function () {
  function show(label, detail) {
    let d = document.getElementById("__errdump");
    if (!d) {
      d = document.createElement("pre");
      d.id = "__errdump";
      d.style.cssText = "position:fixed;top:0;left:0;z-index:99999;background:#7a1010;color:#fff;font-size:11px;white-space:pre-wrap;max-width:90vw;max-height:45vh;overflow:auto;margin:0;padding:4px 8px;";
      document.body.appendChild(d);
    }
    d.textContent += "== " + label + " ==\n" + detail + "\n\n";
  }
  window.addEventListener("error", function (e) {
    // 「ResizeObserver loop」是浏览器对「RO 回调内改布局」的良性提示
    // （不是应用错误，拖面板/缩放窗口时随时可能触发）——业界惯例直接忽略，
    // 否则红屏刷满这条吓人（真循环的根因在各自的 RO 回调里修，见 rp-tabs/行卡）
    if (String(e.message || "").indexOf("ResizeObserver loop") >= 0) return;
    show("error", e.message + " @ " + (e.filename || "?") + ":" + e.lineno + ":" + e.colno + "\n" + ((e.error && e.error.stack) || ""));
  });
  window.addEventListener("unhandledrejection", function (e) {
    show("rejection", ((e.reason && (e.reason.stack || e.reason.message)) || String(e.reason)));
  });
})();
// __ERR_TRAP_END

// 面板一次性接线：输入框回车快速添加、「＋ 新建任务」按钮
wireProjectTasksPanel();
