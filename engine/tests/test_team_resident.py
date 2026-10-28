"""常驻团队（T3.2）测试：注册表落盘 / 工单板快照回放 / 频道续号 /
restore_team 恢复装配 / WS 生命周期方法（resident_set·clear）/ 定时触发复用。

常驻是「跨会话唤醒同一支队伍」：team_id 是唯一锚（频道历史按 team_id 落
team_messages 表、工单板快照随登记落 team_resident.json），定时团队触发按
（模板，项目）匹配复用而不是每次重建。引擎层复用 test_team.py 的注入写法；
cron 复用走 run_cron_task_core 全链路（脚本池顺序 = 成员 → 总管）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skysheep.core.team import TeamBoard, TeamChannel, TeamMemberSpec, TeamOrchestrator
from skysheep.core.team_resident import (
    RESIDENT_TEAMS_KEEP,
    ResidentTeamEntry,
    ResidentTeamStore,
)
from skysheep.core.team_store import TeamError
from skysheep.events import TeamMessage
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend
from skysheep.session.store import SessionStore


def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


def make_orch(**kwargs) -> tuple[list, TeamOrchestrator]:
    events, emit = collector()
    orch = TeamOrchestrator(emit=emit, **kwargs)
    return events, orch


def spec(name: str, provider, model: str = "m",
         provider_name: str = "") -> TeamMemberSpec:
    return TeamMemberSpec(
        name=name, provider_name=provider_name or f"p-{name}", model=model,
        provider=provider,
    )


# ---------- 注册表：落盘 / 容错 / 增删查 ----------


def test_resident_store_roundtrip_and_bad_file(tmp_path):
    """注册表落盘往返：upsert 落盘 → 新实例 load 还原；坏文件/坏条目不致命
    （回空册 / 跳过条目，下次保存整体覆盖）——沿模板册 team_store 同姿态。"""
    path = tmp_path / "team_resident.json"
    store = ResidentTeamStore(path)
    store.load()
    assert store.teams == [] and store.list() == []

    store.upsert(ResidentTeamEntry(
        team_id="abc", name="夜班小队", template="夜班小队", project_id=3,
        roster=["甲", "乙"], last_seq=7, wake_count=2,
    ))
    store.upsert(ResidentTeamEntry(team_id="def", name="另一队"))

    fresh = ResidentTeamStore(path)
    fresh.load()
    assert [t.team_id for t in fresh.teams] == ["abc", "def"]
    got = fresh.get("abc")
    assert got is not None
    assert got.name == "夜班小队" and got.template == "夜班小队"
    assert got.project_id == 3 and got.roster == ["甲", "乙"]
    assert got.last_seq == 7 and got.wake_count == 2

    # 坏 JSON 不致命：回空册
    path.write_text("not json", encoding="utf-8")
    broken = ResidentTeamStore(path)
    broken.load()
    assert broken.teams == []

    # 单条形状不对：跳过坏条目，好条目照常载入
    path.write_text(json.dumps({"teams": [
        {"team_id": "ok1"},
        {"team_id": 42, "roster": "不是列表"},  # 字段类型不对 → ValidationError
        "不是字典",
    ]}), encoding="utf-8")
    mixed = ResidentTeamStore(path)
    mixed.load()
    assert [t.team_id for t in mixed.teams] == ["ok1"]


def test_resident_store_upsert_idempotent_and_requires_id(tmp_path):
    """upsert 语义：team_id 是唯一键（同 id 原位覆盖、created_at 保留首次声明
    时刻）；空 id 拒绝——常驻的锚就是 team_id，频道落库与回放都凭它归组。"""
    store = ResidentTeamStore(tmp_path / "team_resident.json")
    store.load()

    first = store.upsert(ResidentTeamEntry(team_id="t1", name="一队"))
    created = first.created_at
    assert created > 0

    second = store.upsert(ResidentTeamEntry(
        team_id="t1", name="一队", roster=["甲"], wake_count=3))
    assert [t.team_id for t in store.teams] == ["t1"]
    assert second.created_at == created, "重复登记保留首次声明时刻"
    assert second.wake_count == 3

    with pytest.raises(TeamError):
        store.upsert(ResidentTeamEntry(team_id="  "))


def test_resident_store_remove_missing_raises(tmp_path):
    """解除未登记的队伍报错（与模板删除同姿态，不静默）。"""
    store = ResidentTeamStore(tmp_path / "team_resident.json")
    store.load()
    store.upsert(ResidentTeamEntry(team_id="t1"))
    removed = store.remove("t1")
    assert removed.team_id == "t1" and store.teams == []
    with pytest.raises(TeamError):
        store.remove("t1")
    assert store.is_resident("t1") is False


def test_resident_store_find_for_template(tmp_path):
    """定时触发匹配：同模板 + 同项目取最近更新；空模板（未绑定的登记）
    与不同项目都不参与匹配。"""
    store = ResidentTeamStore(tmp_path / "team_resident.json")
    store.load()
    assert store.find_for_template("夜班小队", 1) is None
    assert store.find_for_template("", 1) is None

    store.upsert(ResidentTeamEntry(team_id="a", template="夜班小队", project_id=1))
    store.teams[0].updated_at = 100.0  # upsert 打的时间戳手动拨旧，造「更旧」
    store.upsert(ResidentTeamEntry(
        team_id="b", template="夜班小队", project_id=1, wake_count=1))
    store.upsert(ResidentTeamEntry(
        team_id="c", template="夜班小队", project_id=2))
    store.upsert(ResidentTeamEntry(team_id="d", template="", project_id=1))

    hit = store.find_for_template("夜班小队", 1)
    assert hit is not None and hit.team_id == "b", "同模板同项目取最近更新"
    assert store.find_for_template("夜班小队", 2).team_id == "c"
    assert store.find_for_template("别的模板", 1) is None


def test_resident_store_eviction_cap(tmp_path):
    """卫生闸：超过 RESIDENT_TEAMS_KEEP 条时按 updated_at 从旧到新淘汰
    （常驻是显式声明，正常触不到；上限只为写盘体积有界）。"""
    store = ResidentTeamStore(tmp_path / "team_resident.json")
    store.load()
    store.teams = [
        ResidentTeamEntry(team_id=f"t{i:03d}", updated_at=float(i))
        for i in range(RESIDENT_TEAMS_KEEP + 3)
    ]
    store.save()
    fresh = ResidentTeamStore(tmp_path / "team_resident.json")
    fresh.load()
    assert len(fresh.teams) == RESIDENT_TEAMS_KEEP
    ids = [t.team_id for t in fresh.teams]
    assert "t000" not in ids and "t002" not in ids, "最旧的被淘汰"
    assert ids[-1] == f"t{RESIDENT_TEAMS_KEEP + 2:03d}"


# ---------- 工单板快照回放 / 频道续号 ----------


def test_board_restore_replays_snapshot_and_advances_counter():
    """restore：原 id/状态/指派上板、自增工单号越过历史号；坏条目逐条跳过
    （指派不在名册 / 状态未知 / 缺标题），deps 收敛到实际回放的 id。"""
    src = TeamBoard(members=["甲", "乙"])
    src.add("巡检目录", "甲")
    src.add("写报告", "乙", deps=["T1"])
    src.set_status("T1", "in_progress")
    src.set_status("T1", "review")
    src.set_status("T1", "done")
    snapshot = [t.to_dict() for t in src.tasks()]

    dst = TeamBoard(members=["甲", "乙"])
    restored, skipped = dst.restore(snapshot + [
        {"id": "T9", "title": "幽灵单", "assignee": "路人", "status": "done"},
        {"id": "T8", "title": "怪状态", "assignee": "甲", "status": "不存在"},
        {"id": "", "title": "缺 id", "assignee": "甲", "status": "pending"},
    ])
    assert (restored, skipped) == (2, 3)
    assert [t.id for t in dst.tasks()] == ["T1", "T2"]
    assert dst.get("T1").status == "done" and dst.get("T1").assignee == "甲"
    assert dst.get("T2").deps == ["T1"], "指向已回放工单的依赖保留"
    assert dst.deps_satisfied(dst.get("T2")) is True

    follow = dst.add("跟进单", "甲")
    assert follow.id == "T3", "自增序号越过历史号"

    # 快照行被跳过时，指向它的依赖一并清除（否则依赖判定永远不满）
    bad = TeamBoard(members=["甲"])
    bad.restore([
        {"id": "T1", "title": "好单", "assignee": "甲", "status": "done"},
        {"id": "T2", "title": "被跳过的单", "assignee": "路人",
         "status": "pending", "deps": ["T1"]},
        {"id": "T3", "title": "带坏引用", "assignee": "甲",
         "status": "pending", "deps": ["T2", "T1", "T2"]},
    ])
    assert bad.get("T3").deps == ["T1"], "坏引用清除、好引用保留且去重"
    assert bad.deps_satisfied(bad.get("T3")) is True


def test_channel_continue_from_resumes_seq_watermark():
    """continue_from：新消息从水位之后续号（team_messages 的 (team_id, seq)
    不与历史行冲突）；水位回拨无效；历史消息不进内存频道。"""
    ch = TeamChannel()
    ch.post("甲", text="一")
    ch.post("甲", text="二")
    assert ch.messages[-1].seq == 2

    ch.continue_from(10)
    assert ch.post("甲", text="三").seq == 11

    ch.continue_from(3)
    assert ch.post("甲", text="四").seq == 12, "水位回拨不生效"


# ---------- 编排器：restore_team 恢复装配 ----------


async def test_restore_team_keeps_id_replays_board_and_announces():
    """restore_team：沿用原 team_id 续频道、按快照回放工单板、发 system 唤醒
    消息（走 post → 事件路径）、不发 TeamStarted（不是建队）；工单自增越过
    历史号；空 team_id / 空名册拒绝。"""
    events1, orch1 = make_orch()
    await orch1.create_team([spec("甲", FakeProvider([[TextBlock(text="x")]]))])
    first_id = orch1.team_id
    assert first_id
    orch1.board.add("巡检目录", "甲")
    orch1.board.set_status("T1", "in_progress")
    orch1.board.set_status("T1", "review")
    orch1.board.set_status("T1", "done")
    board_snapshot = [t.to_dict() for t in orch1.board.tasks()]
    orch1.channel.post("system", text="建队注记")
    seq_after_first = orch1.channel.messages[-1].seq

    events2, orch2 = make_orch()
    roster = await orch2.restore_team(
        first_id, [spec("甲", FakeProvider([[TextBlock(text="y")]]))],
        tasks=board_snapshot, last_seq=seq_after_first,
        wake_note="由定时任务「夜间巡检」唤醒（第 2 次复用）。",
    )
    assert [m["name"] for m in roster] == ["甲"], "返回名册 dict 形状同建队"
    assert orch2.team_id == first_id, "恢复沿用原 team_id（频道史不换册）"
    kinds = [type(e).__name__ for e in events2]
    assert "TeamStarted" not in kinds, "恢复不是建队，不发 TeamStarted"
    assert orch2.board.get("T1") is not None and orch2.board.get("T1").status == "done"
    wake = [e for e in events2 if isinstance(e, TeamMessage)
            and e.from_member == "system"]
    assert wake, "唤醒宣告走正常频道消息路径"
    assert wake[0].seq == seq_after_first + 1, "频道 seq 从注册表水位续排"
    assert "常驻团队唤醒" in wake[0].text and "回放 1 单" in wake[0].text
    assert "第 2 次复用" in wake[0].text and "夜间巡检" in wake[0].text

    assert orch2.board.add("跟进单", "甲").id == "T2", "工单自增越过历史号"

    with pytest.raises(TeamError):
        await orch2.restore_team("", [spec("甲", FakeProvider([]))])
    with pytest.raises(TeamError):
        await orch2.restore_team("some-id", [])


async def test_restore_team_normalizes_roster_like_create():
    """恢复路径的名册规整与建队同款：重名不同模型补序号、缺名沿 provider 名。"""
    _, orch = make_orch()
    await orch.create_team([
        spec("甲", FakeProvider([])),
        spec("甲", FakeProvider([]), model="mb"),  # 重名不同模型 → 补序号
        spec("", None, provider_name="fc"),  # 缺名沿 provider 名
    ])
    assert [m.name for m in orch.roster] == ["甲", "甲2", "fc"]

    _, orch2 = make_orch()
    await orch2.restore_team("rid", [
        TeamMemberSpec(name="甲", provider_name="pa", model="m"),
        TeamMemberSpec(name="甲", provider_name="pb", model="mb"),
        TeamMemberSpec(name="", provider_name="fc", model="m"),
    ])
    assert [m.name for m in orch2.roster] == ["甲", "甲2", "fc"], "与建队同款规整"


# ---------- 后端：WS 生命周期方法 ----------


def test_team_resident_ws_lifecycle(home):
    """team.resident_set / team.resident_clear / team.get 常驻标记：
    登记落盘隔离 home、幂等刷新、绑定不存在的模板被拒、解除未登记被拒、
    无活动团队被拒；分发层两条方法 local_only（写全局册仅本机）。"""
    from test_team import make_team_client, recv_until

    from skysheep.server.app import _WS_METHODS

    entry = _WS_METHODS.get("team.resident_set")
    assert entry is not None and entry.local_only, "resident_set 必须本机专属"
    entry = _WS_METHODS.get("team.resident_clear")
    assert entry is not None and entry.local_only, "resident_clear 必须本机专属"

    scripts = [
        [[TextBlock(text="普通回合")]],
        [[TextBlock(text="调研完成：结论 A")]],
    ]
    with make_team_client(home, scripts) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "@小研 先做调研", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        recv_until(ws, "r1")

        ws.send_json({"id": "g1", "method": "team.get", "params": {}})
        got = recv_until(ws, "g1")["result"]
        assert got["active"] is True and got["team"]["resident"] is False
        team_id = got["team"]["team_id"]
        assert team_id

        # 登记（不绑模板）：回包带 entry，落盘 team_resident.json
        ws.send_json({"id": "s1", "method": "team.resident_set", "params": {}})
        r1 = recv_until(ws, "s1")["result"]
        assert r1["resident"] is True and r1["team_id"] == team_id
        assert r1["entry"]["template"] == "" and r1["entry"]["roster"] == ["小研"]
        f = home / "home" / "team_resident.json"
        assert f.exists()
        saved = json.loads(f.read_text(encoding="utf-8"))
        assert [t["team_id"] for t in saved["teams"]] == [team_id]

        # team.get 的常驻标记点亮
        ws.send_json({"id": "g2", "method": "team.get", "params": {}})
        assert recv_until(ws, "g2")["result"]["team"]["resident"] is True

        # 幂等重复登记：仍是一条（原位覆盖）
        ws.send_json({"id": "s2", "method": "team.resident_set", "params": {}})
        recv_until(ws, "s2")
        assert len(json.loads(f.read_text(encoding="utf-8"))["teams"]) == 1

        # 绑定不存在的模板被拒（防悬空名永不匹配）
        ws.send_json({"id": "s3", "method": "team.resident_set",
                      "params": {"template": "查无此队"}})
        r3 = recv_until(ws, "s3")
        assert r3["ok"] is False and "查无此队" in r3["error"]

        # 绑定真实模板：登记条目随刷新带上模板名
        ws.send_json({"id": "t1", "method": "team.template_save", "params": {
            "name": "夜班小队", "director_mode": "user",
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        assert recv_until(ws, "t1")["ok"]
        ws.send_json({"id": "s4", "method": "team.resident_set",
                      "params": {"template": "夜班小队"}})
        assert recv_until(ws, "s4")["result"]["entry"]["template"] == "夜班小队"

        # 无活动团队的会话拒绝登记（本机连接显式传断言的 sid 也照拒）
        ws.send_json({"id": "s5", "method": "team.resident_set",
                      "params": {"session_id": "no-such-session"}})
        r5 = recv_until(ws, "s5")
        assert r5["ok"] is False and "没有进行中的团队" in r5["error"]

        # 解除：回 resident False；再解除报错
        ws.send_json({"id": "c1", "method": "team.resident_clear", "params": {}})
        c1 = recv_until(ws, "c1")["result"]
        assert c1["resident"] is False and c1["team_id"] == team_id
        assert json.loads(f.read_text(encoding="utf-8"))["teams"] == []
        ws.send_json({"id": "c2", "method": "team.resident_clear", "params": {}})
        c2 = recv_until(ws, "c2")
        assert c2["ok"] is False and "没有登记为常驻" in c2["error"]


def test_team_resident_without_active_team_rejected(home):
    """没有活动团队时 resident_set / clear 报参数错（不隐式登记任何东西）。"""
    backend = ServerBackend(working_dir=home / "proj",
                            provider_factory=lambda: FakeProvider([]))
    backend.resident_teams.load()
    with pytest.raises(TeamError):
        asyncio.run(backend.team_resident_set({}))
    with pytest.raises(TeamError):
        asyncio.run(backend.team_resident_clear({}))


# ---------- 定时触发复用常驻队伍 ----------


def _director_loop(assign_title: str, accept_task: str, tag: str = "d") -> list:
    """AI 总管闭环脚本（两段式）：建单轮（纯文本收轮，把控制交回自动循环
    ——派发与成员唤醒发生在总管轮之间）→ 验收交付轮。注意总管一次
    run_turn 的工具循环会连续消费多条脚本，验收必须等下一轮才能打在
    待验收状态上。"""
    return [
        [TextBlock(text="分工方案"), ToolUseBlock(
            id=f"{tag}1", name="team_assign",
            input={"title": assign_title, "assignee": "队员0", "type": "exec"})],
        [TextBlock(text="已建单，等待队员完成")],
        [TextBlock(text="对照验收标准：合格"), ToolUseBlock(
            id=f"{tag}2", name="team_accept", input={"task_id": accept_task})],
        [TextBlock(text="全部完成，交付"), ToolUseBlock(
            id=f"{tag}3", name="team_deliver", input={})],
    ]


async def test_cron_team_without_resident_creates_fresh(home):
    """无登记照旧建队（§14 口径不变）：定时触发不隐式登记常驻——
    是否常驻由用户在团队卡上显式声明。"""
    from test_team import _cron_team_backend, _cron_team_task, _team_template

    from skysheep.server.backend_parts.automation import run_cron_task_core

    store = await SessionStore(home / "cron-resident.db").connect()
    try:
        task = await _cron_team_task(store, home)
        backend = _cron_team_backend(
            home, store,
            [FakeProvider([[TextBlock(text="巡检完成")]]),
             FakeProvider(_director_loop("巡检目录", "T1", "d"))])
        _team_template(backend, "夜班小队")
        backend.resident_teams.load()

        final = await run_cron_task_core(store, task["id"],
                                         execute=backend._execute_cron_turn)
        assert final["last_status"] == "ok"
        assert backend.resident_teams.teams == [], "不隐式登记常驻"
    finally:
        await store.close()


async def test_cron_team_reuses_resident_team(home):
    """复用闭环：登记常驻后定时触发唤醒同一支队伍——原 team_id 续频道
    （system 唤醒宣告落库、seq 续号）、工单板延续（自增越过历史号 T1→T2）、
    收尾回写注册表（wake_count+1、板面刷新）；装配路径与防失控上限照旧。"""
    from test_team import _cron_team_backend, _cron_team_task, _team_template

    from skysheep.server.backend_parts.automation import run_cron_task_core

    store = await SessionStore(home / "cron-reuse.db").connect()
    try:
        task = await _cron_team_task(store, home)
        backend = _cron_team_backend(
            home, store,
            [FakeProvider([[TextBlock(text="巡检完成：未见异常")]]),
             FakeProvider(_director_loop("巡检目录", "T1", "a")),
             FakeProvider([[TextBlock(text="复查完成：仍无异常")]]),
             FakeProvider(_director_loop("复查目录", "T2", "b"))])
        _team_template(backend, "夜班小队")
        backend.resident_teams.load()

        # 第一轮：正常建队跑完（产出 T1 done），不登记常驻
        final = await run_cron_task_core(store, task["id"],
                                         execute=backend._execute_cron_turn)
        assert final["last_status"] == "ok"

        # 手工登记常驻：第一轮跑出的 T1 快照 + team_id 从库里的消息行取
        proj = await store.get_project(task["project_id"])
        sessions = await store.list_sessions(proj.id)
        sid1 = next(s.id for s in sessions if (s.title or "").startswith("⏰ "))
        cur = await store._db.execute(
            "SELECT DISTINCT team_id FROM team_messages WHERE session_id = ?",
            (sid1,))
        team_id = (await cur.fetchone())[0]
        backend.resident_teams.upsert(ResidentTeamEntry(
            team_id=team_id, name="夜班小队", template="夜班小队",
            project_id=proj.id, roster=["队员0"],
            board=[{"id": "T1", "title": "巡检目录", "assignee": "队员0",
                    "type": "exec", "deps": [], "accept": "", "status": "done",
                    "redo": 0, "created_at": 1.0, "detail": "", "source": {},
                    "auto_dispatch": True}],
            last_seq=0,
        ))

        # 第二轮：唤醒常驻队伍而不是重建（第一轮跑完已重排 next_run，
        # 定时判定不认——显式 force 跳过到点判定直接触发）
        final = await run_cron_task_core(store, task["id"], force=True,
                                         execute=backend._execute_cron_turn)
        assert final["last_status"] == "ok"

        # 频道延续：同一 team_id 名下出现 system 唤醒宣告（落库、可回放）
        msgs = await store.list_team_messages(team_id)
        sys_rows = [m for m in msgs if m["from_member"] == "system"
                    and m["msg_kind"] == "system"]
        assert sys_rows and "常驻团队唤醒" in sys_rows[-1]["text"]
        assert "回放 1 单" in sys_rows[-1]["text"], "T1 快照回放上板"
        assert "第 1 次复用" in sys_rows[-1]["text"] and "夜间巡检" in sys_rows[-1]["text"]

        # 第二轮总管新派的工单越过历史号（T1 已在板，新单是 T2）
        entry = backend.resident_teams.get(team_id)
        assert entry is not None and entry.wake_count == 1, "收尾回写唤醒计数"
        assert entry.project_id == proj.id and entry.last_seq > 0
        board_ids = {t["id"] for t in entry.board}
        assert board_ids == {"T1", "T2"}, f"板面延续且自增越号：{board_ids}"
        t2 = next(t for t in entry.board if t["id"] == "T2")
        assert t2["title"] == "复查目录" and t2["status"] == "done"
    finally:
        await store.close()
