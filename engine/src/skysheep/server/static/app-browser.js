// ---------- 设置 · 浏览器控制 ----------
// 内置浏览器在本项目里是两处：应用窗口 / 右侧预览面板（pywebview 的 WebView2，
// profile 落在数据目录的 webview/EBWebView，改动证书开关要重启应用）与 web_page
// 工具用的无头 Edge（临时 profile，下次拉起即按新参数）。这一页管四件事：
// 总开关（browser_control，决定 browser / web_page 是否注册给 Agent）、忽略证书
// 校验、清除缓存、清除全部数据。
// 后端方法：browser.status / browser.save / browser.clear。

function browserCtlStatus(id, text, ok = true) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  // 与归档页同一套：成功字样几秒后自己淡掉，失败留着让人看清
  autoHideStatus(el, text, ok);
}

function fmtBrowserBytes(n) {
  const b = Number(n) || 0;
  if (b >= 1024 * 1024 * 1024) return (b / 1024 / 1024 / 1024).toFixed(1) + " GB";
  if (b >= 1024 * 1024) return Math.round(b / 1024 / 1024) + " MB";
  if (b >= 1024) return Math.round(b / 1024) + " KB";
  return b + " B";
}

function applyBrowserStatus(d) {
  const master = document.getElementById("browser-control-toggle");
  const cert = document.getElementById("browser-cert-toggle");
  if (master) master.checked = !!d.browser_control;
  if (cert) cert.checked = !!d.ignore_cert;
  const size = document.getElementById("browser-data-size");
  if (size) {
    const exists = (d.profiles || []).some((p) => p.exists);
    size.innerHTML = exists
      ? `内置浏览器把 Cookie、站点数据与缓存存在数据目录里，当前约 <b>${escapeHtml(fmtBrowserBytes(d.total_bytes))}</b>。`
      : "内置浏览器把 Cookie、站点数据与缓存存在数据目录里；当前还没有生成数据（用过右侧预览面板或打开过应用窗口后才会出现）。";
  }
  if (d.pending_reset) {
    browserCtlStatus("browser-data-status", "已登记：下次启动应用时清空 Cookie 与站点数据。");
  }
}

async function loadBrowserPage() {
  try {
    applyBrowserStatus(await request("browser.status"));
  } catch (e) {
    browserCtlStatus("browser-control-status", "加载失败：" + e.message, false);
  }
}

async function saveBrowserCtl(key, statusId) {
  const master = document.getElementById("browser-control-toggle");
  const cert = document.getElementById("browser-cert-toggle");
  const params = key === "ignore_cert"
    ? { ignore_cert: !!cert.checked }
    : { browser_control: !!master.checked };
  try {
    applyBrowserStatus(await request("browser.save", params));
    browserCtlStatus(statusId, key === "ignore_cert" ? "✓ 已保存" : "✓ 已保存并生效");
    // 总开关改变的是 Agent 的工具清单：设置页的「内置工具」列表跟着刷新
    if (key !== "ignore_cert") { try { await renderSettings(); } catch (e) { /* 列表刷新失败不影响保存 */ } }
  } catch (e) {
    // 失败回读：控件回到真实配置，别让界面停在一个没生效的状态上
    if (key === "ignore_cert") cert.checked = !cert.checked; else master.checked = !master.checked;
    browserCtlStatus(statusId, "✗ 保存失败：" + e.message, false);
  }
}

async function clearBrowserData(mode) {
  if (mode === "all") {
    const ok = await confirmModal(
      "清除内置浏览器的全部数据？\n\n" +
      "Cookie、站点数据、本地存储与缓存都会被删掉，网站登录状态随之丢失，无法撤销。\n" +
      "应用正在使用这份数据，删除会在下次启动应用时落地。",
      { title: "确认清除", okLabel: "清除全部", danger: true });
    if (!ok) return;
  }
  const btn = document.getElementById(mode === "all" ? "btn-browser-clear-all" : "btn-browser-clear-cache");
  if (btn) btn.disabled = true;
  try {
    const r = await request("browser.clear", { mode });
    const cleared = r.removed
      ? `已清除 ${r.removed} 项缓存${r.freed_bytes ? `，释放 ${fmtBrowserBytes(r.freed_bytes)}` : ""}`
      : "没有需要清除的缓存";
    const stuck = r.failed ? `；另有 ${r.failed} 项被占用，重启应用后再清一次` : "";
    const temp = r.temp_profiles_removed ? `；顺带清掉 ${r.temp_profiles_removed} 份无头浏览器残留` : "";
    applyBrowserStatus(await request("browser.status"));
    // 结果提示放在回读之后：回读会把「已登记待清除」的常驻提示写上，
    // 顺序反了这次的明细（清掉几项、释放多少）会被那行盖掉
    if (mode === "all") {
      browserCtlStatus(
        "browser-data-status",
        `已登记：下次启动应用时清空 Cookie 与站点数据（应用正在使用这份数据，只能启动时删）；本次${cleared}${stuck}${temp}。`);
    } else {
      browserCtlStatus("browser-data-status", `${cleared}${stuck}${temp}。`);
    }
  } catch (e) {
    browserCtlStatus("browser-data-status", "✗ 清除失败：" + e.message, false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 事件绑定放 DOMContentLoaded 之后（本文件先于 app.js 加载，但绑定只依赖本页元素）
document.getElementById("browser-control-toggle").addEventListener("change", () =>
  saveBrowserCtl("browser_control", "browser-control-status"));
document.getElementById("browser-cert-toggle").addEventListener("change", () =>
  saveBrowserCtl("ignore_cert", "browser-cert-status"));
document.getElementById("btn-browser-clear-cache").onclick = () => clearBrowserData("cache");
document.getElementById("btn-browser-clear-all").onclick = () => clearBrowserData("all");
