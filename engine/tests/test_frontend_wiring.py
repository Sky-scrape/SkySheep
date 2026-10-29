"""前端静态资源接线：评审修复（错误陷阱移除 / 流式贴底跟随 / 任务面板轮询快照 /
vendor 库失败可重试 / diff 与补零去重 / 流式增量渲染 / 历史窗口化）。

前端零构建、无 JS 运行时测试，沿用本套件既有惯例（test_settings_extras 等）：
锁「源码里接线存在、误回归的写法不存在」这一层。语法完整性由 node --check /
人工冒烟保证，这里不重复。
"""

from __future__ import annotations

from pathlib import Path

from conftest import read_app_bundle

# 用例可能从任意 cwd 启动（仓库根 / engine/），静态资源一律按本文件定位成
# 绝对路径；与 server/app.py:299 及 test_settings_extras.py 的写法同源。
ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ---------- 评审 F3：调试错误陷阱（__errdump 红屏浮层）不在线上代码里 ----------

def test_err_trap_removed():
    js = read_app_bundle()
    assert "__errdump" not in js, "调试红屏浮层（__errdump）又被加回来了"
    assert "ERR_TRAP" not in js


# ---------- 评审 F23：流式输出不再无条件拽底 + 「回到底部」浮标 ----------

def test_stream_scroll_follows_bottom_only():
    js = read_app_bundle()
    # scrollLog 尊重贴底跟随标记（上翻回看时不抢滚动位置）
    assert "function logNearBottom" in js
    assert "_followBottom === false" in js
    # 每条聊天流创建时挂默认跟随与 scroll 记账
    assert "logEl._followBottom = true" in js
    assert "logEl.addEventListener(\"scroll\"" in js
    # 用户主动发送强制回底；浮标在 index.html / app.css 成套出现
    assert "document.getElementById(\"jump-bottom\").onclick" in js
    # 浮标显隐必须同步切 .hidden 类：html 初始态带 class="hidden"（!important），
    # 光切 hidden 属性压不住它，浮标会永远显示不出来（死 UI）
    assert "classList.toggle(\"hidden\", !show)" in js
    html = read_static("index.html")
    assert 'id="jump-bottom"' in html
    css = read_static("app.css")
    assert "#jump-bottom" in css


# ---------- 评审 F24：任务面板 3s 轮询按快照跳过无变化重绘 ----------

def test_tasks_poll_snapshot_guard():
    js = read_app_bundle()
    assert "loadTasks._snap" in js, "任务面板轮询缺少序列化快照对比"
    # 失实的「关闭即停」注释不复存在（定时器常驻，靠可见性跳过 + 快照跳过重绘）
    assert "关闭即停" not in js
    # 切项目后快照失效，下一轮强制重绘
    i = js.index("function resetProjectPanels()")
    body = js[i:js.index("\n}\n", i)]
    assert "loadTasks._snap = null" in body


# ---------- 评审 F30：流式渲染增量化（定稿前缀只渲染一次） ----------

def test_stream_render_is_incremental():
    js = read_app_bundle()
    assert "function streamSealCut" in js, "缺少定稿切点计算（空行 + 围栏配对）"
    assert "function initStreamBody" in js
    assert "function renderStreamPart" in js
    # 旧的全量重渲 + 光标拼进 HTML 的写法不得回归（O(n²) 来源）
    assert 'renderMarkdown(t.streamingText) + "<p>▍</p>"' not in js
    # 定稿（finishAssistant）仍是整文精确渲染，光标随 innerHTML 一起移除
    assert "t._streamBody = null" in js
    # 思考块同模式
    assert "t._thinkBody" in js


# ---------- 评审 F42：vendor 库一次加载失败后仍可重试 ----------

def test_loadlib_failure_not_cached_forever():
    js = read_app_bundle()
    assert "delete _libLoads[name]" in js, \
        "loadLib 失败必须清缓存，否则 mermaid/高亮/终端/二维码 刷新前永久不可用"


# ---------- 评审 F43：diff 行分类与两位补零去重；三表单共用骨架 ----------

def test_diff_and_pad2_deduplicated():
    js = read_app_bundle()
    # 补零实现全局只有 pad2 一处；diff 行分类只有 renderDiffText 一处
    assert js.count("padStart(2") == 1
    assert js.count('startsWith("+++")') == 1
    assert "function pad2" in js
    tools = read_static("app-tools.js")
    assert "async function renderProviderCfg" in tools
    for fn in ("renderWebsearchCfg", "renderImagegenCfg", "renderSpeechCfg"):
        assert fn in tools
        # 三个表单都改为走共用骨架
        i = tools.index(f"async function {fn}")
        seg = tools[i:tools.index("\n}\n", i)]
        assert "renderProviderCfg({" in seg, f"{fn} 未复用 renderProviderCfg"
    # 顺带锁死抽取时修掉的潜伏 bug：表单里从不存在 [data-f="provider"] 控件，
    # 保存必须回传 get 到的当前 provider（后端收到空串会把档位重置回 auto）
    assert "querySelector('[data-f=\"provider\"]')" not in tools
    assert "provider: d.provider" in tools


# ---------- 评审 F49：历史窗口化（保留首段同步画） ----------

def test_history_windowing_keeps_first_sync_paint():
    js = read_app_bundle()
    assert "HISTORY_WINDOW" in js, "缺少历史窗口常驻上限"
    assert "加载更早的消息" in js and "function loadEarlierHistory" in js
    # 首段同步画约束：openTabForSession 返回后立刻看 children.length 判空会话，
    # renderHistory 必须在返回前同步画出至少一个节点——这条注释与逻辑不许被窗口化冲掉
    assert "children.length 判断是否空会话" in js
    assert "paintHistorySlice(tab, view, 0, first)" in js
    # 「加载更早」必须顶部插入：addUser/addAssistantDone 一律 appendChild 到流末尾，
    # 补画后要把新节点搬回按钮之后、原内容之前，否则更早的消息倒序画在新消息下面
    assert "const firstOld = btn ? btn.nextSibling : log.firstElementChild" in js
    assert "log.insertBefore(node, firstOld)" in js
    css = read_static("app.css")
    assert ".history-more" in css


# ---------- 对抗审查 11：链接规则回溯护栏（廉价预检 + 长度界 + 超长降级） ----------

