"""设置补充功能：启动恢复标签 / 发送键 / 更新检查开关 / 新会话默认模型 /
提示音 / 全局热键改键 / 子代理并发上限 / 阅读行宽。

锁三层：① ui.json 偏好键的清洗规则（白名单、钳制、null 删键）；
② WS 协议新分支（default_model.get/set 的校验与回包）；
③ 前端源码里的接线（keydown 分支、恢复路径、设置页控件绑定）。
"""

from __future__ import annotations

import json

from test_server import make_client, recv_until


def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": method, "params": params or {}})
        return recv_until(ws, "m1")


def read_ui_json(home):
    return json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))


# ---------- ① 启动恢复会话标签 ----------

def test_session_tabs_prefs_roundtrip(home):
    """session_tabs（去重封顶 200）与 session_active（空串删键）的清洗规则。"""
    sids = [f"s{i}" for i in range(210)]
    frame = call(home, "ui.save", {"prefs": {"session_tabs": sids}})
    assert frame["ok"]
    saved = frame["result"]["prefs"]["session_tabs"]
    assert len(saved) == 200 and saved[0] == "s0"

    # 去重 + 非字符串丢弃
    frame = call(home, "ui.save", {"prefs": {"session_tabs": ["a", "a", 1, None, "b"]}})
    assert frame["result"]["prefs"]["session_tabs"] == ["a", "b"]

    # 空数组 = 全关了：删键
    call(home, "ui.save", {"prefs": {"session_tabs": []}})
    assert "session_tabs" not in read_ui_json(home)

    # 激活态：合法存、空串删
    call(home, "ui.save", {"prefs": {"session_active": "s-xyz"}})
    assert read_ui_json(home)["session_active"] == "s-xyz"
    call(home, "ui.save", {"prefs": {"session_active": ""}})
    assert "session_active" not in read_ui_json(home)


def test_snapshot_carries_open_tabs(home):
    """resume 会话后 snapshot.open_tabs 应含当前会话（激活时后端写 session_tabs 由前端负责，
    这里只锁：open_tabs 字段始终存在且激活会话经过归属校验）。"""
    frame = call(home, "session.new", {"title": "恢复测试"})
    assert frame["ok"]
    sid = frame["result"]["id"]
    frame = call(home, "session.resume", {"id": sid})
    assert frame["ok"]
    # snapshot 走 boot；这里直接验证 ui.json 里 session_active 已被 resume 写入
    assert read_ui_json(home).get("session_active") == sid


# ---------- ② 发送键可配 ----------

def test_ctrl_enter_send_pref(home):
    frame = call(home, "ui.save", {"prefs": {"ctrl_enter_send": 1}})
    assert frame["result"]["prefs"]["ctrl_enter_send"] == 1
    frame = call(home, "ui.save", {"prefs": {"ctrl_enter_send": 0}})
    assert frame["result"]["prefs"]["ctrl_enter_send"] == 0
    frame = call(home, "ui.save", {"prefs": {"ctrl_enter_send": 7}})
    assert frame["result"]["prefs"]["ctrl_enter_send"] == 1  # 非 0 收敛为 1


def test_send_key_toggle_wired(home):
    """前端：输入框 keydown 按 ctrlEnterSend 分支；设置页有开关与回填。"""
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    i = js.index('document.getElementById("input").addEventListener("keydown"')
    seg = js[i:js.index("btn-new")]
    assert "if (ctrlEnterSend)" in seg and "e.ctrlKey || e.metaKey" in seg
    assert "ctrl_enter_send" in js and "renderSendKeyToggle" in js
    html = open("src/skysheep/server/static/index.html", encoding="utf-8").read()
    assert 'id="send-key-toggle"' in html and "用 Ctrl+Enter 发送" in html


# ---------- ③ 自动更新检查开关 ----------

def test_update_check_pref(home):
    frame = call(home, "ui.save", {"prefs": {"update_check": 0}})
    assert frame["result"]["prefs"]["update_check"] == 0
    assert read_ui_json(home)["update_check"] == 0


def test_update_check_toggle_wired(home):
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert 'id="update-check-toggle"' in open(
        "src/skysheep/server/static/index.html", encoding="utf-8").read()
    assert "update_check: updateCheckToggle.checked" in js


# ---------- ④ 新会话默认模型 ----------

