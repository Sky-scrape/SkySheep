// app-tools.js —— 工具配置区块（联网搜索 / AI 画图 / 语音输入 / 圆桌）
// 从 app.js 拆出的第二份手写前端：同为零构建、普通 script、不引入 ES modules。
// index.html 先载 app.js 再载本文件；两文件靠全局函数互调，都是运行时调用
// （用户打开设置页时两份脚本早已加载完毕），加载顺序安全。
// 本文件调用 app.js 侧的 request / addNotice / escapeHtml / autoHideStatus，
// 并读写 app.js 顶层的 rtDebate / rtChair（let 全局词法绑定，经典脚本间共享）。
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

// 三个「服务商型」配置表单（联网搜索 / AI 画图 / 语音输入）共用同一套骨架：
// 取当前配置 → 画「服务商三档 + 自定义明细 + 状态行」→ 接三档切换 / 已配置
// 点选 / 保存。差异只有字段清单与文案，全部经 cfg 传入（render*Cfg 各自给）：
//   formId / hintId / getMethod / saveMethod —— 表单、提示条与 WS 方法
//   labels      —— 服务商三档文案（原样传给 providerPicker）
//   detailHtml  —— 自定义档的明细行 HTML（仅 custom 模式调用）
//   statusHtml  —— 状态行（.toolcfg-state 整段）
//   fields      —— 保存时按 data-f 收集的文本字段
//   pickParams  —— 「已配置」点选服务时附带的参数
//   pickedNotice / saved / saveFailed —— 点选与保存成功/失败的反馈（通道与文案不同）
async function renderProviderCfg(cfg) {
  let d;
  try { d = await request(cfg.getMethod); } catch (e) { return; }
  document.getElementById(cfg.hintId).textContent = d.config_hint;
  const form = document.getElementById(cfg.formId);
  // 当前档位：服务商是服务名 → 「已配置」；custom → 自定义；其余（含 auto）→ 自动
  const mode = providerModeOf(cfg.formId, d);
  const detail = mode === "custom" ? cfg.detailHtml(d) : "";
  form.innerHTML = `
    <div class="toolcfg-row"><label>服务商</label>${providerPicker(d, cfg.labels, mode)}</div>
    ${detail}
    <div class="toolcfg-row">
      ${cfg.statusHtml(d)}
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelectorAll(".provider-seg button").forEach((btn) => {
    btn.onclick = () => saveSwitch(cfg.formId, cfg.saveMethod, btn.dataset.mode);
  });
  if (mode === "configured") {
    pickConfiguredService(form, cfg.saveMethod, () => cfg.pickParams(form), () => {
      addNotice(cfg.pickedNotice);
      cfg.rerender();
    });
  }
  const saveBtn = form.querySelector('[data-act="save"]');
  if (saveBtn) saveBtn.onclick = async () => {
    // provider 回传 get 时的当前值：表单里没有可编辑的 provider 控件，
    // 档位切换由分段按钮即时保存，这里只负责保存自定义明细字段
    // （以前读不存在的 [data-f="provider"] 元素，点保存会抛 TypeError——
    // 后端收到空 provider 会把档位重置回 auto，所以必须回传当前值）
    const params = { provider: d.provider || "" };
    for (const f of cfg.fields) {
      const el = form.querySelector(`[data-f="${f}"]`);
      if (el) params[f] = el.value;
    }
    const keyEl = form.querySelector('[data-f="key"]');
    const key = keyEl ? keyEl.value.trim() : "";
    if (key) params.api_key = key;
    try {
      await request(cfg.saveMethod, params);
      cfg.saved();
      cfg.rerender();
    } catch (e) {
      cfg.saveFailed(e);
    }
  };
}

async function renderWebsearchCfg() {
  const labels = { auto: "自动（优先复用已配置的 Key）", custom: "自定义（自建搜索服务）" };
  return renderProviderCfg({
    formId: "websearch-form", hintId: "websearch-hint",
    getMethod: "websearch.get", saveMethod: "websearch.save",
    labels,
    detailHtml: (d) => {
      const keyHint = d.key_mask
        ? "已配置（" + d.key_mask + "），留空不修改"
        : "可留空（自建 SearXNG 等无需鉴权）";
      return `
    <div class="toolcfg-row"><label>API Key</label>
      <input type="password" data-f="key" autocomplete="new-password" placeholder="${keyHint}">
    </div>
    <div class="toolcfg-row"><label>接口地址</label>
      <input type="text" data-f="base_url" value="${escapeHtml(d.base_url || "")}"
             placeholder="自建搜索服务地址（如 http://localhost:8080 或 .../search?format=json）">
    </div>`;
    },
    statusHtml: (d) => `
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + (labels[d.resolved_provider] || d.resolved_provider) + "」"
        : "○ 未配置：Agent 联网搜索时会给出配置指引"}</span>`,
    fields: ["base_url"],
    pickParams: () => ({}),
    pickedNotice: "已选用该服务作搜索服务商",
    saved: () => addNotice("联网搜索配置已保存并生效"),
    saveFailed: (e) => addNotice("保存失败: " + e.message),
    rerender: renderWebsearchCfg,
  });
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
  const labels = { auto: "自动（优先复用已配置的 Key）", custom: "自定义 OpenAI 兼容" };
  return renderProviderCfg({
    formId: "imagegen-form", hintId: "imagegen-hint",
    getMethod: "imagegen.get", saveMethod: "imagegen.save",
    labels,
    detailHtml: (d) => `
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
    </div>`,
    statusHtml: (d) => `
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + (labels[d.resolved_provider] || d.resolved_provider) + " / " + d.resolved_model + "」"
        : "○ 未配置：配好任一服务的 Key 即可零配置使用"}</span>`,
    fields: ["base_url", "model"],
    pickParams: (form) => {
      const m = form.querySelector('[data-f="model"]');
      return m && m.value.trim() ? { model: m.value.trim() } : {};
    },
    pickedNotice: "已选用该服务作画图服务商",
    saved: () => addNotice("AI 画图配置已保存并生效"),
    saveFailed: (e) => addNotice("保存失败: " + e.message),
    rerender: renderImagegenCfg,
  });
}

async function renderSpeechCfg() {
  const labels = { auto: "自动（优先复用已配置的 Key）", custom: "自定义 OpenAI 兼容" };
  return renderProviderCfg({
    formId: "speech-form", hintId: "speech-hint",
    getMethod: "speech.get", saveMethod: "speech.save",
    labels,
    detailHtml: (d) => `
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
    </div>`,
    statusHtml: (d) => `
      <span class="toolcfg-state ${d.has_key ? "ok" : ""}">${d.has_key
        ? "● 已就绪，当前用「" + (labels[d.resolved_provider] || d.resolved_provider) + " / " + d.resolved_model + "」"
        : "○ 未配置：麦克风按钮会提示先来这里配置"}</span>`,
    fields: ["base_url", "model", "language"],
    pickParams: (form) => {
      const m = form.querySelector('[data-f="model"]');
      return m && m.value.trim() ? { model: m.value.trim() } : {};
    },
    pickedNotice: "已选用该服务作语音转写服务商",
    saved: () => speechStatus("✓ 已保存，麦克风按钮立即可用", true),
    saveFailed: (e) => speechStatus("✗ 保存失败：" + e.message, false),
    rerender: renderSpeechCfg,
  });
}

function speechStatus(text, ok = true) {
  const el = document.getElementById("speech-status");
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
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

// ---------- 对抗设置：单场问题上限 / 单角色超时 ----------
async function renderAdversarialCfg() {
  let d;
  try { d = await request("adversarial.get"); } catch (e) { return; }
  document.getElementById("adversarial-hint").textContent = d.config_hint;
  const form = document.getElementById("adversarial-form");
  if (!form) return;
  form.innerHTML = `
    <div class="toolcfg-row"><label>单场问题上限</label>
      <input type="number" data-f="max_findings" min="1" max="100" class="num-sm" value="${d.max_findings}">
      <span class="dim small">条（发现者超出按序截断并提示）</span>
    </div>
    <div class="toolcfg-row"><label>单角色超时</label>
      <input type="number" data-f="role_timeout_s" min="10" class="num-sm" value="${d.role_timeout_s}">
      <span class="dim small">秒（裁判阶段自动加倍；单角色失败不拖垮其余阶段）</span>
    </div>
    <div class="toolcfg-row">
      <span class="toolcfg-state ${d.configured_services ? "ok" : ""}">${d.configured_services
        ? `● ${d.configured_services} 个已配置 Key 的服务可作角色`
        : "○ 还没有已配置 Key 的服务：对抗角色来自「模型服务」页配好的服务"}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelector('[data-act="save"]').onclick = async () => {
    const params = {
      max_findings: Number(form.querySelector('[data-f="max_findings"]').value),
      role_timeout_s: Number(form.querySelector('[data-f="role_timeout_s"]').value),
    };
    try {
      await request("adversarial.save", params);
      addNotice("对抗设置已保存并生效");
      renderAdversarialCfg();
    } catch (e) {
      addNotice("保存失败: " + e.message);
    }
  };
}

