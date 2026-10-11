// app-about.js —— 设置 · 关于页：更新检查 + 打开目录 / 诊断包 / 反馈入口 /
// 会话库备份恢复 / 数据管理（保留策略）/ 存储占用与表级清理
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有函数声明与状态常量，零加载期执行，按钮接线仍留在 app.js 原位；
// request / showModal / escapeHtml 等助手是运行时调用。

// ---------- 设置 · 关于：更新检查 ----------
const REPO_PAGE = "https://github.com/Sky-scrape/SkySheep";
const RELEASES_PAGE = REPO_PAGE + "/releases";
// 更新渠道偏好缓存（stable/beta）：loadBackups 的 ui.get 回填，渲染状态行时用；
// 未回填前按空串渲染，不显示渠道字样（不猜）
let updateChannelPref = "";
function channelLabel() {
  return updateChannelPref === "beta" ? "测试版渠道" : "稳定版渠道";
}
function renderUpdatePanel(snap) {
  const box = document.getElementById("about-update");
  if (!box) return;
  const cur = snap.version || "";
  const frozen = !!snap.frozen; // 安装版=可在应用内静默更新；源码版提示 git pull
  const edition = frozen ? "安装版" : "源码版"; // 排障时的关键信息，随版本号一起亮出来
  const upd = snap.update || null;
  // 上次更新的结果对账（后端启动时结算）：失败亮现场直到「知道了」，成功提示一次
  const fail = snap.update_failure || null;
  const failHtml = fail ? `
    <div class="card-status bad upd-fail" id="upd-fail">
      ⚠ 上次更新未完成${fail.target ? `（目标 v${escapeHtml(fail.target)}）` : ""}：当前仍运行 v${escapeHtml(cur)}。
      更新前的用户数据备份在${fail.backup_dir
        ? ` <span class="copyable-path">${escapeHtml(fail.backup_dir)}</span>`
        : "（未生成）"}；安装详情见 logs/update-setup.log。
      可到 <a href="${RELEASES_PAGE}" target="_blank">Releases 页</a> 手动下载安装包重新安装。
      <button class="btn-ghost" id="btn-upd-fail-dismiss">知道了</button>
    </div>` : "";
  const okHtml = snap.update_success
    ? `<div class="card-status ok">✓ 上次更新完成：v${escapeHtml(snap.update_success.from || "?")} → v${escapeHtml(snap.update_success.to)}</div>`
    : "";
  box.innerHTML = failHtml + okHtml + `
    <button class="btn-ghost" id="btn-check-update">检查更新</button>
    <span class="upd-state ${upd ? "new" : ""}" id="upd-state">${
      upd
        ? `🆕 发现新版本 v${escapeHtml(upd.version)}（当前 v${escapeHtml(cur)} · ${edition}${updateChannelPref ? ` · ${channelLabel()}` : ""}）`
        : `当前版本 v${escapeHtml(cur)} · ${edition}${updateChannelPref ? ` · ${channelLabel()}` : ""}`
    }</span>
    <span id="upd-actions"></span>`;
  const failBtn = box.querySelector("#btn-upd-fail-dismiss");
  if (failBtn) failBtn.onclick = async () => {
    try {
      await request("app.dismiss_update_failure");
    } catch (e) {
      addNotice("操作失败：" + e.message);
      return;
    }
    const card = box.querySelector("#upd-fail");
    if (card) card.remove();
    addNotice("已收起未完成更新的提醒；数据备份目录仍在，需要时可手动回退");
  };
  const state = box.querySelector("#upd-state");
  const actions = box.querySelector("#upd-actions");

  // 一键更新：下载最新安装包 → 退出并静默安装（仅安装版；源码版提示 git pull）
  const setUpdButtons = (version) => {
    actions.innerHTML = "";
    const link = `<a href="${RELEASES_PAGE}" target="_blank">前往下载页</a>`;
    if (!frozen) {
      actions.innerHTML = `<span class="dim small">源码版请在仓库里 git pull 后重启更新，或${link}</span>`;
      return;
    }
    const b = document.createElement("button");
    b.className = "btn-ghost";
    b.textContent = `⬇ 一键更新到 v${escapeHtml(version)}`;
    b.onclick = async () => {
      b.disabled = true;
      state.textContent = "正在下载更新包…";
      try {
        const r = await request("app.install_update");
        if (!r.update_available) {
          state.textContent = `已是最新版本（v${escapeHtml(r.current)}）`;
          actions.innerHTML = "";
          return;
        }
        // 校验透明度（对抗审查联动项；字段契约：verified 布尔，后端下载时探测
        // 同名 .sha256 附件并核对，remote.py 已有该字段、无需后端改动）：只有明确
        // verified === true 才算核对过；false 或旧后端没带这个字段一律按「未核对」
        // 处理，明确告诉用户，别让人默认装的是核对过的包。
        const shaOk = r.verified === true;
        state.textContent = (r.uac
          ? "下载完成。本机是「为所有用户」安装，点「退出并安装」后应用会先退出，"
            + "紧接着系统弹出授权窗口（用户账户控制）——请点「是」，装完自动重新打开。"
          : "下载完成。点「退出并安装」后应用会自动退出并静默安装，装完自动重新打开。")
          + (shaOk ? "" : " ⚠ 未能核对安装包校验值（发布页缺 .sha256 校验附件或核对未"
            + "通过），无法确认安装包未被篡改；谨慎起见可到下载页手动核对 SHA-256 后再装。");
        const b2 = document.createElement("button");
        b2.className = "btn-ghost";
        b2.textContent = "退出并安装";
        b2.onclick = async () => {
          b2.disabled = true;
          state.textContent = "正在退出并启动安装程序…装完会自动重新打开 SkySheep。";
          if (r.uac) {
            // 应用马上退出，界面上的提示留不住：补一条系统通知（停在通知中心），
            // 提醒用户去点稍后出现的授权窗口
            request("app.notify", {
              title: "正在更新 SkySheep",
              body: `请在系统授权窗口（用户账户控制）中点「是」；装完会自动打开 v${version}。`,
            }).catch(() => {});
            // 授权窗口紧随应用退出出现，给通知与消息送达留一拍
            await new Promise((ok) => setTimeout(ok, 900));
          }
          try {
            await request("app.apply_update");
          } catch (e) {
            state.textContent = "启动安装失败：" + e.message;
            b2.disabled = false;
          }
        };
        actions.innerHTML = "";
        actions.appendChild(b2);
      } catch (e) {
        state.textContent = "更新失败：" + e.message;
        b.disabled = false;
      }
    };
    actions.appendChild(b);
    actions.insertAdjacentHTML("beforeend", `<span class="dim small">或${link}</span>`);
  };

  if (upd && upd.version) setUpdButtons(upd.version);

  box.querySelector("#btn-check-update").onclick = async () => {
    state.textContent = "正在检查更新…";
    state.className = "upd-state";
    actions.innerHTML = "";
    try {
      const r = await request("app.check_update");
      if (r.channel === "beta" || r.channel === "stable") updateChannelPref = r.channel;
      if (r.available) {
        state.innerHTML = `🆕 发现新版本 v${escapeHtml(r.version)}（当前 v${escapeHtml(r.current)} · ${edition} · ${channelLabel()}）`;
        state.className = "upd-state new";
        setUpdButtons(r.version);
      } else if (r.error) {
        state.textContent = "检查失败：" + r.error;
      } else {
        state.textContent = `已是最新版本（v${escapeHtml(r.current)} · ${edition} · ${channelLabel()}）`;
      }
    } catch (e) {
      state.textContent = "检查失败：" + e.message;
    }
  };
}

