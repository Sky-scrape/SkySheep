"""SkySheep 桌面/前端服务层：FastAPI + WebSocket 协议。

协议（JSON 文本帧）：
    客户端请求:  {"id": "r1", "method": "chat.send", "params": {"text": "..."}}
    服务端回复:  {"id": "r1", "ok": true, "result": {...}} / {"id": "r1", "ok": false, "error": "..."}
    事件推送:    {"event": "text_delta", "data": {..AgentEvent 序列化..}}

请求并发处理：chat.send 执行期间，permission.respond / stop 等仍可送达。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import ipaddress
import json
import logging
import mimetypes
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..bgtasks import spawn_bg
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


_OWN_HOST_NAMES: frozenset[str] | None = None


def _own_host_names() -> frozenset[str]:
    """本机可用于访问自己的主机名 / 地址集合（进程内缓存）。

    取不到的部分静默跳过：这份清单只用于「对端是本机时 Host 必须指向本机」
    的判定，宁可少几个别名（大不了回退到拒绍），也不能因为拿不到就把校验关掉。
    """
    global _OWN_HOST_NAMES
    if _OWN_HOST_NAMES is not None:
        return _OWN_HOST_NAMES
    names = {"localhost", "127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        low = hostname.lower()
        names.add(low)
        names.add(low.split(".", 1)[0])
        try:
            fqdn = socket.getfqdn(hostname).lower()
            names.add(fqdn)
            names.add(fqdn.split(".", 1)[0])
        except OSError:
            pass
        # 本机名解析出的各网卡地址（LAN / Tailscale 客户端用的就是这些）
        try:
            for info in socket.getaddrinfo(hostname, None):
                names.add(str(info[4][0]).split("%")[0].lower())
        except OSError:
            pass
    _OWN_HOST_NAMES = frozenset(names)
    return _OWN_HOST_NAMES


def _strip_port(host_header: str) -> str:
    """从 Host 头里取出主机部分（IPv6 字面量带方括号，不能按冒号直接切）。"""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def _host_header_is_own_machine(host_header: str) -> bool:
    """Host 头指向的是不是本机自己。

    DNS rebinding 的关口在这里：攻击者的域名解析到 127.0.0.1 后，受害者浏览器
    发出的请求里 Origin 与 Host 相等（都写着 evil.com），「Origin 与 Host 一致」
    那条检查照过；而按对端 IP 判定又是 local → 免令牌 + 可调本机专属方法。
    真正的判据不是两者一致，而是这个主机名确实是本机自己的地址。
    """
    host = _strip_port(host_header)
    if not host:
        return False
    if host in _own_host_names():
        return True
    try:
        obj = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
        obj = obj.ipv4_mapped
    return bool(obj.is_loopback)


def _request_host_allowed(host_header: str, client) -> bool:
    """对端是本机时，Host 头必须指向本机；其它来源不校验。

    非本机对端不校验是有意的：局域网 / Tailscale 客户端可能用机器名、mDNS 名
    或自定义域名访问，那些名字不在本机清单里，一律拒绍会直接打断远程控制；
    而 rebinding 的前提就是「浏览器从本机发起」，对端必然是回环地址。
    """
    if client_origin(client) != "local":
        return True
    # Starlette TestClient：peer 固定 ("testclient", 50000)、Host 固定 testserver。
    # 只在 peer 确实是 TestClient 时认这个 Host，免得把它变成通用的单标签名白名单。
    peer = client[0] if isinstance(client, (tuple, list)) and client else ""
    if str(peer) == "testclient" and (host_header or "").strip().lower() == "testserver":
        return True
    return _host_header_is_own_machine(host_header)


def _host_guard(app) -> None:
    """HTTP 侧的 Host 校验（WS 侧在 ws_endpoint 里单独做：中间件不覆盖 websocket）。"""

    @app.middleware("http")
    async def _reject_foreign_host(request, call_next):
        if not _request_host_allowed(request.headers.get("host", ""), request.client):
            # 421 Misdirected Request：语义正好是「本服务器不愿为该 Host 服务」
            return JSONResponse({"detail": "Host 不指向本机，已拒绍"}, status_code=421)
        resp = await call_next(request)
        # 界面不允许被外部页面嵌进 iframe（安全审查低危项）：点击劫持面收掉。
        # /preview 自己的 iframe 是内嵌资源、不受这条限制（只作用于本响应）
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.url.path == "/":
            # 主文档上完整 CSP（审查 P3-21）：脚本只认自身文件 + 首帧主题脚本
            # 的内容哈希（index.html 内联的那段「首帧外观」）；样式放行内联
            # （Markdown 渲染器/代码高亮大量内联样式与 style 属性）；iframe 面
            # 板要能打开外部网页（浏览器预览）。其余响应维持仅 frame-ancestors
            # ——/preview 装的是用户项目里的 HTML，自带脚本，套本应用的脚本
            # 白名单会把它们全拦死。
            resp.headers.setdefault(
                "Content-Security-Policy",
                # 首帧主题脚本的 sha256：index.html 里唯一一段内联脚本
                "default-src 'self'; "
                "script-src 'self' 'sha256-c937d1705e12f092136602323e666a5c0c5305678a5f3ee4f5e3ac1fc90392d1'; "  # noqa: E501
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; "
                "connect-src 'self' ws: wss:; "
                "frame-src 'self' http: https:; "
                "media-src 'self' data: blob:; "
                "object-src 'none'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'self'",
            )
        else:
            resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        return resp


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
    """手写静态资源（index.html / app.js / app-tools.js / app.css）一律 no-store。

    前端是零构建手写资源，文件名不带指纹，StaticFiles 默认的 ETag 协商在
    WebView 上可能让旧窗口继续用缓存里的 app.js——改了前端却看不到效果，
    每次都要用户手动强刷。这几个文件加起来不到 500KB 且是本地磁盘读，
    每次都取最新版远比省这点 IO 划算。app-tools.js（工具配置区块，从 app.js
    拆出）与 app.js 必须同版配套，混用新旧两份会缺函数，更要禁缓存。

    刻意不包括 /static/vendor/：那是第三方构建产物（mermaid 单文件 3.3MB）
    且随手写代码一起发版，局域网手机访问时每次重下代价太大——它们靠 ETag
    协商已经足够。
    """
    no_store = {
        "/",
        "/static/index.html",
        "/static/app.js",
        "/static/app-memmap.js",
        "/static/app-projects.js",
        "/static/app-providers.js",
        "/static/app-schedule.js",
        "/static/app-skills.js",
        "/static/app-tools.js",
        "/static/app-whitelist.js",
        "/static/app.css",
        # PWA 两个入口文件同属「手写、无指纹、改版要立即可见」：sw.js 不禁缓存
        # 的话，浏览器按启发式缓存旧副本，前端改版后 PWA 用户迟迟拿不到新 SW
        "/manifest.webmanifest",
        "/sw.js",
    }

    @app.middleware("http")
    async def _static_headers(request, call_next):
        resp = await call_next(request)
        if request.url.path in no_store:
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


STATIC_DIR = _static_dir()

# PWA 清单的 Content-Type：.webmanifest 是否被识别取决于 Python 内置表与
# Windows 注册表（后者因机器而异，可能给成 text/plain），StaticFiles 按它
# 猜类型——显式注册成标准类型，/static/ 下的直连与显式路由返回一致。
# 先 init 再 add_type：注册要压过注册表里可能存在的错误映射。
mimetypes.init()
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _is_hidden_static_path(path: str) -> bool:
    """请求路径是否落在 static 下的点开头（隐藏）文件/目录里。

    纵深防线：StaticFiles 不会拒绝点开头路径，而这些目录（如开发期的
    .mimosa/、外部 Agent 工具会话状态）一旦被误打进安装包，就会随
    /static 挂载变成未认证可读，泄露变更哈希、会话 id 与源码快照。
    打包侧（SkySheep.spec）已在收集阶段跳过点开头路径，这里再堵一道，
    确保即使构建配置回退也不会把这类内容提供出去。
    """
    tail = path[len("/static"):]
    return any(part.startswith(".") for part in tail.split("/") if part)

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
        # auto（或缺省 / 非法值）：服务端不知道系统深浅，交给首帧脚本按系统判定；
        # 深浅各自的落点（设置页「跟随系统时」选的主题）也一并注入，缺省 = 纸墨/夜墨
        parts.append('data-theme-mode="auto"')
        auto_light = prefs.get("theme_auto_light")
        auto_dark = prefs.get("theme_auto_dark")
        if auto_light in ("paper", "celadon", "kaki"):
            parts.append(f'data-theme-auto-light="{auto_light}"')
        if auto_dark in ("night", "indigo", "pine"):
            parts.append(f'data-theme-auto-dark="{auto_dark}"')
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


# ---- WS 方法注册表（表驱动分发的唯一事实来源） ----
# 全部 209 个 WS 方法的分支体自 dispatch 的 if-chain 逐字迁入 handler；
# local_only / local_gate 的判定与拒绝文案，同原 LOCAL_ONLY_METHODS 表与原内联
# 「if … and not local: raise」逐字一致。

_LOCAL_ONLY_MSG = "该操作涉及配置或本机权限，只能在桌面端本机执行"


@dataclasses.dataclass(frozen=True)
class _WsMethod:
    """单个 WS 方法的注册表条目。

    handler 统一签名 ``async def _h_xxx(backend, params, emit, local) -> dict``，
    分支体自 dispatch 逐字迁入。local_only=True 的方法在 local=False 时由分发
    入口以 local_error 拒绝（默认即原 LOCAL_ONLY_METHODS 表的统一文案）；
    local_gate 是按入参判定的条件门禁（返回错误文案或 None），同样只在
    local=False 时由分发入口调用——等价于旧分支内的 ``if <条件> and not local: raise``。
    """

    handler: Callable[..., Awaitable[dict]]
    local_only: bool = False
    local_gate: Callable[[dict], str | None] | None = None
    local_error: str = _LOCAL_ONLY_MSG


def _gate_permission_set_mode(params: dict) -> str | None:
    mode = str(params.get("mode", "confirm"))
    # 「自动编辑 / 完全访问」会放宽写入与执行的确认：只能在本机界面上切换，
    # 不允许被局域网客户端（或远程驱动的前端）打开
    if mode in ("accept_edits", "full_access"):
        return "放宽权限的档位（自动编辑/完全访问）只能在本机界面上切换"
    return None


async def _h_boot(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 安全审查 M7：远程客户端拿到的快照不含本机绝对路径与服务 endpoint
    return await backend.snapshot(local=local)


async def _h_chat_send(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
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
    # AI 总管（二期，建队轮生效）：director_mode 只认 "ai"/"user"（其余按缺省
    # 用户总管处理，与 chat.send 其他错型参数「coercion 吞掉不炸」同一姿态）；
    # director 为 {provider, model}，provider 为空视同未指定
    director_mode = "ai" if params.get("director_mode") == "ai" else "user"
    raw_director = params.get("director")
    director = None
    if isinstance(raw_director, dict):
        d_provider = str(raw_director.get("provider", "") or "").strip()
        if d_provider:
            director = {
                "provider": d_provider,
                "model": str(raw_director.get("model", "") or "").strip(),
            }
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
        team=bool(params.get("team", False)),
        adversarial=bool(params.get("adversarial", False)),
        director_mode=director_mode,
        director=director,
        local=local,
    )


async def _h_chat_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # session_id 可选：给出时查那个会话的 runtime（分屏列的上下文徽标辅路），
    # 查无/已淘汰由后端显式报错，不静默回退当前会话。
    # 远程来源不开放按任意 sid 查询：runtime 状态含 todo 全文与上下文明细，
    # 收敛到该客户端正在交互的会话（与 tasks.list 的远端口径同源，安全审查 B 族）
    sid = str(params.get("session_id", "") or "") if isinstance(params, dict) else ""
    if sid and not local:
        sid = backend.session.id if backend.session else ""
    return await backend.status(session_id=sid or None)


async def _h_chat_compact(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.compact_now()


async def _h_chat_aux(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    text = str(params.get("text", ""))
    # 远端调用走一次性历史：不读本机共享面板历史、也不写入（审查 P1-3 收口）
    return await backend.chat_aux(text, emit, local=local)


async def _h_permission_respond(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {
        "delivered": backend.respond_permission(
            str(params.get("request_id", "")), str(params.get("decision", "deny"))
        )
    }


async def _h_permission_mode(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"mode": backend.permission_mode()}


async def _h_permission_set_mode(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    mode = str(params.get("mode", "confirm"))
    return await backend.set_permission_mode(mode)


async def _h_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    sid = str(params["session_id"]) if params.get("session_id") else ""
    # 远程连接只能停止自己正在交互的会话；连接尚未绑定会话时不能把空 sid
    # 解释成后端当前活动会话，否则拿到令牌的客户端可停止本机其他会话。
    if not local and not sid:
        return {"cancelled": False}
    return {"cancelled": backend.cancel_run(sid or None)}


async def _h_tasks_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 远程客户端只能看自己正在交互的会话的子代理任务（安全审查 B13：
    # 任务详情含 prompt/result，本机任务簿保持全局视图不变）
    only = backend.session.id if (not local and backend.session) else None
    return await backend.tasks_list(params, session_id=only)


async def _h_tasks_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = backend.session.id if (not local and backend.session) else None
    return await backend.tasks_get(params, session_id=only)


async def _h_tasks_cancel_all(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = backend.session.id if (not local and backend.session) else None
    return await backend.tasks_cancel_all(session_id=only)


async def _h_tasks_cancel(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    only = backend.session.id if (not local and backend.session) else None
    return await backend.tasks_cancel(params, session_id=only)


async def _h_aux_model_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.aux_model_state()


async def _h_aux_model_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_aux_model(params)


async def _h_aux_clear(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.aux_clear()


async def _h_model_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
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


async def _h_model_reasoning(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {
        "reasoning": backend.reasoning_state(),
    }


async def _h_model_set_reasoning(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_reasoning_effort(
        str(params.get("effort", "auto")),
        str(params["provider"]) if params.get("provider") else None,
    )


async def _h_model_switch(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.switch_model(
        str(params.get("name", "")), model=params.get("model") or None
    )


async def _h_default_model_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.default_model_state()


async def _h_default_model_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_default_model(params)


async def _h_schedule_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.store.list_schedules(
        include_done=bool(params.get("include_done", False))
    )


async def _h_schedule_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.schedule_add(params)


async def _h_schedule_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.schedule_update(params)


async def _h_schedule_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.schedule_delete(int(params.get("id", 0)))


async def _h_schedule_due(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"schedules": await backend.store.due_schedules()}


async def _h_cron_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_list()


async def _h_cron_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_add(params)


async def _h_cron_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_update(params)


async def _h_cron_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_delete(params)


async def _h_cron_run_now(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_run_now(params)


async def _h_cron_schtask_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_schtask_export(params)


async def _h_cron_schtask_remove(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_schtask_remove(params)


async def _h_cron_schtask_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cron_schtask_status(params)


def _gate_session_search(params: dict) -> str | None:
    scope = "all" if str(params.get("scope", "project")) == "all" else "project"
    if scope == "all":
        # 跨项目搜索是有意的本机产品功能，但远程客户端不该能借它枚举
        # 其他项目的会话 id/标题/片段（安全审查：scope=all 是 B 族的放大器）
        return "跨项目搜索只能在桌面端本机使用"
    return None


def _gate_session_list(params: dict) -> str | None:
    if params.get("all_projects"):
        return "跨项目会话列表只能在桌面端本机使用"
    return None


async def _h_session_search(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    scope = "all" if str(params.get("scope", "project")) == "all" else "project"
    return {
        "query": str(params.get("query", "")),
        "scope": scope,
        "results": await backend.store.search_messages(
            backend._cur_project_id(), str(params.get("query", "")), scope=scope
        ),
    }


async def _h_session_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 分组侧栏要全部项目的会话；跨项目的标题/摘要是枚举其他项目的放大器，
    # 与 session.search scope=all 同一安全口径：只给本机桌面端（安全审查 B 族）
    def _sess_brief(s) -> dict:
        return {
            "id": s.id, "title": s.title, "updated_at": s.updated_at,
            "pinned": bool(s.pinned), "project_id": s.project_id,
            "summary": s.summary,
            "tags": [t for t in str(s.tags or "").split(",") if t],
            "archived": bool(s.archived),
        }

    quick_sessions: list = []
    if params.get("all_projects"):
        grouped = await backend.store.list_sessions_by_project(50)
        sessions = [s for group in grouped.values() for s in group]
    else:
        sessions = (
            await backend.store.list_sessions(backend.project.id)
            if backend.project is not None
            else []
        )
        # 经典视图的快聊区块：快聊会话的 project_id 是 NULL，永远不会出现在
        # 「当前项目」列表里，不带这个字段侧栏就看不到它们。与 all_projects
        # 同一安全口径：快聊不属于当前项目，只给本机桌面端（远程客户端
        # 连字段都不下发，侧栏也就不用显示一个永远为空的区块）。
        if local:
            quick_sessions = await backend.store.list_quick_sessions()
    empty_count = await backend.store.count_empty_sessions(backend._cur_project_id())
    # 归档角标含快聊：侧栏归档弹窗能列快聊归档会话（见 list_archived_sessions），
    # 这里的计数必须同一口径，否则入口不出现，弹窗也就点不开。
    archived_count = await backend.store.count_archived_sessions(
        backend._cur_project_id(), include_projectless=True
    )
    out = {
        "empty_count": empty_count,
        "archived_count": archived_count,
        "sessions": [
            _sess_brief(s)
            for s in (sessions if params.get("all_projects") else sessions[:50])
        ],
    }
    if local and not params.get("all_projects"):
        out["quick_sessions"] = [_sess_brief(s) for s in quick_sessions]
    return out


async def _h_session_new(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.new_session(str(params.get("title", "")))


async def _h_session_new_task(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.create_task_chat()


async def _h_session_truncate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.truncate_session(params, local=local)


async def _h_session_fork(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.fork_session(params, local=local)


async def _h_session_activate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.activate_session(str(params.get("id", "")))


async def _h_session_resume(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.resume_session(str(params.get("id", "")))


async def _h_session_image(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 历史图片按需拉取（boot/历史只下占位，见 _msg_brief）；本机跨项目放行
    return await backend.session_image(params, local=local)


async def _h_session_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_session(str(params.get("id", "")))


async def _h_session_project_path(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_project_path(str(params.get("id", "")))


async def _h_session_peek(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.peek_session_messages(str(params.get("id", "")), local=local)


async def _h_session_model_switch(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_model_switch(
        str(params.get("id", "")),
        str(params.get("name", "")),
        str(params.get("model", "")),
    )


async def _h_session_model_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.session_model_get(str(params.get("id", "")))


async def _h_session_reasoning_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 先取 id 再做任何 await：空参/未知 id 立即报错（本机专属方法整表空参回归的快速失败路径）
    sid = str(params.get("id", "") or "")
    return await backend.session_reasoning_get(sid)


async def _h_session_reasoning_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    sid = str(params.get("id", "") or "")
    return await backend.session_reasoning_set(sid, str(params.get("effort", "auto")))


async def _h_session_reveal(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.reveal_session_project(str(params.get("id", "")))


async def _h_session_accept_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_accept_get(str(params.get("id", "")))


async def _h_session_accept_set(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.session_accept_set(
        str(params.get("id", "")), str(params.get("mode", "confirm"))
    )


async def _h_session_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.export_session(
        str(params.get("id", "")), fmt=str(params.get("fmt", "md"))
    )


async def _h_session_cleanup_empty(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.cleanup_empty_sessions()


async def _h_session_rename(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.rename_session(
        str(params.get("id", "")), str(params.get("title", ""))
    )


async def _h_session_pin(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pin_session(
        str(params.get("id", "")), bool(params.get("pinned", False))
    )


async def _h_session_archive(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.archive_session(
        str(params.get("id", "")), bool(params.get("archived", False))
    )


async def _h_session_list_archived(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.list_archived_sessions()


async def _h_session_move(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    pid = params.get("project_id")
    return await backend.move_session(
        str(params.get("id", "")), int(pid) if pid is not None else None
    )


async def _h_session_tags(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_session_tags(
        str(params.get("id", "")), params.get("tags") or []
    )


async def _h_session_tags_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"tags": await backend.store.list_all_tags(backend._cur_project_id())}


async def _h_session_backups(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
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


async def _h_session_restore_backup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.restore_session_backup(str(params.get("name", "")))


async def _h_session_create_backup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.create_session_backup()


async def _h_session_delete_backup(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_session_backup(str(params.get("name", "")))


async def _h_project_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    projects = await backend.store.list_projects()
    # 拖动排序（分组视图）：有保存的顺序就按它来（见 Backend.ordered_projects）
    projects = backend.ordered_projects(projects)
    # 安全审查 C5：项目根目录的绝对路径是 B/C 族攻击的目标枚举器，
    # 远程客户端只需要名称与当前标记（切换/删除本已限本机）
    return {"projects": [
        {
            "id": p.id, "name": p.name,
            # 「远程连接」固定项目没有真实目录：root_path 报空，
            # 前端据此不提供切换/删除（点击组内会话直接打开）
            "root_path": "" if backend.is_remote_project(p) else (p.root_path if local else ""),
            "is_current": (backend.project is not None and p.id == backend.project.id),
        }
        for p in projects
    ]}


async def _h_project_switch(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.switch_project(str(params.get("path", "")))


async def _h_project_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_project(int(params.get("id", 0)))


async def _h_project_task_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.project_task_list(params.get("project_id"))


async def _h_project_task_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.project_task_add(params)


async def _h_project_task_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.project_task_update(params)


async def _h_project_task_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.project_task_delete(params)


async def _h_project_instructions(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.get_instructions()


async def _h_project_save_instructions(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    bm = params.get("base_mtime")
    return await backend.save_instructions(
        str(params.get("text", "")),
        base_mtime=None if bm is None else float(bm),
    )


async def _h_fs_read(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.fs_read(params)


async def _h_fs_write(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.fs_write(params)


async def _h_fs_files(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.workspace_files()


async def _h_fs_open(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.open_workspace_file(str(params.get("path", "")))


async def _h_snippets_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # builtin 随响应下发：用户把指令删光时，前端 / 菜单用它兜底；
    # initials 供 ~ 菜单按拼音首字母过滤（后端算好，前端不引拼音库）
    rows = await backend.store.list_snippets()
    for r in rows:
        r["initials"] = backend.snippet_initials(str(r.get("name", "")))
    return {"snippets": rows, "builtin": BUILTIN_SNIPPETS}


async def _h_snippets_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    name = str(params.get("name", "")).strip()
    content = str(params.get("content", "")).strip()
    if not name or not content:
        raise RuntimeError("名称与内容不能为空")
    return {"snippet": await backend.store.add_snippet(name, content)}


async def _h_snippets_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    ok = await backend.store.update_snippet(
        int(params.get("id", 0)),
        str(params.get("name", "")).strip(),
        str(params.get("content", "")).strip(),
        None if "enabled" not in params else bool(params.get("enabled")),
    )
    if not ok:
        raise RuntimeError("快捷指令不存在")
    return {"updated": True}


async def _h_snippets_set_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"updated": await backend.store.set_snippet_enabled(
        int(params.get("id", 0)), bool(params.get("enabled"))
    )}


async def _h_snippets_polish(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.snippets_polish(params)


async def _h_snippets_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"deleted": await backend.store.delete_snippet(int(params.get("id", 0)))}


async def _h_snippets_used(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 插入使用上报：计数 +1 与最近使用时间（设置页展示用）；失败不影响插入
    return {"updated": await backend.store.mark_snippet_used(int(params.get("id", 0)))}


async def _h_snippets_reorder(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 拖拽排序提交整份顺序；上限兜底，防异常载荷
    ids = [int(i) for i in (params.get("ids") or [])][:500]
    return {"reordered": await backend.store.reorder_snippets(ids)}


async def _h_snippets_restore_builtin(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"added": await backend.restore_builtin_snippets()}


async def _h_snippets_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.export_snippets()


async def _h_snippets_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.import_snippets(params)


async def _h_checkpoint_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.list_checkpoints()


async def _h_checkpoint_restore(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.restore_checkpoint(
        str(params.get("id", "")), force=bool(params.get("force", False))
    )


async def _h_checkpoint_diff(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.checkpoint_diff(str(params.get("id", "")))


async def _h_diagnostics_turn_breakdown(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 审查页「轮次诊断」：读结构化日志里的每轮耗时拆解（只读，不碰引擎状态）
    if not local:
        # 远程只看当前活动会话（安全审查 B13 同款收窄）：诊断行含时间/耗时/
        # 工具名/token，凭传入 sid 能翻别会话的日志，还能借 turns 空/非空
        # 探测某 id 是否在本机日志里出现过。活动指针为 None 时取空串——
        # 日志行都带真实 session_id，空串匹配不到任何行（探测面为零）
        sid = backend.session.id if backend.session else ""
        return await backend.turn_breakdown(sid, params.get("limit", 20))
    return await backend.turn_breakdown(
        str(params.get("session_id") or ""),
        params.get("limit", 20),
    )


async def _h_term_spawn(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tid = str(params.get("term_id", "") or "")
    rows = int(params.get("rows", 24) or 24)
    cols = int(params.get("cols", 100) or 100)
    return backend.term_spawn(tid, rows, cols)


async def _h_term_input(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tid = str(params.get("term_id", "") or "")
    rows = int(params.get("rows", 24) or 24)
    cols = int(params.get("cols", 100) or 100)
    return backend.term_input(tid, str(params.get("data", "")), rows, cols)


async def _h_term_resize(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    tid = str(params.get("term_id", "") or "")
    rows = int(params.get("rows", 24) or 24)
    cols = int(params.get("cols", 100) or 100)
    return backend.term_resize(tid, rows, cols)


async def _h_term_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # Ctrl+C 是收紧动作，远端也放行（否则远程发起的程序停不下来）；
    # 不带 term_id 时发给所有终端标签的前台进程
    return backend.term_stop(str(params.get("term_id", "") or ""))


async def _h_term_close(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 关闭终端标签：结束该标签的 shell 并丢弃执行槽
    return backend.term_close(str(params.get("term_id", "") or ""))


async def _h_usage_stats(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.usage_stats(params)


async def _h_pipeline_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_list()


async def _h_pipeline_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_get(params)


async def _h_pipeline_create(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_create(params)


async def _h_pipeline_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_update(params)


async def _h_pipeline_start(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_start(params)


async def _h_pipeline_cancel(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_cancel(params)


async def _h_pipeline_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_delete(params)


async def _h_pipeline_node_rerun(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_node_rerun(params)


async def _h_pipeline_attach(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_attach(params)


async def _h_pipeline_import_cron(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_import_cron(params)


async def _h_pipeline_add_session(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_add_session(params)


async def _h_pipeline_add_task(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_add_task(params)


async def _h_pipeline_duplicate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_duplicate(params)


async def _h_pipeline_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_export(params)


async def _h_pipeline_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.pipeline_import(params)


async def _h_automation_report_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 每日运行日报的设置回读：只有开关 / 时刻 / 上次已发日期，不含敏感面
    return await backend.daily_report_status()


async def _h_automation_report_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.daily_report_save(params)


async def _h_run_center_summary(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 今日运行总览（只读聚合）：明细已按当前项目过滤（B14 同款），远端与本地同视图
    return await backend.run_center_summary()


async def _h_ui_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.get_ui_prefs()


async def _h_trust_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.trust_status()


async def _h_trust_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 清单含全部项目的绝对路径：与 session.backups 同理，只给本机界面
    return backend.trust_list()


async def _h_trust_revoke_path(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 撤销信任是收紧动作，远端也允许（避免被远程锁死在信任态）
    return await backend.trust_revoke_path(str(params.get("path", "")))


async def _h_trust_grant(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 信任一个项目 = 允许执行它自带的本地命令，只能由本机用户在界面上确认
    return await backend.trust_grant()


async def _h_trust_revoke(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 收回信任是收紧防护，远调用也允许（避免被远程锁死在信任态）
    return await backend.trust_revoke()


async def _h_ui_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    prefs = dict(params.get("prefs") or {})
    if not local and "accept_edits" in prefs:
        # 远端不能间接打开自动写入档（permission.set_mode 已拦，这里再堵一次）
        prefs.pop("accept_edits")
        logger.warning("远程客户端尝试通过 ui.save 设置 accept_edits，已忽略")
    return await backend.save_ui_prefs(prefs)


async def _h_memory_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_get()


async def _h_memory_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    bm = params.get("base_mtime")
    return await backend.memory_save(
        str(params.get("text", "")),
        base_mtime=None if bm is None else float(bm),
    )


async def _h_memory_maintain_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    gi, pi = params.get("global_enabled"), params.get("project_enabled")
    ih = params.get("interval_hours")
    return await backend.memory_maintain_save(
        global_enabled=None if gi is None else bool(gi),
        project_enabled=None if pi is None else bool(pi),
        interval_hours=None if ih is None else int(ih),
    )


async def _h_memory_maintain_now(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_maintain_now()


async def _h_memory_digest_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 归档自动记忆总闸：只切一个布尔开关，不写敏感配置，远程可调
    return await backend.memory_digest_save(bool(params.get("enabled", True)))


async def _h_memory_candidates(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 轮次沉淀的待审候选：条目带上下文摘录与会话归属（对话内容的枚举面），
    # 与 memory.get 同一姿态——本机专属
    return await backend.memory_candidates()


async def _h_memory_candidate_adopt(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_candidate_adopt(str(params.get("candidate_id", "")))


async def _h_memory_candidate_ignore(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.memory_candidate_ignore(str(params.get("candidate_id", "")))


async def _h_memory_distill_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 轮次沉淀总闸：只切一个布尔开关（引擎自有状态文件），与 memory.digest_save
    # 同一姿态，远程可调
    return await backend.memory_distill_save(bool(params.get("enabled", False)))


async def _h_map_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 记忆地图载荷：跨项目查看是枚举面（标题/摘要/文件路径），只给本机
    return await backend.map_get(params, local=local)


async def _h_map_generate(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.map_generate(params, local=local)


async def _h_map_save_config(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.map_save_config(params)


async def _h_advanced_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.advanced_settings()


async def _h_advanced_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_advanced_settings(params)


async def _h_retention_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 数据保留策略回读：各类保留天数与目录占用（只读度量面，不含敏感内容）
    return await backend.retention_status()


async def _h_retention_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.retention_save(params)


async def _h_retention_sweep(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 立即清理删的是数据目录已知目录里的过期文件，属本机维护动作
    return await backend.retention_sweep_now()


async def _h_hooks_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.hooks_settings()


async def _h_hooks_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_hooks_settings(params)


async def _h_hooks_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 测试会真实执行命令，与 hooks.save 同一信任模型：本机专属
    return await backend.test_hook(params)


async def _h_app_notify(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.notify(params)


async def _h_app_apply_theme(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.apply_theme(params)


async def _h_app_check_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.check_update()


async def _h_app_install_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_update()


async def _h_app_apply_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.apply_update()


async def _h_app_restart(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 重启等于在本机拉起新进程：LOCAL_ONLY_METHODS 已把远程拦下
    return await backend.app_restart()


async def _h_app_open_path(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.open_path(str(params.get("kind", "")))


async def _h_app_open_external(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.open_external(str(params.get("target", "")), local=local)


async def _h_app_export_diagnostics(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.export_diagnostics()


async def _h_lan_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 令牌就是远程访问凭据本身：只给本机界面（安全审查 A9）
    return await backend.lan_status(include_token=local)


async def _h_lan_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.lan_enable(params)


async def _h_lan_disable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.lan_disable()


async def _h_lan_rotate_token(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 换共享令牌立即生效：旧地址/二维码/cookie 全部作废（LOCAL_ONLY 已拦远程）
    return await backend.lan_rotate_token()


async def _h_lan_set_port(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 固定服务端口（PWA 的 start_url 在安装时固化 origin 含端口）：改配置类
    # 设置，LOCAL_ONLY 已拦远程
    return await backend.lan_set_port(params)


async def _h_remote_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remote_status(include_token=local)


async def _h_remote_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remote_enable(params)


async def _h_remote_disable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remote_disable()


async def _h_demo_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.demo_enable()


async def _h_ollama_detect(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.ollama_detect()


async def _h_ollama_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.ollama_enable(str(params.get("model") or ""))


async def _h_websearch_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.websearch_detail()


async def _h_websearch_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.websearch_save(params)


async def _h_roundtable_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.roundtable_detail()


async def _h_roundtable_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.roundtable_save(params)


# 对抗：设置页配置卡（仿 roundtable.get/save：读写均各端可用，改动只影响
# 之后发起的对抗轮）；对抗轮本体走 chat.send 的 adversarial 标志。
async def _h_adversarial_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.adversarial_detail()


async def _h_adversarial_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.adversarial_save(params)


# 团队：工单板只由用户经这些方法触达，聊天仍走 chat.send（会话级活动团队
# 会把消息吸进频道；AI 总管模式经 director_mode/director 建队并自动闭环）。
# 参数错误（无活动团队 / 指派对象不在名册 / 状态机不许可 / redo 超限）按
# TeamError 原文回给调用方。
def _team_scoped_params(backend: ServerBackend, params: dict, local: bool) -> dict:
    """非本机连接把 session_id 收敛到其绑定的活动会话（沿 tasks.* 既有口径）。

    团队快照、工单板、收队/交付/接管与频道回放只对「该连接正在交互的会话」
    生效——team.* 的归属校验不能信任调用方断言的 sid（sid 可经 session.list
    枚举）：断言他人 sid 读他人团队快照与全量频道文本、加单/改态他人工单板、
    越权交付乃至收队他人团队，在入口即被收敛拒绝。返回新 dict，不改调用方的
    参数对象；本机调用不受影响（前端显式传 sid 的既有用法照旧）。
    """
    if not local and backend.session is not None:
        params = {**params, "session_id": backend.session.id}
    return params


async def _h_team_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_get(_team_scoped_params(backend, params, local))


async def _h_team_task_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_task_add(_team_scoped_params(backend, params, local))


async def _h_team_task_update(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_task_update(_team_scoped_params(backend, params, local))


async def _h_team_stop(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_stop(_team_scoped_params(backend, params, local))


async def _h_team_deliver(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_deliver(_team_scoped_params(backend, params, local))


async def _h_team_takeover(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_takeover(_team_scoped_params(backend, params, local))


# 三期：频道回放（收队/重启后按 meta 里的 team_id 拉全量，归属校验在 backend）、
# 建队模板（全局册：读各端可看，写仅本机——与子代理定义同姿态）、设置页配置卡
# （仿 roundtable.get/save：读写均各端可用，改动只影响之后新建的团队）。
async def _h_team_log(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_log(_team_scoped_params(backend, params, local))


async def _h_team_template_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_template_list()


async def _h_team_template_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_template_save(params)


async def _h_team_template_remove(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_template_remove(params)


async def _h_teamcfg_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.team_config_detail()


async def _h_teamcfg_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.team_config_save(params)


async def _h_imagegen_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.imagegen_detail()


async def _h_imagegen_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.imagegen_save(params)


async def _h_speech_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.speech_detail()


async def _h_speech_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.speech_save(params)


async def _h_speech_transcribe(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 录音转文字：只读型能力（把音频交给已配置的服务），不弹确认；
    # 但它会向外发请求，局域网远端不给用（本地终端场景）
    return await backend.speech_transcribe(
        str(params.get("audio", "")), str(params.get("mime", ""))
    )


async def _h_settings_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.settings_export()


async def _h_settings_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.settings_import(params)


async def _h_channel_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_status()


async def _h_channel_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_save(params)


async def _h_channel_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_enable(params)


async def _h_channel_disable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_disable(params)


async def _h_channel_set_timeout(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_set_timeout(params)


async def _h_channel_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_test(params)


async def _h_channel_weixin_login_start(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_weixin_login_start()


async def _h_channel_weixin_login_poll(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_weixin_login_poll(params)


async def _h_channel_weixin_logout(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.channel_weixin_logout()


async def _h_config_add_provider_model(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.add_provider_model(
        str(params.get("name", "")), str(params.get("model", ""))
    )


async def _h_config_remove_provider_model(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.remove_provider_model(
        str(params.get("name", "")), str(params.get("model", ""))
    )


async def _h_config_providers(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.providers_detail()


async def _h_config_save_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
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
        price_cache=(float(params["price_cache"]) if params.get("price_cache") is not None else None),
        proxy=(str(params["proxy"]) if params.get("proxy") is not None else None),
    )


async def _h_config_add_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
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


async def _h_config_delete_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_provider(str(params.get("name", "")))


async def _h_config_restore_provider(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.restore_provider(str(params.get("name", "")))


async def _h_config_set_provider_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_provider_enabled(
        str(params.get("name", "")), bool(params.get("enabled", True))
    )


async def _h_config_probe_models(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.probe_models(
        name=str(params.get("name", "")),
        kind=str(params.get("kind", "")),
        base_url=str(params.get("base_url", "")),
        api_key=str(params.get("api_key", "")),
    )


async def _h_config_probe_context(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.probe_context(
        name=str(params.get("name", "")),
        kind=str(params.get("kind", "")),
        base_url=str(params.get("base_url", "")),
        api_key=str(params.get("api_key", "")),
        model=str(params.get("model", "")),
    )


def _gate_whitelist_enable(params: dict) -> str | None:
    enabling = bool(params.get("enabled", True))
    if enabling:
        return "启用白名单规则只能在本机界面上操作"
    return None


async def _h_subagent_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.subagents_detail()


async def _h_subagent_save(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    enabled = params.get("enabled")
    max_iters = params.get("max_iterations")
    max_conc = params.get("max_concurrent")
    return await backend.save_subagent_settings(
        enabled=bool(enabled) if enabled is not None else None,
        max_iterations=(int(max_iters) if max_iters not in (None, "") else None),
        max_concurrent=(int(max_conc) if max_conc not in (None, "") else None),
    )


async def _h_subagent_save_builtin(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_subagent_builtin(
        str(params.get("agent_type", "")),
        provider=str(params.get("provider", "")),
        model=str(params.get("model", "")),
        reasoning=str(params.get("reasoning", "")),
        description=str(params.get("description", "")),
        prompt=str(params.get("prompt", "")),
    )


async def _h_subagent_save_custom(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
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


async def _h_subagent_delete_custom(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_subagent_custom(str(params.get("name", "")))


async def _h_whitelist_remove(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 归属校验在 backend：凭枚举到的 rule_id 不能删别的项目的规则（B11）
    return await backend.remove_whitelist_rule(int(params.get("id", 0)))


async def _h_whitelist_add(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 添加规则 = 放行更多操作（降防护），只能在本机界面上操作；
    # 与 permission.set_mode / trust.grant 的约束一致
    return await backend.add_whitelist_rule(
        str(params.get("tool", "")),
        str(params.get("kind", "")),
        str(params.get("pattern", "")),
    )


async def _h_whitelist_clear(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 清空是收紧动作，远端也放行（与 remove 一致）
    return await backend.clear_whitelist_rules(str(params.get("kind", "")))


async def _h_whitelist_check(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.check_whitelist_rule(
        str(params.get("tool", "")), str(params.get("text", ""))
    )


async def _h_whitelist_export(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.export_whitelist()


async def _h_whitelist_enable(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 启停规则：停用是收紧（远端也允许），启用是放宽 = 与 whitelist.add 同限本机
    enabling = bool(params.get("enabled", True))
    return await backend.set_whitelist_rule_enabled(
        int(params.get("id", 0)), enabling
    )


async def _h_whitelist_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 导入同样是添加规则：与本机约束一致
    rules = params.get("rules")
    return await backend.import_whitelist(rules if isinstance(rules, list) else [])


async def _h_skills_toggle(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.toggle_skill(
        str(params.get("name", "")), bool(params.get("enabled", True))
    )


async def _h_skills_scope(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    raw = params.get("projects")
    return await backend.set_skill_scope(
        str(params.get("name", "")),
        str(params.get("mode", "all")),
        [str(p) for p in raw] if isinstance(raw, list) else None,
    )


async def _h_skills_body(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.skill_body(str(params.get("name", "")))


async def _h_skills_scan_local(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.scan_local_skills()


async def _h_skills_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_skill(
        str(params.get("source", "")), str(params.get("scope", "global")),
        overwrite=bool(params.get("overwrite", False)),
    )


async def _h_skills_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_skill(str(params.get("name", "")))


async def _h_skills_gallery(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 纯本地只读（打包内清单 + 已装比对）：与技能清单同级，不进本机专属表
    return backend.gallery_skills()


async def _h_skills_save_from_session(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 只读生成（读会话消息 → 返回草稿文本）：写盘发生在 skills.save_draft
    return await backend.save_skill_from_session(str(params.get("id", "")))


async def _h_skills_save_draft(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 落盘写 SKILL.md：与 skills.install 同级（技能正文进 system prompt），仅本机
    return await backend.save_skill_draft(
        str(params.get("name", "")),
        str(params.get("content", "")),
        scope=str(params.get("scope", "global")),
    )


# ---- Mods 扩展（实验性）----
# 只读方法（list/get/get_template/official）各端可看；全部写方法（安装/启停/删除/
# 草稿/测试器）与本机执行面同级——Mod 是第三方代码且能收紧权限门行为，绝不能被
# 远程持令牌客户端安装或改动。

async def _h_mods_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.list_mods()


async def _h_mods_get(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.get_mod(str(params.get("id", "")))


async def _h_mods_get_template(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.mod_template()


async def _h_mods_official(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return backend.official_mods()


async def _h_mods_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_mod(str(params.get("source", "")))


async def _h_mods_confirm_install(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.confirm_install_mod(str(params.get("install_token", "")))


async def _h_mods_install_official(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.install_official_mod(str(params.get("id", "")))


async def _h_mods_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_mod(str(params.get("id", "")))


async def _h_mods_toggle(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.toggle_mod(
        str(params.get("id", "")), bool(params.get("enabled", True))
    )


async def _h_mods_set_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_mods_enabled(bool(params.get("enabled", False)))


async def _h_mods_save_draft(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 草稿写进 <项目>/.skysheep/mods-drafts/：安装仍要两段人工确认
    return await backend.save_mod_draft(params)


async def _h_mods_test(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 实跑一个 handler（不记执行记录）：本机专属，与 hooks.test 同姿态
    return await backend.test_mod(params)


async def _h_mcp_import(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.import_mcp_servers(
        snippet=str(params.get("snippet", "")),
        path=str(params.get("path", "")),
        scope=str(params.get("scope", "global")),
        overwrite=bool(params.get("overwrite", False)),
        # M10：含 stdio 定义时先回 needs_confirm，前端确认后带 true 重试
        confirmed=bool(params.get("confirmed", False)),
    )


async def _h_mcp_save_server(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.save_mcp_server(
        str(params.get("name", "")),
        command=str(params.get("command", "")),
        args=params.get("args") or None,
        url=str(params.get("url", "")),
        env=params.get("env") or None,
        headers=params.get("headers") or None,
        readonly=bool(params.get("readonly", False)),
        timeout=float(params.get("timeout") or 0),
        scope=str(params.get("scope", "global")),
    )


async def _h_mcp_delete(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.delete_mcp_server(
        str(params.get("name", "")), str(params.get("scope", "global"))
    )


async def _h_mcp_add_preset(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.add_mcp_preset(
        str(params.get("name", "")), str(params.get("scope", "global"))
    )


async def _h_mcp_set_enabled(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return await backend.set_mcp_enabled(
        str(params.get("name", "")),
        bool(params.get("enabled", True)),
        str(params.get("scope", "global")),
    )


async def _h_mcp_reconnect(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    warnings = await backend._reconnect_mcp()
    return {"mcp": backend._mcp_status_list(backend.mcp), "mcp_warnings": warnings}


async def _h_mcp_status(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"mcp": backend._mcp_status_list(backend.mcp)}


async def _h_tools_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    return {"tools": [
        {"name": t.name, "safety": t.safety.value, "description": t.description}
        for t in backend.agent.registry.all()
    ]}


async def _h_whitelist_list(backend: ServerBackend, params: dict, emit, local: bool) -> dict:
    # 走 backend：除库字段外还给每条规则带 stale / stale_reason（失效徽章）
    return {"rules": await backend.list_whitelist_rules()}


_WS_METHODS: dict[str, _WsMethod] = {
    "boot": _WsMethod(_h_boot),
    "chat.send": _WsMethod(_h_chat_send),
    "chat.status": _WsMethod(_h_chat_status),
    "chat.compact": _WsMethod(_h_chat_compact),
    "chat.aux": _WsMethod(_h_chat_aux),
    "permission.respond": _WsMethod(_h_permission_respond),
    "permission.mode": _WsMethod(_h_permission_mode),
    "permission.set_mode": _WsMethod(_h_permission_set_mode, local_gate=_gate_permission_set_mode),
    "stop": _WsMethod(_h_stop),
    "tasks.list": _WsMethod(_h_tasks_list),
    "tasks.get": _WsMethod(_h_tasks_get),
    "tasks.cancel_all": _WsMethod(_h_tasks_cancel_all),
    "tasks.cancel": _WsMethod(_h_tasks_cancel),
    "aux.model.get": _WsMethod(_h_aux_model_get),
    "aux.model.set": _WsMethod(_h_aux_model_set, local_only=True),
    "aux.clear": _WsMethod(_h_aux_clear),
    "model.list": _WsMethod(_h_model_list),
    "model.reasoning": _WsMethod(_h_model_reasoning),
    "model.set_reasoning": _WsMethod(_h_model_set_reasoning),
    "model.switch": _WsMethod(_h_model_switch, local_only=True),
    "default_model.get": _WsMethod(_h_default_model_get),
    "default_model.set": _WsMethod(_h_default_model_set, local_only=True),
    "schedule.list": _WsMethod(_h_schedule_list),
    "schedule.add": _WsMethod(_h_schedule_add),
    "schedule.update": _WsMethod(_h_schedule_update),
    "schedule.delete": _WsMethod(_h_schedule_delete),
    "schedule.due": _WsMethod(_h_schedule_due),
    "cron.list": _WsMethod(_h_cron_list),
    "cron.add": _WsMethod(_h_cron_add, local_only=True),
    "cron.update": _WsMethod(_h_cron_update, local_only=True),
    "cron.delete": _WsMethod(_h_cron_delete, local_only=True),
    "cron.run_now": _WsMethod(_h_cron_run_now, local_only=True),
    "cron.schtask_export": _WsMethod(_h_cron_schtask_export, local_only=True),
    "cron.schtask_remove": _WsMethod(_h_cron_schtask_remove, local_only=True),
    "cron.schtask_status": _WsMethod(_h_cron_schtask_status),
    "session.search": _WsMethod(_h_session_search, local_gate=_gate_session_search),
    "session.list": _WsMethod(_h_session_list, local_gate=_gate_session_list),
    "session.new": _WsMethod(_h_session_new),
    "session.new_task": _WsMethod(_h_session_new_task),
    "session.truncate": _WsMethod(_h_session_truncate),
    "session.fork": _WsMethod(_h_session_fork),
    "session.activate": _WsMethod(_h_session_activate),
    "session.resume": _WsMethod(_h_session_resume),
    "session.image": _WsMethod(_h_session_image),
    "session.delete": _WsMethod(_h_session_delete),
    "session.project_path": _WsMethod(_h_session_project_path, local_only=True),
    "session.reveal": _WsMethod(_h_session_reveal, local_only=True),
    "session.peek_messages": _WsMethod(_h_session_peek),
    "session.model_switch": _WsMethod(_h_session_model_switch, local_only=True),
    "session.model_get": _WsMethod(_h_session_model_get, local_only=True),
    # 会话级思考强度（本机专属）：读不建 runtime，写换会话私有 provider
    # 会话级权限三档（本机专属）：只动该会话私有门，引擎默认档不受影响
    "session.accept_get": _WsMethod(_h_session_accept_get, local_only=True),
    "session.accept_set": _WsMethod(_h_session_accept_set, local_only=True),
    "session.reasoning_get": _WsMethod(_h_session_reasoning_get, local_only=True),
    "session.reasoning_set": _WsMethod(_h_session_reasoning_set, local_only=True),
    "session.export": _WsMethod(_h_session_export),
    "session.cleanup_empty": _WsMethod(_h_session_cleanup_empty),
    "session.rename": _WsMethod(_h_session_rename),
    "session.pin": _WsMethod(_h_session_pin),
    "session.archive": _WsMethod(_h_session_archive),
    "session.list_archived": _WsMethod(_h_session_list_archived),
    "session.move": _WsMethod(_h_session_move),
    "session.tags": _WsMethod(_h_session_tags),
    "session.tags_list": _WsMethod(_h_session_tags_list),
    "session.backups": _WsMethod(_h_session_backups),
    "session.restore_backup": _WsMethod(_h_session_restore_backup, local_only=True),
    "session.create_backup": _WsMethod(_h_session_create_backup, local_only=True),
    "session.delete_backup": _WsMethod(_h_session_delete_backup, local_only=True),
    "project.list": _WsMethod(_h_project_list),
    "project.switch": _WsMethod(_h_project_switch, local_only=True),
    "project.delete": _WsMethod(_h_project_delete, local_only=True),
    "project.task_list": _WsMethod(_h_project_task_list),
    "project.task_add": _WsMethod(_h_project_task_add),
    "project.task_update": _WsMethod(_h_project_task_update),
    "project.task_delete": _WsMethod(_h_project_task_delete),
    "project.instructions": _WsMethod(_h_project_instructions),
    "project.save_instructions": _WsMethod(_h_project_save_instructions, local_only=True),
    "fs.read": _WsMethod(_h_fs_read),
    "fs.write": _WsMethod(_h_fs_write),
    "fs.files": _WsMethod(_h_fs_files),
    "fs.open": _WsMethod(_h_fs_open, local_only=True),
    "snippets.list": _WsMethod(_h_snippets_list),
    "snippets.add": _WsMethod(_h_snippets_add),
    "snippets.update": _WsMethod(_h_snippets_update),
    "snippets.set_enabled": _WsMethod(_h_snippets_set_enabled),
    "snippets.polish": _WsMethod(_h_snippets_polish),
    "snippets.delete": _WsMethod(_h_snippets_delete),
    "snippets.used": _WsMethod(_h_snippets_used),
    "snippets.reorder": _WsMethod(_h_snippets_reorder),
    "snippets.restore_builtin": _WsMethod(_h_snippets_restore_builtin),
    "snippets.export": _WsMethod(_h_snippets_export),
    "snippets.import": _WsMethod(_h_snippets_import),
    "checkpoint.list": _WsMethod(_h_checkpoint_list),
    "checkpoint.restore": _WsMethod(_h_checkpoint_restore),
    "checkpoint.diff": _WsMethod(_h_checkpoint_diff),
    "diagnostics.turn_breakdown": _WsMethod(_h_diagnostics_turn_breakdown),
    # 终端是常驻 shell（用户手敲命令，不进权限门，见 TerminalManager 注释），
    # 所以它只在“用户就在这台机器面前”时成立。局域网模式下持有令牌的设备
    # 也能连 WS，若允许它调用这里，令牌就等于 shell 访问权限。
    "term.spawn": _WsMethod(_h_term_spawn, local_only=True, local_error="终端面板只能在本机上使用"),
    "term.input": _WsMethod(_h_term_input, local_only=True, local_error="终端面板只能在本机上使用"),
    "term.resize": _WsMethod(_h_term_resize, local_only=True, local_error="终端面板只能在本机上使用"),
    "term.stop": _WsMethod(_h_term_stop),
    "term.close": _WsMethod(_h_term_close, local_only=True),
    "usage.stats": _WsMethod(_h_usage_stats),
    "pipeline.list": _WsMethod(_h_pipeline_list),
    "pipeline.get": _WsMethod(_h_pipeline_get),
    "pipeline.create": _WsMethod(_h_pipeline_create, local_only=True),
    "pipeline.update": _WsMethod(_h_pipeline_update, local_only=True),
    "pipeline.start": _WsMethod(_h_pipeline_start, local_only=True),
    "pipeline.cancel": _WsMethod(_h_pipeline_cancel, local_only=True),
    "pipeline.delete": _WsMethod(_h_pipeline_delete, local_only=True),
    "pipeline.node_rerun": _WsMethod(_h_pipeline_node_rerun, local_only=True),
    "pipeline.attach": _WsMethod(_h_pipeline_attach, local_only=True),
    "pipeline.import_cron": _WsMethod(_h_pipeline_import_cron, local_only=True),
    "pipeline.add_session": _WsMethod(_h_pipeline_add_session, local_only=True),
    "pipeline.add_task": _WsMethod(_h_pipeline_add_task, local_only=True),
    "pipeline.duplicate": _WsMethod(_h_pipeline_duplicate, local_only=True),
    "pipeline.export": _WsMethod(_h_pipeline_export),
    "pipeline.import": _WsMethod(_h_pipeline_import, local_only=True),
    # 每日运行日报（无人值守三期）：状态只读各端可看；开启属无人值守推送面，
    # 与 cron.add 同一姿态仅本机
    "automation.daily_report_status": _WsMethod(_h_automation_report_status),
    "automation.daily_report_save": _WsMethod(
        _h_automation_report_save, local_only=True,
        local_error="运行日报的设置只能在桌面端本机修改",
    ),
    # 今日运行总览：只读聚合，明细已按当前项目过滤，各端同视图可看
    "automation.run_center_summary": _WsMethod(_h_run_center_summary),
    "ui.get": _WsMethod(_h_ui_get),
    "trust.status": _WsMethod(_h_trust_status),
    "trust.list": _WsMethod(_h_trust_list, local_only=True, local_error="信任清单只能在本机界面上查看"),
    "trust.revoke_path": _WsMethod(_h_trust_revoke_path),
    "trust.grant": _WsMethod(_h_trust_grant, local_only=True, local_error="工作区信任只能在本机界面上确认"),
    "trust.revoke": _WsMethod(_h_trust_revoke),
    "ui.save": _WsMethod(_h_ui_save),
    "memory.get": _WsMethod(_h_memory_get, local_only=True),
    "memory.save": _WsMethod(_h_memory_save, local_only=True),
    "memory.maintain_save": _WsMethod(_h_memory_maintain_save),
    "memory.maintain_now": _WsMethod(_h_memory_maintain_now),
    "memory.digest_save": _WsMethod(_h_memory_digest_save),
    # 轮次记忆沉淀（记忆二期）：候选条目带对话摘录与会话归属，本机专属；
    # 开关只切布尔，与 memory.digest_save 同姿态
    "memory.candidates": _WsMethod(
        _h_memory_candidates, local_only=True,
        local_error="记忆候选列表只能在本机界面上查看",
    ),
    "memory.candidate_adopt": _WsMethod(
        _h_memory_candidate_adopt, local_only=True,
        local_error="记忆候选的采纳与忽略只能在本机界面上操作",
    ),
    "memory.candidate_ignore": _WsMethod(
        _h_memory_candidate_ignore, local_only=True,
        local_error="记忆候选的采纳与忽略只能在本机界面上操作",
    ),
    "memory.distill_save": _WsMethod(_h_memory_distill_save),
    "map.get": _WsMethod(_h_map_get),
    "map.generate": _WsMethod(_h_map_generate),
    "map.save_config": _WsMethod(_h_map_save_config),
    "advanced.get": _WsMethod(_h_advanced_get),
    "advanced.save": _WsMethod(_h_advanced_save, local_only=True),
    # 数据保留策略（只进不出目录的治理）：状态各端可看；改保留天数与立即清理
    # 是删文件的动作面，与 advanced.save 同姿态仅本机
    "retention.status": _WsMethod(_h_retention_status),
    "retention.save": _WsMethod(
        _h_retention_save, local_only=True,
        local_error="数据保留设置只能在桌面端本机修改",
    ),
    "retention.sweep": _WsMethod(
        _h_retention_sweep, local_only=True,
        local_error="立即清理只能在桌面端本机执行",
    ),
    "hooks.get": _WsMethod(_h_hooks_get),
    "hooks.save": _WsMethod(_h_hooks_save, local_only=True),
    "hooks.test": _WsMethod(_h_hooks_test, local_only=True, local_error="钩子测试只能在本机界面上操作"),
    "app.notify": _WsMethod(_h_app_notify, local_only=True),
    "app.apply_theme": _WsMethod(_h_app_apply_theme),
    "app.check_update": _WsMethod(_h_app_check_update),
    "app.install_update": _WsMethod(_h_app_install_update, local_only=True),
    "app.apply_update": _WsMethod(_h_app_apply_update, local_only=True),
    "app.restart": _WsMethod(_h_app_restart, local_only=True),
    "app.open_path": _WsMethod(_h_app_open_path),
    "app.open_external": _WsMethod(_h_app_open_external),
    "app.export_diagnostics": _WsMethod(_h_app_export_diagnostics, local_only=True),
    "lan.status": _WsMethod(_h_lan_status),
    "lan.enable": _WsMethod(_h_lan_enable, local_only=True),
    "lan.disable": _WsMethod(_h_lan_disable),
    "lan.rotate_token": _WsMethod(_h_lan_rotate_token, local_only=True),
    "lan.set_port": _WsMethod(_h_lan_set_port, local_only=True),
    "remote.status": _WsMethod(_h_remote_status),
    "remote.enable": _WsMethod(_h_remote_enable, local_only=True),
    "remote.disable": _WsMethod(_h_remote_disable),
    "demo.enable": _WsMethod(_h_demo_enable, local_only=True),
    "ollama.detect": _WsMethod(_h_ollama_detect),
    "ollama.enable": _WsMethod(_h_ollama_enable, local_only=True),
    "websearch.get": _WsMethod(_h_websearch_get),
    "websearch.save": _WsMethod(_h_websearch_save, local_only=True),
    "roundtable.get": _WsMethod(_h_roundtable_get),
    "roundtable.save": _WsMethod(_h_roundtable_save),
    "adversarial.get": _WsMethod(_h_adversarial_get),
    "adversarial.save": _WsMethod(_h_adversarial_save),
    # 团队：team.get 供团队卡与刷新；工单板只由用户改；交付（用户确认的正常
    # 终态）、收队（立即终止，含正在跑的成员轮）与接管（AI 总管 → 用户总管，
    # 二期）。聊天入口仍是 chat.send（team 标志；AI 总管经 director_mode/director）。
    # 非 local 调用的 session_id 一律收敛到该连接绑定的活动会话（_team_scoped_params，
    # 断言他人 sid 读写/收队他人团队在入口即拒）。三期另开三面：team.log 回放
    # 全量频道消息（读，一致性校验在 backend）；team.template_* 建队模板（读各
    # 端可看、写仅本机，与子代理定义同姿态）；teamcfg.get/save 设置页配置卡
    # （仿 roundtable.get/save）
    "team.get": _WsMethod(_h_team_get),
    "team.task_add": _WsMethod(_h_team_task_add),
    "team.task_update": _WsMethod(_h_team_task_update),
    "team.stop": _WsMethod(_h_team_stop),
    "team.deliver": _WsMethod(_h_team_deliver),
    "team.takeover": _WsMethod(_h_team_takeover),
    "team.log": _WsMethod(_h_team_log),
    "team.template_list": _WsMethod(_h_team_template_list),
    "team.template_save": _WsMethod(_h_team_template_save, local_only=True),
    "team.template_remove": _WsMethod(_h_team_template_remove, local_only=True),
    "teamcfg.get": _WsMethod(_h_teamcfg_get),
    "teamcfg.save": _WsMethod(_h_teamcfg_save),
    "imagegen.get": _WsMethod(_h_imagegen_get),
    "imagegen.save": _WsMethod(_h_imagegen_save, local_only=True),
    "speech.get": _WsMethod(_h_speech_get),
    "speech.save": _WsMethod(_h_speech_save, local_only=True),
    "speech.transcribe": _WsMethod(
        _h_speech_transcribe, local_only=True,
        local_error="语音输入只能在本机界面上使用",
    ),
    "settings.export": _WsMethod(_h_settings_export, local_only=True),
    "settings.import": _WsMethod(_h_settings_import, local_only=True),
    # 聊天软件渠道：写配置/启停属降低防护的操作，与开局域网同一姿态——仅本机可改。
    # 远端（局域网/ tailnet）能看状态，但不能改，否则一个被扫码链接挟持的手机
    # 就能把 Agent 接给任意聊天账号。
    "channel.status": _WsMethod(_h_channel_status),
    "channel.save": _WsMethod(
        _h_channel_save, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.enable": _WsMethod(
        _h_channel_enable, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.disable": _WsMethod(
        _h_channel_disable, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.set_timeout": _WsMethod(
        _h_channel_set_timeout, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    "channel.test": _WsMethod(
        _h_channel_test, local_only=True,
        local_error="聊天机器人渠道的配置只能在桌面端本机修改",
    ),
    # 微信扫码登录：生成二维码 / 轮询确认 / 退出登录。扫码得到的是能操控 Agent 的凭据，
    # 与启用渠道同一敏感度，因此同样只允许本机操作。
    "channel.weixin_login_start": _WsMethod(
        _h_channel_weixin_login_start, local_only=True,
        local_error="微信扫码登录只能在桌面端本机完成",
    ),
    "channel.weixin_login_poll": _WsMethod(
        _h_channel_weixin_login_poll, local_only=True,
        local_error="微信扫码登录只能在桌面端本机完成",
    ),
    "channel.weixin_logout": _WsMethod(
        _h_channel_weixin_logout, local_only=True,
        local_error="微信登录状态只能在桌面端本机修改",
    ),
    "config.add_provider_model": _WsMethod(_h_config_add_provider_model, local_only=True),
    "config.remove_provider_model": _WsMethod(_h_config_remove_provider_model, local_only=True),
    "config.providers": _WsMethod(_h_config_providers),
    "config.save_provider": _WsMethod(_h_config_save_provider, local_only=True),
    "config.add_provider": _WsMethod(_h_config_add_provider, local_only=True),
    "config.delete_provider": _WsMethod(_h_config_delete_provider, local_only=True),
    "config.restore_provider": _WsMethod(_h_config_restore_provider, local_only=True),
    "config.set_provider_enabled": _WsMethod(_h_config_set_provider_enabled, local_only=True),
    "config.probe_models": _WsMethod(_h_config_probe_models, local_only=True),
    "config.probe_context": _WsMethod(_h_config_probe_context, local_only=True),
    "subagent.get": _WsMethod(_h_subagent_get),
    "subagent.save": _WsMethod(_h_subagent_save, local_only=True),
    "subagent.save_builtin": _WsMethod(_h_subagent_save_builtin, local_only=True),
    "subagent.save_custom": _WsMethod(_h_subagent_save_custom, local_only=True),
    "subagent.delete_custom": _WsMethod(_h_subagent_delete_custom, local_only=True),
    "whitelist.remove": _WsMethod(_h_whitelist_remove),
    "whitelist.add": _WsMethod(
        _h_whitelist_add, local_only=True,
        local_error="白名单添加只能在本机界面上操作",
    ),
    "whitelist.clear": _WsMethod(_h_whitelist_clear),
    "whitelist.check": _WsMethod(_h_whitelist_check),
    "whitelist.export": _WsMethod(_h_whitelist_export),
    "whitelist.enable": _WsMethod(_h_whitelist_enable, local_gate=_gate_whitelist_enable),
    "whitelist.import": _WsMethod(
        _h_whitelist_import, local_only=True,
        local_error="白名单导入只能在本机界面上操作",
    ),
    "skills.toggle": _WsMethod(_h_skills_toggle, local_only=True),
    "skills.scope": _WsMethod(_h_skills_scope, local_only=True),
    "skills.body": _WsMethod(_h_skills_body),
    "skills.scan_local": _WsMethod(_h_skills_scan_local),
    "skills.install": _WsMethod(_h_skills_install, local_only=True),
    "skills.delete": _WsMethod(_h_skills_delete, local_only=True),
    "skills.gallery": _WsMethod(_h_skills_gallery),
    "skills.save_from_session": _WsMethod(_h_skills_save_from_session),
    "skills.save_draft": _WsMethod(_h_skills_save_draft, local_only=True),
    # Mods 扩展（实验性）：写方法全部仅本机（第三方代码 + 收紧权限门的能力面）
    "mods.list": _WsMethod(_h_mods_list),
    "mods.get": _WsMethod(_h_mods_get),
    "mods.get_template": _WsMethod(_h_mods_get_template),
    "mods.official": _WsMethod(_h_mods_official),
    "mods.install": _WsMethod(_h_mods_install, local_only=True),
    "mods.confirm_install": _WsMethod(_h_mods_confirm_install, local_only=True),
    "mods.install_official": _WsMethod(_h_mods_install_official, local_only=True),
    "mods.delete": _WsMethod(_h_mods_delete, local_only=True),
    "mods.toggle": _WsMethod(_h_mods_toggle, local_only=True),
    "mods.set_enabled": _WsMethod(_h_mods_set_enabled, local_only=True),
    "mods.save_draft": _WsMethod(_h_mods_save_draft, local_only=True),
    "mods.test": _WsMethod(
        _h_mods_test, local_only=True, local_error="Mod 测试器只能在本机界面上操作",
    ),
    "mcp.import": _WsMethod(_h_mcp_import, local_only=True),
    "mcp.save_server": _WsMethod(_h_mcp_save_server, local_only=True),
    "mcp.delete": _WsMethod(_h_mcp_delete, local_only=True),
    "mcp.add_preset": _WsMethod(_h_mcp_add_preset, local_only=True),
    "mcp.set_enabled": _WsMethod(_h_mcp_set_enabled, local_only=True),
    "mcp.reconnect": _WsMethod(_h_mcp_reconnect, local_only=True),
    "mcp.status": _WsMethod(_h_mcp_status),
    "tools.list": _WsMethod(_h_tools_list),
    "whitelist.list": _WsMethod(_h_whitelist_list),
}


# ---- 远程客户端（局域网 / Tailscale，持有令牌）不可调用的方法 ----
# 安全审查 2026-09 根因一：dispatch 里只有零星几个方法有本机门禁，其余几十个
# 写接口只靠 WS Token 一层防护。这里集中收口：凡是「改配置、写凭据、启动
# 本机程序、导出含密数据、切换全局安全姿态」的 RPC 一律本机专属。
# 收紧类动作（trust.revoke / lan.disable / whitelist.remove 等）不在此表，
# 远端仍可调用；聊天、会话、文件面板、任务/用量查看等常规遥控功能不受影响。
# （原手写表已并入上方 _WS_METHODS：local_only / local_gate 即守卫，本集合
# 为派生兼容视图，供既有测试按名导入。）
LOCAL_ONLY_METHODS = frozenset(m for m, e in _WS_METHODS.items() if e.local_only)


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
        backend.start_daily_report_loop()
        backend.start_retention_loop()
        import logging

        logging.getLogger("skysheep").info(
            "服务就绪（自进程启动 %.2fs）", time.perf_counter() - PROCESS_START
        )
        yield
        backend.stop_cron_loop()
        backend.stop_pipeline_loop()
        backend.stop_reminder_loop()
        backend.stop_memory_maintenance_loop()
        backend.stop_daily_report_loop()
        backend.stop_retention_loop()
        # crash.flag 的清除在 backend.shutdown() 开头做：收尾链最后一环最容易
        # 被截断（桌面壳 1.5s 上限、程序内更新的 os._exit），清在末尾等于没清
        await backend.shutdown()

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
        "<p style='opacity:.6'>令牌在桌面端 设置 · 手机控制 里查看</p>"
        "<p style='opacity:.6'>从主屏幕图标（独立窗口）打开时没有地址栏："
        "请先用手机浏览器打开带 token 的地址完成验证，再回到本应用。</p>"
        "<p style='opacity:.6'>若打开一直失败，确认电脑端 SkySheep 正在运行"
        "（离线时浏览器只会显示打不开，与令牌无关）。</p></div></body></html>"
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
        needs_token = origin != "local" and (lan or (ts and origin == "tailscale"))
        if not needs_token:
            return await call_next(request)
        # fail closed：远程来源需要令牌而令牌为空（配置被手改坏等）时直接拒绝，
        # 不能因为「没令牌可比对」就退化成无鉴权放行。连续试错的来源由
        # token_throttle 即时拒绝（见 backend.TokenThrottle），失败都留痕给设置页。
        ip = str(request.client[0]) if request.client else ""
        if backend.token_throttle.blocked(ip):
            backend.note_token_failure(ip, "http")
            return HTMLResponse(FORBIDDEN_HTML, status_code=403)
        token = (cfg.server.token if cfg is not None else "") or ""
        if not token:
            backend.note_token_failure(ip, "http")
            return HTMLResponse(FORBIDDEN_HTML, status_code=403)
        supplied = (
            request.query_params.get("token")
            or request.cookies.get("skysheep_token")
            or request.headers.get("x-skysheep-token")
            or ""
        )
        if supplied and hmac.compare_digest(supplied, token):
            backend.token_throttle.note_success(ip)
            response = await call_next(request)
            if request.query_params.get("token"):
                # 首次带 token 访问成功后种 cookie，静态资源等后续请求免拼参数
                response.set_cookie(
                    "skysheep_token", token, max_age=30 * 86400, httponly=True, samesite="lax"
                )
            return response
        backend.token_throttle.note_failure(ip)
        backend.note_token_failure(ip, "http")
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

    # ---- PWA：手机端「添加到主屏幕」（局域网 / Tailscale 访问） ----
    # 两个文件本体都在 static/ 下，但必须从站点根提供：
    #   · /manifest.webmanifest —— start_url "./" 相对 manifest URL 解析，
    #     挂在 /static/ 下会解析到 /static/（那不是应用页面），PWA scope 就废了；
    #   · /sw.js —— 脚本在站点根时默认 scope 是 /，罩得住主页面；
    #     挂在 /static/ 下 scope 只有 /static/，主页面不受控，SW 等于白装。
    # Content-Type 显式指定，不依赖 mimetypes 对这两类扩展名的机器差异。
    @app.get("/manifest.webmanifest")
    async def pwa_manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        # Service-Worker-Allowed：脚本已在根路径、默认 scope 即 /，此头只是
        # 把约定写明，防未来有人把脚本挪回子路径后 scope 静默收窄
        return FileResponse(
            STATIC_DIR / "sw.js",
            headers={"Service-Worker-Allowed": "/"},
            media_type="text/javascript",
        )

    # 静态资源禁缓存：前端零构建、文件名无指纹，否则用户永远卡在旧 app.js
    _no_store_static(app)
    _host_guard(app)

    # 点开头路径先挡在挂载之外：StaticFiles 不拒隐藏路径，见 _is_hidden_static_path。
    @app.middleware("http")
    async def _static_hidden_guard(request, call_next):
        if request.url.path.startswith("/static") and _is_hidden_static_path(
            request.url.path
        ):
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return await call_next(request)

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
        entry = _WS_METHODS.get(method)
        if entry is None:
            raise RuntimeError("unknown method: " + method)   # 与原链尾逐字一致，行为不变
        if not local:
            # 安全审查根因一：配置/管理类 RPC 集中收口，与 permission.set_mode /
            # trust.grant 同一姿态——手机遥控可以聊天，但不能改引擎的安全配置。
            if entry.local_only:
                raise RuntimeError(entry.local_error)
            if entry.local_gate is not None:
                denied = entry.local_gate(params)
                if denied:
                    raise RuntimeError(denied)
        return await entry.handler(backend, params, emit, local)

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
        # 安全审查 M2：对端是本机时 Host 必须指向本机。DNS rebinding 页面下
        # Origin 与 Host 同时写成攻击者域名，上面那条一致性检查拦不住，
        # 而按对端 IP 判定又是 local → 免令牌且可调本机专属方法。
        if not _request_host_allowed(ws.headers.get("host", ""), ws.client):
            await ws.accept()
            await ws.close(code=4403)
            return
        # 本机（回环）永远免令牌，与 HTTP 守卫同一规则；tailnet 来源必须验令牌；
        # 远程来源需要令牌而令牌为空时同样 fail closed（不能退化成无鉴权放行）
        needs_token = origin != "local" and (lan or (ts and origin == "tailscale"))
        if needs_token:
            ip = str(ws.client[0]) if ws.client else ""
            token = (cfg.server.token if cfg is not None else "") or ""
            supplied = ""
            if token and not backend.token_throttle.blocked(ip):
                supplied = (
                    ws.query_params.get("token")
                    or ws.cookies.get("skysheep_token")
                    or ws.headers.get("x-skysheep-token")
                    or ""
                )
            if not token or not supplied or not hmac.compare_digest(supplied, token):
                backend.token_throttle.note_failure(ip)
                backend.note_token_failure(ip, "ws")
                await ws.accept()
                await ws.close(code=4401)
                return
            backend.token_throttle.note_success(ip)
        await ws.accept()
        lock = asyncio.Lock()
        # 客户端来源决定部分方法是否可用（远端不允许切换降低防护的开关）
        client_is_local = _client_is_local(ws.client)
        # 该连接正在交互的会话（activate/resume/chat.send 时更新）：远程连接的
        # 子代理直播 / 任务终态事件只推给它正在看的会话（安全审查 B15），
        # 本机前端不过滤（多标签由前端自行路由）。
        conn_state = {"session": None}
        remote_permissions: dict[str, str] = {}

        async def send(obj: dict) -> None:
            async with lock:
                await ws.send_text(json.dumps(obj, ensure_ascii=False, default=str))

        async def emit(ev: dict) -> None:
            if not client_is_local:
                k = ev.get("kind")
                if ev.get("session_id"):
                    if conn_state["session"] != ev["session_id"]:
                        return  # 所有会话事件（含用户消息/工具/权限）按连接归属过滤
                elif k in ("subagent_spawned", "subagent_event", "task_finished"):
                    return  # 缺少归属的子代理事件不能向远端推送
                elif k in ("term_data", "term_exit"):
                    # 终端输出只属于本机终端面板：spawn/input 仅本机（app.py:664），
                    # 输出里可能含用户亲手回显的环境变量等敏感值，远端一律不推送
                    # （审查 P1-1）。
                    return
            if not client_is_local:
                rid = str(ev.get("request_id") or "")
                if ev.get("kind") == "permission_request" and rid:
                    remote_permissions[rid] = str(ev.get("session_id") or "")
                elif ev.get("kind") == "permission_resolved":
                    remote_permissions.pop(rid, None)
            await send({"event": ev.get("kind", ""), "data": ev})

        backend.ws_emitters.append(emit)
        try:
            while True:
                # 用通用 receive 判别帧类型：receive_text 对二进制帧取 message["text"]
                # 会抛 KeyError 直接断连——客户端只见连接消失，无从排查。
                message = await ws.receive()
                if message["type"] != "websocket.receive":
                    break  # disconnect 等控制消息：退出循环，finally 摘除 emitter
                if message.get("bytes") is not None:
                    # 二进制帧不属于本协议：回一条可读错误帧而不是无声断连
                    await send({"ok": False,
                                "error": "不支持二进制帧：请发送 JSON 文本帧"})
                    continue
                raw = message.get("text") or ""
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    # 畸形文本帧：此前无声丢弃，客户端对着黑洞等回复；给个回执
                    await send({"ok": False, "error": "请求不是有效的 JSON 文本"})
                    continue
                if not isinstance(msg, dict):
                    # 合法 JSON 但不是对象（裸字符串/数字/数组）：同样给回执
                    await send({"ok": False, "error": "请求必须是 JSON 对象"})
                    continue
                mid = msg.get("id")
                method = str(msg.get("method", ""))
                params = msg.get("params") or {}

                async def process(mid=mid, method=method, params=params):
                    request_bound = False

                    async def request_emit(ev: dict) -> None:
                        nonlocal request_bound
                        # 只由本请求实际开始运行时发出的会话事件绑定连接，
                        # 广播与被拒绝的请求均不能改变订阅。预估先于 turn_started，
                        # 也要接收；已绑定连接发送到另一个会话时同样更新一次。
                        if (not client_is_local and method == "chat.send"
                                and not request_bound and ev.get("session_id")):
                            conn_state["session"] = ev["session_id"]
                            request_bound = True
                        await emit(ev)

                    try:
                        if not isinstance(params, dict):
                            raise RuntimeError("params 必须是 JSON 对象")
                        if method == "permission.respond" and not client_is_local:
                            rid = str(params.get("request_id") or "")
                            # 远程只能回答本连接实际收到、且属于本连接会话的确认。
                            # 不能把「未登记」的 request_id 与未绑定会话的 None
                            # 误判成相等（空映射 + 空会话曾形成旁路）。
                            if (
                                not rid
                                or rid not in remote_permissions
                                or remote_permissions[rid] != conn_state["session"]
                            ):
                                await send({"id": mid, "ok": True, "result": {"delivered": False}})
                                return
                        if method == "chat.send" and params.get("session_id"):
                            sid = str(params["session_id"])
                            await backend._get_owned_session(sid, local=client_is_local)
                        # stop 的 session_id 不能由远程调用方断言：只接受该连接已经
                        # 通过 turn_started / session 操作绑定的会话。未绑定时注入空串，
                        # 由 _h_stop 返回 cancelled=false，绝不回退到 backend.session。
                        dispatch_params = params
                        if method == "stop" and not client_is_local:
                            dispatch_params = {
                                **params,
                                "session_id": conn_state["session"] or "",
                            }
                        # 后端需要 request_emit 才能在首个会话事件时绑定远程
                        # 会话，但广播排除发送方时要识别它实际对应的连接 emitter。
                        # 给包装函数挂稳定身份，避免把发送方自己的 user_message
                        # 再广播回来（request_emit 与 emit 本来不是同一个对象）。
                        request_emit._source_emit = emit
                        result = await dispatch(
                            method, dispatch_params, request_emit, local=client_is_local
                        )
                        # 跟踪连接当前交互的会话（B15 的事件隔离用）
                        if method == "session.delete":
                            # 删除后后端已自动切走：跟随 new_active（可能为 None）。
                            # 删除的返回里没有 id 键，照下面的通用分支取会让跟踪
                            # 永远停在已删会话上，之后收不到子代理等定向事件
                            conn_state["session"] = (result or {}).get("new_active")
                        elif method in ("session.new", "session.activate", "session.resume",
                                        "session.fork", "session.truncate"):
                            sid = (result or {}).get("id")
                            if isinstance(sid, str) and sid:
                                conn_state["session"] = sid
                        elif method == "project.switch":
                            sess = ((result or {}).get("session") or {})
                            conn_state["session"] = sess.get("id") if isinstance(sess, dict) else None
                        await send({"id": mid, "ok": True, "result": result})
                    except Exception as e:
                        await send({"id": mid, "ok": False, "error": str(e)})

                spawn_bg(process())
        except WebSocketDisconnect:
            return
        finally:
            try:
                backend.ws_emitters.remove(emit)
            except ValueError:
                pass

    return app