def test_markdown_link_rule_anti_backtracking():
    r"""旧写法 [^\]]+ 对大量未闭合 `[` 平方级回溯（Node 实测 200KB 病态文单次
    全量渲染 30 秒以上，流式期间每 80ms tick 重放一遍），必须加护栏。"""
    js = read_app_bundle()
    i = js.index("function renderMarkdown(")
    seg = js[i:i + 4600]
    # 旧的无界写法必须消失（平方级回溯的根源）
    assert r"\[([^\]]+)\]\((https?:[^)]+)\)" not in js
    # 长度界：链接文本 ≤500、URL ≤2000，单个候选位置的最坏回溯封顶（线性化）
    assert r"\[([^\]]{1,500})\]\((https?:[^)]{1,2000})\)" in seg
    # 廉价预检：文本里连 `](` 都没有时整条规则直接跳过（indexOf 线性）
    assert 'text.includes("](")' in seg
    # 超长降级：>MD_PLAIN_LIMIT 走「先 escapeHtml 再换行」的纯文本渲染
    assert "MD_PLAIN_LIMIT" in seg
    assert r'escapeHtml(src).replace(/\n/g, "<br>")' in seg
    # 转义顺序锚定（硬约束）：仍是先 escapeHtml 存进 text，之后才做结构替换；
    # 预检/替换都必须排在 escapeHtml 之后
    assert 'let text = escapeHtml(src || "")' in seg
    assert seg.index('let text = escapeHtml(src || "")') < seg.index('text.includes("](")')
    # 外链安全属性不得随护栏丢失
    assert 'rel="noopener noreferrer"' in seg and 'target="_blank"' in seg


# ---------- 对抗审查 12：MCP 导入确认框摆出 env（字段契约：env 键值字符串对象） ----------

def test_mcp_import_confirm_shows_env():
    r"""stdio 服务的环境变量可改变目标程序运行时行为（NODE_OPTIONS 等），
    确认框必须逐键可见；没有 env 时输出与原来完全一致。"""
    js = read_app_bundle()
    # 锚定到 MCP 导入确认块（app.js 里 needs_confirm 有多处，别抓错）
    i = js.rindex("if (r.needs_confirm)", 0, js.index("以下 MCP 服务会在连接时执行本机命令"))
    seg = js[i:i + 1800]
    # 字段契约：env 由后端 pending_stdio_commands 下发（键 → 字符串值的对象）
    assert "d.env" in seg
    assert "环境变量：" in seg
    # 值超长只截断「展示」，确认导入的仍是完整值
    assert "已截断" in seg
    # 命令与参数的原有拼装不得变（无 env 时界面不变）
    assert '[d.command, ...(d.args || [])].join(" ")' in seg


# ---------- 对抗审查 13：下载完成提示区分「已校验/未校验」（字段契约：verified） ----------

def test_install_update_notice_reports_sha_verification():
    """install_update 响应带 verified 布尔（是否成功核对 .sha256 附件），
    前端必须展示；只有明确 true 才算核对过——false 或旧后端缺字段一律按
    「未核对」处理。"""
    js = read_app_bundle()
    i = js.index('request("app.install_update")')
    seg = js[i:i + 1600]
    # 严格相等才放行「已核对」：undefined/false 都落到警告分支
    assert "r.verified === true" in seg
    assert "未能核对安装包校验值" in seg


# ---------- 更新可信度：渠道切换接线 + 更新失败/成功现场展示 ----------

def test_update_channel_switch_wired_to_prefs():
    """关于页的渠道下拉必须落到 ui 偏好 update_channel（后端 STRING_PREFS 白名单），
    切换后立即重查一次（新渠道不等下次启动生效）。"""
    js = read_app_bundle()
    assert 'update_channel: updateChannelSel.value' in js
    i = js.index('const updateChannelSel = document.getElementById("update-channel")')
    seg = js[i:i + 900]
    assert 'saveUiPrefs({ update_channel: updateChannelSel.value })' in seg
    assert 'btn-check-update' in seg, "切渠道后要触发立即重查"


def test_update_failure_scene_rendered_with_dismiss():
    """boot 快照带 update_failure 时，关于页要亮出现场（目标版本 / 备份目录 /
    日志指引）并提供「知道了」确认（调 app.dismiss_update_failure）；
    update_success 则一次性展示「更新完成」。"""
    js = read_app_bundle()
    assert "snap.update_failure" in js
    assert "snap.update_success" in js
    i = js.index("function renderUpdatePanel(")
    seg = js[i:i + 3600]
    assert "上次更新未完成" in seg
    assert 'request("app.dismiss_update_failure")' in seg
    assert "logs/update-setup.log" in seg
    assert "上次更新完成" in seg


# ---------- 辅助对话模型面板：与主模型菜单同款的页签（默认/自定义）+ 管理入口 ----------

def test_aux_model_menu_tabbed_like_main_menu():
    """辅助对话的模型面板升级为主模型菜单同款：页签切换（默认/自定义）、
    自定义页签空态指引、「⚙ 管理模型服务…」底栏；旧的分组小标题写法下线。"""
    js = read_app_bundle()
    i = js.index("function buildAuxMenu(")
    seg = js[i:js.index("async function pickAuxModel")]
    # 页签状态与主菜单同口径的分段控件
    assert 'let auxMenuTab = "preset"' in js
    assert 'tab: "preset", label: "默认"' in seg
    assert 'tab: "custom", label: "自定义"' in seg
    assert "seg-row seg-mini mm-seg" in seg
    # 自定义页签空态指到管理入口；管理入口进 设置·模型服务
    assert "还没有自定义服务" in seg
    assert "管理模型服务" in seg
    assert 'openSettings("providers")' in seg
    # 旧的分组小标题（aux-mm-head）不再使用
    assert "aux-mm-head" not in js


# ---------- 模型菜单限高滚动：主菜单与辅助面板的行列表包进 .mm-list ----------

def test_model_menus_rows_scrollable():
    """模型菜单的行列表都包进固定高度滚动容器：辅助面板沿用 .mm-list；
    主菜单改为服务列（.mm-services）+ 模型弹层（.mm-models）各自滚动，
    底部的「管理模型服务」入口固定在两列之外、不随列表滚走。"""
    js = read_app_bundle()
    assert js.count('className = "mm-list"') == 1, "辅助面板仍用 .mm-list 滚动容器"
    assert 'className = "mm-services"' in js, "主菜单服务列要自成滚动容器"
    assert 'className = "mm-models"' in js, "主菜单模型弹层要自成滚动容器"
    css = read_static("app.css")
    i = css.index(".model-menu .mm-list")
    seg = css[i:i + 200]
    assert "height: min(" in seg and "overflow-y: auto" in seg
    svc_seg = css[css.index("#model-menu .mm-services"):][:260]
    assert "max-height: min(" in svc_seg and "overflow-y: auto" in svc_seg
    mdl_seg = css[css.index("#model-menu .mm-models {"):][:360]
    assert "max-height: min(" in mdl_seg and "overflow-y: auto" in mdl_seg


# ---------- 模型菜单行样式统一：logo + 名称 + 厂家徽章右贴 ----------

