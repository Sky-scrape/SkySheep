// 电脑控制操作回放（P13）：宏清单 / 录制 / 二次确认重放 / 停止。
//
// 安全口径与后端一致：录制只收 Agent 自己做过、用户逐次放行过的键鼠动作；
// 重放前给完整预览 + 二次确认，开始后逐条显示进度并可随时停。
// 电脑控制开关关着时整个区块只显示提示，不给入口。
//
// 零构建：普通 script，靠全局函数与 app.js 互调（app.js 负责在设置·技能页
// 渲染时调用 loadMacroCard()，并转发 macro_progress / macro_finished 事件）。

let macroState = { list: [], computer_control: false, replaying: false, running: null };
let macroMsg = null; // {text, kind}

function macroMsgRender() {
  const el = document.getElementById("macro-hint");
  if (!el) return;
  el.hidden = !macroMsg;
  el.textContent = macroMsg ? macroMsg.text : "";
  el.className = "dim small" + (macroMsg && macroMsg.kind ? " " + macroMsg.kind : "");
}

function macroMsgSet(text, kind) {
  macroMsg = text ? { text, kind: kind || "" } : null;
  macroMsgRender();
}

async function loadMacroCard() {
  const box = document.getElementById("macro-list");
  if (!box) return;
  let st;
  try {
    st = await request("macro.list", {});
  } catch (e) {
    // 旧后端没有这组方法：整块隐藏，不留一个点了没反应的空壳
    const card = document.getElementById("macro-card");
    if (card) card.hidden = true;
    return;
  }
  macroState.list = st.macros || [];
  macroState.computer_control = !!st.computer_control;
  macroState.replaying = !!st.replaying;
  renderMacroList();
}

function renderMacroList() {
  const box = document.getElementById("macro-list");
  if (!box) return;
  if (!macroState.computer_control) {
    box.innerHTML = '<li class="dim small">电脑控制还关着：宏要从「Agent 真做过的动作」里录，'
      + '先把上面的开关打开，让它用一次鼠标键盘后再回来录。</li>';
    macroMsgSet("");
    return;
  }
  if (!macroState.list.length) {
    box.innerHTML = '<li class="dim small">还没有宏。让 Agent 做一套操作（点按钮 / 输入 / 切窗口），'
      + '然后点右上「录制当前会话」把它存下来。</li>';
    macroMsgRender();
    return;
  }
  box.innerHTML = macroState.list.map((m) => `
    <li class="macro-row" data-name="${escapeHtml(m.name)}">
      <span class="macro-name">${escapeHtml(m.name)}</span>
      <span class="dim small">${m.steps} 步${m.created_at ? " · " + macroAgo(m.created_at) : ""}</span>
      <span class="spacer"></span>
      <button class="rp-mini macro-play" data-name="${escapeHtml(m.name)}">重放</button>
      <button class="rp-mini macro-del" data-name="${escapeHtml(m.name)}">删除</button>
    </li>`).join("");
  bindMacroRows();
  macroMsgRender();
}

// 「录制当前会话」：就地录入名字（不弹系统 prompt，WebView 里体验更稳）
function macroStartRecord() {
  const box = document.getElementById("macro-list");
  if (!box || !macroState.computer_control) return;
  box.innerHTML = `
    <li class="macro-row macro-record-row">
      <input id="macro-name-input" class="modal-input" type="text" maxlength="40"
             placeholder="给这个宏起个名字，例如「导出报表」">
      <button id="macro-name-ok" class="rp-mini primary">录制</button>
      <button id="macro-name-cancel" class="rp-mini">取消</button>
    </li>`;
  const input = document.getElementById("macro-name-input");
  if (input) input.focus();
  const submit = async () => {
    const name = (input && input.value || "").trim();
    if (!name) { macroMsgSet("先给宏起个名字", "bad"); return; }
    const sid = macroCurrentSid();
    if (!sid) { macroMsgSet("当前没有打开的会话，先聊两句再录", "bad"); return; }
    try {
      const st = await request("macro.save", { name, session_id: sid });
      macroState.list = st.macros || [];
      macroMsgSet(`已录制「${name}」`, "");
      await loadMacroCard();
    } catch (e) {
      macroMsgSet("录制失败：" + e.message, "bad");
      await loadMacroCard();
    }
  };
  const ok = document.getElementById("macro-name-ok");
  if (ok) ok.onclick = submit;
  const cancel = document.getElementById("macro-name-cancel");
  if (cancel) cancel.onclick = () => { macroMsgSet(""); loadMacroCard(); };
  if (input) input.onkeydown = (ev) => { if (ev.key === "Enter") submit(); };
}

