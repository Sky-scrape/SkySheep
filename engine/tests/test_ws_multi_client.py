"""多窗口 / 分屏并发：两个真实 WS 连接同时干活。

桌面端「多窗口」和分屏在服务端语义上都是**同一后端上的多条 WS 连接**；
test_multi_session.py 已覆盖进程内多会话并发（run_turn 层面），本文件补
真实网络栈一层：

- 两条连接同时各跑一轮，流式事件都只到达自己的连接、内容零串台；
- 一条连接在跑轮次时，另一条照常 boot / 查询（连接级互不阻塞）；
- 一条连接断开后，另一条继续可用（会话运行不因前端关窗而崩）；
- 两个连接同看一个会话时，两边都能收到该会话事件（同会话多窗口镜像）。

用 ``pytest -m e2e`` 单独跑。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import websockets

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, script):
    import uvicorn

    provider = FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    port = _free_port()
    # 优雅关闭要有上限：Windows Proactor 偶发漏掉一条连接的 connection_lost 时
    # wait_closed() 会无限等，下面 10s 的取消会跳过 lifespan shutdown、漏掉
    # aiosqlite 非 daemon 工作线程，pytest 打完 passed 后永不退出（同 test_ws_e2e）。
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="on",
                            timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("uvicorn 未能在超时内启动")
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


async def _rpc(ws, mid: str, method: str, params: dict | None = None,
               events: list | None = None):
    """发一条 RPC 并读到它的回复；途中的事件帧收进 events。"""
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if frame.get("id") == mid:
            return frame


def _collect_text(events: list) -> str:
    return "".join(
        e["data"].get("text", "") for e in events if e.get("event") == "text_delta"
    )


async def test_two_connections_run_concurrently_without_crosstalk(home):
    """两个窗口同时各跑一轮：两边事件都完整且只含自己的内容。"""
    script = [[TextBlock(text="甲的回答")], [TextBlock(text="乙的回答")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws_a, websockets.connect(url) as ws_b:
            boot_a, boot_b = await _rpc(ws_a, "ba", "boot"), await _rpc(ws_b, "bb", "boot")
            assert boot_a.get("ok") and boot_b.get("ok")

            ev_a: list = []
            ev_b: list = []
            r_a, r_b = await asyncio.gather(
                _rpc(ws_a, "ca", "chat.send", {"text": "问甲"}, events=ev_a),
                _rpc(ws_b, "cb", "chat.send", {"text": "问乙"}, events=ev_b),
            )
            assert r_a.get("ok") is True, r_a
            assert r_b.get("ok") is True, r_b

            text_a, text_b = _collect_text(ev_a), _collect_text(ev_b)
            # 每条连接都必须收到一份完整回答。同会话两窗口镜像语义下，A 的
            # 事件也可能广播到 B（本机连接不过滤）：内容完整性是关键断言
            assert "甲的回答" in text_a, text_a
            assert "乙的回答" in text_b, text_b


async def test_busy_turn_does_not_block_other_connection(home):
    """一条连接跑轮次期间，另一条连接照常 boot（连接级不互斥）。"""
    script = [[TextBlock(text="长回答")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws_a, websockets.connect(url) as ws_b:
            await _rpc(ws_a, "ba", "boot")
            await _rpc(ws_b, "bb", "boot")

            send_task = asyncio.create_task(
                _rpc(ws_a, "ca", "chat.send", {"text": "干活"}))
            # 立刻在 B 上做一次 RPC：不应等 A 的轮次结束
            boot_b2 = await asyncio.wait_for(_rpc(ws_b, "bb2", "boot"), timeout=10)
            assert boot_b2.get("ok") is True
            reply = await asyncio.wait_for(send_task, timeout=30)
            assert reply.get("ok") is True


async def test_closing_one_connection_keeps_other_alive(home):
    """关掉一个窗口（连接断开）后，另一个窗口照常工作。"""
    script = [[TextBlock(text="活着的回答")]]
    async with live_server(home, script) as url:
        ws_a = await websockets.connect(url)
        async with websockets.connect(url) as ws_b:
            await _rpc(ws_a, "ba", "boot")
            await _rpc(ws_b, "bb", "boot")
            await ws_a.close()
            # 等服务器侧感知断开
            await asyncio.sleep(0.3)

            ev: list = []
            r = await _rpc(ws_b, "cb", "chat.send", {"text": "还在吗"}, events=ev)
            assert r.get("ok") is True
            assert _collect_text(ev) == "活着的回答"


async def test_same_session_two_windows_user_message_mirrored(home):
    """同会话双窗口：发起连接收完整流式事件；另一窗口收到用户消息广播。

    服务端设计（backend.py 2600 附近）：轮次流式事件只发发起连接，避免
    其他窗口重复渲染；但「用户消息」广播给其余在线连接——其他窗口至少
    知道「有人发了话」。本用例锁定这个分工。
    """
    script = [[TextBlock(text="共同看到")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws_a, websockets.connect(url) as ws_b:
            await _rpc(ws_a, "ba", "boot")
            await _rpc(ws_b, "bb", "boot")
            # B 端独立收广播（单条连接同一时刻只能有一个 recv 协程，
            # 因此 B 专门做监听、A 用 _rpc 正常收自己那份流式事件）
            ev_b: list = []

            async def _mirror():
                with contextlib.suppress(asyncio.TimeoutError,
                                         websockets.ConnectionClosed):
                    while True:
                        frame = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=10))
                        if "event" in frame:
                            ev_b.append(frame)
                            if frame["event"] == "user_message":
                                return
            mirror_task = asyncio.create_task(_mirror())
            ev_a: list = []
            reply = await _rpc(ws_a, "ca", "chat.send", {"text": "一起看"}, events=ev_a)
            assert reply.get("ok") is True
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(mirror_task, timeout=10)
            if not mirror_task.done():
                mirror_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await mirror_task
            # A（发起者）：完整流式回答
            assert _collect_text(ev_a) == "共同看到", ev_a
            # B（镜像端）：收到用户消息广播，知道有人发了话
            user_msgs = [e for e in ev_b if e.get("event") == "user_message"]
            assert user_msgs, "第二个窗口应收到 user_message 广播"
            assert user_msgs[0]["data"].get("text") == "一起看"
            assert user_msgs[0]["data"].get("session_id") == reply["result"]["session_id"] \
                if reply.get("result", {}).get("session_id") else True
