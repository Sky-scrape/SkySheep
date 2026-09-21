"""真实网络栈的 WebSocket 端到端测试。

现有协议测试全部走 ``fastapi.testclient.TestClient``——那是进程内 ASGI 直调，
不经过真实 socket、握手与分帧。所以「真实网络栈」这一层此前完全没有覆盖：
协议帧的序列化/反序列化、握手校验、长连接下的多次往返，都只能靠手动验证。

这个文件用 uvicorn 监听随机端口 + websockets 客户端真连一次，跑通
boot → chat.send → 事件流 → 完成 的完整链路，覆盖：

- 真实握手通过（脚本客户端不带 Origin，应被放行）；
- boot 快照能从真实 socket 拿到；
- chat.send 的流式事件经真实分帧到达，且最终 assistant 文本拼得回来；
- 连续多轮复用同一条连接（长连接不因一轮结束而失效）。

标记为 e2e：依赖本机端口与事件循环调度，比进程内测试慢，用
``pytest -m e2e`` 单独跑、``pytest -m "not e2e"`` 跳过。
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
    """让操作系统分配一个空闲端口，避免并行跑测试时撞端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home, script):
    """起一个真实 uvicorn 服务（随机端口），yield 出 ws:// 地址。"""
    import uvicorn

    provider = FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        # 等真正开始监听：轮询到端口可连为止
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


async def test_real_socket_boot_and_streaming_chat(home):
    """真实 socket 上跑通 boot → 一轮流式对话。"""
    script = [[TextBlock(text="你好，世界")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True, boot
            assert "session" in (boot.get("result") or {})

            events: list = []
            reply = await _rpc(
                ws, "c1", "chat.send", {"text": "打个招呼"}, events=events
            )
            assert reply.get("ok") is True, reply

            # 流式正文经真实分帧到达，拼回来必须完整
            deltas = [
                e["data"].get("text", "")
                for e in events
                if e.get("event") == "text_delta"
            ]
            assert "".join(deltas) == "你好，世界", deltas
            # 轮次收尾事件一定到达（否则前端会一直显示「运行中」）
            kinds = [e.get("event") for e in events]
            assert "turn_finished" in kinds, kinds


async def test_real_socket_connection_survives_multiple_turns(home):
    """同一条真实连接连续跑两轮：长连接不该在一轮结束后失效。"""
    script = [[TextBlock(text="第一轮")], [TextBlock(text="第二轮")]]
    async with live_server(home, script) as url:
        async with websockets.connect(url) as ws:
            await _rpc(ws, "b1", "boot")

            ev1: list = []
            r1 = await _rpc(ws, "c1", "chat.send", {"text": "一"}, events=ev1)
            assert r1.get("ok") is True, r1
            text1 = "".join(
                e["data"].get("text", "") for e in ev1 if e.get("event") == "text_delta"
            )
            assert text1 == "第一轮", text1

            ev2: list = []
            r2 = await _rpc(ws, "c2", "chat.send", {"text": "二"}, events=ev2)
            assert r2.get("ok") is True, r2
            text2 = "".join(
                e["data"].get("text", "") for e in ev2 if e.get("event") == "text_delta"
            )
            assert text2 == "第二轮", text2


async def test_real_socket_reports_error_for_unknown_method(home):
    """未知方法经真实 socket 返回 ok=false，而不是断开连接。"""
    async with live_server(home, []) as url:
        async with websockets.connect(url) as ws:
            reply = await _rpc(ws, "x1", "definitely.not.a.method")
            assert reply.get("ok") is False
            assert reply.get("error")
            # 连接仍可用
            boot = await _rpc(ws, "b1", "boot")
            assert boot.get("ok") is True
