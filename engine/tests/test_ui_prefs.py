"""ui.get / ui.save：界面布局偏好（侧栏宽、输入区高）的持久化。"""

from __future__ import annotations

import re

from conftest import read_app_bundle
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


def test_welcome_card_pref_roundtrip_and_default(home):
    """欢迎卡偏好：默认开（键缺省时不出现在 prefs）、可关、可恢复默认（null 删除）。"""
    # 默认开：前端读不到键即视为开；这里只验证存储面的「未写入=无键」
    frame = call_ui(home, "ui.get")
    assert frame["ok"]
    assert "welcome_card" not in frame["result"]["prefs"]

    # 关闭
    frame = call_ui(home, "ui.save", {"prefs": {"welcome_card": 0}})
    assert frame["ok"]
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"]["welcome_card"] == 0

    # 非法值收敛到合法区间
    frame = call_ui(home, "ui.save", {"prefs": {"welcome_card": 7}})
    assert frame["ok"]
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"]["welcome_card"] == 1

    # 恢复默认（null = 删除该键）
    frame = call_ui(home, "ui.save", {"prefs": {"welcome_card": None}})
    assert frame["ok"]
    frame = call_ui(home, "ui.get")
    assert "welcome_card" not in frame["result"]["prefs"]


def test_welcome_card_wiring_source_contract(home):
    """源码契约：设置卡、偏好键、showWelcome 出口守卫三处接线都在。"""
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static"
    js = read_app_bundle()
    html = (static / "index.html").read_text(encoding="utf-8")
    # 后端键
    from skysheep.server.backend import ServerBackend
    assert "welcome_card" in ServerBackend.UI_PREFS_LIMITS
    # 前端：开关元素、偏好应用、showWelcome 出口守卫
    assert 'id="welcome-card-toggle"' in html
    assert 'prefs.welcome_card == null ? true : prefs.welcome_card === 1' in js
    assert "if (!welcomeOn)" in js
    # 关闭时开关即时清空当前空白视图
    assert "if (welcomeOn) showWelcome();" in js
    # 两条会话打开路径（withMessages / 懒加载）都要有空会话欢迎卡兜底——
    # 之前漏了懒加载那条，侧栏点开旧的空会话就是一片空白
    assert js.count('if (!tab.logEl.children.length) withTab(tab, showWelcome);') == 1
    assert "if (!t.logEl.children.length) withTab(t, showWelcome);" in js


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


def test_team_last_seen_pref_roundtrip(home):
    """团队室未读水位（团队模式设计 §15.4）：{team_id: last_seen_seq} 随 ui.save
    既有通道按队伍持久化，badge = latest_seq − last_seen_seq 由前端求差。
    键内清洗：非数水位逐条跳过、负数收敛 0、条目封顶；整键替换语义、
    空表/null 删键（= 未读回「全计」默认）。"""
    frame = call_ui(home, "ui.save", {"prefs": {"team_last_seen": {"team-abc": 7}}})
    assert frame["ok"]
    assert frame["result"]["prefs"]["team_last_seen"] == {"team-abc": 7}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"]["team_last_seen"] == {"team-abc": 7}

    # 同键再存是整键替换（前端发整表镜像），不做条目级合并
    frame = call_ui(home, "ui.save", {"prefs": {"team_last_seen": {"team-xyz": 3}}})
    assert frame["result"]["prefs"]["team_last_seen"] == {"team-xyz": 3}

    # 脏条目逐条跳过（非数/布尔），合法条目保留；负数收敛 0
    frame = call_ui(home, "ui.save", {"prefs": {"team_last_seen": {
        "team-xyz": "9", "team-bad": True, "team-neg": -5, "team-ok": 2}}})
    assert frame["result"]["prefs"]["team_last_seen"] == {"team-neg": 0, "team-ok": 2}

    # 条目封顶 100：异常超大载荷不收
    frame = call_ui(home, "ui.save", {"prefs": {"team_last_seen": {
        f"team-{i}": i for i in range(105)}}})
    assert len(frame["result"]["prefs"]["team_last_seen"]) == 100

    # 其他偏好照常共存（水位键不挤掉别的键，别的保存也不冲掉水位键）
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 320}})
    assert frame["result"]["prefs"]["sidebar_w"] == 320
    assert "team_last_seen" in frame["result"]["prefs"]

    # null 与空表删键（= 未读回「全计」默认）
    frame = call_ui(home, "ui.save", {"prefs": {"team_last_seen": None}})
    assert "team_last_seen" not in frame["result"]["prefs"]
    frame = call_ui(home, "ui.save", {"prefs": {"team_last_seen": {}}})
    assert "team_last_seen" not in frame["result"]["prefs"]
    frame = call_ui(home, "ui.get")
    assert "team_last_seen" not in frame["result"]["prefs"]


def test_agenda_time_span_ui(home):
    """日程时间段的前端契约：可设结束时间，且三种视图都按区间呈现。

    锁四件事：弹窗有「设定时间段」开关与结束时间输入；未勾选时结束时间行隐藏；
    周视图按持续时间给块高而不是固定 20px；列表/横幅走 fmtAgendaSpan（拼「起—止」）。
    后端侧的时间段行为在 test_schedule.py。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
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

    js = read_app_bundle()
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
    """经典视图要能看到快聊会话（快聊提升为项目区一行）。

    快聊会话的 project_id 是 NULL，而经典视图只取当前项目列表，所以它们
    在那里原本一个都看不到（只在分组视图与「全部项目」搜索里露面）。
    这里锁三处协议：session.list 下发 quick_sessions（仅本机）、经典视图
    点「快聊」行陈列快聊列表（含空列表引导）、陈列函数与分组视图共用
    折叠/显示更多状态且不接拖动排序。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    # 经典视图渲染函数拿得到快聊数据（服务端单独下发，不混进「当前项目」）
    body = js[js.index("async function refreshSessions("):]
    body = body[:body.index("// 空会话清理入口")]
    assert "quick_sessions" in body
    assert "const qlist = Array.isArray(quick_sessions) ? quick_sessions : [];" in body
    assert 'renderPlainList(ul, qOrdered, "quick", "")' in body
    # 字段缺失（远程客户端）时不渲染那个永远为空的列表
    assert "Array.isArray(quick_sessions)" in body
    # 「远程连接」列一下：跨项目拉取后按项目过滤（项目行点击后切换）
    assert 'classicViewGk.startsWith("remote:")' in body
    assert "all_projects: 1" in body
    # 陈列函数：与分组视图的组共用展开/显示更多状态（按 gk），组内序走同一份偏好
    quick = js[js.index("function renderPlainList("):]
    quick = quick[:quick.index("// 空会话清理入口")]
    assert "groupState(gk)" in quick, "与分组视图的快聊组共用折叠状态"
    assert 'orderedSessionList(list, gk)' in quick, "组内序走同一份偏好"
    assert "GROUP_PREVIEW" in quick, "组内默认只露几条，其余收进「显示更多」"
    assert "emptyHint" in quick, "空列表给引导提示"
    assert "不接拖动排序" in js, "经典视图的快聊行不能混进当前项目的拖动序"
    # 项目区的「快聊」行：点击高亮并陈列；悬浮＋走 session.new_task 新建快聊；
    # 行本身接拖动排序（pid 记 "quick" 伪键，commitProjectOrder 拆成 quick_pos 落库）
    row = js[js.index("function buildQuickRow("):]
    row = row[:row.index("// 删除项目：")]
    assert 'classicViewGk = "quick"' in row, "点快聊行切到快聊列表"
    assert "session.new_task" in row, "行的 ＋ 要能新建快聊对话"
    assert 'li.dataset.pid = "quick"' in row, "快聊行以 quick 伪键参与项目区拖动序"
    assert "wireListDrag" in row, "快聊行接拖拽（quick_pos 锚点排序）"
    assert "quickAnchor" in js and "quickPosPref" in js, "快聊锚点：读取与插序的配套状态"
    # 样式：快聊行沿用项目行布局，选中态高亮
    assert ".quick-row" in css


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

    js = read_app_bundle()
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
    # 标签栏尾部有 ＋ 新建入口（与侧栏「新建会话」同一动作：跟随侧栏高亮）
    assert "tab-new" in tabs and "newSessionFromHighlight" in tabs

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

    js = read_app_bundle()
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    backend = (STATIC_DIR.parent / "backend_parts" / "preferences.py").read_text(encoding="utf-8")

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


