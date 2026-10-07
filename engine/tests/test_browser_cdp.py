"""web_page（浏览器页内自动化 / CDP）测试：动作→CDP 命令映射与错误路径。

不启动真实浏览器：
- 进程层 FakeProc（monkeypatch browser_cdp._spawn），stderr 按 DEVTOOLS_LINE
  回放「DevTools listening on ws://…」，EOF / 悬挂分别对应启动失败与超时；
- WebSocket 层 FakeTransport（monkeypatch websockets.connect），按请求的
  method 回放 CDP JSON-RPC 响应，支持杂帧（事件/迟到响应）与非 JSON 垃圾帧。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from pathlib import Path

import pytest
import websockets

import skysheep.tools.browser_cdp as cdp
from skysheep.config import skysheep_home
from skysheep.tools import default_tools
from skysheep.tools.base import Safety, ToolContext, ToolError
from skysheep.tools.browser_cdp import CdpBrowser, CdpConnection, WebPageTool

DEVTOOLS_LINE = (
    b"[9260:12356:1007/101500.123:INFO:content_main.cc(123)] "
    b"DevTools listening on ws://127.0.0.1:9222/devtools/browser/guid-123\n"
)


def _ctx(tmp_path):
    return ToolContext(working_dir=tmp_path)


@pytest.fixture(autouse=True)
async def _reset_singleton(monkeypatch):
    """单例是模块级状态：每个用例都从干净状态出发，跑完把残留实例收干净。

    部分用例会用假进程走真 start()（真建临时配置目录），不收尾就会每次跑测试
    泄漏一个 skysheep-edge-* 临时目录。
    """
    monkeypatch.setattr(cdp, "_BROWSER", None)
    yield
    b, cdp._BROWSER = cdp._BROWSER, None
    if b is not None:
        await b.aclose()


# ---- 替身 ----


class FakeTransport:
    """假 websockets 连接：按请求的 method 回放 CDP JSON-RPC 响应。

    script: method -> result；dict 直接回放，list 依次消费（同名方法多次调用
    各给各的，用尽回落 {}）；Exception 回放成 CDP error 帧。noise 是响应前先吐
    的杂帧（事件 / 迟到的旧响应）；garbage 是 recv 永远返回的垃圾帧（解析失败
    用例）；hang=True 时 recv 永久悬挂（超时用例）。
    """

    def __init__(self, script=None, *, noise=(), garbage=None, hang=False):
        self.script = dict(script or {})
        self.noise = list(noise)
        self.garbage = garbage
        self.hang = hang
        self.sent: list[dict] = []
        self.closed = False
        self._pending: deque[str] = deque()

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("id") is None:
            return
        if self.hang:
            return  # 悬挂模式：命令发出去永远没有响应（超时用例）
        script = self.script.get(msg["method"], {})
        if isinstance(script, list):
            result = script.pop(0) if script else {}  # 用尽即回落 {}
        else:
            result = script
        if isinstance(result, BaseException):
            self._pending.append(
                json.dumps({"id": msg["id"], "error": {"message": str(result)}})
            )
        elif isinstance(result, dict) and "error" in result:
            self._pending.append(json.dumps({"id": msg["id"], "error": result["error"]}))
        else:
            self._pending.append(json.dumps({"id": msg["id"], "result": result}))

    async def recv(self):
        if self.garbage is not None:
            return self.garbage
        if self.noise:
            return self.noise.pop(0)
        if not self._pending:
            if self.hang:
                await asyncio.sleep(3600)
            raise AssertionError("FakeTransport 收到了没有预备响应的请求")
        return self._pending.popleft()

    async def close(self):
        self.closed = True


def _install_transport(monkeypatch, transport) -> list[str]:
    """把 websockets.connect 换成回放 FakeTransport 的假连接，返回连过的 URL。"""
    urls: list[str] = []

    async def _connect(url, **kwargs):
        urls.append(url)
        return transport

    monkeypatch.setattr(websockets, "connect", _connect)
    return urls


class FakeProc:
    """假进程：stderr 按行吐 lines（EOF 返回 b\"\"），可被 terminate/kill。"""

    def __init__(self, lines=(), returncode=None):
        self._lines = list(lines)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.stderr = self  # start() 读 proc.stderr，直接拿自己当 stderr（有 readline）

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self):
        self.killed = True
        self.terminate()

    async def wait(self):
        return self.returncode