def test_model_menu_rows_logo_and_right_badge():
    """主菜单改为「服务列 + 模型弹层」两级：服务行带头像（providerAvatar(g.name)）
    与名字；厂家徽章随服务名上移到服务列，模型行不再重复；★ 新会话默认标记
    仍缀在模型行末尾。"""
    js = read_app_bundle()
    i = js.index("async function toggleModelMenu")
    seg = js[i:js.index("modelChip.style.cursor")]
    assert "providerAvatar(g.name)" in seg, "服务行应带服务头像"
    assert "mm-svc-name" in seg, "服务行要有名字列"
    assert "mm-def" in seg, "★ 新会话默认标记仍在模型行"
    assert "mm-prov" not in seg, "厂家徽章已上移到服务列，模型行不再重复"
    css = read_static("app.css")
    item_seg = css[css.index("#model-menu .mm-svc {"):]
    assert "width: 100%" in item_seg[:140], "服务行须占满菜单宽，名字才能撑满"


# ---------- 定时任务终态推送：cronModal 的「完成后推送到聊天渠道」开关接线 ----------

def test_cron_modal_channel_notify_toggle():
    """创建/编辑定时任务的弹窗要有推送开关：默认关（防打扰）、勾选值随
    cron.add / cron.update 的 params 回传（notify_channel）。"""
    js = read_app_bundle()
    assert 'id="cron-notify"' in js, "cronModal 缺少推送开关控件"
    assert "完成后推送到聊天渠道" in js, "开关文案缺失"
    # 默认对象显式 false：新建任务默认不推送（防打扰）
    assert "notify_channel: false" in js
    # 勾选态跟随已有任务：t.notify_channel 为真才 checked
    assert "t.notify_channel ? \" checked\"" in js
    # 保存回调把勾选值回传后端
    assert "notify_channel: box.querySelector(\"#cron-notify\").checked" in js
    css = read_static("app.css")
    assert ".cron-fields label.cron-notify" in css, "开关行样式缺失"


# ---------- Mods：存草稿入口（「让 Agent 创建 Mod」闭环的前端半边） ----------

def test_mod_save_draft_entry_wired():
    """Mods 设置页要有「存为草稿」入口：按钮接线 mods.save_draft（Agent 没有这个
    工具面，粘贴保存由人完成）；提示语不得再引导「让它保存为草稿」。"""
    js = read_app_bundle()
    assert 'getElementById("btn-mod-save-draft").onclick' in js, "缺「存为草稿」按钮接线"
    assert 'request("mods.save_draft"' in js, "存草稿弹窗必须走 mods.save_draft"
    html = read_static("index.html")
    assert 'id="btn-mod-save-draft"' in html, "设置页缺「存为草稿」按钮"
    # 误导性口径（Agent 能自己存草稿）不许回归
    assert "让它保存为草稿" not in js
    assert "Agent 保存为草稿后" not in html


def test_mods_status_bar_not_cleared_by_render():
    """Mods 确认条不许被 renderModsCfg 收尾清空（源码契约）。

    本页操作（行内启停/删除/一键安装/总开关/确认安装）都是「先 modsStatus(提示)
    再 renderModsCfg()」：渲染尾部一旦 modsStatus("") 清状态条，刚显示的
    成功/失败提示就被压成两次本地往返的残影——autoHideStatus 的 6 秒驻留与
    「错误保留到下一次操作」全被架空（截图验收实测确认条不可见）。此后若要在
    渲染路径清状态条，必须先解决与操作提示的覆盖关系，不许直接加回。
    """
    js = read_app_bundle()
    assert 'modsStatus("")' not in js, "renderModsCfg 尾部清状态条会冲掉操作确认提示"


# ---------- 跨文件顶层声明重名扫描（普通 script 共享全局词法作用域） ----------

# 手写前端拆为多个普通 script 后，跨文件顶层名必须全局唯一：
#   - 同名 let/const 在拼接后直接 SyntaxError（node --check 整包可抓，
#     但 pytest 里没有 node，所以这里一并扫描）；
#   - 同名 function 是**后加载者静默覆盖**（拼接级无错）——loadRunCenter 曾在
#     app-schedule.js（自动化页「今日运行」卡片版）与 app.js（设置·运行中心页版）
#     各有一份，app.js 后加载把卡片版顶掉，「今日运行」卡片从此不渲染。
# 扫描只认第 0 列开始的声明行：模板串内部与块注释里的伪声明必然带缩进，天然排除。
# 本清单以 index.html 的 script 标签为单一事实来源，新增分区文件自动并入。

def _bundle_js_files() -> list[str]:
    import re
    html = read_static("index.html")
    return re.findall(r'src="/static/(app[^"]*\.js)"', html)


def _top_level_decls(src: str) -> list[str]:
    import re
    decl = re.compile(
        r'^(?:async\s+)?function\s+([\w$]+)|^(?:let|const|var)\s+([\w$]+)|^class\s+([\w$]+)')
    out: list[str] = []
    for line in src.splitlines():
        m = decl.match(line)
        if m:
            out.append(m.group(1) or m.group(2) or m.group(3))
    return out


def test_no_cross_file_top_level_name_collisions():
    """手写前端各文件（app.js + app-*.js）的顶层声明不得跨文件重名。

    重名 function 会被后加载文件静默覆盖，本用例就是为它兜底（见上方注释的
    loadRunCenter 实例）。发现重名时不要再加一份——把其中一个改名或合并。
    """
    seen: dict[str, str] = {}
    dups: list[str] = []
    for name in _bundle_js_files():
        path = STATIC_DIR / name
        if not path.is_file():
            continue
        for decl_name in _top_level_decls(path.read_text(encoding="utf-8")):
            prev = seen.get(decl_name)
            if prev is not None and prev != name:
                dups.append(f"{decl_name}（{prev} 与 {name}）")
            else:
                seen[decl_name] = name
    assert not dups, (
        "跨文件顶层重名（后加载者会静默覆盖前者，且让行内跳转/调试指错文件）：\n  "
        + "\n  ".join(sorted(dups)))


def test_run_center_names_stay_distinct():
    """运行中心两处入口的名字护栏（2026-10-10 修复的重名事故回归）。

    - 自动化页「今日运行」卡片：app-schedule.js 的 loadRunCenter（index.html
      注释与 test_run_center 均锚定此名）；
    - 设置·运行中心页：app.js 的 loadRunsPage（刻意与卡片版错开）。
    二者若再被改成同名，上面的通用扫描也会先拦下；本用例把意图写死，
    防止未来有人「顺手统一命名」把事故带回来。
    """
    sched = read_static("app-schedule.js")
    appjs = read_static("app.js")
    assert "async function loadRunCenter()" in sched, "卡片版定义名不许改"
    assert 'request("automation.run_center_summary", {})' in sched
    assert "function loadRunCenter" not in appjs, (
        "app.js 不许再定义 loadRunCenter（会覆盖 app-schedule.js 的卡片版）——运行中心页请用 loadRunsPage")
    assert "function loadRunsPage()" in appjs, "设置·运行中心页的加载函数名应为 loadRunsPage"