def test_provider_available_models_search(home):
    """可用模型面板的搜索：检测到模型后才能用，按名字模糊匹配，命中数实时显示。

    锁住：
      ① 模板里带搜索框（avail-search / avail-filter / avail-count）；
      ② 换服务时清空搜索词（不同服务命名差异大，继承上一次的过滤会误导）；
      ③ 过滤逻辑是大小写不敏感的子串包含，无命中给可读提示；
      ④ 搜索框在「收起」时不出现（收起就是不想看列表，再留搜索没意义）。
    """
    from skysheep.server.app import STATIC_DIR

    prov = (STATIC_DIR / "app-providers.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    # ① 模板：搜索行在可用模型列表上方，独立 id 便于状态同步
    assert 'id="avail-search"' in prov
    assert 'id="avail-filter"' in prov
    assert 'id="avail-count"' in prov

    # ② 换服务清空搜索词
    open_fn = prov[prov.index("function openProviderDetail("):]
    open_fn = open_fn[:open_fn.index("\n}\n")]
    assert 'availFilter = ""' in open_fn

    # ③ 过滤：toLowerCase + includes（大小写不敏感子串）
    render = prov[prov.index("const renderAvailPanel = "):]
    render = render[:render.index("const note = ")]
    assert "availFilter.trim().toLowerCase()" in render
    assert "m.toLowerCase().includes(filter)" in render
    assert "没有匹配" in render

    # ④ 收起时不出现搜索框
    assert "searchBox.hidden = !all.length || availListCollapsed" in render

    # 样式存在
    assert ".avail-search" in css and ".as-count" in css


def test_settings_search_indexes_nested_cards(home):
    """设置搜索必须能索引到技能页的卡片。

    技能页的卡片不直接挂在 .settings-page 下，而是包在 #skill-list-view /
    #skill-manage-view 里（两视图互斥切换）。索引只扫 :scope > .settings-card
    会把整页漏掉——搜「技能」「场景模板」都搜不到。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # HTML 侧确认技能页确实是嵌套结构（否则这条测试失去意义）
    skills = html[html.index('id="settings-page-skills"'):]
    skills = skills[:skills.index('id="settings-page-subagents"')]
    assert 'id="skill-list-view"' in skills
    assert skills.index('id="skill-list-view"') < skills.index('id="skill-card"')

    # JS 侧：索引要下探一层容器，不能只认直接子卡
    idx = js[js.index("function settingsCardIndex("):]
    idx = idx[:idx.index("function hideSettingsSearch(")]
    assert 'querySelectorAll(":scope > div")' in idx, "要下探到中间层容器"
    assert "containers.forEach" in idx

    # 分组标题参与正文匹配（搜「多模型协作」能列出该组下的卡）
    assert "groupTitle ? groupTitle" in idx


def test_settings_page_group_titles(home):
    """长设置页用分区标题做扫描锚点，避免卡片平铺失去方位感。

    锁住四处分组（这几页卡片多，是「滚到中间不知道在哪」的高发区）；
    标题必须出现在正确位置——错位比没有更糟（会让人以为分组是别的意思）。
    """
    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def page(id_):
        start = html.index('id="settings-page-' + id_ + '"')
        seg = html[start:]
        nxt = seg.find('<section class="settings-page')
        return seg[:nxt] if nxt > 0 else seg

    # 技能与工具：四组，且「接力」在多模型协作组里
    skills = page("skills")
    for cap in ("技能扩展", "外部服务", "多模型协作", "工具"):
        assert ">" + cap + "</div>" in skills, cap
    assert skills.index("多模型协作") < skills.index('id="relay-config-card"')

    # 界面与通知：外观 / 尺寸两分
    ui = page("ui")
    assert ">外观与通知</div>" in ui and ">尺寸与操作</div>" in ui
    assert ui.index("外观与通知") < ui.index("尺寸与操作")

    # 关于：应用 / 数据与迁移两分，「数据与隐私」属于后者
    about = page("about")
    assert ">应用</div>" in about and ">数据与迁移</div>" in about
    assert about.index("数据与迁移") < about.index("数据与隐私")

    # 分区标题样式存在
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".settings-group-title" in css


def test_settings_nav_grouping(home):
    """设置侧栏分组按「这是什么」排，不按新旧。

    锁住三处易错位：
      ① 远程控制归安全组（它决定谁能远程操作本机，不该混在普通能力里）；
      ② Mods 归能力组（扩展能力，不是安全项）；
      ③ 协作方式（圆桌 / 团队 / 对抗 / 接力）都在「技能与工具」页里，不单列导航项。
    """
    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    nav = html[html.index('<ul id="settings-nav"'):]
    nav = nav[:nav.index("</ul>")]

    # ① 远程控制在安全组（安全标题之后）
    assert nav.index('aria-hidden="true">安全') < nav.index('data-target="remote"')
    # ② Mods 在能力组（日常标题之前）
    assert nav.index('data-target="mods"') < nav.index('aria-hidden="true">日常')
    # ③ 接力不再单列
    assert 'data-target="relay"' not in nav
    assert 'id="settings-page-relay"' not in html
    # 接力说明卡搬进技能与工具页，且与对抗卡相邻
    skills = html[html.index('id="settings-page-skills"'):]
    skills = skills[:skills.index('id="settings-page-advanced"')]
    assert 'id="relay-config-card"' in skills
    assert skills.index('id="adversarial-form"') < skills.index('id="relay-config-card"')
    assert 'id="relay-open-workbench"' in skills


def test_side_nav_order_matches_rail(home):
    """左栏展开态（.side-menu）与收起态（.rail-btn 图标条）的功能顺序必须一致。

    两处是同一套动作的两个渲染面：顺序不一致会让「展开/收起侧栏」看起来
    像功能被挪了位置。顺序按使用频次排，改一处必须同步改另一处。
    「工厂看板」已升为与「新建会话」并列的主操作（展开态 #btn-factory，收起态
    紧随 rail-new），两处都挪出了这段功能菜单，所以不参与这组顺序比对。
    """
    import re

    from skysheep.server.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    side = re.findall(r'class="nav-item" data-act="([^"]+)"', html)
    # 收起态只取图标条那一段（第一条分隔线之前）：分隔线下面是主操作与最近会话
    rail_strip = html[html.index('id="sb-rail"'):html.index('<div class="rail-sep"')]
    rail = re.findall(r'class="rail-btn" data-rail-act="([^"]+)"', rail_strip)

    assert side, "侧栏导航没找到"
    assert side == rail, f"两处顺序不一致：侧栏={side} 收起栏={rail}"
    # 频次顺序：任务与日程最前，配置类（MCP/Skills）沉底
    assert side[0] == "todo" and side[1] == "agenda"
    assert side[-1] == "ext"
    # 主操作位：两处都上移了工厂看板（展开态在 #btn-new 之后，收起态在 #rail-new 之后）
    assert "factory" not in side and "factory" not in rail, "工厂看板不该留在功能菜单里"
    assert 'class="nav-new" data-act="factory"' in html
    assert html.index('id="btn-new"') < html.index('id="btn-factory"')
    assert html.index('id="rail-new"') < html.index('data-rail-act="factory"')


def test_temperature_presets(home):
    """温度字段：常用档位胶囊 + 当前生效状态提示。

    锁住：
      ① 四档（0 / 0.3 / 0.7 / 1.2）+ 清空，点一下直接填值；
      ② 清空 = 空串（保存时清除该项，回到「不发送该参数」）；
      ③ 状态行区分「不发送，用服务默认」与具体值；
      ④ 说明文字点明推理模型通常不接受此项（避免用户白调）。
    """
    from skysheep.server.app import STATIC_DIR

    prov = (STATIC_DIR / "app-providers.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    i = prov.index("function renderProviderDetail(")
    seg = prov[i:i + 60000]

    # ① 档位与清空
    for v, label in (("0", "精确"), ("0.3", "保守"), ("0.7", "均衡"), ("1.2", "创意")):
        assert f'data-v="{v}">{label}' in seg, label
    assert 'data-v="">清空' in seg

    # ② 点击写入 + 清空是空串
    assert "tempInput.value = c.dataset.v;" in seg
    assert 'c.dataset.v === ""' in seg or 'cv === ""' in seg

    # ③ 状态文案
    assert "当前：不发送，用服务默认" in seg
    assert "当前：" in seg

    # ④ 推理模型提示
    assert "推理模型" in seg and "不接受此项" in seg

    # 样式
    assert ".temp-chip" in css and ".temp-presets" in css
    assert ".temp-state.dim-state" in css and ".temp-state.on-state" in css


def test_provider_detail_unsaved_guard(home):
    """服务详情有未保存改动时，返回列表先确认。

    锁住：
      ① 渲染完采集快照；返回列表比对当前值，不同就弹确认；
      ② Key 只比「有没有填」，不把明文存进快照；
      ③ 只读 / 一次性输入字段（name、manual_model）不算改动；
      ④ resetProviderView 清快照（内部路径不走确认）。
    """
    from skysheep.server.app import STATIC_DIR

    src = (STATIC_DIR / "app-providers.js").read_text(encoding="utf-8")

    # ① 快照采集 + 比对确认
    assert "detailSnapshot = detailFormFingerprint(box)" in src
    close_fn = src[src.index("async function closeProviderDetail("):]
    close_fn = close_fn[:close_fn.index("function resetProviderView(")]
    assert "detailFormFingerprint(box) !== detailSnapshot" in close_fn
    assert "await confirmModal(" in close_fn and "danger: true" in close_fn

    # ② Key 不存明文
    fp = src[src.index("function detailFormFingerprint("):]
    fp = fp[:fp.index("\n}\n")]
    assert 'el.value.trim() ? "set" : ""' in fp, "Key 只记填没填"
    assert "api_key" in fp

    # ③ 只读字段不算改动
    assert 'if (f === "name" || f === "manual_model") return;' in fp

    # ④ 重置时清快照
    reset = src[src.index("function resetProviderView("):]
    reset = reset[:reset.index("\n}\n")]
    assert "detailSnapshot = \"\"" in reset


def test_vision_model_inference(home):
    """按模型名推断多模态：只认通用命名信号，纯文本模型绝不误判。

    误判的代价不对称——把纯文本模型标成能看图，用户贴图后才收到上游报错；
    漏判只是少一个提示。所以规则必须保守。
    """
    from skysheep.server.backend_parts.providers import _model_looks_vision

    # 该判成多模态的
    for name in ("gpt-4o", "gpt-4o-mini", "qwen2.5-vl-72b-instruct",
                 "glm-4.5v", "internvl2", "llava-vision", "pixtral-12b",
                 "gemini-2.0-flash-omni"):
        assert _model_looks_vision(name), name

    # 绝不能误判的（纯文本模型，名字里带 vl/omni 之外的元素也不行）
    for name in ("deepseek-chat", "deepseek-reasoner", "glm-5.3", "kimi-k3",
                 "gpt-4-turbo", "claude-sonnet-4", "qwen3-max", "glm-5.3-flash"):
        assert not _model_looks_vision(name), name

    # 空值不炸
    assert not _model_looks_vision("")
    assert not _model_looks_vision(None)


def test_add_provider_modal_search(home):
    """添加自定义服务弹窗里，检测出的模型列表同样可搜索。

    锁住：
      ① 检测结果渲染函数按关键词过滤（大小写不敏感子串）；
      ② 搜索只影响「显示哪些行」，勾选状态不因筛选丢失——全选/清空只作用于当前可见行；
      ③ 无匹配给可读提示。
    """
    from skysheep.server.app import STATIC_DIR

    src = (STATIC_DIR / "app-whitelist.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    i = src.index("function addProviderModal(")
    seg = src[i:src.index("function deleteProviderModal(")]
    assert "const renderPickList = () =>" in seg
    assert "m.toLowerCase().includes(f)" in seg
    assert "没有匹配" in seg
    # 全选/清空限定在 .ap-list 内（当前可见行），不波及被筛掉的行
    assert 'pickWrap.querySelectorAll(".ap-list input")' in seg
    assert ".ap-search" in css and ".ap-count" in css


def test_provider_bulk_enable_and_delete_confirm(home):
    """可用模型批量启用 + 已启用模型删除确认。

    锁住：
      ① 批量启用按钮只对「当前筛选结果里未启用」的模型生效，文案带数量；
      ② 批量启用走单个接口循环，但只统一刷新一次（不 N 次 renderSettings）；
      ③ 删除已启用模型前必须确认；唯一模型给更强的提示。
    """
    from skysheep.server.app import STATIC_DIR

    prov = (STATIC_DIR / "app-providers.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    # ① 按钮出现条件与文案
    assert 'id="avail-bulk"' in prov
    assert "const pending = shown.filter((m) => !enabled.has(m))" in prov
    assert 'bulkBtn.classList.toggle("hidden", !pending.length' in prov
    assert "＋ 全部启用" in prov

    # ② 批量：循环调接口，循环外只刷新一次
    bulk = prov[prov.index("const setEnabledModels = "):]
    bulk = bulk[:bulk.index("const setEnabledModel = ")]
    assert 'request("config.add_provider_model"' in bulk
    assert bulk.count("await renderSettings()") == 1, "批量启用只能重绘一次"
    assert "for (const [i, model] of list.entries())" in bulk, "逐个启用并给进度"

    # ③ 删除确认
    dele = prov[prov.index('li.querySelector(".em-del").onclick'):]
    dele = dele[:dele.index("enabledBox.appendChild(li)")]
    assert "await confirmModal(" in dele
    assert "danger: true" in dele
    assert "唯一的模型" in dele, "删光该服务的模型要单独提示"

    assert ".avail-search .as-bulk" in css


def test_provider_drag_order_protocol(home):
    """顶栏模型菜单的服务列拖动排序：provider_order 偏好 + 拖动 + 弹层模型列表。

    锁住：
      ① 后端 ui.save 收 provider_order（服务名字符串数组，去重封顶 100），
         且读取面同样清洗后回传给前端；
      ② 菜单只列已配置 Key 的服务，未配 Key 的整组不进清单；
      ③ 服务行可拖动，落位后把完整顺序写回偏好；
      ④ 模型列表改为点服务行才弹出的浮层（同一时间只开一个），
         弹层不被 .session-menu 的 overflow 裁掉。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    backend = (STATIC_DIR.parent / "backend_parts" / "preferences.py").read_text(encoding="utf-8")

    # ① 后端：写入白名单 + 清洗 + 读取回传
    assert '"provider_order"' in backend
    writer = backend[backend.index("async def _write_ui_prefs("):]
    branch = writer[writer.index('if key == "provider_order"'):]
    branch = branch[:branch.index("\n                continue")]
    assert "len(seen_p) >= 100" in branch, "服务序要封顶，防异常超大载荷"
    reader = backend[backend.index("def _read_ui_prefs("):]
    reader = reader[:reader.index("def notify_enabled")]
    assert 'data.get("provider_order")' in reader, "读取面也要回传，否则下次打开丢序"

    # ② 只列已配置 Key 的服务
    menu = js[js.index("async function toggleModelMenu("):]
    menu = menu[:menu.index("modelChip.style.cursor")]
    assert "if (!r.hasKey) return;" in menu, "没配 Key 的服务切过去必然失败，不进清单"

    # ③ 拖动接线 + 落盘完整顺序
    assert "b.draggable = true" in menu
    assert "providerOrder = svcList.map((x) => x.name)" in menu
    assert "saveUiPrefs({ provider_order: providerOrder })" in menu
    assert "#model-menu .mm-svc.dragging" in css and "#model-menu .mm-svc.drag-over" in css

    # ④ 模型列表是点服务行弹出的浮层，且菜单不能裁掉它
    assert "const openPopup = (btn, group) =>" in menu
    assert "modelPopup = { el, btn }" in menu
    assert "#model-menu { width: 260px; overflow: visible; }" in css, \
        "session-menu 默认 overflow:hidden 会把右侧浮层裁掉"
    assert "#model-menu .mm-models {" in css
    assert "position: absolute" in css.split("#model-menu .mm-models {")[1].split("}")[0]

    # ⑤ 日常细节：初次打开自动展开当前服务、当前服务行有竖条、Esc 逐层退
    assert 'paint("current")' in menu, "打开菜单应直接展开当前模型所在服务"
    assert 'b.className = "mm-svc" + (isCur ? " current" : "")' in menu
    assert "#model-menu .mm-svc.current::before" in css
    assert "if (modelPopup) {" in js, "Esc 应先收弹层再关菜单"
    assert "closePopup(); // 行要挪位置" in menu, "拖动前要收起弹层，避免锚点失效飘位"


def test_tab_takes_over_blank_placeholder(home):
    """打开真会话时要接管已有空标签，不能旁边再叠一张。

    回归：启动时没有历史会话会留一张空标签（欢迎页），此时点快聊组头的 ＋
    新建会话，旧代码直接 push 新标签，界面上就是「点了一下 ＋，冒出两个会话」。
    这里锁住四条不变量：
      ① 接管的是空标签（isBlankTab：无 sid、不 running），正在跑的不算；
      ② 优先接管当前激活那张，否则激活的空标签会赖在栏上；
      ③ 接管后要补上标签名与 currentSessionId（activateTab 对「已是当前标签」
         会早退，这些记账不做就永远停在「新会话」）；
      ④ 「新建会话」点击即新建（原则统一后不再复用未用过的标签）。
    """
    js = read_app_bundle()

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

    # ④ 「新建会话」原则统一：点击即在当前高亮项目下新建，不再复用未发过
    #    消息的标签（连点留下多个空会话，交给「清理空会话」兜底）
    start = js[js.index("function startNewTab("):]
    start = start[:start.index("\n}\n")]
    assert "isUnusedTab" not in start, "复用分支已按新原则移除：点击即新建"
    assert "if (!persist) return blank();" in start, "启动恢复仍走懒创建（不落库）"
    assert "if (creatingTab) return activeTab;" in start, "creatingTab 只是在途请求的连击互斥"
    assert 'request("session.new"' in start, "新建会话要立即落库（侧栏才有行）"
    assert start.index('request("session.new"') < \
        start.index("chatTabs.push(t)", start.index('request("session.new"')), \
        "先落库拿到 sid，再开新标签"

    # 历史加载收口到一处：activateTab 与 openTabForSession 共用，不再各写一份
    assert "async function loadTabHistory(" in js
    activate = js[js.index("async function activateTab("):]
    activate = activate[:activate.index("\n}\n")]
    assert "await loadTabHistory(tab)" in activate, "activateTab 要走共用的历史加载"


def test_session_updated_broadcast_does_not_spawn_tabs(home):
    """session_updated 元数据广播的两条标签栏不变量：不多开、不少关。

    回归一（不多开）：广播带 session_id，handleEvent 的多会话路由此前对
    「没有打开标签的 session_id」一律后台补开一张标签——归档一个没开着标签
    的会话，它会立刻以「会话」标签原样弹回标签栏。元数据广播只该更新缓存与
    侧栏，不建标签；轮次事件（其他窗口/手机端发消息）的自动跟签行为不变。

    回归二（不少关）：归档后侧栏行消失，但该会话在标签栏的标签此前留在原地
    （用户报的「左侧标签消失，上方标签没跟着消失」）。带 archived 标记的
    广播现在把标签一并 closeTab 收掉。
    """
    js = read_app_bundle()
    routing = js[js.index("function handleEvent("):]
    routing = routing[:routing.index("switch (kind)")]
    assert 'kind !== "session_updated"' in routing, \
        "元数据广播不走自动开标签的路由"
    # 自动开标签的兜底只属于轮次路由本身（openTabForSession background 路径）
    assert "openTabForSession(data.session_id" in routing

    # 归档广播（带 archived 标记）要把该会话的标签一并收掉：侧栏行与标签栏
    # 同步消失，不能只剩侧栏变化。收标签必须走 closeTab（它会同步写回
    # ui.json 的恢复列表，否则重启又把归档标签还原出来）。
    case = js[js.index('case "session_updated"'):]
    case = case[:case.index('case "user_message"')]
    assert "data.archived === true" in case
    assert "const t = tabFor(data.session_id)" in case and "closeTab(t)" in case


def test_archive_modal_fixed_size_and_bulk_ops(home):
    """归档弹窗：方框长宽固定 + 批量选择。

    尺寸由 CSS `.modal-box:has(.archive-wrap)` 固定（列表区自己滚，条目再多
    也不撑高弹窗）；批量操作条常驻列表上方，提供全选 / 恢复所选 / 删除所选，
    删除沿用单行那套「确认删除」两步手感，选择一变要重新确认。
    """
    from skysheep.server.app import STATIC_DIR

    js = read_app_bundle()
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".modal-box:has(.archive-wrap)" in css, "归档弹窗要有固定尺寸规则"
    fixed = css[css.index(".modal-box:has(.archive-wrap)"):]
    fixed = fixed[:fixed.index("}")]
    assert "width:" in fixed and "height:" in fixed, "长宽都要固定"

    fn = js[js.index("async function openArchiveModal("):]
    fn = fn[:fn.index("\n}\n")]
    assert "archive-bulk" in fn and "全选" in fn, "批量操作条：全选"
    assert "恢复所选" in fn and "删除所选" in fn
    assert "确认删除 ${rows.length} 个？" in fn, "批量删除要二次确认"
    assert 'row.dataset.sid = s.id' in fn, "批量操作要按落库的会话 id 发请求"
    assert "syncBulk()" in fn, "单行操作后要同步批量条的计数与全选态"


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
    js = read_app_bundle()

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

    # 4. 组头的 ＋ 常显（已取代折叠箭头，不靠悬停才现）；快聊行沿用项目行样式
    assert "is-empty" in js
    pg_add = css[css.index("#session-list li.pgroup-head .pg-add {"):]
    pg_add = pg_add[:pg_add.index("}")]
    assert "opacity: 0" not in pg_add, "＋ 常显（悬停才现会让入口难以发现）"
    assert ".quick-row" in css and "quick-row" in js, "快聊行沿用项目行（激活态高亮）"

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


def test_ui_prefs_browser_fit_clamped(home):
    """浏览器面板自适应（缺省开；存 0 = 上次切到了原始大小）：0/1 直接存，越界收敛，null 删键。"""
    frame = call_ui(home, "ui.save", {"prefs": {"browser_fit": 0}})
    assert frame["result"]["prefs"] == {"browser_fit": 0}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {"browser_fit": 0}
    frame = call_ui(home, "ui.save", {"prefs": {"browser_fit": 9}})
    assert frame["result"]["prefs"] == {"browser_fit": 1}
    frame = call_ui(home, "ui.save", {"prefs": {"browser_fit": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_browser_page_zoom_clamped(home):
    """浏览器页面缩放（50–300%，Ctrl+滚轮步进；缺省 100%）：合法存、越界收敛、null 删键。"""
    frame = call_ui(home, "ui.save", {"prefs": {"browser_page_zoom": 125}})
    assert frame["result"]["prefs"] == {"browser_page_zoom": 125}
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {"browser_page_zoom": 125}
    frame = call_ui(home, "ui.save", {"prefs": {"browser_page_zoom": 999}})
    assert frame["result"]["prefs"] == {"browser_page_zoom": 300}
    frame = call_ui(home, "ui.save", {"prefs": {"browser_page_zoom": 10}})
    assert frame["result"]["prefs"] == {"browser_page_zoom": 50}
    frame = call_ui(home, "ui.save", {"prefs": {"browser_page_zoom": None}})
    assert frame["result"]["prefs"] == {}


def test_browser_panel_fit_wiring_source_contract(home):
    """浏览器面板自适应的前端契约：视口容器、1280×1080 虚拟视口、切换钮与偏好应用。

    跨源页面读不到内容宽高，自适应只能按固定虚拟视口排版后整体 transform
    缩到面板宽；视口高度限一屏（1080px）并垂直居中——铺满面板需要把视口拉到
    h/scale（窄面板下远超一屏），站点首屏渲染不满时内容会贴顶、下面留出页面
    自己的一大片空白。显隐切在视口容器上（hidden 留在 iframe 上会让容器占着
    flex:1，空态和预览各分走一半面板）。
    """
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static"
    js = read_app_bundle()
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    from skysheep.server.backend import ServerBackend
    assert "browser_fit" in ServerBackend.UI_PREFS_LIMITS
    assert "browser_page_zoom" in ServerBackend.UI_PREFS_LIMITS
    # 前端三处接线：视口容器与切换钮、缩放逻辑、启动读回偏好
    assert 'id="browser-viewport"' in html and 'id="browser-fit"' in html
    assert "BROWSER_FIT_W" in js and "function layoutBrowserFrame(" in js
    assert "browserFit = prefs.browser_fit !== 0" in js
    assert 'browserViewport.classList.remove("hidden")' in js, "显隐要切在容器上"
    # 限高垂直居中：视口高 ≤1080，超出面板的部分上下均分（居中的几何）
    assert "BROWSER_FIT_MAX_H" in js
    assert "Math.min(h / scale, BROWSER_FIT_MAX_H)" in js
    assert "(h - vh * scale) / 2" in js, "限高后要整块垂直居中"
    assert 'browserFrame.style.top = ""' in js, "原始档要清掉居中偏移"
    # 页面缩放：Ctrl+滚轮悬停浏览器面板时只步进页面缩放（不打全局界面缩放），
    # 接管面只可能是面板自身区域（跨源 iframe 内容区的事件父页面收不到）
    assert "BROWSER_ZOOM_STOPS" in js and "function stepBrowserPageZoom(" in js
    assert "Math.min(1, w / BROWSER_FIT_W) * browserPageZoom" in js, "页面缩放要进自适应缩放"
    assert 'e.target.closest("#rp-page-browser")' in js
    assert 'browserFit && browserViewport && !browserViewport.classList.contains("hidden")' in js, \
        "原始档/空态不下接管 Ctrl+滚轮"
    assert 'id="browser-zoom-hud"' in html, "缩放要有 HUD 反馈"
    # 样式：容器裁掉缩放溢出且不自带白底（留白区露面板底色），iframe 绝对定位
    assert ".rp-browser-viewport" in css and "overflow: hidden" in css
    vp_css = css[css.index(".rp-browser-viewport {"):]
    assert "background" not in vp_css[:vp_css.index("}")], \
        "视口容器不要白底：限高居中后留白区应是面板底色（深色主题不发白）"
    assert "transform-origin: 0 0" in css


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


def test_ui_prefs_same_content_save_skips_rewrite(home, monkeypatch):
    """同内容保存不落盘：偏好保存是高频路径（拖拽/切签/通知日志、启动落
    active_project 都走 _write_ui_prefs），合并结果与盘上语义一致（JSON 解析后
    相等，键序无关）时跳过原子写——同值重写只会搅动 mtime，让「这个文件最近
    被谁动过」类排查失真（真机曾观察到 ui.json 内容未变、mtime 被推进）。
    值真的变了照常落盘；跨进程启动的同值写入（_bind_project 落 active_project）
    同样被拦下。
    """
    from pathlib import Path

    from skysheep.server.backend_parts import preferences

    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 320}})
    assert frame["result"]["prefs"] == {"sidebar_w": 320}

    calls: list[str] = []
    real = preferences.write_text_atomic

    def _spy(path, text, **kwargs):
        calls.append(Path(path).name)
        return real(path, text, **kwargs)

    monkeypatch.setattr(preferences, "write_text_atomic", _spy)

    # 新开一个后端（新一次「启动落 active_project」）+ 同值重发：两者都不得再写。
    # 注意盘上键序可能与合并序不同（读取按 UI_PREFS_LIMITS 序、合并按插入序），
    # 比较必须语义相等而非逐字节
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 320}})
    assert frame["ok"] and frame["result"]["prefs"] == {"sidebar_w": 320}
    assert "ui.json" not in calls, "同内容保存/启动同值落 active_project 不应重写 ui.json"

    # 值变了必须照常落盘
    frame = call_ui(home, "ui.save", {"prefs": {"sidebar_w": 400}})
    assert frame["result"]["prefs"] == {"sidebar_w": 400}
    assert "ui.json" in calls
    frame = call_ui(home, "ui.get")
    assert frame["result"]["prefs"] == {"sidebar_w": 400}


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
    """theme 键值域：六套主题 id + auto + timed + 旧版 light/dark；未知值丢弃、null 恢复默认。"""
    for val in ("auto", "timed", "light", "dark", "paper", "celadon", "kaki", "night", "indigo", "pine"):
        frame = call_ui(home, "ui.save", {"prefs": {"theme": val}})
        assert frame["result"]["prefs"] == {"theme": val}
    frame = call_ui(home, "ui.save", {"prefs": {"theme": "solarized"}})
    assert frame["result"]["prefs"] == {}
    frame = call_ui(home, "ui.save", {"prefs": {"theme": None}})
    assert frame["result"]["prefs"] == {}