// ---------- 关于：打开目录 / 诊断包 / 会话库备份恢复 ----------
async function openFixedPath(kind, label) {
  try {
    const r = await request("app.open_path", { kind });
    addNotice(`已打开${label}：${r.path}`);
  } catch (e) {
    addNotice(`打开${label}失败：` + e.message);
  }
}

// 反馈流程（共享核心）：打包脱敏日志 + 打开 GitHub 反馈页，用户把 zip 拖进附件即可。
// 设置 · 关于「反馈问题」与 记忆地图工具栏「反馈」两处共用；诊断包失败不阻塞打开反馈页。
async function openFeedbackPage() {
  try {
    await request("app.export_diagnostics");
  } catch (e) { /* 诊断包失败不阻塞打开反馈页 */ }
  const opened = await request("app.open_external", {
    target: REPO_PAGE + "/issues/new?template=bug_report.md",
  });
  // 局域网/远程访问时服务端不代开（审查 P1-3）：链接交回本端浏览器
  if (opened && opened.remote) {
    window.open(opened.url, "_blank", "noopener");
  }
}

function fmtBackupTime(ts) {
  // 精确到秒：与备份行（文件名时间戳）同一粒度，列表里两种时间才对得上
  const d = new Date(ts * 1000);
  const pad = pad2;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// 备份列表：默认收起只列「当前数据 + 最近几份」，其余收进末尾的展开按钮

// 备份名里的时间戳（20260917-084701 → 2026-09-17 08:47:01）；「恢复前」副本只看前缀
function fmtBackupStamp(stamp, fallbackTs) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(String(stamp || ""));
  if (!m) return fmtBackupTime(fallbackTs);
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

let backupsExpanded = false; // 展开状态在本页内存里保持；重新加载后回到默认收起
const BACKUP_PREVIEW = 4; // 收起时展示的最近备份数

function backupRowHtml(b) {
  // 时间取 b.taken（解析自文件名的备份时刻）：备份文件的 mtime 是复制时带过来的源库
  // 修改时间，20 份备份会显示成同一个时刻。当前数据没有备份时刻，只能标它的写入时间。
  const size = `${(b.size / 1024).toFixed(0)} KB`;
  const when = b.current ? "当前数据" : fmtBackupStamp(b.stamp, b.taken || b.mtime);
  const meta = b.current ? `最后写入 ${fmtBackupTime(b.mtime)} · ${size}` : size;
  const tag = b.safety ? '<span class="backup-tag">恢复前</span>' : "";
  return `<li class="backup-row${b.current ? " cur" : ""}">
      <span class="item-name">${when}</span>${tag}
      <span class="dim small">${meta}</span>
      <span class="spacer"></span>
      ${b.current ? "" : `<button class="btn-ghost" data-restore="${escapeHtml(b.name)}" ` +
        `data-when="${escapeHtml(when)}" data-size="${b.size || 0}">恢复到此版本</button>` +
        `<button class="btn-ghost danger" data-del="${escapeHtml(b.name)}" ` +
        `data-when="${escapeHtml(when)}" title="删除这份备份（不影响当前会话数据）">删除</button>`}
    </li>`;
}

async function loadBackups() {
  // 自动检查更新开关 + 更新渠道：进关于页时从偏好回填（保存即生效）
  request("ui.get").then((ui) => {
    const t = document.getElementById("update-check-toggle");
    if (t) t.checked = !ui || ui.prefs.update_check == null ? true : ui.prefs.update_check === 1;
    const sel = document.getElementById("update-channel");
    updateChannelPref = (ui && ui.prefs.update_channel) || "stable";
    if (sel) sel.value = updateChannelPref;
  }).catch(() => {});
  const ul = document.getElementById("backup-list");
  let d;
  try { d = await request("session.backups"); } catch (e) {
    ul.innerHTML = `<li class="dim small">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const items = d.backups || [];
  const cur = items.filter((b) => b.current);
  const hist = items.filter((b) => !b.current);
  // 默认收起：只列「当前数据 + 最近几份」，卡片不再被 20 行撑满；
  // 其余收进末尾的展开/收起按钮（样式在 .backup-more）
  const shown = backupsExpanded ? hist : hist.slice(0, BACKUP_PREVIEW);
  const rows = cur.concat(shown).map(backupRowHtml);
  const hidden = hist.length - shown.length;
  if (hidden > 0) {
    rows.push(`<li class="backup-more"><button class="btn-ghost" data-more="1">▾ 展开其余 ${hidden} 份备份</button></li>`);
  } else if (backupsExpanded && hist.length > BACKUP_PREVIEW) {
    rows.push(`<li class="backup-more"><button class="btn-ghost" data-more="1">▴ 收起备份列表</button></li>`);
  }
  ul.className = "list" + (backupsExpanded ? " expanded" : "");
  ul.innerHTML = rows.join("") || '<li class="dim small">还没有备份（应用下次启动时会自动生成一份）</li>';
  ul.querySelectorAll("[data-restore]").forEach((btn) => {
    btn.onclick = () => restoreBackupConfirm(btn.dataset.restore, btn.dataset.when, btn, Number(btn.dataset.size || 0));
  });
  ul.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => deleteBackupConfirm(btn.dataset.del, btn.dataset.when, btn);
  });
  ul.querySelectorAll("[data-more]").forEach((btn) => {
    btn.onclick = () => { backupsExpanded = !backupsExpanded; loadBackups().catch(() => {}); };
  });
  const bs = document.getElementById("backup-status");
  const totalKB = hist.reduce((s, b) => s + (b.size || 0), 0) / 1024;
  const totalTxt = totalKB >= 1024 ? (totalKB / 1024).toFixed(2) + " MB" : totalKB.toFixed(0) + " KB";
  bs.textContent = `共 ${hist.length} 份备份 · 共占 ${totalTxt} · 备份目录：${d.dir}（保留最近 ${d.keep} 份）`;
  bs.className = "card-status ok";
  bs.hidden = false;
}

function restoreBackupConfirm(name, when, btn, size = 0) {
  const box = document.createElement("div");
  const sizeTxt = size > 0 ? `（${(size / 1024).toFixed(0)} KB）` : "";
  box.innerHTML = `<p>要用 <b>${escapeHtml(when || name)}</b>${sizeTxt} 这份备份覆盖当前会话数据吗？</p>
    <p class="dim small">备份文件：${escapeHtml(name)}<br>
    当前数据会先自动另存为一份「恢复前」备份，所以还能再换回来；
    恢复后需要刷新界面（会话列表会回到那个时间点的样子）。</p>`;
  showModal("恢复会话库备份", box, async () => {
    btn.disabled = true;
    try {
      const r = await request("session.restore_backup", { name });
      addNotice(`✓ 已恢复到 ${r.restored}` + (r.safety_copy ? "（恢复前的数据已另存一份）" : ""));
      await boot(); // 会话/项目列表按恢复后的库重画
      await loadBackups();
    } catch (e) {
      addNotice("恢复失败：" + e.message);
      btn.disabled = false;
    }
  }, "恢复");
}

function deleteBackupConfirm(name, when, btn) {
  const box = document.createElement("div");
  box.innerHTML = `<p>要删除 <b>${escapeHtml(when || name)}</b> 这份备份吗？</p>
    <p class="dim small">备份文件：${escapeHtml(name)}<br>
    删除后不可找回；当前会话数据不受影响。</p>`;
  showModal("删除会话库备份", box, async () => {
    btn.disabled = true;
    try {
      await request("session.delete_backup", { name });
      await loadBackups();
    } catch (e) {
      addNotice("删除失败：" + e.message);
      btn.disabled = false;
    }
  }, "删除");
}

// ---------- 设置 · 关于：数据管理（截图副本 / 隔离区 / 子代理报告的保留策略） ----------
function retentionStatus(text, ok = true) {
  const el = document.getElementById("retention-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 字节数 → 人读大小（B/KB/MB/GB）；只有这一处用，不值得进公共工具
function retentionBytes(n) {
  const units = ["B", "KB", "MB", "GB"];
  let v = Number(n) || 0, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i ? v.toFixed(1).replace(/\.0$/, "") : String(v)) + " " + units[i];
}

async function renderRetentionCfg() {
  let d;
  try { d = await request("retention.status"); } catch (e) {
    retentionStatus("加载失败：" + e.message, false);
    return;
  }
  const q = (id) => document.getElementById(id);
  q("retention-screenshots").value = d.days.screenshots;
  q("retention-quarantine").value = d.days.quarantine;
  q("retention-reports").value = d.days.reports;
  // 每类当前占用随行展示；天数填 0（该类关闭清理）的行点一句说明
  const u = d.usage || {};
  q("retention-usage-screenshots").textContent = u.screenshots
    ? `当前 ${u.screenshots.files} 个文件，共 ${retentionBytes(u.screenshots.bytes)}`
    : "";
  q("retention-usage-quarantine").textContent = u.quarantine
    ? `当前 ${u.quarantine.files} 个文件，共 ${retentionBytes(u.quarantine.bytes)}`
    : "";
  q("retention-usage-reports").textContent = u.reports
    ? `当前 ${u.reports.files} 个文件，共 ${retentionBytes(u.reports.bytes)}`
    : "";
  const note = q("retention-usage-note");
  const closed = ["screenshots", "quarantine", "reports"].filter((k) => !(d.days[k] > 0));
  const labels = { screenshots: "截图副本", quarantine: "隔离区", reports: "子代理报告" };
  if (closed.length === 3) {
    note.textContent = "三类都已关闭自动清理（天数为 0），文件会一直累积；只跑上面的「立即清理」。";
    note.hidden = false;
  } else if (closed.length) {
    note.textContent = `已关闭自动清理：${closed.map((k) => labels[k]).join("、")}（天数为 0）。`;
    note.hidden = false;
  } else {
    note.textContent = d.last_sweep_date ? `上次自动清理：${d.last_sweep_date}。` : "";
    note.hidden = !d.last_sweep_date;
  }
  retentionStatus("");
}

async function saveRetention() {
  const q = (id) => document.getElementById(id);
  // 清空的输入框传 null（后端按「不动该项」处理），绝不折成 0——
  // 0 的语义是「关闭该类自动清理」，手滑清空不该变成永久关闭
  const days = (id) => {
    const raw = q(id).value.trim();
    return raw === "" ? null : Number(raw);
  };
  try {
    await request("retention.save", {
      screenshots_days: days("retention-screenshots"),
      quarantine_days: days("retention-quarantine"),
      reports_days: days("retention-reports"),
    });
    await renderRetentionCfg();
    retentionStatus("✓ 已保存并立即生效");
  } catch (e) {
    retentionStatus("✗ 保存失败：" + e.message, false);
  }
}

// ---------- 设置 · 关于：存储占用与表级清理 ----------
function storageStatus(text, ok = true) {
  const el = document.getElementById("storage-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

async function renderStorageCard() {
  const list = document.getElementById("storage-overview");
  let d;
  try { d = await request("storage.usage"); } catch (e) {
    list.innerHTML = `<li class="empty-hint">加载失败：${escapeHtml(e.message)}</li>`;
    return;
  }
  const rows = [
    `<li class="dim">会话数据库：<b>${retentionBytes(d.db_bytes)}</b>　备份目录：${retentionBytes(d.backups_bytes)}　日志：${retentionBytes(d.logs_bytes)}</li>`,
  ];
  for (const t of d.tables || []) {
    if (t.rows <= 0) continue; // 空表不占行：少即是多
    rows.push(
      `<li>${escapeHtml(t.label)}：<b>${t.rows}</b> 条` +
      ` <button class="btn-ghost" data-clean-table="${escapeHtml(t.table)}" title="删除 30 天前的记录">清理 30 天前</button>` +
      ` <button class="btn-ghost" data-clean-all="${escapeHtml(t.table)}" title="清空整表（不可恢复）">清空</button></li>`
    );
  }
  if (rows.length === 1) rows.push(`<li class="empty-hint">没有可清理的账本数据</li>`);
  rows.push(`<li><button id="btn-storage-vacuum" class="btn-ghost" title="整理数据库文件，释放已删数据占用的空间（大库会花几秒）">整理数据库（VACUUM）</button></li>`);
  list.innerHTML = rows.join("");

  list.querySelectorAll("[data-clean-table]").forEach((btn) => {
    btn.onclick = () => cleanTable(btn.dataset.cleanTable, 30, btn);
  });
  list.querySelectorAll("[data-clean-all]").forEach((btn) => {
    btn.onclick = () => cleanTable(btn.dataset.cleanAll, 0, btn);
  });
  const vb = document.getElementById("btn-storage-vacuum");
  if (vb) vb.onclick = async () => {
    vb.disabled = true;
    try {
      const r = await request("storage.cleanup", { targets: [], vacuum: true });
      await renderStorageCard();
      storageStatus(r.freed_bytes > 0
        ? `✓ 已整理数据库，释放 ${retentionBytes(r.freed_bytes)}`
        : "✓ 数据库已整理");
    } catch (e) {
      storageStatus("✗ 整理失败：" + e.message, false);
    } finally { vb.disabled = false; }
  };
}

async function cleanTable(table, days, btn) {
  const label = btn.closest("li").textContent.split("：")[0];
  const what = days > 0 ? `删除「${label}」中 30 天前的记录` : `清空「${label}」（全部记录，不可恢复）`;
  if (!(await confirmModal(`${what}？\n\n清理后无法恢复，确定继续？`,
      { title: "确认清理", okLabel: "清理", danger: true }))) return;
  btn.disabled = true;
  try {
    const r = await request("storage.cleanup", {
      targets: [{ table, days }],
      vacuum: false,
    });
    await renderStorageCard();
    const n = r.deleted[table] || 0;
    storageStatus(n > 0 ? `✓ 已删除 ${n} 条记录` : "没有符合条件的记录");
  } catch (e) {
    storageStatus("✗ 清理失败：" + e.message, false);
    btn.disabled = false;
  }
}
