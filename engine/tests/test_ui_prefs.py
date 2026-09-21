"""ui.get / ui.save：界面布局偏好（侧栏宽、输入区高）的持久化。"""

from __future__ import annotations

from test_server import make_client, recv_until


def call_ui(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u1", "method": method, "params": params or {}})
        return recv_until(ws, "u1")


def test_ui_prefs_roundtrip(home):
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 320, "composer_h": 200}})
    assert frame["ok"]
    assert frame["result"]["prefs"] == {"sidebar_w": 320, "composer_h": 200}
    assert (home / "home" / "ui.json").is_file()

    frame = call_ui(home, "ui.get")
    assert frame["ok"]
    assert frame["result"]["prefs"] == {"sidebar_w": 320, "composer_h": 200}


def test_ui_prefs_clamped_and_unknown_keys_ignored(home):
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 9999, "composer_h": 1, "evil": "x"}})
    assert frame["ok"]
    assert frame["result"]["prefs"] == {"sidebar_w": 460, "composer_h": 74}


def test_ui_prefs_ui_scale_clamped(home):
    """界面缩放：合法值直接存，越界值收敛到 70–120。"""
    frame = call_ui(home, "ui.save", {"prefs": {"ui_scale": 80}})
    assert frame["result"]["prefs"] == {"ui_scale": 80}
    frame = call_ui(home, "ui.save", {"prefs": {"ui_scale": 999}})
    assert frame["result"]["prefs"] == {"ui_scale": 120}
    frame = call_ui(home, "ui.save", {"prefs": {"ui_scale": 1}})
    assert frame["result"]["prefs"] == {"ui_scale": 70}
    # 非整数忽略，null 恢复默认（删除）
    frame = call_ui(home, "ui.save", {"prefs": {"ui_scale": "90%"}})
    assert frame["result"]["prefs"] == {"ui_scale": 70}
    frame = call_ui(home, "ui.save", {"prefs": {"ui_scale": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_sidebar_view_whitelist(home):
    """侧栏呈现形式（classic/grouped）：合法值直接存；未知值删键（theme 同款语义）；null 恢复默认。"""
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_view": "grouped"}})
    assert frame["result"]["prefs"] == {"sidebar_view": "grouped"}
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_view": "compact"}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_view": "classic"}})
    assert frame["result"]["prefs"] == {"sidebar_view": "classic"}
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_view": None}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {}


def test_agenda_time_span_ui(home):
    """日程时间段的前端契约：可设结束时间，且三种视图都按区间呈现。

    锁四件事：弹窗有「设定时间段」开关与结束时间输入；未勾选时结束时间行隐藏；
    周视图按持续时间给块高而不是固定 20px；列表/横幅走 fmtAgendaSpan（拼「起—止」）。
    后端侧的时间段行为在 test_schedule.py。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    modal = js[js.index("function agendaModal("):]
    modal = modal[:modal.index('document.getElementById("btn-agenda-add")')]
    for needle in ("ag-span-on", "ag-end", "ag-end-label", "设定时间段"):
        assert needle in modal, f"日程弹窗缺时间段节点：{needle}"
    # 结束时间早于或等于开始时间在前端先拦一道（后端也会拦）
    assert "结束时间要晚于开始时间" in modal
    assert "end_at" in modal

    # 周视图：块高由持续时间算，最少 20 分钟；按点事件仍用默认高度
    week = js[js.index("function renderWeekGrid("):]
    week = week[:week.index("function renderMonthGrid(")]
    assert "it.end_at" in week and "spanMin" in week
    assert "Math.max(20" in week, "过短的时段也要保证块能放下标题"
    assert "ag-evt" in css and ".ag-evt.span" in css

    # 列表与提醒横幅共用同一套时间段文案
    assert "function fmtAgendaSpan(" in js
    assert "fmtAgendaSpan(it)" in js
    assert "fmtAgendaSpan(item)" in js


def test_classic_view_lists_quick_chats(home):
    """经典视图底部要常驻一个「快聊」区块。

    快聊会话的 project_id 是 NULL，而经典视图只取当前项目列表，所以它们
    在那里原本一个都看不到（只在分组视图与「全部项目」搜索里露面）。
    这里锁住三处协议：session.list 下发 quick_sessions（仅本机）、
    经典视图单开一节渲染它、渲染位置在标签分组之后。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    # 经典视图渲染函数拿得到快聊数据（服务端单独下发，不混进「当前项目」）
    body = js[js.index("async function refreshSessions("):]
    body = body[:body.index("// 空会话清理入口")]
    assert "quick_sessions" in body
    assert "renderQuickSection(ul, quick_sessions" in body
    # 字段缺失（远程客户端）时不渲染那个永远为空的区块
    assert "Array.isArray(quick_sessions)" in body
    # 区块在标签分组与空提示之后渲染：不能被「暂无会话」那句盖掉
    assert body.index('暂无会话') < body.index("renderQuickSection(")
    # 常驻：没会话时也要出席（快聊区块本身不发空提示）
    quick = js[js.index("function renderQuickSection("):]
    quick = quick[:quick.index("// ---- 拖动排序")]
    assert 'groupState("quick")' in quick, "与分组视图的快聊组共用折叠状态"
    assert "session.new_task" in quick, "组头 ＋ 要能新建快聊对话"
    assert "orderedSessionList(list, \"quick\")" in quick, "组内序走同一份偏好"
    assert "GROUP_PREVIEW" in quick, "组内默认只露几条，其余收进「显示更多」"
    assert "不接拖动排序" in js, "经典视图的快聊行不能混进当前项目的拖动序"
    # 样式：组头 ＋ 的手感同分组视图
    assert ".s-quick-add" in css and ".s-quick" in css


