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
        "factory-panel-filter", "factory-search", "factory-demo-badge",
        "factory-stage", "factory-canvas", "factory-labels", "factory-toolbar",
        "factory-center", "factory-labels-toggle",
        "factory-all-runs", "factory-drawer", "factory-notice",
        "factory-weather",
        "factory-num-running", "factory-num-queued", "factory-num-attention",
        "factory-num-done", "factory-num-tokens", "factory-num-chat",
    ):
        assert f'id="{el_id}"' in html, f"缺少工厂 DOM 契约里的 #{el_id}"
    # 五个计数格用 data-k 区分，前端与测试都按它定位
    for key in ("running", "queued", "attention", "done", "tokens", "chat"):
        assert f'data-k="{key}"' in html
    # 面板筛选的四个模式按钮
    for mode in ("all", "running", "attention", "stopped"):
        assert f'data-filter="{mode}"' in html, f"缺少筛选模式 {mode}"
    # 抽屉默认 hidden
    assert re.search(r'id="factory-drawer"\s+class="hidden"', html)


def test_factory_nav_entries_in_sidebar_and_rail():
    html = read_static("index.html")
    # 展开态：与「新建会话」并列的主操作（同一个 .nav-new 皮），排在 #btn-new 之后
    assert 'class="nav-new" data-act="factory"' in html
    assert html.index('id="btn-new"') < html.index('id="btn-factory"')
    # 收起态：窄栏主操作组里紧随 rail-new
    assert 'data-rail-act="factory"' in html
    assert html.index('id="rail-new"') < html.index('data-rail-act="factory"')
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
    # 工厂看板是主操作（#btn-factory），选择器把它和 .side-menu 的导航项一起收进来
    i = js.index('document.querySelectorAll(".nav-item, #btn-factory").forEach')
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
    for fn in ("factoryMount", "factoryUnmount", "factoryReload", "factoryOnEvent",
               "factoryRepaint", "factoryOnAutomationChanged"):
        assert f"function {fn}(" in js, f"缺少全局函数 {fn}"
        assert f"window.{fn} = {fn};" in js, f"{fn} 没有暴露到 window（app.js 用 typeof 查它）"


def test_factory_module_calls_all_ws_methods():
    js = read_static("app-factory.js")
    for method in (
        "factory.state", "factory.runs", "factory.run_now",
        "factory.toggle", "factory.cancel", "factory.ack",
        "factory.line_add", "factory.save", "factory.run_mark",
    ):
        assert f'request("{method}"' in js, f"前端没有接 {method}"


def test_factory_calls_present_in_bundled_frontend():
    """app.js + 全部 app-*.js 按 index.html 加载顺序拼起来后仍能看见这些调用。

    这条与 conftest.read_app_bundle 同一口径：分区文件挪动或标签顺序改动时，
    工厂的装配不会静默丢失。
    """
    js = read_app_bundle()
    for method in (
        "factory.state", "factory.runs", "factory.run_now",
        "factory.toggle", "factory.cancel", "factory.ack",
        "factory.line_add", "factory.save", "factory.run_mark",
    ):
        assert f'request("{method}"' in js, f"拼接后的前端里没有 {method}"


def test_app_js_resyncs_factory_on_automation_updates():
    """cron_updated / pipeline_updated 都要触发工厂重拉。

    产线的新建 / 删除 / 暂停与跑完后的「下次运行」只存在于服务端快照里，
    不重拉就会一直显示旧值（设计稿 §8.2 要求复用这两个快照）。
    """
    js = read_static("app.js")
    for case in ('case "cron_updated": {', 'case "pipeline_updated": {'):
        i = js.index(case)
        body = js[i:js.index("break;", i)]
        assert "factoryOnAutomationChanged" in body, f"{case} 没有同步工厂"
    fjs = read_static("app-factory.js")
    assert "function scheduleResync(" in fjs
    # 防抖 + 只在工厂打开时重拉（没打开就不必为它查库）
    assert "S.resyncTimer" in fjs and "if (!S.mounted) return;" in fjs