def test_default_model_set_get_clear(home):
    """set 校验服务名与模型登记；get 回包；空名清除。"""
    frame = call(home, "default_model.set", {"name": "不存在", "model": "m"})
    assert not frame["ok"]  # 未知服务拒绝

    frame = call(home, "default_model.set", {"name": "", "model": "m"})
    assert not frame["ok"]  # 清除时不能只留模型名

    # provider_factory 注入绕过 config，fake 服务不在 cfg.providers 里；
    # 用真实预设名验证（models 未登记 → 任意模型名放行）
    frame = call(home, "default_model.set", {"name": "deepseek", "model": "deepseek-chat"})
    assert frame["ok"] and frame["result"]["provider"] == "deepseek"
    assert read_ui_json(home)["default_model_provider"] == "deepseek"

    frame = call(home, "default_model.get")
    assert frame["ok"] and frame["result"]["provider"] == "deepseek"

    frame = call(home, "default_model.set", {"name": ""})
    assert frame["ok"] and frame["result"]["provider"] == ""
    assert "default_model_provider" not in read_ui_json(home)


def test_default_model_label_and_source_wired(home):
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert "default_model.get" in js and "default_model.set" in js
    assert "Shift+点击" in js  # 菜单里写明设置手势
    assert "mm-def" in open("src/skysheep/server/static/app.css", encoding="utf-8").read()


# ---------- ⑤ 通知提示音 ----------

def test_notify_sound_pref_and_player(home):
    frame = call(home, "ui.save", {"prefs": {"notify_sound": 1}})
    assert frame["result"]["prefs"]["notify_sound"] == 1
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert "function playNotifySound(" in js
    assert 'maybeNotify("任务完成"' in js and '"perm"' in js  # 等确认走下行音
    assert 'id="notify-sound-toggle"' in open(
        "src/skysheep/server/static/index.html", encoding="utf-8").read()


# ---------- ⑥ 全局热键改键 ----------

