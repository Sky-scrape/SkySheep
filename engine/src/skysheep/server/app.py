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
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import resolve_api_key
from .backend import ServerBackend


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


def _first_paint_attrs(prefs: dict) -> str:
    """把 ui.json 的首帧外观偏好翻译成 <html> 上的属性 / CSS 变量。

    项目切换是整页 reload，而偏好历来只能等 ui.get 返回后再由 JS 应用——
    中间那几十毫秒用的是默认外观（浅色 + 90% 缩放 + 默认栏宽），
    恢复完再跳一次，看起来就是"闪一下"。这里直接由服务端写进首帧 HTML，
    第一帧渲染出来就是用户上次的样子。
    """
    parts = []
    mode = prefs.get("theme") if prefs.get("theme") in ("light", "dark") else "auto"
    parts.append(f'data-theme-mode="{mode}"')
    if mode == "dark":
        parts.append('data-theme="dark"')
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
        import logging

        logging.getLogger("skysheep").info(
            "服务就绪（自进程启动 %.2fs）", time.perf_counter() - PROCESS_START
        )
        yield
        backend.stop_cron_loop()
        backend.stop_reminder_loop()
        await backend.shutdown()
        try:
            flag.unlink(missing_ok=True)
        except OSError:
            pass

    app = FastAPI(title="SkySheep", lifespan=lifespan)

    # ---- 局域网访问令牌守卫（默认关闭 = token 为空，本地直连不设防） ----
    # 开启后服务监听 0.0.0.0（cli/_start_backend 负责），任何请求都要带令牌：
    # ?token=…（首次，成功后写 cookie）/ cookie skysheep_token / 头 X-SkySheep-Token。
    FORBIDDEN_HTML = (
        '<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">'
        "<title>SkySheep</title>"
        '<body style="font-family:sans-serif;background:#1d1a16;color:#f4ecd8;'
        'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
        "<div style='text-align:center'><h1>🐑 SkySheep</h1>"
        "<p>需要访问令牌：请在地址后加 <code>?token=你的令牌</code></p>"
        "<p style='opacity:.6'>令牌在桌面端 设置 · 局域网访问 里查看</p></div></body></html>"
    )

    @app.middleware("http")
    async def _lan_token_guard(request, call_next):
        token = backend.cfg.server.token if (
            backend.cfg is not None and backend.cfg.server.lan
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
        return FileResponse(target)

    # ---- 方法分发 ----

    async def dispatch(method: str, params: dict, emit) -> dict:
        if method == "boot":
            return await backend.snapshot()
        if method == "chat.send":
            text = str(params.get("text", "")).strip()
            images = params.get("images")
            regenerate = bool(params.get("regenerate", False))
            if not text and not (isinstance(images, list) and images) and not regenerate:
                raise RuntimeError("empty text")
            members = params.get("members")
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
            return await backend.set_permission_mode(str(params.get("mode", "confirm")))
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
            return await backend.tasks_list()
        if method == "tasks.cancel_all":
            return await backend.tasks_cancel_all()
        if method == "usage.stats":
            return await backend.usage_stats(params)
        if method == "fs.read":
            return await backend.fs_read(params)
        if method == "snippets.list":
            return {"snippets": await backend.store.list_snippets()}
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
        if method == "term.run":
            return await backend.term_run(str(params.get("command", "")), emit)
        if method == "term.stop":
            return backend.term_stop()
        if method == "chat.aux":
            text = str(params.get("text", ""))
            return await backend.chat_aux(text, emit)
        if method == "aux.clear":
            return backend.aux_clear()
        if method == "session.search":
            scope = "all" if str(params.get("scope", "project")) == "all" else "project"
            return {
                "query": str(params.get("query", "")),
                "scope": scope,
                "results": await backend.store.search_messages(
                    backend.project.id, str(params.get("query", "")), scope=scope
                ),
            }
        if method == "session.list":
            sessions = await backend.store.list_sessions(backend.project.id)
            empty_count = await backend.store.count_empty_sessions(backend.project.id)
            return {
                "empty_count": empty_count,
                "sessions": [
                    {
                        "id": s.id, "title": s.title, "updated_at": s.updated_at,
                        "pinned": bool(s.pinned), "project_id": s.project_id,
                        "summary": s.summary,
                    }
                    for s in sessions[:50]
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
        if method == "session.move":
            pid = params.get("project_id")
            return await backend.move_session(
                str(params.get("id", "")), int(pid) if pid is not None else None
            )
        if method == "project.list":
            projects = await backend.store.list_projects()
            return {"projects": [
                {
                    "id": p.id, "name": p.name, "root_path": p.root_path,
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
        if method == "ui.save":
            return await backend.save_ui_prefs(params.get("prefs") or {})
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
        if method == "advanced.get":
            return backend.advanced_settings()
        if method == "advanced.save":
            return await backend.save_advanced_settings(params)
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
            return backend.list_session_backups()
        if method == "session.restore_backup":
            return await backend.restore_session_backup(str(params.get("name", "")))
        if method == "websearch.get":
            return await backend.websearch_detail()
        if method == "websearch.save":
            return await backend.websearch_save(params)
        if method == "imagegen.get":
            return await backend.imagegen_detail()
        if method == "imagegen.save":
            return await backend.imagegen_save(params)
        if method == "settings.export":
            return await backend.settings_export()
        if method == "settings.import":
            return await backend.settings_import(params)
        if method == "lan.status":
            return await backend.lan_status()
        if method == "lan.enable":
            return await backend.lan_enable(params)
        if method == "lan.disable":
            return await backend.lan_disable()
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
            rule_id = int(params.get("id", 0))
            await backend.store.remove_rule(rule_id)
            await backend.gate.load_project_rules()
            return {"removed": rule_id}
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
        # 局域网模式：WS 也要验令牌（query/cookie/头任一），不对就拒绝升级
        token = backend.cfg.server.token if (
            backend.cfg is not None and backend.cfg.server.lan
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

        async def send(obj: dict) -> None:
            async with lock:
                await ws.send_text(json.dumps(obj, ensure_ascii=False, default=str))

        async def emit(ev: dict) -> None:
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

                async def process(mid=mid, method=method, params=params):
                    try:
                        result = await dispatch(method, params, emit)
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
