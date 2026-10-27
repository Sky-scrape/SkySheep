"""浏览器控制（设置 · 浏览器控制）：证书开关、数据清除与启动前落地。

锁五层：① 配置（browser_ignore_cert 读写回环）；② WebView2 启动参数与
待清除标记的落地（webviewargs，纯标准库那条路径）；③ 无头 Edge 的启动
参数跟随开关；④ 后端方法（browser.status / save / clear 的语义：缓存与
Cookie 的边界、全部数据必须留给下次启动）；⑤ 前端接线（页面卡片、切页
钩子、脚本加载）。全部走 SKYSHEEP_HOME 隔离（home fixture），不碰真实
~/.skysheep，也不真启动浏览器。
"""

from __future__ import annotations

import time
from pathlib import Path

from conftest import read_app_bundle
from test_server import make_client, recv_until

from skysheep import webviewargs
from skysheep.config import load_config, set_advanced_settings_in_config
from skysheep.server.backend_parts import browser_data
from skysheep.tools import browser_cdp

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": method, "params": params or {}})
        return recv_until(ws, "m1")


def make_profile() -> Path:
    """造一份假的 WebView2 profile：缓存 + Cookie + 站点数据各一份。"""
    from skysheep.config import skysheep_home

    root = skysheep_home() / "webview" / webviewargs.PROFILE_INNER
    (root / "Default" / "Cache").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Cache" / "data_1").write_bytes(b"x" * 4096)
    (root / "Default" / "Service Worker" / "CacheStorage").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Service Worker" / "CacheStorage" / "c1").write_bytes(b"y" * 1024)
    (root / "Default" / "GPUCache").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "GPUCache" / "g").write_bytes(b"z" * 512)
    (root / "GPUPersistentCache").mkdir(parents=True, exist_ok=True)
    (root / "GPUPersistentCache" / "p").write_bytes(b"p" * 256)
    (root / "Default" / "Cookies").write_bytes(b"cookie-jar")
    (root / "Default" / "Local Storage").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Local Storage" / "leveldb").write_bytes(b"ls")
    return root


# ---------- ① 配置回环 ----------


def test_ignore_cert_config_roundtrip(home):
    assert load_config().browser_ignore_cert is False, "默认必须是关（校验证书的安全一侧）"
    set_advanced_settings_in_config(browser_ignore_cert=True)
    assert load_config().browser_ignore_cert is True
    # 只提交这一项时，浏览器总开关不能被顺手改掉
    assert load_config().browser_control is False
    set_advanced_settings_in_config(browser_ignore_cert=False)
    assert load_config().browser_ignore_cert is False


# ---------- ② WebView2 参数与待清除标记 ----------


def test_browser_args_appends_cert_switch_only_when_enabled():
    off = webviewargs.browser_args()
    assert "--ignore-certificate-errors" not in off
    assert "--overscroll-history-navigation=0" in off, "原有参数不能被顶掉"
    on = webviewargs.browser_args(ignore_cert=True)
    assert "--ignore-certificate-errors" in on
    assert "--disable-features=" in on


