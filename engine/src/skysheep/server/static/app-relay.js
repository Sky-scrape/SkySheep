/* 接力（主副屏交替）：右侧看板控制 + 副屏分栏。
 *
 * 主屏 = 当前会话；副屏 = 绑定给这次接力的独立会话。每次「执行」让轮到的一方
 * 跑完整的一轮正常对话（后端走 chat 管道，工具与权限门照常），完成后自动交棒。
 * 这个文件只管控制与轮次展示；两屏的正文由各自的会话界面渲染。 */
"use strict";

let relayDual = null;          // 最近一次快照 {pair|null, turns, main, peer, defaults}
let relayDualPairId = null;    // 当前接力的 pair id（没有则为 null）
let relayDualOpenFor = null;   // 这份快照属于哪个主屏会话
let relayDualBusy = false;     // 控制请求在飞（防连点）
let relayDualSaveTimer = null; // 任务编辑的防抖保存
let relayDualSyncTimer = null; // 切会话时的面板刷新防抖
let relayDualLoading = null;   // 正在进行的 dual_open（并发入口合流）
let relayDualSeq = 0;

const RELAY_SIDE_LABEL = { main: "主屏", peer: "副屏" };
const RELAY_STATE_LABEL = {
  idle: "等待执行",
  running: "执行中",
  paused: "已中断",
  interrupted: "上次中断",
};

function relayEl(id) {
  return document.getElementById(id);
}

function relayStatus(text) {
  const el = relayEl("relay-run-status");
  if (el) el.textContent = text;
}

function relayRequestId() {
  relayDualSeq += 1;
  return "relay-" + Date.now().toString(36) + "-" + relayDualSeq;
}

function relayPair() {
  return relayDual && relayDual.pair ? relayDual.pair : null;
}

function relayCurrentSide() {
  const pair = relayPair();
  return pair && pair.current_side === "peer" ? "peer" : "main";
}

function relayNextSide() {
  return relayCurrentSide() === "peer" ? "main" : "peer";
}

// ---------- 打开 / 渲染 ----------

async function relayDualOpen(options = {}) {
  const sid = typeof currentSessionId !== "undefined" ? currentSessionId : null;
  if (!sid) {
    relayDual = null;
    relayDualPairId = null;
    relayDualOpenFor = null;
    relayDualRender();
    relayStatus("先在主界面打开一个会话，再开始接力");
    return null;
  }
  let data;
  try {
    // 侧栏入口与右侧面板加载可能同时触发：合流成一次请求，避免并发创建副屏
    if (relayDualLoading) {
      data = await relayDualLoading;
    } else {
      relayDualLoading = request("relay.dual_open", {
        session_id: String(sid),
        create_peer: !!options.createPeer,
      });
      try {
        data = await relayDualLoading;
      } finally {
        relayDualLoading = null;
      }
    }
    // 并发加载的旧响应可能晚于「创建副屏」那次到达：已经开着的副屏不能被它抹掉
    const previousPeer = relayDual && relayDualOpenFor === String(sid) ? relayDual.peer : null;
    const keepPeer = previousPeer
      && splitPanes.some((pane) => pane.sid === previousPeer.id) ? previousPeer : null;
    relayDual = {
      ...data,
      peer: data.peer || keepPeer,
      pair: data.pair ? data.pair.pair : null,
      turns: (data.pair && data.pair.turns) || [],
    };
    relayDualPairId = relayDual.pair ? relayDual.pair.id : null;
    relayDualOpenFor = String(sid);
    relayDualRender();
    if (options.openPeer && data.peer) await relayDualShowPeer(data.peer);
    return data;
  } catch (error) {
    relayDualLoading = null;
    relayStatus("读取接力失败：" + error.message);
    return null;
  }
}

async function relayDualShowPeer(peer) {
  if (!peer || !peer.id) return;
  const exists = splitPanes.find((pane) => pane.sid === peer.id);
  if (exists) {
    focusSplitPane(exists);
    return;
  }
  await openSplitPane(peer.id, peer.title || "接力 · 副屏");
}

