"""接力运行期（无工具串行 A/B）后端级测试：真实 backend + 隔离 home + FakeProvider。

覆盖：串行闭环、工具调用拒绝、确认幂等/过期 revision、暂停与迟到返回、
resume 不暗中重跑、预算、重启中断、第二后端独占、项目归属、普通聊天不受影响。
无真实 key / 无真实用户数据：provider 全部注入 FakeProvider 或本地假 provider。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from conftest import FakeProvider

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.base import (
    Provider,
    ProviderDone,
    ProviderTextDelta,
    ProviderToolUse,
)
from skysheep.server.backend import ServerBackend


async def _mk(home, prov) -> ServerBackend:
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov)
    await be.setup()
    return be


class RecProvider(Provider):
    """记录 (messages, tool_schemas) 的假 provider；脚本组耗尽后吐固定文本。"""

    name = "fake"
    model = "fake-rec"

    def __init__(self, groups, default="收尾") -> None:
        super().__init__()
        self.groups = list(groups)
        self.default = default
        self.calls: list[tuple[list, list]] = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append((list(messages), list(tool_schemas)))
        blocks = self.groups.pop(0) if self.groups else [TextBlock(text=self.default)]
        tool = False
        for b in blocks:
            if isinstance(b, TextBlock):
                yield ProviderTextDelta(b.text)
            else:
                tool = True
                yield ProviderToolUse(id=b.id, name=b.name, input=b.input)
        yield ProviderDone(
            stop_reason="tool_use" if tool else "end_turn",
            input_tokens=11, output_tokens=7,
        )


class GatedProvider(Provider):
    """阻塞到测试放行才产出文本；用于制造「迟到的模型返回」。"""

    name = "fake"
    model = "fake-gated"

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        self.started.set()
        await self.gate.wait()
        yield ProviderTextDelta(self.text)
        yield ProviderDone(stop_reason="end_turn", input_tokens=5, output_tokens=5)


def _workflow(**over) -> dict:
    wf = {
        "start_node": "n1",
        "nodes": [
            {"id": "n1", "slot": "A", "prompt": "第一节点：研究", "terminal": False},
            {"id": "n2", "slot": "B", "prompt": "第二节点：复核", "terminal": True},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }
    wf.update(over)
    return wf


async def _start(be, sid, goal="调研并给出建议", request_id="s1", workflow=None) -> dict:
    return await be.relay_start({
        "session_id": sid, "goal": goal,
        "workflow": workflow or _workflow(),
        "request_id": request_id, "expected_revision": 0,
    })


def _node(snap: dict, node_id: str) -> dict:
    return next(n for n in snap["nodes"] if n["node_id"] == node_id)


async def _wait_snapshot(be, task_id, state, timeout=3.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = await be.store.relay_get(task_id)
        if snap and snap["task"]["state"] == state:
            return snap
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout}s 内进入状态 {state}")


async def _wait_node_running(be, task_id, node_id, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = await be.store.relay_get(task_id)
        node = _node(snap, node_id) if snap else None
        if node and node["status"] == "running":
            return snap
        await asyncio.sleep(0.01)
    raise AssertionError("节点未进入 running")


async def _await_worker(be, task_id):
    worker = be._relay_state()["workers"].get(task_id)
    if worker is not None:
        await asyncio.wait_for(asyncio.shield(worker), timeout=3.0)


async def test_serial_ab_full_flow(home):
    prov = FakeProvider([[TextBlock(text="A 研究报告")], [TextBlock(text="B 复核结论")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        assert snap["task"]["state"] == "running"

        snap = await _wait_snapshot(be, task_id, "waiting_confirmation")
        n1 = _node(snap, "n1")
        assert n1["status"] == "reported"
        assert n1["report"] == "A 研究报告"
        assert n1["report_hash"] and n1["report_id"]
        assert snap["task"]["step"] == 1
        assert snap["task"]["active_slot"] == "A"

        await be.relay_approve({
            "task_id": task_id, "request_id": "a1",
            "expected_revision": snap["task"]["revision"],
            "report_id": n1["report_id"], "report_hash": n1["report_hash"],
        })
        snap = await _wait_snapshot(be, task_id, "completed")
        n2 = _node(snap, "n2")
        assert n2["report"] == "B 复核结论"
        assert n2["status"] == "reported"
        assert snap["task"]["step"] == 2
        # 第二节点收到含第一节点报告的交接材料（纯文本协商，不是伪装用户消息）
        assert len(prov.calls) == 2
        second = "".join(m.text for m in prov.calls[1] if m.role != "system")
        assert "A 研究报告" in second
    finally:
        await be.shutdown()


async def test_tool_use_is_protocol_error_and_pauses(home):
    prov = RecProvider([[ToolUseBlock(id="t1", name="run_command", input={"cmd": "rm -rf /"})]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        snap = await _wait_snapshot(be, task_id, "paused")
        assert snap["task"]["error"]["code"] == "relay_protocol_error"
        # 无工具是结构性保证：发给 provider 的工具 schema 恒为空列表
        assert prov.calls and prov.calls[0][1] == []
        # 未产出报告，节点不得推进
        assert _node(snap, "n1")["status"] == "running"
        assert snap["task"]["step"] == 0
    finally:
        await be.shutdown()


async def test_approve_idempotent_wrong_revision_and_report_binding(home):
    prov = FakeProvider([[TextBlock(text="A 报告")], [TextBlock(text="B 报告")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid, request_id="start-1")
        task_id = snap["task"]["id"]
        snap = await _wait_snapshot(be, task_id, "waiting_confirmation")
        rev = snap["task"]["revision"]
        n1 = _node(snap, "n1")

        with pytest.raises(ValueError, match="relay_conflict"):
            await be.relay_approve({
                "task_id": task_id, "request_id": "bad-rev",
                "expected_revision": rev + 9,
                "report_id": n1["report_id"], "report_hash": n1["report_hash"],
            })
        with pytest.raises(ValueError, match="哈希不符|报告已变化"):
            await be.relay_approve({
                "task_id": task_id, "request_id": "bad-hash",
                "expected_revision": rev,
                "report_id": n1["report_id"], "report_hash": "deadbeef",
            })

        ok = await be.relay_approve({
            "task_id": task_id, "request_id": "a1",
            "expected_revision": rev,
            "report_id": n1["report_id"], "report_hash": n1["report_hash"],
        })
        assert ok["task"]["id"] == task_id
        # 同一 request_id 重放：返回已存结果，不重复推进
        replay = await be.relay_approve({
            "task_id": task_id, "request_id": "a1",
            "expected_revision": rev,
            "report_id": n1["report_id"], "report_hash": n1["report_hash"],
        })
        assert replay.get("replayed") is True
        # 启动幂等：同一 start request_id 返回同一任务
        again = await _start(be, sid, request_id="start-1")
        assert again["task"]["id"] == task_id
        await _wait_snapshot(be, task_id, "completed")
    finally:
        await be.shutdown()


async def test_pause_and_late_return_do_not_advance(home):
    prov = GatedProvider("迟到的报告")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        snap = await _wait_node_running(be, task_id, "n1")
        rev = snap["task"]["revision"]
        await be.relay_pause({
            "task_id": task_id, "request_id": "p1", "expected_revision": rev,
        })
        prov.gate.set()  # 迟到返回：暂停后模型才产出
        await _await_worker(be, task_id)
        snap = await be.store.relay_get(task_id)
        assert snap["task"]["state"] == "paused"
        assert _node(snap, "n1")["status"] == "running"  # 迟到返回不得让节点推进
        assert _node(snap, "n1")["report"] == ""
        assert prov.calls == 1
    finally:
        await be.shutdown()


async def test_resume_does_not_silently_rerun_node(home):
    prov = GatedProvider("报告")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        snap = await _wait_node_running(be, task_id, "n1")
        await be.relay_pause({
            "task_id": task_id, "request_id": "p1",
            "expected_revision": snap["task"]["revision"],
        })
        prov.gate.set()
        await _await_worker(be, task_id)

        snap = await be.store.relay_get(task_id)
        rev = snap["task"]["revision"]
        await be.relay_resume({
            "task_id": task_id, "request_id": "r1", "expected_revision": rev,
        })
        await _await_worker(be, task_id)
        snap = await be.store.relay_get(task_id)
        # resume 不许暗中重跑未完成的模型节点：停在等待显式批准
        assert snap["task"]["state"] == "waiting_confirmation"
        assert prov.calls == 1
        node = _node(snap, "n1")
        assert node["status"] == "running"

        # 显式批准（重试）才会真正再跑一次
        await be.relay_approve({
            "task_id": task_id, "request_id": "retry-1",
            "expected_revision": snap["task"]["revision"],
            "report_id": "", "report_hash": "",
        })
        snap = await _wait_snapshot(be, task_id, "waiting_confirmation")
        assert _node(snap, "n1")["status"] == "reported"
        assert prov.calls == 2
    finally:
        await be.shutdown()


async def test_token_budget_pauses(home):
    prov = FakeProvider([[TextBlock(text="A")], [TextBlock(text="B")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid, workflow=_workflow(token_budget=1024))
        task_id = snap["task"]["id"]
        snap = await _wait_snapshot(be, task_id, "paused")
        assert snap["task"]["error"]["code"] == "relay_token_budget"
        assert prov.calls == []  # 预算不足时不发请求
    finally:
        await be.shutdown()


async def test_restart_marks_task_interrupted(home):
    prov = GatedProvider("报告")
    be = await _mk(home, prov)
    sid = (await be.new_session())["id"]
    snap = await _start(be, sid)
    task_id = snap["task"]["id"]
    await _wait_node_running(be, task_id, "n1")
    gen_before = (await be.store.relay_get_task(task_id))["generation"]
    await be.shutdown()  # 模拟进程退出（worker 被取消）

    prov2 = FakeProvider([[TextBlock(text="新报告")]])
    be2 = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov2)
    await be2.setup()
    try:
        task = await be2.store.relay_get_task(task_id)
        assert task["state"] == "interrupted"
        assert task["generation"] == gen_before + 1  # 旧执行代际作废
        # 不自动重跑：没有新模型调用
        assert prov2.calls == []
    finally:
        await be2.shutdown()


async def test_second_backend_does_not_interrupt_live_task(home):
    prov = GatedProvider("报告")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        await _wait_node_running(be, task_id, "n1")

        prov2 = FakeProvider([[TextBlock(text="x")]])
        be2 = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov2)
        await be2.setup()
        try:
            assert be2._relay_state()["locked"] is False
            # 第二后端不得打断活任务，也不得启动新任务
            task = await be2.store.relay_get_task(task_id)
            assert task["state"] == "running"
            with pytest.raises(RuntimeError, match="relay_locked"):
                await _start(be2, sid, request_id="s2")
        finally:
            await be2.shutdown()
    finally:
        prov.gate.set()
        await be.shutdown()


async def test_task_project_ownership_enforced(home):
    prov = FakeProvider([[TextBlock(text="A")], [TextBlock(text="B")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        other = await be.store.get_or_create_project(str(home / "other-pkg"))
        be.project = other  # 切到别的项目
        with pytest.raises(ValueError, match="relay_not_found"):
            await be.relay_get({"task_id": task_id})
        with pytest.raises(ValueError, match="relay_not_found"):
            await be.relay_approve({
                "task_id": task_id, "request_id": "x", "expected_revision": 0,
            })
    finally:
        await be.shutdown()


async def test_quick_chat_session_starts_under_current_project(home):
    """快聊会话（无项目归属）也能启动接力：任务挂在当前项目下。

    回归：此前按 session.project_id 与当前项目严格比对，侧栏「快聊」里点开的
    会话一律报 relay_forbidden，用户没有任何办法启动接力。
    """
    prov = FakeProvider([[TextBlock(text="A")], [TextBlock(text="B")]])
    be = await _mk(home, prov)
    try:
        assert be.project is not None  # _mk 已把启动目录登记成项目
        quick = await be.store.create_session(None, title="快聊")
        snap = await _start(be, quick.id, request_id="quick-1")
        assert snap["task"]["project_id"] == be.project.id
        assert snap["task"]["session_id"] == quick.id
        # 归属校验按任务所属项目走：当前项目里能读到它
        assert (await be.relay_get({"task_id": snap["task"]["id"]}))["task"]["id"] == snap["task"]["id"]
    finally:
        await be.shutdown()


async def test_cross_project_session_still_rejected(home):
    """属于别的项目的会话仍在启动时拒绝，并指出该切到哪个项目。"""
    prov = FakeProvider([[TextBlock(text="A")]])
    be = await _mk(home, prov)
    try:
        other = await be.store.get_or_create_project(str(home / "other-pkg"))
        foreign = await be.store.create_session(other.id, title="别的项目")
        with pytest.raises(ValueError, match="relay_forbidden.*other-pkg"):
            await _start(be, foreign.id)
        # 拒绝发生在建任务之前：当前项目下没有留下残留任务
        assert await be.store.relay_list_tasks(be._cur_project_id()) == []
    finally:
        await be.shutdown()


async def test_node_retries_are_bounded(home):
    # 节点持续失败时只在 max_retries 内重跑，超过即暂停等人工处理
    prov = RecProvider([
        [ToolUseBlock(id="t1", name="run_command", input={"cmd": "x"})],
        [ToolUseBlock(id="t2", name="run_command", input={"cmd": "x"})],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid, workflow=_workflow(max_retries=1))
        task_id = snap["task"]["id"]
        snap = await _wait_snapshot(be, task_id, "paused")
        assert snap["task"]["error"]["code"] == "relay_protocol_error"
        assert _node(snap, "n1")["attempt"] == 1

        await be.relay_resume({
            "task_id": task_id, "request_id": "r1",
            "expected_revision": snap["task"]["revision"],
        })
        snap = await _wait_snapshot(be, task_id, "waiting_confirmation")
        await be.relay_approve({
            "task_id": task_id, "request_id": "retry", "expected_revision": snap["task"]["revision"],
            "report_id": "", "report_hash": "",
        })
        snap = await _wait_snapshot(be, task_id, "paused")
        assert snap["task"]["error"]["code"] == "relay_max_retries"
        assert len(prov.calls) == 1  # 超出上限后不再发请求
    finally:
        await be.shutdown()


async def test_concurrent_stop_and_approve_only_one_wins(home):
    prov = FakeProvider([[TextBlock(text="A 报告")], [TextBlock(text="B 报告")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        snap = await _wait_snapshot(be, task_id, "waiting_confirmation")
        rev = snap["task"]["revision"]
        n1 = _node(snap, "n1")

        approve = asyncio.create_task(be.relay_approve({
            "task_id": task_id, "request_id": "race-approve", "expected_revision": rev,
            "report_id": n1["report_id"], "report_hash": n1["report_hash"],
        }))
        stop = asyncio.create_task(be.relay_stop({
            "task_id": task_id, "request_id": "race-stop", "expected_revision": rev,
        }))
        results = await asyncio.gather(approve, stop, return_exceptions=True)
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(winners) == 1 and len(losers) == 1
        assert "relay_conflict" in str(losers[0])
        # 不产生第二执行者：只有一个控制提交，状态停留在合法单路径结果上。
        # 停止路径允许先落 stopping（提交时 worker 尚未退出），但必须自行收敛
        # 到 stopped——卡在「正在停止」比多推进一步更糟，所以这里轮询而不是
        # 抓取瞬间快照。
        legal = ("running", "waiting_confirmation", "completed", "stopped")
        end = time.monotonic() + 3.0
        snap = await be.store.relay_get(task_id)
        while time.monotonic() < end and snap["task"]["state"] not in legal:
            await asyncio.sleep(0.01)
            snap = await be.store.relay_get(task_id)
        assert snap["task"]["state"] in legal
    finally:
        await be.shutdown()


async def test_stop_during_running_finalizes_stopped(home):
    """节点执行中停止：worker 在模型返回后必须把 stopping 收尾成 stopped。

    回归点：worker 曾在节点执行后直接 return，任务永远停在 stopping。
    """
    prov = GatedProvider("迟到的报告")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        snap = await _wait_node_running(be, task_id, "n1")

        stopped = await be.relay_stop({
            "task_id": task_id, "request_id": "stop-mid",
            "expected_revision": snap["task"]["revision"],
        })
        assert stopped["task"]["state"] == "stopping"

        prov.gate.set()  # 迟到返回：不得写入报告，也不得卡在 stopping
        await _await_worker(be, task_id)
        final = await be.store.relay_get(task_id)
        assert final["task"]["state"] == "stopped"
        assert _node(final, "n1")["report"] == ""
        assert prov.calls == 1
    finally:
        prov.gate.set()
        await be.shutdown()


async def test_broadcast_does_not_reach_remote_connections(home):
    """接力快照含目标与报告：只推本机连接，远端连接收不到（控制 RPC 本就 local_only）。"""
    prov = GatedProvider("报告")
    be = await _mk(home, prov)
    try:
        local_frames: list = []
        remote_frames: list = []

        async def local_emit(ev):
            local_frames.append(ev)

        async def remote_emit(ev):
            remote_frames.append(ev)

        be.ws_emitters.extend([local_emit, remote_emit])
        be.ws_connections.extend([
            {"emit": local_emit, "local": True},
            {"emit": remote_emit, "local": False},
        ])

        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        snap = await _wait_node_running(be, task_id, "n1")
        await be.relay_pause({
            "task_id": task_id, "request_id": "p1",
            "expected_revision": snap["task"]["revision"],
        })
        assert any(ev.get("kind") == "relay_state" for ev in local_frames)
        assert remote_frames == []
    finally:
        prov.gate.set()
        await be.shutdown()


async def test_per_project_task_limit_isolated_across_projects(home):
    """「每项目最多一个非终态接力」按项目隔离，不是全局唯一。

    项目 A 起任务后切到项目 B：B 能正常起自己的任务，两者同时活着；
    切回 A 再起仍被拒，且 B 的任务在 A 的归属校验下读不到（反之亦然）。
    """
    prov = FakeProvider([[TextBlock(text="A 报告")], [TextBlock(text="B 报告")]])
    be = await _mk(home, prov)
    try:
        proj_a = be.project
        sid = (await be.new_session())["id"]
        snap_a = await _start(be, sid, request_id="a-start")
        task_a = snap_a["task"]["id"]

        (home / "proj-b").mkdir()
        be.project = await be.store.get_or_create_project(str(home / "proj-b"))
        sid_b = (await be.new_session())["id"]
        snap_b = await _start(be, sid_b, request_id="b-start")
        task_b = snap_b["task"]["id"]
        assert task_b != task_a
        # 两个项目各有一份非终态任务：隔离生效
        assert await be.store.relay_get_task(task_a) is not None
        assert (await be.store.relay_get_task(task_a))["state"] not in ("stopped", "completed")
        assert (await be.store.relay_get_task(task_b))["project_id"] == be.project.id

        # B 里再起被拒（本项目已有未结束任务）
        with pytest.raises(ValueError, match="relay_conflict"):
            await _start(be, sid_b, request_id="b-again")

        # 切回 A：A 的任务仍活着，再起同样被拒；B 的任务在 A 下不可见
        be.project = proj_a
        with pytest.raises(ValueError, match="relay_conflict"):
            await _start(be, sid, request_id="a-again")
        with pytest.raises(ValueError, match="relay_not_found"):
            await be.relay_get({"task_id": task_b})
    finally:
        await be.shutdown()


async def test_concurrent_starts_create_at_most_one_task(home):
    """并发启动同一项目：同 request_id 幂等复用，不同 request_id 最多成功一个。

    「每项目最多一个非终态任务」是检查后创建（check-then-act），并发下不能
    靠两次独立读各自放行——否则会留下两条非终态任务，之后谁都无法再启动。
    """
    prov = FakeProvider([[TextBlock(text="报告")], [TextBlock(text="报告")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        # 同 request_id：两个并发 start 必须落到同一个任务
        r1, r2 = await asyncio.gather(
            _start(be, sid, request_id="same"),
            _start(be, sid, request_id="same"),
        )
        assert r1["task"]["id"] == r2["task"]["id"]
        tasks = await be.store.relay_list_tasks(be._cur_project_id(), limit=100)
        assert len(tasks) == 1

        # 已有非终态任务时，不同 request_id 的并发 start 全部被拒
        results = await asyncio.gather(
            _start(be, sid, request_id="x1"),
            _start(be, sid, request_id="x2"),
            return_exceptions=True,
        )
        assert all(isinstance(r, ValueError) for r in results)
        assert len(await be.store.relay_list_tasks(be._cur_project_id(), limit=100)) == 1
    finally:
        await be.shutdown()


async def test_normal_chat_unaffected_by_relay(home):
    prov = RecProvider([
        [ToolUseBlock(id="t1", name="run_command", input={"cmd": "whoami"})],
        [TextBlock(text="普通聊天回答")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        snap = await _start(be, sid)
        task_id = snap["task"]["id"]
        await _wait_snapshot(be, task_id, "paused")  # 工具调用 → 暂停

        events: list = []

        async def _collect(ev):
            events.append(ev)

        await be.send("你好，普通聊天", _collect)
        history = be.runtimes[sid].agent.history
        assert any("普通聊天回答" in (m.text or "") for m in history)
    finally:
        await be.shutdown()
