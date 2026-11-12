// app-collab.js —— 协作模板（L1）：设置页画廊 / 模板编辑器 / 协作菜单分组 / 跑前清单
// 零构建普通 script，先 app.js 后本文件；只做函数声明与状态常量，加载期只做
// 「静态按钮接线 + 首次拉模板册」两件事。跨文件互调 request / escapeHtml /
// showModal / hideModal / confirmModal / addNotice / autoHideStatus /
// setRtOn / setTeamOn / setAdvOn / updateCoopHint（都在 app.js，运行时才解析）。
//
// 安全口径（不可协商，前端只展示与传参，判定在引擎侧）：
// · 模板永远不能关闭或降低权限门：模板 schema 里没有 gate 字段，只能声明
//   needs(read|write|exec) 与工具白名单；
// · 跑前勾选「本次不再逐次确认」只对模板声明过的写入（且落在工作目录内）
//   与命令前缀生效：删除语义、DANGEROUS 工具、引擎主目录写入、工作目录外
//   写入一律仍逐次确认（GrantedGate 在 security/gate.py 里兜底）；
// · 圆桌 / 对抗本期仍是纯文本协作：角色只读、不挂工具，模板里写了也会被后端拒绝。

const COLLAB_KIND_LABELS = { roundtable: "圆桌", team: "团队", adversarial: "对抗" };
const COLLAB_NEEDS_LABELS = { read: "只读", write: "可写", exec: "可执行" };
const COLLAB_NEEDS_ORDER = { read: 0, write: 1, exec: 2 };
// 与 core/collab_templates.py 的 BUILTIN_NAMES 同源（前端只做即时提示，最终以后端校验为准）
const COLLAB_BUILTIN_NAMES = ["圆桌·多模型评审", "团队·交付小组", "对抗·代码审查"];

// 合并后的模板册（内置 + 我的；团队模板已转成协作模板形状）。协作菜单同步读它，
// 所以设置页与菜单共用一份缓存，保存/删除后一起刷新。
let collabTplAll = [];
// 编辑器状态：{ draft, source, builtin, isNew, teamTools }
let collabEditorState = null;
// config.providers 缓存（只用于服务/模型候选提示，不做任何可用性判定）
let collabProvidersCache = null;

function collabClone(v) {
  try { return JSON.parse(JSON.stringify(v)); } catch (e) { return v; }
}

/** 模板的 needs 档位：优先模板级，其次角色最高档（团队模板由 members 反向生成）。 */
function collabTplNeeds(t) {
  const direct = String((t && t.needs) || "").trim();
  if (direct && COLLAB_NEEDS_LABELS[direct]) return direct;
  let top = "read";
  for (const r of (t && Array.isArray(t.roles) ? t.roles : [])) {
    const n = String((r && r.needs) || "read");
    if ((COLLAB_NEEDS_ORDER[n] || 0) > (COLLAB_NEEDS_ORDER[top] || 0)) top = n;
  }
  return top;
}

/** 团队模板册 → 协作模板形状（画廊/编辑器/菜单统一口径）。
 *
 * 两种输入都认：后端 `collab.template_list` 的 team_templates 已经是协作模板
 * 形状（roles/aggregator/limits/budget），直接沿用；手写 teams.json 的原生形状
 * （members/director）在这里转换。两种都按团队级 needs/tools 回填到每个角色
 * （本期团队是团队级工具并集，编辑器保存时会收拢回最高档角色）。
 */
function collabTemplateFromTeam(t) {
  const src = t || {};
  const budget = src.budget || {};
  const limits = src.limits || {};
  const teamNeeds = String(src.needs || "").trim();
  const needsOf = (raw) => (COLLAB_NEEDS_LABELS[raw] ? raw : null);
  const teamAllow = Array.isArray(src.tools_allow) ? src.tools_allow.slice() : [];
  const teamDeny = Array.isArray(src.tools_deny) ? src.tools_deny.slice() : [];
  const roles = (Array.isArray(src.roles) ? src.roles : [])
    .map((r, i) => ({
      key: String((r && r.key) || ("m" + i)),
      name: String((r && r.name) || "").trim() || `队员${i + 1}`,
      provider: String((r && r.provider) || ""),
      model: String((r && r.model) || ""),
      prompt: String((r && r.prompt) || ""),
      needs: needsOf(r && r.needs) || needsOf(teamNeeds) || "read",
      tools_allow: Array.isArray(r && r.tools_allow) ? r.tools_allow.slice() : teamAllow.slice(),
      tools_deny: Array.isArray(r && r.tools_deny) ? r.tools_deny.slice() : teamDeny.slice(),
    }));
  if (!roles.length) {
    // 原生 teams.json 形状：members → roles（persona 即提示词）
    (Array.isArray(src.members) ? src.members : []).forEach((m, i) => {
      roles.push({
        key: "m" + i,
        name: String((m && m.name) || "").trim() || `队员${i + 1}`,
        provider: String((m && m.provider) || ""),
        model: String((m && m.model) || ""),
        prompt: String((m && m.persona) || ""),
        needs: needsOf(teamNeeds) || "read",
        tools_allow: teamAllow.slice(),
        tools_deny: teamDeny.slice(),
      });
    });
  }
  const dir = src.director || {};
  const aggSrc = src.aggregator || {
    provider: dir.provider, model: dir.model, prompt: "",
  };
  return {
    name: String(src.name || ""),
    kind: "team",
    desc: String(src.desc || ""),
    roles,
    aggregator: {
      provider: String(aggSrc.provider || ""),
      model: String(aggSrc.model || ""),
      prompt: String(aggSrc.prompt || ""),
    },
    limits: {
      max_members: Number(limits.max_members || 0) || 0,
      member_timeout_s: Number(limits.member_timeout_s || 0) || 0,
      debate_rounds: Number(limits.debate_rounds || 0) || 0,
      max_findings: Number(limits.max_findings || 0) || 0,
      role_timeout_s: Number(limits.role_timeout_s || 0) || 0,
    },
    budget: {
      max_calls: Number(budget.max_calls || 0) || 0,
      max_tokens: Number(budget.max_tokens || 0) || 0,
      max_seconds: Number(budget.max_seconds || 0) || 0,
    },
    created_at: Number(src.created_at || 0) || 0,
    builtin: !!src.builtin,
    source: "team",
  };
}

