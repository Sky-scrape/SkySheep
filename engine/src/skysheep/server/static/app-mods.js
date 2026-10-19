// app-mods.js —— Mods 扩展（实验性）：事件片段渲染 + 设置页
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有函数声明与状态常量，零加载期执行，按钮接线仍留在 app.js 原位；
// request / showModal / escapeHtml 等助手是运行时调用。

// ---------- Mods 扩展（实验性）：事件片段 + 设置页 ----------
// Mod 的 JS 永不进前端：这里只渲染引擎侧产出的声明式词表片段
// （stat/badge/progress/timeline/text 五种，未知 kind 引擎侧已丢弃）。

// 权限卡的 Mod 片段暂存：permission_request 事件之前到达的 slot=perm 片段
// 由 showPermission 消费，hidePermission 清空（第三方文本带 [Mod·<id>] 前缀展示）。
// 按会话分桶：两个会话同时挂着待确认卡时，A 会话的 Mod 片段不能串到 B 的卡上
// （第三方文本上错安全确认卡，比不上卡更糟）。
let pendingPermModLines = new Map();

/** tray 片段随会话标签渲染：t.modTray 挂在标签对象上，跨会话不串。 */
function renderModTray() {
  const el = document.getElementById("mod-tray");
  if (!el) return;
  const tray = activeTab && activeTab.modTray;
  if (!tray || !tray.text) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `<span class="mod-tray-chip mod-lv-${escapeHtml(tray.level || "ok")}"` +
    ` title="${escapeHtml(tray.text)}">${escapeHtml(tray.text)}</span>`;
  el.classList.remove("hidden");
}

/** 消息流内的 Mod 片段卡：badge / progress / timeline 三种形态。 */
function addModFragment(modId, w) {
  const t = routeTab || activeTab;
  if (!t || !t.logEl) return;
  const div = document.createElement("div");
  div.className = "mod-fragment";
  const cap = `<span class="mod-frag-src" title="由 Mod「${escapeHtml(modId)}」产出">Mod·${escapeHtml(modId)}</span>`;
  if (w.kind === "progress") {
    const val = Math.max(0, Math.min(100, Number(w.value) || 0));
    div.innerHTML = `${cap}<span class="mod-progress"><span class="mod-progress-bar" style="width:${val}%"></span></span>` +
      `<span class="mod-frag-text">${escapeHtml(w.text || "")}</span>`;
  } else if (w.kind === "timeline" && Array.isArray(w.items)) {
    div.innerHTML = `${cap}<ul class="mod-timeline">` +
      w.items.map((it) =>
        `<li><span class="mod-tl-t">${escapeHtml(it.t || "")}</span>` +
        `<span class="mod-tl-text">${escapeHtml(it.text || "")}</span></li>`).join("") +
      "</ul>";
  } else {
    // badge / text / 其它落在 stream 的兜底：一行文本
    div.innerHTML = `${cap}<span class="mod-frag-text">${escapeHtml(w.text || "")}</span>`;
  }
  t.logEl.appendChild(div);
  scrollLog();
}

/** mod_ui 事件入口：按 slot 分流（tray 状态条 / perm 确认卡 / stream 消息流）。 */
function onModUi(data) {
  const w = data.widget || {};
  if (data.slot === "tray") {
    const t = tabFor(data.session_id) || routeTab || activeTab;
    if (t) t.modTray = { text: w.text || "", level: w.level || "ok" };
    renderModTray();
    return;
  }
  if (data.slot === "perm") {
    // permission_request 事件稍后到达：先暂存进本会话的桶，showPermission 渲染
    const sid = data.session_id || "";
    if (!pendingPermModLines.has(sid)) pendingPermModLines.set(sid, []);
    pendingPermModLines.get(sid).push(
      `<span class="mod-frag-src">Mod·${escapeHtml(data.mod_id || "")}</span>` +
      `<span class="mod-frag-text">${escapeHtml(w.text || w.badge || "")}</span>`);
    return;
  }
  addModFragment(data.mod_id || "", w);
}

// ---- 设置 · Mods 扩展 ----

let modsState = { mods: [], enabled: false, runtime_available: true, runtime_version: "" };