async function macroReplay(name) {
  let preview;
  try {
    preview = await request("macro.replay", { name });
  } catch (e) {
    macroMsgSet("重放失败：" + e.message, "bad");
    return;
  }
  if (!preview || !preview.needs_confirm) {
    // 后端已经开跑（理论上只有手工调用才会这样）：直接提示
    macroMsgSet("重放已开始", "");
    return;
  }
  const lines = (preview.preview || []).slice(0, 12).join("\n");
  const more = (preview.preview || []).length > 12 ? "\n…（还有更多步骤）" : "";
  const ok = await confirmModal(
    `将重放「${name}」共 ${preview.steps} 步：\n\n${lines}${more}\n\n${preview.hint || ""}`,
    { title: "重放操作", okLabel: "开始重放" },
  );
  if (!ok) return;
  const sid = macroCurrentSid();
  try {
    const r = await request("macro.replay", { name, confirm: true, session_id: sid });
    macroState.running = name;
    macroMsgSet(`正在重放「${name}」（${r.steps} 步）…`, "");
    const btn = document.getElementById("macro-replay-stop");
    if (!btn) macroAppendStopButton();
  } catch (e) {
    macroMsgSet("重放失败：" + e.message, "bad");
  }
}

function macroAppendStopButton() {
  const card = document.getElementById("macro-card");
  if (!card) return;
  const row = document.createElement("div");
  row.className = "macro-ops";
  const btn = document.createElement("button");
  btn.id = "macro-replay-stop";
  btn.className = "rp-mini danger";
  btn.textContent = "停止重放";
  btn.onclick = async () => {
    try { await request("macro.stop"); } catch (e) { /* 已结束就忽略 */ }
  };
  row.appendChild(btn);
  card.appendChild(row);
}

function macroRemoveStopButton() {
  const btn = document.getElementById("macro-replay-stop");
  if (btn && btn.parentElement) btn.parentElement.remove();
}

// 当前会话 id：录制与重放都归属它（宏从会话历史里录）
function macroCurrentSid() {
  try {
    const t = (typeof curTab === "function") ? curTab() : null;
    return (t && t.sid) || "";
  } catch (e) { return ""; }
}

function macroAgo(ts) {
  const diff = Date.now() / 1000 - Number(ts || 0);
  if (!isFinite(diff) || diff <= 0) return "刚刚";
  if (diff < 60) return Math.round(diff) + " 秒前";
  if (diff < 3600) return Math.round(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.round(diff / 3600) + " 小时前";
  return Math.round(diff / 86400) + " 天前";
}

// 进度事件（app.js 的引擎事件 switch 转发进来）
function macroOnProgress(d) {
  macroState.running = d.name || macroState.running;
  if (!document.getElementById("macro-replay-stop")) macroAppendStopButton();
  const okMark = d.ok ? "✓" : "✗";
  macroMsgSet(`${okMark} 第 ${d.index}/${d.total} 步：${d.step_tool} — ${d.preview || ""}`, d.ok ? "" : "bad");
}

function macroOnFinished(d) {
  macroState.running = null;
  macroRemoveStopButton();
  const head = d.stopped
    ? `已停止：执行了 ${d.executed}/${d.total} 步`
    : (d.error ? `重放中断：${d.error}` : `「${d.name}」重放完成（${d.executed} 步）`);
  macroMsgSet(head, d.error || d.stopped ? "bad" : "");
  loadMacroCard();
}

function bindMacroRows() {
  document.querySelectorAll(".macro-play").forEach((btn) => {
    btn.onclick = () => macroReplay(btn.dataset.name);
  });
  document.querySelectorAll(".macro-del").forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      const ok = await confirmModal(`删除宏「${name}」？删了就没了。`,
        { title: "删除宏", okLabel: "删除", danger: true });
      if (!ok) return;
      try {
        const st = await request("macro.delete", { name });
        macroState.list = st.macros || [];
        macroMsgSet(`已删除「${name}」`, "");
        renderMacroList();
      } catch (e) {
        macroMsgSet("删除失败：" + e.message, "bad");
      }
    };
  });
}

const macroRecordBtn = document.getElementById("macro-record");
if (macroRecordBtn) macroRecordBtn.onclick = () => macroStartRecord();