def test_ui_prefs_theme_timed_roundtrip(home):
    """定时切换（P20）：theme=timed 入白名单，时段 HH:MM 合法收、非法删。"""
    frame = call_ui(home, "ui.save", {"prefs": {
        "theme": "timed", "theme_timed_from": "18:30", "theme_timed_to": "6:05",
    }})
    assert frame["result"]["prefs"] == {
        "theme": "timed", "theme_timed_from": "18:30", "theme_timed_to": "6:05",
    }
    # 非法时段（越界 / 缺冒号 / 非数字）一律删除；此前合法保存的键与 theme 不受牵连
    frame = call_ui(home, "ui.save", {"prefs": {
        "theme_timed_from": "25:00", "theme_timed_to": "ab:cd",
    }})
    assert frame["result"]["prefs"] == {"theme": "timed"}
    frame = call_ui(home, "ui.save", {"prefs": {"theme_timed_from": "07:30"}})
    assert frame["result"]["prefs"] == {"theme": "timed", "theme_timed_from": "07:30"}


def test_timed_window_active():
    """定时窗判定：普通窗、跨零点窗、空窗、非法起止回默认。"""
    from types import SimpleNamespace

    from skysheep.server.app import _timed_window_active

    def clock(h, m):
        return SimpleNamespace(tm_hour=h, tm_min=m)

    assert _timed_window_active("19:00", "07:00", now=clock(20, 0))      # 跨零点窗内
    assert _timed_window_active("19:00", "07:00", now=clock(2, 30))
    assert not _timed_window_active("19:00", "07:00", now=clock(12, 0))  # 窗外
    assert _timed_window_active("12:00", "18:00", now=clock(12, 0))      # 普通窗含起点
    assert not _timed_window_active("12:00", "18:00", now=clock(18, 0))  # 不含终点
    assert not _timed_window_active("08:00", "08:00", now=clock(8, 30))  # 空窗
    # 非法起止回默认 19:00/07:00：22:00 在默认窗内
    assert _timed_window_active("x", None, now=clock(22, 0))
    assert not _timed_window_active("x", None, now=clock(12, 0))


