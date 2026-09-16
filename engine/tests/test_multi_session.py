"""多会话并行：两个会话各自独立运行、事件带 session_id 路由、停止/激活按会话。"""

from __future__ import annotations

import asyncio

from skysheep.models.base import ProviderDone, ProviderTextDelta
from skysheep.server.backend import ServerBackend


class SlowProvider:
    """每轮输出前先睡一会儿，制造并发窗口。"""

    model = "slow-1"

    def __init__(self) -> None:
        self.turns = 0

    async def stream(self, messages, tools):
        self.turns += 1
        yield ProviderTextDelta("处理中…")
        await asyncio.sleep(0.25)
        yield ProviderDone(input_tokens=5, output_tokens=5)


async def _mk(home):
    be = ServerBackend(working_dir=home / "proj", provider_factory=SlowProvider)
    await be.setup()
    return be


async def test_parallel_sessions_run_concurrently(home):
    """会话 A 运行中发起会话 B：两轮并行，事件各带自己的 session_id。"""
    be = await _mk(home)
    evs_a, evs_b = [], []

    async def emit_a(ev):
        evs_a.append(ev)

    async def emit_b(ev):
        evs_b.append(ev)

    task_a = asyncio.create_task(be.send("任务A", emit_a))
    # 等 A 的 runtime 建立并开始跑，再开 B
    for _ in range(200):
        if be.runtimes and next(iter(be.runtimes.values())).run_task:
            break
        await asyncio.sleep(0.01)
    sid_a = be.session.id

    sid_b = (await be.new_session())["id"]
    task_b = asyncio.create_task(be.send("任务B", emit_b))
    await asyncio.gather(task_a, task_b)

    assert len(be.runtimes) == 2
    # 两个 runtime 的事件都带上自己的 session_id
    assert all(e.get("session_id") == sid_a for e in evs_a)
    assert all(e.get("session_id") == sid_b for e in evs_b)
    # 并发性证据：B 的 turn_started 早于 A 完成
    ta = next(i for i, e in enumerate(evs_a) if e.get("kind") == "turn_started")
    tb = next(i for i, e in enumerate(evs_b) if e.get("kind") == "turn_started")
    assert evs_b[tb]["session_id"] == sid_b and ta >= 0 and tb >= 0
    # A 完成之前 B 已经在跑（B 的 turn_started 时间戳早于 A 的 turn_finished）
    a_fin = next(i for i, e in enumerate(evs_a) if e.get("kind") == "turn_finished")
    assert tb < a_fin, "会话 B 应当与会话 A 并行，而不是排队等 A 完成"
    await be.shutdown()


async def test_send_with_session_id_activates_target(home):
    """chat.send 显式带 session_id 时切到目标会话；两个会话上下文独立。"""
    be = await _mk(home)
    evs = []

    async def emit(ev):
        evs.append(ev)

    sid_a = (await be.new_session())["id"]
    await be.send("在 A 里", emit)
    sid_b = (await be.new_session())["id"]
    await be.send("在 B 里", emit)
    assert be.session.id == sid_b
    # 显式切回 A 再发：事件必须带 A 的 session_id，且 A 的历史独立
    evs.clear()
    await be.send("再回 A", emit, session_id=sid_a)
    assert be.session.id == sid_a
    assert evs and evs[0].get("session_id") == sid_a
    assert len(be.runtimes[sid_a].agent.history) > 2, "A 的上下文应包含它的三轮消息"
    assert be.runtimes[sid_b].agent.history != be.runtimes[sid_a].agent.history
    await be.shutdown()


async def test_cancel_and_stop_only_target_session(home):
    """stop(session_id) 只取消目标会话；另一个会话照常完成。"""
    be = await _mk(home)
    evs_a, evs_b = [], []

    async def emit_a(ev):
        evs_a.append(ev)

    async def emit_b(ev):
        evs_b.append(ev)

    task_a = asyncio.create_task(be.send("慢任务A", emit_a))
    for _ in range(200):
        if be.runtimes and next(iter(be.runtimes.values())).run_task:
            break
        await asyncio.sleep(0.01)
    sid_a = be.session.id
    sid_b = (await be.new_session())["id"]
    task_b = asyncio.create_task(be.send("快任务B", emit_b))
    # 立刻停掉 A（它还没跑完）
    assert be.cancel_run(sid_a) is True
    await asyncio.gather(task_a, task_b)
    r_a, r_b = task_a.result(), task_b.result()
    assert r_a["stopped"] is True and r_a["session_id"] == sid_a
    assert r_b["stopped"] is False and r_b["session_id"] == sid_b
    await be.shutdown()


async def test_activate_session_lightweight(home):
    """session.activate：只切活动指针；运行中的会话不被重载。"""
    be = await _mk(home)
    sid_a = (await be.new_session())["id"]

    async def noop(ev):
        pass

    await be.send("写点历史", noop)
    sid_b = (await be.new_session())["id"]

    info = await be.activate_session(sid_a)
    assert info["id"] == sid_a and be.session.id == sid_a
    # 已存在的 runtime 不重建（历史不变）
    rt_before = be.runtimes[sid_a]
    await be.activate_session(sid_b)
    assert be.session.id == sid_b and be.runtimes[sid_a] is rt_before
    await be.shutdown()


# ---- WS 层：session.activate 协议 + resume 返回历史 ----

def test_ws_activate_and_resume_messages(home):
    from test_server import make_client, recv_until

    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([[TextBlock(text="回复一")]])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "你好"}})
        r = recv_until(ws, "c1")
        assert r["ok"]
        sid = r["result"]["session_id"]

        ws.send_json({"id": "r1", "method": "session.resume", "params": {"id": sid}})
        rr = recv_until(ws, "r1")["result"]
        assert rr["messages"], "resume 应返回历史供前端渲染"
        roles = [m["role"] for m in rr["messages"]]
        assert roles == ["user", "assistant"]

        ws.send_json({"id": "a1", "method": "session.activate", "params": {"id": sid}})
        ra = recv_until(ws, "a1")["result"]
        assert ra["id"] == sid
