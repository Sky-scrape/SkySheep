"""多会话并行：两个会话各自独立运行、事件带 session_id 路由、停止/激活按会话。"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.models.base import ProviderDone, ProviderTextDelta
from skysheep.server.backend import ServerBackend


class SlowProvider:
    """每轮输出前先睡一会儿，制造并发窗口。"""

    model = "slow-1"

    def __init__(self) -> None:
        self.turns = 0

    async def stream(self, messages, tools, effort=None):
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


async def test_send_pins_target_session_against_concurrent_activate(home):
    """send 执行期间活动指针被并发激活切走：轮次仍钉在目标会话跑与落库。

    回归（并发正确性）：send 在激活完成后有多个 await，期间另一条
    session.activate 可以改写全局活动指针——修复前 send 之后重读
    self.session，整轮会跑到别的会话的 runtime 里并落库过去。
    """
    be = await _mk(home)
    sid_a = (await be.new_session())["id"]
    sid_b = (await be.new_session())["id"]
    await be.activate_session(sid_b)  # 当前活动 = B；目标 = A（触发激活路径）

    orig_activate = be.activate_session

    async def sneaky_activate(sid):
        r = await orig_activate(sid)
        # 模拟并发请求在 send 的激活窗口里激活了另一个会话：指针最终停在 B
        await orig_activate(sid_b)
        return r

    be.activate_session = sneaky_activate

    evs = []

    async def emit(ev):
        evs.append(ev)

    r = await be.send("给 A 的消息", emit, session_id=sid_a)
    assert r["session_id"] == sid_a
    assert all(e.get("session_id") == sid_a for e in evs), "事件不得路由到别的会话"
    assert any(m.role == "user" and "给 A" in m.text
               for m in be.runtimes[sid_a].agent.history), "消息应落在 A 的历史"
    assert not any(m.role == "user" and "给 A" in m.text
                   for m in be.runtimes[sid_b].agent.history), "不得落到 B 的历史"
    await be.shutdown()


async def test_second_cancel_does_not_skip_turn_cleanup(home, monkeypatch):
    """连点两次停止：收尾期的重复取消被吞掉，注册表恢复与落库必须完成。

    第二个 CancelledError 若从 finally 的 await 里逃逸，plan_mode 的只读
    注册表恢复不执行（runtime 常驻，之后所有轮次都拿残缺工具集跑），
    已产出消息也不落库。
    """
    from skysheep.server.backend import StreamDeltaMerger

    be = await _mk(home)
    orig_aclose = StreamDeltaMerger.aclose

    async def double_cancel_aclose(self):
        asyncio.current_task().cancel()  # 第二次取消落在 finally 的这个 await 上
        await asyncio.sleep(0.01)        # 让取消投递进来
        return await orig_aclose(self)

    monkeypatch.setattr(StreamDeltaMerger, "aclose", double_cancel_aclose)

    evs = []

    async def emit(ev):
        evs.append(ev)

    task = asyncio.create_task(be.send("慢任务", emit, plan_mode=True))
    # 等真正进入流式（首条增量已发出、用户消息已入历史）再取消，
    # 否则取消落在 pipeline 前奏、无产出可落库，测不到收尾
    for _ in range(200):
        if any(e.get("kind") == "text_delta" for e in evs):
            break
        await asyncio.sleep(0.01)
    sid = be.session.id
    task.cancel()  # 第一次取消：流式中被捕获，语义定为 stopped
    r = await task
    assert r["stopped"] is True
    # 只读注册表必须已恢复成完整工具集
    tool_names = {t.name for t in be.runtimes[sid].agent.registry.all()}
    assert "write_file" in tool_names, "plan_mode 结束后注册表必须恢复"
    # 已产出的消息仍要落库
    msgs = await be.store.load_messages(sid)
    assert msgs, "取消轮的收尾落库不应被第二次取消跳过"
    await be.shutdown()


class StopWindowProvider:
    """每轮输出前先睡久一点：给「连点两次停止」留出稳定的注入窗口。"""

    model = "slow-2"

    async def stream(self, messages, tools, effort=None):
        yield ProviderTextDelta("处理中…")
        await asyncio.sleep(0.4)
        yield ProviderDone(input_tokens=5, output_tokens=5)


async def test_double_cancel_on_persist_harvests_before_handoff(home, monkeypatch):
    """连点两次停止、第二刀落在落库 shield 上：必须先收割后台落库再交棒。

    回归（并发收尾不变式）：第二刀 CancelledError 落在 _persist_turn 的
    shield await 上时，后台落库留在独立任务继续跑；若收尾不等它完成就
    _pop_next 交棒，交棒轮自己的落库会与后台旧落库并发「SELECT MAX(seq)+1
    → INSERT」，撞 messages(session_id, seq) 唯一约束——要么交棒轮报
    IntegrityError，要么被停轮的 INSERT 失败被 shield 回调静默吞掉、消息
    无声丢失。用 store 层时序门控复现冲突交错：被停轮第一条消息的 INSERT
    被扣住，交棒轮的落库若在门开前发生，就会算出同一个 seq。
    """
    be = ServerBackend(working_dir=home / "proj", provider_factory=StopWindowProvider)
    await be.setup()
    evs = []

    async def emit(ev):
        evs.append(ev)

    task = asyncio.create_task(be.send("慢任务", emit))
    # 等真正进入流式（首条增量已发出、用户消息已入历史）再取消
    for _ in range(200):
        if any(e.get("kind") == "text_delta" for e in evs):
            break
        await asyncio.sleep(0.01)
    sid = be.session.id

    # 第二条消息进 runtime.queue，等被停轮交棒执行
    task2 = asyncio.create_task(be.send("排队消息", emit))
    for _ in range(200):
        if be.runtimes[sid].queue:
            break
        await asyncio.sleep(0.01)
    assert be.runtimes[sid].queue, "第二轮应排队等待交棒"

    # 时序门控：被停轮的第一条消息 INSERT 扣在门外，同时补第二刀——
    # 外层此刻正 await shield(persist_task)，CancelledError 正好落在那里
    # （persist_task 已在后台独立跑）。
    db = be.store._db
    orig_execute = db.execute
    gate = asyncio.Event()
    held = False

    async def gated_execute(sql, *args, **kwargs):
        nonlocal held
        if (
            isinstance(sql, str)
            and sql.startswith("INSERT INTO messages (")
            and not held
        ):
            held = True
            task.cancel()  # 第二刀
            await gate.wait()
        return await orig_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db, "execute", gated_execute)

    task.cancel()  # 第一次取消：流式中被捕获，语义定为 stopped
    gate_open = False
    try:
        await asyncio.sleep(0.05)
        # 修复后的不变式：后台落库没完成（门未开）就不得交棒——排队轮必须还在
        # 队列里。修复前的收尾会在这里已经把交棒轮拉起来了。
        assert be.runtimes[sid].queue, "后台落库未完成前不得交棒（先落库再交棒）"
        gate.set()  # 放行被停轮的落库
        gate_open = True
        r = await task
        assert r["stopped"] is True
        r2 = await task2
        assert r2["stopped"] is False and r2["session_id"] == sid, "交棒轮必须正常完成"
        msgs = await be.store.load_messages(sid)
        texts = [m.text for m in msgs]
        assert any("慢任务" in t for t in texts), \
            "被停轮的用户消息必须落库（不得无声丢失）"
        assert any("排队消息" in t for t in texts), "交棒轮的用户消息必须落库"
    finally:
        # 断言失败也要收尾：放行门、收割两轮、关后端，不留挂死的资源
        if not gate_open:
            gate.set()
        await asyncio.gather(task, task2, return_exceptions=True)
        await be.shutdown()


async def test_delete_session_waits_for_background_persist(home):
    """delete_session 要等在飞的后台落库完成再删行（等的不是主轮任务）。

    双取消窗口里主轮任务可能已经结束、后台落库还在独立任务里跑；删早了
    消息会插在删除之后留下孤儿行（库未开外键，插得进去）。
    """
    from skysheep.messages import Message, TextBlock
    from skysheep.models.fake import FakeProvider

    be = ServerBackend(working_dir=home / "proj",
                       provider_factory=lambda: FakeProvider([[TextBlock(text="答")]]))
    await be.setup()
    sid = (await be.new_session())["id"]

    # 直接种一个在飞的后台落库（等价于双取消窗口：主轮任务已结束、
    # 落库还挂在 _persist_tasks 里没跑完）
    gate = asyncio.Event()

    async def bg_persist():
        await gate.wait()
        await be.store.append_message(sid, Message.user(text="迟到落库"))
        await be.store.touch(sid)

    bg = asyncio.create_task(bg_persist())
    be._persist_tasks[sid] = bg

    del_task = asyncio.create_task(be.delete_session(sid))
    try:
        for _ in range(30):
            if del_task.done():
                break
            await asyncio.sleep(0.01)
        assert not del_task.done(), "后台落库未完成前，删除必须等它（不能先删行）"

        gate.set()
        await del_task
        # 删除发生在落库完成之后：迟到的消息随会话一起删掉，不留孤儿行
        import sqlite3 as _sq

        con = _sq.connect(home / "home" / "skysheep.db")
        try:
            rows = con.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        finally:
            con.close()
        assert rows == 0, "删除必须覆盖后台落库的消息（不留孤儿行）"
    finally:
        # 断言失败也要收尾：放行门、收割删除任务、关后端
        gate.set()
        await asyncio.gather(del_task, bg, return_exceptions=True)
        await be.shutdown()


async def test_global_concurrency_cap_rejects_new_turns(home, monkeypatch):
    """跨会话并行轮数到达上限后，新起一轮的 send 被可读拒绝；同会话排队不受影响。"""
    be = await _mk(home)
    monkeypatch.setattr(ServerBackend, "MAX_INTERACTIVE_TURNS", 2)

    async def noop(ev):
        pass

    # 背景会话 B、C 各跑一轮（谁都不是必须的「活动会话」）
    sid_b = (await be.new_session())["id"]
    t_b = asyncio.create_task(be.send("B 轮", noop, session_id=sid_b))
    for _ in range(200):
        if be.runtimes.get(sid_b) and be.runtimes[sid_b].run_task:
            break
        await asyncio.sleep(0.01)
    sid_c = (await be.new_session())["id"]
    t_c = asyncio.create_task(be.send("C 轮", noop, session_id=sid_c))
    for _ in range(200):
        if be.runtimes.get(sid_c) and be.runtimes[sid_c].run_task:
            break
        await asyncio.sleep(0.01)
    assert be._running_turn_count() == 2

    # 第三个会话新起一轮：超限，可读拒绝（不入队、不静默）
    sid_d = (await be.new_session())["id"]
    with pytest.raises(RuntimeError, match="并行任务太多"):
        await be.send("D 轮", noop, session_id=sid_d)

    # 同会话排队不受上限影响：排队轮交棒接替刚结束的轮，不增加全局并发
    t_q = asyncio.create_task(be.send("排队轮", noop, session_id=sid_b))
    for _ in range(200):
        if be.runtimes[sid_b].queue:
            break
        await asyncio.sleep(0.01)
    assert be.runtimes[sid_b].queue, "同会话消息应照常排队"

    r_b, r_q, r_c = await asyncio.gather(t_b, t_q, t_c)
    assert r_b["session_id"] == sid_b and r_q["session_id"] == sid_b
    assert r_c["session_id"] == sid_c
    assert be._running_turn_count() == 0
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
