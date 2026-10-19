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
let availFilter = "";            // 可用模型列表的搜索词（按模型名模糊匹配；换服务时清空）
let detailOpen = false;   // 是否正停在某个服务的配置页
let detailName = "";
let providerTab = "preset"; // 列表页签：preset=默认（内置预设）、custom=自定义（用户新增）
let providerHealthCache = {};  // 服务名 → 健康状态（model.providers_health 拉取）

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

function providerHealthDot(name) {
  // 健康状态灯：最近一次测试/运行错误的结果（内存态，重启清零）
  const h = (providerHealthCache[name] || {}).level;
  if (!h || h === "unknown") return "";
  const meta = { ok: ["●", "ph-ok"], warn: ["▲", "ph-warn"], down: ["●", "ph-down"] }[h];
  if (!meta) return "";
  return `<span class="ph-dot ${meta[1]}" title="健康状态：${h}">${meta[0]}</span>`;
}

function providerRow([name, p]) {
  const row = document.createElement("div");
  row.className = "svc-row" + (p.is_active ? " cur" : "");
  row.dataset.name = name;
  const keyState = p.has_key ? "Key 已配置" + (p.key_from_env ? "（环境变量）" : "") : "未配置 Key";
  const badges =
    (p.is_active ? '<span class="chip chip-blue">使用中</span>' : "") +
    (p.is_default ? '<span class="chip">★ 默认</span>' : "") +
    providerHealthDot(name);
  row.innerHTML = `
    ${providerAvatar(name)}
    <span class="svc-main">
      <span class="svc-name">${escapeHtml(providerLabel(name, p))}<span class="svc-key${p.has_key ? "" : " svc-warn"}">· ${escapeHtml(keyState)}</span>${badges}</span>
      <span class="svc-sub">${escapeHtml(p.kind)} · ${escapeHtml(p.model || "未设置模型")}</span>
    </span>
    <button class="svc-del" type="button" title="${p.is_preset ? "停用该服务（可在下方「已停用的服务」里恢复）" : "删除该服务（配置从 config.toml 移除）"}">✕</button>
    <button class="svc-go" type="button" title="进入配置">›</button>`;
  // 整行可点：进该服务的配置页
  row.onclick = () => openProviderDetail(name);
  // 行上直接给删除入口：此前只有详情页底部有，列表页找不到删服务的门
  row.querySelector(".svc-del").onclick = (e) => {
    e.stopPropagation(); // 别触发整行的进详情
    deleteProviderModal(name, p.is_preset);
  };
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
  availFilter = "";            // 换服务时清空搜索：不同服务的模型清单没有继承关系
  document.getElementById("provider-list-view").hidden = true;
  document.getElementById("provider-detail").hidden = false;
  document.getElementById("btn-add-provider").hidden = true;
  document.getElementById("provider-card-title").textContent = "模型服务";
  renderProviderDetail(name);
}

// 返回列表：有未保存改动时先确认。异步确认走不了同步函数签名，
// 所以这里返回 Promise，调用方（按钮 onclick）不关心返回值。
async function closeProviderDetail() {
  if (detailOpen) {
    const box = document.getElementById("provider-detail-body");
    if (box && detailSnapshot && detailFormFingerprint(box) !== detailSnapshot) {
      const ok = await confirmModal(
        "当前服务有没保存的改动，返回列表会丢弃它们。确定返回？",
        { title: "有未保存的改动", okLabel: "丢弃并返回", danger: true }
      );
      if (!ok) return;
    }
  }
  detailSnapshot = "";
  resetProviderView();
  renderProviderList();
}

// 只切视图、不重新拉数据（删除后由 renderSettings 统一刷新）
function resetProviderView() {
  detailOpen = false;
  detailName = "";
  detailSnapshot = ""; // 视图已重置，脏检测基线一并清掉
  document.getElementById("provider-detail").hidden = true;
  document.getElementById("provider-list-view").hidden = false;
  document.getElementById("btn-add-provider").hidden = false;
  const ds = document.getElementById("provider-detail-status");
  ds.hidden = true;
  ds.textContent = "";
}