def test_index_timed_first_paint_resolves_by_local_time(home):
    """首帧定时切换：服务端按本地时刻解析落点，注入 data-theme-mode="timed"。

    make_client 起的是子进程，测试进程 monkeypatch 时钟管不到它——深色侧
    现场算一个「包含当前时刻 ±30 分钟」的宽窗（子进程再慢也落在窗内），
    浅色侧用起止相同=空窗（恒浅色），两条都不依赖墙钟落在哪个时段。
    """
    import time as _time

    now = _time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    frm = ((cur - 30) % 1440, (cur + 30) % 1440)  # (起, 止) 同一宽窗，跨零点安全
    call_ui(home, "ui.save", {"prefs": {
        "theme": "timed",
        "theme_timed_from": f"{frm[0] // 60:02d}:{frm[0] % 60:02d}",
        "theme_timed_to": f"{frm[1] // 60:02d}:{frm[1] % 60:02d}",
        "theme_auto_dark": "indigo", "theme_auto_light": "celadon",
    }})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme-mode="timed"' in tag
    assert 'data-theme="indigo"' in tag  # 深色落点用「跟随系统时」选的主题

    # 空窗（起止相同）恒浅色
    call_ui(home, "ui.save", {"prefs": {
        "theme": "timed", "theme_timed_from": "08:00", "theme_timed_to": "08:00",
    }})
    with make_client(home, []) as client:
        tag = _html_tag(client.get("/").text)
    assert 'data-theme="celadon"' in tag


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
    js = read_app_bundle()
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
    js = read_app_bundle()

    assert 'id="btn-fold-all"' in html
    assert ".head-ops" in css and ".fold-all.expand" in css
    assert "function syncFoldAllBtn()" in js
    assert "head.dataset.gkey" in js and "groupState(h.dataset.gkey)" in js
    # 三个调用点：applySidebarView（视图切换）、refreshSessionsGrouped（渲染尾部）、
    # renderSessionList（搜索结果没有组，按钮要随之隐藏）
    assert js.count("syncFoldAllBtn();") >= 3