/** 点主页面的「接力」：开右侧看板 + 把副屏会话打开在分栏里。 */
async function relayOpenWithPeer() {
  const sid = typeof currentSessionId !== "undefined" ? currentSessionId : null;
  if (!sid) {
    relayStatus("先在主界面打开一个会话，再开始接力");
    return;
  }
  let data = relayDualOpenFor === String(sid) && relayDual
    ? relayDual
    : await relayDualOpen();
  // 面板自动加载不会建副屏（避免开应用就多一条空会话）；显式点接力才建
  if (!data || !data.peer) data = await relayDualOpen({ createPeer: true });
  if (data && data.peer) await relayDualShowPeer(data.peer);
}

function relayDualFillInputs() {
  const pair = relayPair();
  const apply = (id, value) => {
    const el = relayEl(id);
    if (!el || document.activeElement === el) return; // 正在输入时不抢
    el.value = value || "";
  };
  apply("relay-goal", pair ? pair.goal : "");
  apply("relay-main-task", pair ? pair.main_task : "");
  apply("relay-peer-task", pair ? pair.peer_task : "");
  const auto = relayEl("relay-auto-alternate");
  if (auto && document.activeElement !== auto) {
    auto.checked = !!(pair && pair.auto_alternate);
  }
}

function relayDualTurnPreview(turn) {
  if (turn.status === "running") return "正在执行…";
  if (turn.status === "cancelled") return "已中断";
  if (turn.status === "error") return (turn.error || "执行失败").slice(0, 120);
  const text = (turn.output || "").trim().replace(/\s+/g, " ");
  return text ? text.slice(0, 120) + (text.length > 120 ? "…" : "") : "（没有产出文本）";
}

function relayDualRenderTurns() {
  const box = relayEl("relay-turn-list");
  const count = relayEl("relay-turn-count");
  if (!box) return;
  while (box.firstChild) box.removeChild(box.firstChild);
  const turns = (relayDual && relayDual.turns) || [];
  if (count) count.textContent = turns.length ? `共 ${turns.length} 轮` : "";
  if (!turns.length) {
    const p = document.createElement("p");
    p.className = "dim small";
    p.textContent = "还没有轮次。点「开始接力」让主屏先来。";
    box.appendChild(p);
    return;
  }
  turns.slice().reverse().forEach((turn) => {
    const row = document.createElement("div");
    row.className = "relay-turn" + (turn.status === "running" ? " running" : "")
      + (turn.status === "error" ? " failed" : "");
    const head = document.createElement("div");
    head.className = "relay-turn-head";
    const no = document.createElement("b");
    no.textContent = `第 ${turn.turn_no} 轮 · ${RELAY_SIDE_LABEL[turn.side] || turn.side}`;
    const state = document.createElement("span");
    state.className = "dim small";
    state.textContent = {
      running: "执行中", done: "已完成", cancelled: "已中断", error: "失败",
    }[turn.status] || turn.status;
    head.appendChild(no);
    head.appendChild(state);
    const body = document.createElement("p");
    body.className = "relay-turn-text";
    body.textContent = relayDualTurnPreview(turn);
    if (turn.output) row.title = turn.output;
    row.appendChild(head);
    row.appendChild(body);
    box.appendChild(row);
  });
}

