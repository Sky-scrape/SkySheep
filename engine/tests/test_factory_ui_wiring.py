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

def read_factory_js() -> str:
    """工厂前端按加载顺序拼接（主文件 + scene + drawer + draw 四个分区）。

    工厂拆成四个分区文件后，源码锚定断言对拼接文本跑，语义与拆分前一致。
    """
    return "\n".join(read_static(n) for n in (
        "app-factory.js", "app-factory-scene.js",
        "app-factory-drawer.js", "app-factory-draw.js"))


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
        # 顶栏只剩一盏「需留意」灯 + 回放（读数已经长在场景里）
        "factory-attention", "factory-attn-num",
        "factory-replay", "factory-replay-bar", "factory-replay-time",
        "factory-replay-stop",
    ):
        assert f'id="{el_id}"' in html, f"缺少工厂 DOM 契约里的 #{el_id}"
    # 六格计数条（运行中 / 排队 / 需留意 / 今日做成 / 今日 token / 对话在跑）已撤掉：
    # 这些数字现在由场景表达（包裹 / 成品垛 / 返工堆 / 厂区红灯），不再是一排报表。
    assert "fh-cells" not in html and "fh-cell" not in html
    assert "factory-num-running" not in html and "factory-num-done" not in html
    assert "factory-num-tokens" not in html and "factory-num-chat" not in html
    # 建线入口退回自动化页：工厂里不再有「＋」
    assert 'id="factory-line-add"' not in html
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
    # 四个工厂分区按依赖顺序加载：主文件创建 FX 与各绑定源，绘制层最后
    names = ["app-factory.js", "app-factory-scene.js",
             "app-factory-drawer.js", "app-factory-draw.js"]
    pos = [html.index(f'<script src="/static/{n}"></script>') for n in names]
    assert pos == sorted(pos), f"工厂分区加载顺序应为 {' → '.join(names)}"
    # app.js 之后、app-tools.js 之前（与注释里「先 app.js 后本文件」一致）
    i_tools = html.index('<script src="/static/app-tools.js"></script>')
    assert pos[-1] < i_tools


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
    js = read_factory_js()
    for fn in ("factoryMount", "factoryUnmount", "factoryReload", "factoryOnEvent",
               "factoryRepaint", "factoryOnAutomationChanged"):
        assert f"function {fn}(" in js, f"缺少全局函数 {fn}"
        assert f"window.{fn} = {fn};" in js, f"{fn} 没有暴露到 window（app.js 用 typeof 查它）"


def test_factory_module_calls_all_ws_methods():
    js = read_factory_js()
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
    fjs = read_factory_js()
    assert "function scheduleResync(" in fjs
    # 防抖 + 只在工厂打开时重拉（没打开就不必为它查库）
    assert "S.resyncTimer" in fjs and "if (!S.mounted) return;" in fjs


def test_factory_module_wires_progress_cancel_health_and_ack():
    """工序进度 / 叫停 / 产线体检 / 需留意消化四件事的前端接线。"""
    js = read_factory_js()
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
    js = read_factory_js()
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
    js = read_factory_js()
    assert "(prefers-reduced-motion: reduce)" in js
    assert "document.hidden" in js
    assert "visibilitychange" in js
    # 昼光系数：本机 06:00 / 18:00 为中心各 ±40 分钟过渡带；开现实天气时
    # 优先用当地日出日落时刻，只有 is_day 布尔时仍硬切
    assert "function daylightFactor(" in js
    assert "DAYLIGHT_BAND_MIN = 40" in js
    assert "w.is_day" in js and "w.sunrise" in js
    assert "S.daylightF" in js


def test_factory_module_wires_new_heavy_models():
    """新大型设备（冷却塔 / 管廊 / 输煤栈桥）必须有绘制分派与场景构建接线。

    坐标、path/高度与假 Canvas 绘制在 `test_factory_scene_models.py` 里跑运行时探针；
    这里只锁源码接线，防止 kind 或函数名改半边。
    """
    js = read_factory_js()
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
    js = read_factory_js()
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
    js = read_factory_js()
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
    js = read_factory_js()
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


