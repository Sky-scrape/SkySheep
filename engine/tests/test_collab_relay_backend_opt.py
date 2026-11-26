"""协作（圆桌 / 团队 / 对抗）与接力（串行 / 双屏）后端优化与缺陷回归测试。"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.config import SkySheepConfig
from skysheep.core.collab_budget import BudgetedProvider, RunBudget
from skysheep.core.team import (
    DirectorSpec,
    HuddleRequest,
    TeamChannel,
    TeamError,
    TeamMemberSpec,
    TeamOrchestrator,
)
from skysheep.core.team_board import TeamBoard
from skysheep.messages import Message, TextBlock
from skysheep.models.base import Provider
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend
from skysheep.session.store import SessionStore
from skysheep.tools import ToolRegistry

_RESTATED = (
    "【目标】完成示例任务并交付\n"
    "【已完成】主屏已给出第一步方案\n"
    "【待办】按方案执行并反馈\n"
    "【计划】执行方案里的第一步并记录结果\n"
    "【不确定】无"
)
_VERDICT_PASS = "结论：通过\n【缺漏】无\n【纠正】无\n【理由】复述与交接内容一致"


class _FailingProvider(Provider):
    """每次 stream() 都抛上游异常，供 Agent._turn_events 转成 ErrorEvent。"""

    name = "fake"
    model = "fake-fail"

    def __init__(self, msg: str = "upstream 401 unauthorized") -> None:
        super().__init__()
        self.msg = msg
        self.calls: list[list[Message]] = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        raise RuntimeError(self.msg)
        yield  # pragma: no cover - 使生成器函数满足签名


class _ClosingProvider(FakeProvider):
    """带 aclose 计数器的假模型，验证协作路径不泄漏临时 httpx 连接池。"""

    def __init__(self, scripted: list) -> None:
        super().__init__(scripted)
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


async def _wait_until(pred, *, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待异步条件超时")


def _serial_workflow_dict() -> dict:
    return {
        "nodes": [
            {"id": "n1", "slot": "A", "prompt": "节点1：给出初始方案"},
            {"id": "n2", "slot": "B", "prompt": "节点2：复核并终结", "terminal": True},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
        "start_node": "n1",
    }


def test_team_channel_unread_for_bisect_and_board_restore_strips_self_deps():
    ch = TeamChannel()
    for i in range(1, 21):
        to = "dev" if i % 3 == 0 else ("qa" if i % 3 == 1 else "all")
        sender = "dev" if i == 15 else "director"
        ch.post(sender, to_member=to, msg_kind="report", text=f"msg-{i}")

    ch._cursors["dev"] = 10
    unread = ch.unread_for("dev", exclude_seqs={12})
    got = [m.seq for m in unread]
    expected = [
        m.seq for m in ch.messages
        if m.seq > 10 and m.from_member != "dev"
        and m.to_member in ("all", "dev") and m.seq != 12
    ]
    assert got == expected

    board = TeamBoard(members={"dev", "qa"})
    kept, dropped = board.restore([
        {
            "id": "T1", "title": "one", "assignee": "dev",
            "status": "pending", "deps": ["T1", "MISSING"],
        },
        {
            "id": "T2", "title": "two", "assignee": "qa",
            "status": "pending", "deps": ["T1", "T2", "GHOST"],
        },
    ])
    assert (kept, dropped) == (2, 0)
    assert board.get("T1").deps == []
    assert board.get("T2").deps == ["T1"]


async def test_team_member_error_event_marks_task_error_not_review(home):
    events = []

    async def _collect(ev) -> None:
        events.append(ev)

    fail_prov = _FailingProvider("upstream 401 unauthorized")
    good_prov = FakeProvider([TextBlock(text="ok")])

    orch = TeamOrchestrator(
        emit=_collect,
        working_dir=home / "proj",
        registry=ToolRegistry(),
        cfg=SkySheepConfig().team,
    )
    await orch.create_team([
        TeamMemberSpec(name="coder", provider_name="p1", model="m1", provider=fail_prov),
        TeamMemberSpec(name="helper", provider_name="p2", model="m2", provider=good_prov),
    ])
    task = await orch.task_add("修复登录", "coder", type="exec")
    await orch.task_update(task["id"], "in_progress")

    res = await orch.run_member_turn("coder")
    assert res.status == "error"
    assert "upstream 401" in res.error

    # 验证自动唤醒闭环下，失败成员的 exec 工单落到 error 而非被误升为 review
    task2 = await orch.task_add("修复退出", "coder", type="exec")
    await orch.task_update(task2["id"], "in_progress")
    await orch._wake_actionable()
    assert orch.board.get(task2["id"]).status == "error"


async def test_team_ai_director_error_event_stops_auto_loop(home):
    events = []

    async def _collect(ev) -> None:
        events.append(ev)

    dir_fail = _FailingProvider("director 403 forbidden")
    mem_good = FakeProvider([TextBlock(text="member ok")])

    orch = TeamOrchestrator(
        emit=_collect,
        working_dir=home / "proj",
        registry=ToolRegistry(),
        cfg=SkySheepConfig().team,
        director_mode="ai",
        director=DirectorSpec(provider_name="d", model="dm", provider=dir_fail),
    )
    await orch.create_team([
        TeamMemberSpec(name="dev", provider_name="p1", model="m1", provider=mem_good),
        TeamMemberSpec(name="qa", provider_name="p2", model="m2", provider=mem_good),
    ])
    res = await orch.run_auto_turn("推进任务")
    assert orch.loop_running is False
    assert res["finished"] is None
    assert len(dir_fail.calls) == 1
    assert any("本轮失败" in m.text and "director 403" in m.text for m in orch.channel.messages)


async def test_team_huddle_respects_budget_without_double_counting(home):
    # 1) debate_rounds=1 + 2 名成员 + 1 次主席融合 = 5 次调用；
    #    max_calls=5 必须完整走完，若有 2x 重复扣费会在第 3 次就提前撞墙。
    budget_ok = RunBudget(max_calls=5)
    raw_a = FakeProvider([TextBlock(text="方案 A 更好"), TextBlock(text="补充 A")])
    raw_b = FakeProvider([TextBlock(text="方案 B 稳妥"), TextBlock(text="补充 B")])
    raw_chair = FakeProvider([TextBlock(text="采纳方案 B")])
    orch_ok = TeamOrchestrator(
        emit=lambda _ev: asyncio.sleep(0),
        working_dir=home / "proj",
        registry=ToolRegistry(),
        cfg=SkySheepConfig().team,
        director_mode="ai",
        director=DirectorSpec(
            provider_name="d", model="dm",
            provider=BudgetedProvider(raw_chair, budget_ok),
        ),
    )
    orch_ok.collab_budget = budget_ok
    await orch_ok.create_team([
        TeamMemberSpec(
            name="a", provider_name="p1", model="m1",
            provider=BudgetedProvider(raw_a, budget_ok),
        ),
        TeamMemberSpec(
            name="b", provider_name="p2", model="m2",
            provider=BudgetedProvider(raw_b, budget_ok),
        ),
    ])
    await orch_ok._run_huddle(HuddleRequest(topic="选型争议", members=["a", "b"]))
    assert budget_ok.calls == 5
    assert len(raw_a.calls) == 2
    assert len(raw_b.calls) == 2
    assert len(raw_chair.calls) == 1
    assert any(m.msg_kind == "ruling" and "采纳方案 B" in m.text for m in orch_ok.channel.messages)

    # 2) max_calls=2 时：第 0 轮 2 名成员用尽次数，辩论尝试触发 BudgetExceeded 后
    #    直接截断并跳过主席融合（len(cap_chair.calls) == 0）。
    budget_cap = RunBudget(max_calls=2)
    cap_a = FakeProvider([TextBlock(text="意见 A")])
    cap_b = FakeProvider([TextBlock(text="意见 B")])
    cap_chair = FakeProvider([TextBlock(text="不应执行")])
    orch_cap = TeamOrchestrator(
        emit=lambda _ev: asyncio.sleep(0),
        working_dir=home / "proj",
        registry=ToolRegistry(),
        cfg=SkySheepConfig().team,
        director_mode="ai",
        director=DirectorSpec(
            provider_name="d", model="dm",
            provider=BudgetedProvider(cap_chair, budget_cap),
        ),
    )
    orch_cap.collab_budget = budget_cap
    await orch_cap.create_team([
        TeamMemberSpec(
            name="a", provider_name="p1", model="m1",
            provider=BudgetedProvider(cap_a, budget_cap),
        ),
        TeamMemberSpec(
            name="b", provider_name="p2", model="m2",
            provider=BudgetedProvider(cap_b, budget_cap),
        ),
    ])
    await orch_cap._run_huddle(HuddleRequest(topic="预算触顶争议", members=["a", "b"]))
    assert budget_cap.exceeded() is True
    assert len(cap_chair.calls) == 0
    assert any(
        m.msg_kind == "system" and "上限" in m.text
        for m in orch_cap.channel.messages
    )


async def test_provider_cleanup_on_roundtable_adversarial_and_team_paths(home, monkeypatch):
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: FakeProvider(["ok"]))
    await be.setup()
    try:
        sid = (await be.new_session())["id"]

        # 1) 圆桌：template_chair 构建后因无可用成员提前报错，必须释放 template_chair
        rt_chair = _ClosingProvider([TextBlock(text="chair")])
        monkeypatch.setattr(be, "_build_member_provider", lambda _n, _m="": rt_chair)
        monkeypatch.setattr(be, "_resolve_members", lambda *_a, **_kw: [])
        tpl_rt = {
            "name": "rt-bad",
            "mode": "roundtable",
            "roles": [],
            "aggregator": {"provider": "p1", "model": "m1"},
            "limits": {"debate_rounds": 0},
        }
        with pytest.raises(RuntimeError, match="圆桌没有可用成员"):
            await asyncio.create_task(
                be._roundtable_body(
                    "问题", lambda _e: asyncio.sleep(0), None,
                    sid=sid, template=tpl_rt,
                )
            )
        for _ in range(3):
            await asyncio.sleep(0)
        assert rt_chair.closed == 1

        # 2) 对抗：显式角色覆盖全部 3 个位置时，specs_auto 里多建的临时 provider 也必须释放
        unused_auto = [_ClosingProvider(["u1"]), _ClosingProvider(["u2"])]
        explicit_provs = [
            _ClosingProvider(['{"findings": []}']),
            _ClosingProvider(["i"]),
            _ClosingProvider(["a"]),
        ]
        exp_iter = iter(explicit_provs)
        monkeypatch.setattr(
            be, "_resolve_members",
            lambda *_a, **_kw: [
                type("S", (), {
                    "provider_name": f"auto{k}", "model": f"m{k}",
                    "provider": p, "build_error": "", "name": "",
                })()
                for k, p in enumerate(unused_auto)
            ],
        )
        monkeypatch.setattr(be, "_build_member_provider", lambda _n, _m="": next(exp_iter))
        await asyncio.create_task(
            be._adversarial_body(
                "审查", lambda _e: asyncio.sleep(0),
                [
                    {"role": "finder", "provider": "pf", "model": "mf"},
                    {"role": "investigator", "provider": "pi", "model": "mi"},
                    {"role": "advisor", "provider": "pa", "model": "ma"},
                    {"provider": "extra-auto", "model": "me"},
                ],
                sid=sid,
            )
        )
        for _ in range(3):
            await asyncio.sleep(0)
        assert all(p.closed == 1 for p in unused_auto)
        assert all(p.closed == 1 for p in explicit_provs)

        # 3) _template_team_specs：同 key 去重被跳过的显式 provider 与多余自动池应释放
        kept_p = _ClosingProvider(["kept"])
        dup_p = _ClosingProvider(["dup"])
        used_auto = _ClosingProvider(["used-auto"])
        extra_auto = _ClosingProvider(["extra-auto"])
        seq_iter = iter([kept_p, dup_p])
        monkeypatch.setattr(be, "_build_member_provider", lambda _n, _m="": next(seq_iter))
        monkeypatch.setattr(
            be, "_resolve_members",
            lambda *_a, **_kw: [
                type("S", (), {
                    "provider_name": "pa", "model": "ma1",
                    "provider": used_auto, "build_error": "", "name": "",
                })(),
                type("S", (), {
                    "provider_name": "pb", "model": "mb1",
                    "provider": extra_auto, "build_error": "", "name": "",
                })(),
            ],
        )
        monkeypatch.setattr(be, "_expand_auto_pool", lambda pool: pool)

        async def _build_specs():
            return be._template_team_specs({
                "roles": [
                    {"name": "r0", "provider": ""},
                    {"name": "r1", "provider": "px", "model": "mx"},
                    {"name": "r2", "provider": "px", "model": "mx"},
                ]
            })

        specs = await asyncio.create_task(_build_specs())
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(specs) == 2
        assert used_auto.closed == 0
        assert kept_p.closed == 0
        assert dup_p.closed == 1
        assert extra_auto.closed == 1

        # 4) _team_create：create_team 抛异常时释放已构建的成员与 AI 总管 provider
        mem_p = _ClosingProvider(["mem"])
        dir_p = _ClosingProvider(["dir"])
        t_iter = iter([dir_p, mem_p])
        monkeypatch.setattr(be, "_build_member_provider", lambda _n, _m="": next(t_iter))

        async def _boom(self, _specs):
            raise TeamError("init failed")

        monkeypatch.setattr(TeamOrchestrator, "create_team", _boom)
        with pytest.raises(TeamError, match="init failed"):
            await asyncio.create_task(
                be._team_create(
                    sid, lambda _e: asyncio.sleep(0),
                    [{"provider": "px", "model": "mx", "name": "dev"}],
                    recorder=None, director_mode="ai",
                    director_params={"provider": "pd", "model": "md"},
                )
            )
        for _ in range(3):
            await asyncio.sleep(0)
        assert dir_p.closed == 1
        assert mem_p.closed == 1
    finally:
        await be.shutdown()


async def test_team_oob_actions_deliver_to_live_reconnected_emitters(home):
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: FakeProvider(["ok"]))
    await be.setup()
    try:
        sid = (await be.new_session())["id"]

        orch = TeamOrchestrator(
            emit=lambda _ev: asyncio.sleep(0),
            working_dir=home / "proj",
            registry=ToolRegistry(),
            cfg=SkySheepConfig().team,
        )
        await orch.create_team([
            TeamMemberSpec(name="dev", provider_name="p1", model="m1", provider=FakeProvider(["ok"])),
            TeamMemberSpec(name="qa", provider_name="p2", model="m2", provider=FakeProvider(["ok"])),
        ])

        # 模拟建队时的旧 WS 连接已断开：若轮外仍走旧闭包就会报错/丢帧
        t1 = await orch.task_add("初始工单", "dev", type="exec")

        async def _dead_emit(_ev):
            raise ConnectionError("old ws closed")

        orch._emit = _dead_emit
        be._teams[sid] = orch

        # 向存储放入已完成的对抗消息与圆桌消息
        adv_msg = Message.assistant([TextBlock(text="对抗报告")])
        adv_msg.adversarial = {
            "status": "done",
            "findings": [{
                "id": "F1", "title": "越权漏洞", "severity": "high",
                "verdict": "confirmed", "location": "api.py:12", "description": "缺校验",
            }],
        }
        await be.store.append_message(sid, adv_msg)

        rt_msg = Message.assistant([TextBlock(text="圆桌最终决定\n按计划 A 推进")])
        rt_msg.roundtable = {"mode": "roundtable", "status": "done"}
        await be.store.append_message(sid, rt_msg)

        # 模拟前端重连后的新 WS 连接
        live_events: list[dict] = []

        async def _live_emit(ev: dict) -> None:
            live_events.append(ev)

        be.ws_emitters = [_live_emit]

        await be.adversarial_to_team_tasks({
            "session_id": sid, "seq": adv_msg.seq, "finding_ids": ["F1"], "assignee": "dev",
        })
        await be.roundtable_to_team_task({
            "session_id": sid, "seq": rt_msg.seq, "assignee": "qa",
        })
        await be.team_task_update({
            "session_id": sid, "task_id": t1["id"], "status": "in_progress", "note": "先处理",
        })
        await be.team_stop({"session_id": sid, "reason": "人工收队"})

        kinds = [ev.get("kind") for ev in live_events]
        assert kinds.count("team_task_updated") >= 3  # adversarial + roundtable + task_update
        assert "team_message" in kinds
        assert "team_finished" in kinds
        assert all(ev.get("session_id") == sid for ev in live_events)
    finally:
        await be.shutdown()


async def test_dual_relay_concurrency_rollback_targeted_queries_and_neg_usage(home, monkeypatch):
    # 1) token_budget 纳入交接协商消耗：1 轮产出(18) + 复述(18) + 核验(18) = 54 >= 45
    prov = FakeProvider([
        [TextBlock(text="主屏方案：先建表")],
        [TextBlock(text=_RESTATED)],
        [TextBlock(text=_VERDICT_PASS)],
        [TextBlock(text="副屏不应被调用")],
    ])
    be = ServerBackend(working_dir=home / "proj", provider_factory=lambda: prov)
    await be.setup()
    monkeypatch.setattr(be.cfg.relay.workflow, "token_budget", 45)
    try:
        sid = (await be.new_session())["id"]

        # 并发 dual_open(create_peer=True) 仅创建一条副屏会话
        r1, r2 = await asyncio.gather(
            be.relay_dual_open({"session_id": sid, "create_peer": True}),
            be.relay_dual_open({"session_id": sid, "create_peer": True}),
        )
        assert r1["peer"]["id"] == r2["peer"]["id"]
        peer_id = r1["peer"]["id"]

        # 开跑（带核验 + 自动交替），协商把运行总量推过 45 即暂停在第 2 轮开轮前
        started = await be.relay_dual_run({
            "session_id": sid,
            "goal": "联调双屏",
            "main_task": "设计",
            "peer_task": "实现",
            "auto_alternate": True,
            "verify_handoff": True,
            "request_id": "run-1",
            "expected_revision": 0,
        })
        pair_id = started["pair"]["id"]
        await _wait_until(
            lambda: _pair_state_is(be.store, pair_id, "paused"), timeout_s=6.0,
        )
        snap = await be.store.relay_pair_snapshot(pair_id)
        assert snap["pair"]["error"]["code"] == "relay_token_budget"
        assert len(snap["turns"]) == 1
        assert len(prov.calls) == 3  # 第 1 轮 + 复述 + 核验，未空开第 2 轮

        # 验证交接协商的用量也已写入 usage_log（主屏与副屏各至少有协商记录）
        cur = await be.store._db.execute(
            "SELECT session_id, SUM(in_tokens + out_tokens) AS tot"
            " FROM usage_log GROUP BY session_id"
        )
        by_sid = {row["session_id"]: row["tot"] for row in await cur.fetchall()}
        assert by_sid.get(sid, 0) >= 36  # 第 1 轮(18) + 核验(18)
        assert by_sid.get(peer_id, 0) >= 18  # 复述(18)

        # CAS 冲突时不得污染 next_notes 与 manual_handoff
        await be.store.relay_pair_update(
            pair_id, {"error": {"code": "relay_verification_failed", "message": "x"}},
        )
        with pytest.raises(ValueError, match="relay_conflict"):
            await be.relay_dual_run({
                "session_id": sid,
                "pair_id": pair_id,
                "next_note": "stale-note-must-not-stick",
                "request_id": "bad-cas",
                "expected_revision": 9999,
            })
        assert pair_id not in be._relay_dual_state()["next_notes"]
        assert pair_id not in be._relay_dual_state()["manual_handoff"]

        # 定向 SQL 查询回归：跳过纯空白 done 轮、忽略同侧，正确取最后非空助手正文
        await be.store.relay_pair_start_turn(
            pair_id, "peer", "b2",
            expected_revision=(await be.store.relay_pair_get(pair_id))["revision"],
        )
        # 故意写入纯换行空白 output
        await be.store.relay_pair_finish_turn(pair_id, 2, status="done", output=" \n\t ")
        assert await be.store.relay_pair_last_done_output(pair_id, exclude_side="main") == ""
        assert "先建表" in await be.store.relay_pair_last_done_output(pair_id, exclude_side="peer")

        # last_assistant_text 跳过末尾空助手/用户消息，精准命中最后有效正文
        empty_a = Message.assistant([TextBlock(text="   \n  ")])
        await be.store.append_message(sid, Message.assistant([TextBlock(text="有效历史回答")]))
        await be.store.append_message(sid, empty_a)
        await be.store.append_message(sid, Message.user("下一句"))
        assert await be.store.last_assistant_text(sid) == "有效历史回答"
    finally:
        await be.shutdown()


async def _pair_state_is(store: SessionStore, pair_id: str, state: str) -> bool:
    p = await store.relay_pair_get(pair_id)
    return p is not None and p["state"] == state


async def test_serial_relay_atomic_approve_ownership_before_replay_and_shutdown(home, monkeypatch):
    proj_a = home / "proj_a"
    proj_b = home / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    prov = FakeProvider([
        [TextBlock(text="## 结论\n节点 1 报告内容")],
        [TextBlock(text="## 结论\n节点 2 报告内容")],
    ])
    be = ServerBackend(working_dir=proj_a, provider_factory=lambda: prov)
    await be.setup()
    monkeypatch.setattr(be.cfg.relay.slots["A"], "provider", "fake")
    monkeypatch.setattr(be.cfg.relay.slots["B"], "provider", "fake")
    try:
        sid_a = (await be.new_session())["id"]
        started = await be.relay_start({
            "session_id": sid_a,
            "goal": "串行测试",
            "workflow": _serial_workflow_dict(),
            "request_id": "s-start-1",
            "expected_revision": 0,
        })
        task_id = started["task"]["id"]

        async def _is_waiting():
            snap = await be.store.relay_get(task_id)
            return snap is not None and snap["task"]["state"] == "waiting_confirmation"

        await _wait_until(_is_waiting, timeout_s=5.0)
        snap = await be.store.relay_get(task_id)
        n1 = next(n for n in snap["nodes"] if n["node_id"] == "n1")

        # 统计 relay_approve 触发 store.relay_mutate 的事务次数（必须仅 1 次）
        mutate_calls: list[dict] = []
        orig_mutate = be.store.relay_mutate

        async def _spy_mutate(*args, **kwargs):
            mutate_calls.append({"args": args, "kwargs": kwargs})
            return await orig_mutate(*args, **kwargs)

        be.store.relay_mutate = _spy_mutate  # type: ignore[method-assign]
        try:
            approved = await be.relay_approve({
                "task_id": task_id,
                "request_id": "approve-1",
                "expected_revision": snap["task"]["revision"],
                "report_id": n1["report_id"],
                "report_hash": n1["report_hash"],
            })
        finally:
            be.store.relay_mutate = orig_mutate  # type: ignore[method-assign]

        assert len(mutate_calls) == 1
        assert approved["task"]["state"] == "running"
        assert approved["task"]["current_node"] == "n2"
        cached = await be.store.relay_action_get(task_id, "approve-1")
        assert cached is not None
        by_id = {n["node_id"]: n for n in cached["nodes"]}
        assert by_id["n1"]["status"] == "confirmed"
        assert by_id["n2"]["status"] == "authorized"

        # 切到项目 B 后拿原 request_id 重放批准，必须在读缓存之前先被归属拦截
        await be.switch_project(str(proj_b))
        with pytest.raises(ValueError, match="不属于当前项目"):
            await be.relay_approve({
                "task_id": task_id,
                "request_id": "approve-1",
                "expected_revision": 0,
            })
    finally:
        await be.shutdown()