function relayDualRender() {
  const pair = relayPair();
  const mainChip = relayEl("relay-main-chip");
  const peerChip = relayEl("relay-peer-chip");
  const head = relayEl("relay-pair-head");
  const mainTitle = (relayDual && relayDual.main && relayDual.main.title) || "当前会话";
  const peerTitle = (relayDual && relayDual.peer && relayDual.peer.title) || "未创建";
  if (mainChip) mainChip.textContent = "主屏 · " + mainTitle;
  if (peerChip) {
    peerChip.textContent = "副屏 · " + peerTitle;
    peerChip.classList.toggle("off", !(relayDual && relayDual.peer));
  }
  const runningNow = !!(pair && pair.state === "running");
  if (mainChip) mainChip.classList.toggle("active", runningNow && pair.current_side !== "peer");
  if (peerChip) peerChip.classList.toggle("active", runningNow && pair.current_side === "peer");
  if (head) {
    head.textContent = pair
      ? `${RELAY_SIDE_LABEL[pair.current_side] || "主屏"} · ${
          RELAY_STATE_LABEL[pair.state] || pair.state
        }${pair.turn_no ? ` · 已完成 ${pair.turn_no} 轮` : ""}`
      : "";
  }

  relayDualFillInputs();
  relayDualRenderTurns();

  const start = relayEl("relay-run-start");
  const stop = relayEl("relay-run-stop");
  const state = pair ? pair.state : "idle";
  const running = state === "running";
  if (start) {
    start.textContent = running
      ? "执行中…"
      : (!pair ? "开始接力" : `让${RELAY_SIDE_LABEL[relayCurrentSide()]}执行`);
    start.disabled = relayDualBusy || running || !relayDualOpenFor;
  }
  if (stop) stop.disabled = relayDualBusy || !running;

  if (!pair) {
    if (relayDualOpenFor) relayStatus("填写总任务和两屏任务，点「开始接力」");
    return;
  }
  const side = RELAY_SIDE_LABEL[pair.current_side] || "主屏";
  const other = RELAY_SIDE_LABEL[relayNextSide()];
  if (running) {
    relayStatus(`第 ${pair.turn_no + 1} 轮 · ${side}正在执行…`);
  } else if (state === "paused") {
    relayStatus(`已中断 · 点「让${side}执行」继续`);
  } else if (state === "interrupted") {
    relayStatus(`上次中断 · 点「让${side}执行」继续`);
  } else if (pair.error && pair.error.message) {
    relayStatus(pair.error.message);
  } else {
    relayStatus(
      pair.turn_no
        ? `第 ${pair.turn_no} 轮完成 · 轮到${side}（上次是${other}）`
        : `准备就绪 · 从${side}开始`
    );
  }
}

// ---------- 执行 / 中断 ----------

async function relayDualRun() {
  const sid = typeof currentSessionId !== "undefined" ? currentSessionId : null;
  if (!sid) {
    relayStatus("先在主界面打开一个会话，再开始接力");
    return;
  }
  const goalEl = relayEl("relay-goal");
  const mainTaskEl = relayEl("relay-main-task");
  const peerTaskEl = relayEl("relay-peer-task");
  const autoEl = relayEl("relay-auto-alternate");
  const goal = (goalEl && goalEl.value || "").trim();
  if (!goal) {
    if (goalEl) goalEl.focus();
    relayStatus("先填写总任务");
    return;
  }
  const mainTask = (mainTaskEl && mainTaskEl.value || "").trim();
  if (!mainTask) {
    if (mainTaskEl) mainTaskEl.focus();
    relayStatus("先填写主屏每一轮做什么");
    return;
  }
  const peerTask = (peerTaskEl && peerTaskEl.value || "").trim();
  if (!peerTask) {
    if (peerTaskEl) peerTaskEl.focus();
    relayStatus("先填写副屏每一轮做什么");
    return;
  }
  const pair = relayPair();
  const params = {
    session_id: String(sid),
    goal,
    main_task: mainTask,
    peer_task: peerTask,
    auto_alternate: !!(autoEl && autoEl.checked),
    request_id: relayRequestId(),
    expected_revision: pair ? pair.revision : 0,
  };
  if (relayDualPairId) params.pair_id = relayDualPairId;
  relayDualBusy = true;
  relayDualRender();
  try {
    const data = await request("relay.dual_run", params);
    relayDual = {
      ...(relayDual || {}),
      ...data,
      pair: data.pair,
      turns: data.turns || [],
    };
    relayDualPairId = data.pair ? data.pair.id : null;
    relayDualOpenFor = String(sid);
    relayDualRender();
    if (data.peer) await relayDualShowPeer(data.peer);
    relayStatus(`已交给${RELAY_SIDE_LABEL[data.pair.current_side] || "主屏"}执行…`);
  } catch (error) {
    relayStatus("执行失败：" + error.message);
  } finally {
    relayDualBusy = false;
    relayDualRender();
  }
}

async function relayDualStop() {
  const pair = relayPair();
  if (!pair) return;
  relayDualBusy = true;
  relayDualRender();
  try {
    const data = await request("relay.dual_stop", {
      pair_id: pair.id,
      request_id: relayRequestId(),
      expected_revision: pair.revision,
    });
    relayDual = {
      ...(relayDual || {}),
      ...data,
      pair: data.pair,
      turns: data.turns || [],
    };
    relayDualRender();
    relayStatus("已中断本轮；点「执行」可以从这一方继续");
  } catch (error) {
    relayStatus("中断失败：" + error.message);
  } finally {
    relayDualBusy = false;
    relayDualRender();
  }
}

