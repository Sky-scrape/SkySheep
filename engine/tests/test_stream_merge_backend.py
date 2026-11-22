"""ServerBackend 级流式收尾 flush 三路径（批 5a 复核发现的测试缺口）。

``tests/test_stream_merge.py`` 只直接构造 ``StreamDeltaMerger``，全文件不出现
``ServerBackend``——它锚得住合并语义，却锚不住 ``_run_turn_pipeline`` 在
轮末 / 异常 / 取消三条路径上对 ``merger.aclose()`` 的调用。漏掉任何一条都会
让回答缺尾，所以这里从真实 ``send()`` 路径出发，断言三条路径下已产出的正文
都到达 emit。

定时冲刷在本文件里被关掉（``_schedule_flush`` 置空），缓冲只可能由收尾
flush 放出——这样断言的是 finally，而不是 40ms 计时器的时序。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.models.base import ProviderDone, ProviderTextDelta
from skysheep.server.backend import ServerBackend, StreamDeltaMerger


class TwoChunkProvider:
    """两段增量：第一段立即发出，第二段进缓冲（定时冲刷已关闭）。"""

    model = "two-chunk"

    def __init__(self, *, hang_after: bool = False) -> None:
        self.hang_after = hang_after
        self.second_sent = asyncio.Event()

    async def stream(self, messages, tools, effort=None):
        yield ProviderTextDelta("第一段")
        yield ProviderTextDelta("第二段")
        self.second_sent.set()
        if self.hang_after:
            await asyncio.Event().wait()  # 永不返回，等取消
        yield ProviderDone(input_tokens=4, output_tokens=4)


def _joined(evs: list[dict]) -> str:
    return "".join(e.get("text", "") for e in evs if e.get("kind") == "text_delta")


async def _mk(home, provider, monkeypatch) -> tuple[ServerBackend, list[dict], list[dict]]:
    monkeypatch.setattr(StreamDeltaMerger, "_schedule_flush", lambda self: None)
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: provider)
    await be.setup()
    evs: list[dict] = []

    async def emit(ev):
        evs.append(ev)

    return be, evs, emit


async def test_turn_end_flush_keeps_buffered_tail(home, monkeypatch):
    """轮末路径：缓冲里的第二段在轮结束后必须已到达 emit。"""
    be, evs, emit = await _mk(home, TwoChunkProvider(), monkeypatch)
    try:
        r = await be.send("你好", emit)
        assert r.get("stopped") is not True
        assert _joined(evs) == "第一段第二段", "轮末 flush 丢了缓冲增量"
    finally:
        await be.shutdown()


async def test_exception_flush_keeps_buffered_tail(home, monkeypatch):
    """异常路径：流式循环外抛的异常逃出 pipeline 前，缓冲正文仍要冲刷出来。

    provider 自身抛错会被 Agent 收敛成 error 事件（非异常路径）；这里直接让
    ``Agent.run_turn`` 在产出两段增量后抛错，走 ``_run_turn_pipeline`` 的
    ``except Exception`` → ``finally`` → ``merger.aclose()``。
    """
    from skysheep.core.agent import Agent
    from skysheep.events import TextDelta

    async def boom_turn(self, text, images=None, append_user=True):
        yield TextDelta(text="第一段")
        yield TextDelta(text="第二段")
        raise RuntimeError("pipeline 炸了")

    be, evs, emit = await _mk(home, TwoChunkProvider(), monkeypatch)
    monkeypatch.setattr(Agent, "run_turn", boom_turn)
    try:
        with pytest.raises(RuntimeError, match="pipeline 炸了"):
            await be.send("你好", emit)
        assert _joined(evs) == "第一段第二段", "异常路径 flush 丢了缓冲增量"
    finally:
        await be.shutdown()


async def test_cancel_flush_keeps_buffered_tail(home, monkeypatch):
    """取消路径：取消轮把缓冲里已产出的正文冲刷出来（回答不缺尾）。"""
    provider = TwoChunkProvider(hang_after=True)
    be, evs, emit = await _mk(home, provider, monkeypatch)
    try:
        task = asyncio.create_task(be.send("你好", emit))
        await asyncio.wait_for(provider.second_sent.wait(), timeout=5)
        task.cancel()
        r = await task
        assert r.get("stopped") is True
        assert _joined(evs) == "第一段第二段", "取消路径 flush 丢了缓冲增量"
    finally:
        await be.shutdown()
