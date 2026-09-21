"""SkySheep 桌面/前端服务层：FastAPI + WebSocket 协议。

协议（JSON 文本帧）：
    客户端请求:  {"id": "r1", "method": "chat.send", "params": {"text": "..."}}
    服务端回复:  {"id": "r1", "ok": true, "result": {...}} / {"id": "r1", "ok": false, "error": "..."}
    事件推送:    {"event": "text_delta", "data": {..AgentEvent 序列化..}}

请求并发处理：chat.send 执行期间，permission.respond / stop 等仍可送达。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import resolve_api_key
from .backend import BUILTIN_SNIPPETS, THEME_PREFS, ServerBackend, client_origin

logger = logging.getLogger("skysheep.security")


def _client_is_local(client) -> bool:
    """WebSocket 客户端是否来自本机。

    用于把「自动允许写入」这类降低防护的开关锁在本机：局域网 / 远程访问模式下
    服务绑定 0.0.0.0，任何拿到令牌的设备都能连 WS，这类开关不能由远端切换。
    来源分类（回环 / tailnet 网段 / 其它）统一走 client_origin，与 HTTP 守卫一致。
    """
    return client_origin(client) == "local"


# ---- 远程客户端（局域网 / Tailscale，持有令牌）不可调用的方法 ----
# 安全审查 2026-09 根因一：dispatch 里只有零星几个方法有本机门禁，其余几十个
# 写接口只靠 WS Token 一层防护。这里集中收口：凡是「改配置、写凭据、启动
# 本机程序、导出含密数据、切换全局安全姿态」的 RPC 一律本机专属。
# 收紧类动作（trust.revoke / lan.disable / whitelist.remove 等）不在此表，
# 远端仍可调用；聊天、会话、文件面板、任务/用量查看等常规遥控功能不受影响。
LOCAL_ONLY_METHODS = frozenset({
    # 模型服务配置（改 base_url/Key → 凭据外发面；probe 会把已存 Key 发往任填地址）
    "config.add_provider", "config.save_provider", "config.delete_provider",
    "config.restore_provider", "config.set_provider_enabled",
    "config.add_provider_model", "config.remove_provider_model",
    "config.probe_models",
    # 联网搜索 / 画图 / 语音：改 endpoint + Key 复用 = 凭据外发
    "websearch.save", "imagegen.save", "speech.save",
    # 钩子命令不经权限门直接 cmd 执行
    "hooks.save",
    # 安全姿态开关（restrict_to_workdir / computer_control / browser_control / 自启）
    "advanced.save",
    # 全局记忆文件（持久注入 system prompt）
    "memory.save",
    # 设置包导出含明文 API Key；导入可覆盖全局配置
    "settings.export", "settings.import",
    # MCP：写入任意 command 保存即启动本地程序
    "mcp.save_server", "mcp.import", "mcp.delete", "mcp.add_preset",
    # 技能：正文直接进 system prompt
    "skills.install", "skills.toggle", "skills.scope", "skills.delete",
    # 定时任务：allowed_tools 可预授权 run_command 等危险工具
    "cron.add", "cron.update", "cron.run_now",
    # 任务编排：无人值守节点同样按 allowed_tools 预授权危险工具
    "pipeline.create", "pipeline.start", "pipeline.cancel",
    "pipeline.delete", "pipeline.node_rerun",
    # 纳入现有任务：挂接任务簿 / 定时任务导入（可停用原任务）/ 会话续跑
    "pipeline.attach", "pipeline.import_cron", "pipeline.add_session",
    # 流水线复制 / 导入（allowed_tools 可随节点预授权危险工具；导出只读不在表内）
    "pipeline.duplicate", "pipeline.import",
    # 网络暴露开关（放宽方向；disable 是收紧、不在表内）
    "lan.enable", "remote.enable",
    # 项目边界：切任意目录 / 删项目 / 改 AGENTS.md（持久 prompt 注入链）
    "project.switch", "project.delete", "project.save_instructions",
    # 整库恢复（覆盖全部项目的会话数据）
    "session.restore_backup",
    # 子代理定义（自定义 prompt 注入）
    "subagent.save", "subagent.save_builtin", "subagent.save_custom",
    "subagent.delete_custom",
    # 其余写配置入口
    "demo.enable", "ollama.enable", "app.export_diagnostics",
})


def _ws_origin_allowed(ws: WebSocket) -> bool:
    """浏览器 WebSocket 握手必须来自应用自己的页面（Origin 与 Host 一致）。

    安全审查 D5：/preview 里的项目文件与浏览器面板里嵌入的外部网页，脚本能
    从本机回环发起 WS 连接（令牌守卫拦不住），等于拿到与本机界面同级的
    调用权。但它们的 Origin 分别是 null（sandbox iframe）或外部地址，
    与 Host 对不上——一律拒绝，切断这条链。
    非浏览器客户端（测试/脚本）不带 Origin 头，不受影响。
    """
    origin = ws.headers.get("origin")
    if not origin:
        return True
    host = ws.headers.get("host")
    if not host:
        return False
    scheme = "https" if ws.url.scheme == "wss" else "http"
    return origin.rstrip("/").lower() == f"{scheme}://{host}".lower()


def _static_dir() -> Path:
    """打包后静态资源被解包到 sys._MEIPASS；开发态直接用源码目录。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "skysheep" / "server" / "static"
    return Path(__file__).parent / "static"


