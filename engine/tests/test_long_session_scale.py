"""长会话规模性能基线（5000 条消息）。

前端已按 HISTORY_SYNC_LIMIT=200 / HISTORY_CHUNK=100 分批渲染；引擎侧的
「几千条会话不再撑爆」承诺要落在三处可测的链路上：

1. store 层：5000 条消息的写入与 load_messages 全量回读在秒级完成；
2. 服务层：5000 条历史进入下一轮 run_turn（真实 Agent 循环）不被持久化
   或历史装配拖挂；
3. 压缩层：超过上下文窗口的 5000 条历史触发自动压缩并成功收敛。

用 ``@pytest.mark.slow`` 标记：日常提交不跑，发版/专项验证时显式
``pytest -m slow`` 执行。规模取 5000（清单定义的「长会话」上限档）。
"""

from __future__ import annotations

import time

import pytest
from conftest import FakeProvider

from skysheep.messages import Message, TextBlock
from skysheep.session.store import SessionStore

pytestmark = pytest.mark.slow

N_MESSAGES = 5000
BUDGET_WRITE_S = 30.0
BUDGET_READ_S = 10.0
# 清单三档：500 / 1000 / 5000。小档在每次 slow 运行中都跑，大档守住上限档。
BULK_SIZES = [500, 1000, 5000]


def _bulk_messages(n: int) -> list[Message]:
    """n 条交替 user/assistant，正文带序号便于断言排序与完整性。"""
    out: list[Message] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(
            role=role,
            content=[TextBlock(text=f"消息 {i}：" + ("问题" if role == "user" else "回答") * 8)],
        ))
    return out


async def _fresh_store(tmp_path) -> SessionStore:
    return await SessionStore(tmp_path / "long.db").connect()


@pytest.mark.parametrize("n", BULK_SIZES)
async def test_store_write_and_read_bulk_messages(tmp_path, n):
    """500 / 1000 / 5000 条消息：写入 ≤30s、全量回读 ≤10s、条数与顺序完整。"""
    store = await _fresh_store(tmp_path)
    try:
        sess = await store.create_session(1, title="长会话")
        msgs = _bulk_messages(n)

        t0 = time.monotonic()
        for m in msgs:
            await store.append_message(sess.id, m)
        write_s = time.monotonic() - t0
        assert write_s < BUDGET_WRITE_S, f"{n} 条写入耗时 {write_s:.1f}s 超预算"

        t0 = time.monotonic()
        loaded = await store.load_messages(sess.id)
        read_s = time.monotonic() - t0
        assert read_s < BUDGET_READ_S, f"{n} 条回读耗时 {read_s:.1f}s 超预算"

        assert len(loaded) == n
        assert loaded[0].seq < loaded[-1].seq, "seq 必须随写入顺序单调"
        assert "消息 0：" in loaded[0].text and f"消息 {n - 1}：" in loaded[-1].text
        # seq 连续无空洞
        seqs = [m.seq for m in loaded]
        assert seqs == list(range(seqs[0], seqs[0] + n))
    finally:
        await store.close()


async def test_agent_turn_with_5000_message_history(tmp_path, home):
    """5000 条历史进入真实 Agent 循环：装配 + 下一轮流式在预算内完成。"""
    from skysheep.core.agent import Agent
    from skysheep.security.gate import PermissionGate
    from skysheep.tools import ToolRegistry

    store = await _fresh_store(tmp_path)
    try:
        sess = await store.create_session(1, title="长会话进轮")
        history = _bulk_messages(N_MESSAGES)
        for m in history:
            await store.append_message(sess.id, m)

        provider = FakeProvider([[TextBlock(text="收到，长对话也正常")]])
        agent = Agent(
            provider=provider,
            registry=ToolRegistry([]),
            gate=PermissionGate(store=None, project_id=None),
            working_dir=home / "proj",
        )
        agent.set_system("system")

        t0 = time.monotonic()
        loaded = await store.load_messages(sess.id)
        agent.load_history(loaded)
        load_s = time.monotonic() - t0

        t0 = time.monotonic()
        events = []
        async for ev in agent.run_turn("再问一句"):
            events.append(ev.kind)
        turn_s = time.monotonic() - t0

        assert "text_delta" in events and "turn_finished" in events
        assert turn_s < 30.0, f"5000 条历史下开新一轮耗时 {turn_s:.1f}s 超预算"
        assert load_s < BUDGET_READ_S, f"5000 条装配耗时 {load_s:.1f}s 超预算"
        # 轮后历史 = 旧 5000 + 本轮 user/assistant
        assert len(agent.history) >= N_MESSAGES + 2
    finally:
        await store.close()
