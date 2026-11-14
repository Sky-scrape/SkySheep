// app-proc.js —— 操作过程时间线（思考行 / 工具行 / 中间说明）与一轮一个过程框。
// 从 app.js 拆出（20k 行巨石）；普通 script 全局函数互调，加载顺序见 index.html。
// 依赖 app.js 里的渲染助手（renderMarkdown / initStreamBody / renderStreamPart /
// streamSealCut / scrollLog / curLog / curTab / showThinking），都在调用时解析。

// ---------- 思考行（思考型模型）：一行「💭 思考 · 3.3 秒 · 470 字」，点开看全文 ----------
// 过程（思考 / 中间说明 / 工具行）按发生顺序排成一条时间线，与正文交错显示，
// 不再整段摊开占屏；耗时优先用引擎实测值（历史恢复时带上），流式期间用本地跨度。
function fmtThinkMs(ms) {
  // 100ms 以下不显示（秒级以下的耗时没有信息量，只留字数）
  if (!ms || ms < 100) return "";
  const s = ms / 1000;
  return s < 10 ? `${s.toFixed(1)} 秒` : `${Math.round(s)} 秒`;
}

function thinkMeta(el) {
  const data = (el && el._think) || {};
  const n = Math.round((data.text || "").length / 10) * 10;
  const dur = fmtThinkMs(data.ms || 0);
  const parts = [];
  if (dur) parts.push(dur);
  if (n) parts.push(`${n} 字`);
  return parts.length ? parts.join(" · ") : "思考中…";
}

function updateThinkMeta(el) {
  const meta = el && el.querySelector(".proc-meta");
  if (meta) meta.textContent = thinkMeta(el);
}

function bindThinkRow(el) {
  el.querySelector(".proc-head").onclick = () => {
    el.classList.toggle("folded");
    updateThinkMeta(el);
  };
}

// 过程框：一轮里的思考行 / 工具行 / 中间说明整体套一层虚线框，框头可整体折叠。
// 一轮结束（最终回答）时收口并收起；下一轮开始另起一个框。
function procMetaText(box) {
  if (!box) return "";
  const steps = box.querySelectorAll(".tool-card").length;
  const thinkMs = [...box.querySelectorAll(".proc-think")]
    .reduce((n, el) => n + ((el._think && el._think.ms) || 0), 0);
  const parts = [];
  if (steps) parts.push(`${steps} 步`);
  if (thinkMs) parts.push(`思考 ${fmtThinkMs(thinkMs)}`);
  return parts.join(" · ");
}

function updateProcMeta(box) {
  const meta = box && box.querySelector(".pb-meta");
  if (meta) meta.textContent = procMetaText(box);
}

function buildProcBox(folded) {
  const box = document.createElement("div");
  box.className = "proc-box" + (folded ? " folded" : "");
  box.innerHTML =
    '<button type="button" class="proc-box-head">' +
    '<span class="pb-ico">💭</span><span class="pb-title">操作过程</span>' +
    '<span class="pb-meta"></span><span class="pb-caret">▶</span>' +
    "</button>" +
    '<div class="proc-box-body"></div>';
  box.querySelector(".proc-box-head").onclick = () => {
    box.classList.toggle("folded");
    updateProcMeta(box);
  };
  return box;
}

function ensureProcBox(t) {
  if (!t) return curLog();
  if (!t.procBox || !t.procBox.isConnected) {
    const box = buildProcBox(false); // 运行中默认展开，看得见过程在往前走
    curLog().appendChild(box);
    t.procBox = box.querySelector(".proc-box-body");
  }
  return t.procBox;
}