def test_factory_module_wires_progress_cancel_health_and_ack():
    """工序进度 / 叫停 / 产线体检 / 需留意消化四件事的前端接线。"""
    js = read_static("app-factory.js")
    # 工序进度：phase=progress 走独立分支，不污染运行行
    assert 'if (data.phase === "progress") { applyRunProgress(data.run); return; }' in js
    assert "function applyRunProgress(" in js and "function progressText(" in js
    assert "freshProgress(" in js, "进度要有新鲜度判定，不能一直显示上一个工具名"
    # 叫停：走确认框，再调 factory.cancel
    assert "function cancelRun(" in js and "confirmModal(" in js
    assert 'data-cancel-line="1"' in js and 'data-cancel-task="' in js
    # 产线体检：抽屉里的指标与状态色条
    assert "function healthBlock(" in js and "fx-strip" in js
    # 需留意：按产线「知道了」
    assert "function ackAttention(" in js and 'data-ack-all="1"' in js
    # 单次成本：运行详情里的用量行
    assert "function tokenRow(" in js and "in_tokens" in js


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
    # 昼夜由本机时间决定；开启现实天气时优先用当地日出日落（is_day）
    assert "function weatherIsDay(" in js
    assert "S.weather.is_day" in js and "h >= 6 && h < 18" in js


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
    i_utility = js.index('kind: "cooling"', i_site)
    i_bg = js.index("// 远景工业带", i_utility)
    assert i_site < i_utility < i_bg
    utility_seg = js[i_utility:i_bg]
    for kind in ("cooling", "pipeRack"):
        assert f'kind: "{kind}"' in utility_seg, f"公用设施段没有构建 {kind}"
    # 转运桥退出主厂区、挪到远景工业带，且必须带 bg 标记（只作天际线层次）
    i_bridge = js.index('kind: "transferBridge"', i_bg)
    assert "bg: true" in js[i_bridge:i_bridge + 260], "远景转运桥缺少 bg 标记"