/** 两册合并：协作模板册（含内置三张）+ 团队模板册；同名只留先出现的（内置优先）。 */
function collabMergeTemplateLists(collabList, teamList) {
  const out = [];
  const seen = new Set();
  for (const raw of (collabList || [])) {
    if (!raw || typeof raw !== "object") continue;
    const t = collabClone(raw);
    t.builtin = !!t.builtin;
    // 内置模板只住在代码里（协作册命名空间）：来源一律记 collab，
    // 免得预检时被当成 teams.json 里的用户团队模板而查不到
    t.source = t.builtin ? "collab" : String(t.source || "collab");
    if (!Array.isArray(t.roles)) t.roles = [];
    if (seen.has(t.name)) continue;
    seen.add(t.name);
    out.push(t);
  }
  for (const raw of (teamList || [])) {
    if (!raw || typeof raw !== "object") continue;
    const t = collabTemplateFromTeam(raw);
    if (!t.name || seen.has(t.name)) continue;
    seen.add(t.name);
    out.push(t);
  }
  return out;
}

/** 拉一次两册模板；返回 {skipped, degraded}。坏条目由后端跳过并计数。 */
async function collabRefreshTemplates() {
  let collabList = [];
  let teamList = [];
  let skipped = 0;
  let degraded = false;
  try {
    const r = (await request("collab.template_list")) || {};
    collabList = Array.isArray(r.templates) ? r.templates : [];
    teamList = Array.isArray(r.team_templates) ? r.team_templates : [];
    skipped = Number(r.skipped || 0) || 0;
  } catch (e) {
    // 老后端没有 collab.*：退回只显示团队模板册（既有方法），画廊不空着
    degraded = true;
    try {
      const r2 = (await request("team.template_list")) || {};
      teamList = Array.isArray(r2.templates) ? r2.templates : [];
    } catch (e2) { /* 两册都拿不到：保持空册，设置页会给「还没有模板」 */ }
  }
  collabTplAll = collabMergeTemplateLists(collabList, teamList);
  return { skipped, degraded };
}

function collabFindTemplate(name) {
  const key = String(name || "").trim();
  if (!key) return null;
  return collabTplAll.find((t) => t.name === key) || null;
}

function collabStatus(text, ok = true) {
  const el = document.getElementById("collab-tpl-status");
  if (!el) return;
  el.textContent = text || "";
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  if (typeof autoHideStatus === "function") autoHideStatus(el, text, ok);
}

// ---------- 设置 · 多模型协作：协作模板画廊 ----------

function collabGroupTitle(text) {
  const el = document.createElement("div");
  el.className = "collab-group-title";
  el.textContent = text;
  return el;
}

function collabOpButton(label, title, onClick, danger = false) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "btn-ghost collab-op" + (danger ? " danger" : "");
  b.textContent = label;
  b.title = title;
  b.onclick = onClick;
  return b;
}

function collabTemplateCard(t) {
  const el = document.createElement("div");
  el.className = "gallery-item collab-tpl-card";
  const kindLabel = COLLAB_KIND_LABELS[t.kind] || String(t.kind || "");
  const needs = collabTplNeeds(t);
  const roles = Array.isArray(t.roles) ? t.roles : [];
  const head = document.createElement("div");
  head.className = "gallery-item-head";
  head.innerHTML =
    `<span class="item-name" title="${escapeHtml(String(t.name || ""))}">${escapeHtml(String(t.name || ""))}</span>` +
    '<span class="gallery-item-ops">' +
    `<span class="item-badge collab-tpl-kind">${escapeHtml(kindLabel)}</span>` +
    (t.builtin ? '<span class="item-badge">内置</span>' : "") +
    "</span>";
  const meta = document.createElement("div");
  meta.className = "collab-tpl-meta";
  meta.textContent = `${roles.length} 个角色 · ${COLLAB_NEEDS_LABELS[needs] || needs}` +
    (t.source === "team" ? " · 团队模板" : "");
  const desc = document.createElement("div");
  desc.className = "gallery-desc";
  desc.textContent = String(t.desc || "").trim()
    || `${kindLabel}协作的默认分工与限额（没有额外说明）`;
  const ops = document.createElement("div");
  ops.className = "collab-tpl-ops";
  ops.appendChild(collabOpButton("复制", "以这份模板为底，另存一份自己的模板",
    () => openCollabTemplateEditor(t, { copy: true })));
  if (!t.builtin) {
    ops.appendChild(collabOpButton("编辑", "编辑这份模板",
      () => openCollabTemplateEditor(t)));
    ops.appendChild(collabOpButton("删除", "删除这份模板（不可恢复）",
      () => { collabRemoveTemplate(t.name, t.source); }, true));
  }
  el.append(head, meta, desc, ops);
  return el;
}

/** 设置 · 多模型协作顶部的画廊卡：内置三张 + 我的模板（协作册与团队册合并）。 */
async function renderCollabTemplates() {
  const grid = document.getElementById("collab-tpl-gallery");
  if (!grid) return;
  if (!collabTplAll.length) grid.innerHTML = '<div class="settings-hint">加载中…</div>';
  const meta = await collabRefreshTemplates();
  grid.innerHTML = "";
  const builtin = collabTplAll.filter((t) => t.builtin);
  const mine = collabTplAll.filter((t) => !t.builtin);
  if (!collabTplAll.length) {
    const empty = document.createElement("div");
    empty.className = "settings-hint";
    empty.textContent = "还没有协作模板。点右上角「＋ 新建模板」建一份，或在团队卡上「另存为模板」。";
    grid.appendChild(empty);
  }
  if (builtin.length) {
    grid.appendChild(collabGroupTitle("内置模板（只读；点「复制」改成自己的）"));
    builtin.forEach((t) => grid.appendChild(collabTemplateCard(t)));
  }
  if (mine.length) {
    grid.appendChild(collabGroupTitle("我的模板"));
    mine.forEach((t) => grid.appendChild(collabTemplateCard(t)));
  }
  const bits = [];
  if (meta.skipped) bits.push(`有 ${meta.skipped} 条模板无法识别，已跳过（可检查 collab_templates.json）`);
  if (meta.degraded) bits.push("协作模板册暂时读不到，当前只显示团队模板");
  collabStatus(bits.join("；"), !meta.degraded);
  // 设置页搜索索引持有卡片正文缓存：画廊重画后必须失效
  if (typeof invalidateSettingsCardIndex === "function") invalidateSettingsCardIndex();
}

// ---------- 设置 · 协作模板编辑器 ----------

function collabNewTemplateDraft() {
  return {
    name: "",
    kind: "team",
    desc: "",
    roles: [
      { key: "r0", name: "调研员", provider: "", model: "", prompt: "你负责调研：先读现有实现，列出事实与约束，不改文件。", needs: "read", tools_allow: [], tools_deny: [] },
      { key: "r1", name: "实施者", provider: "", model: "", prompt: "你负责实现：按调研结论改文件，完成后说明改了什么。", needs: "write", tools_allow: [], tools_deny: [] },
    ],
    aggregator: { provider: "", model: "", prompt: "" },
    limits: { max_members: 0, member_timeout_s: 0, debate_rounds: 0, max_findings: 0, role_timeout_s: 0 },
    budget: { max_calls: 0, max_tokens: 0, max_seconds: 0 },
    created_at: 0,
    builtin: false,
  };
}

