/* SkySheep 桌面前端逻辑：WebSocket 协议客户端 + 聊天 UI。无外部依赖。 */
"use strict";

/* ── 目录（按分区注释 "// ----------" 检索；新增代码请挂在最接近的分区下）────
 *
 *  ① 基础设施：迷你 Markdown 渲染 / WebSocket 客户端（request、事件路由）
 *  ② 聊天渲染：会话标签、消息卡、思考块、圆桌卡、事件处理、图片附件、
 *     消息操作（复制/编辑/分叉/回退/重生成）、输入浮层（/ 命令、@ 提及、历史）
 *  ③ 侧栏与导航：项目列表、会话搜索、会话菜单、功能导航、日程/定时任务面板
 *  ④ 顶栏：模型菜单、思考强度、通知中心、系统通知
 *  ⑤ 右侧面板：终端/浏览器/辅助对话/审查/文件/任务/宠物
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
  // 引用
  text = text.replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>");
  text = mdLists(text);
  // 粗体 / 斜体 / 删除线 / 链接
  text = text.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  text = text.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  text = text.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
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
  return {
    sid: sid || null, title: title || "", logEl, running: false,
    streamingEl: null, streamingText: "", rtCard: null, rtMemberEls: [],
    lastAssistantText: "", usage: null, needsPerm: false, permData: null,
    needHistory: false,
  };
}
function tabFor(sid) { return sid ? chatTabs.find((t) => t.sid === sid) || null : null; }
function curTab() { return routeTab || activeTab; }
function curLog() { const t = curTab(); return t ? t.logEl : chatBox; }
function scrollLog() {
  const t = curTab();
  if (t && t !== activeTab) return; // 后台标签追加内容不抢滚动
  chatBox.scrollTop = chatBox.scrollHeight;
}
function withTab(tab, fn) { routeTab = tab; try { fn(); } finally { routeTab = null; } }

function renderTabs() {
  const bar = document.getElementById("chat-tabs");
  bar.classList.toggle("hidden", chatTabs.length <= 1);
  bar.innerHTML = "";
  chatTabs.forEach((t) => {
    const el = document.createElement("div");
    el.className = "chat-tab" + (t === activeTab ? " active" : "") +
      (t.running ? " running" : "") + (t.needsPerm ? " needs-perm" : "");
    const title = t.title || (t.sid ? (sessionMeta[t.sid] || {}).title || "会话" : "新会话");
    el.innerHTML = `<span class="tab-title">${escapeHtml(title)}</span>` +
      (t.running ? '<span class="tab-dot" title="运行中"></span>' : "") +
      (t.needsPerm ? '<span class="tab-perm" title="等待你确认">🔒</span>' : "") +
      `<button class="tab-close" title="关闭标签${t.running ? "（运行中的会话会一并停止）" : ""}">✕</button>`;
    el.title = title;
    el.onclick = (e) => {
      if (e.target.closest(".tab-close")) return;
      activateTab(t);
    };
    el.querySelector(".tab-close").onclick = (e) => {
      e.stopPropagation();
      closeTab(t);
    };
    bar.appendChild(el);
  });
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
  // 后台标签从未渲染过历史（事件创建的）→ 拉一次历史；否则轻量激活
  if (tab.needHistory && tab.sid && !tab.running) {
    tab.needHistory = false;
    try {
      const info = await request("session.resume", { id: tab.sid });
      renderHistory(tab, info.messages || []);
    } catch (e) { addNotice("加载会话内容失败: " + e.message); }
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
  if (!t) {
    t = newTabObj(sid, title);
    t.needHistory = !opts.withMessages;
    chatTabs.push(t);
  }
  if (opts.withMessages) {
    renderHistory(t, opts.withMessages);
    if (!t.logEl.children.length) withTab(t, showWelcome); // 空会话回到欢迎页
  }
  if (!opts.background) activateTab(t);
  else renderTabs();
  return t;
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

function addUser(text, images) {
  const d = document.createElement("div");
  d.className = "msg user";
  // 蓝色气泡画在内层 .user-bubble 上：操作按钮行要常驻占位在气泡下方（外层不再有底色）
  const bubble = document.createElement("div");
  bubble.className = "user-bubble";
  if (text) bubble.textContent = text;
  (images || []).forEach((im) => {
    const img = document.createElement("img");
    img.className = "user-image";
    img.src = `data:${im.media_type};base64,${im.data}`;
    img.alt = "图片附件";
    img.onclick = () => window.open(img.src, "_blank");
    bubble.appendChild(img);
  });
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
}

function appendStream(txt) {
  const t = curTab();
  if (!t.streamingEl) beginAssistant();
  // 正文开始 → 思考块自动折叠（思考先于正文产出）
  if (t.thinkText && t.thinkEl && !t.thinkEl.classList.contains("folded")) {
    t.thinkEl.classList.add("folded");
    const sum = t.thinkEl.querySelector(".think-sum");
    if (sum) sum.textContent = `💭 思考过程（${Math.round(t.thinkText.length / 10) * 10} 字）· 点击展开`;
  }
  t.streamingText += txt;
  t.streamingEl.firstElementChild.innerHTML = renderMarkdown(t.streamingText) + "<p>▍</p>";
  scrollLog();
}

// ---------- 思考过程块（思考型模型：流式灰显，正文开始后折叠，可展开回看） ----------
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
      if (sum) {
        const n = (t.thinkText || "").length;
        sum.textContent = folded
          ? `💭 思考过程（${Math.round(n / 10) * 10} 字）· 点击展开`
          : "💭 思考过程 · 点击折叠";
      }
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
  t.thinkText += txt;
  el.querySelector(".think-body .md").innerHTML = renderMarkdown(t.thinkText);
  scrollLog();
}

function finishThinking(t) {
  if (!t || !t.thinkEl || !t.thinkText) return;
  const folded = t.thinkEl.classList.contains("folded");
  const sum = t.thinkEl.querySelector(".think-sum");
  if (sum && !folded) {
    sum.textContent = `💭 思考过程（${Math.round(t.thinkText.length / 10) * 10} 字）· 点击折叠`;
  }
}

// 历史恢复：按消息携带的 thinking 文本渲染折叠块
function addThinkingDone(text, tab) {
  const t = tab || curTab();
  if (!text) return;
  const el = ensureThinkEl(t);
  t.thinkText = text;
  el.querySelector(".think-body .md").innerHTML = renderMarkdown(text);
  // 历史渲染默认折叠，不占空间
  el.classList.add("folded");
  const sum = el.querySelector(".think-sum");
  if (sum) sum.textContent = `💭 思考过程（${Math.round(text.length / 10) * 10} 字）· 点击展开`;
  // 历史恢复的思考块是一次性的：随消息渲染后断开流式关联
  t.thinkEl = null;
  t.thinkText = "";
  return el;
}

function finishAssistant(rtMeta, seq) {
  const t = curTab();
  if (!t || !t.streamingEl) return;
  finishThinking(t);
  t.thinkEl = null;
  t.thinkText = "";
  t.streamingEl.firstElementChild.innerHTML = renderMarkdown(t.streamingText);
  if (rtMeta) {
    addRtBadge(t.streamingEl, rtMeta);
    // 融合结论已就位：自动折叠成员草稿卡（此前一直展开，占着大块空白）；标题栏随时可展开回看
    const allSettled = t.rtMemberEls.length &&
      t.rtMemberEls.every((x) => x.card.classList.contains("ok") || x.card.classList.contains("err"));
    if (rtMeta.members && allSettled && t.rtCard && !t.rtCard.classList.contains("folded")) {
      t.rtCard.classList.add("folded");
      const foldBtn = t.rtCard.querySelector(".rt-fold");
      if (foldBtn) foldBtn.textContent = "展开";
      const sub = t.rtCard.querySelector(".rt-sub");
      if (sub) sub.textContent = `${t.rtMemberEls.length} 个模型 · 已折叠 · 点击标题栏展开查看各成员草稿`;
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
  const okCount = (meta.members || []).filter((m) => m.status === "done").length;
  const badge = document.createElement("div");
  badge.className = "rt-badge";
  badge.title = (meta.members || [])
    .map((m) => `${m.provider}/${m.model}：${m.status === "done" ? "已参与" : "失败"}`)
    .join("\n");
  badge.textContent = `◆ 圆桌融合 · ${okCount} 个成员 + 主席`;
  el.prepend(badge);
}

// ---------- 圆桌卡片：成员草稿并列展示 + 融合进度（状态挂标签） ----------
function beginRoundtable(members) {
  const t = curTab();
  const card = document.createElement("div");
  card.className = "roundtable";
  t.rtCard = card;
  const head = document.createElement("div");
  head.className = "rt-head";
  const sub = document.createElement("span");
  sub.className = "rt-sub";
  sub.textContent = `${members.length} 个模型正在并行思考…`;
  const title = document.createElement("span");
  title.className = "rt-title";
  title.textContent = "👥 圆桌讨论";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "折叠";
  // 折叠开关：点按钮或标题栏任意处都生效（此前闭包里引用了未定义的 rtCard，点击直接抛错）
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    sub.textContent = folded
      ? `${members.length} 个模型 · 已折叠 · 点击展开查看各成员草稿`
      : `${members.length} 个模型正在并行思考…`;
  };
  fold.onclick = (e) => { e.stopPropagation(); toggleFold(); };
  head.onclick = (e) => { if (e.target !== fold) toggleFold(); };
  head.append(title, sub, fold);
  const grid = document.createElement("div");
  grid.className = "rt-grid";
  // 按成员数定列数：1/2/3 各成一列排一行，4 及以上 2×2——auto-fit 会排出 3+1 的孤行，视觉很乱
  grid.style.setProperty("--rt-cols", members.length >= 4 ? 2 : Math.max(1, members.length));
  t.rtMemberEls = members.map((m) => {
    const card = document.createElement("div");
    card.className = "rt-member";
    card.innerHTML =
      `<div class="rt-m-head"><span class="rt-m-name">${escapeHtml(m.provider)}</span>` +
      `<span class="rt-m-model">${escapeHtml(m.model)}</span>` +
      '<span class="rt-m-status">⋯</span></div>' +
      '<div class="rt-m-body"><div class="md"></div></div>';
    grid.appendChild(card);
    return {
      card,
      body: card.querySelector(".rt-m-body .md"),
      status: card.querySelector(".rt-m-status"),
      text: "",
    };
  });
  t.rtCard.append(head, grid);
  curLog().appendChild(t.rtCard);
  scrollLog();
}

function rtMemberDelta(data) {
  const t = curTab();
  const m = t.rtMemberEls[data.member_index];
  if (!m) return;
  m.text += data.text || "";
  m.body.innerHTML = renderMarkdown(m.text);
  scrollLog();
}

function rtMemberFinished(data) {
  const t = curTab();
  const m = t.rtMemberEls[data.member_index];
  if (!m || !t.rtCard) return;
  if (data.status === "error") {
    m.card.classList.add("err");
    m.status.textContent = "✗";
    if (!m.text) m.body.innerHTML = `<p class="dim">✗ ${escapeHtml(data.error || "作答失败")}</p>`;
  } else {
    m.card.classList.add("ok");
    m.status.textContent = "✓";
    // 成员草稿收尾：重渲一遍并高亮代码块（流式期间的最后一次 renderMarkdown 留下的是原文）
    m.body.innerHTML = renderMarkdown(m.text);
    renderMermaidIn(m.body);
    highlightCodeIn(m.body);
  }
  const settled = t.rtMemberEls.filter(
    (x) => x.card.classList.contains("ok") || x.card.classList.contains("err")
  ).length;
  if (settled === t.rtMemberEls.length) {
    const sub = t.rtCard.querySelector(".rt-sub");
    if (sub) sub.textContent = "草稿完成，主席融合中…";
  }
}

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
  curLog().appendChild(card);
  scrollLog();
}

function finishToolCard(data) {
  finishAssistant();
  const card = curLog().querySelector(`[data-call-id="${data.tool_call_id}"]`);
  if (!card) return;
  card.classList.add(data.is_error ? "err" : "ok");
  card.querySelector(".t-status").textContent = data.is_error ? "✗" : `✓ ${data.duration_ms}ms`;
  card.querySelector(".t-body pre").textContent =
    (data.is_error ? "[错误] " : "") + (data.preview || "(无输出)");
  // 写出的 HTML 页面 / 生成的图片：给「预览」按钮，在浏览器标签里直接看效果
  const tname = card._toolName || "";
  const tpath = String((card._toolInput || {}).path || "");
  const written = !data.is_error && tpath && (tname === "write_file" || tname === "generate_image");
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
    document.getElementById("usage").textContent =
      `tokens ↑${usageIn.toLocaleString()} ↓${usageOut.toLocaleString()}`;
  }
}

function setContextUsage(tokens, limit, tab) {
  const t = tab || activeTab;
  if (t) t.usage = limit ? { tokens, limit } : null;
  if (t && t !== activeTab) return; // 后台标签只记数，不改输入栏
  const ring = document.getElementById("ctx-ring");
  if (!limit) { ring.classList.add("hidden"); return; }
  ring.classList.remove("hidden");
  const pct = Math.min(100, Math.round((100 * tokens) / limit));
  const C = 2 * Math.PI * 9; // 环半径 r=9（viewBox 24）
  ring.querySelector(".ring-val").style.strokeDashoffset = String(C * (1 - pct / 100));
  ring.querySelector(".ring-txt").textContent = String(pct);
  ring.title = `上下文约 ${tokens.toLocaleString()} / ${limit.toLocaleString()} tokens（${pct}%），超过阈值会自动压缩历史`;
  ring.classList.toggle("warn", pct >= 70 && pct < 90);
  ring.classList.toggle("bad", pct >= 90);
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
    case "roundtable_started": beginRoundtable(data.members || []); break;
    case "roundtable_member_delta": rtMemberDelta(data); break;
    case "roundtable_member_finished": rtMemberFinished(data); break;
    case "tool_call_started": addToolCard(data); break;
    case "tool_call_finished": finishToolCard(data); break;
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
    case "notice": addNotice("⏳ " + data.message); break;
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
        const doneTab = routeTab;
        if (doneTab && doneTab.lastAssistantText.trim()) {
          maybeNotify("任务完成", "本轮任务已结束，回来看看结果");
        }
        if (doneTab && doneTab !== activeTab) renderTabs(); // 运行点熄灭
      }
      break;
    case "terminal_chunk":
      termAppend(data.text || "", data.stream === "err" ? "term-err" : "");
      break;
    case "terminal_done":
      termSetBusy(false);
      termAppend("\n[退出码 " + (data.code ?? "?") + (data.stopped ? " · 已停止" : "") + "]\n", "term-exit");
      break;
    case "aux_delta":
      if (auxStreamingEl) {
        auxStreamingText += data.text || "";
        auxStreamingEl.innerHTML = renderMarkdown(auxStreamingText);
        auxLog.scrollTop = auxLog.scrollHeight;
      }
      break;
    case "aux_thinking":
      auxThinkingDelta(data.text);
      break;
    case "turn_finished": {
      finishAssistant();
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

function showPermission(data) {
  if (routeTab && routeTab !== activeTab) {
    // 后台会话的确认请求：标到标签上，切过去再展示
    routeTab.needsPerm = true;
    routeTab.permData = data;
    maybeNotify("需要你确认", `${data.tool_name}（后台会话）正在等待决定`);
    renderTabs();
    return;
  }
  permRequest = data.request_id;
  maybeNotify("需要你确认", `${data.tool_name} 正在等待你的决定`);
  document.getElementById("perm-tool").textContent = `${data.tool_name} [${data.safety}]`;
  const argsEl = document.getElementById("perm-args");
  if (data.diff) {
    // 写入类操作：展示改前→改后 diff，确认时有真实依据
    argsEl.innerHTML = renderDiffText(data.diff);
    argsEl.classList.add("is-diff");
  } else {
    argsEl.textContent = data.detail || JSON.stringify(data.input, null, 2);
    argsEl.classList.remove("is-diff");
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

async function refreshSessions(prefetched) {
  if (sessionSearchActive) return;
  // prefetched：站内切换项目时已先取回，直接渲染（不经过网络等待，避免列表先清空）
  const { sessions, empty_count } = prefetched || await request("session.list");
  const ul = document.getElementById("session-list");
  ul.innerHTML = "";
  sessions.forEach((s) => {
    const li = document.createElement("li");
    li.innerHTML = `
      ${s.pinned ? '<span class="pin" title="已置顶">📌</span>' : ""}
      <span class="s-title">${escapeHtml(s.title || "(未命名)")}</span>
      <button class="s-export" title="导出为 Markdown">⬇</button>
      <button class="s-more" title="更多操作">⋯</button>` +
      (s.summary ? `<span class="s-sub" title="最近进展">${escapeHtml(s.summary)}</span>` : "");
    li.title = s.id;
    li.querySelector(".s-export").onclick = (e) => {
      e.stopPropagation();
      exportSession(s);
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
      ul.querySelectorAll("li").forEach((x) => x.classList.remove("active"));
      li.classList.add("active");
      const t = openTabForSession(s.id, s.title);
      clearTodoPanel();
      addNotice(`已恢复会话 ${s.title || s.id}`);
      if (t && !t.running) t.needHistory = false;
    };
    ul.appendChild(li);
  });
  if (!sessions.length) ul.innerHTML = '<li class="empty-hint">暂无会话</li>';

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
}

function exportSession(s) {
  return request("session.export", { id: s.id })
    .then((r) => downloadText(r.filename, r.markdown))
    .catch((e) => addNotice("导出失败: " + e.message));
}

// ---------- 会话搜索（标题 + 消息全文，对标 Claude Code /resume 检索） ----------
const searchEl = document.getElementById("session-search");
let searchTimer = null;
let searchScope = "project"; // project | all
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderSessionList(searchEl.value.trim()), 200);
});
// 搜索范围切换：本项目 / 全部项目（记不住某件事在哪个项目聊过时用）
document.querySelectorAll("#search-scope button").forEach((b) => {
  b.onclick = () => {
    searchScope = b.dataset.scope;
    document.querySelectorAll("#search-scope button").forEach((x) =>
      x.classList.toggle("active", x === b));
    if (searchEl.value.trim()) renderSessionList(searchEl.value.trim());
  };
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
  const r = await request("session.search", { query, scope: searchScope })
    .catch(() => ({ results: [] }));
  ul.innerHTML = "";
  if (!r.results.length) {
    ul.innerHTML = searchScope === "all"
      ? '<li class="empty-hint">所有项目里都没有匹配的会话或消息</li>'
      : '<li class="empty-hint">没有匹配的会话或消息<br><span class="dim">试试切到「全部项目」跨项目找</span></li>';
    return;
  }
  r.results.forEach((s) => {
    const li = document.createElement("li");
    li.className = "has-snippet";
    li.innerHTML =
      `<span class="s-title">${escapeHtml(s.title || "(未命名)")}</span>` +
      // 跨项目搜索时标出来自哪个项目，否则一堆同名标题分不清
      (searchScope === "all"
        ? `<span class="s-project dim small" title="${escapeHtml(s.project_path || "")}">📁 ${escapeHtml(s.project_name || "")}</span>`
        : "") +
      `<span class="s-snippet">${highlightSnippet(s.snippet, query)}</span>`;
    li.title = "点击打开这个会话";
    li.onclick = async () => {
      // 跨项目命中：先切工作项目再打开，否则会话在当前项目列表里找不到
      if (searchScope === "all" && s.project_path &&
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

function showSessionMenu(s, li, pos) {
  menuEl.innerHTML = "";
  const items = [
    { label: "✏️ 重命名", act: () => startInlineRename(s, li) },
    {
      label: s.pinned ? "📍 取消置顶" : "📌 置顶",
      act: async () => {
        await request("session.pin", { id: s.id, pinned: !s.pinned });
        addNotice(s.pinned ? `已取消置顶` : `已置顶「${s.title || "(未命名)"}」`);
        refreshSessions();
      },
    },
    { label: "📁 移动到…", act: () => moveSessionModal(s) },
    { label: "⬇ 导出 Markdown", act: () => exportSession(s) },
    { label: "🗑 删除", danger: true, act: () => deleteSessionModal(s) },
  ];
  items.forEach((it) => {
    const b = document.createElement("button");
    b.textContent = it.label;
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

function showModal(title, contentEl, onOk, okLabel = "确定") {
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
}
document.getElementById("modal-cancel").onclick = hideModal;

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
  chip.textContent = `思考 ${label}`;
  chip.title = `当前思考强度：${label} · 点击调整（自动 / 低 / 中 / 高）`;
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
    auto: "不干预，沿用服务默认行为",
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
        active: p.is_active && m === (p.active_model || p.model),
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
  modelMenu.innerHTML = "";
  const rows = buildModelRows(detail);
  if (!rows.length) {
    const hint = document.createElement("div");
    hint.className = "mm-empty";
    hint.textContent = "还没有已启用的模型服务";
    modelMenu.appendChild(hint);
  }
  rows.forEach((row) => {
    const b = document.createElement("button");
    b.className = "mm-item" + (row.active ? " active" : "");
    b.innerHTML =
      `<span class="mm-model">${row.active ? "✓ " : ""}${escapeHtml(row.model)}</span>` +
      `<span class="mm-prov${row.hasKey ? "" : " no-key"}">${row.hasKey ? "" : "⚠ "}${escapeHtml(row.name)}</span>`;
    b.title = row.hasKey
      ? `切换到 ${row.name} / ${row.model}`
      : `「${row.name}」还没配置 API Key，切换过去会失败`;
    b.onclick = async () => {
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
  });
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

function showBanner(text) {
  document.getElementById("cfg-banner-text").textContent = text;
  document.getElementById("cfg-banner").hidden = false;
}
function hideBanner() {
  document.getElementById("cfg-banner").hidden = true;
}
document.getElementById("cfg-banner-close").onclick = hideBanner;

// ---------- 用量统计（设置页 · 仪表盘布局：统计磁贴 / Token 活动柱状图 / 模型用量环形图 / 会话排行） ----------
let usageRangeDays = 14; // 范围 tabs 记忆在本窗口内

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
  const tile = (num, label, sub) =>
    `<div class="usage-tile"><div class="ut-num">${num}</div>` +
    `<div class="ut-label">${label}</div>` +
    (sub ? `<div class="ut-sub">${sub}</div>` : "") + `</div>`;
  return (
    tile(usageFmtTokens(total), "累计 Token 数", `输入 ${usageFmtTokens(r.total_in)} · 输出 ${usageFmtTokens(r.total_out)}`) +
    tile(usageFmtTokens(peak), "单日峰值 Token", days.length ? "最近 " + days.length + " 天内" : "") +
    tile(String(days.reduce((s, d) => s + ((d.it || 0) + (d.ot || 0) > 0 ? 1 : 0), 0)) + " 天", "有记录天数", `统计窗口 ${usageRangeDays} 天`) +
    tile(String((r.by_session || []).length), "参与会话", (r.by_session || []).length ? "按 tokens 排行见下方" : "")
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
  const bars = days.map((d, i) => {
    const it = d.it || 0, ot = d.ot || 0;
    const inH = Math.round((it / maxDay) * 100), outH = Math.round((ot / maxDay) * 100);
    const label = (i % step === 0 || i === days.length - 1)
      ? `<span class="ut-date">${escapeHtml(String(d.day).slice(5))}</span>`
      : '<span class="ut-date"></span>';
    return `<div class="ut-col" title="${escapeHtml(d.day)} · 输入 ${usageFmtTokens(it)} · 输出 ${usageFmtTokens(ot)}">` +
      `<div class="ut-bar"><i class="ut-in" style="height:${inH}%"></i><i class="ut-out" style="height:${outH}%"></i></div>${label}</div>`;
  }).join("");
  if (lg) {
    lg.innerHTML =
      '<span class="lg-dot in"></span>输入 tokens' +
      '<span class="lg-dot out"></span>输出 tokens' +
      `<span class="lg-total">合计 ${usageFmtTokens((r.total_in || 0) + (r.total_out || 0))}</span>`;
  }
  return `<div class="ut-chart">${bars}</div>`;
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
  // 环形图分段（stroke-dasharray）；色板取纸墨主题交互色系
  const PALETTE = ["#1257c4", "#3a7d44", "#b03a2e", "#a8720a", "#6b4fa3", "#0f7c8a", "#8a5a3a", "#57503f"];
  const R = 54, C = 2 * Math.PI * R;
  let offset = 0;
  const segs = rows.map((p, i) => {
    const frac = ((p.it || 0) + (p.ot || 0)) / total;
    const seg = `<circle class="dm-seg" cx="70" cy="70" r="${R}" fill="none"
      stroke="${PALETTE[i % PALETTE.length]}" stroke-width="20"
      stroke-dasharray="${(frac * C).toFixed(2)} ${(C - frac * C).toFixed(2)}"
      stroke-dashoffset="${(-offset * C).toFixed(2)}"></circle>`;
    offset += frac;
    return seg;
  }).join("");
  const legend = rows.map((p, i) => {
    const t = (p.it || 0) + (p.ot || 0);
    const pct = Math.round((t / total) * 100);
    const name = (p.model || p.provider || "?");
    return `<div class="dm-row"><span class="lg-dot" style="background:${PALETTE[i % PALETTE.length]}"></span>` +
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

async function loadUsage() {
  try {
    const r = await request("usage.stats", { days: usageRangeDays });
    document.getElementById("usage-tiles").innerHTML = usageTiles(r);
    document.getElementById("usage-trend").innerHTML = usageTrendChart(r);
    usageDonut(r);
    document.getElementById("usage-sessions").innerHTML = (r.by_session || [])
      .map((s) => {
        const total = (s.it || 0) + (s.ot || 0);
        return `<div class="usage-row"><span class="ud" title="${escapeHtml(s.sid)}">${escapeHtml((s.title || "未命名").slice(0, 22))}</span>` +
          `<span class="un">${usageFmtTokens(total)} tokens</span></div>`;
      }).join("") || "<p class='dim small'>暂无数据</p>";
  } catch (e) {
    document.getElementById("usage-tiles").innerHTML =
      `<p class="dim small">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}
document.getElementById("usage-refresh").onclick = () => loadUsage();
document.querySelectorAll("#usage-range-tabs button").forEach((b) => {
  b.onclick = () => {
    usageRangeDays = Number(b.dataset.days) || 14;
    document.querySelectorAll("#usage-range-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    loadUsage();
  };
});

// ---------- 快捷指令：自定义提示词模板（/ 菜单置顶展示） ----------
let snippetsCache = [];

// 内置示例模板：用户一条自定义指令都没有时，/ 菜单给几个点一下就能跑的
// 场景（第一个任务的成功时刻比空输入框更能留住新用户）。不落库、不可删，
// 用户自己的指令始终排在前面。
const BUILTIN_SNIPPETS = [
  { id: "builtin-summarize", name: "总结当前项目", content: "请浏览当前项目的结构和关键文件，用中文总结：这个项目是做什么的、怎么运行、代码组织如何。" },
  { id: "builtin-pdf", name: "总结 PDF 文档", content: "请读取 @文件.pdf，提炼要点成一页速览：核心结论（不超过 5 条）、关键数据、值得注意的风险或限制。" },
  { id: "builtin-excel", name: "Excel 转图表报告", content: "请读取 @表格.xlsx，检查数据质量（缺失/重复/格式问题），清洗后生成一份汇总报告，告诉我有什么发现。" },
  { id: "builtin-web", name: "调研一个话题", content: "请联网调研：，把最新进展整理成带信息来源的简报（重点、时间线、不同观点）。" },
  { id: "builtin-fix", name: "帮我修报错", content: "我遇到了这个报错：\n\n（粘贴报错信息）\n\n请分析原因并给出修复方案；如果是代码问题，直接读文件帮我改好。" },
];

function builtinSnippets() {
  return snippetsCache.length ? [] : BUILTIN_SNIPPETS;
}

async function loadSnippets() {
  try {
    snippetsCache = (await request("snippets.list")).snippets || [];
  } catch (e) { snippetsCache = []; }
  if (document.getElementById("snippets-list")) renderSnippets();
}

function renderSnippets() {
  const ul = document.getElementById("snippets-list");
  ul.innerHTML = "";
  if (!snippetsCache.length) {
    ul.innerHTML = '<li class="dim small" style="padding:6px 10px">还没有自定义快捷指令 —— 点右上角「＋ 新建」；' +
      '输入 / 时可先用内置示例模板。</li>';
    return;
  }
  snippetsCache.forEach((s) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="s-title">${escapeHtml(s.name)}</span>` +
      `<span class="s-sub">${escapeHtml((s.content || "").replace(/\s+/g, " ").slice(0, 60))}</span>`;
    const ops = document.createElement("span");
    ops.className = "cron-ops";
    ops.innerHTML = `<button class="cron-op" title="编辑">✎</button>` +
      `<button class="cron-op danger" title="删除">✕</button>`;
    const [edit, del] = ops.querySelectorAll("button");
    edit.onclick = (e) => { e.stopPropagation(); snippetModal(s); };
    del.onclick = async (e) => {
      e.stopPropagation();
      await request("snippets.delete", { id: s.id });
      await loadSnippets();
    };
    li.appendChild(ops);
    li.onclick = () => snippetModal(s);
    ul.appendChild(li);
  });
}

function snippetModal(existing) {
  const box = document.createElement("div");
  box.innerHTML = `
    <div class="cron-fields">
      <label>名称（/ 菜单里显示）</label>
      <input id="sn-name" class="modal-input" type="text" maxlength="40" placeholder="例如：代码审查" value="${existing ? escapeHtml(existing.name) : ""}">
      <label>内容（{{clipboard}} 会替换为剪贴板文本）</label>
      <textarea id="sn-content" class="modal-input" rows="5" placeholder="请审查以下代码，关注正确性与边界情况：&#10;${clipboard}">${existing ? escapeHtml(existing.content) : ""}</textarea>
    </div>`;
  showModal(existing ? "编辑快捷指令" : "新建快捷指令", box, async () => {
    const name = box.querySelector("#sn-name").value.trim();
    const content = box.querySelector("#sn-content").value.trim();
    if (!name || !content) throw new Error("名称与内容不能为空");
    if (existing) await request("snippets.update", { id: existing.id, name, content });
    else await request("snippets.add", { name, content });
    await loadSnippets();
  }, existing ? "保存" : "创建");
}
document.getElementById("btn-snippet-add").onclick = () => snippetModal(null);

// 插入到输入框：{{clipboard}} 占位符替换（失败则原样保留）
async function insertSnippet(s) {
  let content = s.content || "";
  if (content.includes("{{clipboard}}")) {
    try {
      const t = await navigator.clipboard.readText();
      if (t) content = content.split("{{clipboard}}").join(t);
    } catch (e) { /* 剪贴板不可用：保留占位符 */ }
  }
  const input = document.getElementById("input");
  input.value = (input.value ? input.value + "\n" : "") + content;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  autoGrowInput();
  hideInputMenu();
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
  const projPath = document.getElementById("project-path"); // 侧栏已移除工作目录框
  if (projPath) {
    projPath.textContent = snap.working_dir;
    projPath.title = snap.working_dir;
  }
  if (snap.provider_error) {
    showBanner("⚠ " + snap.provider_error.split("\n")[0] +
      " —— 配置好后到 设置 · 模型服务 里选择一个已配置 Key 的 provider 即可生效。");
    setModelChip("未配置模型");
  } else {
    hideBanner();
    setModelChip(`${snap.provider}/${snap.model}`);
  }
  curProviderName = snap.provider || "";
  curModelName = snap.model || "";
  curSupportsVision = snap.supports_vision !== false; // 贴图前的前置提示用
  refreshReasoning(); // 思考强度控件按当前服务的支持情况显示
  sessionMeta = {};
  (snap.sessions || []).forEach((s) => { sessionMeta[s.id] = { title: s.title }; });
  if (snap.session) {
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
  if (document.getElementById("snippets-list")) renderSnippets();
  // 输入栏的上下文环形仪表：启动即用当前会话的占用初始化（此前只在发过消息后才出现，用户会以为没有这个功能）
  request("chat.status").then((st) => {
    if (st) setContextUsage(st.context_tokens || 0, st.context_limit || 0);
  }).catch(() => {});
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
  const anyKeyed = Object.values(snap.providers || {}).some((p) => p.has_key);
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
  } catch (e) {
    setSwitchBusy(false);
    switchingProject = false;
    throw e;
  }
  setSwitchBusy(false);
  switchingProject = false;
}

