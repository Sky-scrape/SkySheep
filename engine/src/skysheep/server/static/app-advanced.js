// app-advanced.js —— 设置  高级页：Hooks 钩子面板 + 运行参数 / 目录限制 /
// 开机自启 / 信任项目清单
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有函数声明与状态常量，零加载期执行，按钮接线仍留在 app.js 原位；
// request / showModal / escapeHtml 等助手是运行时调用。

// ---------- 设置  高级：Hooks 钩子面板 ----------
// 规则状态存在内存里，编辑后点「保存」一次性提交（与运行参数同一交互习惯）
let hooksState = { pre: [], post: [], stop: [], tool_names: [] };
const HOOK_KIND_LABEL = { pre: "调用前", post: "调用后", stop: "任务完成" };

function hooksStatus(text, ok = true) {
  const el = document.getElementById("hooks-status");
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 工具名候选：来自后端实际注册的工具清单，写 match 时不用凭记忆
function hookToolOptions() {
  return hooksState.tool_names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
}

function fillHookToolDatalist() {
  const dl = document.getElementById("hook-tools");
  if (dl) dl.innerHTML = hookToolOptions();
}

function renderHooksList(kind) {
  const box = document.getElementById(`hooks-${kind}-list`);
  if (!box) return;
  const items = hooksState[kind] || [];
  if (!items.length) {
    // 空态跟随卡片里的其他空列表（左对齐的 dim 小字）；rp-empty 是右面板的
    // 居中式空态，放进这条宽卡片会孤零零地悬在中间，离左侧分组标题很远
    box.innerHTML = `<div class="empty-hint">还没有${HOOK_KIND_LABEL[kind] || kind}钩子。</div>`;
    return;
  }
  box.innerHTML = "";
  items.forEach((rule, i) => {
    const row = document.createElement("div");
    row.className = "hook-row";
    // stop 钩子不按工具过滤，没有 match 输入；每条钩子带启停开关（停用保留配置）
    const matchField = kind === "stop"
      ? `<span class="hook-match-static dim small" title="任务完成（最终回答、不再调工具）时触发，不按工具过滤">最终回答结束时</span>`
      : `<input class="hook-match" list="hook-tools" value="${escapeHtml(rule.match || "*")}"
          placeholder="*" title="工具名通配，如 write_file / run_command / *">`;
    row.innerHTML = `
      <label class="hook-enable" title="停用后保留配置，不再执行；重新勾选即恢复">
        <input type="checkbox" class="hook-enabled" ${rule.enabled === false ? "" : "checked"}>启用
      </label>
      ${matchField}
      <input class="hook-cmd" value="${escapeHtml(rule.command || "")}"
        placeholder="python check.py" title="要执行的命令（Windows 走 cmd，其它平台走 bash）">
      <input class="hook-timeout" type="number" min="1" max="600" step="1"
        value="${Number(rule.timeout_s) || 10}" title="超时秒数（1–600）">
      <button class="btn-ghost rp-mini" title="删除这条钩子">✕</button>`;
    if (kind !== "stop") {
      row.querySelector(".hook-match").oninput = (e) => { rule.match = e.target.value.trim() || "*"; };
    }
    row.querySelector(".hook-enabled").onchange = (e) => { rule.enabled = e.target.checked; };
    row.querySelector(".hook-cmd").oninput = (e) => { rule.command = e.target.value; };
    row.querySelector(".hook-timeout").oninput = (e) => { rule.timeout_s = Number(e.target.value) || 10; };
    row.querySelector("button").onclick = () => {
      hooksState[kind].splice(i, 1);
      renderHooksList(kind);
    };
    box.appendChild(row);
  });
}

function addHookRule(kind) {
  hooksState[kind].push(kind === "stop"
    ? { command: "", timeout_s: 10, enabled: true }
    : { match: "*", command: "", timeout_s: 10, enabled: true });
  renderHooksList(kind);
  const box = document.getElementById(`hooks-${kind}-list`);
  const last = box.querySelector(".hook-row:last-child .hook-cmd");
  if (last) last.focus();
}

async function renderHooksCfg() {
  renderHooksList("pre");
  renderHooksList("post");
  renderHooksList("stop");
  try {
    const d = await request("hooks.get");
    hooksState = {
      pre: d.pre || [], post: d.post || [], stop: d.stop || [],
      tool_names: d.tool_names || [],
    };
    renderHooksList("pre");
    renderHooksList("post");
    renderHooksList("stop");
    renderHooksCounts(d.active_pre, d.active_post, d.active_stop);
    renderHookRecentRuns(d.recent || []);
    const pathEl = document.getElementById("hooks-config-path");
    if (pathEl) pathEl.textContent = `存于配置文件：${d.config_path || ""}`;
    fillHookToolDatalist();
    hooksStatus("");
  } catch (e) {
    hooksStatus("加载失败：" + e.message, false);
  }
}

function renderHooksCounts(activePre, activePost, activeStop) {
  const parts = [
    ["hooks-active-pre", activePre],
    ["hooks-active-post", activePost],
    ["hooks-active-stop", activeStop],
  ];
  for (const [id, n] of parts) {
    const el = document.getElementById(id);
    if (el) el.textContent = n ? `已生效 ${n} 条` : "未配置";
  }
}

// 最近执行（含钩子自身故障）：非 0 非 2 退出码按设计放行，但必须看得见
function renderHookRecentRuns(runs) {
  const ul = document.getElementById("hooks-recent-list");
  if (!ul) return;
  if (!runs.length) {
    ul.innerHTML = '<li class="dim small">还没有执行记录（配置钩子并在对话中触发后出现在这里）</li>';
    return;
  }
  const statusMeta = {
    ok: ["✓ 正常", "safe-mark"],
    blocked: ["⛔ 阻止", "write-mark"],
    error: ["✗ 故障", "danger-mark"],
  };
  ul.innerHTML = runs.slice().reverse().map((r) => {
    const [label, cls] = statusMeta[r.status] || [r.status || "?", ""];
    const when = new Date((r.time || 0) * 1000).toLocaleTimeString();
    const head = `${escapeHtml(HOOK_KIND_LABEL[r.kind] || r.kind)}  ${escapeHtml(r.tool || "—")}`;
    const out = r.output ? escapeHtml(r.output) : "";
    return `<li class="hook-run st-${escapeHtml(r.status || "ok")}">
      <span class="chip ${cls}">${label}</span>
      <span class="hook-run-head" title="${head}">${head}</span>
      <span class="hook-run-out" title="${out}">${out}</span>
      <span class="dim small hook-run-meta">${when}  ${Number(r.code) || 0}  ${Number(r.duration_ms) || 0}ms</span>
    </li>`;
  }).join("");
}

async function saveHooks() {
  // 前端先拦一道：空命令会让保存直接失败，不如就地指出是哪一行
  for (const kind of ["pre", "post", "stop"]) {
    const bad = (hooksState[kind] || []).findIndex((r) => !String(r.command || "").trim());
    if (bad >= 0) {
      hooksStatus(`✗ ${HOOK_KIND_LABEL[kind]}第 ${bad + 1} 条钩子还没填命令`, false);
      return;
    }
  }
  try {
    const d = await request("hooks.save", {
      pre: hooksState.pre, post: hooksState.post, stop: hooksState.stop,
    });
    hooksState = {
      pre: d.pre || [], post: d.post || [], stop: d.stop || [],
      tool_names: d.tool_names || hooksState.tool_names,
    };
    renderHooksList("pre");
    renderHooksList("post");
    renderHooksList("stop");
    // 生效计数也要跟着刷新，否则刚保存完还显示「未配置」，看起来像没生效
    renderHooksCounts(d.active_pre, d.active_post, d.active_stop);
    renderHookRecentRuns(d.recent || []);
    hooksStatus("✓ 已保存（下一轮起生效）");
  } catch (e) {
    hooksStatus("✗ 保存失败：" + e.message, false);
  }
}

// 钩子测试器：拿示例参数把命令实跑一遍（不保存配置、不进执行记录）
const hookTestRun = async () => {
  const kind = document.getElementById("hook-test-kind").value;
  const tool = document.getElementById("hook-test-tool").value.trim();
  const command = document.getElementById("hook-test-cmd").value.trim();
  const timeout = Number(document.getElementById("hook-test-timeout").value) || 10;
  const out = document.getElementById("hook-test-result");
  const show = (text, cls) => {
    out.textContent = text;
    out.className = "rule-check-result " + (cls || "");
    out.hidden = false;
  };
  if (!command) return show("请先填写要测试的命令", "warn");
  if (kind !== "stop" && !tool) return show("请填写工具名（如 write_file）", "warn");
  let input = {};
  const raw = document.getElementById("hook-test-input").value.trim();
  if (raw) {
    try {
      input = JSON.parse(raw);
    } catch (e) {
      return show("参数不是合法 JSON：" + e.message, "warn");
    }
  }
  show("运行中…", "");
  try {
    const r = await request("hooks.test", { kind, tool, command, timeout_s: timeout, input });
    const verdict = kind === "pre"
      ? (r.blocked ? "⛔ 这次调用会被阻止" : "这次调用会放行")
      : "已执行（仅通知，不影响任务）";
    const detail = [
      `${verdict}（退出码 ${r.code}，耗时 ${r.duration_ms}ms）`,
      r.stdout ? `stdout：${r.stdout}` : "",
      r.stderr ? `stderr：${r.stderr}` : "",
    ].filter(Boolean).join("\n");
    show(detail, r.blocked ? "warn" : "ok");
  } catch (err) {
    show("测试失败：" + err.message, "warn");
  }
};

// ---------- 设置  高级：运行参数 / 目录限制 / 开机自启 ----------
function advancedStatus(text, ok = true) {
  const el = document.getElementById("advanced-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 加载时记下的热键：保存后判断是否变了——热键要重启才生效，提示不能撒谎
let advancedLoadedHotkey = "";
// 凭据加密降级提示是否已收起（本次页面会话内一次性）：引擎侧降级标记按进程
// 存活（明文已落盘的事实不随一次加密成功消失），刷新页面后提示会再出现
let credentialCryptoDismissed = false;

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
  // 触发比例以百分数编辑（50–98），存的是 0.5–0.98 的比例
  q("adv-compaction-trigger").value = Math.round((d.compaction_trigger ?? 0.9) * 100);
  // 自动压缩总开关（老配置没有该字段 = 默认开）
  q("adv-compaction-auto").checked = d.compaction_auto !== false;
  syncCompactionInputs();
  q("adv-restrict-workdir").checked = !!d.restrict_to_workdir;
  // 「安全与后台」：定时调度总开关与命令沙箱。读取点都在各自执行路径上按
  // 当前配置取值，保存后即热生效，无需重启
  q("adv-system-schedule").checked = !!d.system_schedule;
  // 这两个开关在卡里没有自己的保存按钮（原先只能去上面「运行参数」卡点保存，
  // 拨完不落盘、切页回来就回弹）：改为拨动即存。每次渲染重挂一次接线
  const safetyChange = (e) => saveSafetyCard(e.target);
  q("adv-restrict-workdir").onchange = safetyChange;
  q("adv-system-schedule").onchange = safetyChange;
  q("adv-job-containment").checked = d.job_containment !== false;
  q("adv-sandbox-level").value = d.sandbox_level === "restricted" ? "restricted" : "job";
  syncSandboxLevel();
  renderTrustState();
  renderTrustList();
  q("adv-daily-budget").value = d.daily_token_budget || "";
  q("adv-turn-budget").value = d.turn_token_budget || "";
  q("adv-permission-timeout").value = d.permission_timeout_s || "";
  q("adv-fallback-model").value = d.fallback_model || "";
  // 全局出网设置（代理 / 绕行名单 / 自签 CA）
  q("adv-network-proxy").value = d.network_proxy || "";
  q("adv-network-no-proxy").value = d.network_no_proxy || "";
  q("adv-network-ca").value = d.network_ca_cert || "";
  // 全局唤起热键（仅 Windows 桌面版生效；其它平台置灰提示）
  const hk = q("adv-hotkey");
  advancedLoadedHotkey = d.hotkey || "";
  if (hk) {
    hk.value = d.hotkey || "";
    hk.placeholder = d.hotkey || "Ctrl+Alt+Space";
    hk.disabled = !d.hotkey_supported;
  }
  const hint = q("adv-context-hint");
  const src = d.current_provider_context_limit
    ? `当前服务「${d.current_provider}」自带设置 ${d.current_provider_context_limit.toLocaleString()}`
    : "当前服务没单独设置，用这个全局值";
  hint.textContent = `默认 1000000。${src}；当前实际生效 ${d.context_limit_tokens_effective.toLocaleString()} tokens`;
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
    autoHint.textContent = `已开启：开机自动在后台启动（关窗选「缩到系统托盘」即常驻，定时任务按时触发）。`;
  } else {
    autoHint.textContent = "开启后开机自动启动，定时任务与日程提醒才能真正无人值守";
  }
  // 凭据加密降级提示：DPAPI 加密失败时凭据按明文落盘、只留日志——「已加密」
  // 的用户被静默降级必须看得见（reason 是引擎侧记录的失败首因）
  const cryptoWarn = q("adv-credential-crypto-warn");
  if (cryptoWarn) {
    const cc = d.credential_crypto || {};
    if (cc.degraded && !credentialCryptoDismissed) {
      cryptoWarn.innerHTML = "⚠ 凭据未能加密，已按明文保存，请检查 DPAPI 环境" +
        (cc.reason ? `（${escapeHtml(cc.reason)}）` : "") +
        ' <button class="btn-ghost" id="btn-crypto-warn-dismiss">知道了</button>';
      cryptoWarn.hidden = false;
      const dismiss = cryptoWarn.querySelector("#btn-crypto-warn-dismiss");
      if (dismiss) dismiss.onclick = () => {
        credentialCryptoDismissed = true;
        cryptoWarn.hidden = true;
      };
    }
  }
  await renderRoutingCfg(d);
  // 切换卡的即改即存状态行是就地插进卡里的元素，重进页面时把上一条收起
  const safetyEl = document.getElementById("safety-status");
  if (safetyEl) {
    safetyEl.hidden = true;
    safetyEl.textContent = "";
  }
  advancedStatus("");
}

// 模型自动分层（A3）：服务下拉来自 config.providers（Key 只回掩码），
// 模型是自由文本（服务默认模型留空即可）。读取值随 advanced.get 一并回来。
async function renderRoutingCfg(d) {
  const q = (id) => document.getElementById(id);
  const box = q("routing-enabled");
  if (!box) return;
  box.checked = !!d.routing_enabled;
  q("routing-light-model").value = d.routing_light_model || "";
  q("routing-heavy-model").value = d.routing_heavy_model || "";
  let providers = {};
  try {
    providers = (await request("config.providers")).providers || {};
  } catch (e) { /* 取不到就只留「不指定」，保存时后端仍校验服务名 */ }
  const fill = (sel, current) => {
    const opts = ['<option value="">不指定</option>'];
    Object.keys(providers).forEach((name) => {
      const pc = providers[name] || {};
      opts.push(`<option value="${escapeHtml(name)}"${current === name ? " selected" : ""}>` +
        `${escapeHtml(pc.label || name)}${pc.has_key ? "" : " · 未配置 Key"}</option>`);
    });
    if (current && !providers[current]) {
      opts.push(`<option value="${escapeHtml(current)}" selected>${escapeHtml(current)}（已不存在）</option>`);
    }
    sel.innerHTML = opts.join("");
  };
  fill(q("routing-light-provider"), d.routing_light_provider || "");
  fill(q("routing-heavy-provider"), d.routing_heavy_provider || "");
}

async function saveRouting() {
  const q = (id) => document.getElementById(id);
  const el = q("routing-status");
  el.hidden = true;
  try {
    await request("advanced.save", {
      routing_enabled: q("routing-enabled").checked,
      routing_light_provider: q("routing-light-provider").value || "",
      routing_light_model: (q("routing-light-model").value || "").trim(),
      routing_heavy_provider: q("routing-heavy-provider").value || "",
      routing_heavy_model: (q("routing-heavy-model").value || "").trim(),
    });
    await renderAdvancedCfg();
    el.textContent = "✓ 已保存（下一轮起生效）";
    el.className = "card-status ok";
    el.hidden = false;
    autoHideStatus(el, el.textContent, true);
  } catch (e) {
    el.textContent = "✗ 保存失败：" + e.message;
    el.className = "card-status bad";
    el.hidden = false;
  }
}

// 「安全与后台」卡里没有预置状态行（#advanced-status 在隔了几张卡的「运行参数」
// 卡上，拨这里的开关看不到）：反馈就地给——状态行建一次、插在被拨开关的下面
function safetyStatus(anchorEl, text, ok = true) {
  let el = document.getElementById("safety-status");
  if (!el) {
    el = document.createElement("div");
    el.id = "safety-status";
    const row = anchorEl && anchorEl.closest(".adv-toggle");
    if (!row) return;
    row.insertAdjacentElement("afterend", el);
  }
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 「仅允许 Agent 访问当前工作目录」「系统级定时调度」即改即存：提交整卡当前值
// （与网络卡/归档卡的即改即存同一习惯），不再要求用户再去点另一张卡的「保存」
async function saveSafetyCard(toggleEl) {
  const q = (id) => document.getElementById(id);
  try {
    await request("advanced.save", {
      restrict_to_workdir: q("adv-restrict-workdir").checked,
      system_schedule: q("adv-system-schedule").checked,
      job_containment: q("adv-job-containment").checked,
      sandbox_level: q("adv-sandbox-level").value,
    });
    safetyStatus(toggleEl, "✓ 已保存（立即生效）");
  } catch (e) {
    toggleEl.checked = !toggleEl.checked; // 失败回拨：开关别停在没存上的位置
    safetyStatus(toggleEl, "✗ 保存失败：" + e.message, false);
  }
}

async function saveAdvanced() {
  const q = (id) => document.getElementById(id);
  const newHotkey = (q("adv-hotkey").value || "").trim();
  const oldHotkey = advancedLoadedHotkey; // renderAdvancedCfg 会刷新它，先记旧值
  try {
    const r = await request("advanced.save", {
      max_iterations: Number(q("adv-max-iterations").value),
      context_limit_tokens: Number(q("adv-context-limit").value),
      compaction_keep_recent: Number(q("adv-keep-recent").value),
      compaction_trigger: Number(q("adv-compaction-trigger").value) / 100,
      compaction_auto: q("adv-compaction-auto").checked,
      daily_token_budget: Number(q("adv-daily-budget").value) || 0,
      turn_token_budget: Number(q("adv-turn-budget").value) || 0,
      permission_timeout_s: Number(q("adv-permission-timeout").value) || 0,
      fallback_model: (q("adv-fallback-model").value || "").trim(),
      restrict_to_workdir: q("adv-restrict-workdir").checked,
      system_schedule: q("adv-system-schedule").checked,
      job_containment: q("adv-job-containment").checked,
      sandbox_level: q("adv-sandbox-level").value,
      autostart: q("adv-autostart").checked,
      hotkey: newHotkey,
    });
    await renderAdvancedCfg();
    // 热键随窗口创建注册，改键要重启才生效：重启入口在页顶横条，状态行里一并说明
    if (newHotkey !== oldHotkey) needsRestart(); // 页顶横条给出就地重启入口
    advancedStatus(newHotkey !== oldHotkey
      ? "✓ 已保存（下一轮起生效）；新热键需按页顶提示重启后生效"
      : "✓ 已保存（下一轮起生效）");
    const st = r.autostart || {};
    if (q("adv-autostart").checked && st.enabled) {
      addNotice("✓ 已设置开机自启；定时任务从此可以无人值守");
    }
  } catch (e) {
    advancedStatus("✗ 保存失败：" + e.message, false);
  }
}

function networkStatus(text, ok = true) {
  const el = document.getElementById("network-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 网络卡单独保存：只提交三项，不动运行参数（避免用户只改代理却连带重置轮数）
async function saveNetwork() {
  const q = (id) => document.getElementById(id);
  try {
    await request("advanced.save", {
      network_proxy: (q("adv-network-proxy").value || "").trim(),
      network_no_proxy: (q("adv-network-no-proxy").value || "").trim(),
      network_ca_cert: (q("adv-network-ca").value || "").trim(),
    });
    networkStatus("✓ 已保存（下一轮起生效）");
  } catch (e) {
    networkStatus("✗ 保存失败：" + e.message, false);
  }
}

// 自动压缩关闭时置灰两个压缩参数：开关管「压不压」，比例/条数只在开着时有意义
function syncCompactionInputs() {
  const off = !document.getElementById("adv-compaction-auto").checked;
  document.getElementById("adv-compaction-trigger").disabled = off;
  document.getElementById("adv-keep-recent").disabled = off;
}

// 沙箱遏制关闭时置灰级别下拉：总开关关 = 完全不沙箱，级别不会把遏制复活
function syncSandboxLevel() {
  const off = !document.getElementById("adv-job-containment").checked;
  document.getElementById("adv-sandbox-level").disabled = off;
}

// 已信任项目清单：查看 + 按路径撤销（撤当前项目会同步断开其 MCP、重载技能）
async function renderTrustList() {
  const ul = document.getElementById("trust-list");
  if (!ul) return;
  let d;
  try { d = await request("trust.list"); } catch (e) {
    ul.innerHTML = `<li class="dim small">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const items = d.items || [];
  if (!items.length) {
    ul.innerHTML = '<li class="dim small">还没有已信任的项目（首次打开带 .skysheep/ 配置的项目并确认后出现在这里）</li>';
    return;
  }
  ul.innerHTML = "";
  items.forEach((it) => {
    const li = document.createElement("li");
    li.className = "trust-row";
    const when = it.trusted_at ? fmtBackupTime(it.trusted_at) : "";
    li.innerHTML = `
      <span class="item-name" title="${escapeHtml(it.path)}">${escapeHtml(it.path)}</span>
      ${it.is_current ? '<span class="backup-tag">当前</span>' : ""}
      <span class="dim small rule-age">${when}</span>
      <span class="spacer"></span>
      <button class="btn-ghost danger">撤销信任</button>`;
    li.querySelector("button").onclick = () => {
      const box = document.createElement("div");
      box.innerHTML = `<p>撤销对 <b>${escapeHtml(it.path)}</b> 的信任？</p>
        <p class="dim small">撤销后，下次打开该项目时其中的 .skysheep/ 配置（MCP 与技能）会重新询问确认。</p>`;
      showModal("撤销信任", box, async () => {
        // 失败不在这里吞掉：抛回给 showModal，弹层留在原地显示行内原因
        // （设置页里 addNotice 挂在对话区，用户根本看不到）
        await request("trust.revoke_path", { path: it.path });
        await renderTrustList();
        renderTrustState();
        addNotice(`已撤销信任：${it.path}`);
      }, "撤销");
    };
    ul.appendChild(li);
  });
}

// ---------- 设置·高级：终端 / 交互与电源 ----------
let termCfg = { shell: "auto", font: "Cascadia Mono", size: 13 };

async function renderTerminalCfg() {
  let p = {};
  try {
    const r = await request("ui.get");
    p = (r && r.prefs) || {};
  } catch (e) { /* 读不到就按默认值渲染，不阻塞整页 */ }
  termCfg.shell = p.term_shell || "auto";
  termCfg.font = p.term_font || "Cascadia Mono";
  termCfg.size = Number(p.term_font_size) || 13;
  const shell = document.getElementById("term-shell");
  if (shell) shell.value = termCfg.shell;
  const font = document.getElementById("term-font");
  if (font) font.value = termCfg.font;
  const size = document.getElementById("term-font-size");
  if (size) size.value = termCfg.size;
  const ps = document.getElementById("prevent-sleep-toggle");
  if (ps) ps.checked = p.prevent_sleep === 1;
  const im = document.getElementById("interaction-mode");
  if (im) im.value = p.interaction_mode === "steer" ? "steer" : "queue";
}

async function saveTerminalCfg() {
  const shell = (document.getElementById("term-shell") || {}).value || "auto";
  const font = (document.getElementById("term-font") || {}).value || "Cascadia Mono";
  const rawSize = Number((document.getElementById("term-font-size") || {}).value);
  const size = Number.isFinite(rawSize) ? Math.min(24, Math.max(10, Math.round(rawSize))) : 13;
  termFont = font;
  termFontSize = size;
  if (typeof applyTermAppearance === "function") applyTermAppearance();
  const el = document.getElementById("terminal-status");
  // 走 saveUiPrefs 只管发出去（它自己吞错），这里要「存成功了再报」，所以直接等 ui.save
  try {
    await request("ui.save", {
      prefs: {
        term_shell: shell === "auto" ? null : shell,
        term_font: font === "Cascadia Mono" ? null : font,
        term_font_size: size === 13 ? null : size,
      },
    });
  } catch (e) {
    if (el) {
      el.textContent = "✗ 保存失败：" + e.message;
      el.className = "card-status bad";
      el.hidden = false;
    }
    return;
  }
  if (el) {
    el.textContent = "✓ 已保存：新开/重启的终端使用这个 Shell，字体已应用到已打开的标签";
    el.className = "card-status ok";
    el.hidden = false;
    autoHideStatus(el, el.textContent, true);
  }
}