@pytest.fixture
def fake_spawn(monkeypatch):
    """替换 _spawn：不真的拉起浏览器进程。返回 (参数记录, 进程队列)。"""
    procs: list[FakeProc] = []
    holder: dict = {}

    async def _spawn(cmd, **kwargs):
        holder["cmd"] = cmd
        if not procs:
            raise AssertionError("FakeProc 队列空了")
        return procs.pop(0)

    monkeypatch.setattr(cdp, "_spawn", _spawn)
    return holder, procs


class StubBrowser:
    """替身单例：open_page 直接给挂上 FakeTransport 的 CdpConnection。"""

    def __init__(self, transport):
        self.transport = transport
        self.detached: list[str] = []

    async def open_page(self):
        return CdpConnection(self.transport), "sess-1"

    async def detach(self, session_id):
        self.detached.append(session_id)

    def drop_connection(self):
        pass


async def _run_tool(monkeypatch, tmp_path, transport, *, ctx=None, blocked_hosts=(), **kw):
    """跑一次 WebPageTool.run：单例与 websockets 都已替身。

    SSRF 公网校验换成可控假守卫（默认全放行、不触 DNS），blocked_hosts 里
    的主机按「非公网」拒绝——真实解析逻辑由带 literal IP 的专项用例覆盖。
    """
    stub = StubBrowser(transport)

    async def _get_browser():
        return stub

    def _fake_resolve(host):
        if str(host).lower() in {h.lower() for h in blocked_hosts}:
            raise ToolError(f"web_fetch 拒绝非公网地址（{host} → 192.0.2.1）")
        return ["203.0.113.9"]

    monkeypatch.setattr(cdp, "get_browser", _get_browser)
    monkeypatch.setattr(cdp, "_resolve_public_ips", _fake_resolve)
    ctx = ctx or _ctx(tmp_path)
    out = await WebPageTool().run(WebPageTool.args_model(**kw), ctx)
    return out, stub, ctx


def _methods(transport):
    return [m["method"] for m in transport.sent]


# ---- find_edge：定位 msedge ----


def test_find_edge_uses_path(monkeypatch):
    monkeypatch.setattr(cdp.shutil, "which", lambda name: "C:/Edge/msedge.exe")
    assert cdp.find_edge() == "C:/Edge/msedge.exe"


def test_find_edge_falls_back_to_program_files(monkeypatch):
    monkeypatch.setattr(cdp.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        Path, "is_file", lambda self: str(self) == cdp.EDGE_CANDIDATES[1]
    )
    assert cdp.find_edge() == cdp.EDGE_CANDIDATES[1]