def test_factory_module_wires_front_buildings():
    """六种厂外建筑（料场 / 罐区）要有绘制分派、接地影与近景建筑群接线。

    这些模型全靠 buildIndustrialSite 里的 utilityGroup 摆件：函数写了但没分派、
    或建筑群漏了 near 标记，界面上的表现分别是「整件不动」和「近景发灰成远景」。
    这里锁源码接线，几何与假 Canvas 绘制由 test_factory_scene_models.py 的探针跑。
    """
    js = read_static("app-factory.js")
    for kind, fn in (
        ("coalShed", "drawCoalShedModel"),
        ("siloBattery", "drawSiloBatteryModel"),
        ("gasholder", "drawGasholderModel"),
        ("basin", "drawBasinModel"),
        ("loadingRack", "drawLoadingRackModel"),
        ("flare", "drawFlareModel"),
    ):
        assert f"function {fn}(" in js, f"缺少绘制函数 {fn}"
        assert f'else if (m.kind === "{kind}") {fn}(ctx, pal, m);' in js, (
            f"drawModel 没有分派 {kind} → {fn}"
        )
        assert f'kind: "{kind}"' in js, f"{kind} 没有摆进场景（buildIndustrialSite 里找不到）"
    # 接地影：站在地上的建筑没有影子会像浮在沙上；水池是下沉的，反过来不该有方形影
    i_shadow = js.index("function drawShadows(")
    shadow = js[i_shadow:js.index("\n  function ", i_shadow + 10)]
    assert 'm.kind === "coalShed" || m.kind === "loadingRack"' in shadow
    for kind in ("siloBattery", "gasholder", "flare"):
        assert f'm.kind === "{kind}"' in shadow, f"{kind} 没有接地影分支"
    assert 'm.kind === "basin"' not in shadow, "水池是下沉的，不该有方形接地影"
    # 近景建筑群：utilityGroup 多了 opts，near 不标 bg（标了就成远景灰）、
    # plateauAligned 不再按环数整体位移（位置已按台地前缘公路算好）
    assert "function utilityGroup(name, x, y, build, opts)" in js
    assert "if (!(opts && opts.near)) m.bg = true;" in js
    for name in ("stockyard", "tankFarm"):
        head = f'utilityGroup("{name}", '
        assert head in js, f"缺少前场建筑群 {name}"
        i = js.index(head)
        assert "{ near: true, plateauAligned: true });" in js[i:i + 4000], (
            f"{name} 的 near / plateauAligned 标记漏了"
        )
    # 贴路：锚点按台地算，并明确跳过按环数的位移
    assert "function roadsideAnchor(side, t, gap)" in js
    assert "var stockYard = roadsideAnchor(-1, 0.52, 0);" in js
    assert "var tankYard = roadsideAnchor(1, 0.52, 0);" in js
    assert "if (m.plateauAligned) return { x: 0, y: 0 };" in js, (
        "plateauAligned 必须在 perimeterOffset 里短路，否则贴路的两组会被再推一次"
    )
    # 「贴着路边」由实测决定，不写死常数：量完最近那面墙再整组挪到 ROAD_CLEARANCE
    assert "var ROAD_CLEARANCE = 14;" in js
    assert "function alignYardToRoad(name, armCorner, n)" in js
    assert "var shift = Math.round(ROAD_CLEARANCE - near);" in js
    # 两组脚下直接是沙地：不许再铺混凝土平台，模型也不许抬高基准（z0 全 0）
    assert "addBox(x, y, YARD_HW, YARD_HH, 2, \"concrete\", { paving: true, curb: true });" not in js
    assert "YARD_HW" not in js and "YARD_HH" not in js
    # 公路两端延长（主干道仍是唯一的路）
    assert "function extendRoad(from, toward, len)" in js
    assert "function drawLayer(m)" in js
    # 层序：铺装 0 → 远景 1 → 立体件 2
    i_layer = js.index("function drawLayer(m) {")
    layer_body = js[i_layer:js.index("\n  }", i_layer)]
    assert "if (m.paving) return 0;" in layer_body
    assert "return m.bg ? 1 : 2;" in layer_body
    assert "var layerA = drawLayer(a.m);" in js and "var layerB = drawLayer(b.m);" in js
    # 两条场内道与前场连接道都已按要求删掉：road 这种模型不该再回来
    assert "function drawRoadModel(" not in js
    assert 'm.kind === "road"' not in js
    assert 'kind: "road"' not in js
    # 台地是贴路布置的前提：单测 / 探针只传两个参数时也得有一份
    assert "if (!plateau) plateau = makePlateau(factories);" in js
    # 后场两翼保持远景（不传 opts）——全改成近景会破坏远景雾化层次
    for name in ("serviceQuarter", "energyQuarter"):
        i = js.index(f'utilityGroup("{name}",')
        body = js[i:js.index("\n    });", i)]
        assert "near" not in body and "plateauAligned" not in body, (
            f"{name} 是后场公用建筑，不该标成近景 / 贴路"
        )


def test_factory_module_wires_stockyard_props_and_night_lighting():
    """料场新增的煤堆 / 地磅 / 投光要有绘制、接地影，还要接上夜间地面光池。"""
    js = read_static("app-factory.js")
    for kind, fn in (
        ("coalPile", "drawCoalPileModel"),
        ("weighbridge", "drawWeighbridgeModel"),
        ("floodlight", "drawFloodlightModel"),
    ):
        assert f"function {fn}(" in js, f"缺少绘制函数 {fn}"
        assert f'else if (m.kind === "{kind}") {fn}(ctx, pal, m);' in js, (
            f"drawModel 没有分派 {kind} → {fn}"
        )
    i_shadow = js.index("function drawShadows(")
    shadow = js[i_shadow:js.index("\n  function ", i_shadow + 10)]
    for kind in ("coalPile", "weighbridge", "floodlight"):
        assert f'm.kind === "{kind}"' in shadow, f"{kind} 缺少接地影分支"
    # 夜间可读性：光池先于主体绘制，冷白投光有自己的色键
    assert "function drawLightPools(" in js
    assert "drawLightPools(ctx, pal);" in js
    assert "glowCool" in js and "pal.glowCool" in js