def test_factory_dock_labels_match_pixel_parcel_style():
    """月台两块标签的样式：常驻待发告示牌（board）与排队包裹（queued）。

    告示牌把原来的多包裹行列换成一块固定牌，标签改为 board 家族
    （title / text / more）；排队包裹一侧不动，等超 3 分钟仍转红。
    """
    css = read_static("app.css")
    seg = css[css.index("工厂（第三顶层视图）"):]
    for cls in (".fx-label.board", ".fx-label.queued"):
        assert cls in seg, f"缺少月台标签样式 {cls}"
    assert "text-shadow:" in seg
    assert ".fx-label.queued.hot { color: var(--bad); }" in seg


def test_factory_css_defines_factory_scoped_classes():
    """工厂样式段里的组件类（计数格 fh-* / 面板 fp-*）都在，且只此一家。"""
    css = read_static("app.css")
    seg = css[css.index("工厂（第三顶层视图）"):]
    # 外壳主结构走 #factory-* id；组件类用工厂专属短前缀（fh- / fp-），
    # 与既有页面共用类（.btn-ghost / .sec-ops）之外不引入全局新类。
    for cls in (".fh-lamp", ".fp-title", ".fp-ops", ".fx-more"):
        assert re.search(rf"^{re.escape(cls)}\b", seg, re.M), f"工厂样式缺少 {cls} 类规则"


def test_factory_css_hides_canvas_on_narrow_screen():
    css = read_static("app.css")
    i = css.index("@media (max-width: 860px)", css.index("工厂（第三顶层视图）"))
    seg = css[i:i + 900]
    assert "#factory-stage { display: none; }" in seg
    assert "#factory-panel" in seg


def test_factory_panel_is_a_directory_not_an_ops_console():
    """目录该有的留下，运维台该退的都退了（这是「工厂=可视化场景」的边界）。"""
    js = read_factory_js()
    # 筛选：纯函数 + 绑定 + 空结果与「清除筛选」出口
    for fn in ("filterProjects", "matchFilterMode", "bindFilter", "syncFilterChips",
               "resetFilter", "filterActive"):
        assert f"function {fn}(" in js, f"缺少 {fn}"
    assert 'data-clear-filter="1"' in js
    # 厂区少的时候筛选条不出场
    assert "SPARSE_PROJECTS" in js and 'classList.toggle("sparse"' in js
    # 批量派工 / 批量暂停 / 建线表单都退出工厂（派工仍在：点机台或点产线行，一次一条）
    for gone in ("BATCH_RUN_LIMIT", "data-batch-run", "data-batch-pause",
                 "batchRunLines", "batchPauseLines", "data-add-project",
                 "openNewLineDrawer(0)"):
        assert gone not in js, f"工厂里不该再出现 {gone}"
    assert "function openLineDrawer(" in js or "FX.openLineDrawer" in js
    # 键盘：面板内 ↑↓ / Home / End
    assert "function panelKeyNav(" in js
    assert "dom.panelBody.addEventListener(\"keydown\", panelKeyNav)" in js
    # 样板厂边界：顶栏灰掉 + 「演示」标记
    assert 'dom.hud.classList.toggle("demo", !!S.demo)' in js
    assert 'dom.demoBadge.classList.toggle("hidden", !S.demo)' in js
    # 目录默认折叠（场景优先），折叠按钮仍能翻回来
    assert "panelFolded: true" in js
    assert 'dom.panel.classList.toggle("folded", S.panelFolded)' in js


