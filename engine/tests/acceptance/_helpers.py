"""验收层共享件：脚本化模型、进程内客户端、真实服务、RPC 与事件断言。

验收层与单测的分工见 docs/测试说明.md：这里只关心「用户看到的结果」，
不 mock 被测模块的内部函数；模型一律用 FakeProvider 脚本回放，
数据目录一律落在 pytest 的 tmp_path。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from pathlib import Path

from fastapi.testclient import TestClient

from skysheep.messages import TextBlock
from skysheep.models.base import Provider, ProviderDone, ProviderTextDelta
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app

FALLBACK = [[TextBlock(text="（备用回答）")]]


class SlowProvider(Provider):
    """按小块慢速吐字，供「流式中途停止」类验收用例在输出中途插手。"""

    name = "fake"
    model = "fake-slow"

    def __init__(self, text: str, chunk: int = 8, delay_s: float = 0.005) -> None:
        super().__init__()
        self.text = text
        self.chunk = chunk
        self.delay_s = delay_s
        self.calls: list[list] = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        for i in range(0, len(self.text), self.chunk):
            yield ProviderTextDelta(self.text[i : i + self.chunk])
            await asyncio.sleep(self.delay_s)
        yield ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7)


def make_client(home: Path, scripts=()) -> TestClient:
    """按 provider_factory 调用顺序消费脚本；脚本项可以是 list 或 Provider 实例。"""
    pool = [p if isinstance(p, Provider) else FakeProvider(p) for p in scripts]

    def factory():
        return pool.pop(0) if pool else FakeProvider([list(FALLBACK[0])])

    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=factory,
    )
    return TestClient(app)


def make_shared_client(home: Path, script) -> TestClient:
    """所有 provider 调用共用同一个 FakeProvider 脚本队列。

    适合「一次操作会派生多个 provider 实例」的场景（定时任务、流水线节点、
    子代理）：按调用顺序消费同一个 script，断言顺序稳定。
    """
    provider = FakeProvider(script)

    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    # 验收用例需要回看「到底给模型喂了什么」时从这里取（依赖释放注入、压缩提示等）
    app.state.acceptance_provider = provider
    return TestClient(app)


def recv_until(ws, wanted_id: str | None = None, events: list | None = None):
    """读帧直到目标 RPC 的响应；途中的事件帧收进 events。"""
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def rpc(ws, mid: str, method: str, params: dict | None = None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    return recv_until(ws, mid)


def run_turn(
    ws,
    mid: str,
    params: dict,
    decisions=("allow_once",),
    events: list | None = None,
):
    """发一条 chat.send 并自动应答途中的权限卡，返回 (最终响应, 事件列表)。

    decisions 按权限请求出现顺序消费；用完后沿用最后一个决策。
    """
    evs: list = [] if events is None else events
    queue = list(decisions)
    last = queue[-1] if queue else "allow_once"
    ws.send_json({"id": mid, "method": "chat.send", "params": params})
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            evs.append(frame)
            if frame["event"] == "permission_request":
                decision = queue.pop(0) if len(queue) > 1 else last
                rid = frame["data"]["request_id"]
                ws.send_json(
                    {
                        "id": "p-" + rid,
                        "method": "permission.respond",
                        "params": {"request_id": rid, "decision": decision},
                    }
                )
            continue
        if frame.get("id") == mid:
            return frame, evs


def kinds(events) -> list[str]:
    return [e.get("event", "") for e in events]


def of_kind(events, kind: str) -> list[dict]:
    return [e["data"] for e in events if e.get("event") == kind]


def first(events, kind: str):
    items = of_kind(events, kind)
    return items[0] if items else None


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):
    """同步轮询直到 predicate() 为真；超时抛 AssertionError（便于定位）。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待条件超时（{timeout}s），最后结果：{last!r}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(home: Path, scripts=()):
    """起真实 uvicorn 服务（随机端口），yield ws:// 地址。

    与 tests/test_ws_e2e.py 的 live_server 同一形态；timeout_graceful_shutdown
    是 Windows 上避免 Proactor 漏 connection_lost 导致挂死的必要参数。
    """
    import uvicorn

    pool = [p if isinstance(p, Provider) else FakeProvider(p) for p in scripts]

    def factory():
        return pool.pop(0) if pool else FakeProvider([list(FALLBACK[0])])

    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=factory,
    )
    port = free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=5,
    )
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
