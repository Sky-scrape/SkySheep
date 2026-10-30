"""P30 并发稳定性压测：子代理满员排队 / 写租约竞争 / 多团队并发。

三条压力线全部用确定性断言（不依赖墙钟敏感窗，整段 asyncio.wait_for 包住防死锁）：

1. 后台子代理并发满员自动排队：max_concurrent=2 一次派 10 个后台任务
   （FakeProvider 一行产出），断言全部完成、出队（开跑）顺序严格 FIFO、
   任务簿终态一致无幽灵（内存与落盘两份口径）、叫醒注记按完成序注入且
   一批取走后不再重复。
2. 写租约竞争：8 个并发写方对同一文件 claim_write 依次获得→修改→归还
   （事件等待队列 FIFO，注记为「已等待后继续」变体）；再加占用方不归还的
   超时放行路径——按 leases.py 实际语义断言：第一个到点的后来者对持有方
   超时接管（注记「未结束」变体），其后到点者发现路径已空、带「等待后
   继续」注记直接写入；前任迟到的 release 按身份归还不留残留。
3. 多团队并发：3 个会话并发跑到交付（2 个 AI 总管闭环各 1 成员 1 工单 +
   1 个用户总管 2 成员 2 工单），断言各自终态正确、team_id 唯一、
   频道/工单/落库消息按 team_id 隔离不串场。

既有覆盖与本文件的分工（只加压力规模，不重复等价子项）：
- test_subagent.py 已覆盖「满员收排队 + 单个补位 + 排队满拒收 + 取消不留
  僵尸」（规模 1-2 个任务）；本文件放大到 10 个任务并断言全队列消化、
  出队顺序与叫醒注记顺序。
- test_write_leases.py 已覆盖单冲突等待/超时接管/祖先重叠/取消泄漏；
  本文件做 N 方同路径竞争与超时路径下的多写方收尾。
- test_team.py 已覆盖单团队全流程与 AI 总管闭环；本文件做 3 团队并发与
  team_id 归组隔离。
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

from skysheep.core.subagent import TaskManager
from skysheep.core.team import DirectorSpec, TeamMemberSpec, TeamOrchestrator
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security import leases

# ---------- 公共小件 ----------


def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


def spec(name: str, provider, model: str = "m") -> TeamMemberSpec:
    return TeamMemberSpec(
        name=name, provider_name=f"p-{name}", model=model, provider=provider,
    )


def _team_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        max_members=3, member_timeout_s=30, max_rounds=40, redo_limit=2, stall_limit=5,
    )


class _StartOrderFake(FakeProvider):
    """在首次模型调用时把收到的 prompt 记进共享日志（记录任务真正开跑的顺序）。"""

    def __init__(self, scripted: list, start_log: list) -> None:
        super().__init__(scripted)
        self._start_log = start_log

    async def stream(self, messages, tool_schemas, effort=None):
        self._start_log.append(messages[-1].text)
        async for delta in super().stream(messages, tool_schemas, effort):
            yield delta


# ---------- 线一：后台子代理满员排队 ----------


async def test_background_subagents_queue_and_drain_in_order(tmp_path):
    """max_concurrent=2 一次派 10 个后台任务：全部完成、出队 FIFO、
    任务簿终态一致无幽灵、叫醒注记按完成序注入一批取走。"""
    total = 10
    start_log: list[str] = []  # prompt 出现序 = 任务从队列开跑的顺序
    finish_order: list[str] = []  # task_finished 广播序 = 完成顺序

    def emit(ev: dict) -> None:
        if ev.get("kind") == "task_finished":
            finish_order.append(ev["task_id"])

    def factory() -> FakeProvider:
        return _StartOrderFake([[TextBlock(text="REPORT: OK")]], start_log)

    tasks = TaskManager(
        provider_factory=factory,
        working_dir=tmp_path,
        max_concurrent=2,
        state_path=tmp_path / "subagent_tasks.json",
        event_emitter=emit,
    )
    ids = [
        tasks.start_background("explore", f"压力任务 P{i:02d}", session_id="sess-stress")
        for i in range(total)
    ]
    assert len(set(ids)) == total
    # 立即开跑的只有 2 个，其余 8 个进队列（满员排队而非报错）
    assert sum(1 for r in tasks._tasks.values() if r.status == "running") == 2
    assert sum(1 for r in tasks._tasks.values() if r.status == "queued") == total - 2

    # 全部跑到终态（wait_for 包整段防死锁）
    waits = [tasks.status(tid).done_event.wait() for tid in ids]
    await asyncio.wait_for(asyncio.gather(*waits), timeout=30)

    # 终态一致：全部 done、结果正确、时间戳齐备
    recs = [tasks.status(tid) for tid in ids]
    assert all(r.status == "done" for r in recs)
    assert all(r.result == "REPORT: OK" for r in recs)
    assert all(r.error is None for r in recs)
    assert all(r.started_at > 0 and r.finished_at >= r.started_at for r in recs)

    # 队列消化完、无幽灵：内存任务簿无 running/queued 残留，记录数恰为 total
    assert tasks._queue == []
    assert len(tasks._tasks) == total
    assert not [r for r in tasks._tasks.values() if r.status in ("running", "queued")]

    # 出队顺序严格 FIFO：前 2 个立即开跑，其后按补位序消化队列
    assert start_log == [f"压力任务 P{i:02d}" for i in range(total)]

    # 叫醒注记按完成序注入（注记入桶与 task_finished 广播在同一同步段），
    # 一批取走后不再重复
    note = tasks.pop_turn_note("sess-stress")
    assert note.startswith("（系统提示：")
    note_ids = re.findall(r'check_task\(task_id="([0-9a-f]+)"\)', note)
    assert note_ids == finish_order
    assert sorted(note_ids) == sorted(ids)
    assert len(set(note_ids)) == total
    assert tasks.pop_turn_note("sess-stress") == ""

    # 落盘任务簿一致：10 条终态记录、状态全 done、结果与内存一致
    data = json.loads((tmp_path / "subagent_tasks.json").read_text(encoding="utf-8"))
    persisted = {t["id"]: t for t in data["tasks"]}
    assert set(persisted) == set(ids)
    assert {t["status"] for t in persisted.values()} == {"done"}
    assert all(t["result"] == "REPORT: OK" for t in persisted.values())
    assert all(t["error"] in ("", None) for t in persisted.values())


# ---------- 线二：写租约竞争 ----------


async def test_write_lease_contention_serializes_eight_writers(tmp_path):
    """8 个并发写方对同一文件依次获得→修改→归还：获得顺序 FIFO、
    每次修改都不丢、等待者的注记为「已等待后继续」变体、结束表清空。"""
    hub = leases.WriteLeaseHub()
    path = tmp_path / "shared.txt"
    total = 8
    order: list[int] = []
    hold_gate = asyncio.Event()

    async def writer(i: int) -> str:
        lease = await hub.claim([(path, "shared.txt")], owner=f"s{i}", wait_s=5.0)
        try:
            order.append(i)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"writer-{i}\n")
            if i == 0:
                # 首个写方持锁握手等待：等其余写方全部完成首次 claim 并在
                # 事件上停稳后再放手（纯 asyncio 同步，不依赖墙钟），
                # 逼出真实的排队竞争而不是「释放早于排队」的空跑。
                await hold_gate.wait()
        finally:
            lease.release()
        return lease.note

    first = asyncio.create_task(writer(0))
    for _ in range(50):
        if hub._entries:
            break
        await asyncio.sleep(0)
    assert any(e["owner"] == "s0" for e in hub._entries.values()), "首个写方未领到租约"
    rest = [asyncio.create_task(writer(i)) for i in range(1, total)]
    for _ in range(4 * total):  # 让每个等待者跑完首次 claim、在租约事件上停稳
        await asyncio.sleep(0)
    hold_gate.set()

    notes = await asyncio.wait_for(asyncio.gather(first, *rest), timeout=20)

    assert notes[0] == ""  # 首个写方无冲突
    for note in notes[1:]:
        assert "并行写入" in note and "已等待后继续" in note
    assert order == list(range(total)), "写方应按排队顺序依次获得租约"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [f"writer-{i}" for i in range(total)], "每次修改都不得丢失"
    assert hub._entries == {}


async def test_write_lease_timeout_takeover_when_holder_never_releases(tmp_path):
    """占用方不归还时的超时放行路径（按 leases.py 实际语义断言）：
    每个后来者都放行写入（fail open）、注记必为冲突变体且恰有一个对持有方
    超时接管（「未结束」变体——其后到点者发现路径已空，落到「等待后继续」）；
    前任持有者迟到的 release 按身份归还不留残留。"""
    hub = leases.WriteLeaseHub()
    path = tmp_path / "contested.txt"
    holder = await hub.claim([(path, "contested.txt")], owner="s-holder")
    total = 5

    async def late_writer(i: int) -> str:
        lease = await hub.claim([(path, "contested.txt")], owner=f"w{i}", wait_s=0.2)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"late-{i}\n")
        finally:
            lease.release()
        return lease.note

    try:
        notes = await asyncio.wait_for(
            asyncio.gather(*(late_writer(i) for i in range(total))), timeout=15
        )
    finally:
        holder.release()  # 迟到归还：自己的登记已被接管，不得误删现任租约

    assert all("并行写入" in note for note in notes)
    assert sum(1 for note in notes if "未结束" in note) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == sorted(f"late-{i}" for i in range(total)), "每个写方都放行且恰好一次"
    assert hub._entries == {}


# ---------- 线三：多团队并发到交付 ----------


def _director_script(tag: str, member: str, title: str) -> FakeProvider:
    """AI 总管闭环脚本：建单 → 验收 → 交付（与 test_team.py 闭环主路径同形）。"""
    return FakeProvider([
        [TextBlock(text=f"{tag}：分工方案"), ToolUseBlock(
            id=f"{tag}-a1", name="team_assign",
            input={"title": title, "assignee": member, "type": "exec", "accept": "有结论"})],
        [TextBlock(text="建单完成，等队员报告")],
        [TextBlock(text="对照验收标准：合格"), ToolUseBlock(
            id=f"{tag}-a2", name="team_accept", input={"task_id": "T1"})],
        [TextBlock(text="验收通过，全部工单完成")],
        [TextBlock(text=f"《交付说明》：{title} 已完成。"), ToolUseBlock(
            id=f"{tag}-d1", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])


async def test_multi_team_concurrent_delivery_isolated_by_team_id():
    """3 个会话并发跑到交付：各自终态正确；team_id 唯一；频道、工单板与
    message_sink 落库消息按 team_id 归组，互不串场。"""
    sink_calls: list[tuple[str, object]] = []

    def sink(team_id: str, msg) -> None:
        sink_calls.append((team_id, msg))

    def make_ai_orch(tag: str, member: str, title: str, session: str):
        events, emit = collector()
        orch = TeamOrchestrator(
            emit=emit, cfg=_team_cfg(), director_mode="ai",
            director=DirectorSpec(provider_name="dp", model="fake-1",
                                  provider=_director_script(tag, member, title)),
            session_id=session, message_sink=sink,
        )
        return events, orch

    events_a, orch_a = make_ai_orch("甲总", "甲研", "甲队-调研报告", "sess-a")
    events_b, orch_b = make_ai_orch("乙总", "乙研", "乙队-综述报告", "sess-b")
    await orch_a.create_team([spec("甲研", FakeProvider([[TextBlock(text="甲研报告：调研完成，结论 A")]]))])
    await orch_b.create_team([spec("乙研", FakeProvider([[TextBlock(text="乙研报告：综述完成，结论 B")]]))])

    async def run_user_team():
        """用户总管团队：2 成员 2 工单，点名唤醒后推进工单并交付。"""
        events_c, emit = collector()
        orch = TeamOrchestrator(
            emit=emit, cfg=_team_cfg(), session_id="sess-c", message_sink=sink,
        )
        await orch.create_team([
            spec("丙一", FakeProvider([[TextBlock(text="丙一报告：纪要整理完成")]])),
            spec("丙二", FakeProvider([[TextBlock(text="丙二报告：素材归档完成")]])),
        ])
        await orch.task_add("丙队-整理纪要", "丙一", type="exec")
        await orch.task_update("T1", "in_progress")
        await orch.task_add("丙队-归档素材", "丙二", type="advisor")
        turned = await orch.handle_user_message("@丙一 @丙二 各自推进并汇报")
        await orch.task_update("T1", "review")
        await orch.task_update("T1", "done")
        delivered = await orch.finish("done")
        return events_c, orch, turned, delivered

    (res_a, res_b, res_c) = await asyncio.wait_for(asyncio.gather(
        orch_a.run_auto_turn("目标：甲队产出调研报告"),
        orch_b.run_auto_turn("目标：乙队产出综述报告"),
        run_user_team(),
    ), timeout=60)
    events_c, orch_c, turned_c, delivered_c = res_c

    # ---- 各自终态正确 ----
    assert res_a["finished"]["status"] == "done" and res_b["finished"]["status"] == "done"
    assert delivered_c["status"] == "done"
    for orch, member in ((orch_a, "甲研"), (orch_b, "乙研")):
        assert orch.rounds_used == 4  # 3 个总管轮 + 1 个成员唤醒轮
        task = orch.board.get("T1")
        assert (task.status, task.assignee) == ("done", member)
    assert res_a["posted_seqs"] == [1] and res_b["posted_seqs"] == [1]
    assert [w["member"] for w in res_a["woke"]] == ["甲研"]
    assert [w["member"] for w in res_b["woke"]] == ["乙研"]
    # 用户总管团队：两名队员按名册顺序各唤醒一轮，工单一完一留（未尽事项）
    assert [w["member"] for w in turned_c["woke"]] == ["丙一", "丙二"]
    assert all(w["status"] == "done" for w in turned_c["woke"])
    assert turned_c["posted_seqs"] == [1, 2]  # 两个点名各发一条定向副本
    assert (orch_c.board.get("T1").status, orch_c.board.get("T2").status) == ("done", "pending")
    summary_c = delivered_c["summary"]
    assert "《交付说明》" in summary_c
    assert "T1「丙队-整理纪要」（丙一）" in summary_c
    assert "未尽事项" in summary_c and "T2「丙队-归档素材」" in summary_c

    # ---- team_id 唯一 ----
    team_ids = {orch_a.team_id, orch_b.team_id, orch_c.team_id}
    assert len(team_ids) == 3
    assert all(len(t) == 32 for t in team_ids)

    # ---- 频道 / 工单板按 team_id 隔离不串场 ----
    profile = {
        orch_a.team_id: ({"甲研"}, {"甲队-调研报告"}),
        orch_b.team_id: ({"乙研"}, {"乙队-综述报告"}),
        orch_c.team_id: ({"丙一", "丙二"}, {"丙队-整理纪要", "丙队-归档素材"}),
    }
    for orch in (orch_a, orch_b, orch_c):
        own_members, _own_titles = profile[orch.team_id]
        foreign: set[str] = set()
        for tid, (members, titles) in profile.items():
            if tid != orch.team_id:
                foreign |= members | titles
        for msg in orch.channel.messages:
            hit = next((f for f in foreign if f in msg.text), None)
            assert hit is None, f"频道串场：{orch.team_id} 的消息含他人内容「{hit}」"
        for t in orch.board.tasks():
            assert t.assignee in own_members
            assert not any(f in t.title for f in foreign)
        assert not any(f in orch.finished["summary"] for f in foreign)

    # ---- message_sink 落库按 team_id 归组：逐条对应本队频道，不混册 ----
    by_team: dict[str, list] = {}
    for team_id, msg in sink_calls:
        by_team.setdefault(team_id, []).append(msg)
    assert set(by_team) == team_ids
    for orch in (orch_a, orch_b, orch_c):
        msgs = by_team[orch.team_id]
        assert [m.seq for m in msgs] == [m.seq for m in orch.channel.messages]

    # ---- 事件流各自完整：一次建队一次交付，全程零权限事件（纯文本协作）----
    for events in (events_a, events_b, events_c):
        assert [e.status for e in events if e.kind == "team_finished"] == ["done"]
        assert len([e for e in events if e.kind == "team_started"]) == 1
        assert not any(e.kind.startswith("permission_") for e in events)