def test_factory_module_wires_west_depot():
    """台地左角外的物资场要真的进场景；新用的小龙门吊要有接地影。"""
    js = read_static("app-factory.js")
    assert 'utilityGroup("westDepot"' in js, "西侧物资场没有摆进场景"
    assert 'kind: "gantry"' in js, "物资场缺少龙门吊"
    i_shadow = js.index("function drawShadows(")
    shadow = js[i_shadow:js.index("\n  function ", i_shadow + 10)]
    assert 'm.kind === "gantry"' in shadow, "龙门吊缺少接地影分支"


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


def test_factory_panel_filter_batch_and_keyboard_wiring():
    """P2：筛选 / 批量操作 / 键盘导航 / 样板厂边界的接线。"""
    js = read_static("app-factory.js")
    # 筛选：纯函数 + 绑定 + 空结果与「清除筛选」出口
    for fn in ("filterProjects", "matchFilterMode", "bindFilter", "syncFilterChips",
               "resetFilter", "filterActive"):
        assert f"function {fn}(" in js, f"缺少 {fn}"
    assert 'data-clear-filter="1"' in js
    # 批量操作：厂区头两个按钮 + 单次上限 + 样板厂一律不派
    assert "BATCH_RUN_LIMIT = 5" in js
    assert 'data-batch-run="' in js and 'data-batch-pause="' in js
    assert "function batchRunLines(" in js and "function batchPauseLines(" in js
    assert "if (S.demo) { addNotice(" in js, "样板厂不能批量派工/暂停"
    # 键盘：面板内 ↑↓ / Home / End
    assert "function panelKeyNav(" in js
    assert "dom.panelBody.addEventListener(\"keydown\", panelKeyNav)" in js
    # 样板厂边界：计数条灰掉 + 「演示」标记
    assert 'dom.hud.classList.toggle("demo", !!S.demo)' in js
    assert 'dom.demoBadge.classList.toggle("hidden", !S.demo)' in js


def test_factory_module_wires_focus_filters_and_edit_chain():
    """三期：聚焦档救活机台标签、全部运行筛选分页、打开产出、去编辑、人工判定。"""
    js = read_static("app-factory.js")
    # 聚焦档：镜头真的推进（以前只改选中态，机台标签的 zoom 条件恒为假）
    assert "function applyFocusCamera(" in js
    assert "S.focusPid" in js
    assert "if (S.focusPid || S.cam.zoom >= (S.fitZoom || 0.82) * 1.15)" in js
    assert "if (S.focusPid) applyFocusCamera(false);" in js, "resize 不能把镜头拉回全景"
    # 全部运行：状态 / 厂区筛选 + 分页 + 行内打开产出
    for needle in ("S.runsFilter", "fx-runs-status", "fx-runs-project", "RUN_PAGE",
                   "offset: f.offset || 0"):
        assert needle in js, f"全部运行缺少 {needle}"
    # 重开时筛选要一起重置，不能出现「界面全部、列表还在按上次筛」的错位
    assert 'initial = initial || {};' in js
    assert 'status: initial.status || ""' in js
    assert 'statusSel.value = S.runsFilter.status;' in js
    assert "function openFactoryResult(" in js and "result_path" in js
    # 现实天气：设置项、轮询、场景天气层与 HUD 胶囊
    for needle in ("fx-cfg-weather", "fx-cfg-weather-location",
                   "function refreshWeather(", 'request("factory.weather"',
                   "function weatherKind(", "function drawWeather(",
                   "function drawRain(", "function drawSnow(", "function drawLightning("):
        assert needle in js, f"现实天气缺少 {needle}"
    # 对话在跑：实时轮询 + 只读抽屉 + 跳到会话
    assert "function openLiveDrawer(" in js and "data-live-session" in js
    assert "function refreshLive(" in js and 'request("factory.live")' in js
    assert "function liveStale(" in js
    # 运行详情跟随 finished 事件刷新，不能停在「运行中 / 叫停」
    assert 'S.drawer.type === "run"' in js and "openRunDrawer(run.id)" in js
    # 需留意行可点击 / 键盘进入对应产线
    assert 'data-attn-line="' in js and "function openAttentionDrawer(" in js
    # 聚焦档的可见返回出口
    assert "dom.centerBtn" in js and 'if (dom.centerBtn)' in js
    assert "function outcomeChip(" in js and 'request("factory.run_mark"' in js
    # 名字兜底（删掉的任务在列表里不能渲染成空）
    assert "function runTitle(" in js and "（已删除的产线）" in js

    app = read_static("app.js")
    assert "async function openAutomationFor(" in app
    assert 'openRightTab("cron")' in app, "「去编辑」要落到自动化标签"
    sched = read_static("app-schedule.js")
    assert "li.dataset.cronId = t.id" in sched, "任务行要能被定位"