def _no_store_static(app) -> None:
    """手写静态资源（index.html / app.js / app.css）一律 no-store。

    前端是零构建手写资源，文件名不带指纹，StaticFiles 默认的 ETag 协商在
    WebView 上可能让旧窗口继续用缓存里的 app.js——改了前端却看不到效果，
    每次都要用户手动强刷。这三个文件加起来不到 500KB 且是本地磁盘读，
    每次都取最新版远比省这点 IO 划算。

    刻意不包括 /static/vendor/：那是第三方构建产物（mermaid 单文件 3.3MB）
    且随手写代码一起发版，局域网手机访问时每次重下代价太大——它们靠 ETag
    协商已经足够。
    """
    no_store = {"/", "/static/index.html", "/static/app.js", "/static/app.css"}

    @app.middleware("http")
    async def _static_headers(request, call_next):
        resp = await call_next(request)
        if request.url.path in no_store:
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


STATIC_DIR = _static_dir()

# 具体主题 id（去掉 auto 与旧版 light/dark 两档）：<html data-theme="…"> 只打这些值
THEME_IDS = tuple(v for v in THEME_PREFS if v not in ("auto", "light", "dark"))


def _first_paint_attrs(prefs: dict) -> str:
    """把 ui.json 的首帧外观偏好翻译成 <html> 上的属性 / CSS 变量。

    项目切换是整页 reload，而偏好历来只能等 ui.get 返回后再由 JS 应用——
    中间那几十毫秒用的是默认外观（浅色 + 90% 缩放 + 默认栏宽），
    恢复完再跳一次，看起来就是"闪一下"。这里直接由服务端写进首帧 HTML，
    第一帧渲染出来就是用户上次的样子。
    """
    parts = []
    theme = prefs.get("theme")
    if theme == "light":  # 旧版两档值：分别映射到默认浅色 / 深色主题
        theme = "paper"
    elif theme == "dark":
        theme = "night"
    if theme in THEME_IDS:
        parts.append(f'data-theme-mode="{theme}"')
        parts.append(f'data-theme="{theme}"')
    else:
        # auto（或缺省 / 非法值）：服务端不知道系统深浅，交给首帧脚本按系统判定
        parts.append('data-theme-mode="auto"')
    style = []
    scale = prefs.get("ui_scale")
    if isinstance(scale, int) and not isinstance(scale, bool):
        style.append(f"--ui-zoom:{scale / 100}")
    for key, css in (("sidebar_w", "--sb-w"), ("composer_h", "--cp-h"), ("right_w", "--rp-w")):
        val = prefs.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            style.append(f"{css}:{val}px")
    if style:
        parts.append('style="' + ";".join(style) + '"')
    return " ".join(parts)