/** 复制时的默认新名：原名 +（副本）；重名由用户自己再改。 */
function collabCopyName(name) {
  const base = String(name || "").trim() || "未命名模板";
  const suffix = "（副本）";
  return (base.length + suffix.length > 60 ? base.slice(0, 60 - suffix.length) : base) + suffix;
}

async function collabLoadProviders() {
  if (collabProvidersCache) return collabProvidersCache;
  try { collabProvidersCache = await request("config.providers"); }
  catch (e) { collabProvidersCache = null; }
  return collabProvidersCache;
}

function collabFillProviderOptions() {
  const provDl = document.getElementById("collab-provider-options");
  const modelDl = document.getElementById("collab-model-options");
  if (!provDl || !modelDl) return;
  const providers = (collabProvidersCache && collabProvidersCache.providers) || {};
  const keyed = [];
  const all = [];
  const models = new Set();
  Object.entries(providers).forEach(([name, p]) => {
    all.push(name);
    // 与协作文档同口径：只把已配置 Key 的服务列成候选（缺 Key 选了也跑不了）
    if (p && p.key_mask) keyed.push(name);
    (p && Array.isArray(p.models) ? p.models : []).forEach((m) => models.add(String(m)));
  });
  const names = keyed.length ? keyed : all;
  provDl.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
  modelDl.innerHTML = [...models].map((m) => `<option value="${escapeHtml(m)}"></option>`).join("");
}