# ---------- 长会话性能：搜索跳转 / 查找覆盖备注 / 设置搜索缓存（2026-10-11） ----------

def test_search_hit_jump_wiring():
    """搜索命中跳转到具体消息的接线护栏：拿到 seq → 打开会话 → 定位 + 闪高亮。"""
    js = read_app_bundle()
    # 点击搜索结果时带上命中 seq；只对「消息命中」（有 seq）启用跳转
    assert "jumpToMessageSeq(t, hitSeq, { query })" in js
    assert "const hitSeq = s.seq != null ? Number(s.seq) : null" in js
    # 目标消息闪高亮 + 命中词标注
    assert "msg-hit-flash" in js and "search-hit-mark" in js
    # 窗口外命中自动补画（每批 100 条、上限 60 批）
    assert "MESSAGE_JUMP_MAX_PRELOAD_ROUNDS" in js
    assert "loadEarlierHistory(tab)" in js
    # 「加载更早」后查找自动纳入新内容
    assert "findCoverageNote" in js
    css = read_static("app.css")
    assert "@keyframes msgHitFlash" in css
    assert "mark.search-hit-mark" in css


def test_settings_search_cache_wiring():
    """设置内搜索：索引缓存 + 失效 + 防抖三件套（防回退成每键全量扫描）。"""
    js = read_app_bundle()
    assert "_settingsCardIndexCache" in js
    assert "function invalidateSettingsCardIndex" in js
    # renderSettings 刷新后主动失效（app-whitelist.js）
    wl = read_static("app-whitelist.js")
    assert "invalidateSettingsCardIndex" in wl
    # 输入防抖
    assert "settingsSearchTimer" in js


def test_split_pane_close_releases_memory():
    """分屏列关闭时的资源释放（观察器断开 / 大字段清空 / 录音收尾）。"""
    js = read_app_bundle()
    i = js.index("function closeSplitPane(")
    seg = js[i:i + 3200]
    assert "pane._cpRO.disconnect()" in seg
    assert "pane._historyAll = null" in seg
    assert "pane._jumpSeqGen" in seg
    assert "if (voiceFace === pane) voiceReset()" in seg
    assert "routeTab = null" in seg


# ---------- 确认弹窗统一走应用内主题化弹层，不再用原生 confirm() ----------

def test_no_native_confirm_in_frontend():
    """前端不得再出现原生 confirm()：它不受主题变量控制、不跟随深浅色，
    在桌面壳里看起来是外来的系统弹窗（与其余界面不搭）。统一走 confirmModal()。"""
    import re

    for name in ("app.js", "app-about.js", "app-schedule.js", "app-skills.js",
                 "app-whitelist.js", "app-advanced.js", "app-mods.js",
                 "app-memmap.js", "app-projects.js", "app-providers.js",
                 "app-tools.js"):
        path = STATIC_DIR / name
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        # 排除注释行里的 confirm 提及，只查真实调用
        hits = [
            (i, line) for i, line in enumerate(src.split("\n"), 1)
            if re.search(r"(?<![A-Za-z_.])confirm\s*\(", line)
            and not line.lstrip().startswith("//")
            and not line.lstrip().startswith("*")   # 块注释行
            and "confirmModal" not in line
        ]
        assert not hits, f"{name} 仍有原生 confirm() 调用：{hits}"


def test_confirm_modal_definition_is_single_and_themed():
    """confirmModal 只有一个定义（重复定义会互相覆盖），且走 .overlay/.modal-box。"""
    js = read_app_bundle()
    assert js.count("function confirmModal(") == 1, "confirmModal 出现了重复定义"
    i = js.index("function confirmModal(")
    body = js[i:i + 2600]
    assert "showModal(" in body, "确认弹窗必须复用主题化弹层"
    assert "btn-danger" in body, "破坏性确认需支持危险色按钮"
    # 两种调用形式都要支持：HTML 正文与纯文本 + opts
    assert "optsStyle" in body


def test_danger_button_style_exists():
    css = read_static("app.css")
    assert ".btn-danger" in css and "var(--reg)" in css, "危险按钮样式缺失"
    assert ".confirm-text" in css, "确认弹窗正文样式缺失"


# ---------- 团队空闲原因 / 继续推进 / 用量快照 ----------


def test_team_idle_resume_and_usage_wiring():
    js = read_app_bundle()
    # 「继续推进」不存在的 sendMessage 已移除；走捕获标签的定向 chat.send
    assert "sendMessage(" not in js
    assert "function tcContinueTeam(card)" in js
    assert "session_id: t.sid" in js
    assert "t.readonly" in js, "只读分屏列不能继续推进团队"
    # 空闲原因与继续按钮：仅 AI 总管、活动态、未终态时显示
    assert "team.idle_reason" in js
    assert "resumeBtn.hidden = !idle || card._finished || !active" in js
    assert "tc-resume" in js
    # 成员/总管 token 快照徽标
    assert "function tcMemberTokens" in js
    assert "tc-m-tokens" in js and ".director || {}" in js
    # 终态/中断卡只读：交付/收队/接管/继续与工单按钮全部收起
    i = js.index("function tcFinish(card, status, summary)")
    body = js[i:js.index("\n}\n", i)]
    assert "ui.deliverBtn" in body and "ui.stopBtn" in body
    assert "ui.takeoverBtn" in body and "ui.resumeBtn" in body
    assert "ui.taskForm.hidden = true" in body
    assert 'card.querySelectorAll(".tt-op")' in body
    # 终态不再保留活动期空闲副标题：直接收口与快照刷新两条路径都要清
    assert "function tcClearIdle(card)" in js
    assert "tcClearIdle(card);" in body
    assert "const idle = active ? (team.idle_reason" in js


