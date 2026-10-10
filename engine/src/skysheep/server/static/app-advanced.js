// app-advanced.js —— 设置 · 高级页：Hooks 钩子面板 + 运行参数 / 目录限制 /
// 开机自启 / 信任项目清单
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有函数声明与状态常量，零加载期执行，按钮接线仍留在 app.js 原位；
// request / showModal / escapeHtml 等助手是运行时调用。

// ---------- 设置 · 高级：Hooks 钩子面板 ----------
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
    box.innerHTML = `<div class="rp-empty">还没有${HOOK_KIND_LABEL[kind] || kind}钩子。</div>`;
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
    const head = `${escapeHtml(HOOK_KIND_LABEL[r.kind] || r.kind)} · ${escapeHtml(r.tool || "—")}`;
    const out = r.output ? escapeHtml(r.output) : "";
    return `<li class="hook-run st-${escapeHtml(r.status || "ok")}">
      <span class="chip ${cls}">${label}</span>
      <span class="hook-run-head" title="${head}">${head}</span>
      <span class="hook-run-out" title="${out}">${out}</span>
      <span class="dim small hook-run-meta">${when} · ${Number(r.code) || 0} · ${Number(r.duration_ms) || 0}ms</span>
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
    hooksStatus(`✓ 已保存并立即生效（前 ${d.active_pre} / 后 ${d.active_post} / 完成 ${d.active_stop}）`);
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
      ? (r.blocked ? "⛔ 这次调用会被阻止" : "✓ 这次调用会放行")
      : "✓ 已执行（仅通知，不影响任务）";
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

// ---------- 设置 · 高级：运行参数 / 目录限制 / 开机自启 ----------
function advancedStatus(text, ok = true) {
  const el = document.getElementById("advanced-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 加载时记下的热键：保存后判断是否变了——热键要重启才生效，提示不能撒谎
let advancedLoadedHotkey = "";

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
  q("adv-job-containment").checked = d.job_containment !== false;
  q("adv-sandbox-level").value = d.sandbox_level === "restricted" ? "restricted" : "job";
  syncSandboxLevel();
  renderTrustState();
  renderTrustList();
  q("adv-daily-budget").value = d.daily_token_budget || "";
  q("adv-turn-budget").value = d.turn_token_budget || "";
  q("adv-fallback-model").value = d.fallback_model || "";
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
    autoHint.textContent = `已开启：开机后自动在后台启动（关闭窗口时选「缩到系统托盘」即持续运行，定时任务才会按点触发）。`;
  } else {
    autoHint.textContent = "开启后开机自动启动，定时任务与日程提醒才能真正无人值守";
  }
  advancedStatus("");
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
      fallback_model: (q("adv-fallback-model").value || "").trim(),
      restrict_to_workdir: q("adv-restrict-workdir").checked,
      system_schedule: q("adv-system-schedule").checked,
      job_containment: q("adv-job-containment").checked,
      sandbox_level: q("adv-sandbox-level").value,
      autostart: q("adv-autostart").checked,
      hotkey: newHotkey,
    });
    // 先重渲染（会把状态行清掉）再写成功提示，否则"✓ 已保存"一闪就没
    await renderAdvancedCfg();
    // 热键随窗口创建注册，改键要重启才生效——与「已立即生效」分开说，别误导
    advancedStatus(newHotkey !== oldHotkey
      ? "✓ 已保存；全局唤起热键在重启应用后生效"
      : "✓ 已保存并立即生效");
    if (newHotkey !== oldHotkey) needsRestart(); // 页顶横条给出就地重启入口
    const st = r.autostart || {};
    if (q("adv-autostart").checked && st.enabled) {
      addNotice("✓ 已设置开机自启；定时任务从此可以无人值守");
    }
  } catch (e) {
    advancedStatus("✗ 保存失败：" + e.message, false);
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
        try {
          await request("trust.revoke_path", { path: it.path });
          await renderTrustList();
          renderTrustState();
          addNotice(`已撤销信任：${it.path}`);
        } catch (err) {
          addNotice("撤销失败：" + err.message);
        }
      }, "撤销");
    };
    ul.appendChild(li);
  });
}
