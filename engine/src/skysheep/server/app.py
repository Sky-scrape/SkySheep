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
import ipaddress
import json
import logging
import mimetypes
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..bgtasks import spawn_bg
from ._dispatch_automation import METHODS as _M_automation
from ._dispatch_chat import METHODS as _M_chat
from ._dispatch_collab import METHODS as _M_collab

# 拆分兼容面（P28）：既有测试按 `from skysheep.server.app import _h_*` 直引
# handler（tests/test_team.py），拆分后经这里自域模块转发回来，拆分前的
# import 表面完全保留。新代码请勿直引 handler，改查 _WS_METHODS。
from ._dispatch_collab import (  # noqa: F401
    _h_team_get,
    _h_team_log,
    _h_team_stop,
    _h_team_task_add,
)
from ._dispatch_common import _CONN_SESSION, _WsMethod
from ._dispatch_ext import METHODS as _M_ext
from ._dispatch_files import METHODS as _M_files
from ._dispatch_ops import METHODS as _M_ops
from ._dispatch_prefs import METHODS as _M_prefs
from ._dispatch_session import METHODS as _M_session
from .backend import THEME_PREFS, ServerBackend, client_origin

logger = logging.getLogger("skysheep.security")


def _client_is_local(client) -> bool:
    """WebSocket 客户端是否来自本机。

    用于把「自动允许写入」这类降低防护的开关锁在本机：局域网 / 远程访问模式下
    服务绑定 0.0.0.0，任何拿到令牌的设备都能连 WS，这类开关不能由远端切换。
    来源分类（回环 / tailnet 网段 / 其它）统一走 client_origin，与 HTTP 守卫一致。
    """
    return client_origin(client) == "local"



# 无会话归属（引擎级）事件里明确允许下发远端的一组（安全审查 M05：白名单）。
# 旧实现对这类事件是黑名单——只拦子代理/工厂/终端三组，新引擎级事件默认
# 直达远端，cron_updated（携带任务 prompt、预授权工具名单与运行结果）、
# pipeline_updated（其他项目流水线全文）、map_updated（项目记忆生成状态）
# 这类整行广播曾借此送达任何持令牌设备。改为默认拒绝后：
# - 新增引擎级事件想给远端看，必须来这里显式登记（新事件默认不出门）；
# - 会话事件（带 session_id）不受本表影响，仍按连接归属过滤；
# - 子代理直播 / 工厂运行 / 终端输出 / 上述三类整行广播等继续拦在门外。
_REMOTE_ANON_KINDS = frozenset({
    # chat.aux 的请求内流式（辅助对话面板远端可用，delta 链路不能断）
    "aux_delta",
    "aux_thinking",
    # 无载荷刷新信号：远端界面的 MCP / Mods / 日程列表与桌面保持一致
    "mcp_updated",
    "mods_updated",
    "schedule_updated",
    # 日程提醒推送（title/notes，用户写给自己的跨设备提醒；
    # 不含 prompt / 预授权名单 / 运行结果这类项目数据）
    "schedule_reminder",
})


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
        # app.js 与全部拆分模块（清单 = index.html 的 script 标签，顺序同载入序）：
        # 模块间靠全局函数互调、必须同版配套，漏登记的模块会被 WebView 按启发式
        # 缓存供旧副本，与新 backend / 新 app.js 混跑（审计 M18/L29）；清单漂移
        # 由 tests/test_frontend_wiring.py 的三清单对齐断言机械守住
        "/static/app.js",
        "/static/app-teamroom.js",  # 团队室页签模块（index.html 加载清单强制对齐）
        "/static/app-projects.js",
        "/static/app-schedule.js",
        "/static/app-providers.js",
        "/static/app-whitelist.js",
        "/static/app-skills.js",
        "/static/app-memmap.js",
        "/static/app-advanced.js",
        "/static/app-relay.js",
        "/static/app-about.js",
        "/static/app-archive.js",
        "/static/app-browser.js",
        "/static/app-mods.js",
        "/static/app-factory.js",
        "/static/app-factory-scene.js",
        "/static/app-factory-drawer.js",
        "/static/app-factory-draw.js",
        "/static/app-tools.js",
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


# index.html 内容极小、几乎不变，但每次 GET / 都在事件循环里同步读盘（审查 B5）。
# 按 (mtime_ns, size) 缓存：文件没动就直接用缓存，开发时改了也能立刻生效。
# 单条目缓存即可——本函数只服务 index.html。
_index_html_cache: tuple[tuple[int, int], str] | None = None


def _read_index_html() -> str:
    global _index_html_cache
    path = STATIC_DIR / "index.html"
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    if _index_html_cache is not None and _index_html_cache[0] == key:
        return _index_html_cache[1]
    text = path.read_text(encoding="utf-8")
    _index_html_cache = (key, text)
    return text

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
# 全部 WS 方法的分支体按域拆在同目录 _dispatch_*.py（P28 拆分，分支体逐字
# 迁入，行为零变化）；此处按域合并出总分发表。local_only / local_gate 的
# 判定与拒绝文案由 dispatch 入口统一执行，语义与拆分前逐字一致。