def test_team_resident_toggle_wiring():
    """常驻开关（T3.2）：团队卡上的按钮、set/clear 两向请求与快照对齐、
    终态收口一起隐藏——切换态只在活动快照对齐路径上刷，避免把用户刚点的
    开关被 meta 回放刷灭。"""
    js = read_app_bundle()
    assert 'tcEl("button", "tc-resident", "常驻")' in js
    assert '"team.resident_set"' in js and '"team.resident_clear"' in js
    # 快照对齐：resident 键存在且活动态才动开关（meta 回放无键不刷灭）
    assert "team.resident !== undefined" in js
    assert "residentBtn.classList.toggle" in js
    # 建队即亮出按钮；终态收口把按钮一起收起
    assert "card._ui.residentBtn.hidden = false" in js
    i = js.index("function tcFinish(card, status, summary)")
    body = js[i:js.index("\n}\n", i)]
    assert "ui.residentBtn" in body
    # 建队来源模板接进登记链路（评审①）：从模板创建随建队轮带 team_template、
    # 快照落 card._template、登记请求自含 template——UI 登记的常驻条目绑得回模板。
    # applyTeamTemplate 内部读写的 menu 是它自己的形参（setter 合法）；调用面在
    # buildTeamSettingsInto(box) 里——自由引用 menu 的两处旧账（29f81fd 拆分漏改
    # 的调用、评审①曾写出的 menu._tplName 回读）都会让选模板/点确定当场
    # ReferenceError（浮层关不掉、teamTplName 落不了 localStorage、chat.send 永远
    # 不带 team_template），锚定调用点与读取点的正确标识符，回归即红
    assert "applyTeamTemplate(box, tpl)" in js
    assert "teamTplName = box._tplName || null;" in js
    assert "team_template: teamThisTurn && teamTplName" in js
    assert "template: on ? undefined : (card._template || undefined)" in js
    assert "card._template = String(team.template" in js
    # 终态保留解除入口（评审③）：在册队伍终态后按 team_id 探询/解除，
    # 收口与回放卡两条路径都挂探询，按钮点击走按 id 解除
    assert "async function tcProbeResidentClear(card)" in js
    assert '"team.resident_get"' in js
    i = js.index("function tcFinish(card, status, summary)")
    body = js[i:js.index("\n}\n", i)]
    assert "tcProbeResidentClear(card)" in body
    assert 'request("team.resident_clear", { team_id: card._teamId })' in js
    replay_i = js.index("function buildTeamReplayCard(meta, tab)")
    replay_body = js[replay_i:js.index("\n}\n", replay_i)]
    assert "tcProbeResidentClear(card)" in replay_body


# ---------- 对抗发现转团队工单：显式选择 / 来源锚点 / 只读保护 ----------


def test_adversarial_to_team_transfer_wiring():
    js = read_app_bundle()
    assert "function advAttachTransfer(tab, meta, seq)" in js
    assert "function advTransferActions(meta, seq, tab)" in js
    # 真实消息序号在落库后随 chat.send 响应回来
    assert "r.adversarial_seq" in js
    assert "advAttachTransfer(tab, r.adversarial, r.adversarial_seq)" in js
    # 新一轮对抗清掉旧元数据/旧 seq，避免按钮指向上一份报告
    i = js.index("function beginAdversarial(roles)")
    body = js[i:js.index("\n}\n", i)]
    assert "t.advMeta = null" in body and "t.advSeq = 0" in body
    # advFinished 不提前挂按钮（事件里没有 seq）；只在拿到 seq 后挂一次
    i = js.index("function advFinished(data)")
    body = js[i:js.index("\n}\n", i)]
    assert "advTransferActions" not in body and "advMeta" not in body
    # 逐条勾选、默认不选；空选拒绝；只读标签不挂入口
    i = js.index("function advTransferActions(meta, seq, tab)")
    body = js[i:js.index("\n}\n\nfunction advFinished", i)]
    assert 'cb.type = "checkbox"' in body
    assert "cb.checked = true" not in body
    assert "请至少勾选一条成立或部分成立的发现" in body
    assert "if (!tab || tab.readonly" in body
    assert "adversarial.to_team_tasks" in body


# ---------- 中断团队只读回放：roster 形状 / 同 team 匹配 / team.log 会话锚点 ----------


def test_interrupted_team_replay_wiring():
    js = read_app_bundle()
    assert "function teamCardFor(t, teamId)" in js
    # 中断记录里的 roster 是字符串数组，必须补成对象再建卡
    assert 'typeof m === "object"' in js
    assert 'team_id: teamId, session_id: t.sid' in js
    # team.log 按当前标签的会话请求（后台/分屏标签不会被后端按活动会话误判）
    assert 'session_id: (team && team.session_id) || (tab && tab.sid)' in js
    # 同一 team 的卡只标一次中断，不重复建卡
    assert "const card = teamCardFor(t, teamId);" in js
    assert "if (t._teamInterruptedNoticed) return;" in js


# ---------- 三种协作模式可视化：圆桌对照卡 / 团队看板 / 对抗流程条 ----------


def test_roundtable_viewpoint_cards_wiring():
    """圆桌升级为观点对照卡：最多三列、长文折叠可展开、窄屏单列堆叠；
    现有协议没有结构化的共识/分歧数据，界面只并列展示，不凭空推断。"""
    js = read_app_bundle()
    # 列数上限三列；4 个及以上用 2 列（2×N），避免 3+1 孤行
    assert 'grid.style.setProperty("--rt-cols", members.length >= 4 ? 2' in js
    # 长草稿折叠/展开（阈值与 CSS max-height 240px 对齐）
    assert "const RT_DRAFT_CLAMP_PX = 240" in js
    assert "function rtSyncClamp(m)" in js
    assert "function rtToggleDraft(m)" in js
    assert "展开全文" in js and "aria-expanded" in js
    assert "m.card.classList.remove(\"streaming\")" in js
    # 阈值跟随 CSS 的 max-height（窄屏 200px 时不会漏掉「展开全文」）
    assert "parseFloat(getComputedStyle(body).maxHeight)" in js
    # 对照说明只陈述事实，不生成共识/分歧区块
    assert "对照各方观点，展开全文查看完整回答" in js
    assert "buildRtCardShell(members, subText, noteText)" in js
    # 历史回放展开时重算折叠入口（折叠网格量不出高度）
    assert "card._rtMembers" in js
    assert "if (!folded && card._rtMembers) card._rtMembers.forEach(rtSyncClamp)" in js
    css = read_static("app.css")
    assert ".rt-member .rt-m-more" in css
    assert ".rt-member.expanded .rt-m-body" in css
    assert ".rt-member.streaming .rt-m-body" in css
    assert ".roundtable .rt-note" in css
    assert ".roundtable.folded .rt-note { display: none; }" in css