def test_apply_browser_args_idempotent_and_merges(tmp_path):
    env: dict[str, str] = {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--existing"}
    merged = webviewargs.apply_browser_args(env, ignore_cert=True)
    assert "--existing" in merged and "--ignore-certificate-errors" in merged
    again = webviewargs.apply_browser_args(env, ignore_cert=True)
    assert again.count("--ignore-certificate-errors") == 1, "重复调用不该叠加"
    assert again.count("--existing") == 1


def test_read_ignore_cert_reads_toml_without_config_import(tmp_path):
    (tmp_path / "config.toml").write_text(
        'browser_ignore_cert = true\n[browser]\nignore = 1\n', encoding="utf-8"
    )
    assert webviewargs.read_ignore_cert(tmp_path) is True
    (tmp_path / "config.toml").write_text("broken = [\n", encoding="utf-8")
    assert webviewargs.read_ignore_cert(tmp_path) is False, "配置读坏了按默认（不忽略）"
    assert webviewargs.read_ignore_cert(tmp_path / "nope") is False


def test_apply_pending_reset_renames_profile_and_clears_marker(home):
    from skysheep.config import skysheep_home

    root = make_profile()
    base = skysheep_home() / "webview"
    (skysheep_home() / webviewargs.RESET_MARKER_NAME).write_text("{}", encoding="utf-8")

    renamed = webviewargs.apply_pending_reset()
    assert renamed, "有标记时必须让位"
    assert not (skysheep_home() / webviewargs.RESET_MARKER_NAME).exists(), "标记一次性"
    assert not root.exists() and renamed[0].exists()
    # 后台线程删改名后的目录：等一下即可（几 KB 的假 profile）
    deadline = time.time() + 10
    while time.time() < deadline and any(base.glob("*.old-*")):
        time.sleep(0.05)
    assert not any(base.glob("*.old-*")), "改名后的残留要清干净"


def test_apply_pending_reset_purges_root_level_renames(home):
    """审计 SEC-L30 回归：profile 尚无 EBWebView 内层时（首次清除的典型形态），
    改名让位的是整个 ``<名>``，产物落根级 ``<名>.old-*``——后台清理必须也扫根级，
    否则孤儿目录永久留存（旧版只扫 ``<名>/`` 内层）。"""
    from skysheep.config import skysheep_home

    root_home = skysheep_home()
    for name in webviewargs.PROFILE_DIRS:
        (root_home / name / "Default").mkdir(parents=True)  # 无 EBWebView 内层
    stale = root_home / "webview.old-999"  # 上次启动没删完的根级残留
    stale.mkdir()
    foreign = root_home / "logs.old-1"  # 名单外：不能顺手扫掉
    foreign.mkdir()
    (root_home / webviewargs.RESET_MARKER_NAME).write_text("{}", encoding="utf-8")

    renamed = webviewargs.apply_pending_reset()
    assert len(renamed) == len(webviewargs.PROFILE_DIRS), "两份 profile 都要让位"
    assert all(t.parent == root_home for t in renamed), "无内层时产物落根级（本回归的前提）"

    deadline = time.time() + 10
    while time.time() < deadline and (stale.exists() or any(t.exists() for t in renamed)):
        time.sleep(0.05)
    assert not any(t.exists() for t in renamed), "根级改名产物要被后台清理删掉"
    assert not stale.exists(), "上次启动留下的根级残留也要被扫掉"
    assert foreign.exists(), "只认 PROFILE_DIRS 名单，名单外的 .old-* 不能动"


def test_apply_pending_reset_noop_without_marker(home):
    from skysheep.config import skysheep_home

    make_profile()
    assert webviewargs.apply_pending_reset() == []
    assert (skysheep_home() / "webview" / webviewargs.PROFILE_INNER).is_dir(), "没标记不动数据"


# ---------- ③ 无头 Edge 启动参数 ----------


def test_headless_browser_cert_args_follow_config(home, monkeypatch):
    browser = browser_cdp.CdpBrowser()
    assert browser._cert_args() == []
    set_advanced_settings_in_config(browser_ignore_cert=True)
    assert browser._cert_args() == ["--ignore-certificate-errors"]
    # 参数必须真的进启动命令行（而不是只造了个函数）
    captured: list[list[str]] = []

    async def fake_spawn(cmd):
        captured.append(cmd)
        raise OSError("停在启动前：本用例只关心命令行")

    monkeypatch.setattr(browser_cdp, "_spawn", fake_spawn)
    import asyncio

    with_cert = browser_cdp.CdpBrowser()
    try:
        asyncio.run(with_cert.start())
    except Exception:  # noqa: BLE001 - 启动被上面的假 spawn 打断
        pass
    assert "--ignore-certificate-errors" in captured[0]
    set_advanced_settings_in_config(browser_ignore_cert=False)


# ---------- ④ 后端方法 ----------


def _mixin():
    """裸 mixin + 真配置：这三个方法只读 self.cfg（其余全是文件操作）。

    真身是 ServerBackend 的 mixin，这里不拉起整个后端——浏览器数据的语义
    全在文件系统上，用真实的 SKYSHEEP_HOME 跑最直接。
    """
    mixin = browser_data.BrowserDataMixin()
    mixin.cfg = load_config()
    return mixin


def test_browser_status_reports_profiles_and_switches(home):
    make_profile()
    body = _mixin().browser_status()
    assert body["browser_control"] is False and body["ignore_cert"] is False
    assert body["total_bytes"] > 0
    names = [p["name"] for p in body["profiles"]]
    assert names == list(webviewargs.PROFILE_DIRS)
    assert body["pending_reset"] is False


def test_clear_cache_keeps_cookies_and_site_data(home):
    root = make_profile()
    mixin = _mixin()
    import asyncio

    result = asyncio.run(mixin.clear_browser_data("cache"))
    assert result["mode"] == "cache"
    assert result["removed"] >= 3 and result["freed_bytes"] > 0
    assert not (root / "Default" / "Cache").exists()
    assert (root / "Default" / "Cookies").exists(), "缓存口径必须保住 Cookie"
    assert (root / "Default" / "Local Storage").is_dir(), "站点数据同理"
    assert result["needs_restart"] is False


def test_clear_all_defers_cookies_to_next_start(home):
    from skysheep.config import skysheep_home

    root = make_profile()
    mixin = _mixin()
    import asyncio

    result = asyncio.run(mixin.clear_browser_data("all"))
    assert result["mode"] == "all" and result["needs_restart"] is True
    assert (skysheep_home() / webviewargs.RESET_MARKER_NAME).is_file()
    # 使用中删不掉的 Cookie 仍在盘上——真正的删除在下次启动（见上一条用例）
    assert (root / "Default" / "Cookies").exists()
    assert mixin.browser_status()["pending_reset"] is True


def test_save_browser_settings_toggles_and_restarts_headless(home, monkeypatch):
    import asyncio

    closed: list[int] = []

    async def fake_close():
        closed.append(1)

    monkeypatch.setattr(browser_cdp, "close_browser", fake_close)
    mixin = _mixin()
    mixin._apply_registry_to_agents = lambda: None  # type: ignore[attr-defined]

    body = asyncio.run(mixin.save_browser_settings({"ignore_cert": True}))
    assert body["ignore_cert"] is True and load_config().browser_ignore_cert is True
    assert closed, "证书开关变了要关掉在跑的无头实例，下次动作按新参数拉起"
    # 无头实例没在跑时不报错（close_browser 自己兜住）
    body = asyncio.run(mixin.save_browser_settings({"browser_control": True}))
    assert body["browser_control"] is True and load_config().browser_control is True
    # 不传 = 不动
    assert asyncio.run(mixin.save_browser_settings({}))["ignore_cert"] is True


def test_ws_browser_methods_roundtrip(home):
    r = call(home, "browser.status")
    assert r["result"]["ignore_cert"] is False
    r = call(home, "browser.save", {"ignore_cert": True, "browser_control": True})
    assert r["result"]["ignore_cert"] is True and r["result"]["browser_control"] is True
    assert load_config().browser_ignore_cert is True
    r = call(home, "browser.clear", {"mode": "cache"})
    assert r["result"]["mode"] == "cache" and "removed" in r["result"]
    # 收尾：总开关留着会让别的用例看到多出来的工具
    call(home, "browser.save", {"browser_control": False, "ignore_cert": False})


# ---------- ⑤ 前端接线 ----------


def test_browser_page_wired():
    html = read_static("index.html")
    assert '<li data-target="browser">浏览器控制</li>' in html, "设置导航缺少入口"
    assert 'id="settings-page-browser"' in html
    for eid in (
        "browser-control-toggle", "browser-control-status",
        "browser-cert-toggle", "browser-cert-status",
        "browser-data-size", "browser-data-status",
        "btn-browser-clear-cache", "btn-browser-clear-all",
    ):
        assert f'id="{eid}"' in html, f"浏览器控制页缺少 #{eid}"
    # 工具页那份开关搬走了：只剩电脑控制
    assert 'id="tool-computer-control"' in html
    assert 'id="tool-browser-control"' not in html, "开关已迁到浏览器控制页，不该留重复控件"
    assert 'src="/static/app-browser.js"' in html

    js = read_app_bundle()
    assert "async function loadBrowserPage" in js
    assert 'if (target === "browser") loadBrowserPage();' in js
    for m in ("browser.status", "browser.save", "browser.clear"):
        assert f'request("{m}"' in js, f"前端没有调用 {m}"
    assert "清除全部" in js and "confirmModal" in js, "清除全部必须过确认弹窗"

    css = read_static("app.css")
    for cls in (".bctl-row", ".bctl-row-main"):
        assert cls in css, f"app.css 缺少 {cls}"


def test_headless_close_browser_is_callable_without_instance():
    import asyncio

    asyncio.run(browser_cdp.close_browser())  # 没有实例时也必须安全返回