def test_ui_prefs_left_collapsed_clamped(home):
    """左侧栏折叠态（1=折叠，折叠钮/Ctrl+B 切换）：0/1 直接存，越界收敛，null 恢复默认。"""
    frame = call_ui(home, "ui.save", {"prefs": {"left_collapsed": 1}})
    assert frame["result"]["prefs"] == {"left_collapsed": 1}
    frame = call_ui(home, "ui.save", {"prefs": {"left_collapsed": 9}})
    assert frame["result"]["prefs"] == {"left_collapsed": 1}
    frame = call_ui(home, "ui.save", {"prefs": {"left_collapsed": -3}})
    assert frame["result"]["prefs"] == {"left_collapsed": 0}
    frame = call_ui(home, "ui.save", {"prefs": {"left_collapsed": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_null_deletes_key(home):
    call_ui(home, "ui.save", {"prefs": {"sidebar_w": 300}})
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": None}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_non_numeric_ignored(home):
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": "300px", "composer_h": True}})
    assert frame["ok"]
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_pet_toggle(home):
    """对话区宠物开关：0/1 合法、越界收敛、null 恢复默认。"""
    frame = call_ui(home, "ui.save", {"prefs": {"pet": 0}})
    assert frame["result"]["prefs"] == {"pet": 0}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {"pet": 0}
    frame = call_ui(home, "ui.save", {"prefs": {"pet": 5}})
    assert frame["result"]["prefs"] == {"pet": 1}
    frame = call_ui(home, "ui.save", {"prefs": {"pet": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_pet_position(home):
    """宠物拖放位置 pet_x / pet_y：合法存、越界收敛、null 恢复默认。"""
    frame = call_ui(home, "ui.save", {"prefs": {"pet_x": 120, "pet_y": 300}})
    assert frame["result"]["prefs"] == {"pet_x": 120, "pet_y": 300}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {"pet_x": 120, "pet_y": 300}
    frame = call_ui(home, "ui.save", {"prefs": {"pet_x": 99999}})
    assert frame["result"]["prefs"] == {"pet_x": 4000, "pet_y": 300}
    # 双击归位：null 删除键
    frame = call_ui(home, "ui.save", {"prefs": {"pet_x": None, "pet_y": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_corrupt_file_tolerated(home):
    (home / "home").mkdir(parents=True, exist_ok=True)
    (home / "home" / "ui.json").write_text("{not json", encoding="utf-8")
    frame = call_ui(home, "ui.get")
    assert frame["ok"]
    assert frame["result"]["prefs"] == {}
    # 损坏文件之后仍可正常保存
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 280}})
    assert frame["result"]["prefs"] == {"sidebar_w": 280}


# ---------- 首帧外观注入：切换项目是整页 reload，首帧必须已是上次的外观 ----------


def _html_tag(html: str) -> str:
    """取 <html ...> 开标签：首帧外观注入都落在这一个标签上。"""
    start = html.index("<html")
    return html[start:html.index(">", start)]


def test_index_injects_first_paint_from_ui_json(home):
    """ui.json 的主题/缩放/栏宽写进 <html>，新页面第一帧就是夜墨 + 用户缩放与栏宽。

    theme 存的是旧版两档值 dark：读取时映射到默认深色主题「夜墨」（id: night）。
    """
    call_ui(home, "ui.save", {"prefs": {
        "theme": "dark", "ui_scale": 80, "sidebar_w": 320,
        "composer_h": 120, "right_w": 400,
    }})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme-mode="night"' in tag
    assert 'data-theme="night"' in tag
    assert "--ui-zoom:0.8" in tag
    assert "--sb-w:320px" in tag
    assert "--cp-h:120px" in tag
    assert "--rp-w:400px" in tag


def test_index_first_paint_defaults_when_prefs_absent(home):
    """没有任何偏好时不注入样式、不误标 dark（auto 由前端按系统深色判定）。"""
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme-mode="auto"' in tag
    assert "data-theme=" not in tag
    assert "--ui-zoom" not in tag


def test_index_legacy_light_maps_to_paper(home):
    """旧版两档值 light 同理映射到默认浅色主题「纸墨」（id: paper）。"""
    call_ui(home, "ui.save", {"prefs": {"theme": "light"}})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme-mode="paper"' in tag
    assert 'data-theme="paper"' in tag


def test_index_explicit_theme_id_injects_data_theme(home):
    """新版主题 id：选了哪套就首帧注入哪套，不再交由系统深浅判定。"""
    call_ui(home, "ui.save", {"prefs": {"theme": "celadon"}})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme-mode="celadon"' in tag
    assert 'data-theme="celadon"' in tag

    call_ui(home, "ui.save", {"prefs": {"theme": "pine"}})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme="pine"' in tag


def test_ui_prefs_theme_whitelist(home):
    """theme 键值域：六套主题 id + auto + 旧版 light/dark；未知值丢弃、null 恢复默认。"""
    for val in ("auto", "light", "dark", "paper", "celadon", "kaki", "night", "indigo", "pine"):
        frame = call_ui(home, "ui.save", {"prefs": {"theme": val}})
        assert frame["result"]["prefs"] == {"theme": val}
    frame = call_ui(home, "ui.save", {"prefs": {"theme": "solarized"}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"theme": None}})
    assert frame["result"]["prefs"] == {}


def test_index_tolerates_corrupt_ui_json(home):
    """ui.json 损坏不能让首页 500（宁可退回默认外观）。"""
    (home / "home").mkdir(parents=True, exist_ok=True)
    (home / "home" / "ui.json").write_text("{ not json", encoding="utf-8")
    with make_client(home, []) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "SkySheep" in r.text


# ---------- 前端首帧防闪机制：这些名字是 html / css / js 三处的隐式协议 ----------


def test_frontend_project_switch_is_in_page(home):
    """切换项目必须是站内切换，且遵循「先取数、再替换」的顺序。

    这条测试锁的是两个容易回退的行为：
    1. 不能再基于 location.reload（整页重载必然先画一屏空 DOM，
       不管拿什么遮罩去盖，用户看到的都是一屏加载中）；
    2. 不能在数据到手前先清空列表 DOM（那会渲染出一帧空列表）。
    """
    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # 去掉注释再断言：注释里会提到这些被废弃的做法（讲清为什么不能那么做）
    code = _strip_js_comments(js)

    # 不再有整页刷新/遮罩机制
    assert "location.reload" not in code
    assert "switch-veil" not in html
    assert "switch-veil" not in css
    assert "boot-hidden" not in code
    # 取数与渲染分离，且顺序是「取数 → 清状态 → 重画」
    assert "async function fetchWorkspaceData()" in code
    assert "async function applyWorkspaceData(" in code
    strip = code[code.index("async function switchProject("):]
    switch_body = strip[:strip.index("\n}\n")]
    assert switch_body.index("fetchWorkspaceData") < switch_body.index("resetWorkspaceState")
    assert switch_body.index("resetWorkspaceState") < switch_body.index("applyWorkspaceData")
    # 列表渲染支持传入预取数据（否则又要在清空后等网络）
    assert "async function refreshSessions(prefetched)" in code
    assert "async function refreshProjects(prefetched)" in code
    # 底色仍留在 html 上（浏览器模式 F5 刷新时的防白底兜底）
    assert "html { background: var(--bg); }" in css
    assert "data-theme-mode" in html


def test_grouped_view_fold_all_button_protocol(home):
    """分组视图「折叠全部项目」按钮的三处隐式协议：
    index.html 出按钮，app.css 管显隐与全收态的箭头翻转，app.js 用
    data-gkey 找回各组开合状态并在视图切换/分组渲染/搜索渲染三处同步按钮。
    名字任一处漂移按钮就失灵，这里锁住。"""
    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="btn-fold-all"' in html
    assert ".head-ops" in css and ".fold-all.expand" in css
    assert "function syncFoldAllBtn()" in js
    assert "head.dataset.gkey" in js and "groupState(h.dataset.gkey)" in js
    # 三个调用点：applySidebarView（视图切换）、refreshSessionsGrouped（渲染尾部）、
    # renderSessionList（搜索结果没有组，按钮要随之隐藏）
    assert js.count("syncFoldAllBtn();") >= 3


def _strip_js_comments(src: str) -> str:
    """去掉 // 行注释与 /* */ 块注释（字符串里的 // 简化不处理，够用）。"""
    out = []
    i = 0
    n = len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def test_frontend_workspace_reset_covers_project_bound_state(home):
    """切换后旧项目的状态必须被清干净（漏清比加载屏更糟：会显示错的数据）。"""
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    reset = js[js.index("function resetWorkspaceState()"):]
    reset_body = reset[:reset.index("\n}\n")]
    for name in (
        "chatTabs", "activeTab", "sessionMeta", "currentSessionId",
        "sessionSearchActive", "fileIndex", "providerCfg", "bootSnap",
        "resetProjectPanels",
    ):
        assert name in reset_body, f"resetWorkspaceState 漏清 {name}"
    panels = js[js.index("function resetProjectPanels()"):]
    panels_body = panels[:panels.index("\n}\n")]
    for name in ("filesLoaded", "agCache", "tasksTimer", "resetTermTabs"):
        assert name in panels_body, f"resetProjectPanels 漏清 {name}"


def test_roundtable_menu_opens_on_left_and_keeps_actions_reachable(home):
    """圆桌成员浮层的两条布局约定：靠左对齐、操作行常驻可点。

    此前浮层贴的是主区右上角（right:18px）：既远离触发它的圆桌按钮，
    又会在窄窗口被右缘裁切；且 max-height(360) 小于内容高度，
    底部「确定」会被滚出可视区、必须先滚动才能确认。
    """
    from skysheep.server.app import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    rt_rule = css[css.index(".rt-menu {"):]
    rt_rule = rt_rule[:rt_rule.index("}")]
    assert "left:" in rt_rule
    assert "right: auto" in rt_rule, "浮层不能再固定贴右"
    assert "max-width: calc(100% - 36px)" in rt_rule, "窄窗口要能收敛"
    # 操作行常驻底部
    act = css[css.index(".rt-menu-actions {"):]
    act = act[:act.index("}")]
    assert "position: sticky" in act
    assert "bottom:" in act
    # 定位是实时算的，且做容器内收敛
    assert "function positionRtMenu(" in js
    assert "positionRtMenu(menu)" in js
    body = js[js.index("function positionRtMenu("):]
    body = body[:body.index("\n}\n")]
    assert "rt-switch" in body and "chat-main" in body
    assert "uiScale" in body, "物理像素要换算回布局坐标"
    assert "Math.min" in body and "Math.max" in body, "要有容器内收敛"


# ---------- project_order：分组视图拖动排序的持久化与 project.list 生效 ----------


def _list_projects(home, wid):
    frame = call_ui(home, "project.list")
    assert frame["ok"], frame
    return frame["result"]["projects"], wid


def test_ui_prefs_project_order_roundtrip_and_cleanup(home):
    """project_order：合法 id 数组直接存；去重、去非正数/非整数、null 删键。"""
    frame = call_ui(home, "ui.save", {"prefs": {"project_order": [3, 1, 2]}})
    assert frame["result"]["prefs"] == {"project_order": [3, 1, 2]}
    # 去重 + 丢弃非法项（bool 是 int 子类也要挡掉）
    frame = call_ui(home, "ui.save", {"prefs": {"project_order": [2, 2, 0, -1, "5", True, 7]}})
    assert frame["result"]["prefs"] == {"project_order": [2, 7]}
    # 全无效 → 删键（视为未自定义排序）
    frame = call_ui(home, "ui.save", {"prefs": {"project_order": ["x", 0, -3]}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"project_order": None}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"project_order": "1,2,3"}})
    assert frame["result"]["prefs"] == {}


def test_project_list_follows_saved_order(home):
    """拖动排序生效：project.list 按 project_order 返回；名单外的项目仍按原序垫底。"""
    proj2 = home / "proj2"
    proj2.mkdir()
    proj3 = home / "proj3"
    proj3.mkdir()
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "sw2", "method": "project.switch", "params": {"path": str(proj2)}})
        assert recv_until(ws, "sw2")["ok"]
        ws.send_json({"id": "sw3", "method": "project.switch", "params": {"path": str(proj3)}})
        assert recv_until(ws, "sw3")["ok"]
        ws.send_json({"id": "pl1", "method": "project.list", "params": {}})
        projects = recv_until(ws, "pl1")["result"]["projects"]
        # 无保存序：默认 created_at DESC → proj3, proj2, proj
        assert [p["name"] for p in projects] == ["proj3", "proj2", "proj"]
        ids = {p["name"]: p["id"] for p in projects}

        # 保存拖动后的顺序：proj 排最前，其余跟随
        order = [ids["proj"], ids["proj3"], ids["proj2"]]
        frame = call_ui(home, "ui.save", {"prefs": {"project_order": order}})
        assert frame["result"]["prefs"] == {"project_order": order}

        ws.send_json({"id": "pl2", "method": "project.list", "params": {}})
        projects = recv_until(ws, "pl2")["result"]["projects"]
        assert [p["name"] for p in projects] == ["proj", "proj3", "proj2"]

        # 名单外的新项目（proj4）不被旧偏好藏掉：追加在已排序尾部
        proj4 = home / "proj4"
        proj4.mkdir()
        ws.send_json({"id": "sw4", "method": "project.switch", "params": {"path": str(proj4)}})
        assert recv_until(ws, "sw4")["ok"]
        ws.send_json({"id": "pl3", "method": "project.list", "params": {}})
        projects = recv_until(ws, "pl3")["result"]["projects"]
        assert [p["name"] for p in projects] == ["proj", "proj3", "proj2", "proj4"]


def test_ui_prefs_session_order_roundtrip_and_cleanup(home):
    """session_order（组内会话拖动序）：合法字典直接存；非字符串 id / 空组剔除；null 删键。"""
    good = {"12": ["s3", "s1", "s2"], "quick": ["a", "b"]}
    frame = call_ui(home, "ui.save", {"prefs": {"session_order": good}})
    assert frame["result"]["prefs"] == {"session_order": good}
    # 非字符串 id 剔除、空组剔除、数字键转字符串；全部无效 → 删键
    frame = call_ui(home, "ui.save", {"prefs": {"session_order": {
        "5": ["ok", 3, None, ""], "": ["x"], "bad": "not-a-list",
    }}})
    assert frame["result"]["prefs"] == {"session_order": {"5": ["ok"]}}
    frame = call_ui(home, "ui.save", {"prefs": {"session_order": {"x": [1, 2]}}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"session_order": "nolist"}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"session_order": None}})
    assert frame["result"]["prefs"] == {}
    # 整体替换语义：第二次保存只含新组，旧组不残留
    call_ui(home, "ui.save", {"prefs": {"session_order": good}})
    frame = call_ui(home, "ui.save", {"prefs": {"session_order": {"7": ["z"]}}})
    assert frame["result"]["prefs"] == {"session_order": {"7": ["z"]}}