def test_grouped_head_click_highlight_protocol(home):
    """分组视图组头「点哪行亮哪行」：点组头把高亮收过来，切语境时交回。

    组头高亮平时跟活动会话所在组（快聊会话亮快聊组）；点了某个组头（折叠/
    展开）后高亮先借给那个组，再点会话/切标签时交回。单值状态保证同一时刻
    最多一个组头亮；点的组若已不存在自动落回，不整列无高亮。
    """
    js = read_app_bundle()
    # 单值状态：最多一个组头亮
    assert "let groupedClickGk = null;" in js
    # 点组头（折叠/展开）时把高亮收给点的那个组
    head = js[js.index("function renderProjectGroup("):]
    head = head[:head.index("// ---- 拖动排序")]
    assert "groupedClickGk = String(key);" in head
    assert "st.open = !st.open;" in head, "行点击仍要展开/折叠"
    # 渲染时：点了组头先亮它；组没了落回活动会话（不整列无高亮）
    body = js[js.index("async function refreshSessionsGrouped("):]
    body = body[:body.index("function renderProjectGroup(")]
    assert "const highlightKey = groupedClickGk ||" in body
    assert "if (!stillThere) groupedClickGk = null;" in body
    # 切标签/点会话、新建会话、切视图、切项目：临时高亮都要复位
    act = js[js.index("async function activateTab("):]
    act = act[:act.index("\n}\n")]
    assert "groupedClickGk = null;" in act, "切标签/点会话把高亮交回活动会话"
    start = js[js.index("async function startNewTab("):]
    start = start[:start.index("\n}\n")]
    assert "groupedClickGk = null;" in start, "新建会话回到当前项目语境"
    assert js.count("groupedClickGk = null") >= 4, "切视图/切项目等复位点至少在位"


