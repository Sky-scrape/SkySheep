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
 *  共享的状态时注意。部分分区已按上述边界拆为 app-<区名>.js 普通 script
 *  （projects / schedule / providers / whitelist / skills / memmap / advanced /
 *   about / mods / tools），
 *  本文件为主文件；跨文件共享的顶层名不得重复（重名 function 会被后加载者
 *  静默覆盖，守卫见 tests/test_frontend_wiring.py 的跨文件重名扫描用例）。
 * ─────────────────────────────────────────────────────────────────────── */

// ---------- 历史导航兜底（本应用只有一个页面） ----------
// 触屏边滑 / 鼠标侧键的后退手势会把窗口带回启动动画页——那是一张没有脚本的
// 静态页（bootpages.splash_html），回去了就没人再导航回来，窗口永远停在
// 「正在启动」。这里压一个同地址哨兵并逐次补压：后退/前进手势被原地吞掉，
// 永远走不出本页（desktop.py 另有看守兜底，覆盖本脚本尚未执行的空窗）。
history.pushState(null, "", location.href);
addEventListener("popstate", () => history.pushState(null, "", location.href));

// ---------- 迷你 Markdown 渲染（先转义再渲染，安全） ----------
function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
// 两位补零的全局助手：时间戳/日期格式化多处要用（原先十来处各自局部重写）
function pad2(n) { return String(n).padStart(2, "0"); }
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

// 单条消息 markdown 全套渲染的长度上限：超过就降级为「先 escapeHtml 再换行」的
// 纯文本渲染（对抗审查 DoS 加固）。128KB 级的长文跑全套结构规则本就吃力，病态
// 构造更会在流式期间被 80ms tick 反复重放；宁可保读性不保排版。
const MD_PLAIN_LIMIT = 128 * 1024;

function renderMarkdown(src) {
  // 超长降级路径同样先 escapeHtml 再做结构替换（换行 → <br>），转义顺序不变
  if ((src || "").length > MD_PLAIN_LIMIT) {
    return escapeHtml(src).replace(/\n/g, "<br>");
  }
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
  // 打开后，新窗口拿不到 window.opener，也带不走 Referer。
  // 回溯护栏（对抗审查 DoS 项）：旧写法 [^\]]+ 对大量未闭合 `[` 平方级回溯
  // （实测 200KB 病态文单次全量渲染 30 秒以上，流式期间每 80ms tick 重放一遍）。
  // 两道防线：① 廉价预检——文本里连 `](` 都没有时整条规则直接跳过（indexOf 线性）；
  // ② 长度界——链接文本 ≤500、URL ≤2000，单个候选位置的最坏回溯封顶，整体从
  // 平方级降到线性。转义顺序不动：仍是对 escapeHtml 之后的 text 做结构替换。
  if (text.includes("](")) {
    text = text.replace(
      /\[([^\]]{1,500})\]\((https?:[^)]{1,2000})\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    );
  }
  // 段落（代码块此时还是占位符，同样不能再包一层 <p>）
  const parts = text.split(/\n{2,}/).map((p) => {
    const t = p.trim();
    return MD_BLOCK_RE.test(t) || /^\u0000CODE\d+\u0000$/.test(t)
      ? p
      : `<p>${p.replace(/\n/g, "<br>")}</p>`;
  });
  text = parts.join("");
  // 还原代码块（占位符可能被模型在正文里伪造：越界索引一律替换为空，
  // 不能把 "undefined" 渲染出来）
  text = text.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => codeBlocks[i] ?? "");
  return text;
}

// ---------- WebSocket 客户端 ----------
let ws = null;
let reqSeq = 0;
const pendingReplies = new Map();

// ---------- 连接（WS 协议客户端） ----------
let wsEverConnected = false; // 是否成功连上过至少一次（区分首次连接与断线重连）
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    setConn(true);
    // 断线期间挂着的请求（没来得及发出去的）挪到新连接上补发
    pendingReplies.forEach((p) => { if (!p.sent && p.send) p.send(); });
    // 曾连上过又断开 → 现在恢复了：断线期间面板里的数据（日程/任务/文件树…）
    // 可能已经变了，整体重拉一遍；首次连接不刷（boot/initUiPrefs 自己会拉）
    if (wsEverConnected) {
      reloadProjectPanels();
      restorePendingPermissions(); // 重连后补回未决的权限确认卡
      // 工厂开着时重拉一次：断线期间的运行事件可能丢了，计数与场景以服务端为准
      if (factoryOpen && typeof factoryReload === "function") factoryReload();
    }
    wsEverConnected = true;
  };
  ws.onclose = () => {
    setConn(false);
    // 断线清账：已经发出去的请求永远不会等到回包（新连接不认识旧 id），
    // 就地报错让调用方的 finally / catch 走起来——否则像辅助对话的 auxBusy
    // 会永远卡在 true，发送按钮从此没反应。还没发出去的留在表里等重连补发。
    pendingReplies.forEach((p, id) => {
      if (p.sent) {
        pendingReplies.delete(id);
        p.reject(new Error("连接已断开，请重试"));
      }
    });
    setTimeout(connect, 1500);
  };
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
    const entry = { resolve, reject, sent: false, send: null };
    pendingReplies.set(id, entry);
    // send 幂等：断线清账 / 重连补发 / 「等 open」监听三条路径都可能叫到它，
    // 真正发出去的只许一次（重复发送会让后端把同一请求执行两遍）
    entry.send = () => {
      if (entry.sent || !pendingReplies.has(id)) return;
      try {
        ws.send(JSON.stringify({ id, method, params }));
        entry.sent = true;
      } catch (e) {
        pendingReplies.delete(id);
        reject(e);
      }
    };
    if (ws.readyState === WebSocket.OPEN) entry.send();
    else ws.addEventListener("open", entry.send, { once: true }); // 连接就绪再发，避免首屏 boot 静默失败
  });
}

let wsConnOn = false; // 当前连接状态镜像：分屏列在连接建立后才创建，创建时要按现状落初始值

function setConn(on) {
  wsConnOn = on;
  // 重连恢复：接力任务只刷新快照，绝不重放执行（业务推进在后端）
  if (on && typeof relayOnReconnect === "function") relayOnReconnect();
  // 文字已删，只留圆点：连接状态走 title 悬停说明（断线时悬停可见「重连中…」）
  const dot = document.getElementById("conn-status");
  dot.className = "dot " + (on ? "on" : "off");
  dot.title = on ? "已连接" : "重连中…";
  // 分屏列的连接镜像：点两下变灰（.dot.on/off 的样式对类名生效）
  document.querySelectorAll("#split-pane .split-conn").forEach((el) => {
    el.className = "dot split-conn " + (on ? "on" : "off");
    el.title = on ? "已连接" : "重连中…";
  });
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
  // 贴底跟随标记：默认跟随；用户上翻（scroll 离开底部 4px 以上）即挂起，
  // 滚回底部自动恢复。scrollLog 只读这个标记（见其注释）。
  logEl._followBottom = true;
  logEl.addEventListener("scroll", () => {
    logEl._followBottom = logNearBottom(logEl);
    updateJumpBottom();
  }, { passive: true });
  return {
    sid: sid || null, title: title || "", logEl, running: false,
    streamingEl: null, streamingText: "", rtCard: null, rtMemberEls: [],
    teamCard: null, // 团队进行中的团队卡（team_started 建立，事件实时驱动更新）
    lastAssistantText: "", usage: null, needsPerm: false, permData: null,
    needHistory: false, eta: null, etaEl: null,
    thinkMs: 0, thinkT0: 0,
  };
}
function tabFor(sid) { return sid ? chatTabs.find((t) => t.sid === sid) || null : null; }
function curTab() { return routeTab || activeTab; }
function curLog() { const t = curTab(); return t ? t.logEl : chatBox; }
// 贴底跟随：用户上翻回看时暂停「新内容拽到底部」，滚回底部自动恢复。
// 判断必须在「追加内容之前」的滚动位置上记账——scrollLog 都在节点追加/长高之后
// 才被调用，那时 scrollHeight 已变大，现量必然误判成「已离开底部」、永远不再跟随。
// scroll 事件只在真实滚动时触发（DOM 追加长高不触发），所以在 scroll 监听里把
// 「此刻是否贴底」记到流元素上，scrollLog 只看标记。阈值与 renderHistory
// 分批渲染的贴底判断一致（4px）。
function logNearBottom(el) {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
}
function curLogEl() {
  const t = curTab();
  return (t && t.logEl) || chatBox.querySelector(".chat-log");
}
// 「回到底部」浮标只在当前可见的聊天流离开底部时出现
function updateJumpBottom() {
  const btn = document.getElementById("jump-bottom");
  if (!btn) return;
  const t = curTab();
  const el = curLogEl();
  const show = !!el && t === activeTab && el._followBottom === false;
  btn.hidden = !show;
  // html 初始态带着 class="hidden"（display:none !important，压过一切）：
  // 光切 hidden 属性摘不掉这个类，浮标会永远显示不出来，必须同步切类
  btn.classList.toggle("hidden", !show);
}
function scrollLog() {
  const t = curTab();
  // 分屏列是屏上可见的独立对话面：内容进列、不占主栏，照常跟随滚动；
  // 只有后台标签（看不见的页签）追加内容才不抢滚动
  if (t && t !== activeTab && !splitPanes.includes(t)) return;
  // 滚动发生在当前标签的 .chat-log 上（#chat 本体 overflow:hidden，滚它无效）
  const el = (t && t.logEl) || chatBox.querySelector(".chat-log");
  if (!el) return;
  if (el._followBottom === false) return; // 用户上翻回看：不抢位置（滚回底部自动恢复）
  el.scrollTop = el.scrollHeight;
  updateJumpBottom();
}
// 强制回到底部并恢复跟随：用户主动发送消息 / 点「回到底部」浮标时用
function followLogBottom(el) {
  if (!el) return;
  el._followBottom = true;
  el.scrollTop = el.scrollHeight;
  updateJumpBottom();
}
document.getElementById("jump-bottom").onclick = () => followLogBottom(curLogEl());
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
    // 中键关闭：浏览器标签的肌肉记忆手势（auxclick 只认主键盘之外的键）
    el.onauxclick = (e) => {
      if (e.button === 1) { e.preventDefault(); closeTab(t); }
    };
    t.el = el; // 右键菜单/改名要找回这张标签的 DOM（renderTabs 每次重建，随渲染刷新）
    if (t.sid) wireTabDrag(el, t);
    bar.appendChild(el);
  });
  // 标签溢出（可横滚）时保证激活标签落在可视区：启动恢复/接管后台会话后
  // 激活的往往是最边上那张，不滚进来用户看不到当前会话是哪个。
  // 顺手把溢出态挂在 bar 上：CSS 据此画右缘渐隐，暗示右边还有
  const overflowing = bar.scrollWidth > bar.clientWidth;
  bar.classList.toggle("has-overflow", overflowing);
  if (activeTab && activeTab.el && overflowing) {
    activeTab.el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  // Mods 状态托盘（实验性）：stat 片段随会话标签走，跨会话不串
  renderModTray();
  // 标签栏尾部的 ＋ 新建：浏览器式页签的固定收尾。与侧栏「新建会话」/ Ctrl+N
  // 同一个动作（newSessionFromHighlight：落在侧栏高亮的项目/快聊里），
  // 只是把入口放到用户视线所在的标签栏上
  let plus = bar.querySelector(".tab-new");
  if (!plus) {
    plus = document.createElement("button");
    plus.className = "tab-new";
    plus.type = "button";
    plus.title = "新建会话（Ctrl+N）";
    plus.setAttribute("aria-label", "新建会话");
    plus.textContent = "＋";
    plus.onclick = () => newSessionFromHighlight();
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
    // 溢出时有细滚动条，滚轮也直接横滚——标签栏不比抢一次鼠标位移的精度
    bar.addEventListener("wheel", (e) => {
      if (e.deltaY && bar.scrollWidth > bar.clientWidth) {
        bar.scrollLeft += e.deltaY;
        e.preventDefault();
      }
    }, { passive: false });
    // 双击空白处新建标签：浏览器页签的惯例手势（点在标签/＋上时不触发——
    // 它们自己消费 dblclick/onclick，冒泡上来时 target 不是 bar）
    bar.addEventListener("dblclick", (e) => {
      if (e.target === bar) newSessionFromHighlight();
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
// 悬停提示消息开头文字。消息太多匀不下时等距抽样（一颗代表一段，跳到段首）：
// 先按消息数封顶（超过 MINI_MAX_MSGS 条先对消息抽样，rebuild 不再逐条全量测量），
// 再按可用高度封顶（短视口下圆点更少）。
// 只在内容可滚动且不止一条消息时出现；随 attachTabLog 绑定当前标签的聊天流。
const chatMinimap = document.createElement("div");
chatMinimap.id = "chat-minimap";
chatMinimap.className = "hidden";
chatBox.appendChild(chatMinimap);
const MINI_ITEM_H = 10, MINI_GAP = 4; // 与 app.css 的圆点热区高 / 列间距保持一致
const MINI_MAX_MSGS = 150; // 消息数上限：超过就先对消息等距抽样，圆点从样本里选代表
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
  // 消息数超上限先对消息等距抽样：下面这段逐条测量每条消息各读一次
  // getBoundingClientRect，几千条消息的会话流式期间每轮 rebuild 全量量一遍太重。
  // 抽样后每颗圆点仍对准样本里的真实消息（高亮与点击都按它算），末尾留一个
  // 名额给最后一条消息——点最末一颗、贴底强制高亮都对准真正的结尾；
  // 不超上限时 pool 就是原数组，行为与逐条测量完全一致。
  let pool = msgs;
  if (msgs.length > MINI_MAX_MSGS) {
    pool = new Array(MINI_MAX_MSGS);
    const s0 = msgs.length / MINI_MAX_MSGS;
    for (let i = 0; i < MINI_MAX_MSGS; i++) {
      pool[i] = i === MINI_MAX_MSGS - 1 ? msgs[msgs.length - 1] : msgs[Math.floor(i * s0)];
    }
  }
  // 目标位置相同的连续消息合并成一颗（保留最后一条——滚到底时看到的正是末尾那几条）。
  // 内容只比视口高一点点时，末尾好几条的目标会被一起钳到底部；
  // 不合并就会出现多颗点了没反应、高亮全落最后一颗的死圆点。
  const slots = [];
  for (const el of pool) {
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
  updateJumpBottom(); // 浮标跟着当前可见的聊天流走
}

// ---------- 主栏输入区状态按会话记忆 ----------
// mode（规划/执行）、rtOn/teamOn（一次性开关）、draft（草稿）、当前生效模型与
// 视觉能力、图片托盘与 & 引用，全部随会话存取——切标签不串状态（A 标签开的
// 规划模式、贴的图、写的草稿都不会随 B 发出去）。键是会话 sid；空标签共用
// "" 键，懒创建/接管时随标签换绑迁移。
const composerBySid = new Map();
// 引擎全局生效模型的镜像（boot 快照 / 全局切换时更新）：空标签没有会话私有
// 模型，徽章永远按它画。curProviderName 在主栏会话级切换时会被改写成该会话
// 的私有模型——空标签若从 cur 继承，显示的就不是实际发送所用模型（引擎全局）
let globalProviderName = "", globalModelName = "", globalSupportsVision = true;

function composerState(sid) {
  const key = sid || "";
  let st = composerBySid.get(key);
  if (!st) {
    // 新条目 = 默认态：模式与一次性开关不继承当前值（A 标签开的规划模式
    // 不能串到没去过的会话）；模型三件套继承当前全局显示（无覆盖的会话
    // 本来就跟随全局，model_get 落地后校正为会话生效值）
    st = {
      mode: "execute", rtOn: false, teamOn: false, draft: "",
      providerName: curProviderName, modelName: curModelName,
      supportsVision: curSupportsVision, images: [], refs: [],
    };
    composerBySid.set(key, st);
  }
  return st;
}

/** 切入前调用：把输入区当前状态写回旧会话的条目。 */
function saveComposerState(sid) {
  const st = composerState(sid);
  st.mode = workMode;
  st.rtOn = rtOn;
  st.teamOn = teamOn;
  st.advOn = advOn;
  st.draft = inputEl.value;
  // 模型三件套只随会话条目记：空标签没有会话私有模型（显示跟随引擎全局，
  // 由全局镜像维护），把 cur 写回 "" 条目会把上个会话的私有模型串进去
  if (sid) {
    st.providerName = curProviderName;
    st.modelName = curModelName;
    st.supportsVision = curSupportsVision;
  }
  st.images = pendingImages;
  st.refs = pendingRefs;
}

/** 切入后调用：按新会话的条目恢复输入区（无条目 = 默认态）。
    顺序硬规定：先写 mode 再恢复 rt/team——setTeamOn 的「规划模式开着」
    提示按 workMode 当前值判定，顺序反了会在恢复时弹误报；silent 压掉
    互斥/联动的三处 notice（恢复不是用户操作，不该弹提示）。 */
function restoreComposerState(sid) {
  const st = composerState(sid);
  setWorkMode(st.mode || "execute");
  setRtOn(!!st.rtOn, true);
  setTeamOn(!!st.teamOn, true);
  setAdvOn(!!st.advOn, true);
  if (sid) {
    curProviderName = st.providerName || "";
    curModelName = st.modelName || "";
    curSupportsVision = st.supportsVision !== false;
  } else {
    // 空标签：模型三件套取引擎全局镜像，不读 "" 条目——条目里可能是上个
    // 会话的私有模型（会话级切换只改 cur，引擎全局并没变），显示会串值
    curProviderName = globalProviderName;
    curModelName = globalModelName;
    curSupportsVision = globalSupportsVision;
  }
  pendingImages = Array.isArray(st.images) ? st.images : [];
  pendingRefs = Array.isArray(st.refs) ? st.refs : [];
  renderImageTray();
  renderRefTray();
  inputEl.value = st.draft || "";
  autoGrowInput(); // 草稿回填后重算输入框高度，否则残留上一会话内容的高度
  if (curProviderName) {
    setModelChip(curModelName ? `${curProviderName}/${curModelName}` : curProviderName);
  }
}

/** 空标签认领会话（懒创建/接管）："" 条目随标签换绑迁移到新 sid。 */
function migrateComposerSid(oldSid, newSid) {
  const prev = composerBySid.get(oldSid || "");
  if (!prev) return;
  const nk = newSid || "";
  if (!composerBySid.has(nk)) composerBySid.set(nk, prev);
  composerBySid.delete(oldSid || "");
}

// 按当前活动会话刷新模型徽章与会话生效模型缓存（切标签/懒创建认领共用）。
// 竞态防护：闭包捕获 tab，落地前 activeTab 已变（或回包 session_id 对不上）
// 就丢弃——快速连切标签时慢回包不会把别的会话的模型画到当前栏。
function refreshActiveSessionBadges(tab) {
  if (!tab || !tab.sid) return;
  const t = tab;
  request("session.model_get", { id: t.sid }).then((r) => {
    if (!r || activeTab !== t || (r.session_id && r.session_id !== t.sid)) return;
    const st = composerState(t.sid);
    st.providerName = r.name || "";
    st.modelName = r.model || "";
    st.supportsVision = r.supports_vision !== false;
    curProviderName = st.providerName;
    curModelName = st.modelName;
    curSupportsVision = st.supportsVision;
    setModelChip(st.modelName ? `${st.providerName}/${st.modelName}` : st.providerName);
  }).catch(() => {});
  refreshReasoning();
}

async function activateTab(tab) {
  if (!tab) return;
  focusMainPane(); // 切回主栏面：所有列变灰（没有列时是空操作）
  // 切标签/点会话：分组视图里「点组头」借走的高亮到此交回活动会话所在组
  // （没借过就不动，也不多画一下；早退路径补一次重画）
  if (groupedClickGk != null) {
    groupedClickGk = null;
    if (tab === activeTab) refreshSessions();
  }
  if (tab === activeTab) {
    attachTabLog(tab);
    // 已是活动标签也可能欠着历史：needHistory 被旁路设上后若只走这条早退，
    // 历史永远不来，用户盯着一面空墙。补拉一次再退（loadTabHistory 会消费标记）
    if (tab.needHistory && tab.sid && !tab.running) await loadTabHistory(tab);
    return;
  }
  // 查找高亮挂在旧标签的 DOM 上：切标签先收掉，避免 mark 残留计数错乱
  if (findHits.length || !document.getElementById("find-bar").classList.contains("hidden")) {
    closeFindBar();
  }
  // 会话记忆：切入前把输入区状态写回旧会话条目（必须在 activeTab 换指前读旧值）
  if (activeTab && activeTab !== tab) saveComposerState(activeTab.sid);
  activeTab = tab;
  attachTabLog(tab);
  renderTabs();
  hidePermission();
  currentSessionId = tab.sid;
  // 接力工作台顶部的会话名跟着活动会话走（右侧面板开着时切标签也要更新）
  // 接力看板跟着活动会话走（右侧面板开着时才重读）
  if (typeof relayDualOnSessionChange === "function") relayDualOnSessionChange();
  // 侧栏会话行选中态跟着活动标签走（与分组视图组头高亮同一来源）：
  // 否则从侧栏点开 A 会话再切到快聊标签，组头高亮动了、行高亮还留在 A
  activeSessionSid = tab.sid || null;
  restoreComposerState(tab.sid);
  // 模型/思考徽章按会话刷新（session.model_get / session.reasoning_get，
  // 带竞态校验；空标签保持全局口径不请求）
  refreshActiveSessionBadges(tab);
  // 后台标签从未渲染过历史（事件创建的）→ 拉一次历史；否则轻量激活。
  // 两种情形互斥：正在跑的标签不能去拉历史（会把流式内容盖掉），只做轻量激活。
  if (tab.needHistory && tab.sid && !tab.running) {
    await loadTabHistory(tab);
  } else if (tab.sid) {
    request("session.activate", { id: tab.sid }).catch(() => {});
  }
  if (tab.usage) setContextUsage(tab.usage.tokens, tab.usage.limit, tab);
  else setContextUsage(0, 0, tab); // 该标签还没发过消息：清掉读数并隐藏环，避免残留上一标签的数值
  // 路由指针跟着活动标签走：下面的权限卡恢复要靠 showPermission 里的
  // routeTab 分支判断，残留的上一个事件路由（常指向别的后台标签）会把
  // 恢复误判成「后台会话的确认请求」，卡片弹不出来。后续事件到达时会照常覆盖。
  routeTab = tab;
  if (tab.needsPerm && tab.permData) showPermission(tab.permData);
  // 会话变了，右侧面板里「跟会话绑定」的页跟着换数据：审查页取的是后端当前
  // 会话的检查点、任务清单取的是当前会话的 todo——不重拉就会拿上一个会话的
  // 内容冒充本会话（工具栏还写着「本会话中 Agent 改过的文件」）。上面的
  // activate / resume 与这两个请求同一条 WS 按序到达，后端看到的已是新会话。
  if (rightTabs.includes("review")) loadRightTab("review");
  if (rightTabs.includes("tasks")) loadRightTab(rightView.tasks);
  // 会话级活动团队状态对齐：其他窗口收的队、重启后的「已中断」标注，
  // 都靠 team.get 落到团队卡上（内存快照，顺带补齐历史里缺的卡）
  if (tab.sid) refreshTeamState(tab);
  refreshAcceptChip(); // 盾牌显示活动会话自己的档位（每会话独立）
  petPrevRunning = !!tab.running;
  petRefresh();
  refreshSessions();
}

function openTabForSession(sid, title, opts = {}) {
  // 接力在右侧面板与聊天并行显示，切换会话时保留面板并更新其会话上下文。
  // 同一会话只在一个面上打开：主栏要开它就把开着的那一列收掉（侧栏点行、
  // 通知跳转、列头 🔒 等路径都汇在这里）。不收的话事件按 tabFor 优先全进
  // 标签，列的运行点/输入锁再也没有事件来复位
  if (sid) closeSplitPanesForSids(new Set([sid]), { restore: false }); // 会话正要开进主栏，无须搬回
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
      // 接管前先把空标签上的实时输入区状态写回 "" 条目，再随换绑迁移——
      // 不写回的话，刚敲的草稿会被条目里的旧值顶掉
      if (blank === activeTab) saveComposerState("");
      t = blank;
      t.sid = sid;
      t.title = title || "";
      t.titleFixed = false; // 空标签上预命名的名字随标签换绑丢弃：它现在代表另一个会话
      migrateComposerSid("", sid); // 空标签的输入区状态条目随换绑迁移到新会话
      adopted = true;
    } else {
      t = newTabObj(sid, title);
      chatTabs.push(t);
    }
    t.needHistory = !opts.withMessages;
  }
  if (opts.withMessages) {
    renderHistory(t, opts.withMessages, opts.messagesOffset);
    if (!t.logEl.children.length) withTab(t, showWelcome); // 空会话回到欢迎页
  }
  if (!opts.background) {
    // 接管来的标签常常就是当前激活那张，而 activateTab 对「已经是当前标签」
    // 会早退（它的语义是切标签）——这里补上接管必需的记账，否则标签名会一直
    // 停在「新会话」、currentSessionId 也不跟着走。
    if (adopted && t === activeTab) {
      attachTabLog(t);
      currentSessionId = t.sid;
      if (typeof relayDualOnSessionChange === "function") relayDualOnSessionChange();
      restoreComposerState(t.sid); // activateTab 早退了：输入区状态在这里恢复
      refreshActiveSessionBadges(t);
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
    renderHistory(tab, info.messages || [], info.messages_offset);
    // 空会话也回欢迎页（与 openTabForSession 的 withMessages 路径同一口径）——
    // 之前漏了这条，侧栏点开旧的空会话就是一片空白（用户报的「有的出现有的不出现」）
    if (!tab.logEl.children.length) withTab(tab, showWelcome);
  } catch (e) { addNotice("加载会话内容失败: " + e.message); }
}

// ---------- 页签拖动排序（水平）：与侧栏拖拽同一套手势，落点是左右半分 ----------

/** 给页签接上水平拖拽。item 用 sid 当 id（空标签无 sid，不接拖——垫底占位）。 */
function wireTabDrag(el, t) {
  wireListDrag(el, { id: t.sid }, {
    tag: "tab", // 对话区的落点只认标签拖拽（侧栏会话行的拖拽不变形）
    // 落点按水平中线分左右：拖到页签左半=插到它前面，右半=后面
    over: (e) => ({ id: t.sid, pos: dragHalfPosX(e, el) }),
    commit: (dst) => commitTabOrder(t.sid, dst.id, dst.pos),
    rerender: renderTabs,
  });
  // 拖拽到标签栏左右边缘自动横滚：标签溢出后，屏外的目标标签也够得着。
  // HTML5 拖拽中鼠标静止不重复触发 dragover，方向变量 + rAF 循环持续滚
  const bar = document.getElementById("chat-tabs");
  if (bar._dragScrollBound) return;
  bar._dragScrollBound = true;
  let dir = 0, raf = 0;
  const step = () => {
    if (!dir) { raf = 0; return; }
    bar.scrollLeft += dir * 10;
    raf = requestAnimationFrame(step);
  };
  bar.addEventListener("dragover", (e) => {
    const r = bar.getBoundingClientRect();
    dir = e.clientX < r.left + 28 ? -1 : e.clientX > r.right - 28 ? 1 : 0;
    if (dir && !raf) raf = requestAnimationFrame(step);
  });
  const stop = () => { dir = 0; if (raf) { cancelAnimationFrame(raf); raf = 0; } };
  bar.addEventListener("dragend", stop);
  bar.addEventListener("drop", stop);
  bar.addEventListener("dragleave", (e) => { if (!bar.contains(e.relatedTarget)) stop(); });
}

// 标签 / 侧栏会话行 拖进对话区：拖到消息区（#chat）松手 = 这一会话转分屏打开
// （拖拽排序语义只在各自列表内，出了列表就是「搬进分屏」）；拖到输入栏上 =
// 「引用该对话」——挂进那一栏的引用托盘随下一条消息注入上下文。三个落点
// 各挂各的树：#chat 只含消息区，#composer（主栏输入栏）与 #split-pane（分屏列）
// 都是 #chat-main 的孩子、不在 #chat 子树里，事件不会冒泡过去，必须各自接线。
// 分组头等其他拖拽不带标记，不会误触。
const chatDropZone = document.getElementById("chat");
// 落点命中的输入栏："main"=主栏，pane 对象=分屏列，undefined=不在任何输入栏。
// #composer 与 #split-pane 都在 #chat-main 下，#chat 的拖放监听够不到它们，
// 三处各自的监听用本函数统一判定落点
function composerFaceAt(x, y) {
  const c = document.getElementById("composer");
  if (c) {
    const r = c.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return "main";
  }
  for (const p of splitPanes) {
    const el = p.el && p.el.querySelector(".split-composer");
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return p;
  }
  return undefined;
}
/** 拖拽引用是否放行：本机桌面端已放开跨项目引用（后端对 local 发送注入
    并标注来源项目；快聊↔项目同样放开），用一次 session.list all_projects
    探测区分端别。远程端拿不到跨项目列表（local_gate 直接拒），后端仍按
    归属剔除引用——退回工作项目比对拦截，避免挂上发送时被静默丢弃的引用片。 */
async function refQuoteAllowed(item) {
  const all = await request("session.list", { all_projects: 1 }).catch(() => null);
  if (all !== null) return true; // 本机桌面端：任意会话可互相引用
  return String(item.project_id == null ? "" : item.project_id)
    === String(bootSnap && bootSnap.project_id != null ? bootSnap.project_id : "");
}
const isSplitDrag = () => {
  const st = wireListDrag.active;
  return !!(st && (st.tag === "tab" || st.tag === "session") && st.item && st.item.id);
};
// 拖拽中的会话标题（页签改名过的优先，其次行名/快照元数据）
function dragSessionTitle(sid, st) {
  const t = chatTabs.find((x) => x.sid === sid);
  return (t && t.title) || (st && st.item.name)
    || (sessionMeta[sid] || {}).title || "会话";
}
// 拖拽引用的公共落点：远程端项目边界拦截 + 挂进那一栏的引用托盘
function dropQuoteRef(face) {
  const st = wireListDrag.active;
  const sid = String(st.item.id);
  const title = dragSessionTitle(sid, st);
  refQuoteAllowed(st.item).then((allowed) => {
    if (!allowed) {
      addNotice(face === "main"
        ? `「${title}」与当前对话不属于同一项目，不能引用`
        : `「${title}」与这一列的对话不属于同一项目，不能引用`);
      return;
    }
    addSessionRef(sid, title, face === "main" ? null : face);
  });
}
/** 拖拽悬停时点亮落点所在的输入栏（引用落点提示）。 */
function syncRefHint(e) {
  clearRefDropHint();
  const face = composerFaceAt(e.clientX, e.clientY);
  if (face === undefined) return null;
  const el = face === "main"
    ? document.getElementById("composer")
    : face.el && face.el.querySelector(".split-composer");
  if (el) el.classList.add("ref-drop-hint");
  return face;
}
function clearRefDropHint() {
  document.querySelectorAll("#composer.ref-drop-hint, .split-composer.ref-drop-hint")
    .forEach((el) => el.classList.remove("ref-drop-hint"));
}
if (chatDropZone && !chatDropZone._tabSplitBound) {
  chatDropZone._tabSplitBound = true;
  chatDropZone.addEventListener("dragover", (e) => {
    if (!isSplitDrag()) return;
    e.preventDefault(); // 允许作为落点
    e.dataTransfer.dropEffect = "move";
    chatDropZone.classList.add("tab-drop-hint");
  });
  chatDropZone.addEventListener("dragleave", (e) => {
    if (!chatDropZone.contains(e.relatedTarget)) chatDropZone.classList.remove("tab-drop-hint");
  });
  chatDropZone.addEventListener("drop", async (e) => {
    if (!isSplitDrag()) return;
    e.preventDefault();
    chatDropZone.classList.remove("tab-drop-hint");
    const st = wireListDrag.active;
    const sid = String(st.item.id);
    st.over = null; // 落在对话区：dragend 不再走列表排序提交
    const title = dragSessionTitle(sid, st);
    // 跨项目会话（分组视图/「看其他项目」视图拖出）：先切到它的项目再开列，
    // 否则列里的会话会被当前项目归属校验拒掉（发送/换模型全被拒，等于废列）。
    // 标签拖出（tag "tab"）没有 project_id，必然属于当前项目，直通。
    const dpid = st.item.project_id;
    if (dpid != null && !(bootSnap && String(bootSnap.project_id) === String(dpid))) {
      const dprojs = ((await request("project.list").catch(() => null)) || {}).projects || [];
      const dtp = dprojs.find((p) => String(p.id) === String(dpid));
      if (dtp && dtp.root_path && !dtp.is_current) {
        try {
          await request("project.switch", { path: dtp.root_path });
          await applyWorkspaceData(await fetchWorkspaceData());
        } catch (err) {
          addNotice("切换到该项目失败：" + err.message);
          return;
        }
      }
    }
    openSplitPane(sid, title).catch(() => {});
  });
}

// 主栏输入栏：拖会话到输入栏 = 引用该对话，挂进主栏的引用托盘。
// dragover 必须 preventDefault：不然 text/plain 数据（会话 id）会按浏览器
// 默认行为直接插进 textarea，还顺带触发 input 监听里的 autoGrowInput 报错
const composerDropZone = document.getElementById("composer");
if (composerDropZone && !composerDropZone._refDropBound) {
  composerDropZone._refDropBound = true;
  composerDropZone.addEventListener("dragover", (e) => {
    if (!isSplitDrag()) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    syncRefHint(e);
  });
  composerDropZone.addEventListener("dragleave", (e) => {
    if (!composerDropZone.contains(e.relatedTarget)) clearRefDropHint();
  });
  composerDropZone.addEventListener("drop", (e) => {
    if (!isSplitDrag()) return;
    e.preventDefault();
    clearRefDropHint();
    const st = wireListDrag.active;
    st.over = null; // 落在对话区：dragend 不再走列表排序提交
    dropQuoteRef("main");
  });
}

// 分屏列（#split-pane 同样在 #chat-main 下）：拖会话进列输入栏 = 引用进那一列
// 的引用托盘；列的消息区不是落点（不 preventDefault，浏览器不响应）
const splitDropZone = document.getElementById("split-pane");
if (splitDropZone && !splitDropZone._refDropBound) {
  splitDropZone._refDropBound = true;
  splitDropZone.addEventListener("dragover", (e) => {
    if (!isSplitDrag() || composerFaceAt(e.clientX, e.clientY) === undefined) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    syncRefHint(e);
  });
  splitDropZone.addEventListener("dragleave", (e) => {
    if (!splitDropZone.contains(e.relatedTarget)) clearRefDropHint();
  });
  splitDropZone.addEventListener("drop", (e) => {
    if (!isSplitDrag()) return;
    const face = composerFaceAt(e.clientX, e.clientY);
    if (face === undefined || face === "main") return; // 主栏有自己的落点监听
    e.preventDefault();
    clearRefDropHint();
    const st = wireListDrag.active;
    st.over = null;
    dropQuoteRef(face);
  });
}

// 主栏参与焦点态：点对话区/标签栏/输入栏/权限条即把焦点交回主栏（各列变灰）；
// 切标签（含 Ctrl+Tab 键盘切换）走 activateTab 里的同一入口
for (const mainFaceId of ["chat", "chat-tabs", "composer", "permission-bar"]) {
  const mainFace = document.getElementById(mainFaceId);
  if (mainFace) mainFace.addEventListener("pointerdown", focusMainPane);
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
  // 关标签＝只把这张页签从对话页收掉，会话本身留着（侧栏仍在，随时点回来）。
  // 删除会话是另一件事：统一走左侧会话行的 ⋯ →「删除会话」或底部「清理空
  // 会话」，不在关标签时顺带删任何数据
  tab.logEl.remove();
  chatTabs.splice(idx, 1);
  if (activeTab === tab) {
    // 关标签先把实时输入区状态写回该会话条目：会话还在侧栏，随时点回来，
    // 重开时按记忆恢复（含关标签前刚敲的草稿）
    saveComposerState(tab.sid);
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
      activeSessionSid = null;
      groupedClickGk = null;
      restoreComposerState(null); // 不过 activateTab：直接恢复输入区/托盘/徽章默认态
      refreshReasoning(); // 空标签回到全局口径的思考档位
      showWelcome();
      renderTabs();
      refreshSessions(); // 侧栏组头高亮回落引擎当前项目（欢迎页无会话可跟）
    }
  } else renderTabs();
}

let creatingTab = false; // 防连点：session.new 在途时再点新建不再叠请求

// 批量收标签：删会话/清理空会话后，把这些会话打开着的页签一并收掉——
// 只更新侧栏不收标签的话，上方会残留点不开的死标签（会话已不存在）
function closeTabsForSids(sids) {
  const set = new Set(sids || []);
  if (!set.size) return;
  chatTabs.filter((t) => t.sid && set.has(t.sid)).forEach((t) => closeTab(t));
  closeSplitPanesForSids(set, { restore: false }); // 删会话/清理空会话：会话已不存在，不搬回
}

// —— 分屏：主聊天流右侧并排展示其他会话（对照看 / 独立对话） ——
// #split-pane 绝对定位铺在 #chat-main 右侧全高（topbar 下沿到界面底缘），
// 列输入框与主输入框底对齐；列对象同时是渲染伪 tab：补齐流式渲染需要的
// 字段（appendStream/工具卡等都经 curTab() 路由，事件带 sid 时 handleEvent
// 会把 routeTab 指到列上）。分屏会话在后台跑，不影响主栏当前会话。
const splitPaneEl = document.getElementById("split-pane");
const chatMainEl = document.getElementById("chat-main");
const splitPanes = []; // { sid, title, el, logEl, running, needsPerm, ...流式渲染字段 }
const SPLIT_MAX = 2;   // 最多两列：三栏再挤输入区就没法看了

// 分屏区顶部对齐顶栏下沿：top:44px 是 CSS 兜底，真实高度在这里按 topbar
// 实测同步（界面缩放/窗口变化后 topbar 高度会漂）；顶栏尺寸变化时重算
function syncSplitTop() {
  const tb = document.getElementById("topbar");
  if (tb && splitPanes.length) {
    splitPaneEl.style.top = (tb.offsetTop + tb.offsetHeight) + "px";
    const rz = document.getElementById("split-resizer");
    if (rz) rz.style.top = (tb.offsetTop + tb.offsetHeight) + "px";
    // 宽度自愈：可分宽度随 侧栏/右面板/窗口 实时变，存下的旧宽度可能已经越界
    // （比如右面板开着时拖出的宽，换个大屏又把主区盖住）——每次同步顺带把
    // --split-w 收回合法区间；未存偏好时（CSS 46% 兜底，永远合法）不动
    const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--split-w"));
    if (Number.isFinite(cur) && cur > 0) {
      const v = clampUi("split_w", cur);
      if (v !== Math.round(cur)) setUiVar("split_w", v);
    }
  }
}
if (typeof ResizeObserver === "function") {
  new ResizeObserver(syncSplitTop).observe(document.getElementById("topbar"));
  // 侧栏/右面板宽一块，可分宽度就窄一块：它们变了也要重算（RO 对 display 切换同样触发）
  new ResizeObserver(syncSplitTop).observe(document.getElementById("sidebar"));
  new ResizeObserver(syncSplitTop).observe(document.getElementById("right-panel"));
}
window.addEventListener("resize", syncSplitTop);

async function openSplitPane(sid, title) {
  // 同一会话只在一个面上打开（事件按 tabFor 优先路由进标签，列的运行点/
  // 输入锁再也没有事件来复位）：主栏开着同会话的标签就先收掉再开列——
  // 「在分屏打开」的语义 = 把会话从主栏搬进分屏。运行中的标签随关闭一并停止轮次
  // （与手动关标签同一语义），提示里说明。
  const conflict = tabFor(sid);
  let conflictNotice = "";
  if (conflict) {
    const wasRunning = conflict.running;
    closeTab(conflict);
    conflictNotice = wasRunning
      ? "已从主栏搬进分屏：原标签已关闭，运行中的轮次一并停止"
      : "已从主栏搬进分屏：原标签已关闭";
  }
  const exist = splitPanes.find((p) => p.sid === sid);
  if (exist) closeSplitPane(exist, { restore: false }); // 已开着这一栏：关掉重开 = 拉最新快照
  if (splitPanes.length >= SPLIT_MAX) {
    addNotice(`分屏最多同时开 ${SPLIT_MAX} 列，先关掉一列再试`);
    return;
  }
  let info;
  try {
    info = await request("session.peek_messages", { id: sid });
  } catch (e) {
    addNotice("打开分屏失败: " + e.message);
    return;
  }
  // interactive=false 只会出现在远程客户端（PWA/局域网）：跨项目会话的
  // runtime 绑定会话所属项目干活，本机分屏列一律可交互（完整会话）；远程
  // 对跨项目会话连 peek 都被拒，这道降级只是后端边界的前端兜底
  const readonly = info && info.interactive === false;
  const col = document.createElement("section");
  col.className = "split-col";
  // 输入区与主 composer 同构（工具条 + 大输入框 + 操作行）。行为分两类：
  // 闪电/圆桌/团队 = 每列独立的发送参数（chat.send 直接收），真实可用；
  // 盾牌/文件/语音/▾/模型/连接 = 全局单例状态，镜像显示、点击提示去主栏
  col.innerHTML =
    '<div class="split-head"><span class="split-title"></span>' +
    '<button class="split-refresh" title="重新拉取这个会话的最新消息">⟳</button>' +
    '<button class="split-close" title="关闭这一栏">✕</button></div>' +
    '<div class="chat-log split-log"></div>' +
    (readonly
      ? '<div class="split-readonly">该会话属于其他项目：分屏只读查看。在侧栏切到该项目后打开，即可继续对话。</div>'
      : '<div class="ui-resizer ui-resizer-h split-cp-resizer" title="拖动调整输入区高度 · 双击恢复默认"></div>' +
    '<div class="split-composer">' +
    '<div class="image-tray split-image-tray hidden"></div>' +
    '<div class="ref-tray split-ref-tray hidden"></div>' +
    '<textarea class="split-input" rows="2" placeholder="描述你的任务…（Enter 发送 / Shift+Enter 换行）"></textarea>' +
    '<div class="composer-bar split-bar">' +
    '<button class="cp-icon" data-role="plan" title="规划模式（每栏独立）：本栏消息只调研、不改动文件">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.8 13.2h5.7L10.2 22l8.9-11.6h-5.6L13 2z"/></svg></button>' +
    '<button class="cp-icon" data-role="coop" title="协作（每栏独立）：圆桌 / 团队 / 对抗，点击选择。分阶段报告请从侧栏打开接力工作台">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.2"/><path d="M3.5 19.5c.7-3.4 2.9-5.2 5.5-5.2s4.8 1.8 5.5 5.2"/><circle cx="17" cy="9.5" r="2.5"/><path d="M15.7 14.6c2.5.3 4.3 1.9 4.8 4.4"/></svg>' +
    '<span class="cp-state" hidden></span>' +
    '</button>' +
    '<button class="cp-icon" data-role="accept" title="权限三档（每栏独立）：安全执行 / 自动编辑 / 完全访问">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></button>' +
    '<button class="cp-icon" data-role="file" title="添加文件：选择文件以「@路径」引用进这一栏的输入框（项目外文件也可以）">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7.5A1.5 1.5 0 0 0 6 4.5v15A1.5 1.5 0 0 0 7.5 21h9a1.5 1.5 0 0 0 1.5-1.5V7l-4-4z"/><path d="M14 3v4h4"/><path d="M12 11v6M9.5 14.5 12 17l2.5-2.5"/></svg></button>' +
    '<button class="cp-icon" data-role="voice" title="语音输入：点击开始录音，再点一下结束并转成文字（转入这一栏的输入框）">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21M8.5 21h7"/></svg></button>' +
    '<span class="ctx-ring split-ctx-ring hidden" title="本会话上下文占用（悬停看明细）">' +
    '<svg viewBox="0 0 24 24"><circle class="ring-track" cx="12" cy="12" r="9"></circle>' +
    '<circle class="ring-val" cx="12" cy="12" r="9"></circle>' +
    '<text class="ring-txt" x="12" y="12.4"></text></svg></span>' +
    '<span class="spacer"></span>' +
    '<span class="dot split-conn on" title="与本地引擎的连接状态"></span>' +
    '<button class="split-reasoning chip chip-reasoning hidden" data-role="reasoning" title="">' +
    '<svg class="chip-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>' +
    '<span class="chip-txt"></span></button>' +
    '<button class="split-model" data-role="model" title="这一栏使用的模型（点击更换，只影响本栏）">' +
    '<svg class="sm-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="14" height="14" rx="2"/><rect x="10" y="10" width="4" height="4"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>' +
    '<span class="sm-txt"></span></button>' +
    '<button class="split-send btn-primary" title="发送（Enter）">' +
    '<svg class="send-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 15 22 11 13 2 9Z"/><path d="M22 2 11 13"/></svg>' +
    '<span class="send-txt">发送</span></button></div></div>');
  col.querySelector(".split-title").textContent = info.title || "(未命名)";
  // 连接镜像按当前状态落：setConn 只在状态变化时广播，连接已建立后才开的列
  // 不能停在灰点态
  const connDot = col.querySelector(".split-conn");
  if (connDot) {
    connDot.className = "dot split-conn " + (wsConnOn ? "on" : "off");
    connDot.title = wsConnOn ? "已连接" : "重连中…";
  }
  splitPaneEl.appendChild(col);
  splitPaneEl.classList.remove("hidden");
  chatMainEl.classList.add("has-split"); // composer 等底部件让出右侧宽度（CSS）
  syncSplitTop();
  // 列对象同时是渲染伪 tab：补齐流式渲染需要的字段（appendStream/工具卡等
  // 都经 curTab() 路由，事件带 sid 时 handleEvent 会把 routeTab 指到列上）。
  // 字段口径对齐 newTabObj：缺 lastAssistantText 的话，无流式正文的轮在
  // queue_updated 收尾时会 `.trim()` 抛 TypeError，中断该次事件后续处理
  const pane = {
    sid, title: info.title || "(未命名)", el: col,
    logEl: col.querySelector(".split-log"),
    running: false, needsPerm: false, permData: null,
    // 跨项目只读列：历史渲染据此不挂会改会话的操作（消息级编辑/重发/分叉/
    // 回退/重新生成、圆桌「引用追问」）——列上没有输入框，点了要么抛错要么
    // 换来一句误导性的「session not found」
    readonly: readonly,
    streamingEl: null, streamingText: "", thinkText: "", thinkEl: null,
    lastAssistantText: "", _toolCards: new Map(),
    // 列是完整会话：附件/引用/权限三档/上下文明细都在列上（与主栏标签各自独立）
    images: [], refs: [], acceptMode: null, ctxDetail: null,
  };
  // 贴底跟随：与主栏标签同一套（用户上翻挂起、滚回底部恢复）——列没有这套
  // 监听的话，流式长回答只会在列里往下长高，视口不动
  pane.logEl._followBottom = true;
  pane.logEl.addEventListener("scroll", () => {
    pane.logEl._followBottom = logNearBottom(pane.logEl);
  }, { passive: true });
  splitPanes.push(pane);
  const reload = () => renderHistory(pane, info.messages || [], info.messages_offset);
  col.querySelector(".split-close").onclick = () => closeSplitPane(pane);
  col.querySelector(".split-refresh").onclick = async () => {
    // 运行中不刷新：renderHistory 会清空日志，而流式增量还写进已被摘除的
    // 旧流式节点——本轮回答在列里不可见（主栏 loadTabHistory 同一守卫）
    if (pane.running) { paneNotice(pane, "这一栏正在运行，等当前轮结束再刷新"); return; }
    try {
      const fresh = await request("session.peek_messages", { id: sid });
      pane.title = fresh.title || pane.title;
      col.querySelector(".split-title").textContent = pane.title;
      renderHistory(pane, fresh.messages || [], fresh.messages_offset);
    } catch (e) { paneNotice(pane, "刷新分屏失败: " + e.message); }
  };
  reload();
  // 搬栏提示写进新列：它描述的是这一栏，不该出现在主栏
  if (conflictNotice) paneNotice(pane, conflictNotice);
  if (!pane.logEl.children.length) {
    pane.logEl.innerHTML = '<div class="rp-empty">这个会话还没有消息。</div>';
  }
  // 焦点态：点击哪列哪列正常亮度，其他列变灰（主栏恒正常）
  col.addEventListener("click", () => focusSplitPane(pane));
  focusSplitPane(pane);
  // 独立发送：chat.send 带 session_id 定向到这一栏的会话（后端已支持），
  // 流式事件带同一 sid，handleEvent 路由回这一列。
  // 闪电/圆桌/团队是每列独立的发送参数，随发送带上
  const input = col.querySelector(".split-input");
  const sendBtn = col.querySelector(".split-send");
  const doSend = async () => {
    const text = input.value.trim();
    const images = pane.images.slice();
    const refs = pane.refs.slice();
    if ((!text && !images.length) || pane.running) return;
    // 视觉能力按本列生效模型判（与主栏同一道提前拦截）
    if (images.length && pane.model && pane.model.supports_vision === false) {
      paneNotice(pane, `这一栏生效的模型「${pane.model.name}」不支持图片输入，请先在栏上换成多模态模型`);
      return;
    }
    input.value = "";
    autoGrowInput(input);
    clearPendingImages(pane);
    clearPendingRefs(pane);
    hideInputMenu();
    if (refFace === pane) closeRefMenu();
    pane.running = true;
    paintSplitHead(pane);
    withTab(pane, () => addUser(text, images, refs));
    const params = { text, session_id: sid };
    if (images.length) params.images = images;
    if (refs.length) params.refs = refs;
    if (pane.planMode) params.plan_mode = true;
    // 圆桌/团队/对抗是一次性开关（与主栏同语义）：发送即消费，失败也不回滚——
    // 不复位的话下一条消息会静默再带上 roundtable/team/adversarial=true
    const rtThisTurn = !!pane.rtOn;
    const teamThisTurn = !!pane.teamOn;
    const advThisTurn = !!pane.advOn;
    if (rtThisTurn) params.roundtable = true;
    if (teamThisTurn) params.team = true;
    if (advThisTurn) {
      params.adversarial = true;
      const advMembers = advMembersPayload(); // 列内也用主栏指定的角色模型
      if (advMembers) params.members = advMembers;
    }
    pane.rtOn = pane.teamOn = pane.advOn = false;
    syncSplitBarToggles(pane);
    try {
      const r = await request("chat.send", params);
      // 上下文徽标以本列发送响应为准：回包 session_id 与列会话一致才落
      if (r && (!r.session_id || r.session_id === sid)) {
        paintSplitCtx(pane, r.context_tokens, r.context_limit);
        if (r.context_detail) setContextDetail(r.context_detail, pane);
        // 列内改了文件也要有「撤销本轮改动」条（主栏 send 同款）；检查点条
        // 画进本列日志，回包对不上会话就不落
        if (r.checkpoint) addCheckpointBar(r.checkpoint, pane);
        // 列内规划轮同样给「按此计划执行」（addPlanActions 按列分派回这一栏）
        if (r.plan_mode && (pane.lastAssistantText || "").trim()) {
          addPlanActions(pane.lastAssistantText, pane);
        }
      }
    } catch (e) {
      pane.running = false;
      paintSplitHead(pane);
      paneNotice(pane, "发送失败: " + e.message);
    } finally {
      // 兜底复位：事件路由异常时列的运行态不能永久卡死（蓝点常亮 + 输入锁死）。
      // 正常路径轮末 queue_updated(pending=0) 已复位，这里是双保险
      if (splitPanes.includes(pane) && pane.running) {
        pane.running = false;
        paintSplitHead(pane);
      }
    }
  };
  if (!readonly) {
    pane.send = doSend; // 列内「按此计划执行」等入口从外部触发这一栏的发送
    sendBtn.onclick = doSend;
    // 列输入框与主输入框同一套键语义（发送键偏好 / ~ @ / & 菜单 / 菜单键盘导航）
    input.addEventListener("keydown", (e) => composerKeydown(e, pane));
    input.addEventListener("input", () => { updateInputMenu(pane); autoGrowInput(input); });
    input.addEventListener("blur", () => setTimeout(() => { if (menuFace === pane) hideInputMenu(); }, 120));
    bindComposerPaste(input, pane);
    bindComposerDrop(col.querySelector(".split-composer"), pane);
    // 列输入区高度手柄：写分屏自己的 split_composer_h（--split-cp-h），与主栏
    // 各调各的；双击恢复默认走 setupResizer 内建
    setupResizer(col.querySelector(".split-cp-resizer"), "split_composer_h", {
      base: () => col.querySelector(".split-composer").getBoundingClientRect().height / uiScale,
      value: (s, e) => s.base - (e.clientY - s.y) / uiScale, // 向上拖 = 输入区变高
    }, (v) => {
      if (v == null) autoGrowInput(input);
      else input.style.height = "";
    });
    autoGrowInput(input);
    // 列 composer 宽度观察器：与主栏同款（boot 期错误折行的高度在这里纠正）。
    // 存到 pane 上，关列时统一断开——匿名 RO 拿不到引用，列摘除后观察器仍
    // 挂着旧节点，反复开关列会在浏览器里持续积累（内存慢性泄漏）
    let _splitCpW = 0;
    pane._cpRO = new ResizeObserver((entries) => {
      for (const en of entries) {
        const w = Math.round(en.contentRect.width);
        if (w && w !== _splitCpW) {
          _splitCpW = w;
          autoGrowInput(input);
        }
      }
    });
    pane._cpRO.observe(col.querySelector(".split-composer"));
    syncSplitBarToggles(pane);
    // 权限三档：读回这一栏会话的档位（每栏独立，点击循环只写本栏）
    const acceptBtn = col.querySelector('[data-role="accept"]');
    request("session.accept_get", { id: sid }).then((g) => {
      pane.acceptMode = g.mode;
      paintAcceptIcon(acceptBtn, g.mode);
    }).catch(() => {});
    // 上下文环：悬停/点按看明细（与主栏同一只弹层，按面取数）
    const ring = col.querySelector(".split-ctx-ring");
    if (ring) {
      ring.addEventListener("mouseenter", () => openCtxPop(pane));
      ring.addEventListener("mouseleave", () => closeCtxPop());
      ring.addEventListener("click", () => {
        if (ctxPop.classList.contains("hidden")) openCtxPop(pane); else closeCtxPop(true);
      });
    }
  }
  // 工具条分派：plan/rt/team 每列独立开关（互斥同主栏），其余为全局单例或
  // 主栏专属，镜像显示 + 引导去主栏
  col.querySelectorAll("[data-role]").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const role = btn.dataset.role;
      if (role === "plan") {
        pane.planMode = !pane.planMode;
        btn.classList.toggle("plan", pane.planMode);
        if (pane.planMode) { pane.rtOn = pane.teamOn = pane.advOn = false; syncSplitBarToggles(pane); }
      } else if (role === "coop") {
        showSplitCoopMenu(pane, btn);
      } else if (role === "model") {
        pickSplitModel(pane, btn);
      } else if (role === "reasoning") {
        toggleReasoningMenu(e, btn, pane.sid); // 会话级思考强度：菜单锚定列徽章
      } else if (role === "file") {
        await attachSplitFiles(pane);
      } else if (role === "accept") {
        await cycleSplitAccept(pane, btn);
      } else if (role === "voice") {
        // 语音输入按面：录转的文字落进这一栏的输入框（同一支麦克风，谁点归谁）
        if (voiceBusy) {
          if (voiceRecorder && voiceRecorder.state === "recording") voiceStop();
          else paneNotice(pane, "正在识别上一条录音，稍等一下…");
        } else voiceStart(pane);
      } else {
        // 剩余镜像入口各给一句如实提示（不再统一一句模糊话）
        paneNotice(pane, "这是全局设置/主栏功能，请到主栏操作");
      }
    };
  });
  // 模型显示：会话级覆盖优先（session.model_get 兜底返回全局）；只读列没有
  // 这些操作入口，不浪费请求
  if (!readonly) {
    request("session.model_get", { id: sid }).then((r) => {
      if (r && r.name) paintSplitModel(pane, r);
    }).catch(() => {});
    // 思考强度徽章：按该会话生效档位拉取（session.reasoning_get；不支持则保持隐藏）
    paintSplitReasoning(pane);
    // 上下文徽标辅路：有落库历史的会话即有读数（无 runtime 时后端按落库消息
    // 估算，重启/淘汰后打开旧会话立刻能看到占用）；空会话/查无此会话后端
    // 显式报错——维持隐藏空态，发送后由 chat.send 响应落精确值
    request("chat.status", { session_id: sid }).then((r) => {
      if (r && r.session_id === sid && splitPanes.includes(pane)) {
        paintSplitCtx(pane, r.context_tokens, r.context_limit);
      }
    }).catch(() => { /* 空会话/查无此会话：空态是常态，保持隐藏 */ });
  }
}

// 列内思考强度徽章：按会话拉生效档位并画到徽章上（列已关闭就不落）
async function paintSplitReasoning(pane) {
  const el = pane.el.querySelector(".split-reasoning");
  if (!el || !pane.sid) return;
  const r = await request("session.reasoning_get", { id: pane.sid }).catch(() => null);
  if (!r || !el.isConnected) return;
  renderReasoningChip(el, {
    supported: r.supported, effort: r.effort, labels: r.labels, efforts: r.efforts,
  });
}

// 列内上下文占用徽标：有数据才显示（空态 = 默认态，发送后才有读数）
function paintSplitCtx(pane, tokens, limit) {
  const el = pane.el.querySelector(".split-ctx-ring");
  if (!el) return;
  pane.usage = limit ? { tokens, limit } : null;
  if (!limit) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const pct = Math.min(100, Math.round((100 * tokens) / limit));
  const C = 2 * Math.PI * 9; // 环半径 r=9（viewBox 24），与主栏 ctx-ring 同款
  el.querySelector(".ring-val").style.strokeDashoffset = String(C * (1 - pct / 100));
  el.querySelector(".ring-txt").textContent = String(pct);
  el.classList.toggle("warn", pct >= 70 && pct < 90);
  el.classList.toggle("bad", pct >= 90);
}

// 分屏列的权限三档：循环切换，只写这一栏的会话（session.accept_set，
// 本机专属）；引擎默认档与其他会话不受影响
async function cycleSplitAccept(pane, btn) {
  const order = ["confirm", "accept_edits", "full_access"];
  let cur = pane.acceptMode;
  if (!cur) {
    try {
      const g = await request("session.accept_get", { id: pane.sid });
      cur = g.mode;
    } catch (e) { paneNotice(pane, "读取权限档位失败: " + e.message); return; }
  }
  const next = order[(order.indexOf(cur || "confirm") + 1) % order.length];
  try {
    const r = await request("session.accept_set", { id: pane.sid, mode: next });
    pane.acceptMode = r.mode;
    paintAcceptIcon(btn, r.mode);
    paneNotice(pane, (ACCEPT_NOTICE[r.mode] || ACCEPT_NOTICE.confirm) + "（仅这一栏）");
  } catch (e) {
    paneNotice(pane, "切换失败: " + e.message);
  }
}

// 列内 @ 引用：选文件后以「@路径」插进这一栏的输入框（与主栏同语义——
// 发送后由 Agent 自己读文件），项目外的文件也可以
async function attachSplitFiles(pane) {
  const r = await pickPath("file").catch(() => ({ error: "no-picker" }));
  if (r.error) { paneNotice(pane, "打开文件选择框失败：" + (r.error === "no-picker" ? "未安装文件选择组件" : r.error)); return; }
  const paths = (r.paths || []).filter(Boolean);
  if (!paths.length) return;
  const input = pane.el.querySelector(".split-input");
  input.value = (input.value ? input.value + " " : "") + paths.map((p) => "@" + p).join(" ");
  input.focus();
  paneNotice(pane, `已引用 ${paths.length} 个文件（将随这一栏的消息一起发送）`);
}

// 列的模型显示与选择：会话级覆盖（session.model_switch），只影响这一栏
function paintSplitModel(pane, m) {
  pane.model = m;
  const el = pane.el.querySelector(".split-model");
  if (el) {
    // 与主栏 setModelChip 同一拼接格式（provider/model，无空格），两栏显示一致
    (el.querySelector(".sm-txt") || el).textContent =
      m.model ? `${m.name}/${m.model}` : m.name;
    el.title = m.overridden
      ? "这一栏独立使用的模型（点击更换）"
      : "跟随全局模型（点击为这一栏单独指定）";
  }
}

// 列工具条开关的视觉同步（.plan 蓝 / .on 紫/绿，与主栏同款）
function syncSplitBarToggles(pane) {
  const q = (role) => pane.el.querySelector(`[data-role="${role}"]`);
  const plan = q("plan"), coop = q("coop");
  if (plan) plan.classList.toggle("plan", !!pane.planMode);
  if (coop) {
    coop.classList.toggle("on", !!(pane.rtOn || pane.teamOn || pane.advOn));
    coop.classList.toggle("coop-rt", !!pane.rtOn && !pane.teamOn && !pane.advOn);
    coop.classList.toggle("coop-team", !!pane.teamOn && !pane.advOn);
    coop.classList.toggle("coop-adv", !!pane.advOn);
    const st = coop.querySelector(".cp-state");
    if (st) {
      st.hidden = !pane.rtOn && !pane.teamOn && !pane.advOn;
      st.textContent = pane.rtOn ? "圆桌" : pane.teamOn ? "团队" : pane.advOn ? "对抗" : "";
    }
    coop.title = pane.rtOn ? "圆桌（每栏独立）：本栏消息由多个模型共同思考。点击可换模式"
      : pane.teamOn ? "团队（每栏独立）：本栏消息组建团队协作。点击可换模式"
      : pane.advOn ? "对抗（每栏独立）：本栏消息做对抗性审查。点击可换模式"
      : "协作（每栏独立）：圆桌 / 团队 / 对抗，点击选择。分阶段报告请从侧栏打开接力工作台";
  }
}

// 列内协作统一菜单：圆桌 / 团队 / 对抗 / 关闭 多选一，写这一栏自己的开关
// （pane.rtOn/pane.teamOn/pane.advOn 随本栏发送带上）；成员/总管指定仍到主栏
function showSplitCoopMenu(pane, btn) {
  const old = pane.el.querySelector(".coop-menu");
  if (old) { old.remove(); return; }
  const menu = document.createElement("div");
  menu.className = "rt-menu coop-menu";
  const tip = document.createElement("div");
  tip.className = "rt-menu-tip";
  tip.textContent = "协作模式（本栏，发送后复位）：";
  menu.appendChild(tip);
  const modes = [
    { key: "off", label: "不用协作", active: !pane.rtOn && !pane.teamOn && !pane.advOn },
    { key: "rt", label: "圆桌", active: pane.rtOn },
    { key: "team", label: "团队", active: pane.teamOn },
    { key: "adv", label: "对抗", active: pane.advOn },
  ];
  for (const m of modes) {
    const b = document.createElement("button");
    b.className = "coop-mode-item" + (m.active ? " active" : "");
    b.innerHTML = `<span class="coop-mode-check">${m.active ? "✓" : ""}</span>` +
      `<span class="coop-mode-body"><span class="coop-mode-label">${m.label}</span></span>`;
    b.onclick = () => {
      if (m.key === "rt") { pane.rtOn = !pane.rtOn; if (pane.rtOn) { pane.teamOn = pane.advOn = false; } }
      else if (m.key === "team") { pane.teamOn = !pane.teamOn; if (pane.teamOn) { pane.rtOn = pane.advOn = false; } }
      else if (m.key === "adv") { pane.advOn = !pane.advOn; if (pane.advOn) { pane.rtOn = pane.teamOn = false; } }
      else { pane.rtOn = pane.teamOn = pane.advOn = false; }
      syncSplitBarToggles(pane);
      menu.remove();
    };
    menu.appendChild(b);
  }
  const sep = document.createElement("div");
  sep.className = "coop-sep";
  menu.appendChild(sep);
  const hint = document.createElement("div");
  hint.className = "rt-menu-tip";
  hint.textContent = "指定成员/总管：把会话切到主栏后操作";
  menu.appendChild(hint);
  pane.el.appendChild(menu);
  // fixed 定位贴着按钮向上展开，横向收敛在窗口内（rect 是物理像素，除回 uiScale）
  const r = btn.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.top = "auto";
  menu.style.bottom = (window.innerHeight / uiScale - r.top / uiScale + 6) + "px";
  menu.style.left = Math.max(8, Math.min(r.left / uiScale, window.innerWidth / uiScale - 300)) + "px";
  menu.style.right = "auto";
  const close = (e) => {
    if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
      menu.remove();
      document.removeEventListener("click", close, true);
    }
  };
  document.addEventListener("click", close, true);
}

// 分屏列的模型选择菜单：复用模型菜单样式，确认走 session.model_switch
let splitModelRows = [];
async function pickSplitModel(pane, anchor) {
  const menu = document.getElementById("split-model-menu");
  if (!menu.classList.contains("hidden")) { menu.classList.add("hidden"); return; }
  menu.innerHTML = '<div class="mm-empty">加载中…</div>';
  menu.classList.remove("hidden");
  const detail = await request("config.providers").catch(() => null);
  if (!detail || menu.classList.contains("hidden")) return;
  const cur = await request("session.model_get", { id: pane.sid }).catch(() => null);
  splitModelRows = buildModelRows(detail);
  menu.innerHTML = "";
  const addRow = (row) => {
    const on = cur && cur.overridden && cur.name === row.name && cur.model === row.model;
    const b = document.createElement("button");
    b.className = "mm-item" + (on ? " active" : "");
    b.innerHTML = auxMiniAvatar(row.name) +
      `<span class="mm-model">${on ? "✓ " : ""}${escapeHtml(row.model)}</span>` +
      `<span class="mm-prov${row.hasKey ? "" : " no-key"}">${row.hasKey ? "" : "⚠ "}${escapeHtml(row.label)}</span>`;
    b.title = row.hasKey
      ? `这一栏使用 ${row.label} / ${row.model}（只影响本栏）`
      : `「${row.label}」还没配置 API Key`;
    b.onclick = async () => {
      menu.classList.add("hidden");
      try {
        const r = await request("session.model_switch", { id: pane.sid, name: row.name, model: row.model });
        paintSplitModel(pane, { name: r.provider, model: r.model, overridden: true });
        paneNotice(pane, `✓ 这一栏将使用「${r.provider} / ${r.model}」，主栏不变`);
      } catch (err) { paneNotice(pane, "切换失败: " + err.message); }
    };
    menu.appendChild(b);
  };
  const rows = splitModelRows;
  for (const [label, pred] of [["默认", (r) => r.preset], ["自定义", (r) => !r.preset]]) {
    const hits = rows.filter(pred);
    if (!hits.length) continue;
    // 分组小标题：无样式的裸 div（旧的分组头类已废弃下线，见
    // test_frontend_wiring 的整库禁用断言；它本就没有任何 CSS 规则）
    const head = document.createElement("div");
    head.textContent = label;
    menu.appendChild(head);
    hits.forEach(addRow);
  }
  if (!rows.length) menu.innerHTML = '<div class="mm-empty">还没有可用的模型服务</div>';
  const r = anchor.getBoundingClientRect();
  const vh = window.innerHeight / uiScale;
  menu.style.top = "auto";
  menu.style.bottom = (vh - r.top / uiScale + 6) + "px";
  menu.style.left = Math.max(8, r.right / uiScale - 250) + "px";
}
document.addEventListener("click", (e) => {
  const menu = document.getElementById("split-model-menu");
  if (menu && !menu.classList.contains("hidden") &&
      !menu.contains(e.target) && !e.target.closest('[data-role="model"]')) menu.classList.add("hidden");
});

// 分屏列头状态：运行中蓝点 + 等确认金标
function paintSplitHead(pane) {
  const head = pane.el.querySelector(".split-head");
  let dot = head.querySelector(".split-run");
  if (pane.running && !dot) {
    dot = document.createElement("span");
    dot.className = "split-run";
    dot.title = "这一栏的会话正在运行";
    head.insertBefore(dot, head.querySelector(".split-title"));
  } else if (!pane.running && dot) {
    dot.remove();
  }
  let lock = head.querySelector(".split-perm");
  if (pane.needsPerm && !lock) {
    lock = document.createElement("span");
    lock.className = "split-perm";
    lock.textContent = "🔒";
    lock.title = "这一栏的会话在等待确认——点击到主栏打开并处理";
    // 确认面板是主栏单例：把会话提升到主栏（正常激活路径）才能点允许/拒绝。
    // 待确认数据此刻挂在列对象上（permission_request 路由进列时 showPermission
    // 写的是 pane.permData），必须一并转移到目标标签——新标签的 permData 初始
    // 是 null，activateTab 只认 tab.permData 恢复卡，不带过去就是断头：轮次
    // 在权限门上无限等待、确认条永远弹不出来
    lock.onclick = () => {
      const perm = pane.permData;
      const t = tabFor(pane.sid);
      if (t) {
        if (perm && !t.permData) { t.needsPerm = true; t.permData = perm; }
        activateTab(t); // activateTab 在 tab.needsPerm && tab.permData 时恢复卡
      } else {
        const nt = openTabForSession(pane.sid, pane.title);
        if (perm && nt && !nt.permData) {
          nt.needsPerm = true;
          nt.permData = perm;
          showPermission(perm); // activateTab 的恢复点已过：这里显式挂卡
        }
      }
    };
    head.insertBefore(lock, head.querySelector(".split-title"));
  } else if (!pane.needsPerm && lock) {
    lock.remove();
  }
}

// 焦点态（主栏与各列统一）：最近点击的「面」正常显示，其余一律降饱和变灰——
// 点列时主栏也变灰；点回主栏（对话区/输入栏/切标签）时各列变灰，一眼可辨
// 输入将去往哪个面
function focusSplitPane(pane) {
  if (!pane.el || !pane.el.isConnected) return; // 列已摘除（关列的点击冒泡到列头僵尸监听）：不得再上灰
  if (!splitPanes.some((q) => q.el && q.el.isConnected)) return; // 已无任何活列：同样不上灰
  chatMainEl.classList.add("main-dimmed");
  splitPanes.forEach((p) => {
    p.el.classList.toggle("dimmed", p !== pane);
    p.el.classList.toggle("focused", p === pane);
  });
}

function focusMainPane() {
  if (!splitPanes.length) return; // 没有列就没有焦点之分
  chatMainEl.classList.remove("main-dimmed");
  splitPanes.forEach((p) => {
    p.el.classList.remove("focused");
    p.el.classList.add("dimmed");
  });
}

function closeSplitPane(pane, opts = {}) {
  if (refFace === pane) closeRefMenu();
  if (menuFace === pane) hideInputMenu();
  if (ctxPopFace === pane) closeCtxPop(true);
  // 正在录音且录的就是这一栏：列都要没了，录音必须收尾并丢弃，
  // 否则录音计时器与麦克风轨道继续挂着、转写文本还会往已摘除的输入框写
  if (voiceFace === pane) voiceReset();
  // 观察器断开 + 大字段释放：列关掉后这些对象没人再用，但引用仍活在某些
  // 闭包/观察器/标签字典里——反复开关列会持续积累（长会话里 _historyAll
  // 持着整会话消息引用，最重）。
  if (pane._cpRO) { pane._cpRO.disconnect(); pane._cpRO = null; }
  pane._historyGen = (pane._historyGen || 0) + 1; // 在途的分批渲染 rAF 作废
  pane._historyAll = null;
  pane._historyStart = 0;
  pane._historyLoading = false; // 在途的 messages.earlier 翻页回来即作废（gen 已推进）
  pane._jumpSeqGen = (pane._jumpSeqGen || 0) + 1; // 在途的搜索跳转轮询作废
  if (pane._toolCards) pane._toolCards.clear();
  pane.images.length = 0;
  pane.refs.length = 0;
  pane.streamingEl = null;
  // logEl 对象保留（路由/收尾路径可能还拿着它，置 null 会炸出 TypeError）
  // 但清空子节点：消息 DOM 是内存大头，摘除后由 GC 回收
  if (pane.logEl) {
    // 图片懒加载观察器显式注销：IntersectionObserver 对已摘除节点不再回调，
    // 但显式 unobserve 能不让浏览器持着这批 img 的引用（大图会话更重）
    pane.logEl.querySelectorAll("img").forEach((img) => {
      try { _lazyImgs.unobserve(img); } catch (e) { /* 已注销 */ }
    });
    pane.logEl.textContent = "";
  }
  if (routeTab === pane) routeTab = null; // 残留路由指针清理（否则下一条 addNotice 会写进死列）
  pane.el.remove();
  const i = splitPanes.indexOf(pane);
  if (i >= 0) splitPanes.splice(i, 1);
  if (!splitPanes.length) {
    splitPaneEl.classList.add("hidden");
    chatMainEl.classList.remove("has-split");
    chatMainEl.classList.remove("main-dimmed"); // 列全关：主栏焦点态一并撤
    if (opts.restore !== false) restoreMainAfterSplit(pane); // 空墙不留给用户
  } else if (!splitPanes.some((p) => p.el.classList.contains("focused"))) {
    focusMainPane(); // 关掉的是聚焦列：焦点回落主栏，其余列变灰
  }
}

function closeSplitPanesForSids(sidSet, opts = {}) {
  splitPanes.filter((p) => sidSet.has(p.sid)).forEach((p) => closeSplitPane(p, opts));
}

/** 关掉最后一列分屏时，主栏不能留一堵空墙。
 *
 *  分屏的语义是「把会话从主栏搬进分屏」（openSplitPane 会先收掉主栏冲突标签），
 *  那么关掉最后一列就应把会话搬回主栏——否则主栏常停在空会话/空白标签上，
 *  主栏当前有内容（消息/欢迎卡/提示）时不搬，不抢已有会话的焦点；跨项目只读
 *  列与运行中的列不搬。（另注：用户当时报的「关了分屏主栏还是灰」是独立 bug——
 *  关列的点击冒泡到已摘除列的聚焦监听，把 main-dimmed 重新加了回去；已在
 *  focusSplitPane 加活列守卫，CSS 侧再以 :not(:has(.split-col)) 兜底。）
 */
function restoreMainAfterSplit(pane) {
  if (pane.readonly || pane.running) return;
  const t = activeTab;
  if (!t || t.running) return;
  // 空不空看「真内容」：搬家提示这类 .msg.notice 是过程性通知，不算内容——
  // 否则 openSplitPane 留下的「已从主栏搬进分屏」会顶掉搬回，主栏照样空墙
  const kids = t.logEl ? [...t.logEl.children] : [];
  const real = kids.filter((el) => !el.classList.contains("notice"));
  if (real.length) return;
  kids.forEach((el) => { if (el.classList.contains("notice")) el.remove(); }); // 会话回来了，搬家提示作废
  openTabForSession(pane.sid, pane.title); // 搬回：接管空标签或新开标签并激活
}

/** 新建会话标签。
 *
 *  原则统一：只要点击，就在当前高亮的项目下真正新建一个会话。persist=true
 *  （默认）立即向后端落库——侧栏列表只陈列已落库的会话，不落库就永远不会
 *  出现（用户报的「新建后侧栏没有新标签」正是这个错位）。发送首条消息时
 *  send() 见 sid 已存在就直接用，欢迎页照旧。creatingTab 只挡「一次请求
 *  还在路上」的连击，不是行为规则；连点会落下多个空会话，交给
 *  「清理空会话」兜底。
 *
 *  persist=false：仅启动恢复时无可恢复会话用——保持懒创建（后端
 *  open_initial_session 的同一取舍：启动不落库，免得每次启动都堆空会话），
 *  发首条消息时才落。落库失败时也退回这条路径，只弹提示。 */
async function startNewTab(persist = true) {
  // 新建会话＝回到当前项目语境：「点组头」借来的高亮交回（新会话激活后跟它走）
  groupedClickGk = null;
  const blank = () => {
    // 新建前把旧活动标签的输入区状态写回它的条目：草稿/贴图/开关随会话记忆，
    // 不写回就被空标签的默认态顶掉，切回旧会话全部丢失（与 activateTab /
    // closeTab 同一口径）
    if (activeTab) saveComposerState(activeTab.sid);
    const t = newTabObj(null, "");
    chatTabs.push(t);
    activeTab = t;
    attachTabLog(t);
    currentSessionId = null;
    activeSessionSid = null;
    restoreComposerState(null); // 不过 activateTab：输入区恢复默认态（空标签口径）
    refreshReasoning(); // 空标签：思考徽章回到全局口径
    clearTodoPanel();
    showWelcome();
    renderTabs();
    refreshSessions();
    return t;
  };
  if (!persist) return blank();
  if (creatingTab) return activeTab;
  creatingTab = true;
  try {
    const s = await request("session.new", {});
    // 换指前写回旧活动标签（await 期间用户可能又敲了字——必须在 activeTab
    // 换指前读旧值，与 activateTab 同一口径）
    if (activeTab) saveComposerState(activeTab.sid);
    const t = newTabObj(s.id, s.title || "");
    chatTabs.push(t);
    activeTab = t;
    attachTabLog(t);
    currentSessionId = s.id;
    activeSessionSid = s.id;
    restoreComposerState(s.id); // 不过 activateTab：新会话从默认态开始
    refreshActiveSessionBadges(t); // 默认模型会话的徽章与会话生效模型对齐
    clearTodoPanel();
    showWelcome();
    renderTabs();
    // 新会话归属当前项目：列表切回本项目视图（哪怕之前在看快聊/远程），新行马上可见
    if (classicViewGk != null) {
      classicViewGk = null;
      refreshProjects();
    }
    refreshSessions(); // 侧栏立即出现新会话行
    return t;
  } catch (e) {
    const t = blank();
    addNotice("会话创建失败，已先打开欢迎页：" + e.message);
    return t;
  } finally {
    creatingTab = false;
  }
}

/** 「新建会话」的落点＝侧栏项目区高亮的那一项（点哪行亮哪行，新会话就建哪）：
      高亮快聊 → 新建快聊会话（列表不动，新行马上出现在快聊里）；
      高亮其他真实项目 → 先切过去再新建（同组头 ＋）；
      高亮「远程连接 / 其他」→ 没有本地工作目录，就地提示不建；
      其余（当前项目 / 无所依）→ startNewTab 照旧在当前项目里建。
    左上按钮 / Ctrl+N / 窄栏＋ / 标签栏＋ / 命令 /new 共用这一处；
    组头与项目行自己的 ＋ 不走这里——它们本身就是明确落点。 */
async function newSessionFromHighlight() {
  const litKey = sidebarView === "grouped" ? lastGroupedHighlightKey : classicViewGk;
  if (litKey === "quick") {
    try {
      const r = await request("session.new_task", {});
      await openTabForSession(r.id, r.title);
      refreshSessions();
    } catch (e) { addNotice("新建快聊失败：" + e.message); }
    return;
  }
  if (litKey === "loose") {
    addNotice("「其他」里的会话属于已被移除的项目，不能在这里新建");
    return;
  }
  if (litKey && String(litKey).startsWith("remote:")) { // 经典视图点亮的远程行
    addNotice("「远程连接」的对话来自飞书/微信渠道，不能在这里新建");
    return;
  }
  if (litKey && String(litKey).startsWith("proj:")) { // 经典视图点亮的「看其他项目」行
    const pid = String(litKey).slice("proj:".length);
    const pprojects = ((await request("project.list").catch(() => null)) || {}).projects || [];
    const tp = pprojects.find((p) => String(p.id) === pid);
    if (tp && !tp.is_current && tp.root_path) {
      try {
        await request("project.switch", { path: tp.root_path });
        await applyWorkspaceData(await fetchWorkspaceData());
      } catch (e) {
        addNotice("切换到该项目失败：" + e.message);
        return;
      }
    }
    startNewTab(); // 切换成功（或本就是当前项目）后照常新建；失败路径上面已 return
    return;
  }
  const projects = ((await request("project.list").catch(() => null)) || {}).projects || [];
  const lit = projects.find((p) => litKey != null && String(p.id) === String(litKey));
  if (lit && !lit.is_current) {
    if (!lit.root_path) { // 「远程连接」固定项目：同经典视图远程行
      addNotice("「远程连接」的对话来自飞书/微信渠道，不能在这里新建");
      return;
    }
    try {
      await request("project.switch", { path: lit.root_path });
      await applyWorkspaceData(await fetchWorkspaceData());
    } catch (e) {
      addNotice("切换到该项目失败：" + e.message);
      return;
    }
    startNewTab();
    addNotice(`已切换到项目「${lit.name}」，新建会话`);
    return;
  }
  startNewTab();
}

// 内置示例任务：场景模板清单拉不到时的回落（见 showWelcome）
const WELCOME_SAMPLES = [
  { label: "🎮 写一个贪吃蛇网页并测试", q: "帮我在这个目录里创建一个贪吃蛇网页游戏，写完自己打开测试一下" },
  { label: "🗂 总结当前项目结构", q: "看看当前项目的结构，给我一份架构总结" },
  { label: "🧹 整理当前目录文件", q: "把目录下所有文件按类型整理进子文件夹，并列出你做了什么" },
];

// 场景模板前 6 条（打包内官方技能清单）：进程内缓存一次，失败不缓存
// （下次进欢迎页重试），始终回落内置示例
let welcomeSceneCache = null;
async function welcomeSceneSamples() {
  if (welcomeSceneCache) return welcomeSceneCache;
  try {
    const r = await request("skills.gallery");
    const items = (r.templates || []).slice(0, 6);
    if (items.length) {
      welcomeSceneCache = items.map((t) => ({
        label: "🧩 " + (t.display_name || t.name),
        title: t.description,
        q: `请使用「${t.name}」技能完成任务：${t.description}`,
      }));
    }
  } catch (e) { /* 加载失败：回落内置示例 */ }
  return welcomeSceneCache;
}

function welcomeSamplesHtml(samples) {
  return samples.map((s) =>
    `<button class="w-sample" data-q="${escapeHtml(s.q)}" title="${escapeHtml(s.title || s.q)}">${escapeHtml(s.label)}</button>`
  ).join("");
}

function bindWelcomeSamples(host) {
  host.querySelectorAll(".w-sample").forEach((btn) => {
    btn.onclick = () => {
      document.getElementById("input").value = btn.dataset.q;
      send();
    };
  });
}

function showWelcome() {
  const host = curLog();
  // 设置 · 界面与通知 关掉欢迎卡后，空白会话保持空白（四个调用点统一从这里出口）
  if (!welcomeOn) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = `
    <div class="welcome">
      <div class="w-title"><i class="w-logo" aria-hidden="true"></i>欢迎使用 SkySheep</div>
      <div class="w-sub">一个跑在你电脑上的 AI Agent 工作台。试着给它一个完整任务，比如：</div>
      <div class="w-samples">${welcomeSamplesHtml(WELCOME_SAMPLES)}</div>
      <div class="w-note">写文件、执行命令等敏感操作都会先征求你的确认。</div>
    </div>`;
  bindWelcomeSamples(host);
  // 示例任务优先用场景模板（A2）：先画内置示例不空等，清单取到前 6 条就原位替换；
  // 加载失败 / 清单为空时内置示例原地不动（回落）。欢迎卡已被清掉时不再写回。
  welcomeSceneSamples().then((samples) => {
    if (!samples) return;
    const box = host.querySelector(".w-samples");
    if (!box) return;
    box.innerHTML = welcomeSamplesHtml(samples);
    bindWelcomeSamples(host);
  }).catch(() => {});
}

function addNotice(text) {
  const d = document.createElement("div");
  d.className = "msg notice" + (text.includes("\n") ? " multiline" : "");
  d.textContent = text;
  curLog().appendChild(d);
  scrollLog();
}

/** 分屏列自己的操作提示：必须写进那一列。
 *  addNotice 落在 curTab（routeTab || activeTab），分屏里的按钮操作没有路由，
 *  直接调会把「这一栏将使用…」写进主栏——两块对话串台，用户看到的就是这个。 */
function paneNotice(pane, text) {
  if (!pane || !pane.logEl) { addNotice(text); return; }
  withTab(pane, () => addNotice(text));
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
  t._streamBody = null; // 增量渲染状态槽随新气泡重建（见 renderStreamPart）
  t.thinkEl = null;
  t.thinkText = "";
  t.thinkMs = 0;
  t.thinkT0 = 0;
  t.thinkT1 = 0;
}

// ---------- 流式增量渲染 ----------
// 流式期间每 80ms 的重渲只对「未定稿尾块」做：已定稿前缀按段落（空行）切块，
// 且要求块内 ``` 围栏配对，保证每块单独 renderMarkdown 与整文渲染同构
// （本渲染器里列表/表格/引用块都止于空行，见 mdTables/mdLists）；块一定稿
// 就只渲染一次、不再动它，单次渲染成本从「全量文本」降到「尾块」，整轮流式
// 的渲染总开销从 O(n²) 降为线性。光标 ▍ 是独立兄弟元素，不再拼进 HTML。
// 找最靠后的可定稿切点：[from, cut) 以空行结尾且围栏配对；找不到返回 from。
function streamSealCut(text, from) {
  let end = text.length;
  for (;;) {
    const idx = text.lastIndexOf("\n\n", end - 1);
    if (idx < from) return from;
    const cut = idx + 2;
    const chunk = text.slice(from, cut);
    if ((chunk.match(/^[ \t]*```/gm) || []).length % 2 === 0) return cut;
    // 切点落在未闭合围栏里：回退到围栏开启行之前的空行再试
    const open = chunk.lastIndexOf("```");
    if (open < 0) return from;
    end = from + open;
  }
}
// 把 [sealed, cut) 定稿进容器（st.tail 的前置位置，直接落在 .md 下，与定稿
// 整文渲染的 DOM 形状一致），再重画尾块。st 是 initStreamBody 的状态槽。
function renderStreamPart(st, text, sealed, cut) {
  if (cut > sealed) {
    st.tail.insertAdjacentHTML("beforebegin", renderMarkdown(text.slice(sealed, cut)));
  }
  st.tail.innerHTML = renderMarkdown(text.slice(cut));
  return cut;
}
function initStreamBody(md, withCursor) {
  md.innerHTML = "";
  const tail = document.createElement("div");
  md.appendChild(tail);
  let cursor = null;
  if (withCursor) {
    cursor = document.createElement("p");
    cursor.textContent = "▍";
    md.appendChild(cursor);
  }
  return { tail, cursor, sealed: 0 };
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
  // 节流渲染：delta 频率远高于人眼需要；定稿前缀不再逐 tick 重跑 markdown
  // （增量方案见上方说明），流结束由 finishAssistant 全量渲染兜底
  if (!t._streamRenderTimer) {
    t._streamRenderTimer = setTimeout(() => {
      t._streamRenderTimer = null;
      if (!t.streamingEl) return;
      if (!t._streamBody) t._streamBody = initStreamBody(t.streamingEl.firstElementChild, true);
      const text = t.streamingText;
      t._streamBody.sealed = renderStreamPart(
        t._streamBody, text, t._streamBody.sealed, streamSealCut(text, t._streamBody.sealed));
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
  // 节流渲染（同 appendStream 的增量方案）：思考文本往往比正文还长
  if (!t._thinkRenderTimer) {
    t._thinkRenderTimer = setTimeout(() => {
      t._thinkRenderTimer = null;
      if (!t.thinkEl || !t.thinkText) return;
      const body = t.thinkEl.querySelector(".think-body .md");
      if (!body) return;
      if (!t._thinkBody) t._thinkBody = initStreamBody(body, false);
      const text = t.thinkText;
      t._thinkBody.sealed = renderStreamPart(
        t._thinkBody, text, t._thinkBody.sealed, streamSealCut(text, t._thinkBody.sealed));
      scrollLog();
    }, 80);
  }
}

function finishThinking(t) {
  if (!t || !t.thinkEl || !t.thinkText) return;
  // 补上节流攒下的最后一次渲染（定稿按整文精确重画一次），再更新折叠摘要
  if (t._thinkRenderTimer) {
    clearTimeout(t._thinkRenderTimer);
    t._thinkRenderTimer = null;
    const body = t.thinkEl.querySelector(".think-body .md");
    if (body) body.innerHTML = renderMarkdown(t.thinkText);
  }
  t._thinkBody = null;
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
  // 定稿按整文精确重画一次（覆盖增量渲染的临时结构，光标元素一并移除）
  t.streamingEl.firstElementChild.innerHTML = renderMarkdown(t.streamingText);
  t._streamBody = null;
  if (rtMeta) {
    if (rtMeta.mode === "adversarial") {
      // 对抗报告：徽标 + 折叠上方的问题清单卡（报告已流进本气泡）
      addAdvBadge(t.streamingEl, rtMeta);
      t.streamingEl._rtMeta = rtMeta; // 重新生成时据此重跑同样的对抗配置
      rtMeta.seq = seq;
      t.advSeq = seq;
      t.advMeta = rtMeta;
      advAttachTransfer(t, rtMeta, seq);
      if (t.advCard && t.advCard.isConnected && !t.advCard.classList.contains("folded") && t.advFold) {
        t.advFold();
      }
    } else {
      addRtBadge(t.streamingEl, rtMeta);
      t.streamingEl._rtMeta = rtMeta; // 重新生成时据此重跑同样的圆桌配置
      // 融合结论已就位：自动折叠成员草稿卡（此前一直展开，占着大块空白）；标题栏随时可展开回看
      const rtEls = t.rtMemberEls || [];
      const allSettled = rtEls.length &&
        rtEls.every((x) => x.card.classList.contains("ok") || x.card.classList.contains("err")
          || x.card.classList.contains("skipped"));
      if (t.rtCard) {
        // 对照卡底部说明：A/B 与圆桌的结论去向不同，但不推断共识/分歧
        const note = t.rtCard.querySelector(".rt-note");
        if (note) {
          note.textContent = rtMeta.mode === "compare"
            ? "对照各方观点，展开全文查看完整回答；所选模型的回答见下方。"
            : "对照各方观点，展开全文查看完整回答，融合结论见下方回答。";
        }
        // 自动折叠后展开时用的副标题（rtMemberFinished 的进度文案已过时）
        t.rtCard._rtSubText = rtMeta.mode === "compare"
          ? `${rtEls.length} 个模型 · 已定稿 · 展开查看各成员草稿`
          : `${rtEls.length} 个模型 · 草稿完成，融合结论见下方回答`;
      }
      if (rtMeta.members && allSettled && t.rtCard && !t.rtCard.classList.contains("folded")) {
        t.rtCard.classList.add("folded");
        const foldBtn = t.rtCard.querySelector(".rt-fold");
        if (foldBtn) foldBtn.textContent = "展开";
        if (foldBtn) foldBtn.setAttribute("aria-expanded", "false");
        const sub = t.rtCard.querySelector(".rt-sub");
        if (sub) sub.textContent = `${rtEls.length} 个模型 · 已折叠 · 点击标题栏展开查看各成员草稿`;
      }
      // 融合结论已随本气泡落库（seq 就位）：挂「转为团队工单」入口（P5）
      const rtEx = rtToTeamActions(rtMeta, seq, t);
      if (rtEx && t.rtCard && t.rtCard.isConnected) t.rtCard.appendChild(rtEx);
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

// 引用成员草稿追问：把草稿以引用块形式填进输入框（截断防超长）。
// tab 给出时按发起面分流：分屏列的成员卡回填该列输入框（焦点也留在列里），
// 不再固定写主栏——列里点「引用追问」后内容串去主栏，顺手一发送就发错会话
function quoteRoundtableDraft(provider, model, text, tab) {
  const raw = (text || "").trim();
  if (!raw) { addNotice("这份草稿还没有内容"); return; }
  const clipped = raw.length > 1500 ? raw.slice(0, 1500) + "\n…（草稿过长，已截断）" : raw;
  const quoted = clipped.split("\n").map((line) => "> " + line).join("\n");
  const pane = tab && splitPanes.includes(tab) ? tab : null;
  const input = pane ? pane.el.querySelector(".split-input") : document.getElementById("input");
  if (!input) {
    // 只读列没有输入框（跨项目列等）：给可读提示，不静默抛错——实时圆桌卡
    // 也走这条路径，光靠挂按钮时不挂不住所有来源
    addNotice("这一栏是只读列（会话属于其他项目），不能在这里追问");
    return;
  }
  const existing = input.value.trim();
  input.value = `【引用圆桌成员 ${provider}/${model} 的草稿】\n${quoted}\n\n我的追问：`
    + (existing ? "\n" + existing : "");
  if (!pane) autoGrowInput(); // 主栏输入框随内容长高；列输入框是固定两行
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

// 一批成员卡片的公共 DOM：head + grid + 底部说明；返回 {card, grid, sub, note}
// note 只陈述「并列展示、不推断共识/分歧」这一事实——现有协议没有结构化的
// 立场/共识数据，界面不凭空生成共识或分歧区块。
function buildRtCardShell(members, subText, noteText) {
  const card = document.createElement("div");
  card.className = "roundtable";
  const head = document.createElement("div");
  head.className = "rt-head";
  const sub = document.createElement("span");
  sub.className = "rt-sub";
  sub.textContent = subText;
  card._rtSubText = subText; // 展开态复用的最新副标题（轮次进度会覆盖它）
  const title = document.createElement("span");
  title.className = "rt-title";
  title.textContent = "👥 圆桌 · 观点对照";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "折叠";
  fold.setAttribute("aria-expanded", "true");
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    fold.setAttribute("aria-expanded", folded ? "false" : "true");
    sub.textContent = folded
      ? `${members.length} 个模型 · 已折叠 · 点击展开查看各成员草稿`
      : (card._rtSubText || subText);
    // 折叠时网格 display:none 量不出高度；展开后重算「展开全文」入口
    if (!folded && card._rtMembers) card._rtMembers.forEach(rtSyncClamp);
  };
  fold.onclick = (e) => { e.stopPropagation(); toggleFold(); };
  head.onclick = (e) => { if (e.target !== fold) toggleFold(); };
  head.append(title, sub, fold);
  const grid = document.createElement("div");
  grid.className = "rt-grid";
  // 按成员数定列数：1/2/3 各成一列排一行，4 及以上 2 列（2×N）——
  // 上限三列；4 个时若排 3 列会出现 3+1 孤行，视觉更乱
  grid.style.setProperty("--rt-cols", members.length >= 4 ? 2 : Math.max(1, members.length));
  const note = document.createElement("div");
  note.className = "rt-note";
  note.textContent = noteText
    || "对照各方观点，展开全文查看完整回答。";
  card.append(head, grid, note);
  return { card, grid, sub, note };
}

// 成员卡的公共 DOM：序号 + head + body + foot（展开 / 用量 / 引用追问）
// 返回 {el, mcard, statusEl, roundEl, tokensEl, footEl, bodyEl, quoteBtn, moreBtn}
function buildRtMemberCard(m, index) {
  const mcard = document.createElement("div");
  mcard.className = "rt-member";
  const idx = Number.isInteger(index) ? index + 1 : 0;
  mcard.innerHTML =
    `<div class="rt-m-head"><span class="rt-m-idx">${idx || "·"}</span>` +
    `<span class="rt-m-name">${escapeHtml(m.provider)}</span>` +
    `<span class="rt-m-model">${escapeHtml(m.model)}</span>` +
    `<span class="rt-m-role"${m.role ? "" : " hidden"}>${m.role ? escapeHtml(rtRoleLabel(m.role)) : ""}</span>` +
    '<span class="rt-m-round" hidden></span>' +
    '<span class="rt-m-status">⋯</span></div>' +
    '<div class="rt-m-body"><div class="md"></div></div>' +
    '<div class="rt-m-foot" hidden><span class="rt-m-tokens"></span>' +
    '<button class="rt-m-more" type="button" aria-expanded="false" hidden>展开全文</button>' +
    '<button class="rt-m-quote" type="button" title="把这份草稿引用进输入框，继续追问">引用追问</button></div>';
  return {
    card: mcard,
    status: mcard.querySelector(".rt-m-status"),
    roundEl: mcard.querySelector(".rt-m-round"),
    tokensEl: mcard.querySelector(".rt-m-tokens"),
    foot: mcard.querySelector(".rt-m-foot"),
    body: mcard.querySelector(".rt-m-body .md"),
    moreBtn: mcard.querySelector(".rt-m-more"),
    quoteBtn: mcard.querySelector(".rt-m-quote"),
  };
}

// 长草稿折叠/展开：body 的 max-height 由 CSS 控制（桌面 240px、窄屏 200px），
// 阈值按实际计算值取，媒体查询下调后「展开全文」入口不会漏出。
// 只有内容确实溢出才显示「展开全文」；展开状态在流式重渲与事件重画间保留。
const RT_DRAFT_CLAMP_PX = 240;
function rtSyncClamp(m) {
  if (!m || !m.card || !m.moreBtn) return;
  const body = m.body.parentElement;
  if (!body) return;
  const expanded = m.card.classList.contains("expanded");
  if (!expanded) {
    const maxH = parseFloat(getComputedStyle(body).maxHeight) || RT_DRAFT_CLAMP_PX;
    m._draftOverflow = body.scrollHeight > maxH + 6;
  }
  const show = !!m._draftOverflow;
  m.card.classList.toggle("can-expand", show);
  m.moreBtn.hidden = !show;
  m.moreBtn.textContent = expanded ? "收起" : "展开全文";
  m.moreBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
  m.moreBtn.title = expanded ? "收起这份草稿" : "展开完整草稿（不截断内容）";
}
function rtToggleDraft(m) {
  if (!m || !m.card) return;
  const expanded = m.card.classList.toggle("expanded");
  if (expanded) m._draftOverflow = true; // 已确认可展开，收起后继续保持入口
  rtSyncClamp(m);
}
// 窗口宽度跨过 760/520 断点时 max-height 会变，已渲染的对照卡重新量一次
let rtClampResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(rtClampResizeTimer);
  rtClampResizeTimer = setTimeout(() => {
    document.querySelectorAll(".roundtable").forEach((card) => {
      (card._rtMembers || []).forEach(rtSyncClamp);
    });
  }, 150);
});

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
  t.rtMemberEls = members.map((m, i) => {
    const u = buildRtMemberCard(m, i);
    grid.appendChild(u.card);
    u.text = "";
    u.round = 0;
    u.finishedRound = -1;
    u.tokens = 0;
    u.moreBtn.onclick = (e) => { e.stopPropagation(); rtToggleDraft(u); };
    u.quoteBtn.onclick = (e) => {
      e.stopPropagation();
      quoteRoundtableDraft(m.provider, m.model, u.text, t);
    };
    return u;
  });
  card._rtMembers = t.rtMemberEls;
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
    m.card.classList.remove("ok", "err", "skipped", "expanded", "can-expand");
    m.card.classList.add("streaming");
    m._draftOverflow = false;
    if (m.moreBtn) m.moreBtn.hidden = true;
    m.status.textContent = "⋯";
    setMemberRound(m, t, r, r > 0 ? `第 ${r + 1} 轮修订` : `第 ${r + 1} 轮`);
    m.body.innerHTML = "";
  } else if (r === 0 && (t.rtRounds || 1) > 1) {
    setMemberRound(m, t, 0);
  }
  m.text += data.text || "";
  m.card.classList.add("streaming");
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
    m.card.classList.remove("ok", "err", "skipped", "expanded", "can-expand");
    m._draftOverflow = false;
    if (m.moreBtn) m.moreBtn.hidden = true;
    m.body.innerHTML = "";
  }
  m.card.classList.remove("streaming");
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
  rtSyncClamp(m);
  // 按轮结算：本轮所有成员的结束事件到齐（含 skipped）→ 更新进度文案
  const total = t.rtRounds || 1;
  const allNow = t.rtMemberEls.every((x) => x.finishedRound >= r);
  if (allNow) {
    const sub = t.rtCard.querySelector(".rt-sub");
    const msg = (r + 1 < total)
      ? `第 ${r + 1} / ${total} 轮完成，进入修订…`
      : "草稿完成，主席融合中…";
    t.rtCard._rtSubText = msg;
    if (sub) sub.textContent = msg;
  }
}

// 历史回放：从持久化的元数据重建圆桌卡片（默认折叠，可展开回看草稿）。
// tab 供「引用追问」按发起面回填对应输入框（分屏列里回放的历史卡也要回列）
function buildRtReplayCard(meta, tab, msg) {
  const members = meta.members || [];
  const compare = meta.mode === "compare";
  const rounds = meta.rounds || 1;
  // 展开态副标题先在壳里挂好：fold 切换回展开时用的是这一份（不能拿折叠文案
  // 当 subText，否则展开后仍写着「已折叠」）
  let expandedSub = `${members.length} 个模型 · 各成员草稿（独立成稿）`;
  if (rounds > 1) expandedSub += ` · 辩论 ${rounds - 1} 轮`;
  const { card, grid, sub } = buildRtCardShell(
    members,
    expandedSub,
    compare
      ? "对照各方观点，展开全文查看完整回答；所选模型的回答见下方。"
      : "对照各方观点，展开全文查看完整回答，融合结论见下方回答。"
  );
  card.classList.add("folded");
  card.querySelector(".rt-fold").textContent = "展开";
  card.querySelector(".rt-fold").setAttribute("aria-expanded", "false");
  sub.textContent = `${members.length} 个模型 · 已折叠 · 点击展开查看各成员草稿`;
  const memberEls = [];
  members.forEach((m, i) => {
    const u = buildRtMemberCard(m, i);
    const draft = m.draft || "";
    u.card.classList.add(m.status === "done" ? "ok" : "err");
    u.status.textContent = m.status === "done" ? "✓" : "✗";
    const tk = (m.input_tokens || 0) + (m.output_tokens || 0);
    if (tk) u.tokensEl.textContent = `≈${fmtTokens(tk)} tokens`;
    u.moreBtn.onclick = (e) => { e.stopPropagation(); rtToggleDraft(u); };
    if (draft.trim()) {
      u.body.innerHTML = renderMarkdown(draft);
      u.foot.hidden = false;
      if (tab && tab.readonly) {
        u.quoteBtn.remove(); // 只读列没有输入框可回填，不挂「引用追问」
      } else {
        u.quoteBtn.onclick = (e) => {
          e.stopPropagation();
          quoteRoundtableDraft(m.provider, m.model, draft, tab);
        };
      }
    } else {
      u.body.innerHTML = `<p class="dim">✗ ${escapeHtml(m.error || "作答失败")}</p>`;
    }
    grid.appendChild(u.card);
    memberEls.push(u);
  });
  card._rtMembers = memberEls;
  // 圆桌结论转工单（P5）：回放卡带消息 seq 即可挂入口（融合文本在库，前端不传）
  const rtEx = rtToTeamActions(meta, Number(msg && msg.seq) || 0, tab);
  if (rtEx) card.appendChild(rtEx);
  return card;
}

// ---------- 对抗卡片：四角色流水线进度 + 问题清单（聊天流内的审查台） ----------
// adversarial_started 建卡并保持展开，随阶段/问题/裁决事件实时更新；
// adversarial_finished 后折叠（终审报告流进下方回答气泡，清单可展开回看）。
const ADV_PHASES = [
  { key: "finder", label: "发现" },
  { key: "investigator", label: "调查" },
  { key: "advisor", label: "建议" },
  { key: "judge", label: "裁判" },
];
const ADV_SEVERITY_LABEL = { critical: "致命", high: "高", medium: "中", low: "低" };
const ADV_VERDICT_LABEL = { confirmed: "成立", refuted: "已推翻", partial: "部分成立", pending: "待定" };
// 问题清单的裁决筛选（统计条与筛选共用一份词表；键与元数据 verdict 一一对应）
const ADV_VERDICT_FILTERS = [
  ["all", "全部"],
  ["confirmed", "成立"],
  ["partial", "部分"],
  ["refuted", "推翻"],
  ["pending", "待定"],
];
const ADV_SEVERITY_FILTERS = [
  ["all", "全部严重度"],
  ["critical", "致命"],
  ["high", "高"],
  ["medium", "中"],
  ["low", "低"],
];
const advRoleLabel = (role) =>
  ({ finder: "发现者", investigator: "调查者", advisor: "建议者", judge: "裁判" })[role] || role || "";

function advRolesTitle(roles) {
  return (roles || [])
    .filter((r) => r && r.role)
    .map((r) => `${advRoleLabel(r.role)}：${r.provider}${r.model ? "/" + r.model : ""}`)
    .join("\n");
}

// 一行候选问题（实时与回放共用）：编号 + 严重度 + 类别 + 位置 + 裁决徽记，
// 下方是问题正文；依据/建议收进可展开的 details（默认收起，键盘可操作）。
function advFindingRow(f, opts = {}) {
  const row = document.createElement("div");
  row.className = "adv-finding";
  row.dataset.findingId = f.finding_id || f.id || "";
  const sev = f.severity || f.effective_severity || "medium";
  row.dataset.severity = sev;
  const verdict = f.verdict || "";
  row.dataset.verdict = verdict || "pending";
  const sevLabel = ADV_SEVERITY_LABEL[sev] || sev;
  const vLabel = verdict ? (ADV_VERDICT_LABEL[verdict] || verdict) : "";
  row.innerHTML =
    `<div class="adv-f-head">` +
    `<span class="adv-f-id">${escapeHtml(f.finding_id || f.id || "")}</span>` +
    `<span class="adv-f-sev sev-${escapeHtml(sev)}">${escapeHtml(sevLabel)}</span>` +
    `<span class="adv-f-cat">${escapeHtml(f.category || "")}</span>` +
    `<span class="adv-f-loc" title="${escapeHtml(f.location || "")}">${escapeHtml(f.location || "")}</span>` +
    (vLabel ? `<span class="adv-f-verdict verdict-${escapeHtml(verdict)}">${escapeHtml(vLabel)}</span>` : "<span class=\"adv-f-verdict\" hidden></span>") +
    `</div>` +
    `<div class="adv-f-desc md"></div>`;
  row.querySelector(".adv-f-desc").innerHTML = renderMarkdown(f.description || "");
  if (f.reason) row.title = f.reason;
  // 转圆桌议题（P5 跨形态流转）：把这条发现填进输入框当议题——只回填不自动
  // 发送，发不发、以什么形态（圆桌/追问）发由用户决定，不制造意外开销
  if (opts.tab && !opts.tab.readonly && opts.tab.sid) {
    const btn = tcEl("button", "adv-f-topic", "转议题");
    btn.title = "把这条发现填进输入框作为议题：选好圆桌模式与成员后发送";
    btn.onclick = (e) => {
      e.stopPropagation();
      const parts = [`【对抗发现 ${f.finding_id || f.id || ""}】${(f.description || "").trim()}`];
      if (f.evidence) parts.push(`（依据：${f.evidence}）`);
      if (f.solution) parts.push(`（修复建议：${f.solution}）`);
      const topic = parts.filter((s) => s.trim()).join("\n");
      const pane = splitPanes.includes(opts.tab) ? opts.tab : null;
      const input = pane ? pane.el.querySelector(".split-input") : document.getElementById("input");
      if (!input) { addNotice("这一栏没有输入框可回填"); return; }
      const existing = input.value.trim();
      input.value = existing ? existing + "\n" + topic : topic;
      if (!pane) autoGrowInput();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      addNotice("发现已填入输入框：选好圆桌模式与成员后发送即可。");
    };
    row.querySelector(".adv-f-head").appendChild(btn);
  }
  // 证据/依据/建议：按可用字段拼细节区（实时只有裁决后才有依据）
  const details = document.createElement("details");
  details.className = "adv-f-details";
  details.hidden = true;
  const summary = document.createElement("summary");
  summary.textContent = opts.replay && f.solution
    ? "证据、依据与修复建议" : "证据与裁决依据";
  const body = document.createElement("div");
  body.className = "adv-f-detail-body";
  details.append(summary, body);
  if (f.evidence) {
    const ev = document.createElement("div");
    ev.className = "adv-f-evidence dim";
    ev.innerHTML = "发现依据：" + renderMarkdown(f.evidence);
    body.appendChild(ev);
  }
  if (f.reason) {
    const reason = document.createElement("div");
    reason.className = "adv-f-reason dim";
    reason.innerHTML = "裁决依据：" + renderMarkdown(f.reason);
    body.appendChild(reason);
  }
  if (opts.replay && f.solution) {
    const fix = document.createElement("div");
    fix.className = "adv-f-fix";
    fix.innerHTML = "🛠 " + renderMarkdown(f.solution);
    body.appendChild(fix);
  }
  details.hidden = !body.children.length;
  row.appendChild(details);
  return row;
}

// 四阶段流程条：编号 + 名称 + 连接线。done/active 由 advSetPhase 切换，
// 回放时整条置 done（已经走完的流水线）。
function advBuildFlow() {
  const flow = document.createElement("div");
  flow.className = "adv-steps";
  flow.setAttribute("role", "list");
  flow.setAttribute("aria-label", "对抗审查四阶段");
  const stepEls = {};
  ADV_PHASES.forEach((p, i) => {
    const step = document.createElement("div");
    step.className = "adv-step";
    step.dataset.phase = p.key;
    step.setAttribute("role", "listitem");
    step.title = `第 ${i + 1} 阶段：${p.label}`;
    const num = document.createElement("span");
    num.className = "adv-step-num";
    num.textContent = String(i + 1);
    const name = document.createElement("span");
    name.className = "adv-step-name";
    name.textContent = p.label;
    step.append(num, name);
    flow.appendChild(step);
    stepEls[p.key] = step;
    if (i < ADV_PHASES.length - 1) {
      const link = document.createElement("span");
      link.className = "adv-step-link";
      link.setAttribute("aria-hidden", "true");
      flow.appendChild(link);
    }
  });
  return { flow, stepEls };
}

// 裁决统计 + 筛选条：每个徽章是开关按钮（aria-pressed），右侧严重度下拉。
// 统计数从已渲染的问题行实时数出来（实时事件只给单条裁决，没有汇总字段）。
function advBuildToolbar(card) {
  const bar = document.createElement("div");
  bar.className = "adv-toolbar";
  const stats = document.createElement("div");
  stats.className = "adv-stats";
  stats.setAttribute("role", "group");
  stats.setAttribute("aria-label", "按裁决筛选问题");
  const buttons = {};
  ADV_VERDICT_FILTERS.forEach(([key, label]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "adv-stat v-" + key;
    btn.dataset.verdict = key;
    btn.setAttribute("aria-pressed", key === "all" ? "true" : "false");
    btn.textContent = label;
    const n = document.createElement("span");
    n.className = "adv-stat-n";
    n.textContent = "0";
    btn.appendChild(n);
    btn.onclick = () => {
      card._advFilter = Object.assign({}, card._advFilter, { verdict: key });
      advApplyFilter(card);
    };
    stats.appendChild(btn);
    buttons[key] = btn;
  });
  const sev = document.createElement("select");
  sev.className = "adv-sev-filter";
  sev.title = "按严重度筛选问题";
  sev.setAttribute("aria-label", "按严重度筛选问题");
  ADV_SEVERITY_FILTERS.forEach(([key, label]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = label;
    sev.appendChild(opt);
  });
  sev.onchange = () => {
    card._advFilter = Object.assign({}, card._advFilter, { severity: sev.value || "all" });
    advApplyFilter(card);
  };
  bar.append(stats, sev);
  card._advStats = { buttons, sev, counts: {} };
  return bar;
}

function advRowCounts(card) {
  const counts = { all: 0, confirmed: 0, partial: 0, refuted: 0, pending: 0 };
  if (!card || !card._advList) return counts;
  card._advList.querySelectorAll(".adv-finding").forEach((row) => {
    const v = row.dataset.verdict || "pending";
    counts.all += 1;
    counts[v] = (counts[v] || 0) + 1;
  });
  return counts;
}

function advUpdateStats(card) {
  if (!card || !card._advStats) return;
  const counts = advRowCounts(card);
  card._advStats.counts = counts;
  Object.entries(card._advStats.buttons).forEach(([key, btn]) => {
    const n = btn.querySelector(".adv-stat-n");
    if (n) n.textContent = String(counts[key] || 0);
    btn.classList.toggle("zero", !(counts[key] || 0));
    btn.setAttribute("aria-pressed",
      String(((card._advFilter || {}).verdict || "all") === key));
  });
  if (card._advStats.sev) {
    card._advStats.sev.value = (card._advFilter || {}).severity || "all";
  }
}

function advApplyFilter(card) {
  if (!card || !card._advList) return;
  const f = card._advFilter || { verdict: "all", severity: "all" };
  const rows = [...card._advList.querySelectorAll(".adv-finding")];
  let visible = 0;
  rows.forEach((row) => {
    const okV = f.verdict === "all" || (row.dataset.verdict || "pending") === f.verdict;
    const okS = f.severity === "all" || row.dataset.severity === f.severity;
    row.hidden = !(okV && okS);
    if (!row.hidden) visible += 1;
  });
  advUpdateStats(card);
  if (card._advEmpty) {
    if (!rows.length) {
      card._advEmpty.hidden = false;
      card._advEmpty.textContent = card._advFinished
        ? "未发现候选问题。" : "等待发现者产出候选问题…";
    } else if (!visible) {
      card._advEmpty.hidden = false;
      card._advEmpty.textContent = "没有符合筛选条件的问题。";
    } else {
      card._advEmpty.hidden = true;
    }
  }
}

function advToggleFold(card, sub, fold, t) {
  const folded = card.classList.toggle("folded");
  fold.textContent = folded ? "展开" : "折叠";
  fold.setAttribute("aria-expanded", folded ? "false" : "true");
  sub.textContent = folded
    ? `${(t.advFindingEls || []).length} 条候选问题 · 已折叠 · 点击展开查看清单`
    : (t.advSubText || "");
}

function beginAdversarial(roles) {
  const t = curTab();
  const card = document.createElement("div");
  card.className = "adversarial";
  const head = document.createElement("div");
  head.className = "rt-head";
  const title = document.createElement("span");
  title.className = "rt-title";
  title.textContent = "⚔ 对抗审查";
  const sub = document.createElement("span");
  sub.className = "rt-sub";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "折叠";
  fold.setAttribute("aria-expanded", "true");
  const toggleFold = () => advToggleFold(card, sub, fold, t);
  fold.onclick = (e) => { e.stopPropagation(); toggleFold(); };
  head.onclick = (e) => { if (e.target !== fold) toggleFold(); };
  head.append(title, sub, fold);
  const { flow, stepEls } = advBuildFlow();
  const list = document.createElement("div");
  list.className = "adv-findings";
  const empty = document.createElement("div");
  empty.className = "adv-empty";
  card._advList = list;
  card._advEmpty = empty;
  card._advFilter = { verdict: "all", severity: "all" };
  card._advFinished = false;
  const toolbar = advBuildToolbar(card);
  card.append(head, flow, toolbar, list, empty);
  if (roles && roles.length) card.title = advRolesTitle(roles);
  t.advCard = card;
  t.advSub = sub;
  // 上一场对抗的元数据/序号作废：新一轮的「转为团队工单」必须用本轮的
  // findings 与 seq，残留旧值会让按钮指向旧报告（甚至重复挂载）
  t.advMeta = null;
  t.advSeq = 0;
  t.advSubText = "四角色流水线审查进行中…";
  sub.textContent = t.advSubText;
  t.advPhaseEls = stepEls;
  t.advFindingEls = [];
  t.advFindingsBox = list;
  t.advFold = toggleFold;
  advApplyFilter(card);
  curLog().appendChild(card);
  scrollLog();
}

function advSetPhase(phase, note) {
  const t = curTab();
  if (!t || !t.advCard) return;
  const order = ADV_PHASES.map((p) => p.key);
  const idx = order.indexOf(phase);
  order.forEach((k, i) => {
    const el = t.advPhaseEls[k];
    if (!el) return;
    el.classList.toggle("done", idx >= 0 && i < idx);
    el.classList.toggle("active", i === idx);
    if (i === idx) el.setAttribute("aria-current", "step");
    else el.removeAttribute("aria-current");
  });
  if (note) {
    t.advSubText = note;
    if (!t.advCard.classList.contains("folded")) t.advSub.textContent = note;
  }
}

function advAddFinding(data) {
  const t = curTab();
  if (!t || !t.advCard) return;
  const row = advFindingRow(data, { tab: t });
  t.advFindingsBox.appendChild(row);
  t.advFindingEls.push(row);
  advApplyFilter(t.advCard);
}

function advApplyVerdict(data) {
  const t = curTab();
  if (!t || !t.advCard) return;
  const row = (t.advFindingEls || []).find((r) => r.dataset.findingId === data.finding_id);
  if (!row) return;
  const v = row.querySelector(".adv-f-verdict");
  if (!v) return;
  v.hidden = false;
  v.textContent = ADV_VERDICT_LABEL[data.verdict] || data.verdict || "";
  v.className = "adv-f-verdict verdict-" + (data.verdict || "pending");
  row.dataset.verdict = data.verdict || "pending";
  if (data.reason) {
    row.title = data.reason;
    const details = row.querySelector(".adv-f-details");
    const body = details && details.querySelector(".adv-f-detail-body");
    if (body && !body.querySelector(".adv-f-reason")) {
      const reason = document.createElement("div");
      reason.className = "adv-f-reason dim";
      reason.innerHTML = "裁决依据：" + renderMarkdown(data.reason);
      body.appendChild(reason);
      details.hidden = false;
    }
  }
  advApplyFilter(t.advCard);
}

// 导出对抗报告（P3）：把落库 meta 的结构化 Markdown 从后端取回并下载。
// seq 已知即可用，不依赖团队在不在场（回放卡与实时卡共用）。
function advExportActions(meta, seq, tab) {
  if (!tab || tab.readonly || !tab.sid || !seq) return null;
  const box = document.createElement("div");
  box.className = "adv-actions";
  const button = tcEl("button", "adv-export", "导出报告");
  button.title = "把本次对抗审查（角色名册 / 终审报告 / 全部发现与裁决）导出为 Markdown 报告";
  button.onclick = (ev) => {
    ev.stopPropagation();
    request("adversarial.export", { session_id: tab.sid, seq })
      .then((r) => downloadText(r.filename, r.markdown))
      .catch((e) => addNotice("导出失败: " + e.message));
  };
  box.appendChild(button);
  return box;
}

// 圆桌结论转团队工单（P5 跨形态流转）：登记为执行型待办、不自动派工，
// 同一结论幂等（后端按 (roundtable, seq) 去重）；需团队在场，指派表单镜像对抗流。
function rtToTeamActions(meta, seq, tab) {
  if (!tab || tab.readonly || !tab.sid || !seq || !meta
      || meta.mode !== "roundtable" || meta.status === "cancelled") return null;
  const box = document.createElement("div");
  box.className = "adv-actions";
  const button = tcEl("button", "adv-to-team", "转为团队工单");
  button.title = "把融合结论登记为一张执行型待办（不自动派工；同一结论重复登记不会重复建单）";
  button.onclick = async (ev) => {
    ev.stopPropagation();
    let team;
    try { team = await request("team.get", { session_id: tab.sid }); }
    catch (e) { addNotice("读取团队状态失败: " + e.message); return; }
    if (!team || !team.active || !team.team) {
      addNotice("请先组建团队，再把圆桌结论转为工单。");
      return;
    }
    const roster = team.team.roster || [];
    if (!roster.length) { addNotice("团队名册为空，无法指派。"); return; }
    const form = document.createElement("div");
    form.className = "form-grid adv-to-team-form";
    form.appendChild(tcEl("p", "dim small adv-to-team-hint",
      "把融合结论登记为执行型待办，工单停在待办状态；是否派工由你在工单板上显式操作。"
      + "同一结论重复登记不会重复建单。"));
    const assignee = document.createElement("select");
    roster.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.name;
      assignee.appendChild(opt);
    });
    const assigneeLabel = document.createElement("label");
    assigneeLabel.className = "wide";
    assigneeLabel.textContent = "指派队员（执行型）";
    assigneeLabel.appendChild(assignee);
    form.appendChild(assigneeLabel);
    showModal("把圆桌结论转为团队工单", form, async () => {
      if (!assignee.value) throw new Error("请选择指派队员");
      const r = await request("roundtable.to_team_task", {
        session_id: tab.sid, seq, assignee: assignee.value,
      });
      const card = teamCardFor(tab, String((team.team && team.team.team_id) || ""))
        || lastTeamCard(tab);
      if (r.task) tcUpsertTask(card, r.task);
      addNotice("已登记为执行型待办工单，尚未派工。");
    }, "登记工单");
  };
  box.appendChild(button);
  return box;
}

// 把「转为团队工单」入口挂到当前对抗卡（先摘掉旧入口再重建：同一条报告
// 只会有一个按钮，晚到的 seq 也只会补一次，不会重复）。
function advAttachTransfer(tab, meta, seq) {
  if (!tab || !tab.advCard || !tab.advCard.isConnected) return;
  tab.advSeq = Number(seq) || 0;
  tab.advMeta = meta;
  [...tab.advCard.children].forEach((el) => {
    if (el.classList.contains("adv-actions")) el.remove();
  });
  const ex = advExportActions(meta, tab.advSeq, tab);
  if (ex) tab.advCard.appendChild(ex);
  const actions = advTransferActions(meta, tab.advSeq, tab);
  if (actions) tab.advCard.appendChild(actions);
}

function advTransferActions(meta, seq, tab) {
  if (!tab || tab.readonly || !tab.sid || !seq || meta.status !== "done") return null;
  const eligible = (meta.findings || []).filter((f) => ["confirmed", "partial"].includes(f.verdict));
  if (!eligible.length) return null;
  const sid = tab.sid;
  const box = document.createElement("div");
  box.className = "adv-actions";
  const button = tcEl("button", "adv-to-team", "转为团队工单");
  button.title = "逐条勾选成立的发现，登记为顾问型待办（不会自动派工）";
  button.onclick = async (ev) => {
    ev.stopPropagation();
    let team;
    try { team = await request("team.get", { session_id: sid }); }
    catch (e) { addNotice("读取团队状态失败: " + e.message); return; }
    if (!team || !team.active || !team.team) {
      addNotice("请先组建团队，再将对抗发现转为工单。");
      return;
    }
    const roster = team.team.roster || [];
    if (!roster.length) {
      addNotice("团队还没有队员，无法登记顾问型待办。");
      return;
    }
    const form = document.createElement("div");
    form.className = "form-grid adv-to-team-form";
    form.appendChild(tcEl("p", "dim small adv-to-team-hint",
      "只登记为顾问型待办，工单停在待办状态；是否派工由你在工单板上显式操作。"
      + "对抗模型的输出是待核实内容，不是执行指令。"));
    const list = document.createElement("div");
    list.className = "adv-to-team-list";
    const boxes = [];
    eligible.forEach((f) => {
      const row = document.createElement("label");
      row.className = "adv-to-team-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = f.id || "";
      boxes.push(cb);
      const body = document.createElement("span");
      body.className = "adv-to-team-body";
      const head = document.createElement("span");
      head.className = "adv-to-team-head";
      head.textContent = `${f.id || "?"} · ${f.verdict === "confirmed" ? "成立" : "部分成立"}`
        + ` · ${f.severity || "medium"} · ${f.category || "问题"}`;
      body.appendChild(head);
      const loc = String(f.location || "");
      if (loc) body.appendChild(tcEl("span", "adv-to-team-loc", loc));
      body.appendChild(tcEl("span", "adv-to-team-desc", f.description || ""));
      if (f.evidence) body.appendChild(tcEl("span", "adv-to-team-evidence", "依据：" + f.evidence));
      row.title = [f.description, f.reason, f.solution].filter(Boolean).join("\n");
      row.append(cb, body);
      list.appendChild(row);
    });
    const assignee = document.createElement("select");
    roster.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.name;
      assignee.appendChild(opt);
    });
    const assigneeLabel = document.createElement("label");
    assigneeLabel.className = "wide";
    assigneeLabel.textContent = "指派队员（顾问型，只出复核意见）";
    assigneeLabel.appendChild(assignee);
    form.append(list, assigneeLabel);
    showModal("选择要转为顾问型待办的发现", form, async () => {
      const ids = boxes.filter((cb) => cb.checked && cb.value).map((cb) => cb.value);
      if (!ids.length) throw new Error("请至少勾选一条成立或部分成立的发现");
      if (!assignee.value) throw new Error("请选择指派队员");
      const r = await request("adversarial.to_team_tasks", {
        session_id: sid, seq, finding_ids: ids, assignee: assignee.value,
      });
      const card = teamCardFor(tab, String((team.team && team.team.team_id) || ""))
        || lastTeamCard(tab);
      (r.tasks || []).forEach((task) => tcUpsertTask(card, task));
      addNotice(`已登记 ${((r && r.tasks) || []).length} 张顾问型待办工单，尚未派工。`);
    }, "登记工单");
  };
  box.appendChild(button);
  return box;
}

function advFinished(data) {
  const t = curTab();
  if (!t || !t.advCard) return;
  for (const k in t.advPhaseEls) {
    t.advPhaseEls[k].classList.remove("active");
    t.advPhaseEls[k].classList.add("done");
    t.advPhaseEls[k].removeAttribute("aria-current");
  }
  const status = data.status || "done";
  t.advCard.classList.add(status === "done" ? "ok" : status === "cancelled" ? "cancelled" : "err");
  t.advCard._advFinished = true;
  const st = status === "done" ? ("✓ " + (data.summary || "审查完成"))
    : status === "cancelled" ? "已中止（已得结论保留）"
    : "✗ " + (data.summary || "审查未完成");
  t.advSubText = st;
  if (!t.advCard.classList.contains("folded")) t.advSub.textContent = st;
  advApplyFilter(t.advCard);
  // 「转为团队工单」要等报告消息落库拿到 seq（AssistantMessage 事件的 seq 为空，
  // 真实序号随 chat.send 响应回来），这里不提前挂按钮
  // 终审报告随后流进下方回答气泡：稍候自动折叠清单（可随时展开回看）
  if (status === "done") {
    setTimeout(() => {
      if (t.advCard && t.advCard.isConnected && !t.advCard.classList.contains("folded")) t.advFold();
    }, 600);
  }
}

// 历史回放：从消息的 adversarial 元数据重建对抗卡（默认折叠，与圆桌回放卡同姿态）。
// 统计条与筛选照常可用（数据来自持久化的 findings，不依赖实时事件）。
function buildAdvReplayCard(meta, seq, tab) {
  meta = { ...meta, seq };
  const findings = meta.findings || [];
  const card = document.createElement("div");
  card.className = "adversarial folded " +
    (meta.status === "error" ? "err" : meta.status === "cancelled" ? "cancelled" : "ok");
  card._advFilter = { verdict: "all", severity: "all" };
  card._advFinished = true;
  const head = document.createElement("div");
  head.className = "rt-head";
  const title = document.createElement("span");
  title.className = "rt-title";
  title.textContent = "⚔ 对抗审查";
  const s = meta.stats || {};
  const sumBits = [];
  if (s.total) {
    sumBits.push(`${s.total} 条候选`);
    if (s.confirmed) sumBits.push(`成立 ${s.confirmed}`);
    if (s.partial) sumBits.push(`部分 ${s.partial}`);
    if (s.refuted) sumBits.push(`推翻 ${s.refuted}`);
    if (s.pending) sumBits.push(`待定 ${s.pending}`);
  }
  const sub = document.createElement("span");
  sub.className = "rt-sub";
  const subText = sumBits.length ? sumBits.join(" · ") : "未发现候选问题";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "展开";
  fold.setAttribute("aria-expanded", "false");
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    fold.setAttribute("aria-expanded", folded ? "false" : "true");
    sub.textContent = folded
      ? `${findings.length} 条候选问题 · 已折叠 · 点击展开查看清单`
      : subText;
  };
  fold.onclick = (e) => { e.stopPropagation(); toggleFold(); };
  head.onclick = (e) => { if (e.target !== fold) toggleFold(); };
  head.append(title, sub, fold);
  const { flow } = advBuildFlow();
  flow.querySelectorAll(".adv-step").forEach((el) => el.classList.add("done"));
  const list = document.createElement("div");
  list.className = "adv-findings";
  const empty = document.createElement("div");
  empty.className = "adv-empty";
  card._advList = list;
  card._advEmpty = empty;
  const toolbar = advBuildToolbar(card);
  findings.forEach((f) => list.appendChild(advFindingRow(f, { replay: true, tab })));
  const actions = advTransferActions(meta, Number(meta.seq || 0), tab);
  card.append(head, flow, toolbar, list, empty);
  const ex = advExportActions(meta, Number(meta.seq || 0), tab);
  if (ex) card.appendChild(ex);
  if (actions) card.appendChild(actions);
  if (meta.roles && meta.roles.length) card.title = advRolesTitle(meta.roles);
  advApplyFilter(card);
  sub.textContent = `${findings.length} 条候选问题 · 已折叠 · 点击展开查看清单`;
  return card;
}

// 对抗徽标：标记这条报告是四角色流水线审查的产出（悬停看角色名册）
function addAdvBadge(el, meta) {
  const s = meta.stats || {};
  const badge = document.createElement("div");
  badge.className = "rt-badge adv-badge";
  badge.title = (advRolesTitle(meta.roles) || "对抗审查") +
    (meta.degraded ? "\n（终审未完成，报告由复核后的中间结果拼成）" : "");
  const parts = ["⚔ 对抗审查"];
  if (s.total) {
    const bits = [];
    if (s.confirmed) bits.push(`成立 ${s.confirmed}`);
    if (s.partial) bits.push(`部分 ${s.partial}`);
    if (s.refuted) bits.push(`推翻 ${s.refuted}`);
    if (s.pending) bits.push(`待定 ${s.pending}`);
    parts.push(`${s.total} 条候选` + (bits.length ? `（${bits.join(" · ")}）` : ""));
  } else {
    parts.push("未发现候选问题");
  }
  if (meta.status === "cancelled") parts.push("已中止");
  else if (meta.status === "error") parts.push("未完成");
  badge.textContent = parts.join(" · ");
  el.prepend(badge);
}

// ---------- 团队卡片：名册 + 工单板 + 频道时间线（聊天流内的协作台） ----------
// 一期用户总管：卡在 team_started 时建立并保持展开，随团队事件实时更新；
// 终态（交付/收队/轮次耗尽）后自动折叠。结构仿圆桌卡，但内容分三区：
// 名册（头像+成员名+人设）、工单板（登记/派工/验收）、频道时间线（流式）。

// 厂商头像：static/logos/ 下按服务名取（zhipu 只有 png，其余 svg）；没有同名文件就摘掉
function tcAvatar(provider) {
  const img = document.createElement("img");
  img.className = "tc-avatar";
  const p = String(provider || "").toLowerCase();
  img.src = "/static/logos/" + (p === "zhipu" ? "zhipu.png" : p + ".svg");
  img.alt = "";
  img.loading = "lazy";
  img.onerror = () => img.remove();
  return img;
}

function tcEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined && text !== "") el.textContent = text;
  return el;
}

// 工单看板的三列（待办 / 进行中 / 待验收）；完成与失败收进可折叠尾区。
// 列顺序就是工单生命周期顺序，不再额外发明阶段。
const TEAM_TASK_COLUMNS = [
  { key: "pending", label: "待办" },
  { key: "in_progress", label: "进行中" },
  { key: "review", label: "待验收" },
];

function tcColumnKey(status) {
  return TEAM_TASK_COLUMNS.some((c) => c.key === status) ? status : "closed";
}

// 拖拽流转（P2）：沿用按钮操作表（TEAM_TASK_OPS）判定「这列收不收」——
// 非法跨列不响应高亮也不接 drop；打回（review→in_progress）走行内理由
// 表单（与按钮同款），其余直接流转。
function tcCanTransition(from, to) {
  return (TEAM_TASK_OPS[from] || []).some(([next]) => next === to);
}

// 看板骨架：三列 + 完成/失败折叠区。列容器保持稳定，工单行按状态在各列间搬。
function tcBuildKanban(card) {
  const kanban = tcEl("div", "tc-kanban");
  kanban.setAttribute("role", "group");
  kanban.setAttribute("aria-label", "团队工单看板");
  const cols = {};
  TEAM_TASK_COLUMNS.forEach((col) => {
    const sec = tcEl("section", "tc-col");
    sec.dataset.col = col.key;
    sec.setAttribute("aria-label", col.label);
    const head = tcEl("div", "tc-col-head");
    head.append(
      tcEl("span", "tc-col-title", col.label),
      tcEl("span", "tc-col-count", "0"),
    );
    const list = tcEl("div", "tc-col-list");
    const empty = tcEl("div", "tc-col-empty", "暂无工单");
    sec.append(head, list, empty);
    // 拖拽落列（P2）：只有「按钮操作表允许的跨列」才高亮/接收；落列即流转，
    // 打回走行内理由表单。只读列/已收尾的卡不可拖也不可落。
    sec.addEventListener("dragover", (e) => {
      const drag = card && card._drag;
      if (!drag || !tcCanTransition(drag.from, col.key)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      sec.classList.add("drag-over");
    });
    sec.addEventListener("dragleave", (e) => {
      if (!sec.contains(e.relatedTarget)) sec.classList.remove("drag-over");
    });
    sec.addEventListener("drop", (e) => {
      e.preventDefault();
      sec.classList.remove("drag-over");
      const drag = card && card._drag;
      if (!drag || !tcCanTransition(drag.from, col.key)) return;
      const row = card._ui.boardList.querySelector(`[data-task="${drag.id}"]`);
      if (!row || !row._task) return;
      card._drag = null;
      tcTaskTransition(card, row._task, col.key, null);
    });
    kanban.appendChild(sec);
    cols[col.key] = { sec, list, count: head.querySelector(".tc-col-count"), empty };
  });
  const closed = document.createElement("details");
  closed.className = "tc-closed";
  closed.hidden = true;
  const summary = document.createElement("summary");
  summary.className = "tc-closed-summary";
  const closedCount = tcEl("span", "tc-closed-count", "已完成 0 · 失败 0");
  summary.appendChild(closedCount);
  const closedList = tcEl("div", "tc-closed-list");
  closed.append(summary, closedList);
  return { kanban, cols, closed, closedList, closedCount };
}

// 工单 / 频道视图切换：两个视图都常驻 DOM，只切显隐——频道流式与历史回放
// 在后台照常落行，切回来不会丢内容。
function tcBuildViewTabs(card) {
  const bar = tcEl("div", "tc-views");
  bar.setAttribute("role", "tablist");
  bar.setAttribute("aria-label", "团队视图");
  const make = (view, label) => {
    const b = tcEl("button", "tc-view-tab", label);
    b.type = "button";
    b.dataset.view = view;
    b.setAttribute("role", "tab");
    const n = tcEl("span", "tc-view-count", "0");
    b.appendChild(n);
    b.onclick = () => tcSetView(card, view);
    return { b, n };
  };
  const board = make("board", "工单");
  const channel = make("channel", "频道");
  channel.b.title = "团队频道：成员发言与总管指令的完整时间线";
  bar.append(board.b, channel.b);
  const tabs = [board.b, channel.b];
  tabs.forEach((tab, i) => {
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      tcSetView(card, next.dataset.view);
      next.focus();
    });
  });
  return { bar, tabs, board, channel };
}

function tcSetView(card, view) {
  const ui = card && card._ui;
  if (!ui || !ui.tabBoard) return;
  const v = view === "channel" ? "channel" : "board";
  card._view = v;
  card.classList.toggle("view-channel", v === "channel");
  [ui.tabBoard, ui.tabChannel].forEach((tab) => {
    const on = tab.dataset.view === v;
    tab.classList.toggle("is-active", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  });
  if (v === "channel") {
    ui.tabChannel.classList.remove("has-new");
    ui.channelEl.scrollTop = ui.channelEl.scrollHeight;
  }
}

// 频道有新行时给「频道」页签一个圆点（只在用户正看工单板时提示；
// 卡片折叠着不打扰——历史回放的大量补行也不算未读）
function tcMarkChannelNew(card) {
  if (!card || !card._ui || card._view === "channel" || card._suppressNew) return;
  if (card.classList.contains("folded")) return;
  card._ui.tabChannel.classList.add("has-new");
}

function tcSyncChannelCount(card) {
  if (!card || !card._ui) return;
  const n = card._ui.channelEl.querySelectorAll(".tc-msg").length;
  if (card._ui.tabChannelCount) card._ui.tabChannelCount.textContent = String(n);
}

// 看板计数与空态：列头数字、空列提示、完成/失败折叠区计数、
// 「工单」页签显示未终态张数（完成/失败不计入待办负担）
function tcSyncBoardCounts(card) {
  const ui = card && card._ui;
  if (!ui) return;
  const counts = { pending: 0, in_progress: 0, review: 0, done: 0, error: 0 };
  ui.boardList.querySelectorAll(".tc-task").forEach((row) => {
    const st = row.dataset.status || "pending";
    counts[st] = (counts[st] || 0) + 1;
  });
  TEAM_TASK_COLUMNS.forEach((col) => {
    const c = ui.cols[col.key];
    const n = counts[col.key] || 0;
    c.count.textContent = String(n);
    c.empty.hidden = n > 0;
    c.sec.classList.toggle("is-empty", n === 0);
  });
  const done = counts.done || 0;
  const err = counts.error || 0;
  ui.closed.hidden = done + err === 0;
  ui.closedCount.textContent = `已完成 ${done} · 失败 ${err}`;
  const active = (counts.pending || 0) + (counts.in_progress || 0) + (counts.review || 0);
  if (ui.tabBoardCount) ui.tabBoardCount.textContent = String(active);
  tcSyncAttendance(card);
  tcSyncChannelCount(card);
}

// 出勤/绩效摘要（P2）：按名册聚合工单状态——完成/进行/待办/打回次数；
// 名册里没派到单的成员也点名（「未派单」），谁闲着一眼可见。数据全部来自
// 板面现有行，不另起后端查询。
function tcSyncAttendance(card) {
  const ui = card && card._ui;
  if (!ui || !ui.attendance) return;
  const stats = {};
  (card._roster || []).forEach((m) => {
    if (m && m.name) stats[m.name] = { done: 0, active: 0, todo: 0, redo: 0 };
  });
  ui.boardList.querySelectorAll(".tc-task").forEach((row) => {
    const task = row._task || {};
    const name = String(task.assignee || "");
    if (!name) return;
    if (!stats[name]) stats[name] = { done: 0, active: 0, todo: 0, redo: 0 };
    const st = row.dataset.status || "pending";
    if (st === "done") stats[name].done += 1;
    else if (st === "in_progress" || st === "review") stats[name].active += 1;
    else if (st === "pending") stats[name].todo += 1;
    stats[name].redo += Number(task.redo) || 0;
  });
  const bits = Object.entries(stats).map(([name, s]) => {
    const parts = [];
    if (s.done) parts.push(`完成 ${s.done}`);
    if (s.active) parts.push(`进行 ${s.active}`);
    if (s.todo) parts.push(`待办 ${s.todo}`);
    if (s.redo) parts.push(`打回 ${s.redo}`);
    return `${name}：${parts.length ? parts.join(" / ") : "未派单"}`;
  });
  ui.attendance.textContent = bits.length ? "出勤：" + bits.join(" · ") : "";
  ui.attendance.hidden = !bits.length;
}

function buildTeamCard(roster, directorMode, director) {
  const card = document.createElement("div");
  card.className = "teamcard";
  // 总管标识（二期 AI 总管）：null = 用户总管；{provider, model} = AI 总管，
  // 由 team_started 事件 / 团队快照 director 块喂入（updateTeamCard 每次刷新）
  card._director = (directorMode || "user") === "ai"
    ? { provider: (director && director.provider) || "", model: (director && director.model) || "" }
    : null;
  const head = tcEl("div", "tc-head");
  const sub = tcEl("span", "tc-sub", "组建中…");
  const status = tcEl("span", "tc-status", "进行中");
  const fold = tcEl("button", "tc-fold", "折叠");
  fold.setAttribute("aria-expanded", "true");
  const resumeBtn = tcEl("button", "tc-resume", "继续推进");
  resumeBtn.hidden = true;
  resumeBtn.title = "向 AI 总管发送继续指令，恢复自动推进";
  resumeBtn.onclick = (e) => { e.stopPropagation(); tcContinueTeam(card); };
  // 在团队室打开（团队模式设计 §15.1 入口①）：跳右面板「团队室」页签，
  // 频道 / 工单板 / 成员三分区同屏；卡片与团队室同源渲染、双向即时同步
  const roomBtn = tcEl("button", "tc-open-room", "在团队室打开");
  roomBtn.title = "在右侧面板的「团队室」页签打开本团队：成员、频道、工单板三分区同屏";
  roomBtn.onclick = (e) => { e.stopPropagation(); if (typeof openRightTab === "function") openRightTab("teamroom"); };
  head.append(tcEl("span", "tc-title", "🧩 团队"), sub, status, roomBtn, fold);
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    fold.setAttribute("aria-expanded", folded ? "false" : "true");
  };
  fold.onclick = (e) => { e.stopPropagation(); toggleFold(); };
  head.onclick = (e) => { if (e.target !== fold) toggleFold(); };
  const body = tcEl("div", "tc-body");
  const rosterEl = tcEl("div", "tc-roster");
  const kanban = tcBuildKanban(card);
  const taskForm = buildTaskForm(card);
  const attendance = tcEl("div", "tc-attendance");
  attendance.hidden = true; // 无名册/无工单时不占位（tcSyncAttendance 回填）
  const boardEl = tcEl("div", "tc-board");
  boardEl.append(attendance, kanban.kanban, kanban.closed, taskForm);
  const channelEl = tcEl("div", "tc-channel");
  const views = tcBuildViewTabs(card);
  const deliverBtn = tcEl("button", "tc-deliver", "交付");
  deliverBtn.title = "确认交付：产出《交付说明》并关闭团队（未完成的工单会记为未尽事项），本会话恢复普通对话";
  deliverBtn.onclick = async () => {
    if (!(await confirmModal("交付会结束团队并产出《交付说明》（未完成的工单记为未尽事项），本会话恢复普通对话。确定交付？",
      { title: "确认交付", okLabel: "交付" }))) return;
    const t = curTab();
    deliverBtn.disabled = true;
    deliverBtn.textContent = "交付中…";
    try {
      const r = await request("team.deliver", { session_id: t.sid });
      // 终态一般已由 team_finished 事件落到卡上；这里用返回快照兜底对齐
      if (r && r.team) updateTeamCard(card, r.team);
      tcFinish(card, (r && r.status) || "done", (r && r.summary) || "");
    } catch (e) {
      addNotice("交付失败: " + e.message);
      deliverBtn.disabled = false;
      deliverBtn.textContent = "交付";
    }
  };
  const stopBtn = tcEl("button", "tc-stop", "收队");
  stopBtn.title = "立即终止团队：正在发言的队员会把已产出的部分定稿进频道，随后产出《收队说明》";
  stopBtn.onclick = async () => {
    if (!(await confirmModal("收队会立即终止团队：正在发言的队员会把已产出的部分定稿进频道，随后产出《收队说明》，本会话恢复普通对话。确定收队？",
      { title: "确认收队", okLabel: "收队", danger: true }))) return;
    const t = curTab();
    stopBtn.disabled = true;
    stopBtn.textContent = "收队中…";
    try {
      const r = await request("team.stop", { session_id: t.sid });
      // 终态一般已由 team_finished 事件落到卡上；这里用返回快照兜底对齐
      if (r && r.team) updateTeamCard(card, r.team);
      tcFinish(card, (r && r.status) || "aborted", (r && r.summary) || "");
    } catch (e) {
      addNotice("收队失败: " + e.message);
      stopBtn.disabled = false;
      stopBtn.textContent = "收队";
    }
  };
  // 接管（二期 AI 总管专属）：切回用户总管模式，正在推进的自动循环干净取消，
  // 工单板与团队频道保留；用户总管模式/终态后隐藏（tcUpdateRoster 同步显隐）
  const takeoverBtn = tcEl("button", "tc-takeover", "接管");
  takeoverBtn.hidden = true;
  takeoverBtn.title = "接管：切回用户总管模式，正在推进的自动循环会被取消（正在发言的成员会把已产出的部分定稿进频道），工单板与团队频道保留";
  takeoverBtn.onclick = async () => {
    if (!(await confirmModal("接管会切回用户总管模式：正在推进的自动循环会被取消（正在发言的成员会把已产出的部分定稿进频道），工单板与团队频道保留，此后由你直接指挥。确定接管？",
      { title: "确认接管", okLabel: "接管" }))) return;
    const t = curTab();
    takeoverBtn.disabled = true;
    takeoverBtn.textContent = "接管中…";
    try {
      const r = await request("team.takeover", { session_id: t.sid });
      if (r && r.team) updateTeamCard(card, r.team);
      addNotice("已接管：已切换为用户总管模式，工单板与团队频道保留，此后每条消息都由你直接指挥。");
    } catch (e) {
      addNotice("接管失败: " + e.message);
      takeoverBtn.disabled = false;
      takeoverBtn.textContent = "接管";
    }
  };
  // 存为模板（三期）：把当前名册与总管形态存成团队模板，之后建队可一键填入。
  // 终态后仍可用（收队的团队正是模板的好素材），所以不进 ended 隐藏清单
  const saveTplBtn = tcEl("button", "tc-savetpl", "另存为模板");
  saveTplBtn.title = "把当前名册与总管形态存成团队模板：之后在输入框的团队面板里「从模板创建」一键填入";
  saveTplBtn.onclick = () => tcSaveTemplateModal(card);
  // 常驻开关（T3.2）：把这支队伍登记为跨会话常驻——定时团队任务触发时优先
  // 唤醒它（频道历史与工单板延续）而不是每次重建；再点一次解除。只在活动
  // 团队上可操作（快照对齐点亮，tcFinish 收口），终态后随卡隐藏
  const residentBtn = tcEl("button", "tc-resident", "常驻");
  residentBtn.hidden = true;
  residentBtn.title = "常驻：把这支队伍登记为跨会话常驻，定时团队任务触发时优先唤醒它（频道历史与工单板延续）而不是每次重建；再点一次解除常驻";
  residentBtn.onclick = async (e) => {
    e.stopPropagation();
    const t = curTab();
    // 终态卡的唯一常驻操作：按 team_id 解除（resident_clear 已放开对已结束
    // 团队按 id 解除，评审③）——已交付/收队的队伍登记还在，不能没了出口
    if (card._finished) {
      if (!card._teamId) return;
      residentBtn.disabled = true;
      try {
        await request("team.resident_clear", { team_id: card._teamId });
        residentBtn.hidden = true;
        addNotice("已解除常驻：定时团队任务将照常重建队伍。");
      } catch (err) {
        addNotice("解除常驻失败: " + err.message);
      } finally {
        residentBtn.disabled = false;
      }
      return;
    }
    const on = residentBtn.classList.contains("is-active");
    residentBtn.disabled = true;
    try {
      const r = await request(on ? "team.resident_clear" : "team.resident_set",
        { session_id: t ? t.sid : undefined,
          // 建队来源模板名（四期常驻）：请求自含绑定依据；后端另有编排器
          // 记录与原登记条目两级兜底， meta 缺 template 键的旧快照不受影响
          template: on ? undefined : (card._template || undefined) });
      const now = !!(r && r.resident);
      residentBtn.classList.toggle("is-active", now);
      residentBtn.textContent = now ? "常驻中" : "常驻";
      addNotice(now
        ? "已设为常驻：定时团队任务触发时将优先唤醒这支队伍（频道历史与工单板延续）。"
        : "已解除常驻：定时团队任务将照常重建队伍。");
    } catch (err) {
      addNotice((on ? "解除常驻失败" : "设为常驻失败") + ": " + err.message);
    } finally {
      residentBtn.disabled = false;
    }
  };
  const foot = tcEl("div", "tc-foot");
  foot.append(saveTplBtn, residentBtn, takeoverBtn, resumeBtn, deliverBtn, stopBtn);
  body.append(rosterEl, views.bar, boardEl, channelEl, foot);
  card.append(head, body);
  card._ui = {
    sub, status, fold, rosterEl, taskForm, channelEl, deliverBtn, stopBtn,
    takeoverBtn, resumeBtn, residentBtn,
    boardList: boardEl, // 看板根（含完成/失败折叠区）：既有查询（[data-task]）继续在这里找行
    cols: kanban.cols, closed: kanban.closed, closedList: kanban.closedList,
    closedCount: kanban.closedCount, attendance,
    tabBoard: views.board.b, tabChannel: views.channel.b,
    tabBoardCount: views.board.n, tabChannelCount: views.channel.n,
  };
  card._roster = [];
  card._view = "board";
  card._tab = curTab(); // 事件/回放都在所属标签的渲染面里建卡（继续/转单用）
  tcSetView(card, "board");
  tcSyncBoardCounts(card);
  tcUpdateRoster(card, roster || [], directorMode || "user");
  return card;
}

// 工单登记小表单：登记后是「待办」，要开工在工单行上点「派工」
function buildTaskForm(card) {
  const form = tcEl("div", "tc-task-form");
  const assignee = document.createElement("select");
  assignee.className = "tf-assignee";
  assignee.title = "指派队员（必须在团队名册中）";
  const title = document.createElement("input");
  title.type = "text";
  title.className = "tf-title";
  title.placeholder = "工单标题";
  const type = document.createElement("select");
  type.className = "tf-type";
  type.title = "工单类型：执行型带工具干活，顾问型只出意见（工单级属性）";
  [["exec", "执行型"], ["advisor", "顾问型"]].forEach(([v, t]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = t;
    type.appendChild(o);
  });
  const accept = document.createElement("input");
  accept.type = "text";
  accept.className = "tf-accept";
  accept.placeholder = "验收标准（可选）";
  const add = tcEl("button", "tf-add", "登记工单");
  add.type = "button";
  add.onclick = async () => {
    const t = curTab();
    const text = title.value.trim();
    if (!text) { addNotice("工单标题不能为空"); return; }
    if (!assignee.value) { addNotice("还没有可指派的队员"); return; }
    add.disabled = true;
    try {
      const r = await request("team.task_add", {
        session_id: t.sid,
        title: text,
        assignee: assignee.value,
        type: type.value,
        accept: accept.value.trim(),
      });
      tcUpsertTask(card, r.task);
      title.value = "";
      accept.value = "";
    } catch (e) {
      addNotice("登记工单失败: " + e.message);
    } finally {
      add.disabled = false;
    }
  };
  form.append(assignee, title, type, accept, add);
  return form;
}

// 工单行操作按钮（状态机：待办→进行中→待验收→完成；待验收打回→进行中 redo+1；
// 失败的工单可重新派工；待办可直接标记失败——依赖链卡死时的手工出口，
// 失败工单记入未尽事项）。派工/打回都会由后端代发一条总管派工消息进频道。
const TEAM_TASK_OPS = {
  pending: [
    ["in_progress", "派工", "把工单派给队员：派工指令进频道，队员被点名后开工（依赖工单需先完成）"],
    ["error", "标记失败", "放弃这张待办工单：记为失败并进未尽事项（依赖它的工单会由总管强制裁定）"],
  ],
  in_progress: [["review", "转待验收", "队员已交报告且你认为完成时，把工单转入待验收"]],
  review: [
    ["done", "通过", "验收通过，工单完成"],
    ["in_progress", "打回", "验收不合格，打回重做（打回次数 +1，附理由转给队员）"],
  ],
  error: [["in_progress", "重新派工", "失败收尾的工单重新派发"]],
};

function buildTaskRow(card, task) {
  const row = tcEl("div", "tc-task st-" + (task.status || "pending"));
  row.dataset.task = task.id;
  row.dataset.status = task.status || "pending";
  row.dataset.col = tcColumnKey(task.status || "pending");
  row._task = task; // 事件更新时补齐 deps/accept 等事件里没有的字段
  // 顶部：编号 + 标题（kanban 列窄，标题换行；来源/待手动派工徽标跟在标题后）
  const top = tcEl("div", "tt-top");
  top.appendChild(tcEl("span", "tt-id", task.id));
  const title = tcEl("span", "tt-title", task.title);
  const tips = [];
  if (task.accept) tips.push(`验收标准：${task.accept}`);
  if (task.detail) tips.push(`工单详情：\n${task.detail}`);
  if (tips.length) title.title = tips.join("\n\n");
  top.appendChild(title);
  const srcKind = task.source && task.source.kind;
  if (srcKind === "adversarial") {
    const src = tcEl("span", "tt-src", "对抗");
    src.title = `来自对抗审查消息 #${task.source.seq || "?"} / ${task.source.finding_id || "?"}`
      + "：外部模型输出，仅为待核实证据";
    top.appendChild(src);
  }
  if (task.auto_dispatch === false && task.status === "pending") {
    const manual = tcEl("span", "tt-manual", "待手动派工");
    manual.title = "该工单不会被自动循环派发：确认要处理时点下方「派工」";
    top.appendChild(manual);
  }
  // 元信息行：指派人、类型、状态、打回次数、依赖
  const meta = tcEl("div", "tt-meta");
  meta.appendChild(tcEl("span", "tt-assignee", "@" + task.assignee));
  meta.appendChild(tcEl("span", "tt-type", task.type === "advisor" ? "顾问型" : "执行型"));
  meta.appendChild(tcEl("span", "tt-status", TEAM_TASK_STATUS_LABEL[task.status] || task.status));
  if (task.redo) meta.appendChild(tcEl("span", "tt-redo", `打回 ${task.redo} 次`));
  if ((task.deps || []).length) {
    const dep = tcEl("span", "tt-deps", "依赖 " + task.deps.join("、"));
    dep.title = "依赖工单全部完成后才能派发";
    meta.appendChild(dep);
  }
  row.append(top, meta);
  // 终态/中断卡不再重建操作按钮（晚到的事件重画时也保持只读）
  if (!card._finished) {
    const ops = tcEl("span", "tt-ops");
    (TEAM_TASK_OPS[task.status] || []).forEach(([next, label, tip]) => {
      const b = tcEl("button", "tt-op", label);
      b.type = "button";
      b.title = tip;
      b.onclick = () => tcTaskTransition(card, task, next, b);
      ops.appendChild(b);
    });
    if (ops.children.length) row.appendChild(ops);
  }
  // 拖拽流转（P2）：有可执行操作的非终态工单才可拖；起点状态记在卡上
  // （dragover 阶段读不到 dataTransfer 内容，跨引擎行为不一）
  if (!card._finished && !card.readonly && (TEAM_TASK_OPS[task.status] || []).length) {
    row.draggable = true;
    row.addEventListener("dragstart", (e) => {
      card._drag = { id: task.id, from: task.status || "pending" };
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", task.id); } catch (_) {} // IE 兼容面忽略
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => {
      card._drag = null;
      row.classList.remove("dragging");
      Object.values(card._ui.cols).forEach(({ sec }) => sec.classList.remove("drag-over"));
    });
  }
  return row;
}

// 工单状态流转：打回时在行内展开理由输入框（不用弹窗——pywebview 下 prompt 不可靠）
function tcTaskTransition(card, task, next, btn) {
  const t = curTab();
  if (next === "in_progress" && task.status === "review") {
    const board = card._ui.boardList;
    const row = board.querySelector(`[data-task="${task.id}"]`);
    if (row && row.querySelector(".tt-reject-box")) return; // 已在填理由，别叠一层
    const box = tcEl("div", "tt-reject-box");
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "打回理由（会连同派工指令转给队员）";
    const yes = tcEl("button", "tt-op", "确认打回");
    yes.type = "button";
    const no = tcEl("button", "tt-op", "取消");
    no.type = "button";
    const submit = () => {
      const reason = input.value.trim();
      box.remove();
      tcTaskUpdateRequest(card, task, "in_progress", reason, btn);
    };
    yes.onclick = submit;
    no.onclick = () => box.remove();
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") submit();
      if (ev.key === "Escape") box.remove();
      ev.stopPropagation();
    });
    box.append(input, yes, no);
    row.appendChild(box);
    input.focus();
    return;
  }
  tcTaskUpdateRequest(card, task, next, "", btn);
}

async function tcTaskUpdateRequest(card, task, status, note, btn) {
  const t = curTab();
  if (btn) btn.disabled = true;
  try {
    const r = await request("team.task_update", {
      session_id: t.sid, task_id: task.id, status,
      note: note || undefined,
    });
    tcUpsertTask(card, r.task);
    // 派工/重派/打回重做都代发一条总管派工消息：返回里带就先上屏（事件到达时幂等覆盖）
    if (r.assign_message) tcUpsertMsg(card, r.assign_message);
  } catch (e) {
    addNotice("工单状态更新失败: " + e.message);
    if (btn) btn.disabled = false;
  }
}

// 单张工单落位：同编号替换；跨列（状态变化）时从旧列摘掉再插到新列，
// 列内按 T<n> 升序（与工单编号的生成顺序一致）。
function tcUpsertTask(card, task) {
  if (!card || !card._ui) return;
  const board = card._ui.boardList;
  const key = tcColumnKey(task.status || "pending");
  const target = key === "closed" ? card._ui.closedList : card._ui.cols[key].list;
  const fresh = buildTaskRow(card, task);
  const old = board.querySelector(`[data-task="${task.id}"]`);
  if (old) old.remove();
  const n = Number(String(task.id || "").replace(/\D/g, "")) || 0;
  let anchor = null;
  for (const r of target.children) {
    if ((Number(String(r.dataset.task || "").replace(/\D/g, "")) || 0) > n) { anchor = r; break; }
  }
  target.insertBefore(fresh, anchor);
  tcSyncBoardCounts(card);
}

// 全量重画看板（team.get 快照 / 历史回放共用）：只清行，不重建列/折叠区，
// 用户展开的「完成 / 失败」区与当前视图保持不变。
function tcRenderTasks(card, tasks) {
  if (!card || !card._ui) return;
  TEAM_TASK_COLUMNS.forEach((col) => { card._ui.cols[col.key].list.innerHTML = ""; });
  card._ui.closedList.innerHTML = "";
  (tasks || []).slice().sort((a, b) =>
    (Number(String(a.id || "").replace(/\D/g, "")) || 0)
    - (Number(String(b.id || "").replace(/\D/g, "")) || 0)
  ).forEach((task) => {
    const key = tcColumnKey(task.status || "pending");
    const target = key === "closed" ? card._ui.closedList : card._ui.cols[key].list;
    target.appendChild(buildTaskRow(card, task));
  });
  tcSyncBoardCounts(card);
}

function tcMemberTokens(card, name) {
  const u = (((card._usage || {}).members) || {})[name];
  return u ? (u.input_tokens || 0) + (u.output_tokens || 0) : 0;
}

function tcUpdateRoster(card, roster, directorMode) {
  card._roster = roster || [];
  const box = card._ui.rosterEl;
  box.innerHTML = "";
  box.appendChild(tcEl("span", "tc-roster-label", "名册"));
  (roster || []).forEach((m) => {
    const item = tcEl("span", "tc-m");
    item.dataset.member = m.name || "";
    item.appendChild(tcAvatar(m.provider));
    item.appendChild(tcEl("span", "tc-m-name", m.name));
    if (m.provider || m.model) {
      item.appendChild(tcEl("span", "tc-m-model", `${m.provider || "?"}/${m.model || "默认"}`));
    }
    const tok = tcMemberTokens(card, m.name);
    if (tok) item.appendChild(tcEl("span", "tc-m-tokens", fmtTokens(tok)));
    if (m.persona) {
      const per = tcEl("span", "tc-m-persona", m.persona);
      per.title = `成员人设：${m.persona}`;
      item.appendChild(per);
    }
    box.appendChild(item);
  });
  // 尾部总管标识（二期）：AI 总管显示 provider/model，用户总管照旧一期文案
  const dir = card._director;
  if (dir) {
    const du = (card._usage || {}).director || {};
    const dTok = (du.input_tokens || 0) + (du.output_tokens || 0);
    const d = tcEl("span", "tc-director is-ai",
      `AI 总管 · ${dir.provider || "?"}/${dir.model || "默认"}` +
      (dTok ? ` · ${fmtTokens(dTok)}` : ""));
    d.title = "AI 总管自动推进团队循环（拆解、派工、验收、交付）；你随时插话，或点下方「接管」亲自指挥";
    box.appendChild(d);
  } else {
    const dirEl = tcEl("span", "tc-director", TEAM_DIRECTOR_LABEL[directorMode] || "用户总管");
    dirEl.title = "你本人就是总管：每条消息都是一道总管指令，派工/验收在工单板上操作";
    box.appendChild(dirEl);
  }
  tcSyncTakeover(card, directorMode);
  // 工单表单的指派下拉同步名册
  const sel = card._ui.taskForm.querySelector(".tf-assignee");
  sel.innerHTML = "";
  (roster || []).forEach((m) => {
    const o = document.createElement("option");
    o.value = m.name;
    o.textContent = m.name;
    sel.appendChild(o);
  });
}

// 接管按钮显隐同步：仅 AI 总管模式且未终态时可见（用户总管/终态后藏起）
function tcSyncTakeover(card, directorMode) {
  const btn = card._ui && card._ui.takeoverBtn;
  if (!btn) return;
  const on = (directorMode || "user") === "ai" && !card._finished;
  btn.hidden = !on;
  if (on) { btn.disabled = false; btn.textContent = "接管"; }
}

// 「存为模板」弹窗（三期）：把这张团队卡的名册与总管形态存成团队模板。
// 名字是唯一键——同名覆盖原条目（位置不变、内容换新），弹窗里实时提示会覆盖；
// 名字不合法（空/超长/带路径分隔符）由后端 TeamError 原文回显在弹窗行内
async function tcSaveTemplateModal(card) {
  const roster = (card._roster || []).map((m) => ({
    provider: m.provider || "", model: m.model || "",
    name: m.name || "", persona: m.persona || "",
  }));
  const director = card._director
    ? { provider: card._director.provider || "", model: card._director.model || "" }
    : { provider: "", model: "" };
  const dirLabel = card._director
    ? `AI 总管（${escapeHtml(director.provider || "?")}/${escapeHtml(director.model || "默认")}）`
    : "用户总管（你本人）";
  // 先拉一次模板册：重名提示要用（拉不到就不提示覆盖，保存仍可用）
  let existing = [];
  try {
    existing = ((await request("team.template_list")).templates || []).map((t) => t.name);
  } catch (e) { /* 模板册读不到：照常打开弹窗，覆盖与否交给保存结果 */ }
  const box = document.createElement("div");
  box.className = "form-grid";
  const memberLabel = roster.map((m) => escapeHtml(m.name || `${m.provider}/${m.model}`)).join("、");
  box.innerHTML = `
    <label class="wide">模板名（同名会覆盖旧模板）
      <input data-f="name" maxlength="60" autocomplete="off" placeholder="如：调研三人组、代码评审小队">
    </label>
    <p class="dim small" data-f="overwrite" hidden></p>
    <p class="dim small">保存内容：${roster.length} 名成员（${memberLabel || "空名册"}）· ${dirLabel}。
      之后建队时在团队面板「从模板创建」一键填入；成员只记服务与模型标识，不校验 Key 是否还在。</p>`;
  const nameInput = box.querySelector('[data-f="name"]');
  const overwriteTip = box.querySelector('[data-f="overwrite"]');
  const syncOverwrite = () => {
    const name = nameInput.value.trim();
    const covered = existing.includes(name);
    overwriteTip.textContent = covered ? `⚠ 已有同名模板「${name}」，保存将覆盖它（位置不变、内容换新）` : "";
    overwriteTip.hidden = !covered;
  };
  nameInput.addEventListener("input", syncOverwrite);
  showModal("另存为团队模板", box, async () => {
    const name = nameInput.value.trim();
    if (!name) throw new Error("请先给模板起个名字");
    const r = await request("team.template_save", {
      name,
      director_mode: card._director ? "ai" : "user",
      director,
      members: roster,
    });
    const savedName = (r && r.template && r.template.name) || name;
    addNotice(existing.includes(savedName)
      ? `已保存模板「${savedName}」（覆盖了同名旧模板）`
      : `已保存模板「${savedName}」`);
  }, "保存模板");
  nameInput.focus();
}

// 「继续推进」：向 AI 总管发一条普通消息，驱动 run_auto_turn 从空闲点续跑。
// 定向到建卡时所属的会话（不依赖当前活动标签），只读分屏列不允许发消息。
async function tcContinueTeam(card) {
  const t = card._tab || curTab();
  if (!t || !t.sid) { addNotice("会话尚未就绪，无法继续推进团队"); return; }
  if (t.readonly) { addNotice("该会话属于其他项目（只读查看），不能继续推进团队。"); return; }
  if (t.running) { addNotice("团队本轮仍在推进中，请等它结束"); return; }
  const text = "继续推进团队，先检查当前工单与待验收事项。";
  withTab(t, () => {
    addUser(text);
    attachMsgOps(curLog().lastElementChild, t, "user", () => text);
    scrollLog();
  });
  setRunning(true, t);
  try {
    const plan = splitPanes.includes(t) ? !!t.planMode : workMode === "plan";
    const r = await request("chat.send", { text, session_id: t.sid, plan_mode: plan });
    setContextUsage(r.context_tokens || 0, r.context_limit || 0, t);
    if (r.context_detail) setContextDetail(r.context_detail, t);
    if (r.team && r.team.team) withTab(t, () => applyTeamMeta(t, r.team.team));
  } catch (e) {
    addNotice("继续推进失败: " + e.message);
    finishEta(t);
    setRunning(false, t);
  }
}

function tcSetSub(card, team) {
  const tasks = team.tasks || [];
  card._usage = team.usage || {};
  const usage = card._usage;
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  const active = (team.status || "active") === "active";
  // 空闲原因是活动期提示：终态快照即使还带着旧值也不能再渲染（「已交付」旁边
  // 挂着「已空闲」会自相矛盾）；直接交付走 tcFinish 的 tcClearIdle 清掉残留。
  const idle = active ? (team.idle_reason || "") : "";
  const idleShort = idle.length > 80 ? idle.slice(0, 80) + "…" : idle;
  card._ui.sub.textContent =
    `${(team.roster || []).length} 名队员 · 已用 ${team.rounds_used || 0}` +
    `${team.max_rounds ? `/${team.max_rounds}` : ""} 轮` +
    (tasks.length ? ` · 工单 ${tasks.length} 张` : " · 尚无工单") +
    (tokens ? ` · ${fmtTokens(tokens)} tokens` : "") +
    (idleShort ? ` · 已空闲：${idleShort}` : "");
  card._ui.sub.title = idle || (tokens
    ? `团队累计用量 ${tokens.toLocaleString()} tokens（成员与总管合计）` : "");
  if (idle) card._ui.channelEl.dataset.idleReason = idle;
  else delete card._ui.channelEl.dataset.idleReason;
  card._ui.status.title = idle || "";
  if (card._ui.resumeBtn) {
    card._ui.resumeBtn.hidden = !idle || card._finished || !active
      || team.director_mode !== "ai";
  }
  // 用量徽标随快照刷新（tcUpdateRoster 先于 tcSetSub 画名册，这里补一次）
  card._ui.rosterEl.querySelectorAll(".tc-m").forEach((item) => {
    item.querySelectorAll(".tc-m-tokens").forEach((el) => el.remove());
    const n = tcMemberTokens(card, item.dataset.member || "");
    if (n) item.appendChild(tcEl("span", "tc-m-tokens", fmtTokens(n)));
  });
}

function tcSetStatus(card, status) {
  const el = card._ui.status;
  el.textContent = TEAM_STATUS_LABEL[status] || status || "进行中";
  el.dataset.status = status || "active";
}

// 终态收口时清掉活动期的空闲副标题（tcSetSub 已对终态快照做了同样防护，
// 这里兜住 team.deliver/stop 直接 tcFinish、不经快照刷新那条路径）。
function tcClearIdle(card) {
  const ui = card._ui;
  if (!ui) return;
  const sub = ui.sub;
  if (sub) {
    const i = sub.textContent.indexOf(" · 已空闲：");
    if (i >= 0) sub.textContent = sub.textContent.slice(0, i);
    sub.title = "";
  }
  if (ui.channelEl) delete ui.channelEl.dataset.idleReason;
  if (ui.status) ui.status.title = "";
}

// 终态卡的「解除常驻」探询（评审③）：按 team_id 查常驻登记——在册才亮出
// 解除入口（后端 team.resident_get 只读探针，仅本机）。回放/中断重建的卡
// 没有活动快照，登记状态只能这样对齐；拉不到（旧后端没有该方法 / 断线）
// 保持隐藏：入口缺席只是降级，不炸渲染
async function tcProbeResidentClear(card) {
  const btn = card._ui && card._ui.residentBtn;
  if (!btn || !card._teamId) return;
  try {
    const r = await request("team.resident_get", { team_id: card._teamId });
    if (!(r && r.resident)) return;
    btn.hidden = false;
    btn.disabled = false;
    btn.classList.remove("is-active");
    btn.textContent = "解除常驻";
    btn.title = "解除常驻：撤销这支队伍的跨会话常驻登记（队伍已结束，登记仍在，定时任务仍会按它复用）";
  } catch (e) { /* 旧后端 / 断线：保持隐藏 */ }
}

// 终态收口：停用交付/收队/表单/行内按钮，展示《交付/收队说明》，自动折叠让出空间
function tcFinish(card, status, summary) {
  card._finished = true;
  tcSetStatus(card, status);
  tcClearIdle(card);
  card.classList.add("ended");
  // 终态/中断卡一律只读：交付、收队、接管、继续推进、工单表单与状态按钮
  // 全部收起（中断回放没有可操作的运行时，留着按钮只会得到「没有团队」）；
  // 常驻开关例外（评审③）——已登记的队伍终态后仍要能解除常驻，按 team_id
  // 走 resident_clear，活动团队要求不再适用
  const ui = card._ui;
  if (ui) {
    [ui.deliverBtn, ui.stopBtn, ui.takeoverBtn, ui.resumeBtn].forEach((btn) => {
      if (!btn) return;
      btn.hidden = true;
      btn.disabled = true;
    });
    if (ui.residentBtn) {
      if (ui.residentBtn.classList.contains("is-active")) {
        // 收口前登记还在（「常驻中」）：直接换解除态，不用再探询
        ui.residentBtn.hidden = false;
        ui.residentBtn.disabled = false;
        ui.residentBtn.classList.remove("is-active");
        ui.residentBtn.textContent = "解除常驻";
        ui.residentBtn.title = "解除常驻：撤销这支队伍的跨会话常驻登记（队伍已结束，登记仍在，定时任务仍会按它复用）";
      } else {
        ui.residentBtn.hidden = true;
        ui.residentBtn.disabled = true;
        tcProbeResidentClear(card);
      }
    }
    if (ui.taskForm) ui.taskForm.hidden = true;
  }
  card.querySelectorAll(".tt-op").forEach((btn) => {
    btn.hidden = true;
    btn.disabled = true;
  });
  if (summary) {
    const old = card.querySelector(".tc-summary");
    if (old) old.remove();
    const sum = tcEl("div", "tc-summary");
    sum.appendChild(tcEl("div", "tc-summary-title",
      status === "aborted" ? "《收队说明》" : "《交付说明》"));
    sum.appendChild(tcEl("pre", "tc-summary-text", summary));
    card._ui.channelEl.after(sum);
  }
  if (!card.classList.contains("folded")) {
    card.classList.add("folded");
    card._ui.fold.textContent = "展开";
    card._ui.fold.setAttribute("aria-expanded", "false");
  }
}

// 频道消息行：from ∈ 成员名|user|director|system；to 非 all 时显示「→ @xx」；
// 用户消息（总管指令）整行高亮；AI 总管发言（含总管口吻的派工消息）行首挂
// 「总管」徽标 + 金底行样式；msg_kind=ruling 的裁定整行紫底；小会系统注记
// 虚线可辨。频道消息是纯文本，一律 textContent 不走 markdown。
function buildMsgRow(card, seq, from, to, kind, taskRef, text) {
  const row = tcEl("div", "tc-msg");
  row.dataset.seq = String(seq);
  const headEl = tcEl("div", "tc-m-head");
  const member = (card._roster || []).find((x) => x.name === from);
  if (member) headEl.appendChild(tcAvatar(member.provider));
  const nameMap = { user: "我（总管）", director: "总管", system: "系统" };
  const fromCls = from === "user" ? " is-user" : from === "director" ? " is-director" : "";
  headEl.appendChild(tcEl("span", "tc-m-from" + fromCls, member ? member.name : (nameMap[from] || from)));
  if (to && to !== "all") {
    headEl.appendChild(tcEl("span", "tc-m-to", to === "director" ? "→ @总管" : "→ @" + to));
  }
  if (from === "user") {
    headEl.appendChild(tcEl("span", "tc-m-kind", "总管指令"));
    row.classList.add("hl-user");
  } else {
    if (from === "director") {
      headEl.appendChild(tcEl("span", "tc-m-badge", "总管"));
      row.classList.add("hl-director");
    }
    if (kind && kind !== "system") {
      headEl.appendChild(tcEl("span", "tc-m-kind k-" + kind, TEAM_MSG_KIND_LABEL[kind] || kind));
    }
  }
  // 裁定行高亮只上非用户行：后端把用户消息也以 ruling 类别入簿（from="user"，
  // 一期样式是蓝底「总管指令」，不叠加紫底）；紫底给总管验收裁定与小会收口
  if (kind === "ruling" && from !== "user") row.classList.add("hl-ruling");
  if (from === "system" && tcIsHuddleNote(text)) row.classList.add("huddle");
  if (taskRef) headEl.appendChild(tcEl("span", "tc-m-ref", "#" + taskRef));
  row.appendChild(headEl);
  row.appendChild(tcEl("div", "tc-m-text", text || ""));
  return row;
}

// 小会系统注记辨识（设计 §4.3）：后端没有专门字段，靠注记文案匹配——
// 开场「总管就「…」召开小会：…」、失败「（小会未能…」；文案变了只会降级
// 成普通系统消息，不会渲染出错
function tcIsHuddleNote(text) {
  const s = String(text || "");
  return s.includes("召开小会") || s.startsWith("（小会");
}

// 定稿消息（事件 TeamMessage 与 team.get 快照 messages 共用；快照键名是 from/to）。
// 按 seq upsert：已有流式半成品行整行替换（定稿 text 是全文，覆盖最稳），按 seq 有序插入。
function tcUpsertMsg(card, m) {
  if (!card || !card._ui) return;
  const channel = card._ui.channelEl;
  const from = m.from_member !== undefined ? m.from_member : m.from;
  const to = m.to_member !== undefined ? m.to_member : m.to;
  const seq = m.seq;
  const old = channel.querySelector(`[data-seq="${seq}"]`);
  const isNew = !old;
  if (old) old.remove();
  const row = buildMsgRow(card, seq, from, to, m.msg_kind, m.task_ref, m.text);
  let anchor = null;
  for (const r of channel.children) {
    if (Number(r.dataset.seq) > seq) { anchor = r; break; }
  }
  channel.insertBefore(row, anchor);
  // 近底部才自动滚：用户上翻回看时频道不抢滚动位置
  if (channel.scrollHeight - channel.scrollTop - channel.clientHeight < 80) {
    channel.scrollTop = channel.scrollHeight;
  }
  if (isNew) tcSyncChannelCount(card); // 定稿替换不改变行数，不必重数
  tcMarkChannelNew(card);
}

// 发言流式增量：按 (member_index, seq) 找（或建）半成品行，追加文本；
// member_index=-1 是 AI 总管（二期）——以 from="director" 建行，徽标/行样式
// 与定稿后的总管消息一致，定稿整行替换（按 seq upsert）
function tcOpenStreamRow(card, data) {
  const channel = card._ui.channelEl;
  let row = channel.querySelector(`[data-seq="${data.seq}"]`);
  const isNew = !row;
  if (!row) {
    const isDirector = data.member_index === -1;
    const member = isDirector ? null : (card._roster || [])[data.member_index] || null;
    row = buildMsgRow(card, data.seq,
      isDirector ? "director" : (member ? member.name : `队员${(data.member_index || 0) + 1}`),
      "", "", "", "");
    row.classList.add("streaming");
    let anchor = null;
    for (const r of channel.children) {
      if (Number(r.dataset.seq) > data.seq) { anchor = r; break; }
    }
    channel.insertBefore(row, anchor);
  }
  row.querySelector(".tc-m-text").textContent += data.text || "";
  if (channel.scrollHeight - channel.scrollTop - channel.clientHeight < 80) {
    channel.scrollTop = channel.scrollHeight;
  }
  if (isNew) tcSyncChannelCount(card);
  tcMarkChannelNew(card);
}

// 用一份团队快照全量对齐卡片（team.get 刷新 / meta 回放 / stop·takeover 返回值共用）
function updateTeamCard(card, team) {
  // 总管标识（二期）：ai 模式快照带 director 块；user 模式无块（一期键集），
  // 一期 meta/回放走这里得 null，照旧渲染「用户总管」
  card._director = (team.director_mode || "user") === "ai"
    ? {
        provider: (team.director && team.director.provider) || "",
        model: (team.director && team.director.model) || "",
      }
    : null;
  card._teamId = String(team.team_id || card._teamId || "");
  // 建队来源模板名（四期常驻）：随快照/meta 落库，常驻登记请求凭它绑定回
  // 模板（后端另有编排器记录兜底，这里带上让请求自含、回放卡也能对上）
  card._template = String(team.template || card._template || "");
  card._lastTeam = team; // 最近一份快照：团队室对终态团队的只读渲染从这里取
  card._usage = team.usage || {}; // 名册用量徽标在 tcUpdateRoster 里读
  tcUpdateRoster(card, team.roster || [], team.director_mode || "user");
  tcSetSub(card, team);
  const status = team.status || "active";
  tcSetStatus(card, status);
  tcRenderTasks(card, team.tasks || []);
  // 常驻开关（T3.2）：team.get 回包贴 resident 标记，点亮/熄灭开关；meta
  // 回放与事件喂入没有这个键时不动当前态——别把用户刚点的开关刷灭
  if (card._ui.residentBtn && team.resident !== undefined && status === "active") {
    card._ui.residentBtn.hidden = false;
    card._ui.residentBtn.classList.toggle("is-active", !!team.resident);
    card._ui.residentBtn.textContent = team.resident ? "常驻中" : "常驻";
  }
  // 快照批量补行：不逐条闪未读圆点，结束后按「行数是否真的变多」补一次
  const before = card._ui.channelEl.querySelectorAll(".tc-msg").length;
  card._suppressNew = true;
  (team.messages || []).forEach((m) => tcUpsertMsg(card, m));
  card._suppressNew = false;
  tcSyncChannelCount(card);
  if (card._ui.channelEl.querySelectorAll(".tc-msg").length > before) tcMarkChannelNew(card);
  if (status !== "active") card.classList.add("ended");
}

// 流里最后一张团队卡（历史回放/接管查找用）
function lastTeamCard(t) {
  const cards = t.logEl.querySelectorAll(".teamcard");
  return cards.length ? cards[cards.length - 1] : null;
}

// 按 team_id 找同一队的卡（无 id 的旧记录退回最后一张）
function teamCardFor(t, teamId) {
  const cards = t.logEl.querySelectorAll(".teamcard");
  if (!teamId) return cards.length ? cards[cards.length - 1] : null;
  for (let i = cards.length - 1; i >= 0; i--) {
    if (cards[i]._teamId === teamId) return cards[i];
  }
  return null;
}

// team_started：建卡（同一个团队不会重复发 team_started；已有未终态卡时只对齐一次）
function beginTeam(data) {
  const t = curTab();
  if (!t) return;
  // 总管字段（二期）：user 模式 provider/model 为空串，按用户总管渲染
  const director = tcDirectorOfEvent(data);
  const prev = (t.teamCard && t.teamCard.isConnected) ? t.teamCard : lastTeamCard(t);
  if (prev && !prev.classList.contains("ended")) {
    t.teamCard = prev;
    prev._director = director;
    tcUpdateRoster(prev, data.roster || [], data.director_mode || "user");
    tcSetStatus(prev, "active");
    return;
  }
  const card = buildTeamCard(data.roster || [], data.director_mode || "user", director);
  tcSetStatus(card, "active");
  // 常驻开关（T3.2）：刚建队的卡直接亮出按钮（不带点亮态；是否常驻由
  // team.get 快照对齐），不等下一次快照刷新
  if (card._ui.residentBtn) card._ui.residentBtn.hidden = false;
  t.teamCard = card;
  curLog().appendChild(card);
  scrollLog();
}

// team_started 事件的总管标识（ai 模式才带 provider/model；user 模式空串 → null）
function tcDirectorOfEvent(data) {
  if ((data.director_mode || "user") !== "ai") return null;
  return { provider: data.director_provider || "", model: data.director_model || "" };
}

function teamMemberDelta(data) {
  const t = curTab();
  const card = (t && t.teamCard && t.teamCard.isConnected) ? t.teamCard : null;
  if (!card) return; // 流式增量是瞬态：定稿与快照刷新会兜底
  tcOpenStreamRow(card, data);
}

function teamMsgFinal(data) {
  const t = curTab();
  const card = (t && t.teamCard && t.teamCard.isConnected) ? t.teamCard : null;
  if (!card) {
    if (t && t.sid) refreshTeamState(t); // 半路进来的标签：拉快照把卡补上
    return;
  }
  tcUpsertMsg(card, data);
}

// team_task_updated 事件里没有 deps/accept：与行上记住的旧数据合并后再重画
function teamTaskUpdated(data) {
  const t = curTab();
  const card = (t && t.teamCard && t.teamCard.isConnected) ? t.teamCard : null;
  if (!card) return;
  const row = card._ui.boardList.querySelector(`[data-task="${data.task_id}"]`);
  const prev = (row && row._task) || {};
  tcUpsertTask(card, Object.assign({}, prev, {
    id: data.task_id, title: data.title, assignee: data.assignee,
    type: data.type, status: data.status, redo: data.redo,
    detail: data.detail !== undefined ? data.detail : prev.detail,
    source: data.source !== undefined ? data.source : prev.source,
    auto_dispatch: data.auto_dispatch !== undefined ? data.auto_dispatch : prev.auto_dispatch,
  }));
}

function teamFinished(data) {
  const t = curTab();
  const card = (t && t.teamCard && t.teamCard.isConnected) ? t.teamCard : lastTeamCard(t);
  if (!card) return;
  t.teamCard = card;
  tcFinish(card, data.status, data.summary || "");
}

// ---------- 会话级活动团队状态：切标签 / 加载历史时向引擎对齐 ----------
// 团队是会话级状态，本窗口的事件只覆盖实时轮；其他窗口的收队、应用重启后的
// 「已中断」标注，都要靠 team.get 对齐（内存快照，代价可忽略）。
async function refreshTeamState(tab) {
  if (!tab || !tab.sid || tab._teamRefreshing) return;
  tab._teamRefreshing = true;
  try {
    const r = await request("team.get", { session_id: tab.sid });
    withTab(tab, () => applyTeamSnapshot(r));
  } catch (e) { /* 引擎暂无团队方法（旧后端 / 连接断开）：静默跳过 */ }
  finally { tab._teamRefreshing = false; }
}

function applyTeamSnapshot(r) {
  const t = curTab();
  if (!t) return;
  if (r && r.active && r.team) {
    applyTeamMeta(t, r.team);
    return;
  }
  // 重启/切项目前的团队一律「已中断」，不自动恢复（沿子代理任务簿先例）：
  // 流里有同一队的未终态卡就标中断；没有就建一张只读回放卡（名册是
  // ["名字"] 字符串，建卡前补成 {name, provider, model} 形状）
  if (r && r.interrupted) {
    const teamId = String(r.interrupted.team_id || "");
    const card = teamCardFor(t, teamId);
    if (card) {
      if (!card.classList.contains("ended")) {
        tcFinish(card, "interrupted", "");
        card.classList.add("interrupted");
        card._ui.sub.textContent = "应用重启前的团队记录 · 只读回放";
      }
      if (teamId) tcFetchFullLog(t, card, { team_id: teamId, session_id: t.sid });
      return;
    }
    if (t._teamInterruptedNoticed) return;
    t._teamInterruptedNoticed = true;
    const roster = (r.interrupted.roster || []).map((m) =>
      (m && typeof m === "object")
        ? m
        : { name: String(m || ""), provider: "", model: "" });
    const fresh = buildTeamCard(roster, r.interrupted.director_mode || "user",
      r.interrupted.director_mode === "ai" ? { provider: "", model: "" } : null);
    fresh._teamId = teamId;
    fresh.classList.add("interrupted");
    fresh._ui.sub.textContent = "应用重启前的团队记录 · 只读回放";
    tcFinish(fresh, "interrupted", "");
    t._lastTeamCard = fresh;
    t.logEl.appendChild(fresh);
    if (teamId) tcFetchFullLog(t, fresh, { team_id: teamId, session_id: t.sid });
    addNotice(`${r.interrupted.interrupt_reason || "应用重启，团队已中断"}：已载入只读频道回放；工单板与执行状态未恢复。`);
  }
}

// 把一份团队快照落到当前聊天流：卡在流中就刷新；否则建一张（插在纪要气泡前）
function applyTeamMeta(t, team) {
  let card = (t.teamCard && t.teamCard.isConnected) ? t.teamCard : lastTeamCard(t);
  const active = (team.status || "active") === "active";
  if (!card || (card.classList.contains("ended") && active)) {
    // 前一张卡已终态而团队仍在活动（重新建队）：另起一张新卡
    const fresh = buildTeamCard(team.roster || [], team.director_mode || "user",
      tcDirectorOfSnapshot(team));
    fresh._tab = t;
    fresh._teamId = String(team.team_id || "");
    t.teamCard = fresh;
    const last = t.logEl.lastElementChild;
    // assistant_message 事件先于 chat.send 返回到达：纪要气泡已在流尾，卡插在它前面
    if (last && last.classList.contains("msg") && last.classList.contains("assistant")) {
      t.logEl.insertBefore(fresh, last);
    } else {
      t.logEl.appendChild(fresh);
    }
    card = fresh;
  } else {
    t.teamCard = card;
  }
  updateTeamCard(card, team);
}

// 历史回放：从持久化的 meta 重建团队卡（默认折叠；同一团队跨多轮刷新同一张卡）。
// meta 兼容二期 director 块（ai 模式新增）；一期 meta 无块照旧渲染「用户总管」
function buildTeamReplayCard(meta, tab) {
  const team = meta.team || {};
  const card = buildTeamCard(team.roster || [], team.director_mode || "user",
    tcDirectorOfSnapshot(team));
  card._tab = tab || curTab();
  card._teamId = String(team.team_id || "");
  card.classList.add("folded");
  card._ui.fold.textContent = "展开";
  card._ui.fold.setAttribute("aria-expanded", "false");
  // 终态团队的历史卡一律只读（重画工单行时不再挂派工/验收按钮）
  if ((team.status || "active") !== "active") {
    card._finished = true;
    card.classList.add("ended");
    // 常驻登记不随队伍终态消失：在册的队伍亮出「解除常驻」（评审③）
    tcProbeResidentClear(card);
  }
  updateTeamCard(card, team);
  return card;
}

// 团队快照 meta 的总管标识（ai 模式带 director 块；一期 meta 无块 → null）
function tcDirectorOfSnapshot(team) {
  if ((team.director_mode || "user") !== "ai") return null;
  const d = team.director || {};
  return { provider: d.provider || "", model: d.model || "" };
}

// 三期回放增强：meta 里的频道是截断尾部（条数与单条长度都有上限），有 team_id
// 就拉 team.log 全量回放，把完整时间线叠画到卡上（tcUpsertMsg 按 seq 幂等覆盖，
// 行键 from_member/to_member 与快照 from/to 两种写法都认）。
// 两级容错：team_id 缺失 / 请求失败（他人会话的 id / 旧后端 / 连接断开）/
// 库内零记录（旧版本团队没落过库）都静默退回截断 meta 渲染——回放是锦上添花，
// 不能让它炸掉历史渲染。同一团队在长历史里跨多轮 meta 共用一张卡（或终态后
// 重建卡），teamLogCache 按 team_id 只拉一次，后到的卡直接复用已拉的行。
const teamLogCache = new Map(); // team_id -> rows[]（成功响应才缓存；空数组也算结果）
const TEAM_LOG_CACHE_MAX = 40;

function tcFetchFullLog(tab, card, team) {
  if (!card || !card._ui) return;
  const teamId = String((team || {}).team_id || "");
  if (!teamId) return; // 旧 meta 没落过 team_id：截断 meta 就是全部家底
  if (card._logTeamId === teamId) {
    // 这张卡已经拉过/正在拉同一队：在途就等结果落地；已入缓存就把全量行
    // 再叠一遍——后来画的截断 meta 可能刚覆盖了同 seq 的行
    if (teamLogCache.has(teamId)) tcApplyLogRows(card, teamLogCache.get(teamId));
    return;
  }
  card._logTeamId = teamId; // 同一张卡只发一次请求（跨多轮 meta 重复调用来去重）
  if (teamLogCache.has(teamId)) {
    tcApplyLogRows(card, teamLogCache.get(teamId));
    return;
  }
  request("team.log", {
    team_id: teamId,
    session_id: (team && team.session_id) || (tab && tab.sid) || undefined,
  })
    .then((r) => {
      const rows = (r && r.messages) || [];
      teamLogCache.set(teamId, rows);
      if (teamLogCache.size > TEAM_LOG_CACHE_MAX) {
        teamLogCache.delete(teamLogCache.keys().next().value); // 淘汰最早入册的
      }
      tcApplyLogRows(card, rows);
    })
    .catch(() => { /* 拉不到：保持截断 meta 渲染，下次重建卡再试 */ });
}

function tcApplyLogRows(card, rows) {
  if (!rows || !rows.length) return;
  rows.forEach((m) => tcUpsertMsg(card, m));
}

// 团队纪要徽标：标记这条回答是团队协作的纪要（队员发言全文见团队卡的频道时间线）
function addTeamBadge(el, meta) {
  const team = meta.team || {};
  const roster = team.roster || [];
  const badge = document.createElement("div");
  badge.className = "rt-badge team-badge";
  badge.title = roster.map((m) => `${m.name}（${m.provider}/${m.model}）`).join("\n") +
    (team.director_mode === "ai" && team.director
      ? `\nAI 总管（${team.director.provider}/${team.director.model}）` : "");
  badge.textContent =
    `🧩 团队纪要 · ${roster.length} 名队员` +
    (team.director_mode === "ai" ? " · AI 总管" : "") +
    ` · ${TEAM_STATUS_LABEL[team.status] || "进行中"}`;
  el.prepend(badge);
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
  // 用户切过去就能看到 Agent 刚写的文件，不用再手动点「刷新」；
  // 审查页同理——新的改动轮次自动出现在列表里
  if (!data.is_error && (tname === "write_file" || tname === "edit_file" ||
      tname === "move_file" || tname === "delete_file" || tname === "make_dir")) {
    scheduleFilesRefresh();
    scheduleReviewRefresh();
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
    pre.innerHTML = renderDiffText(data.diff); // 与审查页同一套 diff 行分类
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
let ctxPopFace = null; // 弹层当前服务哪一面（null = 主栏环）
function renderCtxPop() {
  const d = ctxPopFace ? ctxPopFace.ctxDetail : (activeTab && activeTab.ctxDetail);
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
  const ringEl = ctxPopFace ? ctxPopFace.el.querySelector(".split-ctx-ring") : ctxRingEl;
  const r = ringEl.getBoundingClientRect();
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
function openCtxPop(face) {
  clearTimeout(ctxPopHideTimer);
  ctxPopFace = face || null;
  ctxPopOpen = true;
  if (renderCtxPop()) { ctxPop.classList.remove("hidden"); placeCtxPop(); return; }
  // 还没有明细数据（本轮尚未发过消息也没拉过状态）：拉一次，回来时仍悬停着就补弹
  request("chat.status", ctxPopFace ? { session_id: ctxPopFace.sid } : {}).then((st) => {
    if (st && st.context_detail) {
      setContextDetail(st.context_detail, ctxPopFace || activeTab);
      if (ctxPopOpen && renderCtxPop()) { ctxPop.classList.remove("hidden"); placeCtxPop(); }
    }
  }).catch(() => {});
}
function closeCtxPop(now = false) {
  clearTimeout(ctxPopHideTimer);
  if (now) { ctxPopOpen = false; ctxPopFace = null; ctxPop.classList.add("hidden"); return; }
  ctxPopHideTimer = setTimeout(() => { ctxPopOpen = false; ctxPopFace = null; ctxPop.classList.add("hidden"); }, 150);
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
  if (t === ctxPopFace && ctxPopOpen && renderCtxPop()) placeCtxPop();
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

/** 拉当前会话的任务清单画一遍（chat.status 的 todos 字段，/todos 命令同源）。
 *
 *  todo_updated 事件只在 Agent 写清单时来；切会话 / 展开面板 / 恢复上次打开的
 *  标签这些路径没有事件，不主动拉就会显示上一个会话的清单或一层空壳。 */
async function loadTodoPanel() {
  try {
    const st = await request("chat.status");
    renderTodoPanel(st.todos || []);
  } catch (e) { /* 拉不到就保留现状（多为无项目/断线，别拿错误刷掉旧内容） */ }
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
      <button class="ptask-del" title="删除这条事项">✕</button>`;
    li.querySelector(".ptask-check").onchange = async (e) => {
      try {
        await request("project.task_update", { id: t.id, done: e.target.checked });
        loadProjectTasks(true);
      } catch (err) {
        e.target.checked = !e.target.checked;
        addNotice("更新事项失败：" + err.message);
      }
    };
    li.querySelector(".ptask-del").onclick = async () => {
      try {
        await request("project.task_delete", { id: t.id });
        loadProjectTasks(true);
      } catch (err) {
        addNotice("删除事项失败：" + err.message);
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
  showModal(isNew ? "新建事项" : "编辑事项", box, async () => {
    const title = box.querySelector('[data-f="title"]').value.trim();
    if (!title) throw new Error("事项内容不能为空");
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
      .catch((err) => addNotice("添加事项失败：" + err.message))
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
  // 文案写进 .send-txt（挤压档按钮翻成纯图标，svg 常驻不能被 textContent 清掉）；
  // 图标态下「发送/排队」的区别靠 title 悬停说明
  (sendBtn.querySelector(".send-txt") || sendBtn).textContent = on ? "排队" : "发送";
  sendBtn.title = on ? "运行中：点击排队追加一条消息（Enter）" : "发送（Enter）";
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
  // 分屏列也是渲染面（task_estimate 会路由进列）：不遍历到它，列里的
  // 「已 X 秒」永远停在 0
  for (const t of [...chatTabs, ...splitPanes]) {
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
  // 接力（主副屏交替）状态：无会话路由，交给右侧看板按 pair_id 更新。
  if (kind === "relay_pair_state") {
    if (typeof relayOnPairState === "function") relayOnPairState(data && data.relay);
    return;
  }
  // 旧的无工具串行接力事件（界面已不再使用）：保留钩子，避免旧后端混跑时报错。
  if (kind === "relay_state") {
    if (typeof relayOnState === "function") relayOnState(data && data.relay);
    return;
  }
  // 多会话路由：事件带 session_id → 找到（或后台创建）对应标签再渲染。
  // session_updated 例外：它只是另一窗口「改名/归档/移动」等元数据操作的广播
  // 回声，不承载轮次内容——为它开标签会把刚归档（或刚关掉标签）的会话原样
  // 弹回标签栏（用户报的「归档后自动冒出一个新标签」正是它）。它的 case 自带
  // 缓存更新与 refreshSessions，不需要 routeTab。
  if (data && data.session_id && kind !== "session_updated") {
    routeTab = tabFor(data.session_id);
    if (!routeTab) {
      // 分屏列优先：该会话在分屏里跑（独立输入发出的），流式增量直接进列，
      // 不走下面的「后台开标签」——分屏会话不该因此在标签栏冒出一个标签
      const pane = splitPanes.find((p) => p.sid === data.session_id);
      if (pane) {
        routeTab = pane;
      } else {
        // 无绑定会话的运行中标签：说明这是它懒创建的会话，直接认领，避免重复建标签
        const adopt = chatTabs.find((t) => !t.sid && t.running);
        if (adopt) {
          adopt.sid = data.session_id;
          currentSessionId = data.session_id;
          migrateComposerSid("", data.session_id); // 空标签的输入区状态条目随认领迁移
          if (adopt === activeTab) refreshActiveSessionBadges(adopt);
          routeTab = adopt;
          renderTabs();
        } else {
          routeTab = openTabForSession(data.session_id, "", { background: true });
          refreshSessions();
        }
      }
    }
  } else {
    routeTab = activeTab;
  }
  switch (kind) {
    case "thinking_delta": appendThinking(data.text); break;
    case "text_delta": appendStream(data.text); break;
    case "assistant_message": {
      // 有流式元素（普通轮/主席融合/对抗报告）→ 收尾；无流式元素（对比模式的成员回答）→ 直接渲染完整气泡
      const t = curTab();
      const meta = data.message && (data.message.roundtable || data.message.adversarial);
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
    case "adversarial_started": beginAdversarial(data.roles || []); break;
    case "adversarial_phase": advSetPhase(data.phase, data.note || ""); break;
    case "adversarial_finding_proposed": advAddFinding(data); break;
    case "adversarial_verdict": advApplyVerdict(data); break;
    case "adversarial_finished": advFinished(data); break;
    // 团队五事件在喂团队卡的同时喂团队室容器（同一套 tc* 渲染函数，
    // app-teamroom.js 的 trRoomFeed 分发；页签未打开时那边只累计未读）
    case "team_started": beginTeam(data); if (typeof trRoomFeed === "function") trRoomFeed("trOnStarted", data); break;
    case "team_message_delta": teamMemberDelta(data); if (typeof trRoomFeed === "function") trRoomFeed("trOnDelta", data); break;
    case "team_message": teamMsgFinal(data); if (typeof trRoomFeed === "function") trRoomFeed("trOnMsg", data); break;
    case "team_task_updated": teamTaskUpdated(data); if (typeof trRoomFeed === "function") trRoomFeed("trOnTask", data); break;
    case "team_finished": teamFinished(data); if (typeof trRoomFeed === "function") trRoomFeed("trOnFinish", data); break;
    case "tool_call_started": addToolCard(data); break;
    case "tool_call_finished": finishToolCard(data); break;
    case "subagent_spawned": onSubagentSpawned(data); break;
    case "subagent_event": onSubagentEvent(data); break;
    case "task_finished": onTaskFinished(data); break;
    case "permission_request":
      showPermission(data); // 通知统一在 showPermission 里发（活动/后台各一份）
      break;
    case "permission_resolved": {
      // 只收起「这一张」卡：多标签同时各挂一张确认卡时，后台标签的 resolved
      // 不该把活动标签正在显示的另一张真卡也藏掉（藏掉后 permRequest 已 null，
      // 真卡按钮会静默失效，只能靠切标签恢复）
      if (!data.request_id || data.request_id === permRequest) hidePermission();
      // 同样只清「对应的那个标签」：permission_resolved 不带 session_id（后端
      // 契约如此），handleEvent 的会话路由会回落到 activeTab——旧实现无条件
      // 清 routeTab 的卡数据，于是「A 标签的卡被解决」会把 B 标签挂着的那张
      // 未决卡一并抹掉：切到 B 时 activateTab 的 `needsPerm && permData` 恢复
      // 分支拿不到数据，卡永久消失，而后端那一轮还挂在权限门上无限等待。
      // 改为按 request_id 匹配，只清真正被解决的那一张。
      const rid = data.request_id;
      if (rid) {
        for (const t of [...chatTabs, ...splitPanes]) {
          if (t.permData && t.permData.request_id === rid) {
            t.needsPerm = false;
            t.permData = null;
          }
        }
        if (routeTab && routeTab.permData && routeTab.permData.request_id === rid) {
          routeTab.needsPerm = false;
          routeTab.permData = null;
        }
        renderTabs();
      }
      break;
    }
    case "usage":
      usageIn += data.input_tokens || 0;
      usageOut += data.output_tokens || 0;
      renderUsage();
      break;
    case "todo_updated": renderTodoPanel(data.items); break;
    case "task_estimate": showTaskEstimate(data); break;
    case "schedule_updated": {
      if (rightViewVisible("agenda")) loadAgenda();
      break;
    }
    case "factory_run": {
      // 工厂：一次运行的开始 / 结束（顶层不带 session_id，会话 id 只在 run 里）。
      // 视图没开也照传——app-factory.js 自己决定只更新内存态还是顺带重绘。
      if (typeof factoryOnEvent === "function") factoryOnEvent(data);
      break;
    }
    case "mcp_updated": {
      // MCP 后台连接完成/状态变化（启动不阻塞，连完推送）：设置页开着或
      // MCP/Skills 标签开着都重绘——ext 页签的卡片是从设置页搬过去的同一份
      // DOM，不刷就会一直显示旧的连接态
      if (settingsOpen || rightTabs.includes("ext")) {
        renderSettings().catch(() => {});
      }
      break;
    }
    case "mod_ui":
      // Claude 兼容模式的元素树走 onModUiTree；旧的 widget 词表走 onModUi
      if (data.payload && data.payload.tree) onModUiTree(data);
      else onModUi(data);
      break;
    case "mods_updated": {
      // Mods 安装/启停/删除后的广播（notify_mods_updated）：设置页开着就重绘清单
      if (settingsOpen) renderModsCfg().catch(() => {});
      break;
    }
    case "schedule_reminder":
      pushNotice("⏰ 日程提醒", data.title || "");
      showAgendaReminder(data);
      break;
    case "session_updated": {
      if (data.session_id) {
        if (data.deleted) {
          // 其他窗口删了这个会话：摘掉本地缓存与打开的标签/分屏列，列表随之刷新
          //（分屏列不收的话会残留成死列：⟳ 与发送对着已删会话只会报错）
          delete sessionMeta[data.session_id];
          closeSplitPanesForSids(new Set([data.session_id]));
          const dead = tabFor(data.session_id);
          if (dead) closeTab(dead);
          // 删的是接力副屏：看板上的「副屏」胶囊跟着回到未创建
          if (typeof relayPeerDeleted === "function") relayPeerDeleted(data.session_id);
        } else {
          if (data.archived === true) {
            // 归档：侧栏行消失，上方标签与分屏列一并收掉（与删除同一处置，但
            // 会话没删——归档弹窗里恢复后重新点开即可）。收标签走 closeTab，
            // 标签集合与 ui.json 的恢复列表由 renderTabs 同步写回，重启不会把
            // 归档标签还原。
            closeSplitPanesForSids(new Set([data.session_id]));
            const t = tabFor(data.session_id);
            if (t) closeTab(t);
          }
          if (data.title) {
            // 只在带标题时覆盖缓存：pin/移动/标签的广播不带新标题，
            // 拿空串覆盖会把标签名抹成空白
            sessionMeta[data.session_id] = { title: data.title };
            const t = tabFor(data.session_id);
            if (t) { t.title = data.title; renderTabs(); }
          }
        }
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
      if (rightViewVisible("cron")) {
        loadCron();
      }
      // 工厂开着时同步：跑完后的「下次运行」、新建 / 暂停 / 删除的产线都要跟上
      if (typeof factoryOnAutomationChanged === "function") factoryOnAutomationChanged();
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
        if (rightViewVisible("pipeline")) loadPipelines();
      }
      // 工厂开着时同步：编排起停、节点状态与产线成败都要跟上
      if (typeof factoryOnAutomationChanged === "function") factoryOnAutomationChanged();
      break;
    }
    case "notice": addNotice("⏳ " + data.message); break;
    case "memory_digest": addNotice("🧠 " + data.message); break;
    case "memory_maintain": addNotice("🧹 " + data.message); break;
    case "map_updated": {
      // 演化摘要生成完成/失败：按钮状态复位 + 提示 + 面板正开着就重拉
      mapState.genBusy = false;
      updateMapGenBtn();
      addNotice((data.ok === false ? "🗺 " : "🗺✦ ") + (data.message || "演化摘要已更新"));
      if (rightViewVisible("map")) loadMemoryMap();
      break;
    }
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
        // `|| ""` 兜底：伪 tab（分屏列）理论上都有初值，这里再防一手
        // undefined——trim 抛错会中断这次事件的后续处理
        if (doneTab && (doneTab.lastAssistantText || "").trim()) {
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
    case "aux_delta": {
      const sec = document.getElementById("rp-page-" + (data.aux_id || "aux1"));
      if (sec) {
        const st = getAuxState(sec);
        st.streamingText += data.text || "";
        st.thinkingPhase = false;
        auxScheduleRender(sec);
      }
      break;
    }
    case "aux_thinking": {
      const sec = document.getElementById("rp-page-" + (data.aux_id || "aux1"));
      if (sec) {
        const st = getAuxState(sec);
        st.thinkingText += data.text || "";
        st.thinkingPhase = !st.streamingText;
        auxScheduleRender(sec);
      }
      break;
    }
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
  // 分屏列的路由后状态同步：列头运行点/确认徽记跟着事件走。
  // permission_request 主栏口径的全局确认条照弹（showPermission），列头另挂
  // 🔒 提示这一栏在等确认；确认面板是主栏单例，点列头 🔒 带着待确认数据去
  // 主栏处理。运行点只认 queue_updated（后端的运行状态唯一事实源）：
  // assistant_message 每个中间轮（含工具调用轮）都会发、task_finished 是后台
  // 子代理任务终态——拿它们当轮末信号会把蓝点在回合中途熄灭；running 标志
  // 已由上面 setRunning 按 queue_updated 维护，这里只补画列头（setRunning 对
  // 非活动标签提前 return，不会自己调 paintSplitHead）
  if (splitPanes.includes(routeTab)) {
    const pane = routeTab;
    if (kind === "queue_updated") {
      paintSplitHead(pane);
    } else if (kind === "permission_request") {
      pane.needsPerm = true;
      pane.permData = data; // 列头 🔒 点击时转移到主栏标签恢复确认卡
      paintSplitHead(pane);
    } else if (kind === "permission_resolved") {
      pane.needsPerm = false;
      pane.permData = null;
      paintSplitHead(pane);
    }
  }
}

// 工具安全级展示名：权限条/勾选表等面向用户的界面统一用它，不露内部枚举值
const SAFETY_LABELS = { readonly: "只读", write: "写入", dangerous: "高危" };

// 断线重连 / 页面刷新后，后端可能还挂着未决的权限请求（那一轮仍在等决策），
// 但确认卡随事件流已丢——补拉 permission.queue 重建当前会话的卡，避免
// 「后端在等、前端看不到」的假死（安全审查 M12 同源问题：残留卡可被迟到
// 投递，丢卡同样危险）。只恢复**活动会话**的请求；后台会话的确认卡由标签
// 徽记与运行中心「等待确认」分区承接，恢复时不动它们。
let restoredPermReq = null; // 已恢复过的 request_id：同一张卡只重建一次

async function restorePendingPermissions() {
  let pending = [];
  try {
    const r = await request("permission.queue", {});
    pending = (r && r.pending) || [];
  } catch (e) {
    return; // 拉不到（后端旧版/断线）就维持现状
  }
  if (!pending.length) { restoredPermReq = null; return; }
  const sid = activeTab && activeTab.sid;
  const mine = pending.find((p) => !sid || String(p.session_id) === String(sid));
  if (!mine) return;
  if (restoredPermReq === mine.request_id) return; // 已在屏上 / 已恢复过
  restoredPermReq = mine.request_id;
  // detail 为空时 showPermission 会回退到 JSON.stringify(input)；恢复数据没有
  // input 字段，给个空对象避免渲染出 "undefined"
  showPermission({ ...mine, input: mine.detail ? undefined : {}, session_id: mine.session_id || sid });
}

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
  // 活动标签的卡同样要标到标签上：切走再切回时 activateTab 靠 needsPerm/permData
  // 恢复显示。旧实现只写全局单例 permRequest，切一下标签卡片就永久丢失——
  // 而后端那一轮还挂在等决策上，会话看起来像假死。
  if (routeTab) {
    routeTab.needsPerm = true;
    routeTab.permData = data;
    renderTabs();
  }
  permRequest = data.request_id;
  maybeNotify("需要你确认", `${data.tool_name} 正在等待你的决定`, "perm",
    { sid: routeTab && routeTab.sid });
  document.getElementById("perm-tool").textContent =
    `${data.tool_name} · ${SAFETY_LABELS[data.safety] || data.safety}`;
  // 固定字段行：发起方 / 影响范围 / 可撤销性。后端没给的字段整行隐藏，
  // 旧版本引擎（无这些字段）的卡退化为原有版式，不出空槽位。
  const metaEl = document.getElementById("perm-meta");
  const originEl = document.getElementById("perm-origin");
  const scopeEl = document.getElementById("perm-scope");
  const revertEl = document.getElementById("perm-revert");
  let metaShown = false;
  if (originEl) {
    originEl.textContent = data.origin ? `发起方：${data.origin}` : "";
    originEl.hidden = !data.origin;
    if (data.origin) metaShown = true;
  }
  if (scopeEl) {
    scopeEl.textContent = data.scope ? `范围：${data.scope}` : "";
    scopeEl.hidden = !data.scope;
    if (data.scope) metaShown = true;
  }
  if (revertEl) {
    revertEl.textContent = data.reversible
      ? (data.revert_note || "可撤销：可用「撤销本轮改动」恢复")
      : (data.revert_note || "不可自动回滚");
    revertEl.className = "perm-meta-item " + (data.reversible ? "perm-revert-ok" : "perm-revert-no");
    revertEl.hidden = false;
    metaShown = true;
  }
  if (metaEl) metaEl.hidden = !metaShown;
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
  // Mod（实验性）的附加说明与 perm 片段：强制 [Mod·<id>] 来源前缀（引擎侧已带）、
  // 独立样式区渲染——第三方文本不上无标识的安全确认卡
  const modNoteEl = document.getElementById("perm-mod-note");
  if (modNoteEl) {
    // 用 DOM 构建而非 innerHTML 拼接：Mod 元素树是结构化节点，必须走
    // textContent 落地；旧的字符串片段（html 已 escape）用 <br> 分隔保持原样。
    modNoteEl.replaceChildren();
    let hasContent = false;
    if (data.mod_note) {
      const line = document.createElement("div");
      line.innerHTML = escapeHtml(data.mod_note);
      modNoteEl.appendChild(line);
      hasContent = true;
    }
    const sidLines = pendingPermModLines.get(data.session_id || "");
    if (sidLines && sidLines.length) {
      sidLines.forEach((entry) => {
        if (entry && typeof entry === "object" && entry.tree) {
          const el = buildModNode(entry.tree);
          if (!el) return;
          const wrap = document.createElement("div");
          wrap.className = "mod-tree";
          const cap = document.createElement("span");
          cap.className = "mod-frag-src";
          cap.textContent = "Mod·" + (entry.modId || "");
          wrap.appendChild(cap);
          wrap.appendChild(el);
          modNoteEl.appendChild(wrap);
          hasContent = true;
        } else if (typeof entry === "string" && entry) {
          const line = document.createElement("div");
          line.innerHTML = entry;
          modNoteEl.appendChild(line);
          hasContent = true;
        }
      });
    }
    modNoteEl.hidden = !hasContent;
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
  pendingPermModLines.clear(); // 确认卡收起：上一张卡的 Mod 片段不留到下一张
  const modNoteEl = document.getElementById("perm-mod-note");
  if (modNoteEl) { modNoteEl.hidden = true; modNoteEl.innerHTML = ""; }
  document.getElementById("permission-bar").hidden = true;
  petRefresh();
}

document.querySelectorAll("#permission-bar [data-decision]").forEach((btn) => {
  btn.onclick = () => {
    if (!permRequest) return;
    const rid = permRequest;
    request("permission.respond", { request_id: rid, decision: btn.dataset.decision })
      .then((res) => {
        if (res && res.delivered === false) {
          // 决策没有送达（那一轮已被停止，request_id 已失效）：收起死卡并明说，
          // 别让用户对着永远无响应的卡反复点（后端停止时已补发清卡事件，
          // 这里是「事件丢了/竞态」的兜底）
          for (const t of chatTabs) {
            if (t.permData && t.permData.request_id === rid) {
              t.needsPerm = false;
              t.permData = null;
            }
          }
          hidePermission();
          renderTabs();
          addNotice("该权限请求已失效：对应的轮次已停止，无需再确认。");
        }
      })
      .catch(() => {});
  };
});

// ---------- 侧栏 ----------
let sessionSearchActive = false; // 搜索结果展示期间，禁止列表刷新覆盖
let activeSessionSid = null; // 侧栏里被点开的会话 id（两种视图共用）：列表因折叠/快照等重画后，选中高亮照它恢复
// 经典视图选中的项目列表：null=当前项目；"quick"=快聊；"remote:<id>"=远程连接。
// 点哪行亮哪行（项目区同时最多一行高亮）；会话区高亮独立、最多一条（sess-active）
let classicViewGk = null;
// 本机桌面端才拿得到跨项目会话列表（session.list all_projects）：首次探测后
// 记住 true/false，远程端不再反复请求（false=项目区不放快聊/远程行）
let classicQuickAvailable = null;

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
  // 无项目态默认陈列快聊（会话区只可能是快聊）；已选远程连接时不覆盖。
  // 远程客户端拿不到 quick_sessions（字段不下发），不强推快聊视图
  if (classicGk == null && classicViewGk == null && Array.isArray(quick_sessions)) {
    classicViewGk = "quick";
  }
  // 特殊列表（快聊 / 远程连接）：由项目行点击切换（见 refreshProjects）。
  // 不随标签切换自动进退——高亮与陈列都只由「点哪行」决定
  if (classicViewGk === "quick") {
    // 无快聊对话时不摆空态提示（入口就在项目区的「快聊」行上，不必再教一遍）
    const qlist = Array.isArray(quick_sessions) ? quick_sessions : [];
    const qOrdered = orderedSessionList(qlist, classicViewGk);
    lastGroupedListByGroup.set(classicViewGk, qOrdered); // 拖拽排序的家族归并数据源（commitSessionOrder 读）
    renderPlainList(ul, qOrdered, "quick", "");
    // 与 proj 视图同一套拖拽：列表内重排（序存 quick 键）、拖出进对话区=分屏
    // （快聊属固定项目、无 root_path，drop 侧不会触发切换，本机分屏列全可交互）
    wirePlainListDrag(ul, qOrdered, classicViewGk);
    const footer = document.getElementById("session-footer");
    if (footer) { // 页脚统计是当前项目的，特殊列表下不展示也不残留
      footer.classList.add("hidden");
      footer.innerHTML = "";
    }
    renderRailSessions(sessions);
    return;
  }
  if (classicViewGk && classicViewGk.startsWith("remote:")) {
    // 「远程连接」列表：跨项目拉取后按项目过滤（本机桌面端才有 all_projects）
    const rid = classicViewGk.slice("remote:".length);
    const all = await request("session.list", { all_projects: 1 }).catch(() => null);
    const rlist = ((all && all.sessions) || []).filter((s) => String(s.project_id) === rid);
    const rOrdered = orderedSessionList(rlist, classicViewGk);
    lastGroupedListByGroup.set(classicViewGk, rOrdered); // 拖拽排序的家族归并数据源（commitSessionOrder 读）
    renderPlainList(ul, rOrdered, classicViewGk,
      "还没有渠道对话——在飞书/微信里给机器人发条消息就会出现在这里");
    // 与 proj 视图同一套拖拽：列表内重排（序存 remote:<id> 键）、拖出进对话区
    // =分屏（渠道会话属固定项目、无 root_path，drop 侧不触发切换，本机列全可交互）
    wirePlainListDrag(ul, rOrdered, classicViewGk);
    const footer = document.getElementById("session-footer");
    if (footer) {
      footer.classList.add("hidden");
      footer.innerHTML = "";
    }
    renderRailSessions(sessions);
    return;
  }
  if (classicViewGk && classicViewGk.startsWith("proj:")) {
    // 「看其他项目」列表（点项目区其他项目行进入）：跨项目拉取后按项目过滤；
    // 行点击＝先切工作项目再打开（后端要求会话在其项目内激活，同分组视图
    // 跨项目行点击的流程）
    const pid = classicViewGk.slice("proj:".length);
    const all = await request("session.list", { all_projects: 1 }).catch(() => null);
    const plist = ((all && all.sessions) || []).filter((s) => String(s.project_id) === pid);
    const projs = ((await request("project.list").catch(() => null)) || {}).projects || [];
    const target = projs.find((p) => String(p.id) === pid);
    ul.innerHTML = "";
    if (!plist.length) {
      ul.innerHTML = '<li class="empty-hint">这个项目还没有会话</li>';
    } else {
      const ordered = orderedSessionList(plist, classicViewGk);
      lastGroupedListByGroup.set(classicViewGk, ordered); // 拖拽排序的家族归并数据源（commitSessionOrder 读）
      ordered.forEach((s) => {
        const li = renderSessionItem(s, ul);
        // 与经典/分组视图同一套拖拽：列表内重排（序存 proj:<id> 键）、
        // 拖出列表进对话区 = 转分屏打开（跨项目由 drop 侧先切项目再开列）
        wireSessionDrag(li, s, ordered, classicViewGk, () => refreshSessions(), "classic");
        if (target && target.root_path && !target.is_current) {
          li.title = "属于项目「" + (target.name || "") + "」—— 点击切换过去并打开";
          li.onclick = async () => {
            // 先记住目标会话再切项目：切完的列表重画会照它恢复选中高亮
            activeSessionSid = s.id;
            try {
              await request("project.switch", { path: target.root_path });
              await applyWorkspaceData(await fetchWorkspaceData());
            } catch (e) {
              addNotice("切换到该项目失败：" + e.message);
              return;
            }
            openTabForSession(s.id, s.title);
            clearTodoPanel();
            addNotice("已打开会话 " + (s.title || s.id));
          };
        }
        ul.appendChild(li);
      });
    }
    const footer = document.getElementById("session-footer");
    if (footer) {
      footer.classList.add("hidden");
      footer.innerHTML = "";
    }
    renderRailSessions(sessions);
    return;
  }
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
        wireSessionDrag(li, s, classicList, classicGk, () => refreshSessions(), "classic");
      }
      ul.appendChild(li);
    });
  });
  renderSessionExtras({ empty_count, archived_count });
  renderRailSessions(sessions);
}

/** 经典视图特殊列表（快聊 / 远程连接）的平铺陈列：无组头——项目区对应行
    就是入口。展开/「显示更多」状态按 gk 独立记忆（键 "quick" / "remote:<id>"）。
    不接拖动排序：经典视图的 commitSessionOrder 按整张列表收集会话 id，混入
    特殊列表会把当前项目的拖动序污染（分组视图里的对应组仍可拖，那份顺序在
    这里照常生效——orderedSessionList 会读偏好）。 */
function renderPlainList(ul, list, gk, emptyHint) {
  const st = groupState(gk);
  const ordered = orderedSessionList(list, gk);
  lastGroupedListByGroup.set(gk, ordered);
  if (!ordered.length) {
    // emptyHint 为空＝这个列表不摆空态提示（如快聊：入口就在项目区行上）
    ul.innerHTML = emptyHint ? `<li class="empty-hint">${escapeHtml(emptyHint)}</li>` : "";
    return;
  }
  const visible = st.all ? ordered : ordered.slice(0, GROUP_PREVIEW);
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
        if (!(await confirmModal(`将删除 ${empty_count} 个没有任何消息的空会话，删除后无法恢复。`,
          { title: "清理空会话", okLabel: "清理", danger: true }))) return;
        const r = await request("session.cleanup_empty");
        closeTabsForSids(r.removed_ids); // 被清掉的会话若开着标签，一并收掉
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
      if (pageViewOpen()) backToChat();
      openTabForSession(s.id, s.title);
    };
    box.appendChild(b);
  });
}

// ---------- 无障碍：侧栏列表行补键盘激活 ----------
// 会话/项目列表行是 li（非 button，历史结构），键盘用户原本 Tab 不进去。
// 补 tabindex=0 让 Tab 能走到；Enter/空格触发行上既有的 onclick
// （el.click() 派发到行本身，激活逻辑只有一份，不复制不改写）。
// 焦点在行内真正的控件上时不劫持：⋯/✕/＋ 等按钮有原生键盘激活，
// 内联重命名输入框有自己的按键语义——事件 target 不是行本身就直接放过。
function wireRowKeyboard(el) {
  if (!el) return;
  el.tabIndex = 0;
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (e.target !== el) return;
    e.preventDefault(); // 空格防页面滚动，Enter 防再触发外层默认行为
    el.click();
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
    // 点的就是当前会话时不提示：反复点同一行不该反复弹「已恢复」
    if (currentSessionId !== s.id) addNotice(`已恢复会话 ${s.title || s.id}`);
    if (t && !t.running) t.needHistory = false;
  };
  wireRowKeyboard(li); // 键盘可达：Enter/空格触发上面的行 onclick（经典与分组视图共用本渲染）
  return li;
}

// ---------- 分组视图：会话按项目折叠收拢（项目一多，经典两区挤不下会话） ----------
// 与经典视图并存，sidebar_view 偏好记住选择（classic=原两区样式，保留不删）
let sidebarView = "classic";
let groupSeq = 0; // 渲染序号：两次并发的分组渲染，慢的那个回来后直接丢弃
const projGroupState = new Map(); // 分组 key -> { open, all }：折叠与「显示更多」记忆，重渲染不丢
let lastGroupedCurrentKey = null; // 上一次分组渲染时的当前项目 key：项目切换时自动展开新组（取代旧「当前置顶」）
// 「点组头亮它」：组头高亮平时跟活动会话所在组（快聊会话亮快聊组）；点了
// 某个组头后高亮先借给那个组（折叠/展开都算一次点击），切标签/点会话时交回。
// 单值状态，同一时刻最多一个组头亮
let groupedClickGk = null;
// 上次分组渲染实际点亮的高亮 key（groupedClickGk || 活动会话所在组 || 当前
// 项目）：与组头高亮同一来源——「新建会话」的落点也读它（newSessionFromHighlight）
let lastGroupedHighlightKey = null;
// 组内会话的自定义顺序（ui.json 的 session_order，偏好回包后由 initUiPrefs 填充）。
// 键 = 组 key（字符串化项目 id），值 = 会话 id 数组（只记手动拖过的，新的照时间追加）
let sessionOrderPrefs = {};
// 标签栏页签的拖动序（ui.json 的 tab_order，sid 数组，只记有 sid 的会话标签；
// 偏好回包后由 initUiPrefs 填充，renderTabs 按它排，空标签始终垫底）
let tabOrderPrefs = [];
// 快聊分组的锚点位置（ui.json 的 quick_pos，经典/分组两视图共用）：
// null=垫底（默认），"top"=最前，{before: 项目id}=插在该项目前。
// 快聊是伪组（没有项目 id）进不了 project_order，位置单独记；偏好回包后由
// initUiPrefs 填充，拖动提交时从最终序反推（见 commitProjectOrder）
let quickPosPref = null;

/** 快聊组/行的插入时机（渲染前算好）：锚点项目不在本次列表（已删除）时回落垫底 */
function quickAnchor(projects) {
  if (quickPosPref === "top") return { mode: "top" };
  if (quickPosPref && typeof quickPosPref === "object" && quickPosPref.before != null
      && projects.some((p) => String(p.id) === String(quickPosPref.before))) {
    return { mode: "before", id: quickPosPref.before };
  }
  return { mode: "bottom" };
}
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
  classicViewGk = null; // 经典视图快聊态是视图内状态，切视图后随当前项目重置
  groupedClickGk = null; // 分组视图「点组头」的高亮同样是视图内状态
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
  // 组头高亮跟随「正在看的会话」所在组：快聊/远程连接的会话激活时引擎工作项目
  // 不变（切目录才有意义），若高亮仍挂在当前项目上，「看到的组」与「高亮的组」
  // 就对不上（用户报的「已切到快聊、高亮还在 A」）。没有活动会话（欢迎页/
  // 空标签）回落引擎当前项目——欢迎页属于当前项目的工作上下文。切换/删除/
  // 跨项目会话点击仍按 isCurrent（引擎当前）判定，高亮纯视觉。
  // 活动会话不在本次列表（超 50 条被截/已归档）时同样回落。点了组头
  // （groupedClickGk，见下）时高亮先借给点的那个组，切会话/切标签再交回。
  const knownIds = new Set(projects.map((p) => String(p.id)));
  const sidToGroup = new Map();
  sessions.forEach((s) => {
    sidToGroup.set(String(s.id),
      s.project_id == null ? "quick"
        : (knownIds.has(String(s.project_id)) ? String(s.project_id) : "loose"));
  });
  // 点了某个组头就先亮它（点哪行亮哪行）；那个组已不在（项目被移除、孤儿
  // 清空）就丢弃，落回活动会话——免得整列一个都不亮
  if (groupedClickGk != null) {
    const looseNow = [...byProject.keys()].some(
      (k) => k !== "quick" && !knownIds.has(String(k)));
    const stillThere = groupedClickGk === "quick"
      || (groupedClickGk === "loose" ? looseNow : knownIds.has(groupedClickGk));
    if (!stillThere) groupedClickGk = null;
  }
  const activeSid = activeTab && activeTab.sid != null ? String(activeTab.sid)
    : (currentSessionId != null ? String(currentSessionId) : null);
  const highlightKey = groupedClickGk ||
    (activeSid && sidToGroup.get(activeSid)) ||
    (currentKey != null ? String(currentKey) : null);
  lastGroupedHighlightKey = highlightKey; // 「新建会话」的落点＝这里点亮的那一项
  // 快聊组按锚点插序（quick_pos）：top=项目组前、before=某项目组前、bottom=
  // 项目组后（默认）。「其他」兜底组仍固定最末，不参与拖动
  const anchor = quickAnchor(projects);
  const renderQuickGroup = () => {
    // 「快聊」分组常驻：不绑定文件夹的对话都在这里（有时候只是想聊一句、
    // 做点小任务，不需要工作目录）。组头 ＋ 一键新建快聊对话。
    renderProjectGroup(frag, {
      key: "quick", name: "快聊", list: byProject.get("quick") || [],
      isCurrent: false, rootPath: "", project: null,
      isActive: highlightKey === "quick",
      headPlus: async () => {
        const r = await request("session.new_task", {});
        await openTabForSession(r.id, r.title);
        refreshSessionsGrouped();
      },
    });
  };
  if (anchor.mode === "top") renderQuickGroup();
  projects.forEach((p) => {
    if (anchor.mode === "before" && String(p.id) === String(anchor.id)) renderQuickGroup();
    renderProjectGroup(frag, {
      key: p.id, name: p.name, list: byProject.get(p.id) || [],
      isCurrent: !!p.is_current, rootPath: p.root_path || "", project: p,
      isActive: String(p.id) === highlightKey,
    });
    byProject.delete(p.id);
  });
  if (anchor.mode === "bottom") renderQuickGroup();
  const loose = [];
  byProject.forEach((list, key) => {
    if (key !== "quick") loose.push(...list);
  });
  if (loose.length) {
    renderProjectGroup(frag, {
      key: "loose", name: "其他", list: loose,
      isCurrent: false, rootPath: "", project: null,
      isActive: highlightKey === "loose",
    });
  }
  ul.innerHTML = "";
  ul.appendChild(frag);
  renderSessionExtras(listRes);
  renderRailSessions(sessions);
  syncFoldAllBtn();
}

function renderProjectGroup(frag, { key, name, list, isCurrent, rootPath, project, headPlus, isActive }) {
  const ul = document.getElementById("session-list");
  const st = groupState(key);
  const head = document.createElement("li");
  // is-empty 保留：空组头样式钩子（无会话行可藏）；高亮（active）跟
  // 「正在看的会话所在组」走（点组头时先借给点的那个组），isCurrent 只管切换/删除等逻辑
  head.className = "pgroup-head" + (isActive ? " active" : "") +
    (st.open ? " open" : "") + (list.length ? "" : " is-empty");
  head.dataset.gkey = key; // 折叠全部/展开全部要靠它找回各组的状态
  head.innerHTML = FOLDER_SVG +
    `<span class="pg-name">${escapeHtml(name)}</span>` +
    // 「远程连接」也可从列表移除（渠道再用时自动重建）；「其他」兜底组
    // （project=null 且非快聊）仍不提供新建：那些会话本属某项目，
    // 只是项目被移除后成了孤儿，新建无意义
    (project
      ? `<button class="pg-del" title="${isCurrent ? "重置这个项目（清空会话与记录）" : "从列表中移除这个项目"}">✕</button>` : "") +
    // 兜底组「其他」（project=null 且非快聊）不提供新建：那些会话本属某项目，
    // 只是项目被移除后成了孤儿，新建无意义
    (headPlus || project
      ? `<button class="pg-add" title="${headPlus ? "新建快聊（不需要文件夹，随时能聊）" : "新建会话"}">＋</button>` : "");
  // 组头交互：整行点击展开/折叠；右侧＋新建会话（箭头已移除，展开态靠组下
  // 会话行本身可见）。＋行为按组分流：快聊保持原 headPlus；当前项目=新标签
  // （发送第一条消息时才落库，见 create_task_chat）；其他项目先切过去再新建；
  // 远程连接没有工作目录，打开它名下最近的对话。
  head.title = `${name} —— 点击展开/折叠该项目下的会话；点＋新建会话`;
  const addBtn = head.querySelector(".pg-add");
  if (addBtn) {
    addBtn.onclick = async (e) => {
      e.stopPropagation();
      try {
        if (headPlus) {
          // 快聊组：沿用自带的新建流程
          await headPlus();
        } else if (isCurrent) {
          // 当前项目：立即落库一个新会话（无需先切；startNewTab 自带侧栏刷新）
          startNewTab();
        } else if (rootPath) {
          // 其他项目：先切工作项目再新建（applyWorkspaceData 会重画侧栏）
          await request("project.switch", { path: rootPath });
          await applyWorkspaceData(await fetchWorkspaceData());
          startNewTab();
          addNotice(`已切换到项目「${name}」，新建会话`);
        } else {
          // 远程连接：无工作目录，打开最近的渠道对话
          await switchToGroupProject(project, name, rootPath);
        }
      } catch (e2) {
        addNotice("新建会话失败：" + e2.message);
      }
    };
  }
  // 行点击＝展开/折叠＋把高亮收过来（点哪行亮哪行；切换项目走经典视图或会话行；
  // ＋/✕ 自行 stopPropagation）
  head.onclick = () => {
    st.open = !st.open;
    groupedClickGk = String(key);
    refreshSessionsGrouped();
  };
  // 拖动与点击是两套手势：拖组头换位仍走整行 dragstart，不受行点击影响。
  // 快聊是伪组也参与排序（位置记 quick_pos 锚点）；「其他」兜底组固定最末不接
  if (project || key === "quick") wireGroupDrag(head, key);
  wireRowKeyboard(head); // 组头也是一行：键盘 Enter/空格同样展开/折叠
  const del = head.querySelector(".pg-del");
  if (del) del.onclick = (e) => { e.stopPropagation(); deleteProjectModal(project); };
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
    wireListDrag.active = { item, over: null, tag: opts.tag || null };
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

/** 分组视图组头＋（远程连接分支用）：固定项目不可切工作目录，打开它名下
    最近的会话——与经典视图点「远程连接」同一行为。 */
async function switchToGroupProject(project, name, rootPath) {
  if (!rootPath) {
    // 远程连接：无真实目录，切工作目录无从谈起，直接开最近的渠道会话
    const r = await request("session.list", { all_projects: 1 }).catch(() => null);
    const list = ((r && r.sessions) || [])
      .filter((s) => s.project_id === project.id)
      .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    if (!list.length) {
      addNotice("「远程连接」还没有会话——在飞书/微信里给机器人发条消息就会出现在这里");
      return;
    }
    await openTabForSession(list[0].id, list[0].title);
    refreshSessionsGrouped();
    return;
  }
  try {
    await switchProject(rootPath);
  } catch (e) {
    addNotice("切换失败: " + e.message);
  }
}

/** 给组头接上拖拽（分组视图）。head 的点击行为（折叠）不受影响：拖拽与点击是两套手势。 */
function wireGroupDrag(head, key) {
  wireListDrag(head, { id: String(key) }, {
    // commitProjectOrder 的签名是 (srcKey, dstKey, pos)：第一个参数必须是
    // 被拖的那个组，落点组与落点位置随后——曾经漏传 srcKey、把 pos 当成了
    // dstKey，indexOf 必然落空直接早退，分组视图拖组头从未真正保存过
    commit: (dst, pos) => commitProjectOrder(String(key), dst.id, pos),
    rerender: refreshSessionsGrouped,
  });
}

function clearDragVisual() {
  document.querySelectorAll("#session-list .pgroup-head.drop-above, #session-list .pgroup-head.drop-below, #project-list li.drop-above, #project-list li.drop-below, #chat-tabs .chat-tab.drop-left, #chat-tabs .chat-tab.drop-right")
    .forEach((el) => el.classList.remove("drop-above", "drop-below", "drop-left", "drop-right"));
}

/** 项目新序提交：listId 决定从哪个列表读当前 DOM 序（分组视图从组头、
    经典视图从项目行），两种视图保存同一份 project_order。
    快聊（quick）一起收集参与排序——它没有项目 id，落库时从最终序反拆：
    项目序照旧存 project_order，快聊的位置单独存 quick_pos 锚点（top/bottom/
    {before: 下一行的项目 id}），渲染时按锚点插回（quickAnchor）。「其他」
    （loose）兜底组不进任何偏好：拖到它上面按垫底处理。 */
function commitProjectOrder(srcKey, dstKey, pos, listId = "session-list", rowSel = ".pgroup-head", attr = "gkey") {
  const keys = [...document.querySelectorAll(`#${listId} ${rowSel}`)]
    .map((h) => String(h.dataset[attr]))
    .filter((k) => k && k !== "loose"); // loose 垫底不进序；quick 参与排序
  if (dstKey === "loose") { // 拖到「其他」上＝放到当前垫底组的后面
    dstKey = keys[keys.length - 1];
    pos = "after";
  }
  const from = keys.indexOf(srcKey);
  let to = keys.indexOf(dstKey);
  if (from < 0 || to < 0 || from === to) return Promise.resolve();
  keys.splice(from, 1);
  to = keys.indexOf(dstKey); // 抽走 src 后下标可能前移，重找
  if (pos === "after") to += 1;
  keys.splice(to, 0, srcKey);
  const qi = keys.indexOf("quick");
  const prefs = {
    project_order: keys.filter((k) => k !== "quick").map((k) => parseInt(k, 10)).filter((n) => n > 0),
  };
  if (qi === 0) prefs.quick_pos = "top";
  else if (qi === keys.length - 1) prefs.quick_pos = "bottom";
  else prefs.quick_pos = { before: parseInt(keys[qi + 1], 10) }; // quick 与下一行间无 loose，下一行必是项目
  quickPosPref = prefs.quick_pos; // 本地同步：重渲染（rerender）前锚点已生效
  return request("ui.save", { prefs }).catch(() => {});
}

/** 给会话行接上组内拖拽（分组/经典两种视图共用）。同级限制：└/⑂ 子会话跟随父
    （作为一整个家族块）参与排序；拖到子行上时落点重定向到它的父（即插到该
    家族块的前/后），不会插进父与子之间。经典视图必须传 view="classic"——
    commitSessionOrder 靠它决定沿 DOM 收集会话行的方式，漏传会按分组视图的
    .pgroup-head 去收集，经典视图里一个都找不到，拖动永远空转 */
function wireSessionDrag(li, s, list, gkey, rerender, view = "grouped") {
  wireListDrag(li, s, {
    tag: "session", // 对话区的落点认这个标记：会话行拖进对话区 = 转分屏打开
    over: (e, item, dragged) => {
      // 同级判定：拖到子行上重定向到父；同家族块不响应。
      // 注意三个来源别搞混：item＝被悬停的行（本闭包绑定的会话），
      // dragged＝wireListDrag 传进来的拖动源（st.item）——家族比较必须是
      // 「拖动源的家族 vs 悬停行的家族」。曾经误用本闭包的 srcFamily（＝
      // 悬停行自己的家族）自己比自己，永远相等，over 永远返回 null，
      // 两种视图的会话拖动从诞生起就没生效过
      const overRow = findParentInGroup(list, item);
      const overFamily = overRow ? overRow.id : item.id;
      const draggedRow = dragged ? findParentInGroup(list, dragged) : null;
      const draggedFamily = draggedRow ? draggedRow.id : (dragged ? dragged.id : null);
      if (!draggedFamily || overFamily === draggedFamily) return null;
      return { id: overFamily, pos: dragHalfPos(e, li) };
    },
    commit: (dst, pos) => commitSessionOrder(String(gkey), s.id, dst.id, pos, view),
    rerender: rerender || refreshSessionsGrouped,
  });
}

/** 快聊 / 远程连接列表（renderPlainList 渲染）的拖拽接线：按 sid 找回行元素，
    套上与会话行同一套 wireSessionDrag（列表内重排 + 拖出进对话区=分屏）。
    renderPlainList 的行结构对这里透明——没有 data-sid 的行（空态提示等）自然跳过。 */
function wirePlainListDrag(ul, ordered, gkey) {
  ordered.forEach((s) => {
    const li = ul.querySelector('li[data-sid="' + String(s.id) + '"]');
    if (li) wireSessionDrag(li, s, ordered, gkey, () => refreshSessions(), "classic");
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

// 「引用此会话」：挂进输入框的引用托盘，随下一条消息把该会话内容注入上下文。
// 主栏与分屏列共用一条通路（pane 空 = 主栏）：右键菜单与拖拽入输入栏都汇到这里
function addSessionRef(sid, title, pane) {
  const cur = pane ? pane.sid : (activeTab && activeTab.sid);
  if (cur === sid) {
    addNotice("这就是当前正在对话的会话，直接提问即可");
    return false;
  }
  const list = faceRefs(pane);
  if (list.some((r) => r.id === sid)) {
    addNotice("已引用该会话");
    return false;
  }
  list.push({ id: sid, title: title || "未命名会话" });
  renderRefTray(pane);
  if (!pane) inputEl.focus();
  addNotice(`已引用「${title || "未命名会话"}」，随下一条消息发给 Agent`);
  return true;
}

function quoteThisSession(s) {
  addSessionRef(s.id, s.title, null);
}

// 归档当前会话：侧栏列表、搜索、启动续聊三处同时隐藏，可在归档弹窗恢复
async function archiveSession(s) {
  await request("session.archive", { id: s.id, archived: true });
  addNotice(`已归档「${s.title || "(未命名)"}」，点侧栏底部「归档会话」可找回`);
  refreshSessions();
}

// —— 归档弹窗：查看已归档会话，恢复或彻底删除（删除需二次确认） ——
async function openArchiveModal() {
  const r = await request("session.list_archived");
  const list = r.sessions || [];
  // 弹窗固定长宽（CSS :has(.archive-wrap)），列表区自己滚；批量操作条常驻列表上方
  const wrap = document.createElement("div");
  wrap.className = "archive-wrap";
  const bulk = document.createElement("div");
  bulk.className = "archive-bulk";
  bulk.innerHTML =
    '<label class="archive-pick"><input type="checkbox" data-f="all">全选</label>' +
    '<span class="archive-count" data-f="count">已选 0</span>' +
    '<span class="bulk-spacer"></span>';
  const restoreSel = document.createElement("button");
  restoreSel.className = "rp-mini";
  restoreSel.textContent = "恢复所选";
  const delSel = document.createElement("button");
  delSel.className = "rp-mini danger";
  delSel.textContent = "删除所选";
  bulk.appendChild(restoreSel);
  bulk.appendChild(delSel);
  const box = document.createElement("div");
  box.className = "archive-list";
  if (!list.length) {
    box.innerHTML = '<p class="dim small">没有已归档的会话。右键会话（或点 ⋯）选「归档」，它就会从侧栏消失并收进这里。</p>';
  }
  const closeIfEmpty = () => {
    if (!box.querySelector(".archive-row")) { hideModal(); refreshSessions(); }
  };
  const rowsOf = () => [...box.querySelectorAll(".archive-row")];
  const pickedOf = () =>
    rowsOf().filter((el) => el.querySelector(".archive-pick input").checked);
  let armedBatch = false; // 批量删除的二次确认：选择一变就要求重新确认
  const syncBulk = () => {
    const total = rowsOf().length;
    const n = pickedOf().length;
    const all = bulk.querySelector('[data-f="all"]');
    bulk.querySelector('[data-f="count"]').textContent = `已选 ${n}`;
    all.checked = total > 0 && n === total;
    all.indeterminate = n > 0 && n < total;
    armedBatch = false;
    delSel.textContent = n ? `删除所选（${n}）` : "删除所选";
    restoreSel.textContent = n ? `恢复所选（${n}）` : "恢复所选";
  };
  bulk.querySelector('[data-f="all"]').onchange = (e) => {
    rowsOf().forEach((el) => { el.querySelector(".archive-pick input").checked = e.target.checked; });
    syncBulk();
  };
  restoreSel.onclick = async () => {
    const rows = pickedOf();
    if (!rows.length) { addNotice("先勾选要恢复的会话"); return; }
    let ok = 0;
    for (const el of rows) {
      try {
        await request("session.archive", { id: el.dataset.sid, archived: false });
        ok += 1;
        el.remove();
      } catch (e) { addNotice("恢复失败: " + e.message); }
    }
    if (ok) addNotice(`已恢复 ${ok} 个会话`);
    syncBulk();
    closeIfEmpty();
  };
  delSel.onclick = async () => {
    const rows = pickedOf();
    if (!rows.length) { addNotice("先勾选要删除的会话"); return; }
    if (!armedBatch) { // 与单行删除同一套「确认删除」两步手感
      armedBatch = true;
      delSel.textContent = `确认删除 ${rows.length} 个？`;
      return;
    }
    armedBatch = false;
    let ok = 0;
    const deadIds = [];
    for (const el of rows) {
      try {
        await request("session.delete", { id: el.dataset.sid });
        ok += 1;
        deadIds.push(el.dataset.sid);
        el.remove();
      } catch (e) { addNotice("删除失败: " + e.message); }
    }
    closeTabsForSids(deadIds); // 删掉的会话若开着标签，一并收掉
    if (ok) addNotice(`已删除 ${ok} 个会话`);
    syncBulk();
    closeIfEmpty();
  };
  list.forEach((s) => {
    const row = document.createElement("div");
    row.className = "archive-row";
    row.dataset.sid = s.id;
    row.innerHTML =
      '<label class="archive-pick"><input type="checkbox"></label>' +
      `<div class="archive-main"><b>${escapeHtml(s.title || "(未命名)")}</b>` +
      `<span class="archive-time">最近活跃：${fmtRuleAge(s.updated_at)}</span></div>`;
    row.querySelector("input").onchange = syncBulk;
    const ops = document.createElement("div");
    ops.className = "archive-ops";
    const restore = document.createElement("button");
    restore.className = "rp-mini";
    restore.textContent = "恢复";
    restore.onclick = async () => {
      await request("session.archive", { id: s.id, archived: false });
      addNotice(`已恢复「${s.title || "(未命名)"}」`);
      row.remove();
      syncBulk();
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
      closeTabsForSids([s.id]); // 删掉的会话若开着标签，一并收掉
      addNotice(`已删除「${s.title || "(未命名)"}」`);
      row.remove();
      syncBulk();
      closeIfEmpty();
    };
    ops.appendChild(restore);
    ops.appendChild(del);
    row.appendChild(ops);
    box.appendChild(row);
  });
  if (list.length) wrap.appendChild(bulk); // 无条目时不摆批量操作条
  wrap.appendChild(box);
  syncBulk();
  showModal("归档会话", wrap, async () => {}, "关闭");
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
    li.title = s.seq != null
      ? "点击打开这个会话并跳到命中消息"
      : "点击打开这个会话";
    // 命中消息的 seq（旧后端不返回时为 undefined）：只在「这条结果本身是消息命中」
    // 时启用跳转。仅标题命中（无 seq）保持原行为只打开会话。
    const hitSeq = s.seq != null ? Number(s.seq) : null;
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
      const t = openTabForSession(s.session_id, s.title);
      clearTodoPanel();
      addNotice(`已打开会话 ${s.title || s.session_id}`);
      refreshSessions();
      // 命中消息跳转：等历史加载完再定位。openTabForSession 的 loadTabHistory
      // 是异步的（标签已激活但消息可能还没画完），所以轮询等待目标 seq 出现，
      // 超时静默放弃（跳转失败不该挡着会话打开）。
      if (hitSeq != null) jumpToMessageSeq(t, hitSeq, { query });
    };
    wireRowKeyboard(li); // 搜索结果行同样是 li：键盘也能打开命中的会话
    ul.appendChild(li);
  });
}

/** 搜索结果 → 跳到命中消息：滚动到目标 seq 的消息并闪烁高亮。
 *
 * 历史窗口化（后端只下发尾部窗口，更早的经 messages.earlier 翻页）后，命中
 * 可能落在未加载的更早区间：先看当前已画节点，没有就按批「加载更早的消息」向前补画，
 * 直到目标出现或到底（补画上线防止极端会话里长循环）。消息画出来后
 * 再滚进视口居中并加 .msg-hit-flash 闪一下，用户立刻看得到落点。
 *
 * 轮询等待历史：openTabForSession 内部的历史拉取是异步的，调用时节点
 * 可能还没画完；jumpSeq.wait 以 60ms 间隔重试，至多 MJUMP_WAIT ms。
 * 同一标签并发跳转时后一次覆盖前一次（_jumpSeqGen 代际作废旧轮询）。
 */
const MESSAGE_JUMP_WAIT_MS = 4000;
const MESSAGE_JUMP_FLASH_MS = 1800;
const MESSAGE_JUMP_MAX_PRELOAD_ROUNDS = 60; // 每轮 HISTORY_CHUNK 条，上限 6000 条

function jumpToMessageSeq(tab, seq, opts = {}) {
  if (!tab || seq == null) return;
  const gen = (tab._jumpSeqGen = (tab._jumpSeqGen || 0) + 1);
  const t0 = Date.now();
  tab._jumpPreloadRounds = 0; // 本次跳转的补画轮数配额
  const attempt = () => {
    if (!tab._jumpSeqGen || tab._jumpSeqGen !== gen) return; // 已被更新跳转取代
    if (!tab.logEl) return;
    const el = tab.logEl.querySelector(`.msg[data-seq="${seq}"]`);
    if (el) { paintJumpTarget(tab, el, opts); return; }
    // 没画到：可能 (a) 历史还在异步拉取，(b) 命中在窗口外（更早区间）。
    // (b) 有「加载更早」按钮时点它经 messages.earlier 补一批（在途翻页复用
    // 同一 Promise，不会重复发请求）；(a) 没有按钮就再等等。
    // 轮数配额防极端会话里无限补画（每轮 HISTORY_CHUNK 条）。
    const more = tab.logEl.querySelector(".history-more");
    if (more && tab._historyStart > 0) {
      if (tab._historyLoading) { setTimeout(attempt, 60); return; } // 翻页在途：等下一拍
      tab._jumpPreloadRounds += 1;
      if (tab._jumpPreloadRounds <= MESSAGE_JUMP_MAX_PRELOAD_ROUNDS) {
        loadEarlierHistory(tab).then(() => setTimeout(attempt, 0)); // 画完下一拍再看
        return;
      }
    }
    if (Date.now() - t0 < MESSAGE_JUMP_WAIT_MS) {
      setTimeout(attempt, 60);
    }
  };
  setTimeout(attempt, 30);
}

function paintJumpTarget(tab, el, opts) {
  if (!el || !el.isConnected) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("msg-hit-flash");
  void el.offsetWidth; // 强制回流以重启动画
  el.classList.add("msg-hit-flash");
  setTimeout(() => el.classList.remove("msg-hit-flash"), MESSAGE_JUMP_FLASH_MS);
  // 命中词在消息里就顺手帮用户标上（对话内查找的同一效果，不占查找条）
  const q = String((opts || {}).query || "").trim();
  if (q.length >= 1 && q.length <= 80) showSearchHitInMessage(el, q);
}

/** 在单条消息里把搜索词包成临时高亮（仅视觉，不作 DOM 结构改动以外的事情）。
 * 与对话内查找（Ctrl+F）不同：只处理一条消息、 5s 后自动清除；不碰 pre/code。*/
function showSearchHitInMessage(msg, query) {
  const q = query.toLowerCase();
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
  const marks = [];
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
      mark.className = "search-hit-mark";
      mark.textContent = text.slice(p, p + query.length);
      marks.push(mark);
      frag.appendChild(mark);
      i = p + query.length;
    }
    node.parentNode.replaceChild(frag, node);
  });
  if (!marks.length) return;
  setTimeout(() => {
    marks.forEach((m) => {
      const parent = m.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      parent.normalize();
    });
  }, MESSAGE_JUMP_FLASH_MS + 2200);
}

// ---------- 会话操作菜单（⋯ 按钮或右键唤出） ----------
const menuEl = document.getElementById("session-menu");

/** 弹层落点：默认在指针下方展开；下方放不下（会被视口底缘裁掉）就整体翻到
    指针上方；上下都放不下（指针贴边或菜单比视口还高）才贴向较近的一缘，
    并限高内部滚动兕底。此前两处调用各自写死估高（-240/-140）往下钳：估高
    远小于会话菜单实高（十几项），侧栏底部右键仍然裁掉后半截——改为展开后
    量实际尺寸再定位。pos 与 window 尺寸是物理像素，菜单自带 zoom 定位在
    缩放布局坐标系，结论算完再除回 uiScale 落 style。 */
function placeMenuAt(menu, pos) {
  menu.style.bottom = "auto";
  menu.style.right = "auto";
  menu.style.maxHeight = ""; // 上一次「比视口还高」留下的限高要先撤掉再量
  menu.style.overflowY = "";
  const m = 8; // 视口边缘安全距离（物理像素）
  // 量布局尺寸而非 getBoundingClientRect：浮层有 ui-pop-in 入场动画（scale/translate），
  // 开菜单第一帧的 rect 是动画中间态，向上翻时会差出几像素；offset* 是不受
  // transform 影响的布局盒（已含 zoom），× uiScale 还原成物理像素
  const hPhys = menu.offsetHeight * uiScale, wPhys = menu.offsetWidth * uiScale;
  let topPhys;
  if (pos.y + hPhys <= window.innerHeight - m) {
    topPhys = pos.y; // 下方放得下：向下展开
  } else if (pos.y - hPhys >= m) {
    topPhys = pos.y - hPhys; // 上方放得下：向上翻
  } else {
    // 上下都放不下：限高内部滚动，菜单贴向指针较近的一缘
    const avail = window.innerHeight - 2 * m;
    menu.style.maxHeight = avail / uiScale + "px";
    menu.style.overflowY = "auto";
    topPhys = pos.y > window.innerHeight / 2 ? window.innerHeight - m - avail : m;
  }
  const xPhys = Math.max(m, Math.min(pos.x, window.innerWidth - m - wPhys));
  menu.style.top = topPhys / uiScale + "px";
  menu.style.left = xPhys / uiScale + "px";
}

// 线条风小图标（stroke 继承文字色，与界面线稿风格一致）
function svgIcon(name) {
  const P = {
    quote: '<path d="M13.9 3.2H2.1a.9.9 0 0 0-.9.9v6.3a.9.9 0 0 0 .9.9h2.6v2.6l3.3-2.6h5.9a.9.9 0 0 0 .9-.9V4.1a.9.9 0 0 0-.9-.9z"/>',
    pin: '<path d="M5.5 2.5h5v3l1.5 2.5H4l1.5-2.5v-3z"/><path d="M8 9v4.5"/>',
    move: '<path d="M2.5 5H12m0 0L9.5 2.5M12 5 9.5 7.5"/><path d="M13.5 11H4m0 0 2.5-2.5M4 11l2.5 2.5"/>',
    rename: '<path d="M11.2 2.4l2.4 2.4L6 12.4l-3.2.8.8-3.2 7.6-7.6z"/>',
    trash: '<path d="M2.5 4h11M6.5 4V2.8h3V4M4.2 4l.6 9h6.4l.6-9"/><path d="M6.6 6.5v4.5M9.4 6.5v4.5"/>',
    archive: '<path d="M2.5 2.8h11v2.4h-11z"/><path d="M3.6 5.2v7.1a.8.8 0 0 0 .8.8h7.2a.8.8 0 0 0 .8-.8V5.2"/><path d="M6.3 8.1h3.4"/>',
    tag: '<path d="M2.5 3.5h4.4l6.6 6.6-4.4 4.4-6.6-6.6z"/><circle cx="5.8" cy="5.8" r="1"/>',
    folder: '<path d="M1.8 4.2a.9.9 0 0 1 .9-.9h3.4l1.5 1.7h5.7a.9.9 0 0 1 .9.9v6a.9.9 0 0 1-.9.9H2.7a.9.9 0 0 1-.9-.9z"/>',
    copy: '<rect x="5.4" y="5.4" width="8" height="8" rx="1"/><path d="M10.6 5.4V3.6a1 1 0 0 0-1-1H3.6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h1.8"/>',
    split: '<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1"/><path d="M8 2.8v10.4"/>',
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
    { icon: "tag", label: "编辑标签", act: () => editSessionTags(s) },
    { icon: "move", label: "迁移到其他项目", act: () => moveSessionModal(s) },
    {
      icon: "archive",
      label: s.archived ? "取消归档" : "归档会话",
      act: async () => {
        await request("session.archive", { id: s.id, archived: !s.archived });
        addNotice(s.archived
          ? `已恢复「${s.title || "(未命名)"}」`
          : `已归档「${s.title || "(未命名)"}」，点侧栏底部「归档会话」可找回`);
        refreshSessions();
      },
    },
    { icon: "rename", label: "重命名", act: () => startInlineRename(s, li) },
    {
      icon: "split",
      label: "在分屏打开",
      act: () => openSplitPane(s.id, s.title),
    },
    // 定位/复制类：快聊（fast-talk 专用文件夹）/远程连接（remote-control）/项目会话
    // 都有真实工作目录，这两项对所有会话开放
    ...([
      {
        icon: "folder",
        label: "在资源管理器中打开",
        act: async () => {
          try { await request("session.reveal", { id: s.id }); }
          catch (e) { addNotice(e.message); }
        },
      },
      {
        icon: "copy",
        label: "复制项目路径",
        act: async () => {
          try {
            const r = await request("session.project_path", { id: s.id });
            if (!r.path) { addNotice("这个会话没有工作目录"); return; }
            await copyTextToClipboard(r.path);
            addNotice("已复制工作目录路径");
          } catch (e) { addNotice(e.message); }
        },
      },
    ]),
    {
      icon: "copy",
      label: "复制会话 ID",
      act: async () => {
        await copyTextToClipboard(s.id);
        addNotice("已复制会话 ID");
      },
    },
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
  placeMenuAt(menuEl, pos);
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
  // 与 showSessionMenu 同一套「量实际尺寸、可向上翻」的落点
  placeMenuAt(menuEl, pos);
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
  input.maxLength = 80;
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
  input.maxLength = 80;
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
  // 快聊也是合法目标（后端 project_id=None）：不属于任何项目的会话已在快聊，不重复给入口
  const quickOpt = s.project_id == null ? "" :
    '<label class="move-opt"><input type="radio" name="move-target" value="quick"> <b>快聊</b> <span class="dim small">不绑定文件夹的对话区</span></label>';
  box.innerHTML = `
    <p class="dim small">把会话移动到其他项目或快聊（移走后可在对应列表中找到）：</p>
    <div class="move-list">${quickOpt}${options || '<p class="dim small">暂无其他项目——先在其他目录启动一次 SkySheep 即可创建。</p>'}</div>`;
  showModal("移动会话", box, async () => {
    const checked = box.querySelector("input[name=move-target]:checked");
    if (!checked) throw new Error("请先选择目标项目");
    const r = await request("session.move", {
      id: s.id,
      project_id: checked.value === "quick" ? null : Number(checked.value),
    });
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
    closeSplitPanesForSids(new Set([s.id])); // 开着分屏的也一并收掉
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
  const chip = document.getElementById("model-chip");
  (chip.querySelector(".mc-txt") || chip).textContent = text;
}

// ---------- 思考强度：顶栏控件（自动 / 低 / 中 / 高）----------
// 档位按会话生效（session.reasoning_get/set，本机专属）；空标签（无会话）
// 回全局口径（model.reasoning / model.set_reasoning）。
let reasoningState = { supported: false, effort: "auto", labels: {}, efforts: [] };
const reasoningChipEl = document.getElementById("reasoning-chip");

/** 把思考强度状态画到目标徽章上（主栏 chip 与分屏列徽章共用一套视觉）；
    不支持思考的服务隐藏徽章。目标是主栏 chip 时顺带更新全局缓存。 */
function renderReasoningChip(targetEl, state) {
  const st = state || { supported: false, effort: "auto", labels: {}, efforts: [] };
  if (targetEl === reasoningChipEl) reasoningState = st;
  const supported = !!st.supported;
  targetEl.classList.toggle("hidden", !supported);
  if (!supported) return;
  const label = (st.labels || {})[st.effort] || st.effort;
  // 徽章只显示档位（不再拼「思考」前缀）；完整语义走 title 悬停提示。
  // 档位写进 .chip-txt：挤压档整个徽章翻成纯图标（图标是常驻 DOM，不能被清掉）
  (targetEl.querySelector(".chip-txt") || targetEl).textContent = label;
  targetEl.title = st.effort === "auto"
    ? "当前思考强度：自动 · 每轮按任务复杂度实时调整（简单更快更省、复杂加深）· 点击调整"
    : `当前思考强度：${label} · 点击调整（自动 / 低 / 中 / 高）`;
  targetEl.classList.toggle("weak", st.effort === "auto");
}

async function refreshReasoning() {
  // 活动标签有会话：按会话生效档位刷（三段解析在后端）；空标签：全局口径。
  // 竞态防护：闭包捕获 tab，落地前已切走就丢弃（照抄 loadTabHistory 的先消费模式）
  const tab = activeTab;
  if (tab && tab.sid) {
    const r = await request("session.reasoning_get", { id: tab.sid }).catch(() => null);
    if (r) {
      if (activeTab !== tab || (r.session_id && r.session_id !== tab.sid)) return;
      renderReasoningChip(reasoningChipEl, {
        supported: r.supported, effort: r.effort, labels: r.labels, efforts: r.efforts,
      });
      return;
    }
    // 读不到就回退全局口径（远程端 session.reasoning_get 是本机专属——
    // 不回退的话远程端徽章永远隐藏）；本地读不到也说明该会话没有会话级口径
  }
  const r = await request("model.reasoning").catch(() => null);
  if (r && activeTab === tab) renderReasoningChip(reasoningChipEl, r.reasoning);
}

const reasoningMenu = document.getElementById("reasoning-menu");
let reasoningAnchorEl = null; // 菜单当前锚点（主栏 chip 或分屏列徽章）
let reasoningSid = "";        // 菜单作用的会话 sid（空 = 全局口径）

function hideReasoningMenu() {
  reasoningMenu.classList.add("hidden");
  reasoningAnchorEl = null;
  reasoningSid = "";
}

/** 打开思考强度菜单。anchor：定位与外点关闭豁免的锚点元素；sid：作用会话
    （主栏传 activeTab.sid，分屏列传 pane.sid；空 = 全局口径）。 */
async function toggleReasoningMenu(e, anchor, sid) {
  if (e) e.stopPropagation();
  if (!reasoningMenu.classList.contains("hidden")) return hideReasoningMenu();
  reasoningAnchorEl = anchor || reasoningChipEl;
  reasoningSid = sid || "";
  const rect = reasoningAnchorEl.getBoundingClientRect();
  reasoningMenu.innerHTML = '<div class="rt-menu-tip">加载中…</div>';
  reasoningMenu.classList.remove("hidden");
  // 徽章住在屏幕底部的输入区工具条里：向上弹（uiScale 换算与模型菜单同套）
  reasoningMenu.style.top = "auto";
  reasoningMenu.style.bottom = (window.innerHeight / uiScale - rect.top / uiScale + 6) + "px";
  reasoningMenu.style.left = Math.max(8, rect.left / uiScale) + "px";
  // 会话级：开菜单先拉该会话生效档位（不依赖主栏缓存）；全局：用缓存
  let st = reasoningState;
  if (reasoningSid) {
    const r = await request("session.reasoning_get", { id: reasoningSid }).catch(() => null);
    if (!r) {
      // 读不到（远程端该方法本机专属等）：给可读提示并自动收起，不把菜单
      // 永远停在「加载中…」
      reasoningMenu.innerHTML =
        '<div class="rt-menu-tip">无法读取该会话的思考档位（按会话调整仅限本机界面）</div>';
      setTimeout(() => {
        // 只收起还停在这条提示上的菜单：期间用户重开菜单不误伤
        if (!reasoningMenu.classList.contains("hidden") &&
            reasoningMenu.firstElementChild &&
            reasoningMenu.firstElementChild.classList.contains("rt-menu-tip")) {
          hideReasoningMenu();
        }
      }, 2600);
      return;
    }
    if (reasoningMenu.classList.contains("hidden")) return;
    st = { supported: r.supported, effort: r.effort, labels: r.labels, efforts: r.efforts };
  }
  reasoningMenu.innerHTML = "";
  if (!st.supported) {
    const tip = document.createElement("div");
    tip.className = "rt-menu-tip";
    tip.textContent = "当前模型不支持调整思考强度";
    reasoningMenu.appendChild(tip);
    return;
  }
  const efforts = st.efforts && st.efforts.length ? st.efforts : ["auto", "low", "medium", "high"];
  const desc = {
    auto: "按任务复杂度实时调整",
    low: "更快、更省，适合简单问答与格式整理",
    medium: "兼顾速度与深度，适合多数开发任务",
    high: "深入推敲，适合复杂调试与方案设计",
  };
  efforts.forEach((eff) => {
    const b = document.createElement("button");
    b.className = "rp-mini" + (eff === st.effort ? " primary" : "");
    b.style.display = "block";
    b.style.width = "100%";
    b.textContent = `${(st.labels || {})[eff] || eff} · ${desc[eff] || ""}`;
    b.onclick = async () => {
      const anchorEl = reasoningAnchorEl;
      const targetSid = reasoningSid;
      // 落地竞态防护（照模型菜单先例）：开菜单后切过标签/关过列就作废——
      // 旧 sid 会把档位设到另一个会话上，还会把旧会话的档位画进当前会话的
      // 徽章。Ctrl+Tab 等键盘切标签不产生外点点击，菜单会带着旧 sid 存活，
      // 这里在落地一刻校验锚点与会话仍然匹配。主栏 chip 要求活动会话仍是
      // 发起时的那个；列徽章要求那一列还开着且锚点仍在列上。
      const anchorValid = !!anchorEl && anchorEl.isConnected && (!targetSid ||
        splitPanes.some((p) => p.sid === targetSid && p.el.contains(anchorEl)) ||
        (activeTab && activeTab.sid === targetSid && anchorEl === reasoningChipEl));
      hideReasoningMenu();
      if (!anchorValid) {
        addNotice("会话已切换，请重新打开思考强度菜单再选");
        return;
      }
      try {
        if (targetSid) {
          const res = await request("session.reasoning_set", { id: targetSid, effort: eff });
          // 落地校验：只刷发起菜单的那个徽章（主栏 chip 顺带更新缓存）
          renderReasoningChip(anchorEl, {
            supported: res.supported, effort: res.effort, labels: res.labels, efforts: res.efforts,
          });
          const label = (res.labels || {})[eff] || eff;
          addNotice(`思考强度已设为「${label}」，本会话下一轮对话生效`);
        } else {
          const res = await request("model.set_reasoning", { effort: eff });
          renderReasoningChip(anchorEl, res.reasoning);
          const label = (res.reasoning.labels || {})[eff] || eff;
          addNotice(`思考强度已设为「${label}」，下一轮对话生效`);
        }
      } catch (err) {
        addNotice("设置失败: " + err.message);
      }
    };
    reasoningMenu.appendChild(b);
  });
}

reasoningChipEl.onclick = (e) =>
  toggleReasoningMenu(e, reasoningChipEl, activeTab && activeTab.sid);
document.addEventListener("click", (e) => {
  // 外点关闭只豁免当前锚点（主栏 chip 或分屏列徽章，随菜单存引用比对）
  if (!reasoningMenu.classList.contains("hidden") &&
      !reasoningMenu.contains(e.target) &&
      !(reasoningAnchorEl && reasoningAnchorEl.contains(e.target))) hideReasoningMenu();
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
let modelPopup = null; // 当前展开的模型弹层（同一时间只有一个，随菜单重画一同销毁）
let providerOrder = []; // 服务列表的拖动序（ui.json 的 provider_order；缺省按后端默认序）
let defaultModelPref = { provider: "", model: "", label: "" }; // 新会话默认模型（Shift+点击行设置）

function hideModelMenu() {
  modelMenu.classList.add("hidden");
  modelMenu.innerHTML = "";
  modelPopup = null; // 弹层随 innerHTML 一起没了，引用一并清掉
}

function buildModelRows(detail, cur) {
  // 已启用的服务 × 各自已启用的模型 = 可切换清单（disabled 名单里的不会出现在 detail.providers）
  // cur：会话当前生效模型 {name, model}（主栏按会话标 ✓）；不传按全局 config 标
  const rows = [];
  for (const [name, p] of Object.entries(detail.providers)) {
    for (const m of p.models || []) {
      rows.push({
        name, model: m, hasKey: p.has_key,
        label: p.label || name, // 界面显示名（预设如「智谱」「小米 Mimo」）
        vision: !!p.supports_vision, // 能否看图（右列「视觉」标签）
        active: cur
          ? name === cur.name && m === (cur.model || p.active_model || p.model)
          : p.is_active && m === (p.active_model || p.model),
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
  modelMenu.style.left = Math.max(8, chipR.right / uiScale - 260) + "px";
  const detail = await request("config.providers").catch(() => null);
  if (!detail || modelMenu.classList.contains("hidden")) return; // 请求失败或期间已被关掉
  // 新会话默认模型（★ 行标记用）：拉取失败不阻塞菜单
  defaultModelPref = await request("default_model.get").catch(() => defaultModelPref) || defaultModelPref;
  // 会话生效模型：主栏开着会话时按会话标 ✓（三段解析在后端，含默认模型会话）；
  // 空标签按全局 config 标。开菜单到落地都认 tabAtOpen，切换标签后菜单作废
  const tabAtOpen = activeTab;
  let sessCur = null;
  if (tabAtOpen && tabAtOpen.sid) {
    const cg = await request("session.model_get", { id: tabAtOpen.sid }).catch(() => null);
    if (cg && activeTab === tabAtOpen && cg.session_id === tabAtOpen.sid) sessCur = cg;
  }
  const rows = buildModelRows(detail, sessCur);
  // 两列菜单：左列服务、右列该服务的模型（不再分「默认/自定义」页签）。
  // 只列已配置 API Key 的服务——没配 Key 的切过去必然失败，不如不进清单
  const svcGroups = new Map();
  rows.forEach((r) => {
    if (!r.hasKey) return;
    let g = svcGroups.get(r.name);
    if (!g) {
      g = { name: r.name, label: r.label, models: [] };
      svcGroups.set(r.name, g);
    }
    g.models.push(r);
  });
  // 服务列按用户的拖动序排；名单外的新服务排后面（保持后端默认序，稳定排序）
  const rank = new Map(providerOrder.map((n, i) => [n, i]));
  const svcList = [...svcGroups.values()].sort((a, b) => {
    const ra = rank.has(a.name) ? rank.get(a.name) : Infinity;
    const rb = rank.has(b.name) ? rank.get(b.name) : Infinity;
    return ra === rb ? 0 : ra - rb;
  });
  const addRow = (row) => {
    const b = document.createElement("button");
    b.className = "mm-item" + (row.active ? " active" : "");
    b.innerHTML =
      `<span class="mm-title">` +
      `<span class="mm-model">${row.active ? "✓ " : ""}${escapeHtml(row.model)}</span>` +
      (row.vision ? '<span class="mm-vision">视觉</span>' : "") +
      `</span>` +
      (defaultModelPref.provider === row.name && defaultModelPref.model === row.model
        ? `<span class="mm-def" title="新会话将默认使用这个模型">★ 新会话默认</span>` : "");
    b.title = `切换到 ${row.label} / ${row.model}（点击${sessCur ? "，只影响当前会话" : ""}）；Shift+点击 设为新会话默认`;
    b.onclick = async (ev) => {
      // Shift+点击：把这一行设为「新会话默认模型」（不改当前对话）
      if (ev.shiftKey) {
        try {
          const r = await request("default_model.set", { name: row.name, model: row.model });
          defaultModelPref = r;
          addNotice(`✓ 已设为新会话默认：${r.label}（当前对话不变）`);
          // 重建菜单但保持这个服务的弹层开着：★ 标记会立刻出现在刚点的那行上，
          // 用户不用为了看一眼效果再点一次服务
          paint(row.name);
        } catch (err) {
          addNotice("设置失败: " + err.message);
        }
        return;
      }
      hideModelMenu();
      // 落地竞态防护：开菜单后切过标签就作废，模型换到错会话比不切更糟
      if (activeTab !== tabAtOpen) {
        addNotice("标签已切换，请重新打开模型菜单再选");
        return;
      }
      try {
        // 主栏有会话：切换记在该会话上（session.model_switch，本机专属），
        // 其余会话与全局不动；空标签：全局切换
        const sid = tabAtOpen ? tabAtOpen.sid : "";
        const r = sid
          ? await request("session.model_switch", { id: sid, name: row.name, model: row.model })
          : await request("model.switch", { name: row.name, model: row.model });
        curProviderName = r.provider;
        curModelName = r.model;
        if (r.supports_vision !== undefined) curSupportsVision = r.supports_vision !== false;
        if (!sid) {
          // 全局切换：空标签徽章的全局镜像一并刷新（会话级切换不动全局）
          globalProviderName = r.provider;
          globalModelName = r.model;
          globalSupportsVision = curSupportsVision;
        }
        if (sid) saveComposerState(sid); // 会话生效模型写回状态条目
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
    return b;
  };
  // 模型弹层：点服务行后在它右侧浮出该服务的模型列表。
  // 同一时间只开一个；开关状态与选中态都记在弹层自己身上，服务行不再显示选中
  const closePopup = () => {
    if (!modelPopup) return;
    modelPopup.el.remove();
    modelPopup.btn.classList.remove("active");
    modelPopup.btn.querySelector(".mm-svc-go").textContent = "›";
    modelPopup = null;
  };
  const openPopup = (btn, group) => {
    // 点同一个服务 = 收起（toggle）；点另一个服务 = 换内容并挪到新行旁
    if (modelPopup && modelPopup.btn === btn) return closePopup();
    closePopup();
    const el = document.createElement("div");
    el.className = "mm-models";
    group.models.forEach((row) => el.appendChild(addRow(row)));
    modelMenu.appendChild(el);
    btn.classList.add("active");
    btn.querySelector(".mm-svc-go").textContent = "⌄"; // 展开态箭头朝下
    // 贴着服务行右侧浮出；右侧放不下就翻到左边，两边都窄时收窄宽度留在右侧。
    // 注意 getBoundingClientRect 给的是物理像素（菜单带 zoom），而 style.left/bottom
    // 用的是布局像素——所有几何量都必须先除回 uiScale，否则会按缩放比错位重叠
    const menuR = modelMenu.getBoundingClientRect();
    const rowR = btn.getBoundingClientRect();
    const vw = window.innerWidth / uiScale;
    const mRight = menuR.right / uiScale, mLeft = menuR.left / uiScale;
    // 弹层底边与行底对齐；模型很多时按「行底到屏幕顶」收高度，绝不顶出视口
    el.style.bottom = (menuR.bottom - rowR.bottom) / uiScale + 4 + "px";
    el.style.maxHeight = Math.max(120, rowR.bottom / uiScale - 12) + "px";
    const w = el.offsetWidth;      // 布局像素（el 在 zoom 容器内，offsetWidth 已是布局值）
    const menuW = menuR.width / uiScale; // 物理 → 布局，绝对定位的 left 基准在 padding box
    if (mRight + 4 + w + 8 <= vw) {
      el.style.left = (menuW + 4) + "px"; // 右侧够宽
    } else if (mLeft - 4 - w >= 8) {
      el.style.left = (-w - 4) + "px"; // 右侧不够，翻到左边
    } else {
      // 两边都放不下：留在右侧并收窄（菜单本身已贴到窗口边时才会走到）
      el.style.left = (menuW + 4) + "px";
      el.style.maxWidth = Math.max(160, vw - mRight - 16) + "px";
    }
    // 模型多时让当前 ✓ 那行进入弹层可视区：只调弹层自己的 scrollTop，
    // 不用 scrollIntoView——后者会把外层滚动容器（乃至页面）一起带着滚
    const curRow = el.querySelector(".mm-item.active");
    if (curRow) {
      const rr = curRow.getBoundingClientRect();
      const er = el.getBoundingClientRect();
      if (rr.top < er.top || rr.bottom > er.bottom) {
        el.scrollTop += (rr.top - er.top) / uiScale;
      }
    }
    modelPopup = { el, btn };
  };
  // 整页重画（服务列 + 管理入口），数据已在手，切服务时无需重新请求。
  // openSvc：重建后要自动展开弹层的服务——
  //   "current"（默认 true 的语义）= 当前生效模型所在的服务（初次打开菜单时用）；
  //   服务名字符串 = 指定服务（Shift+点击设默认后保持弹层不跳）；
  //   "" 或 null = 不展开。
  const paint = (openSvc = null) => {
    modelMenu.innerHTML = "";
    modelPopup = null; // innerHTML 清空时弹层一并没了
    let curBtn = null, curGroup = null, svcBox = null; // 当前生效模型所在的服务行
    const svcBtns = new Map(); // 服务名 → 行按钮（重建后按名找回要展开的那行）
    if (!svcList.length) {
      // 没有已配置 Key 的服务：只留一句空态与下方管理入口
      const hint = document.createElement("div");
      hint.className = "mm-empty";
      hint.textContent = "还没有配置好 API Key 的服务，点下方「管理模型服务」配置";
      modelMenu.appendChild(hint);
    } else {
      svcBox = document.createElement("div");
      svcBox.className = "mm-services";
      let dragFrom = -1; // 拖动中的源行序号
      let justDragged = false; // 拖完落位后浏览器仍可能补一个 click，别让它顺手开弹层
      for (const [i, g] of svcList.entries()) {
        const b = document.createElement("button");
        // 正在使用的服务：行首竖条持久标识（弹层关掉也能一眼看出当前用的是谁）
        const isCur = g.models.some((r) => r.active);
        b.className = "mm-svc" + (isCur ? " current" : "");
        if (isCur) { curBtn = b; curGroup = g; }
        svcBtns.set(g.name, b);
        b.draggable = true;
        b.innerHTML = providerAvatar(g.name) +
          `<span class="mm-svc-name">${escapeHtml(g.label)}</span>` +
          '<span class="mm-svc-go">›</span>';
        b.title = `${g.label}（${g.models.length} 个模型）· 点击选择模型 · 可拖动调整顺序`;
        b.onclick = () => {
          if (justDragged) { justDragged = false; return; }
          openPopup(b, g);
        };
        b.ondragstart = (ev) => {
          dragFrom = i;
          justDragged = true;
          closePopup(); // 行要挪位置，弹层的锚点已失效，先收掉
          b.classList.add("dragging");
          ev.dataTransfer.effectAllowed = "move";
          try { ev.dataTransfer.setData("text/plain", g.name); } catch {}
        };
        b.ondragend = () => {
          b.classList.remove("dragging");
          svcBox.querySelectorAll(".mm-svc").forEach((x) => x.classList.remove("drag-over"));
          dragFrom = -1;
          // 复位放到下一个宏任务：拖动结束后浏览器可能补发一个 click（同步派发），
          // 那时标记必须还在才能把它吞掉；放 setTimeout(0) 里正好晚于那次 click。
          // 落位成功的路径已由 paint() 重建 DOM，旧闭包变量随之作废
          setTimeout(() => { justDragged = false; }, 0);
        };
        b.ondragover = (ev) => {
          if (dragFrom < 0 || dragFrom === i) return;
          ev.preventDefault();
          ev.dataTransfer.dropEffect = "move";
          b.classList.add("drag-over");
        };
        b.ondragleave = () => b.classList.remove("drag-over");
        b.ondrop = (ev) => {
          ev.preventDefault();
          b.classList.remove("drag-over");
          if (dragFrom < 0 || dragFrom === i) return;
          const moved = svcList.splice(dragFrom, 1)[0];
          svcList.splice(i, 0, moved);
          // 落盘拖动序：当前完整顺序（含未在名单里的新服务，下次照样按此排）
          providerOrder = svcList.map((x) => x.name);
          saveUiPrefs({ provider_order: providerOrder });
          dragFrom = -1;
          closePopup(); // 行顺序变了，弹层锚点已失效，直接收起
          paint();
        };
        svcBox.appendChild(b);
      }
      modelMenu.appendChild(svcBox);
    }
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
    // 服务多时把当前服务行滚进可视区（同上，只调服务列自己的 scrollTop）
    if (curBtn && svcBox) {
      const br = curBtn.getBoundingClientRect();
      const sr = svcBox.getBoundingClientRect();
      if (br.top < sr.top || br.bottom > sr.bottom) {
        svcBox.scrollTop += (br.top - sr.top) / uiScale;
      }
    }
    // 按 openSvc 决定重建后展开哪个服务的弹层
    const want = openSvc === "current" ? (curGroup ? curGroup.name : "") : (openSvc || "");
    const openBtn = want ? svcBtns.get(want) : null;
    if (openBtn) openPopup(openBtn, svcGroups.get(want));
  };
  paint("current"); // 初次打开：直接展开当前模型所在服务，省一次点击
}
modelChip.style.cursor = "pointer";
modelChip.title = "当前模型 · 点击切换已启用的模型";
modelChip.onclick = toggleModelMenu;
document.addEventListener("click", (e) => {
  if (!modelMenu.classList.contains("hidden") &&
      !modelMenu.contains(e.target) && !modelChip.contains(e.target)) hideModelMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // 两级菜单逐层退：先收模型弹层，再按一次才关整个菜单
  if (modelPopup) {
    modelPopup.el.remove();
    modelPopup.btn.classList.remove("active");
    modelPopup = null;
    return;
  }
  hideModelMenu();
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
    loadUsageCollab();
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

// ---------- 协作成本看板（用量页）：按协作形态分列的 token / 轮次 / 估算费用 ----------
// 形态由后端按会话消息里的协作标记判定（分不出的归「普通」）；色板走主题
// 变量 --dm-c*（app.css 定义、各主题各自调色，写死色值会在深色主题下看不清）。
const COLLAB_FORM_ORDER = ["normal", "roundtable", "team", "adversarial"];
const COLLAB_FORM_LABELS = { normal: "普通", roundtable: "圆桌", team: "团队", adversarial: "对抗" };
const COLLAB_FORM_COLORS = {
  normal: "var(--dm-c1)", roundtable: "var(--dm-c6)",
  team: "var(--dm-c2)", adversarial: "var(--dm-c3)",
};

function usageCollabGroup(title, buckets, hasPrice) {
  const keys = COLLAB_FORM_ORDER;
  const maxTotal = Math.max(1, ...keys.map(
    (k) => ((buckets[k] || {}).in || 0) + ((buckets[k] || {}).out || 0)));
  const rows = keys.map((k) => {
    const d = buckets[k] || {};
    const total = (d.in || 0) + (d.out || 0);
    const w = total > 0 ? Math.max(3, Math.round((total / maxTotal) * 100)) : 0;
    const cost = hasPrice ? `¥${(d.cost || 0).toFixed(2)} · ` : "";
    const tip = `输入 ${usageFmtTokens(d.in || 0)} · 输出 ${usageFmtTokens(d.out || 0)}` +
      ((d.cached || 0) > 0 ? ` · 缓存命中 ${usageFmtTokens(d.cached)}` : "");
    return `<div class="uc-row"${total > 0 ? "" : ' style="opacity:.55"'}>` +
      `<span class="uc-dot" style="background:${COLLAB_FORM_COLORS[k]}"></span>` +
      `<span class="uc-name">${COLLAB_FORM_LABELS[k]}</span>` +
      `<span class="uc-bar"><i style="width:${w}%;background:${COLLAB_FORM_COLORS[k]}"></i></span>` +
      `<span class="uc-val" title="${tip}">${usageFmtTokens(total)} tokens</span>` +
      `<span class="uc-sub">${cost}${d.turns || 0} 轮 · ${d.sessions || 0} 会话</span></div>`;
  }).join("");
  return `<div class="uc-group"><h4>${title}</h4>${rows}</div>`;
}

async function loadUsageCollab() {
  const el = document.getElementById("usage-collab");
  if (!el) return;
  try {
    const r = await request("usage.collab_report", { days: 30 });
    const hasPrice = !!r.has_price;
    el.innerHTML = usageCollabGroup("今日", r.today || {}, hasPrice) +
      usageCollabGroup(`近 ${r.days || 30} 天`, r.forms || {}, hasPrice);
    const totalEl = document.getElementById("usage-collab-total");
    if (totalEl) {
      const f = r.forms || {};
      totalEl.textContent = hasPrice ? `近 ${r.days || 30} 天估算 ${cost_sum(f)}` : "";
    }
    renderUsagePricing(r.pricing || {});
  } catch (e) {
    el.innerHTML = `<p class="dim small">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function cost_sum(f) {
  const c = COLLAB_FORM_ORDER.reduce((s, k) => s + ((f[k] || {}).cost || 0), 0);
  return `¥${c.toFixed(2)}`;
}

function renderUsagePricing(p) {
  const el = document.getElementById("usage-pricing");
  if (!el) return;
  el.innerHTML =
    '<div class="uc-price-tip">费用折算默认价目（元 / 百万 tokens）：填 0 表示不计价，' +
    "留空表示不改动。想按服务用更准的单价，在 config.toml 的 " +
    "[usage_pricing.providers.服务名] 里覆盖。</div>" +
    '<div class="price-row">' +
    `<input id="uc-price-in" type="number" min="0" step="0.1" value="${p.price_in || ""}" placeholder="输入价" title="每百万输入 tokens 的单价（元）" autocomplete="off">` +
    `<input id="uc-price-out" type="number" min="0" step="0.1" value="${p.price_out || ""}" placeholder="输出价" title="每百万输出 tokens 的单价（元）" autocomplete="off">` +
    `<input id="uc-price-cache" type="number" min="0" step="0.1" value="${p.price_cache || ""}" placeholder="缓存命中价" title="命中提示词缓存的输入单价（元）；留空 = 不区分缓存" autocomplete="off">` +
    '</div><div class="uc-price-ops"><button id="uc-price-save" class="btn-ghost">保存价目</button>' +
    '<span id="uc-price-status" class="dim small"></span></div>';
  document.getElementById("uc-price-save").onclick = saveUsagePricing;
}

async function saveUsagePricing() {
  const status = document.getElementById("uc-price-status");
  const body = {};
  const pairs = [
    ["uc-price-in", "price_in", "输入价"],
    ["uc-price-out", "price_out", "输出价"],
    ["uc-price-cache", "price_cache", "缓存命中价"],
  ];
  for (const [id, key, label] of pairs) {
    const v = document.getElementById(id).value.trim();
    if (v === "") continue;
    const n = Number(v);
    if (!(n >= 0)) {
      status.textContent = `${label}要是不小于 0 的数字`;
      return;
    }
    body[key] = n;
  }
  try {
    await request("usage.pricing_set", body);
    status.textContent = "已保存";
    loadUsageCollab();
  } catch (e) {
    status.textContent = escapeHtml(e.message);
  }
}

// ---------- 提示词：自定义提示词模板（输入 ~ 时展示；原名「快捷指令」，2.0 起更名并换触发符号） ----------
let snippetsCache = [];

// 内置示例模板（内容定义在后端 BUILTIN_SNIPPETS，首启已落成真实记录、可在设置页
// 编辑/删除）：只在用户把指令删光时给 / 菜单兜底，随 snippets.list 响应下发。
let builtinSnippetsCache = [];

function builtinSnippets() {
  return snippetsCache.length ? [] : builtinSnippetsCache;
}

// ~ 候选排序：manual = 列表手动顺序（默认）；top = 常用优先（次数多、用得近的在前）。
// 偏好存 ui.json 的 snippets_sort，只影响候选菜单，设置页列表顺序仍由拖拽决定
let snippetsSort = "manual";

// ~ 菜单候选池：启用中的用户提示词（按排序偏好）+ 内置示例兜底（无 id，排最后）
function snippetCandidates() {
  const users = snippetsCache.filter((s) => s.enabled !== 0);
  if (snippetsSort === "top") {
    users.sort((a, b) => (b.use_count || 0) - (a.use_count || 0) ||
      (b.last_used_at || 0) - (a.last_used_at || 0));
  }
  return users.concat(builtinSnippets());
}

// ~ 过滤匹配：名称 / 内容子串，外加名称的拼音首字母串（如 hwxbc ↔ 帮我修报错）
function snippetMatch(s, q) {
  return s.name.toLowerCase().includes(q) ||
    (s.content || "").toLowerCase().includes(q) ||
    (s.initials || "").includes(q);
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
  // 排序开关的档位文案（影响 ~ 候选顺序，不影响下面这份拖拽排的列表）
  const sortBtn = document.getElementById("btn-snippets-sort");
  if (sortBtn) {
    sortBtn.textContent = snippetsSort === "top" ? "排序：常用优先" : "排序：手动顺序";
    sortBtn.title = snippetsSort === "top"
      ? "~ 候选当前按使用次数 / 最近使用排序；点击改回列表手动顺序"
      : "~ 候选当前按列表顺序（行首 ⋮⋮ 拖拽决定）；点击改为常用优先";
  }
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
  const p2 = pad2;
  const nowYear = new Date().getFullYear();
  // Alt+1..8 直插的是候选前 8 条：序号按 snippetCandidates 的真实顺序算
  //（常用优先模式下 ≠ 列表顺序），停用项不在候选里不标
  const hotIdx = new Map();
  snippetCandidates().slice(0, 8).forEach((s, i) => hotIdx.set(s.id, i + 1));
  snippetsCache.forEach((s) => {
    const li = document.createElement("li");
    li.draggable = true;
    li.dataset.sid = String(s.id);
    const enabled = s.enabled !== 0;
    if (!enabled) li.classList.add("off");
    li.title = "点行编辑；按住行首 ⋮⋮ 拖动调整顺序（列表顺序就是 ~ 候选顺序）";
    const handle = document.createElement("span");
    handle.className = "s-drag";
    handle.textContent = "⋮⋮";
    li.appendChild(handle);
    if (enabled && hotIdx.has(s.id)) {
      const rank = document.createElement("span");
      rank.className = "s-rank";
      rank.textContent = hotIdx.get(s.id);
      rank.title = `Alt+${hotIdx.get(s.id)} 可直接插入这条`;
      li.appendChild(rank);
    }
    const title = document.createElement("span");
    title.className = "s-title";
    title.textContent = s.name + (enabled ? "" : "（已停用）");
    const sub = document.createElement("span");
    sub.className = "s-sub";
    const lu = s.last_used_at ? new Date(s.last_used_at * 1000) : null;
    const luText = lu ? ` · 上次 ${lu.getFullYear() === nowYear
      ? `${p2(lu.getMonth() + 1)}-${p2(lu.getDate())} ${p2(lu.getHours())}:${p2(lu.getMinutes())}`
      : `${lu.getFullYear()}-${p2(lu.getMonth() + 1)}-${p2(lu.getDate())}`}` : "";
    sub.textContent = (s.content || "").replace(/\s+/g, " ").slice(0, 60) +
      (s.use_count ? ` · 用过 ${s.use_count} 次${luText}` : "");
    sub.title = (s.content || "").replace(/\s+/g, " ") || "（空模板）"; // 预览截断了，悬停看全文
    li.append(title, sub);
    const ops = document.createElement("span");
    ops.className = "cron-ops";
    ops.innerHTML = `<button class="cron-op" title="${enabled ? "停用（~ 候选不再显示，可随时再启用）" : "启用（回到 ~ 候选）"}"><i class="${enabled ? "ic-pause" : "ic-play"}"></i></button>` +
      `<button class="cron-op" title="复制模板原文">⧉</button>` +
      `<button class="cron-op" title="编辑">✎</button>` +
      `<button class="cron-op danger" title="删除">✕</button>`;
    const [tog, copy, edit, del] = ops.querySelectorAll("button");
    tog.onclick = async (e) => {
      e.stopPropagation();
      try {
        await request("snippets.set_enabled", { id: s.id, enabled: !enabled });
        s.enabled = enabled ? 0 : 1;
        renderSnippets();
      } catch (err) { addNotice("操作失败: " + err.message); }
    };
    copy.onclick = async (e) => {
      e.stopPropagation();
      await copyTextToClipboard(s.content || "");
      copy.textContent = "✓";
      setTimeout(() => { copy.textContent = "⧉"; }, 1200);
    };
    edit.onclick = (e) => { e.stopPropagation(); snippetModal(s); };
    del.onclick = async (e) => {
      e.stopPropagation();
      // 提示词是用户攒的资产，✕ 又紧挨着编辑/复制，删错就没了——问一声
      if (!(await confirmModal("删除提示词",
        `<p>删除「${escapeHtml(s.name)}」？删除后不可恢复。</p>`, "删除"))) return;
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
        <button type="button" class="sn-chip" data-ph="{{任务|默认值}}">{{字段|默认值}} 填空（插入时现场填写）</button>
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
  // AI 润色：让当前模型把内容改写得更清晰（占位符原样保留）；
  // 结果只填回编辑框，用户看过、改过、点保存才生效
  const polish = document.createElement("button");
  polish.type = "button";
  polish.className = "sn-chip";
  polish.title = "让当前模型把内容改写得更清晰结构化，结果填回编辑框，满意再保存";
  polish.textContent = "✨ AI 润色";
  polish.onclick = async () => {
    const text = ta.value.trim();
    if (!text) { addNotice("先写点内容再润色"); return; }
    polish.disabled = true;
    polish.textContent = "润色中…";
    try {
      const r = await request("snippets.polish", { content: text });
      if (r.content) ta.value = r.content;
    } catch (err) { addNotice("润色失败: " + err.message); }
    polish.disabled = false;
    polish.textContent = "✨ AI 润色";
  };
  box.querySelector(".sn-chips").appendChild(polish);
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
// ~ 候选排序开关：手动顺序（拖拽决定）↔ 常用优先（次数多、用得近的在前）
document.getElementById("btn-snippets-sort").onclick = () => {
  snippetsSort = snippetsSort === "top" ? "manual" : "top";
  saveUiPrefs({ snippets_sort: snippetsSort === "top" ? "top" : null });
  renderSnippets();
};

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
// 已知占位符之外的 {{...}} 都是「填空字段」：插入时弹小表单当场填写
// （{{字段|默认值}} 支持默认值），填完才落进输入框
const SNIPPET_KEY_PH = new Set(["clipboard", "date", "time", "project"]);

function snippetFillFields(content) {
  const fields = new Map(); // 字段名 -> 默认值（同名只出现一次）
  const re = /\{\{([^{}|]+)(?:\|([^{}]*))?\}\}/g;
  let m;
  while ((m = re.exec(content))) {
    const key = m[1].trim();
    if (!key || SNIPPET_KEY_PH.has(key)) continue;
    if (!fields.has(key)) fields.set(key, m[2] == null ? "" : m[2]);
  }
  return fields;
}

function snippetFillModal(fields) {
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.innerHTML = '<p class="dim small">这条提示词里有需要现场填写的内容（留空则用默认值）：</p>' +
      [...fields].map(([k, v]) =>
        `<label class="small dim" style="display:block;margin-top:6px">${escapeHtml(k)}</label>` +
        `<input class="modal-input" data-f="${escapeHtml(k)}" style="width:100%" value="${escapeHtml(v)}">`
      ).join("");
    showModal("填写提示词", box, () => {
      const out = new Map();
      box.querySelectorAll("input[data-f]").forEach((i) => out.set(i.dataset.f, i.value));
      resolve(out);
    }, "插入");
    document.getElementById("modal-cancel").onclick = () => { hideModal(); resolve(null); };
  });
}

let snippetFace = null; // 插入目标面（菜单唤起的那一面）
async function insertSnippet(s, face) {
  snippetFace = face || null;
  let content = s.content || "";
  // 剪贴板：读得到直接替换；读不到 / 为空时原位换成填空字段，别让占位符原样溜进消息
  if (content.includes("{{clipboard}}")) {
    let clip = "";
    try { clip = (await navigator.clipboard.readText()) || ""; } catch (e) { /* 授权被拒 */ }
    content = clip ? content.split("{{clipboard}}").join(clip)
      : content.split("{{clipboard}}").join("{{剪贴板内容}}");
  }
  // 填空字段：有就先弹表单；取消 = 不插入
  const fields = snippetFillFields(content);
  if (fields.size) {
    const filled = await snippetFillModal(fields);
    if (filled == null) return;
    content = content.replace(/\{\{([^{}|]+)(?:\|([^{}]*))?\}\}/g, (whole, rawName) => {
      const key = rawName.trim();
      if (SNIPPET_KEY_PH.has(key)) return whole; // 日期/时间/项目名走下面的同步替换
      const v = (filled.get(key) || "").trim();
      return v || (fields.get(key) || "");
    });
  }
  // 本地占位符：日期 / 时间 / 当前项目名（同步替换）
  if (content.includes("{{date}}") || content.includes("{{time}}") || content.includes("{{project}}")) {
    const now = new Date();
    const p2 = pad2;
    content = content.split("{{date}}").join(
      `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())}`);
    content = content.split("{{time}}").join(`${p2(now.getHours())}:${p2(now.getMinutes())}`);
    content = content.split("{{project}}").join(currentProjectName() || "当前项目");
  }
  const input = faceInputEl(snippetFace);
  input.value = (input.value ? input.value + "\n" : "") + content;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  autoGrowInput(input);
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
  // 空标签徽章的全局镜像同源刷新（boot / 切项目都走这里）
  globalProviderName = curProviderName;
  globalModelName = curModelName;
  globalSupportsVision = curSupportsVision;
  refreshReasoning(); // 思考徽章按当前口径显示（活动会话取会话级，空标签取全局）
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
                      { withMessages: snap.session.messages || [],
                        messagesOffset: snap.session.messages_offset });
  } else {
    startNewTab(false); // 启动无可恢复会话：只开欢迎页，不落库（发首条消息时才建）
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
  if (rightViewVisible("memory")) loadMemoryPanel();
  // 启动后台已查过更新：有新版本时进通知中心（设置 · 关于里可手动再查）
  if (snap.update) {
    pushNotice(`🆕 新版本 v${snap.update.version} 可用`, "设置 · 关于 里可前往下载");
  }
  // 上次应用内更新的结果对账：成功报一次喜，失败带着现场提醒（设置 · 关于可「知道了」）
  if (snap.update_success) {
    const s = snap.update_success;
    pushNotice(`✓ 更新完成${s.from ? `：v${s.from} → ` : ""}v${s.to}`,
      "会话与设置数据不受影响，更新前已自动备份");
  }
  if (snap.update_failure) {
    const f = snap.update_failure;
    pushNotice(`⚠ 上次更新未完成${f.target ? `（目标 v${f.target}）` : ""}`,
      "应用仍运行原版本，更新前数据已备份；设置 · 关于 里有现场与处理指引");
  }
  if (snap.crashed_last_run) {
    pushNotice("⚠ 上次 SkySheep 未正常关闭（可能崩溃或被强杀）",
      "会话数据有滚动备份，一般无碍；如遇异常可到 设置 · 关于 导出诊断包反馈");
  }
  // 首次加载（刷新页面）时后端可能正挂着未决的权限请求：事件流里的确认卡
  // 已经丢了，补拉一次把当前会话的卡重建出来（重连路径在 ws.onopen 里也调）
  restorePendingPermissions();
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
// 无 Key 用户还有一条免注册出路：内置演示模式。
function maybeOnboard(snap) {
  const hasWorkingProvider = !snap.provider_error && snap.provider;
  // 「用户真的配过 Key」要排除 localhost 服务：本地服务通常不校验 Key，
  // 若把本地服务的 has_key 也算进来，全新机器会被误判成已配置，
  // 向导一次都不弹、还倒写 onboarded 把自己永久关掉
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
      <div class="ob-alt-title dim small">没有 Key？可以先看演示模式：</div>
      <div class="ob-alt-row">
        <button type="button" class="btn-ghost" data-f="ob-demo">🎬 先看演示模式</button>
      </div>
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

// 协作单按钮统一画笔：圆桌 / 团队共用一个入口——徽记显示当前模式与成员数，
// 配色随模式（圆桌紫 / 团队绿），关闭时恢复纯图标。setRtOn/setTeamOn/两处
// 成员确定回包都经它刷新。
function updateCoopHint() {
  const btn = document.getElementById("coop-switch");
  if (!btn) return;
  const n = rtMembers ? rtMembers.length : 0;
  const tn = teamMembers ? teamMembers.length : 0;
  const an = advRolesPicked();
  btn.classList.toggle("on", rtOn || teamOn || advOn);
  btn.classList.toggle("coop-rt", rtOn && !teamOn && !advOn);
  btn.classList.toggle("coop-team", teamOn && !advOn);
  btn.classList.toggle("coop-adv", advOn);
  if (rtOn) {
    btn.title = (n
      ? `圆桌（已指定 ${n} 个成员）：本条消息由多个模型并行思考，融合成更好的答案`
      : "圆桌：自动挑选已配置 Key 的模型共同思考，融合成更好的答案（点击可换模式或指定成员）");
  } else if (teamOn) {
    if (teamDirectorIsAi()) {
      const who = `AI 总管（${teamDirector.provider}/${teamDirector.model || "服务默认模型"}）`;
      btn.title = `团队（${tn ? `已指定 ${tn} 名队员` : "队员自动挑选"} · ${who}）：` +
        "AI 总管自动拆解、派工、验收、交付；你随时插话，或点团队卡上的「接管」亲自指挥";
    } else {
      btn.title = (tn
        ? `团队（已指定 ${tn} 个成员）：开启后组建团队，你的消息作为总管指令进入团队频道`
        : "团队：自动挑选已配置 Key 的模型组建团队，你以总管身份派工、验收、交付（点击可换模式或指定成员）");
    }
  } else if (advOn) {
    btn.title = (an
      ? `对抗（${an} 个角色已指定模型）：发现者找问题 → 调查者核实 → 建议者给方案，裁判出终审报告`
      : "对抗：自动挑选已配置 Key 的模型做对抗性审查——找问题、核实真伪、给方案、终审报告（点击可换模式或指定角色模型）");
  } else {
    btn.title = "协作：圆桌 / 团队 / 对抗（点击选择模式与成员）";
  }
  const state = btn.querySelector(".cp-state");
  if (!state) return;
  state.hidden = !rtOn && !teamOn && !advOn;
  state.textContent = rtOn ? (n ? `圆桌·${n}` : "圆桌·自动")
    : teamOn ? (teamDirectorIsAi() ? "团队·AI总管" : tn ? `团队·${tn}人` : "团队·自动")
    : advOn ? (an ? `对抗·指定${an}` : "对抗·自动")
    : "";
}

function setRtOn(on, silent = false) {
  const wasTeam = teamOn;
  rtOn = on;
  // 同一条消息只能选一种协作模式：开圆桌自动关团队/对抗（后端多开报参数错）
  if (on) setTeamOn(false, silent);
  if (on) setAdvOn(false, silent);
  if (!silent && on && wasTeam) addNotice("已切换到圆桌，团队已关闭（同一条消息只能选一种协作模式）");
  updateCoopHint();
}

// ---------- 协作统一菜单：圆桌 / 团队 / 关闭 一个入口 ----------
// 菜单保持单列模式清单；点「圆桌」「团队」即生效，并在本弹层右方放出对应的
// 设置弹层（rt-menu / team-menu，锚在菜单右侧，放不下自动换到左侧）。
// coopRenderGuard：程序性重建菜单（innerHTML 清空再画）会让被点的按钮瞬间
// 脱离 DOM，外点关闭监听会误判「点在菜单外」把菜单关掉——重建期间跳过关闭。
let coopRenderGuard = false;

function showCoopMenu(e) {
  e.stopPropagation(); // 别让随后冒泡到 document 的 click 立刻把菜单关掉
  const menu = document.getElementById("coop-menu");
  if (!menu.classList.contains("hidden")) {
    menu.classList.add("hidden");
    closeRtTeamPopups();
    return;
  }
  renderCoopMenuItems(menu);
  positionRtMenu(menu, "coop-switch");
  menu.classList.remove("hidden");
}

function renderCoopMenuItems(menu) {
  const closeMenu = () => { menu.classList.add("hidden"); closeRtTeamPopups(); };
  menu.innerHTML = "";
  const active = rtOn ? "rt" : teamOn ? "team" : advOn ? "adv" : "off";
  const tip = document.createElement("div");
  tip.className = "rt-menu-tip";
  tip.textContent = "协作模式（本条消息生效，发送后复位）：";
  menu.appendChild(tip);
  const n = rtMembers ? rtMembers.length : 0;
  const tn = teamMembers ? teamMembers.length : 0;
  const an = advRolesPicked();
  const modes = [
    {
      key: "off", active: active === "off",
      label: "不用协作", desc: "普通单模型对话",
    },
    {
      key: "rt", active: active === "rt",
      label: rtOn && n ? `圆桌 · ${n} 个成员` : "圆桌",
      desc: "多个模型并行思考，融合成更好的答案",
    },
    {
      key: "team", active: active === "team",
      label: teamOn ? (teamDirectorIsAi() ? "团队 · AI 总管" : tn ? `团队 · ${tn} 名队员` : "团队 · 自动")
        : "团队",
      desc: "组建团队，你的消息作为总管指令进入团队频道",
    },
    {
      key: "adv", active: active === "adv",
      label: advOn && an ? `对抗 · 指定 ${an} 个角色` : "对抗",
      desc: "对抗性审查：找问题 → 核实 → 给方案 → 终审报告",
    },
  ];
  for (const m of modes) {
    const b = document.createElement("button");
    b.className = "coop-mode-item" + (m.active ? " active" : "");
    b.innerHTML = `<span class="coop-mode-check">${m.active ? "✓" : ""}</span>` +
      `<span class="coop-mode-body">` +
      `<span class="coop-mode-label">${escapeHtml(m.label)}</span>` +
      `<span class="coop-mode-desc">${escapeHtml(m.desc)}</span>` +
      `</span>`;
    b.title = m.active
      ? (m.key === "off" ? "点击收起" : "点击打开设置")
      : "点击启用，并在右侧放出设置";
    b.onclick = () => {
      // 已启用的模式再点不放出设置弹层（「不用协作」没有设置，仍只收起）
      if (m.active) {
        if (m.key !== "off") openSettingsBeside(m.key);
        else closeMenu();
        return;
      }
      coopRenderGuard = true;
      if (m.key === "rt") { setRtOn(true); openSettingsBeside("rt"); }
      else if (m.key === "team") { setTeamOn(true); openSettingsBeside("team"); }
      else if (m.key === "adv") { setAdvOn(true); openSettingsBeside("adv"); }
      else { setRtOn(false, true); setTeamOn(false, true); setAdvOn(false, true); closeRtTeamPopups(); }
      renderCoopMenuItems(menu);
      positionRtMenu(menu, "coop-switch");
      setTimeout(() => { coopRenderGuard = false; }, 0);
    };
    menu.appendChild(b);
  }
  // 团队进行中：协作菜单附「团队室」直达（团队模式设计 §15.1 入口②）——
  // 只跳页签，不动本条消息的协作开关
  const teamLive = activeTab && activeTab.teamCard && activeTab.teamCard.isConnected
    && !activeTab.teamCard.classList.contains("ended");
  if (teamLive && typeof openRightTab === "function") {
    const room = document.createElement("button");
    room.className = "coop-mode-item";
    room.title = "打开右侧面板的「团队室」页签：成员、频道、工单板三分区同屏";
    room.innerHTML = '<span class="coop-mode-check"></span>' +
      '<span class="coop-mode-body">' +
      '<span class="coop-mode-label">团队室</span>' +
      '<span class="coop-mode-desc">成员 · 频道 · 工单板 大屏同看（独立页签）</span></span>';
    room.onclick = () => { closeMenu(); openRightTab("teamroom"); };
    menu.appendChild(room);
  }
}

// 设置弹层贴在协作菜单右侧放出（放不下换到左侧）；确定后连协作菜单一起收起
function closeRtTeamPopups() {
  for (const id of ["rt-menu", "team-menu", "adv-menu"]) {
    const m = document.getElementById(id);
    if (m) m.classList.add("hidden");
  }
}

function openSettingsBeside(kind) {
  closeRtTeamPopups();
  const menu = document.getElementById(
    kind === "rt" ? "rt-menu" : kind === "team" ? "team-menu" : "adv-menu"
  );
  const coopMenu = document.getElementById("coop-menu");
  menu.innerHTML = '<div class="rt-menu-tip">加载中…</div>';
  menu.classList.remove("hidden");
  positionMenuBeside(menu, coopMenu);
  const closeMenu = () => { menu.classList.add("hidden"); coopMenu.classList.add("hidden"); };
  const fill = kind === "rt" ? buildRtSettingsInto
    : kind === "team" ? buildTeamSettingsInto : buildAdvSettingsInto;
  fill(menu, closeMenu)
    .then(() => { if (!menu.classList.contains("hidden")) positionMenuBeside(menu, coopMenu); })
    .catch(() => {});
}

// 把弹层锚在参照物（协作菜单）右侧：同一 bottom 对齐，横向放不下换到左侧；
// 两侧都放不下（右侧面板一开聊天区就窄了，协作菜单 350 + 团队浮层 420 经常
// 超宽）就升到参照物正上方竖排——直接钳边会让弹层整块压在协作菜单上互相遮挡
function positionMenuBeside(menu, ref) {
  const composer = document.getElementById("composer");
  const host = document.getElementById("chat-main");
  if (!composer || !host || !ref) return;
  const hostRect = host.getBoundingClientRect();
  const refRect = ref.getBoundingClientRect();
  const menuW = menu.offsetWidth || 360;
  const hostW = hostRect.width / uiScale;
  const refLeft = (refRect.left - hostRect.left) / uiScale;
  const refRight = (refRect.right - hostRect.left) / uiScale;
  const gap = 8, margin = 8;
  let left = refRight + gap; // 首选：参照物右缘外 8px
  let fits = left + menuW <= hostW - margin;
  if (!fits) {
    left = refLeft - menuW - gap; // 次选：参照物左缘外 8px
    fits = left >= margin;
  }
  let bottom = composer.offsetHeight + gap;
  if (!fits) {
    // 兜底：竖排——升到参照物上方，左缘对齐参照物（右溢出时钳边也仍在
    // 参照物上方，竖向已错开，不会互相遮挡）
    const refH = refRect.height / uiScale;
    bottom = composer.offsetHeight + refH + gap;
    left = refLeft;
  }
  menu.style.top = "auto";
  menu.style.bottom = Math.round(bottom) + "px";
  menu.style.left = Math.round(Math.max(margin, Math.min(left, Math.max(margin, hostW - menuW - margin)))) + "px";
}

async function buildRtSettingsInto(box, closeMenu) {
  box.innerHTML = '<div class="rt-menu-tip">加载中…</div>';
  const cfg = await request("config.providers").catch(() => null);
  if (!cfg || !box.isConnected || box.classList.contains("hidden")) return;
  box.innerHTML = "";
  const tip = document.createElement("div");
  tip.className = "rt-menu-tip";
  tip.textContent = "选择圆桌成员（不选 = 自动挑选已配置 Key 的模型）：";
  box.appendChild(tip);
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
  box.appendChild(list);
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
  box.appendChild(extra);
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
    updateCoopHint();
    closeMenu();
  };
  actions.append(clear, ok);
  box.appendChild(actions);
}

// 对抗设置弹层：发现/调查/建议三个角色逐行指定模型（下拉，留空 = 自动挑选，
// 同一模型可身兼数角）；裁判固定用当前主模型。改动即存（localStorage），确定收起。
async function buildAdvSettingsInto(box, closeMenu) {
  box.innerHTML = '<div class="rt-menu-tip">加载中…</div>';
  const cfg = await request("config.providers").catch(() => null);
  if (!cfg || !box.isConnected || box.classList.contains("hidden")) return;
  box.innerHTML = "";
  const tip = document.createElement("div");
  tip.className = "rt-menu-tip";
  tip.textContent = "为每个角色指定模型（留空 = 自动挑选）：";
  box.appendChild(tip);
  // 流水线示意：前三个角色由下面指定的模型担任，裁判固定是当前主模型
  const flow = document.createElement("div");
  flow.className = "adv-flow";
  flow.innerHTML =
    '<span class="adv-flow-step">发现</span><span class="adv-flow-sep">→</span>' +
    '<span class="adv-flow-step">调查</span><span class="adv-flow-sep">→</span>' +
    '<span class="adv-flow-step">建议</span><span class="adv-flow-sep">→</span>' +
    '<span class="adv-flow-step adv-flow-judge" title="裁判由当前主模型担任，无需选择">裁判 · 主模型</span>';
  box.appendChild(flow);
  // 只列已配置 Key 的模型：缺 Key 的服务选了也跑不了，列出来只是噪声
  const options = [];
  Object.entries(cfg.providers || {}).forEach(([name, p]) => {
    if (!p.key_mask) return;
    (p.models || []).forEach((m) => options.push(name + "/" + m));
  });
  const ROLE_DEFS = [
    { key: "finder", label: "发现者", desc: "穷举找问题" },
    { key: "investigator", label: "调查者", desc: "逐条验证真伪" },
    { key: "advisor", label: "建议者", desc: "给修复方案" },
  ];
  const list = document.createElement("div");
  list.className = "adv-role-list";
  const saveAdvRoles = () => {
    try { localStorage.setItem("skysheep.adv.roles", JSON.stringify(advRoles)); } catch {}
    updateCoopHint();
  };
  for (const def of ROLE_DEFS) {
    const row = document.createElement("div");
    row.className = "adv-role-row";
    const chip = document.createElement("span");
    chip.className = "adv-role-label";
    chip.textContent = def.label;
    const desc = document.createElement("span");
    desc.className = "adv-role-desc";
    desc.textContent = def.desc;
    const sel = document.createElement("select");
    sel.className = "adv-role-select";
    sel.title = `${def.label}用什么模型（留空 = 自动挑选已配置 Key 的服务）`;
    const optAuto = document.createElement("option");
    optAuto.value = "";
    optAuto.textContent = "自动";
    sel.appendChild(optAuto);
    for (const key of options) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = key;
      sel.appendChild(opt);
    }
    sel.value = String(advRoles[def.key] || "");
    if (sel.selectedIndex === -1) sel.value = ""; // 已存模型被删 Key 后回落自动
    sel.onchange = () => { advRoles[def.key] = sel.value; saveAdvRoles(); };
    row.append(chip, desc, sel);
    list.appendChild(row);
  }
  box.appendChild(list);
  if (!options.length) {
    const none = document.createElement("div");
    none.className = "rt-menu-tip";
    none.textContent = "还没有已配置 Key 的模型——先到「设置 · 模型服务」配置 API Key，或留空自动挑选。";
    box.appendChild(none);
  }
  const judgeRow = document.createElement("div");
  judgeRow.className = "adv-judge-row";
  judgeRow.innerHTML =
    '<span class="adv-flow-step adv-flow-judge">裁判 · 当前主模型</span>' +
    '<span class="adv-role-desc">终审报告由它产出，无需选择</span>';
  box.appendChild(judgeRow);
  const actions = document.createElement("div");
  actions.className = "rt-menu-actions";
  const clear = document.createElement("button");
  clear.className = "link-btn";
  clear.textContent = "恢复自动";
  clear.onclick = () => {
    for (const def of ROLE_DEFS) advRoles[def.key] = "";
    saveAdvRoles();
    list.querySelectorAll(".adv-role-select").forEach((s) => { s.value = ""; });
  };
  const ok = document.createElement("button");
  ok.className = "btn-primary";
  ok.textContent = "确定";
  ok.onclick = closeMenu; // 选择即存：确定只收起（连协作菜单一起）
  actions.append(clear, ok);
  box.appendChild(actions);
}

document.getElementById("coop-switch").onclick = showCoopMenu;

// ---------- 团队：多模型分工协作，用户以总管身份派工、验收、交付 ----------
// 与圆桌并列的第二种协作模式（docs/团队模式设计.md）：圆桌是会诊，团队是开工。
// 开关是一次性的（发送后复位）；团队本体是会话级状态——建队后消息自动进团队
// 频道（后端路由），直到交付/收队。团队卡在聊天流中保持展开，跨多轮持续更新。
let teamOn = false;
let teamMembers = (() => {
  try { return JSON.parse(localStorage.getItem("skysheep.team.members") || "null"); }
  catch { return null; }
})();
// 总管形态（二期 AI 总管）：null / {mode:"user"} = 我当总管（一期行为）；
// {mode:"ai", provider, model} = 指定一个已配 Key 的模型服务担任总管，随建队轮发后端
let teamDirector = (() => {
  try { return JSON.parse(localStorage.getItem("skysheep.team.director") || "null"); }
  catch { return null; }
})();
const teamDirectorIsAi = () =>
  !!(teamDirector && teamDirector.mode === "ai" && String(teamDirector.provider || "").trim());
// 建队来源模板名（四期常驻）：团队面板「从模板创建」应用的名字，随建队轮
// 发后端记进团队快照——之后在团队卡上登记常驻（team.resident_set）凭它绑回
// 模板，定时团队任务触发才能按（模板，项目）匹配复用。与成员/总管形态
// 同为「下一次建队的规划值」，随确认落 localStorage；手选成员不改它（模板
// 应用后微调成员仍算这个模板的队），不应用模板建队则为 null
let teamTplName = (() => {
  try { return JSON.parse(localStorage.getItem("skysheep.team.template") || "null"); }
  catch { return null; }
})();

// 团队状态徽标 / 工单状态 / 频道消息类别的唯一中文叫法（与 docs/界面术语表 §12 对齐）
const TEAM_STATUS_LABEL = {
  active: "进行中", done: "已交付", aborted: "已收队",
  rounds_exhausted: "轮次耗尽", interrupted: "已中断",
  budget_exhausted: "预算耗尽",
};
const TEAM_TASK_STATUS_LABEL = {
  pending: "待办", in_progress: "进行中", review: "待验收",
  done: "完成", error: "失败",
};
const TEAM_MSG_KIND_LABEL = {
  assign: "派工", report: "汇报", ask: "请示", help: "求助",
  object: "异议", ruling: "裁定", system: "系统",
};
const TEAM_DIRECTOR_LABEL = { user: "用户总管", ai: "AI 总管" };

// ---------- 对抗：四角色流水线审查（与圆桌/团队并列的第三种协作模式） ----------
// 发现者穷举问题 → 调查者逐条对抗验证 → 建议者给修复方案 → 裁判（当前主模型）
// 终审报告。开关是一次性的（发送后复位）；发现/调查/建议三个角色在弹层里
// 逐角色指定模型（留空 = 自动挑选，同一模型可身兼数角），裁判固定用当前主模型。
let advOn = false;
let advRoles = (() => {
  // 旧版勾选列表（skysheep.adv.members，按勾选顺序映射角色）一次性迁移成
  // 逐角色指定；迁移后旧键不再读取
  try {
    const saved = JSON.parse(localStorage.getItem("skysheep.adv.roles") || "null");
    if (saved && typeof saved === "object") return saved;
  } catch { /* 损坏则走迁移 */ }
  try {
    const legacy = JSON.parse(localStorage.getItem("skysheep.adv.members") || "null");
    if (Array.isArray(legacy) && legacy.length) {
      const roles = {};
      ["finder", "investigator", "advisor"].forEach((r, i) => {
        const m = legacy[i];
        if (m && m.provider) roles[r] = m.provider + "/" + (m.model || "");
      });
      try { localStorage.setItem("skysheep.adv.roles", JSON.stringify(roles)); } catch {}
      return roles;
    }
  } catch { /* 旧键也没有：全自动 */ }
  return {};
})();
// 显式指定的角色数（按钮徽记用）
const advRolesPicked = () =>
  ["finder", "investigator", "advisor"].filter((r) => String(advRoles[r] || "").trim()).length;
// 显式指定 → members 载荷（条目带 role 键，后端按角色直取）；全空 = 自动（不带 members）
function advMembersPayload() {
  const picked = advRolesPicked();
  if (!picked.length) return undefined;
  return picked.map((r) => {
    const v = String(advRoles[r]).trim();
    const idx = v.indexOf("/");
    return { provider: v.slice(0, idx), model: v.slice(idx + 1), role: r };
  });
}

updateCoopHint(); // 启动时把协作按钮画到当前档（无模式 = 纯图标）

function setTeamOn(on, silent = false) {
  const wasRt = rtOn;
  teamOn = on;
  // 同一条消息只能选一种协作模式：开团队自动关圆桌/对抗（后端多开报参数错）
  if (on) setRtOn(false, silent);
  if (on) setAdvOn(false, silent);
  if (!silent && on && wasRt) addNotice("已切换到团队，圆桌已关闭（同一条消息只能选一种协作模式）");
  // 规划模式与团队联动（后端同款语义）：规划模式开着开团队不拦——本轮队员
  // 一律只读执行（只调研、不写文件、不跑命令），这里说明降级，避免「以为
  // 队员会动手改文件」的误期待（silent=切标签恢复，不是用户操作，不弹）
  if (!silent && on && workMode === "plan") {
    addNotice("规划模式开着：本轮团队队员将以只读方式执行（不写文件、不跑命令）；需要队员动手请先切回执行模式。");
  }
  updateCoopHint();
}

function setAdvOn(on, silent = false) {
  const wasRt = rtOn;
  const wasTeam = teamOn;
  advOn = on;
  // 同一条消息只能选一种协作模式：开对抗自动关圆桌/团队（后端多开报参数错）
  if (on) setRtOn(false, silent);
  if (on) setTeamOn(false, silent);
  if (!silent && on && (wasRt || wasTeam)) {
    addNotice("已切换到对抗，原协作模式已关闭（同一条消息只能选一种协作模式）");
  }
  updateCoopHint();
}

// 激活态状态徽记：AI 总管是关键模式切换必须可见，优先显示；其余按队员数

// 团队成员选择浮层：与圆桌浮层同皮，勾选后展开 成员名 + 一句话人设 两个小输入框；
// 顶部是总管形态区（二期）：我当总管（默认）/ AI 总管 + 担任总管的模型单选
async function buildTeamSettingsInto(box, closeMenu) {
  box.innerHTML = '<div class="rt-menu-tip">加载中…</div>';
  const cfg = await request("config.providers").catch(() => null);
  if (!cfg || !box.isConnected || box.classList.contains("hidden")) return;
  box.innerHTML = "";
  // 模板区（三期）：「从模板创建」——选中后回填下面的总管形态与成员勾选。
  // 模板册是全局的（team.template_list）；拉不到（旧后端/断线）整块不出现，
  // 浮层照常手选，建队流程不受影响
  let templates = [];
  try { templates = ((await request("team.template_list")).templates) || []; } catch (e) {}
  if (templates.length && !box.classList.contains("hidden")) {
    const tplBox = document.createElement("div");
    tplBox.className = "tm-template";
    tplBox.appendChild(tcEl("div", "rt-menu-tip", "从模板创建："));
    const sel = document.createElement("select");
    sel.className = "tm-tpl-sel";
    sel.title = "选中模板后自动勾选成员、填好成员名与人设，并切好总管形态；确认前可随意微调";
    sel.innerHTML = '<option value="">— 选择模板 —</option>' +
      templates.map((t, i) => `<option value="${i}">${escapeHtml(t.name)}</option>`).join("");
    sel.onchange = () => {
      const tpl = templates[Number(sel.value)];
      if (tpl) applyTeamTemplate(box, tpl);
    };
    tplBox.appendChild(sel);
    box.appendChild(tplBox);
  }
  // 总管形态区：单选「我当总管 / AI 总管」；选 AI 才展开模型单选列表
  // （只列已配置 Key 的服务——总管缺 Key 跑不了，后端也会 fail-closed 拒建）
  const dirBox = document.createElement("div");
  dirBox.className = "tm-director";
  dirBox.appendChild(tcEl("div", "rt-menu-tip", "总管："));
  const dirModes = document.createElement("div");
  dirModes.className = "tm-dir-modes";
  const savedAi = teamDirectorIsAi() ? teamDirector : null;
  const mkDirMode = (value, label, checked) => {
    const lab = document.createElement("label");
    lab.className = "tm-dir-mode";
    lab.title = value === "ai"
      ? "AI 总管：自动拆解、派工、验收、交付；你随时插话或接管"
      : "我当总管：你的每条消息都是一道总管指令，派工/验收由你在团队卡上操作";
    const rb = document.createElement("input");
    rb.type = "radio";
    rb.name = "tm-dir-mode";
    rb.value = value;
    rb.checked = checked;
    lab.append(rb, tcEl("span", "", label));
    return lab;
  };
  const rbUser = mkDirMode("user", "我当总管（默认）", !savedAi);
  const rbAi = mkDirMode("ai", "AI 总管", !!savedAi);
  dirModes.append(rbUser, rbAi);
  dirBox.appendChild(dirModes);
  const dirOpts = document.createElement("div");
  dirOpts.className = "tm-dir-opts" + (savedAi ? "" : " hidden");
  dirOpts.appendChild(tcEl("div", "rt-menu-tip", "选择担任总管的模型（需已配置 Key）："));
  const dirList = document.createElement("div");
  dirList.className = "rt-menu-list";
  let dirEntries = 0;
  Object.entries(cfg.providers || {}).forEach(([name, p]) => {
    if (!p.key_mask) return;
    (p.models || []).forEach((m) => {
      dirEntries += 1;
      const key = name + "/" + m;
      const lab = document.createElement("label");
      lab.className = "rt-menu-item";
      lab.innerHTML =
        `<input type="radio" name="tm-dir-model" value="${escapeHtml(key)}"` +
        `${savedAi && savedAi.provider === name && savedAi.model === m ? " checked" : ""}>` +
        `<span class="mi-cmd">${escapeHtml(name)}</span>` +
        `<span class="mi-model">${escapeHtml(m)}</span>`;
      dirList.appendChild(lab);
    });
  });
  if (!dirEntries) {
    dirList.innerHTML = '<div class="rt-menu-tip">还没有已配置 Key 的模型——先到「设置 · 模型服务」配置 API Key。</div>';
  }
  dirOpts.appendChild(dirList);
  dirBox.appendChild(dirOpts);
  rbAi.querySelector("input").addEventListener("change", () => {
    dirOpts.classList.toggle("hidden", !rbAi.querySelector("input").checked);
  });
  rbUser.querySelector("input").addEventListener("change", () => {
    dirOpts.classList.toggle("hidden", rbUser.querySelector("input").checked);
  });
  box.appendChild(dirBox);
  const tip = document.createElement("div");
  tip.className = "rt-menu-tip";
  tip.textContent = "选择团队成员（不选 = 自动挑选已配置 Key 的模型）：";
  box.appendChild(tip);
  const list = document.createElement("div");
  list.className = "rt-menu-list";
  const selected = new Map((teamMembers || []).map((m) => [m.provider + "/" + m.model, m]));
  let entries = 0;
  Object.entries(cfg.providers || {}).forEach(([name, p]) => {
    // 只列已配置 Key 的服务：缺 Key 的服务选了也跑不了，列出来只是噪声
    if (!p.key_mask) return;
    (p.models || []).forEach((m) => {
      entries += 1;
      const key = name + "/" + m;
      const saved = selected.get(key) || {};
      const label = document.createElement("label");
      label.className = "rt-menu-item";
      label.innerHTML =
        `<input type="checkbox" value="${escapeHtml(key)}" ${selected.has(key) ? "checked" : ""}>` +
        `<span class="mi-cmd">${escapeHtml(name)}</span>` +
        `<span class="mi-model">${escapeHtml(m)}</span>`;
      const cb = label.querySelector("input");
      // 成员名（缺省沿服务名）+ 人设（写进该成员系统提示词）：勾选后才展开
      const extra = document.createElement("span");
      extra.className = "mi-extra" + (cb.checked ? "" : " hidden");
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "mi-name";
      nameInput.placeholder = `成员名（默认 ${name}）`;
      nameInput.value = saved.name || "";
      nameInput.title = "成员名：团队频道与工单指派都用它称呼这位队员";
      const personaInput = document.createElement("input");
      personaInput.type = "text";
      personaInput.className = "mi-persona";
      personaInput.placeholder = "一句话人设（可选）";
      personaInput.value = saved.persona || "";
      personaInput.title = "人设会写进这位队员的系统提示词，例如「资深行业分析师，先列证据再下结论」";
      extra.append(nameInput, personaInput);
      // 在输入框里点击/输入不能触发 label 的勾选切换，也不能把浮层点没
      extra.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); });
      extra.addEventListener("keydown", (ev) => ev.stopPropagation());
      cb.addEventListener("change", () => extra.classList.toggle("hidden", !cb.checked));
      label.appendChild(extra);
      label._nameInput = nameInput;
      label._personaInput = personaInput;
      list.appendChild(label);
    });
  });
  if (!entries) {
    list.innerHTML = '<div class="rt-menu-tip">还没有已配置 Key 的模型——先到「设置 · 模型服务」配置 API Key，再回来挑选队员。</div>';
  }
  box.appendChild(list);
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
    // 总管形态先落库：选 AI 必须已挑模型（否则留着浮层让用户补选）
    const dirMode = dirBox.querySelector('input[name="tm-dir-mode"]:checked');
    if (dirMode && dirMode.value === "ai") {
      const pick = dirBox.querySelector('input[name="tm-dir-model"]:checked');
      if (!pick) { addNotice("已选 AI 总管：请先挑选担任总管的模型服务"); return; }
      const di = pick.value.indexOf("/");
      teamDirector = { mode: "ai", provider: pick.value.slice(0, di), model: pick.value.slice(di + 1) };
    } else {
      teamDirector = { mode: "user" };
    }
    try { localStorage.setItem("skysheep.team.director", JSON.stringify(teamDirector)); } catch {}
    const picked = [...list.querySelectorAll("input:checked")].map((i) => {
      const idx = i.value.indexOf("/");
      const item = { provider: i.value.slice(0, idx), model: i.value.slice(idx + 1) };
      const row = i.closest(".rt-menu-item");
      const name = (row._nameInput.value || "").trim();
      const persona = (row._personaInput.value || "").trim();
      if (name) item.name = name;
      if (persona) item.persona = persona;
      return item;
    });
    teamMembers = picked.length ? picked : null;
    try { localStorage.setItem("skysheep.team.members", JSON.stringify(teamMembers)); } catch {}
    // 本浮层的形参是 box：来源模板名挂在 box._tplName（applyTeamTemplate 写入）
    teamTplName = box._tplName || null;
    try { localStorage.setItem("skysheep.team.template", JSON.stringify(teamTplName)); } catch {}
    updateCoopHint();
    closeMenu();
  };
  actions.append(clear, ok);
  box.appendChild(actions);
}


// 模板回填（三期）：清掉当前勾选 → 按模板勾成员、填成员名与人设 → 切总管形态。
// 成员按 provider/model 对上已配置服务；对不上的跳过并计数提示——服务没配 Key
// 或模型已下架的条目硬勾上也跑不了（后端照常降级），不能让用户以为填上了。
// 只回填表单，不落 localStorage：用户点「确定」才按既有路径存本轮值
function applyTeamTemplate(menu, tpl) {
  const dirOpts = menu.querySelector(".tm-dir-opts");
  const rbUser = menu.querySelector('input[name="tm-dir-mode"][value="user"]');
  const rbAi = menu.querySelector('input[name="tm-dir-mode"][value="ai"]');
  const cbs = [...menu.querySelectorAll("label.rt-menu-item input[type=\"checkbox\"]")];
  // 来源模板名挂浮层上，随「确定」落 teamTplName（建队轮发后端，见全局注释）
  menu._tplName = String(tpl.name || "");
  // 成员区先全清：模板定义整支团队，而不是往现有勾选上叠加
  cbs.forEach((cb) => {
    cb.checked = false;
    const extra = cb.closest(".rt-menu-item").querySelector(".mi-extra");
    if (extra) extra.classList.add("hidden");
  });
  const want = new Map();
  (tpl.members || []).forEach((m) => {
    const key = `${m.provider || ""}/${m.model || ""}`;
    if (key !== "/") want.set(key, m);
  });
  let hit = 0;
  cbs.forEach((cb) => {
    const m = want.get(cb.value);
    if (!m) return;
    cb.checked = true;
    const label = cb.closest(".rt-menu-item");
    if (label._nameInput) label._nameInput.value = m.name || "";
    if (label._personaInput) label._personaInput.value = m.persona || "";
    const extra = label.querySelector(".mi-extra");
    if (extra) extra.classList.remove("hidden");
    hit += 1;
  });
  const miss = (tpl.members || []).length - hit;
  // 总管形态：模板指定 AI 总管（带服务名）才切 AI；user 模式/空块回到「我当总管」
  const d = tpl.director || {};
  const wantAi = (tpl.director_mode || "user") === "ai" && String(d.provider || "").trim() !== "";
  let dirRadio = null;
  if (wantAi) {
    if (rbAi) rbAi.checked = true;
    if (rbUser) rbUser.checked = false;
    if (dirOpts) dirOpts.classList.remove("hidden");
    // 精确对到 provider+model；模型名空（服务默认）或已改名时退到同服务第一个
    menu.querySelectorAll('input[name="tm-dir-model"]').forEach((r) => {
      const i = r.value.indexOf("/");
      if (r.value.slice(0, i) !== d.provider) return;
      if (!dirRadio) dirRadio = r; // 同服务第一个：兜底候选
      if (d.model && r.value.slice(i + 1) === d.model) dirRadio = r;
    });
    if (dirRadio) dirRadio.checked = true;
  } else {
    if (rbUser) rbUser.checked = true;
    if (rbAi) rbAi.checked = false;
    if (dirOpts) dirOpts.classList.add("hidden");
  }
  const bits = [(tpl.members || []).length
    ? `模板「${tpl.name}」已填入 ${hit} 名成员`
    : `模板「${tpl.name}」是空名册，只回填了总管形态`];
  if (miss) bits.push(`${miss} 名成员不在已配置服务中，已跳过`);
  if (wantAi) {
    if (dirRadio) {
      if (!d.model || dirRadio.value !== `${d.provider}/${d.model}`) {
        bits.push("模板总管的模型已不在该服务列表，改选了同服务第一个可用模型");
      }
    } else {
      bits.push("模板指定的总管服务未配置 Key，请在上方手动挑一个总管模型");
    }
  }
  addNotice(bits.join("；"));
}

/** 成员选择浮层定位：底边贴在输入栏上沿，左缘与触发按钮对齐。
 *
 *  用按钮的实时位置算（而不是写死 left），这样界面缩放、窗口宽度、
 *  输入栏高度变化都不会错位；再向容器内收敛，窄窗口下不会溢出或被裁切。
 *  anchorId 缺省为圆桌开关（团队浮层传自己的开关 id）。 */
function positionRtMenu(menu, anchorId) {
  const composer = document.getElementById("composer");
  const host = document.getElementById("chat-main");
  if (!composer || !host) return;
  const hostRect = host.getBoundingClientRect();
  const anchor = document.getElementById(anchorId || "coop-switch")
    || document.getElementById("coop-group");
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
  if (coopRenderGuard) return; // 程序性重建期间不算「点在外面」
  // #coop-menu 也算协作 UI：点菜单项（含再点已启用模式放出设置）不该被当成「点在外面」
  const inCoop = !!(e.target.closest && e.target.closest("#coop-group, #coop-menu"));
  const coopMenu = document.getElementById("coop-menu");
  const clickInSettings = !!(e.target.closest && e.target.closest("#rt-menu, #team-menu, #adv-menu"));
  if (coopMenu && !coopMenu.classList.contains("hidden") &&
      !coopMenu.contains(e.target) && !inCoop && !clickInSettings) {
    coopMenu.classList.add("hidden");
  }
  for (const id of ["rt-menu", "team-menu", "adv-menu"]) {
    const m = document.getElementById(id);
    if (m && !m.classList.contains("hidden") &&
        !m.contains(e.target) && !inCoop && !clickInSettings) {
      m.classList.add("hidden");
    }
  }
});

let currentSessionId = null; // 当前会话（/export、检查点列表用）

// ---------- Windows 系统通知：失焦时才弹，前台不打扰；开关存 ui.json（notify） ----------
let uiNotifyOn = true;
let ctrlEnterSend = false; // 发送键：false = Enter 发送（默认），true = Ctrl+Enter 发送
let uiNotifySound = false; // 提示音（默认关）：任务完成/等确认时合成短音，设置页开关
let welcomeOn = true; // 空白会话欢迎卡（默认开）：新建/空白会话显示「欢迎使用」，设置 · 界面与通知 可关
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
  const p = pad2;
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
        if (pageViewOpen()) backToChat(); // 在设置 / 工厂页里点的：先回对话视图再切标签
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

// 更新渠道（关于页）：stable 只收正式版，beta 提前收预发布；切换后立即按新渠道重查
const updateChannelSel = document.getElementById("update-channel");
if (updateChannelSel) {
  updateChannelSel.onchange = () => {
    saveUiPrefs({ update_channel: updateChannelSel.value });
    addNotice(updateChannelSel.value === "beta"
      ? "已切换到测试版渠道：预发布版本会提前推送，稳定性可能不如正式版"
      : "已切换到稳定版渠道：只推送正式版本");
    const btn = document.getElementById("btn-check-update");
    if (btn) btn.click(); // 立即按新渠道查一次，不等下次启动
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

// ---------- 空白会话欢迎卡（welcome_card，设置 · 界面与通知） ----------
function renderWelcomeToggle() {
  const t = document.getElementById("welcome-card-toggle");
  if (t) t.checked = welcomeOn;
}
const welcomeCardToggle = document.getElementById("welcome-card-toggle");
if (welcomeCardToggle) {
  welcomeCardToggle.onchange = () => {
    welcomeOn = welcomeCardToggle.checked;
    saveUiPrefs({ welcome_card: welcomeOn ? 1 : 0 });
    // 活动标签若是空白会话就即时跟着变（在设置页里关也能生效）：开→补欢迎卡，关→清成空白
    const log = curLog();
    if (log) {
      const blankView = !log.children.length || !!log.querySelector(".welcome");
      if (blankView) {
        if (welcomeOn) showWelcome();
        else log.innerHTML = "";
      }
    }
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
      migrateComposerSid("", s.id); // 空标签的输入区状态条目随懒创建迁移到新会话
      tab.title = tab.title || text.slice(0, 20) || "新会话";
      currentSessionId = s.id;
      activeSessionSid = s.id;
      refreshAcceptChip(); // 懒创建落库后盾牌切到新会话档位（继承引擎默认）
      if (tab.logEl.querySelector(".welcome")) tab.logEl.innerHTML = ""; // 清掉欢迎页
      renderTabs();
      refreshSessions(); // 侧栏立即出现新会话行（启动欢迎页首发消息的懒创建路径）
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
  // 用户主动发送：无论翻到哪儿都回到底部看自己的消息（并恢复自动跟随）
  const sendLog = curLog();
  if (sendLog) sendLog._followBottom = true;
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
    const teamThisTurn = teamOn;
    const advThisTurn = advOn;
    let compareThisTurn = false;
    if (rtThisTurn) {
      const cb = document.getElementById("rt-compare");
      compareThisTurn = !!(cb && cb.checked);
    }
    // 圆桌/团队同用 members 字段，对抗用带 role 键的角色指定（互斥开关保证同轮只有一个生效）
    const membersThisTurn = rtThisTurn ? rtMembers
      : teamThisTurn ? teamMembers
      : advThisTurn ? advMembersPayload() : null;
    // 总管形态（二期）：建队轮才消费——AI 总管随 team=true 带上 provider/model；
    // 用户总管（默认）不传参，后端按一期行为建队
    const dirAiThisTurn = teamThisTurn && teamDirectorIsAi();
    // 协作都是一次性开关：发送后复位
    setRtOn(false);
    setTeamOn(false);
    setAdvOn(false);
    // 发送消费掉了草稿与一次性开关：同步清该会话的状态条目，
    // 不然切走再切回来，旧草稿/开关会原样复活
    saveComposerState(tab.sid);
    // 辩论轮数 / 主席出草稿：弹层里的本轮值优先于配置（config 作默认值）
    const r = await request("chat.send", {
      text,
      session_id: tab.sid || undefined,
      wants_title: tab.firstSend === true,
      plan_mode: workMode === "plan",
      roundtable: rtThisTurn,
      team: teamThisTurn || undefined,
      adversarial: advThisTurn || undefined,
      members: membersThisTurn || undefined,
      director_mode: dirAiThisTurn ? "ai" : undefined,
      director: dirAiThisTurn
        ? { provider: String(teamDirector.provider).trim(), model: String(teamDirector.model || "").trim() }
        : undefined,
      // 建队来源模板名（四期常驻）：「从模板创建」应用过的名字随建队轮带上，
      // 后端记进团队快照——之后在团队卡上登记常驻凭它绑回模板
      team_template: teamThisTurn && teamTplName ? teamTplName : undefined,
      compare: compareThisTurn || undefined,
      debate_rounds: rtThisTurn ? rtDebate : undefined,
      chair_answers: rtThisTurn ? rtChair : undefined,
      images: images.length ? images : undefined,
      refs: refs.length ? refs : undefined,
    });
    setContextUsage(r.context_tokens || 0, r.context_limit || 0, tab);
    if (r.context_detail) setContextDetail(r.context_detail, tab);
    // 对抗报告的真实消息序号随响应回来（assistant_message 事件先于落库，
    // 事件里的 seq 为空）：补挂「转为团队工单」入口
    if (r.adversarial && r.adversarial_seq) {
      advAttachTransfer(tab, r.adversarial, r.adversarial_seq);
    }
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
    // 团队轮：返回值带本轮结束时的团队快照（建队/收队/轮次计数在这里对齐；
    // 进行中的消息与工单已由实时事件驱动，upsert 幂等）
    if (r.team && r.team.team) applyTeamMeta(tab, r.team.team);
    if (r.plan_mode && (tab.lastAssistantText || "").trim()) addPlanActions(tab.lastAssistantText, tab);
    if (r.checkpoint) addCheckpointBar(r.checkpoint, tab);
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

// 面参数（pane）：null = 主栏；分屏列传自己的 pane（状态与托盘都在列上），
// 主栏与各列的粘贴/拖图/托盘预览互不串
function faceImageTray(pane) {
  return pane ? pane.el.querySelector(".split-image-tray") : document.getElementById("image-tray");
}
function renderImageTray(pane) {
  const list = pane ? pane.images : pendingImages;
  const tray = faceImageTray(pane);
  if (!tray) return;
  tray.classList.toggle("hidden", !list.length);
  tray.innerHTML = "";
  list.forEach((im, i) => {
    const cell = document.createElement("div");
    cell.className = "image-thumb";
    cell.innerHTML = `<img src="data:${im.media_type};base64,${im.data}" alt="附件${i + 1}">` +
      `<button class="thumb-del" title="移除">✕</button>`;
    cell.querySelector(".thumb-del").onclick = () => {
      list.splice(i, 1);
      renderImageTray(pane);
    };
    tray.appendChild(cell);
  });
}

function clearPendingImages(pane) {
  if (pane) pane.images = [];
  else pendingImages = [];
  renderImageTray(pane);
}

function addPendingImageFile(file, pane) {
  const list = pane ? pane.images : pendingImages;
  if (!IMAGE_TYPES[file.type]) {
    addNotice("仅支持 PNG / JPEG / WebP / GIF 图片");
    return;
  }
  if (file.size > IMAGE_MAX_BYTES) {
    addNotice(`图片太大（${(file.size / 1048576).toFixed(1)}MB），最大 4MB`);
    return;
  }
  if (list.length >= IMAGE_MAX_COUNT) {
    addNotice("一条消息最多带 4 张图片");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const url = String(reader.result || "");
    const m = /^data:([^;]+);base64,(.+)$/.exec(url);
    if (!m) return;
    list.push({ media_type: m[1], data: m[2] });
    renderImageTray(pane);
  };
  reader.readAsDataURL(file);
}

const composerInput = document.getElementById("input");
function bindComposerPaste(input, pane) {
  input.addEventListener("paste", (e) => {
    for (const item of e.clipboardData?.items || []) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const f = item.getAsFile();
        if (f) {
          e.preventDefault();
          addPendingImageFile(f, pane);
        }
      }
    }
  });
}
bindComposerPaste(composerInput, null);
function bindComposerDrop(el, pane) {
  ["dragover", "drop"].forEach((evt) => {
    el.addEventListener(evt, (e) => {
      e.preventDefault();
      if (evt !== "drop") return;
      const dropped = [...(e.dataTransfer?.files || [])];
      if (!dropped.length) return;
      // 图片进附件托盘；其他文件转 @ 引用（浏览器 Drop 事件只给文件名不给路径，
      // 故按文件名匹配项目文件索引，命中即插入 @项目内相对路径）
      dropped.filter((f) => f.type.startsWith("image/")).forEach((f) => addPendingImageFile(f, pane));
      const others = dropped.filter((f) => !f.type.startsWith("image/"));
      if (others.length) attachDroppedAsMentions(others, pane);
    });
  });
}
const composerEl = document.getElementById("composer");
bindComposerDrop(composerEl, null);
async function attachDroppedAsMentions(files, pane) {
  const el = faceInputEl(pane);
  const index = await updateFileIndex();
  for (const f of files) {
    const name = f.name.toLowerCase();
    const hits = index.filter((p) => {
      const lp = p.toLowerCase();
      return lp === name || lp.endsWith("/" + name);
    });
    if (hits.length === 1) {
      const cur = el.value;
      const sep = cur && !/\s$/.test(cur) ? " " : "";
      el.value = cur + sep + "@" + hits[0] + " ";
    } else if (hits.length > 1) {
      addNotice(`「${f.name}」在项目里有 ${hits.length} 个同名文件，请输入 @ 手动选择要引用哪一个`);
    } else {
      addNotice(`「${f.name}」不在当前项目里——拖拽拿不到文件真实路径，` +
        "请点输入框左侧的「添加文件」按钮选它（支持项目外的文件与多选）。");
    }
  }
  autoGrowInput(el);
  el.focus();
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
    d._rtMeta = rtMeta; // 重新生成时据此重跑同样的协作配置
    if (rtMeta.mode === "team") addTeamBadge(d, rtMeta);
    else if (rtMeta.mode === "adversarial") addAdvBadge(d, rtMeta);
    else if (rtMeta.members) addRtBadge(d, rtMeta);
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
  // 跨项目只读分屏列：只留复制类操作——编辑/重发、分叉、回退、重新生成都会
  // 触达会话（后端归属校验虽兜得住，报的却是误导性的「session not found」）
  const ro = !!(tab && tab.readonly);
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
    if (!ro) mk("edit", "编辑这条消息并重新发送", () => msgEditResend(tab, el, seq));
    mk("snip", "存为提示词（存进设置 · 提示词，输入 ~ 可调用）",
      () => saveAsSnippet((getText && getText()) || el.textContent.trim()));
  } else {
    mk("copy", "复制整条回答（Markdown 原文）", async () => {
      await copyTextToClipboard(getText());
    });
    if (!ro) {
      mk("fork", "从这里分叉出新会话（不影响本会话）", () => msgFork(tab, seq));
      mk("rollback", "回退到提问前（删除这条回答及之后的内容）", () => msgRollback(tab, el, seq));
      mk("regen", "重新生成这条回答", () => msgRegenerate(tab, el, seq));
    }
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
// 在分批之上再做历史窗口化：后端切会话只下发最后 HISTORY_SYNC_LIMIT 条 brief
// （session.resume / session.peek_messages / boot / session.fork 同一窗口），
// 更早的靠聊天流顶部的「加载更早的消息」按钮按 HISTORY_CHUNK 经 WS
// messages.earlier 向前翻页——整会话的 brief 不再一帧进前端、常驻内存。
const HISTORY_SYNC_LIMIT = 200;
const HISTORY_CHUNK = 100;

// messages 只是服务端尾部窗口时，offset 是窗口之前还有多少条（0/缺省 = 没有更早）。
function historyOffsetOf(payload) {
  const off = payload && payload.messages_offset;
  return Number.isFinite(off) && off > 0 ? off : 0;
}

function renderHistory(tab, messages, offset) {
  const all = messages || [];
  // 渲染代际：期间又渲染过同一条聊天流（切标签 / 重进会话）时，旧的批处理作废
  const gen = (tab._historyGen = (tab._historyGen || 0) + 1);
  tab.logEl.innerHTML = "";
  tab.logEl._followBottom = true; // 重进会话从底部看起，跟随标记一并复位
  tab._toolCards = new Map(); // 在途工具卡随流清空，避免持已分离节点
  tab._historyAll = all; // 已加载的窗口（服务端只给了尾部一段）
  // 窗口起点取服务端口径 messages_offset（被截掉多少条；0/缺省 = 没有更早），
  // 不再从数组长度推——服务端只给了最后一段，本地没有「全量」可比。
  // 在途翻页随代际推进一并复位
  tab._historyStart = Math.max(0, Number.isFinite(offset) ? offset : 0);
  tab._historyLoading = false;
  const view = all;
  if (tab._historyStart > 0) addHistoryMoreBtn(tab);

  if (view.length <= HISTORY_SYNC_LIMIT) {
    paintHistorySlice(tab, view, 0, view.length);
    finishHistoryRender(tab, true);
    return;
  }

  // 先同步画头一段：openTabForSession 会在 renderHistory 返回后马上看
  // children.length 判断是否空会话，零节点会被误当成空会话而错误地弹欢迎页
  // （服务端窗口最多 HISTORY_SYNC_LIMIT 条，这条路径只在翻页累积后重渲染时走到）
  const first = Math.min(HISTORY_CHUNK, view.length);
  paintHistorySlice(tab, view, 0, first);
  tab.logEl.scrollTop = tab.logEl.scrollHeight;
  let i = first;
  const step = () => {
    if (gen !== tab._historyGen) return; // 已被更新的渲染取代
    // 追加前先看用户是不是在底部：在底部就继续跟随最新内容，
    // 用户已经往上翻了就不抢他的位置（分批期间尤其重要）
    const atBottom = tab.logEl.scrollTop + tab.logEl.clientHeight >= tab.logEl.scrollHeight - 4;
    const end = Math.min(i + HISTORY_CHUNK, view.length);
    paintHistorySlice(tab, view, i, end);
    if (atBottom) tab.logEl.scrollTop = tab.logEl.scrollHeight;
    i = end;
    if (i < view.length) requestAnimationFrame(step);
    else finishHistoryRender(tab, atBottom);
  };
  requestAnimationFrame(step);
}

// 窗口顶部「加载更早的消息」按钮：经 WS messages.earlier 向前翻一页，
// 画完保持视口停在原内容上。翻页期间按钮禁用（防重复点击翻重）
function addHistoryMoreBtn(tab) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "history-more";
  btn.textContent = `加载更早的消息（还有 ${tab._historyStart} 条）`;
  btn.onclick = () => { loadEarlierHistory(tab); };
  tab.logEl.appendChild(btn); // 先画按钮再画消息 → 它始终在最顶上
}

function loadEarlierHistory(tab) {
  if (!tab || !tab._historyAll || tab._historyStart <= 0) return Promise.resolve();
  // 在途翻页直接复用同一个 Promise：重复点击 / 搜索跳转轮询不会发出重复请求
  if (tab._historyLoading && tab._historyWait) return tab._historyWait;
  tab._historyLoading = true;
  const btn = tab.logEl.querySelector(".history-more");
  if (btn) btn.disabled = true;
  const gen = tab._historyGen || 0; // 期间 renderHistory 重跑则本次结果作废
  const oldest = tab._historyAll[0];
  const beforeSeq = oldest && oldest.seq != null ? oldest.seq : 0;
  tab._historyWait = (async () => {
    let r;
    try {
      r = await request("messages.earlier", {
        session_id: tab.sid, before_seq: beforeSeq, limit: HISTORY_CHUNK,
      });
    } catch (e) {
      addNotice("加载更早的消息失败: " + e.message);
      return;
    } finally {
      tab._historyLoading = false;
      const b = tab.logEl && tab.logEl.querySelector(".history-more");
      if (b) b.disabled = false;
    }
    if (gen !== (tab._historyGen || 0)) return; // 已被重渲染取代
    const page = (r && r.messages) || [];
    const offset = historyOffsetOf(r);
    if (!page.length) {
      // 更早的没有了（或 seq 边界已到底）：收掉按钮
      tab._historyStart = 0;
      if (btn && btn.isConnected) btn.remove();
      return;
    }
    const all = page.concat(tab._historyAll);
    tab._historyAll = all;
    const log = tab.logEl;
    const prevHeight = log.scrollHeight, prevTop = log.scrollTop;
    // addUser/addAssistantDone 一律 appendChild 到流末尾（.chat-log 是纵向 flex，
    // 追加即在底部）：先记住原内容第一个节点，画完把这批新节点搬到「加载更早」
    // 按钮之后、原内容之前，才是真正的顶部插入——否则更早的消息会画到新消息下面
    const moreBtn = log.querySelector(".history-more");
    const firstOld = moreBtn ? moreBtn.nextSibling : log.firstElementChild;
    const prevCount = log.children.length;
    paintHistorySlice(tab, all, 0, page.length);
    const added = Array.prototype.slice.call(log.children, prevCount);
    for (const node of added) log.insertBefore(node, firstOld);
    tab._historyStart = offset;
    // 内容插在顶部：滚动位置按新增高度下移补偿，视口里看到的内容不动
    log.scrollTop = prevTop + (log.scrollHeight - prevHeight);
    attachHistoryOps(tab); // 补画进来的用户消息也要挂复制/编辑（attachMsgOps 幂等）
    // 对话内查找开着且搜过词：补画进来的消息也纳入命中（否则用户点了「加载更早」
    // 也搜不到刚加载的内容，与查找条的“仅已加载”备注对不上）。这里不重走
    // paintFindHit 的 scrollIntoView——用户正在看的是刚补画的更早内容，
    // 跳回第一条命中会把他拽走；只更新计数与高亮态。
    const findBar = document.getElementById("find-bar");
    if (tab === activeTab && findBar && !findBar.classList.contains("hidden")) {
      const inp = document.getElementById("find-input");
      const q = inp ? inp.value.trim() : "";
      if (q) {
        const keep = findIdx;
        runFind(q);
        findIdx = findHits.length ? Math.min(Math.max(0, keep), findHits.length - 1) : -1;
        const count = document.getElementById("find-count");
        if (count) {
          findHits.forEach((m, i) => m.classList.toggle("cur", i === findIdx));
          count.textContent = (findHits.length ? `${findIdx + 1}/${findHits.length}` : "0/0")
            + findCoverageNote(tab);
        }
      }
    }
    if (moreBtn) {
      if (offset <= 0) moreBtn.remove();
      else moreBtn.textContent = `加载更早的消息（还有 ${offset} 条）`;
    }
  })();
  return tab._historyWait;
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
        // 对抗报告消息：先重建问题清单卡（默认折叠），再放报告本体
        if (m.adversarial && ((m.adversarial.findings || []).length || m.adversarial.stats)) {
          const advCard = buildAdvReplayCard(m.adversarial, m.seq, tab);
          curLog().appendChild(advCard);
          renderMermaidIn(advCard);
          highlightCodeIn(advCard);
        }
        // 圆桌融合消息：先重建成员草稿卡（默认折叠），再放回答本体
        if (m.roundtable && m.roundtable.mode === "roundtable" && (m.roundtable.members || []).length) {
          const rtCard = buildRtReplayCard(m.roundtable, tab, m);
          curLog().appendChild(rtCard);
          renderMermaidIn(rtCard);
          highlightCodeIn(rtCard);
        } else if (m.roundtable && m.roundtable.mode === "team" && m.roundtable.team) {
          // 团队纪要：团队跨多轮持续，同一团队的每一轮都带 meta——未终态的卡
          // 就地刷新快照（工单/频道/轮次取最新），终态后（或换队）才另起一张。
          // 有 team_id 时异步拉 team.log 把截断频道补成全量（三期，失败静默）
          const prev = (tab._lastTeamCard && tab._lastTeamCard.isConnected)
            ? tab._lastTeamCard : null;
          if (prev && !prev.classList.contains("ended")) {
            updateTeamCard(prev, m.roundtable.team);
            tcFetchFullLog(tab, prev, m.roundtable.team);
          } else {
            const tc = buildTeamReplayCard(m.roundtable, tab);
            tab._lastTeamCard = tc;
            curLog().appendChild(tc);
            renderMermaidIn(tc);
            highlightCodeIn(tc);
            tcFetchFullLog(tab, tc, m.roundtable.team);
          }
        }
        addAssistantDone(
          m.text, m.roundtable || m.adversarial, tab, m.seq, m.thinking,
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
    // 回填按发起面分流：截断的是这一栏的会话，编辑文本也必须回到这一栏的
    // 输入框——固定写主栏的话，回车就把截自列会话的内容发进主栏会话（串会话）
    const pane = splitPanes.includes(tab) ? tab : null;
    const input = pane ? pane.el.querySelector(".split-input") : document.getElementById("input");
    input.value = r.text || el.textContent.trim();
    input.focus();
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
    // 清掉这条回答对应的旧圆桌/对抗卡（历史回放里卡片紧挨在消息前面）。
    // 团队卡不在此列：它不紧挨纪要气泡（中间隔着频道消息），且团队可能仍在
    // 进行中——卡留在原地继续接事件，重新生成的轮次照常更新它。
    const prevEl = el && el.previousElementSibling;
    if (prevEl && prevEl.classList &&
        (prevEl.classList.contains("roundtable") || prevEl.classList.contains("adversarial"))) {
      prevEl.remove();
    }
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
      // 规划开关按发起面取：列内重新生成读该列自己的 planMode（每栏独立），
      // 主栏读工具条的全局值——读错的话列开着规划却按执行模式重跑
      plan_mode: splitPanes.includes(tab) ? !!tab.planMode : workMode === "plan",
    };
    // 圆桌回答的重新生成：沿用原配置重跑同样的圆桌（成员/融合或对比/辩论轮数）
    if (rtMeta && rtMeta.mode === "adversarial") {
      // 对抗报告的重新生成：按原角色名册逐角色重跑（裁判仍用当前主模型）
      params.adversarial = true;
      params.members = (rtMeta.roles || [])
        .filter((r) => r.role !== "judge" && r.provider)
        .map((r) => ({ provider: r.provider, model: r.model, role: r.role }));
    } else if (rtMeta && (rtMeta.members || []).length) {
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
    await request("chat.send", params).then((r) => {
      if (r && r.adversarial && r.adversarial_seq) {
        advAttachTransfer(tab, r.adversarial, r.adversarial_seq);
      }
    });
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
    openTabForSession(r.id, r.title, { withMessages: r.messages || [],
                                       messagesOffset: r.messages_offset });
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
    const ok = await confirmModal(
      `以下 ${(r.files || []).length} 个文件在保存该检查点之后又被修改过` +
      `（可能是其他并行任务写入）：\n${names}\n\n` +
      "强制恢复会用当时的旧内容覆盖这些改动，确定吗？",
      { title: "强制恢复检查点", okLabel: "强制恢复", danger: true }
    );
    if (!ok) throw new Error("已取消：文件有后续改动，未恢复");
    r = await request("checkpoint.restore", { id, force: true });
  }
  return r;
}

// 检查点条：本轮改动过文件时出现，可一键回滚到改前状态
// 轮次产物（检查点条 / 规划执行按钮）的落点：优先画进本轮所属面板的日志
// （tab.logEl），没有面板语境才落当前视图。多会话并行（分屏的核心场景）时
// chat.send 的响应返回前可能有任何面板的事件到达改写 routeTab，withTab 的
// finally 也会把它复位——按 curLog() 落会插进别人的聊天流、点击还触发别栏
// 的发送；归属面明确的东西必须跟着面板走。落点不是当前可见视图时不调
// scrollLog（后台内容不抢滚动，scrollLog 也只滚当前视图）。
function appendToTurnLog(node, tab) {
  const host = (tab && tab.logEl) || curLog();
  host.appendChild(node);
  if (host === curLog()) scrollLog();
}

function addCheckpointBar(cp, tab) {
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
      if (rightViewVisible("files")) loadFiles(true);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "↩ 撤销本轮改动";
      addNotice("回滚失败: " + e.message);
    }
  };
  bar.append(label, files, btn);
  appendToTurnLog(bar, tab);
}

// 规划模式结果：在计划下方放「按此计划执行」按钮。
// tab 按发起面分派：列内规划轮的执行按钮把计划回填这一栏的输入框并发送
// （模式与输入都是每栏独立的），主栏照旧走全局工具条与主输入框
function addPlanActions(plan, tab) {
  const bar = document.createElement("div");
  bar.className = "msg plan-actions";
  const btn = document.createElement("button");
  btn.className = "btn-primary";
  btn.textContent = "▶ 按此计划执行";
  btn.onclick = () => {
    bar.remove();
    if (tab && splitPanes.includes(tab)) {
      tab.planMode = false; // 批准计划 = 回到执行模式（该列自己的开关）
      syncSplitBarToggles(tab);
      const input = tab.el.querySelector(".split-input");
      input.value = "请严格按以下计划执行：\n\n" + plan;
      if (tab.send) tab.send();
      return;
    }
    setWorkMode("execute"); // 批准计划 = 回到执行模式
    saveComposerState(activeTab && activeTab.sid); // 模式切换写回当前会话条目
    document.getElementById("input").value =
      "请严格按以下计划执行：\n\n" + plan;
    send();
  };
  const hint = document.createElement("span");
  hint.className = "dim small";
  hint.textContent = " 满意就一键交给 Agent 执行；或继续补充需求。";
  bar.appendChild(btn);
  bar.appendChild(hint);
  appendToTurnLog(bar, tab);
}

document.getElementById("btn-send").onclick = send;
document.getElementById("btn-stop").onclick = () =>
  request("stop", activeTab && activeTab.sid ? { session_id: activeTab.sid } : {});

// ---------- 输入浮层：/ 命令 与 @ 文件提及（对标 Claude Code / Codex 输入体验） ----------
const inputEl = document.getElementById("input");

// 输入框随内容自动增高：最多 6 行，超出内部滚动；拖了 --cp-h 时输入区仍由 flex-grow 填满
const INPUT_MAX_LINES = 6;
function autoGrowInput(el) {
  el = el || inputEl;
  const cs = getComputedStyle(el);
  const lh = parseFloat(cs.lineHeight) || 21;
  const cap = Math.ceil(lh * INPUT_MAX_LINES +
    parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + 3); // +上下边框
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, cap) + "px";
  // 溢出永远 auto（此前内容 ≤ 上限时写 hidden）：--cp-h 比内容矮时 flex 会把
  // 输入框压得比 autoGrow 给的高度还小，hidden 会让超出的文字既看不见也滚不了；
  // auto 在装得下时不显示滚动条，被压缩时立刻可滚
  el.style.overflowY = "auto";
}
inputEl.addEventListener("input", () => autoGrowInput()); // 直接传函数引用会把事件对象当 el 传进去
autoGrowInput();

// 输入区宽度变化 → 重跑 autoGrow（2026-10-09）：boot 期布局未定时，长占位符
// 按错误宽度折行会把 textarea 内联高度顶到 6 行帽值——内容高度从此压过
// --cp-h（composer 高度 = max(--cp-h, 内容)），拖动手柄往下调完全无响应，
// 直到切换会话触发 restoreComposerState 才被纠正。观察宽度而非高度：
// autoGrow 只写高度不会自触发，无回环；侧栏/右面板/分屏拖宽、折叠、
// 窗口缩放等一切宽度变化都自动覆盖。
let _composerW = 0;
new ResizeObserver((entries) => {
  for (const en of entries) {
    const w = Math.round(en.contentRect.width);
    if (w && w !== _composerW) {
      _composerW = w;
      autoGrowInput();
    }
  }
}).observe(document.getElementById("composer"));

// ---------- 「& 引用对话」：输入 & 唤起会话选择器，选中的对话随消息发给后端注入上下文 ----------
const refMenu = document.getElementById("ref-menu");
const refTray = document.getElementById("ref-tray");
let pendingRefs = [];   // 已选引用 [{ id, title }]，随下一条消息发送（主栏；列的在 pane.refs）
let refCandidates = []; // 弹层候选（session.list，已排除当前会话）
let refMatches = [];    // 按 & 后的查询串过滤后的候选
let refPick = 0;        // 键盘高亮行
let refLoaded = false;  // 候选已拉取（弹层开着时继续输入只做本地过滤）
let refFace = null;     // 引用菜单当前服务哪一面（null = 主栏，否则是分屏 pane）
// 面参数（pane）：null = 主栏；& 菜单/托盘都落在焦点所在的那一面
function faceInputEl(pane) {
  return pane ? pane.el.querySelector(".split-input") : inputEl;
}
function faceRefTray(pane) {
  return pane ? pane.el.querySelector(".split-ref-tray") : refTray;
}
function faceRefs(pane) {
  return pane ? pane.refs : pendingRefs;
}

function renderRefTray(pane) {
  const tray = faceRefTray(pane);
  const list = faceRefs(pane);
  if (!tray) return;
  tray.classList.toggle("hidden", !list.length);
  tray.innerHTML = "";
  list.forEach((r, i) => {
    const chip = document.createElement("span");
    chip.className = "ref-chip";
    chip.title = "引用对话的内容会随这条消息一起给 Agent";
    chip.textContent = `🔗 ${r.title || "未命名会话"}`;
    const del = document.createElement("button");
    del.className = "ref-chip-del";
    del.textContent = "✕";
    del.title = "移除引用";
    del.onclick = () => { list.splice(i, 1); renderRefTray(pane); };
    chip.appendChild(del);
    tray.appendChild(chip); // 追加到目标栏自己的托盘（此前误写全局 refTray，分屏列的引用片一直挂到主栏去）
  });
}
function clearPendingRefs(pane) {
  if (pane) pane.refs = [];
  else pendingRefs = [];
  renderRefTray(pane);
}

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
function refTokenAt(pane) {
  const el = faceInputEl(pane);
  const pos = el.selectionStart ?? el.value.length;
  const m = el.value.slice(0, pos).match(/(?:^|\s)&([^&\s]*)$/);
  return m ? { start: pos - m[1].length - 1, query: m[1].toLowerCase() } : null;
}

function closeRefMenu() {
  refMenu.classList.add("hidden");
  refMenu.innerHTML = "";
  refLoaded = false;
  refCandidates = [];
}

function positionRefMenu(pane) {
  const r = faceInputEl(pane).getBoundingClientRect(); // 物理像素；弹层带 zoom，除回 uiScale
  refMenu.style.top = "auto";
  refMenu.style.bottom = (window.innerHeight / uiScale - r.top / uiScale + 6) + "px";
  refMenu.style.left = Math.min(r.left / uiScale, window.innerWidth / uiScale - 340) + "px";
}

function highlightRefPick() {
  const items = refMenu.querySelectorAll(".mm-item");
  items.forEach((el, i) => el.classList.toggle("active", i === refPick));
  if (items[refPick]) items[refPick].scrollIntoView({ block: "nearest" });
}

function paintRefMenu() {
  const tok = refTokenAt(refFace);
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

async function ensureRefMenu(pane) {
  refFace = pane || null;
  if (!refLoaded) {
    try {
      const r = await request("session.list");
      const cur = pane ? pane.sid : (activeTab && activeTab.sid);
      refCandidates = (r.sessions || []).filter((s) => s.id !== cur);
    } catch {
      refCandidates = [];
    }
    refLoaded = true;
  }
  positionRefMenu(pane);
  refMenu.classList.remove("hidden");
  paintRefMenu();
}

function pickRef(s) {
  const list = faceRefs(refFace);
  if (!list.some((r) => r.id === s.id)) {
    list.push({ id: s.id, title: s.title || "未命名会话" });
    renderRefTray(refFace);
  }
  const el = faceInputEl(refFace);
  const tok = refTokenAt(refFace);
  if (tok) {
    const pos = el.selectionStart ?? el.value.length;
    el.value = el.value.slice(0, tok.start) + el.value.slice(pos);
    el.setSelectionRange(tok.start, tok.start);
    autoGrowInput(el);
  }
  closeRefMenu();
  el.focus();
}

// 输入时唤起/收起引用菜单：主栏与分屏列的输入框都走这一句
function wireRefInput(el, pane) {
  el.addEventListener("input", () => {
    if (refTokenAt(pane)) ensureRefMenu(pane);
    else if (refFace === (pane || null)) closeRefMenu();
  });
  el.addEventListener("blur", () => {
    setTimeout(() => { if (refFace === (pane || null)) closeRefMenu(); }, 120);
  });
}
wireRefInput(inputEl, null);

// 键盘劫持挂在 document 捕获阶段：同一元素的监听按注册顺序执行，
// 捕获阶段才能抢在既有「Enter 发送」监听之前把引用态下的按键吃掉
document.addEventListener("keydown", (e) => {
  if (refMenu.classList.contains("hidden") || e.target !== faceInputEl(refFace)) return;
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
      !refMenu.contains(e.target) && e.target !== faceInputEl(refFace)) closeRefMenu();
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
let menuFace = null; // 输入菜单当前服务哪一面（null = 主栏）
function showInputMenu(items, emptyHint, face) {
  face = face === undefined ? menuFace : face;
  menuItems = items;
  menuActive = 0;
  menuFace = face || null;
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
  if (face) {
    // 分屏列：菜单脱离 #composer 的 absolute 定位语境，改按列输入框视口锚定
    const r = faceInputEl(face).getBoundingClientRect();
    const col = face.el.getBoundingClientRect();
    inputMenu.style.position = "fixed";
    inputMenu.style.top = "auto";
    inputMenu.style.bottom = (window.innerHeight / uiScale - r.top / uiScale + 6) + "px";
    inputMenu.style.left = (col.left / uiScale + 8) + "px";
    inputMenu.style.right = "auto";
    inputMenu.style.width = Math.max(280, Math.min(520, col.width / uiScale - 16)) + "px";
  } else {
    inputMenu.style.position = "";
    inputMenu.style.top = "";
    inputMenu.style.width = "";
    const composer = document.getElementById("composer");
    inputMenu.style.bottom = (composer.offsetHeight + 8) + "px";
    inputMenu.style.left = "";
    inputMenu.style.right = "";
  }
  inputMenu.classList.remove("hidden");
}
function pickMenuItem(i) {
  const it = menuItems[i];
  const face = menuFace;
  hideInputMenu();
  if (it) it.onPick();
  faceInputEl(face).focus();
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
  { cmd: "/todos", desc: "查看当前步骤清单" },
  { cmd: "/export", desc: "导出当前会话为 Markdown" },
  { cmd: "/save-skill", desc: "把本次会话的做法存成技能草稿" },
];

async function execSlash(cmd, face) {
  // 会话级命令按面取会话：列里敲 /export 导出的是这一栏的会话
  const slashSid = face ? face.sid : currentSessionId;
  switch (cmd) {
    case "/help":
      addNotice("可用命令：\n" + SLASH_COMMANDS.map((c) => `${c.cmd} — ${c.desc}`).join("\n") +
        "\n提示：输入 @ 可以引用项目里的文件（支持文件夹）；项目外的文件用输入框左侧的文件按钮选。\n" +
        "快捷键：\n" +
        "Ctrl+N 新建会话 · Ctrl+F 在本对话里查找 · Ctrl+Shift+F 搜索会话（可跨项目）\n" +
        "Ctrl+K 切换模型 · Ctrl+W 关闭标签 · Ctrl+Tab 切换标签 · Esc 停止运行 · Enter 发送 · Shift+Enter 换行\n" +
        "Ctrl+Alt+Space 全局热键（唤起窗口并预填剪贴板）\n" +
        "更多说明点右上角「？」看帮助。");
      break;
    case "/new": {
      newSessionFromHighlight();
      break;
    }
    case "/compact": {
      if (face) { addNotice("/compact 作用于主栏当前会话；要压缩这一栏的上下文，请把会话切到主栏后操作"); break; }
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
        addNotice("当前没有步骤清单。给 Agent 一个多步任务，它会用 todo_write 维护步骤。");
        break;
      }
      const icon = { pending: "○", in_progress: "◐", completed: "●" };
      addNotice("步骤清单：\n" + r.todos.map((t) => `${icon[t.status] || "○"} ${t.content}`).join("\n"));
      break;
    }
    case "/export":
      if (!slashSid) { addNotice("当前还没有会话可导出。"); break; }
      request("session.export", { id: slashSid })
        .then((r) => downloadText(r.filename, r.markdown))
        .catch((e) => addNotice("导出失败: " + e.message));
      request("session.export", { id: slashSid, fmt: "html" })
        .then((r) => downloadText(r.filename, r.html))
        .catch(() => {}); // HTML 版失败不打扰（MD 版已成功）
      break;
    case "/save-skill":
      saveSkillDraftModal();
      break;
  }
}

async function updateFileIndex() {
  if (fileIndex && Date.now() - fileIndex.ts < 30000) return fileIndex.files;
  const r = await request("fs.files").catch(() => ({ files: [] }));
  fileIndex = { files: r.files || [], ts: Date.now() };
  return fileIndex.files;
}

function updateInputMenu(face) {
  const el = faceInputEl(face);
  menuFace = face || null;

  const caret = el.selectionStart;
  const before = el.value.slice(0, caret);
  // ~ 唤起提示词菜单（原「快捷指令」，自带内置示例兜底）。
  // 查询段用 \S*：提示词名是中文，\w 匹配不到 CJK；空格视为结束（收起菜单）。
  // 过滤面：名称 / 内容子串 + 名称拼音首字母（如 xzb ↔ 写周报）；候选上限 12。
  const tilde = before.match(/^~(\S*)$/);
  if (tilde) {
    const q = tilde[1].toLowerCase();
    const items = snippetCandidates()
      .filter((s) => !q || snippetMatch(s, q))
      .slice(0, 12)
      .map((s) => ({
        label: "◆ " + s.name,
        desc: snippetsCache.includes(s) ? "提示词 · 选中即插入" : "内置示例 · 选中即插入",
        onPick: () => {
          // ~ 查询串只是唤起器，不属于消息内容：插入前把它从输入框里去掉
          el.value = el.value.replace(/^~[^\n]*/, "");
          insertSnippet(s, face);
        },
      }));
    // 顺手把写了一半的输入存成模板，不用绕去设置页新建
    items.push({
      label: "＋ 存为提示词",
      desc: "把当前输入框内容保存为新提示词",
      onPick: () => {
        const text = el.value.replace(/^~[^\n]*/, "");
        el.value = text;
        saveAsSnippet(text);
        hideInputMenu();
      },
    });
    const sortHint = snippetsSort === "top" ? "常用优先" : "手动顺序（设置页可切换）";
    showInputMenu(items, items.length
      ? `（回车或点击插入；~ 后接名称/内容/拼音首字母过滤 · 当前：${sortHint}）`
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
        onPick: () => { el.value = ""; autoGrowInput(el); execSlash(c.cmd, face); },
      }));
    showInputMenu(items, "（没有匹配的命令，回车仍会作为消息发送）");
    return;
  }
  const at = before.match(/@([^\s@\\/]*)$/);
  if (at) {
    updateFileIndex().then((files) => {
      if (!/@([^\s@\\/]*)$/.test(el.value.slice(0, el.selectionStart))) return;
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
            el.setRangeText("@" + f + " ", range[0], range[1], "end");
            autoGrowInput(el);
          },
        };
      }), "（项目里没有匹配的文件）");
    });
    return;
  }
  hideInputMenu();
}
inputEl.addEventListener("input", () => updateInputMenu(null));
inputEl.addEventListener("blur", () => setTimeout(hideInputMenu, 120));

function composerKeydown(e, face) {
  const el = faceInputEl(face);
  const menuOpen = !inputMenu.classList.contains("hidden");
  // Alt+1..8 直插候选（与 ~ 菜单同一候选池：启用中的提示词按当前排序 + 内置兜底）；
  // 不抢菜单打开时的 Enter/Tab 语义，数字键也没有 IME 组词冲突
  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.isComposing &&
      /^[1-8]$/.test(e.key)) {
    const cand = snippetCandidates()[Number(e.key) - 1];
    if (cand) { e.preventDefault(); insertSnippet(cand, face); }
    return;
  }
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
  if (!face && !menuOpen && !e.isComposing && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (e.key === "ArrowUp" && (el.value === "" || histActive())) {
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
      el.value = inputHistory.draft;
      autoGrowInput();
      return;
    }
  }
  if (e.key === "Enter" && !e.isComposing) {
    // 发送键可配（ui.json 的 ctrl_enter_send）：默认 Enter 发送 / Shift+Enter 换行；
    // 开启后 Ctrl+Enter 发送 / Enter 一律换行（不再吞 Shift 语义）。
    if (ctrlEnterSend) {
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); if (face) face.send(); else send(); }
      return; // 其余组合（含裸 Enter）交给浏览器换行
    }
    if (!e.shiftKey) { e.preventDefault(); if (face) face.send(); else send(); }
  }
}
document.getElementById("input").addEventListener("keydown", (e) => composerKeydown(e, null));
document.getElementById("btn-new").onclick = () => {
  if (pageViewOpen()) backToChat(); // 工厂 / 设置是整屏视图：先回对话再新建
  newSessionFromHighlight();
};
window.addEventListener("keydown", (e) => {
  if (!e.ctrlKey && !e.metaKey) {
    // Esc 的优先级：弹窗 → 工厂（先收抽屉）→ 设置 → 停止运行。
    // 弹窗自己的 document 监听会处理取消；这里只负责视图层。
    if (e.key === "Escape") {
      if (modalOpen()) return;
      if (factoryOpen) {
        e.preventDefault();
        if (typeof factoryEscape === "function" && factoryEscape()) return; // 抽屉开着：先关抽屉
        closeFactoryPage();
        return;
      }
      if (settingsOpen) {
        e.preventDefault();
        backToChat();
        return;
      }
      // 停止运行：设置页/弹窗打开时不劫持——它们有自己的 Esc 语义。
      // 停谁按键盘焦点定，不按 routeTab：那是事件路由的残留指针，分屏列在跑
      // 时它常指向那一列、人却在主栏——按残留停会停错会话，且列里没有停止
      // 按钮无从纠正。焦点在分屏列内停那一列，否则停主栏活动标签。
      const ae = document.activeElement;
      const colEl = ae && ae.closest ? ae.closest(".split-col") : null;
      const pane = colEl ? splitPanes.find((p) => p.el === colEl) : null;
      const target = pane || activeTab;
      if (target && target.running) {
        e.preventDefault();
        if (target.sid) request("stop", { session_id: target.sid }).catch(() => {});
      }
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
    if (!pageViewOpen()) openSettingsSearch();
    return;
  }
  if (e.shiftKey || e.altKey) return;
  // Ctrl+N 新建会话
  if (k === "n") {
    e.preventDefault();
    if (!typing && !pageViewOpen()) document.getElementById("btn-new").click();
    return;
  }
  // Ctrl+F 在本对话里查找（所有编辑类软件的惯例；会话搜索见 Ctrl+Shift+F）
  if (k === "f" && !pageViewOpen()) {
    e.preventDefault();
    if (!settingsOpenFlag()) openFindBar();
    return;
  }
  // Ctrl+K 全局命令面板（见文件尾「全局命令面板」段）：全局可用——设置 /
  // 工厂开着、焦点在输入框里都响应，面板自己收焦点。原「模型切换菜单」
  // 快捷键让位，菜单仍可点输入区模型徽章打开
  if (k === "k") {
    e.preventDefault();
    togglePalette();
    return;
  }
  // Ctrl+B 左侧栏折叠/展开（对标 VS Code 的侧栏开关；输入框聚焦时也响应）
  if (k === "b") {
    e.preventDefault();
    toggleLeftSidebar();
    return;
  }
  // Ctrl+W 关闭当前会话标签
  if (k === "w" && !pageViewOpen()) {
    e.preventDefault();
    if (!typing && activeTab) closeTab(activeTab);
    return;
  }
  // Ctrl+Tab / Ctrl+Shift+Tab 在会话标签间循环切换（按展示序，即拖拽后的顺序）
  if (k === "Tab" && (e.ctrlKey || e.metaKey) && !pageViewOpen()) {
    e.preventDefault();
    cycleTab(e.shiftKey ? -1 : 1);
    return;
  }
});

// 标签循环切换：方向 +1 下一个 / -1 上一个，到头绕回。按 DOM 序找（renderTabs
// 每次按展示序重建，DOM 序就是用户看到的顺序），再映射回 chatTabs 对象激活
function cycleTab(dir) {
  const els = [...document.querySelectorAll('#chat-tabs [role="tab"]')];
  if (els.length < 2) return;
  const idx = els.findIndex((el) => el.classList.contains("active"));
  const t = chatTabs.find((x) => x.el === els[(idx + dir + els.length) % els.length]);
  if (t) activateTab(t);
}

function modalOpen() {
  return !document.getElementById("modal").classList.contains("hidden");
}
// 对话视图是否被设置 / 工厂两个顶层视图遮住（Ctrl+F 等快捷键的守卫读这里）
function settingsOpenFlag() { return pageViewOpen(); }

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

/** 查找覆盖范围备注：窗口化后只搜到已画出的消息。更早区间未加载时
 * 在计数旁注明（含条数），引导用户用顶部「加载更早的消息」。 */
function findCoverageNote(tab) {
  if (!tab || !tab._historyStart) return "";
  return `（仅已加载；还有 ${tab._historyStart} 条更早消息，用顶部按钮加载后可继续搜）`;
}

function paintFindHit() {
  const { count } = findEls();
  findHits.forEach((m, i) => m.classList.toggle("cur", i === findIdx));
  const note = findCoverageNote(activeTab);
  if (!findHits.length) { count.textContent = "0/0" + note; return; }
  count.textContent = `${findIdx + 1}/${findHits.length}` + note;
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
      <p>涉及写文件、执行命令时它会先弹确认条（<b>允许一次 / 总是允许（本项目） / 拒绝</b>）；
      只读操作自动放行。想少点确认，可点输入框工具条上的权限按钮（盾牌图标）在三档间切换：
      「自动编辑」只自动放行工作目录内的文件写入，目录外的写入与执行命令仍会征询；
      「完全访问」连执行命令也不再征询，仅在完全信任任务的环境里使用。</p>
    </div>
    <div class="help-sec">
      <h4>③ 常用开关</h4>
      <ul>
        <li><b>⚡ 规划模式</b>：先出方案再动手，适合大改动</li>
        <li><b>👥 圆桌</b>：一条消息让多个模型同时作答再融合，难题更稳</li>
        <li><b>接力</b>：主屏与副屏使用各自的会话交替处理任务，每轮可指定执行屏幕；它与输入框里的圆桌、团队、对抗是分开的。</li>
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

// ---------- 左栏功能导航（工厂看板 / 任务 / 日程 / 接力 / 自动化 / 项目记忆 / 记忆地图 / MCP·Skills） ----------
// 顺序按使用频次排，与 index.html 的 .side-menu、.rail-btn 图标条两处保持一致（改一处必须同步改另外两处）
// 工厂看板是与「新建会话」并列的主操作（#btn-factory，同一身 .nav-new 皮），已不在 .side-menu 里，
// 但动作分发仍只在这一处：选择器把两个都收进来，别在别处再挂一遍 onclick
const navPending = {}; // 已全部上线，保留结构便于扩展

document.querySelectorAll(".nav-item, #btn-factory").forEach((btn) => {
  btn.onclick = () => {
    const act = btn.dataset.act;
    // 窄屏上侧栏是抽屉：点了导航就收回（任务等打开的是右侧面板标签，
    // 不收回的话侧栏（z 更高）正好盖住刚打开的面板）
    document.body.classList.remove("sidebar-open");
    // 工厂是第三顶层视图：从对话 / 设置任一处都能直接进
    if (act === "factory") return openFactoryPage();
    // 其余导航项都在对话视图里生效：工厂开着时先回对话
    if (factoryOpen) backToChat();
    // 任务/日程/自动化：右侧面板标签（不再用侧栏折叠区）
    if (act === "todo") return openRightTab("todo");
    if (act === "agenda") return openRightTab("agenda");
    if (act === "auto") return openRightTab("auto"); // 自动化 = 定时任务 + 任务编排（容器标签，默认落定时任务分段）
    if (act === "memory") return openRightTab("memory");
    if (act === "map") return openRightTab("map");
    // 接力：右侧看板 + 在当前会话下打开副屏分栏（入口统一走 openRelayWorkbench）
    if (act === "relay") return openRelayWorkbench();
    if (act === "project") return projectModal();
    if (act === "ext") return openRightTab("ext");
    if (navPending[act]) addNotice(`「${navPending[act]}」开发中，即将上线`);
  };
});

// 工厂是整屏视图：点侧栏里的会话/项目行时先回对话，否则点了看不见结果。
// 捕获阶段挂一次即可（列表内容会被重建，容器本身不动）。
["session-list", "project-list"].forEach((id) => {
  const box = document.getElementById(id);
  if (box) box.addEventListener("click", () => { if (factoryOpen) backToChat(); }, true);
});

document.getElementById("btn-agenda-add").onclick = () => agendaModal(null);

// 视图切换与周/月导航（周网格 / 月网格 / 分组列表）
document.querySelectorAll("#ag-view-seg button").forEach((b) => {
  b.onclick = () => { agView = b.dataset.v; renderAgendaView(); };
});
document.getElementById("ag-list-btn").onclick = () => { agView = "list"; renderAgendaView(); };
document.getElementById("ag-prev").onclick = () => shiftAgenda(-1);
document.getElementById("ag-next").onclick = () => shiftAgenda(1);
document.getElementById("ag-today").onclick = () => { agAnchor = new Date(); renderAgendaView(); };

document.getElementById("btn-cron-add").onclick = () => cronModal(null);

document.getElementById("btn-pipeline-add").onclick = () => pipelineCreateModal();
document.getElementById("btn-pipeline-import").onclick = () => pipelineImportModal({});

// ---------- 项目记忆：右侧面板编辑（AGENTS.md，保存即注入每一轮对话） ----------
// 原来是弹窗；改成与日程/定时任务同款的右面板标签——编辑约定时还能同时看着对话。
const rpMemoryText = document.getElementById("rp-memory-text");
let memoryLoaded = false; // 与项目绑定：切项目后由 resetProjectPanels 置回 false
let rpMemoryMtime = 0; // 加载时的文件 mtime：保存时带回比对，防覆盖后台重写的 AGENTS.md
let rpMemoryOrig = ""; // 加载时的原文：重读前判断「有没有未保存的修改」用

// 成功类状态提示的统一收尾：6 秒后自动收起（一次性确认，不必手动清）；
// 错误保留，留时间读原因，直到下一次操作覆盖。计时器挂在元素自身上，
// 同一函数管多条状态栏（如子代理页的内置/自定义）时互不干扰
function autoHideStatus(el, text, ok) {
  clearTimeout(el._statusTimer);
  if (text && ok) {
    el._statusTimer = setTimeout(() => { el.hidden = true; el.textContent = ""; }, 6000);
  }
}

function memoryStatus(text, ok = true) {
  const el = document.getElementById("memory-status");
  el.textContent = text;
  el.className = "rp-memory-status" + (ok ? "" : " bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

async function loadMemoryPanel(force = false) {
  if (memoryLoaded && !force) return;
  try {
    const r = await request("project.instructions");
    rpMemoryText.value = r.text || "";
    rpMemoryMtime = r.mtime || 0;
    rpMemoryOrig = rpMemoryText.value;
    memoryLoaded = true;
    memoryStatus((r.path
      ? `记忆文件：${r.path}`
      : "本项目还没有记忆文件，保存时会自动创建 AGENTS.md") +
      (r.encoding_text ? ` · ${r.encoding_text}` : "") +
      (r.editable === false ? " · 只读（编码无法识别，改了存不回去）" : ""));
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
    rpMemoryOrig = rpMemoryText.value; // 已落盘：重读不再视为「有未保存修改」
    if (res.truncated) {
      // 截断不再静默：明说超了多少、文件里留了多少
      memoryStatus(
        `⚠ 已保存，但超出 ${res.limit} 字上限：只保留前 ${res.chars} 字，` +
        `后 ${res.original_chars - res.chars} 字没有写入 → ${res.path}`, false);
      addNotice(`项目记忆超出 ${res.limit} 字上限，超出部分未保存`);
    } else {
      memoryStatus(`✓ 已保存（${res.chars} 字）→ ${res.path}，下一轮对话即生效`);
      addNotice(`项目记忆已保存（${res.chars} 字）→ ${res.path}`);
    }
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
document.getElementById("memory-reload").onclick = async () => {
  // 重读会丢掉未保存的修改：编辑框与加载基线不一致时先确认（与文件编辑器同一套）
  if (memoryLoaded && rpMemoryText.value !== rpMemoryOrig &&
      !(await confirmModal("放弃修改并重读？",
        "<p>项目记忆有未保存的修改，重读会用磁盘上的内容覆盖编辑框。</p>",
        "放弃修改并重读"))) {
    return;
  }
  loadMemoryPanel(true);
};
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

// ---------- 设置页 / 工厂页：视图切换（侧栏同步切换为设置子项目） ----------
const viewChat = document.getElementById("view-chat");
const viewSettings = document.getElementById("view-settings");
const viewFactory = document.getElementById("view-factory");
const sideChat = document.getElementById("side-chat");
const sideSettings = document.getElementById("side-settings");
const btnSettings = document.getElementById("btn-settings");
let settingsOpen = false;
// 第三顶层视图「工厂」。与 settingsOpen 互斥；快捷键守卫统一读 pageViewOpen()，
// 避免逐处再加一个标志（工厂 / 设置都不是对话视图）。
let factoryOpen = false;
function pageViewOpen() { return settingsOpen || factoryOpen; }

function openSettings(page = "providers") {
  closeExtPanel(); // MCP/Skills 卡片若正被右面板借用，先搬回来，技能/MCP 两页才不会是空的
  closeFactoryView(); // 从工厂直接进设置：先卸掉场景循环、让出视图
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

// 只负责「把工厂视图收起来」：不碰设置页状态，供 openSettings / backToChat 共用
function closeFactoryView() {
  if (!factoryOpen) return;
  factoryOpen = false;
  if (typeof factoryUnmount === "function") factoryUnmount();
  viewFactory.classList.add("hidden");
}

function openFactoryPage() {
  if (factoryOpen) return;
  if (settingsOpen) {
    // 从设置页进工厂：把设置视图的收尾走一遍（但不调用 petSettle 之外的对话刷新）
    settingsOpen = false;
    viewSettings.classList.add("hidden");
    sideSettings.classList.add("hidden");
    sideChat.classList.remove("hidden");
    btnSettings.textContent = "⚙ 设置";
  }
  factoryOpen = true;
  viewChat.classList.add("hidden");
  viewSettings.classList.add("hidden");
  viewFactory.classList.remove("hidden");
  document.body.classList.remove("sidebar-open"); // 窄屏：侧栏抽屉别盖住场景
  // 视图必须已经可见再 mount：Canvas 要按真实尺寸算内部缓冲
  if (typeof factoryMount === "function") factoryMount();
}

function closeFactoryPage() {
  if (!factoryOpen) return;
  closeFactoryView();
  viewChat.classList.remove("hidden");
  document.body.classList.remove("sidebar-open");
  petSettle();
}

function backToChat() {
  settingsOpen = false;
  viewSettings.classList.add("hidden");
  closeFactoryView(); // 工厂 → 对话：场景循环一并停掉
  viewChat.classList.remove("hidden");
  document.body.classList.remove("sidebar-open"); // 手机：返回对话同样先收回抽屉
  sideSettings.classList.add("hidden");
  sideChat.classList.remove("hidden");
  btnSettings.textContent = "⚙ 设置";
  // 对话区重新可见：地面回来了，小羊按新地面归位（设置页期间位置一直冻结着）
  petSettle();
}
btnSettings.onclick = () => (settingsOpen ? backToChat() : openSettings());
document.getElementById("factory-close").onclick = () => closeFactoryPage();

// ---------- 接力工作台：作为右侧面板标签与聊天并排显示 ----------
function openRelayWorkbench() {
  if (pageViewOpen()) backToChat();
  if (!document.getElementById("rp-page-relay")) return openSettings("skills"); // 接力说明卡在技能与工具页
  openRightTab("relay");
  // 点「接力」= 在当前会话开一个副屏：把绑定会话打开在右侧分栏里
  if (typeof relayOpenWithPeer === "function") relayOpenWithPeer();
}

document.getElementById("relay-open-workbench").onclick = openRelayWorkbench;

// 设置子项目：点击导航切换独立页面
function showSettingsPage(target) {
  document.querySelectorAll("#settings-nav li").forEach((x) =>
    x.classList.toggle("active", x.dataset.target === target)
  );
  document.querySelectorAll(".settings-page").forEach((pg) =>
    pg.classList.toggle("hidden", pg.id !== "settings-page-" + target)
  );
  if (target === "runs") loadRunsPage();
  if (target === "usage") loadUsage();
  if (target === "memory") loadMemoryPage();
  if (target === "archive") loadArchivePage();
  if (target === "browser") loadBrowserPage();
  if (target === "snippets") loadSnippets();
  if (target === "remote") { loadLanPanel(); loadTsPanel(); loadChannelPanel(); }
  if (target === "skills") { loadToolControl(); renderTeamCfg().catch(() => {}); }
  if (target === "subagents") renderSubagentCfg().catch(() => {});
  if (target === "advanced") { renderAdvancedCfg().catch(() => {}); renderHooksCfg(); }
  if (target === "mods") renderModsCfg().catch(() => {});
  if (target === "about") { loadBackups().catch(() => {}); renderRetentionCfg().catch(() => {}); renderStorageCard().catch(() => {}); }
}
document.querySelectorAll("#settings-nav li[data-target]").forEach((li) => {
  li.onclick = () => {
    document.body.classList.remove("sidebar-open"); // 手机：切换子页前先收回侧栏抽屉
    showSettingsPage(li.dataset.target);
  };
});

// ---------- 设置内搜索：按卡片标题与正文过滤，点结果跳到对应页并高亮那张卡 ----------
// 38 张卡片没有分组锚点时找一项要靠翻页；搜索框在页顶常驻，输入即出结果。
const settingsSearchInput = document.getElementById("settings-search");
const settingsSearchResults = document.getElementById("settings-search-results");

// 卡片索引缓存：索引项持有 card 元素引用与全量正文（textContent）。设置卡片是
// 静态 HTML（除非设置页被重渲染，内容不変），每键全量重扫 48 张卡（读 textContent）
// 纯属浪费——首次构建后缓存复用；失效靠 renderSettings 后主动清除（见
// invalidateSettingsCardIndex）。
let _settingsCardIndexCache = null;

function invalidateSettingsCardIndex() {
  _settingsCardIndexCache = null;
}

function settingsCardIndex() {
  if (_settingsCardIndexCache) return _settingsCardIndexCache;
  const items = [];
  // 面板打开时 MCP / 技能卡片被搬进右面板，不再挂在 .settings-page 下，
  // 单扫 .settings-page 会让设置搜索漏掉它们——把面板里的两个容器也当页收进来
  const pages = [...document.querySelectorAll(".settings-page")]
    .map((pg) => [pg, pg.id.replace("settings-page-", "")]);
  const SLOT_PAGES = { "rp-view-mcp": "mcp", "rp-view-skills": "skills" };
  Object.entries(SLOT_PAGES).forEach(([id, pageId]) => {
    const el = document.getElementById(id);
    if (el) pages.push([el, pageId]);
  });
  pages.forEach(([pg, pageId]) => {
    const nav = document.querySelector(`#settings-nav li[data-target="${pageId}"]`);
    const pageName = nav ? nav.textContent.trim() : pageId;
    // 收卡范围：直接子卡 + 包在中间层里的卡。技能页的卡片挂在 #skill-list-view /
    // #skill-manage-view 下（两视图互斥切换），只扫 :scope > .settings-card 会整页漏掉
    const containers = [pg, ...pg.querySelectorAll(":scope > div")];
    containers.forEach((box) => {
      // 分组标题只影响「正文」匹配，不单独成为索引项：它没有自己的卡片，
      // 点进去该滚到哪一张并不明确。这样搜「多模型协作」能把该组下所有卡片
      // 都作为候选列出（每张卡的 text 里带上所属分组名）
      let groupTitle = "";
      [...box.children].forEach((el) => {
        if (el.classList.contains("settings-group-title")) {
          groupTitle = el.textContent.trim();
          return;
        }
        if (!el.classList.contains("settings-card")) return;
        const h3 = el.querySelector("h3");
        items.push({
          pageId, pageName, card: el,
          title: h3 ? h3.textContent.trim() : "(未命名卡片)",
          // 预小写全量正文：搜索时不再每键逐卡转小写（重复工作）
          text: ((groupTitle ? groupTitle + " " : "") + (el.textContent || "")).toLowerCase(),
        });
      });
    });
  });
  _settingsCardIndexCache = items;
  return items;
}

function hideSettingsSearch() {
  settingsSearchResults.classList.add("hidden");
}

function runSettingsSearch(qs) {
  const q = (qs || "").trim().toLowerCase();
  settingsSearchResults.innerHTML = "";
  if (!q) { hideSettingsSearch(); return; }
  const hits = settingsCardIndex().filter((it) =>
    it.title.toLowerCase().includes(q) || it.text.includes(q));
  if (!hits.length) {
    settingsSearchResults.innerHTML = '<div class="ssr-empty dim small">没有匹配的设置项</div>';
  }
  hits.slice(0, 12).forEach((it) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ssr-item";
    b.innerHTML = `<b>${escapeHtml(it.title)}</b><span class="dim small">${escapeHtml(it.pageName)}</span>`;
    b.onclick = () => {
      hideSettingsSearch();
      settingsSearchInput.value = "";
      if (settingsOpen) showSettingsPage(it.pageId);
      else openSettings(it.pageId);
      // 目标卡滚进可视区并闪烁提示（等页面切换渲染完再滚）
      setTimeout(() => {
        it.card.scrollIntoView({ block: "center", behavior: "smooth" });
        it.card.classList.remove("ssr-flash");
        void it.card.offsetWidth; // 强制回流以重启动画
        it.card.classList.add("ssr-flash");
        setTimeout(() => it.card.classList.remove("ssr-flash"), 1600);
      }, 60);
    };
    settingsSearchResults.appendChild(b);
  });
  settingsSearchResults.classList.remove("hidden");
}
if (settingsSearchInput) {
  // 输入防抖：每次击键都全量过滤与重建结果列表，连续输入时逐键删建 DOM 全属浪费
  // （与侧栏搜索/对话内查找同一配方）；索引本身已在 settingsCardIndex 里缓存。
  let settingsSearchTimer = null;
  settingsSearchInput.addEventListener("input", () => {
    clearTimeout(settingsSearchTimer);
    settingsSearchTimer = setTimeout(
      () => runSettingsSearch(settingsSearchInput.value), 120);
  });
  // 聚焦即重建索引：设置页开着时另一端/对话中可能刚改过内容（如白名单新增），
  // 索引缓存可能过时；一次聚焦重建一次，输入突发期间全部命中缓存。
  settingsSearchInput.addEventListener("focus", () => invalidateSettingsCardIndex());
  settingsSearchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // 搜索框里的 Esc 先清检索，不再冒泡成「关闭设置页」
      e.stopPropagation();
      settingsSearchInput.value = "";
      hideSettingsSearch();
    }
  });
  document.addEventListener("click", (e) => {
    if (e.target !== settingsSearchInput && !settingsSearchResults.contains(e.target)) {
      hideSettingsSearch();
    }
  });
}

document.getElementById("btn-provider-back").onclick = closeProviderDetail;

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

document.getElementById("btn-rules-refresh").onclick = async () => {
  // 手动重读（renderSettings 会重新拉 whitelist.list）：另一端或 CLI 刚改过规则时用
  await renderSettings();
  showRulesStatus("已刷新白名单", null);
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

document.getElementById("btn-rule-check").onclick = ruleCheckRun;
document.getElementById("rule-check-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    ruleCheckRun();
  }
});

document.getElementById("btn-add-provider").onclick = addProviderModal;

// 两个视图（总览卡片 / 技能页）各有一组同义按钮：导入动作完全一致，按 ID 合并绑定；
// 「本机现存」两处行为不同（总览=先进技能页再探测，技能页=再点一次即收起），各自绑定
document.querySelectorAll("#btn-import-skill, #btn-import-skill-2").forEach((b) => {
  b.onclick = () => importSkillModal();
});
document.getElementById("btn-scan-skill").onclick = () => {
  // 总览卡片的按钮：先进入技能页再拉取，候选始终只在技能页内平铺
  openSkillManage();
  loadLocalSkills();
};
document.getElementById("btn-scan-skill-2").onclick = () => {
  // 已展开时再点一次即收起（与面板里的「收起」等效），否则探测展开
  const panel = document.getElementById("skill-local-panel");
  if (panel && !panel.hidden) { panel.hidden = true; return; }
  loadLocalSkills();
};
document.getElementById("btn-skill-local-import").onclick = () => importLocalSkills();
bindLocalSkillToolbar();

document.getElementById("btn-import-mcp").onclick = () => importMcpModal();
document.getElementById("btn-add-mcp").onclick = addMcpModal;

// 热力图不再按底栏宽度自适应（固定可视 4 周起步 + 横向滚动，列数随项目
// 周龄增长），按宽度重铺的 ResizeObserver 已随宽度优先方案移除

// 记忆地图的工具条事件（选择器/视图切换/生成按钮/自动开关）
document.getElementById("map-project").addEventListener("change", (e) => {
  mapState.projectId = parseInt(e.target.value, 10) || 0;
  mapState.fileFilter = null;
  loadMemoryMap();
});
document.getElementById("map-range").addEventListener("change", (e) => {
  mapState.range = e.target.value;
  loadMemoryMap();
});
document.querySelectorAll("#map-segbar button").forEach((b) => {
  b.onclick = () => {
    mapState.view = b.dataset.view;
    renderMapChrome();
    renderMapView();
    renderMapAside();
  };
});
document.getElementById("map-gen").addEventListener("click", async () => {
  if (mapState.genBusy) return;
  mapState.genBusy = true;
  updateMapGenBtn();
  try {
    const params = {};
    if (mapState.projectId) params.project_id = mapState.projectId;
    const r = await request("map.generate", params);
    if (r && r.started === false) {
      mapState.genBusy = false;
      updateMapGenBtn();
      addNotice("🗺 " + (r.reason || "已有一个摘要在生成中"));
    }
  } catch (e) {
    mapState.genBusy = false;
    updateMapGenBtn();
    addNotice("🗺 生成失败：" + e.message);
  }
});
document.getElementById("map-auto").addEventListener("click", async () => {
  const cur = !!(mapState.data && mapState.data.config && mapState.data.config.auto_digest);
  try {
    const r = await request("map.save_config", { auto_digest: !cur });
    if (mapState.data) mapState.data.config = { auto_digest: !!r.auto_digest };
    renderMapChrome();
    addNotice(!cur ? "🗺 已开启自动生成演化摘要" : "🗺 已关闭自动生成演化摘要");
  } catch (e) {
    addNotice("🗺 设置失败：" + e.message);
  }
});
// 记忆地图的反馈入口：与 设置 · 关于「反馈问题」共用同一套流程（openFeedbackPage），
// 这里没有 diag-msg 状态行，结果走通知条
document.getElementById("map-feedback").addEventListener("click", async () => {
  addNotice("🗺 正在生成诊断包并打开反馈页…");
  try {
    await openFeedbackPage();
    addNotice("🗺 已生成诊断包并打开反馈页——把诊断包 zip 拖进附件，描述问题即可");
  } catch (e) {
    addNotice("🗺 打开反馈页失败：" + e.message + "（可手动访问：" + REPO_PAGE + "/issues）");
  }
});

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
  // 自动化（定时任务 + 任务编排 的容器标签）：闪电＝「不用盯着、自己会跑」
  auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 2.5 5.5 13h5L10 21.5 18.5 11h-5.2z"/></svg>',
  pipeline: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5.5" cy="6" r="2.1"/><circle cx="5.5" cy="18" r="2.1"/><circle cx="18.5" cy="12" r="2.1"/><path d="M7.4 6.9 16.6 11.1M7.4 17.1 16.6 12.9"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3.5" width="14" height="17" rx="2"/><path d="M9 3.5v17"/><path d="M12.5 8h4M12.5 12h4"/></svg>',
  // 记忆地图：三颗星连成星座——项目演化「星轨图」的意象
  map: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="17" r="1.7"/><circle cx="12" cy="6" r="1.7"/><circle cx="18.5" cy="13.5" r="1.7"/><path d="m7.2 15.4 3.9-7.9M13.6 7l3.9 5.2M7.7 17.1l9.1-3"/></svg>',
  ext: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3.5V8M15 3.5V8"/><path d="M6.5 8h11v2.5a5.5 5.5 0 0 1-5.5 5.5 5.5 5.5 0 0 1-5.5-5.5z"/><path d="M12 16v4.5"/></svg>',
  relay: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2.3"/><circle cx="18" cy="18" r="2.3"/><path d="M8 6h5a4 4 0 0 1 4 4v2M16 18h-5a4 4 0 0 1-4-4v-2"/><path d="m13 9 4 3 3-4M11 15l-4-3-3 4"/></svg>',
  // 团队室：左侧横线＝频道消息流，右侧方格＝工单看板（页签三分区的意象）
  teamroom: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5.5h16"/><path d="M4 10h8M4 13.5h6M4 17h8"/><rect x="15" y="9" width="5" height="10" rx="1"/><path d="M16.8 11.8h1.4M16.8 14h1.4"/></svg>',
  // 面板开关：箭头指明点击后的动作——收起时 `>`（向右展开）、展开时 `<`（向左收起），
  // 展开态把面板列填色提示"此刻是开着的"
  panelClosed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/><path d="m8.5 9.5 2.5 2.5-2.5 2.5"/></svg>',
  panelOpen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/><path d="M15.5 6h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-3z" fill="currentColor" stroke="none" opacity=".3"/><path d="m11.5 9.5-2.5 2.5 2.5 2.5"/></svg>',
};
// 面板标签：原先 12 个平铺在「＋」菜单里，其中三个「任务…」和两个自动化页
// 挤在一起看着像同一件事的多个入口。现在合并成两个带分段的标签：
//   任务   = 子代理 / 步骤 / 事项（分段短名；完整语义见 docs/界面术语表.md）
//   自动化 = 定时 / 编排
// 合并的只是「菜单里占几格」：各分段仍是原来的 DOM、原来的渲染与加载函数，
// 侧栏与窄栏的入口（步骤 / 事项 / 定时 / 编排）点进去直接
// 落在对应分段，不因为合并就多一步点击。
const TAB_META = {
  aux: { title: "辅助对话" },
  review: { title: "审查" },
  files: { title: "文件" },
  browser: { title: "浏览器" },
  tasks: { title: "任务", sub: "子代理 · 步骤 · 事项" },
  agenda: { title: "日程" },
  auto: { title: "自动化", sub: "定时 · 编排" },
  relay: { title: "接力" },
  memory: { title: "项目记忆" },
  map: { title: "记忆地图", sub: "项目演化 · 时间线 · 图谱" },
  ext: { title: "MCP / Skills" },
  // 团队室（团队模式设计 §15）：入口跟团队走（团队卡 / 协作菜单），不进「＋」菜单
  teamroom: { title: "团队室", sub: "频道 · 工单 · 成员" },
};
// 分段 id → 所属标签。合并前的 ui.json 里存的就是分段 id（todo / ptasks /
// cron / pipeline），靠这张表映射回所属标签并把分段一并还原。
const TAB_OF = { tasks: "tasks", todo: "tasks", ptasks: "tasks", cron: "auto", pipeline: "auto", mcp: "ext", skills: "ext" };
// 标签 id → 分段顺序（第一个是打开时的默认段）；不在表里的标签没有分段
const TAB_VIEWS = { tasks: ["tasks", "todo", "ptasks"], auto: ["cron", "pipeline"], ext: ["mcp", "skills"] };
let rightTabs = [];    // 打开的标签 id（有序；只存容器标签，不存分段）
let rightActive = null;
// 每个合并标签当前显示哪一段（内存态：落盘时把分段 id 写进 right_active，
// 重启后还原——见 rightActiveView / initUiPrefs）
const rightView = { tasks: "tasks", auto: "cron", ext: "mcp" };

/** 分段 id（或普通标签 id）→ 面板标签 id。 */
function rightTabFor(id) {
  return TAB_OF[id] || id;
}

// 辅助对话实例的自定义标题（ui.json 的 aux_titles）：{ aux1: "代码问题" }
let auxTitles = {};

function auxDefaultTitle(id) {
  return "辅助对话" + id.slice(3);
}

function getTabMeta(id) {
  if (id.startsWith("aux") && id !== "aux") {
    return { title: auxTitles[id] || auxDefaultTitle(id) };
  }
  return TAB_META[id] || { title: id };
}

/** 当前标签实际显示的视图 id：合并标签返回当前分段，普通标签就是它自己。
    落盘与「某视图是否正显示」都以它为准（right_active 存的就是视图）。 */
function rightActiveView() {
  if (!rightActive) return null;
  return TAB_VIEWS[rightActive] ? rightView[rightActive] : rightActive;
}

/** 某个视图此刻是否正显示在面板上（标签打开着、面板没收起、分段也对）。
    后台事件（文件变更 / 子代理任务推进）用它决定要不要立刻重绘。 */
function rightViewVisible(view) {
  if (!view || rightCollapsed || !rightActive) return false;
  if (rightPanel.classList.contains("hidden")) return false;
  return rightActiveView() === view;
}

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

/** 右面板各视图的数据加载入口（一处定义，多处复用）。

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
  map: () => loadMemoryMap(),
  ext: () => openExtPanel(), // 容器 id 直接传进来时（恢复偏好等）也接得住
  mcp: () => openExtPanel(),
  skills: () => openExtPanel(),
  todo: () => loadTodoPanel(),
  aux: () => refreshAuxModel(), // 模型下拉：每次激活都重拉（主模型换了「跟随」项的标注要跟着变）
  relay: (force) => relayWorkbenchShown({ force, focus: false }),
  teamroom: (force) => loadTeamRoom(force), // 团队室：激活/切会话都重拉（永远对准当前会话）
};

/** 按需加载某个视图的数据；未知 id 静默跳过。

    传分段 id（todo / cron…）拉它自己；传容器标签 id（tasks / auto）拉该标签
    当前的分段——initUiPrefs 恢复标签、reloadProjectPanels 重拉打开的标签、
    renderRightPanel 展开面板都传的是容器 id，靠这一条统一接住。 */
function loadRightTab(id, force) {
  const tab = rightTabFor(id);
  if (tab.startsWith("aux") && tab !== "aux") return refreshAuxModel(); // 辅助对话多实例：只重拉模型徽章
  const view = TAB_VIEWS[tab] ? (id === tab ? rightView[tab] : id) : tab;
  const fn = RIGHT_TAB_LOADERS[view];
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
    const meta = getTabMeta(id);
    const b = document.createElement("button");
    b.className = "rp-tab" + (id === rightActive ? " active" : "");
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", id === rightActive ? "true" : "false");
    b.dataset.tabId = id;
    b.title = meta.title + (meta.sub ? "：" + meta.sub : "") + "（可拖动排序 · 右键更多）";
    b.setAttribute("aria-label", meta.title); // 图标档藏文字后无障碍名不丢
    // 团队室页签：后台有新频道消息时标题旁挂未读数（进页签即清零，trTabBadge 在 app-teamroom.js）
    const roomBadge = id === "teamroom" && typeof trTabBadge === "function" ? trTabBadge() : 0;
    b.innerHTML = (RP_ICONS[id] || RP_ICONS.aux) + "<span>" + meta.title + "</span>" +
      (roomBadge ? '<span class="rp-tab-badge">' + roomBadge + "</span>" : "") +
      '<span class="rp-x" title="关闭标签">✕</span>';
    b.querySelector(".rp-x").onclick = (e) => { e.stopPropagation(); closeRightTab(id); };
    b.onclick = () => {
      if (rpJustDragged) { rpJustDragged = false; return; } // 拖完补发的 click 别切标签
      activateRightTab(id);
    };
    if (id.startsWith("aux")) {
      b.ondblclick = (e) => { e.preventDefault(); startRpTabRename(id); };
    }
    attachRpTabDnD(b, id);
    b.oncontextmenu = (e) => { e.preventDefault(); showRpTabMenu(id, e.clientX, e.clientY); };
    rpTabs.appendChild(b);
  });
  // 行首「＋」：唤出标签小菜单（不弹窗）。固定在行首，不压在标签的关闭钮上
  // （右缘方案会盖住半截标签的 ✕）
  const addBtn = document.createElement("button");
  addBtn.className = "rp-add";
  addBtn.title = "打开其它标签";
  addBtn.textContent = "＋";
  addBtn.onclick = (e) => { e.stopPropagation(); toggleTabsMenu(addBtn); };
  rpTabs.prepend(addBtn);
  // 行尾「»」：标签挤到看不清时出现，点开列出全部标签直接跳
  const moreBtn = document.createElement("button");
  moreBtn.className = "rp-more";
  moreBtn.title = "全部标签";
  moreBtn.textContent = "»";
  moreBtn.onclick = (e) => { e.stopPropagation(); showRpOverflowMenu(moreBtn); };
  rpTabs.appendChild(moreBtn);
  // 压缩档位：均分宽度不足时先藏图标留字，再挤就出「»」全部标签菜单；
  // 判定抽成 applyRpTabDensity，容器宽度变化时由 ResizeObserver 走同一条路
  applyRpTabDensity();
  document.querySelectorAll("#rp-body .rp-page").forEach((p) => p.classList.add("hidden"));
  if (rightActive && !rightCollapsed) {
    let page = document.getElementById("rp-page-" + rightActive);
    if (!page && rightActive.startsWith("aux")) {
      createAuxPage(rightActive);
      page = document.getElementById("rp-page-" + rightActive);
    }
    if (page) page.classList.remove("hidden");
  }
  applyRightSubView(rightActive); // 合并标签里该露哪一段（收起时也刷，展开即正确）
}

/** 标签栏压缩档位：按均分到每个标签的宽度降级（CSS 规则见 app.css 的
    rp-tight / rp-has-more）。renderRightPanel 与容器宽度变化
    （ResizeObserver）都调它——拖面板宽度、缩放窗口后档位即时跟上。
    档位无变化时不动 DOM：RO 回调里每次写 class 会重新触发尺寸观察，
    形成「ResizeObserver loop completed」告警循环。

    标签永远均分压缩、不横向滚动（横滚要来回拖、找标签成本高）：
    不足 92px/个藏图标只留字；不足 64px/个时文字已只能显示两三个字，
    这时出现行尾「»」按钮，点开列出全部标签直接跳。 */
let _rpDensityKey = "";
function applyRpTabDensity() {
  const tabCount = rightTabs.length;
  const avail = rpTabs.clientWidth - 38; // 扣「＋」钮、右侧间距与内边距
  const per = tabCount > 0 ? avail / tabCount : 999;
  const key = per < 64 ? "more" : per < 92 ? "tight" : "";
  if (key === _rpDensityKey) return; // 档位未变：不写 DOM，断掉 RO 循环
  _rpDensityKey = key;
  rpTabs.classList.toggle("rp-tight", key === "tight");
  // 「»」在文字已经挤到看不清时才出现，均分档下多一个按钮只是噪音
  rpTabs.classList.toggle("rp-has-more", key === "more");
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

// —— 右侧标签：拖拽排序 / 右键菜单 / 重命名 / 溢出菜单 ——
// 标签一多就挤成几个字，只能靠记忆猜；这几个入口分别解决：自己排顺序、
// 给实例起名、一眼看到全部标签直接跳。

let rpDragFrom = null;   // 拖动源标签 id
let rpJustDragged = false; // 拖完补发的 click 要吞掉，别顺手切标签

function attachRpTabDnD(el, id) {
  el.draggable = true;
  el.addEventListener("dragstart", (e) => {
    rpDragFrom = id;
    rpJustDragged = true;
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", id); } catch {}
  });
  el.addEventListener("dragend", () => {
    rpDragFrom = null;
    el.classList.remove("dragging");
    rpTabs.querySelectorAll(".rp-tab").forEach((x) => x.classList.remove("drag-over"));
    // 复位放宏任务：拖动结束后浏览器可能同步补一个 click
    setTimeout(() => { rpJustDragged = false; }, 0);
  });
  el.addEventListener("dragover", (e) => {
    if (!rpDragFrom || rpDragFrom === id) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    el.classList.add("drag-over");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drag-over");
    if (!rpDragFrom || rpDragFrom === id) return;
    const from = rightTabs.indexOf(rpDragFrom);
    const to = rightTabs.indexOf(id);
    if (from < 0 || to < 0) return;
    const [moved] = rightTabs.splice(from, 1);
    rightTabs.splice(to, 0, moved);
    rpDragFrom = null;
    renderRightPanel();
    saveUiPrefs({ right_tabs: [...rightTabs] });
  });
}

// —— 标签右键菜单：重命名（仅辅助对话）/ 清空（仅辅助对话）/ 关闭 / 关闭其它 ——
let rpTabMenuEl = null;
function hideRpTabMenu() {
  if (rpTabMenuEl) { rpTabMenuEl.remove(); rpTabMenuEl = null; }
}
function showRpTabMenu(id, x, y) {
  hideRpTabMenu();
  const menu = document.createElement("div");
  menu.className = "tabs-menu rp-tab-menu";
  const items = [];
  if (id.startsWith("aux")) {
    items.push({ label: "重命名…", act: () => startRpTabRename(id) });
    items.push({
      label: "清空对话", act: () => {
        const sec = document.getElementById("rp-page-" + id);
        const btn = sec && sec.querySelector(".aux-clear-btn");
        if (btn) btn.click();
      },
    });
  }
  items.push({ label: "关闭标签", act: () => closeRightTab(id) });
  if (rightTabs.length > 1) {
    items.push({
      label: "关闭其它标签", act: () => {
        [...rightTabs].filter((t) => t !== id).forEach((t) => closeRightTab(t));
      },
    });
  }
  items.forEach((it) => {
    const b = document.createElement("button");
    b.className = "tabs-menu-item";
    b.textContent = it.label;
    b.onclick = () => { hideRpTabMenu(); it.act(); };
    menu.appendChild(b);
  });
  document.body.appendChild(menu);
  rpTabMenuEl = menu;
  // 贴鼠标、不越界（物理像素除回布局坐标）
  const w = 168, h = menu.offsetHeight || 120;
  menu.style.left = Math.max(8, Math.min(x / uiScale, window.innerWidth / uiScale - w - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y / uiScale, window.innerHeight / uiScale - h - 8)) + "px";
}
document.addEventListener("click", hideRpTabMenu);
window.addEventListener("keydown", (e) => { if (e.key === "Escape") hideRpTabMenu(); });

// —— 标签行内重命名（仅辅助对话实例）：双击或右键菜单进入 ——
function startRpTabRename(id) {
  const el = rpTabs.querySelector('.rp-tab[data-tab-id="' + id + '"]');
  if (!el) return;
  const span = el.querySelector("span:not(.rp-x)");
  if (!span) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rp-tab-rename";
  input.value = getTabMeta(id).title;
  input.maxLength = 20;
  input.placeholder = auxDefaultTitle(id);
  span.replaceWith(input);
  el.draggable = false;
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    if (save) {
      // 改回默认名 = 清掉自定义标题
      if (val && val !== auxDefaultTitle(id)) auxTitles[id] = val;
      else delete auxTitles[id];
      saveUiPrefs({ aux_titles: Object.keys(auxTitles).length ? auxTitles : null });
    }
    renderRightPanel();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
  input.onclick = (e) => e.stopPropagation();
  input.ondblclick = (e) => e.stopPropagation();
}

// —— 溢出菜单：标签挤到看不清时点「»」列出全部标签直接跳转 ——
function showRpOverflowMenu(anchor) {
  if (tabsMenuEl) { hideTabsMenu(); return; }
  tabsMenuEl = document.createElement("div");
  tabsMenuEl.className = "tabs-menu";
  rightTabs.forEach((id) => {
    const b = document.createElement("button");
    b.className = "tabs-menu-item" + (id === rightActive ? " active" : "");
    b.innerHTML = (RP_ICONS[id] || RP_ICONS.aux) +
      "<span>" + escapeHtml(getTabMeta(id).title) + "</span>" +
      (id === rightActive ? '<span class="tm-state">当前</span>' : "");
    b.onclick = () => { hideTabsMenu(); activateRightTab(id); };
    tabsMenuEl.appendChild(b);
  });
  document.body.appendChild(tabsMenuEl);
  const r = anchor.getBoundingClientRect();
  const w = 184;
  tabsMenuEl.style.top = Math.min(r.bottom / uiScale + 6, window.innerHeight / uiScale - 210) + "px";
  tabsMenuEl.style.left = Math.max(8, r.right / uiScale - w) + "px";
}

/** 合并标签的分段切换（任务 / 自动化）。

    分段按钮的高亮与分段视图的显隐都从 rightView 一处推导：打开标签、恢复偏好、
    点分段三条路径共用同一函数，免得某一条漏刷出现「按钮亮着、内容却是另一段」。 */
function applyRightSubView(tabId) {
  const views = TAB_VIEWS[tabId];
  if (!views) return;
  const active = rightView[tabId];
  views.forEach((v) => {
    const page = document.getElementById("rp-view-" + v);
    if (page) page.classList.toggle("hidden", v !== active);
  });
  const bar = document.getElementById("rp-segs-" + tabId);
  if (bar) {
    bar.querySelectorAll("button[data-view]").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === active);
    });
  }
}

/** 点分段：切内容并拉这一段的数据。「当前分段」借着 right_active 落盘
    （存的是视图 id，如 ptasks）——不新增偏好键，老的 ui.json 也能原样读回来。 */
function setRightSubView(tabId, view) {
  if (!TAB_VIEWS[tabId] || !TAB_VIEWS[tabId].includes(view)) return;
  rightView[tabId] = view;
  applyRightSubView(tabId);
  if (rightActive === tabId) {
    loadRightTab(view);
    saveUiPrefs({ right_active: view });
  }
}

document.querySelectorAll("#rp-body .rp-segs").forEach((bar) => {
  const tabId = bar.dataset.tab;
  bar.querySelectorAll("button[data-view]").forEach((b) => {
    b.onclick = () => setRightSubView(tabId, b.dataset.view);
  });
});

function openRightTab(id) {
  if (id === "aux") {
    // 辅助对话可多开：每次点入口分配下一个未占用的编号（辅助对话1、2…）
    let n = 1;
    while (rightTabs.includes("aux" + n)) n++;
    id = "aux" + n;
    createAuxPage(id);
  }
  const tab = rightTabFor(id);
  if (!TAB_META[tab] && !tab.startsWith("aux")) return;
  if (TAB_VIEWS[tab] && id !== tab) rightView[tab] = id; // 入口指定了分段就落到那一段
  document.body.classList.remove("sidebar-open"); // 手机互斥：开右抽屉就收侧栏
  if (!rightTabs.includes(tab)) rightTabs.push(tab);
  rightActive = tab;
  rightCollapsed = false;
  renderRightPanel();
  const openPrefs = { right_tabs: [...rightTabs], right_active: rightActiveView() };
  if (!NARROW_MQ.matches && !TABLET_MQ.matches) openPrefs.right_collapsed = 0;
  saveUiPrefs(openPrefs);
  loadRightTab(rightActiveView());
}

function activateRightTab(id) {
  rightActive = rightTabFor(id);
  renderRightPanel();
  saveUiPrefs({ right_active: rightActiveView() });
  loadRightTab(rightActiveView());
}

/** 工厂 → 自动化页：切到目标项目、打开右侧「自动化」标签，并把任务行滚进视野。

    工厂里看到「上次失败」之后最常见的下一步就是改提示词 / 补预授权工具，
    以前要「返回对话 → 切项目 → 开面板 → 找任务」四步，观战台看到问题却
    停在那里不动。失败不静默：切不过去就如实说，至少把面板打开。 */
async function openAutomationFor(projectId, taskId) {
  let target = null;
  try {
    const { projects } = await request("project.list");
    target = (projects || []).find((p) => String(p.id) === String(projectId)) || null;
  } catch (e) {
    addNotice("读取项目列表失败：" + e.message);
    return false;
  }
  if (!target || !target.root_path) {
    addNotice("该产线所属项目已不存在，无法打开");
    return false;
  }
  if (!target.is_current) {
    try {
      await request("project.switch", { path: target.root_path });
      await applyWorkspaceData(await fetchWorkspaceData());
    } catch (e) {
      addNotice("切换到该项目失败：" + e.message);
      return false;
    }
  }
  if (typeof backToChat === "function") backToChat();
  openRightTab("cron"); // rightTabFor("cron") → 自动化合并标签的定时分段
  // 任务行由 loadCron 异步渲染：轮询几拍再定位（最多约 2 秒），找不到就只开面板
  let tries = 0;
  const focusRow = () => {
    const li = taskId
      ? document.querySelector(`#cron-list [data-cron-id="${taskId}"]`)
      : null;
    if (!li) {
      if (tries++ < 8) setTimeout(focusRow, 250);
      return;
    }
    li.scrollIntoView({ block: "center", behavior: "smooth" });
    li.classList.add("cron-focus");
    setTimeout(() => li.classList.remove("cron-focus"), 2600);
  };
  setTimeout(focusRow, 120);
  return true;
}

async function openPipelineFor(projectId, pipelineId, nodeId) {
  try {
    const { projects } = await request("project.list");
    const target = (projects || []).find((p) => String(p.id) === String(projectId));
    if (!target || !target.root_path) {
      addNotice("该编排所属项目已不存在，无法打开");
      return false;
    }
    if (!target.is_current) {
      await request("project.switch", { path: target.root_path });
      await applyWorkspaceData(await fetchWorkspaceData());
    }
    const r = await request("pipeline.get", { id: Number(pipelineId) });
    if (!r.pipeline) { addNotice("该编排已不存在"); return false; }
    backToChat();
    openRightTab("pipeline");
    pipelineModal(r.pipeline, nodeId);
    return true;
  } catch (e) {
    addNotice("打开编排失败：" + e.message);
    return false;
  }
}

function closeRightTab(id) {
  const tab = rightTabFor(id);
  if (tab.startsWith("aux")) {
    const page = document.getElementById("rp-page-" + tab);
    if (page) page.remove();
    request("aux.clear", { aux_id: tab }).catch(() => {});
  }
  // 关闭 MCP/Skills 标签时，把卡片搬回设置页，否则设置页里这两个页面会空白
  if (tab === "ext") {
    const mcpPage = document.getElementById("settings-page-mcp");
    if (mcpPage.closest("#rp-page-ext")) {
      const body = document.querySelector(".settings-body");
      body.insertBefore(mcpPage, document.getElementById("settings-page-advanced"));
      const skillsPage = document.getElementById("settings-page-skills");
      const listView = document.getElementById("skill-list-view");
      const manageView = document.getElementById("skill-manage-view");
      skillsPage.insertBefore(listView, document.getElementById("skill-extras"));
      skillsPage.appendChild(manageView);
    }
  }
  rightTabs = rightTabs.filter((t) => t !== tab);
  if (rightActive === tab) rightActive = rightTabs[rightTabs.length - 1] || null;
  if (!rightTabs.length) rightCollapsed = true; // 最后一个标签关掉 → 面板收起
  renderRightPanel();
  const closePrefs = { right_tabs: [...rightTabs], right_active: rightActiveView() };
  if (!NARROW_MQ.matches && !TABLET_MQ.matches) closePrefs.right_collapsed = rightCollapsed ? 1 : 0;
  saveUiPrefs(closePrefs);
}

// —— MCP / Skills 标签：技能与 MCP 卡片只有一份，靠 DOM 搬家在「设置页 ↔ 右面板」
// 之间共享（appendChild 移动节点时事件监听器随节点走，按 ID 绑定/查找不受位置影响）。
// 面板里分 MCP / Skills 两段，各占一个独立容器：两页卡片不再上下堆在一起。
const extPanelPage = document.getElementById("rp-page-ext");

/** 把设置页的卡片搬进面板各自的容器（已搬过则不动）。
    Skills 段只搬「技能 / 场景模板」两个视图——外部服务、多模型协作、内置工具
    不属于技能本身，留在设置页（#skill-extras），不随面板走。 */
function mountExtPanels() {
  const mcpPage = document.getElementById("settings-page-mcp");
  const mcpSlot = document.getElementById("rp-view-mcp");
  if (mcpPage && mcpSlot) {
    if (mcpPage.parentElement !== mcpSlot) mcpSlot.appendChild(mcpPage);
    // 回到设置页时 showSettingsPage 会给它加 hidden，这里每次都要撤掉
    mcpPage.classList.remove("hidden");
  }
  const skillSlot = document.getElementById("rp-view-skills");
  const listView = document.getElementById("skill-list-view");
  const manageView = document.getElementById("skill-manage-view");
  if (skillSlot && listView && manageView && listView.parentElement !== skillSlot) {
    skillSlot.appendChild(listView);
    skillSlot.appendChild(manageView);
  }
}

function openExtPanel() {
  mountExtPanels();
  if (rightView.ext === "skills") resetSkillView(); // 与进设置页一致：技能页从总览开始
  renderSettings().catch((e) => addNotice("加载设置失败: " + e.message));
}

function closeExtPanel() {
  const mcpPage = document.getElementById("settings-page-mcp");
  if (!mcpPage.closest("#rp-page-ext")) return; // 卡片本来就在设置页
  const body = document.querySelector(".settings-body");
  body.insertBefore(mcpPage, document.getElementById("settings-page-advanced"));
  // 技能两视图放回技能页，位置在「外部服务」那组之前
  const skillsPage = document.getElementById("settings-page-skills");
  const listView = document.getElementById("skill-list-view");
  const manageView = document.getElementById("skill-manage-view");
  if (skillsPage && listView && manageView && listView.parentElement !== skillsPage) {
    skillsPage.insertBefore(listView, document.getElementById("skill-extras"));
    skillsPage.appendChild(manageView);
  }
  if (rightTabs.includes("ext")) closeRightTab("ext"); // 内容回设置页，面板不留空壳标签
}

// 面板收起/展开由顶栏按钮直接切换；「＋」菜单选要打开的标签（小浮层，不弹窗）
let rightCollapsed = false;
let tabsMenuEl = null;
// 窄屏（手机，与 app.css 的 @media max-width:860px 同一断点）：右面板是覆盖抽屉。
// 展开/收起在那里是会话内的临时状态——启动一律收起（不照搬桌面 ui.json 的
// right_collapsed，否则手机一进来就被抽屉盖住），操作也不回写偏好（桌面端不受手机影响）。
const NARROW_MQ = window.matchMedia("(max-width: 860px)");
// 平板档（与 app.css 的 @media max-width:1100px 同一断点）：面板仍是停靠栏但收窄，
// 启动默认收起给对话让位；与手机同一条纪律——该档的收起/展开不回写桌面偏好。
const TABLET_MQ = window.matchMedia("(max-width: 1100px)");

function persistRightCollapsed() {
  if (NARROW_MQ.matches || TABLET_MQ.matches) return;
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
    if (id === "teamroom") return; // 团队室：入口跟团队走（团队卡/协作菜单），不进「＋」菜单常驻
    const b = document.createElement("button");
    b.className = "tabs-menu-item";
    // 辅助对话是多开标签（存 auxN），菜单项按「有没有任何一个 aux 实例」判断
    const opened = id === "aux"
      ? rightTabs.some((t) => t.startsWith("aux"))
      : rightTabs.includes(id);
    b.innerHTML = RP_ICONS[id] + "<span>" + TAB_META[id].title + "</span>" +
      (opened ? '<span class="tm-state">已打开</span>' : "");
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
  if (pageViewOpen()) backToChat();
  newSessionFromHighlight();
};
document.getElementById("rail-search").onclick = () => {
  if (pageViewOpen()) backToChat();
  if (leftCollapsed) toggleLeftSidebar();
  requestAnimationFrame(() => {
    const el = document.getElementById("session-search");
    if (el) { el.focus(); el.select(); }
  });
};
// 窄栏功能导航七件套（含主操作组的工厂看板）：与侧栏导航项同一套动作
// （openRightTab 打开右侧面板标签）；停在设置页时先回对话，别让面板开在看不见的地方。
document.querySelectorAll("#sb-rail [data-rail-act]").forEach((btn) => {
  btn.onclick = () => {
    // 工厂走第三顶层视图；其余入口都回到对话再开右侧标签
    if (btn.dataset.railAct === "factory") return openFactoryPage();
    if (pageViewOpen()) backToChat();
    // 接力走带副屏的入口；其余照旧打开右侧标签
    if (btn.dataset.railAct === "relay") return openRelayWorkbench();
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
// 支持多开：每个「辅助对话N」对应 rp-page-auxN 区块，历史、流式状态各自独立
const AUX_EMPTY = '<div class="rp-empty">在这里问点小问题，不会进入主对话，也不会写进会话记录。</div>';

function getAuxState(section) {
  if (!section._auxState) {
    section._auxState = {
      streamingEl: null, streamingText: "", thinkingText: "",
      busy: false, renderTimer: null, thinkingPhase: false,
    };
  }
  return section._auxState;
}

function auxScheduleRender(section) {
  const st = getAuxState(section);
  if (st.renderTimer) return;
  st.renderTimer = setTimeout(() => {
    st.renderTimer = null;
    if (!st.streamingEl) return;
    st.streamingEl.innerHTML = st.thinkingPhase
      ? '<div class="think-inline">💭 思考中…<br>' +
        escapeHtml(st.thinkingText.slice(-400)) +
        "</div><p>▍</p>"
      : renderMarkdown(st.streamingText);
    const log = section.querySelector(".aux-log");
    if (log) log.scrollTop = log.scrollHeight;
  }, 80);
}

function createAuxPage(id) {
  const section = document.createElement("section");
  section.id = "rp-page-" + id;
  section.className = "rp-page hidden";
  section.innerHTML = `
    <div class="rp-toolbar">
      <span class="dim small">主对话之外的快速提问，独立进行、不占用主会话。</span>
      <span class="spacer"></span>
      <button class="rp-mini aux-clear-btn" title="清空辅助对话（模型对这段对话的记忆一并重置）">清空</button>
    </div>
    <div class="aux-log">${AUX_EMPTY}</div>
    <div class="aux-row">
      <textarea class="aux-input" rows="2" placeholder="问点小的…（Enter 发送 / Shift+Enter 换行）"></textarea>
      <div class="aux-side">
        <button class="aux-model-chip" title="辅助对话使用的模型（独立于主对话）"><span class="svc-avatar aux-mm-tile">?</span></button>
        <button class="rp-mini primary aux-send-btn">发送</button>
      </div>
    </div>`;
  document.getElementById("rp-body").appendChild(section);
  const log = section.querySelector(".aux-log");
  const input = section.querySelector(".aux-input");
  const sendBtn = section.querySelector(".aux-send-btn");
  const chip = section.querySelector(".aux-model-chip");

  const addUser = (text) => {
    const empty = log.querySelector(".rp-empty");
    if (empty) empty.remove();
    const d = document.createElement("div");
    d.className = "aux-msg user";
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
  };

  const send = async () => {
    const st = getAuxState(section);
    const text = input.value.trim();
    if (!text || st.busy) return;
    st.busy = true;
    sendBtn.disabled = true;
    sendBtn.textContent = "…";
    input.value = "";
    addUser(text);
    const stream = document.createElement("div");
    stream.className = "aux-msg ai";
    stream.innerHTML = "<p>▍</p>";
    log.appendChild(stream);
    log.scrollTop = log.scrollHeight;
    st.streamingEl = stream;
    st.streamingText = "";
    st.thinkingText = "";
    try {
      const r = await request("chat.aux", { text, aux_id: id });
      stream.innerHTML = renderMarkdown(r.text || st.streamingText);
      renderMermaidIn(stream);
      highlightCodeIn(stream);
    } catch (e) {
      stream.innerHTML = '<p class="dim">✗ ' + escapeHtml(e.message) + "</p>";
    } finally {
      if (st.renderTimer) { clearTimeout(st.renderTimer); st.renderTimer = null; }
      st.streamingEl = null;
      st.streamingText = "";
      st.thinkingText = "";
      st.busy = false;
      sendBtn.disabled = false;
      sendBtn.textContent = "发送";
      log.scrollTop = log.scrollHeight;
    }
  };

  const clear = async () => {
    if (log.querySelector(".aux-msg") &&
        !(await confirmModal("清空辅助对话",
          "<p>清空辅助对话？模型对这段对话的记忆也会一并重置。</p>", "清空"))) return;
    request("aux.clear", { aux_id: id }).catch((e) => addNotice("重置辅助对话记忆失败：" + e.message));
    log.innerHTML = AUX_EMPTY;
    const st = getAuxState(section);
    st.streamingEl = null;
    st.streamingText = "";
    st.thinkingText = "";
  };

  sendBtn.onclick = send;
  section.querySelector(".aux-clear-btn").onclick = clear;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  chip.onclick = async (e) => {
    e.stopPropagation();
    if (!auxModelMenu.classList.contains("hidden")) return hideAuxModelMenu();
    auxModelMenu.innerHTML = '<div class="mm-empty">加载中…</div>';
    auxModelMenu.classList.remove("hidden");
    if (!auxModelRows.length) await refreshAuxModel();
    const chipR = chip.getBoundingClientRect();
    const vh = window.innerHeight / uiScale;
    auxModelMenu.style.top = "auto";
    auxModelMenu.style.bottom = (vh - chipR.top / uiScale + 6) + "px";
    auxModelMenu.style.left = Math.max(8, chipR.right / uiScale - 250) + "px";
    buildAuxMenu(chip);
  };
}

// —— 辅助对话模型：面板独立的模型选择（跟随主对话或指定服务/模型） ——
// 后端按 ui.json 偏好在每次 chat.aux 时构建专用 provider，主对话与其余
// 会话不受影响；这里只负责把选择写进偏好，发送路径不用带模型参数。
// 入口是发送钮上方右下角的品牌徽章：闭合态只显 logo（跟随主对话就画主对话
// 服务的 logo，跟的是谁一眼可见），全称挪到悬停提示与菜单行里
const auxModelMenu = document.getElementById("aux-model-menu");
let auxModelRows = []; // config.providers 展开的服务×模型行（与主模型菜单同源）
let auxModelState = { provider: "", model: "", label: "", main_provider: "", main_label: "" };
let auxMenuTab = "preset"; // 面板页签：preset=默认（内置预设）、custom=自定义（用户新增），与主模型菜单同口径

// 菜单行里的头像比设置页小一圈；占位 tile 给「没有任何可用服务」的空态
function auxMiniAvatar(name) {
  return name ? providerAvatar(name)
    : '<span class="svc-avatar aux-mm-tile">?</span>';
}

async function refreshAuxModel() {
  const [detail, state] = await Promise.all([
    request("config.providers").catch(() => null),
    request("aux.model.get").catch(() => null),
  ]);
  if (!detail || !state) return; // 拉失败保持原样（初值即「跟随主对话」）
  auxModelRows = buildModelRows(detail);
  auxModelState = state;
  paintAuxChip();
}

function paintAuxChip() {
  const eff = auxModelState.provider || auxModelState.main_provider || "";
  document.querySelectorAll(".aux-model-chip").forEach((chip) => {
    chip.innerHTML = auxMiniAvatar(eff);
    chip.title = auxModelState.provider
      ? `辅助对话模型：${auxModelState.label}（独立于主对话，点按更换）`
      : "辅助对话跟随主对话" + (auxModelState.main_label ? `（${auxModelState.main_label}）` : "")
        + "，点按更换";
  });
}

function hideAuxModelMenu() {
  auxModelMenu.classList.add("hidden");
  auxModelMenu.innerHTML = "";
}

function buildAuxMenu(chip) {
  // 与主模型菜单同款：页签（默认/自定义）+ 行列表 + 管理入口。「跟随主对话」
  // 不属于任何页签，固定在页签上方——这是辅助面板独有的一行
  const addRow = (row) => {
    const on = !!auxModelState.provider &&
      auxModelState.provider === row.name && auxModelState.model === row.model;
    const b = document.createElement("button");
    b.className = "mm-item" + (on ? " active" : "");
    b.innerHTML = auxMiniAvatar(row.name) +
      `<span class="mm-model">${on ? "✓ " : ""}${escapeHtml(row.model)}</span>` +
      `<span class="mm-prov${row.hasKey ? "" : " no-key"}">${row.hasKey ? "" : "⚠ "}${escapeHtml(row.label)}</span>`;
    b.title = row.hasKey
      ? `辅助对话使用 ${row.label} / ${row.model}`
      : `「${row.label}」还没配置 API Key，选用后发消息会失败`;
    b.onclick = () => pickAuxModel(row);
    return b;
  };
  const groups = [
    {
      tab: "preset", label: "默认", rows: auxModelRows.filter((r) => r.preset),
      empty: "内置服务都已停用，可在管理页恢复",
    },
    {
      tab: "custom", label: "自定义", rows: auxModelRows.filter((r) => !r.preset),
      empty: "还没有自定义服务，点下方「管理模型服务」添加",
    },
  ];
  const paint = () => {
    auxModelMenu.innerHTML = "";
    const follow = document.createElement("button");
    follow.className = "mm-item" + (auxModelState.provider ? "" : " active");
    follow.innerHTML = auxMiniAvatar(auxModelState.main_provider) +
      `<span class="mm-model">跟随主对话</span>` +
      (auxModelState.main_label ? `<span class="mm-prov">${escapeHtml(auxModelState.main_label)}</span>` : "");
    follow.title = auxModelState.main_label
      ? `辅助对话跟随主对话（当前：${auxModelState.main_label}）`
      : "辅助对话跟随主对话";
    follow.onclick = () => pickAuxModel(null);
    auxModelMenu.appendChild(follow);
    const seg = document.createElement("div");
    seg.className = "seg-row seg-mini mm-seg";
    for (const g of groups) {
      const t = document.createElement("button");
      t.textContent = g.label;
      t.classList.toggle("active", auxMenuTab === g.tab);
      t.onclick = (ev) => {
        ev.stopPropagation(); // 别让冒泡到 document 的 click 把菜单关掉
        if (auxMenuTab === g.tab) return;
        auxMenuTab = g.tab;
        paint();
      };
      seg.appendChild(t);
    }
    auxModelMenu.appendChild(seg);
    const cur = groups.find((g) => g.tab === auxMenuTab) || groups[0];
    const list = document.createElement("div");
    list.className = "mm-list"; // 限高滚动容器：跟随行/页签/管理入口固定，行多了列表内滚
    if (!cur.rows.length) {
      const hint = document.createElement("div");
      hint.className = "mm-empty";
      hint.textContent = cur.empty;
      list.appendChild(hint);
    }
    cur.rows.forEach((row) => list.appendChild(addRow(row)));
    auxModelMenu.appendChild(list);
    const manage = document.createElement("button");
    manage.className = "mm-manage";
    manage.textContent = "⚙ 管理模型服务…";
    manage.title = "到设置里添加服务、启用/停用模型、配置 API Key";
    manage.onclick = () => {
      hideAuxModelMenu();
      openSettings("providers");
    };
    auxModelMenu.appendChild(manage);
    // 内容替换后按实际宽度重新定位：贴着徽章上方，右缘对齐徽章（不够放时往左收）
    const chipR = chip.getBoundingClientRect(); // 物理像素，下同
    const mw = auxModelMenu.offsetWidth; // 布局像素，无需换算
    auxModelMenu.style.bottom = (window.innerHeight / uiScale - chipR.top / uiScale + 6) + "px";
    auxModelMenu.style.left =
      Math.max(8, Math.min(chipR.right / uiScale - mw, window.innerWidth / uiScale - mw - 8)) + "px";
  };
  paint();
}

async function pickAuxModel(row) {
  hideAuxModelMenu();
  try {
    const r = await request("aux.model.set", row ? { name: row.name, model: row.model } : { name: "" });
    auxModelState = r;
    paintAuxChip();
    addNotice(row ? `✓ 辅助对话将使用「${r.label}」，主对话不变` : "✓ 辅助对话已改回跟随主对话");
  } catch (err) {
    addNotice("设置失败: " + err.message);
  }
}


document.addEventListener("click", (e) => {
  if (!auxModelMenu.classList.contains("hidden") &&
      !auxModelMenu.contains(e.target) && !e.target.closest(".aux-model-chip")) hideAuxModelMenu();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideAuxModelMenu(); });
window.addEventListener("resize", hideAuxModelMenu);

// —— 审查：本会话的文件改动轮次 + 改前→现在 diff ——
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = pad2;
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
  refreshTurnDiag(); // 轮次诊断跟审查列表同进退：切会话/自动刷新时一起重拉
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
  // 时间线（P12）：轨道顶端是「现在」——默认磁盘状态在最新一轮之后；
  // 本视图里回滚到某轮后，标记挪到该轮之下（磁盘停在「该轮改动前」）。
  // 回滚后又发生新改动则会产生新轮次排在标记之上，语义仍自洽。
  list.classList.add("review-timeline");
  const nowEl = document.createElement("div");
  nowEl.className = "review-now";
  nowEl.innerHTML = "<b>现在</b> · 磁盘状态";
  const restoredId = list.dataset.nowCp || "";
  const rows = [];
  [...cps].reverse().forEach((cp) => {
    const item = document.createElement("div");
    item.className = "review-item";
    item.dataset.cpId = cp.id;
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
        // 「现在」标记挪到这一轮之下：磁盘停在该轮改动前（P12 时间线）
        list.dataset.nowCp = cp.id;
        refreshReview();
        // 磁盘被回滚：文件树缓存失效（开着文件标签就直接刷新）
        filesLoaded = false;
        if (rightViewVisible("files")) loadFiles(true);
      } catch (e) {
        rb.disabled = false;
        rb.textContent = "↩ 恢复";
        addNotice("恢复失败: " + e.message);
      }
    };
    rows.push(item);
  });
  // 装配时间线：「现在」标记默认在轨道顶端，回滚过就插在那一轮之下
  let placed = false;
  for (const item of rows) {
    list.appendChild(item);
    if (restoredId && item.dataset.cpId === restoredId) {
      list.appendChild(nowEl);
      placed = true;
    }
  }
  if (!placed) list.insertBefore(nowEl, list.firstChild);
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

// —— 轮次诊断：本会话最近 N 轮的耗时拆解（回答「这一轮为什么慢」）——
//
// 数据来自引擎轮末写进桌面日志的结构化行（obs.py，ev=turn）；后端
// diagnostics.turn_breakdown 负责读文件与按会话过滤，这里只管摆出来。
// 旧会话/升级前的轮次没有记录，空态要把这一点讲清楚，别让用户以为坏了。

function fmtMs(ms) {
  if (ms == null || isNaN(ms)) return "—";
  if (ms >= 60000) return (ms / 60000).toFixed(1) + "min";
  if (ms >= 1000) return (ms / 1000).toFixed(1) + "s";
  return ms + "ms";
}

async function refreshTurnDiag() {
  const box = document.getElementById("review-diag");
  let r;
  try {
    r = await request("diagnostics.turn_breakdown", { session_id: currentSessionId, limit: 20 });
  } catch (e) {
    box.innerHTML = `<div class="rp-empty">轮次诊断加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  const turns = r.turns || [];
  if (!turns.length) {
    box.innerHTML = '<div class="rp-empty">还没有轮次耗时记录。引擎从每轮结束时开始把耗时拆解写进结构化日志（旧会话与升级前跑过的轮次没有）；跑一轮对话后再点「刷新」。</div>';
    return;
  }
  box.innerHTML = "";
  [...turns].reverse().forEach((t) => {
    const row = document.createElement("div");
    row.className = "diag-row";
    const tool = t.tool_calls != null
      ? `工具 ${t.tool_calls} 次` + (t.slowest_tool ? ` · 最慢 ${t.slowest_tool} ${fmtMs(t.slowest_tool_ms)}` : "")
      : "工具 —";
    const perm = t.permission_waits ? `权限等 ${fmtMs(t.permission_ms)}` : "权限无等待";
    const tok = (t.in_tokens != null || t.out_tokens != null)
      ? `token ${t.in_tokens ?? "—"}/${t.out_tokens ?? "—"}`
      : "token —";
    row.innerHTML = `<b>${t.ts ? fmtClock(t.ts) : "时间未知"}</b>` +
      `<span class="diag-dur">总 ${fmtMs(t.duration_ms)}</span>` +
      `<span>${escapeHtml(tool)}</span><span>${perm}</span><span>${tok}</span>` +
      (t.error ? `<span class="diag-err">${escapeHtml(t.error)}</span>` : "");
    box.appendChild(row);
  });
}
document.getElementById("review-diag-refresh").onclick = refreshTurnDiag;

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
    if (rightViewVisible("files")) loadFiles(true);
  }, FILES_REFRESH_DEBOUNCE_MS);
}

// 审查页的自动刷新：Agent 每次落盘（写/改/移/删文件）都会生成新的检查点，
// 防抖后重拉列表，审查页不必手动点「刷新」才能看到最新改动轮次。
let reviewRefreshTimer = 0;
function scheduleReviewRefresh() {
  if (reviewRefreshTimer) clearTimeout(reviewRefreshTimer);
  reviewRefreshTimer = setTimeout(() => {
    reviewRefreshTimer = 0;
    if (rightTabs.includes("review") && !rightPanel.classList.contains("hidden")) {
      refreshReview();
    }
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
  const titleText =
    (dirty ? "● " : "") + fileEdit.path + fileEdit.sizeText +
    (fileEdit.isNew ? " · 新文件" : "") + meta +
    (fileEdit.editable ? "" : " · 只读（" + (fileEdit.readonlyWhy || "改了存不回去") + "）");
  const titleEl = document.getElementById("files-preview-title");
  titleEl.textContent = titleText;
  titleEl.title = titleText;  // 标题超长被省略号收起时，悬停可看全文
  const saveBtn = document.getElementById("files-save");
  saveBtn.classList.toggle("hidden", !fileEdit.editable);
  saveBtn.textContent = dirty ? "● 保存" : "保存";
  fileEditorBody().readOnly = !fileEdit.editable;
}

// 有未保存修改时的放弃确认：确定 → true（走 onOk），取消 → false
/** 应用内确认弹窗：确定 → true，取消 / Esc / 点遮罩 → false。
 *
 *  两种调用形式（同一实现，避免同名函数互相覆盖）：
 *    confirmModal(标题, "HTML 正文", "确定文案")            —— 正文需自带转义
 *    confirmModal("纯文本正文", { title, okLabel, danger }) —— 纯文本自动转义
 *
 *  原生 confirm() 用的是浏览器 / WebView 自带对话框，不受主题变量控制、不跟随
 *  六套主题，在桌面壳里像一个外来的系统弹窗；破坏性操作改用本弹层。
 *  danger=true 时确定按钮走危险色，且默认焦点落在「取消」，避免误按回车确认。
 */
function confirmModal(arg1, arg2, arg3) {
  const optsStyle = arg2 && typeof arg2 === "object";
  const title = optsStyle ? (arg2.title || "请确认") : String(arg1 ?? "");
  const okText = optsStyle ? (arg2.okLabel || "确定") : (arg3 || "确定");
  const danger = optsStyle ? !!arg2.danger : false;
  return new Promise((resolve) => {
    const box = document.createElement("div");
    if (optsStyle) {
      const p = document.createElement("p");
      p.className = "confirm-text";
      p.textContent = String(arg1 ?? "");   // 纯文本：自动转义 + 保留换行
      box.appendChild(p);
    } else {
      box.innerHTML = String(arg2 ?? "");   // HTML 正文（调用方自行转义）
    }

    const modalEl = document.getElementById("modal");
    const okBtn = document.getElementById("modal-ok");
    const cancelBtn = document.getElementById("modal-cancel");
    const prevCancel = cancelBtn.onclick;

    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };

    // 取消 / Esc：Esc 在全局 handler 里转成 modal-cancel.click()
    cancelBtn.onclick = () => { settle(false); hideModal(); };
    showModal(title, box, () => settle(true), okText);
    if (danger) okBtn.classList.add("btn-danger");

    // 点遮罩 = 取消（基础弹窗本来不支持，这里为确认框补上）
    const onBackdrop = (ev) => {
      if (ev.target === modalEl) { settle(false); hideModal(); }
    };
    modalEl.addEventListener("click", onBackdrop);

    const cleanup = () => {
      modalEl.removeEventListener("click", onBackdrop);
      okBtn.classList.remove("btn-danger");
      cancelBtn.onclick = prevCancel;
      obs.disconnect();
    };
    const obs = new MutationObserver(() => {
      if (modalEl.classList.contains("hidden")) { settle(false); cleanup(); }
    });
    obs.observe(modalEl, { attributes: true, attributeFilter: ["class"] });

    if (danger) setTimeout(() => { if (modalOpen()) cancelBtn.focus(); }, 0);
  });
}

function confirmDiscard() {
  return confirmModal("未保存的修改", "<p>有未保存的修改，关闭后会丢失。</p>", "放弃修改并关闭");
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
    document.getElementById("files-resizer").classList.remove("hidden");
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
  document.getElementById("files-resizer").classList.add("hidden");
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
// 树与预览之间的分隔条：走 setupResizer 同一套（手柄样式 .ui-resizer-h、
// 高度存 --fp-h / ui.json 的 files_preview_h），与侧栏、输入区的拖拽手感一致；
// 双击恢复默认档（只读 46% / 可编辑 68%），恢复由 initUiPrefs 的 UI_LIMITS
// 通用循环处理，这里不用单写
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
// loadTasks._snap：上次渲染的任务清单序列化快照。面板由 3s 轮询驱动，
// 数据无变化时直接跳过整表 innerHTML 重建——重建会打断悬停/选区，
// 还可能与点击竞争（mousedown 落在旧节点、click 派发前节点已被替换）。

async function loadTasks() {
  const ul = document.getElementById("tasks-list");
  try {
    const tasks = (await request("tasks.list")).tasks || [];
    // 快照覆盖所有上屏字段：运行中任务的 tokens/耗时在变，失配即照常重绘
    const snap = JSON.stringify(tasks.map((t) => [
      t.id, t.status, t.agent_type, t.prompt,
      t.result ? t.result.slice(0, 300) : "",
      t.tokens_in || 0, t.tokens_out || 0, t.duration_s || 0, t.error || "",
    ]));
    if (snap === loadTasks._snap) return;
    loadTasks._snap = snap;
    loadTasks._errSnap = "";
    ul.innerHTML = "";
    if (!tasks.length) {
      ul.innerHTML = '<div class="rp-empty">还没有子代理任务。Agent 派出的后台并行任务会出现在这里。</div>';
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
        `<button class="cron-op" data-op="pipeline" title="纳入编排（挂接为流水线节点）">⛓</button></div>` +
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
    // 失败不留快照：下一轮照常重试；错误文案没变就不重写占位
    loadTasks._snap = null;
    const msg = `加载失败：${escapeHtml(e.message)}`;
    if (loadTasks._errSnap !== msg) {
      loadTasks._errSnap = msg;
      ul.innerHTML = `<div class="rp-empty">${msg}</div>`;
    }
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
  if (rightViewVisible("tasks")) loadTasks();
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
  // 取消不可恢复（任务没有「继续跑」）：确认一下再动手
  if (!(await confirmModal("取消全部子任务",
    "<p>取消所有运行中与排队中的子代理任务？已完成的任务不受影响。</p>", "全部取消"))) return;
  await request("tasks.cancel_all").catch((e) => addNotice("取消失败: " + e.message));
  addNotice("已请求取消全部运行中的子任务");
  loadTasks();
};
// 常驻 3s 轮询：面板不可见时跳过请求；可见但数据无变化时 loadTasks 内部
// 按快照跳过重绘（不做 start/clearInterval 生命周期，避免在面板开合的多处
// 钩子里接线，也和切页时 loadRightTab("tasks") 的即时刷新互不干扰）
setInterval(() => {
  if (rightViewVisible("tasks")) loadTasks();
}, 3000);

// —— 浏览器：iframe 预览（本地开发服务器 / 可内嵌网页） ——
const browserUrl = document.getElementById("browser-url");
const browserFrame = document.getElementById("browser-frame");
const browserViewport = document.getElementById("browser-viewport");
const browserEmpty = document.getElementById("browser-empty");
const browserHint = document.getElementById("browser-hint");
const browserFitBtn = document.getElementById("browser-fit");
const browserZoomHud = document.getElementById("browser-zoom-hud");

// 内网/本机地址（开发服务器一般不设 X-Frame-Options，基本都能嵌）；公网站点
// 大多带 X-Frame-Options / frame-ancestors 拒绝内嵌——只留一片空白，提前说清
function isLocalishUrl(u) {
  try {
    const h = new URL(u).hostname;
    return h === "localhost" || h === "::1" ||
      /^127\./.test(h) || /^192\.168\./.test(h) || /^10\./.test(h) ||
      /^172\.(1[6-9]|2\d|3[01])\./.test(h);
  } catch (e) { return true; }
}

// —— 自适应面板宽：跨源页面读不到内容宽度，按固定虚拟视口 1280 排版后整体
// transform 缩放到面板宽——bilibili 这类带大 min-width 的桌面版式不再横向溢出，
// 代价是字变小；面板比 1280 还宽就不缩（响应式站点自然铺满，也不放大免得发虚）。
// 偏好存 ui.json（browser_fit，缺省 = 自适应开）。本地开发服务器的响应式调试
// 需要真实面板宽度时，点「自适应」钮切回原始大小。
const BROWSER_FIT_W = 1280;
// 页面视口高度上限：为铺满面板，视口要拉到 h/scale（窄面板下远超一屏），而跨源
// 页面读不到内容高度——站点首屏渲染不满这个视口时（bilibili 首页实测约 1100px），
// 内容会顶着面板顶部、下面留出页面自己的一大片空白。限高到一屏（1080px）并把
// 整块垂直居中：内容多时在块内滚动，内容不足时空白收敛进块内、四周是面板底色。
const BROWSER_FIT_MAX_H = 1080;
// 页面缩放（自适应档）：Ctrl+滚轮悬停浏览器面板时步进，Chrome 同款档位，偏好存
// ui.json（browser_page_zoom，缺省 100% = 删键）。只接管面板自身区域（工具条/
// 上下留白/提示条）——跨源 iframe 内容区的滚轮事件父页面收不到，那片区域保持
// WebView2 原生行为。
const BROWSER_ZOOM_STOPS = [50, 67, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300];
let browserFit = true;
let browserPageZoom = 1;
let browserZoomHudTimer = null;

function layoutBrowserFrame() {
  if (!browserViewport || browserViewport.classList.contains("hidden")) return;
  if (!browserFit) {
    browserFrame.style.transform = "none";
    browserFrame.style.top = "";
    browserFrame.style.width = "100%";
    browserFrame.style.height = "100%";
    return;
  }
  // 界面缩放（CSS zoom）下 getBoundingClientRect 是物理像素，除回 uiScale 得布局像素
  const rect = browserViewport.getBoundingClientRect();
  const w = rect.width / uiScale, h = rect.height / uiScale;
  if (!(w > 10) || !(h > 10)) return;
  const scale = Math.min(1, w / BROWSER_FIT_W) * browserPageZoom;
  const vh = Math.min(h / scale, BROWSER_FIT_MAX_H);
  browserFrame.style.width = Math.round(w / scale) + "px";
  browserFrame.style.height = Math.round(vh) + "px";
  browserFrame.style.top = Math.max(0, Math.round((h - vh * scale) / 2)) + "px";
  browserFrame.style.transform = Math.abs(scale - 1) < 0.0005 ? "none" : "scale(" + scale + ")";
}

function renderBrowserFit() {
  if (!browserFitBtn) return;
  browserFitBtn.textContent = browserFit ? "自适应" : "原始";
  browserFitBtn.title = browserFit
    ? "整页缩到面板宽并垂直居中，不出横向滚动；悬停面板时 Ctrl+滚轮缩放页面（点一下改回原始大小）"
    : "原始大小：页面宽于面板会出横向滚动（点一下恢复自适应）";
}

function stepBrowserPageZoom(dir) {
  const cur = Math.round(browserPageZoom * 100);
  let i = BROWSER_ZOOM_STOPS.indexOf(cur);
  if (i < 0) {
    // 不在档位上（读回旧偏好等）：先落到不超过当前值的最近档，再按方向步进
    i = 0;
    for (let k = 0; k < BROWSER_ZOOM_STOPS.length; k++) {
      if (BROWSER_ZOOM_STOPS[k] <= cur) i = k;
      else break;
    }
  }
  applyBrowserPageZoom(BROWSER_ZOOM_STOPS[Math.max(0, Math.min(BROWSER_ZOOM_STOPS.length - 1, i + dir))] / 100);
}

function applyBrowserPageZoom(z) {
  browserPageZoom = Math.min(3, Math.max(0.5, z));
  layoutBrowserFrame();
  showBrowserZoomHud();
  // 缺省即 100%：删键而非存 100（与 ui_scale / read_width 同一套约定）
  const pct = Math.round(browserPageZoom * 100);
  saveUiPrefs({ browser_page_zoom: pct === 100 ? null : pct });
}

function showBrowserZoomHud() {
  if (!browserZoomHud) return;
  browserZoomHud.textContent = "页面缩放 " + Math.round(browserPageZoom * 100) + "%";
  browserZoomHud.classList.remove("browser-zoom-hud-off");
  clearTimeout(browserZoomHudTimer);
  browserZoomHudTimer = setTimeout(() => browserZoomHud.classList.add("browser-zoom-hud-off"), 900);
}

if (browserFitBtn) browserFitBtn.onclick = () => {
  browserFit = !browserFit;
  renderBrowserFit();
  layoutBrowserFrame();
  // 缺省即自适应：删键而非存 1（与 ui_scale / read_width 同一套约定）
  saveUiPrefs({ browser_fit: browserFit ? null : 0 });
};

if (window.ResizeObserver && browserViewport) {
  new ResizeObserver(() => layoutBrowserFrame()).observe(browserViewport);
}

function browserGo() {
  let u = browserUrl.value.trim();
  if (!u) return;
  if (!/^https?:\/\//i.test(u)) u = "http://" + u;
  browserUrl.value = u;
  browserFrame.src = u;
  browserViewport.classList.remove("hidden");
  browserEmpty.classList.add("hidden");
  // 先定提示条显隐再量尺寸：提示条占一行，量早了居中偏移会按偏高旧值算
  if (browserHint) browserHint.classList.toggle("hidden", isLocalishUrl(u));
  layoutBrowserFrame();
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
  // 浏览器面板的自适应缩放按「视口布局像素」算，zoom 变了布局像素就变，跟着重算
  layoutBrowserFrame();
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
// 浏览器面板（自适应档 + 已加载页面）例外：Ctrl+滚轮只步进页面缩放，
// 不动全局界面缩放——跨源 iframe 内容区的事件父页面收不到，接管的是面板自身区域。
let zoomWheelAccum = 0, browserZoomAccum = 0;
window.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  if (browserFit && browserViewport && !browserViewport.classList.contains("hidden")
      && e.target && e.target.closest && e.target.closest("#rp-page-browser")) {
    browserZoomAccum += e.deltaY;
    if (Math.abs(browserZoomAccum) >= 40) {
      stepBrowserPageZoom(browserZoomAccum < 0 ? 1 : -1);
      browserZoomAccum = 0;
    }
    return;
  }
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
  split_composer_h: { css: "--split-cp-h", min: 74, max: 520 }, // 分屏列输入区高（独立于主栏）
  right_w: { css: "--rp-w", min: 240, max: 720 },
  read_width: { css: "--chat-max-w", min: 680, max: 1400 }, // 阅读行宽：消息卡最大宽度
  files_preview_h: { css: "--fp-h", min: 96, max: 640 }, // 文件面板预览区高度
  split_w: { css: "--split-w", min: 300, max: 1100 }, // 分屏宽度（未存偏好时走 CSS 的 46%）
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
      // 分屏锚在 chat-main（侧栏与右面板之间）：上限必须扣掉两侧实占宽度，
      // 只留 420px 主区——否则右面板开着时能拖到比分屏可用的地方还宽，盖住主会话
      : key === "split_w"
        ? (window.innerWidth
            - document.getElementById("sidebar").getBoundingClientRect().width
            - rightPanel.getBoundingClientRect().width) / uiScale - 420
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
  // 浏览器面板自适应（缺省开；存 0 = 上次切到了原始大小）
  browserFit = prefs.browser_fit !== 0;
  renderBrowserFit();
  // 浏览器页面缩放（Ctrl+滚轮步进，缺省 100%）
  const bpz = Number(prefs.browser_page_zoom);
  if (Number.isFinite(bpz) && bpz >= 50 && bpz <= 300) browserPageZoom = bpz / 100;
  // ~ 候选排序偏好（manual=列表顺序 / top=常用优先），默认手动
  snippetsSort = prefs.snippets_sort === "top" ? "top" : "manual";
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
  engineAcceptMode = prefs.accept_edits === 2 ? "full_access" : prefs.accept_edits === 1 ? "accept_edits" : "confirm";
  renderAcceptSwitch(engineAcceptMode);
  // 主题（auto | 主题 id，默认 auto 跟随系统；旧版 light/dark 映射到纸墨/夜墨）。
  // auto 的深浅落点也在这里回填（要在 applyThemeMode 之前，首帧解析用得上）
  themeAutoLight = THEMES[prefs.theme_auto_light] ? prefs.theme_auto_light : "paper";
  themeAutoDark = THEMES[prefs.theme_auto_dark] ? prefs.theme_auto_dark : "night";
  if (HHMM_RE.test(prefs.theme_timed_from || "")) themeTimedFrom = prefs.theme_timed_from;
  if (HHMM_RE.test(prefs.theme_timed_to || "")) themeTimedTo = prefs.theme_timed_to;
  renderThemeAutoRow();
  renderThemeTimedRow();
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
  // 顶栏模型菜单里服务列的拖动序（服务名数组）：渲染前先填好，按它排
  providerOrder = Array.isArray(prefs.provider_order) ? prefs.provider_order : [];
  // 快聊分组的锚点位置（渲染前先填好，refreshSessions/refreshProjects 按它插）
  quickPosPref = (prefs.quick_pos === "top" || prefs.quick_pos === "bottom")
    ? prefs.quick_pos
    : (prefs.quick_pos && typeof prefs.quick_pos === "object" && prefs.quick_pos.before > 0
      ? { before: prefs.quick_pos.before } : null);
  applySidebarView();
  if (sidebarView === "grouped") refreshSessions();
  // 对话区宠物开关（默认显示）+ 大小（pet_scale，缺省 100%）+ 横向落点
  // （pet_x；纵向有重力，总是落在底部）
  petOn = prefs.pet == null ? true : prefs.pet === 1;
  welcomeOn = prefs.welcome_card == null ? true : prefs.welcome_card === 1;
  renderWelcomeToggle();
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
  // 恢复右侧面板：上次打开了哪些标签、激活的是哪个、面板是否收起。
  // right_tabs 只收容器标签；老 ui.json 里存的分段 id（todo / ptasks / cron /
  // pipeline）在这里映射回所属标签，并把该分段一并还原——升级后打开的面板
  // 与升级前看到的是同一页，不会退到默认分段。
  auxTitles = (prefs.aux_titles && typeof prefs.aux_titles === "object") ? prefs.aux_titles : {};
  rightTabs = [];
  for (const t of prefs.right_tabs || []) {
    let tab = rightTabFor(t);
    // 老 ui.json 存的是无编号的「辅助对话」：迁移成带编号的 auxN（多开语义）
    if (tab === "aux") {
      let n = 1;
      while (rightTabs.includes("aux" + n)) n++;
      tab = "aux" + n;
    }
    if ((!TAB_META[tab] && !tab.startsWith("aux")) || rightTabs.includes(tab)) continue;
    rightTabs.push(tab);
    if (TAB_VIEWS[tab] && t !== tab) rightView[tab] = t;
  }
  let activeTab = rightTabFor(prefs.right_active);
  // 激活项是旧的无编号 aux：指向迁移后的第一个辅助对话标签
  if (activeTab === "aux") activeTab = rightTabs.find((t) => t.startsWith("aux")) || null;
  if (rightTabs.includes(activeTab)) {
    rightActive = activeTab;
    if (TAB_VIEWS[activeTab] && TAB_VIEWS[activeTab].includes(prefs.right_active)) {
      rightView[activeTab] = prefs.right_active;
    }
  } else {
    rightActive = rightTabs[rightTabs.length - 1] || null;
  }
  rightCollapsed = prefs.right_collapsed === 1;
  if (NARROW_MQ.matches) rightCollapsed = true; // 手机启动一律收起覆盖抽屉，别让面板盖住对话区
  else if (TABLET_MQ.matches) rightCollapsed = true; // 平板同样默认收起（收窄的一栏给对话让位），顶栏按钮随时开
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
  // 宽度/缩放/面板全部就位后再算一次：boot 早期按未稳宽度测出的折行高度
  // （textarea 内联 6 行帽值）在这里被纠正，--cp-h 恢复主导（见 composer
  // 宽度观察器处的注释）
  autoGrowInput();
}

function setupResizer(el, key, compute, afterApply) {
  // afterApply：每次拖拽改写布局变量后调用（composer 类手柄用它重跑
  // autoGrowInput——内容高度压过 --cp-h 时，不重算的话往小拖会无响应）
  // 拖拽修复（2026-10-07）：此前 move/up 直接挂 window、且不处理 pointercancel——
  // WebView2 下偶发的 cancel/丢 up 会让 start 残留，之后不按键移动鼠标也持续改布局
  // （表现为「拖完边界后鼠标飘、界面跟着指针跑」）。现改为指针捕获 + 异常收尾兜底：
  // ① down 即 setPointerCapture：拖出窗口/扫过浮层也持续收到 move，up 一定送达；
  // ② pointercancel / lostpointercapture 强制收尾，start 不残留；
  // ③ move 且 e.buttons===0 视为 up 已丢，按松手处理；
  // ④ move 经 rAF 合成每帧一次布局写入（预览区高度变化会带动文件树整块重排，
  //    逐事件直写会掉帧，手感像手柄「追着鼠标飘」），lastApplied 供异常收尾按现状落盘。
  let start = null, lastApplied = null, pendingE = null, rafId = 0;
  const clearViz = () => {
    el.classList.remove("active");
    document.body.classList.remove("resizing", "resizing-x", "resizing-y");
  };
  // e 为空 = 异常收尾（cancel/丢捕获）：拿不到终点坐标，按已应用的最新值落盘
  const finish = (e) => {
    if (!start || (e && e.pointerId !== start.pointerId)) return;
    const v = e ? clampUi(key, compute.value(start, e)) : lastApplied;
    start = null; lastApplied = null; pendingE = null;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    clearViz();
    if (v != null) saveUiPrefs({ [key]: v });
  };
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || start) return; // 已在拖动（触屏第二指）不重复接管
    e.preventDefault();
    start = { x: e.clientX, y: e.clientY, base: compute.base(), pointerId: e.pointerId };
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    el.classList.add("active");
    document.body.classList.add("resizing",
      (key === "sidebar_w" || key === "split_w") ? "resizing-x" : "resizing-y");
  });
  el.addEventListener("pointermove", (e) => {
    if (!start || e.pointerId !== start.pointerId) return;
    if (!e.buttons) { finish(e); return; } // 无按键的 move = up 被宿主吞了，就地收尾
    pendingE = e;
    if (!rafId) rafId = requestAnimationFrame(() => {
      rafId = 0;
      if (!start || !pendingE) { pendingE = null; return; }
      const v = clampUi(key, compute.value(start, pendingE));
      pendingE = null;
      lastApplied = v;
      setUiVar(key, v);
      if (afterApply) afterApply(v);
    });
  });
  el.addEventListener("pointerup", finish);
  el.addEventListener("pointercancel", () => finish(null));
  // 正常 up 后捕获自动释放也会触发本事件，此时 start 已空，天然幂等
  el.addEventListener("lostpointercapture", () => { if (start) finish(null); });
  el.addEventListener("dblclick", () => {
    setUiVar(key, null);
    saveUiPrefs({ [key]: null });
    if (afterApply) afterApply(null);
  });
}

setupResizer(document.getElementById("sb-resizer"), "sidebar_w", {
  base: () => document.getElementById("sidebar").getBoundingClientRect().width / uiScale,
  value: (s, e) => s.base + (e.clientX - s.x) / uiScale,
});
setupResizer(document.getElementById("cp-resizer"), "composer_h", {
  base: () => document.getElementById("composer").getBoundingClientRect().height / uiScale,
  value: (s, e) => s.base - (e.clientY - s.y) / uiScale, // 向上拖 = 输入区变高
}, (v) => {
  // 拖拽中清掉 textarea 内联高度：boot 期错误折行或此前的 6 行帽值残留在
  // 内联高度里时，内容高度会压过 --cp-h，往下拖到内容高度就卡死；清掉后
  // --cp-h 全程主导，可一路缩到下限。输入事件会重新触发 autoGrow，打字
  // 高度逻辑不受影响；双击恢复默认（v=null）时内容高度重新主导，重算。
  if (v == null) autoGrowInput();
  else inputEl.style.height = "";
});
setupResizer(document.getElementById("files-resizer"), "files_preview_h", {
  base: () => document.getElementById("files-preview").getBoundingClientRect().height / uiScale,
  value: (s, e) => s.base - (e.clientY - s.y) / uiScale, // 向上拖 = 预览区变高
});
setupResizer(rpResizer, "right_w", {
  base: () => rightPanel.getBoundingClientRect().width / uiScale,
  value: (s, e) => s.base - (e.clientX - s.x) / uiScale, // 向左拖 = 面板变宽
});
setupResizer(document.getElementById("split-resizer"), "split_w", {
  base: () => {
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--split-w"));
    return Number.isFinite(v) && v > 0
      ? v
      : document.getElementById("split-pane").getBoundingClientRect().width / uiScale;
  },
  value: (s, e) => s.base + (s.x - e.clientX) / uiScale, // 向左拖 = 分屏变宽
});

// ============================================================
// 新功能区块：主题 / Mermaid / 预览 / 记忆页 / 局域网 /
// 联网搜索与画图配置 / 更新检查 / 窄屏适配
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
  if (v === "timed") return "timed";
  return Object.prototype.hasOwnProperty.call(THEMES, v) ? v : "auto";
}
let themePref = "auto"; // auto | timed | 主题 id
// 「跟随系统」的深浅落点（设置页主题卡下方的两个下拉；默认纸墨/夜墨）
let themeAutoLight = "paper";
let themeAutoDark = "night";
// 「定时切换」（P20）的深色时段：可跨零点，深浅落点复用上面两个下拉
let themeTimedFrom = "19:00";
let themeTimedTo = "07:00";
const HHMM_RE = /^([01]?\d|2[0-3]):[0-5]\d$/;
function timedInWindow() {
  const mins = (s, dft) => {
    if (!HHMM_RE.test(s || "")) return dft;
    const [h, m] = s.split(":").map(Number);
    return h * 60 + m;
  };
  const now = new Date();
  const t = now.getHours() * 60 + now.getMinutes();
  const f = mins(themeTimedFrom, 19 * 60), e = mins(themeTimedTo, 7 * 60);
  if (f === e) return false; // 起止相同视为空窗，恒浅色
  return f < e ? (t >= f && t < e) : (t >= f || t < e);
}
const themeMql = window.matchMedia("(prefers-color-scheme: dark)");

function resolvedThemeId() {
  if (themePref === "timed") return timedInWindow() ? themeAutoDark : themeAutoLight;
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
  // 工厂场景的昼夜盘由「本地时刻 + 当前主题明暗」决定：主题切换后重绘一次
  // （系统深浅切换同样走这里，见 themeMql 的 change 监听）
  if (typeof factoryRepaint === "function") factoryRepaint();
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
// 的变量，不必真的切全局主题（切了会整页闪）。纸墨是默认主题、变量在 :root 上，
// 靠 app.css 把该组同时挂到 [data-theme="paper"] 才能被这里读到（裸 :root 会继承
// 当前主题，纸墨卡就跟着变色了）。
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
  // 跟随系统卡是「若此刻为 auto 会落到哪」的预览：只看下方两个下拉 + 系统深浅，
  // 与当前选中的主题无关（否则选了具体主题后，这张卡会跟那张卡长得一模一样）
  paint("auto", themeMql.matches ? themeAutoDark : themeAutoLight);
  // 定时切换卡同理：预览「若此刻为 timed 会落到哪」（按当前时段 + 两个下拉）
  paint("timed", timedInWindow() ? themeAutoDark : themeAutoLight);
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
  else renderThemeSwatches(); // 选着具体主题时也刷新跟随系统卡的预览落点
});
// 「定时切换」的深色时段输入（P20）：非法值回填旧值，合法即存即重解析
function renderThemeTimedRow() {
  const f = document.getElementById("theme-timed-from");
  const t = document.getElementById("theme-timed-to");
  if (f) f.value = themeTimedFrom;
  if (t) t.value = themeTimedTo;
}
const timedFromInput = document.getElementById("theme-timed-from");
if (timedFromInput) {
  timedFromInput.onchange = () => {
    if (HHMM_RE.test(timedFromInput.value)) {
      themeTimedFrom = timedFromInput.value;
      saveUiPrefs({ theme_timed_from: themeTimedFrom });
      renderThemeSwatches();
      if (themePref === "timed") applyThemeMode("timed", false);
    } else renderThemeTimedRow();
  };
}
const timedToInput = document.getElementById("theme-timed-to");
if (timedToInput) {
  timedToInput.onchange = () => {
    if (HHMM_RE.test(timedToInput.value)) {
      themeTimedTo = timedToInput.value;
      saveUiPrefs({ theme_timed_to: themeTimedTo });
      renderThemeSwatches();
      if (themePref === "timed") applyThemeMode("timed", false);
    } else renderThemeTimedRow();
  };
}
// 定时切换的分钟级巡检：跨过时段边界时按新落点重解析（只在 timed 模式下做事）
setInterval(() => {
  if (themePref !== "timed") return;
  const id = resolvedThemeId();
  if (document.documentElement.getAttribute("data-theme") !== id) {
    applyThemeMode("timed", false);
  }
}, 60000);

// ---------- 第三方大库按需加载 ----------
// mermaid 单文件 3.3MB、xterm 双文件约 290KB，此前页面一打开就同步解析，
// 冷启动白屏几百毫秒到秒级，而绝大多数会话一张图不画、底部终端默认关着。
// 首次用到时才注入 <script>；加载失败由调用方按纯文本兜底。
const _libLoads = {};
function loadLib(name, srcs) {
  if (!(_libLoads[name] instanceof Promise)) {
    _libLoads[name] = new Promise((resolve, reject) => {
      let i = 0;
      // 失败不缓存：拒绝态的 Promise 若留在表里，mermaid/高亮/终端/二维码
      // 在刷新页面前就永久不可用。vendor 文件是本机资源，一次失败多因瞬时
      // 占用或安装损坏，先清缓存再 reject，下次调用可重新加载。
      const fail = (err) => { delete _libLoads[name]; reject(err); };
      const next = () => {
        if (i >= srcs.length) { fail(new Error("库加载失败: " + name)); return; }
        const s = document.createElement("script");
        s.src = srcs[i++];
        s.onload = () => (i < srcs.length ? next() : resolve());
        s.onerror = () => fail(new Error("库加载失败: " + s.src));
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
// highlight.js（代码高亮）与 qrcode（LAN/Tailscale/微信登录二维码）同样按需加载：
// 此前在 index.html 里同步 <script src>，首屏解析白背几百 KB；绝大多数会话
// 用不到二维码，纯文本对话也用不到高亮。加载失败由调用方按原文/无码兜底。
function ensureHighlight() {
  if (window.hljs) return Promise.resolve();
  return loadLib("hljs", ["/static/vendor/highlight.min.js"]);
}
function ensureQrcode() {
  if (window.QRCode) return Promise.resolve();
  return loadLib("qrcode", ["/static/vendor/qrcode.min.js"]);
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
  if (!container) return;
  if (!window.hljs) {
    // 库未就绪（懒加载中）：加载完成后把容器里还没高亮的块补一遍。
    // 最多重试一次（then 里确认 hljs 真的在），失败静默保持原文。
    ensureHighlight().then(() => { if (window.hljs) highlightCodeIn(container); }).catch(() => {});
    return;
  }
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

// 注入上限提示：文件超过注入上限时，页面必须明说 Agent 只看到了最近一段（默认是静默截断）
function memoryInjectNote(fullLen, injectLen) {
  const el = document.getElementById("memory-inject-note");
  if (!el) return;
  if (fullLen > injectLen && injectLen > 0) {
    el.textContent = `⚠ 记忆共 ${fullLen} 字，超过单轮注入上限：每轮只注入最近 ${injectLen} 字（按行截断），` +
      "更早的条目 Agent 看不到（文件里不会丢）；可精简表述或删掉过时条目。";
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
    autoHideStatus(st, st.textContent, true);
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

// 已连接设备 + 最近访问（lan / ts 共用）：本机连接不列（没意义），
// 远程连接带「断开」按钮（lan.kick，仅本机可用）
function connectionsNote(conns) {
  const remote = (conns || []).filter((c) => c.origin !== "local");
  if (!remote.length) return "";
  const rows = remote.map((c) => {
    const where = c.origin === "tailscale" ? "远程（Tailscale）" : "局域网";
    const ua = c.ua ? `　${escapeHtml(c.ua.split(")")[0] + ")")}` : "";
    return `<div class="lan-note">· ${escapeHtml(c.ip)}（${where}，连于 ${new Date(c.connected_at * 1000).toLocaleString()}）${ua}
      <button class="btn-ghost" data-kick="${escapeHtml(c.id)}" title="断开这台设备的连接">断开</button></div>`;
  }).join("");
  return `<div class="lan-note"><b>已连接设备（${remote.length}）</b></div>${rows}`;
}

function wireKickButtons() {
  document.querySelectorAll("[data-kick]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await confirmModal("断开这台设备的连接？对方页面会断开，重新打开地址即可重连。",
        { title: "断开设备", okLabel: "断开", danger: true }))) return;
      try {
        const r = await request("lan.kick", { connection_id: btn.dataset.kick });
        addNotice(`已断开 ${r.kicked} 个连接`);
        loadLanPanel();
        loadTsPanel();
      } catch (e) {
        addNotice("操作失败: " + e.message);
      }
    };
  });
}

function recentAccessNote(access) {
  if (!access || !access.total) return "";
  const rows = (access.recent || []).map((f) =>
    `<div class="lan-note">· ${escapeHtml(f.ip || "")}（${f.where === "ws" ? "应用连接" : "页面访问"}，${new Date(f.ts * 1000).toLocaleString()}）</div>`
  ).join("");
  return `<div class="lan-note"><b>最近访问（自启动以来 ${access.total} 次）</b></div>${rows}`;
}

// 重新生成访问令牌：立即生效，旧地址/二维码/已登录的手机全部作废
async function rotateAccessToken() {
  if (!(await confirmModal("重新生成访问令牌？所有已发出的地址和二维码立即作废，手机需要用新地址重新打开。",
    { title: "重新生成令牌", okLabel: "重新生成", danger: true }))) return;
  try {
    const r = await request("lan.rotate_token", {});
    addNotice(r.note || "令牌已更换");
    loadLanPanel();
    loadTsPanel();
  } catch (e) {
    addNotice("操作失败: " + e.message);
  }
}

// 固定服务端口（PWA / 固定地址的前提）：已安装的 PWA 把安装时的地址（含
// 端口）固化进 start_url，端口随机时桌面一重启手机图标就打不开。0 = 恢复随机。
function lanPortSettingHtml(st) {
  return `
    <div class="lan-note">服务端口：<input id="lan-port" class="lan-port-input" type="number" min="0" max="65535"
      value="${st.port ? Number(st.port) : ""}" placeholder="随机"> <button id="lan-port-save" class="btn-ghost">保存</button>
      <span class="dim small">0 = 每次启动随机。要安装 PWA（添加到主屏幕）或长期使用固定地址，
      先固定端口——否则重启 SkySheep 后手机图标打开的就是旧地址（连接失败）。保存后重启生效。</span></div>`;
}

async function saveLanPort() {
  const input = document.getElementById("lan-port");
  if (!input) return;
  const raw = input.value.trim();
  try {
    const r = await request("lan.set_port", { port: Number(raw) || 0 });
    addNotice(r.note || "端口设置已保存");
    // 固定端口与开关一样需要重启才生效：不给统一的重启横条，用户会以为
    // 已立即生效（对 PWA 用户尤其误导，旧地址在重启前依然有效）
    needsRestart();
    loadLanPanel();
    loadTsPanel();
  } catch (e) {
    addNotice("操作失败: " + e.message);
  }
}

function wireLanPortSetting() {
  const btn = document.getElementById("lan-port-save");
  if (btn) btn.onclick = saveLanPort;
}

async function loadLanPanel() {
  let st;
  try { st = await request("lan.status"); } catch (e) {
    // 读取失败不能再静默 return：用户会以为远程功能是关闭的，或看着空白
    // 无从判断是断线、权限拒绝还是后端故障。在卡片内显式报错。
    const info = document.getElementById("lan-info");
    if (info) info.innerHTML = `<div class="lan-note">加载远程状态失败：${escapeHtml(e.message)}。请检查连接后点「刷新」。</div>`;
    return;
  }
  const info = document.getElementById("lan-info");
  const toggle = document.getElementById("lan-toggle");
  toggle.checked = !!st.enabled;
  const urls = (st.ips || []).map((ip) => `http://${ip}:${location.port}/?token=${st.token || "你的令牌"}`);
  if (!st.enabled) {
    info.innerHTML = `<div class="lan-note">${escapeHtml(st.note || "")}</div>` + lanPortSettingHtml(st);
    wireLanPortSetting();
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
        ${st.port ? "" : "⚠️ 当前端口随机：<b>装 PWA 前先在下方固定端口</b>，否则重启后手机图标打开的是旧地址。<br>"}
        <button id="lan-copy" class="btn-ghost" style="margin-top:6px">⧉ 复制地址</button>
        <button id="lan-rotate" class="btn-ghost" style="margin-top:6px">⟳ 重新生成令牌</button>
        <span id="lan-copy-msg" class="io-msg"></span>
      </div>
    </div>
    ${lanPortSettingHtml(st)}
    ${connectionsNote(st.connections)}
    ${recentAccessNote(st.recent_access)}
    ${tokenFailureNote(st.token_failures)}`;
  wireLanPortSetting();
  wireKickButtons();
  const copyBtn = document.getElementById("lan-copy");
  if (copyBtn) copyBtn.onclick = () => copyAccessUrl(urls[0] || "", "lan-copy-msg");
  const rotateBtn = document.getElementById("lan-rotate");
  if (rotateBtn) rotateBtn.onclick = () => rotateAccessToken();
  if (urls.length && st.token) {
    // 二维码库懒加载：就绪后画；期间面板若已重渲染（getElementById 落空）就放弃
    ensureQrcode().then(() => {
      const qrBox = document.getElementById("lan-qr");
      if (!window.QRCode || !qrBox) return;
      try {
        new QRCode(qrBox, {
          text: urls[0], width: 132, height: 132, correctLevel: QRCode.CorrectLevel.M,
        });
      } catch (e) { /* 二维码失败不影响地址文本 */ }
    }).catch(() => { /* 二维码失败不影响地址文本 */ });
  }
}
document.getElementById("lan-toggle").addEventListener("change", async (e) => {
  if (e.target.checked && !(await confirmModal(
    "开启手机控制？\n\n" +
    "开启后，同一 Wi-Fi 下的设备凭令牌即可访问本机 SkySheep（完整界面与文件读写能力），" +
    "地址为 http 明文传输，令牌会出现在地址栏与二维码里。\n\n" +
    "只建议在可信网络使用；跨网络或不可信 Wi-Fi 请改用「远程访问（Tailscale）」。\n\n确定开启？",
    { title: "开启手机控制", okLabel: "开启", danger: true }
  ))) { e.target.checked = false; return; }
  try {
    const r = e.target.checked ? await request("lan.enable", {}) : await request("lan.disable");
    addNotice(r.note || "手机控制设置已更新");
    loadLanPanel();
    needsRestart(); // LAN 开关要重启才生效：页顶横条提醒（可就地重启）
  } catch (err) {
    addNotice("操作失败: " + err.message);
    e.target.checked = !e.target.checked;
  }
});

// ---------- 设置 · 远程访问（Tailscale）：不在同一网络也能连回家 ----------
async function loadTsPanel() {
  let st;
  try { st = await request("remote.status"); } catch (e) {
    // 同 loadLanPanel：读取失败要可见，不静默留旧内容/空白
    const info = document.getElementById("ts-info");
    if (info) info.innerHTML = `<div class="lan-note">加载远程状态失败：${escapeHtml(e.message)}。请检查连接后点「刷新」。</div>`;
    return;
  }
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
        ${st.port ? "" : "⚠️ 当前端口随机：装 PWA（添加到主屏幕）前先在「手机控制」卡片固定服务端口，否则重启后手机图标打开的是旧地址。<br>"}
        <button id="ts-copy" class="btn-ghost" style="margin-top:6px">⧉ 复制地址</button>
        <button id="ts-rotate" class="btn-ghost" style="margin-top:6px">⟳ 重新生成令牌</button>
        <span id="ts-copy-msg" class="io-msg"></span>
      </div>
    </div>
    ${connectionsNote(st.connections)}
    ${recentAccessNote(st.recent_access)}
    ${tokenFailureNote(st.token_failures)}`;
  wireKickButtons();
  const copyBtn = document.getElementById("ts-copy");
  if (copyBtn) copyBtn.onclick = () => copyAccessUrl(urls[0] || "", "ts-copy-msg");
  const rotateBtn = document.getElementById("ts-rotate");
  if (rotateBtn) rotateBtn.onclick = () => rotateAccessToken();
  if (urls.length && st.token) {
    ensureQrcode().then(() => {
      const qrBox = document.getElementById("ts-qr");
      if (!window.QRCode || !qrBox) return;
      try {
        new QRCode(qrBox, {
          text: urls[0], width: 132, height: 132, correctLevel: QRCode.CorrectLevel.M,
        });
      } catch (e) { /* 二维码失败不影响地址文本 */ }
    }).catch(() => { /* 二维码失败不影响地址文本 */ });
  }
}
document.getElementById("ts-toggle").addEventListener("change", async (e) => {
  if (e.target.checked && !(await confirmModal(
    "开启远程访问（Tailscale）？\n\n" +
    "开启后，登录同一 Tailscale 账号的设备可在任意网络访问本机 SkySheep；" +
    "链路端到端加密，比局域网 http 明文更安全，但令牌仍是唯一门锁。\n\n确定开启？",
    { title: "开启远程访问", okLabel: "开启", danger: true }
  ))) { e.target.checked = false; return; }
  try {
    const r = e.target.checked ? await request("remote.enable", {}) : await request("remote.disable");
    addNotice(r.note || "远程访问设置已更新");
    loadTsPanel();
    needsRestart(); // Tailscale 开关要重启才生效
  } catch (err) {
    addNotice("操作失败: " + err.message);
    e.target.checked = !e.target.checked;
  }
});

// ---------- 一键重启：拉起新实例后旧进程优雅退出（LOCAL_ONLY，仅桌面本机可调） ----------
// 需重启生效统一提醒：改了「重启才生效」的设置（LAN / Tailscale / 全局热键）后调用。
// 横条常驻到用户重启或手动关掉；渠道启停不在此列（后端会立即重建渠道，热生效）。
function needsRestart() {
  const b = document.getElementById("restart-banner");
  if (b) b.classList.remove("hidden");
}
document.getElementById("restart-banner-close").onclick = () => {
  document.getElementById("restart-banner").classList.add("hidden");
};
document.getElementById("btn-app-restart").onclick = async () => {
  if (!(await confirmModal("重启 SkySheep？未完成的对话轮会被中断，重启后窗口自动恢复。",
    { title: "重启应用", okLabel: "重启" }))) return;
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

// ---------- 设置 · 技能与工具：电脑控制（工具总开关，开关即保存） ----------
// 开关决定对应工具是否注册给 Agent（打开即热生效）。它是「Agent 能用哪些工具」，
// 不是「远程接入」能力，所以放在「内置工具」列表处；advanced.save 支持只提交
// 要改的字段，其余高级参数保持不变。
// 浏览器控制那份开关 2026-10-21 搬到了设置 · 浏览器控制页（那里一起管证书开关
// 与浏览器数据），前端接线在 app-browser.js，走的是 browser.save。
async function loadToolControl() {
  let d;
  try { d = await request("advanced.get"); } catch (e) { return; }
  const c = document.getElementById("tool-computer-control");
  if (c) c.checked = !!d.computer_control;
}
function toolControlStatus(statusId, text, ok = true) {
  const st = document.getElementById(statusId);
  if (!st) return;
  st.textContent = text;
  st.className = "card-status " + (ok ? "ok" : "bad");
  st.hidden = !text;
}
async function saveToolControl(el, key, statusId) {
  try {
    const d = await request("advanced.save", { [key]: el.checked });
    const c = document.getElementById("tool-computer-control");
    if (c) c.checked = !!d.computer_control;
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
    saveToolControl(e.target, "computer_control", "tool-computer-status"));
}

// ---------- 设置 · 聊天机器人渠道（Bot Channel） ----------
// 渠道是「受限遥控端」：能对话与审批，但不能改降低防护的开关（后端 dispatch 层也拦，
// 这里不提供入口）。默认关闭；聊天渠道（飞书/微信）启用前必须把账号加入允许名单，
// webhook 纯出站没有名单门槛（填了 URL 即为推送目标）。
const CHANNEL_LABEL = { feishu: "飞书", weixin: "微信", webhook: "Webhook" };
// 微信的凭据来自扫码（不是手填 Token），且会失效需重登
let wxLoginQrcode = "";
let wxLoginTimer = null;
let wxLoginRefreshCount = 0; // 连续自动换新的二维码数（检测到扫描即清零），防止接口异常时无限重取
let wxVerifyCode = ""; // 风控配对码：手机微信上显示的数字，轮询时随 verify_code 带回

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
    </div>
    <div class="channel-footer">
      <label class="channel-opt">
        <input type="checkbox" id="channel-perm-push" ${st.permission_push ? "checked" : ""}>
        <span>权限请求推送到渠道（只提醒，确认仍在桌面端）</span>
      </label>
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
  const isWebhook = c.name === "webhook";
  // 状态做成一枚小徽标：已关闭 / 运行中 / 已启用但没跑起来。
  // 适配器报 extra.connected === false 时（飞书长连接的“任务在跑、连接未建立”
  // 这种中间态），不能只说「运行中」——那就又变成用户看不到原因的状态。
  // webhook 是纯出站：没有后台轮询，「运行」恒为假，启用即就绪，不套聊天渠道的
  // 「已启用但未运行」文案（那会让用户以为它坏了）。
  const connDown = c.extra && c.extra.connected === false;
  const badge = isWebhook
    ? (c.enabled
      ? '<span class="channel-badge on">已启用 · 纯出站推送</span>'
      : '<span class="channel-badge">已关闭</span>')
    : (c.running
    ? (connDown
      ? `<span class="channel-badge warn">已启用但未连上</span>`
      : `<span class="channel-badge on">运行中</span>`)
    : (c.enabled
      ? `<span class="channel-badge warn">已启用但未运行</span>`
      : `<span class="channel-badge">已关闭</span>`));
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
  // 凭据区按平台分流：飞书是手填 App ID + App Secret，微信是扫码登录，
  // webhook 是推送地址 + 可选签名密钥（纯出站，没有入站凭据）
  const credBlock = c.name === "weixin"
    ? weixinLoginBlock(c)
    : isWebhook
    ? `<div class="channel-field">推送地址 URL</div>
       <input class="channel-input channel-url" data-name="${c.name}" type="text"
         placeholder="${c.has_url ? "已保存（留空则不修改）" : "https://example.com/hook"}">
       <div class="channel-field">签名密钥（可选）</div>
       <input class="channel-input channel-secret" data-name="${c.name}" type="password"
         placeholder="${c.has_secret ? "已保存（留空则不修改）" : "留空不签名；填写后发送时附带 HMAC-SHA256 签名头供接收方验源"}">`
    : `<div class="channel-field">App ID</div>
       <input class="channel-input channel-appid" data-name="${c.name}" type="text"
         placeholder="${c.has_app_id ? "已保存（留空则不修改）" : "cli_xxxxxxxxxxxxxxxx"}">
       <div class="channel-field">App Secret</div>
       <input class="channel-input channel-appsecret" data-name="${c.name}" type="password"
         placeholder="${c.has_app_secret ? "已保存（留空则不修改）" : "从开发者后台复制 App Secret"}">`;
  // 首次配置的顺序指引：启用不需要先填名单（机器人跑起来才能发现来源），
  // 但名单为空时它对一切消息保持沉默，所以要提示用户「启用 → 发消息 → 回来认领」。
  // webhook 没有入站，名单语义反转（留空 = 广播），不存在「认领」步骤
  const needClaim = c.enabled && !isWebhook && !(c.allowed_ids || []).length;
  const hint = isWebhook
    ? "纯出站推送：填好 URL 并启用后，定时与编排的运行摘要、每日运行日报等出站消息会以 JSON POST 到这个地址；发送失败只记日志，不重试。"
    : c.name === "weixin"
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
    ${isWebhook
      ? '<div class="channel-hint">无需允许名单：webhook 不收消息，没有准入名单的概念——启用且填好 URL 即为推送目标（名单留空即广播）。</div>'
      : `<div class="channel-field">允许名单（每行一个 ${idLabel}；<b>空名单 = 拒绝一切</b>）</div>
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
      : ""}`}
    <div class="channel-ops">
      <button class="btn-ghost channel-save" data-name="${c.name}">保存</button>
      <button class="btn-ghost channel-test" data-name="${c.name}">${isWebhook ? "发送测试推送" : "发测试消息"}</button>
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
  box.innerHTML = '<div class="channel-hint">二维码组件加载中…</div>';
  ensureQrcode().then(() => {
    if (!window.QRCode || !document.getElementById(elId)) return;
    box.innerHTML = "";
    try {
      new QRCode(box, { text, width: 168, height: 168, correctLevel: QRCode.CorrectLevel.M });
    } catch (e) {
      box.innerHTML = `<div class="channel-hint bad">二维码生成失败：${escapeHtml(e.message)}</div>`;
    }
  }).catch(() => {
    box.innerHTML = '<div class="channel-hint bad">二维码组件加载失败，请刷新页面重试</div>';
  });
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
  wxVerifyCode = ""; // 新码是新会话，旧配对码作废
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
      st = await request("channel.weixin_login_poll", {
        qrcode: wxLoginQrcode,
        verify_code: wxVerifyCode || undefined,
      });
    } catch (e) {
      const el = document.getElementById("wx-qr-status");
      if (el) el.textContent = "轮询失败：" + e.message;
      wxLoginTimer = setTimeout(tick, 3000);
      return;
    }
    const el = document.getElementById("wx-qr-status");
    if (st.status === "confirmed") {
      wxLoginQrcode = "";
      wxVerifyCode = "";
      channelMsg("微信登录成功", "ok");
      loadChannelPanel();
      return;
    }
    if (st.status === "error") {
      // 服务器明确报错（如二维码失效）：把原因亮出来并停止，不能装作还在等待
      wxLoginQrcode = "";
      wxVerifyCode = "";
      if (el) el.textContent = "扫码流程出错：" + (st.error || "未知错误") + "，请点「生成登录二维码」重试。";
      return;
    }
    if (st.status === "binded") {
      // 官方 binded_redirect：该微信已绑定过机器人，再扫不会成功
      wxLoginQrcode = "";
      wxVerifyCode = "";
      if (el) el.textContent = "该微信已连接过此机器人，无需重复扫码。";
      return;
    }
    if (st.status === "need_verifycode") {
      // 风控配对码：手机微信上会显示数字，填进来继续；已填过再要就是输错了
      const wrong = Boolean(wxVerifyCode);
      if (el) {
        el.innerHTML = (wrong ? "数字不匹配，请重新输入" : "请输入") +
          "手机微信上显示的数字：" +
          `<input id="wx-verify-input" class="modal-input" inputmode="numeric" maxlength="8" autocomplete="off"> ` +
          `<button class="btn-ghost" id="wx-verify-ok">提交</button>`;
        const submit = () => {
          const inp = document.getElementById("wx-verify-input");
          const v = inp ? String(inp.value || "").trim() : "";
          if (!v) return;
          wxVerifyCode = v;
          pollWeixinLogin();
        };
        const ok = document.getElementById("wx-verify-ok");
        const input = document.getElementById("wx-verify-input");
        if (ok) ok.onclick = submit;
        if (input) {
          input.onkeydown = (ev) => { if (ev.key === "Enter") submit(); };
          input.focus();
        }
      }
      return; // 停在输入框上等用户提交，不继续轮询
    }
    if (st.status === "expired") {
      // 过期自动换新码继续等，但限制连续次数；检测到扫描会清零计数
      wxLoginQrcode = "";
      wxVerifyCode = "";
      if (!document.getElementById("wx-qr-area")) return; // 面板已关，别在后台空转
      if (wxLoginRefreshCount >= 3) {
        if (el) el.textContent = "二维码已过期，请点「生成登录二维码」重新开始。";
        return;
      }
      wxLoginRefreshCount++;
      if (el) el.textContent = "二维码已过期，正在自动重新生成…";
      wxLoginTimer = setTimeout(startWeixinLogin, 1500);
      return;
    }
    if (st.status === "scaned") { wxLoginRefreshCount = 0; wxVerifyCode = ""; } // 配对码被接受
    if (el) el.textContent = st.status === "scaned" ? "已扫描，请在手机上确认…" : "等待扫描…";
    wxLoginTimer = setTimeout(tick, 1000);
  };
  tick();
}

function bindChannelEvents() {
  const wxLogin = document.querySelector(".channel-wx-login");
  if (wxLogin) wxLogin.onclick = () => {
    wxLoginRefreshCount = 0; // 手动重开视为新一轮，恢复自动换新额度
    startWeixinLogin();
  };

  const wxLogout = document.querySelector(".channel-wx-logout");
  if (wxLogout) wxLogout.onclick = async () => {
    if (!(await confirmModal("退出登录后需要重新扫码才能使用微信渠道，继续吗？",
      { title: "退出微信登录", okLabel: "退出登录", danger: true }))) return;
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

  // 权限请求推渠道：只提醒、不审批。失败时把复选框退回原状，别让界面
  // 显示一个其实没生效的开关。
  const permPush = document.getElementById("channel-perm-push");
  if (permPush) permPush.onchange = async (e) => {
    const wanted = !!e.target.checked;
    try {
      await request("channel.set_permission_push", { enabled: wanted });
      channelMsg(wanted
        ? "已开启：权限请求会推到各渠道（不含审批按钮，确认仍在桌面端）"
        : "已关闭权限请求推送", "ok");
    } catch (err) {
      e.target.checked = !wanted;
      channelMsg("保存失败: " + err.message, "bad");
    }
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
      // webhook 是纯出站：没有 chat_id 概念，直接对配置的 URL 试发一条
      if (name === "webhook") {
        try {
          const r = await request("channel.test", { name });
          channelMsg(r.ok ? "测试推送已发出，去接收端看看"
            : ("发送失败: " + (r.error || "未知原因")), r.ok ? "ok" : "bad");
        } catch (e) { channelMsg("发送失败: " + e.message, "bad"); }
        return;
      }
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
      // 名单是否为空按用户眼前输入框里的值算（重渲染后就取不到了）；
      // webhook 没有入站名单语义，空名单即广播，不算「还没配完」
      const idsEl = document.querySelector(`.channel-ids[data-name="${name}"]`);
      const idsEmpty = name !== "webhook" && !(idsEl?.value || "").trim();
      try {
        if (e.target.checked) {
          await request("channel.enable", { name });
          channelMsg(`「${label}」已启用` + (idsEmpty
            ? "。名单还是空的：先在聊天窗口给它发一句话，再回来点「加入允许名单」"
            : (name === "webhook" ? "。到点的汇总推送会发往配置的 URL" : "")), "ok");
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
  const urlEl = document.querySelector(`.channel-url[data-name="${name}"]`);
  const secretEl = document.querySelector(`.channel-secret[data-name="${name}"]`);
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
  // webhook 的推送地址与签名密钥同一待遇：留空 = 不修改（空名单语义不受影响）
  if (urlEl && urlEl.value.trim()) payload.url = urlEl.value.trim();
  if (secretEl && secretEl.value.trim()) payload.secret = secretEl.value.trim();
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
let voiceFace = null; // 这次录音服务哪一面（null = 主栏，否则是分屏 pane）

function voiceMicEl(face) {
  return face ? face.el.querySelector('[data-role="voice"]') : voiceBtn;
}
function voiceReset(keepBusy = false) {
  if (voiceTimer) { clearInterval(voiceTimer); voiceTimer = null; }
  voiceSeconds = 0;
  if (!keepBusy) voiceBusy = false;
  const btn = voiceMicEl(voiceFace);
  if (btn) {
    btn.classList.remove("recording", "busy");
    btn.title = "语音输入：点击开始录音，再点一下结束并转成文字";
  }
  voiceFace = null;
}

async function voiceStart(face) {
  voiceFace = face || null;
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
  const mic = voiceMicEl(voiceFace);
  if (mic) mic.classList.add("recording");
  if (mic) mic.title = "正在录音：点击结束并转成文字";
  const tick = () => {
    voiceSeconds += 1;
    if (mic) mic.title = `正在录音 ${voiceSeconds}s：点击结束并转成文字`;
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
  const micEl = voiceMicEl(voiceFace);
  if (micEl) micEl.classList.remove("recording");
  if (micEl) micEl.classList.add("busy");
  if (micEl) micEl.title = "正在识别…";
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
    const input = faceInputEl(voiceFace);
    const text = (r.text || "").trim();
    if (text) {
      input.value = input.value ? input.value.replace(/\s*$/, " ") + text : text;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      input.dispatchEvent(new Event("input"));
      autoGrowInput(input);
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

// 技能页两个视图的按钮接线（openSkillManage / resetSkillView / renderSkillSummary
// 的函数本体在 app-skills.js；该文件先于本文件加载，接线时已可引用）

document.getElementById("skill-summary").onclick = () => openSkillManage();
document.getElementById("btn-skill-back").onclick = () => {
  resetSkillView();
  renderSkillSummary(bootSnap ? bootSnap.skills || [] : []);
};

// 关于页（更新检查 / 目录与诊断 / 备份恢复 / 数据管理 / 存储清理）的
// 函数本体在 app-about.js（该文件先于本文件加载）；下方为加载期接线。

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

// ---------- 窄屏抽屉手势（纯表现层增强）：左缘右滑开侧栏、抽屉上左滑收回、右缘左滑开右面板 ----------
// 只拨弄既有的 classList / rightCollapsed 通道（与 ☰、顶栏开关同路径），不动任何业务状态；
// passive 监听、不 preventDefault——纵向滚动与系统手势都不受影响，老内核吃不到事件时静默无手势。
(function initDrawerGestures() {
  let sx = 0, sy = 0, tracking = false;
  document.addEventListener("touchstart", (ev) => {
    tracking = false;
    if (!NARROW_MQ.matches || !ev.touches.length) return;
    const t = ev.touches[0];
    sx = t.clientX; sy = t.clientY; tracking = true;
  }, { passive: true });
  document.addEventListener("touchmove", (ev) => {
    if (!tracking || !ev.touches.length) return;
    const t = ev.touches[0];
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if (Math.abs(dx) < 64) return;                       // 未到阈值继续观察
    tracking = false;
    if (Math.abs(dy) > Math.abs(dx) * 0.6) return;       // 偏纵向的滑动不抢（列表滚动优先）
    const fromLeftEdge = sx <= 24, fromRightEdge = sx >= window.innerWidth - 24;
    const sidebarOpen = document.body.classList.contains("sidebar-open");
    const inSidebar = sidebarOpen && sx <= Math.min(320, window.innerWidth * 0.84); // 与抽屉 CSS 宽度一致
    if (dx > 0 && fromLeftEdge && !sidebarOpen) {
      document.body.classList.add("sidebar-open");
    } else if (dx < 0 && inSidebar) {
      document.body.classList.remove("sidebar-open");
    } else if (dx < 0 && fromRightEdge && !sidebarOpen && rightTabs.length && rightCollapsed) {
      rightCollapsed = false;                            // 与顶栏开关同一条通道（内部含手机互斥）
      renderRightPanel();
    }
  }, { passive: true });
})();
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
let acceptMode = "confirm"; // 主栏盾牌当前显示的档位（活动会话的档位或引擎默认）
let engineAcceptMode = "confirm"; // 引擎默认档（ui.json 的 accept_edits；新会话的起点）
const sessionAcceptCache = new Map(); // sid → 权限档位（会话级三档）
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
function paintAcceptIcon(btn, mode) {
  if (!btn) return;
  const m = ACCEPT_ICONS[mode] ? mode : "confirm";
  btn.innerHTML = ACCEPT_ICONS[m];
  btn.title = ACCEPT_TITLE[m];
  btn.classList.toggle("on", m === "accept_edits");
  btn.classList.toggle("full", m === "full_access");
  return m;
}
function renderAcceptSwitch(mode) {
  const b = document.getElementById("accept-switch");
  if (!b) return;
  acceptMode = paintAcceptIcon(b, mode);
}
// 主栏盾牌显示的是「活动会话」的档位（每会话独立）；空标签显示引擎默认档
// （也是新会话的起点）。切标签即刷新，调档只写当前会话/引擎默认，不串别的会话。
async function refreshAcceptChip() {
  const sid = activeTab && activeTab.sid;
  const btn = document.getElementById("accept-switch");
  if (!sid) { paintAcceptIcon(btn, engineAcceptMode); acceptMode = engineAcceptMode; return; }
  try {
    const r = await request("session.accept_get", { id: sid });
    sessionAcceptCache.set(sid, r.mode);
    acceptMode = paintAcceptIcon(btn, r.mode);
  } catch (e) { /* 读不到保持现状（切标签时会再试） */ }
}
document.getElementById("accept-switch").onclick = async () => {
  const order = ["confirm", "accept_edits", "full_access"];
  const sid = activeTab && activeTab.sid;
  let cur = sid ? sessionAcceptCache.get(sid) : acceptMode;
  if (sid && !cur) {
    try {
      const g = await request("session.accept_get", { id: sid });
      cur = g.mode;
      sessionAcceptCache.set(sid, cur);
    } catch (e) { addNotice("读取权限档位失败: " + e.message); return; }
  }
  const next = order[(order.indexOf(cur || "confirm") + 1) % order.length];
  try {
    if (sid) {
      const r = await request("session.accept_set", { id: sid, mode: next });
      sessionAcceptCache.set(sid, r.mode);
    } else {
      const r = await request("permission.set_mode", { mode: next });
      engineAcceptMode = r.mode;
    }
    paintAcceptIcon(document.getElementById("accept-switch"), next);
    acceptMode = next;
    addNotice(ACCEPT_NOTICE[next] || ACCEPT_NOTICE.confirm);
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

// Hooks 面板的函数本体在 app-advanced.js（该文件先于本文件加载）；
// 下方为加载期按钮接线。

document.getElementById("btn-hook-add-pre").onclick = () => addHookRule("pre");
document.getElementById("btn-hook-add-post").onclick = () => addHookRule("post");
document.getElementById("btn-hook-add-stop").onclick = () => addHookRule("stop");
document.getElementById("btn-hooks-save").onclick = () => saveHooks();

document.getElementById("btn-hook-test").onclick = hookTestRun;
// stop 类型没有工具名/参数：藏起不相干输入
document.getElementById("hook-test-kind").onchange = (e) => {
  const isStop = e.target.value === "stop";
  document.getElementById("hook-test-tool").style.display = isStop ? "none" : "";
  document.getElementById("hook-test-input-row").style.display = isStop ? "none" : "";
};

// ---------- 设置 · 运行中心：全局运行列表 + 权限队列 ----------
// 按会话 id 打开主栏标签：从运行中心/权限队列跳转用。侧栏列表里找得到就复用
// 它的数据；找不到（如分屏列独有的会话）直接用 id 打开（历史会懒加载）。
async function activateSessionById(sid) {
  if (!sid) return;
  const li = document.querySelector(`#session-list li[data-sid="${CSS.escape(sid)}"]`);
  if (li) { li.click(); return; }
  openTabForSession(sid, "");
}
const RUN_STATE_META = {
  queued:        { label: "排队中",   cls: "run-st-wait" },
  thinking:      { label: "正在思考", cls: "run-st-run" },
  tool_running:  { label: "正在执行工具", cls: "run-st-run" },
  waiting_permission: { label: "等待确认", cls: "run-st-perm" },
  retrying:      { label: "重试中",  cls: "run-st-warn" },
  cancelled:     { label: "已取消",  cls: "run-st-dead" },
  failed:        { label: "失败",    cls: "run-st-bad" },
  completed:     { label: "已完成",  cls: "run-st-ok" },
  partial:       { label: "部分完成", cls: "run-st-warn" },
  interrupted:   { label: "已中断（应用重启）", cls: "run-st-bad" },
};

function runStateChip(state) {
  const meta = RUN_STATE_META[state] || { label: state, cls: "" };
  return `<span class="run-state ${meta.cls}">${escapeHtml(meta.label)}</span>`;
}

function fmtRunTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  return sameDay ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

async function loadRunsPage() {
  const listBox = document.getElementById("runs-list");
  const permBox = document.getElementById("perm-queue-box");
  if (!listBox) return;
  let runs = [], pending = [];
  try {
    const [r, pq] = await Promise.all([
      request("runs.list", { limit: 100 }),
      request("permission.queue", {}),
    ]);
    runs = r.runs || [];
    pending = pq.pending || [];
  } catch (e) {
    listBox.innerHTML = `<p class="empty-hint">加载失败：${escapeHtml(e.message)}</p>`;
    return;
  }
  // 权限请求队列（跨会话）：等待确认的运行置顶可见
  if (pending.length) {
    permBox.innerHTML = `
      <h4>等待确认（${pending.length}）</h4>
      ${pending.map((p) => `
        <div class="run-row run-perm-row">
          <span class="run-state run-st-perm">等待确认</span>
          <span class="run-title">${escapeHtml(p.tool_name || "工具调用")}</span>
          <span class="run-sub">${escapeHtml(p.detail || "")}</span>
          <button class="btn-ghost run-goto" data-sid="${escapeHtml(p.session_id || "")}">去处理</button>
        </div>`).join("")}`;
    permBox.querySelectorAll(".run-goto").forEach((btn) => {
      btn.onclick = () => {
        const sid = btn.dataset.sid;
        activateSessionById(sid).catch(() => {});
      };
    });
  } else {
    permBox.innerHTML = "";
  }
  if (!runs.length) {
    listBox.innerHTML = '<p class="empty-hint">还没有运行记录。发一条消息、或派一个后台子代理任务后，这里会显示它的状态。</p>';
    return;
  }
  listBox.innerHTML = runs.map((r) => {
    const kindChip = r.kind === "subagent"
      ? '<span class="chip">子代理</span>'
      : '<span class="chip">主对话</span>';
    const active = r.running;
    const actions = [];
    if (active) {
      actions.push(`<button class="btn-ghost run-cancel" data-id="${escapeHtml(r.id)}">停止</button>`);
    } else if (r.state === "failed" || r.state === "interrupted") {
      if (r.retryable && r.retry_text) {
        const label = r.kind === "subagent" ? "重跑" : "重发";
        actions.push(`<button class="btn-ghost run-retry" data-id="${escapeHtml(r.id)}">${label}</button>`);
      }
    }
    if (r.session_id) {
      actions.push(`<button class="btn-ghost run-goto" data-sid="${escapeHtml(r.session_id)}">打开会话</button>`);
    }
    if (r.error) {
      actions.push(`<button class="btn-ghost run-copy" data-err="${escapeHtml(r.error)}">复制错误</button>`);
    }
    return `
      <div class="run-row" data-id="${escapeHtml(r.id)}">
        <div class="run-main">
          ${runStateChip(r.state)}
          <span class="run-title">${escapeHtml(r.title || r.id)}</span>
          ${kindChip}
        </div>
        <div class="run-sub">${escapeHtml(r.detail || "")}${r.error ? ` · <span class="run-err">${escapeHtml(r.error)}</span>` : ""}</div>
        <div class="run-meta">${fmtRunTime(r.created_at)}${r.tools_used ? ` · 工具 ${r.tools_used} 次` : ""}${r.retries ? ` · 重试 ${r.retries} 次` : ""}</div>
        <div class="run-ops">${actions.join("")}</div>
      </div>`;
  }).join("");
  listBox.querySelectorAll(".run-cancel").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try { await request("runs.cancel", { run_id: btn.dataset.id }); await loadRunsPage(); }
      catch (e) { addNotice("停止失败: " + e.message); btn.disabled = false; }
    };
  });
  listBox.querySelectorAll(".run-retry").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const r = await request("runs.retry", { run_id: btn.dataset.id });
        if (r.kind === "subagent") {
          // 子代理重跑 = 后端按原 agent_type/prompt 重新派生任务，直接刷新列表
          addNotice(`已按原任务重新派发子代理${r.task_id ? `（${r.task_id}）` : ""}`);
          await loadRunsPage();
          return;
        }
        // 重发 = 把原文在同会话再发一次：切到该会话、填进输入框、走正常发送
        if (r.session_id) await activateSessionById(r.session_id).catch(() => {});
        if (r.text) {
          const input = document.getElementById("input");
          input.value = r.text;
          input.dispatchEvent(new Event("input"));
          await send();
        }
        backToChat();
      } catch (e) { addNotice("重发失败: " + e.message); btn.disabled = false; }
    };
  });
  listBox.querySelectorAll(".run-copy").forEach((btn) => {
    btn.onclick = async () => {
      try { await navigator.clipboard.writeText(btn.dataset.err); btn.textContent = "已复制"; }
      catch { addNotice("复制失败，请手动选中错误文本"); }
    };
  });
  listBox.querySelectorAll(".run-goto").forEach((btn) => {
    btn.onclick = () => {
      const sid = btn.dataset.sid;
      activateSessionById(sid).then(backToChat).catch(() => {});
    };
  });
}
document.getElementById("runs-refresh").onclick = () => loadRunsPage();

// 设置·高级（运行参数 / 目录限制 / 信任清单）的函数本体在 app-advanced.js；
// 下方为加载期接线。
document.getElementById("btn-advanced-save").onclick = saveAdvanced;
document.getElementById("btn-network-save").onclick = saveNetwork;
document.getElementById("adv-compaction-auto").onchange = syncCompactionInputs;
document.getElementById("adv-job-containment").onchange = syncSandboxLevel;

// 恢复默认：填回默认值并立即保存（40 轮 / 1,000,000 / 自动压缩开 / 触发 90% / 保留 8 / 预算不限）
document.getElementById("btn-advanced-reset").onclick = async () => {
  const q = (id) => document.getElementById(id);
  q("adv-max-iterations").value = 40;
  q("adv-context-limit").value = 1000000;
  q("adv-compaction-auto").checked = true;
  syncCompactionInputs();
  q("adv-keep-recent").value = 8;
  q("adv-compaction-trigger").value = 90;
  q("adv-daily-budget").value = "";
  q("adv-turn-budget").value = "";
  q("adv-fallback-model").value = "";
  await saveAdvanced();
};

// 关于页 B 段（打开目录 / 备份 / 数据管理 / 存储）函数本体在 app-about.js；
// 下方与上方保留的接线在各块原位置。
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

// 跨机迁移（P24）：导出打包（密钥与 mcp.json 不进包）→ b64 zip 下载；
// 导入选 zip → 确认替换（后端先自动备份本机数据）→ 重启提示。仅本机设置页可达。
document.getElementById("btn-migration-export").onclick = async () => {
  const btn = document.getElementById("btn-migration-export");
  const msg = document.getElementById("migration-msg");
  btn.disabled = true;
  if (msg) { msg.textContent = "正在打包（大会话库可能要等一会儿）…"; msg.className = "io-msg"; }
  try {
    const r = await request("migration.export");
    downloadDataZip(r.filename, r.data_b64);
    if (msg) {
      msg.textContent = `✓ 已导出（${((r.size || 0) / 1024).toFixed(0)} KB），模型密钥与 MCP 配置不进包。`;
      msg.className = "io-msg ok";
    }
  } catch (e) {
    if (msg) { msg.textContent = "✗ " + e.message; msg.className = "io-msg bad"; }
  } finally {
    btn.disabled = false;
  }
};

document.getElementById("btn-migration-import").onclick = () => {
  document.getElementById("migration-file").click();
};

document.getElementById("migration-file").onchange = async () => {
  const input = document.getElementById("migration-file");
  const btn = document.getElementById("btn-migration-import");
  const msg = document.getElementById("migration-msg");
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  if (file.size > 96 * 1024 * 1024) {
    addNotice("迁移包超过 96MB（后端上限 64MB）——请确认选择的是 SkySheep 迁移包");
    return;
  }
  const b64 = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const m = /^data:[^;]+;base64,(.+)$/.exec(String(reader.result || ""));
      resolve(m ? m[1] : "");
    };
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
  if (!b64) { addNotice("读取迁移包失败"); return; }
  const ok = await confirmModal(
    "导入将替换本机的会话库、技能、界面偏好与配置（模型密钥与 MCP 配置不进包、需重填）。"
    + "导入前会自动备份当前数据到 backups 目录，完成后需重启应用生效。确定导入？",
    { title: "导入迁移包", okLabel: "导入" });
  if (!ok) return;
  btn.disabled = true;
  if (msg) { msg.textContent = "正在导入…"; msg.className = "io-msg"; }
  try {
    const r = await request("migration.import", { data_b64: b64 });
    const man = r.manifest || {};
    const bits = [];
    if (man.db_restored) bits.push("会话已还原");
    if (man.skills_count) bits.push(`技能 ${man.skills_count} 个（新增 ${(man.skills_new || []).length}，覆盖 ${(man.skills_overwritten || []).length}）`);
    if (man.config_restored) bits.push("配置已还原（密钥需重填）");
    if (msg) {
      msg.textContent = "✓ 导入完成" + (bits.length ? "：" + bits.join("，") : "") + "——重启应用后生效。";
      msg.className = "io-msg ok";
    }
    addNotice("迁移包导入完成，重启应用后生效。导入前的数据已自动备份到 backups 目录。");
  } catch (e) {
    if (msg) { msg.textContent = "✗ " + e.message; msg.className = "io-msg bad"; }
  } finally {
    btn.disabled = false;
  }
};


document.getElementById("btn-feedback").onclick = async () => {
  const msg = document.getElementById("diag-msg");
  msg.textContent = "正在打包并打开反馈页…";
  try {
    await openFeedbackPage();
    msg.textContent = "✓ 已生成诊断包并打开反馈页——把诊断包 zip 拖进附件，描述问题即可";
    msg.className = "io-msg ok";
  } catch (e) {
    msg.textContent = "✗ 打开反馈页失败：" + e.message + "（可手动访问：" + REPO_PAGE + "/issues）";
    msg.className = "io-msg bad";
  }
};


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

// 面板一次性接线：输入框回车快速添加、「＋ 新建事项」按钮
wireProjectTasksPanel();

document.getElementById("btn-retention-save").onclick = saveRetention;

document.getElementById("btn-retention-sweep").onclick = async () => {
  const btn = document.getElementById("btn-retention-sweep");
  btn.disabled = true;
  try {
    const r = await request("retention.sweep");
    await renderRetentionCfg();
    retentionStatus(r.ran
      ? `✓ 已清理 ${r.deleted} 个文件，释放 ${retentionBytes(r.bytes)}`
      : "今天已经自动清理过了");
  } catch (e) {
    retentionStatus("✗ 清理失败：" + e.message, false);
  } finally {
    btn.disabled = false;
  }
};


document.getElementById("btn-storage-refresh").onclick = () => renderStorageCard().catch(() => {});

// Mods 面板的函数本体在 app-mods.js（该文件先于本文件加载）；
// 下方为加载期接线。

// 「＋ 安装 Mod」：两段式——先拿内容预览，确认后才落盘（照技能导入的手感）
document.getElementById("btn-mod-install").onclick = () => {
  const box = document.createElement("div");
  const canPick = typeof nativePickerAvailable === "function" && nativePickerAvailable();
  box.innerHTML = `
    <p class="dim small">安装来源：一个 Mod 文件夹（含 <code>mod.json</code>）或 .zip 压缩包。
    安装前会先展示清单与入口内容预览，<b>你确认后才会写入</b>。也认 Claude Code Mods 的
    <code>mods.json</code> 清单（实验性兼容，能力收敛到 SkySheep 子集）。</p>
    <div class="form-grid">
      <label class="wide">Mod 文件夹 / .zip 路径
        <span class="model-line">
          <input data-f="source" placeholder="${canPick ? "点右边按钮选择，或直接粘贴路径" : "把 Mod 文件夹或 .zip 的完整路径粘贴到这里"}" autocomplete="off">
          <button class="btn-ghost browse" type="button">选择…</button>
        </span>
      </label>
      <div class="form-status"></div>
    </div>`;
  const input = box.querySelector('input[data-f="source"]');
  const status = box.querySelector(".form-status");
  const browse = box.querySelector(".browse");
  if (!canPick) browse.title = "当前是浏览器模式，请手动粘贴路径";
  browse.onclick = async () => {
    const r = await pickPath("dir");
    if (r.paths && r.paths.length) { input.value = r.paths[0]; return; }
    // 文件夹选择框打不开（浏览器模式等）再试 zip；用户主动取消就到此为止
    if (r.error) {
      const z = await pickPath("skill_zip");
      if (z.paths && z.paths.length) { input.value = z.paths[0]; return; }
      status.textContent = "打不开系统选择框，请手动粘贴路径";
      status.className = "form-status bad";
    }
  };
  showModal("安装 Mod（实验性）", box, async () => {
    const source = input.value.trim();
    if (!source) throw new Error("请选择或粘贴 Mod 路径");
    status.textContent = "正在读取并校验…";
    status.className = "form-status";
    const p = await request("mods.install", { source });
    // 第二步：预览确认（真正的落盘确认在这里）。showModal 没有 onCancel 回调，
    // 不能用「外层 await 内层 Promise」的套法——点「取消」内层 Promise 永不
    // settle，外层 onOk 就挂在半路。确认动作直接放进内层 onOk：
    // 取消 = 只关弹层不动盘；出错 = 行内报错、弹层留在原地可重试。
    const prev = document.createElement("div");
    const declText = [];
    if (p.declarative) {
      (p.declarative.deny_tools || []).forEach((x) => declText.push("拒执行 " + x));
      (p.declarative.require_confirm_tools || []).forEach((x) => declText.push("强制确认 " + x));
    }
    prev.innerHTML = `
      <p><b>${escapeHtml(p.name || "")}</b> <span class="dim small">v${escapeHtml(p.version || "")} · id: ${escapeHtml(p.id)}</span></p>
      <p class="dim small">${escapeHtml(p.description || "")}</p>
      <p class="dim small">挂点：${escapeHtml((p.hooks || []).join(" / ") || "无")} ·
        ${p.permissions === "tighten" ? "收紧档" : "观察档"} · ${Number(p.files) || 0} 个文件 · ${Number(p.bytes) || 0} 字节</p>
      ${declText.length ? `<p class="dim small">收紧声明：${escapeHtml(declText.join("；"))}</p>` : ""}
      ${p.cc_mods ? '<p class="dim small">声明了实验性 CC Mods 兼容。</p>' : ""}
      <h4 class="mod-detail-cap">main.js 开头</h4>
      <pre class="mod-readme">${escapeHtml(p.entry_head || "")}</pre>`;
    showModal(`确认安装「${p.name || p.id}」`, prev, async () => {
      await request("mods.confirm_install", { install_token: p.install_token });
      modsStatus(`✓ 已安装 ${p.name || p.id}（立即生效）`);
      renderModsCfg().catch(() => {});
    }, "安装");
  });
};

// 总开关
document.getElementById("mods-toggle").onchange = async (e) => {
  try {
    await request("mods.set_enabled", { enabled: e.target.checked });
    modsStatus(`✓ Mods 已${e.target.checked ? "启用" : "关闭"}（立即生效）`);
  } catch (err) {
    modsStatus("✗ " + err.message, false);
  }
  renderModsCfg().catch(() => {});
};

document.getElementById("btn-mods-refresh").onclick = () => renderModsCfg().catch(() => {});

// 「存为草稿」：把 Agent 回答里的 mod.json / main.js（README.md 可选）粘贴进来，
// 走 mods.save_draft 落到 <项目>/.skysheep/mods-drafts/<id>/。Agent 侧没有保存草稿
// 的工具面（save_draft 是本机专属 WS 方法，不进 Agent 工具清单），这一步由人完成；
// 草稿不进 Mods 根、不会生效，还要点「＋ 安装 Mod」选草稿目录确认安装（两道人工动作）。
document.getElementById("btn-mod-save-draft").onclick = () => {
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">把 Agent 回答里的两段代码粘贴到这里：清单 <code>mod.json</code>（会校验
    字段白名单与 id）与入口 <code>main.js</code>（README.md 可选）。保存为草稿<b>不会安装</b>——
    保存后点「＋ 安装 Mod」，选择下面返回的草稿目录，预览确认后才落盘生效。</p>
    <div class="form-grid">
      <label class="wide">mod.json（清单 JSON）
        <textarea data-f="manifest" rows="8" placeholder='{
  "id": "my-mod", "name": "我的 Mod", "version": "1.0.0",
  "hooks": ["tool_pre"], "permissions": "observe"
}'></textarea>
      </label>
      <label class="wide">main.js（入口 JS，单文件 ≤128KB）
        <textarea data-f="main_js" rows="10" placeholder="export default { toolPre(p) { return {} } }"></textarea>
      </label>
      <label class="wide">README.md（可选）
        <textarea data-f="readme" rows="3"></textarea>
      </label>
      <div class="form-status"></div>
    </div>`;
  const status = box.querySelector(".form-status");
  showModal("保存 Mod 草稿（实验性）", box, async () => {
    const manifest = box.querySelector('[data-f="manifest"]').value.trim();
    const mainJs = box.querySelector('[data-f="main_js"]').value;
    const readme = box.querySelector('[data-f="readme"]').value;
    if (!manifest) throw new Error("请粘贴 mod.json 清单内容");
    if (!mainJs.trim()) throw new Error("请粘贴 main.js 入口内容");
    // 目录 id 直接取清单里的 id 字段（后端要求两者一致，不让用户填两遍）
    let manifestObj;
    try { manifestObj = JSON.parse(manifest); } catch (e) {
      throw new Error("mod.json 不是有效 JSON：" + e.message);
    }
    const modId = String((manifestObj && manifestObj.id) || "").trim();
    if (!modId) throw new Error("清单里要有 id 字段（它同时是草稿目录名）");
    status.textContent = "正在校验并保存…";
    status.className = "form-status";
    const r = await request("mods.save_draft", { id: modId, manifest: manifestObj, main_js: mainJs, readme });
    status.textContent = `✓ 已保存草稿「${r.name}」`;
    status.className = "form-status ok";
    // 草稿路径回显给「＋ 安装 Mod」用（本机专属方法，回本机路径没有泄露面）
    modsStatus(`✓ 已保存草稿「${r.name}」到 ${r.path}——点「＋ 安装 Mod」选这个目录完成安装`);
  }, "保存草稿");
};

// 「让 AI 帮我写一个 Mod」：模板填入输入框由用户发送；草稿安装仍是两道人工动作
document.getElementById("btn-mod-template").onclick = async () => {
  try {
    const d = await request("mods.get_template");
    const text = d.template || "";
    const hint = document.getElementById("mods-draft-hint");
    if (pageViewOpen()) backToChat();
    inputEl.value = text;
    autoGrowInput();
    inputEl.focus();
    inputEl.setSelectionRange(text.length, text.length);
    if (hint) {
      hint.textContent = "提示词已填入输入框：补一句你的需求再发送。Agent 给出代码后，回到本页点" +
        "「存为草稿」把 mod.json 与 main.js 粘贴保存（Agent 自己保存不了草稿），再点「＋ 安装 Mod」确认安装。";
      hint.className = "card-status ok";
      hint.hidden = false;
      autoHideStatus(hint, hint.textContent, true);
    }
  } catch (e) {
    addNotice("获取模板失败：" + e.message);
  }
};

// ---------- PWA：手机端「添加到主屏幕」（局域网 / Tailscale 访问时） ----------
// 只在非本机来源注册 service worker：桌面窗口与本机浏览器都走 127.0.0.1 /
// localhost，保持静态资源 no-store 直读最新版的行为不被 SW 缓存层插足。
// 平台约束（如实定位，不是配置问题）：官方分发的手机地址都是 http 明文，
// 不是安全上下文，navigator.serviceWorker 根本不存在——SW 缓存与 Android 的
// PWA 安装在无 HTTPS 前置（如 tailscale serve / 反向代理）时不可用，手机端
// 实际所得是 iOS 主屏图标 + manifest 主题色。注册失败静默：PWA 是增强能力，
// 不影响正常使用；控制台留一句可查的说明，避免「装了 PWA 就有缓存加速」的
// 错误预期。
(function () {
  var host = location.hostname;
  if (host === "localhost" || host === "127.0.0.1" || host === "::1" ||
      host === "[::1]" || host === "") return;
  if (!("serviceWorker" in navigator)) {
    console.info("SkySheep：当前地址不是安全上下文（HTTPS），Service Worker 缓存与 PWA 安装不可用；" +
      "手机端可用 iOS 主屏图标。需要缓存加速请用 HTTPS 前置（如 tailscale serve）。");
    return;
  }
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  });
})();

// ============================================================
// 全局命令面板（Ctrl+K）
// 纯前端快启入口：呼出一个可搜索的动作清单，动作全部来自本文件与各
// app-*.js 模块的既有全局函数与既有入口（新建会话 / 协作模式 / 主题 /
// 设置页与设置卡 / 右面板页签 / 工厂看板 / 导出诊断包 / 最近会话），
// 不新增任何后端方法；「导出诊断包」与 设置 · 关于 的按钮走同一个
// app.export_diagnostics。面板 DOM 骨架在 index.html（#palette），
// 样式在 app.css 的「全局命令面板」段。
//
// 交互：Ctrl+K 呼出 / 再按关闭（分发在本文件上方的快捷键总控里）；
// Esc 关闭；↑↓ 选择（到头绕回）；Enter 执行；输入即子串过滤（大小写
// 不敏感，标签 + 关键词）；点面板外关闭。会话清单在每次打开时异步拉取
// （本机优先跨项目，远程端被门控拒绝就退回当前项目），拉到前先陈列
// 静态命令，拉到后并入清单。
// ============================================================

// ---------- 命令面板：DOM 与状态 ----------
const paletteEl = document.getElementById("palette");
const paletteInputEl = document.getElementById("palette-input");
const paletteListEl = document.getElementById("palette-list");
let paletteItems = [];  // 本次打开的全量动作 {label, hint, tag, group, kwd, primary, run}
let paletteShown = [];  // 过滤后的可见动作（paletteItems 的子集，下标对齐选中行）
let paletteSel = 0;     // 当前高亮行（paletteShown 下标）
let paletteOn = false;  // 面板开着吗（DOM hidden 之外的一份状态：会话异步回填时判）

// 默认（空查询）陈列上限：主命令太多会淹没会话，超出部分输入即达
const PALETTE_DEFAULT_CMDS = 20;
const PALETTE_DEFAULT_SESS = 8;
const PALETTE_FILTER_CAP = 60;

function togglePalette() {
  if (paletteOn) closePalette();
  else openPalette();
}

function openPalette() {
  if (!paletteEl || !paletteInputEl || !paletteListEl) return;
  paletteOn = true;
  paletteEl.classList.remove("hidden");
  paletteInputEl.value = "";
  paletteSel = 0;
  // 每次打开重建静态动作：设置导航文案 / 协作与主题的「当前」标记都可能变
  paletteItems = paletteBaseItems();
  palettePaint();
  paletteInputEl.focus();
  // 会话异步并入：期间面板可能已被关掉，回填前再看一眼状态
  paletteSessionItems().then((sess) => {
    if (!paletteOn) return;
    paletteItems = paletteItems.concat(sess);
    palettePaint();
  });
}

function closePalette() {
  paletteOn = false;
  if (paletteEl) paletteEl.classList.add("hidden");
  if (paletteInputEl) paletteInputEl.value = "";
}

// 面板打开时 Esc 随时能关（焦点可能不在搜索框里）：捕获阶段拦下，
// 不让上方窗口级 Esc（关设置页 / 停止运行）跟着触发
document.addEventListener("keydown", (e) => {
  if (!paletteOn) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closePalette();
  }
}, true);

// 点面板外关闭（mousedown：与菜单类交互一致，点选动作项也不会先触发这里）
document.addEventListener("mousedown", (e) => {
  if (!paletteOn) return;
  if (paletteEl && paletteEl.contains(e.target)) return;
  closePalette();
});

// ---------- 动作清单：静态命令 ----------
function paletteBaseItems() {
  const items = [];
  // 协作模式等对话内动作：先回对话视图，让输入区的协作徽章可见可再点
  const backToChatFirst = () => { if (pageViewOpen()) backToChat(); };

  // —— 会话 ——
  items.push({
    label: "新建会话", tag: "会话", group: "命令", primary: true,
    kwd: "new session chat 新建会话 开聊天",
    // 与 #btn-new 的处理一致：整屏视图先回对话再新建
    run: async () => { backToChatFirst(); await newSessionFromHighlight(); },
  });

  // —— 协作模式（本条消息生效的一次性开关；同输入区协作菜单四选一） ——
  items.push({
    label: "协作：圆桌", tag: "协作", group: "命令", primary: true,
    hint: rtOn ? "当前" : "",
    kwd: "roundtable 多模型 共同思考 协作",
    run: () => { backToChatFirst(); setRtOn(true); },
  });
  items.push({
    label: "协作：团队", tag: "协作", group: "命令", primary: true,
    hint: teamOn ? "当前" : "",
    kwd: "team 总管 队员 工单 分工 协作",
    run: () => { backToChatFirst(); setTeamOn(true); },
  });
  items.push({
    label: "协作：对抗", tag: "协作", group: "命令", primary: true,
    hint: advOn ? "当前" : "",
    kwd: "adversarial 审查 找问题 终审 协作",
    run: () => { backToChatFirst(); setAdvOn(true); },
  });
  items.push({
    label: "协作：不用协作", tag: "协作", group: "命令", primary: true,
    hint: (!rtOn && !teamOn && !advOn) ? "当前" : "",
    kwd: "off 普通对话 单模型 关闭协作",
    run: () => { backToChatFirst(); setRtOn(false, true); setTeamOn(false, true); setAdvOn(false, true); },
  });

  // —— 页面与视图（右面板页签 / 工厂；与侧栏导航同一批入口） ——
  items.push({
    label: "工厂看板", tag: "页面", group: "命令", primary: true,
    kwd: "factory 自动化 观战 派工 产线",
    run: () => openFactoryPage(), // 设置 / 对话任一处都能直接进（openFactoryPage 自理）
  });
  [
    ["todo", "任务", "todo 子代理 步骤 事项"],
    ["agenda", "日程", "agenda 提醒 日历"],
    ["auto", "自动化", "cron 定时 编排 无人值守"],
    ["memory", "项目记忆", "agents.md 约定 记忆"],
    ["map", "记忆地图", "memmap 图谱 时间线 演化"],
    ["ext", "MCP / Skills", "mcp skills 技能 工具 管理"],
  ].forEach(([id, label, kwd]) => {
    items.push({
      label, tag: "页面", group: "命令", primary: true, kwd,
      run: () => { backToChatFirst(); openRightTab(id); },
    });
  });
  items.push({
    label: "接力工作台", tag: "页面", group: "命令", primary: true,
    kwd: "relay 主屏 副屏 交棒 工作台",
    run: () => openRelayWorkbench(), // 自带回对话与右面板处理（openRelayWorkbench）
  });

  // —— 导出诊断包（与 设置 · 关于「导出诊断包」按钮同一个 WS 方法） ——
  items.push({
    label: "导出诊断包", tag: "关于", group: "命令", primary: true,
    kwd: "diagnostics 日志 反馈 打包 排障",
    run: async () => {
      addNotice("正在生成诊断包…");
      try {
        const r = await request("app.export_diagnostics");
        addNotice(`✓ 已生成诊断包（${(r.size / 1024).toFixed(0)} KB）并打开所在文件夹`);
      } catch (e) {
        addNotice("✗ 生成诊断包失败：" + e.message);
      }
    },
  });

  // —— 设置各页：清单直接读设置导航（#settings-nav li[data-target]），
  //    页面名与顺序跟侧栏永远一致，新增页零维护 ——
  document.querySelectorAll("#settings-nav li[data-target]").forEach((li) => {
    items.push({
      label: "设置 · " + li.textContent.trim(), tag: "设置", group: "命令", primary: true,
      kwd: "settings 配置 选项",
      run: () => openSettings(li.dataset.target),
    });
  });

  // —— 六套主题 + 跟随系统：名单取上方 THEMES（改主题列表只改那一处） ——
  Object.keys(THEMES).forEach((id) => {
    items.push({
      label: "主题：" + THEMES[id].name, tag: "主题", group: "主题",
      hint: themePref === id ? "当前" : "",
      kwd: "theme 主题 " + (THEMES[id].dark ? "深色 暗色" : "浅色 亮色") + " " + id,
      run: () => applyThemeMode(id),
    });
  });
  items.push({
    label: "主题：跟随系统", tag: "主题", group: "主题",
    hint: themePref === "auto" ? "当前" : "",
    kwd: "theme auto 系统 深浅 自动",
    run: () => applyThemeMode("auto"),
  });

  // —— 设置各卡片：复用设置内搜索的卡片索引（标题 + 所属页名可搜），
  //    执行动作与搜索结果的跳转一致：开页 → 滚到卡 → 闪烁提示 ——
  settingsCardIndex().forEach((it) => {
    items.push({
      label: it.title, hint: it.pageName, tag: "设置卡", group: "设置",
      kwd: "设置 卡片 " + it.pageName,
      run: () => {
        if (settingsOpen) showSettingsPage(it.pageId);
        else openSettings(it.pageId);
        // 等页面切换渲染完再滚（同设置内搜索的时序）
        setTimeout(() => {
          it.card.scrollIntoView({ block: "center", behavior: "smooth" });
          it.card.classList.remove("ssr-flash");
          void it.card.offsetWidth; // 强制回流以重启动画
          it.card.classList.add("ssr-flash");
          setTimeout(() => it.card.classList.remove("ssr-flash"), 1600);
        }, 60);
      },
    });
  });
  return items;
}

// ---------- 动作清单：最近会话（异步） ----------
async function paletteSessionItems() {
  // 本机优先跨项目（同分组侧栏的 session.list all_projects 口径）；远程端被
  // 门控拒绝时退回当前项目清单，面板在两端都可用
  let data = await request("session.list", { all_projects: 1 }).catch(() => null);
  if (!data) data = await request("session.list").catch(() => null);
  if (!data) return [];
  const projs = ((await request("project.list").catch(() => null)) || {}).projects || [];
  const curPid = bootSnap && bootSnap.project_id != null ? String(bootSnap.project_id) : null;
  const seen = new Set();
  const rows = [...(data.sessions || []), ...(data.quick_sessions || [])]
    .filter((s) => {
      if (!s || !s.id || seen.has(s.id) || s.archived) return false;
      seen.add(s.id);
      return true;
    })
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
    .slice(0, 40);
  return rows.map((s) => {
    const proj = projs.find((p) => String(p.id) === String(s.project_id));
    const cross = s.project_id != null &&
      (curPid == null || String(s.project_id) !== curPid);
    return {
      label: s.title || "(未命名会话)",
      hint: cross && proj && proj.name ? proj.name : "",
      tag: "会话", group: "最近会话",
      kwd: "会话 对话 session " + (proj && proj.name ? proj.name : ""),
      run: () => paletteOpenSession(s),
    };
  });
}

// 打开最近会话：跨项目的先切工作项目再开（同侧栏搜索跨项目命中的既有路径）
async function paletteOpenSession(s) {
  const curPid = bootSnap && bootSnap.project_id != null ? String(bootSnap.project_id) : null;
  if (s.project_id != null && (curPid == null || String(s.project_id) !== curPid)) {
    const projs = ((await request("project.list").catch(() => null)) || {}).projects || [];
    const tp = projs.find((p) => String(p.id) === String(s.project_id));
    if (!tp || !tp.root_path) {
      addNotice("这个会话所在项目已不可用");
      return;
    }
    try {
      await request("project.switch", { path: tp.root_path });
      await applyWorkspaceData(await fetchWorkspaceData());
    } catch (e) {
      addNotice("切换到该项目失败：" + e.message);
      return;
    }
  }
  if (pageViewOpen()) backToChat();
  openTabForSession(s.id, s.title);
  refreshSessions();
}

// ---------- 过滤与渲染 ----------
function paletteRefilter() {
  const q = paletteInputEl.value.trim().toLowerCase();
  if (!q) {
    const cmds = paletteItems.filter((it) => it.primary);
    const sess = paletteItems.filter((it) => it.group === "最近会话");
    paletteShown = cmds.slice(0, PALETTE_DEFAULT_CMDS).concat(sess.slice(0, PALETTE_DEFAULT_SESS));
    return;
  }
  paletteShown = paletteItems.filter((it) =>
    it.label.toLowerCase().includes(q) ||
    (it.hint || "").toLowerCase().includes(q) ||
    (it.kwd || "").toLowerCase().includes(q)
  ).slice(0, PALETTE_FILTER_CAP);
}

function palettePaint() {
  paletteRefilter();
  paletteListEl.innerHTML = "";
  if (!paletteShown.length) {
    const d = document.createElement("div");
    d.className = "palette-empty";
    d.textContent = "没有匹配的命令或会话";
    paletteListEl.appendChild(d);
    return;
  }
  let lastGroup = null;
  paletteShown.forEach((it, i) => {
    if (it.group !== lastGroup) {
      const h = document.createElement("div");
      h.className = "palette-group";
      h.textContent = it.group;
      paletteListEl.appendChild(h);
      lastGroup = it.group;
    }
    const b = document.createElement("button");
    b.type = "button";
    b.className = "palette-item" + (i === paletteSel ? " active" : "");
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", i === paletteSel ? "true" : "false");
    b.innerHTML =
      `<span class="pi-label">${escapeHtml(it.label)}</span>` +
      (it.hint ? `<span class="pi-hint">${escapeHtml(it.hint)}</span>` : "") +
      (it.tag ? `<span class="pi-tag">${escapeHtml(it.tag)}</span>` : "");
    // mousedown 不转移焦点：点击执行后搜索框仍是落点，键盘操作不断线
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.onclick = () => paletteRunIndex(i);
    b.onmousemove = () => { if (paletteSel !== i) { paletteSel = i; palettePaintSel(); } };
    paletteListEl.appendChild(b);
  });
}

// 只翻高亮不重建 DOM：hover / 键盘移动都走这里，列表大也不闪
function palettePaintSel() {
  const rows = paletteListEl.querySelectorAll(".palette-item");
  rows.forEach((el, i) => {
    el.classList.toggle("active", i === paletteSel);
    el.setAttribute("aria-selected", i === paletteSel ? "true" : "false");
  });
  const cur = rows[paletteSel];
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

function paletteRunIndex(i) {
  const it = paletteShown[i];
  if (!it) return;
  closePalette();
  let r;
  try {
    r = it.run();
  } catch (e) {
    addNotice("执行失败：" + e.message);
    return;
  }
  if (r && typeof r.catch === "function") {
    r.catch((e) => addNotice("执行失败：" + e.message));
  }
}

// ---------- 搜索框键盘：↑↓ 选择、Enter 执行（Esc 在上面的捕获监听里统一关） ----------
if (paletteInputEl) {
  paletteInputEl.addEventListener("input", () => {
    paletteSel = 0;
    palettePaint();
  });
  paletteInputEl.addEventListener("keydown", (e) => {
    // 中文输入法组词中的 Enter / 方向键是候选操作，不是面板操作
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      if (!paletteShown.length) return;
      const dir = e.key === "ArrowDown" ? 1 : -1;
      paletteSel = (paletteSel + dir + paletteShown.length) % paletteShown.length;
      palettePaintSel();
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      paletteRunIndex(paletteSel);
    }
  });
}