def test_find_edge_missing_gives_readable_error(monkeypatch):
    monkeypatch.setattr(cdp.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(ToolError) as ei:
        cdp.find_edge()
    msg = str(ei.value)
    assert "msedge" in msg and "Edge" in msg


# ---- 启动：stderr 解析与失败路径 ----


async def test_start_parses_devtools_url_and_launch_args(fake_spawn, monkeypatch):
    holder, procs = fake_spawn
    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    procs.append(FakeProc([DEVTOOLS_LINE]))
    b = CdpBrowser()
    await b.start()
    assert b.ws_url == "ws://127.0.0.1:9222/devtools/browser/guid-123"
    cmd = holder["cmd"]
    assert cmd[0] == "C:/Edge/msedge.exe"
    assert "--headless=new" in cmd
    assert "--remote-debugging-port=0" in cmd
    assert "about:blank" in cmd
    udd = [a for a in cmd if a.startswith("--user-data-dir=")]
    assert len(udd) == 1
    profile = Path(udd[0].split("=", 1)[1])
    assert profile.is_dir()  # 一次性临时配置目录真的建出来了
    await b.aclose()
    assert not profile.exists()  # 清理路径会删掉临时目录


async def test_start_failure_when_process_exits_without_devtools(fake_spawn, monkeypatch):
    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    proc = FakeProc([b"some unrelated output\n"])  # EOF 且没有调试地址
    fake_spawn[1].append(proc)
    with pytest.raises(ToolError) as ei:
        await CdpBrowser().start()
    assert "启动失败" in str(ei.value)
    assert proc.terminated  # 失败路径也要收掉进程


async def test_start_failure_when_spawn_raises(monkeypatch):
    async def boom(cmd, **kwargs):
        raise FileNotFoundError(2, "msedge.exe")

    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    monkeypatch.setattr(cdp, "_spawn", boom)
    b = CdpBrowser()
    with pytest.raises(ToolError) as ei:
        await b.start()
    assert "无法启动" in str(ei.value)
    assert b.profile_dir == ""  # 临时目录已清理


async def test_start_timeout_waiting_for_devtools(fake_spawn, monkeypatch):
    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    monkeypatch.setattr(cdp, "_START_TIMEOUT", 0.1)

    class HangProc(FakeProc):
        async def readline(self):
            await asyncio.sleep(5)
            return b""

    proc = HangProc()
    fake_spawn[1].append(proc)
    with pytest.raises(ToolError) as ei:
        await CdpBrowser().start()
    assert "超时" in str(ei.value)
    assert proc.terminated


# ---- CDP 客户端：按 id 收发 / 错误 / 解析失败 / 超时 ----


async def test_send_matches_response_by_id_and_skips_noise():
    t = FakeTransport(
        {"Target.getTargets": {"targetInfos": [{"type": "page", "targetId": "T1"}]}},
        noise=[
            json.dumps({"method": "Page.frameStartedLoading", "params": {}}),
            json.dumps({"id": 999, "result": {}}),  # 迟到的旧响应
        ],
    )
    conn = CdpConnection(t)
    res = await conn.send("Target.getTargets", timeout=2)
    assert res["targetInfos"][0]["targetId"] == "T1"
    assert t.sent[0]["id"] == 1 and t.sent[0]["method"] == "Target.getTargets"
    res2 = await conn.send(
        "Target.attachToTarget", {"targetId": "T1"}, session_id="S", timeout=2
    )
    assert res2 == {}
    assert t.sent[1]["id"] == 2 and t.sent[1]["sessionId"] == "S"


async def test_send_surfaces_cdp_error():
    t = FakeTransport({"Page.navigate": RuntimeError("net::ERR_NAME_NOT_RESOLVED")})
    with pytest.raises(ToolError) as ei:
        await CdpConnection(t).send(
            "Page.navigate", {"url": "https://nope.example"}, timeout=2
        )
    assert "ERR_NAME_NOT_RESOLVED" in str(ei.value)
    assert "Page.navigate" in str(ei.value)


@pytest.mark.parametrize("garbage", [b"not-json", json.dumps([1, 2]), json.dumps("hello")])
async def test_send_rejects_unparsable_frames(garbage):
    t = FakeTransport(garbage=garbage)
    with pytest.raises(ToolError) as ei:
        await CdpConnection(t).send("Runtime.evaluate", timeout=2)
    assert "无法解析" in str(ei.value)


async def test_send_timeout_raises_tool_error():
    t = FakeTransport(hang=True)
    with pytest.raises(ToolError) as ei:
        await CdpConnection(t).send("Page.captureScreenshot", timeout=0.05)
    assert "超时" in str(ei.value)


# ---- 页面 target：复用 / 新建 / 重建 ----


async def test_open_page_reuses_existing_tab():
    t = FakeTransport({
        "Target.getTargets": {
            "targetInfos": [
                {"type": "page", "targetId": "T1"},
                {"type": "iframe", "targetId": "X"},
            ]
        },
        "Target.attachToTarget": {"sessionId": "S1"},
    })
    b = CdpBrowser()
    b._conn = CdpConnection(t)  # 直接塞连接，绕过真实 websockets
    b._target_id = "T1"
    conn, sid = await b.open_page()
    assert conn is b._conn and sid == "S1"
    assert _methods(t) == ["Target.getTargets", "Target.attachToTarget"]  # 不新开 tab
    assert t.sent[1]["params"] == {"targetId": "T1", "flatten": True}


async def test_open_page_creates_tab_when_none():
    t = FakeTransport({
        "Target.getTargets": {"targetInfos": []},
        "Target.createTarget": {"targetId": "T2"},
        "Target.attachToTarget": {"sessionId": "S2"},
    })
    b = CdpBrowser()
    b._conn = CdpConnection(t)
    _, sid = await b.open_page()
    assert sid == "S2"
    assert _methods(t) == ["Target.createTarget", "Target.attachToTarget"]  # 无缓存 tab 时不查列表
    assert b._target_id == "T2"


async def test_open_page_recreates_tab_after_close():
    t = FakeTransport({
        "Target.getTargets": {"targetInfos": []},
        "Target.createTarget": {"targetId": "T3"},
        "Target.attachToTarget": {"sessionId": "S3"},
    })
    b = CdpBrowser()
    b._conn = CdpConnection(t)
    b._target_id = "stale"
    _, sid = await b.open_page()
    assert sid == "S3" and b._target_id == "T3"


async def test_open_page_attach_error_is_toolerror():
    t = FakeTransport({
        "Target.getTargets": {"targetInfos": [{"type": "page", "targetId": "T1"}]},
        "Target.attachToTarget": {"error": {"message": "No target with given id"}},
    })
    b = CdpBrowser()
    b._conn = CdpConnection(t)
    b._target_id = "T1"
    with pytest.raises(ToolError):
        await b.open_page()


async def test_detach_is_best_effort():
    t = FakeTransport({"Target.detachTarget": RuntimeError("session gone")})
    b = CdpBrowser()
    b._conn = CdpConnection(t)
    await b.detach("S1")  # 出错也不抛
    assert _methods(t) == ["Target.detachTarget"]
    await CdpBrowser().detach("S1")  # 无连接时静默


# ---- 单例：get_browser / close_browser / aclose ----


async def test_get_browser_reuses_singleton(fake_spawn, monkeypatch):
    _, procs = fake_spawn
    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    procs.append(FakeProc([DEVTOOLS_LINE]))
    urls = _install_transport(
        monkeypatch,
        FakeTransport({
            "Target.createTarget": {"targetId": "T1"},
            "Target.attachToTarget": {"sessionId": "S1"},
        }),
    )
    b1 = await cdp.get_browser()
    conn, sid = await b1.open_page()
    assert sid == "S1"
    assert urls == ["ws://127.0.0.1:9222/devtools/browser/guid-123"]
    b2 = await cdp.get_browser()
    assert b2 is b1 and procs == []  # 第二次没有再拉进程


async def test_get_browser_restarts_after_death(fake_spawn, monkeypatch):
    _, procs = fake_spawn
    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    p1, p2 = FakeProc([DEVTOOLS_LINE]), FakeProc([DEVTOOLS_LINE])
    procs += [p1, p2]
    _install_transport(monkeypatch, FakeTransport())
    b1 = await cdp.get_browser()
    p1.returncode = 1  # 进程死了
    b2 = await cdp.get_browser()
    assert b2 is not b1 and b2.proc is p2


async def test_aclose_closes_ws_terminates_and_cleans_profile(tmp_path):
    b = CdpBrowser()
    profile = tmp_path / "profile"
    profile.mkdir()
    b.profile_dir = str(profile)
    t = FakeTransport()
    b._conn = CdpConnection(t)
    proc = FakeProc()
    b.proc = proc
    await b.aclose()
    assert t.closed
    assert b.proc is None and proc.terminated
    assert b.profile_dir == "" and not profile.exists()


def test_close_sync_terminates_process():
    b = CdpBrowser()
    proc = FakeProc()
    b.proc = proc
    b.close_sync()
    assert proc.terminated and b.proc is None


async def test_close_browser_shuts_singleton_down():
    closed = []

    class ClosingStub:
        async def aclose(self):
            closed.append(True)

    cdp._BROWSER = ClosingStub()
    await cdp.close_browser()
    assert closed == [True]
    assert cdp._BROWSER is None


# ---- web_page 动作 → CDP 命令映射（走 WebPageTool.run 全链路） ----


async def test_run_navigate_sends_page_navigate_and_waits_ready(tmp_path, monkeypatch):
    t = FakeTransport({
        "Page.navigate": {},
        "Runtime.evaluate": [
            {"result": {"type": "string", "value": "complete"}},  # readyState
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({"title": "Example", "url": "https://example.com/"}),
                }
            },
        ],
    })
    out, stub, _ = await _run_tool(
        monkeypatch, tmp_path, t, action="navigate", url="https://example.com"
    )
    assert "已导航" in out and "Example" in out
    assert _methods(t) == ["Page.navigate", "Runtime.evaluate", "Runtime.evaluate"]
    assert t.sent[0]["params"] == {"url": "https://example.com"}
    assert t.sent[0]["sessionId"] == "sess-1"
    assert t.sent[1]["params"]["expression"].startswith("document.readyState")
    assert "document.title" in t.sent[2]["params"]["expression"]
    assert stub.detached == ["sess-1"]