function modsStatus(text, ok = true) {
  const el = document.getElementById("mods-status");
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

const MOD_HOOK_LABELS = {
  tool_pre: "工具调用前", tool_post: "工具调用后", permission_request: "权限请求",
  iteration_start: "迭代开始", turn_stop: "回合结束",
  // Claude 兼容模式的事件名（点号风格）：tool.check 是权限决策、tool.pre 是
  // 已放行后的前置通知——两者都在"调用前"，但语义不同，不能显示成同一个名字
  "tool.check": "权限决策", "tool.pre": "工具调用前",
  "tool.post": "工具调用后", "prompt.submit": "提示提交",
  "turn.stop": "回合结束", "iteration.start": "迭代开始",
  "session.start": "会话开始", "session.end": "会话结束",
};

/** permissions 档位 → 界面文案：两套模式的词表不同，不能共用一条判断。
    - quickjs：observe（仅观察提示）/ tighten（可拒绝、要求确认）
    - Claude 兼容：observe / tighten / full（可 allow、可改写参数）
    "full" 在 Claude 模式是最高权限档，绝不能显示成"观察档"。 */
function modPermLabel(perm) {
  if (perm === "tighten") return "收紧档（可拒绝/要求确认）";
  if (perm === "full") return "完全权限（可放行/改写参数）";
  return "观察档（仅观察与提示）";
}

// 执行状态 → [界面文案, chip 样式]：列表「最近：」行与详情弹窗共用同一份映射
const MOD_STATUS_META = {
  ok: ["✓ 正常", "safe-mark"], timeout: ["⏱ 超时", "write-mark"],
  error: ["✗ 出错", "danger-mark"], dropped: ["⨯ 已丢弃", "danger-mark"],
};

async function renderModsCfg() {
  try {
    const [d, off] = await Promise.all([
      request("mods.list"),
      request("mods.official").catch(() => ({ mods: [], bundled: false })),
    ]);
    modsState = {
      mods: d.mods || [], enabled: !!d.enabled,
      runtime_available: d.runtime_available !== false,
      runtime_version: d.runtime_version || "",
      mode: d.mode === "claude" ? "claude" : "quickjs",
      official: off.mods || [], bundled: !!off.bundled,
    };
    const tg = document.getElementById("mods-toggle");
    if (tg) tg.checked = modsState.enabled;
    renderModsModeText(modsState);
    renderModsList(modsState.mods);
    renderOfficialMods(modsState.official);
    // 注意：这里不能像 hooks/advanced/retention 页那样以 Status("") 收尾——
    // 本页的操作（行内启停/删除/一键安装/总开关/确认安装）都是
    // 「先 modsStatus(提示) 再 renderModsCfg()」，渲染尾部清空会把刚显示的
    // 成功/失败确认条压成两次本地往返的残影（autoHideStatus 的 6 秒驻留与
    // 「错误保留到下一次操作」全被架空）。残留的「加载失败」按错误驻留设计
    // 保留，由下一次操作覆盖
  } catch (e) {
    modsStatus("加载失败：" + e.message, false);
  }
}

/** 按运行模式改写说明区文案（quickjs 沙箱 vs Claude 兼容 Node 无沙箱）。

  两种模式的安全承诺完全不同：quickjs 只能收紧、不能改参数、无 IO；Claude 兼容
  可 allow/ask/deny、可 rewrite input、有完整 Node 能力。index.html 里写的是
  quickjs 口径（向后兼容），Claude 模式下必须整段换掉，否则用户看到的是与
  实际能力相反的安全承诺。 */
function renderModsModeText(state) {
  const claude = state.mode === "claude";
  const intro = document.getElementById("mods-intro");
  if (intro) {
    intro.innerHTML = claude
      ? `Mod 是<b>第三方编写的事件处理扩展</b>：挂在工具调用前后、权限请求与回合生命周期上。
         本模式兼容 <b>Claude Code 插件格式</b>（<code>.claude-plugin/plugin.json</code>），
         Mod 可直接返回 <b>allow / ask / deny</b> 并<b>改写工具参数</b>。`
      : `Mod 是<b>第三方编写的事件处理扩展</b>：挂在工具调用前后、权限请求与回合生命周期上，
         做观察、提示或<b>收紧</b>（拒绝 / 要求确认）。`;
  }
  const safety = document.getElementById("mods-hint-safety");
  if (safety) {
    safety.innerHTML = claude
      ? "<b>安全边界</b>：Mod 可放宽到自动放行、可改写参数——<b>只安装来源可信的 Mod</b>。引擎硬性守卫保留：出站密钥预拒绝、引擎主目录写入逐次确认、递归删除守卫。"
      : "<b>安全底线</b>：对权限门<b>只能收紧、永不放行</b>，也不能修改工具参数。";
  }
  const sandbox = document.getElementById("mods-hint-sandbox");
  if (sandbox) {
    sandbox.innerHTML = claude
      ? "<b>无沙箱</b>：Mod 跑在独立 Node.js 子进程里，可读写文件、发起网络请求、启动子进程、import npm 包。"
      : "代码跑在引擎侧沙箱，无文件 / 网络 / 进程能力。";
  }
  const uiHint = document.getElementById("mods-hint-ui");
  if (uiHint) {
    uiHint.innerHTML = claude
      ? "界面片段仍是声明式元素树（box / text / button / input / select / image），由固定渲染器画；Mod 不能自由绘制界面。"
      : "界面里的 Mod 片段是固定渲染器画的声明式小部件，Mod 无法自由绘制界面。";
  }
  const rootHint = document.getElementById("mods-root-hint");
  if (rootHint) {
    rootHint.replaceChildren();
    rootHint.appendChild(document.createTextNode("安装位置在本机用户目录的 "));
    const rcode = document.createElement("code");
    // 反斜杠用 fromCharCode 拼：模板字符串 + JS 字符串两层转义太容易把 \ 吃掉
    const BS = String.fromCharCode(92);
    rcode.textContent = ".skysheep" + BS + "mods" + BS;
    rootHint.appendChild(rcode);
    rootHint.appendChild(document.createTextNode(
      claude
        ? "；本模式认 Claude Code 插件标准位置 .claude-plugin/plugin.json（也兼容 mod.json）。安装来源是本机文件夹或 .zip 压缩包，安装前会先展示内容预览。"
        : "；安装来源是本机文件夹（含 mod.json）或 .zip 压缩包。安装前会先展示内容预览，确认后才落盘。",
    ));
  }
  const rt = document.getElementById("mods-runtime-status");
  if (rt) {
    if (state.runtime_available) {
      const label = claude ? "Node.js 运行时就绪" : "JS 沙箱运行时就绪";
      const ver = claude ? "Node.js" : "quickjs";
      rt.textContent = state.runtime_version
        ? `${label}（${ver} ${state.runtime_version}）`
        : label;
      rt.className = "card-status ok";
      rt.hidden = false;
    } else {
      rt.textContent = claude
        ? "Node.js 运行时不可用：Claude 兼容 Mod 不会执行（需安装 Node.js 18+ 并加入 PATH）。"
        : "JS 运行时不可用：Mod 的 JS 处理函数不会执行（清单与收紧声明仍生效）。";
      rt.className = "card-status bad";
      rt.hidden = false;
    }
  }
}

function renderModsList(mods) {
  const ul = document.getElementById("mods-list");
  if (!ul) return;
  if (!mods.length) {
    ul.innerHTML = '<li class="dim small">还没有安装任何 Mod。装官方示例试试，或点右上角「＋ 安装 Mod」。</li>';
    return;
  }
  ul.innerHTML = "";
  mods.forEach((m) => {
    const li = document.createElement("li");
    li.className = "mod-row";
    const hooks = (m.hooks || []).map((h) => MOD_HOOK_LABELS[h] || h).join(" / ");
    const decl = [];
    if (m.declarative && (m.declarative.deny_tools || []).length) {
      decl.push(`<span class="mod-decl">拒执行：${escapeHtml(m.declarative.deny_tools.join("、"))}</span>`);
    }
    if (m.declarative && (m.declarative.require_confirm_tools || []).length) {
      decl.push(`<span class="mod-decl">强制确认：${escapeHtml(m.declarative.require_confirm_tools.join("、"))}</span>`);
    }
    const flags = [];
    if (m.load_error) {
      flags.push(`<span class="chip danger-mark" title="${escapeHtml(m.load_error)}">装载失败</span>`);
    }
    if (m.auto_deactivated) {
      flags.push('<span class="chip danger-mark" title="连续超时 3 次，本进程内已停用；重启应用后恢复">超时自动停用</span>');
    }
    if (m.cc_mods) {
      flags.push('<span class="chip safe-mark" title="实验性兼容 Claude Code Mods 的清单与入口形状（子集）">CC 兼容 · 实验性</span>');
    }
    if (Number(m.errors) > 0) {
      // 运行期被丢弃/出错的动作数（本进程内）：解析丢弃的明细在「说明」的最近执行里（dropped 行）
      flags.push(`<span class="chip danger-mark" title="本进程内出错/被丢弃的动作累计 ${m.errors} 次；明细见「说明」的最近执行">错误 ${m.errors}</span>`);
    }
        li.innerHTML = `
      <label class="hook-enable" title="停用后保留安装，不再执行；重新勾选即恢复">
        <input type="checkbox" class="mod-enabled" ${m.enabled ? "checked" : ""}>启用
      </label>
      <span class="mod-main">
        <span class="item-name">${escapeHtml(m.name)} <span class="dim small">v${escapeHtml(m.version || "")} · ${escapeHtml(m.source || "")}</span></span>
        <span class="item-desc">${escapeHtml(m.description || "")}</span>
        <span class="dim small">${escapeHtml(hooks || "（未声明 hook）")} · ${escapeHtml(modPermLabel(m.permissions))}</span>
        ${decl.join(" ")}
        ${m.load_error ? `<span class="dim small mod-err">${escapeHtml(m.load_error)}</span>` : ""}
      </span>
      <span class="head-ops">${flags.join(" ")}
        <button class="btn-ghost rp-mini mod-detail">说明</button>
        <button class="btn-ghost rp-mini mod-del" title="删除这个 Mod">✕</button>
      </span>`;
    li.querySelector(".mod-enabled").onchange = async (e) => {
      try {
        await request("mods.toggle", { id: m.id, enabled: e.target.checked });
        modsStatus(`✓ ${m.name} 已${e.target.checked ? "启用" : "停用"}（立即生效）`);
      } catch (err) {
        modsStatus("✗ " + err.message, false);
      }
      renderModsCfg().catch(() => {});
    };
    li.querySelector(".mod-detail").onclick = () => showModDetail(m.id);
    li.querySelector(".mod-del").onclick = async () => {
      if (!(await confirmModal(`删除 Mod「${m.name}」？`, "<p>其运行状态也会一并删除。</p>"))) return;
      try {
        await request("mods.delete", { id: m.id });
        modsStatus(`✓ 已删除 ${m.name}`);
      } catch (err) {
        modsStatus("✗ " + err.message, false);
      }
      renderModsCfg().catch(() => {});
    };
    ul.appendChild(li);
  });
}

async function showModDetail(modId) {
  try {
    const d = await request("mods.get", { id: modId });
    const m = d.mod || {};
    const recent = d.recent || [];
    const box = document.createElement("div");
    box.innerHTML = `
      <p class="dim small">${escapeHtml(m.description || "")}</p>
      <p class="dim small">版本 ${escapeHtml(m.version || "")} · ${escapeHtml(m.source || "")} ·
        ${escapeHtml(modPermLabel(m.permissions))} ·
        挂点：${escapeHtml((m.hooks || []).map((h) => MOD_HOOK_LABELS[h] || h).join(" / ") || "无")}</p>
      <h4 class="mod-detail-cap">说明（README）</h4>
      <pre class="mod-readme">${escapeHtml(d.readme || "（没有 README.md）")}</pre>
      <h4 class="mod-detail-cap">最近执行（${recent.length}/24）</h4>
      <ul class="list mod-recent-list">${recent.length ? recent.slice().reverse().map((r) => {
        const pair = MOD_STATUS_META[r.status] || [r.status || "?", ""];
        const when = new Date((r.time || 0) * 1000).toLocaleTimeString();
        return `<li><span class="chip ${pair[1]}">${pair[0]}</span>` +
          `<span class="dim small">${escapeHtml(MOD_HOOK_LABELS[r.hook] || r.hook || "")} · ${when} · ${Number(r.duration_ms) || 0}ms</span>` +
          (r.output ? `<span class="mod-frag-text" title="${escapeHtml(r.output)}">${escapeHtml(r.output)}</span>` : "") + "</li>";
      }).join("") : '<li class="dim small">还没有执行记录。</li>'}</ul>`;
    showModal(`Mod · ${m.name || modId}`, box, async () => {}, "关闭");
  } catch (e) {
    modsStatus("✗ " + e.message, false);
  }
}

function renderOfficialMods(entries) {
  const grid = document.getElementById("mods-official-grid");
  if (!grid) return;
  if (!entries.length) {
    grid.innerHTML = '<span class="dim small">本机没有当前模式可用的官方示例（随应用安装包内置）。</span>';
    return;
  }
  grid.innerHTML = "";
  entries.forEach((m) => {
    const card = document.createElement("div");
    card.className = "gallery-item";
    // 格式徽标：让用户一眼看出这个示例用的是哪套 Mod API
    const fmtBadge = m.format === "cc"
      ? '<span class="item-badge">Claude Code 插件格式</span>'
      : '<span class="item-badge">SkySheep 格式</span>';
    card.innerHTML = `
      <span class="item-name">${escapeHtml(m.name || m.id)}</span>
      ${fmtBadge}
      <span class="item-desc">${escapeHtml(m.description || "")}</span>
      <button class="btn-ghost" ${m.installed ? "disabled" : ""}>${m.installed ? "已安装" : "一键安装"}</button>`;
    const btn = card.querySelector("button");
    if (!m.installed) {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await request("mods.install_official", { id: m.id });
          modsStatus(`✓ 已安装官方示例 ${m.name || m.id}（立即生效）`);
        } catch (e) {
          modsStatus("✗ " + e.message, false);
        }
        renderModsCfg().catch(() => {});
      };
    }
    grid.appendChild(card);
  });
}



