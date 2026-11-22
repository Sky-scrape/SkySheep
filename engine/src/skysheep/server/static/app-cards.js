// app-cards.js —— 协作卡片渲染：圆桌 rt* 系 / 对抗 adv* 系 / 团队 tc* 系（含历史回放卡）。
// 从 app.js 拆出（路线图第 2 批）；普通 script 全局函数互调，加载顺序见 index.html：
// 后于 app.js（卡片函数调用它的渲染助手与 request/showModal 等，事件期解析）、
// 先于 app-teamroom.js（团队室三分区容器复用 tc* 系整卡）。
// 共享助手 fmtTokens 仍留在 app.js（任务页签与工厂视图也用）。
// 协作发起区（setWorkMode/showCoopMenu/build*SettingsInto…，原 9247-10153）第 3 批迁入本文件。

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
      ? `◆ 融合失败  保留 ${m.provider}/${m.model} 的草稿`
      : `◆ A/B 对比  ${m.provider}/${m.model}`;
  } else {
    const rounds = meta.rounds || 1;
    badge.textContent = `◆ 圆桌融合  ${okCount} 个成员 + 主席`
      + (rounds > 1 ? `  辩论 ${rounds - 1} 轮` : "");
  }
  el.prepend(badge);
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
  title.textContent = "👥 圆桌  观点对照";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "折叠";
  fold.setAttribute("aria-expanded", "true");
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    fold.setAttribute("aria-expanded", folded ? "false" : "true");
    sub.textContent = folded
      ? `${members.length} 个模型  已折叠  点击展开查看各成员草稿`
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
  // 角色位：模板成员带 name（模板里起的角色名）优先显示；没名字才回落到内置角色键
  const roleText = m.name ? String(m.name) : (m.role ? rtRoleLabel(m.role) : "");
  mcard.innerHTML =
    `<div class="rt-m-head"><span class="rt-m-idx">${idx || "·"}</span>` +
    `<span class="rt-m-name">${escapeHtml(m.provider)}</span>` +
    `<span class="rt-m-model">${escapeHtml(m.model)}</span>` +
    `<span class="rt-m-role"${roleText ? "" : " hidden"}>${roleText ? escapeHtml(roleText) : ""}</span>` +
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
  let expandedSub = `${members.length} 个模型  各成员草稿（独立成稿）`;
  if (rounds > 1) expandedSub += `  辩论 ${rounds - 1} 轮`;
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
  sub.textContent = `${members.length} 个模型  已折叠  点击展开查看各成员草稿`;
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
    ? `${(t.advFindingEls || []).length} 条候选问题  已折叠  点击展开查看清单`
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
  // 与历史对比（P3 剩余半边）：看这次比同会话上一次多了/少了哪些发现。
  // 只读、只在这张卡下方加一行注记，不新增弹窗层级。
  const cmp = tcEl("button", "adv-compare", "与历史对比");
  cmp.title = "对比同一会话里上一次对抗审查：发现总数与裁决档位变化、新增与已消失的问题";
  cmp.onclick = (ev) => {
    ev.stopPropagation();
    cmp.disabled = true;
    request("adversarial.compare", { session_id: tab.sid, seq })
      .then((r) => { advRenderCompare(box, r); })
      .catch((e) => addNotice("对比失败: " + e.message))
      .finally(() => { cmp.disabled = false; });
  };
  box.appendChild(cmp);
  // 一键修复成立项（O2）：把「成立 / 部分成立」的发现拼成一段修复指令回填
  // 输入框——只回填不发送（修不修、在本会话还是转团队由用户决定，不制造
  // 意外开销与写操作）。prompt 由后端按库内 meta 组装，前端不拼正文。
  const fixable = (meta && meta.findings || []).filter(
    (f) => f && (f.verdict === "confirmed" || f.verdict === "partial"),
  );
  if (fixable.length) {
    const fix = tcEl("button", "adv-fix", `一键修复 ${fixable.length} 项`);
    fix.title = "把认定成立的问题拼成一段修复指令填进输入框（不自动发送）";
    fix.onclick = (ev) => {
      ev.stopPropagation();
      fix.disabled = true;
      request("adversarial.fix_prompt", { session_id: tab.sid, seq })
        .then((r) => {
          const pane = splitPanes.includes(tab) ? tab : null;
          const input = pane ? pane.el.querySelector(".split-input") : document.getElementById("input");
          if (!input) { addNotice("这一栏没有输入框可回填"); return; }
          const existing = input.value.trim();
          input.value = existing ? existing + "\n" + r.prompt : r.prompt;
          if (!pane) autoGrowInput();
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
          addNotice(`已填入 ${r.count} 条成立项的修复指令：确认后发送即可。`);
        })
        .catch((e) => addNotice("生成修复指令失败: " + e.message))
        .finally(() => { fix.disabled = false; });
    };
    box.appendChild(fix);
  }
  return box;
}

