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


def test_agenda_week_drag_select_ui(home):
    """周视图的拖选/单击交互契约：拖动圈时段、单击选时点、就地快建气泡、
    已有时段块可拖边缘改时长。

    同样按「读源码断言关键节点」的方式锁，理由与 test_agenda_time_span_ui
    相同：这些是纯前端交互（无构建链、无需 node 环境），后端只能验到落库。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    # 拖动走 pointer 事件 + 指针捕获（鼠标移出网格也不丢）
    assert "setPointerCapture" in js
    assert "pointercancel" in js, "拖动要能被取消（不掉一个半成品）"
    assert "AG_SNAP_MIN" in js and "AG_CLICK_MIN" in js, "拖动 5 分钟吸附、单击 30 分钟取整"
    assert "pointermove" in js

    week = js[js.index("function renderWeekGrid("):]
    week = week[:week.index("function renderMonthGrid(")]
    assert "bindAgendaCanvas(canvas, days)" in week, "空白处拖选/单击统一入口"
    assert "bindAgendaEvtDrag(el, days)" in week, "已有块要能拖边缘/拖身"
    assert "ag-draft" in week or "ag-draft" in js, "拖动中要有幽灵块"
    assert "closeAgendaPop" in week, "重渲染前先收气泡"

    # 就地快建气泡：回车保存、Esc 关闭、空标题变灰、可转完整弹窗
    pop = js[js.index("function agendaQuickCreate("):]
    pop = pop[:pop.index("function agendaModal(")]
    for needle in ("ag-pop-title", "ag-pop-start", "ag-pop-end", "ag-pop-remind", "schedule.add"):
        assert needle in pop, f"快建气泡缺节点/调用：{needle}"
    assert '"Enter"' in pop and '"Escape"' in pop
    assert "更多选项" in pop, "要能转成完整弹窗"
    assert "结束时间要晚于开始时间" in pop, "区间倒置在气泡里先拦一道"

    # 拖边缘改时长：手柄只在时间段块上出现，且按点事件不给手柄
    assert "ag-grip up" in js and "ag-grip down" in js
    assert "拖动改开始时间" in js and "拖动改结束时间" in js
    assert "schedule.update" in js
    # 拖动中只改内联 top/height，松手才落库一次
    evt = js[js.index("function bindAgendaEvtDrag("):]
    evt = evt[:evt.index("function renderMonthGrid(")]
    assert "el.style.top" in evt and "el.style.height" in evt
    assert "_agDragged" in evt, "拖完那一下不应顺手打开编辑弹窗"

    # 样式：幽灵块、气泡、手柄、触屏不被网格滚动吃掉
    assert ".ag-draft" in css and "#ag-pop" in css
    assert ".ag-evt .ag-grip" in css
    assert "touch-action: none" in css, "触屏拖选不能被当成滚动"


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
    # 区块渲染位置：快聊区块在会话列表的标签分组之后渲染，不会被分组头盖掉。
    # 不断言具体文案——空提示已经整体删掉（会话列表为空就留空，不再写引导文字），
    # 靠字面量锁的断言会随文案消失而失真。
    assert body.index("groups.forEach(({ tag, list })") < body.index("renderQuickSection(")
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


def test_tab_rename_protocol(home):
    """标签栏可命名：双击行内改名 + 右键菜单，改名走 session.rename 并同步侧栏。

    锁四件事：
      ① renderTabs 挂双击与右键，提示文案含「双击重命名」；
      ② 行内改名收口到 renameTabSession（一处定义，列表与标签不各写一份落库）；
      ③ 改名后更新 sessionMeta 并 refreshSessions（侧栏同步）；
      ④ 空标签预命名：send() 创建会话时把名字落库并关掉首轮自动标题
         （tab.titleFixed → wants_title 必须为 false，否则用户的名字一发就被冲掉）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    # ① 入口：双击 + 右键菜单
    tabs = js[js.index("function renderTabs("):]
    tabs = tabs[:tabs.index("// 方向键在标签间切换")]
    assert "el.ondblclick" in tabs and "startTabRename" in tabs
    assert "el.oncontextmenu" in tabs and "showTabMenu" in tabs
    assert "双击重命名" in tabs
    # 菜单复用侧栏会话行的同一个 #session-menu（一套外观与关闭逻辑）
    menu = js[js.index("function showTabMenu("):]
    menu = menu[:menu.index("\n}\n")]
    assert "menuEl" in menu and "重命名" in menu and "closeTab" in menu
    # 页签常驻：renderTabs 不再按数量隐藏（单会话也显示，浏览器式）
    assert 'classList.toggle("hidden", chatTabs.length' not in tabs, \
        "标签栏改常驻后不得再按数量隐藏"
    # 标签栏尾部有 ＋ 新建入口
    assert "tab-new" in tabs and "startNewTab" in tabs

    # ② 行内改名与落库
    assert "function startTabRename(" in js
    assert "function renameTabSession(" in js
    rename = js[js.index("async function renameTabSession("):]
    rename = rename[:rename.index("\n}\n")]
    assert 'request("session.rename"' in rename
    assert "sessionMeta" in rename and "refreshSessions" in rename
    # 样式：标签内改名框（与列表行内改名同一语言）+ 悬浮胶囊页签（用户拍板的定稿：
    # 页签浮在墨线上方、墨线连续，激活页签是带描边与硬阴影的卡纸胶囊。
    # 「压线截断」与「开窗立柱」两种融合形态都试过，用户最终选了悬浮胶囊——
    # 不要再改回任何下探/融合形态）
    assert ".chat-tab .rename-inline" in css
    tabs_block = css.split("#chat-tabs {")[1].split("}")[0]
    assert "margin-bottom: -12px" not in tabs_block and "align-self: stretch" not in tabs_block, \
        "页签不得下探压线（悬浮胶囊形态：墨线从头到尾连续）"
    active_block = css.split(".chat-tab.active")[1].split("}")[0]
    assert "background: var(--paper-raised)" in active_block
    assert "border-color: var(--line2)" in active_block
    assert "box-shadow: var(--shadow-sm)" in active_block
    assert ".chat-tab.needs-perm { color: var(--gold)" in css, "等确认标签用金色标题示意"

    # ③ 空标签预命名：创建会话时带标题 + 压制首轮自动标题
    send = js[js.index("recordInputHistory(text);"):]
    send = send[:send.index("input.value = \"\";")]
    assert 'request("session.new", preNamed ? { title: preNamed } : {})' in send
    assert "tab.firstSend = !preNamed" in send, "预命名过就不能再请求自动标题"
    assert "titleFixed" in send