// ---------- Claude Code 兼容模式：元素树渲染（P3） ----------
// 引擎侧已把 Mod 产出的数据收敛到白名单（core/mods_ui.py），这里只做落地。
// 铁律：任何第三方文本一律 textContent 赋值，绝不拼进 innerHTML。

const MOD_UI_TONE_CLASS = {
  normal: "", dim: "mod-tone-dim", ok: "mod-tone-ok",
  warn: "mod-tone-warn", danger: "mod-tone-danger",
};

/** 递归构建元素树节点；返回 Element 或 null。 */
function buildModNode(node) {
  if (!node || typeof node !== "object") return null;
  const type = node.type;
  if (type === "box") {
    const div = document.createElement("div");
    div.className = "mod-box mod-dir-" + (node.direction === "row" ? "row" : "column");
    (node.children || []).forEach((child) => {
      const el = buildModNode(child);
      if (el) div.appendChild(el);
    });
    return div;
  }
  if (type === "text") {
    const span = document.createElement("span");
    span.className = "mod-text " + (MOD_UI_TONE_CLASS[node.tone] || "");
    span.textContent = node.text || "";
    return span;
  }
  if (type === "button") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mod-btn mod-btn-" + (node.style || "normal");
    btn.textContent = node.label || "";
    btn.onclick = () => sendModAction(node.id, "click", null);
    return btn;
  }
  if (type === "input") {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "mod-input";
    input.value = node.value || "";
    input.placeholder = node.placeholder || "";
    input.onchange = () => sendModAction(node.id, "change", input.value);
    return input;
  }
  if (type === "select") {
    const sel = document.createElement("select");
    sel.className = "mod-select";
    (node.options || []).forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt.value || "";
      o.textContent = opt.label || opt.value || "";
      if (opt.value === node.value) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => sendModAction(node.id, "change", sel.value);
    return sel;
  }
  if (type === "image") {
    const img = document.createElement("img");
    img.className = "mod-image";
    img.alt = node.alt || "";
    img.src = node.src;
    if (node.width) img.width = node.width;
    if (node.height) img.height = node.height;
    return img;
  }
  return null;
}