def test_factory_scene_has_traces_not_numbers():
    """② 痕迹：成品码垛 / 返工堆 / 出货卡车 / 在制火花 / 到点沙漏 / 回放。"""
    js = read_factory_js()
    # 场院上的「今天干了多少」：托盘按件数码、封顶、满垛插旗
    for needle in ("function refreshProjectStats(", "function statFor(",
                   "PALLET_SIZE", "PALLET_MAX", "REWORK_MAX"):
        assert needle in js, f"成品垛 / 返工堆缺少 {needle}"
    assert "kind: \"rework\"" in js and "kind: \"gate\"" in js
    assert "function drawReworkModel(" in js and "function drawGateModel(" in js
    # 出货：满一托盘放一辆车（纯表现，不参与判定）
    assert "function spawnTrip(" in js and "function updateTrips(" in js
    assert "function drawTrips(" in js
    # 在制火花 / 到点沙漏挂在机台上
    assert "料口灯带" in js and "待发沙漏" in js
    assert "UPCOMING_GLYPH_S" in js
    # 回放今天：拉今天的运行、按虚拟时间重演、Esc 可收队
    assert "function toggleReplay(" in js and "function drawReplay(" in js
    assert 'view: "tokens_today"' in js
    assert "REPLAY_CAP_MS" in js and "function replayProgress(" in js
    # 厂区心情（大门 / 工头 / 烟 都由它驱动）
    assert "function refreshFactoryMood(" in js
    for mood in ("working", "trouble", "stopped", "idle"):
        assert f'"{mood}"' in js


def test_factory_module_wires_focus_filters_and_edit_chain():
    """三期：聚焦档救活机台标签、全部运行筛选分页、打开产出、去编辑、人工判定。"""
    js = read_factory_js()
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
    # 入口必须绑在「设置」抽屉里（顶栏那一格撤掉之后它挪进来了）；
    # 绑错抽屉的话按钮点了不动——纯源码断言查得出来
    drawer_js = read_static("app-factory-drawer.js")
    i_set = drawer_js.index("function openSettingsDrawer(")
    set_body = drawer_js[i_set:drawer_js.index("\n  function ", i_set)]
    assert 'data-open-live' in set_body and "openLiveDrawer()" in set_body
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
    js = read_factory_js()
    assert "function factoryAlert(" in js
    assert "function refreshFactoryAlerts(" in js
    assert "function drawAlertGround(" in js
    assert "function drawAlertBeacon(" in js
    # 判定每帧算一次，两处绘制共用，避免「地面红灯、灯柱黄灯」的错位
    i_render = js.index("function render() {\n")  # 真身在绘制层（主文件有同名单行转发函数）
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


def test_factory_scene_wires_noticeboard():
    """待发告示牌：右月台常驻一块固定牌，空态也画，倒计时贴牌。

    旧形态把未来 30 分钟到点的产线摆成「左排队、右待发」的两列包裹（多包裹
    行列布局 + 溢出末端），现在右月台换成一块常驻告示牌：有排期显示「任务名 +
    倒计时」，没有则显示「暂无待发」。这里锁：纯函数计算层存在、绘制接进 render
    且在在途包裹之前、排除语义与窗口、过期不下界（超 30 分钟仍 due）、空态文案、
    点击只开详情不派工；并按契约断言旧行列布局已退出。
    """
    js = read_factory_js()
    for fn in ("function refreshUpcoming(", "function drawUpcoming(",
               "function drawNoticeboard(", "function noticeboardState(",
               "function upcomingList(", "function fmtCountdown("):
        assert fn in js, f"缺少 {fn}"
    # 常量：30 分钟窗口、牌面只挂一枚、名字截 8 字、固定牌几何
    assert "UPCOMING_WINDOW_S = 1800" in js
    assert "UPCOMING_MAX = 1" in js and "UPCOMING_NAME_MAX = 8" in js
    assert "UPCOMING_BOARD = {" in js
    # 排除语义：只认 idle cron、停工厂区与超窗都不待发；过期不设下界
    assert 'l.status !== "idle"' in js and 'l.kind !== "cron"' in js
    assert "p.root_ok === false" in js
    assert "eta > UPCOMING_WINDOW_S" in js
    # 过期不设下界：到点没跑起来的排期留在 due（行为断言在运行时测试里跑）
    # 接线顺序：先算，再画；告示牌画在模型之后、在途包裹之前
    i_render = js.index("function render() {\n")  # 真身在绘制层（主文件有同名单行转发函数）
    body = js[i_render:js.index("\n  }", i_render)]
    i_refresh = body.index("refreshUpcoming();")
    i_models = body.index("drawModels(ctx, pal);")
    i_board = body.index("drawUpcoming(ctx, pal);")
    i_packages = body.index("drawPackages(ctx, pal);")
    assert i_refresh < i_models < i_board < i_packages
    # 空态文案 + 常驻固定牌几何（无任务也写 S.noticeboard）
    assert "暂无待发" in js
    i_board_fn = js.index("function drawNoticeboard(ctx, pal) {")
    board = js[i_board_fn:js.index("\n  function drawUpcoming(", i_board_fn)]
    assert "S.noticeboard = {" in board
    assert "UPCOMING_BOARD" in board
    assert 'type: "upcoming"' in board
    assert "view.state === \"due\" ? pal.rust" in board, "到点未跑的牌面要有区分"
    # 旧多包裹行列布局已退出
    for gone in ("function upcomingPosition(", "rowDrop", "upcomingOverflow",
                 "upcomingPositions", "Math.floor(index / 2)"):
        assert gone not in js, f"旧多包裹排布应已移除：{gone}"
    # 告示牌打开只读弹窗，任务详情仍保留在其他对象的点击路径。
    i_up = js.index("function onPointerUp(")
    up_body = js[i_up:js.index("\n  }", i_up)]
    assert 'hit.type === "upcoming"' in up_body
    assert "openUpcomingDialog()" in up_body
    assert "FX.openUpcomingDialog = openUpcomingDialog" in js
    assert "factory.run_now" not in up_body
    label_body = js[js.index("function updateLabels("):js.index("function loop(")]
    assert '"待发", "board title"' in label_body
    assert '"board text"' not in label_body and '"board more"' not in label_body


