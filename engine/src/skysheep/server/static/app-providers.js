// app-providers.js —— 设置 · 模型服务（列表视图 / 详情视图 / 探测与编辑弹窗）
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有声明与纯数据常量，零加载期执行，btn-provider-back 接线仍留
// 在 app.js 原位（加载期解引用 closeProviderDetail，故 index.html 先载
// 本文件再载 app.js）；request / renderSettings 等助手是运行时调用。

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