/** Mod 界面交互回传（P3 后端尚未实现该方法，失败静默）。 */
function sendModAction(actionId, kind, value) {
  if (typeof request !== "function") return;
  request("mods_claude.action", {
    action_id: actionId, kind: kind, value: value,
    mod_id: (activeTab && activeTab.lastModId) || "",
    session_id: (activeTab && activeTab.sessionId) || "",
  }).catch(() => {});
}

/** 把一棵元素树挂进容器（先清空）。 */
function renderModTree(container, tree, modId) {
  if (!container) return;
  container.replaceChildren();
  const el = buildModNode(tree);
  if (!el) return;
  const wrap = document.createElement("div");
  wrap.className = "mod-tree";
  const cap = document.createElement("span");
  cap.className = "mod-frag-src";
  cap.textContent = "Mod·" + (modId || "");
  wrap.appendChild(cap);
  wrap.appendChild(el);
  container.appendChild(wrap);
}

/** 区域空/非空时切 hidden（避免占位留白）。 */
function refreshModRegions() {
  ["mod-pane-host", "mod-above-prompt"].forEach((id) => {
    const host = document.getElementById(id);
    if (!host) return;
    host.classList.toggle("hidden", host.children.length === 0);
  });
}

/** Pane 区域：常驻侧栏/底部面板，每 Mod 一个槽。 */
function renderModPane(modId, tree) {
  const host = document.getElementById("mod-pane-host");
  if (!host) return;
  let slot = host.querySelector('[data-mod-pane="' + CSS.escape(modId) + '"]');
  if (!slot) {
    slot = document.createElement("div");
    slot.className = "mod-pane";
    slot.dataset.modPane = modId;
    host.appendChild(slot);
  }
  renderModTree(slot, tree, modId);
  refreshModRegions();
}