def test_close_tab_only_closes_tab(home):
    """关标签只把页签从对话页收掉，不删会话；删除统一走左侧会话行。

    回归：为「新建后侧栏没有新行」补的立即落库一度配了「关掉未发送标签顺手
    删空会话」——那是关标签路径里的隐式删除，用户明确要求关标签≠删会话：
    关掉后会话仍在侧栏，删除只能显式走侧栏 ⋯ →「删除会话」（或底部批量
    「清理空会话」）。
    """
    js = read_app_bundle()
    close = js[js.index("function closeTab("):]
    close = close[:close.index("\n}\n")]
    assert "session.delete" not in close, "关标签不得顺带删会话"
    assert "tab.logEl.remove();" in close, "只从对话页收掉页签"
    assert 'request("stop"' in close, "跑着的标签先停（这不是删除会话）"
    # 删除入口仍在左侧：会话行 ⋯ 菜单（带确认弹窗）+ 底部批量清空会话
    menu = js[js.index("function showSessionMenu("):]
    menu = menu[:menu.index("function showTabMenu(")]
    assert "删除会话" in menu and "deleteSessionModal" in menu
    assert 'request("session.delete"' in js[js.index("function deleteSessionModal("):]
    assert "session.cleanup_empty" in js, "空会话批量清理入口在侧栏底部"


def test_new_session_follows_sidebar_highlight(home):
    """「新建会话」的落点＝侧栏项目区高亮的那一项（两视图统一）。

    分组视图点亮的组头（或活动会话所在组）与经典视图正在看的行，就是
    左上按钮 / Ctrl+N / 窄栏＋ / 标签栏＋ / /new 的建会话落点：
    快聊=建快聊、其他项目=先切过去、远程/其他=提示不建。此前恒在当前项目
    建并跳回当前项目视图（「点了 A，新会话却跑去反代」）。
    """
    js = read_app_bundle()
    # 高亮来源与组头渲染同一份：分组视图渲染时记下 highlightKey
    assert "let lastGroupedHighlightKey = null;" in js
    body = js[js.index("async function refreshSessionsGrouped("):]
    body = body[:body.index("function renderProjectGroup(")]
    assert "lastGroupedHighlightKey = highlightKey;" in body
    # 分发函数：快聊=session.new_task；其他项目=切过去再建；远程=提示不建
    fn = js[js.index("async function newSessionFromHighlight("):]
    fn = fn[:fn.index("\n}\n")]
    assert 'request("session.new_task"' in fn
    assert 'request("project.switch"' in fn and "startNewTab();" in fn
    assert "不能在这里新建" in fn
    # 五个入口都走它（标签栏＋ / 左上按钮 / 窄栏＋ / /new）
    assert "plus.onclick = () => newSessionFromHighlight();" in js
    # 左上按钮：工厂/设置开着时先回对话再建，落点仍走同一个分发函数
    btn_new = js[js.index('getElementById("btn-new").onclick'):]
    assert "newSessionFromHighlight();" in btn_new[:200]
    rail = js[js.index('getElementById("rail-new").onclick'):]
    assert "newSessionFromHighlight();" in rail[:200]
    slash = js[js.index('case "/new"'):]
    assert "newSessionFromHighlight();" in slash[:200]


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
    js = read_app_bundle()
    reset = js[js.index("function resetWorkspaceState()"):]
    reset_body = reset[:reset.index("\n}\n")]
    for name in (
        "chatTabs", "activeTab", "sessionMeta", "currentSessionId",
        "sessionSearchActive", "fileIndex", "providerCfg", "bootSnap",
        "resetProjectPanels", "splitPanes",
    ):
        assert name in reset_body, f"resetWorkspaceState 漏清 {name}"
    panels = js[js.index("function resetProjectPanels()"):]
    panels_body = panels[:panels.index("\n}\n")]
    # loadTasks._snap：任务面板轮询的序列化快照（切项目后必须失效，强制重绘）；
    # tasksTimer 曾是死代码（从未赋值）已随轮询快照改造移除
    for name in ("filesLoaded", "agCache", "loadTasks._snap", "resetTermTabs"):
        assert name in panels_body, f"resetProjectPanels 漏清 {name}"


