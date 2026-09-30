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