def test_team_kanban_board_wiring():
    """团队工单列表升级为待办/进行中/待验收看板：完成与失败折叠、
    工单/频道视图切换，且既有派工/验收/打回/流式/回放接线全部保留。"""
    js = read_app_bundle()
    assert "const TEAM_TASK_COLUMNS = [" in js
    for label in ("待办", "进行中", "待验收"):
        assert f'label: "{label}"' in js
    assert "function tcColumnKey(status)" in js
    assert "function tcBuildKanban(card)" in js
    assert "function tcRenderTasks(card, tasks)" in js
    assert "function tcSyncBoardCounts(card)" in js
    # 完成/失败进原生 details 折叠区；快照重画只清行，保留展开状态
    assert '"tc-closed"' in js and '"tc-closed-list"' in js
    assert "card._ui.closedList.innerHTML = \"\"" in js
    assert "ui.closed.hidden = done + err === 0" in js
    # 工单 / 频道两视图常驻，只切显隐；键盘方向键可切页签
    assert "function tcBuildViewTabs(card)" in js
    assert "function tcSetView(card, view)" in js
    assert 'card.classList.toggle("view-channel", v === "channel")' in js
    assert "e.key !== \"ArrowLeft\" && e.key !== \"ArrowRight\"" in js
    assert "tcMarkChannelNew(card)" in js
    # 既有操作/流式/历史回放不能因看板改造丢失
    assert "const TEAM_TASK_OPS = {" in js
    assert "function tcOpenStreamRow(card, data)" in js
    assert "function tcFetchFullLog(tab, card, team)" in js
    assert "tcSyncBoardCounts(card);" in js  # 单行增量更新后同步列计数
    # 终态/中断回放卡只读：重画时也不再挂派工按钮
    i = js.index("function buildTeamReplayCard(meta, tab)")
    body = js[i:js.index("\n}\n", i)]
    assert "card._finished = true" in body
    css = read_static("app.css")
    assert ".teamcard .tc-kanban" in css
    assert ".teamcard .tc-view-tab.is-active" in css
    assert ".teamcard.view-channel .tc-channel { display: flex; }" in css
    # 互斥只对页签卡成立：团队室卡不参与（:not 排除，见 test_team_room_tab_wiring）
    assert ".teamcard.view-channel:not(.teamroom-card) .tc-board { display: none; }" in css
    assert ".teamcard .tc-closed-summary" in css


# ---------- 团队室（独立页签）：三分区容器 / 聚合探针 / 入口与事件同源 ----------


def test_team_room_tab_wiring():
    """团队室页签（团队模式设计 §15）：右面板新页签三分区同屏，渲染复用
    团队卡 tc* 系（不造第二份渲染逻辑）；装载走 team.room 聚合探针；入口
    跟团队走（团队卡头部 + 协作菜单），不进「＋」菜单常驻；空态降级照
    §15.4；三分区样式只引用主题变量，六套主题无写死色。"""
    import re

    js = read_app_bundle()
    html = read_static("index.html")
    css = read_static("app.css")

    # 页签注册（app.js 页签机制内加）+ 页签体与加载顺序（index.html）
    assert 'teamroom: { title: "团队室", sub: "频道 · 工单 · 成员" }' in js
    assert "teamroom: (force) => loadTeamRoom(force)" in js
    assert 'id="rp-page-teamroom"' in html and 'id="teamroom-root"' in html
    assert html.index('<script src="/static/app-teamroom.js">') > html.index(
        '<script src="/static/app.js">')

    # 三分区容器：buildTeamCard 整卡重排，_ui 同形，频道主区 + 工单板右栏
    assert "function buildTeamRoomCard(roster, directorMode, director)" in js
    assert "const card = buildTeamCard(roster, directorMode, director);" in js
    assert 'card.classList.add("teamroom-card", "view-channel")' in js
    assert "grid.append(card._ui.channelEl, card._ui.boardList)" in js
    # 工单板在团队室常显：页签卡的 view-channel 互斥对团队室卡 :not 排除，
    # 板面显隐由团队室节自己的规则决定。回归形态：互斥规则（0,3,0）压过
    # .teamroom-card .tc-board（0,2,0），板面 display:none 而右栏 220px 空列
    # 照留、频道被挤窄（2026-10-29 真实浏览器视觉验收）
    assert ".teamcard.view-channel:not(.teamroom-card) .tc-board { display: none; }" in css
    assert ".teamroom-card .tc-board { display: block;" in css

    # 装载：team.room 聚合探针 + team.log 全量回放复用（两级容错照旧）
    assert 'request("team.room"' in js
    assert "tcFetchFullLog({ sid }, card" in js

    # 五团队事件同源喂团队室（app.js 分发分支喂卡的同时喂这里）
    assert "function trRoomFeed(name, data)" in js
    for feeder in ("trOnStarted", "trOnDelta", "trOnMsg", "trOnTask", "trOnFinish"):
        assert f'trRoomFeed("{feeder}", data)' in js

    # 入口两个，都跟团队走；不进「＋」菜单常驻（空态率太高，§15.1）
    assert "在团队室打开" in js and "tc-open-room" in js
    assert '<span class="coop-mode-label">团队室</span>' in js
    # 不进「＋」菜单常驻（§15.1：空态率太高）——「＋」菜单跳过团队室的锚定
    assert 'if (id === "teamroom") return;' in js
    assert "入口跟团队走（团队卡/协作菜单），不进「＋」菜单常驻" in js

    # 空态与降级（§15.4）：空态两动作、中断只读回放、页签内错误行 + 重试
    assert "本会话没有进行中的团队" in js
    assert "从模板建队" in js and "回到对话发起" in js
    assert "team.template_list" in js
    assert "只读回放频道" in js and "tcFinish(card, \"interrupted\", \"\")" in js
    assert "团队室加载失败" in js and "loadTeamRoom(true)" in js
    # 「从模板建队」先回对话视图：调用的必须是全局存在的 backToChat——
    # 命令面板的 backToChatFirst 是 paletteBaseItems 的局部函数，全局查无此名，
    # 旧写法的 typeof 守卫静默跳过（死引用）
    tr_js = read_static("app-teamroom.js")
    assert "backToChatFirst" not in tr_js
    assert 'pageViewOpen() && typeof backToChat === "function") backToChat();' in tr_js

    # 终态只读：trMountSnapshot 与回放卡同口径（_finished 先于重画工单行）
    i = js.index("function trMountSnapshot")
    body = js[i:js.index("\n}\n", i)]
    assert "card._finished = true" in body
    assert 'card.classList.add("ended")' in body

    # 未读 badge 照 §15.4：badge = latest_seq − last_seen_seq——last_seen 按
    # team_id 随 saveUiPrefs 落 ui.json（原子写在后端），team.room 专门带回的
    # latest_seq 是权威水位（刷新/断线重连空档的消息由它兜住，不靠内存计数），
    # 进页签即清零 = 把 last_seen 推到服务端 latest
    assert "function trTabBadge()" in js
    assert "trLatestSeq - trSeenFor(trTeamId)" in js
    assert "saveUiPrefs({ team_last_seen:" in js
    assert "prefs.team_last_seen" in js  # 镜像从 ui.get 回包取回
    assert "function trTrackRoom(teamId, latestSeq, sid)" in js
    assert "trTrackRoom(r.team.team_id, r.latest_seq, sid)" in js
    assert "rp-tab-badge" in css and "rp-tab-badge" in js
    # 内存计数制的旧写法不许回来：刷新/重连即清零、计数不落盘
    assert "trUnread" not in js
    assert "trBumpUnread" not in js

    # 三分区样式全走主题变量：团队室 CSS 块无写死色值（hex / rgb 函数）
    i = css.index("团队室（独立页签）")
    seg = css[i:css.index("---------- 对抗卡", i)]
    assert "var(--" in seg
    assert not re.search(r"#[0-9a-fA-F]{3,8}", seg)
    assert not re.search(r"rgba?\(", seg)