def test_tab_drag_order_protocol(home):
    """页签拖动排序：tab_order 偏好 + 水平拖拽 + 渲染排序三件套。

    锁住：
      ① 后端 ui.save 收 tab_order（sid 字符串数组，去重封顶）；
      ② renderTabs 按 tab_order 排、空标签垫底（无 sid 不参与排序）；
      ③ 页签接水平拖拽（wireTabDrag），落点指示用左右缘竖线；
      ④ 提交时从标签栏 DOM 收集当前序一并入偏好（避免新旧两段顺序错乱）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    backend = (STATIC_DIR.parent / "backend.py").read_text(encoding="utf-8")

    # ① 后端：tab_order 进 ui.save 白名单 + 清洗（字符串数组、去重、封顶 200）
    assert 'data.get("tab_order")' in backend
    assert '"tab_order"' in backend
    writer = backend[backend.index("async def _write_ui_prefs("):]
    # 边界：到落盘调用为止（写入本身改走 write_text_atomic，见 M13）
    writer = writer[:writer.index("write_text_atomic(")]
    branch = writer[writer.index('if key == "tab_order"'):]
    branch = branch[:branch.index("\n                continue")]
    assert "len(seen_tab) >= 200" in branch, "页签序要封顶，防异常超大载荷"

    # ② 渲染排序：偏好序优先，空标签垫底
    tabs = js[js.index("function renderTabs("):]
    tabs = tabs[:tabs.index("// 方向键在标签间切换")]
    assert "tabOrderPrefs.forEach" in tabs
    assert 'chatTabs.filter((t) => !t.sid)' in tabs, "空标签不参与排序，始终垫底"

    # ③ 拖拽接线与水平落点
    assert "function wireTabDrag(" in js
    assert "function dragHalfPosX(" in js, "页签落点按水平中线分左右"
    assert ".chat-tab.drop-left" in css and ".chat-tab.drop-right" in css
    # 可拖的只有有 sid 的页签（空标签垫底占位，不可拖）
    assert "if (t.sid) wireTabDrag(el, t);" in js

    # ④ 提交收序：读 DOM 现序 + 去重 + 落 ui.save
    assert "function commitTabOrder(" in js
    commit = js[js.index("function commitTabOrder("):]
    commit = commit[:commit.index("\n}\n")]
    assert 'x.el === el' in commit
    assert 'request("ui.save", { prefs: { tab_order: keys } })' in commit


def test_tab_takes_over_blank_placeholder(home):
    """打开真会话时要接管已有空标签，不能旁边再叠一张。

    回归：启动时没有历史会话会留一张空标签（欢迎页），此时点快聊组头的 ＋
    新建会话，旧代码直接 push 新标签，界面上就是「点了一下 ＋，冒出两个会话」。
    这里锁住四条不变量：
      ① 接管的是空标签（isBlankTab：无 sid、不 running），正在跑的不算；
      ② 优先接管当前激活那张，否则激活的空标签会赖在栏上；
      ③ 接管后要补上标签名与 currentSessionId（activateTab 对「已是当前标签」
         会早退，这些记账不做就永远停在「新会话」）；
      ④ 连点「新建会话」不堆空标签（startNewTab 先复用）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # ① 空标签的判定：无 sid 且没在跑（跑着的正懒创建会话，抢走会劫持运行）
    assert "function isBlankTab(" in js
    blank = js[js.index("function isBlankTab("):]
    blank = blank[:blank.index("\n}")]
    assert "!t.sid" in blank and "!t.running" in blank

    # ② 优先接管当前激活那张，否则退回「任一空标签」
    open_fn = js[js.index("function openTabForSession("):]
    open_fn = open_fn[:open_fn.index("\n}\n")]
    assert "isBlankTab(activeTab)" in open_fn, "接管要先看当前激活标签"
    assert "chatTabs.find(isBlankTab)" in open_fn, "没有激活空标签时退回任一空标签"
    assert "const blank =" in open_fn
    # 没有空标签才新建，且新建时 push 到 chatTabs
    assert "t = newTabObj(sid, title)" in open_fn and "chatTabs.push(t)" in open_fn

    # ③ 接管的是当前激活标签时，补上记账（否则标签名停在「新会话」）
    assert "adopted && t === activeTab" in open_fn
    assert "currentSessionId = t.sid" in open_fn
    assert "loadTabHistory" in open_fn, "接管后要补拉历史（activateTab 会早退）"

    # ④ 连点「新建会话」不堆空标签
    start = js[js.index("function startNewTab("):]
    start = start[:start.index("\n}\n")]
    assert "isBlankTab(activeTab)" in start, "已在空标签上时不应再叠一张"
    assert start.index("isBlankTab(activeTab)") < start.index("chatTabs.push(t)"), \
        "复用判定必须在新建之前"

    # 历史加载收口到一处：activateTab 与 openTabForSession 共用，不再各写一份
    assert "async function loadTabHistory(" in js
    activate = js[js.index("async function activateTab("):]
    activate = activate[:activate.index("\n}\n")]
    assert "await loadTabHistory(tab)" in activate, "activateTab 要走共用的历史加载"