def test_factory_scene_wires_factory_level_alert_signals():
    """厂区级异常信号（地面脉冲环 + 警报灯柱）的接线与判定口径。

    以前「哪座厂区出事了」只有左面板的「连错 N」和机台那盏几十像素的小灯能
    表达，全景下扫不到。这里锁四件事：判定函数存在并被逐帧调用、两层绘制都
    接进 render、停工不闪、阈值读配置。
    """
    js = read_static("app-factory.js")
    assert "function factoryAlert(" in js
    assert "function refreshFactoryAlerts(" in js
    assert "function drawAlertGround(" in js
    assert "function drawAlertBeacon(" in js
    # 判定每帧算一次，两处绘制共用，避免「地面红灯、灯柱黄灯」的错位
    i_render = js.index("function render() {")
    render_body = js[i_render:js.index("\n  }", i_render)]
    assert "refreshFactoryAlerts();" in render_body
    # 地面层必须先于模型（不能盖住机台），灯柱必须在模型之后（不被前景挡住）
    i_ground = render_body.index("drawAlertGround(ctx, pal);")
    i_models = render_body.index("drawModels(ctx, pal);")
    i_beacon = render_body.index("drawAlertBeacon(ctx, pal);")
    assert i_ground < i_models < i_beacon, "警报信号两层的前后顺序不对"
    # 等级口径：停工 4 / 连错达阈值 3 / 失败 2 / 空产出与有拦截 1
    i_alert = js.index("function factoryAlert(f) {")
    alert_body = js[i_alert:js.index("\n  }", i_alert)]
    assert "if (f.root_ok === false) return { level: 4 };" in alert_body
    assert "maxStreak >= threshold" in alert_body
    assert "hasFail" in alert_body and "hasWarn" in alert_body
    assert "alert_after_failures" in alert_body
    # 停工是结构性状态，不随时间闪；脉冲只在 reduced-motion 之外生效
    i_beacon_fn = js.index("function drawAlertBeacon(ctx, pal) {")
    beacon_body = js[i_beacon_fn:js.index("\n  }", i_beacon_fn)]
    assert "a.level === 4 ? 0.5" in beacon_body, "停工灯头应当静止半透明，不参与闪烁"
    i_ground_fn = js.index("function drawAlertGround(ctx, pal) {")
    ground_body = js[i_ground_fn:js.index("\n  }", i_ground_fn)]
    assert "S.reduced ? 0 :" in ground_body, "脉冲要在 reduced-motion 下退化为静止描边"