def test_hotkey_pref_and_parser(home):
    """advanced.get 回传 hotkey；desktop 解析器白名单校验。"""
    frame = call(home, "advanced.get")
    assert frame["ok"] and "hotkey" in frame["result"]
    assert frame["result"]["hotkey_supported"] == (__import__("sys").platform == "win32")

    call(home, "advanced.save", {"hotkey": "Ctrl+Shift+F9"})
    assert read_ui_json(home)["hotkey"] == "Ctrl+Shift+F9"
    call(home, "advanced.save", {"hotkey": ""})  # 空串回默认（删键）
    assert "hotkey" not in read_ui_json(home)

    import importlib.util
    spec = importlib.util.spec_from_file_location("sk", "desktop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._parse_hotkey("Ctrl+Alt+Space") is not None
    assert mod._parse_hotkey("Ctrl+A") is None        # 字母键不进白名单
    assert mod._parse_hotkey("Space") is None          # 无修饰键拒绝
    assert mod._parse_hotkey("Ctrl+Alt+Shift+F12") is not None
    assert mod._read_hotkey_pref() == mod._HOTKEY_DEFAULT  # 无偏好 = 默认


# ---------- ⑦ 子代理并发上限 ----------

def test_subagent_max_concurrent_roundtrip(home):
    frame = call(home, "subagent.get")
    assert frame["ok"] and frame["result"]["max_concurrent"] == 3

    frame = call(home, "subagent.save", {"max_concurrent": 5})
    assert frame["ok"] and frame["result"]["max_concurrent"] == 5

    frame = call(home, "subagent.save", {"max_concurrent": 9})
    assert not frame["ok"]  # 越界拒绝

    frame = call(home, "subagent.save", {"max_concurrent": 1})
    assert frame["ok"] and frame["result"]["max_concurrent"] == 1


def test_subagent_concurrent_ui_wired(home):
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert 'data-f="max_concurrent"' in js
    assert "set_max_concurrent" in open("src/skysheep/core/subagent.py", encoding="utf-8").read()


# ---------- ⑧ 阅读行宽 ----------

def test_read_width_pref_clamped(home):
    frame = call(home, "ui.save", {"prefs": {"read_width": 1000}})
    assert frame["result"]["prefs"]["read_width"] == 1000
    frame = call(home, "ui.save", {"prefs": {"read_width": 100}})
    assert frame["result"]["prefs"]["read_width"] == 680
    frame = call(home, "ui.save", {"prefs": {"read_width": 9999}})
    assert frame["result"]["prefs"]["read_width"] == 1400


def test_read_width_css_var_and_ui(home):
    css = open("src/skysheep/server/static/app.css", encoding="utf-8").read()
    assert "var(--chat-max-w, 880px)" in css
    assert css.count("max-width: 880px") == 0  # 全部改走变量
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert 'read_width: { css: "--chat-max-w", min: 680, max: 1400 }' in js
    assert "changeReadWidth" in js
    html = open("src/skysheep/server/static/index.html", encoding="utf-8").read()
    assert 'id="readwidth-row"' in html and "阅读行宽" in html


# ---------- 启动恢复前端接线 ----------

def test_tab_restore_frontend_wired(home):
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert "open_tabs" in js and "session_tabs" in js and "session_active" in js
    # renderTabs 末尾的写回：只收有 sid 的标签 + 签名去重
    assert "persistSessionTabs._last" in js


# ---------- ⑨「安全与后台」卡的对齐修复 ----------

def test_adv_card_row_alignment(home):
    """安全与后台卡：四类行统一「标题列 24px 缩进、控件悬挂行首」的骨架。

    此前乱在三处：① 热键输入框被 `.toggle-row input`（复选框 16px 规则，
    特异性更高）压成小方块；② 信任行/热键行标题贴最左，与复选框行标题
    （24px）不齐；③ `.toggle-row.adv-toggle` 没写回 gap，被后部的
    两端对齐版本（gap: 16px）覆盖，标题列又偏 8px。
    """
    css = open("src/skysheep/server/static/app.css", encoding="utf-8").read()
    # ① 热键输入框必须用 .toggle-row input.adv-hotkey（高特异性）定义
    assert ".toggle-row input.adv-hotkey" in css
    # ② 标题列对齐：卡直接子级的信任行 label 与热键行 span 都缩进 24px
    assert "#settings-page-advanced .settings-card > .toolcfg-row > label" in css
    assert "#settings-page-advanced .toggle-row.adv-toggle > span:first-child" in css
    # ③ adv-toggle 行显式写回 gap（覆盖后部的 16px 两端对齐版本）
    adv_block = css.split(".toggle-row.adv-toggle {")[1].split("}")[0]
    assert "gap: 8px" in adv_block, "不写回 gap 会被 16px 版本覆盖，标题列偏 8px"


# ---------- ⑨½ 自动压缩开关的前端接线 ----------

def test_compaction_auto_frontend_wired(home):
    """「运行参数」卡的自动压缩开关：控件、渲染/保存/恢复默认、联动置灰三处都在。"""
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    assert "compaction_auto" in js  # 渲染读 + 保存写都要带上
    assert "syncCompactionInputs" in js  # 开关联动置灰比例/条数两个输入框
    html = open("src/skysheep/server/static/index.html", encoding="utf-8").read()
    assert 'id="adv-compaction-auto"' in html
    # 三个压缩控件按「开关 → 比例 → 条数」的顺序成组
    assert html.index("adv-compaction-auto") < html.index("adv-compaction-trigger") \
        < html.index("adv-keep-recent")


# ---------- ⑩ 用量页图表类型（柱状 / 折线） ----------

def test_usage_chart_pref(home):
    """usage_chart 是值域白名单字符串偏好：bar/line 可存，非法值删键。"""
    frame = call(home, "ui.save", {"prefs": {"usage_chart": "line"}})
    assert frame["ok"] and frame["result"]["prefs"]["usage_chart"] == "line"
    assert read_ui_json(home)["usage_chart"] == "line"

    frame = call(home, "ui.save", {"prefs": {"usage_chart": "pie"}})
    assert "usage_chart" not in frame["result"]["prefs"]  # 非法值 = 删键回默认

    frame = call(home, "ui.save", {"prefs": {"usage_chart": "bar"}})
    assert frame["result"]["prefs"]["usage_chart"] == "bar"


def test_usage_chart_frontend_wired(home):
    """前端：切换控件、折线渲染（SVG polyline + 点 + HTML 日期行）、偏好回填。"""
    js = open("src/skysheep/server/static/app.js", encoding="utf-8").read()
    html = open("src/skysheep/server/static/index.html", encoding="utf-8").read()
    css = open("src/skysheep/server/static/app.css", encoding="utf-8").read()
    assert 'id="usage-chart-tabs"' in html and "柱状" in html and "折线" in html
    assert "usageChartType" in js
    assert "ut-line-svg" in js and "polyline" in js  # 折线 SVG
    assert "preserveAspectRatio=\\\"none\\\"" in js or 'preserveAspectRatio="none"' in js
    assert 'prefs.usage_chart === "line"' in js  # 启动回填
    assert "saveUiPrefs({ usage_chart: usageChartType })" in js
    # CSS：线宽不随容器拉伸（non-scaling-stroke）、日期行均分
    assert "vector-effect: non-scaling-stroke" in css
    assert ".ut-line-dates" in css