async def test_run_navigate_error_text(tmp_path, monkeypatch):
    t = FakeTransport({"Page.navigate": {"errorText": "net::ERR_NAME_NOT_RESOLVED"}})
    with pytest.raises(ToolError) as ei:
        await _run_tool(
            monkeypatch, tmp_path, t, action="navigate", url="https://nope.example"
        )
    assert "ERR_NAME_NOT_RESOLVED" in str(ei.value)


async def test_run_navigate_rejects_non_http(tmp_path, monkeypatch):
    t = FakeTransport()
    with pytest.raises(ToolError):
        await _run_tool(monkeypatch, tmp_path, t, action="navigate", url="file:///C:/x")
    assert t.sent == []  # 校验失败不碰浏览器


async def test_run_click_resolves_then_dispatches_mouse_events(tmp_path, monkeypatch):
    t = FakeTransport({
        "Runtime.evaluate": [
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({"found": True, "x": 120.4, "y": 88.6}),
                }
            }
        ],
    })
    out, _, _ = await _run_tool(
        monkeypatch, tmp_path, t, action="click", selector="#submit"
    )
    assert "点击" in out
    assert _methods(t) == [
        "Runtime.evaluate",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    press, release = t.sent[1], t.sent[2]
    assert press["params"]["type"] == "mousePressed"
    assert release["params"]["type"] == "mouseReleased"
    assert press["params"]["x"] == 120 and press["params"]["y"] == 89  # 取整
    assert press["params"]["button"] == "left" and press["params"]["clickCount"] == 1
    common = ("x", "y", "button", "clickCount")
    assert all(press["params"][k] == release["params"][k] for k in common)
    expr = t.sent[0]["params"]["expression"]
    assert "querySelector" in expr and "#submit" in expr and "scrollIntoView" in expr


async def test_run_click_element_not_found(tmp_path, monkeypatch):
    t = FakeTransport({
        "Runtime.evaluate": [
            {"result": {"type": "string", "value": json.dumps({"found": False})}}
        ],
    })
    with pytest.raises(ToolError) as ei:
        await _run_tool(monkeypatch, tmp_path, t, action="click", selector="#nope")
    assert "找不到" in str(ei.value)
    assert all(m["method"] != "Input.dispatchMouseEvent" for m in t.sent)


async def test_run_fill_sets_value_with_native_setter(tmp_path, monkeypatch):
    t = FakeTransport({
        "Runtime.evaluate": [
            {"result": {"type": "string", "value": json.dumps({"found": True})}}
        ],
    })
    out, _, _ = await _run_tool(
        monkeypatch, tmp_path, t, action="fill", selector="input#q", text="你好 SkySheep"
    )
    assert "input#q" in out
    expr = t.sent[0]["params"]["expression"]
    assert "HTMLInputElement" in expr and "你好 SkySheep" in expr
    assert "dispatchEvent" in expr and "change" in expr


@pytest.mark.parametrize(
    "key,vk,text",
    [("enter", 13, "\r"), ("tab", 9, "\t"), ("esc", 27, None)],
)
async def test_run_press_named_keys(tmp_path, monkeypatch, key, vk, text):
    t = FakeTransport()
    out, _, _ = await _run_tool(monkeypatch, tmp_path, t, action="press", key=key)
    downs = [m for m in t.sent if m["method"] == "Input.dispatchKeyEvent"]
    assert [m["params"]["type"] for m in downs] == ["keyDown", "keyUp"]
    assert downs[0]["params"]["windowsVirtualKeyCode"] == vk
    if text is None:
        assert "text" not in downs[0]["params"]
    else:
        assert downs[0]["params"]["text"] == text
    assert key in out


async def test_run_press_single_char(tmp_path, monkeypatch):
    t = FakeTransport()
    await _run_tool(monkeypatch, tmp_path, t, action="press", key="a")
    downs = [m for m in t.sent if m["method"] == "Input.dispatchKeyEvent"]
    assert downs[0]["params"]["text"] == "a"
    assert downs[1]["params"]["type"] == "keyUp"


async def test_run_press_rejects_unknown_key(tmp_path, monkeypatch):
    t = FakeTransport()
    with pytest.raises(ToolError) as ei:
        await _run_tool(monkeypatch, tmp_path, t, action="press", key="ctrl+alt+del")
    assert "不支持的按键" in str(ei.value)
    assert t.sent == []  # 校验失败不碰浏览器


async def test_run_extract_selector_text(tmp_path, monkeypatch):
    t = FakeTransport({
        "Runtime.evaluate": [{"result": {"type": "string", "value": "表格里的数据"}}],
    })
    out, _, _ = await _run_tool(
        monkeypatch, tmp_path, t, action="extract", selector="table.data"
    )
    assert "表格里的数据" in out
    expr = t.sent[0]["params"]["expression"]
    assert "querySelector" in expr and "innerText" in expr


async def test_run_extract_whole_page(tmp_path, monkeypatch):
    t = FakeTransport({
        "Runtime.evaluate": [{"result": {"type": "string", "value": "整页文本"}}],
    })
    out, _, _ = await _run_tool(monkeypatch, tmp_path, t, action="extract")
    assert "整页文本" in out
    assert "document.body.innerText" in t.sent[0]["params"]["expression"]


async def test_run_extract_empty_element_errors(tmp_path, monkeypatch):
    t = FakeTransport({
        "Runtime.evaluate": [{"result": {"type": "string", "value": ""}}],
    })
    with pytest.raises(ToolError):
        await _run_tool(monkeypatch, tmp_path, t, action="extract", selector="#empty")


async def test_run_screenshot_attaches_image_and_saves_copy(home, tmp_path, monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload"
    t = FakeTransport(
        {"Page.captureScreenshot": {"data": base64.b64encode(png).decode()}}
    )
    out, _, ctx = await _run_tool(monkeypatch, tmp_path, t, action="screenshot")
    assert len(ctx.images) == 1
    img = ctx.images[0]
    assert img.media_type == "image/png"
    assert base64.b64decode(img.data) == png
    assert "截图完成" in out
    shots = skysheep_home() / "screenshots"
    copies = list(shots.glob("webpage_*.png"))
    assert len(copies) == 1 and copies[0].read_bytes() == png


async def test_run_screenshot_requires_vision(tmp_path, monkeypatch):
    async def _boom():
        raise AssertionError("不支持图片输入时不应拉起浏览器")

    monkeypatch.setattr(cdp, "get_browser", _boom)
    ctx = ToolContext(working_dir=tmp_path, supports_vision=False)
    with pytest.raises(ToolError):
        await WebPageTool().run(WebPageTool.args_model(action="screenshot"), ctx)


async def test_run_close_shuts_singleton_down():
    closed = []

    class ClosingStub:
        async def aclose(self):
            closed.append(True)

    cdp._BROWSER = ClosingStub()
    out = await WebPageTool().run(
        WebPageTool.args_model(action="close"), ToolContext(working_dir=None)
    )
    assert "已关闭" in out
    assert closed == [True]
    assert cdp._BROWSER is None


# ---- 参数校验在拉起浏览器之前 ----


async def test_run_arg_validation_errors(tmp_path, monkeypatch):
    async def _boom():
        raise AssertionError("参数校验失败时不应拉起浏览器")

    monkeypatch.setattr(cdp, "get_browser", _boom)
    with pytest.raises(ToolError):
        await WebPageTool().run(
            WebPageTool.args_model(action="navigate", url=""), _ctx(tmp_path)
        )
    with pytest.raises(ToolError):
        await WebPageTool().run(WebPageTool.args_model(action="click"), _ctx(tmp_path))
    with pytest.raises(ToolError):
        await WebPageTool().run(
            WebPageTool.args_model(action="fill", selector="#a"), _ctx(tmp_path)
        )
    with pytest.raises(ToolError):
        await WebPageTool().run(WebPageTool.args_model(action="press"), _ctx(tmp_path))


async def test_run_ws_connect_failure(fake_spawn, monkeypatch, tmp_path):
    _, procs = fake_spawn
    monkeypatch.setattr(cdp, "find_edge", lambda: "C:/Edge/msedge.exe")
    procs.append(FakeProc([DEVTOOLS_LINE]))

    async def _connect(url, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(websockets, "connect", _connect)
    with pytest.raises(ToolError) as ei:
        await WebPageTool().run(
            WebPageTool.args_model(action="extract", selector="body"), _ctx(tmp_path)
        )
    assert "连接" in str(ei.value)


# ---- 工具元数据与注册 ----


def test_web_page_annotations_and_safety():
    tool = WebPageTool()
    ann = tool.to_schema()["annotations"]
    assert ann == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert tool.safety is Safety.DANGEROUS


def test_arg_text_first_word_is_action():
    tool = WebPageTool()
    # navigate 附显目标主机：URL 过长被截断（或 userinfo@ 伪装形态）时，
    # 确认卡上真实主机名仍可见
    assert (
        tool.arg_text({"action": "navigate", "url": "https://example.com/page"})
        == "navigate https://example.com/page ｜目标主机: example.com"
    )
    assert tool.arg_text({"action": "click", "selector": "#submit-btn"}).startswith("click ")
    assert tool.arg_text({"action": "screenshot"}).split()[0] == "screenshot"


def test_arg_text_fill_shows_content():
    """fill 的确认语义文本必须包含要填入的内容：内容是任意写入面，
    确认卡只显示 arg_text，看不见内容的确认等于盲批。"""
    tool = WebPageTool()
    text = tool.arg_text({"action": "fill", "selector": "#pwd", "text": "secret-token-123"})
    assert text.startswith("fill #pwd"), "首词仍是动作（前缀规则生成依赖它）"
    assert "secret-token-123" in text, "填入内容必须可见"
    long = tool.arg_text({"action": "fill", "selector": "#pwd", "text": "A" * 200})
    assert "AAAAA" in long, "长内容截断展示（前 80 字符可见即可）"


def test_registered_like_browser_tool():
    names_on = {t.name for t in default_tools(browser_control=True)}
    assert {"browser", "web_page"} <= names_on
    names_off = {t.name for t in default_tools()}
    assert "web_page" not in names_off and "browser" not in names_off


# ---- SSRF 防线（与 web_fetch 同级）：navigate 前置校验 + 重定向复核 ----


async def test_navigate_rejects_private_target_before_browser(tmp_path, monkeypatch):
    """内网字面量地址：前置校验直接拒绝，不碰浏览器。"""
    t = FakeTransport()
    with pytest.raises(ToolError) as ei:
        await _run_tool(
            monkeypatch, tmp_path, t,
            action="navigate", url="http://127.0.0.1:8080/admin",
            blocked_hosts=["127.0.0.1"],
        )
    assert "非公网" in str(ei.value)
    assert t.sent == [], "被拒的导航不得发任何 CDP 命令"


async def test_navigate_rejects_host_resolving_private(tmp_path, monkeypatch):
    """域名解析出内网 IP：同样拒绝（假守卫按主机名拦截）。"""
    t = FakeTransport()
    with pytest.raises(ToolError) as ei:
        await _run_tool(
            monkeypatch, tmp_path, t,
            action="navigate", url="https://intranet.example/",
            blocked_hosts=["intranet.example"],
        )
    assert "非公网" in str(ei.value)
    assert t.sent == []


async def test_navigate_redirect_to_private_bounces_to_blank(tmp_path, monkeypatch):
    """重定向落在非公网（确认卡只展示首跳）：拒绝返回、弹回空白页。"""
    t = FakeTransport({
        "Page.navigate": {},
        "Runtime.evaluate": [
            {"result": {"type": "string", "value": "complete"}},
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({
                        "title": "metadata",
                        "url": "http://169.254.169.254/latest/meta-data/",
                    }),
                }
            },
        ],
    })
    with pytest.raises(ToolError) as ei:
        await _run_tool(
            monkeypatch, tmp_path, t,
            action="navigate", url="https://trusted.example/redirect",
            blocked_hosts=["169.254.169.254"],
        )
    assert "非公网" in str(ei.value)
    navs = [m for m in t.sent if m["method"] == "Page.navigate"]
    assert navs[0]["params"]["url"] == "https://trusted.example/redirect"
    assert navs[-1]["params"]["url"] == "about:blank", "最终地址必须弹回空白页"


async def test_navigate_public_target_still_works(tmp_path, monkeypatch):
    """公网目标不受 SSRF 防线影响（守卫全放行路径）。"""
    t = FakeTransport({
        "Page.navigate": {},
        "Runtime.evaluate": [
            {"result": {"type": "string", "value": "complete"}},
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({"title": "OK", "url": "https://example.com/"}),
                }
            },
        ],
    })
    out, _stub, _ctx = await _run_tool(
        monkeypatch, tmp_path, t, action="navigate", url="https://example.com"
    )
    assert "已导航" in out