// 把 compare 结果渲染成卡片下方的一行注记（多行文本，不弹窗）
function advRenderCompare(box, r) {
  const old = box.querySelector(".adv-compare-note");
  if (old) old.remove();
  const note = document.createElement("div");
  note.className = "adv-compare-note dim small";
  if (!r || !r.previous) {
    note.textContent = "这是该会话的第一次对抗审查，暂无可对比的历史。";
    box.appendChild(note);
    return;
  }
  const d = r.delta || {};
  const sign = (n) => (n > 0 ? "+" + n : String(n));
  const bits = [
    `对比 seq ${r.previous_seq}：发现 ${r.previous.total} → ${r.current.total}（${sign(d.total || 0)}）`,
  ];
  const verdict = [];
  for (const [key, label] of [["confirmed", "成立"], ["partial", "部分成立"],
    ["refuted", "推翻"], ["pending", "待定"]]) {
    if (d[key]) verdict.push(`${label} ${sign(d[key])}`);
  }
  if (verdict.length) bits.push("裁决变化：" + verdict.join("、"));
  bits.push(`新增 ${(r.added || []).length} 条、已消失 ${(r.resolved || []).length} 条、仍在 ${r.common} 条`);
  note.textContent = bits.join("｜");
  note.title = [
    (r.added || []).length ? "新增：" + r.added.join("；") : "",
    (r.resolved || []).length ? "已消失：" + r.resolved.join("；") : "",
  ].filter(Boolean).join("\n") || "没有增减的条目";
  box.appendChild(note);
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
      "登记为执行型待办，是否派工由你在工单板上手动操作。"
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
      "只登记为顾问型待办，是否派工由你在工单板上手动操作。"
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
      head.textContent = `${f.id || "?"}  ${f.verdict === "confirmed" ? "成立" : "部分成立"}`
        + `  ${f.severity || "medium"}  ${f.category || "问题"}`;
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
  const subText = sumBits.length ? sumBits.join("  ") : "未发现候选问题";
  const fold = document.createElement("button");
  fold.className = "rt-fold";
  fold.textContent = "展开";
  fold.setAttribute("aria-expanded", "false");
  const toggleFold = () => {
    const folded = card.classList.toggle("folded");
    fold.textContent = folded ? "展开" : "折叠";
    fold.setAttribute("aria-expanded", folded ? "false" : "true");
    sub.textContent = folded
      ? `${findings.length} 条候选问题  已折叠  点击展开查看清单`
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
  sub.textContent = `${findings.length} 条候选问题  已折叠  点击展开查看清单`;
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
    parts.push(`${s.total} 条候选` + (bits.length ? `（${bits.join("  ")}）` : ""));
  } else {
    parts.push("未发现候选问题");
  }
  if (meta.status === "cancelled") parts.push("已中止");
  else if (meta.status === "error") parts.push("未完成");
  badge.textContent = parts.join("  ");
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
  const closedCount = tcEl("span", "tc-closed-count", "已完成 0  失败 0");
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
  ui.closedCount.textContent = `已完成 ${done}  失败 ${err}`;
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
  ui.attendance.textContent = bits.length ? "出勤：" + bits.join("  ") : "";
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
  // 在协作中打开（团队模式设计 §15.1 入口①）：跳右面板「协作」页签，
  // 频道 / 工单板 / 成员三分区同屏；卡片与看板同源渲染、双向即时同步
  const roomBtn = tcEl("button", "tc-open-room", "在协作中打开");
  roomBtn.title = "在右侧面板的「协作」页签打开本团队：成员、频道、工单板三分区同屏";
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
  deliverBtn.title = "确认交付：产出《交付说明》并关闭团队，本会话恢复普通对话";
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
  stopBtn.title = "立即终止团队：发言中的队员定稿后产出《收队说明》";
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
  takeoverBtn.title = "接管：切回你本人指挥，自动推进取消，工单板与频道保留";
  takeoverBtn.onclick = async () => {
    if (!(await confirmModal("接管后由你直接指挥：自动推进取消，工单板与频道保留。确定接管？",
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
  saveTplBtn.title = "把当前队伍存成团队模板：之后在团队面板「从模板创建」一键复用";
  saveTplBtn.onclick = () => tcSaveTemplateModal(card);
  // 常驻开关（T3.2）：把这支队伍登记为跨会话常驻——定时团队任务触发时优先
  // 唤醒它（频道历史与工单板延续）而不是每次重建；再点一次解除。只在活动
  // 团队上可操作（快照对齐点亮，tcFinish 收口），终态后随卡隐藏
  const residentBtn = tcEl("button", "tc-resident", "常驻");
  residentBtn.hidden = true;
  residentBtn.title = "常驻：定时团队任务触发时优先唤醒这支队伍（频道历史与工单板延续）而不是每次重建；再点一次解除";
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
        // 侧栏角标镜像（§15.3.5）：登记已解除，等不到下一次探询就先记负
        if (typeof trSideNote === "function") trSideNote(String(card._teamId || ""), false);
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
      // 侧栏角标镜像（§15.3.5）：set/clear 回包带 team_id 与登记结果，即时并入
      if (typeof trSideNote === "function") {
        trSideNote(String((r && r.team_id) || card._teamId || ""), now);
      }
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
      `AI 总管  ${dir.provider || "?"}/${dir.model || "默认"}` +
      (dTok ? `  ${fmtTokens(dTok)}` : ""));
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
    <p class="dim small">保存内容：${roster.length} 名成员（${memberLabel || "空名册"}）  ${dirLabel}。
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
    `${(team.roster || []).length} 名队员  已用 ${team.rounds_used || 0}` +
    `${team.max_rounds ? `/${team.max_rounds}` : ""} 轮` +
    (tasks.length ? `  工单 ${tasks.length} 张` : "  尚无工单") +
    (tokens ? `  ${fmtTokens(tokens)} tokens` : "") +
    (idleShort ? `  已空闲：${idleShort}` : "");
  // 建队来源模板（协作模板）标在副标题最前面：一眼知道这队按哪份配置建的
  if (card._template) card._ui.sub.textContent = `模板：${card._template}  ` + card._ui.sub.textContent;
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
    const i = sub.textContent.indexOf("  已空闲：");
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
    // 侧栏角标的常驻镜像（§15.3.5）：探询结果两个方向都是权威真值——在册收进
    // 镜像（翻旧会话即给角标补上登记数），不在册就剔除（别处解除过的旧账）
    if (typeof trSideNote === "function") {
      trSideNote(String(card._teamId || ""), !!(r && r.resident));
    }
    if (!(r && r.resident)) return;
    btn.hidden = false;
    btn.disabled = false;
    btn.classList.remove("is-active");
    btn.textContent = "解除常驻";
    btn.title = "解除常驻：定时任务不再按这支队伍复用";
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
    // 侧栏「团队」角标（§15.3.5）：team.get 回包对活动团队是权威——对齐当前
    // 会话的活动团队并并入常驻镜像。快速连切会话时只认仍是当前页签的回包，
    // 迟到的旧会话快照不覆盖新对齐（团队卡走的 withTab 渲染另有归属，不混用）
    if (typeof trSideAlign === "function") {
      const now = curTab();
      if ((now && now.sid) === tab.sid) trSideAlign(r && r.active ? r.team : null, tab.sid);
    }
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
        card._ui.sub.textContent = "应用重启前的团队记录  只读回放";
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
    fresh._ui.sub.textContent = "应用重启前的团队记录  只读回放";
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
    `🧩 团队纪要  ${roster.length} 名队员` +
    (team.director_mode === "ai" ? "  AI 总管" : "") +
    `  ${TEAM_STATUS_LABEL[team.status] || "进行中"}`;
  el.prepend(badge);
}