def test_factory_scene_wires_berths_and_pipeline_flow():
    """调度厅泊位与装配线流向。

    泊位给「排队」补上因果：卡在月台出口的包裹以前没有画面上解释为什么卡，
    额度用完就是那个原因。装配线以前只画三颗灯珠，看得出有几个工位、看不出
    谁在等谁，而依赖流向正是装配线区别于单机产线的地方。
    """
    js = read_factory_js()
    assert "function drawBerths(" in js
    assert "function drawPipelineFlow(" in js
    assert "PIPELINE_MAX_NODES" in js
    # 泊位画在模型之前（它在地面上），且接进 render
    i_render = js.index("function render() {\n")  # 真身在绘制层（主文件有同名单行转发函数）
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
    # 右月台的多包裹行列已换成常驻告示牌（见 test_factory_scene_wires_noticeboard）；
    # 排队包裹一侧不动，仍沿调度厅左下紧凑排列、点它只看产线不派工。
    dock = js[js.index("function drawUpcoming(ctx, pal) {"):]
    dock = dock[:dock.index("\n  function drawPackage(")]
    assert "var leftDock = viewPoint(hall.x - 34, hall.y + 22);" in dock
    assert 'S.hits.push({ type: "queued"' in dock
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
    js = read_factory_js()
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
    assert "S.queuedLane =" in js
    assert "queuedDraw" in js
    assert "top: waits.length ? waits[0] : null," in js
    # 标签只标等待最久的那一枚，且坐标取自左下侧首箱的绘制结果。
    assert "var qLane = S.queuedLane;" in js
    assert '"等 " + fmtCountdown(qLane.top.waited)' in js
    assert "qLane.label.x * (S.cssW / S.bufW)" in js
    assert "qLane.label.y * (S.cssH / S.bufH)" in js
    # 点排队包裹只开抽屉，不派工
    assert 'hit.type === "queued" && hit.key' in js
    i_up = js.index("function onPointerUp(")
    up_body = js[i_up:js.index("\n  }", i_up)]
    assert "openLineDrawer(hit.key" in up_body
    # 命中测试要把排队包裹一起算上（小目标优先于脚下机台）
    i_hit = js.index("function hitTest(")
    hit_body = js[i_hit:js.index("\n  }", i_hit)]
    assert 'h.type !== "queued"' in hit_body or 'h.type === "queued"' in hit_body
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
    js = read_factory_js()
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
    js = read_factory_js()
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