/** AbovePrompt 区域：输入框上方的常驻条。 */
function renderModAbovePrompt(modId, tree) {
  const host = document.getElementById("mod-above-prompt");
  if (!host) return;
  let slot = host.querySelector('[data-mod-above="' + CSS.escape(modId) + '"]');
  if (!slot) {
    slot = document.createElement("div");
    slot.className = "mod-above";
    slot.dataset.modAbove = modId;
    host.appendChild(slot);
  }
  renderModTree(slot, tree, modId);
  refreshModRegions();
}

/** 对话框区域：模态浮层（每 Mod 一个，点背景/关闭按钮收起）。 */
function renderModDialog(modId, tree) {
  const host = document.getElementById("mod-dialog-host");
  if (!host) return;
  let slot = host.querySelector('[data-mod-dialog="' + CSS.escape(modId) + '"]');
  if (!slot) {
    slot = document.createElement("div");
    slot.className = "mod-dialog";
    slot.dataset.modDialog = modId;
    // 点背景关闭（点卡片内部不关）
    slot.addEventListener("click", (ev) => {
      if (ev.target === slot) closeModDialog(modId);
    });
    host.appendChild(slot);
  }
  slot.replaceChildren();
  const card = document.createElement("div");
  card.className = "mod-dialog-card";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "mod-dialog-close";
  close.textContent = "✕";
  close.title = "关闭";
  close.onclick = () => closeModDialog(modId);
  card.appendChild(close);
  const cap = document.createElement("span");
  cap.className = "mod-frag-src";
  cap.textContent = "Mod·" + modId;
  card.appendChild(cap);
  const el = buildModNode(tree);
  if (el) card.appendChild(el);
  slot.appendChild(card);
  host.classList.remove("hidden");
}