# ---- get_browser 启动路径互斥（跨会话并发不双启动） ----


async def test_get_browser_concurrent_start_starts_once(monkeypatch):
    """两个协程同时进启动窗口：只有一个 start() 落地、双方拿到同一实例。

    真实 start() 在检查与赋值之间有秒级挂起（等 stderr 出调试地址）——这里
    用假 start 人为放大窗口。不加锁时两次 start 都会执行、后完成者覆盖单例。
    """
    started: list[CdpBrowser] = []
    released = asyncio.Event()

    class _AliveProc:
        """最小假进程：is_alive() 需要 proc 非空且 returncode 为 None。"""
        returncode = None

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            return 0

    class SlowStartBrowser(CdpBrowser):
        async def start(self):
            started.append(self)
            await released.wait()  # 挂起：等第二个协程也走进启动窗口
            self.proc = _AliveProc()
            self.ws_url = "ws://127.0.0.1:9222/devtools/browser/guid-x"
            self.profile_dir = "unused"

    monkeypatch.setattr(cdp, "CdpBrowser", SlowStartBrowser)

    async def grab():
        return await cdp.get_browser()

    task_a = asyncio.create_task(grab())
    await asyncio.sleep(0.05)  # 让 A 先进 start 的挂起点
    task_b = asyncio.create_task(grab())
    await asyncio.sleep(0.05)
    assert len(started) == 1, "锁内串行：B 必须等 A 完成，不会同时进 start"
    released.set()
    a, b = await asyncio.gather(task_a, task_b)
    assert a is b, "两个协程必须拿到同一实例（不双启动、不覆盖单例）"
    assert len(started) == 1