def test_factory_scene_wires_upcoming_dock():
    """待发月台：把未来 30 分钟到点的产线摆成静止包裹。

    以前场景只表达「此刻」——包裹凭空从调度厅冒出来，看不到还没开始那一段。
    这里锁：计算层存在、绘制接进 render 且在在途包裹之前、被排除的四种情况、
    排布结果交给标签层复用（不各算一套）、点击只开抽屉不派工。
    """
    js = read_static("app-factory.js")
    for fn in ("function refreshUpcoming(", "function drawUpcoming(",
               "function fmtCountdown(", "function placeLabelAt("):
        assert fn in js, f"缺少 {fn}"
    # 常量与窗口
    assert "UPCOMING_WINDOW_S = 1800" in js and "UPCOMING_MAX = 7" in js
    # 接线顺序：先算，再画；月台画在模型之后、在途包裹之前
    i_render = js.index("function render() {")
    body = js[i_render:js.index("\n  }", i_render)]
    i_refresh = body.index("refreshUpcoming();")
    i_models = body.index("drawModels(ctx, pal);")
    i_upcoming = body.index("drawUpcoming(ctx, pal);")
    i_packages = body.index("drawPackages(ctx, pal);")
    assert i_refresh < i_models < i_upcoming < i_packages
    # 四种排除：在跑 / 停工 / 超窗 / 已过期
    i_fn = js.index("function refreshUpcoming() {")
    fn_body = js[i_fn:js.index("\n  }", i_fn)]
    assert 'l.status !== "idle"' in fn_body
    assert "p.root_ok === false" in fn_body
    assert "eta <= 0 || eta > UPCOMING_WINDOW_S" in fn_body
    # 排布交给标签层复用：绘制算一次 startX，标签直接读 S.upcomingLane
    assert "S.upcomingLane = {" in js
    assert "var lane = S.upcomingLane;" in js
    # 点待发包裹只开抽屉，不派工（还没到点的那一轮不该因误触提前跑）
    assert 'hit.type === "upcoming"' in js
    i_up = js.index("function onPointerUp(")
    up_body = js[i_up:js.index("\n  }", i_up)]
    assert "openLineDrawer(hit.key" in up_body
    assert "factory.run_now" not in up_body
    # 命中测试要把待发包裹一并算上
    i_hit = js.index("function hitTest(")
    hit_body = js[i_hit:js.index("\n  }", i_hit)]
    assert 'h.type === "pkg" || h.type === "upcoming"' in hit_body


def test_factory_scene_wires_berths_and_pipeline_flow():
    """调度厅泊位与装配线流向。

    泊位给「排队」补上因果：卡在月台出口的包裹以前没有画面上解释为什么卡，
    额度用完就是那个原因。装配线以前只画三颗灯珠，看得出有几个工位、看不出
    谁在等谁，而依赖流向正是装配线区别于单机产线的地方。
    """
    js = read_static("app-factory.js")
    assert "function drawBerths(" in js
    assert "function drawPipelineFlow(" in js
    assert "PIPELINE_MAX_NODES" in js
    # 泊位画在模型之前（它在地面上），且接进 render
    i_render = js.index("function render() {")
    body = js[i_render:js.index("\n  }", i_render)]
    i_berth = body.index("drawBerths(ctx, pal);")
    i_models = body.index("drawModels(ctx, pal);")
    assert i_berth < i_models, "泊位在地面上，必须先于模型绘制"
    # 额度口径与后端一致：无效值回落默认，0 / 负数夹到 1（不是「不限」）
    i_berth_fn = js.index("function drawBerths(ctx, pal) {")
    berth_body = js[i_berth_fn:js.index("\n  }", i_berth_fn)]
    assert "Number.isFinite(rawCap) ? rawCap : 3" in berth_body
    assert "clamp(Math.round(" in berth_body
    assert "S.berthLane = used ? { cap: cap, used: used } : null;" in berth_body
    # 三层排布：泊位 23 / 排队 33 / 待发 44，互相不重叠
    assert "var queuedY = base.y + Math.round(33 * z);" in js
    assert "var laneY = base.y + Math.round(44 * z);" in js
    # 装配线：失败工位之后的带面断开、下游等料
    flow_body = js[js.index("function drawPipelineFlow("):]
    flow_body = flow_body[:flow_body.index("\n  }")]
    assert "breakAt" in flow_body and "pal.bad" in flow_body
    assert 'st === "ready"' in flow_body and 'st === "done"' in flow_body
    # 原来的三灯珠写法已经换成流向绘制
    assert "装配线：工位灯串在机台后沿" not in js
    assert "drawPipelineFlow(ctx, pal, m, base, baseY, hw, hh);" in js