def create_app(
    working_dir: str | Path = ".",
    provider_name: str | None = None,
    provider_factory=None,
) -> FastAPI:
    backend = ServerBackend(
        working_dir=working_dir,
        provider_name=provider_name,
        provider_factory=provider_factory,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # ---- 崩溃哨兵：启动即落标记，正常关停时删除 ----
        # 下次启动时标记还在 = 上次是崩溃/强杀收场，boot 快照带给前端提示用户
        # 导出诊断包。误报面（断电、结束进程树）正是想覆盖的场景，文案按
        # "未正常关闭"表述。
        from ..config import skysheep_home
        from ..support import PROCESS_START

        flag = skysheep_home() / "crash.flag"
        backend.crashed_last_run = flag.exists()
        try:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(str(asyncio.get_running_loop().time()), encoding="utf-8")
        except OSError:
            pass
        await backend.setup()
        backend.start_reminder_loop()
        backend.start_cron_loop()
        backend.start_pipeline_loop()
        backend.start_memory_maintenance_loop()
        import logging

        logging.getLogger("skysheep").info(
            "服务就绪（自进程启动 %.2fs）", time.perf_counter() - PROCESS_START
        )
        yield
        backend.stop_cron_loop()
        backend.stop_pipeline_loop()
        backend.stop_reminder_loop()
        backend.stop_memory_maintenance_loop()
        await backend.shutdown()
        try:
            flag.unlink(missing_ok=True)
        except OSError:
            pass

    app = FastAPI(title="SkySheep", lifespan=lifespan)
    # 测试与扩展从 app.state 取引擎句柄（create_app 的闭包变量外部不可见）
    app.state.backend = backend

    # ---- 局域网访问令牌守卫（默认关闭 = token 为空，本地直连不设防） ----
    # 开启后服务监听 0.0.0.0（cli/_start_backend 负责），远端请求都要带令牌：
    # ?token=…（首次，成功后写 cookie）/ cookie skysheep_token / 头 X-SkySheep-Token。
    # 本机回环连接免令牌（桌面窗口自己不带令牌，不能被挡在门外）。
    FORBIDDEN_HTML = (
        '<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">'
        "<title>SkySheep</title>"
        '<body style="font-family:sans-serif;background:#1d1a16;color:#f4ecd8;'
        'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
        "<div style='text-align:center'><h1>🐑 SkySheep</h1>"
        "<p>需要访问令牌：请在地址后加 <code>?token=你的令牌</code></p>"
        "<p style='opacity:.6'>令牌在桌面端 设置 · 手机控制 里查看</p></div></body></html>"
    )

    @app.middleware("http")
    async def _lan_token_guard(request, call_next):
        cfg = backend.cfg
        lan = bool(cfg is not None and cfg.server.lan)
        ts = bool(cfg is not None and cfg.server.tailscale)
        origin = client_origin(request.client)
        # 仅远程访问（Tailscale）模式：物理局域网等非 tailnet 来源直接拒绝，
        # tailnet 设备必须验令牌
        if ts and not lan and origin == "other":
            return HTMLResponse(FORBIDDEN_HTML, status_code=403)
        # 本机（回环）永远免令牌：守卫挡的是别的设备，不能把桌面自己关在门外
        # （此前 lan=true 时本机也要令牌，桌面窗口会弹"需要访问令牌"——2026-09-18 修复）
        token = cfg.server.token if (
            cfg is not None and origin != "local" and (lan or (ts and origin == "tailscale"))
        ) else ""
        if not token:
            return await call_next(request)
        supplied = (
            request.query_params.get("token")
            or request.cookies.get("skysheep_token")
            or request.headers.get("x-skysheep-token")
            or ""
        )
        if supplied and hmac.compare_digest(supplied, token):
            response = await call_next(request)
            if request.query_params.get("token"):
                # 首次带 token 访问成功后种 cookie，静态资源等后续请求免拼参数
                response.set_cookie(
                    "skysheep_token", token, max_age=30 * 86400, httponly=True, samesite="lax"
                )
            return response
        if request.url.path == "/health":
            return JSONResponse({"ok": False, "error": "token required"}, status_code=403)
        return HTMLResponse(FORBIDDEN_HTML, status_code=403)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/")
    async def index() -> HTMLResponse:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        try:
            attrs = _first_paint_attrs(backend.first_paint_prefs())
        except Exception:  # noqa: BLE001  偏好读不到就用默认外观，不影响打开
            attrs = ""
        if attrs:
            page = page.replace('<html lang="zh-CN">', f'<html lang="zh-CN" {attrs}>', 1)
        return HTMLResponse(page)

    # 静态资源禁缓存：前端零构建、文件名无指纹，否则用户永远卡在旧 app.js
    _no_store_static(app)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---- 本地 HTML 一键预览（浏览器标签的 iframe 用；只读、锁在工作目录内） ----

    @app.get("/preview")
    async def preview(p: str = ""):
        raw = p.strip()
        if not raw:
            return HTMLResponse("缺少文件路径参数 p", status_code=400)
        target = Path(raw)
        if not target.is_absolute():
            target = backend.working_dir / target
        try:
            target = target.resolve()
            target.relative_to(backend.working_dir.resolve())
        except (OSError, ValueError):
            return HTMLResponse("只能预览工作目录内的文件", status_code=403)
        if not target.is_file():
            return HTMLResponse("文件不存在（或这是目录）", status_code=404)
        # 安全审查 D5：预览的文件与主界面同源，脚本天然能带凭据调同源接口。
        # iframe 的 sandbox 属性是主防线（见 index.html），这里再给响应本身
        # 加 CSP sandbox：即使被直接打开（不经 iframe）也是隔离环境。
        return FileResponse(target, headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox allow-scripts allow-forms",
        })

    # ---- 方法分发 ----

    async def dispatch(method: str, params: dict, emit, local: bool = True) -> dict:
        """local=False 表示请求来自局域网远端：降低防护的开关一律拒绝。"""
        if not local and method in LOCAL_ONLY_METHODS:
            # 安全审查根因一：配置/管理类 RPC 集中收口，与 permission.set_mode /
            # trust.grant 同一姿态——手机遥控可以聊天，但不能改引擎的安全配置。
            raise RuntimeError("该操作涉及配置或本机权限，只能在桌面端本机执行")
        if method == "boot":
            return await backend.snapshot()
        if method == "chat.send":
            text = str(params.get("text", "")).strip()
            images = params.get("images")
            regenerate = bool(params.get("regenerate", False))
            if not text and not (isinstance(images, list) and images) and not regenerate:
                raise RuntimeError("empty text")
            members = params.get("members")
            raw_refs = params.get("refs")
            # 圆桌本轮覆盖值：只在客户端显式传了才覆盖 config（None=用配置值）
            raw_debate = params.get("debate_rounds")
            raw_chair = params.get("chair_answers")
            return await backend.send(
                text,
                emit,
                plan_mode=bool(params.get("plan_mode", False)),
                roundtable=bool(params.get("roundtable", False)),
                members=members if isinstance(members, list) else None,
                images=images if isinstance(images, list) else None,
                session_id=str(params["session_id"]) if params.get("session_id") else None,
                wants_title=bool(params.get("wants_title", False)),
                regenerate=bool(params.get("regenerate", False)),
                compare=bool(params.get("compare", False)),
                refs=[str(r) for r in raw_refs] if isinstance(raw_refs, list) else None,
                debate_rounds=(
                    max(0, min(2, int(raw_debate)))
                    if isinstance(raw_debate, int) and not isinstance(raw_debate, bool)
                    else None
                ),
                chair_answers=raw_chair if isinstance(raw_chair, bool) else None,
            )
        if method == "permission.respond":
            return {
                "delivered": backend.respond_permission(
                    str(params.get("request_id", "")), str(params.get("decision", "deny"))
                )
            }
        if method == "permission.mode":
            return {"mode": backend.permission_mode()}
        if method == "permission.set_mode":
            mode = str(params.get("mode", "confirm"))
            # 「自动编辑 / 完全访问」会放宽写入与执行的确认：只能在本机界面上切换，
            # 不允许被局域网客户端（或远程驱动的前端）打开
            if mode in ("accept_edits", "full_access") and not local:
                raise RuntimeError("放宽权限的档位（自动编辑/完全访问）只能在本机界面上切换")
            return await backend.set_permission_mode(mode)
        if method == "stop":
            return {"cancelled": backend.cancel_run(
                str(params["session_id"]) if params.get("session_id") else None
            )}
        if method == "schedule.list":
            return await backend.store.list_schedules(
                include_done=bool(params.get("include_done", False))
            )
        if method == "schedule.add":
            return await backend.schedule_add(params)
        if method == "schedule.update":
            return await backend.schedule_update(params)
        if method == "schedule.delete":
            return await backend.schedule_delete(int(params.get("id", 0)))
        if method == "cron.list":
            return await backend.cron_list()
        if method == "cron.add":
            return await backend.cron_add(params)
        if method == "cron.update":
            return await backend.cron_update(params)
        if method == "cron.delete":
            return await backend.cron_delete(params)
        if method == "cron.run_now":
            return await backend.cron_run_now(params)
        if method == "schedule.due":
            return {"schedules": await backend.store.due_schedules()}
        if method == "chat.status":
            return backend.status()
        if method == "chat.compact":
            return await backend.compact_now()
        if method == "tasks.list":
            # 远程客户端只能看自己正在交互的会话的子代理任务（安全审查 B13：
            # 任务详情含 prompt/result，本机任务簿保持全局视图不变）
            only = backend.session.id if (not local and backend.session) else None
            return await backend.tasks_list(params, session_id=only)
        if method == "tasks.get":
            only = backend.session.id if (not local and backend.session) else None
            return await backend.tasks_get(params, session_id=only)
        if method == "tasks.cancel_all":
            only = backend.session.id if (not local and backend.session) else None
            return await backend.tasks_cancel_all(session_id=only)
        if method == "pipeline.list":
            return await backend.pipeline_list()
        if method == "pipeline.get":
            return await backend.pipeline_get(params)
        if method == "pipeline.create":
            return await backend.pipeline_create(params)
        if method == "pipeline.start":
            return await backend.pipeline_start(params)
        if method == "pipeline.cancel":
            return await backend.pipeline_cancel(params)
        if method == "pipeline.delete":
            return await backend.pipeline_delete(params)
        if method == "pipeline.node_rerun":
            return await backend.pipeline_node_rerun(params)
        if method == "pipeline.attach":
            return await backend.pipeline_attach(params)
        if method == "pipeline.import_cron":
            return await backend.pipeline_import_cron(params)
        if method == "pipeline.add_session":
            return await backend.pipeline_add_session(params)
        if method == "pipeline.duplicate":
            return await backend.pipeline_duplicate(params)
        if method == "pipeline.export":
            return await backend.pipeline_export(params)
        if method == "pipeline.import":
            return await backend.pipeline_import(params)
        if method == "usage.stats":
            return await backend.usage_stats(params)
        if method == "fs.read":
            return await backend.fs_read(params)
        if method == "fs.write":
            return await backend.fs_write(params)
        if method == "snippets.list":
            # builtin 随响应下发：用户把指令删光时，前端 / 菜单用它兜底
            return {"snippets": await backend.store.list_snippets(),
                    "builtin": BUILTIN_SNIPPETS}
        if method == "snippets.add":
            name = str(params.get("name", "")).strip()
            content = str(params.get("content", "")).strip()
            if not name or not content:
                raise RuntimeError("名称与内容不能为空")
            return {"snippet": await backend.store.add_snippet(name, content)}
        if method == "snippets.update":
            ok = await backend.store.update_snippet(
                int(params.get("id", 0)),
                str(params.get("name", "")).strip(),
                str(params.get("content", "")).strip(),
            )
            if not ok:
                raise RuntimeError("快捷指令不存在")
            return {"updated": True}
        if method == "snippets.delete":
            return {"deleted": await backend.store.delete_snippet(int(params.get("id", 0)))}
        if method == "fs.files":
            return await backend.workspace_files()
        if method == "checkpoint.list":
            return await backend.list_checkpoints()
        if method == "checkpoint.restore":
            return await backend.restore_checkpoint(str(params.get("id", "")))
        if method == "checkpoint.diff":
            return await backend.checkpoint_diff(str(params.get("id", "")))
        if method in ("term.spawn", "term.input", "term.resize"):
            # 终端是常驻 shell（用户手敲命令，不进权限门，见 TerminalManager 注释），
            # 所以它只在“用户就在这台机器面前”时成立。局域网模式下持有令牌的设备
            # 也能连 WS，若允许它调用这里，令牌就等于 shell 访问权限。
            if not local:
                raise RuntimeError("终端面板只能在本机上使用")
            tid = str(params.get("term_id", "") or "")
            rows = int(params.get("rows", 24) or 24)
            cols = int(params.get("cols", 100) or 100)
            if method == "term.spawn":
                return backend.term_spawn(tid, rows, cols)
            if method == "term.input":
                return backend.term_input(tid, str(params.get("data", "")), rows, cols)
            return backend.term_resize(tid, rows, cols)
        if method == "term.stop":
            # Ctrl+C 是收紧动作，远端也放行（否则远程发起的程序停不下来）；
            # 不带 term_id 时发给所有终端标签的前台进程
            return backend.term_stop(str(params.get("term_id", "") or ""))
        if method == "term.close":
            # 关闭终端标签：结束该标签的 shell 并丢弃执行槽
            return backend.term_close(str(params.get("term_id", "") or ""))
        if method == "chat.aux":
            text = str(params.get("text", ""))
            return await backend.chat_aux(text, emit)
        if method == "aux.clear":
            return backend.aux_clear()
        if method == "session.search":
            scope = "all" if str(params.get("scope", "project")) == "all" else "project"
            if scope == "all" and not local:
                # 跨项目搜索是有意的本机产品功能，但远程客户端不该能借它枚举
                # 其他项目的会话 id/标题/片段（安全审查：scope=all 是 B 族的放大器）
                raise RuntimeError("跨项目搜索只能在桌面端本机使用")
            return {
                "query": str(params.get("query", "")),
                "scope": scope,
                "results": await backend.store.search_messages(
                    backend.project.id, str(params.get("query", "")), scope=scope
                ),
            }
        if method == "session.list":
            # 分组侧栏要全部项目的会话；跨项目的标题/摘要是枚举其他项目的放大器，
            # 与 session.search scope=all 同一安全口径：只给本机桌面端（安全审查 B 族）
            if params.get("all_projects"):
                if not local:
                    raise RuntimeError("跨项目会话列表只能在桌面端本机使用")
                grouped = await backend.store.list_sessions_by_project(50)
                sessions = [s for group in grouped.values() for s in group]
            else:
                sessions = await backend.store.list_sessions(backend.project.id)
            empty_count = await backend.store.count_empty_sessions(backend.project.id)
            archived_count = await backend.store.count_archived_sessions(backend.project.id)
            return {
                "empty_count": empty_count,
                "archived_count": archived_count,
                "sessions": [
                    {
                        "id": s.id, "title": s.title, "updated_at": s.updated_at,
                        "pinned": bool(s.pinned), "project_id": s.project_id,
                        "summary": s.summary,
                        "tags": [t for t in str(s.tags or "").split(",") if t],
                        "archived": bool(s.archived),
                    }
                    for s in (sessions if params.get("all_projects") else sessions[:50])
                ],
            }
        if method == "session.new":
            return await backend.new_session()
        if method == "session.truncate":
            return await backend.truncate_session(params)
        if method == "session.fork":
            return await backend.fork_session(params)
        if method == "session.activate":
            return await backend.activate_session(str(params.get("id", "")))
        if method == "session.resume":
            return await backend.resume_session(str(params.get("id", "")))
        if method == "session.image":
            # 历史图片按需拉取（boot/历史只下占位，见 _msg_brief）
            return await backend.session_image(params)
        if method == "session.delete":
            return await backend.delete_session(str(params.get("id", "")))
        if method == "session.export":
            return await backend.export_session(
                str(params.get("id", "")), fmt=str(params.get("fmt", "md"))
            )
        if method == "session.cleanup_empty":
            return await backend.cleanup_empty_sessions()
        if method == "session.rename":
            return await backend.rename_session(
                str(params.get("id", "")), str(params.get("title", ""))
            )
        if method == "session.pin":
            return await backend.pin_session(
                str(params.get("id", "")), bool(params.get("pinned", False))
            )
        if method == "session.archive":
            return await backend.archive_session(
                str(params.get("id", "")), bool(params.get("archived", False))
            )
        if method == "session.list_archived":
            return await backend.list_archived_sessions()
        if method == "session.move":
            pid = params.get("project_id")
            return await backend.move_session(
                str(params.get("id", "")), int(pid) if pid is not None else None
            )
        if method == "session.tags":
            return await backend.set_session_tags(
                str(params.get("id", "")), params.get("tags") or []
            )
        if method == "session.tags_list":
            return {"tags": await backend.store.list_all_tags(backend.project.id)}
        if method == "project.list":
            projects = await backend.store.list_projects()
            # 安全审查 C5：项目根目录的绝对路径是 B/C 族攻击的目标枚举器，
            # 远程客户端只需要名称与当前标记（切换/删除本已限本机）
            return {"projects": [
                {
                    "id": p.id, "name": p.name,
                    "root_path": p.root_path if local else "",
                    "is_current": p.id == backend.project.id,
                }
                for p in projects
            ]}
        if method == "project.switch":
            return await backend.switch_project(str(params.get("path", "")))
        if method == "project.delete":
            return await backend.delete_project(int(params.get("id", 0)))
        if method == "project.instructions":
            return await backend.get_instructions()
        if method == "project.save_instructions":
            return await backend.save_instructions(str(params.get("text", "")))
        if method == "ui.get":
            return await backend.get_ui_prefs()
        if method == "trust.status":
            return await backend.trust_status()
        if method == "trust.grant":
            # 信任一个项目 = 允许执行它自带的本地命令，只能由本机用户在界面上确认
            if not local:
                raise RuntimeError("工作区信任只能在本机界面上确认")
            return await backend.trust_grant()
        if method == "trust.revoke":
            # 收回信任是收紧防护，远调用也允许（避免被远程锁死在信任态）
            return await backend.trust_revoke()
        if method == "ui.save":
            prefs = dict(params.get("prefs") or {})
            if not local and "accept_edits" in prefs:
                # 远端不能间接打开自动写入档（permission.set_mode 已拦，这里再堵一次）
                prefs.pop("accept_edits")
                logger.warning("远程客户端尝试通过 ui.save 设置 accept_edits，已忽略")
            return await backend.save_ui_prefs(prefs)
        if method == "app.notify":
            return await backend.notify(params)
        if method == "app.apply_theme":
            return await backend.apply_theme(params)
        if method == "app.check_update":
            return await backend.check_update()
        if method == "memory.get":
            return await backend.memory_get()
        if method == "memory.save":
            return await backend.memory_save(str(params.get("text", "")))
        if method == "memory.maintain_save":
            gi, pi = params.get("global_enabled"), params.get("project_enabled")
            ih = params.get("interval_hours")
            return await backend.memory_maintain_save(
                global_enabled=None if gi is None else bool(gi),
                project_enabled=None if pi is None else bool(pi),
                interval_hours=None if ih is None else int(ih),
            )
        if method == "memory.maintain_now":
            return await backend.memory_maintain_now()
        if method == "memory.digest_save":
            # 归档自动记忆总闸：只切一个布尔开关，不写敏感配置，远程可调
            return await backend.memory_digest_save(bool(params.get("enabled", True)))
        if method == "advanced.get":
            return backend.advanced_settings()
        if method == "advanced.save":
            return await backend.save_advanced_settings(params)
        if method == "hooks.get":
            return backend.hooks_settings()
        if method == "hooks.save":
            return await backend.save_hooks_settings(params)
        if method == "app.open_path":
            return backend.open_path(str(params.get("kind", "")))
        if method == "app.open_external":
            return backend.open_external(str(params.get("target", "")))
        if method == "app.export_diagnostics":
            return backend.export_diagnostics()
        if method == "demo.enable":
            return await backend.demo_enable()
        if method == "ollama.detect":
            return await backend.ollama_detect()
        if method == "ollama.enable":
            return await backend.ollama_enable(str(params.get("model") or ""))
        if method == "fs.open":
            return await backend.open_workspace_file(str(params.get("path", "")))
        if method == "session.backups":
            # 安全审查 C4：备份列表带绝对路径，远程客户端只看名称/时间
            # （恢复动作本已限本机，列表仅供查看）
            out = backend.list_session_backups()
            if not local:
                out = {
                    **out,
                    "dir": "",
                    "backups": [
                        {**b, "path": ""} for b in out.get("backups", [])
                    ],
                }
            return out
        if method == "session.restore_backup":
            return await backend.restore_session_backup(str(params.get("name", "")))
        if method == "websearch.get":
            return await backend.websearch_detail()
        if method == "websearch.save":
            return await backend.websearch_save(params)
        if method == "roundtable.get":
            return backend.roundtable_detail()
        if method == "roundtable.save":
            return await backend.roundtable_save(params)
        if method == "imagegen.get":
            return await backend.imagegen_detail()
        if method == "imagegen.save":
            return await backend.imagegen_save(params)
        if method == "speech.get":
            return await backend.speech_detail()
        if method == "speech.save":
            return await backend.speech_save(params)
        if method == "speech.transcribe":
            # 录音转文字：只读型能力（把音频交给已配置的服务），不弹确认；
            # 但它会向外发请求，局域网远端不给用（本地终端场景）
            if not local:
                raise RuntimeError("语音输入只能在本机界面上使用")
            return await backend.speech_transcribe(
                str(params.get("audio", "")), str(params.get("mime", ""))
            )
        if method == "settings.export":
            return await backend.settings_export()
        if method == "settings.import":
            return await backend.settings_import(params)
        if method == "lan.status":
            # 令牌就是远程访问凭据本身：只给本机界面（安全审查 A9）
            return await backend.lan_status(include_token=local)
        if method == "lan.enable":
            return await backend.lan_enable(params)
        if method == "lan.disable":
            return await backend.lan_disable()
        if method == "remote.status":
            return await backend.remote_status(include_token=local)
        if method == "remote.enable":
            return await backend.remote_enable(params)
        if method == "remote.disable":
            return await backend.remote_disable()
        # 聊天软件渠道：写配置/启停属降低防护的操作，与开局域网同一姿态——仅本机可改。
        # 远端（局域网/ tailnet）能看状态，但不能改，否则一个被扫码链接挟持的手机
        # 就能把 Agent 接给任意聊天账号。
        if method == "channel.status":
            return await backend.channel_status()
        if method in ("channel.save", "channel.enable", "channel.disable",
                      "channel.set_timeout", "channel.test"):
            if not local:
                raise RuntimeError("聊天机器人渠道的配置只能在桌面端本机修改")
            if method == "channel.save":
                return await backend.channel_save(params)
            if method == "channel.enable":
                return await backend.channel_enable(params)
            if method == "channel.disable":
                return await backend.channel_disable(params)
            if method == "channel.set_timeout":
                return await backend.channel_set_timeout(params)
            return await backend.channel_test(params)
        # 微信扫码登录：生成二维码 / 轮询确认 / 退出登录。扫码得到的是能操控 Agent 的凭据，
        # 与启用渠道同一敏感度，因此同样只允许本机操作。
        if method == "channel.weixin_login_start":
            if not local:
                raise RuntimeError("微信扫码登录只能在桌面端本机完成")
            return await backend.channel_weixin_login_start()
        if method == "channel.weixin_login_poll":
            if not local:
                raise RuntimeError("微信扫码登录只能在桌面端本机完成")
            return await backend.channel_weixin_login_poll(params)
        if method == "channel.weixin_logout":
            if not local:
                raise RuntimeError("微信登录状态只能在桌面端本机修改")
            return await backend.channel_weixin_logout()
        if method == "subagent.get":
            return backend.subagents_detail()
        if method == "subagent.save":
            enabled = params.get("enabled")
            max_iters = params.get("max_iterations")
            return await backend.save_subagent_settings(
                enabled=bool(enabled) if enabled is not None else None,
                max_iterations=(int(max_iters) if max_iters not in (None, "") else None),
            )
        if method == "subagent.save_builtin":
            return await backend.save_subagent_builtin(
                str(params.get("agent_type", "")),
                provider=str(params.get("provider", "")),
                model=str(params.get("model", "")),
                reasoning=str(params.get("reasoning", "")),
            )
        if method == "subagent.save_custom":
            tools = params.get("tools")
            return await backend.save_subagent_custom(
                name=str(params.get("name", "")),
                description=str(params.get("description", "")),
                prompt=str(params.get("prompt", "")),
                tools=tools if isinstance(tools, list) else str(tools if tools else "readonly"),
                provider=str(params.get("provider", "")),
                model=str(params.get("model", "")),
                enabled=bool(params.get("enabled", True)),
            )
        if method == "subagent.delete_custom":
            return await backend.delete_subagent_custom(str(params.get("name", "")))
        if method == "skills.market":
            return await backend.market_list()
        if method == "model.list":
            return {
                "current": backend.provider_name,
                "providers": {
                    name: {
                        "kind": pc.kind,
                        "model": pc.model,
                        "has_key": resolve_api_key(name, pc) is not None,
                    }
                    for name, pc in backend.cfg.providers.items()
                },
            }
        if method == "model.reasoning":
            return {
                "reasoning": backend.reasoning_state(),
            }
        if method == "model.set_reasoning":
            return await backend.set_reasoning_effort(
                str(params.get("effort", "auto")),
                str(params["provider"]) if params.get("provider") else None,
            )
        if method == "model.switch":
            return await backend.switch_model(
                str(params.get("name", "")), model=params.get("model") or None
            )
        if method == "config.add_provider_model":
            return await backend.add_provider_model(
                str(params.get("name", "")), str(params.get("model", ""))
            )
        if method == "config.remove_provider_model":
            return await backend.remove_provider_model(
                str(params.get("name", "")), str(params.get("model", ""))
            )
        if method == "config.providers":
            return await backend.providers_detail()
        if method == "config.save_provider":
            return await backend.save_provider(
                str(params.get("name", "")),
                base_url=params.get("base_url") or None,
                model=params.get("model") or None,
                api_key=params.get("api_key") or None,
                set_default=bool(params.get("set_default", False)),
                supports_reasoning=(
                    bool(params["supports_reasoning"])
                    if params.get("supports_reasoning") is not None else None
                ),
                supports_vision=(
                    bool(params["supports_vision"])
                    if params.get("supports_vision") is not None else None
                ),
                context_limit=(
                    int(params["context_limit"])
                    if params.get("context_limit") not in (None, "") else None
                ),
                # 温度用字符串透传：空串 = 清除该项（回到服务默认）
                temperature=(
                    params["temperature"] if params.get("temperature") is not None else None
                ),
                price_in=(float(params["price_in"]) if params.get("price_in") is not None else None),
                price_out=(float(params["price_out"]) if params.get("price_out") is not None else None),
                proxy=(str(params["proxy"]) if params.get("proxy") is not None else None),
            )
        if method == "config.add_provider":
            models_param = params.get("models")
            return await backend.add_provider(
                str(params.get("name", "")),
                kind=str(params.get("kind", "openai")),
                base_url=str(params.get("base_url", "")),
                model=str(params.get("model", "")),
                api_key=params.get("api_key") or None,
                set_default=bool(params.get("set_default", False)),
                models=[str(m) for m in models_param] if isinstance(models_param, list) else None,
            )
        if method == "config.delete_provider":
            return await backend.delete_provider(str(params.get("name", "")))
        if method == "config.restore_provider":
            return await backend.restore_provider(str(params.get("name", "")))
        if method == "config.set_provider_enabled":
            return await backend.set_provider_enabled(
                str(params.get("name", "")), bool(params.get("enabled", True))
            )
        if method == "config.probe_models":
            return await backend.probe_models(
                name=str(params.get("name", "")),
                kind=str(params.get("kind", "")),
                base_url=str(params.get("base_url", "")),
                api_key=str(params.get("api_key", "")),
            )
        if method == "whitelist.remove":
            # 归属校验在 backend：凭枚举到的 rule_id 不能删别的项目的规则（B11）
            return await backend.remove_whitelist_rule(int(params.get("id", 0)))
        if method == "whitelist.add":
            # 添加规则 = 放行更多操作（降防护），只能在本机界面上操作；
            # 与 permission.set_mode / trust.grant 的约束一致
            if not local:
                raise RuntimeError("白名单添加只能在本机界面上操作")
            return await backend.add_whitelist_rule(
                str(params.get("tool", "")),
                str(params.get("kind", "")),
                str(params.get("pattern", "")),
            )
        if method == "whitelist.clear":
            # 清空是收紧动作，远端也放行（与 remove 一致）
            return await backend.clear_whitelist_rules(str(params.get("kind", "")))
        if method == "whitelist.check":
            return backend.check_whitelist_rule(
                str(params.get("tool", "")), str(params.get("text", ""))
            )
        if method == "whitelist.export":
            return await backend.export_whitelist()
        if method == "whitelist.import":
            # 导入同样是添加规则：与本机约束一致
            if not local:
                raise RuntimeError("白名单导入只能在本机界面上操作")
            rules = params.get("rules")
            return await backend.import_whitelist(rules if isinstance(rules, list) else [])
        if method == "skills.toggle":
            return await backend.toggle_skill(
                str(params.get("name", "")), bool(params.get("enabled", True))
            )
        if method == "skills.scope":
            raw = params.get("projects")
            return await backend.set_skill_scope(
                str(params.get("name", "")),
                str(params.get("mode", "all")),
                [str(p) for p in raw] if isinstance(raw, list) else None,
            )
        if method == "skills.body":
            return backend.skill_body(str(params.get("name", "")))
        if method == "skills.scan_local":
            return await backend.scan_local_skills()
        if method == "skills.install":
            return await backend.install_skill(
                str(params.get("source", "")), str(params.get("scope", "global"))
            )
        if method == "skills.delete":
            return await backend.delete_skill(str(params.get("name", "")))
        if method == "mcp.import":
            return await backend.import_mcp_servers(
                snippet=str(params.get("snippet", "")),
                path=str(params.get("path", "")),
                scope=str(params.get("scope", "global")),
                overwrite=bool(params.get("overwrite", False)),
            )
        if method == "mcp.save_server":
            return await backend.save_mcp_server(
                str(params.get("name", "")),
                command=str(params.get("command", "")),
                args=params.get("args") or None,
                url=str(params.get("url", "")),
                env=params.get("env") or None,
                headers=params.get("headers") or None,
                readonly=bool(params.get("readonly", False)),
                scope=str(params.get("scope", "global")),
            )
        if method == "mcp.delete":
            return await backend.delete_mcp_server(
                str(params.get("name", "")), str(params.get("scope", "global"))
            )
        if method == "mcp.add_preset":
            return await backend.add_mcp_preset(
                str(params.get("name", "")), str(params.get("scope", "global"))
            )
        if method == "mcp.reconnect":
            warnings = await backend._reconnect_mcp()
            return {"mcp": backend._mcp_status_list(backend.mcp), "mcp_warnings": warnings}
        if method == "mcp.status":
            return {"mcp": [
                {"name": n, "connected": st.connected, "error": st.error, "tools": st.tool_names}
                for n, st in backend.mcp.statuses.items()
            ]}
        if method == "tools.list":
            return {"tools": [
                {"name": t.name, "safety": t.safety.value, "description": t.description}
                for t in backend.agent.registry.all()
            ]}
        if method == "whitelist.list":
            rules = await backend.store.list_rules(backend.project.id)
            return {"rules": rules}
        raise RuntimeError("unknown method: " + method)

    # ---- WebSocket 端点 ----

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # 局域网 / 远程访问模式：WS 也要验令牌（query/cookie/头任一），不对就拒绝升级；
        # 仅远程访问（Tailscale）模式时，非 tailnet 来源（物理局域网等）直接拒绝
        cfg = backend.cfg
        lan = bool(cfg is not None and cfg.server.lan)
        ts = bool(cfg is not None and cfg.server.tailscale)
        origin = client_origin(ws.client)
        if ts and not lan and origin == "other":
            await ws.accept()
            await ws.close(code=4403)
            return
        # 浏览器握手的 Origin 必须是本应用自己的页面（安全审查 D5）：被 sandbox
        # 隔离的预览 iframe（Origin: null）与浏览器面板里嵌入的外部网页都过不了这关。
        if not _ws_origin_allowed(ws):
            await ws.accept()
            await ws.close(code=4403)
            return
        # 本机（回环）永远免令牌，与 HTTP 守卫同一规则；tailnet 来源必须验令牌
        token = cfg.server.token if (
            cfg is not None and origin != "local" and (lan or (ts and origin == "tailscale"))
        ) else ""
        if token:
            supplied = (
                ws.query_params.get("token")
                or ws.cookies.get("skysheep_token")
                or ws.headers.get("x-skysheep-token")
                or ""
            )
            if not supplied or not hmac.compare_digest(supplied, token):
                await ws.accept()
                await ws.close(code=4401)
                return
        await ws.accept()
        lock = asyncio.Lock()
        # 客户端来源决定部分方法是否可用（远端不允许切换降低防护的开关）
        client_is_local = _client_is_local(ws.client)
        # 该连接正在交互的会话（activate/resume/chat.send 时更新）：远程连接的
        # 子代理直播 / 任务终态事件只推给它正在看的会话（安全审查 B15），
        # 本机前端不过滤（多标签由前端自行路由）。
        conn_state = {"session": None}

        async def send(obj: dict) -> None:
            async with lock:
                await ws.send_text(json.dumps(obj, ensure_ascii=False, default=str))

        async def emit(ev: dict) -> None:
            if not client_is_local:
                k = ev.get("kind")
                if k == "turn_started" and ev.get("session_id"):
                    # 轮次事件只发给发起连接：据此自动绑定（兼容首条消息懒建会话、
                    # 客户端还没拿到 session_id 的情况）
                    conn_state["session"] = ev["session_id"]
                elif k in ("subagent_event", "task_finished"):
                    sid = ev.get("session_id") or ""
                    if not sid or conn_state["session"] != sid:
                        return  # 不是这个客户端正在交互的会话：不推送
            await send({"event": ev.get("kind", ""), "data": ev})

        backend.ws_emitters.append(emit)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                method = str(msg.get("method", ""))
                params = msg.get("params") or {}

                # 消息发出时先登记目标会话（后续广播事件据此隔离）
                if method == "chat.send" and params.get("session_id"):
                    conn_state["session"] = str(params["session_id"])

                async def process(mid=mid, method=method, params=params):
                    try:
                        result = await dispatch(method, params, emit, local=client_is_local)
                        # 跟踪连接当前交互的会话（B15 的事件隔离用）
                        if method in ("session.new", "session.activate", "session.resume",
                                      "session.fork", "session.truncate", "session.delete"):
                            sid = (result or {}).get("id")
                            if isinstance(sid, str) and sid:
                                conn_state["session"] = sid
                        elif method == "project.switch":
                            sess = ((result or {}).get("session") or {})
                            conn_state["session"] = sess.get("id") if isinstance(sess, dict) else None
                        await send({"id": mid, "ok": True, "result": result})
                    except Exception as e:
                        await send({"id": mid, "ok": False, "error": str(e)})

                asyncio.create_task(process())
        except WebSocketDisconnect:
            return
        finally:
            try:
                backend.ws_emitters.remove(emit)
            except ValueError:
                pass

    return app