def test_team_room_narrow_panel_wiring():
    """团队室窄面板 CSS（真实浏览器视觉验收扫尾，2026-10-29）：
    ① ~400px 窄右面板下页签头部 .tc-sub 被按钮行挤成逐字竖条——副标题
    基础规则 flex:1 假想尺寸 0 抢不到折行，团队室侧给 200px flex-basis
    参与折行（塞不下整条独占一行），头部按钮禁文字内折；
    ② 同一面板下右栏 220px 里工单板三列各 ~68px、「转待验收」竖排折行——
    工单板自身设命名容器，板面摆不开三列（<260px）时看板退单列堆叠，
    列高约束同步放开（与 ≤860px 视口单列档同款处理）。"""
    css = read_static("app.css")

    # ① 头部：副标题 flex-basis 折行 + 按钮不竖排（按钮文字内折是竖排窄条的
    #    另一半成因；视口 ≤860px 的 .teamcard .tc-sub 整行档在其后声明不受影响）
    assert ".teamroom-card .tc-sub { flex: 1 1 200px; }" in css
    assert ".teamroom-card .tc-head button { white-space: nowrap; }" in css

    # ② 板面是命名容器（宽度只随右栏轨道走），窄板面看板退单列
    assert (".teamroom-card .tc-board { display: block; padding: 0 2px 2px; "
            "min-height: 0; overflow-y: auto; container: trboard / inline-size; }") in css
    i = css.index("@container trboard (max-width: 260px)")
    seg = css[i:i + 300]
    assert ".teamroom-card .tc-kanban { grid-template-columns: 1fr; }" in seg
    assert ".teamroom-card .tc-col { min-height: 0; }" in seg


def test_team_room_badge_backlog_wiring():
    """团队室未读 badge 三处补线（第五批评审，§15.4 的回归与死角）：
    ① 后台定稿按 team_id+seq 直接对水位记账、不依赖挂卡（空态/错误态/后台
    起队无容器也要计数，否则进页签即整表清零、未读整体丢失）；
    ② 起队即探针校准 trTeamId/trLatestSeq（眼前看过的消息即时推已见水位，
    切走后第一条后台定稿不再按 seq−0 整表重计）；
    ③ 已见水位合并落盘（落盘前重读服务端现值只并本队键，--browser 多页签
    并存不互相回退条目）。"""
    tr_js = read_static("app-teamroom.js")

    # ① 后台记账：trOnMsg 后台分支的门槛是会话匹配（trBgRoomMatches），
    #    不过 trRoomMatches 的挂卡前提；眼前分支仍要求挂卡（要动手画卡）
    assert "function trBgRoomMatches(data)" in tr_js
    assert "if (trBgRoomMatches(data)) trTrackEventSeq(data && data.seq);" in tr_js
    assert "if (!trRoomMatches(data)) return;" in tr_js
    # trRoomMatches 本体 = 挂卡前提 + 同一套会话匹配（判定不分叉）
    i = tr_js.index("function trRoomMatches(data)")
    body = tr_js[i:tr_js.index("\n}\n", i)]
    assert "trCard.isConnected" in body
    assert "return trBgRoomMatches(data);" in body

    # ② 起动校准：trOnStarted 起队即打 team.room 探针；回包带代次守卫
    #    （期间又起队/权威校准接手则迟到回包作废）与水位只进不退（探针在途
    #    时定稿事件可能已把 seq 抬得更高），回包当下把已见水位推到 latest
    assert "function trCalibrateStarted(sid)" in tr_js
    assert "trCalibrateStarted(sid);" in tr_js
    i = tr_js.index("function trCalibrateStarted(sid)")
    body = tr_js[i:tr_js.index("\n}\n", i)]
    assert "gen !== trCalGen || trTeamId" in body
    assert "if (latest > trLatestSeq) trLatestSeq = latest;" in body
    assert "trMarkSeen(trTeamId, trLatestSeq);" in body
    # 权威校准（trTrackRoom）作废在途起动探针
    i = tr_js.index("function trTrackRoom(teamId, latestSeq, sid)")
    body = tr_js[i:tr_js.index("\n}\n", i)]
    assert "trCalGen++;" in body

    # ③ 合并落盘：写前重读 ui.get 现值，读-改-写只并本队键（别队条目原样
    #    保留、本队别人已推得更远就不动盘）；链上串行防同页并发读改写互踩；
    #    旧写法（整表替换、要求镜像齐全才发）不许回来
    assert "trSeenFlushChain" in tr_js
    i = tr_js.index("async function trSeenFlush(teamId)")
    body = tr_js[i:tr_js.index("\n}\n", i)]
    assert 'request("ui.get")' in body
    assert "Object.assign({}, raw)" in body
    assert "(Number(merged[teamId]) || 0) >= seq" in body
    assert "merged[teamId] = seq;" in body
    assert "saveUiPrefs({ team_last_seen: merged" in tr_js
    assert "saveUiPrefs({ team_last_seen: Object.assign({}, trSeenMap)" not in tr_js
    # 镜像未拉到也照推（合并写不依赖整表；旧实现才要求镜像齐全）
    i = tr_js.index("function trMarkSeen(teamId, seq)")
    body = tr_js[i:tr_js.index("\n}\n", i)]
    assert "trSeenMap) return" not in body
    assert "trSeenMap[teamId] = seq;" in body
    # 镜像页签每次装载都重拉：别的页签可能推进过水位，旧一次性缓存只会把
    # 别人已见的算成本页未读
    i = tr_js.index("async function trSeenLoad()")
    body = tr_js[i:tr_js.index("\n}\n", i)]
    assert "if (trSeenMap) return trSeenMap;" not in body
    assert "trSeenMap = raw;" in body


