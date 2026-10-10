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
};

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
      official: off.mods || [], bundled: !!off.bundled,
    };
    const tg = document.getElementById("mods-toggle");
    if (tg) tg.checked = modsState.enabled;
    // 安装位置不再由后端下发（mods.list 是远程可读方法，不回本机绝对路径），
    // 固定文案在 index.html 的 mods-root-hint 里
    const rt = document.getElementById("mods-runtime-status");
    if (rt) {
      if (modsState.runtime_available) {
        rt.textContent = modsState.runtime_version
          ? `JS 沙箱运行时就绪（quickjs ${modsState.runtime_version}）`
          : "JS 沙箱运行时就绪";
        rt.className = "card-status ok";
        rt.hidden = false;
      } else {
        rt.textContent = "JS 运行时不可用：Mod 的 JS 处理函数不会执行（清单与收紧声明仍生效）。";
        rt.className = "card-status bad";
        rt.hidden = false;
      }
    }
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
        <span class="dim small">${escapeHtml(hooks || "（未声明 hook）")} · ${m.permissions === "tighten" ? "收紧档（可拒绝/要求确认）" : "观察档（仅观察与提示）"}</span>
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
        ${m.permissions === "tighten" ? "收紧档" : "观察档"} ·
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
    grid.innerHTML = '<span class="dim small">本机没有官方示例清单（随应用安装包内置）。</span>';
    return;
  }
  grid.innerHTML = "";
  entries.forEach((m) => {
    const card = document.createElement("div");
    card.className = "gallery-item";
    card.innerHTML = `
      <span class="item-name">${escapeHtml(m.name || m.id)}</span>
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