def test_factory_panel_keeps_scroll_and_focus_on_rebuild():
    """面板重建不丢滚动位置与焦点：运行事件密集时面板不能跳回顶部。

    renderPanel 全量 innerHTML 重建会重置 scrollTop，故重建前记忆、写回后恢复；
    焦点恢复用 preventScroll 防止把滚动拽走；运行事件路径走 refreshPanelStates
    （焦点在面板控件上时不动面板），不绕过保护直调 renderPanel。
    """
    js = read_factory_js()
    assert "var keepScroll = dom.panelBody.scrollTop;" in js
    assert "dom.panelBody.scrollTop = keepScroll;" in js
    assert "rows[i].focus({ preventScroll: true })" in js
    i_ev = js.index("function factoryOnEvent(")
    ev_body = js[i_ev:js.index("\n  function ", i_ev + 10)]
    assert "refreshPanelStates();" in ev_body
    assert "renderPanel();" not in ev_body


def test_factory_static_layer_offscreen_cache():
    """地面 / 斑块 / 投影是纯静态的（零动画引用），走离屏缓存，不每帧重画。

    缓存 key 含世界引用（applyState 重建即失效）、缓冲尺寸与昼夜；离屏不可用
    时直画主画布兜底；缓存层与警报层序不变（阴影仍在 alertGround 之下）。
    """
    js = read_factory_js()
    assert "function drawStaticLayer(ctx, pal)" in js
    assert "c.world === S.world" in js
    assert "c.pal === pal" in js
    # 相机会改变世界→屏幕投影：key 必须含相机三值（否则聚焦/全景缓动期间地面错位）
    assert "c.camX === S.cam.x && c.camY === S.cam.y && c.zoom === S.cam.zoom" in js
    assert 'document.createElement("canvas")' in js
    i_render = js.index("function render() {\n")  # 真身在绘制层（主文件有同名单行转发函数）
    render_body = js[i_render:js.index("\n  }", i_render)]
    assert "drawStaticLayer(ctx, pal);" in render_body
    # 静态三函数不再逐帧直调（已并入缓存层）
    assert "drawTerrain(ctx, pal);" not in render_body
    assert "drawShadows(ctx, pal);" not in render_body
    # 层序不变：静态层（含投影）仍在警报地面之下
    assert render_body.index("drawStaticLayer") < render_body.index("drawAlertGround")


# ---------- 跨文件转发 ----------

def test_factory_cross_file_helpers_are_forwarded():
    """主文件裸调用、实现在另外三个分区文件里的函数，必须有 FX 转发。

    四个工厂分区各自是一个 IIFE，跨文件调用只能经 window.FX 运行时查表。
    漏写一行转发不报语法错误，只在跑到那条分支时抛 ReferenceError；而渲染
    跑在数据加载的 Promise 链里，错误会被 catch 误报成「读取工厂数据失败」。
    工厂三期在 `drawPackage` 的 `pointAt` 上正是这么踩的（首屏一有运行中的
    包裹就白画布 + 「重试」按钮，点多少次都没用）。
    """
    main = read_static("app-factory.js")
    others = "".join(read_static(n) for n in (
        "app-factory-scene.js", "app-factory-drawer.js", "app-factory-draw.js"))
    forwarded = set(re.findall(r"\breturn FX\.(\w+)\(", main))
    forwarded |= set(re.findall(r"= FX\.(\w+);", main))
    # 直接挂到 window 的（factoryEscape 一族）主文件裸调时按全局查表，无需转发
    on_window = set(re.findall(r"window\.(\w+) = \w+;", others))
    own = set(re.findall(r"function (\w+)\(", main))
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", main))
    moved = set(re.findall(r"function (\w+)\(", others)) - own
    missing = sorted((called & moved) - forwarded - on_window)
    assert not missing, f"实现在分区文件里、主文件裸调用却漏了 FX 转发：{missing}"


# ---------- sw.js ----------

def test_service_worker_shell_includes_factory_module():
    sw = read_static("sw.js")
    for name in ("app-factory.js", "app-factory-scene.js",
                 "app-factory-drawer.js", "app-factory-draw.js"):
        assert f'"/static/{name}"' in sw, f"sw.js 壳清单缺少 {name}"
    m = re.search(r'SW_VERSION = "skysheep-shell-v(\d+)"', sw)
    assert m and int(m.group(1)) >= 74, "工厂月台布局更新必须递增 SW_VERSION"