/** 切换期间只做轻量提示（顶栏状态 + 项目列表置灰），不遮挡界面。 */
function setSwitchBusy(on) {
  const list = document.getElementById("project-list");
  if (list) list.classList.toggle("busy", on);
  const text = document.getElementById("status-text");
  if (on) {
    switchStatusPrev = text ? text.textContent : null;
    if (text) text.textContent = "正在切换项目…";
  } else if (text && switchStatusPrev != null) {
    text.textContent = switchStatusPrev;
    switchStatusPrev = null;
  }
}
let switchStatusPrev = null;

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

/** 右面板里跟项目绑定的数据缓存清空（DOM 由各自 loader 重填）。 */
function resetProjectPanels() {
  filesLoaded = false;
  agCache = [];
  if (tasksTimer) { clearTimeout(tasksTimer); tasksTimer = null; }
  const tasks = document.getElementById("tasks-list");
  if (tasks) tasks.innerHTML = "";
  const files = document.getElementById("files-tree");
  if (files) files.innerHTML = "";
  const agenda = document.getElementById("agenda-list");
  if (agenda) agenda.innerHTML = "";
  const cron = document.getElementById("cron-list");
  if (cron) cron.innerHTML = "";
  const review = document.getElementById("review-list");
  if (review) review.innerHTML = "";
  const reviewDiff = document.getElementById("review-diff");
  if (reviewDiff) reviewDiff.classList.add("hidden");
  const preview = document.getElementById("files-preview");
  if (preview) preview.classList.add("hidden");
  // 终端：命令是在项目目录里跑的，旧项目的输出与运行态一并作废
  const termOut = document.getElementById("term-out");
  if (termOut) termOut.innerHTML = "";
  const termIn = document.getElementById("term-in");
  if (termIn) termIn.value = "";
  if (termBusy) { try { request("term.stop").catch(() => {}); } catch (e) {} }
  termBusy = false;
  const termRunBtn = document.getElementById("term-run");
  if (termRunBtn) { termRunBtn.disabled = false; termRunBtn.textContent = "运行"; }
  const termStopBtn = document.getElementById("term-stop");
  if (termStopBtn) termStopBtn.classList.add("hidden");
}