def test_factory_scene_waits_are_tiered_by_duration():
    """排队包裹按等待时长分级：抖动只表达急不急，标签才说等了多久。

    后端 `waiting` 给的是额度之外真正在等的那些（含 due_at），前端按每条自己
    等了多久分三档：1 分钟内静止、1–3 分钟轻抖、3 分钟以上快抖 + 转红。
    连续插值在 10fps 像素场景里读不出差别，所以用分段而不是渐变。
    """
    js = read_static("app-factory.js")
    for fn in ("function queuedWaits(", "function waitingTier("):
        assert fn in js, f"缺少 {fn}"
    # 三段阈值与抖动参数
    assert "WAIT_WARM_S = 60" in js and "WAIT_HOT_S = 180" in js
    assert "var WAIT_TIERS = [" in js
    # 判定顺序：先判热（>=180）再判温（>=60），写反会让 200 秒落进温档
    i_tier = js.index("function waitingTier(sec) {")
    tier_body = js[i_tier:js.index("\n  }", i_tier)]
    assert tier_body.index("WAIT_HOT_S") < tier_body.index("WAIT_WARM_S")
    # 无明细时按计数画、不编造时长（top 为 null）
    assert "var waits = queuedWaits();" in js
    assert "S.queuedLane = queuedDraw ? {" in js
    assert "top: waits.length ? waits[0] : null," in js
    # 标签只标等待最久的那一枚，且坐标取自绘制层
    assert "var qLane = S.queuedLane;" in js
    assert '"等 " + fmtCountdown(qLane.top.waited)' in js
    # 点排队包裹只开抽屉，不派工
    assert 'hit.type === "queued" && hit.key' in js
    i_up = js.index("function onPointerUp(")
    up_body = js[i_up:js.index("\n  }", i_up)]
    assert "openLineDrawer(hit.key" in up_body
    # 命中测试要把排队包裹一起算上
    i_hit = js.index("function hitTest(")
    hit_body = js[i_hit:js.index("\n  }", i_hit)]
    assert 'h.type === "queued"' in hit_body
    # 样式：等超过 3 分钟转红
    css = read_static("app.css")
    assert ".fx-label.queued.hot" in css


def test_factory_state_exposes_waiting_details():
    """后端的 waiting 明细：只有画得出来的字段，不含指令与预授权。"""
    py = (ROOT / "src" / "skysheep" / "server" / "backend_parts" / "factory.py").read_text(
        encoding="utf-8")
    assert "FACTORY_WAITING_LIMIT" in py
    assert '"waiting": waiting_view,' in py
    i = py.index("waiting_view = [")
    # 不能找第一个 "]"：t['id'] 里就有一个，会提前截断
    body = py[i:py.index("for t in over", i)]
    for field in ('"key"', '"name"', '"project_id"', '"due_at"'):
        assert field in body, f"waiting 明细缺少 {field}"
    # 额度之外才是真在等：切片起点是 free_slots
    assert "waiting[free_slots:free_slots + FACTORY_WAITING_LIMIT]" in py