# ---- 白名单粒度（gate 层）：web_page 按动作前缀、fill 固化当次参数 ----


def test_rule_for_web_page_is_action_prefix_not_always():
    """「总是允许」粒度回归：web_page 任一动作都不得沉淀整工具 always 规则
    （此前缺席 _ACTION_PREFIX_TOOLS，一次允许=整工具永久放行）。"""
    from skysheep.security.gate import PermissionGate

    tool = WebPageTool()
    for action, args in (
        ("navigate", {"action": "navigate", "url": "https://example.com/"}),
        ("close", {"action": "close"}),
        ("extract", {"action": "extract", "selector": ""}),
        ("click", {"action": "click", "selector": "#go"}),
    ):
        rule = PermissionGate.rule_for(tool, args, None)
        assert rule.tool == "web_page" and rule.kind == "prefix", (action, rule)
        assert rule.pattern == action
        assert rule.matches("web_page", action), "同动作命中"
    # 前缀是完整词：extract 不命中 extractx（_prefix_match 语义）
    always = [r for r in (PermissionGate.rule_for(tool, args, None) for args in
                          ({"action": "navigate", "url": "https://x.example/"},))
              if r.kind == "always"]
    assert always == []


def test_rule_for_web_page_fill_is_exact():
    """fill 的「总是允许」只固化当次选择器与内容（keyboard 同一先例），
    不同内容必须重新询问。"""
    from skysheep.security.gate import PermissionGate

    tool = WebPageTool()
    args = {"action": "fill", "selector": "#pwd", "text": "hunter2"}
    rule = PermissionGate.rule_for(tool, args, None)
    assert rule.kind == "exact"
    assert rule.matches("web_page", tool.arg_text(args))
    other = {"action": "fill", "selector": "#pwd", "text": "different"}
    assert not rule.matches("web_page", tool.arg_text(other)), "内容不同不得命中"
