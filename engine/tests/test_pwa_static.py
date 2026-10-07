"""PWA 静态壳：manifest / service worker / index.html head 标签 / 服务端路由。

手机端「添加到主屏幕」的三件套只许缓存静态壳（白名单 cache-first），
导航与 API/WS 一律网络直连——这里按源码断言 + 真实路由双重锁住这条边界。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import read_app_bundle
from fastapi.testclient import TestClient

from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.server.app import STATIC_DIR

NIGHT_BG = "#171410"  # 夜墨底色：index.html 首帧兜底与 app.css --bg 同值


def _static(name: str) -> str:
    return (Path(STATIC_DIR) / name).read_text(encoding="utf-8")


def _url_to_disk(url_path: str) -> Path:
    """/static/xxx 的 URL 路径 → static 目录内的磁盘路径。"""
    assert url_path.startswith("/static/"), f"只认 /static/ 下的 URL: {url_path}"
    return Path(STATIC_DIR) / url_path[len("/static/"):]


def _sw_shell_urls() -> list[str]:
    """从 sw.js 源码解析 SHELL_URLS 缓存白名单（数组内每行一个带引号路径）。"""
    src = _static("sw.js")
    m = re.search(r"const SHELL_URLS = \[(.*?)\];", src, re.S)
    assert m, "sw.js 里必须保留 SHELL_URLS 缓存白名单数组"
    return re.findall(r'"([^"]+)"', m.group(1))


def _client(home):
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: FakeProvider([]),
    )
    return TestClient(app)


# ---- manifest.webmanifest ----


def test_manifest_is_valid_and_references_real_icons():
    manifest = json.loads(_static("manifest.webmanifest"))
    assert manifest["name"].strip() and manifest["short_name"].strip()
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "./"
    assert manifest["theme_color"].lower() == NIGHT_BG
    icons = manifest["icons"]
    assert icons, "manifest 必须声明图标"
    for icon in icons:
        src = icon["src"]
        # 绝对路径：不随 manifest 提供位置（站点根路由）漂移
        assert src.startswith("/")
        assert _url_to_disk(src).is_file(), f"图标不存在: {src}"
    # Chrome 可安装性要求至少一个 ≥192px 的图标
    assert any(
        max(int(x) for x in icon["sizes"].split("x") if x.isdigit()) >= 192
        for icon in icons
    )


def test_manifest_theme_color_matches_night_theme_css():
    """theme_color 必须就是夜墨主题的 --bg（改主题色要一起改的那处）。"""
    css = _static("app.css")
    m = re.search(r'\[data-theme="night"\]\s*\{([^}]*)\}', css, re.S)
    assert m, "app.css 里必须有 [data-theme=\"night\"] 变量段"
    bg = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})\s*;", m.group(1))
    assert bg, "夜墨段里必须能取到 --bg"
    manifest = json.loads(_static("manifest.webmanifest"))
    assert manifest["theme_color"].lower() == bg.group(1).lower()
    # 启动画面底色与状态栏同色，白线图标才有衬托
    assert manifest["background_color"].lower() == bg.group(1).lower()


# ---- index.html head ----


def test_index_html_has_pwa_head_tags():
    head = _static("index.html").split("</head>", 1)[0]
    assert '<link rel="manifest" href="/manifest.webmanifest">' in head
    assert f'<meta name="theme-color" content="{NIGHT_BG}">' in head
    assert re.search(
        r'<link rel="apple-touch-icon" href="/static/[^"]+\.png">', head
    ), "iOS 安装图标（apple-touch-icon）缺失"
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in head


# ---- sw.js：只缓存静态壳 ----


def test_sw_cache_allowlist_is_static_only_and_files_exist():
    urls = _sw_shell_urls()
    assert urls, "缓存白名单不能为空"
    for u in urls:
        assert u.startswith("/static/"), f"白名单只允许 /static/ 静态文件: {u}"
        # 预缓存逐条 add、404 静默跳过：文件缺失只会让壳缺角而不报错，这里锁住
        assert _url_to_disk(u).is_file(), f"预缓存目标不存在: {u}"
    # 动态路径一个都不许进白名单
    for banned in ("/ws", "/health", "/preview", "/manifest.webmanifest"):
        assert banned not in urls, f"动态路径不许缓存: {banned}"


def test_sw_fetch_handler_never_touches_dynamic_requests():
    src = _static("sw.js")
    # 两道闸：导航 network-only；白名单之外（/ws、/health、/preview…）一律不拦截
    assert 'req.mode === "navigate"' in src, "fetch 处理器必须放行页面导航"
    assert "SHELL_URLS.indexOf(url.pathname)" in src, "fetch 处理器必须按白名单过滤"
    # 失效机制：版本常量存在且进缓存名（activate 清旧版本）
    assert re.search(r'const SW_VERSION = "[^"]+"', src)
    assert 'caches.delete' in src, "activate 必须清理旧版本缓存"


def test_app_js_registers_sw_silently_and_skips_local():
    js = read_app_bundle()
    assert 'serviceWorker.register("/sw.js")' in js
    # 注册失败静默：PWA 是增强能力，不影响正常使用
    assert re.search(r"register\(\"/sw\.js\"\)\.catch\(", js)
    # 本机回环来源不注册：桌面窗口保持静态资源 no-store 直读最新前端
    assert '"127.0.0.1"' in js and '"localhost"' in js


# ---- 服务端路由（真实 app 构造）----


def test_pwa_routes_serve_correct_content_type(home):
    with _client(home) as client:
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        assert json.loads(r.text)["short_name"]
        # 手写无指纹资源与 app.js 同一纪律：改版立即可见
        assert "no-store" in r.headers["cache-control"]

        # StaticFiles 托管的同一份文件：mimetypes 注册后 Content-Type 一致
        r2 = client.get("/static/manifest.webmanifest")
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("application/manifest+json")

        # SW 必须从站点根可取（scope 才是 /），且声明允许根 scope
        r3 = client.get("/sw.js")
        assert r3.status_code == 200
        assert "javascript" in r3.headers["content-type"]
        assert r3.headers["service-worker-allowed"] == "/"
        assert "SHELL_URLS" in r3.text
        assert "no-store" in r3.headers["cache-control"]


def test_index_page_and_frontend_carry_pwa_wiring(home):
    with _client(home) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert '<link rel="manifest" href="/manifest.webmanifest">' in r.text
        assert f'<meta name="theme-color" content="{NIGHT_BG}">' in r.text
        # app.js（经 /static 下发）里带注册段
        r2 = client.get("/static/app.js")
        assert r2.status_code == 200
        assert 'serviceWorker.register("/sw.js")' in r2.text