def test_split_pane_state_invariants(home):
    """分屏列的状态不变量（回归锁，防「列状态没人复位」类缺陷回归）：
      ① 同一会话只在一个面上打开——openSplitPane 遇主栏已开的同会话标签
         先收标签再开列（「在分屏打开」= 搬进分屏），
         openTabForSession 把开着的那一列收掉（事件按 tabFor 优先路由进标签，
         双开后列的运行点/输入锁再也没事件来复位）；
      ② 列头 🔒 点击把 pane.permData 转移给主栏标签并挂卡（新标签的 permData
         初始为 null，不带过去确认条永远弹不出来，轮次挂在权限门上）；
      ③ 列头运行点只由 queue_updated 驱动重画——assistant_message 每个中间轮
         （含工具调用轮）都发、task_finished 是后台子代理任务终态，拿它们当
         轮末信号蓝点会中途熄灭；
      ④ 列对象补齐 lastAssistantText 初值（伪 tab 字段口径对齐 newTabObj，
         缺了它无流式正文的轮在收尾时 .trim() 抛 TypeError）；
      ⑤ 轮次产物（检查点条/规划执行按钮）落本轮所属面板的日志，不按 routeTab
         落——多会话并行时响应返回前任何面板的事件都可能改写 routeTab，落错
         面板的执行按钮点击还会触发别栏发送；
      ⑥ 跨项目只读列不挂会改会话的操作（消息级编辑/重发/分叉/回退/重新生成、
         圆桌「引用追问」），列上没有输入框，点了要么抛错要么换来误导性的
         「session not found」。"""
    js = read_app_bundle()
    open_fn = js[js.index("async function openSplitPane("):]
    open_fn = open_fn[:open_fn.index("\n}\n")]
    assert "tabFor(sid)" in open_fn and "closeTab(conflict)" in open_fn, \
        "openSplitPane 遇主栏已开的同会话标签：自动收标签后开列（搬进分屏）"
    open_tab = js[js.index("function openTabForSession("):]
    open_tab = open_tab[:open_tab.index("\n}\n")]
    assert "closeSplitPanesForSids" in open_tab, "主栏打开会话要收掉同会话的分屏列"

    head = js[js.index("function paintSplitHead("):]
    head = head[:head.index("\nfunction ")]
    assert "nt.permData = perm" in head and "showPermission(perm)" in head, \
        "列头 🔒 点击必须把待确认数据带到主栏标签恢复确认卡"

    sync = js[js.index("if (splitPanes.includes(routeTab)) {"):]
    sync = sync[:sync.index("\n}")]
    assert 'kind === "queue_updated"' in sync, "列头运行点由 queue_updated 驱动重画"
    assert 'kind === "assistant_message"' not in sync, \
        "assistant_message 是中间轮信号，不当轮末用"
    assert 'kind === "task_finished"' not in sync, \
        "task_finished 是后台子代理任务终态，不当轮末用"
    assert "pane.permData = null" in sync, "确认解决后列上的待确认数据一并清掉"

    pane_lit = js[js.index("const pane = {"):]
    pane_lit = pane_lit[:pane_lit.index("};")]
    assert "lastAssistantText" in pane_lit, "列对象缺 lastAssistantText 会在轮末抛 TypeError"
    assert "readonly:" in pane_lit, "列对象要带 readonly 标记（只读降级的判断依据）"

    # ⑤ 轮次产物落点跟面板走（send 主栏/列与 doSend 列内都传 tab）
    turn = js[js.index("function appendToTurnLog("):]
    turn = turn[:turn.index("\n}\n")]
    assert "tab.logEl" in turn, "轮次产物优先落所属面板的日志"
    assert "host === curLog()" in turn, "落点不是当前视图时不抢滚动"
    plan = js[js.index("function addPlanActions("):]
    plan = plan[:plan.index("\nfunction ")]
    assert "appendToTurnLog(bar, tab)" in plan, "规划执行按钮要落传入面板的日志"
    ckpt = js[js.index("function addCheckpointBar("):]
    ckpt = ckpt[:ckpt.index("\nfunction ")]
    assert "appendToTurnLog(bar, tab)" in ckpt, "检查点条要落传入面板的日志"
    send_fn = js[js.index("async function send("):]
    send_fn = send_fn[:send_fn.index("\n}\n")]
    assert "addCheckpointBar(r.checkpoint, tab)" in send_fn, "主栏检查点条也要带面板语境"

    # ⑥ 只读列的交互面收敛
    ops = js[js.index("function attachMsgOps("):]
    ops = ops[:ops.index("\nfunction ")]
    assert "tab.readonly" in ops, "只读列的消息操作要在挂载点收敛"
    assert ops.count("if (!ro)") == 2, "编辑/重发与分叉/回退/重新生成在只读列不挂"
    replay = js[js.index("function buildRtReplayCard("):]
    replay = replay[:replay.index("\nfunction ")]
    assert "u.quoteBtn.remove()" in replay, "只读列的圆桌回放卡不挂「引用追问」"
    quote = js[js.index("function quoteRoundtableDraft("):]
    quote = quote[:quote.index("\nfunction ")]
    assert "if (!input)" in quote, "引用追问对取不到输入框要有可读兜底（实时卡路径）"


def test_right_tab_restore_loads_data_on_startup(home):
    """启动恢复上次打开的右面板标签时，必须把数据一并拉回来。

    以前 initUiPrefs 只对 ext 做了处理（卡片要搬进面板），其余标签又画了一层
    空壳——重启后停在日程页看到的是空白网格、连日期范围都是空的，得先点别的
    标签再点回来才有内容。这里锁三件事：
      ① 加载入口统一到 RIGHT_TAB_LOADERS / loadRightTab（不再各写一份 if 链）；
      ② initUiPrefs 恢复标签时逐个 loadRightTab；
      ③ 收起/展开与切项目重拉也走同一条路。
    """
    js = read_app_bundle()

    # ① 一张表覆盖所有需要拉数据的标签（含日程，正是漏掉的那个）
    table = js[js.index("const RIGHT_TAB_LOADERS = {"):]
    table = table[:table.index("};\n")]
    for tab in ("files", "tasks", "agenda", "ptasks", "cron", "pipeline", "review", "memory", "ext", "relay"):
        assert f"\n  {tab}:" in table, f"RIGHT_TAB_LOADERS 缺标签 {tab}"
    assert "function loadRightTab(" in js

    # ② 启动恢复：每个恢复出来的标签都要拉数据（不能只对 ext 特珠处理）
    init = js[js.index("async function initUiPrefs()"):]
    init = init[:init.index("// ----------")]
    assert "for (const id of rightTabs) loadRightTab(id)" in init, \
        "启动恢复标签时没拉数据：重启后日程页会是空白网格"

    # openRightTab / activateRightTab 不再各自维护一份 if 链（合并标签后拉的是
    # 当前分段，所以入口统一成 loadRightTab(rightActiveView())）
    for fn in ("function openRightTab(", "function activateRightTab("):
        body = js[js.index(fn):]
        body = body[:body.index("\n}\n")]
        assert "loadRightTab(rightActiveView())" in body, f"{fn} 没走统一加载入口"
        assert 'if (id === "agenda") loadAgenda()' not in body, f"{fn} 还留着旧 if 链"

    # ③ 切项目重拉同样走统一入口，且面板收起时不早退（否则展开即旧内容）
    reload_body = js[js.index("async function reloadProjectPanels("):]
    reload_body = reload_body[:reload_body.index("\n}\n")]
    assert "loadRightTab(id, true)" in reload_body
    assert "if (!rightTabs.length || rightCollapsed) return" not in reload_body, \
        "面板收起时早退：切项目后展开会看到上一个项目的内容"


