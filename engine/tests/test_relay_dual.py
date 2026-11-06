"""接力（主副屏交替）后端测试：双会话绑定、交替推进、交接材料、中断、归属。

无真实 key / 无真实用户数据：provider 全部注入 FakeProvider。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from conftest import FakeProvider

from skysheep.core.subagent import TaskRecord
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


class ScriptedThenBlockProvider(Provider):
    """按脚本给文本（含逐调用计数），脚本耗尽后阻塞：把「连续交替」停在确定的一轮。"""

    name = "fake"
    model = "fake-scripted"

    def __init__(self, groups: list) -> None:
        super().__init__()
        self.script = [list(g) for g in groups]
        self.calls = 0
        self.gate = asyncio.Event()
        self.blocked = asyncio.Event()

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        if self.calls <= len(self.script):
            for block in self.script[self.calls - 1]:
                if isinstance(block, TextBlock):
                    yield ProviderTextDelta(block.text)
            yield ProviderDone(stop_reason="end_turn", input_tokens=5, output_tokens=5)
            return
        self.blocked.set()
        await self.gate.wait()
        yield ProviderTextDelta("继续")
        yield ProviderDone(stop_reason="end_turn", input_tokens=5, output_tokens=5)


async def _wait_pair_state(be, pair_id, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pair = await be.store.relay_pair_get(pair_id)
        if pair and pair["state"] == state:
            return pair
        await asyncio.sleep(0.01)
    raise AssertionError(f"接力未在 {timeout}s 内进入状态 {state}")


# 上限/异常用例关掉交接复述核验：协商调用会吃掉角色脚本（按位置传给
# _start_dual/_run_turn 的 gate 开关）
_NO_GATE = False


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
            # 本测试验证连续交替本身：AlternatingProvider 的固定产出过不了
            # 复述结构校验，交接核验另有多组专门用例，这里关掉
            "verify_handoff": False,
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


# ---- 交接门：外部副作用收敛 + 复述/核验（接力模式设计 §4 / §6） ----

_RESTATED = (
    "【目标】完成示例任务并交付\n"
    "【已完成】主屏已给出第一步方案\n"
    "【待办】按方案执行并反馈\n"
    "【计划】执行方案里的第一步并记录结果\n"
    "【不确定】无"
)
_VERDICT_PASS = "结论：通过\n【缺漏】无\n【纠正】无\n【理由】复述与交接内容一致"
_VERDICT_REJECT = (
    "结论：驳回\n【缺漏】没提第一步的关键结果\n【纠正】把目标理解成了别的任务\n"
    "【理由】复述与输出不一致"
)


async def test_dual_verify_gate_passes_and_enriches_brief(home):
    """自动交替换手：复述通过核验后才开轮，复述随 brief 注入并留痕。"""
    prov = FakeProvider([
        [TextBlock(text="主屏方案：第一步读文件")],  # 第 1 轮（主屏，首轮无交接）
        [TextBlock(text=_RESTATED)],                 # 换手复述（副屏复述）
        [TextBlock(text=_VERDICT_PASS)],             # 移交方（主屏）核验通过
        [TextBlock(text="副屏执行完成")],            # 第 2 轮（副屏）
        [TextBlock(text=_RESTATED)],                 # 再换手的复述（主屏复述）
        [TextBlock(text=_VERDICT_PASS)],             # 移交方（副屏）核验通过
        [TextBlock(text="")],                        # 第 3 轮空产出：停在暂停不空转
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "完成示例任务",
            "main_task": "发布方案", "peer_task": "执行并反馈",
            "auto_alternate": True,
            "request_id": "v-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "paused")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]] == ["main", "peer", "main"]
        gated = snap["turns"][1]
        assert gated["verify_state"] == "passed"
        assert gated["verify_attempts"] == 1
        assert gated["restatement"] == _RESTATED
        assert gated["verify_note"] == ""
        assert "【交接复述" in gated["brief"]
        assert "完成示例任务并交付" in gated["brief"]
        # 首轮没有交接材料，不经核验、不留痕
        assert snap["turns"][0]["verify_state"] == ""
    finally:
        await be.shutdown()


async def test_dual_verify_reject_then_pass_on_second_attempt(home):
    """移交方驳回后带理由重述：第二次通过，驳回理由进 verify_note。"""
    restated2 = _RESTATED + "\n补充：修正——示例任务就是发布方案本身"
    prov = FakeProvider([
        [TextBlock(text="主屏方案：第一步读文件")],
        [TextBlock(text=_RESTATED)],
        [TextBlock(text=_VERDICT_REJECT)],
        [TextBlock(text=restated2)],
        [TextBlock(text=_VERDICT_PASS)],
        [TextBlock(text="副屏执行完成")],
        [TextBlock(text=restated2)],
        [TextBlock(text=_VERDICT_PASS)],
        [TextBlock(text="")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "完成示例任务",
            "main_task": "发布方案", "peer_task": "执行并反馈",
            "auto_alternate": True,
            "request_id": "v-2", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "paused")
        snap = await be.store.relay_pair_snapshot(pair_id)
        gated = snap["turns"][1]
        assert gated["verify_state"] == "passed"
        assert gated["verify_attempts"] == 2
        assert "驳回" in gated["verify_note"]
        assert gated["restatement"] == restated2
    finally:
        await be.shutdown()


async def test_dual_verify_exhausted_pauses_before_next_turn(home):
    """复述三次都结构不完整：核验模型一次都不该被调，pair 暂停等人。"""
    prov = FakeProvider([
        [TextBlock(text="主屏方案：第一步读文件")],  # 第 1 轮
        [TextBlock(text="我没理解，直接开始吧")],    # 复述 1：缺小节
        [TextBlock(text="我没理解，直接开始吧")],    # 复述 2
        [TextBlock(text="我没理解，直接开始吧")],    # 复述 3
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "完成示例任务",
            "main_task": "发布方案", "peer_task": "执行并反馈",
            "auto_alternate": True,
            "request_id": "v-3", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "paused")
        snap = await be.store.relay_pair_snapshot(pair_id)
        # 只跑了第 1 轮；接收方没拿到执行权（已换边但未开轮）
        assert len(snap["turns"]) == 1
        assert snap["pair"]["state"] == "paused"
        assert snap["pair"]["current_side"] == "peer"
        assert snap["pair"]["error"]["code"] == "relay_verification_failed"
        # 结构失败在服务端拦截：核验调用一次都不发生（1 轮 + 3 次复述）
        assert len(prov.calls) == 4
    finally:
        await be.shutdown()


async def test_dual_verify_skipped_when_manual_or_disabled(home):
    """人工逐轮批准与关闭核验都不协商；关闭核验的自动交替也直接换手。"""
    # 空产出收尾在这里不可用：会话已有历史时「最后一轮产出」会回退到上一次
    # 非空输出，空转不停——改用脚本耗尽后阻塞，停在确定的一轮再手动中断
    prov = ScriptedThenBlockProvider([
        [TextBlock(text="主屏方案")],
        [TextBlock(text="副屏反馈")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标", "main_task": "主任务",
            "peer_task": "副任务", "request_id": "s-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")
        pair = await be.store.relay_pair_get(pair_id)
        await be.relay_dual_run({
            "session_id": sid, "pair_id": pair_id,
            "request_id": "s-2", "expected_revision": pair["revision"],
        })
        await _wait_pair_state(be, pair_id, "idle")
        # 关闭核验 + 开自动交替：换手不做协商，一直轮到阻塞处再由人工中断
        await be.relay_dual_save({
            "pair_id": pair_id, "auto_alternate": True, "verify_handoff": False,
        })
        pair = await be.store.relay_pair_get(pair_id)
        assert pair["verify_handoff"] is False
        await be.relay_dual_run({
            "session_id": sid, "pair_id": pair_id,
            "request_id": "s-3", "expected_revision": pair["revision"],
        })
        await asyncio.wait_for(prov.blocked.wait(), timeout=5)
        snap = await be.store.relay_pair_snapshot(pair_id)
        stopped = await be.relay_dual_stop({
            "pair_id": pair_id, "request_id": "s-stop",
            "expected_revision": snap["pair"]["revision"],
        })
        assert stopped["pair"]["state"] == "paused"
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]] == ["main", "peer", "main"]
        assert snap["turns"][-1]["status"] == "cancelled"
        assert all(
            t["verify_state"] == "" and t["verify_attempts"] == 0
            for t in snap["turns"]
        )
        # 三轮对话、零次协商调用
        assert prov.calls == 3
    finally:
        prov.gate.set()
        await be.shutdown()


async def test_dual_external_effects_pause_before_turn(home):
    """会话名下仍有后台子代理在跑：开轮前暂停（external_effects_unknown）。"""
    prov = FakeProvider([[TextBlock(text="主屏方案")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        tm = be._tasks_for_workdir(home / "proj")
        rec = TaskRecord("bg-1", "explore", "还在跑的探索任务", session_id=sid)
        rec.status = "running"
        tm._tasks["bg-1"] = rec
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "目标", "main_task": "主任务",
            "peer_task": "副任务", "request_id": "e-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "paused")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"] == []  # 没开轮
        assert snap["pair"]["error"]["code"] == "external_effects_unknown"
        assert "还在跑的探索任务" in snap["pair"]["error"]["message"]
        # 收敛（任务结束）后再点执行：正常开轮
        rec.status = "done"
        pair = await be.store.relay_pair_get(pair_id)
        await be.relay_dual_run({
            "session_id": sid, "pair_id": pair_id,
            "request_id": "e-2", "expected_revision": pair["revision"],
        })
        await _wait_pair_state(be, pair_id, "idle")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert [t["side"] for t in snap["turns"]] == ["main"]
        assert snap["turns"][0]["status"] == "done"
    finally:
        await be.shutdown()



# ---- 破坏性操作的级联收敛（接力模式设计 §7）：删除会话/清项目/恢复备份
# 前先停止并收敛关联接力；导入备份/替换 store 先冻结 ----

def _serial_workflow():
    return {
        "start_node": "n1",
        "nodes": [
            {"id": "n1", "slot": "A", "prompt": "第一节点：调研", "terminal": False},
            {"id": "n2", "slot": "B", "prompt": "第二节点：复核", "terminal": True},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }


async def _start_dual(be, sid, request_id, gate=True):
    return await be.relay_dual_run({
        "session_id": sid,
        "goal": "完成示例任务",
        "main_task": "发布方案",
        "peer_task": "执行并反馈",
        "request_id": request_id,
        "expected_revision": 0,
        "verify_handoff": gate,
    })


async def test_delete_session_converges_running_pair(home):
    """删除会话前的级联收敛（§7）：pair 在跑时删除主屏会话——worker 先取消、
    pair 停到 interrupted（附原因），删除照常完成；删除结果说明收敛的接力，
    库内历史行保留（不做级联隐删）。"""
    prov = GatedProvider("迟到的输出")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        await be.relay_dual_open({"session_id": sid, "create_peer": True})
        started = await _start_dual(be, sid, "d-del-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "running")

        result = await be.delete_session(sid, local=True)
        assert result["deleted"] == sid
        assert result["relay_converged"], "删除结果应说明收敛的接力"
        assert result["relay_converged"][0]["kind"] == "pair"

        pair = await be.store.relay_pair_get(pair_id)
        assert pair["state"] == "interrupted", pair["state"]
        assert "被删除" in (pair["error"] or {}).get("message", "")
        # worker 已不在飞（冻结完成，不复用旧内存 owner）
        st = be._relay_dual_state()
        assert all(t.done() for t in st["workers"].values())
        assert st["current"].get(pair_id) is None
    finally:
        await be.shutdown()


async def test_delete_session_converges_idle_pair(home):
    """闲着的 pair 也要收敛（§7：删除入口先停止并收敛关联接力）：副屏会话
    被删时 pair 停到 interrupted——不留在 idle 让用户对着已删除的会话点执行。
    （run 路径的副屏自愈仍在：恢复执行时若副屏缺失会换新副屏。）"""
    prov = FakeProvider([
        [TextBlock(text="主屏方案")],
        [TextBlock(text="副屏反馈")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        peer_id = opened["peer"]["id"]
        started = await _start_dual(be, sid, "d-idle-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")

        result = await be.delete_session(peer_id, local=True)
        assert result["deleted"] == peer_id
        assert result["relay_converged"]
        pair = await be.store.relay_pair_get(pair_id)
        assert pair["state"] == "interrupted"
    finally:
        await be.shutdown()


async def test_delete_project_converges_serial_task_and_pair(home):
    """清项目前的级联收敛（§7）：项目里的串行任务与主副屏 pair（非终态）
    都停到 interrupted，删除照常完成。"""
    prov = GatedProvider("迟到的输出")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        task = await be.relay_start({
            "session_id": sid, "goal": "调研并给出建议",
            "workflow": _serial_workflow(),
            "request_id": "s-del-1", "expected_revision": 0,
        })
        serial_id = task["task"]["id"]

        sid2 = (await be.new_session())["id"]
        await be.relay_dual_open({"session_id": sid2, "create_peer": True})
        started = await _start_dual(be, sid2, "d-proj-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "running")

        pid = be.project.id
        result = await be.delete_project(pid)
        assert result["removed"] == pid
        kinds = {c["kind"] for c in result["relay_converged"]}
        assert kinds == {"task", "pair"}, result["relay_converged"]

        serial = await be.store.relay_get(serial_id)
        assert serial["task"]["state"] == "interrupted"
        pair = await be.store.relay_pair_get(pair_id)
        assert pair["state"] == "interrupted"
    finally:
        await be.shutdown()


async def test_create_session_backup_rejects_relay_in_flight(home):
    """备份预检（§7）：接力在飞时不做备份。用「交接核验窗口」构造确定性
    在飞态——worker 的模型调用不挂 run_task，普通轮的 busy 预检看不见，
    relay_inflight 单独盘点；回可读原因而不是静默带跑。"""
    prov = ScriptedThenBlockProvider([
        [TextBlock(text="主屏方案：第一步读文件")],  # 第 1 轮（主屏）
        [TextBlock(text=_RESTATED)],                # 换手复述（副屏）
    ])  # 脚本耗尽 → 核验调用阻塞：pair 在跑、worker 活着、无 run_task
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "完成示例任务",
            "main_task": "发布方案", "peer_task": "执行并反馈",
            "auto_alternate": True,
            "request_id": "d-bak-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await asyncio.wait_for(prov.blocked.wait(), timeout=5.0)
        pair = await be.store.relay_pair_get(pair_id)
        assert pair["state"] == "running"

        with pytest.raises(RuntimeError) as ei:
            await be.create_session_backup()
        assert "接力" in str(ei.value)
    finally:
        await be.shutdown()


async def test_restore_backup_freezes_relay_and_marks_interrupted(home):
    """恢复备份先冻结接力（§7）：换库前在飞 worker 全部取消（核验窗口——
    relay worker 的模型调用不挂 run_task，普通轮的 busy 预检看不见，冻结单独
    兜住）；替换完成后按重启恢复——备份里 running 的 pair 标 interrupted，
    不复用旧内存 owner，不自动重放。"""
    prov = ScriptedThenBlockProvider([
        [TextBlock(text="主屏方案：第一步读文件")],  # 第 1 轮（主屏）
        [TextBlock(text=_RESTATED)],                # 换手复述（副屏）
    ])  # 脚本耗尽 → 核验调用阻塞：在飞窗口
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "完成示例任务",
            "main_task": "发布方案", "peer_task": "执行并反馈",
            "auto_alternate": True,
            "request_id": "d-rst-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await asyncio.wait_for(prov.blocked.wait(), timeout=5.0)
        pair = await be.store.relay_pair_get(pair_id)
        assert pair["state"] == "running"

        # 直接走 store 备份（backend 预检那条口径有自己的用例；此刻要构造的
        # 前置状态是「备份里含 running 的 pair」）
        backup = await be.store.backup_now()
        result = await be.restore_session_backup(backup["name"])
        assert result["restored"] == backup["name"]

        pair = await be.store.relay_pair_get(pair_id)
        assert pair is not None, "备份里的 pair 行随库恢复"
        assert pair["state"] == "interrupted", pair["state"]
        st = be._relay_dual_state()
        assert not st["workers"] and not st["current"], "冻结后不复用旧内存登记"
    finally:
        await be.shutdown()


async def _run_turn(be, sid, pair_id, request_id, revision, auto=False,
                   gate=True):
    """第 N 轮开轮：带 pair_id 与最新 revision（auto_alternate 可选）。"""
    params = {
        "session_id": sid, "pair_id": pair_id,
        "goal": "完成示例任务", "main_task": "发布方案", "peer_task": "执行并反馈",
        "request_id": request_id, "expected_revision": revision,
        "verify_handoff": gate,
    }
    if auto:
        params["auto_alternate"] = True
    return await be.relay_dual_run(params)


async def test_dual_empty_output_with_history_parks_instead_of_looping(home):
    """空产出保护在「会话已有历史」时依然生效（§4：空产出不算完成）：
    本轮产出按 emit 捕获（assistant_message 正文），不回退到会话里上一轮的
    旧文本——会话有历史 + 本轮空产出 → 本轮 error（没有产出文本）、pair 落
    暂停，不拿旧文本伪造轮次历史、模型调用有界（不无限空转烧预算）。"""
    prov = FakeProvider([
        [TextBlock(text="第一轮有产出：方案就绪")],   # 主屏（第 1 轮）
        [[]],                                        # 副屏（第 2 轮）：空产出
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await _start_dual(be, sid, "d-empty-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"][0]["status"] == "done"

        started2 = await _run_turn(be, sid, pair_id, "d-empty-2",
                                   snap["pair"]["revision"])
        assert started2["pair"]["state"] == "running"
        await _wait_pair_state(be, pair_id, "paused")

        snap = await be.store.relay_pair_snapshot(pair_id)
        assert len(snap["turns"]) == 2, "不会无限空转（轮次有界）"
        last = snap["turns"][-1]
        assert last["status"] == "error"
        assert "没有产出文本" in last["error"]
        # 旧文本不被伪造成本轮产出
        assert "第一轮有产出" not in (last["output"] or "")
        assert len(prov.calls) == 2, "两轮两次模型调用后停"
    finally:
        await be.shutdown()


async def test_dual_worker_turn_cap_parks(home, monkeypatch):
    """轮次上限兜底（对齐串行 _relay_run_node 的 max_steps 既有机制）：连续
    交替到达 cfg.relay.workflow.max_steps 即暂停（relay_max_steps），不无上限
    推进。"""
    prov = FakeProvider([
        [TextBlock(text="主屏产出")],
        [TextBlock(text="副屏产出")],
    ])
    be = await _mk(home, prov)
    monkeypatch.setattr(be.cfg.relay.workflow, "max_steps", 2)
    try:
        sid = (await be.new_session())["id"]
        started = await _start_dual(be, sid, "d-cap-1", _NO_GATE)
        pair_id = started["pair"]["id"]
        # 连续交替开在 _start_dual 之外补配：直接改 pair 的配置位
        await be.store.relay_pair_update(
            pair_id, {"auto_alternate": True}, expect_states=("running",))
        await _wait_pair_state(be, pair_id, "paused")

        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["pair"]["error"]["code"] == "relay_max_steps"
        assert len(snap["turns"]) == 2, "到达上限即停"
        assert len(prov.calls) == 2
    finally:
        await be.shutdown()


async def test_dual_worker_token_cap_parks(home, monkeypatch):
    """token 预算兜底（对齐串行 _relay_run_node 的 token_budget 既有机制）：
    本次运行累计用量到达 cfg.relay.workflow.token_budget 即暂停
    （relay_token_budget），不无上限烧预算。"""
    prov = FakeProvider([
        [TextBlock(text="主屏产出")],
        [TextBlock(text="副屏产出")],
    ])
    be = await _mk(home, prov)
    monkeypatch.setattr(be.cfg.relay.workflow, "token_budget", 8)
    try:
        sid = (await be.new_session())["id"]
        started = await _start_dual(be, sid, "d-tok-1", _NO_GATE)
        pair_id = started["pair"]["id"]
        await be.store.relay_pair_update(
            pair_id, {"auto_alternate": True}, expect_states=("running",))
        await _wait_pair_state(be, pair_id, "paused")

        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["pair"]["error"]["code"] == "relay_token_budget"
        assert len(snap["turns"]) == 1, "第一轮用量已达预算即停"
        assert len(prov.calls) == 1
    finally:
        await be.shutdown()


async def test_dual_peer_first_turn_sets_relay_execution_flag(home):
    """副屏首轮（标准场景：relay_peer_session 新建的副屏会话，runtime 由本轮
    懒建）也必须置位接力执行标记（§4 第一条）——runtime 未就位就打标会整轮
    漏置位，shell 的 background 拒绝失效；轮末清除。"""
    prov = FakeProvider([
        [TextBlock(text="主屏产出")],
        [TextBlock(text="副屏产出")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        peer_id = opened["peer"]["id"]
        assert peer_id not in be.runtimes, "前置：副屏 runtime 尚未建立"
        started = await _start_dual(be, sid, "d-flag-peer-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")
        assert peer_id not in be.runtimes, "前置：主屏轮完成后副屏 runtime 仍未建立"

        snap = await be.store.relay_pair_snapshot(pair_id)
        await _run_turn(be, sid, pair_id, "d-flag-peer-2", snap["pair"]["revision"])
        # 副屏轮在飞：runtime 已就位（懒建被确保）且标记置位
        end = time.monotonic() + 5.0
        while time.monotonic() < end and not (
            peer_id in be.runtimes
            and be.runtimes[peer_id].agent.relay_execution is True):
            await asyncio.sleep(0.02)
        assert peer_id in be.runtimes, "副屏 runtime 必须在开轮前就位"
        assert be.runtimes[peer_id].agent.relay_execution is True, \
            "副屏首轮标记必须置位（不能整轮漏置位）"
        await _wait_pair_state(be, pair_id, "idle")
        assert be.runtimes[peer_id].agent.relay_execution is False, "轮末清除"
    finally:
        await be.shutdown()


async def test_dual_worker_exception_parks_pair(home, monkeypatch):
    """worker 外层兜底（对齐串行路径的 except Exception → park）：protected 区
    之外的异常（store 写失败等）不许让 worker 带异常死透把 pair 卡在 running
    ——落成可读暂停态（relay_error），之后还能正常开新一轮。"""
    prov = FakeProvider([
        [TextBlock(text="主屏产出")],
        [TextBlock(text="副屏产出")],
        [TextBlock(text="第三轮产出")],
    ])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await _start_dual(be, sid, "d-err-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")

        # 注入：本轮收尾（status=done）时 store 抛异常（sqlite busy 同型）
        orig = be.store.relay_pair_finish_turn
        async def boom(pair_id_, turn_no_, *, status, **kw):
            if status == "done":
                raise RuntimeError("sqlite busy（注入）")
            return await orig(pair_id_, turn_no_, status=status, **kw)
        be.store.relay_pair_finish_turn = boom

        snap = await be.store.relay_pair_snapshot(pair_id)
        await _run_turn(be, sid, pair_id, "d-err-2", snap["pair"]["revision"])
        await _wait_pair_state(be, pair_id, "paused")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["pair"]["error"]["code"] == "relay_error"
        assert "sqlite busy" in snap["pair"]["error"]["message"]
        st = be._relay_dual_state()
        assert all(t.done() for t in st["workers"].values()), "worker 已退出"

        # 不卡 running：解除注入后能正常开新一轮
        be.store.relay_pair_finish_turn = orig
        snap = await be.store.relay_pair_snapshot(pair_id)
        started3 = await _run_turn(be, sid, pair_id, "d-err-3",
                                   snap["pair"]["revision"])
        assert started3["pair"]["state"] == "running"
        await _wait_pair_state(be, pair_id, "idle")
    finally:
        await be.shutdown()


async def test_finish_turn_guard_rejects_late_cancel(home):
    """finish_turn 的 turn 状态 CAS 守卫（对齐 relay_pair_update 既有形态）：
    迟到的 abort（收尾广播上的取消落在 await 上）不得把已成功的 done 轮改写
    成 cancelled；worker 兜底转而把 pair 落成暂停（不卡 running）。"""
    prov = FakeProvider([[TextBlock(text="主屏产出")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        pair_id = "pair-guard-test"
        await be.store.relay_pair_create({
            "id": pair_id, "project_id": be.project.id,
            "main_session_id": sid, "peer_session_id": sid,
            "goal": "完成示例任务", "main_task": "发布方案",
            "peer_task": "执行并反馈", "state": "running",
            "current_side": "main",
        })
        pair = await be.store.relay_pair_get(pair_id)
        started = await be.store.relay_pair_start_turn(
            pair_id, "main", "brief", expected_revision=pair["revision"],
            verify={"state": "", "attempts": 0, "restatement": "", "note": ""})
        turn_no = started["pair"]["turn_no"]

        done = await be.store.relay_pair_finish_turn(
            pair_id, turn_no, status="done", output="成果")
        assert done is not None and done["pair"]["state"] == "idle"

        # 迟到的 cancelled：守卫拦下，done 轮不被改写
        late = await be.store.relay_pair_finish_turn(
            pair_id, turn_no, status="cancelled", output="迟到")
        assert late is None, "已成功的 done 轮不得被 cancelled 覆写"
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"][-1]["status"] == "done"
        assert snap["turns"][-1]["output"] == "成果"

        # worker 兜底：abort 遇到已终态的轮 → pair 落成暂停（不卡 running）
        await be.store.relay_pair_update(
            pair_id, {"state": "running"}, expect_states=("idle",))
        await be._relay_dual_abort(pair_id, turn_no, sid)
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"][-1]["status"] == "done", "done 轮仍不被改写"
        assert snap["pair"]["state"] == "paused", "worker 已死：pair 不卡 running"
    finally:
        await be.shutdown()


async def test_dual_run_zero_revision_is_enforced(home):
    """expected_revision=0 是显式值（与 store 层 is not None 同口径）：pair 被
    worker 抬升 revision 后，陈旧客户端传 0 免刷新直接开轮被拒
    （relay_conflict）。"""
    prov = FakeProvider([[TextBlock(text="主屏产出")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await _start_dual(be, sid, "d-rev-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "idle")
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["pair"]["revision"] > 0

        with pytest.raises(ValueError, match="relay_conflict"):
            await _run_turn(be, sid, pair_id, "d-rev-2", 0)
    finally:
        await be.shutdown()


async def test_handoff_gate_inventories_live_background_command(home):
    """交接门的外部副作用盘点补上后台命令（接力模式设计 §4 第三条）：复用
    2.6.4 的后台进程登记面做存活盘点——接力会话名下还有活着的后台命令进程
    树时证明不了静止，按 external_effects_unknown 暂停并列出具体命令。"""
    prov = FakeProvider([[TextBlock(text="x")]])
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        opened = await be.relay_dual_open({"session_id": sid, "create_peer": True})
        peer_id = opened["peer"]["id"]
        pair_id = "pair-gate-test"
        await be.store.relay_pair_create({
            "id": pair_id, "project_id": be.project.id,
            "main_session_id": sid, "peer_session_id": peer_id,
            "goal": "完成示例任务", "main_task": "发布方案",
            "peer_task": "执行并反馈", "state": "running",
            "current_side": "peer",
        })
        # 构造「主屏会话名下仍存活的后台命令」：登记面直接造一条假记录
        from skysheep.tools import shell as shell_mod

        class _FakeProc:
            def __init__(self) -> None:
                self.pid = 4242

            def poll(self):
                return None  # 存活

        saved = dict(shell_mod._BG)
        shell_mod._BG.clear()
        shell_mod._BG[42] = {
            "proc": _FakeProc(), "command": "python -m http.server 8000",
            "buf": {"out": ""}, "started": 0.0, "owner": sid,
        }
        try:
            gate = await be._relay_dual_handoff_gate(
                await be.store.relay_pair_get(pair_id), "peer", "任务", "上一轮产出", "brief")
            assert gate is None, "门必须没过（有存活后台命令）"
            snap = await be.store.relay_pair_get(pair_id)
            assert snap["state"] == "paused"
            assert snap["error"]["code"] == "external_effects_unknown"
            assert "http.server" in snap["error"]["message"], snap["error"]["message"]
        finally:
            shell_mod._BG.clear()
            shell_mod._BG.update(saved)
    finally:
        await be.shutdown()


async def test_relay_turn_sets_and_clears_relay_execution_flag(home):
    """worker 开轮置位 agent 的接力执行标记、轮末清除（§4 第一条）：标记只在
    接力自己的轮里生效——用户在同一会话里的普通轮不受 background 限制（那类
    进程由轮首盘点兜住）。"""
    prov = GatedProvider("迟到的输出")
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await _start_dual(be, sid, "d-flag-1")
        pair_id = started["pair"]["id"]
        await _wait_pair_state(be, pair_id, "running")
        agent = be.runtimes[sid].agent
        # worker 从收到 running 到置位标记之间还有开轮簿记：轮询等它置位
        end = time.monotonic() + 5.0
        while time.monotonic() < end and agent.relay_execution is not True:
            await asyncio.sleep(0.02)
        assert agent.relay_execution is True, "接力轮在飞时标记必须置位"

        await be.relay_dual_stop({
            "pair_id": pair_id, "request_id": "d-flag-stop",
            "expected_revision": (await be.store.relay_pair_get(pair_id))["revision"],
        })
        await _wait_pair_state(be, pair_id, "paused")
        assert agent.relay_execution is False, "轮结束标记必须清除"
    finally:
        await be.shutdown()
async def test_import_full_data_freezes_relay(home):
    """完整导入先冻结接力（§7：导入备份/替换 store 先冻结）：导入期间在飞
    worker 取消，完成后按重启恢复——导出包里 running 的 pair 标 interrupted，
    不复用旧内存 owner。"""
    prov = ScriptedThenBlockProvider([
        [TextBlock(text="主屏方案：第一步读文件")],  # 第 1 轮（主屏）
        [TextBlock(text=_RESTATED)],                # 换手复述（副屏）
    ])  # 脚本耗尽 → 核验调用阻塞：在飞窗口
    be = await _mk(home, prov)
    try:
        sid = (await be.new_session())["id"]
        started = await be.relay_dual_run({
            "session_id": sid, "goal": "完成示例任务",
            "main_task": "发布方案", "peer_task": "执行并反馈",
            "auto_alternate": True,
            "request_id": "d-imp-1", "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await asyncio.wait_for(prov.blocked.wait(), timeout=5.0)

        exp = await be.export_full_data({"include_secrets": False})
        result = await be.import_full_data({"path": exp["path"]})
        assert result["db_restored"] is True

        pair = await be.store.relay_pair_get(pair_id)
        assert pair is not None
        assert pair["state"] == "interrupted", pair["state"]
        st = be._relay_dual_state()
        assert not st["workers"] and not st["current"]
    finally:
        await be.shutdown()


# ---- 轮间隙误杀与副屏排他（对抗审查留人工两项的修复回归） ----

async def _gap_window_setup(home):
    """把链停在「上一轮已完成、交接协商复述调用阻塞」的轮间隙窗口：
    worker 活着、current 还登记着刚完成的主屏轮——正是误杀窗口的形态。"""
    prov = ScriptedThenBlockProvider([[TextBlock(text="主屏方案")]])
    be = await _mk(home, prov)
    sid = (await be.new_session())["id"]
    started = await be.relay_dual_run({
        "session_id": sid, "goal": "完成示例任务",
        "main_task": "发布方案", "peer_task": "执行并反馈",
        "auto_alternate": True,  # 默认 verify_handoff=on：协商窗口真实存在
        "request_id": "gap-1", "expected_revision": 0,
    })
    pair_id = started["pair"]["id"]
    await asyncio.wait_for(prov.blocked.wait(), timeout=5)
    snap = await be.store.relay_pair_snapshot(pair_id)
    assert snap["turns"][0]["status"] == "done"
    return prov, be, sid, pair_id


async def test_dual_stop_spares_user_turn_in_gap(home):
    """轮间隙手动中断不得误杀用户自己在同一会话在飞的新消息轮。

    current 在轮间隙不清零：修复前 stop 按它调 cancel_run 会把用户
    自己正在跑的那轮一起取消；修复后以库为准（最新 turn 仍 running
    才取消），已完成的轮与用户的在飞轮都不动。"""
    prov, be, sid, pair_id = await _gap_window_setup(home)
    user_turn = None
    try:
        user_turn = asyncio.create_task(asyncio.sleep(30))
        be.runtimes.get(sid).run_task = user_turn
        stopped = await be.relay_dual_stop({
            "pair_id": pair_id, "request_id": "gap-stop",
            "expected_revision": (await be.store.relay_pair_get(pair_id))["revision"],
        })
        assert stopped["pair"]["state"] == "paused"
        await asyncio.sleep(0.05)
        assert not user_turn.done(), "轮间隙中断不得取消用户在飞轮"
        # 已完成的轮不被改写成中断
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["turns"][0]["status"] == "done"
    finally:
        if user_turn is not None:
            user_turn.cancel()
        prov.gate.set()
        await be.shutdown()


async def test_cascade_spares_user_turn_in_gap(home):
    """级联收敛不波及另一侧会话里用户的在飞轮：删副屏收敛 pair 时，
    主屏会话里用户自己正在跑的那轮要活着。"""
    prov, be, sid, pair_id = await _gap_window_setup(home)
    user_turn = None
    try:
        peer_id = (await be.store.relay_pair_get(pair_id))["peer_session_id"]
        user_turn = asyncio.create_task(asyncio.sleep(30))
        be.runtimes.get(sid).run_task = user_turn
        result = await be.relay_converge_for_deletion(session_id=peer_id)
        assert any(c["kind"] == "pair" for c in result)
        pair = await be.store.relay_pair_get(pair_id)
        assert pair["state"] == "interrupted"
        await asyncio.sleep(0.05)
        assert not user_turn.done(), "级联收敛不得取消另一侧会话里用户在飞轮"
    finally:
        if user_turn is not None:
            user_turn.cancel()
        prov.gate.set()
        await be.shutdown()


async def test_dual_peer_reuse_is_exclusive(home):
    """副屏复用必须排他：pair A 占着的副屏（哪怕还是空的），同项目另一份
    接力不得复用，避免两份接力的轮次交错写进同一会话历史；A 自己重开仍
    复用自己的副屏。"""
    prov = FakeProvider([
        [TextBlock(text="主屏方案A")],
        [TextBlock(text="主屏方案B")],
    ])
    be = await _mk(home, prov)
    try:
        s1 = (await be.new_session())["id"]
        started_a = await be.relay_dual_run({
            "session_id": s1, "goal": "目标A", "main_task": "主任务A",
            "peer_task": "副任务A", "request_id": "ex-a", "expected_revision": 0,
        })
        peer_a = started_a["peer"]["id"]
        await _wait_pair_state(be, started_a["pair"]["id"], "idle")
        # A 只跑了主屏第 1 轮，副屏还是空会话；另一会话开接力不得复用它
        s2 = (await be.new_session())["id"]
        opened_b = await be.relay_dual_open({"session_id": s2, "create_peer": True})
        assert opened_b["peer"] is not None
        assert opened_b["peer"]["id"] != peer_a
        # A 自己重开：仍复用自己的副屏
        opened_a = await be.relay_dual_open({"session_id": s1, "create_peer": True})
        assert opened_a["pair"] is not None
        assert opened_a["peer"]["id"] == peer_a
    finally:
        await be.shutdown()