/** 打开编辑器：template 为空 = 新建；opts.copy = 以现有模板为底另存。 */
function openCollabTemplateEditor(template, opts = {}) {
  const box = document.getElementById("collab-tpl-editor");
  if (!box) return;
  const src = template ? collabClone(template) : null;
  let draft;
  if (src && opts.copy) {
    draft = src;
    draft.name = collabCopyName(src.name);
    draft.builtin = false;
    draft.created_at = 0;
    draft.source = "";
  } else if (src) {
    draft = src;
  } else {
    draft = collabNewTemplateDraft();
  }
  collabEditorState = {
    draft,
    source: src ? String(src.source || "collab") : "collab",
    builtin: !!(src && src.builtin),
    isNew: !src,
    teamTools: { allow: [], deny: [] },
  };
  const title = document.getElementById("collab-tpl-editor-title");
  if (title) title.textContent = src ? (opts.copy ? "复制模板" : "编辑模板") : "新建模板";
  // 已有模板锁类型：跨类型会跨模板册（团队住 teams.json），改名另存即可
  collabFillEditorForm(draft, { lockKind: !!src });
  box.classList.remove("hidden");
  try { box.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (e) {}
  collabLoadProviders().then(() => {
    if (document.getElementById("collab-tpl-form")) collabFillProviderOptions();
  }).catch(() => {});
}

function collabCloseEditor() {
  const box = document.getElementById("collab-tpl-editor");
  if (box) box.classList.add("hidden");
  collabEditorState = null;
}

function collabRenderRoleRow(role, index, kind) {
  const row = document.createElement("div");
  row.className = "collab-role-row";
  row.dataset.key = String((role && role.key) || ("r" + index));
  row.innerHTML =
    '<div class="collab-role-head">' +
    `<span class="collab-role-idx">${index + 1}</span>` +
    `<input class="collab-role-name" type="text" maxlength="60" placeholder="角色名（频道与成员卡用它称呼）"` +
    ` value="${escapeHtml(String((role && role.name) || ""))}">` +
    '<select class="collab-role-needs" title="这个角色的权限档位：只读 / 可写 / 可执行（团队模板生效）">' +
    '<option value="read">只读</option><option value="write">可写</option><option value="exec">可执行</option>' +
    "</select>" +
    '<button class="btn-ghost collab-role-del" type="button" title="移除这个角色">移除</button>' +
    "</div>" +
    '<div class="collab-role-grid">' +
    '<label>服务<input class="collab-role-provider" type="text" list="collab-provider-options"' +
    ' placeholder="留空 = 自动挑选" value="' + escapeHtml(String((role && role.provider) || "")) + '"></label>' +
    '<label>模型<input class="collab-role-model" type="text" list="collab-model-options"' +
    ' placeholder="留空 = 服务默认模型" value="' + escapeHtml(String((role && role.model) || "")) + '"></label>' +
    "</div>" +
    '<label class="collab-role-prompt">提示词' +
    '<textarea rows="2" maxlength="4000" placeholder="这个角色负责什么、按什么标准交付">' +
    `${escapeHtml(String((role && role.prompt) || ""))}</textarea></label>`;
  const needsSel = row.querySelector(".collab-role-needs");
  const roleNeeds = String((role && role.needs) || "read");
  needsSel.value = COLLAB_NEEDS_LABELS[roleNeeds] ? roleNeeds : "read";
  needsSel.disabled = kind !== "team";
  row.querySelector(".collab-role-del").onclick = () => {
    row.remove();
    collabRenumberRoles();
  };
  return row;
}

function collabRenumberRoles() {
  const list = document.getElementById("collab-tpl-roles");
  if (!list) return;
  [...list.querySelectorAll(".collab-role-row")].forEach((row, i) => {
    const idx = row.querySelector(".collab-role-idx");
    if (idx) idx.textContent = String(i + 1);
  });
}

/** 类型切换时的联动：needs/工具只在团队模板里开放，限额按类型换字段。 */
function collabSyncKindUi(kind) {
  const form = document.getElementById("collab-tpl-form");
  if (!form) return;
  const hint = form.querySelector("#collab-tpl-kind-hint");
  if (hint) {
    hint.textContent = kind === "team"
      ? "团队可声明写入与命令前缀；发送前会列一次清单"
      : "圆桌 / 对抗本期仍是纯文本：角色只读、不挂工具";
  }
  form.querySelectorAll(".collab-role-needs").forEach((sel) => {
    sel.disabled = kind !== "team";
    if (kind !== "team") sel.value = "read";
  });
  const toolsBox = form.querySelector("#collab-tpl-team-tools");
  if (toolsBox) toolsBox.classList.toggle("hidden", kind !== "team");
  const limits = form.querySelector("#collab-tpl-limits");
  if (limits) {
    if (kind === "roundtable") {
      limits.innerHTML =
        '<div class="toolcfg-row"><label>成员上限</label>' +
        '<input id="collab-tpl-f-max-members" type="number" min="0" max="8" class="num-sm" value="0">' +
        '<span class="dim small">个；0 = 沿用 设置 · 圆桌 的值</span></div>' +
        '<div class="toolcfg-row"><label>单成员超时</label>' +
        '<input id="collab-tpl-f-member-timeout" type="number" min="0" class="num-sm" value="0">' +
        '<span class="dim small">秒；0 = 沿用配置</span></div>' +
        '<div class="toolcfg-row"><label>辩论修订</label>' +
        '<input id="collab-tpl-f-debate-rounds" type="number" min="0" max="2" class="num-sm" value="0">' +
        '<span class="dim small">轮；0 = 沿用配置（模板轮主席只做融合，不再出草稿）</span></div>';
    } else if (kind === "adversarial") {
      limits.innerHTML =
        '<div class="toolcfg-row"><label>单场问题上限</label>' +
        '<input id="collab-tpl-f-max-findings" type="number" min="0" max="200" class="num-sm" value="0">' +
        '<span class="dim small">条；0 = 沿用 设置 · 对抗 的值</span></div>' +
        '<div class="toolcfg-row"><label>单角色超时</label>' +
        '<input id="collab-tpl-f-role-timeout" type="number" min="0" class="num-sm" value="0">' +
        '<span class="dim small">秒；0 = 沿用配置</span></div>';
    } else {
      limits.innerHTML = '<p class="settings-hint collab-tpl-note">团队忽略这组限额：' +
        '轮次上限由 设置 · 团队的「全局轮次上限」与下面的调用上限取小。</p>';
    }
    // 回填草稿里的值（切类型/重画时保留已填内容）
    const draft = (collabEditorState && collabEditorState.draft) || {};
    const lim = draft.limits || {};
    const put = (id, v) => { const el = document.getElementById(id); if (el) el.value = String(Number(v || 0)); };
    put("collab-tpl-f-max-members", lim.max_members);
    put("collab-tpl-f-member-timeout", lim.member_timeout_s);
    put("collab-tpl-f-debate-rounds", lim.debate_rounds);
    put("collab-tpl-f-max-findings", lim.max_findings);
    put("collab-tpl-f-role-timeout", lim.role_timeout_s);
  }
}

function collabFillEditorForm(draft, opts = {}) {
  const form = document.getElementById("collab-tpl-form");
  if (!form) return;
  const d = draft || {};
  const kind = COLLAB_KIND_LABELS[d.kind] ? d.kind : "team";
  const budget = d.budget || {};
  const agg = d.aggregator || {};
  const aggLabel = kind === "team" ? "总管" : kind === "roundtable" ? "融合主席" : "终审裁判";
  const aggPlaceholder = kind === "team" ? "留空 = 你当总管" : "留空 = 当前会话主模型";
  let teamTools = (collabEditorState && collabEditorState.teamTools) || { allow: [], deny: [] };
  if (!Array.isArray(teamTools.allow) || !Array.isArray(teamTools.deny)) {
    teamTools = { allow: [], deny: [] };
  }
  if (!teamTools.allow.length && !teamTools.deny.length) {
    // 打开已有团队模板时工具写在角色上：回填成团队级字段
    const derived = collabTeamToolsFromRoles(Array.isArray(d.roles) ? d.roles : []);
    if (derived.allow.length || derived.deny.length) teamTools = derived;
  }
  form.innerHTML =
    '<div class="toolcfg-row"><label>名称</label>' +
    `<input id="collab-tpl-f-name" type="text" maxlength="60" placeholder="例如：交付小组" value="${escapeHtml(String(d.name || ""))}">` +
    '<span class="dim small">唯一键；同名保存会覆盖</span></div>' +
    '<div class="toolcfg-row"><label>说明</label>' +
    `<input id="collab-tpl-f-desc" type="text" maxlength="500" placeholder="一句话说明适合什么任务（可留空）" value="${escapeHtml(String(d.desc || ""))}">` +
    "</div>" +
    '<div class="toolcfg-row"><label>类型</label>' +
    `<select id="collab-tpl-f-kind" class="sel-md"${opts.lockKind ? " disabled" : ""}>` +
    '<option value="roundtable">圆桌（多模型并行 + 融合）</option>' +
    '<option value="team">团队（分工干活，可声明工具）</option>' +
    '<option value="adversarial">对抗（发现 → 调查 → 建议 → 裁判）</option>' +
    "</select>" +
    '<span class="dim small" id="collab-tpl-kind-hint"></span></div>' +
    '<datalist id="collab-provider-options"></datalist>' +
    '<datalist id="collab-model-options"></datalist>' +
    '<div class="collab-sec-title">角色</div>' +
    '<div id="collab-tpl-roles" class="collab-role-list"></div>' +
    '<div class="toolcfg-row"><button id="collab-tpl-add-role" class="btn-ghost" type="button">＋ 添加角色</button>' +
    '<span class="dim small">1–12 个；圆桌 / 对抗本期只读、不挂工具</span></div>' +
    '<div id="collab-tpl-team-tools" class="collab-team-tools">' +
    '<div class="toolcfg-row"><label>允许的工具</label>' +
    `<input id="collab-tpl-f-tools-allow" type="text" maxlength="2000"` +
    ' placeholder="逗号分隔：read_file,write_file,run_command:git status"' +
    ` value="${escapeHtml((Array.isArray(teamTools.allow) ? teamTools.allow : []).join(", "))}"></div>` +
    '<div class="toolcfg-row"><label>禁止的工具</label>' +
    `<input id="collab-tpl-f-tools-deny" type="text" maxlength="2000"` +
    ' placeholder="逗号分隔；写在这里的优先于允许"' +
    ` value="${escapeHtml((Array.isArray(teamTools.deny) ? teamTools.deny : []).join(", "))}"></div>` +
    '<p class="settings-hint collab-tpl-note">工具按<b>团队并集</b>生效（本期不做逐成员隔离）。' +
    "删除类、引擎主目录写入、工作目录外写入不会被模板放行；" +
    "<code>run_command</code> 只能写成 <code>run_command:命令前缀</code>（例如 <code>run_command:git status</code>）。</p>" +
    "</div>" +
    `<div class="collab-sec-title">收口（${aggLabel}）</div>` +
    '<div class="toolcfg-row"><label>服务</label>' +
    `<input id="collab-tpl-f-agg-provider" type="text" list="collab-provider-options" placeholder="${aggPlaceholder}"` +
    ` value="${escapeHtml(String(agg.provider || ""))}"></div>` +
    '<div class="toolcfg-row"><label>模型</label>' +
    '<input id="collab-tpl-f-agg-model" type="text" list="collab-model-options" placeholder="留空 = 服务默认模型"' +
    ` value="${escapeHtml(String(agg.model || ""))}"></div>` +
    '<div class="toolcfg-row"><label>提示词</label>' +
    '<textarea id="collab-tpl-f-agg-prompt" rows="2" maxlength="4000"' +
    ` placeholder="追加在收口角色的系统提示词之后（可留空）">${escapeHtml(String(agg.prompt || ""))}</textarea></div>` +
    '<div class="collab-sec-title">限额</div>' +
    '<div id="collab-tpl-limits" class="collab-limits"></div>' +
    '<div class="collab-sec-title">预算</div>' +
    '<div class="toolcfg-row"><label>调用上限</label>' +
    `<input id="collab-tpl-f-max-calls" type="number" min="0" max="500" class="num-sm" value="${Number(budget.max_calls || 0) || 0}">` +
    '<span class="dim small">次 provider 调用（含重试与格式纠错）；0 = 不限</span></div>' +
    '<div class="toolcfg-row"><label>token 上限</label>' +
    `<input id="collab-tpl-f-max-tokens" type="number" min="0" class="num-sm" value="${Number(budget.max_tokens || 0) || 0}">` +
    '<span class="dim small">0 = 不限；团队按运行级累计</span></div>' +
    '<div class="toolcfg-row"><label>时长上限</label>' +
    `<input id="collab-tpl-f-max-seconds" type="number" min="0" max="86400" class="num-sm" value="${Number(budget.max_seconds || 0) || 0}">` +
    '<span class="dim small">秒；0 = 不限</span></div>' +
    '<p class="settings-hint collab-tpl-note">模板只能<b>收窄</b>权限：它不能关闭权限确认，' +
    "也不能把删除、引擎主目录写入、工作目录外写入变成免确认。</p>";
  const kindSel = form.querySelector("#collab-tpl-f-kind");
  if (kindSel) kindSel.value = kind;
  const rolesBox = form.querySelector("#collab-tpl-roles");
  const roles = Array.isArray(d.roles) && d.roles.length ? d.roles : [{ key: "r0", name: "队员", needs: "read" }];
  roles.forEach((r, i) => rolesBox.appendChild(collabRoleRowFor(r, i, kind)));
  const addBtn = form.querySelector("#collab-tpl-add-role");
  if (addBtn) addBtn.onclick = () => {
    const list = document.getElementById("collab-tpl-roles");
    if (!list) return;
    if (list.querySelectorAll(".collab-role-row").length >= 12) {
      collabStatus("角色最多 12 个", false);
      return;
    }
    list.appendChild(collabRenderRoleRow({ key: "r" + Date.now().toString(36) }, list.children.length, collabCurrentKind()));
  };
  if (kindSel && !opts.lockKind) {
    kindSel.onchange = () => {
      const cur = collabReadEditorForm();
      const draftNext = cur || collabClone(d) || {};
      draftNext.kind = kindSel.value;
      if (collabEditorState) {
        collabEditorState.draft = draftNext;
        collabEditorState.teamTools = { allow: [], deny: [] };
      }
      collabFillEditorForm(draftNext, { lockKind: false });
    };
  }
  collabSyncKindUi(kind);
  if (collabProvidersCache) collabFillProviderOptions();
}

function collabRoleRowFor(role, index, kind) {
  return collabRenderRoleRow(role, index, kind);
}

function collabCurrentKind() {
  const sel = document.getElementById("collab-tpl-f-kind");
  return String((sel && sel.value) || (collabEditorState && collabEditorState.draft.kind) || "team");
}

/** 工具条目按中英文逗号/分号拆（不能按空格拆：run_command:git status 带空格）。 */
function collabSplitTools(text) {
  return String(text || "")
    .split(/[,，;；]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 团队工具是团队级声明：从角色上的声明反推并集（编辑器回填用）。 */
function collabTeamToolsFromRoles(roles) {
  const allow = [];
  const deny = [];
  for (const r of (roles || [])) {
    (Array.isArray(r.tools_allow) ? r.tools_allow : []).forEach((x) => {
      if (x && !allow.includes(x)) allow.push(x);
    });
    (Array.isArray(r.tools_deny) ? r.tools_deny : []).forEach((x) => {
      if (x && !deny.includes(x)) deny.push(x);
    });
  }
  return { allow, deny };
}

/** 团队工具是团队级声明：收拢到 needs 最高的第一个角色（并集口径不变）。 */
function collabAttachTeamTools(roles, allow, deny) {
  let top = "read";
  for (const r of roles) {
    if ((COLLAB_NEEDS_ORDER[r.needs] || 0) > (COLLAB_NEEDS_ORDER[top] || 0)) top = r.needs;
  }
  if (!allow.length && !deny.length) return;
  const host = roles.find((r) => r.needs === top);
  if (!host) return;
  host.tools_allow = allow.slice();
  host.tools_deny = deny.slice();
}

function collabReadEditorForm() {
  const form = document.getElementById("collab-tpl-form");
  if (!form) return null;
  const val = (id) => {
    const el = document.getElementById(id);
    return el ? String(el.value || "").trim() : "";
  };
  const num = (id) => {
    const raw = val(id);
    if (!raw) return 0;
    const v = Number(raw);
    return Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
  };
  const kind = collabCurrentKind();
  const usedKeys = new Set();
  const roles = [...form.querySelectorAll(".collab-role-row")].map((row, i) => {
    const nameEl = row.querySelector(".collab-role-name");
    const providerEl = row.querySelector(".collab-role-provider");
    const modelEl = row.querySelector(".collab-role-model");
    const promptEl = row.querySelector(".collab-role-prompt textarea");
    const needsEl = row.querySelector(".collab-role-needs");
    // key 是模板内唯一键（后端校验必需且不许重复）：手改出来的重名在这里补序号
    let key = String(row.dataset.key || ("r" + i));
    if (usedKeys.has(key)) key = key + "_" + (i + 1);
    usedKeys.add(key);
    return {
      key,
      name: String((nameEl && nameEl.value) || "").trim(),
      provider: String((providerEl && providerEl.value) || "").trim(),
      model: String((modelEl && modelEl.value) || "").trim(),
      prompt: String((promptEl && promptEl.value) || "").trim(),
      needs: kind === "team" ? String((needsEl && needsEl.value) || "read") : "read",
      tools_allow: [],
      tools_deny: [],
    };
  });
  const teamTools = {
    allow: kind === "team" ? collabSplitTools(val("collab-tpl-f-tools-allow")) : [],
    deny: kind === "team" ? collabSplitTools(val("collab-tpl-f-tools-deny")) : [],
  };
  if (kind === "team") collabAttachTeamTools(roles, teamTools.allow, teamTools.deny);
  if (collabEditorState) collabEditorState.teamTools = teamTools;
  return {
    name: val("collab-tpl-f-name"),
    kind,
    desc: val("collab-tpl-f-desc"),
    roles,
    aggregator: {
      provider: val("collab-tpl-f-agg-provider"),
      model: val("collab-tpl-f-agg-model"),
      prompt: val("collab-tpl-f-agg-prompt"),
    },
    limits: {
      max_members: num("collab-tpl-f-max-members"),
      member_timeout_s: num("collab-tpl-f-member-timeout"),
      debate_rounds: num("collab-tpl-f-debate-rounds"),
      max_findings: num("collab-tpl-f-max-findings"),
      role_timeout_s: num("collab-tpl-f-role-timeout"),
    },
    budget: {
      max_calls: num("collab-tpl-f-max-calls"),
      max_tokens: num("collab-tpl-f-max-tokens"),
      max_seconds: num("collab-tpl-f-max-seconds"),
    },
    created_at: Number((collabEditorState && collabEditorState.draft.created_at) || 0) || 0,
    builtin: false,
  };
}

/** 前端即时校验（中文原文；最终以后端校验为准，两边口径一致）。 */
function collabTemplatePayload() {
  const t = collabReadEditorForm();
  if (!t) return null;
  const fail = (msg) => { collabStatus("✗ " + msg, false); return null; };
  if (!t.name) return fail("模板名不能为空");
  if (t.name.length > 60) return fail("模板名过长（最多 60 字）");
  if (/[\/\\:]/.test(t.name)) return fail("模板名不能包含路径分隔符（/ 或 \\）或冒号");
  if (COLLAB_BUILTIN_NAMES.includes(t.name)) {
    return fail(`「${t.name}」是内置模板名，请换一个名字再保存（可点「复制」再改名）`);
  }
  if (t.roles.length < 1) return fail("至少要有一个角色");
  if (t.roles.length > 12) return fail("角色最多 12 个");
  for (const r of t.roles) {
    if (!r.name) return fail("每个角色都要有名字");
    if (r.prompt.length > 4000) return fail(`角色「${r.name}」的提示词超过 4000 字`);
  }
  if (t.kind === "team") {
    const tools = (collabEditorState && collabEditorState.teamTools) || { allow: [], deny: [] };
    let top = "read";
    for (const r of t.roles) {
      if ((COLLAB_NEEDS_ORDER[r.needs] || 0) > (COLLAB_NEEDS_ORDER[top] || 0)) top = r.needs;
    }
    if (top === "read" && (tools.allow.length || tools.deny.length)) {
      return fail("团队最高 needs 是只读，不能声明工具：先把某个角色的 needs 改成「可写」或「可执行」");
    }
    // 只对「允许」拦裸 run_command：deny 里写 run_command 是合法语义（整个工具禁用），
    // 后端 _normalize_tool_list 对 deny 也允许裸名
    const bare = tools.allow.find((x) => x === "run_command");
    if (bare) {
      return fail("允许的工具里 run_command 必须写成 run_command:命令前缀（例如 run_command:git status），不能整个工具放行");
    }
  }
  return t;
}

async function collabSaveTemplate() {
  const t = collabTemplatePayload();
  if (!t) return;
  const btn = document.getElementById("collab-tpl-save");
  if (btn) btn.disabled = true;
  try {
    const r = (await request("collab.template_save", { template: t })) || {};
    let saved = r.template && typeof r.template === "object" ? r.template : t;
    // 团队模板可能回 teams.json 形状（members/persona）：统一转回协作模板形状再回填
    if (saved && !Array.isArray(saved.roles)) saved = collabTemplateFromTeam(saved);
    const warns = Array.isArray(r.warnings) ? r.warnings : [];
    if (warns.length) collabStatus("保存时有提醒：" + warns.join("；"), false);
    await renderCollabTemplates();
    if (collabEditorState) {
      collabEditorState.draft = collabClone(saved);
      collabEditorState.source = String(r.source || collabEditorState.source || "");
      collabEditorState.builtin = false;
      collabEditorState.isNew = false;
      collabEditorState.teamTools = { allow: [], deny: [] };
    }
    const title = document.getElementById("collab-tpl-editor-title");
    if (title) title.textContent = "编辑模板";
    collabFillEditorForm(saved, { lockKind: true });
  } catch (e) {
    collabStatus("✗ 保存失败：" + e.message, false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function collabRemoveTemplate(name, source) {
  const label = String(name || "");
  if (!label) return;
  const ok = await confirmModal(`删除协作模板「${label}」？删除后不可恢复。`, {
    title: "删除协作模板", okLabel: "删除", danger: true,
  });
  if (!ok) return;
  try {
    await request("collab.template_remove", source ? { name: label, source } : { name: label });
    collabStatus();
    if (collabEditorState && collabEditorState.draft && collabEditorState.draft.name === label) {
      collabCloseEditor();
    }
    await renderCollabTemplates();
  } catch (e) {
    collabStatus("✗ 删除失败：" + e.message, false);
  }
}

// ---------- 协作菜单：内置模板 / 我的模板 分组 ----------

/**
 * 协作菜单的模板分组。closeMenu 是 renderCoopMenuItems 内的局部函数，必须由调用方
 * 传进来（app-collab.js 拿不到那个闭包）。首次打开菜单时缓存可能还没到：拉一次，
 * 菜单仍开着就原地重画。
 */
function collabTemplateSection(menu, closeMenu) {
  const frag = document.createDocumentFragment();
  if (!collabTplAll.length) {
    collabRefreshTemplates().then(() => {
      if (!collabTplAll.length) return;
      if (!menu || menu.classList.contains("hidden")) return;
      if (typeof renderCoopMenuItems !== "function") return;
      if (typeof coopRenderGuard !== "undefined") coopRenderGuard = true;
      renderCoopMenuItems(menu);
      if (typeof positionRtMenu === "function") positionRtMenu(menu, "coop-switch");
      setTimeout(() => {
        if (typeof coopRenderGuard !== "undefined") coopRenderGuard = false;
      }, 0);
    }).catch(() => {});
    return frag;
  }
  const activeName = activeTab ? String(activeTab.template || "") : "";
  const groups = [
    ["内置模板", collabTplAll.filter((t) => t.builtin)],
    ["我的模板", collabTplAll.filter((t) => !t.builtin)],
  ];
  for (const [title, items] of groups) {
    if (!items.length) continue;
    const head = document.createElement("div");
    head.className = "rt-menu-tip collab-menu-group";
    head.textContent = title;
    frag.appendChild(head);
    for (const t of items) {
      const active = activeName === t.name;
      const kind = COLLAB_KIND_LABELS[t.kind] || String(t.kind || "");
      const needs = collabTplNeeds(t);
      const b = document.createElement("button");
      b.className = "coop-mode-item collab-menu-item" + (active ? " active" : "");
      b.innerHTML = `<span class="coop-mode-check">${active ? "✓" : ""}</span>` +
        '<span class="coop-mode-body">' +
        `<span class="coop-mode-label">${escapeHtml(String(t.name || ""))}</span>` +
        `<span class="coop-mode-desc">${escapeHtml(kind + " · " + (COLLAB_NEEDS_LABELS[needs] || needs) +
          (t.desc ? " · " + t.desc : ""))}</span>` +
        "</span>";
      b.title = `使用模板「${t.name}」：本条消息按这份模板执行` +
        (t.kind === "team" && needs !== "read" ? "（发送前列一次工具与写入清单）" : "");
      b.onclick = () => {
        collabApplyTemplate(t);
        if (typeof closeMenu === "function") closeMenu();
      };
      frag.appendChild(b);
    }
  }
  return frag;
}

/** 选中模板：先把协作模式切到模板对应的 kind，再写 pane.template
    （setRtOn/setTeamOn/setAdvOn 会清空 template，必须先 setter 后赋值）。 */
function collabApplyTemplate(t) {
  if (!t || !t.name) return;
  if (t.kind === "team") setTeamOn(true);
  else if (t.kind === "adversarial") setAdvOn(true);
  else setRtOn(true);
  if (activeTab) activeTab.template = String(t.name);
  updateCoopHint();
  const kind = COLLAB_KIND_LABELS[t.kind] || String(t.kind || "");
  const needs = collabTplNeeds(t);
  addNotice(`已选用模板「${t.name}」（${kind}${t.kind === "team" ? " · " + (COLLAB_NEEDS_LABELS[needs] || needs) : ""}）` +
    (t.kind === "team" && needs !== "read" ? "；发送前会列一次工具与写入清单" : ""));
}

/** 手动切模式或发送后清掉当前模板（模板是一次性的，与协作开关同生命周期）。 */
function collabClearTemplate() {
  if (activeTab) activeTab.template = "";
  updateCoopHint();
}

// ---------- 跑前清单（团队模板 needs != read 时弹一次） ----------

function collabToolLabel(t) {
  const name = String((t && t.name) || "");
  const prefix = String((t && t.command_prefix) || "");
  return prefix ? `${name}:${prefix}` : name;
}

function collabCostText(pv) {
  const reason = String((pv && pv.cost_reason) || "");
  const raw = pv ? pv.estimated_cost : null;
  // 先按 cost_reason 归因：no_price 时后端同样回 estimated_cost=null，
  // 若先判 null 会把「没配价目」误报成「角色未指定服务」
  if (reason === "no_price") {
    return "config.toml [usage_pricing] 没配价目，无法估算费用";
  }
  if (reason === "unbounded") {
    return "模板没设调用上限，调用次数未知，无法估算费用";
  }
  if (reason === "unknown_provider" || raw === null || raw === undefined) {
    return "模板里还有角色未指定模型服务，无法按 config.toml [usage_pricing] 估价";
  }
  const cost = Number(raw);
  if (!Number.isFinite(cost) || cost <= 0) return "费用算不出来（价目缺失）";
  return `约 ¥${cost.toFixed(4)}`;
}

function collabPreviewHtml(pv) {
  const p = pv || {};
  const kind = COLLAB_KIND_LABELS[p.kind] || String(p.kind || "");
  const needs = COLLAB_NEEDS_LABELS[p.needs] || String(p.needs || "");
  const roles = Array.isArray(p.roles) ? p.roles : [];
  const tools = Array.isArray(p.tools) ? p.tools : [];
  const prefixes = Array.isArray(p.command_prefixes) ? p.command_prefixes : [];
  const always = Array.isArray(p.always_confirm) ? p.always_confirm : [];
  const dropped = Array.isArray(p.dropped_roles) ? p.dropped_roles : [];
  const roleRows = roles.map((r) => {
    const who = r.provider ? `${r.provider}/${r.model || "默认模型"}` : "自动挑选";
    const state = r.build_error ? `未就绪：${r.build_error}` : (r.resolved === false ? "未就绪" : "就绪");
    return `<tr><td>${escapeHtml(String(r.name || "未命名"))}</td>` +
      `<td>${escapeHtml(who)}</td>` +
      `<td>${escapeHtml(COLLAB_NEEDS_LABELS[r.needs] || String(r.needs || ""))}</td>` +
      `<td>${escapeHtml(state)}</td></tr>`;
  }).join("");
  const toolRows = tools.map((t) => {
    const mode = t.mode === "auto" ? "本次免确认" : "逐次确认";
    const note = String(t.note || "") || (t.known === false ? "当前工具表里没有这个工具，运行时会忽略" : "");
    return `<tr><td><code>${escapeHtml(collabToolLabel(t))}</code></td>` +
      // 模板声明免确认的工具行打 pv-auto 标记：实际是否免确认还取决于下方
      // 「本次运行中不再逐次确认」复选框，collabPreviewModal 里随勾选联动改写
      `<td class="pv-mode${t.mode === "auto" ? " pv-auto" : ""}">${escapeHtml(mode)}</td>` +
      `<td>${escapeHtml(note)}</td></tr>`;
  }).join("");
  const calls = Number(p.max_calls || 0);
  const secs = Number(p.max_seconds || 0);
  const tokens = Number(p.max_tokens || 0);
  const estCalls = Number(p.estimated_calls || 0);
  const eff = Number(p.effective_member_count || 0);
  const declared = Number(p.roles_declared || roles.length);
  const desc = String(p.desc || "").trim();
  return '<div class="collab-preview">' +
    `<p class="settings-hint">${escapeHtml(kind)} · ${escapeHtml(needs)}` +
    (desc ? ` · ${escapeHtml(desc)}` : "") + "</p>" +
    `<div class="collab-sec-title">角色（声明 ${declared} 个，实际入队 ${eff} 名）</div>` +
    (roleRows
      ? `<table class="collab-preview-table"><thead><tr><th>角色</th><th>服务 / 模型</th><th>权限</th><th>状态</th></tr></thead><tbody>${roleRows}</tbody></table>`
      : '<p class="dim small">这份模板没有声明角色。</p>') +
    (dropped.length
      ? `<p class="dim small">被合并掉的角色：${escapeHtml(dropped.join("、"))}（已配置 Key 的服务不足或模型重复）</p>`
      : "") +
    '<div class="collab-sec-title">工具与写入</div>' +
    '<p class="dim small">写入范围：仅当前工作目录内。删除、引擎主目录写入、工作目录外写入一律回退逐次确认。</p>' +
    (prefixes.length
      ? `<p class="dim small">命令前缀：${prefixes.map((x) => `<code>${escapeHtml(String(x))}</code>`).join("、")}</p>`
      : '<p class="dim small">命令前缀：无（本次不授权运行任何命令）</p>') +
    (toolRows
      ? `<table class="collab-preview-table"><thead><tr><th>工具</th><th>本次</th><th>说明</th></tr></thead><tbody>${toolRows}</tbody></table>`
      : '<p class="dim small">工具：无（本次不挂任何工具）。</p>') +
    (always.length
      ? `<p class="dim small">始终逐次确认：${always.map((x) => `<code>${escapeHtml(String(x))}</code>`).join("、")}</p>`
      : "") +
    (p.role_scoped === false
      ? `<p class="dim small">${escapeHtml(String(p.role_scope_note || "工具按团队声明的并集生效；本期不做逐成员隔离。"))}</p>`
      : "") +
    '<div class="collab-sec-title">预计</div>' +
    `<p class="dim small">${calls ? `最多 ${calls} 次调用` : "调用次数不限"} · ` +
    `${secs ? `${secs} 秒` : "时长不限"}` +
    (tokens ? ` · 最多 ${tokens} tokens` : "") +
    // 团队的 estimated_calls 等于 max_calls（上限本身），再写一遍是重复
    (estCalls && estCalls !== calls ? ` · 预计 ${estCalls} 次调用` : "") +
    ` · ${escapeHtml(collabCostText(p))}</p>` +
    '<p class="dim small">只免模板声明过、且落在工作目录内的写入与命令前缀；删除、DANGEROUS 工具、' +
    '引擎主目录与工作目录外写入仍会逐次征询。</p>' +
    // 勾选项与只读按钮留在滚动区外：角色表 + 工具表可滚，但「本次不再逐次
    // 确认」和「先只读跑一遍」始终可见（冒烟：625px 高窗口里它们被压在
    // .collab-preview 的滚动区底部，用户只看到取消/开始）。说明文字留在
    // 滚动区内，避免把固定区撑高后又把按钮挤出视口。
    "</div>" +
    '<label class="collab-noconfirm"><input type="checkbox" id="collab-pv-noconfirm">' +
    "<span>本次运行中不再逐次确认</span></label>" +
    '<div class="collab-preview-actions">' +
    '<button class="btn-ghost" data-act="readonly" type="button">先只读跑一遍</button>' +
    "</div>";
}

/**
 * 跑前清单弹窗：开始 / 先只读跑一遍 / 取消。
 * 取消必须 resolve（showModal 的 #modal-cancel 默认只 hideModal），所以照
 * confirmModal 的做法接管 cancelBtn.onclick，并在 finish 里恢复。
 */
function collabPreviewModal(preview) {
  return new Promise((resolve) => {
    const pv = preview || {};
    const box = document.createElement("div");
    box.className = "collab-preview-wrap";
    box.innerHTML = collabPreviewHtml(pv);
    const modalEl = document.getElementById("modal");
    const cancelBtn = document.getElementById("modal-cancel");
    const prevCancel = cancelBtn.onclick;
    const noConfirmEl = box.querySelector("#collab-pv-noconfirm");
    const readOnlyBtn = box.querySelector('[data-act="readonly"]');
    let settled = false;
    // 工具表「本次」列与复选框联动：未勾选免确认时，模板声明 auto 的工具
    // 实际仍逐次确认——标签照实改写，不再照抄模板声明误导（走查 collab-5）
    const syncModeCells = () => {
      const on = !!(noConfirmEl && noConfirmEl.checked);
      box.querySelectorAll(".pv-mode.pv-auto").forEach((td) => {
        td.textContent = on ? "本次免确认" : "逐次确认（未勾选免确认）";
      });
    };
    if (noConfirmEl) noConfirmEl.addEventListener("change", syncModeCells);
    syncModeCells();
    const grantRun = () => ({ scope: noConfirmEl && noConfirmEl.checked ? "run" : "ask_each" });
    const finish = (v) => {
      if (settled) return;
      settled = true;
      cancelBtn.onclick = prevCancel;
      obs.disconnect();
      hideModal();
      resolve(v);
    };
    const obs = new MutationObserver(() => {
      if (modalEl.classList.contains("hidden")) {
        if (!settled) {
          settled = true;
          cancelBtn.onclick = prevCancel;
          obs.disconnect();
          resolve(null);
        }
      }
    });
    obs.observe(modalEl, { attributes: true, attributeFilter: ["class"] });
    cancelBtn.onclick = () => finish(null);
    if (readOnlyBtn) {
      readOnlyBtn.onclick = () => finish({ action: "readonly", grant: { scope: "ask_each", read_only: true } });
    }
    showModal(`运行前确认：${String(pv.name || "协作模板")}`, box,
      () => finish({ action: "run", grant: grantRun() }), "开始");
  });
}

/** 调 collab.preview 拿清单；失败给中文提示并返回 null（绝不静默放宽）。 */
async function collabPreflight(name, source) {
  try {
    const pv = await request("collab.preview", source ? { name, source } : { name });
    if (!pv || pv.ok === false) {
      addNotice("模板预检失败：" + ((pv && (pv.error || pv.message)) || "后端没有返回清单"));
      return null;
    }
    return pv;
  } catch (e) {
    addNotice("模板预检失败：" + e.message);
    return null;
  }
}

/**
 * 发送前置：返回 {template, grant} 或 null（取消发送）。
 * 团队模板 needs != read 时先弹清单；圆桌 / 对抗与只读团队模板不弹（它们不申请写权限）。
 */
async function collabSendWithGrant(tab) {
  const name = String((tab && tab.template) || "").trim();
  if (!name) return { template: "", grant: null };
  let tpl = collabFindTemplate(name);
  if (!tpl) {
    await collabRefreshTemplates().catch(() => {});
    tpl = collabFindTemplate(name);
  }
  if (!tpl) {
    if (tab) tab.template = "";
    updateCoopHint();
    addNotice(`找不到协作模板「${name}」，本条消息按普通对话发送`);
    return { template: "", grant: null };
  }
  const needs = collabTplNeeds(tpl);
  if (tpl.kind !== "team" || needs === "read") {
    return { template: name, grant: null };
  }
  // 只有团队册的条目需要指明来源（协作册是默认解析面；内置名也只认协作册）
  const pv = await collabPreflight(name, tpl.source === "team" ? "team" : "");
  if (!pv) return null;   // 预检失败：取消本次发送，不带着未知权限直接跑
  const pick = await collabPreviewModal(pv);
  if (!pick) return null;
  return { template: name, grant: pick.grant };
}

// 兼容旧名（契约里的写法）：同一个入口
function collabBeforeSend(tab) {
  return collabSendWithGrant(tab);
}

// ---------- 静态按钮接线（设置页画廊卡的三个按钮） ----------

function collabWireStaticButtons() {
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  on("collab-tpl-new", () => openCollabTemplateEditor(null));
  on("collab-tpl-close", () => collabCloseEditor());
  on("collab-tpl-save", () => { collabSaveTemplate(); });
}

collabWireStaticButtons();
// 首屏先拉一次模板册：协作菜单打开时直接有内容，不用等一次往返
collabRefreshTemplates().catch(() => {});