def test_sidebar_no_project_state_and_empty_text(home):
    """无项目（快聊）态的三处隐式协议：动作行、快聊按钮文案、空提示不自带「假可点」。

    1. 「添加项目」唯一入口常驻「会话」标题行右端（两种视图共用同一个按钮，
       分组视图下排在视图切换、折叠钮之后）；「项目」分栏标题保留但不再另设按钮。
    2. 搜索不再有「本项目/全部项目」切换器：搜索固定跨全部项目（含快聊），
       跨项目命中自带项目名标注、点击自动切过去。此前的切换器在无项目态
       会说一个并不存在的「本项目」，且分组视图本来也固定按全部项目搜，
       两个视图统一后这个开关没有存在的意义。
    3. 空提示不是可点项（全项目搜过，无一处绑 onclick），不能承接
       .list li 的 pointer 光标与悬浮蓝底。
    """
    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # 1. 添加项目钮两种视图共用（session-add-project），位置随视图搬移：
    #    经典视图在「项目」标题行右端（第一条横线右方），分组视图回「会话」行。
    #    DOM 是单实例移动（appendChild），监听与状态天然保留。
    assert 'id="session-add-project"' in html
    assert 'getElementById("session-add-project").onclick = projectModal' in js
    assert 'id="project-head-ops"' in html and 'id="session-head-ops"' in html
    assert 'dst.appendChild(vt)' in js and 'dst.appendChild(addBtn)' in js, "按视图搬移按钮组"
    assert "btn-project-add" not in html and "btn-project-add" not in js
    assert "project-add-row" not in html and "project-add-row" not in js
    assert "project-add-row" not in css
    assert "#project-section.empty .sec-head" not in css, "标题行不再随空态隐藏"
    assert 'sec.classList.toggle("empty"' in js
    # 分组视图列表尾的旧入口已删；折叠钮在 DOM 序上位于切换钮之前（渲染时 ⌄ 在 📁 左侧）
    assert "grouped-add" not in html and "grouped-add" not in js and "grouped-add" not in css
    assert "pgroup-add" not in js and "pgroup-add" not in css, "列表尾旧入口已删"
    assert html.index('id="btn-fold-all"') < html.index('id="view-toggle"'), "⌄ 在 📁 左侧"
    # 裸 .add-row 是设置页「手动填模型 ID」行的类名，不允许再带 display:none 波及它
    assert css.count(".add-row {") == 1

    # 2. 搜索范围切换器已删：请求固定 scope=all，跨项目命中标注项目名并自动切换
    assert "search-scope" not in html and "search-scope" not in js and "search-scope" not in css
    assert "syncScopeLabels" not in js
    assert 'scope: "all"' in js, "搜索固定跨全部项目"
    assert "s-project" in js, "跨项目命中要标出来源项目"

    # 3. 空提示摘掉光标与悬浮反馈
    assert ".list li.empty-hint { cursor: default; }" in css
    assert ".list li.empty-hint:hover { background: none; }" in css

    # 4. 空组头的 ＋ 常显（空的时候它是唯一的建会话入口）
    assert ".is-empty" in js and "s-quick.is-empty .s-quick-add" in css
    assert "pgroup-head.is-empty .pg-add" in css

    # 5. 不报数字：会话标题与各分组头都不挂条数——列表本身按行陈列，
    #    标题旁再挂个数字是噪声（2026-09-22 应用户要求移除）
    assert 'session-count' not in html and 'session-count' not in js
    assert "syncSessionCount" not in js
    assert "s-group-count" not in js and "s-group-count" not in css
    assert "pg-count" not in js and "pg-count" not in css

    # 6. 切换器的选中态样式随之退场（整个 #search-scope 已不存在，见第 2 组）
    assert "#search-scope" not in css

    # 7. 视图切换合并成单按钮：图标显示当前所在视图（data-current），
    #    点击切换、图标翻到另一个；旧的双按钮 view-seg 整体退场
    assert 'id="view-toggle"' in html
    assert "session-view" not in html and 'class="view-seg"' not in html
    assert "view-seg" not in css, "双按钮分段样式随合并退场"
    assert 'vt.dataset.current' in js and 'getElementById("view-toggle").onclick' in js
    assert 'data-current="classic"' in html, "初始态与 applySidebarView 的赋值字段一致"


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