def test_factory_scene_wires_stall_visualization():
    """停滞可视化：运行卡住了要能从画面上看出来。

    以前这条语义只存在于左面板的计时数字里——要用户自己拿「已跑 47 分」减去
    超时配置才发现。场景直接说：包裹打转 + 问号、机台冒烟、超时后焦黑。
    """
    js = read_static("app-factory.js")
    for fn in ("function runStall(", "function lineStall(",
               "function drawStallMark(", "function drawTimeoutSmoke("):
        assert fn in js, f"缺少 {fn}"
    assert "STALE_S = 300" in js and "NEAR_TIMEOUT_RATIO = 0.8" in js
    # 判定顺序：先判超时（>= 额度），再判接近超时（>= 额度 × 比例），最后判停滞
    i = js.index("function runStall(run, now) {")
    body = js[i:js.index("\n  }", i)]
    assert body.index('return "timeout"') < body.index('return "nearTimeout"')
    assert body.index('return "nearTimeout"') < body.index('return "stale"')
    # 长跑但在出活不该被判停滞：进度事件的 at 优先于 started_at
    assert "run.progress && run.progress.at" in body
    # 0 = 不限超时，但停滞仍要生效（两个判断不能共用一个 return）
    assert "limitMin > 0" in body
    # 三档表现都在 drawPackage 里
    i_dp = js.index("function drawPackage(ctx, pal, pkg, now) {")
    dp = js[i_dp:js.index("\n  }", i_dp)]
    for needle in ('stall === "stale"', 'stall === "nearTimeout"', 'stall === "timeout"'):
        assert needle in dp, f"drawPackage 缺少 {needle}"
    # 打转与问号都在 reduced-motion 下失效
    assert 'stall === "stale" && !S.reduced' in dp
    assert "S.reduced ? 0 :" in js[js.index("function drawStallMark("):]
    # 机台：停滞时灯从绿转警示色，超时前后冒烟
    i_dm = js.index("function drawMachineModel(ctx, pal, m) {")
    dm = js[i_dm:js.index("\n  }", i_dm)]
    assert "lineStall(line)" in dm
    assert 'stall ? pal.warn : pal.ok' in dm
    assert 'stall === "nearTimeout" || stall === "timeout"' in dm


def test_factory_css_covers_panel_filter_batch_and_demo_badge():
    css = read_static("app.css")
    seg = css[css.index("工厂（第三顶层视图）"):]
    for cls in (".fx-chip", ".fx-proj-ops", ".fh-demo", ".fh-weather",
                ".fx-dot.warn", ".fx-line-state.warn", ".fx-strip i.partial",
                ".fx-label.float.warn"):
        assert re.search(rf"^{re.escape(cls)}\b", seg, re.M), f"缺少样式 {cls}"
    # 折叠面板时筛选条一起收起（否则 40px 宽的折叠条里塞不下 chips）
    assert "#factory-panel.folded #factory-panel-filter { display: none; }" in seg


def test_factory_art_direction_motion_and_state_cues():
    js = read_static("app-factory.js")
    css = read_static("app.css")
    seg = css[css.index("工厂（第三顶层视图）"):]

    assert "var cloudDrift = S.reduced ? 0 :" in js
    assert "var sunBreath = S.reduced ? 0 :" in js
    assert "function refreshFlowingProject(pid)" in js
    assert "S.flowingProjects.add(rec.project.id)" in js
    assert "refreshFlowingProject(rec2.project.id)" in js
    assert "!m.spur && m.projectId && S.flowingProjects.has(m.projectId)" in js
    assert "flowStart: acc + len * t0" in js and "flowTotal: total" in js
    assert "var flicker = S.reduced ? 1 :" in js
    assert ".fx-line.running { box-shadow: inset 2px 0 0 var(--blue); }" in seg
    assert "#factory-drawer.factory-entered { animation: fx-drawer-in" in seg
    assert "@media (prefers-reduced-motion: reduce)" in seg
    assert ".fx-dot.running { animation: none; }" in seg
    assert "#factory-drawer.factory-entered { animation: none; }" in seg


# ---------- sw.js ----------

def test_service_worker_shell_includes_factory_module():
    sw = read_static("sw.js")
    assert '"/static/app-factory.js"' in sw
    m = re.search(r'SW_VERSION = "skysheep-shell-v(\d+)"', sw)
    assert m and int(m.group(1)) >= 37, "新增前端文件必须递增 SW_VERSION"
