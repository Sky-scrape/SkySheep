"""工厂一期 · 前端接线（第三顶层视图 / 像素场景 / WS 方法）

零构建前端没有 JS 运行时测试，沿用本套件的既有做法：锁「源码里接线存在、
误回归的写法不存在」这一层；语法完整性由 `node --check` 与人工冒烟保证。

覆盖：
- index.html：`#view-factory` 的位置与全部容器 / 按钮 id、侧栏与窄栏入口、
  app-factory.js 在 app.js 之后加载
- app.js：factoryOpen 状态、进出视图、导航分发、快捷键守卫、factory_run 事件、
  主题重绘钩子、重连重拉
- app-factory.js：五个全局函数、六个 factory.* 方法、像素规格常量、演示模式、
  减少动态效果与可见性暂停
- app.css：工厂外壳只走主题变量、≤860px 隐藏画布
- sw.js：新文件进 SHELL_URLS 且版本号递增
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import read_app_bundle

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ---------- index.html ----------

def test_factory_view_is_third_top_level_after_settings():
    html = read_static("index.html")
    i_settings = html.index('id="view-settings"')
    i_factory = html.index('id="view-factory"')
    i_main_end = html.index("</main>")
    assert i_settings < i_factory < i_main_end, "工厂视图必须是 #view-settings 之后的第三顶层视图"
    assert 'id="view-factory" class="view hidden"' in html


def test_factory_dom_contract_ids_present():
    html = read_static("index.html")
    for el_id in (
        "factory-hud", "factory-clock", "factory-panel", "factory-panel-body",
        "factory-stage", "factory-canvas", "factory-labels", "factory-toolbar",
        "factory-fit", "factory-zoom-in", "factory-zoom-out", "factory-labels-toggle",
        "factory-all-runs", "factory-drawer", "factory-notice",
        "factory-num-running", "factory-num-queued", "factory-num-attention",
        "factory-num-done",
    ):
        assert f'id="{el_id}"' in html, f"缺少工厂 DOM 契约里的 #{el_id}"
    # 四个计数格用 data-k 区分，前端与测试都按它定位
    for key in ("running", "queued", "attention", "done"):
        assert f'data-k="{key}"' in html
    # 抽屉默认 hidden
    assert re.search(r'id="factory-drawer"\s+class="hidden"', html)


def test_factory_nav_entries_in_sidebar_and_rail():
    html = read_static("index.html")
    assert 'class="nav-item" data-act="factory"' in html
    assert 'data-rail-act="factory"' in html
    # 窄栏图标与既有线稿同风格（stroke=currentColor 的内联 SVG）
    i = html.index('data-rail-act="factory"')
    seg = html[i:i + 700]
    assert "<svg" in seg and 'stroke="currentColor"' in seg


def test_factory_script_loads_after_app_js():
    html = read_static("index.html")
    i_app = html.index('<script src="/static/app.js"></script>')
    i_factory = html.index('<script src="/static/app-factory.js"></script>')
    assert i_app < i_factory, "app-factory.js 必须在 app.js 之后加载"
    # app.js 之后、app-tools.js 之前（与注释里「先 app.js 后本文件」一致）
    i_tools = html.index('<script src="/static/app-tools.js"></script>')
    assert i_factory < i_tools


# ---------- app.js ----------

def test_app_js_owns_factory_view_state_and_transitions():
    js = read_static("app.js")
    assert "let factoryOpen = false;" in js
    assert "function pageViewOpen()" in js
    assert "function openFactoryPage()" in js
    assert "function closeFactoryPage()" in js
    # openSettings 与 backToChat 都要处理工厂视图的显隐
    i = js.index("function openSettings(")
    body = js[i:js.index("\nfunction ", i + 10)]
    assert "closeFactoryView()" in body
    i = js.index("function backToChat()")
    body = js[i:js.index("\nfunction ", i + 10)]
    assert "closeFactoryView()" in body


def test_app_js_dispatches_factory_nav_and_rail():
    js = read_static("app.js")
    i = js.index('document.querySelectorAll(".nav-item").forEach')
    body = js[i:js.index("});", i)]
    assert 'act === "factory"' in body and "openFactoryPage()" in body
    i = js.index('document.querySelectorAll("#sb-rail [data-rail-act]").forEach')
    body = js[i:js.index("});", i)]
    assert 'railAct === "factory"' in body and "openFactoryPage()" in body


def test_app_js_factory_event_branch_and_reconnect():
    js = read_static("app.js")
    assert 'case "factory_run": {' in js
    i = js.index('case "factory_run": {')
    body = js[i:js.index("break;", i)]
    assert "typeof factoryOnEvent === \"function\"" in body
    assert "factoryOnEvent(data)" in body
    # 重连后工厂开着要重拉
    assert "if (factoryOpen && typeof factoryReload === \"function\") factoryReload();" in js
    # 主题切换末尾重绘场景
    i = js.index("function applyThemeMode(")
    body = js[i:js.index("\nfunction ", i + 10)]
    assert 'typeof factoryRepaint === "function"' in body


def test_app_js_keyboard_guards_include_factory():
    js = read_static("app.js")
    # Esc：弹窗 → 工厂（先抽屉）→ 设置 → 停止运行
    assert "if (modalOpen()) return;" in js
    assert "if (typeof factoryEscape === \"function\" && factoryEscape()) return;" in js
    assert "closeFactoryPage();" in js
    # 快捷键一律读同一个状态，避免逐处再加标志漏掉
    assert "function settingsOpenFlag() { return pageViewOpen(); }" in js
    for needle in ('k === "f"', 'k === "k"', 'k === "w"', 'k === "Tab"'):
        assert needle in js
    assert js.count("!pageViewOpen()") >= 5


# ---------- app-factory.js ----------

def test_factory_module_exposes_globals():
    js = read_static("app-factory.js")
    for fn in ("factoryMount", "factoryUnmount", "factoryReload", "factoryOnEvent", "factoryRepaint"):
        assert f"function {fn}(" in js, f"缺少全局函数 {fn}"
        assert f"window.{fn} = {fn};" in js, f"{fn} 没有暴露到 window（app.js 用 typeof 查它）"


def test_factory_module_calls_all_six_ws_methods():
    js = read_static("app-factory.js")
    for method in (
        "factory.state", "factory.runs", "factory.run_now",
        "factory.toggle", "factory.line_add", "factory.save",
    ):
        assert f'request("{method}"' in js, f"前端没有接 {method}"


def test_factory_calls_present_in_bundled_frontend():
    """app.js + 全部 app-*.js 按 index.html 加载顺序拼起来后仍能看见六个调用。

    这条与 conftest.read_app_bundle 同一口径：分区文件挪动或标签顺序改动时，
    工厂的装配不会静默丢失。
    """
    js = read_app_bundle()
    for method in (
        "factory.state", "factory.runs", "factory.run_now",
        "factory.toggle", "factory.line_add", "factory.save",
    ):
        assert f'request("{method}"' in js, f"拼接后的前端里没有 {method}"


def test_factory_pixel_specs_and_safety_bounds():
    js = read_static("app-factory.js")
    # 32×16 等距瓦片、内部缓冲 1/2（最小 320×180）、60 枚包裹、40 个标签
    assert "var TILE_W = 32;" in js and "var TILE_H = 16;" in js
    assert "BUFFER_MIN_W = 320" in js and "BUFFER_MIN_H = 180" in js
    assert "Math.round(S.cssW / 2)" in js and "Math.round(S.cssH / 2)" in js
    assert "MAX_PACKAGES = 60" in js
    assert "MAX_LABELS = 40" in js
    assert "imageSmoothingEnabled = false" in js
    # 昼夜两套调色板
    assert "var PAL_DAY = {" in js and "var PAL_NIGHT = {" in js
    # 样板厂只在内存
    assert "S.demo" in js and "样板厂" in js


def test_factory_module_respects_reduced_motion_and_visibility():
    js = read_static("app-factory.js")
    assert "(prefers-reduced-motion: reduce)" in js
    assert "document.hidden" in js
    assert "visibilitychange" in js
    # 主题切换 / 明暗判定用的是既有 resolvedThemeId
    assert "resolvedThemeId" in js


def test_factory_module_wires_new_heavy_models():
    """新大型设备（冷却塔 / 管廊 / 输煤栈桥）必须有绘制分派与场景构建接线。

    坐标、path/高度与假 Canvas 绘制在 `test_factory_scene_models.py` 里跑运行时探针；
    这里只锁源码接线，防止 kind 或函数名改半边。
    """
    js = read_static("app-factory.js")
    for kind, fn in (
        ("cooling", "drawCoolingTowerModel"),
        ("pipeRack", "drawPipeRackModel"),
        ("transferBridge", "drawTransferBridgeModel"),
    ):
        assert f"function {fn}(" in js, f"缺少绘制函数 {fn}"
        assert f'else if (m.kind === "{kind}") {fn}(ctx, pal, m);' in js, (
            f"drawModel 没有分派 {kind} → {fn}"
        )
    # 公用设施在 buildIndustrialSite 里、远景工业带之前构建，与项目产线解耦
    i_site = js.index("function buildIndustrialSite(")
    i_utility = js.index("// 公用设施独立于项目产线", i_site)
    i_bg = js.index("// 远景工业带", i_utility)
    assert i_site < i_utility < i_bg
    utility_seg = js[i_utility:i_bg]
    for kind in ("cooling", "pipeRack"):
        assert f'kind: "{kind}"' in utility_seg, f"公用设施段没有构建 {kind}"
    # 转运桥退出主厂区、挪到远景工业带，且必须带 bg 标记（只作天际线层次）
    i_bridge = js.index('kind: "transferBridge"', i_bg)
    assert "bg: true" in js[i_bridge:i_bridge + 260], "远景转运桥缺少 bg 标记"


# ---------- app.css ----------

def test_factory_css_uses_theme_tokens_only():
    css = read_static("app.css")
    i = css.index("工厂（第三顶层视图）")
    seg = css[i:]
    assert "#view-factory" in seg and "#factory-stage" in seg
    assert "#factory-drawer" in seg and "#factory-toolbar" in seg
    assert "image-rendering: pixelated" in seg
    # 外壳只走主题变量：该段（到文件尾）不得出现硬编码色值
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", seg), "工厂外壳样式里写死了 #hex"
    assert not re.search(r"\brgba?\(", seg), "工厂外壳样式里写死了 rgb()/rgba()"
    assert not re.search(r"\bhsla?\(", seg), "工厂外壳样式里写死了 hsl()/hsla()"


def test_factory_css_defines_factory_scoped_classes():
    """工厂样式段里的组件类（计数格 fh-* / 面板 fp-*）都在，且只此一家。"""
    css = read_static("app.css")
    seg = css[css.index("工厂（第三顶层视图）"):]
    # 外壳主结构走 #factory-* id；组件类用工厂专属短前缀（fh- / fp-），
    # 与既有页面共用类（.btn-ghost / .sec-ops）之外不引入全局新类。
    for cls in (".fh-cell", ".fp-title", ".fp-ops"):
        assert re.search(rf"^{re.escape(cls)}\b", seg, re.M), f"工厂样式缺少 {cls} 类规则"


def test_factory_css_hides_canvas_on_narrow_screen():
    css = read_static("app.css")
    i = css.index("@media (max-width: 860px)", css.index("工厂（第三顶层视图）"))
    seg = css[i:i + 900]
    assert "#factory-stage { display: none; }" in seg
    assert "#factory-panel" in seg


# ---------- sw.js ----------

def test_service_worker_shell_includes_factory_module():
    sw = read_static("sw.js")
    assert '"/static/app-factory.js"' in sw
    m = re.search(r'SW_VERSION = "skysheep-shell-v(\d+)"', sw)
    assert m and int(m.group(1)) >= 37, "新增前端文件必须递增 SW_VERSION"
