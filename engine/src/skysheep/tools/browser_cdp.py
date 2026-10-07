"""web_page 工具：CDP 驱动本机 Edge 无头实例做页内自动化。

与 browser 工具（把网页开在用户自己的浏览器里给用户看）互补：web_page 在引擎
自己拉起的临时无头 Edge 里执行页内动作（navigate / click / fill / press /
extract / screenshot / close），Agent 能拿到页面文本与截图，用户看不到这个
浏览器实例；一次性临时配置目录，Cookie / 登录态与用户真实浏览器互不相通。

机制：
- 启动 ``msedge --headless=new --remote-debugging-port=0 --user-data-dir=<临时目录>``
  （msedge 找 PATH，再退到 Program Files 固定位置），从 stderr 解析
  「DevTools listening on ws://…」拿调试地址；进程与临时目录在 close 动作或
  解释器退出时清理。单例：多个动作共用同一实例——启动路径持 asyncio.Lock
  串行化（检查实例与赋值单例之间隔着秒级挂起的 start()，并行会话共享同一
  事件循环，不加锁会双启动 Edge、后完成者覆盖单例导致进程与临时目录泄漏）；
- 最小 CDP 客户端：websockets 连浏览器级调试端点，按自增 id 发 JSON-RPC 命令、
  收同 id 响应（事件帧与迟到响应跳过）；Target.attachToTarget(flatten) 挂到
  页面 target 后用 sessionId 路由页面级命令；
- 安全模型与 browser / mouse 同档：DANGEROUS 逐次确认，arg_text 首词是 action，
  「总是允许」按动作前缀沉淀（gate._ACTION_PREFIX_TOOLS）；fill 例外——要
  填入的文本是对页面的任意写入面，「总是允许」只固化当次的选择器与内容
  （gate._EXACT_ACTION_TOOLS，与 keyboard 同一先例），arg_text 里内容可见。

SSRF 防线（与 web_fetch 同级）：页面内容会回给模型、页面开在用户看不见的
无头实例里——与 web_fetch 同属「引擎侧拉取」，不能沿用 browser 工具「开在
用户自己浏览器里」的免 SSRF 取舍。navigate 前校验目标主机必须解析到公网；
导航完成后复核最终地址（重定向目标用户在确认卡上看不到），落在非公网即
弹回空白页并拒绝返回——AGENTS.md 的「仅公网、逐跳校验」是本项目联网能力
的底线，本工具是它的延伸而非例外。
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import websockets
from pydantic import BaseModel, Field
from websockets.exceptions import ConnectionClosed, WebSocketException

from ..config import skysheep_home
from ..messages import ImageBlock
from .base import Safety, Tool, ToolContext, ToolError, truncate_output
from .computer import _prune_screenshots
from .web import _resolve_public_ips

# ---- 定位 msedge ----

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_DEVTOOLS_RE = re.compile(r"DevTools listening on (ws://\S+)")

# ---- 时序参数（测试可 monkeypatch 收紧） ----

_START_TIMEOUT = 20.0        # 等 stderr 出现调试地址的上限
_CDP_TIMEOUT = 20.0          # 单条 CDP 命令等响应的上限
_PAGE_READY_TIMEOUT = 15.0   # navigate 后等 document.readyState=complete 的上限
_POLL_INTERVAL = 0.15        # readyState 轮询间隔
_WS_MAX_SIZE = 64 * 1024 * 1024  # 截图 base64 可能远超 websockets 默认 1MB


def find_edge() -> str:
    """定位 msedge.exe：先 PATH，再 Program Files 固定位置；找不到给可读报错。"""
    exe = shutil.which("msedge")
    if exe:
        return exe
    for cand in EDGE_CANDIDATES:
        try:
            if Path(cand).is_file():
                return cand
        except OSError:
            continue
    raise ToolError(
        "找不到 Microsoft Edge（msedge.exe）。已尝试 PATH 与以下位置：\n"
        + "\n".join(EDGE_CANDIDATES)
        + "\n请确认本机已安装 Microsoft Edge 后重试。"
    )


# ---- 最小 CDP 客户端 ----


class CdpConnection:
    """单条 WebSocket 上的 CDP JSON-RPC 客户端：按自增 id 发命令、收同 id 响应。

    事件帧（无 id）与此前超时命令的迟到响应一律跳过；错误响应抛 ToolError；
    非 JSON / 非 JSON 对象的帧按「无法解析」报错。
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        timeout: float = _CDP_TIMEOUT,
    ) -> dict[str, Any]:
        self._next_id += 1
        mid = self._next_id
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send(json.dumps(msg, ensure_ascii=False))
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            remain = end - loop.time()
            if remain <= 0:
                raise ToolError(f"等待 CDP 响应超时：{method}（>{timeout:.0f} 秒）。")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remain)
            except TimeoutError:
                raise ToolError(f"等待 CDP 响应超时：{method}（>{timeout:.0f} 秒）。") from None
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                raise ToolError(
                    f"CDP 返回了无法解析的帧（非 JSON）：{str(raw)[:80]}"
                ) from None
            if not isinstance(data, dict):
                raise ToolError(
                    f"CDP 返回了无法解析的帧（不是 JSON 对象）：{str(raw)[:80]}"
                )
            if data.get("id") != mid:
                continue  # 事件帧 / 其他命令的迟到响应
            if "error" in data:
                err = data.get("error")
                detail = (
                    str(err.get("message", err))[:200]
                    if isinstance(err, dict)
                    else str(err)[:200]
                )
                raise ToolError(f"CDP 命令执行失败（{method}）：{detail}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}


# ---- 无头浏览器单例 ----


async def _spawn(cmd: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


class CdpBrowser:
    """本机 Edge 无头实例的包装：启动、解析调试地址、CDP 连接、页面 target、清理。"""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.ws_url: str = ""
        self.profile_dir: str = ""
        self._conn: CdpConnection | None = None
        self._target_id: str = ""  # 常驻 tab：动作之间复用

    # -- 生命周期 --

    def is_alive(self) -> bool:
        # asyncio 子进程没有 poll()：returncode 为 None 即仍在运行
        return self.proc is not None and self.proc.returncode is None and bool(self.ws_url)

    async def start(self) -> None:
        exe = find_edge()
        self.profile_dir = tempfile.mkdtemp(prefix="skysheep-edge-")
        cmd = [
            exe,
            "--headless=new",
            "--remote-debugging-port=0",
            "--user-data-dir=" + self.profile_dir,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--window-size=1280,900",
            "about:blank",
        ]
        try:
            self.proc = await _spawn(cmd)
        except OSError as e:
            self._cleanup_profile()
            raise ToolError(f"无法启动无头浏览器（{exe}）：{e}") from None
        try:
            self.ws_url = await self._read_devtools_url()
        except ToolError:
            await self.aclose()
            raise

    async def _read_devtools_url(self) -> str:
        assert self.proc is not None and self.proc.stderr is not None
        loop = asyncio.get_running_loop()
        end = loop.time() + _START_TIMEOUT
        seen: list[str] = []
        while True:
            remain = end - loop.time()
            if remain <= 0:
                self._terminate_now()
                raise ToolError(
                    f"启动无头浏览器超时（{_START_TIMEOUT:.0f} 秒内未在输出中"
                    "找到 DevTools 调试地址）。"
                )
            try:
                line = await asyncio.wait_for(self.proc.stderr.readline(), timeout=remain)
            except TimeoutError:
                continue
            if not line:  # EOF：进程退出且没打印调试地址
                self._terminate_now()
                tail = "".join(seen).strip()[-400:]
                hint = f"，输出片段：{tail}" if tail else ""
                raise ToolError(
                    f"无头浏览器启动失败（进程已退出，退出码 {self.proc.returncode}{hint}）。"
                )
            text = line.decode("utf-8", "replace")
            seen.append(text)
            m = _DEVTOOLS_RE.search(text)
            if m:
                return m.group(1)

    async def aclose(self) -> None:
        """收掉 WebSocket、进程与临时配置目录（close 动作 / 单例换血时走这里）。"""
        if self._conn is not None:
            try:
                await asyncio.wait_for(self._conn._ws.close(), timeout=3)
            except Exception:  # noqa: BLE001 - 清理路径，任何失败都不拦着进程收尾
                pass
            self._conn = None
        await self._terminate()
        self._cleanup_profile()

    async def _terminate(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass

    def _terminate_now(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    def close_sync(self) -> None:
        """解释器退出（atexit）路径：不能 await，能 terminate 多少算多少。"""
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        self._cleanup_profile()

    def _cleanup_profile(self) -> None:
        if self.profile_dir:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir = ""

    # -- 连接与页面 target --

    def drop_connection(self) -> None:
        """连接疑似断了：丢弃，下次 open_page 重连（进程死了则 get_browser 重启实例）。"""
        self._conn = None

    async def _connection(self) -> CdpConnection:
        if self._conn is None:
            try:
                ws = await websockets.connect(
                    self.ws_url, max_size=_WS_MAX_SIZE, open_timeout=10
                )
            except (TimeoutError, OSError, WebSocketException) as e:
                raise ToolError(f"连接无头浏览器调试端口失败：{e}") from None
            self._conn = CdpConnection(ws)
        return self._conn

    async def open_page(self) -> tuple[CdpConnection, str]:
        """attach 到常驻 tab（没有就建），返回 (连接, sessionId) 供本页动作使用。"""
        conn = await self._connection()
        if self._target_id:
            try:
                res = await conn.send("Target.getTargets")
            except ToolError:
                res = {}
            infos = res.get("targetInfos") or []
            if not any(t.get("targetId") == self._target_id for t in infos):
                self._target_id = ""  # tab 已被关掉，重建
        if not self._target_id:
            res = await conn.send("Target.createTarget", {"url": "about:blank"})
            tid = str(res.get("targetId") or "")
            if not tid:
                raise ToolError(
                    "无法在无头浏览器中创建页面标签（Target.createTarget 未返回 targetId）。"
                )
            self._target_id = tid
        res = await conn.send(
            "Target.attachToTarget", {"targetId": self._target_id, "flatten": True}
        )
        sid = str(res.get("sessionId") or "")
        if not sid:
            raise ToolError("无法附加到页面（Target.attachToTarget 未返回 sessionId）。")
        return conn, sid

    async def detach(self, session_id: str) -> None:
        """用完的 CDP 会话随手注销——收尾动作，任何失败都不影响主流程。"""
        if not session_id or self._conn is None:
            return
        try:
            await self._conn.send("Target.detachTarget", {"sessionId": session_id}, timeout=5)
        except Exception:  # noqa: BLE001 - finally 路径，不能吞掉业务异常
            pass


_BROWSER: CdpBrowser | None = None
_BROWSER_LOCK = asyncio.Lock()  # 串行化「检查 + start + 赋值」的启动路径
_ATEXIT_REGISTERED = False


def _atexit_close() -> None:
    global _BROWSER
    if _BROWSER is not None:
        _BROWSER.close_sync()
        _BROWSER = None


async def get_browser() -> CdpBrowser:
    """单例入口：实例活着就复用；死了 / 没有就重新拉起。

    启动路径持锁：检查实例与赋值单例之间隔着 ``await b.start()``（等 stderr
    出现 DevTools 地址，秒级挂起）。web_page 虽是 DANGEROUS 不进单轮并发批，
    但并行会话 / 定时任务共享同一事件循环，两个协程可以同时走到 start()——
    不互斥会双启动 Edge，后完成者覆盖单例，先完成者实例（msedge 进程 +
    skysheep-edge-* 临时配置目录）两条清理路径都不可达，永久泄漏。锁只在
    启动路径持有，动作执行不经过它。
    """
    global _BROWSER, _ATEXIT_REGISTERED
    async with _BROWSER_LOCK:
        if _BROWSER is not None:
            if _BROWSER.is_alive():
                return _BROWSER
            await _BROWSER.aclose()
            _BROWSER = None
        b = CdpBrowser()
        await b.start()
        _BROWSER = b
        if not _ATEXIT_REGISTERED:
            atexit.register(_atexit_close)
            _ATEXIT_REGISTERED = True
        return b


async def close_browser() -> None:
    """关闭并清理无头浏览器实例（web_page close 动作）。与 get_browser 同锁：
    避免与并发启动路径交错出「刚启动就被换血」或双实例窗口。"""
    global _BROWSER
    async with _BROWSER_LOCK:
        b, _BROWSER = _BROWSER, None
        if b is not None:
            await b.aclose()


# ---- 页面级动作（动作 → CDP 命令的映射都在这里，测试逐一断言） ----

_JS_PAGE_INFO = "JSON.stringify({title: document.title, url: location.href})"


def _check_url(raw: str) -> str:
    """只放行 http(s) 绝对地址（与 tools/browser.validate_url 同一策略，文案按本工具动作名）。"""
    url = str(raw or "").strip()
    if not url:
        raise ToolError("navigate 动作需要提供 url 参数（http/https 地址）。")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(
            f"web_page 只允许访问 http(s) 网址，收到的是「{url[:80]}」。"
            "本地文件请直接用 read_file / read_document 读取，不需要开浏览器。"
        )
    return url


def _assert_public_target(url: str) -> None:
    """navigate 目标主机必须解析到公网（与 web_fetch 同级 SSRF 防线）。

    web_page 的页面内容会回给模型、页面开在用户看不见的无头实例里，与
    web_fetch 同属引擎侧拉取：内网字面量地址、解析出内网/回环/链路本地
    IP 的域名一律拒绝（复用 web.py 的 _resolve_public_ips，同一套「全部
    解析结果都必须公网」口径）。非网页地址（about:blank 等）不校验。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return
    host = (parsed.hostname or "").strip()
    if not host:
        return
    try:
        _resolve_public_ips(host)
    except ToolError:
        raise ToolError(
            f"web_page 拒绝非公网地址（{host}）：内网/回环/链路本地地址不允许"
            "访问。如需操作内网页面，请直接在自己的浏览器里打开。"
        ) from None


def _js_find_center(selector: str) -> str:
    sel = json.dumps(selector, ensure_ascii=False)
    return (
        "(() => {"
        "const el = document.querySelector(" + sel + ");"
        "if (!el) return JSON.stringify({found:false});"
        "el.scrollIntoView({block:'center', inline:'center'});"
        "const r = el.getBoundingClientRect();"
        "return JSON.stringify({found:true, x:r.left + r.width/2, y:r.top + r.height/2});"
        "})()"
    )


def _js_fill(selector: str, text: str) -> str:
    sel = json.dumps(selector, ensure_ascii=False)
    val = json.dumps(text, ensure_ascii=False)
    return (
        "(() => {"
        "const el = document.querySelector(" + sel + ");"
        "if (!el) return JSON.stringify({found:false});"
        "el.focus();"
        "const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype"
        " : (el instanceof HTMLInputElement ? HTMLInputElement.prototype : null);"
        "if (proto) { Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, " + val + "); }"
        "else { el.textContent = " + val + "; }"
        "el.dispatchEvent(new Event('input', {bubbles:true}));"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "return JSON.stringify({found:true});"
        "})()"
    )


async def _eval_json(conn: CdpConnection, session_id: str, expression: str) -> dict[str, Any]:
    res = await conn.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session_id,
    )
    if res.get("exceptionDetails"):
        detail = res["exceptionDetails"].get("exception") or {}
        raise ToolError("页面脚本执行出错：" + str(detail.get("description", ""))[:200])
    inner = res.get("result") or {}
    if inner.get("type") != "string":
        raise ToolError("页面脚本没有返回预期的 JSON 字符串。")
    try:
        value = json.loads(inner.get("value") or "")
    except ValueError:
        raise ToolError("页面脚本返回的 JSON 无法解析。") from None
    return value if isinstance(value, dict) else {}


async def _eval_text(conn: CdpConnection, session_id: str, expression: str) -> str:
    res = await conn.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session_id,
    )
    if res.get("exceptionDetails"):
        detail = res["exceptionDetails"].get("exception") or {}
        raise ToolError("页面脚本执行出错：" + str(detail.get("description", ""))[:200])
    return str((res.get("result") or {}).get("value") or "")


async def _wait_ready(conn: CdpConnection, session_id: str) -> bool:
    """轮询 document.readyState 到 complete；超时不报错（导航本身已发生）。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + _PAGE_READY_TIMEOUT
    while True:
        try:
            res = await conn.send(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                session_id=session_id,
            )
            state = str((res.get("result") or {}).get("value") or "")
        except ToolError:
            state = ""  # 导航瞬间上下文可能销毁，等下一拍
        if state == "complete":
            return True
        if loop.time() >= end:
            return False
        await asyncio.sleep(_POLL_INTERVAL)


async def cdp_navigate(conn: CdpConnection, session_id: str, url: str) -> str:
    res = await conn.send("Page.navigate", {"url": url}, session_id=session_id)
    if res.get("errorText"):
        raise ToolError(
            f"导航失败：{res['errorText']}（{url}）。检查网址是否正确、本机是否能访问该站点。"
        )
    done = await _wait_ready(conn, session_id)
    info: dict[str, Any] = {}
    try:
        info = await _eval_json(conn, session_id, _JS_PAGE_INFO)
    except ToolError:
        pass  # 标题/最终地址拿不到不影响「已导航」这个结论
    title = str(info.get("title", "")).strip()
    final_url = str(info.get("url", "")).strip() or url
    # 重定向复核（web_fetch「逐跳校验」的等价物）：确认卡只展示首跳 URL，
    # 302/JS 跳走的目标用户看不到——最终地址落在非公网（云 metadata、内网
    # 管理页）时拒绝返回，并把页面弹回空白，内容不进模型上下文、浏览器
    # 也不留在内网页面给后续 extract 留口
    try:
        await asyncio.to_thread(_assert_public_target, final_url)
    except ToolError:
        try:
            await conn.send("Page.navigate", {"url": "about:blank"}, session_id=session_id)
        except ToolError:
            pass  # 弹回失败不掩盖原拒绝原因
        raise
    out = f"已导航到 {final_url}，页面标题「{title or '（无标题）'}」"
    if not done:
        out += (
            f"\n注意：{_PAGE_READY_TIMEOUT:.0f} 秒内页面未加载完成，"
            "内容可能不完整（可稍后重新 extract）。"
        )
    return out


async def cdp_click(conn: CdpConnection, session_id: str, selector: str) -> str:
    info = await _eval_json(conn, session_id, _js_find_center(selector))
    if not info.get("found"):
        raise ToolError(f"页面上找不到元素：{selector}")
    try:
        x = round(float(info["x"]))
        y = round(float(info["y"]))
    except (KeyError, TypeError, ValueError):
        raise ToolError(f"元素「{selector}」没有可点击的位置（可能不可见）。") from None
    params = {"x": x, "y": y, "button": "left", "clickCount": 1}
    await conn.send(
        "Input.dispatchMouseEvent", {**params, "type": "mousePressed"}, session_id=session_id
    )
    await conn.send(
        "Input.dispatchMouseEvent", {**params, "type": "mouseReleased"}, session_id=session_id
    )
    return f"已在 ({x}, {y}) 点击元素：{selector}"


async def cdp_fill(conn: CdpConnection, session_id: str, selector: str, text: str) -> str:
    info = await _eval_json(conn, session_id, _js_fill(selector, text))
    if not info.get("found"):
        raise ToolError(f"页面上找不到元素：{selector}")
    return f"已向 {selector} 填入 {len(text)} 个字符。"


# press 的按键映射：name -> (key, windowsVirtualKeyCode, text)
_KEY_DEFS: dict[str, tuple[str, int, str | None]] = {
    "enter": ("Enter", 13, "\r"),
    "tab": ("Tab", 9, "\t"),
    "esc": ("Escape", 27, None),
    "escape": ("Escape", 27, None),
    "backspace": ("Backspace", 8, None),
    "delete": ("Delete", 46, None),
    "space": (" ", 32, " "),
    "up": ("ArrowUp", 38, None),
    "down": ("ArrowDown", 40, None),
    "left": ("ArrowLeft", 37, None),
    "right": ("ArrowRight", 39, None),
    "home": ("Home", 36, None),
    "end": ("End", 35, None),
    "pageup": ("PageUp", 33, None),
    "pagedown": ("PageDown", 34, None),
}


def _key_params(raw: str, type_: str) -> dict[str, Any]:
    key = raw.strip()
    named = _KEY_DEFS.get(key.lower())
    if named is not None:
        name, vk, text = named
        params: dict[str, Any] = {"type": type_, "key": name, "windowsVirtualKeyCode": vk}
        if text and type_ == "keyDown":
            params["text"] = text
        return params
    if len(key) == 1 and key.isprintable():
        params = {"type": type_, "key": key}
        if type_ == "keyDown":
            params["text"] = key
        return params
    raise ToolError(
        f"不支持的按键：{raw}（支持 enter/tab/esc/backspace/delete/space/"
        "方向键/home/end/pageup/pagedown 或单个字符）"
    )


async def cdp_press(conn: CdpConnection, session_id: str, raw: str) -> str:
    await conn.send("Input.dispatchKeyEvent", _key_params(raw, "keyDown"), session_id=session_id)
    await conn.send("Input.dispatchKeyEvent", _key_params(raw, "keyUp"), session_id=session_id)
    return f"已发送按键：{raw.strip()}"


async def cdp_extract(conn: CdpConnection, session_id: str, selector: str) -> str:
    if selector.strip():
        sel = json.dumps(selector.strip(), ensure_ascii=False)
        expr = (
            "(() => { const el = document.querySelector(" + sel + ");"
            "return el ? (el.innerText || el.textContent || '') : ''; })()"
        )
        text = await _eval_text(conn, session_id, expr)
        if not text:
            raise ToolError(f"元素「{selector}」不存在或没有可见文本。")
    else:
        expr = "(document.body ? document.body.innerText : '')"
        text = await _eval_text(conn, session_id, expr)
        if not text:
            return "（页面没有可见文本。）"
    return truncate_output(text)


async def cdp_screenshot(conn: CdpConnection, session_id: str) -> bytes:
    res = await conn.send("Page.captureScreenshot", {"format": "png"}, session_id=session_id)
    data = res.get("data")
    if not data:
        raise ToolError("无头浏览器没有返回截图数据。")
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError):
        raise ToolError("截图数据（base64）解码失败。") from None


# ---- 工具定义 ----


class WebPageArgs(BaseModel):
    action: Literal["navigate", "click", "fill", "press", "extract", "screenshot", "close"] = (
        Field(
            description=(
                "navigate=打开网址；click=点击元素；fill=向输入框填文本；press=按键；"
                "extract=提取页面/元素文本；screenshot=截图（图像附加进对话）；close=关闭浏览器实例"
            )
        )
    )
    url: str = Field(default="", description="navigate 时的 http(s) 网址")
    selector: str = Field(default="", description="click/fill/extract 的 CSS 选择器")
    text: str = Field(default="", description="fill 要填入的内容")
    key: str = Field(default="", description='press 的按键，如 "enter"、"tab"、方向键或单个字符')


class WebPageTool(Tool):
    name = "web_page"
    description = (
        "在本机 Edge 无头浏览器里完成网页内的自动化操作：navigate 打开网址、"
        "click 点击元素、fill 填输入框、press 按键、extract 提取页面或元素文本、"
        "screenshot 截图（图像会附加到对话中）、close 关闭浏览器实例。"
        "selector 用 CSS 选择器。与 browser 的区别：browser 把网页开在用户自己的"
        "浏览器里给用户看；web_page 操作的是引擎拉起的临时无头实例，你能看到页面"
        "内容而用户看不到，Cookie/登录态与用户浏览器互不相通。"
        "高危操作，会先请求用户确认。"
    )
    safety = Safety.DANGEROUS
    read_only_hint = False
    destructive_hint = True
    idempotent_hint = False
    open_world_hint = True
    args_model = WebPageArgs

    def arg_text(self, input_dict: dict) -> str:
        # 首词是 action：「总是允许」按动作前缀沉淀（gate._ACTION_PREFIX_TOOLS）。
        # fill 的内容必须可见（确认卡只展示 arg_text）——要填入的文本是对页面
        # 的任意写入面，规则也被 _EXACT_ACTION_TOOLS 固化成当次参数，看不
        # 见内容的确认等于盲批（安全审查残留发现）。navigate 补显目标主机：
        # URL 过长被截断时（含 userinfo@ 伪装形态），真实主机名必须可见。
        action = str(input_dict.get("action", ""))
        if action == "fill":
            sel = str(input_dict.get("selector") or "")
            txt = str(input_dict.get("text") or "")
            return f"fill {sel} ｜填入: {txt[:80]}".strip()
        target = (
            input_dict.get("url")
            or input_dict.get("selector")
            or input_dict.get("key")
            or input_dict.get("text")
            or ""
        )
        text = f"{action} {str(target)[:80]}".strip()
        if action == "navigate":
            host = (urlparse(str(input_dict.get("url") or "")).hostname or "").strip()
            if host:
                text += f" ｜目标主机: {host}"
        return text

    async def run(self, args: WebPageArgs, ctx: ToolContext) -> str:
        url = ""
        if args.action == "navigate":
            url = _check_url(args.url)
            # SSRF 前置校验（与 web_fetch 同级，见模块说明）：目标主机必须
            # 解析到公网。getaddrinfo 是阻塞调用，丢线程池防卡事件循环。
            await asyncio.to_thread(_assert_public_target, url)
        if args.action in ("click", "fill") and not args.selector.strip():
            raise ToolError(f"{args.action} 动作需要提供 selector 参数（CSS 选择器）。")
        # extract 不强制 selector：不带就是整页文本
        if args.action == "fill" and not args.text:
            raise ToolError("fill 动作需要提供 text 参数（要填入的内容）。")
        if args.action == "press" and not args.key.strip():
            raise ToolError('press 动作需要提供 key 参数（如 "enter"、"tab" 或单个字符）。')

        if args.action == "close":
            await close_browser()
            return "已关闭无头浏览器实例（页内会话结束，临时配置目录与 Cookie 一并清理）。"

        if args.action == "screenshot" and not getattr(ctx, "supports_vision", True):
            raise ToolError(
                "当前模型不支持图片输入，网页截图它看不到。请在输入框的模型选择器里"
                "换一个多模态模型，或改用 extract 提取页面文本。"
            )

        browser = await get_browser()
        try:
            conn, session_id = await browser.open_page()
        except (ConnectionClosed, WebSocketException, OSError) as e:
            browser.drop_connection()
            raise ToolError(f"连接无头浏览器失败：{e}\n重试会自动重新拉起实例。") from None
        try:
            return await self._dispatch(args, ctx, conn, session_id, url)
        except (ConnectionClosed, WebSocketException, OSError) as e:
            browser.drop_connection()
            raise ToolError(
                f"与无头浏览器的连接中断：{e}\n重试会自动重新拉起实例（页面状态会丢失）。"
            ) from None
        finally:
            await browser.detach(session_id)

    async def _dispatch(
        self,
        args: WebPageArgs,
        ctx: ToolContext,
        conn: CdpConnection,
        session_id: str,
        url: str,
    ) -> str:
        if args.action == "navigate":
            return await cdp_navigate(conn, session_id, url)
        if args.action == "click":
            return await cdp_click(conn, session_id, args.selector.strip())
        if args.action == "fill":
            return await cdp_fill(conn, session_id, args.selector.strip(), args.text)
        if args.action == "press":
            return await cdp_press(conn, session_id, args.key)
        if args.action == "extract":
            return await cdp_extract(conn, session_id, args.selector)
        if args.action == "screenshot":
            return await self._do_screenshot(conn, session_id, ctx)
        raise ToolError(f"未知的 web_page 动作：{args.action}")  # pragma: no cover

    async def _do_screenshot(
        self, conn: CdpConnection, session_id: str, ctx: ToolContext
    ) -> str:
        png = await cdp_screenshot(conn, session_id)
        ctx.images.append(
            ImageBlock(media_type="image/png", data=base64.b64encode(png).decode())
        )
        # 副本与 screenshot 工具同一目录、同一保留策略（_prune_screenshots）
        shots = skysheep_home() / "screenshots"
        path = shots / f"webpage_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        saved = False
        try:
            shots.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            _prune_screenshots(shots)
            saved = True
        except OSError:
            pass  # 副本保存失败不影响主流程（图像已附加进对话）
        lines = [
            f"网页截图完成（{len(png):,} 字节），图像已附加在本条消息之后（模型可直接查看）",
        ]
        lines.append(f"- 副本已保存: {path}" if saved else "- 副本保存失败（不影响本次截图）")
        return "\n".join(lines)