function ensureThinkEl(t) {
  if (!t) return null;
  // 设置里关掉「显示思考过程」：不建元素、不渲染，正文照常流式
  if (!showThinking) return null;
  if (!t.thinkEl || !t.thinkEl.isConnected) {
    const el = document.createElement("div");
    el.className = "proc-row proc-think folded";
    el.innerHTML =
      '<button type="button" class="proc-head">' +
      '<span class="proc-ico">💭</span><span class="proc-name">思考</span>' +
      '<span class="proc-meta">思考中…</span><span class="proc-caret">▶</span>' +
      "</button>" +
      '<div class="proc-body"><div class="md"></div></div>';
    bindThinkRow(el);
    const body = ensureProcBox(t);
    body.appendChild(el);
    updateProcMeta(body.parentElement);
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
  // 元素自持一份摘要数据：历史恢复清空 tab 状态后仍可展开回看
  el._think = { text: t.thinkText, ms: t.thinkMs || (t.thinkT0 && t.thinkT1 ? t.thinkT1 - t.thinkT0 : 0) };
  // 节流渲染（同 appendStream 的增量方案）：思考文本往往比正文还长
  if (!t._thinkRenderTimer) {
    t._thinkRenderTimer = setTimeout(() => {
      t._thinkRenderTimer = null;
      if (!t.thinkEl || !t.thinkText) return;
      const body = t.thinkEl.querySelector(".proc-body .md");
      if (!body) return;
      if (!t._thinkBody) t._thinkBody = initStreamBody(body, false);
      const text = t.thinkText;
      t._thinkBody.sealed = renderStreamPart(
        t._thinkBody, text, t._thinkBody.sealed, streamSealCut(text, t._thinkBody.sealed));
      updateThinkMeta(t.thinkEl);
      scrollLog();
    }, 80);
  }
}

function finishThinking(t) {
  if (!t || !t.thinkEl || !t.thinkText) return;
  // 补上节流攒下的最后一次渲染（定稿按整文精确重画一次），再更新行头
  if (t._thinkRenderTimer) {
    clearTimeout(t._thinkRenderTimer);
    t._thinkRenderTimer = null;
    const body = t.thinkEl.querySelector(".proc-body .md");
    if (body) body.innerHTML = renderMarkdown(t.thinkText);
  }
  t._thinkBody = null;
  t.thinkEl._think = { text: t.thinkText, ms: t.thinkMs || (t.thinkT0 && t.thinkT1 ? t.thinkT1 - t.thinkT0 : 0) };
  updateThinkMeta(t.thinkEl);
  // 框头摘要里的「思考 X 秒」跟着定稿值走
  const box = t.thinkEl.closest(".proc-box");
  if (box) updateProcMeta(box);
}

// 历史恢复：同款思考行（默认折叠，点开看全文）
function addThinkingDone(text, tab, ms) {
  const t = tab || curTab();
  if (!text || !showThinking) return null;
  const el = document.createElement("div");
  el.className = "proc-row proc-think folded";
  el.innerHTML =
    '<button type="button" class="proc-head">' +
    '<span class="proc-ico">💭</span><span class="proc-name">思考</span>' +
    '<span class="proc-meta"></span><span class="proc-caret">▶</span>' +
    "</button>" +
    '<div class="proc-body"><div class="md"></div></div>';
  bindThinkRow(el);
  el.querySelector(".proc-body .md").innerHTML = renderMarkdown(text);
  el._think = { text, ms: ms || 0 };
  updateThinkMeta(el);
  // 历史渲染时收进当轮的过程框（paintHistorySlice 先备好）；实时/其他路径直接进聊天流。
  // 判存在用 contains 而不是 isConnected：启动时日志还没挂进文档（先 renderHistory
  // 后 activateTab），isConnected 恒为 false，会把框误判成「不存在」而把行丢到框外
  const inBox = !!(t && t._histProcBox && t.logEl && t.logEl.contains(t._histProcBox));
  const host = inBox ? t._histProcBox : curLog();
  host.appendChild(el);
  if (inBox) updateProcMeta(t._histProcBox.parentElement);
  // 历史恢复的思考行是一次性的：随消息渲染后断开流式关联
  if (t) { t.thinkEl = null; t.thinkText = ""; t.thinkMs = 0; }
  return el;
}

// 历史渲染的过程框：连续的思考行/中间说明收进同一个虚线框，遇到回答或用户消息收口。
// 用 tab.logEl.contains 判断框是否还在本栏（不能用 isConnected：启动时日志尚未挂进
// 文档，isConnected 恒为 false，会一条消息开一个新框、行全掉到框外）
function histProcBox(tab) {
  if (!tab) return null;
  if (!tab._histProcBox || !tab.logEl || !tab.logEl.contains(tab._histProcBox)) {
    const box = buildProcBox(true); // 历史默认收起，框头一行摘要就能看完
    curLog().appendChild(box);
    tab._histProcBox = box.querySelector(".proc-box-body");
  }
  return tab._histProcBox;
}