// ---------- 设置 · 团队：五道防失控上限（与圆桌卡同款：读回当前值 → 编辑 → 保存热生效） ----------
// 热生效语义（后端契约）：改动只对之后新建的团队生效，进行中团队沿用建队时的值；
// 数值越界由后端 clamp（保存后回传的是 clamp 过的新状态，直接照它回显）
async function renderTeamCfg() {
  let d;
  try { d = await request("teamcfg.get"); } catch (e) { return; }
  const hint = document.getElementById("team-hint");
  const form = document.getElementById("team-form");
  if (!form) return;
  if (hint) hint.textContent = d.config_hint || "";
  form.innerHTML = `
    <div class="toolcfg-row"><label>队员上限</label>
      <input type="number" data-f="max_members" min="1" max="8" class="num-sm" value="${d.max_members}">
      <span class="dim small">个（1-8；自动挑选队员时也不会超出）</span>
    </div>
    <div class="toolcfg-row"><label>单成员超时</label>
      <input type="number" data-f="member_timeout_s" min="10" class="num-sm" value="${d.member_timeout_s}">
      <span class="dim small">秒（单个队员发言/干活超过此时长按失败收尾，不拖垮整队）</span>
    </div>
    <div class="toolcfg-row"><label>全局轮次上限</label>
      <input type="number" data-f="max_rounds" min="1" class="num-sm" value="${d.max_rounds}">
      <span class="dim small">轮（总管轮/成员轮/小会合计，防自动循环失控；到达即强制交付）</span>
    </div>
    <div class="toolcfg-row"><label>打回上限</label>
      <input type="number" data-f="redo_limit" min="0" max="5" class="num-sm" value="${d.redo_limit}">
      <span class="dim small">次（同一工单打回超过此数，总管必须强制裁定：改派/降级顾问/砍掉）</span>
    </div>
    <div class="toolcfg-row"><label>停滞唤醒</label>
      <input type="number" data-f="stall_limit" min="1" class="num-sm" value="${d.stall_limit}">
      <span class="dim small">轮（成员连续空转达到此数，自动停止并移交总管处置）</span>
    </div>
    <div class="toolcfg-row">
      <span class="toolcfg-state">● 保存后对<b>之后新建的团队</b>生效，进行中的团队沿用建队时的值</span>
      <span class="spacer"></span>
      <button class="btn-ghost" data-act="save">保存</button>
    </div>`;
  form.querySelector('[data-act="save"]').onclick = async () => {
    // 清空/非数字的字段传 null = 该键不改（后端只处理非 None 的键；
    // 注意 Number("") 是 0，必须先拦空串，否则清空的字段会被存成 clamp 后的最小值）
    const num = (f) => {
      const raw = String(form.querySelector(`[data-f="${f}"]`).value).trim();
      if (!raw) return null;
      const v = Number(raw);
      return Number.isFinite(v) ? v : null;
    };
    try {
      const r = await request("teamcfg.save", {
        max_members: num("max_members"),
        member_timeout_s: num("member_timeout_s"),
        max_rounds: num("max_rounds"),
        redo_limit: num("redo_limit"),
        stall_limit: num("stall_limit"),
      });
      teamCfgStatus("✓ 已保存并生效：队员上限 " + r.max_members + " · 单成员超时 " +
        r.member_timeout_s + " 秒 · 轮次上限 " + r.max_rounds + " · 打回上限 " +
        r.redo_limit + " · 停滞唤醒 " + r.stall_limit +
        "（对之后新建的团队生效）", true);
      renderTeamCfg(); // 回显 clamp 后的值：界面与 config.toml 保持一致
    } catch (e) {
      teamCfgStatus("✗ 保存失败：" + e.message, false);
    }
  };
}

function teamCfgStatus(text, ok) {
  const el = document.getElementById("team-status");
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
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
  autoHideStatus(el, text, ok);
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