def test_right_panel_merged_tabs_keep_every_view_reachable(home):
    """合并标签（任务 / 自动化）之后，每一个分段都还得点得到。

    12 个平铺标签收成 9 个：任务 = 子代理任务/任务清单/项目任务，自动化 =
    定时任务/任务编排。合并最容易埋的坑是「分段加了、某一头没跟上」——
    按钮少一个、分段视图 div 没写、加载函数漏注册、后端偏好白名单漏收 id，
    都表现为同一类故障：某一页从界面上消失，或者升级后标签静默丢失。
    这份用例把五处对齐：TAB_META 容器 ↔ TAB_VIEWS 分段 ↔ TAB_OF 映射 ↔
    index.html 的分段按钮与分段视图 ↔ RIGHT_TAB_LOADERS ↔ 后端白名单。
    """
    from skysheep.server.app import STATIC_DIR
    from skysheep.server.backend import ServerBackend

    js = read_app_bundle()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # ① 菜单里包含接力工作台；任务/自动化内部段仍不单独占菜单格
    meta = js[js.index("const TAB_META = {"):]
    meta = meta[:meta.index("\n};")]
    tab_ids = re.findall(r"^  (\w+): \{", meta, re.M)
    assert tab_ids == ["aux", "review", "files", "browser", "tasks", "agenda",
                       "auto", "relay", "memory", "map", "ext", "teamroom"], \
        f"标签集合变了：{tab_ids}"
    for old in ("todo", "ptasks", "cron", "pipeline"):
        assert f"\n  {old}: {{" not in meta, f"{old} 已并入容器标签，不该再单独占一格菜单"

    # ② 分段表：TAB_VIEWS 的每个分段都映射回所属容器，TAB_OF 不多不少
    #    （两张表都是单行声明，切到行尾的 "};" 为止，别把后面的 rightView 也吃进来）
    views = js[js.index("const TAB_VIEWS = {"):]
    views = views[:views.index("};")]
    merged = dict(re.findall(r'(\w+): \[([^\]]*)\]', views))
    merged = {k: re.findall(r'"(\w+)"', v) for k, v in merged.items()}
    assert merged == {"tasks": ["tasks", "todo", "ptasks"], "auto": ["cron", "pipeline"],
                      "ext": ["mcp", "skills"]}, merged
    tab_of = js[js.index("const TAB_OF = {"):]
    tab_of = tab_of[:tab_of.index("};")]
    tab_of = dict(re.findall(r'(\w+): "(\w+)"', tab_of))
    for tab, segs in merged.items():
        for seg in segs:
            assert tab_of.get(seg) == tab, f"TAB_OF 缺 {seg} → {tab}"
    assert set(tab_of) == {s for segs in merged.values() for s in segs}, \
        "TAB_OF 与 TAB_VIEWS 不是互为镜像（多一个映射会让老偏好映射到不存在的分段）"

    # ③ 前端三件套齐全：容器页、分段条、分段按钮、分段视图、加载函数
    loaders = js[js.index("const RIGHT_TAB_LOADERS = {"):]
    loaders = loaders[:loaders.index("};\n")]
    icons = js[js.index("const RP_ICONS = {"):]
    icons = icons[:icons.index("\n};")]
    for tab, segs in merged.items():
        assert f'id="rp-page-{tab}"' in html, f"{tab} 缺容器页"
        assert f'id="rp-segs-{tab}"' in html and f'data-tab="{tab}"' in html, \
            f"{tab} 缺分段条"
        for seg in segs:
            assert f'data-view="{seg}"' in html, f"{seg} 没有分段按钮，点不到"
            assert f'id="rp-view-{seg}"' in html, f"{seg} 缺分段视图"
            assert f"\n  {seg}:" in loaders, f"RIGHT_TAB_LOADERS 缺分段 {seg}"
    # 每个标签都得有图标：漏一个，菜单里就渲染成一行「undefined 名称」
    for tab in tab_ids:
        assert f"\n  {tab}:" in icons, f"RP_ICONS 缺 {tab} 的图标"

    # ④ 恢复偏好时要按 TAB_OF 映射（老 ui.json 存的是分段 id），并还原分段
    init = js[js.index("async function initUiPrefs()"):]
    init = init[:init.index("// ----------")]
    # 这里用 let 而非 const：循环里会把 tab 重写成带编号的 auxN（辅助对话多开迁移）
    assert "let tab = rightTabFor(t);" in init, "老偏好里的分段 id 没映射回容器标签"
    assert "rightView[tab] = t" in init, "老偏好里打开的分段没还原"

    # ⑤ 后端白名单要收得下容器与分段两种 id：漏收即升级后标签静默消失
    for one in list(merged) + [s for segs in merged.values() for s in segs]:
        assert one in ServerBackend.RIGHT_TAB_IDS, f"RIGHT_TAB_IDS 漏收 {one}"

    # ⑥ #rp-body 里不留孤儿页：每个 rp-page-* 都得是上面这 9 个标签之一
    #    （rp-page-terminal 是底部终端面板的宿主，不在右面板内）
    pages = set(re.findall(r'id="rp-page-(\w+)"', html)) - {"terminal"}
    assert pages <= set(tab_ids), f"右面板里有已不存在的页：{pages - set(tab_ids)}"


def test_roundtable_menu_opens_on_left_and_keeps_actions_reachable(home):
    """圆桌成员浮层的两条布局约定：靠左对齐、操作行常驻可点。

    此前浮层贴的是主区右上角（right:18px）：既远离触发它的圆桌按钮，
    又会在窄窗口被右缘裁切；且 max-height(360) 小于内容高度，
    底部「确定」会被滚出可视区、必须先滚动才能确认。
    """
    from skysheep.server.app import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = read_app_bundle()

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
    assert "positionRtMenu(menu, \"coop-switch\")" in js
    body = js[js.index("function positionRtMenu("):]
    body = body[:body.index("\n}\n")]
    assert "coop-switch" in body and "chat-main" in body
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
        # 「远程连接」不再启动即建：没有渠道会话时不在列表里（要用时才出现）
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
        # 名单外的新项目追加在已排序尾部；「远程连接」未使用不出现
        assert [p["name"] for p in projects] == ["proj", "proj3", "proj2", "proj4"]


def test_ui_prefs_quick_pos_roundtrip_and_cleanup(home):
    """quick_pos（快聊分组锚点）：固定项 top/bottom 直存；before 只收正整数；
    脏值删键（= 恢复默认垫底）；与 project_order 可同帧提交。"""
    frame = call_ui(home, "ui.save", {"prefs": {"quick_pos": "top"}})
    assert frame["result"]["prefs"] == {"quick_pos": "top"}
    frame = call_ui(home, "ui.save", {"prefs": {"quick_pos": "bottom"}})
    assert frame["result"]["prefs"] == {"quick_pos": "bottom"}
    frame = call_ui(home, "ui.save", {"prefs": {"quick_pos": {"before": 7}}})
    assert frame["result"]["prefs"] == {"quick_pos": {"before": 7}}
    # 脏值一律删键：非法枚举、锚点非正整数/非整数、结构不对
    for bad in ("middle", {"before": 0}, {"before": -2}, {"before": "7"},
                {"after": 7}, {"before": True}, 7, None):
        frame = call_ui(home, "ui.save", {"prefs": {"quick_pos": bad}})
        assert frame["result"]["prefs"] == {}, bad
    # 与 project_order 同帧提交（拖动一次只发一个 ui.save）
    frame = call_ui(home, "ui.save", {"prefs": {"project_order": [3, 1], "quick_pos": {"before": 1}}})
    assert frame["result"]["prefs"] == {"project_order": [3, 1], "quick_pos": {"before": 1}}


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
    点击改为打开名下最近的会话；分组视图组内会话直接打开
    （renderProjectGroup 的 rootPath && !isCurrent 分支天然跳过，不切工作目录）。
    删除钮对所有项目渲染：远程连接可从列表移除，渠道再用时自动重建。
    """
    js = read_app_bundle()
    # 经典视图：root_path 为空即固定项目；不切项目、不显示删除钮
    assert "const remote = !p.root_path;" in js
    assert 'if (remote) li.classList.add("remote-fixed")' in js
    assert "「远程连接」还没有会话" in js, "没有对话时给出去渠道发一条的提示"
    assert "openTabForSession(list[0].id" in js, "点击打开名下最近的会话"
    # 点击处理里固定项目只陈列其会话，而不是切换引擎工作目录（锚在 refreshProjects 区域内）
    seg = js[js.index("async function refreshProjects("):]
    seg = seg[:seg.index("function deleteProjectModal(")]
    remote_branch = seg[seg.index("if (remote) {"):]
    remote_branch = remote_branch[:remote_branch.index("if (p.is_current)")]
    assert "classicViewGk = \"remote:\" + p.id" in remote_branch
    assert "refreshSessions()" in remote_branch
    assert "switchProject(p.root_path)" not in remote_branch
    # 删除钮对所有项目渲染（远程连接也可移除，渠道再用时自动重建）
    assert 'del.className = "p-del"' in seg
    assert "渠道再用时会自动重建" in seg
    # 分组视图：组头 ✕ 对所有带 project 的组渲染（远程连接组同样可移除）
    grp = js[js.index("function renderProjectGroup("):]
    grp = grp[:grp.index("head.onclick")]
    assert '(project\n      ? `<button class="pg-del"' in grp, "远程连接组同样渲染 ✕"


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