function closeModDialog(modId) {
  const host = document.getElementById("mod-dialog-host");
  if (!host) return;
  const slot = host.querySelector('[data-mod-dialog="' + CSS.escape(modId) + '"]');
  if (slot) slot.remove();
  if (!host.children.length) host.classList.add("hidden");
}

/** 元素树形态的 mod_ui 事件入口（旧的 widget 词表路径不受影响）。 */
function onModUiTree(data) {
  const payload = data.payload || {};
  const slot = payload.slot || "stream";
  const tree = payload.tree;
  const modId = data.mod_id || "";
  if (!tree) return;
  if (slot === "pane") { renderModPane(modId, tree); return; }
  if (slot === "above_prompt") { renderModAbovePrompt(modId, tree); return; }
  if (slot === "dialog") { renderModDialog(modId, tree); return; }
  if (slot === "perm") {
    const sid = data.session_id || "";
    if (!pendingPermModLines.has(sid)) pendingPermModLines.set(sid, []);
    pendingPermModLines.get(sid).push({ tree, modId });
    return;
  }
  // stream / tray 兜底：当作一条流内片段
  const t = (typeof routeTab !== "undefined" && routeTab) || activeTab;
  if (!t || !t.logEl) return;
  const div = document.createElement("div");
  div.className = "mod-fragment";
  const el = buildModNode(tree);
  if (el) {
    const cap = document.createElement("span");
    cap.className = "mod-frag-src";
    cap.textContent = "Mod·" + modId;
    div.appendChild(cap);
    div.appendChild(el);
    t.logEl.appendChild(div);
    if (typeof scrollLog === "function") scrollLog();
  }
}