_WS_METHODS: dict[str, _WsMethod] = {
    **_M_chat,
    **_M_session,
    **_M_files,
    **_M_automation,
    **_M_prefs,
    **_M_ops,
    **_M_collab,
    **_M_ext,
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
        backend.start_archive_loop()
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
        backend.stop_archive_loop()
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
            backend.note_auth_success(ip, "http")
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
        page = _read_index_html()
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
        client_ip = str(ws.client[0]) if ws.client else ""
        if needs_token:
            ip = client_ip
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
            backend.note_auth_success(ip, "ws")
        await ws.accept()
        lock = asyncio.Lock()
        # 客户端来源决定部分方法是否可用（远端不允许切换降低防护的开关）
        client_is_local = _client_is_local(ws.client)
        # 该连接正在交互的会话（activate/resume/chat.send 时更新）：远程连接的
        # 子代理直播 / 任务终态事件只推给它正在看的会话（安全审查 B15），
        # 本机前端不过滤（多标签由前端自行路由）。
        conn_state = {"session": None}
        remote_permissions: dict[str, str] = {}
        # 本连接握手时使用的令牌世代：令牌轮换后旧世代连接会被主动关闭
        # （见 backend.token_generation / _revoke_stale_ws）。
        conn_gen = backend.token_generation

        async def send(obj: dict) -> None:
            async with lock:
                try:
                    await ws.send_text(json.dumps(obj, ensure_ascii=False, default=str))
                except (RuntimeError, WebSocketDisconnect):
                    # 连接已被关闭（如令牌轮换后的 kick 与并发请求赛跑）：
                    # 不能再发帧，但也不能把异常冒泡成后台任务异常。
                    pass

        async def emit(ev: dict) -> None:
            if not client_is_local:
                k = ev.get("kind")
                if ev.get("session_id"):
                    if conn_state["session"] != ev["session_id"]:
                        return  # 所有会话事件（含用户消息/工具/权限）按连接归属过滤
                elif k not in _REMOTE_ANON_KINDS:
                    # 无会话归属的引擎级事件默认不下发远端（M05 白名单）：
                    # 缺少归属的子代理事件（B15）、工厂运行事件（六方法全本机）、
                    # 终端输出（P1-1：可能含用户回显的环境变量等敏感值），以及
                    # cron_updated/pipeline_updated/map_updated 等携带其他项目
                    # prompt/预授权名单/结果的整行广播，一并拦在门外。
                    return
            if not client_is_local:
                rid = str(ev.get("request_id") or "")
                if ev.get("kind") == "permission_request" and rid:
                    remote_permissions[rid] = str(ev.get("session_id") or "")
                elif ev.get("kind") == "permission_resolved":
                    remote_permissions.pop(rid, None)
            await send({"event": ev.get("kind", ""), "data": ev})

        backend.ws_emitters.append(emit)
        # 连接登记（与 emitters 平行）：供设置页「已连接设备」与 lan.kick 使用。
        # kick 回调只作用于本连接：close(4409) 后客户端会按重连策略重试，
        # 但重连要重新验令牌（轮换过的旧令牌自然进不来）。
        _conn_id = f"c{len(backend.ws_connections) + 1}-{int(time.time())}"

        async def _kick() -> None:
            try:
                await ws.close(code=4409)
            except Exception:  # noqa: BLE001
                pass

        _conn_entry = {
            "id": _conn_id,
            "emit": emit,
            "ip": client_ip,
            "origin": origin,
            "connected_at": time.time(),
            "ua": str(ws.headers.get("user-agent", ""))[:200],
            "kick": _kick,
            "local": bool(client_is_local),
            "token_generation": conn_gen,
        }
        backend.ws_connections.append(_conn_entry)
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
                    # 令牌已轮换：本连接基于旧令牌建立，不再接受任何 RPC。
                    # 仅靠 kick 的主动关闭不够稳（关闭是异步的，可能落在本次
                    # 请求之后），在分发前再拦一道。
                    if not client_is_local and conn_gen != backend.token_generation:
                        await send({"id": mid, "ok": False,
                                    "error": "访问令牌已轮换，请用新地址重新连接"})
                        return
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
                        # 把本连接当前绑定的会话作为请求级作用域：远程查询
                        # （runs/tasks/status/permission.queue）以它为授权依据，
                        # 不读全局 backend.session（会被其他连接/本机窗口切走）。
                        _scope_token = _CONN_SESSION.set(
                            None if client_is_local else (conn_state["session"] or "")
                        )
                        try:
                            result = await dispatch(
                                method, dispatch_params, request_emit, local=client_is_local
                            )
                        finally:
                            _CONN_SESSION.reset(_scope_token)
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
                        elif (method == "permission.queue" and not client_is_local
                              and isinstance(result, dict)):
                            _register_queue_permissions(remote_permissions, result)
                        await send({"id": mid, "ok": True, "result": result})
                    except Exception as e:  # noqa: BLE001
                        await send({"id": mid, "ok": False, "error": str(e)})

                spawn_bg(process())
        except WebSocketDisconnect:
            return
        finally:
            try:
                backend.ws_emitters.remove(emit)
            except ValueError:
                pass
            try:
                backend.ws_connections.remove(_conn_entry)
            except ValueError:
                pass

    return app


def _register_queue_permissions(remote_permissions: dict[str, str], result: dict) -> None:
    """审查 L08：把 permission.queue 命中的未决请求登记进连接的 remote_permissions。

    断线重连/刷新后确认卡经 queue 重建，但 respond 只认本连接实际收到过
    permission_request 事件的 rid——不登记的话卡片永远答不动（fail-closed
    方向的可用性洞，与 queue 的设计目的直接矛盾）。queue 的远程口径已按
    B 族收敛到绑定会话，这里登记的 rid 天然属于本连接会话。
    """
    for row in (result or {}).get("pending") or []:
        if isinstance(row, dict) and row.get("request_id"):
            remote_permissions[str(row["request_id"])] = str(row.get("session_id") or "")
