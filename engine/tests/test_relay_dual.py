"""接力（主副屏交替）后端测试：双会话绑定、交替推进、交接材料、中断、归属。

无真实 key / 无真实用户数据：provider 全部注入 FakeProvider。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from conftest import FakeProvider

from skysheep.messages import TextBlock
from skysheep.models.base import (
    Provider,
    ProviderDone,
    ProviderTextDelta,
)
from skysheep.server.backend import ServerBackend


async def _mk(home, prov) -> ServerBackend:
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov)
    await be.setup()
    return be


class GatedProvider(Provider):
    """阻塞到放行才吐文本；用于制造「正在执行」窗口验证中断。"""

    name = "fake"
    model = "fake-gated"

    def __init__(self, text: str = "迟到的输出") -> None:
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


class AlternatingProvider(Provider):
    """前 N 轮给文本，之后阻塞：验证「连续交替」会自己换边继续。"""

    name = "fake"
    model = "fake-alt"

    def __init__(self, limit: int = 2) -> None:
        super().__init__()
        self.limit = limit
        self.calls = 0
        self.gate = asyncio.Event()
        self.blocked = asyncio.Event()

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        if self.calls > self.limit:
            self.blocked.set()
            await self.gate.wait()
            yield ProviderTextDelta("继续")
        else:
            yield ProviderTextDelta(f"第{self.calls}轮产出")
        yield ProviderDone(stop_reason="end_turn", input_tokens=5, output_tokens=5)


async def _wait_pair_state(be, pair_id, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pair = await be.store.relay_pair_get(pair_id)
        if pair and pair["state"] == state:
            return pair
        await asyncio.sleep(0.01)
    raise AssertionError(f"接力未在 {timeout}s 内进入状态 {state}")


async def test_dual_open_reuses_peer_and_binds_main(home):
    prov = FakeProvider([TextBlock(text="x")])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        # 面板自动加载（不带 create_peer）不建副屏会话
        idle = await be.relay_dual_open({"session_id": sid})
        assert idle["pair"] is None
        assert idle["peer"] is None
        sessions = await be.store.list_sessions(be.project.id)
        assert not any(s.title == "接力 · 副屏" for s in sessions)

        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        assert opened["pair"] is None
        assert opened["main"]["id"] == sid
        peer_id = opened["peer"]["id"]
        assert peer_id != sid
        # 再点一次「接力」：复用同一份空副屏，不新增会话
        again = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        assert again["peer"]["id"] == peer_id
        sessions = await be.store.list_sessions(be.project.id)
        assert sum(1 for s in sessions if s.title == "接力 · 副屏") == 1
    finally:
        await be.shutdown()


async def test_dual_run_alternates_and_hands_previous_output(home):
    prov = FakeProvider([
        [TextBlock(text="主屏方案：第一步读文件")],
        [TextBlock(text="副屏反馈：已完成")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        started = await be.relay_dual_run({
            "session_id": sid,
            "goal": "完成示例任务",
            "main_task": "发布方案",
            "peer_task": "执行并反馈",
            "request_id": "d-run-1",
            "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        assert started["pair"]["state"] == "running"
        await _wait_pair_state(be, pair_id, "idle")

        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]] == ["main"]
        assert snap["turns"][0]["status"] == "done"
        assert "主屏方案" in snap["turns"][0]["output"]
        assert snap["pair"]["current_side"] == "peer"
        assert "完成示例任务" in snap["turns"][0]["brief"]
        assert "发布方案" in snap["turns"][0]["brief"]

        started2 = await be.relay_dual_run({
            "session_id": sid,
            "pair_id": pair_id,
            "request_id": "d-run-2",
            "expected_revision": snap["pair"]["revision"],
        })
        assert started2["pair"]["id"] == pair_id
        await _wait_pair_state(be, pair_id, "idle")

        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]] == ["main", "peer"]
        assert snap["pair"]["current_side"] == "main"
        # 第二轮（副屏）的指令里带上了主屏上一轮的方案
        assert "主屏方案" in snap["turns"][1]["brief"]
        assert "执行并反馈" in snap["turns"][1]["brief"]
        # 两屏是真实会话：各自历史里都有按接力指令产生的对话
        main_msgs = await be.store.load_messages(sid)
        peer_msgs = await be.store.load_messages(opened["peer"]["id"])
        assert any("主屏方案" in (m.text or "") for m in main_msgs if m.role == "assistant")
        assert any("副屏反馈" in (m.text or "") for m in peer_msgs if m.role == "assistant")
    finally:
        await be.shutdown()


async def test_dual_save_does_not_bump_revision(home):
    prov = FakeProvider([TextBlock(text="x")])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标",
            "main_task": "改方案", "peer_task": "改执行",
            "request_id": "d1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")
        before = (await be.store.relay_pair_get(pair_id))["revision"]
        saved = await be.relay_dual_save({
            "pair_id": pair_id, "goal": "新目标", "main_task": "改方案",
            "peer_task": "改执行", "auto_alternate": True,
        })
        assert saved["pair"]["goal"] == "新目标"
        assert saved["pair"]["revision"] == before  # 内容编辑不推进 CAS 版本
        assert saved["pair"]["auto_alternate"] is True
        assert opened["peer"]["id"] == saved["pair"]["peer_session_id"]
    finally:
        await be.shutdown()


async def test_dual_stop_cancels_running_turn(home):
    prov = GatedProvider()
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标",
            "main_task": "跑起来", "peer_task": "接着跑",
            "request_id": "s1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await asyncio.wait_for(prov.started.wait(), timeout=3)
        await _wait_pair_state(be, pair_id, "running")
        stopped = await be.relay_dual_stop({
            "pair_id": pair_id, "request_id": "stop-1",
            "expected_revision": (await be.store.relay_pair_get(pair_id))["revision"],
        })
        assert stopped["pair"]["state"] == "paused"
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"][0]["status"] == "cancelled"
        # 轮次没有推进：仍轮到主屏
        assert snap["pair"]["current_side"] == "main"
        assert "relay_paused" in (stopped["pair"]["error"] or {}).get("code", "") or \
            stopped["pair"]["state"] == "paused"
    finally:
        prov.gate.set()
        await be.shutdown()


async def test_dual_auto_alternate_keeps_going_until_stopped(home):
    prov = AlternatingProvider(limit=2)
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标",
            "main_task": "主任务", "peer_task": "副任务", "auto_alternate": True,
            "request_id": "auto-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        # 两轮自动跑完后，第三轮又轮到主屏并阻塞在那里
        await asyncio.wait_for(prov.blocked.wait(), timeout=5)
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]][:2] == ["main", "peer"]
        assert snap["turns"][0]["status"] == "done"
        assert snap["turns"][1]["status"] == "done"
        assert snap["pair"]["state"] == "running"
        assert snap["pair"]["current_side"] == "main"
        stopped = await be.relay_dual_stop({
            "pair_id": pair_id, "request_id": "auto-stop",
            "expected_revision": snap["pair"]["revision"],
        })
        assert stopped["pair"]["state"] == "paused"
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]] == ["main", "peer", "main"]
        assert snap["turns"][-1]["status"] == "cancelled"
    finally:
        prov.gate.set()
        await be.shutdown()


async def test_dual_empty_output_pauses_instead_of_spinning(home):
    prov = FakeProvider([[TextBlock(text="")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标",
            "main_task": "主任务", "peer_task": "副任务", "auto_alternate": True,
            "request_id": "empty-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "paused")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"][0]["status"] == "error"
        assert "没有产出文本" in snap["turns"][0]["error"]
        # 只开了一轮就停：连续交替不会在空产出上空转
        assert len(snap["turns"]) == 1
    finally:
        await be.shutdown()


async def test_dual_rejects_cross_project_session(home):
    prov = FakeProvider([TextBlock(text="x")])
    be = await _mk(home, prov)
    try:
        other = await be.store.get_or_create_project(str(home / "other-pkg"))
        foreign = await be.store.create_session(other.id, title="别的项目")
        with pytest.raises(ValueError, match="relay_forbidden.*other-pkg"):
            await be.relay_dual_open({"session_id": foreign.id})
        with pytest.raises(ValueError, match="relay_forbidden.*other-pkg"):
            await be.relay_dual_run({
                "session_id": foreign.id, "goal": "x",
                "request_id": "fp", "expected_revision": 0,
            })
    finally:
        await be.shutdown()


async def test_dual_recreates_peer_after_it_is_deleted(home):
    """副屏会话被删后：再执行会自动换一个新的副屏，不往不存在的会话写消息。"""
    prov = FakeProvider([
        [TextBlock(text="主屏方案")], [TextBlock(text="新副屏反馈")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        old_peer = opened["peer"]["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标",
            "main_task": "主任务", "peer_task": "副任务",
            "request_id": "repair-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")
        # 删除副屏（本机跨项目已放开，这里同项目直接删）
        await be.delete_session(old_peer, local=True)
        assert await be.store.get_session(old_peer) is None

        snapshot = await be.store.relay_pair_snapshot(pair_id)
        await be.relay_dual_run({
            "session_id": sid, "pair_id": pair_id, "request_id": "repair-2",
            "expected_revision": snapshot["pair"]["revision"],
        })
        await _wait_pair_state(be, pair_id, "idle")
        snapshot = await be.store.relay_pair_snapshot(pair_id)
        new_peer = snapshot["pair"]["peer_session_id"]
        assert new_peer != old_peer
        assert await be.store.get_session(new_peer) is not None
        assert [t["side"] for t in snapshot["turns"]] == ["main", "peer"]
        assert snapshot["turns"][-1]["status"] == "done"
    finally:
        await be.shutdown()


async def test_dual_run_requires_both_side_tasks(home):
    """总任务与两屏任务都必须填：不再套用隐藏的默认句。"""
    prov = FakeProvider([TextBlock(text="x")])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        with pytest.raises(ValueError, match="主屏每一轮做什么"):
            await be.relay_dual_run({
                "session_id": sid, "goal": "目标", "request_id": "t1",
                "expected_revision": 0,
            })
        with pytest.raises(ValueError, match="副屏每一轮做什么"):
            await be.relay_dual_run({
                "session_id": sid, "goal": "目标", "main_task": "主",
                "request_id": "t2", "expected_revision": 0,
            })
    finally:
        await be.shutdown()
