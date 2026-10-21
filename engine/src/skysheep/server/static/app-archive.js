// ---------- 设置 · 归档管理 ----------
// 与侧栏「归档会话」弹窗分工：弹窗只看当前项目、单发恢复；这里跨全部项目
// 管理——自动归档开关与保留时长（即改即存）、按项目分组的归档列表
// （搜索 / 排序 / 项目筛选 / 按组或全部恢复删除）。
// 后端方法：archive.status / archive.save / archive.list / archive.restore / archive.delete。

let _archiveMgr = null; // archive.list 的缓存 {sessions, projects}
const _archiveExpanded = new Set(); // 展开的项目组（project_id 字符串化；"null" = 快聊组）

function archiveAutoStatus(text, ok = true) {
  const el = document.getElementById("archive-auto-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

async function renderArchiveAutoCfg() {
  const q = (id) => document.getElementById(id);
  let d;
  try { d = await request("archive.status"); }
  catch (e) { archiveAutoStatus("加载失败：" + e.message, false); return; }
  q("archive-auto-toggle").checked = !!d.auto_archive;
  // 天数不在预设档位（手改过 config.toml）时补一个临时选项，别让它显示成空白
  const sel = q("archive-auto-days");
  const days = String(d.auto_after_days || 14);
  if (![...sel.options].some((o) => o.value === days)) {
    const o = document.createElement("option");
    o.value = days;
    o.textContent = `${days} 天后归档`;
    sel.appendChild(o);
  }
  sel.value = days;
  archiveAutoStatus("");
}

async function saveArchiveAuto(what) {
  try {
    await request("archive.save", {
      auto_archive: document.getElementById("archive-auto-toggle").checked,
      auto_after_days: Number(document.getElementById("archive-auto-days").value),
    });
    archiveAutoStatus("✓ 已保存并立即生效");
  } catch (e) {
    archiveAutoStatus(`✗ 保存失败：${e.message}`, false);
    renderArchiveAutoCfg().catch(() => {}); // 失败回读：控件回到真实配置
  }
}

function archiveMgrStatus(box, text) {
  box.innerHTML = `<p class="dim small">${text}</p>`;
}

async function loadArchiveMgr() {
  const box = document.getElementById("archive-mgr-list");
  try { _archiveMgr = await request("archive.list"); }
  catch (e) { archiveMgrStatus(box, `加载失败：${escapeHtml(e.message)}`); return; }
  // 项目筛选下拉：所有项目 + 各项目（快聊的归档会话在「快聊」组里）
  const ps = _archiveMgr.projects || [];
  const sel = document.getElementById("archive-mgr-project");
  const cur = sel.value;
  sel.innerHTML = '<option value="">所有项目</option>' +
    ps.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  renderArchiveMgr();
}

function renderArchiveMgr() {
  const box = document.getElementById("archive-mgr-list");
  const purge = document.getElementById("btn-archive-purge");
  if (!_archiveMgr) return;
  const all = _archiveMgr.sessions || [];
  purge.hidden = all.length === 0;

  const q = document.getElementById("archive-mgr-search").value.trim().toLowerCase();
  const sort = document.getElementById("archive-mgr-sort").value;
  const pid = document.getElementById("archive-mgr-project").value;
  let list = all.filter((s) => !pid || String(s.project_id ?? "null") === String(pid));
  if (q) list = list.filter((s) => (s.title || "").toLowerCase().includes(q));
  if (sort === "title") {
    list.sort((a, b) => (a.title || "").localeCompare(b.title || "", "zh"));
  } else {
    const key = sort === "created" ? "created_at" : "updated_at";
    list.sort((a, b) => (b[key] || 0) - (a[key] || 0));
  }
  if (!all.length) {
    archiveMgrStatus(box, "没有已归档的会话。右键会话（或点 ⋯）选「归档」，它就会从侧栏消失并收进这里。");
    return;
  }
  if (!list.length) {
    archiveMgrStatus(box, q ? `没有标题包含「${escapeHtml(q)}」的已归档会话。` : "这个项目没有已归档的会话。");
    return;
  }

  // 按项目分组（project_id 为空 = 快聊），组按组内最新一条排序
  const names = new Map((_archiveMgr.projects || []).map((p) => [String(p.id), p.name]));
  const groups = new Map();
  list.forEach((s) => {
    const k = String(s.project_id ?? "null");
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(s);
  });
  const order = [...groups.entries()].sort((a, b) => {
    const ta = Math.max(...a[1].map((s) => s.updated_at || 0));
    const tb = Math.max(...b[1].map((s) => s.updated_at || 0));
    return tb - ta;
  });

  box.innerHTML = "";
  order.forEach(([k, sessions]) => {
    const name = k === "null" ? "快聊" : (names.get(k) || `项目 #${k}`);
    box.appendChild(renderArchiveGroup(k, name, sessions));
  });
}

function renderArchiveGroup(key, name, sessions) {
  const expanded = _archiveExpanded.has(key);
  const grp = document.createElement("div");
  grp.className = "arch-grp" + (expanded ? " open" : "");
  const head = document.createElement("div");
  head.className = "arch-grp-head";
  head.innerHTML =
    `<span class="arch-grp-arrow">›</span>${svgIcon("folder")}` +
    `<b class="arch-grp-name">${escapeHtml(name)}</b>` +
    `<span class="arch-grp-count">${sessions.length} 个会话</span>` +
    '<button class="arch-grp-more" title="更多操作">⋯</button>';
  head.onclick = (e) => {
    if (e.target.closest(".arch-grp-more")) return;
    _archiveExpanded.has(key) ? _archiveExpanded.delete(key) : _archiveExpanded.add(key);
    renderArchiveMgr();
  };
  head.querySelector(".arch-grp-more").onclick = (e) => {
    e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    showArchiveGroupMenu(name, sessions, { x: r.right - 150, y: r.bottom + 4 });
  };
  grp.appendChild(head);
  if (expanded) {
    const rows = document.createElement("div");
    rows.className = "arch-grp-rows";
    sessions.forEach((s) => rows.appendChild(renderArchiveRow(s)));
    grp.appendChild(rows);
  }
  return grp;
}

// 组级菜单复用全局弹层（#session-menu + placeMenuAt）：一次只开一个菜单，
// 点击外部自动关（app.js 的 document click 监听负责）
function showArchiveGroupMenu(name, sessions, pos) {
  const ids = sessions.map((s) => s.id);
  const items = [
    {
      icon: "archive", label: `恢复全部（${ids.length} 个）`,
      act: () => archiveBatchRestore(ids, `已恢复 ${name} 的 ${ids.length} 个会话`),
    },
    {
      icon: "trash", label: `删除全部（${ids.length} 个）`, danger: true,
      act: async () => {
        if (!(await confirmModal(
          `删除「${name}」的 ${ids.length} 个已归档会话？\n\n删除后无法恢复，确定继续？`,
          { title: "确认删除", okLabel: "删除", danger: true }))) return;
        archiveBatchDelete(ids);
      },
    },
  ];
  menuEl.innerHTML = "";
  items.forEach((it) => {
    const b = document.createElement("button");
    b.innerHTML = svgIcon(it.icon) + `<span>${escapeHtml(it.label)}</span>`;
    if (it.danger) b.classList.add("danger");
    b.onclick = () => { menuEl.classList.add("hidden"); it.act(); };
    menuEl.appendChild(b);
  });
  menuEl.classList.remove("hidden");
  placeMenuAt(menuEl, pos);
}

function renderArchiveRow(s) {
  const row = document.createElement("div");
  row.className = "arch-row";
  row.innerHTML =
    `<div class="arch-row-main"><b>${escapeHtml(s.title || "(未命名)")}</b>` +
    `<span class="arch-row-time">最近活跃：${fmtRuleAge(s.updated_at)}</span></div>`;
  const ops = document.createElement("div");
  ops.className = "arch-row-ops";
  const restore = document.createElement("button");
  restore.className = "rp-mini";
  restore.textContent = "恢复";
  restore.onclick = async () => {
    restore.disabled = true;
    try {
      await request("session.archive", { id: s.id, archived: false });
      addNotice(`已恢复「${s.title || "(未命名)"}」`);
      refreshSessions();
      await loadArchiveMgr();
    } catch (e) { addNotice("恢复失败: " + e.message); restore.disabled = false; }
  };
  const del = document.createElement("button");
  del.className = "rp-mini danger";
  del.textContent = "删除";
  del.onclick = async () => {
    if (!(await confirmModal(
      `删除已归档会话「${s.title || "(未命名)"}」？\n\n删除后无法恢复，确定继续？`,
      { title: "确认删除", okLabel: "删除", danger: true }))) return;
    del.disabled = true;
    try {
      await request("session.delete", { id: s.id });
      closeTabsForSids([s.id]);
      addNotice(`已删除「${s.title || "(未命名)"}」`);
      await loadArchiveMgr();
    } catch (e) { addNotice("删除失败: " + e.message); del.disabled = false; }
  };
  ops.appendChild(restore);
  ops.appendChild(del);
  row.appendChild(ops);
  return row;
}

async function archiveBatchRestore(ids, noticeText) {
  try {
    const r = await request("archive.restore", { ids });
    addNotice(r.restored > 0 ? (noticeText || `已恢复 ${r.restored} 个会话`) : "没有可恢复的会话");
    refreshSessions();
    await loadArchiveMgr();
  } catch (e) { addNotice("恢复失败: " + e.message); }
}

async function archiveBatchDelete(ids) {
  try {
    const r = await request("archive.delete", { ids });
    closeTabsForSids(r.ids || []);
    const failed = r.failed ? `，${r.failed} 个失败` : "";
    addNotice(r.deleted > 0 ? `已删除 ${r.deleted} 个会话${failed}` : "没有可删除的会话");
    refreshSessions();
    await loadArchiveMgr();
  } catch (e) { addNotice("删除失败: " + e.message); }
}

async function loadArchivePage() {
  renderArchiveAutoCfg().catch(() => {});
  loadArchiveMgr().catch(() => {});
}

// 事件绑定：配置即改即存；工具行输入即筛。绑定放 DOMContentLoaded 之后
// （本文件先于 app.js 加载，但绑定只依赖本页元素，不必等 app.js）
document.getElementById("archive-auto-toggle").onchange = () => saveArchiveAuto();
document.getElementById("archive-auto-days").onchange = () => saveArchiveAuto();
document.getElementById("archive-mgr-search").addEventListener("input", () => {
  if (_archiveMgr) renderArchiveMgr();
});
document.getElementById("archive-mgr-sort").onchange = () => { if (_archiveMgr) renderArchiveMgr(); };
document.getElementById("archive-mgr-project").onchange = () => { if (_archiveMgr) renderArchiveMgr(); };
document.getElementById("btn-archive-purge").onclick = async () => {
  const n = _archiveMgr ? (_archiveMgr.sessions || []).length : 0;
  if (!n) return;
  if (!(await confirmModal(
    `删除全部 ${n} 个已归档会话？\n\n删除后无法恢复，确定继续？`,
    { title: "确认删除", okLabel: "全部删除", danger: true }))) return;
  try {
    const r = await request("archive.delete", {});
    closeTabsForSids(r.ids || []);
    addNotice(r.deleted > 0 ? `已删除 ${r.deleted} 个会话` : "没有可删除的会话");
    refreshSessions();
    await loadArchiveMgr();
  } catch (e) { addNotice("删除失败: " + e.message); }
};