// 详情表单的「初始值快照」：用来判断有没有未保存的改动（离开时提醒）
let detailSnapshot = "";

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
        <label>服务类型</label>
        <select data-f="kind">
          <option value="openai">openai（OpenAI 兼容协议）</option>
          <option value="anthropic">anthropic（Anthropic 原生协议）</option>
        </select>
      </div>
      <div class="field">
        <label>服务名称</label>
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
        <div class="label-row">
          <label>温度 temperature（可选）</label>
          <span class="temp-state" id="temp-state"></span>
        </div>
        <input data-f="temperature" type="number" min="0" max="2" step="0.1"
               value="${p.temperature === null || p.temperature === undefined ? "" : p.temperature}"
               placeholder="留空 = 用服务默认"
               title="越低越稳定保守，越高越发散有创意；多数服务默认 0.7 左右">
        <!-- 常用档位：不用理解 0–2 的刻度，点一下直接填；「留空」回到交给服务 -->
        <div class="temp-presets" id="temp-presets">
          <button type="button" class="temp-chip" data-v="0">精确 0</button>
          <button type="button" class="temp-chip" data-v="0.3">保守 0.3</button>
          <button type="button" class="temp-chip" data-v="0.7">均衡 0.7</button>
          <button type="button" class="temp-chip" data-v="1.2">创意 1.2</button>
          <button type="button" class="temp-chip temp-chip-clear" data-v="">清空</button>
        </div>
        <div class="field-tip">多数任务留空即可——不填就不发送该参数，用各家服务自己的默认值（通常 0.7–1.0，因服务而异）。
          写文案、起名字、头脑风暴可试 1.0+；改代码、查资料建议留空或 0.2–0.5。
          <b>推理模型（deepseek-reasoner 等）通常不接受此项</b>，填了也会被忽略。</div>
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
      <h4 class="sec-title">连接测试</h4>
      <span class="conn-actions">
        <button class="btn-ghost test-conn" type="button" title="做一次最小真实调用，验证 Key、地址与模型真的可用；同时探测模型是否在服务列表、上下文窗口多大">🔌 测试连接</button>
        <button class="btn-ghost cap-probe" type="button" title="对流式输出、工具调用、图片输入、思考强度各发一次最小真实调用（会产生少量真实计费），测完给出「这个模型适合干什么」的结论">🧪 能力探测</button>
        <button class="btn-ghost copy-diag" type="button" hidden title="把最近一次测试与探测结果整理成脱敏文本并复制，方便发反馈或留档">📋 复制诊断</button>
      </span>
    </div>
    <div class="flat-panel">
      <div class="panel-note" id="test-conn-note">还没有测试：点右上角「测试连接」。检测的是「能不能真的对话」，比模型列表更严格。</div>
      <div id="test-conn-detail" class="field-tip hidden"></div>
      <div id="cap-chips" class="cap-chip-row hidden"></div>
      <div id="cap-summary" class="field-tip" hidden></div>
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
      <!-- 搜索框：上百个模型时按名字模糊筛，右侧实时显示「命中 / 总数」 -->
      <div class="avail-search" id="avail-search" hidden>
        <input type="search" id="avail-filter" placeholder="搜索模型名（模糊匹配）" autocomplete="off">
        <span class="as-count" id="avail-count"></span>
        <!-- 批量启用：中转站动辄返回上百个模型，逐个点 ＋ 太累。
             只启用「当前筛选结果里还没启用的」，配合搜索就是「按关键词批量加」 -->
        <button class="btn-ghost as-bulk hidden" id="avail-bulk" type="button"
          title="把当前列表里还没启用的模型一次全部启用">＋ 全部启用</button>
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
    const all = availCacheModels || [];
    const filter = availFilter.trim().toLowerCase();
    const shown = filter ? all.filter((m) => m.toLowerCase().includes(filter)) : all;
    // 搜索框：有检测结果才出现；「命中 / 总数」实时反馈筛选结果
    const searchBox = document.getElementById("avail-search");
    const filterInput = document.getElementById("avail-filter");
    const countEl = document.getElementById("avail-count");
    if (searchBox) {
      searchBox.hidden = !all.length || availListCollapsed;
      if (filterInput) {
        if (filterInput.value !== availFilter) filterInput.value = availFilter;
        // oninput 只挂一次：renderAvailPanel 会重复调用，重复挂不影响行为
        if (filterInput.oninput == null) {
          filterInput.oninput = () => {
            availFilter = filterInput.value;
            renderAvailPanel();
          };
        }
      }
      if (countEl) countEl.textContent = filter && all.length ? `${shown.length} / ${all.length}` : "";
      // 批量启用按钮：只在「当前可见结果里还有没启用的」时出现，文案带上数量
      const bulkBtn = document.getElementById("avail-bulk");
      if (bulkBtn) {
        const pending = shown.filter((m) => !enabled.has(m));
        bulkBtn.classList.toggle("hidden", !pending.length || availListCollapsed);
        bulkBtn.textContent = `＋ 全部启用 ${pending.length}`;
        bulkBtn.onclick = () => setEnabledModels(pending, bulkBtn);
      }
    }
    if (filter && all.length && !shown.length) {
      // 搜索词存在但没命中：给出可读提示（不同服务命名差异大，一个都匹配不上很常见）
      list.innerHTML =
        `<div class="empty-hint">没有匹配「${escapeHtml(availFilter.trim())}」的模型，换个关键词试试</div>`;
    } else {
      list.innerHTML = shown
        .map((m) => {
          const on = enabled.has(m);
          return `<div class="avail-model ${on ? "on" : ""}" data-model="${escapeHtml(m)}">
            <span class="plus">${on ? "✓" : "＋"}</span><span>${escapeHtml(m)}</span>
            ${on ? '<span class="chip chip-blue">已启用</span>' : ""}</div>`;
        })
        .join("");
    }
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
  const visionSet = new Set(p.vision_models || []); // 后端按模型名推断的多模态清单
  const enabledBox = document.getElementById("enabled-models");
  enabledBox.innerHTML = "";
  enabledModels.forEach((m) => {
    const isCur = p.is_active && currentModel === m;
    const li = document.createElement("div");
    li.className = "enabled-model";
    li.innerHTML = `
      <span class="ok-mark">✓</span>
      <span class="em-name">${escapeHtml(m)}</span>
      ${visionSet.has(m) ? '<span class="chip" title="该模型支持图片输入">视觉</span>' : ""}
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
      // 删除前确认：✕ 就在行尾，误触一次就得重新检测+启用；
      // 唯一模型删光后该服务直接没有可用模型，单独加重提示
      const last = enabledModels.length <= 1;
      const ok = await confirmModal(
        last
          ? `「${m}」是这个服务唯一的模型，删掉后它将没有可用模型（需要重新检测并启用）。确认删除？`
          : `从已启用列表删除「${m}」？${isCur ? "它正在被使用，删除后会自动换一个。" : ""}`,
        { title: "删除已启用模型", okLabel: "删除", danger: true }
      );
      if (!ok) return;
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

  // 批量启用：循环调单个接口（后端没有批量面），中间不刷新，
  // 全部走完再统一 renderSettings + boot，N 个模型只重绘一次
  const setEnabledModels = async (models, btn) => {
    const list = (models || []).filter(Boolean);
    if (!list.length) return;
    const prevText = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = `启用中… 0/${list.length}`; }
    let added = 0, activated = 0;
    const failed = [];
    for (const [i, model] of list.entries()) {
      try {
        const r = await request("config.add_provider_model", { name, model });
        if (r.added !== false) added++;
        if (r.activated) activated++;
      } catch {
        failed.push(model);
      }
      if (btn) btn.textContent = `启用中… ${i + 1}/${list.length}`;
    }
    if (btn) { btn.disabled = false; btn.textContent = prevText; }
    await renderSettings();
    boot();
    const parts = [`✓ 已启用 ${added} 个模型`];
    if (activated) parts.push("并已切换使用");
    if (failed.length) parts.push(`${failed.length} 个失败`);
    note(parts.join("，"), !failed.length);
  };

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

  // 最近一次测试/探测结果存闭包：供「复制诊断」拼报告；进详情重新开始
  let lastTest = null;
  let lastCaps = null;

  // 连接测试：最小真实调用（1 token 非流式），比列表检测更严格。
  // 结果同步进健康缓存，列表行的状态灯随之更新。
  const runTest = async () => {
    const btn = q(".test-conn");
    const noteEl = document.getElementById("test-conn-note");
    const detailEl = document.getElementById("test-conn-detail");
    const params = {
      name,
      kind: q("select[data-f='kind']").value,
      base_url: q('input[data-f="base_url"]').value.trim(),
      api_key: q('input[data-f="api_key"]').value.trim(),
      model: (p2activeModel() || "").trim(),
    };
    btn.disabled = true;
    noteEl.textContent = "测试中…正在向该服务发起一次最小对话调用（几秒内完成）";
    noteEl.className = "panel-note";
    detailEl.classList.add("hidden");
    try {
      const r = await request("model.test", params);
      lastTest = r;
      document.querySelector(".copy-diag").hidden = false;
      if (r.ok) {
        const parts = [`✓ 连接正常（${r.latency_ms} ms）`];
        if (r.model_listed === true) parts.push("模型在服务列表中");
        else if (r.model_listed === false) parts.push("⚠ 模型不在 /models 列表（部分服务不提供列表，可忽略）");
        if (r.context_limit) parts.push(`上下文窗口 ${r.context_limit.toLocaleString()} tokens`);
        noteEl.textContent = parts.join(" · ");
        noteEl.className = "panel-note ok";
        providerHealthCache[name] = { level: "ok" };
      } else {
        noteEl.textContent = `✗ ${r.title || "连接失败"}${r.error ? "：" + r.error : ""}`;
        noteEl.className = "panel-note bad";
        if (r.advice) {
          detailEl.textContent = "建议：" + r.advice;
          detailEl.classList.remove("hidden");
        }
        providerHealthCache[name] = { level: "down" };
      }
      renderProviderList(); // 状态灯刷新
    } catch (e) {
      noteEl.textContent = "✗ " + e.message;
      noteEl.className = "panel-note bad";
    } finally {
      btn.disabled = false;
    }
  };
  // 当前编辑中的模型名：优先已启用列表的当前项，退回配置里的 model
  const p2activeModel = () => {
    const cur = providerCfg.providers[name];
    if (!cur) return "";
    if (cur.is_active && cur.active_model) return cur.active_model;
    return cur.model || "";
  };
  q(".test-conn").onclick = () => runTest();

  // 能力探测：流式 / 工具调用 / 图片输入 / 思考强度各一次最小真实调用。
  // 与「测试连接」分开：这项会产生少量真实计费，由用户点按钮明确触发。
  const runCapProbe = async () => {
    const btn = q(".cap-probe");
    const noteEl = document.getElementById("test-conn-note");
    const chipsEl = document.getElementById("cap-chips");
    const sumEl = document.getElementById("cap-summary");
    const params = {
      name,
      kind: q("select[data-f='kind']").value,
      base_url: q('input[data-f="base_url"]').value.trim(),
      api_key: q('input[data-f="api_key"]').value.trim(),
      model: (p2activeModel() || "").trim(),
    };
    btn.disabled = true;
    noteEl.textContent = "探测中…正在对四项能力各发一次最小调用（流式 / 工具 / 图片 / 思考强度），会产生少量真实计费";
    noteEl.className = "panel-note";
    chipsEl.classList.add("hidden");
    sumEl.classList.add("hidden");
    try {
      const r = await request("model.probe_capabilities", params);
      lastCaps = r;
      const items = [
        ["流式输出", r.streaming], ["工具调用", r.tools],
        ["图片输入", r.vision], ["思考强度", r.reasoning],
      ];
      chipsEl.innerHTML = items.map(([label, it]) => {
        const mark = it.supported === true ? "✓" : it.supported === false ? "✗" : "—";
        const cls = it.supported === true ? "chip-blue" : it.supported === false ? "chip-bad" : "chip-warn";
        return `<span class="chip ${cls}" title="${escapeHtml(it.note || "")}">${label} ${mark}</span>`;
      }).join("") + `<span class="chip" title="探测总耗时（四次最小调用合计）">${r.latency_ms} ms</span>`;
      chipsEl.classList.remove("hidden");
      sumEl.textContent = r.summary || "";
      sumEl.classList.remove("hidden");
      document.querySelector(".copy-diag").hidden = false;
      noteEl.textContent = "探测完成：悬停每个标记看具体说明。测出「不支持」的项建议按结论调整对应开关。";
      noteEl.className = "panel-note";
    } catch (e) {
      noteEl.textContent = "✗ " + e.message;
      noteEl.className = "panel-note bad";
    } finally {
      btn.disabled = false;
    }
  };
  q(".cap-probe").onclick = () => runCapProbe();

  // 脱敏规则：Key 前缀、Bearer 头、URL 里的 token 参数；诊断报告要能直接发出去
  const maskSensitive = (s) => String(s || "")
    .replace(/sk-[A-Za-z0-9_-]{6,}/g, "sk-***")
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]{6,}/gi, "Bearer ***")
    .replace(/([?&](?:key|api_key|apikey|token|access_token)=)[^&\s]*/gi, "$1***");

  const buildDiagReport = () => {
    const kind = q("select[data-f='kind']").value;
    const baseUrl = maskSensitive(q('input[data-f="base_url"]').value.trim());
    const lines = ["SkySheep 模型诊断报告"];
    lines.push(`服务：${name} · 协议 ${kind} · 地址 ${baseUrl || "（默认接口）"}`);
    lines.push(`模型：${(p2activeModel() || "").trim() || "（未填）"}`);
    if (lastTest) {
      if (lastTest.ok) {
        const parts = [`连接正常（${lastTest.latency_ms} ms）`];
        if (lastTest.model_listed === true) parts.push("模型在服务列表中");
        if (lastTest.context_limit) {
          parts.push(`上下文窗口 ${lastTest.context_limit.toLocaleString()} tokens`);
        }
        lines.push("测试连接：✓ " + parts.join(" · "));
      } else {
        lines.push(`测试连接：✗ ${lastTest.title || "连接失败"}`
          + (lastTest.error ? "：" + maskSensitive(lastTest.error) : ""));
        if (lastTest.category) lines.push(`失败类别：${lastTest.category}`);
        if (lastTest.advice) lines.push(`建议：${lastTest.advice}`);
      }
    }
    if (lastCaps) {
      const items = [
        ["流式输出", lastCaps.streaming], ["工具调用", lastCaps.tools],
        ["图片输入", lastCaps.vision], ["思考强度", lastCaps.reasoning],
      ];
      lines.push("能力探测：" + items.map(([label, it]) =>
        `${label} ${it.supported === true ? "✓" : it.supported === false ? "✗" : "未确认"}`
        + (it.note ? `（${maskSensitive(it.note)}）` : "")).join("；"));
      if (lastCaps.summary) lines.push("结论：" + lastCaps.summary);
    }
    lines.push(`生成时间：${new Date().toLocaleString()}`);
    lines.push("（API Key 与令牌已脱敏，可放心分享）");
    return lines.join("\n");
  };

  q(".copy-diag").onclick = async () => {
    try {
      await copyTextToClipboard(buildDiagReport());
      note("诊断报告已复制（Key 已脱敏，可放心分享）", true);
    } catch (e) {
      note("复制失败：" + e.message, false);
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

  // 温度：档位胶囊 + 当前状态同步。输入框与胶囊双向联动，
  // 「生效中」徽章让人一眼看出这项到底发不发参数
  const tempInput = q('input[data-f="temperature"]');
  const tempState = box.querySelector("#temp-state");
  const tempChips = [...box.querySelectorAll("#temp-presets .temp-chip")];
  const syncTempState = () => {
    const v = tempInput.value.trim();
    // 高亮：值正好等于某个档位就点亮它；空值点亮「清空」
    tempChips.forEach((c) => {
      const cv = c.dataset.v;
      const hit = cv === "" ? v === "" : v !== "" && Number(v) === Number(cv);
      c.classList.toggle("on", hit);
    });
    if (tempState) {
      tempState.textContent = v === "" ? "当前：不发送，用服务默认" : `当前：${v}`;
      tempState.className = "temp-state" + (v === "" ? " dim-state" : " on-state");
    }
  };
  tempChips.forEach((c) => {
    c.onclick = () => {
      tempInput.value = c.dataset.v; // 空串 = 清空（保存时清除该项）
      syncTempState();
    };
  });
  tempInput.addEventListener("input", syncTempState);
  syncTempState();

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
  // 采集初始快照：之后与当前值比对，判断有没有没保存的改动。
  // 只收用户可编辑的字段（名称是只读的，Key 只比「填没填」不比明文）
  detailSnapshot = detailFormFingerprint(box);
}

/** 表单指纹：可编辑字段的当前值拼串。Key 只记「有没有填」，不记内容（避免明文留存）。 */
function detailFormFingerprint(box) {
  const parts = [];
  box.querySelectorAll("input[data-f], select[data-f]").forEach((el) => {
    const f = el.dataset.f;
    if (f === "name" || f === "manual_model") return; // 只读 / 一次性输入，不算改动
    if (f === "api_key") { parts.push(f + "=" + (el.value.trim() ? "set" : "")); return; }
    parts.push(f + "=" + (el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value.trim()));
  });
  return parts.join("|");
}