/** 切换后重拉右侧面板里与项目绑定的页（boot 不管这些）。 */
function reloadProjectPanels() {
  if (!rightTabs.length || rightCollapsed) return;
  const loaders = {
    files: () => loadFiles(true),
    tasks: () => loadTasks(),
    agenda: () => loadAgenda(),
    cron: () => loadCron(),
    review: () => refreshReview(),
  };
  Object.entries(loaders).forEach(([id, fn]) => {
    if (!rightTabs.includes(id)) return;
    fn().catch(() => {});
  });
}

async function refreshProjects(prefetched) {
  const ul = document.getElementById("project-list");
  if (!ul) return;
  const { projects } = prefetched || await request("project.list").catch(() => ({ projects: [] }));
  ul.innerHTML = "";
  if (!projects.length) {
    ul.innerHTML = '<li class="empty-hint">点右上角 ＋ 添加项目</li>';
    return;
  }
  projects.forEach((p) => {
    const li = document.createElement("li");
    if (p.is_current) li.classList.add("active");
    li.innerHTML = FOLDER_SVG + `<span class="s-title">${escapeHtml(p.name)}</span>`;
    li.title = p.root_path + (p.is_current ? "（当前项目）" : "—— 点击切换到这个项目");
    // 非当前项目：悬浮出移除按钮（连带会话与白名单，不删电脑上的文件夹）
    if (!p.is_current) {
      const del = document.createElement("button");
      del.className = "p-del";
      del.textContent = "✕";
      del.title = "从列表中移除这个项目";
      del.onclick = (e) => {
        e.stopPropagation();
        deleteProjectModal(p);
      };
      li.appendChild(del);
    }
    li.onclick = async () => {
      if (p.is_current) return;
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
  box.innerHTML =
    `<p>确定从列表中移除项目 <b>${escapeHtml(p.name)}</b> 吗？</p>
     <p class="dim small">该项目下的<b>会话记录与白名单会一并删除</b>；电脑上的文件夹和文件
     <b>不受影响</b>，以后随时可以重新添加回来。</p>
     <span class="mono-path">${escapeHtml(p.root_path)}</span>`;
  showModal("删除项目", box, async () => {
    await request("project.delete", { id: p.id });
    refreshProjects();
    addNotice(`已移除项目「${p.name}」；文件夹仍保留在电脑上。`);
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

function setWorkMode(mode) {
  workMode = mode;
  const btn = document.getElementById("mode-switch");
  btn.innerHTML = mode === "plan" ? CP_ICON_PLAN : CP_ICON_EXECUTE;
  btn.title = MODE_TITLE[mode];
  btn.classList.toggle("plan", mode === "plan");
  document.getElementById("input").placeholder = mode === "plan"
    ? "规划模式：描述目标，我只调研并产出实施计划…"
    : "描述你的任务…";
}
document.getElementById("mode-switch").onclick = () =>
  setWorkMode(workMode === "plan" ? "execute" : "plan");

// ---------- 圆桌：多模型共同思考，融合成更好的答案 ----------
let rtOn = false; // 一次性开关：发送后自动复位
let rtMembers = (() => {
  try { return JSON.parse(localStorage.getItem("skysheep.rt.members") || "null"); }
  catch { return null; }
})();

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
  let entries = 0;
  Object.entries(cfg.providers || {}).forEach(([name, p]) => {
    (p.models || []).forEach((m) => {
      entries += 1;
      const hasKey = !!p.key_mask;
      const key = name + "/" + m;
      const label = document.createElement("label");
      label.className = "rt-menu-item" + (hasKey ? "" : " nokey");
      label.innerHTML =
        `<input type="checkbox" value="${escapeHtml(key)}" ${selected.has(key) ? "checked" : ""} ${hasKey ? "" : "disabled"}>` +
        `<span class="mi-cmd">${escapeHtml(name)}</span>` +
        `<span class="mi-model">${escapeHtml(m)}</span>` +
        (hasKey ? "" : '<span class="mi-nokey">缺 Key</span>');
      list.appendChild(label);
    });
  });
  if (!entries) {
    list.innerHTML = '<div class="rt-menu-tip">暂无可用模型服务——先到「设置 · 模型服务」配置 API Key。</div>';
  }
  menu.appendChild(list);
  const actions = document.createElement("div");
  actions.className = "rt-menu-actions";
  const cmpLabel = document.createElement("label");
  cmpLabel.className = "rt-compare-toggle";
  cmpLabel.title = "不融合各成员回答，而是并排保留每个模型的原始回答（A/B 对比）";
  cmpLabel.innerHTML = `<input type="checkbox" id="rt-compare"> 对比模式（不融合，保留各自回答）`;
  const clear = document.createElement("button");
  clear.className = "link-btn";
  clear.textContent = "恢复自动";
  clear.onclick = () => list.querySelectorAll("input:checked").forEach((i) => (i.checked = false));
  const ok = document.createElement("button");
  ok.className = "btn-primary";
  ok.textContent = "确定";
  actions.prepend(cmpLabel);
  ok.onclick = () => {
    const picked = [...list.querySelectorAll("input:checked")].map((i) => {
      const idx = i.value.indexOf("/");
      return { provider: i.value.slice(0, idx), model: i.value.slice(idx + 1) };
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

function maybeNotify(title, body) {
  pushNotice(title, body); // 无论是否弹系统通知，都进应用内通知中心
  if (!uiNotifyOn) return;
  if (document.hasFocus() && !document.hidden) return;
  request("app.notify", { title, body }).catch(() => {});
}

// ---------- 通知中心：聚合错过的提醒（cron 结果 / 权限请求 / 日程 / 错误） ----------
const notifLog = []; // {ts, title, body, kind}
let notifUnread = 0;
const notifPanel = document.getElementById("notif-panel");

function pushNotice(title, body, kind = "") {
  notifLog.unshift({ ts: Date.now() / 1000, title, body, kind });
  if (notifLog.length > 60) notifLog.pop();
  notifUnread += 1;
  renderBell();
  if (!notifPanel.classList.contains("hidden")) renderNotifPanel();
}

function renderBell() {
  const badge = document.getElementById("bell-badge");
  const n = notifPanel.classList.contains("hidden") ? notifUnread : 0;
  badge.classList.toggle("hidden", n <= 0);
  badge.textContent = n > 9 ? "9+" : String(n);
  document.getElementById("btn-bell").classList.toggle("has-unread", n > 0);
}

function fmtNotifTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
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
    item.className = "notif-item";
    item.innerHTML = `<span class="notif-time">${fmtNotifTime(n.ts)}</span>` +
      `<span class="notif-body"><b>${escapeHtml(n.title)}</b>` +
      (n.body ? `<span>${escapeHtml(n.body)}</span>` : "") + `</span>`;
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

// 容器边界与「地面」：地面 = #chat 底部（宠物自身约 76px，留 4px 边距）
// ok=false 表示容器当前不可见（切到设置页时 #view-chat display:none，尺寸塌成 0）——
// 此时任何「按地面归位」的计算都必须跳过，否则地面会算成负数、把小羊顶到顶部。
function petBounds() {
  const r = document.getElementById("chat").getBoundingClientRect();
  const cw = r.width / uiScale, ch = r.height / uiScale;
  return {
    maxX: Math.max(4, cw - 82),
    floor: Math.max(4, ch - 80),
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

async function send() {
  const input = document.getElementById("input");
  const text = input.value.trim();
  const images = pendingImages.slice();
  if (!text && !images.length) return;
  if (images.length && !curSupportsVision) {
    addNotice(`当前模型「${curProviderName}/${curModelName}」标记为不支持图片输入，` +
      "图片发不出去。请先在输入框右侧切换成多模态模型；" +
      "若该模型其实支持看图，可到 设置 · 模型服务 里打开「模型支持图片输入」。");
    return; // 留在输入框里，别把用户贴的图清掉
  }
  recordInputHistory(text); // 输入历史：供空输入按 ↑ 回看
  const tab = activeTab;
  // 新标签（无会话）：先向后端申请一个全新会话，避免消息落进旧的活动会话
  if (tab && !tab.sid) {
    try {
      const s = await request("session.new");
      tab.sid = s.id;
      tab.title = tab.title || text.slice(0, 20) || "新会话";
      currentSessionId = s.id;
      if (tab.logEl.querySelector(".welcome")) tab.logEl.innerHTML = ""; // 清掉欢迎页
      renderTabs();
      tab.firstSend = true; // 新会话首轮：让后端自动生成标题
    } catch (e) {
      addNotice("新建会话失败: " + e.message);
      return;
    }
  }
  input.value = "";
  autoGrowInput();
  clearPendingImages();
  hideInputMenu();
  addUser(text, images);
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
    setRtOn(false); // 圆桌是一次性开关：发送后复位
    const r = await request("chat.send", {
      text,
      session_id: tab.sid || undefined,
      wants_title: tab.firstSend === true,
      plan_mode: workMode === "plan",
      roundtable: rtThisTurn,
      members: membersThisTurn || undefined,
      compare: compareThisTurn || undefined,
      images: images.length ? images : undefined,
    });
    setContextUsage(r.context_tokens || 0, r.context_limit || 0, tab);
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

function addAssistantDone(text, rtMeta, tab, seq, thinking) {
  const d = document.createElement("div");
  d.className = "msg assistant";
  if (thinking) addThinkingDone(thinking, tab);
  d.innerHTML = `<div class="md">${renderMarkdown(text || "")}</div>`;
  if (rtMeta && rtMeta.members) addRtBadge(d, rtMeta);
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

function renderHistory(tab, messages) {
  tab.logEl.innerHTML = "";
  withTab(tab, () => {
    (messages || []).forEach((m) => {
      let el = null;
      if (m.role === "user") {
        addUser(m.text, m.images);
        el = curLog().lastElementChild;
      } else if (m.role === "assistant") {
        addAssistantDone(m.text, m.roundtable, tab, m.seq, m.thinking);
        el = curLog().lastElementChild;
      }
      if (el && m.seq) el.dataset.seq = String(m.seq);
    });
  });
  attachHistoryOps(tab);
  chatBox.scrollTop = chatBox.scrollHeight;
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
  try {
    await request("session.truncate", { id: tab.sid, mode: "regen", seq: seq || undefined });
    let drop = false;
    [...tab.logEl.children].forEach((node) => {
      if (node === el) { drop = true; return; }
      if (drop) node.remove();
    });
    setRunning(true, tab);
    await request("chat.send", {
      text: "", session_id: tab.sid, regenerate: true,
      plan_mode: workMode === "plan",
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
      const r = await request("checkpoint.restore", { id: cp.id });
      bar.classList.add("done");
      btn.remove();
      label.innerHTML = `↩ 已回滚 ${r.files.length} 个文件到改前状态`;
      addNotice("已撤销本轮文件改动；如需继续任务，Agent 会重新读取最新文件。");
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
  const slash = before.match(/^\/([\w-]*)$/);
  if (slash) {
    const q = slash[1].toLowerCase();
    const snips = snippetsCache.concat(builtinSnippets())
      .filter((s) => !q || s.name.toLowerCase().includes(q))
      .slice(0, 5)
      .map((s) => ({
        label: "◆ " + s.name,
        desc: snippetsCache.includes(s) ? "快捷指令 · 选中即插入" : "内置示例 · 选中即插入",
        onPick: () => insertSnippet(s),
      }));
    const items = snips.concat(
      SLASH_COMMANDS.filter((c) => c.cmd.startsWith("/" + q))
        .map((c) => ({
          label: c.cmd,
          desc: c.desc,
          onPick: () => { inputEl.value = ""; autoGrowInput(); execSlash(c.cmd); },
        }))
    );
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
  if (!menuOpen && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
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
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
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
    if (!settingsOpen) document.getElementById("btn-new").click();
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
    toggleModelMenu();
    return;
  }
  // Ctrl+W 关闭当前会话标签
  if (k === "w" && !settingsOpen) {
    e.preventDefault();
    if (activeTab) closeTab(activeTab);
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
  runFind(e.target.value);
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
  ["Esc", "停止正在运行的任务"],
  ["Enter / Shift + Enter", "发送 / 换行"],
  ["Ctrl + Alt + Space", "全局热键：唤起窗口并预填剪贴板内容"],
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
      只读操作自动放行。想少点确认，可开输入框里的「✎ 自动允许写入」。</p>
    </div>
    <div class="help-sec">
      <h4>③ 常用开关</h4>
      <ul>
        <li><b>⚡ 规划模式</b>：先出方案再动手，适合大改动</li>
        <li><b>👥 圆桌</b>：一条消息让多个模型同时作答再融合，难题更稳</li>
        <li><b>＋ 文件</b>：选本机任意文件（含项目外）让 Agent 读；图片请直接粘贴或拖入</li>
        <li><b>右侧面板</b>：终端、文件树、浏览器预览、审查看每轮改了哪些文件</li>
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
      「上下文上限」调成模型真实的窗口大小（默认 80000）。</div>
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
    // 任务清单/日程/定时任务：右侧面板标签（不再用侧栏折叠区）
    if (act === "todo") return openRightTab("todo");
    if (act === "agenda") return openRightTab("agenda");
    if (act === "cron") return openRightTab("cron");
    if (act === "memory") return memoryModal();
    if (act === "project") return projectModal();
    if (act === "ext") return openSettings("skills");
    if (navPending[act]) addNotice(`「${navPending[act]}」开发中，即将上线`);
  };
});

// ---------- 日程（右侧面板：周/月历视图 + 分组列表 + 新增/编辑弹窗 + 到点提醒横幅） ----------
const AG_HOUR_H = 44;        // 周视图里 1 小时的高度
const AG_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const AG_WNAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
let agView = "week";         // week | month | list
let agAnchor = new Date();   // 当前查看的日期（取其所在周/月）
let agCache = [];            // schedule.list（含已完成）的缓存

function agPad(n) { return String(n).padStart(2, "0"); }
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
    if (t < weekStart || t >= weekEnd) return;
    const d = new Date(t);
    const dayIdx = Math.floor((new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() - weekStart) / 86400000);
    const mins = d.getHours() * 60 + d.getMinutes();
    const evtTitle = `${agPad(d.getHours())}:${agPad(d.getMinutes())} ${it.title}${it.notes ? " · " + it.notes : ""}`;
    evts += `<div class="ag-evt${it.done ? " done" : ""}" data-id="${it.id}"
      style="top:${Math.round(mins / 60 * AG_HOUR_H) + 1}px;left:calc(${dayIdx} * 100% / 7 + 2px);width:calc(100% / 7 - 4px)"
      title="${escapeHtml(evtTitle)}">${it.done ? "✓ " : ""}${escapeHtml(it.title)}</div>`;
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
      const it = agCache.find((x) => x.id == el.dataset.id);
      if (it) agendaModal(it);
    };
  });
  const canvas = week.querySelector(".ag-canvas");
  canvas.onclick = (e) => {
    // 点空白格：定位到所在天 + 30 分钟取整的时间槽，直接开新建弹窗
    const rect = canvas.getBoundingClientRect();
    const col = Math.max(0, Math.min(6, Math.floor((e.clientX - rect.left) / (rect.width / 7))));
    const mins = Math.max(0, Math.min(1439, Math.floor((e.offsetY / AG_HOUR_H) * 2) * 30));
    const day = new Date(days[col]); day.setHours(0, 0, 0, 0);
    agendaModal(null, (day.getTime() + mins * 60000) / 1000);
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
           <span class="agenda-time">${fmtAgendaTime(it.start_at)}</span>
         </div>` +
        (it.notes ? `<div class="agenda-notes">${escapeHtml(it.notes)}</div>` : "");
      const ops = document.createElement("span");
      ops.className = "agenda-ops";
      ops.innerHTML =
        `<button class="agenda-op" data-op="done" title="标记完成">✓</button>` +
        `<button class="agenda-op" data-op="del" title="删除">✕</button>`;
      ops.querySelector('[data-op="done"]').onclick = async (e) => {
        e.stopPropagation();
        await request("schedule.update", { id: it.id, done: true });
        await loadAgenda();
      };
      ops.querySelector('[data-op="del"]').onclick = async (e) => {
        e.stopPropagation();
        await request("schedule.delete", { id: it.id });
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

function agendaModal(existing, defaultTs) {
  const isEdit = !!existing;
  const box = document.createElement("div");
  const d = existing ? new Date(existing.start_at * 1000)
    : defaultTs ? new Date(defaultTs * 1000)
    : new Date(Date.now() + 3600000);
  const p = (n) => String(n).padStart(2, "0");
  const dtLocal = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  box.innerHTML = `
    <div class="agenda-fields">
      <label>标题</label>
      <input id="ag-title" class="modal-input" type="text" placeholder="例如：小组会 / 交房租" value="${existing ? escapeHtml(existing.title) : ""}">
      <label>时间</label>
      <input id="ag-time" class="modal-input" type="datetime-local" value="${dtLocal}">
      <label>备注（可选）</label>
      <input id="ag-notes" class="modal-input" type="text" value="${existing ? escapeHtml(existing.notes || "") : ""}">
      <label class="agenda-remind"><input id="ag-remind" type="checkbox" ${!existing || existing.remind ? "checked" : ""}> 到点在应用内提醒</label>
      <label>提前量</label>
      <select id="ag-remind-before" class="modal-input agenda-select">
        <option value="0">到点才提醒</option>
        <option value="5">提前 5 分钟</option>
        <option value="10">提前 10 分钟</option>
        <option value="15">提前 15 分钟</option>
        <option value="30">提前 30 分钟</option>
        <option value="60">提前 1 小时</option>
      </select>
    </div>`;
  box.querySelector("#ag-remind-before").value = String(existing?.remind_before ?? 0);
  showModal(isEdit ? "编辑日程" : "新增日程", box, async () => {
    const title = box.querySelector("#ag-title").value.trim();
    const tstr = box.querySelector("#ag-time").value;
    if (!title) throw new Error("请填写标题");
    if (!tstr) throw new Error("请选择时间");
    const ts = new Date(tstr).getTime() / 1000;
    if (Number.isNaN(ts)) throw new Error("时间格式不正确");
    const params = {
      title,
      start_at: ts,
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
      `<button class="cron-op" data-op="toggle" title="${t.enabled ? "暂停" : "启用"}">${t.enabled ? "⏸" : "▶"}</button>` +
      `<button class="cron-op danger" data-op="del" title="删除">✕</button>`;
    ops.querySelector('[data-op="run"]').onclick = async (e) => {
      e.stopPropagation();
      try {
        await request("cron.run_now", { id: t.id });
        addNotice(`已开始运行「${t.name}」，结果会写回列表`);
      } catch (err) { addNotice("运行失败: " + err.message); }
    };
    ops.querySelector('[data-op="toggle"]').onclick = async (e) => {
      e.stopPropagation();
      await request("cron.update", { id: t.id, enabled: !t.enabled });
      await loadCron();
    };
    ops.querySelector('[data-op="del"]').onclick = async (e) => {
      e.stopPropagation();
      await request("cron.delete", { id: t.id });
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
  const safetyLabel = { readonly: "只读", write: "写入", dangerous: "高危" };
  const toolRows = tools.length
    ? tools.map((tool) => {
        const locked = tool.safety === "readonly";  // 只读本来就是自动放行的
        const on = locked || allowed.has(tool.name);
        return `<label class="cron-tool${locked ? " locked" : ""}" title="${escapeHtml(tool.description || "")}">
            <input type="checkbox" data-tool="${escapeHtml(tool.name)}"${on ? " checked" : ""}${locked ? " disabled" : ""}>
            <span class="ct-name">${escapeHtml(tool.name)}</span>
            <span class="chip ${tool.safety === "dangerous" ? "danger-mark" : tool.safety === "write" ? "write-mark" : "safe-mark"}">${safetyLabel[tool.safety] || tool.safety}</span>
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
       <span class="small dim">${fmtAgendaTime(item.start_at)}${remainTxt}${item.notes ? " · " + escapeHtml(item.notes) : ""}</span>
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

async function memoryModal() {
  const r = await request("project.instructions");
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">记忆文件：<b>${escapeHtml(r.path || "本项目还没有（保存时自动创建 AGENTS.md）")}</b><br>
    写在这里的项目约定会注入每一轮对话，Agent 会一直遵守（对标 AGENTS.md / CLAUDE.md）。</p>
    <textarea id="memory-text" class="memory-text" placeholder="例如：&#10;- 提交信息用中文&#10;- 改完代码必须跑 pytest"></textarea>`;
  box.querySelector("textarea").value = r.text || "";
  showModal("项目记忆", box, async () => {
    const res = await request("project.save_instructions", { text: box.querySelector("textarea").value });
    addNotice(`项目记忆已保存（${res.chars} 字）→ ${res.path}`);
  }, "保存");
}

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
    <div class="pr-fields" style="margin-top:10px">
      <label>切换到其他文件夹</label>
      <div class="model-line">
        <input id="proj-path-input" autocomplete="off"
          placeholder="${canPick ? "点右侧按钮选择文件夹，或直接粘贴完整路径" : "粘贴文件夹完整路径，如 D:\\\\works\\\\demo"}">
        ${canPick ? '<button id="proj-pick" class="btn-ghost" style="flex-shrink:0">📁 选择</button>' : ""}
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
      li.title = "点击切换到这个项目";
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
document.getElementById("btn-project-add").onclick = projectModal;

// ---------- 设置页：视图切换（侧栏同步切换为设置子项目） ----------
const viewChat = document.getElementById("view-chat");
const viewSettings = document.getElementById("view-settings");
const sideChat = document.getElementById("side-chat");
const sideSettings = document.getElementById("side-settings");
const btnSettings = document.getElementById("btn-settings");
let settingsOpen = false;

function openSettings(page = "providers") {
  settingsOpen = true;
  viewChat.classList.add("hidden");
  viewSettings.classList.remove("hidden");
  sideChat.classList.add("hidden");
  sideSettings.classList.remove("hidden");
  btnSettings.textContent = "← 返回对话";
  resetProviderView(); // 每次进设置都从服务列表开始，不停在上次打开的详情页
  showSettingsPage(page);
  renderSettings().catch((e) => addNotice("加载设置失败: " + e.message));
}
function backToChat() {
  settingsOpen = false;
  viewSettings.classList.add("hidden");
  viewChat.classList.remove("hidden");
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
  if (target === "ui") loadLanPanel();
  if (target === "subagents") renderSubagentCfg().catch(() => {});
  if (target === "advanced") renderAdvancedCfg().catch(() => {});
  if (target === "about") loadBackups().catch(() => {});
}
document.querySelectorAll("#settings-nav li").forEach((li) => {
  li.onclick = () => showSettingsPage(li.dataset.target);
});

// ---------- 模型服务：列表视图 ----------
let providerCfg = null;   // 最近一次拉取到的服务清单
let bootSnap = null;      // 最近一次 boot 快照（模板里要用工作目录等）
let availCacheModels = null; // 详情页探测到的可用模型（切视图不丢）
let availNoteState = null;   // 可用模型面板的提示状态
let detailOpen = false;   // 是否正停在某个服务的配置页
let detailName = "";

const AVATAR_COLORS = ["#1257c4", "#8a6410", "#0f6b3a", "#8f1d1d", "#4f35a8", "#0e6b6b"];

function avatarColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 997;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function renderProviderList() {
  const list = document.getElementById("provider-list");
  list.innerHTML = "";
  const entries = Object.entries(providerCfg.providers);
  entries.forEach(([name, p]) => {
    const row = document.createElement("div");
    row.className = "svc-row" + (p.is_active ? " cur" : "");
    row.dataset.name = name;
    const keyState = p.has_key ? "Key 已配置" + (p.key_from_env ? "（环境变量）" : "") : "未配置 Key";
    const badges =
      (p.is_active ? '<span class="chip chip-blue">使用中</span>' : "") +
      (p.is_default ? '<span class="chip">★ 默认</span>' : "") +
      (p.is_preset ? "" : '<span class="chip chip-self">自定义</span>');
    row.innerHTML = `
      <span class="svc-avatar" style="background:${avatarColor(name)}">${escapeHtml(name.slice(0, 1).toUpperCase())}</span>
      <span class="svc-main">
        <span class="svc-name">${escapeHtml(name)}${badges}</span>
        <span class="svc-sub">${escapeHtml(p.kind)} · ${escapeHtml(p.model || "未设置模型")} · <span class="${p.has_key ? "" : "svc-warn"}">${escapeHtml(keyState)}</span></span>
      </span>
      <button class="svc-go" type="button" title="进入配置">›</button>`;
    // 整行可点：进该服务的配置页
    row.onclick = () => openProviderDetail(name);
    list.appendChild(row);
  });
  if (!entries.length)
    list.innerHTML = '<p class="empty-hint">还没有可用的模型服务，点「＋ 添加自定义服务」新建一个，或从下方恢复内置服务。</p>';

  // —— 已删除的内置服务（可恢复） ——
  const hiddenBox = document.getElementById("provider-hidden");
  const hidden = providerCfg.disabled || [];
  hiddenBox.innerHTML = "";
  hiddenBox.hidden = !hidden.length;
  if (hidden.length) {
    hiddenBox.innerHTML = `
      <h4>已删除的内置服务</h4>
      <p class="settings-hint">内置服务删除后是「隐藏」，随时可以恢复回来；恢复时按最新出厂默认重建（默认模型可能已更新），已配置的 API Key 会保留。</p>
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
        providerStatus(`✓ 已恢复「${btn.dataset.name}」，按最新出厂默认重建（API Key 保留）`);
      };
    });
  }
}

// ---------- 模型服务：详情视图（点列表某一行进入） ----------
function openProviderDetail(name) {
  detailOpen = true;
  detailName = name;
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
          <button class="btn-ghost probe" type="button" title="用地址和 Key 查询该服务是否可用、有哪些模型">⚡ 检测连接</button>
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
        <label>上下文上限（tokens）</label>
        <input data-f="context_limit" type="number" min="0" step="1000"
               value="${p.context_limit ? p.context_limit : ""}"
               placeholder="留空 = 用全局默认 ${p.global_context_limit || 80000}"
               title="该服务模型真实的上下文窗口；填对了才能在撑爆之前自动压缩历史">
        <div class="field-tip">按官方文档填模型的上下文窗口（如 64k 模型填 64000，128k 填 128000）。
          留空则用全局默认（当前 ${(p.global_context_limit || 80000).toLocaleString()}，可在 设置 · 高级 里改）；
          当前生效值 ${(p.effective_context_limit || p.global_context_limit || 0).toLocaleString()} tokens</div>
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
    </div>

    <h4 class="sec-title">已启用模型</h4>
    <div class="flat-panel">
      <div class="panel-note">${(p.models || []).length || 1} 个模型 · 点模型切换使用，✕ 从列表删除</div>
      <div id="enabled-models"></div>
    </div>

    <div class="sec-title-row">
      <h4 class="sec-title">可用模型</h4>
      <button class="btn-ghost fetch" type="button" title="用上面填的地址和 Key 拉取模型列表">⬇ 从供应商获取</button>
    </div>
    <div class="flat-panel">
      <div class="panel-note" id="avail-note">还没有拉取：点右上角「从供应商获取」，或在下面手动填写模型 ID。</div>
      <div id="avail-list"></div>
      <div class="add-row">
        <input data-f="manual_model" placeholder="模型 ID（如 deepseek-chat / glm-5.3）" autocomplete="off">
        <button class="btn-ghost add-manual" type="button" title="把它设为该服务的启用模型">＋</button>
      </div>
    </div>

    <div class="pr-ops">
      <button class="btn-primary save">保存</button>
      <button class="btn-ghost use"${p.is_active ? ' disabled title="正在使用"' : ""}>${p.is_active ? "使用中" : "切换使用"}</button>
      <button class="btn-ghost setdef"${p.is_default ? ' disabled title="已是默认"' : ""}>设为默认 ★</button>
      <button class="btn-ghost danger del">删除</button>
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

  // 检测连接 / 从供应商获取：同一探测；区别只在反馈方式
  const probeFill = async (fillList) => {
    const btn = fillList ? q(".fetch") : q(".probe");
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
  q(".probe").onclick = () => probeFill(true);
  q(".fetch").onclick = () => probeFill(true);
  q(".add-manual").onclick = () => {
    const input = q('input[data-f="manual_model"]');
    const v = input.value.trim();
    if (v) setEnabledModel(v);
    input.value = "";
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
    providerStatus(`✓ 已${on ? "启用" : "停用"}「${name}」` + (on ? "" : "，可在下方已停用列表里恢复"));
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
  (rules || []).forEach((r) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="dot on"></span>
      <span class="rule-text">${escapeHtml(r.tool)} · ${escapeHtml(r.kind)} ${escapeHtml(r.pattern || "(全部)")}</span>
      <button class="rule-del">删除</button>`;
    li.querySelector(".rule-del").onclick = async () => {
      await request("whitelist.remove", { id: r.id });
      await renderSettings();
    };
    ul.appendChild(li);
  });
  if (!rules || !rules.length) ul.innerHTML = '<li class="empty-hint">暂无规则（对话中选「总是允许」后会出现在这里）</li>';

  // —— 技能 ——
  const sul = document.getElementById("settings-skill-list");
  sul.innerHTML = "";
  (snap.skills || []).forEach((s) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="skill-main">
        <span class="item-name">${escapeHtml(s.name)}</span>
        <span class="chip">${s.source === "project" ? "本项目" : "全局"}</span>
        <span class="item-desc" title="${escapeHtml(s.description)}">${escapeHtml(s.description)}</span>
      </div>
      <button class="btn-ghost skill-toggle">${s.enabled ? "🟢 已启用" : "⚪ 已停用"}</button>
      <button class="btn-ghost danger skill-del" title="删除这个技能（从磁盘移除）">删除</button>`;
    const toggle = li.querySelector(".skill-toggle");
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
      boot(); // 同步侧栏技能列表
      await renderSettings();
    };
    li.querySelector(".skill-del").onclick = () => deleteSkillModal(s);
    sul.appendChild(li);
  });
  if (!(snap.skills || []).length)
    sul.innerHTML = '<li class="empty-hint">还没有技能：点右上角「＋ 导入技能」，选一个技能文件夹或 .zip 压缩包即可</li>';

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
      <span class="item-name">${escapeHtml(t.name)}</span>
      <span class="chip ${cls}">${escapeHtml(label)}</span>
      <span class="item-desc" title="${escapeHtml(t.description)}">${escapeHtml(t.description)}</span>`;
    tul.appendChild(li);
  });
  if (!(snap.tools || []).length) tul.innerHTML = '<li class="empty-hint">无工具</li>';

  // —— MCP 服务 ——
  const mul = document.getElementById("settings-mcp-list");
  mul.innerHTML = "";
  renderMcpPresets(snap);
  (snap.mcp || []).forEach((m) => {
    const li = document.createElement("li");
    const toolChips = (m.tools || [])
      .map((t) => `<span class="chip">${escapeHtml(t)}</span>`)
      .join("");
    li.innerHTML = `
      <div class="mcp-head">
        <span class="dot ${m.connected ? "on" : "off"}"></span>
        <span class="item-name">${escapeHtml(m.name)}</span>
        <span class="${m.connected ? "mcp-ok" : "mcp-bad"}">${m.connected ? `已连接 · ${m.tools.length} 个工具` : escapeHtml(m.error || "未连接")}</span>
        <button class="btn-ghost danger mcp-del" title="删除这个 MCP 服务">删除</button>
      </div>
      ${toolChips ? `<div class="mcp-tools">${toolChips}</div>` : ""}`;
    li.querySelector(".mcp-del").onclick = () => deleteMcpModal(m.name);
    mul.appendChild(li);
  });
  if (!(snap.mcp || []).length)
    mul.innerHTML = '<li class="empty-hint">还没有 MCP 服务：点右上角「＋ 导入配置」粘贴一段配置，或「＋ 手动添加」逐个填</li>';

  // —— 关于 ——
  document.getElementById("about-info").innerHTML = `
    <span>版本：<b>v${escapeHtml(snap.version)}</b> · SkySheep 开源项目</span>
    <span>工作目录：<b>${escapeHtml(snap.working_dir)}</b></span>
    <span>配置文件：<b>${escapeHtml(cfg.config_path)}</b>（设置页保存的就是这个文件）</span>`;
  renderUpdatePanel(snap);
  renderWebsearchCfg().catch(() => {});
  renderImagegenCfg().catch(() => {});
  renderSubagentCfg().catch(() => {});

  // —— 数据与隐私 ——
  document.getElementById("about-privacy").innerHTML = `
    <span>会话数据库：<b>${escapeHtml(cfg.db_path)}</b>（所有对话记录都存在这里）</span>
    <span>配置与技能：<b>${escapeHtml(cfg.home_dir)}</b>（config.toml、skills\\、mcp.json）</span>
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
        ioMsg.textContent = "✓ 已导出：" + (r.included || []).join("、");
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

// 从列表里删除模型服务（自定义彻底删除；内置为隐藏，可在下方恢复）
function deleteProviderModal(name, isPreset) {
  const box = document.createElement("div");
  box.innerHTML = isPreset
    ? `<p>确定删除内置服务 <b>${escapeHtml(name)}</b> 吗？</p>
       <p class="dim small">它会从列表里隐藏（本机 config.toml 不再加载它），
       之后可以在列表下方的「已删除的内置服务」里点「恢复」找回来；恢复时按最新出厂默认重建，API Key 会保留。</p>`
    : `<p>确定删除自定义模型服务 <b>${escapeHtml(name)}</b> 吗？</p>
       <p class="dim small">只会从本机 config.toml 里移除这一项；内置服务与历史会话不受影响。</p>`;
  showModal("删除模型服务", box, async () => {
    const r = await request("config.delete_provider", { name });
    if (detailOpen && detailName === name) resetProviderView(); // 被删的就是当前详情 → 回列表
    await renderSettings();
    boot();
    const suffix = r.was_active
      ? "，请在列表里另选一个模型使用"
      : r.hidden
        ? "，可在下方「已删除的内置服务」里恢复"
        : "";
    providerStatus(`✓ 已删除「${name}」${suffix}`, !r.was_active);
  }, "删除");
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
    const need = p.need === "uv" ? "需 uv（随 SkySheep 自带）" : "需 Node.js";
    const has = installed.has(p.name);
    const card = document.createElement("div");
    card.className = "mcp-preset-card" + (has ? " added" : "");
    card.innerHTML = `
      <div class="mpc-top">
        <span class="mpc-label">${escapeHtml(p.label)}</span>
        ${p.readonly ? '<span class="chip safe-mark">只读</span>' : ""}
      </div>
      <div class="mpc-desc" title="${escapeHtml(p.desc)}">${escapeHtml(p.desc)}</div>
      <div class="mpc-foot">
        <span class="mpc-need" title="${escapeHtml(need)}">${escapeHtml(need)}</span>
        <button type="button" class="btn-ghost mpc-add"${has ? " disabled" : ""}>${has ? "✓ 已添加" : "＋ 添加"}</button>
      </div>`;
    const btn = card.querySelector(".mpc-add");
    if (!has) {
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

document.getElementById("btn-import-skill").onclick = () => importSkillModal();

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
    const r = await request("mcp.import", {
      snippet,
      path,
      scope: box.querySelector('select[data-f="scope"]').value,
      overwrite: box.querySelector('input[data-f="overwrite"]').checked,
    });
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
      <label>存到哪个配置
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <label class="wide inline-check">
        <input data-f="readonly" type="checkbox"> 这个服务的工具自动放行（只读类服务可勾选，跳过每次确认）
      </label>
      <div class="form-status"></div>
    </div>`;
  showModal("手动添加 MCP 服务", box, async () => {
    const val = (f) => box.querySelector(`[data-f="${f}"]`).value.trim();
    const args = val("args").split(/\n+/).map((s) => s.trim()).filter(Boolean);
    const r = await request("mcp.save_server", {
      name: val("name"),
      command: val("command"),
      args,
      url: val("url"),
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
  terminal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="m7.5 9.5 3 2.5-3 2.5"/><path d="M12.5 15h4"/></svg>',
  browser: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.8 2.4 2.8 14.6 0 17-2.8-2.4-2.8-14.6 0-17z"/></svg>',
  files: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M9 13h6"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/></svg>',
  todo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 6h11M9.5 12h11M9.5 18h11"/><path d="m3.5 6 1.2 1.2L7 4.9M3.5 12l1.2 1.2L7 10.9M4 18h.01"/></svg>',
  agenda: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5.5" width="16" height="15" rx="2"/><path d="M8 3.5v4M16 3.5v4M4 10.5h16"/></svg>',
  cron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  // 面板开关：箭头指明点击后的动作——收起时 `>`（向右展开）、展开时 `<`（向左收起），
  // 展开态把面板列填色提示"此刻是开着的"
  panelClosed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/><path d="m8.5 9.5 2.5 2.5-2.5 2.5"/></svg>',
  panelOpen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/><path d="M15.5 6h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-3z" fill="currentColor" stroke="none" opacity=".3"/><path d="m11.5 9.5-2.5 2.5 2.5 2.5"/></svg>',
};
const TAB_META = {
  aux: { title: "辅助对话" },
  review: { title: "审查" },
  terminal: { title: "终端" },
  browser: { title: "浏览器" },
  files: { title: "文件" },
  tasks: { title: "任务" },
  todo: { title: "任务清单" },
  agenda: { title: "日程" },
  cron: { title: "定时任务" },
};
let rightTabs = [];    // 打开的标签 id（有序）
let rightActive = null;

const rightPanel = document.getElementById("right-panel");
const rpTabs = document.getElementById("rp-tabs");
const rpResizer = document.getElementById("rp-resizer");
const btnTabs = document.getElementById("btn-tabs");
btnTabs.innerHTML = RP_ICONS.panelClosed;

function renderRightPanel() {
  const open = rightTabs.length > 0 && !rightCollapsed;
  rightPanel.classList.toggle("hidden", !open);
  rpResizer.classList.toggle("hidden", !open);
  btnTabs.classList.toggle("on", open);
  btnTabs.innerHTML = open ? RP_ICONS.panelOpen : RP_ICONS.panelClosed;
  btnTabs.title = open ? "收起右侧面板" : "展开右侧面板";
  rpTabs.innerHTML = "";
  rightTabs.forEach((id) => {
    const b = document.createElement("button");
    b.className = "rp-tab" + (id === rightActive ? " active" : "");
    b.title = TAB_META[id].title;
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
  document.querySelectorAll("#rp-body .rp-page").forEach((p) => p.classList.add("hidden"));
  if (rightActive && !rightCollapsed) document.getElementById("rp-page-" + rightActive).classList.remove("hidden");
}

function openRightTab(id) {
  if (!TAB_META[id]) return;
  if (!rightTabs.includes(id)) rightTabs.push(id);
  rightActive = id;
  rightCollapsed = false;
  renderRightPanel();
  saveUiPrefs({ right_tabs: [...rightTabs], right_active: id, right_collapsed: 0 });
  if (id === "review") refreshReview();
  if (id === "files") loadFiles();
  if (id === "tasks") loadTasks();
  if (id === "agenda") loadAgenda();
  if (id === "cron") loadCron();
  if (id === "terminal" && document.hasFocus()) termIn.focus();
}

function activateRightTab(id) {
  rightActive = id;
  renderRightPanel();
  saveUiPrefs({ right_active: id });
  if (id === "review") refreshReview();
  if (id === "files") loadFiles();
  if (id === "tasks") loadTasks();
  if (id === "agenda") loadAgenda();
  if (id === "cron") loadCron();
  if (id === "terminal" && document.hasFocus()) termIn.focus();
}

function closeRightTab(id) {
  rightTabs = rightTabs.filter((t) => t !== id);
  if (id === "terminal" && termBusy) termStop(); // 关终端页顺手结束还在跑的命令
  if (rightActive === id) rightActive = rightTabs[rightTabs.length - 1] || null;
  if (!rightTabs.length) rightCollapsed = true; // 最后一个标签关掉 → 面板收起
  renderRightPanel();
  saveUiPrefs({
    right_tabs: [...rightTabs],
    right_active: rightActive,
    right_collapsed: rightCollapsed ? 1 : 0,
  });
}

// 面板收起/展开由顶栏按钮直接切换；「＋」菜单选要打开的标签（小浮层，不弹窗）
let rightCollapsed = false;
let tabsMenuEl = null;

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
  if (!rightTabs.length) { openRightTab("terminal"); return; } // 一个标签都没有 → 展开默认终端
  rightCollapsed = !rightCollapsed;
  renderRightPanel();
  saveUiPrefs({ right_collapsed: rightCollapsed ? 1 : 0 });
};

// —— 终端：命令经后端在工作目录执行，输出按块流式回显 ——
const termOut = document.getElementById("term-out");
const termIn = document.getElementById("term-in");
const termRunBtn = document.getElementById("term-run");
const termStopBtn = document.getElementById("term-stop");
let termBusy = false;
const termHistory = [];
let termHistPos = -1;

function termAppend(text, cls) {
  const nearBottom = termOut.scrollHeight - termOut.scrollTop - termOut.clientHeight < 48;
  const span = document.createElement("span");
  if (cls) span.className = cls;
  span.textContent = text;
  termOut.appendChild(span);
  if (nearBottom) termOut.scrollTop = termOut.scrollHeight;
}

// 把终端最近输出带入主输入框（引用块），供 Agent 分析
document.getElementById("term-to-agent").onclick = () => {
  const text = (termOut.textContent || "").slice(-2400).trim();
  if (!text) { addNotice("终端还没有输出，先运行一条命令"); return; }
  const input = document.getElementById("input");
  input.value = (input.value ? input.value + "\n" : "") +
    "终端最近输出：\n```\n" + text + "\n```\n";
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  addNotice("已把终端输出带入输入框，补充你的问题后发送");
};

function termSetBusy(on) {
  termBusy = on;
  termRunBtn.disabled = on;
  termRunBtn.textContent = on ? "运行中…" : "运行";
  termStopBtn.classList.toggle("hidden", !on);
}

async function termRun() {
  const cmd = termIn.value.trim();
  if (!cmd || termBusy) return;
  termIn.value = "";
  if (termHistory[termHistory.length - 1] !== cmd) termHistory.push(cmd);
  if (termHistory.length > 100) termHistory.shift();
  termHistPos = termHistory.length;
  termSetBusy(true);
  termAppend("❯ " + cmd + "\n", "term-cmd");
  try {
    await request("term.run", { command: cmd });
    // 正常收尾由 terminal_done 事件统一复位运行态
  } catch (e) {
    termAppend("错误：" + e.message + "\n", "term-err");
    termSetBusy(false);
  }
}

function termStop() {
  request("term.stop").catch(() => {});
}

termRunBtn.onclick = termRun;
termStopBtn.onclick = termStop;
termIn.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); termRun(); return; }
  if (e.key === "ArrowUp" && termHistory.length) {
    e.preventDefault();
    termHistPos = Math.max(0, termHistPos - 1);
    termIn.value = termHistory[termHistPos] || "";
  } else if (e.key === "ArrowDown" && termHistory.length) {
    e.preventDefault();
    termHistPos = Math.min(termHistory.length, termHistPos + 1);
    termIn.value = termHistory[termHistPos] || "";
  }
});

// —— 辅助对话：独立小问答（不进主会话、不落库） ——
const auxLog = document.getElementById("aux-log");
const auxInput = document.getElementById("aux-input");
const AUX_EMPTY = '<div class="rp-empty">在这里问点小问题，不会进入主对话，也不会写进会话记录。</div>';
let auxStreamingEl = null;
let auxStreamingText = "";
let auxThinkingText = "";
let auxBusy = false;

// 思考模型的推理增量：面板内灰显（收尾后被正文渲染覆盖），不进历史
function auxThinkingDelta(txt) {
  auxThinkingText += txt || "";
  if (auxStreamingEl) {
    auxStreamingEl.innerHTML =
      '<div class="think-inline">💭 思考中…<br>' +
      escapeHtml(auxThinkingText.slice(-400)) +
      "</div><p>▍</p>";
    auxLog.scrollTop = auxLog.scrollHeight;
  }
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
        const r = await request("checkpoint.restore", { id: cp.id });
        addNotice(`已把 ${r.files.length} 个文件恢复到 ${cp.id} 改动前的状态`);
        refreshReview();
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

async function loadFiles(force) {
  if (filesLoaded && !force) return;
  const tree = document.getElementById("files-tree");
  tree.innerHTML = '<div class="dim small" style="padding:8px">加载中…</div>';
  try {
    const r = await request("fs.files");
    const root = buildFileTree(r.files || []);
    tree.innerHTML = renderFileNode(root, "", 0) ||
      '<div class="dim small" style="padding:8px">工作区还没有文件</div>';
    filesLoaded = true;
  } catch (e) {
    tree.innerHTML = `<div class="dim small" style="padding:8px">加载失败：${escapeHtml(e.message)}</div>`;
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

async function previewFile(path) {
  const wrap = document.getElementById("files-preview");
  try {
    const r = await request("fs.read", { path });
    document.getElementById("files-preview-title").textContent =
      r.path + (r.doc ? "（已提取文本）" : "") +
      (r.truncated ? "（过大已截断）" : "") + ` · ${(r.size / 1024).toFixed(1)}KB`;
    document.getElementById("files-preview-body").textContent = r.text;
    wrap.classList.remove("hidden");
    // 预览是只读的；想接着改就交给系统默认程序（脚本/可执行文件会退化为定位）
    const openBtn = document.getElementById("files-preview-open");
    openBtn.dataset.path = r.path;
    openBtn.onclick = async () => {
      try {
        const res = await request("fs.open", { path: r.path });
        addNotice(res.action === "opened"
          ? `已用系统默认程序打开 ${r.path}`
          : `已在文件管理器里定位 ${r.path}（脚本/可执行文件不直接运行）`);
      } catch (e) {
        addNotice("打开失败: " + e.message);
      }
    };
  } catch (e) {
    addNotice("预览失败: " + e.message);
  }
}

document.getElementById("files-refresh").onclick = () => { filesLoaded = false; loadFiles(true); };
document.getElementById("files-preview-close").onclick = () =>
  document.getElementById("files-preview").classList.add("hidden");
document.getElementById("files-tree").addEventListener("click", (e) => {
  const el = e.target.closest(".ft-file");
  if (!el) return;
  const p = el.dataset.path || "";
  // 网页/图片文件直接在浏览器标签里打开真实渲染效果
  if (/\.(html?|png|jpe?g|webp|svg|gif)$/i.test(p)) { openHtmlPreview(p); return; }
  previewFile(p);
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
      const st = { running: "▶ 运行中", done: "✓ 完成", error: "✗ " + (t.error || "失败") }[t.status] || t.status;
      item.innerHTML =
        `<div class="task-head"><b>${escapeHtml(t.agent_type)}</b>` +
        `<span class="task-status">${escapeHtml(st)}</span></div>` +
        `<div class="task-prompt">${escapeHtml(t.prompt)}</div>` +
        (t.result ? `<div class="task-result">${escapeHtml(t.result.slice(0, 300))}</div>` : "");
      ul.appendChild(item);
    });
  } catch (e) {
    ul.innerHTML = `<div class="rp-empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
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
// 实现：CSS zoom 打在 #app 与 body 级浮层上；默认 90%（略缩小），侧栏底部 −/+ 步进调整，
// 偏好存后端 ui.json（ui_scale，70–120）。缩放后物理像素与缩放布局坐标相差 uiScale 倍，
// 凡依赖鼠标坐标 / 视口尺寸的定位与拖拽计算都需要除回 uiScale。
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

function changeUiScale(dir) {
  const pct = Math.round(uiScale * 100) + dir * UI_SCALE_STEP;
  applyUiScale(pct);
  saveUiPrefs({ ui_scale: Math.round(uiScale * 100) });
}

document.getElementById("btn-zoom-out").onclick = () => changeUiScale(-1);
document.getElementById("btn-zoom-in").onclick = () => changeUiScale(1);

// ---------- 手动调整区域大小：拖侧栏右缘改宽度、拖输入区上缘改高度 ----------
// 偏好存后端 ui.json（pywebview 私密模式下 localStorage 每次启动都会清空）。
const UI_LIMITS = {
  sidebar_w: { css: "--sb-w", min: 200, max: 460 },
  composer_h: { css: "--cp-h", min: 74, max: 520 },
  right_w: { css: "--rp-w", min: 240, max: 720 },
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
  // 系统通知开关（默认开）
  uiNotifyOn = prefs.notify == null ? true : prefs.notify === 1;
  renderNotifyToggle();
  // 分级权限模式（1 = 自动允许写入；后端 setup 已把档位应用到 gate，这里只同步界面）
  renderAcceptSwitch(prefs.accept_edits === 1);
  // 主题（auto | light | dark，默认 auto 跟随系统）
  applyThemeMode(prefs.theme === "dark" || prefs.theme === "light" ? prefs.theme : "auto", false);
  // 对话区宠物开关（默认显示）+ 横向落点（pet_x；纵向有重力，总是落在底部）
  petOn = prefs.pet == null ? true : prefs.pet === 1;
  renderPetToggle();
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
  renderRightPanel();
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

// ---------- 主题：纸墨（浅）/ 夜墨（深）/ 跟随系统 ----------
let themePref = "auto"; // auto | light | dark（存 ui.json 的 theme 键）
const themeMql = window.matchMedia("(prefers-color-scheme: dark)");

function resolvedTheme() {
  return themePref === "auto" ? (themeMql.matches ? "dark" : "light") : themePref;
}

function applyThemeMode(mode, save = true) {
  themePref = mode === "dark" || mode === "light" ? mode : "auto";
  const resolved = resolvedTheme();
  if (resolved === "dark") document.documentElement.setAttribute("data-theme", "dark");
  else document.documentElement.removeAttribute("data-theme");
  // 首帧脚本（index.html）靠 data-theme-mode 判断 auto 该跟系统深还是浅，跟着同步
  document.documentElement.setAttribute("data-theme-mode", themePref);
  // 标题栏联动：桌面壳的看板线程按这个值重刷 DWM 标题栏（浏览器模式无副作用）
  request("app.apply_theme", { resolved }).catch(() => {});
  mermaidSetTheme(resolved);
  renderThemeSeg();
  if (save) saveUiPrefs({ theme: themePref === "auto" ? null : themePref });
}

function renderThemeSeg() {
  document.querySelectorAll("#theme-seg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.themeMode === themePref));
}
document.querySelectorAll("#theme-seg button").forEach((b) => {
  b.onclick = () => applyThemeMode(b.dataset.themeMode);
});
themeMql.addEventListener("change", () => {
  if (themePref === "auto") applyThemeMode("auto", false);
});

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
  if (!window.mermaid || !container) return;
  const nodes = container.querySelectorAll(".mermaid:not([data-processed])");
  if (!nodes.length) return;
  try {
    await window.mermaid.run({ nodes });
  } catch (e) {
    // 语法有误的图：退回普通代码块展示原文，不吞掉用户内容
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
async function loadMemoryPage() {
  try {
    const r = await request("memory.get");
    document.getElementById("memory-text").value = r.text || "";
  } catch (e) {
    addNotice("记忆加载失败: " + e.message);
  }
}
document.getElementById("btn-memory-save").onclick = async () => {
  const status = document.getElementById("memory-status");
  try {
    await request("memory.save", { text: document.getElementById("memory-text").value });
    status.textContent = "✓ 已保存，之后所有会话立即生效";
    status.hidden = false;
  } catch (e) {
    status.textContent = "✗ 保存失败：" + e.message;
    status.hidden = false;
  }
};

// ---------- 设置 · 局域网访问 ----------
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
        （聊天 / 批准确认 / 看任务进度）。二维码含访问令牌，<b>请勿截图外传</b>；只建议在可信网络使用。<br>
        <button id="lan-copy" class="btn-ghost" style="margin-top:6px">⧉ 复制地址</button>
        <span id="lan-copy-msg" class="io-msg"></span>
      </div>
    </div>`;
  const copyBtn = document.getElementById("lan-copy");
  if (copyBtn) {
    copyBtn.onclick = async () => {
      const url = urls[0] || "";
      if (!url) return;
      let ok = false;
      try {
        await navigator.clipboard.writeText(url);
        ok = true;
      } catch (e) {
        // 非安全上下文（局域网 http）clipboard API 不可用：execCommand 兜底
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
      const msg = document.getElementById("lan-copy-msg");
      if (msg) {
        msg.textContent = ok ? "✓ 已复制，发到手机后用浏览器打开" : "✗ 复制失败，请手动长按地址复制";
        msg.className = "io-msg " + (ok ? "ok" : "bad");
      }
    };
  }
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

// ---------- 设置 · 联网搜索 / AI 画图 ----------
async function renderWebsearchCfg() {
  let d;
  try { d = await request("websearch.get"); } catch (e) { return; }
  document.getElementById("websearch-hint").textContent = d.config_hint;
  const form = document.getElementById("websearch-form");
  const labels = { auto: "自动（优先复用智谱 Key）", bocha: "博查 Bocha", tavily: "Tavily", zhipu: "智谱" };
  form.innerHTML = `
    <div class="toolcfg-row"><label>服务商</label>
      <select data-f="provider">${d.providers.map((p) =>
        `<option value="${p}"${p === d.provider ? " selected" : ""}>${labels[p] || p}</option>`).join("")}
      </select>
    </div>
    <div class="toolcfg-row"><label>API Key</label>
      <input type="password" data-f="key" autocomplete="new-password"
             placeholder="${d.has_key ? "已配置（" + d.key_mask + "），留空不修改" : "粘贴服务商的 API Key"}">
    </div>
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + d.resolved_provider + "」"
        : "○ 未配置：Agent 联网搜索时会给出配置指引"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelector('[data-act="save"]').onclick = async () => {
    const params = { provider: form.querySelector('[data-f="provider"]').value };
    const key = form.querySelector('[data-f="key"]').value.trim();
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

async function renderImagegenCfg() {
  let d;
  try { d = await request("imagegen.get"); } catch (e) { return; }
  document.getElementById("imagegen-hint").textContent = d.config_hint;
  const form = document.getElementById("imagegen-form");
  const labels = { auto: "自动（智谱 → 硅基流动）", zhipu: "智谱 CogView", siliconflow: "硅基流动 Kolors", custom: "自定义 OpenAI 兼容" };
  form.innerHTML = `
    <div class="toolcfg-row"><label>服务商</label>
      <select data-f="provider">${d.providers.map((p) =>
        `<option value="${p}"${p === d.provider ? " selected" : ""}>${labels[p] || p}</option>`).join("")}
      </select>
    </div>
    <div class="toolcfg-row"><label>API Key</label>
      <input type="password" data-f="key" autocomplete="new-password"
             placeholder="${d.has_key ? "已配置（" + d.key_mask + "），留空不修改" : "留空则复用同名模型服务的 Key"}">
    </div>
    <div class="toolcfg-row"><label>接口地址</label>
      <input type="text" data-f="base_url" value="${escapeHtml(d.base_url || "")}"
             placeholder="仅自定义服务需要（OpenAI 兼容 /images/generations）">
    </div>
    <div class="toolcfg-row"><label>模型</label>
      <input type="text" data-f="model" value="${escapeHtml(d.model || "")}"
             placeholder="留空用服务商默认（如 cogview-3-flash）">
    </div>
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + d.resolved_provider + " / " + d.resolved_model + "」"
        : "○ 未配置：给智谱或硅基流动配好 Key 即可零配置使用"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelector('[data-act="save"]').onclick = async () => {
    const params = {
      provider: form.querySelector('[data-f="provider"]').value,
      base_url: form.querySelector('[data-f="base_url"]').value,
      model: form.querySelector('[data-f="model"]').value,
    };
    const key = form.querySelector('[data-f="key"]').value.trim();
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
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.enabled ? "ok" : ""}">${d.enabled
        ? "● 已启用：Agent 会在合适的任务里派出子代理并行干活"
        : "○ 已关闭：Agent 只在主对话里逐步处理"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  toggle.onchange = () => {
    form.querySelector('[data-f="max_iterations"]').disabled = !toggle.checked;
  };
  form.querySelector('[data-act="save"]').onclick = async () => {
    const maxIters = form.querySelector('[data-f="max_iterations"]').value.trim();
    try {
      const r = await request("subagent.save", {
        enabled: toggle.checked,
        max_iterations: maxIters === "" ? null : Number(maxIters),
      });
      subagentStatus(r.enabled
        ? `✓ 已保存：子代理已启用，最多 ${r.max_iterations} 轮`
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
  // 一个下拉同时选服务与模型：value = "provider|model"；空值 = 跟随主对话
  const sel = `${(selectedProvider || "").trim()}|${(selectedModel || "").trim()}`;
  let html = `<option value="">跟随主对话</option>`;
  (d.providers || []).forEach((p) => {
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
};

function renderBuiltinSubagents(d) {
  const ul = document.getElementById("builtin-subagent-list");
  ul.innerHTML = "";
  (d.builtin_display ? Object.keys(d.builtin_display) : []).forEach((type) => {
    const ov = (d.builtin || {})[type] || { provider: "", model: "", reasoning: "" };
    const li = document.createElement("li");
    li.className = "subagent-row";
    li.innerHTML = `
      <div class="skill-main">
        <span class="item-name" title="${escapeHtml(d.builtin_display[type] || type)}">${escapeHtml(d.builtin_display[type] || type)}</span>
        <span class="tag-cell"><span class="chip">内置</span></span>
        <span class="item-desc" title="${escapeHtml(SUBAGENT_TYPE_DESC[type] || "")}">${escapeHtml(SUBAGENT_TYPE_DESC[type] || "")}</span>
      </div>
      <span class="subagent-ops">
        <select data-f="model" title="这个子代理用哪个模型">${modelOptions(d, ov.provider, ov.model)}</select>
        <select data-f="reasoning" title="思考强度（留空 = 跟随全局设置）">
          <option value="">思考：跟随全局</option>
          ${(d.reasoning_efforts || []).map((r) =>
            `<option value="${r.value}"${r.value === ov.reasoning ? " selected" : ""}>思考：${escapeHtml(r.label)}</option>`).join("")}
        </select>
      </span>`;
    const save = async () => {
      const mv = parseModelValue(li.querySelector('[data-f="model"]').value);
      try {
        await request("subagent.save_builtin", {
          agent_type: type,
          provider: mv.provider,
          model: mv.model,
          reasoning: li.querySelector('[data-f="reasoning"]').value,
        });
        subagentStatus(`✓ 已保存「${d.builtin_display[type] || type}」的设置`, true, "builtin");
      } catch (e) {
        subagentStatus("✗ 保存失败：" + e.message, false, "builtin");
      }
    };
    li.querySelector('[data-f="model"]').onchange = save;
    li.querySelector('[data-f="reasoning"]').onchange = save;
    ul.appendChild(li);
  });
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
          <option value="all"${policy === "all" ? " selected" : ""}>全部工具（写入/执行仍会被自动拒绝）</option>
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

// ---------- 设置 · 技能广场 ----------
async function openMarket() {
  const box = document.createElement("div");
  box.innerHTML = '<p class="dim small">正在获取技能索引…</p>';
  showModal("技能广场", box, async () => {}, "关闭");
  let r;
  try { r = await request("skills.market"); } catch (e) {
    box.innerHTML = `<p>获取失败：${escapeHtml(e.message)}</p>`;
    return;
  }
  const items = r.items || [];
  box.innerHTML =
    (r.note ? `<p class="market-note">${escapeHtml(r.note)}</p>` : "") +
    `<div class="market-list">${items.map((it, i) => `
      <div class="market-item" data-i="${i}">
        <div class="mi-main">
          <span class="mi-name">${escapeHtml(it.name)}</span>
          <span class="mi-desc" title="${escapeHtml(it.description)}">${escapeHtml(it.description || it.url)}</span>
        </div>
        <span class="mi-author">${escapeHtml(it.author || "")}</span>
        <button class="btn-ghost" data-url="${escapeHtml(it.url)}">安装</button>
      </div>`).join("") || '<p class="dim small">索引是空的。</p>'}</div>`;
  box.querySelectorAll(".market-item button").forEach((btn) => {
    btn.onclick = async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "安装中…";
      try {
        const res = await request("skills.install", { source: btn.dataset.url, scope: "global" });
        skillStatus(`✓ 已从广场安装 ${res.count} 个技能：${res.installed.join("、")}`);
        btn.textContent = "✓ 已安装";
        boot();
        renderSettings();
      } catch (e) {
        btn.disabled = false;
        btn.textContent = "安装";
        skillStatus("✗ " + e.message, false);
      }
    };
  });
}
document.getElementById("btn-market").onclick = () => openMarket();

// ---------- 设置 · 关于：更新检查 ----------
function renderUpdatePanel(snap) {
  const box = document.getElementById("about-update");
  if (!box) return;
  const cur = snap.version || "";
  const upd = snap.update || null;
  box.innerHTML = `
    <button class="btn-ghost" id="btn-check-update">检查更新</button>
    <span class="upd-state ${upd ? "new" : ""}" id="upd-state">${
      upd
        ? `🆕 发现新版本 v${escapeHtml(upd.version)}（当前 v${escapeHtml(cur)}），<a href="${escapeHtml(upd.url)}" target="_blank">前往下载</a>`
        : `当前版本 v${escapeHtml(cur)}`
    }</span>`;
  box.querySelector("#btn-check-update").onclick = async () => {
    const state = box.querySelector("#upd-state");
    state.textContent = "正在检查更新…";
    state.className = "upd-state";
    try {
      const r = await request("app.check_update");
      if (r.available) {
        state.innerHTML = `🆕 发现新版本 v${escapeHtml(r.version)}（当前 v${escapeHtml(r.current)}），<a href="${escapeHtml(r.url)}" target="_blank">前往下载</a>`;
        state.className = "upd-state new";
      } else if (r.error) {
        state.textContent = "检查失败：" + r.error;
      } else {
        state.textContent = `已是最新版本（v${escapeHtml(r.current)}）`;
      }
    } catch (e) {
      state.textContent = "检查失败：" + e.message;
    }
  };
}

// ---------- 窄屏适配：侧栏抽屉开关（局域网手机访问用） ----------
const btnMenu = document.createElement("button");
btnMenu.id = "btn-menu";
btnMenu.className = "tb-icon";
btnMenu.title = "打开菜单";
btnMenu.textContent = "☰";
btnMenu.onclick = () => document.body.classList.toggle("sidebar-open");
document.getElementById("topbar").prepend(btnMenu);
document.getElementById("chat").addEventListener("click", () => {
  if (document.body.classList.contains("sidebar-open")) {
    document.body.classList.remove("sidebar-open");
  }
});

connect();
boot();
// 必须在 connect() 之后：ws 尚未创建时 request() 会抛错（被 catch 吞掉、偏好悄悄不生效）
initUiPrefs();

// ---------- 分级权限模式：写入确认 / 自动允许写入（对标 Codex Auto-Edit） ----------
// 高危操作（执行命令）无论哪档都会征询；档位存 ui.json 的 accept_edits。
function renderAcceptSwitch(on) {
  const b = document.getElementById("accept-switch");
  if (!b) return;
  b.classList.toggle("on", on);
  b.title = on
    ? "当前：文件写入/画图自动放行，执行命令仍会征询。点击切回逐步确认"
    : "当前每次写入/执行都会征询。点击开启「自动允许写入」（执行命令仍会确认）";
}
document.getElementById("accept-switch").onclick = async () => {
  const cur = document.getElementById("accept-switch").classList.contains("on");
  const next = cur ? "confirm" : "accept_edits";
  try {
    const r = await request("permission.set_mode", { mode: next });
    renderAcceptSwitch(r.mode === "accept_edits");
    addNotice(r.mode === "accept_edits"
      ? "✎ 已开启自动允许写入：文件写入不再逐次确认，执行命令仍会征询"
      : "已切回逐步确认模式：所有写入/执行都会先征询");
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

// ---------- 设置 · 高级：运行参数 / 目录限制 / 开机自启 ----------
function advancedStatus(text, ok = true) {
  const el = document.getElementById("advanced-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
}

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
  q("adv-restrict-workdir").checked = !!d.restrict_to_workdir;
  q("adv-computer-control").checked = !!d.computer_control;
  q("adv-browser-control").checked = !!d.browser_control;
  q("adv-daily-budget").value = d.daily_token_budget || "";
  const hint = q("adv-context-hint");
  const src = d.current_provider_context_limit
    ? `当前服务「${d.current_provider}」自带设置 ${d.current_provider_context_limit.toLocaleString()}`
    : "当前服务没单独设置，用这个全局值";
  hint.textContent = `默认 80000。${src}；当前实际生效 ${d.context_limit_tokens_effective.toLocaleString()} tokens`;
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

document.getElementById("btn-advanced-save").onclick = async () => {
  const q = (id) => document.getElementById(id);
  try {
    const r = await request("advanced.save", {
      max_iterations: Number(q("adv-max-iterations").value),
      context_limit_tokens: Number(q("adv-context-limit").value),
      compaction_keep_recent: Number(q("adv-keep-recent").value),
      daily_token_budget: Number(q("adv-daily-budget").value) || 0,
      restrict_to_workdir: q("adv-restrict-workdir").checked,
      computer_control: q("adv-computer-control").checked,
      browser_control: q("adv-browser-control").checked,
      autostart: q("adv-autostart").checked,
    });
    // 先重渲染（会把状态行清掉）再写成功提示，否则"✓ 已保存"一闪就没
    await renderAdvancedCfg();
    advancedStatus("✓ 已保存并立即生效");
    const st = r.autostart || {};
    if (q("adv-autostart").checked && st.enabled) {
      addNotice("✓ 已设置开机自启；定时任务从此可以无人值守");
    }
  } catch (e) {
    advancedStatus("✗ 保存失败：" + e.message, false);
  }
};

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
      target: "https://github.com/Sky-scrape/SkySheep/issues/new?template=bug_report.md",
    });
    msg.textContent = "✓ 已生成诊断包并打开反馈页——把诊断包 zip 拖进附件，描述问题即可";
    msg.className = "io-msg ok";
  } catch (e) {
    msg.textContent = "✗ 打开反馈页失败：" + e.message + "（可手动访问 GitHub 仓库 Issues）";
    msg.className = "io-msg bad";
  }
};

function fmtBackupTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadBackups() {
  const ul = document.getElementById("backup-list");
  let d;
  try { d = await request("session.backups"); } catch (e) {
    ul.innerHTML = `<li class="dim small">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const items = d.backups || [];
  ul.innerHTML = items.map((b) => `
    <li class="item-row${b.current ? " cur" : ""}">
      <span class="item-name">${b.current ? "当前数据" : escapeHtml(b.stamp)}</span>
      <span class="dim small">${fmtBackupTime(b.mtime)} · ${(b.size / 1024).toFixed(0)} KB</span>
      <span class="spacer"></span>
      ${b.current ? "" : `<button class="btn-ghost" data-restore="${escapeHtml(b.name)}">恢复到此版本</button>`}
    </li>`).join("") || '<li class="dim small">还没有备份（应用下次启动时会自动生成一份）</li>';
  ul.querySelectorAll("[data-restore]").forEach((btn) => {
    btn.onclick = () => restoreBackupConfirm(btn.dataset.restore, btn);
  });
  const bs = document.getElementById("backup-status");
  bs.textContent = `共 ${items.length} 项 · 备份目录：${d.dir}（保留最近 ${d.keep} 份）`;
  bs.className = "card-status ok";
  bs.hidden = false;
}

function restoreBackupConfirm(name, btn) {
  const box = document.createElement("div");
  box.innerHTML = `<p>要用备份 <b>${escapeHtml(name)}</b> 覆盖当前会话数据吗？</p>
    <p class="dim small">当前数据会先自动另存为一份「恢复前」备份，所以还能再换回来。
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

document.getElementById("btn-backups-refresh").onclick = () => loadBackups().catch(() => {});