// ---------- 任务编辑的兜底保存 ----------

function relayDualScheduleSave() {
  if (!relayDualPairId) return; // 还没有接力：执行时连同当前值一起提交
  clearTimeout(relayDualSaveTimer);
  relayDualSaveTimer = setTimeout(async () => {
    if (!relayDualPairId) return;
    const goalEl = relayEl("relay-goal");
    const mainTaskEl = relayEl("relay-main-task");
    const peerTaskEl = relayEl("relay-peer-task");
    const autoEl = relayEl("relay-auto-alternate");
    try {
      const data = await request("relay.dual_save", {
        pair_id: relayDualPairId,
        goal: (goalEl && goalEl.value || "").trim(),
        main_task: (mainTaskEl && mainTaskEl.value || "").trim(),
        peer_task: (peerTaskEl && peerTaskEl.value || "").trim(),
        auto_alternate: !!(autoEl && autoEl.checked),
      });
      if (data && data.pair && relayDualPairId === data.pair.id) {
        relayDual = { ...(relayDual || {}), ...data, pair: data.pair, turns: data.turns || [] };
      }
    } catch (error) {
      // 静默：下一次「执行」会带着当前输入值一起提交
    }
  }, 500);
}

// ---------- 事件 / 生命周期 ----------

function relayOnPairState(snapshot) {
  if (!snapshot || !snapshot.pair) return;
  if (relayDualOpenFor && snapshot.pair.main_session_id !== relayDualOpenFor) return;
  const prev = relayDual || {};
  relayDual = {
    ...prev,
    pair: snapshot.pair,
    turns: snapshot.turns || [],
    // 广播只带 pair/turns，主副屏标题沿用打开时那一份
    main: prev.main || { id: snapshot.pair.main_session_id, title: "当前会话" },
    peer: prev.peer || { id: snapshot.pair.peer_session_id, title: "接力 · 副屏" },
  };
  relayDualPairId = snapshot.pair.id;
  relayDualRender();
}

function relayDualOnSessionChange() {
  const page = relayEl("rp-page-relay");
  if (!page || page.classList.contains("hidden")) return;
  const sid = typeof currentSessionId !== "undefined" ? currentSessionId : null;
  if (!sid || sid === relayDualOpenFor) return;
  clearTimeout(relayDualSyncTimer);
  relayDualSyncTimer = setTimeout(() => { relayDualOpen(); }, 250);
}

/** 右侧面板切到接力页时调用（与右侧标签加载器同一入口）。 */
function relayWorkbenchShown() {
  const sid = typeof currentSessionId !== "undefined" ? currentSessionId : null;
  if (!sid) {
    relayDualRender();
    relayStatus("先在主界面打开一个会话，再开始接力");
    return;
  }
  if (relayDualOpenFor !== String(sid)) {
    relayDualOpen();
    return;
  }
  relayDualRender();
}

function relayOnReconnect() {
  relayDualOpen();
}

/** 切项目：清掉上个项目的接力快照，等面板再次显示时重读。 */
function relayResetForProject() {
  relayDual = null;
  relayDualPairId = null;
  relayDualOpenFor = null;
  relayDualRender();
}

document.addEventListener("DOMContentLoaded", () => {
  const start = relayEl("relay-run-start");
  if (start) start.addEventListener("click", relayDualRun);
  const stop = relayEl("relay-run-stop");
  if (stop) stop.addEventListener("click", relayDualStop);
  const peer = relayEl("relay-open-peer");
  if (peer) {
    peer.addEventListener("click", () => {
      if (relayDual && relayDual.peer) relayDualShowPeer(relayDual.peer);
      else relayDualOpen({ createPeer: true, openPeer: true });
    });
  }
  ["relay-goal", "relay-main-task", "relay-peer-task"].forEach((id) => {
    const el = relayEl(id);
    if (el) el.addEventListener("input", relayDualScheduleSave);
  });
  const auto = relayEl("relay-auto-alternate");
  if (auto) auto.addEventListener("change", relayDualScheduleSave);
});

/** 副屏会话被删除（侧栏/标签/另一窗口）：看板同步回到「未创建」。 */
function relayPeerDeleted(sid) {
  if (!relayDual || !relayDual.peer || relayDual.peer.id !== sid) return;
  relayDual = { ...relayDual, peer: null };
  relayDualRender();
}
