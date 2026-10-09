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