def test_adversarial_flow_stats_filter_wiring():
    """对抗卡升级为四阶段流程条 + 裁决统计/筛选问题清单；证据仍可展开，
    转团队工单入口不变。"""
    js = read_app_bundle()
    # 四阶段流程条（编号 + 名称 + 连接线），回放整条置 done
    assert "function advBuildFlow()" in js
    assert 'flow.className = "adv-steps"' in js
    assert "adv-step-num" in js and "adv-step-link" in js
    assert 'flow.querySelectorAll(".adv-step").forEach((el) => el.classList.add("done"))' in js
    # 统计 + 筛选：裁决徽章按实时行数计数，严重度下拉
    assert "const ADV_VERDICT_FILTERS = [" in js
    assert "const ADV_SEVERITY_FILTERS = [" in js
    assert "function advBuildToolbar(card)" in js
    assert "function advUpdateStats(card)" in js
    assert "function advApplyFilter(card)" in js
    assert 'btn.setAttribute("aria-pressed"' in js
    assert 'row.dataset.verdict = verdict || "pending"' in js
    assert "row.dataset.severity = sev" in js
    assert "没有符合筛选条件的问题" in js
    # 证据/依据/建议仍是可展开区（details/summary），并按裁决更新
    assert 'details.className = "adv-f-details"' in js
    assert "function advApplyVerdict(data)" in js
    i = js.index("function advApplyVerdict(data)")
    body = js[i:js.index("\n}\n", i)]
    assert "body.querySelector(\".adv-f-reason\")" in body
    assert "advApplyFilter(t.advCard)" in body
    # 转团队工单入口仍在（回归 test_adversarial_to_team_transfer_wiring 之外再锚一次）
    assert "function advTransferActions(meta, seq, tab)" in js
    css = read_static("app.css")
    assert ".adversarial .adv-steps" in css
    assert ".adversarial .adv-stat" in css
    assert ".adv-finding .adv-f-details" in css
    assert '.adv-finding[data-verdict="confirmed"]' in css


def test_permission_resolved_clears_only_matching_tab():
    """permission_resolved 只清「对应那一个」标签的卡数据。

    后端契约里 PermissionResolved 不带 session_id，handleEvent 的会话路由会
    回落到 activeTab。旧实现无条件清 routeTab 的 needsPerm/permData：
    多标签各挂一张确认卡时，「A 的卡被解决」会把 B 挂着的那张未决卡一并
    抹掉——切到 B 时 activateTab 的 `needsPerm && permData` 恢复分支拿不到
    数据，卡永久消失，而后端那一轮还挂在权限门上无限等待。
    """
    js = read_app_bundle()
    i = js.index('case "permission_resolved":')
    body = js[i:js.index("break;", i)]
    # 必须按 request_id 匹配标签，不能无条件清 routeTab
    assert "t.permData.request_id === rid" in body, (
        "permission_resolved 必须按 request_id 匹配标签，不能无条件清 routeTab"
    )
    assert "for (const t of [...chatTabs, ...splitPanes])" in body, (
        "要遍历全部标签与分屏列，不只活动标签"
    )
    # 旧的无条件写法不得回归
    assert "routeTab.needsPerm = false;\n        routeTab.permData = null;" not in body, (
        "无条件清 routeTab 卡数据的旧写法回归了"
    )


def test_coop_cards_share_theme_tokens_and_narrow_rules():
    """三张协作卡共用同一套头部/状态/按钮节奏；新增样式只走主题变量，
    不写死颜色（六套主题自动适配），窄屏统一降级、焦点可见。"""
    css = read_static("app.css")
    i = css.index("三种协作卡共用骨架")
    seg = css[i:css.index("/* 上下文容量环形仪表", i)]
    assert ".roundtable .rt-head" in seg and ".teamcard .tc-head" in seg
    assert ".adversarial .rt-head" in seg
    assert "var(--dur-fast)" in seg and "var(--blue)" in seg
    # 该段不得出现硬编码色值（#hex / rgb() / hsl()），否则深色主题会失衡
    import re
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", seg)
    assert not re.search(r"\brgba?\(", seg)
    assert not re.search(r"\bhsla?\(", seg)
    assert "prefers-reduced-motion: reduce" in seg
    # 窄屏降级挂在归并后的统一断点阶梯上（520/860/1100，旧 760 已并入 860）
    assert "max-width: 860px" in seg and "max-width: 520px" in seg


# ---------- 审计 M18/L29：静态资源三清单对齐（防缓存清单漂移第三次复发） ----------

def _index_html_script_urls() -> list[str]:
    """index.html 实际加载的 /static/ 脚本清单（顶层模块的单一事实来源）。"""
    import re
    html = read_static("index.html")
    return [u for u in re.findall(r'<script src="([^"]+)"', html)
            if u.startswith("/static/")]


def _app_py_no_store_paths() -> set[str]:
    """从 app.py 源码抽 _no_store_static 的 no_store 集合字面量。

    app.py 不能当模块随便 import（import 即装配整台 server），这里按源码
    解析：函数体里 `no_store = {` 起步、闭 `}` 前的字符串字面量集合。
    """
    import re
    src = (ROOT / "src" / "skysheep" / "server" / "app.py").read_text(encoding="utf-8")
    m = re.search(r"def _no_store_static\(.*?no_store = \{(.*?)\n    \}", src, re.S)
    assert m, "app.py 必须保留 _no_store_static 的 no_store 集合字面量"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _sw_shell_urls() -> list[str]:
    """从 sw.js 源码解析 SHELL_URLS 预缓存白名单（与 test_pwa_static 同法）。"""
    import re
    src = read_static("sw.js")
    m = re.search(r"const SHELL_URLS = \[(.*?)\];", src, re.S)
    assert m, "sw.js 里必须保留 SHELL_URLS 缓存白名单数组"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_static_lists_stay_aligned_with_index_html():
    """index.html 加载的每个顶层模块都必须同时登记进 app.py 的 no_store
    （禁缓存，WebView 不许吃启发式缓存的旧脚本）与 sw.js 的 SHELL_URLS
    （PWA 预缓存壳不许缺角）——审计 M18/L29 的六个拆分模块就是这样漏掉的。

    口径：只断言 A ⊆ B 且 A ⊆ C，反向不要求——no_store 另含
    index.html / app.css / manifest / sw.js 等非 script 入口；SHELL_URLS
    另含 vendor 库等 index.html 不直接加载的壳文件。新增分区模块时只要
    登记了 script 标签，本用例会强制两份清单跟上。"""
    loaded = set(_index_html_script_urls())
    assert loaded, "index.html 的 script 清单不能为空"
    no_store = _app_py_no_store_paths()
    shell = set(_sw_shell_urls())
    missing_no_store = sorted(loaded - no_store)
    missing_shell = sorted(loaded - shell)
    assert not missing_no_store, (
        "index.html 加载的脚本缺 no_store 登记（升级后旧脚本与新 app.js 混跑）：\n  "
        + "\n  ".join(missing_no_store))
    assert not missing_shell, (
        "index.html 加载的脚本缺 SHELL_URLS 预缓存登记（PWA 壳缺角）：\n  "
        + "\n  ".join(missing_shell))


def test_frontend_modules_keep_required_load_order():
    scripts = [url.rsplit("/", 1)[-1] for url in _index_html_script_urls()]
    positions = {name: scripts.index(name) for name in (
        "app-skills.js", "app.js", "app-factory.js", "app-tools.js",
    )}

    assert positions["app-skills.js"] < positions["app.js"]
    assert positions["app.js"] < positions["app-factory.js"]
    assert positions["app.js"] < positions["app-tools.js"]