def test_right_tab_restore_loads_data_on_startup(home):
    """启动恢复上次打开的右面板标签时，必须把数据一并拉回来。

    以前 initUiPrefs 只对 ext 做了处理（卡片要搬进面板），其余标签又画了一层
    空壳——重启后停在日程页看到的是空白网格、连日期范围都是空的，得先点别的
    标签再点回来才有内容。这里锁三件事：
      ① 加载入口统一到 RIGHT_TAB_LOADERS / loadRightTab（不再各写一份 if 链）；
      ② initUiPrefs 恢复标签时逐个 loadRightTab；
      ③ 收起/展开与切项目重拉也走同一条路。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # ① 一张表覆盖所有需要拉数据的标签（含日程，正是漏掉的那个）
    table = js[js.index("const RIGHT_TAB_LOADERS = {"):]
    table = table[:table.index("};\n")]
    for tab in ("files", "tasks", "agenda", "ptasks", "cron", "pipeline", "review", "memory", "ext"):
        assert f"\n  {tab}:" in table, f"RIGHT_TAB_LOADERS 缺标签 {tab}"
    assert "function loadRightTab(" in js

    # ② 启动恢复：每个恢复出来的标签都要拉数据（不能只对 ext 特珠处理）
    init = js[js.index("async function initUiPrefs()"):]
    init = init[:init.index("// ----------")]
    assert "for (const id of rightTabs) loadRightTab(id)" in init, \
        "启动恢复标签时没拉数据：重启后日程页会是空白网格"

    # openRightTab / activateRightTab 不再各自维护一份 if 链
    for fn in ("function openRightTab(", "function activateRightTab("):
        body = js[js.index(fn):]
        body = body[:body.index("\n}\n")]
        assert "loadRightTab(id)" in body, f"{fn} 没走统一加载入口"
        assert 'if (id === "agenda") loadAgenda()' not in body, f"{fn} 还留着旧 if 链"

    # ③ 切项目重拉同样走统一入口，且面板收起时不早退（否则展开即旧内容）
    reload_body = js[js.index("async function reloadProjectPanels("):]
    reload_body = reload_body[:reload_body.index("\n}\n")]
    assert "loadRightTab(id, true)" in reload_body
    assert "if (!rightTabs.length || rightCollapsed) return" not in reload_body, \
        "面板收起时早退：切项目后展开会看到上一个项目的内容"


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
        # 无保存序：默认 created_at DESC → proj3, proj2, proj；
        # 固定项目「远程连接」启动时最早创建，垫底
        assert [p["name"] for p in projects] == ["proj3", "proj2", "proj", "远程连接"]
        ids = {p["name"]: p["id"] for p in projects}

        # 保存拖动后的顺序：proj 排最前，其余跟随
        order = [ids["proj"], ids["proj3"], ids["proj2"]]
        frame = call_ui(home, "ui.save", {"prefs": {"project_order": order}})
        assert frame["result"]["prefs"] == {"project_order": order}

        ws.send_json({"id": "pl2", "method": "project.list", "params": {}})
        projects = recv_until(ws, "pl2")["result"]["projects"]
        assert [p["name"] for p in projects] == ["proj", "proj3", "proj2", "远程连接"]

        # 名单外的新项目（proj4）不被旧偏好藏掉：追加在已排序尾部
        proj4 = home / "proj4"
        proj4.mkdir()
        ws.send_json({"id": "sw4", "method": "project.switch", "params": {"path": str(proj4)}})
        assert recv_until(ws, "sw4")["ok"]
        ws.send_json({"id": "pl3", "method": "project.list", "params": {}})
        projects = recv_until(ws, "pl3")["result"]["projects"]
        assert [p["name"] for p in projects] == ["proj", "proj3", "proj2", "proj4", "远程连接"]


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


def test_remote_project_fixed_entry(home):
    """「远程连接」固定项目的前端退化：无真实目录（root_path 报空）的行不切项目。

    后端把它的 root_path 统一报空（哨兵记录，不指向磁盘）：经典视图的项目行
    点击改为打开名下最近的会话，且不显示删除钮；分组视图组内会话直接打开
    （renderProjectGroup 的 rootPath && !isCurrent 分支天然跳过，不切工作目录）。
    """
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # 经典视图：root_path 为空即固定项目；不切项目、不显示删除钮
    assert "const remote = !p.root_path;" in js
    assert 'if (remote) li.classList.add("remote-fixed")' in js
    assert "「远程连接」还没有对话" in js, "没有对话时给出去渠道发一条的提示"
    assert "openTabForSession(list[0].id" in js, "点击打开名下最近的会话"
    # 点击处理里固定项目走打开分支，而不是 switchProject（锚在 refreshProjects 区域内）
    seg = js[js.index("async function refreshProjects("):]
    seg = seg[:seg.index("function deleteProjectModal(")]
    assert "if (remote) {" in seg and "switchProject(p.root_path)" in seg
    # 删除钮只在非固定项目上创建（固定项目不提供删除）
    assert "if (!remote) {" in seg
    # 分组视图：组头 ✕ 同样只对真实项目（有 rootPath）渲染
    grp = js[js.index("function renderProjectGroup("):]
    grp = grp[:grp.index("head.onclick")]
    assert "(project && rootPath" in grp, "固定项目（rootPath 空）不渲染 ✕"


def test_ui_prefs_notify_kinds_and_sound_focus(home):
    """通知类型开关与提示音作用域：0/1 直接存，越界收敛，null 删键回默认（都开 / 仅失焦）。"""
    frame = call_ui(home, "ui.save", {"prefs": {
        "notify_kind_done": 0, "notify_kind_perm": 1, "notify_sound_focus": 0,
    }})
    assert frame["result"]["prefs"] == {
        "notify_kind_done": 0, "notify_kind_perm": 1, "notify_sound_focus": 0,
    }
    frame = call_ui(home, "ui.save", {"prefs": {
        "notify_kind_done": 5, "notify_kind_perm": True, "notify_sound_focus": -3,
    }})
    # bool 不是合法整型偏好（忽略，保留上次存的 1）；越界收敛到 0/1
    assert frame["result"]["prefs"] == {
        "notify_kind_done": 1, "notify_kind_perm": 1, "notify_sound_focus": 0,
    }
    frame = call_ui(home, "ui.save", {"prefs": {
        "notify_kind_done": None, "notify_kind_perm": None, "notify_sound_focus": None,
    }})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_pet_scale_clamped(home):
    """宠物大小：60–140 收敛，null 删键回默认。"""
    frame = call_ui(home, "ui.save", {"prefs": {"pet_scale": 120}})
    assert frame["result"]["prefs"] == {"pet_scale": 120}
    frame = call_ui(home, "ui.save", {"prefs": {"pet_scale": 300}})
    assert frame["result"]["prefs"] == {"pet_scale": 140}
    frame = call_ui(home, "ui.save", {"prefs": {"pet_scale": 10}})
    assert frame["result"]["prefs"] == {"pet_scale": 60}
    frame = call_ui(home, "ui.save", {"prefs": {"pet_scale": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_theme_auto_mapping_whitelist(home):
    """「跟随系统」深浅落点：只收浅色三套 / 深色三套，未知值删键。"""
    frame = call_ui(home, "ui.save", {"prefs": {
        "theme_auto_light": "celadon", "theme_auto_dark": "pine",
    }})
    assert frame["result"]["prefs"] == {
        "theme_auto_light": "celadon", "theme_auto_dark": "pine",
    }
    # 深色落点不能填浅色主题，反之亦然
    frame = call_ui(home, "ui.save", {"prefs": {
        "theme_auto_light": "night", "theme_auto_dark": "paper",
    }})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {
        "theme_auto_light": None, "theme_auto_dark": None,
    }})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_notif_log_roundtrip(home):
    """通知中心日志：合法条目原样存取；空数组删键（清空语义）。"""
    log = [
        {"ts": 1000.5, "title": "任务完成", "body": "回来看看结果", "kind": "done"},
        {"ts": 999.0, "title": "需要你确认", "body": "run_command 等待决定", "kind": "perm"},
    ]
    frame = call_ui(home, "ui.save", {"prefs": {"notif_log": log}})
    assert frame["result"]["prefs"]["notif_log"] == log
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"]["notif_log"] == log
    # 空数组 = 清空：删键，重启后从零开始
    frame = call_ui(home, "ui.save", {"prefs": {"notif_log": []}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.get")
    assert "notif_log" not in frame["result"]["prefs"]


def test_ui_prefs_notif_log_cleaned(home):
    """通知中心日志清洗：非 dict/无 title 丢弃、字段截断、封顶 60 条；sid 不收。"""
    bad = [
        "junk",  # 非 dict
        {"title": ""},  # 空 title
        {"ts": 5, "title": "t" * 500, "body": "b" * 900, "kind": "verylongkind" * 9,
         "sid": "should-not-persist"},
    ]
    frame = call_ui(home, "ui.save", {"prefs": {"notif_log": bad}})
    prefs = frame["result"]["prefs"]["notif_log"]
    assert len(prefs) == 1
    entry = prefs[0]
    assert entry["title"] == "t" * 120
    assert entry["body"] == "b" * 300
    assert len(entry["kind"]) <= 16
    assert "sid" not in entry
    # 封顶 60
    flood = [{"ts": i, "title": f"n{i}", "body": "", "kind": "done"} for i in range(100)]
    frame = call_ui(home, "ui.save", {"prefs": {"notif_log": flood}})
    assert len(frame["result"]["prefs"]["notif_log"]) == 60
    # 整个值不是数组：删键
    frame = call_ui(home, "ui.save", {"prefs": {"notif_log": "oops"}})
    assert "notif_log" not in frame["result"]["prefs"]


def test_index_first_paint_injects_auto_mapping(home):
    """auto 主题的首帧注入带上深浅落点，首帧脚本不必等 ui.get 就能按映射落主题。"""
    call_ui(home, "ui.save", {"prefs": {
        "theme_auto_light": "kaki", "theme_auto_dark": "indigo",
    }})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme-mode="auto"' in tag
    assert 'data-theme-auto-light="kaki"' in tag
    assert 'data-theme-auto-dark="indigo"' in tag
