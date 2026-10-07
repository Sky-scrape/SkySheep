"""团队功能测试：频道 / 工单状态机 / 成员执行 / 编排终态 / 重启中断 / WS 协议。

一期「用户总管 MVP」（docs/团队模式设计.md）。引擎层复用圆桌的写法：
FakeProvider / OrderProbe 注入 + collector 收事件断言事件流；协议层用
TestClient 进程内直调（真实 socket 的端到端在 test_ws_e2e.py）。
安全语义用例（权限来源标注、fail-closed 决策）用真实 PermissionGate +
真实 fs 工具，不 mock 门——门的判定路径正是被测对象。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skysheep.core.subagent import TaskRecord
from skysheep.core.team import (
    MSG_CHAR_LIMIT,
    SNAPSHOT_MESSAGE_TAIL,
    SNAPSHOT_MESSAGE_TEXT_CAP,
    DirectorSpec,
    HuddleRequest,
    TeamBoard,
    TeamChannel,
    TeamError,
    TeamMemberGate,
    TeamMemberSpec,
    TeamOrchestrator,
    TeamRejectArgs,
    TeamTemplate,
    TeamTemplateDirector,
    TeamTemplateMember,
    TeamTemplateStore,
    validate_team_template_name,
)
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.base import (
    Provider,
    ProviderDone,
    ProviderTextDelta,
    ProviderToolUse,
)
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import PermissionGate
from skysheep.security.leases import WriteLeaseHub
from skysheep.server import create_app
from skysheep.server.app import _h_team_get, _h_team_log, _h_team_stop
from skysheep.server.backend import (
    QueuedTurn,
    ServerBackend,
    SessionRuntime,
    StreamDeltaMerger,
)
from skysheep.session.store import SessionStore
from skysheep.tools import ChangeRecorder
from skysheep.tools.base import ToolContext, ToolRegistry
from skysheep.tools.fs import ListDirTool, WriteFileTool


def recv_until(ws, wanted_id=None, events=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


# ---------- 引擎层公共件 ----------


def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


class OrderProbe(Provider):
    """唤醒顺序探针：stream() 时把名字记进共享 log，并记录拿到的工具表大小。"""

    name, model = "probe", "probe-1"

    def __init__(self, tag: str, log: list, tool_counts: list) -> None:
        super().__init__()
        self.tag = tag
        self.log = log
        self.tool_counts = tool_counts
        self.calls: list = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.log.append(self.tag)
        self.tool_counts.append(len(tool_schemas))
        self.calls.append(list(messages))
        yield ProviderTextDelta(f"{self.tag}的汇报")
        yield ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7)


class HangingMember(Provider):
    """永远不产出（成员超时用）。"""

    name, model = "hang", "h1"

    async def stream(self, messages, tool_schemas, effort=None):
        await asyncio.sleep(30)
        yield  # pragma: no cover


class DeltaThenHang(Provider):
    """先吐一段发言再挂住（取消收尾测试）。"""

    name, model = "dh", "d1"

    async def stream(self, messages, tool_schemas, effort=None):
        yield ProviderTextDelta("部分发言")
        await asyncio.sleep(30)


def spec(name: str, provider, persona: str = "", model: str = "m",
         provider_name: str = "") -> TeamMemberSpec:
    return TeamMemberSpec(
        name=name, provider_name=provider_name or f"p-{name}", model=model,
        provider=provider, persona=persona,
    )


def make_orch(cfg=None, working_dir: Path | None = None,
              registry=None, gate=None) -> tuple[list, TeamOrchestrator]:
    events, emit = collector()
    orch = TeamOrchestrator(
        emit=emit, working_dir=working_dir, registry=registry, gate=gate, cfg=cfg,
    )
    return events, orch


# ---------- 频道：seq 单调 / 未读口径 / 截断落盘 ----------


@pytest.mark.asyncio
async def test_channel_seq_monotonic_and_unread_scope():
    ch = TeamChannel()
    ch.post("user", to_member="all", msg_kind="ruling", text="广播")
    seq_reserved = ch.reserve_seq()  # 预占：成员轮先于定稿知道编号
    ch.post("小研", to_member="director", msg_kind="report", text="汇报", seq=seq_reserved)
    ch.post("director", to_member="写手", msg_kind="assign", text="派工")
    assert [m.seq for m in ch.messages] == [1, 2, 3]  # 预占的编号不产生空洞

    # 未读口径：广播人人可见；to=director 只给总管；@定向只给目标；本人发言不算
    assert [m.seq for m in ch.unread_for("写手")] == [1, 3]
    assert [m.seq for m in ch.unread_for("小研")] == [1]
    assert [m.seq for m in ch.unread_for("director")] == [1, 2]  # 广播 + 队员汇报
    # 定向消息的渲染：自己 → 「我」；广播行带 @all
    assert "[#1] user → @all：广播" in ch.render_unread("写手")
    assert "[#3] director → 我：派工" in ch.render_unread("写手")
    assert "[#1] user → @all：广播" in ch.render_unread("小研")
    assert "我：派工" not in ch.render_unread("小研")  # 别人的定向消息不进未读
    ch.mark_read("写手")
    assert ch.unread_for("写手") == [] and ch.render_unread("写手") == ""


def test_channel_clip_long_message_with_report_fallback(tmp_path):
    reports = tmp_path / "reports"
    ch = TeamChannel(reports_dir=reports)
    long_text = "字" * 3000
    msg = ch.post("小研", to_member="director", msg_kind="report", text=long_text)
    assert len(msg.text) < 3000  # 频道里是截断摘录
    assert f"全文 {len(long_text)} 字符" in msg.text
    assert msg.text.endswith(f"，全文已写入 {Path(msg.report_path).name}）")
    assert msg.report_path, "落盘路径必须带回"
    saved = Path(msg.report_path)
    assert saved.parent == reports
    assert saved.read_text(encoding="utf-8") == long_text  # 全文原样落盘
    # 无落盘目录（快聊 / 未注入工作区）：只截断不落盘，不给假路径；
    # 注记文案与落盘成功路径一致、以闭合「）」收尾（评审项）
    ch2 = TeamChannel()
    msg2 = ch2.post("小研", text=long_text)
    assert msg2.report_path == ""
    assert f"全文 {len(long_text)} 字符，超过频道单条上限 {MSG_CHAR_LIMIT}）" in msg2.text
    assert msg2.text.endswith("）")
    # 落盘 OSError（目录被文件占位等）：同样退回整段截断、注记闭合
    blocked = tmp_path / "occupied"
    blocked.write_text("x", encoding="utf-8")
    ch3 = TeamChannel(reports_dir=blocked / "reports")
    msg3 = ch3.post("小研", text=long_text)
    assert msg3.report_path == ""
    assert f"全文 {len(long_text)} 字符，超过频道单条上限 {MSG_CHAR_LIMIT}）" in msg3.text
    assert msg3.text.endswith("）")
    # 限长内的消息原样进频道
    short = ch2.post("user", text="短消息")
    assert short.text == "短消息" and short.report_path == ""


# ---------- 工单状态机 ----------


def make_board() -> TeamBoard:
    return TeamBoard(redo_limit=2, members=["小研", "写手"])


def test_board_add_validation():
    board = make_board()
    with pytest.raises(TeamError, match="标题不能为空"):
        board.add("  ", "小研")
    with pytest.raises(TeamError, match="不在团队名册中"):
        board.add("写报告", "路人")
    with pytest.raises(TeamError, match="exec"):
        board.add("写报告", "小研", type="magic")
    with pytest.raises(TeamError, match="不存在"):
        board.add("写报告", "小研", deps=["T99"])
    task = board.add("写报告", "小研", accept=" 有结论 ")
    assert task.id == "T1" and task.status == "pending" and task.accept == "有结论"


def test_board_state_machine_and_redo_limit():
    board = make_board()
    board.add("写报告", "小研")
    # 正常流转：待办 → 进行中 → 待验收 → 完成
    assert board.set_status("T1", "in_progress").status == "in_progress"
    assert board.set_status("T1", "review").status == "review"
    assert board.set_status("T1", "done").status == "done"
    with pytest.raises(TeamError, match="不能从"):  # 完成是终态
        board.set_status("T1", "in_progress")
    # 打回：待验收 → 进行中，redo+1；超上限拒绝
    board.add("写文档", "写手")
    board.set_status("T2", "in_progress")
    board.set_status("T2", "review")
    assert board.set_status("T2", "in_progress").redo == 1
    board.set_status("T2", "review")
    assert board.set_status("T2", "in_progress").redo == 2
    board.set_status("T2", "review")
    with pytest.raises(TeamError, match="已被打回 2 次，达到上限"):
        board.set_status("T2", "in_progress")
    # 失败收尾：进行中/待验收 → error；error → in_progress（重派）可行
    board.mark_error("T2", reason="方案不可行")
    assert board.get("T2").status == "error"
    assert "失败原因：方案不可行" in board.get("T2").accept
    assert board.set_status("T2", "in_progress").status == "in_progress"
    # 待办 → 失败（补边）：依赖链卡死的待办工单可放弃（设计 §9 砍掉出口），
    # mark_error 与 set_status 同口径放行
    t3 = board.add("闲杂", "小研")
    assert board.mark_error(t3.id, reason="上游取消").status == "error"
    assert "失败原因：上游取消" in board.get(t3.id).accept
    t4 = board.add("另一个待办", "写手")
    assert board.set_status(t4.id, "error").status == "error"
    # 完成是终态：不能对已完成工单标记失败
    t5 = board.add("已完成", "小研")
    board.set_status(t5.id, "in_progress")
    board.set_status(t5.id, "review")
    board.set_status(t5.id, "done")
    with pytest.raises(TeamError, match="不能标记失败"):
        board.mark_error(t5.id)


def test_board_deps_block_dispatch():
    board = make_board()
    t1 = board.add("调研", "小研")
    board.add("写报告", "写手", deps=[t1.id])
    with pytest.raises(TeamError, match="依赖工单未完成"):
        board.set_status("T2", "in_progress")  # 依赖未完成不派发
    assert [t.id for t in board.dispatchable()] == ["T1"]
    board.set_status("T1", "in_progress")
    board.set_status("T1", "review")
    board.set_status("T1", "done")
    assert board.set_status("T2", "in_progress").status == "in_progress"
    assert board.all_done() is False  # T2 还没完
    board.set_status("T2", "review")
    board.set_status("T2", "done")
    assert board.all_done() is True


# ---------- 建队：去重 / 补名 / 上限 ----------


@pytest.mark.asyncio
async def test_create_team_dedupe_rename_and_limit():
    cfg = SimpleNamespace(max_members=3, member_timeout_s=300, max_rounds=40, redo_limit=2)
    events, orch = make_orch(cfg=cfg)
    with pytest.raises(TeamError, match="至少需要一名队员"):
        await orch.create_team([])
    roster = await orch.create_team([
        spec("小研", object()),
        spec("小研", object()),  # 同名同模型 → 去重
        spec("小研", object(), model="mb"),  # 重名不同模型 → 补序号
        spec("", None, provider_name="fc"),  # 缺名沿 provider 名
        spec("写手", object()),
        spec("画手", object()),  # 超上限 → 截断
    ])
    assert [r["name"] for r in roster] == ["小研", "小研2", "fc"]
    assert roster[0] == {
        "index": 0, "name": "小研", "provider": "p-小研", "model": "m", "persona": "",
    }
    # 截断发 Notice（不静默丢人），TeamStarted 随后发、只带截断后的名册
    assert [e.kind for e in events] == ["notice", "team_started"]
    assert "上限为 3 名" in events[0].message
    assert [m["name"] for m in events[1].roster] == ["小研", "小研2", "fc"]
    assert events[1].director_mode == "user"
    assert orch.board.members == ["小研", "小研2", "fc"]  # 指派校验名册回填
    assert orch.active and orch.finished is None


@pytest.mark.asyncio
async def test_create_team_rejects_reserved_member_names():
    """保留字成员名（user/director/system/all，频道 from/to 词汇表）视同非法名：
    回退 provider 名或兜底「队员」，绝不占用——否则后端按 from_member 记的
    用量归属丢账、前端按 from 分流的气泡渲染错位；占用 all 还会让 @点名
    的定向副本以 to_member="all" 落簿、语义退化成广播（评审项）。"""
    cfg = SimpleNamespace(max_members=4, member_timeout_s=300, max_rounds=40, redo_limit=2)
    events, orch = make_orch(cfg=cfg)
    roster = await orch.create_team([
        spec("user", object(), provider_name="fa"),
        spec("director", object(), provider_name="fb"),
        spec("system", object(), provider_name="user"),  # 回退 provider 名仍是保留字
        spec("all", object(), provider_name="fc"),  # 广播词同样不能被成员占用
    ])
    names = [r["name"] for r in roster]
    assert names == ["fa", "fb", "队员", "fc"]
    assert not (set(names) & {"user", "director", "system", "all"})
    # 名册正常入册、无截断 Notice（四人未超上限）
    assert orch.board.members == names
    assert not any(e.kind == "notice" for e in events)
    # 被点名的队员照常唤醒：频道 from 词汇表干净，@点名解析不受影响
    events2, orch2 = make_orch(cfg=cfg)
    await orch2.create_team([spec("user", OrderProbe("x", [], []), provider_name="fa")])
    result = await orch2.handle_user_message("@fa 看看")
    assert [w["member"] for w in result["woke"]] == ["fa"]
    assert orch2.channel.messages[0].from_member == "user"  # 用户消息本身仍是 user
    assert orch2.channel.messages[0].to_member == "fa"  # 定向目标是回退后的成员名


# ---------- 用户总管回合：@定向 / 广播 / 顺序唤醒 ----------


@pytest.mark.asyncio
async def test_directed_mentions_wake_in_roster_order_broadcast_wakes_none():
    log: list = []
    tool_counts: list = []
    events, orch = make_orch()
    await orch.create_team([
        spec("小研", OrderProbe("小研", log, tool_counts)),
        spec("写手", OrderProbe("写手", log, tool_counts)),
    ])
    # 点名顺序与名册相反：仍按名册顺序逐个唤醒（非并行）
    result = await orch.handle_user_message("@写手 @小研 都看一下")
    assert log == ["小研", "写手"]
    assert [w["member"] for w in result["woke"]] == ["小研", "写手"]
    assert all(w["status"] == "done" for w in result["woke"])
    # 多人点名：各发一条定向副本（from=user / msg_kind=ruling）
    first = orch.channel.messages[:2]
    assert [(m.from_member, m.to_member, m.msg_kind) for m in first] == [
        ("user", "小研", "ruling"), ("user", "写手", "ruling"),
    ]
    assert result["posted_seqs"] == [1, 2]
    assert result["finished"] is None and result["snapshot"] is None
    # 广播：不唤醒成员，只进频道等下次唤醒注入
    log.clear()
    tool_counts.clear()
    result2 = await orch.handle_user_message("大家保持节奏")
    assert result2["woke"] == [] and log == [] and tool_counts == []
    assert orch.channel.messages[-1].to_member == "all"
    assert orch.rounds_used == 2  # 只有成员唤醒轮计数，频道纯文本不计入


@pytest.mark.asyncio
async def test_images_in_team_turn_are_noticed_and_ignored():
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    result = await orch.handle_user_message(
        "看看这张图", images=[{"media_type": "image/png", "data": "aGk="}],
    )
    notices = [e for e in events if e.kind == "notice"]
    assert notices and "暂不支持图片" in notices[-1].message
    assert result["woke"] == []  # 广播不唤醒，图片也不会塞给队员


# ---------- 成员执行：顾问型 / 执行型切换 / 上下文注入 ----------


@pytest.mark.asyncio
async def test_member_advisor_vs_exec_registry_switch_and_wake_context(tmp_path):
    log: list = []
    tool_counts: list = []
    registry = ToolRegistry([ListDirTool(), WriteFileTool()])
    gate = PermissionGate(working_dir=tmp_path)
    events, orch = make_orch(working_dir=tmp_path, registry=registry, gate=gate)
    probe = OrderProbe("小研", log, tool_counts)
    await orch.create_team([spec("小研", probe, persona="研究员")])

    await orch.handle_user_message("@小研 先给点建议")
    agent = orch.agents()[0]
    assert agent is not None
    assert len(agent.registry) == 0  # 顾问型：空工具表
    assert tool_counts == [0]  # 模型侧也拿不到任何工具 schema
    # 唤醒上下文 = 人设（系统词）+ 频道未读 + 名下工单，不含会话历史
    sys_text, wake1 = probe.calls[0][0].text, probe.calls[0][-1].text
    assert "研究员" in sys_text and "SkySheep team" in sys_text
    assert "【团队频道 · 未读消息】" in wake1 and "@小研 先给点建议" in wake1

    # 派执行型工单 → 同一 Agent 实例切换到工作区工具表（工单级属性）
    await orch.task_add("写报告", "小研", type="exec")
    await orch.task_update("T1", "in_progress")
    await orch.handle_user_message("@小研 动手写")
    assert orch.agents()[0] is agent  # 跨阶段存活的独立 Agent，history 连续
    assert len(agent.registry) == 2 and tool_counts[-1] == 2
    wake2 = probe.calls[1][-1].text
    assert "T1「写报告」" in wake2 and "执行型" in wake2 and "进行中" in wake2


@pytest.mark.asyncio
async def test_member_context_isolation_between_members():
    log: list = []
    events, orch = make_orch()
    probe_a, probe_b = OrderProbe("小研", log, []), OrderProbe("写手", log, [])
    await orch.create_team([spec("小研", probe_a), spec("写手", probe_b)])
    await orch.handle_user_message("@小研 调研黑洞")
    # 写手未被唤醒 → 零次调用；小研的汇报 to=director，不进其他队员未读
    assert log == ["小研"]
    await orch.handle_user_message("@写手 该你了")
    wake = probe_b.calls[0][-1].text
    assert "小研的汇报" not in wake  # 他人发言不经未读注入
    assert "该你了" in wake  # 自己的定向消息在


@pytest.mark.asyncio
async def test_member_agent_carries_session_id_hooks_and_mods():
    """成员 Agent 透传会话 id / 用户钩子 / Mods：写租约归属与拦截手段对队员不降级。

    会话 id 是 claim_write 的租约 owner——空 owner 会被 security/leases 按
    「无归属」放行，跨会话并行写协调对队员静默失效（回归用例）。
    """
    log: list = []
    hooks, mods = SimpleNamespace(), SimpleNamespace()
    events, emit = collector()
    orch = TeamOrchestrator(emit=emit, session_id="sess-1", hooks=hooks, mods=mods)
    probe = OrderProbe("小研", log, [])
    await orch.create_team([spec("小研", probe)])
    await orch.handle_user_message("@小研 在吗")
    agent = orch.agents()[0]
    assert agent.session_id == "sess-1"
    assert agent.hooks is hooks and agent.mods is mods

    # 租约归属生效：他会话持有租约时，成员按会话身份认领会等待并带冲突注记
    hub = WriteLeaseHub()
    holder = await hub.claim([("out.txt", "out.txt")], owner="sess-2")
    lease = await hub.claim([("out.txt", "out.txt")], owner=agent.session_id, wait_s=0.05)
    assert "并行写入" in lease.note
    holder.release()
    lease.release()


# ---------- 成员执行：权限来源标注与 fail-closed（真实门 + 真实工具） ----------


@pytest.mark.asyncio
async def test_member_exec_readonly_auto_allowed_and_write_confirmed(tmp_path):
    registry = ToolRegistry([ListDirTool(), WriteFileTool()])
    gate = PermissionGate(working_dir=tmp_path)
    events, orch = make_orch(working_dir=tmp_path, registry=registry, gate=gate)
    member = FakeProvider([
        [ToolUseBlock(id="t1", name="list_dir", input={"path": "."})],
        [TextBlock(text="目录已看：空目录")],
        [ToolUseBlock(id="t2", name="write_file", input={"path": "out.txt", "content": "hi"})],
        [TextBlock(text="写入被拒，我改为给出建议")],
    ])
    await orch.create_team([spec("小研", member)])
    await orch.task_add("落盘", "小研", type="exec")
    await orch.task_update("T1", "in_progress")

    # 第一轮：只读工具零确认放行（无 permission_request），工具卡转发
    n1 = len(events)
    r1 = await orch.handle_user_message("@小研 看看目录")
    segment = events[n1:]
    assert not any(e.kind == "permission_request" for e in segment)
    assert any(e.kind == "tool_call_finished" and not e.is_error for e in segment)
    assert r1["woke"][0]["status"] == "done"

    # 第二轮：写文件 → 权限请求带「来自队员」标注；决策路由到成员 Agent
    task = asyncio.create_task(orch.handle_user_message("@小研 写文件"))
    deadline = asyncio.get_running_loop().time() + 5
    while not any(e.kind == "permission_request" for e in events):
        assert asyncio.get_running_loop().time() < deadline, "权限请求未在期限内到达"
        await asyncio.sleep(0.01)
    request = next(e for e in events if e.kind == "permission_request")
    assert request.note.startswith("来自队员『小研』")
    # 乱码决策 → normalize 白名单 fail-closed 按 deny：文件绝不落盘，轮继续到报告
    assert orch.respond_permission(request.request_id, "garbage-decision") is True
    result = await task
    assert result["woke"][0]["status"] == "done"
    assert not (tmp_path / "out.txt").exists()
    assert not orch.respond_permission("no-such-id", "allow_once")  # 未知 id 不误投
    assert "目录已看" in orch.channel.messages[-3].text  # 第一轮的报告照常定稿
    assert "建议" in orch.channel.messages[-1].text  # 被拒后轮继续到报告


@pytest.mark.asyncio
async def test_member_gate_delegates_and_only_annotates(tmp_path):
    inner = PermissionGate(working_dir=tmp_path)
    gated = TeamMemberGate(inner, "小研")
    # 透传：属性读写都落在内部门（档位只有一个判定源，双向验证后还原）
    assert gated.working_dir == inner.working_dir
    gated.auto_accept_write = True
    assert inner.auto_accept_write is True
    gated.auto_accept_write = False
    assert inner.auto_accept_write is False
    # authorize 原样委托：只读放行不变；需确认的请求只加来源标注
    assert await gated.authorize(ListDirTool(), {"path": "."}) is None
    pending = await gated.authorize(WriteFileTool(), {"path": "a.txt", "content": "x"})
    assert pending is not None
    assert pending.note.startswith("来自队员『小研』")
    pending.resolve("deny")


# ---------- 失败隔离 / 取消 / 轮次耗尽 / 收队 ----------


@pytest.mark.asyncio
async def test_member_timeout_isolated_not_fatal():
    cfg = SimpleNamespace(member_timeout_s=1, max_rounds=40)
    events, orch = make_orch(cfg=cfg)
    await orch.create_team([spec("慢员", HangingMember()), spec("小研", OrderProbe(
        "小研", [], []))])
    result = await orch.run_member_turn("慢员")
    assert result.status == "error" and "超时" in result.error
    assert orch.active  # 失败隔离：团队不收队
    # 空发言也落一条频道注记，时间线不缺格
    note = orch.channel.messages[-1]
    assert note.from_member == "慢员" and "超时" in note.text
    # 名册里健康的成员照常可唤醒
    result2 = await orch.run_member_turn("小研")
    assert result2.status == "done"
    # 缺 Key 成员（provider=None）唤醒同样只隔离不传染
    events2, orch2 = make_orch(cfg=cfg)
    dead = TeamMemberSpec(name="坏员", provider_name="p", model="m",
                          provider=None, build_error="还没有配置 API Key")
    await orch2.create_team([dead])
    r = await orch2.run_member_turn("坏员")
    assert r.status == "error" and "API Key" in r.error
    assert orch2.active


@pytest.mark.asyncio
async def test_cancel_mid_turn_keeps_partial_report_and_seq_compact():
    events, orch = make_orch()
    await orch.create_team([spec("小研", DeltaThenHang())])
    task = asyncio.create_task(orch.handle_user_message("@小研 讲讲"))
    await asyncio.sleep(0.1)  # 等第一段增量流出
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 部分发言已 shield 定稿进频道；seq 复用预占编号，无空洞
    assert [m.seq for m in orch.channel.messages] == [1, 2]
    report = orch.channel.messages[-1]
    assert report.from_member == "小研" and report.msg_kind == "report"
    assert "部分发言" in report.text and "已停止" in report.text
    assert orch.active  # 取消不打断团队：下条消息照常进频道


@pytest.mark.asyncio
async def test_max_rounds_exhausted_finishes_team():
    cfg = SimpleNamespace(max_rounds=2, member_timeout_s=30)
    events, orch = make_orch(cfg=cfg)
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    await orch.task_add("没做完的工单", "小研")
    r1 = await orch.handle_user_message("@小研 一")
    assert r1["finished"] is None and orch.rounds_used == 1
    await orch.run_member_turn("小研")  # 第 2 轮：预算见底
    r3 = await orch.handle_user_message("@小研 三")
    assert r3["finished"]["status"] == "rounds_exhausted"
    assert r3["snapshot"]["team"]["status"] == "rounds_exhausted"
    assert r3["woke"][0]["status"] == "error" and "轮次上限" in r3["woke"][0]["error"]
    finished = [e for e in events if e.kind == "team_finished"]
    assert len(finished) == 1 and finished[0].status == "rounds_exhausted"
    assert "未尽事项" in finished[0].summary
    # 终态后团队关闭：消息不再进频道；finish 幂等返回首次结果
    with pytest.raises(TeamError, match="没有进行中的团队"):
        await orch.handle_user_message("还有事")
    again = await orch.finish()
    assert again["summary"] == finished[0].summary
    assert [e.kind for e in events].count("team_finished") == 1


@pytest.mark.asyncio
async def test_stop_aborts_with_leftover_tasks_and_idempotent():
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    await orch.task_add("调研", "小研")
    await orch.task_add("写文档", "小研")
    await orch.task_update("T1", "in_progress")
    result = await orch.stop("改变计划")
    assert result["status"] == "aborted"
    assert "《收队说明》" in result["summary"]
    assert "改变计划" in result["summary"]
    assert "T1「调研」" in result["summary"] and "T2「写文档」" in result["summary"]
    assert result["snapshot"]["team"]["status"] == "aborted"
    finished = [e for e in events if e.kind == "team_finished"]
    assert len(finished) == 1 and finished[0].status == "aborted"
    assert not orch.active
    again = await orch.stop()
    assert again["summary"] == result["summary"]  # 重复收队幂等


# ---------- 快照（meta 落库口径） ----------


@pytest.mark.asyncio
async def test_snapshot_tail_and_text_cap():
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    for i in range(60):
        orch.channel.post("user", to_member="all", text=f"消息{i}")
    long = orch.channel.post("小研", to_member="director", msg_kind="report", text="长" * 600)
    await orch.task_add("唯一工单", "小研")
    snap = orch.snapshot()["team"]
    assert set(snap) == {
        "team_id",  # 三期：建队分配的唯一编号（回放 team.log 的主轴）
        "roster", "director_mode", "status", "rounds_used", "max_rounds", "tasks", "messages",
    }
    assert snap["status"] == "active" and snap["rounds_used"] == 0
    assert len(snap["messages"]) == SNAPSHOT_MESSAGE_TAIL
    assert snap["messages"][0]["seq"] == 12  # 61 条取尾 50 条
    capped = next(m for m in snap["messages"] if m["seq"] == long.seq)
    assert len(capped["text"]) == SNAPSHOT_MESSAGE_TEXT_CAP  # meta 单条截 400
    full = next(m for m in orch.channel.messages if m.seq == long.seq)
    assert len(full.text) == 600  # 频道原文不动
    assert capped["from"] == "小研" and capped["msg_kind"] == "report"
    assert [t["id"] for t in snap["tasks"]] == ["T1"]


# ---------- 工单板编排包装（事件 + 指派校验名册） ----------


@pytest.mark.asyncio
async def test_orchestrator_task_wrappers_fire_events():
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    task = await orch.task_add("写报告", "小研", accept="有结论")
    assert task["status"] == "pending"
    updated = await orch.task_update("T1", "in_progress")
    assert updated["status"] == "in_progress"
    assign = await orch.notify_assign("T1", note="先列提纲")
    assert assign["from"] == "director" and assign["msg_kind"] == "assign"
    assert assign["task_ref"] == "T1" and "先列提纲" in assign["text"]
    kinds = [e.kind for e in events if e.kind == "team_task_updated"]
    assert kinds == ["team_task_updated", "team_task_updated"]
    with pytest.raises(TeamError, match="不在团队名册中"):
        await orch.task_add("黑工单", "路人")
    with pytest.raises(TeamError, match="不存在"):
        await orch.notify_assign("T99")


# ---------- 重启恢复：活动登记一律标「已中断」 ----------


@pytest.mark.asyncio
async def test_team_state_restart_marks_interrupted(home, tmp_path):
    state = tmp_path / "nested" / "team_active.json"
    sid = "sess-1"
    backend1 = ServerBackend(
        working_dir=home / "proj", provider_factory=lambda: FakeProvider([]),
        team_state_path=state,
    )
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    backend1._teams[sid] = orch
    backend1._persist_team_state()
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["teams"] == [{
        "session_id": sid, "roster": ["小研"], "director_mode": "user",
    }]

    # 「重启」：新进程新 backend 实例——登记一律转已中断，并当场清账
    backend2 = ServerBackend(
        working_dir=home / "proj", provider_factory=lambda: FakeProvider([]),
        team_state_path=state,
    )
    got = backend2.team_get({"session_id": sid})
    assert got["active"] is False and got["team"] is None
    assert got["interrupted"]["status"] == "interrupted"
    assert got["interrupted"]["interrupt_reason"] == "应用重启，团队已中断"
    assert got["interrupted"]["roster"] == ["小研"]
    assert json.loads(state.read_text(encoding="utf-8"))["teams"] == []
    # 坏文件按没有活动团队处理，不炸启动
    state.write_text("not json", encoding="utf-8")
    backend3 = ServerBackend(
        working_dir=home / "proj", provider_factory=lambda: FakeProvider([]),
        team_state_path=state,
    )
    assert backend3.team_get({"session_id": sid})["active"] is False


def test_resolve_team_members_persona_and_build_error(home):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("还没有配置 API Key")
        return FakeProvider([[TextBlock(text="x")]])

    backend = ServerBackend(working_dir=home / "proj", provider_factory=factory)
    specs = backend._resolve_team_members([
        {"provider": "fa", "model": "ma", "name": "小研", "persona": "研究员"},
        {"provider": "fb", "model": "mb", "role": "critic"},  # 圆桌的 role 字段被忽略
    ])
    assert specs[0].name == "小研" and specs[0].persona == "研究员"
    assert specs[0].provider is not None
    assert (specs[0].provider_name, specs[0].model) == ("fa", "ma")
    assert specs[1].provider is None
    assert "API Key" in specs[1].build_error  # 构建失败降级为错误成员，不阻断建队


# ---------- 协议级：WS 方法与 chat.send team=true（TestClient 进程内） ----------


def make_team_client(home, scripts):
    """scripts 顺序 = provider_factory 调用顺序：第 1 个是主模型，之后按
    members 列表顺序逐个构建成员。项可以是脚本（list），也可以是实例。"""
    pool = [p if isinstance(p, Provider) else FakeProvider(p) for p in scripts]
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool else FakeProvider([[TextBlock(text="备用")]]),
    )
    return TestClient(app)


def test_team_full_flow_via_ws(home):
    scripts = [
        [[TextBlock(text="收队后的普通回答")]],  # 主模型（收队后恢复普通回合用）
        [[TextBlock(text="调研完成：结论 A")], [TextBlock(text="调研补充：结论 B")]],
        [[TextBlock(text="写手不该被唤醒")]],
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        # ---- 建队：team=true + 显式成员（含 persona）----
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "@小研 先做调研", "team": True,
            "members": [
                {"provider": "fa", "model": "ma", "name": "小研", "persona": "研究员"},
                {"provider": "fb", "model": "mb", "name": "写手"},
            ],
        }})
        events1: list = []
        r1 = recv_until(ws, "r1", events1)
        assert r1["ok"], r1
        kinds = [e["event"] for e in events1 if e["event"] != "task_estimate"]
        assert kinds[0] == "turn_started"
        started = next(e for e in events1 if e["event"] == "team_started")
        roster = started["data"]["roster"]
        assert [m["name"] for m in roster] == ["小研", "写手"]
        assert roster[0]["index"] == 0 and roster[0]["persona"] == "研究员"
        assert roster[0]["provider"] == "fa" and started["data"]["director_mode"] == "user"
        # 用户消息进频道（ruling），成员发言 delta 流式 + 定稿 report
        user_msg = next(e for e in events1 if e["event"] == "team_message"
                        and e["data"]["from_member"] == "user")
        assert user_msg["data"]["msg_kind"] == "ruling" and user_msg["data"]["to_member"] == "小研"
        report = next(e for e in events1 if e["event"] == "team_message"
                      and e["data"]["from_member"] == "小研")
        assert report["data"]["to_member"] == "director"
        assert report["data"]["msg_kind"] == "report"
        deltas = "".join(
            e["data"]["text"] for e in events1
            if e["event"] == "team_message_delta" and e["data"]["seq"] == report["data"]["seq"]
        )
        assert deltas == "调研完成：结论 A"
        assert not any("写手不该被唤醒" in e["data"].get("text", "") for e in events1)
        # assistant 纪要：气泡带成员摘录，meta 走 roundtable 槽（mode=team）
        assistant = next(e for e in events1 if e["event"] == "assistant_message")
        msg = assistant["data"]["message"]
        assert "【小研】" in msg["content"][0]["text"]
        meta1 = msg["roundtable"]
        assert meta1["mode"] == "team" and meta1["team"]["status"] == "active"
        assert r1["result"]["team"] == meta1
        assert meta1["team"]["messages"][0]["from"] == "user"

        # ---- 工单板：登记 → 派工（assign 频道消息 + 工单事件）----
        ws.send_json({"id": "k1", "method": "team.task_add", "params": {
            "title": "写调研报告", "assignee": "小研", "type": "exec", "accept": "有结论",
        }})
        k1 = recv_until(ws, "k1")
        assert k1["ok"] and k1["result"]["task"]["status"] == "pending"
        tid = k1["result"]["task"]["id"]
        ws.send_json({"id": "k2", "method": "team.task_update", "params": {
            "task_id": tid, "status": "in_progress",
        }})
        events2: list = []
        k2 = recv_until(ws, "k2", events2)
        assert k2["ok"] and k2["result"]["task"]["status"] == "in_progress"
        assign = k2["result"]["assign_message"]
        assert assign["from"] == "director" and assign["msg_kind"] == "assign"
        assert assign["task_ref"] == tid
        assert any(e["event"] == "team_task_updated"
                   and e["data"]["status"] == "in_progress" for e in events2)

        # ---- team.get：快照口径 ----
        ws.send_json({"id": "g1", "method": "team.get", "params": {}})
        g1 = recv_until(ws, "g1")
        assert g1["ok"] and g1["result"]["active"] is True
        team_view = g1["result"]["team"]
        assert team_view["tasks"][0]["status"] == "in_progress"
        assert any(m["msg_kind"] == "assign" for m in team_view["messages"])

        # ---- 自动路由：不带 team 标志也进团队频道 ----
        ws.send_json({"id": "r2", "method": "chat.send", "params": {"text": "@小研 继续"}})
        events_r2: list = []
        r2 = recv_until(ws, "r2", events_r2)
        assert r2["ok"], r2
        assert r2["result"]["team"]["team"]["rounds_used"] == 2
        assert any(e["event"] == "team_message" and "调研补充：结论 B" in e["data"]["text"]
                   for e in events_r2)

        # ---- 收队：立即终止 + 未尽事项；会话恢复普通回合 ----
        sid = r1["result"]["session_id"]
        ws.send_json({"id": "s1", "method": "team.stop", "params": {
            "session_id": sid, "reason": "目标达成",
        }})
        events3: list = []
        s1 = recv_until(ws, "s1", events3)
        assert s1["ok"], s1
        assert s1["result"]["status"] == "aborted"
        assert "未尽事项" in s1["result"]["summary"] and "T1" in s1["result"]["summary"]
        assert s1["result"]["team"]["status"] == "aborted"
        assert s1["result"]["session_id"] == sid
        assert any(e["event"] == "team_finished" and e["data"]["status"] == "aborted"
                   for e in events3)
        ws.send_json({"id": "g2", "method": "team.get", "params": {"session_id": sid}})
        g2 = recv_until(ws, "g2")
        assert g2["result"]["active"] is False and g2["result"]["team"] is None

        ws.send_json({"id": "r3", "method": "chat.send", "params": {
            "text": "收尾", "session_id": sid,
        }})
        events4: list = []
        r3 = recv_until(ws, "r3", events4)
        assert r3["ok"] and r3["result"]["team"] is None
        assert not any(e["event"].startswith("team_") for e in events4)

    # 落库：团队轮 user+assistant 成对，assistant 带 team meta；收队后普通轮不带
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    roles = [r[0] for r in rows]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    saved1 = json.loads(rows[1][1])
    assert saved1["roundtable"]["mode"] == "team"
    assert "【小研】" in saved1["content"][0]["text"]
    last = json.loads(rows[-1][1])
    assert last["roundtable"] is None
    assert last["content"][0]["text"] == "收队后的普通回答"


def test_team_deliver_via_ws(home):
    """team.deliver：用户确认交付（正常终态 done + 《交付说明》），收队之外
    唯一的 done 触发路径。未完成工单不拦——记未尽事项。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型
        [[TextBlock(text="小研的汇报")]],
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "@小研 开工", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        r1 = recv_until(ws, "r1")
        assert r1["ok"], r1
        sid = r1["result"]["session_id"]
        # 登记一张不派工的工单：交付时按未尽事项记录
        ws.send_json({"id": "k1", "method": "team.task_add", "params": {
            "title": "没做完的活", "assignee": "小研", "type": "exec",
        }})
        assert recv_until(ws, "k1")["ok"]

        ws.send_json({"id": "d1", "method": "team.deliver", "params": {
            "session_id": sid,
        }})
        events: list = []
        d1 = recv_until(ws, "d1", events)
        assert d1["ok"], d1
        assert d1["result"]["status"] == "done"
        assert "《交付说明》" in d1["result"]["summary"]
        assert "未尽事项" in d1["result"]["summary"] and "T1" in d1["result"]["summary"]
        assert d1["result"]["team"]["status"] == "done"
        assert d1["result"]["session_id"] == sid
        assert any(e["event"] == "team_finished" and e["data"]["status"] == "done"
                   for e in events)
        ws.send_json({"id": "g1", "method": "team.get", "params": {"session_id": sid}})
        g1 = recv_until(ws, "g1")
        assert g1["result"]["active"] is False and g1["result"]["team"] is None

        # 交付后恢复普通回合；重复交付/收队报「没有进行中的团队」
        ws.send_json({"id": "r2", "method": "chat.send", "params": {
            "text": "收尾", "session_id": sid,
        }})
        r2 = recv_until(ws, "r2")
        assert r2["ok"] and r2["result"]["team"] is None
        ws.send_json({"id": "d2", "method": "team.deliver", "params": {"session_id": sid}})
        d2 = recv_until(ws, "d2")
        assert not d2["ok"] and "没有进行中的团队" in d2["error"]


def test_team_roundtable_mutex_and_team_get_empty(home):
    scripts = [
        [[TextBlock(text="x")]],
        [[TextBlock(text="成员应答")]],
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        # 同轮互斥：参数错，原文回给调用方
        ws.send_json({"id": "m1", "method": "chat.send", "params": {
            "text": "问题", "team": True, "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
        }})
        m1 = recv_until(ws, "m1")
        assert not m1["ok"]
        assert "不能同时开启" in m1["error"]
        # 无活动团队：team.get 空态、工单方法报错
        ws.send_json({"id": "g0", "method": "team.get", "params": {}})
        g0 = recv_until(ws, "g0")
        assert g0["ok"] and g0["result"]["active"] is False and g0["result"]["team"] is None
        ws.send_json({"id": "e1", "method": "team.task_add", "params": {
            "title": "t", "assignee": "x",
        }})
        e1 = recv_until(ws, "e1")
        assert not e1["ok"] and "没有进行中的团队" in e1["error"]
        # 建队（广播不唤醒）后：圆桌被活动团队挡住
        ws.send_json({"id": "m2", "method": "chat.send", "params": {
            "text": "先广播一条", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        m2 = recv_until(ws, "m2")
        assert m2["ok"], m2
        sid = m2["result"]["session_id"]
        ws.send_json({"id": "m3", "method": "chat.send", "params": {
            "text": "换个开法", "roundtable": True, "session_id": sid,
        }})
        m3 = recv_until(ws, "m3")
        assert not m3["ok"]
        assert "请先收队" in m3["error"]
        # 工单指派校验：名册外成员原文报错
        ws.send_json({"id": "e2", "method": "team.task_add", "params": {
            "title": "写报告", "assignee": "路人",
        }})
        e2 = recv_until(ws, "e2")
        assert not e2["ok"] and "不在团队名册中" in e2["error"]


def test_team_usage_logged_per_member_only(home):
    """逐成员入账 usage_log（归属当前会话）；主 Agent 团队轮记 0 行。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型（团队轮不消耗）
        [[TextBlock(text="成员的汇报")]],  # 小研：一次迭代 11/7
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u1", "method": "chat.send", "params": {
            "text": "@小研 开始", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        frame = recv_until(ws, "u1")
    assert frame["ok"], frame
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute(
        "SELECT provider, model, in_tokens, out_tokens FROM usage_log"
    ).fetchall()
    assert rows == [("fa", "ma", 11, 7)]


def test_team_member_permission_round_trip_ws(home):
    """成员的权限请求经 WS 冒泡（note 带来源标注），permission.respond 路由进
    成员 Agent；deny 后工具不执行、成员轮继续到报告。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型
        [
            [ToolUseBlock(id="t1", name="write_file",
                          input={"path": "out.txt", "content": "hi"})],
            [TextBlock(text="写入被拒，我改为给出建议")],
        ],
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c0", "method": "chat.send", "params": {
            "text": "建队，先广播", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        c0 = recv_until(ws, "c0")
        assert c0["ok"], c0
        sid = c0["result"]["session_id"]
        # 派执行型工单并置进行中（成员下轮才挂工作区工具表）
        ws.send_json({"id": "k1", "method": "team.task_add", "params": {
            "title": "落盘", "assignee": "小研", "type": "exec",
        }})
        k1 = recv_until(ws, "k1")
        assert k1["ok"]
        tid = k1["result"]["task"]["id"]
        ws.send_json({"id": "k2", "method": "team.task_update", "params": {
            "task_id": tid, "status": "in_progress",
        }})
        assert recv_until(ws, "k2")["ok"]
        # 成员被唤醒发起写文件 → 权限请求 → 用户拒绝
        ws.send_json({"id": "c1", "method": "chat.send", "params": {
            "text": "@小研 写文件", "session_id": sid,
        }})
        request_id = note = resolved = None
        while True:
            frame = ws.receive_json()
            if "event" not in frame:
                if frame.get("id") == "c1":
                    final = frame
                    break
                continue
            if frame["event"] == "permission_request" and request_id is None:
                request_id = frame["data"]["request_id"]
                note = frame["data"]["note"]
                ws.send_json({"id": "p1", "method": "permission.respond", "params": {
                    "request_id": request_id, "decision": "deny",
                }})
            elif frame["event"] == "permission_resolved":
                resolved = frame["data"]
    assert request_id, "应收到带来源标注的权限请求"
    assert note.startswith("来自队员『小研』")
    assert resolved and resolved["decision"] == "deny"
    assert final["ok"], final
    assert not (home / "proj" / "out.txt").exists()  # deny 落到实处：文件没写
    # 成员被拒后轮继续到报告：频道定稿随 meta 里的消息尾部带回
    reports = [m for m in final["result"]["team"]["team"]["messages"] if m["from"] == "小研"]
    assert reports and "建议" in reports[-1]["text"]
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT content FROM messages ORDER BY id").fetchall()
    digest = json.loads(rows[-1][0])
    assert digest["roundtable"]["mode"] == "team"


# ---------- 二期：AI 总管闭环（director_mode="ai"，docs/团队模式设计.md §11） ----------
#
# 总管的内部工具调用用 FakeProvider 脚本化（与 agent/subagent 测试同款写法）：
# 脚本一项 = 总管 Agent 的一次模型调用；带工具的轮会在工具执行后再调一次模型，
# 所以每个带工具的轮要给「工具项 + 收尾文本项」两项。


def make_ai_orch(director_prov, cfg=None, working_dir=None, registry=None, gate=None,
                 budget_check=None, director_spec=None) -> tuple[list, TeamOrchestrator]:
    """AI 总管编排器：director_prov 是总管的 Provider 实例（FakeProvider 脚本或
    自定义探针）；director_spec 可整体替换（总管不可用用例传 provider=None）。"""
    events, emit = collector()
    if director_spec is None:
        director_spec = DirectorSpec(
            provider_name="dirp", model="fake-1", provider=director_prov,
        )
    orch = TeamOrchestrator(
        emit=emit, working_dir=working_dir, registry=registry, gate=gate, cfg=cfg,
        director_mode="ai", director=director_spec, budget_check=budget_check,
    )
    return events, orch


class GatedDirector(Provider):
    """首轮挂在事件上的总管：给「循环推进中的插话」留出确定性窗口。"""

    name, model = "gd", "g1"

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.calls: list = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            await self.gate.wait()
            yield ProviderTextDelta("收到目标，先建单")
            yield ProviderToolUse(id="a1", name="team_assign", input={
                "title": "写调研报告", "assignee": "小研", "type": "exec"})
            yield ProviderDone(stop_reason="tool_use", input_tokens=11, output_tokens=7)
        elif len(self.calls) == 2:
            yield ProviderTextDelta("建单完成")
            yield ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7)
        else:
            yield ProviderTextDelta("持续跟进")
            yield ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7)


class DirectorDeltaThenHang(Provider):
    """先吐一段发言再挂住的总管（取消收尾测试）。"""

    name, model = "ddh", "d1"

    async def stream(self, messages, tool_schemas, effort=None):
        yield ProviderTextDelta("部分交付说明")
        await asyncio.sleep(30)


@pytest.mark.asyncio
async def test_ai_director_closed_loop_assign_accept_deliver(tmp_path):
    """闭环主路径：一句话目标 → 总管建单上板 → 依赖就绪自动派发并唤醒队员 →
    报告自动置待验收 → 验收 → 交付（TeamFinished done + 《交付说明》）。

    总管工具全 READONLY + 独立门：整轮零权限事件是结构保证（总管轮走了真门与
    七个内部工具，不是靠脚本回避）；总管发言以 member_index=-1 流式；用量单独
    记在总管名下（不挂任何队员）。"""
    registry = ToolRegistry([ListDirTool(), WriteFileTool()])
    gate = PermissionGate(working_dir=tmp_path)
    director = FakeProvider([
        [TextBlock(text="分工方案：调研先行"), ToolUseBlock(
            id="d1", name="team_assign",
            input={"title": "写调研报告", "assignee": "小研", "type": "exec",
                   "accept": "有结论"})],
        [TextBlock(text="建单完成，等队员报告")],
        [TextBlock(text="对照验收标准：合格"), ToolUseBlock(
            id="d2", name="team_accept", input={"task_id": "T1", "note": "结论清晰"})],
        [TextBlock(text="验收通过，全部工单完成")],
        [TextBlock(text="《交付说明》：调研报告已完成。"), ToolUseBlock(
            id="d3", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])
    events, orch = make_ai_orch(director, working_dir=tmp_path, registry=registry, gate=gate)
    member = FakeProvider([[TextBlock(text="调研完成：结论 A")]])
    await orch.create_team([spec("小研", member)])

    result = await orch.run_auto_turn("目标：产出调研报告")
    assert result["finished"]["status"] == "done"
    assert "《交付说明》" in result["finished"]["summary"] and "T1" in result["finished"]["summary"]
    assert result["posted_seqs"] == [1]
    assert [w["member"] for w in result["woke"]] == ["小研"]
    assert result["woke"][0]["status"] == "done"
    assert result["rounds_used"] == 4  # 3 个总管轮 + 1 个成员唤醒轮
    # 工单全流转：建单 → 派发 → 报告自动置待验收 → 验收完成
    task = orch.board.get("T1")
    assert (task.status, task.redo, task.assignee) == ("done", 0, "小研")
    statuses = [e.status for e in events if e.kind == "team_task_updated"]
    assert statuses == ["pending", "in_progress", "review", "done"]
    # 频道关键消息：派工注记 / 队员报告 / 验收结论与交付发言（from=director, ruling）
    kinds = [(m.from_member, m.msg_kind) for m in orch.channel.messages]
    assert ("director", "assign") in kinds and ("小研", "report") in kinds
    rulings = [m for m in orch.channel.messages
               if m.from_member == "director" and m.msg_kind == "ruling"]
    assert any("验收通过" in m.text for m in rulings)
    assert any("《交付说明》" in m.text for m in rulings)
    # TeamStarted 带总管字段；总管发言增量 member_index=-1，定稿 from=director
    started = next(e for e in events if e.kind == "team_started")
    assert (started.director_mode, started.director_provider, started.director_model) == \
        ("ai", "dirp", "fake-1")
    director_deltas = [e for e in events if e.kind == "team_message_delta"
                       and e.member_index == -1]
    assert director_deltas and "".join(d.text for d in director_deltas)
    finals = [(e.from_member, e.msg_kind) for e in events if e.kind == "team_message"]
    assert ("director", "ruling") in finals and ("小研", "report") in finals
    # 结构性零权限事件 + 圆桌事件零泄漏
    assert not any(e.kind.startswith("permission_") for e in events)
    assert not any(e.kind.startswith("roundtable") for e in events)
    # 总管用量单独入账（6 次模型调用 × 11/7），队员的 11/7 不混进来
    assert orch.director_usage() == {"input_tokens": 66, "output_tokens": 42}
    snap = orch.snapshot()["team"]
    assert snap["director"] == {
        "mode": "ai", "provider": "dirp", "model": "fake-1",
        "input_tokens": 66, "output_tokens": 42, "stalled": [],
    }
    assert not orch.active  # 交付后团队关闭，会话恢复普通回合


@pytest.mark.asyncio
async def test_ai_director_reject_then_redo_passes():
    """打回 → 重做 → 通过：打回理由进频道，redo+1 后重新唤醒队员，二次报告
    验收通过并交付。stall_limit 调大，避免与停滞守卫用例互相纠缠。"""
    cfg = SimpleNamespace(member_timeout_s=300, max_rounds=40, redo_limit=2, stall_limit=5)
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_assign",
                      input={"title": "写调研报告", "assignee": "小研", "type": "exec"})],
        [TextBlock(text="已建单")],
        [TextBlock(text="初稿缺数据来源"), ToolUseBlock(
            id="d2", name="team_reject",
            input={"task_id": "T1", "reason": "缺少数据来源"})],
        [TextBlock(text="已打回，请补充后重交")],
        [TextBlock(text="数据来源齐了，合格"), ToolUseBlock(
            id="d3", name="team_accept", input={"task_id": "T1"})],
        [TextBlock(text="验收通过")],
        [TextBlock(text="《交付说明》：调研报告已完成（经历一次打回）。"), ToolUseBlock(
            id="d4", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])
    events, orch = make_ai_orch(director, cfg=cfg)
    member = FakeProvider([
        [TextBlock(text="初稿完成")],
        [TextBlock(text="补充数据来源后重做完成")],
    ])
    await orch.create_team([spec("小研", member)])

    result = await orch.run_auto_turn("目标：产出调研报告")
    assert result["finished"]["status"] == "done"
    task = orch.board.get("T1")
    assert (task.status, task.redo) == ("done", 1)
    reject = next(m for m in orch.channel.messages
                  if m.from_member == "director" and "缺少数据来源" in m.text)
    assert reject.msg_kind == "ruling" and reject.task_ref == "T1"
    assert len(member.calls) == 2  # 打回后确实重新唤醒了一次
    assert result["rounds_used"] == 6  # 4 个总管轮 + 2 个成员唤醒轮
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_ai_director_redo_over_limit_forces_adjudication():
    """redo 超上限 → 强制裁定：超限的 team_reject 被状态机拒绝（is_error 回给
    模型），总管按注入的三选一指令走 team_reassign 改派——新队员拿全新的打回
    预算（redo 清零）、自动唤醒重做到交付。"""
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_reject",
                      input={"task_id": "T1", "reason": "再给原队员一次机会"})],
        [ToolUseBlock(id="d2", name="team_reassign",
                      input={"task_id": "T1", "assignee": "写手",
                             "note": "换个思路重做"})],
        [TextBlock(text="已改派给写手")],
        [TextBlock(text="新报告合格"), ToolUseBlock(
            id="d3", name="team_accept", input={"task_id": "T1"})],
        [TextBlock(text="验收通过")],
        [TextBlock(text="《交付说明》：调研报告已完成。"), ToolUseBlock(
            id="d4", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])
    events, orch = make_ai_orch(director)
    writer = FakeProvider([[TextBlock(text="换思路重做完成")]])
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]])),
                            spec("写手", writer)])
    await orch.task_add("写调研报告", "小研", type="exec")
    # 手工把工单推到「待验收且打回预算耗尽」：两次打回后停回待验收
    for status in ("in_progress", "review", "in_progress", "review", "in_progress", "review"):
        await orch.task_update("T1", status)
    assert (orch.board.get("T1").status, orch.board.get("T1").redo) == ("review", 2)

    result = await orch.run_auto_turn("这单怎么办？")
    assert result["finished"]["status"] == "done"
    task = orch.board.get("T1")
    assert (task.assignee, task.redo, task.status) == ("写手", 0, "done")
    # 强制裁定指令出现在总管唤醒上下文（【必须裁定】段，明示不能再打回）
    wake = director.calls[0][-1].text
    assert "【必须裁定（防失控上限已触发）】" in wake and "不能再打回" in wake
    # 超限打回被拒（is_error 工具结果回给模型）；改派落板并给新队员派工注记
    errors = [e for e in events if e.kind == "tool_call_finished" and e.is_error]
    assert any(e.name == "team_reject" for e in errors)
    assign = next(m for m in orch.channel.messages
                  if m.msg_kind == "assign" and m.to_member == "写手")
    assert "换个思路重做" in assign.text
    assert len(writer.calls) == 1  # 新队员被自动唤醒重做


@pytest.mark.asyncio
async def test_failed_dependency_stuck_task_forces_adjudication():
    """依赖已失败的卡死待办（设计 §9，评审项分支）：pending 工单的依赖工单
    标 error 后依赖链无法满足——强制裁定指令注入总管轮（【必须裁定】段点名
    卡死工单），总管按指令重派失败的依赖工单落板（error → 进行中，板上唯一
    可落地的裁定路径），依赖链恢复后卡死工单自动派发、推进到交付。"""
    cfg = SimpleNamespace(member_timeout_s=1, max_rounds=40)
    director = FakeProvider([
        [TextBlock(text="先推进")],
        [ToolUseBlock(id="d1", name="team_reassign",
                      input={"task_id": "T1", "assignee": "写手", "note": "上游失败重派"})],
        [TextBlock(text="已重派上游工单")],
        [TextBlock(text="调研合格"), ToolUseBlock(id="d2", name="team_accept",
                                                  input={"task_id": "T1"})],
        [TextBlock(text="上游完成")],
        [TextBlock(text="汇总合格"), ToolUseBlock(id="d3", name="team_accept",
                                                  input={"task_id": "T2"})],
        [TextBlock(text="全部完成")],
        [TextBlock(text="《交付说明》：调研与汇总均已完成。"), ToolUseBlock(
            id="d4", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])
    events, orch = make_ai_orch(director, cfg=cfg)
    await orch.create_team([
        spec("慢员", HangingMember()),
        spec("写手", FakeProvider([
            [TextBlock(text="调研重做完成")],
            [TextBlock(text="汇总完成")],
        ])),
    ])
    await orch.task_add("写调研报告", "慢员", type="exec")
    await orch.task_update("T1", "in_progress")
    await orch.task_add("汇总报告", "写手", type="advisor", deps=["T1"])

    result = await orch.run_auto_turn("推进")
    assert result["finished"]["status"] == "done"
    # 强制裁定指令注入总管轮（依赖失败分支点名卡死的 T2）
    wake2 = director.calls[1][-1].text
    assert "【必须裁定（防失控上限已触发）】" in wake2
    assert "依赖工单已失败" in wake2 and "T2" in wake2
    # 裁定落板：失败的依赖工单经重派恢复，依赖链满足后卡死的 T2 自动派发，
    # 两单都推进到完成
    assert [(e.task_id, e.status) for e in events if e.kind == "team_task_updated"] == [
        ("T1", "pending"), ("T1", "in_progress"), ("T2", "pending"), ("T1", "error"),
        ("T1", "in_progress"), ("T1", "review"), ("T1", "done"),
        ("T2", "in_progress"), ("T2", "review"), ("T2", "done"),
    ]
    assert (orch.board.get("T1").status, orch.board.get("T2").status) == ("done", "done")
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_dependency_stuck_pending_task_droppable_via_team_drop():
    """砍掉出口补全（设计 §9 + §5 状态机补边）：依赖已失败的卡死待办工单经
    team_drop 落 error 并记入《交付说明》未尽事项——补边前 mark_error 只收
    进行中/待验收，强制裁定指令里的「team_drop 砍掉」对这类工单无单可落，
    总管唯一可落板的裁定只剩重派失败的依赖工单（上一条用例的路径）。"""
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_drop",
                      input={"task_id": "T2", "reason": "上游失败，砍掉汇总"})],
        [TextBlock(text="已砍掉汇总工单")],
        [TextBlock(text="《交付说明》：调研失败，汇总已砍掉。"), ToolUseBlock(
            id="d2", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])
    events, orch = make_ai_orch(director)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    await orch.task_add("写调研报告", "小研", type="exec")
    await orch.task_update("T1", "in_progress")
    await orch.task_update("T1", "error")  # 依赖上游标失败（用户 WS 路径）
    await orch.task_add("汇总报告", "小研", type="exec", deps=["T1"])  # 卡死待办

    r1 = await orch.run_auto_turn("处置卡死的汇总工单")
    assert r1["finished"] is None and orch.active
    # 强制裁定指令注入总管轮：点名卡死的 T2，「砍掉」出口与工具能力一致
    wake1 = director.calls[0][-1].text
    assert "依赖工单已失败" in wake1 and "T2" in wake1
    assert "team_drop 砍掉本工单" in wake1
    # 落板：待办 → 失败（补边），失败原因记入工单，频道有裁定注记
    assert orch.board.get("T2").status == "error"
    assert "失败原因：上游失败，砍掉汇总" in orch.board.get("T2").accept
    ruling = next(m for m in orch.channel.messages
                  if m.from_member == "director" and m.task_ref == "T2")
    assert "放弃 T2" in ruling.text
    assert ("T2", "error") in [(e.task_id, e.status) for e in events
                               if e.kind == "team_task_updated"]

    r2 = await orch.run_auto_turn("交付吧")
    assert r2["finished"]["status"] == "done"
    # 砍掉的 T2 进《交付说明》未尽事项（与失败的 T1 同列）
    assert "未尽事项" in r2["finished"]["summary"]
    assert "T2" in r2["finished"]["summary"] and "失败" in r2["finished"]["summary"]
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_stall_limit_stops_auto_wake_and_hands_over_to_director():
    """停滞守卫（设计 §9）：执行型成员连续 stall_limit 轮发言无新工具结果 →
    停止自动唤醒、注记进频道、停滞指令注入总管轮（覆盖待验收态、明示不要
    打回）；总管经 team_drop 落板，停滞标记随之复位。"""
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_reject",
                      input={"task_id": "T1", "reason": "缺少数据来源"})],
        [TextBlock(text="已打回")],
        [ToolUseBlock(id="d2", name="team_reject",
                      input={"task_id": "T1", "reason": "还是没有数据来源"})],
        [TextBlock(text="再打回一次")],
        [ToolUseBlock(id="d3", name="team_drop",
                      input={"task_id": "T1", "reason": "成员停滞，砍掉重立"})],
        [TextBlock(text="已放弃该工单")],
    ])
    events, orch = make_ai_orch(director)
    member = FakeProvider([
        [TextBlock(text="我补充一下")],
        [TextBlock(text="我还是没有数据")],
    ])
    await orch.create_team([spec("小研", member)])
    await orch.task_add("写调研报告", "小研", type="exec")
    await orch.task_update("T1", "in_progress")
    await orch.task_update("T1", "review")

    result = await orch.run_auto_turn("推进")
    assert result["finished"] is None and orch.active  # 处置完空闲退出，团队保持活动
    assert len(member.calls) == 2  # 停滞后不再自动唤醒
    note = next(m for m in orch.channel.messages if m.from_member == "system"
                and "已停止自动唤醒" in m.text)
    assert "小研" in note.text
    task = orch.board.get("T1")
    assert task.status == "error" and "失败原因" in task.accept
    # 第三个总管轮的唤醒上下文带停滞处置指令
    wake = director.calls[4][-1].text
    assert "没有任何新工具结果" in wake and "不要打回" in wake
    # drop 复位停滞标记：快照的停滞名单清空
    assert orch.snapshot()["team"]["director"]["stalled"] == []
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_member_timeout_marks_task_error_and_wakes_director():
    """自动循环里的成员轮超时（设计 §9 失败隔离，评审项）：名下进行中工单
    标 error（不再一律置待验收），频道有失败注记，总管下一轮被唤醒看到失败
    工单——既有 error → team_reassign 重派路径照用，换人重做到交付。"""
    cfg = SimpleNamespace(member_timeout_s=1, max_rounds=40)
    director = FakeProvider([
        [TextBlock(text="跟进一下")],
        [ToolUseBlock(id="d1", name="team_reassign",
                      input={"task_id": "T1", "assignee": "写手", "note": "超时换人"})],
        [TextBlock(text="已改派给写手")],
        [TextBlock(text="合格"), ToolUseBlock(id="d2", name="team_accept",
                                              input={"task_id": "T1"})],
        [TextBlock(text="验收通过")],
        [TextBlock(text="《交付说明》：调研报告已完成。"), ToolUseBlock(
            id="d3", name="team_deliver", input={})],
        [TextBlock(text="交付收口")],
    ])
    events, orch = make_ai_orch(director, cfg=cfg)
    await orch.create_team([
        spec("慢员", HangingMember()),
        spec("写手", FakeProvider([[TextBlock(text="换思路重做完成")]])),
    ])
    await orch.task_add("写调研报告", "慢员", type="exec")
    await orch.task_update("T1", "in_progress")

    result = await orch.run_auto_turn("推进")
    assert result["finished"]["status"] == "done"
    woke0 = result["woke"][0]
    assert woke0["member"] == "慢员" and woke0["status"] == "error" and "超时" in woke0["error"]
    # 失败落板：T1 标 error（失败原因注记），随后经改派走重派路径到完成
    statuses = [e.status for e in events if e.kind == "team_task_updated"]
    assert statuses == ["pending", "in_progress", "error", "in_progress", "review", "done"]
    assert "失败原因" in orch.board.get("T1").accept
    # 频道有失败注记（成员轮的超时注记，to=director）
    note = next(m for m in orch.channel.messages if m.from_member == "慢员" and "超时" in m.text)
    assert "按失败隔离" in note.text
    # 第 2 个总管轮的唤醒上下文带失败工单：板面「失败」状态 + 频道未读注记
    wake2 = director.calls[1][-1].text
    assert "T1" in wake2 and "失败" in wake2 and "超时" in wake2
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_max_rounds_counts_director_rounds_and_exhausts():
    """max_rounds 是全局口径：总管轮计入——1 个总管轮 + 1 个成员唤醒轮即触顶，
    下一个总管轮入口检查触发 rounds_exhausted 强制交付（不再消耗模型调用）。"""
    cfg = SimpleNamespace(member_timeout_s=300, max_rounds=2)
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_assign",
                      input={"title": "写调研报告", "assignee": "小研", "type": "exec"})],
        [TextBlock(text="已建单")],
    ])
    events, orch = make_ai_orch(director, cfg=cfg)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="做了一半")]]))])

    result = await orch.run_auto_turn("开工")
    assert result["rounds_used"] == 2 and result["max_rounds"] == 2
    assert result["woke"][0]["status"] == "done"
    assert result["finished"]["status"] == "rounds_exhausted"
    assert result["snapshot"]["team"]["status"] == "rounds_exhausted"
    finished = [e for e in events if e.kind == "team_finished"]
    assert len(finished) == 1 and finished[0].status == "rounds_exhausted"
    assert "未尽事项" in finished[0].summary
    assert len(director.calls) == 2  # 触顶的总管轮不再消耗模型调用


@pytest.mark.asyncio
async def test_budget_hook_forces_delivery_and_faulty_hook_passes():
    """预算钩子（构造注入的 callable，同步/异步皆可）：越线 → budget_exhausted
    强制交付（入口检查在前，总管模型一次都不调）；钩子异常按未越线放行——
    引擎不捏造越线，费用护栏由宿主其余路径兜底。"""

    async def over_budget() -> bool:
        return True

    def broken_hook() -> bool:
        raise RuntimeError("用量表读不到")

    director = FakeProvider([[TextBlock(text="不该被调用")]])
    events, orch = make_ai_orch(director, budget_check=over_budget)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    result = await orch.run_auto_turn("开工")
    assert result["finished"]["status"] == "budget_exhausted"
    assert "已达团队 token 预算" in result["finished"]["summary"]
    finished = [e for e in events if e.kind == "team_finished"]
    assert len(finished) == 1 and finished[0].status == "budget_exhausted"
    assert director.calls == []  # 入口检查在前：总管模型一次都没调

    director2 = FakeProvider([[TextBlock(text="现状说明，暂无可派工单")]])
    events2, orch2 = make_ai_orch(director2, budget_check=broken_hook)
    await orch2.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    result2 = await orch2.run_auto_turn("开工")
    assert result2["finished"] is None and orch2.active
    assert len(director2.calls) == 1  # 钩子故障不放行成摆设：总管轮照常推进


@pytest.mark.asyncio
async def test_huddle_reuses_roundtable_without_leaking_events():
    """小会（设计 §4.3）：run_roundtable 只读复用（辩论 1 轮），emit 全部换成
    内部收集——事件流零 Roundtable* 泄漏；队员意见以 TeamMessage(report) 进
    频道，裁定以 TeamMessage(ruling) 收口；各参与者用量先于其定稿发出（宿主
    team_emit 按「先用量后定稿」归属到名下）；主席用量并入 _director_usage，
    快照与 usage_log 两口径一致（评审项）。"""
    director = FakeProvider([
        [TextBlock(text="方案之争需要表决"), ToolUseBlock(
            id="d1", name="team_open_huddle",
            input={"topic": "方案一还是方案二", "members": ["小研", "写手"],
                   "context": "工期两天"})],
        [TextBlock(text="已排定小会")],
        [TextBlock(text="裁定：采用方案一，写手配合小研")],  # 主席融合（第 3 次调用）
    ])
    events, orch = make_ai_orch(director)
    xiao = FakeProvider([[TextBlock(text="我建议方案一")], [TextBlock(text="坚持方案一")]])
    xie = FakeProvider([[TextBlock(text="我倾向方案二")], [TextBlock(text="改支持方案一")]])
    await orch.create_team([spec("小研", xiao), spec("写手", xie)])

    result = await orch.run_auto_turn("两个方案定哪个？")
    assert result["finished"] is None and orch.active  # 小会后空闲退出
    assert result["rounds_used"] == 2  # 总管轮 + 一场小会（一场计 1）
    assert len(xiao.calls) == 2 and len(xie.calls) == 2  # 独立作答 + 辩论修订各一轮
    assert len(director.calls) == 3  # 总管轮 2 次 + 主席融合 1 次
    # 零泄漏：事件流没有任何圆桌事件（含成员增量与主席过程）
    assert not any(e.kind.startswith("roundtable") for e in events)
    announcement = next(m for m in orch.channel.messages if m.from_member == "system"
                        and "召开小会" in m.text)
    assert "方案一还是方案二" in announcement.text
    # 小会段的事件形状：每名参与者「先用量后定稿」，最后是主席裁定的定稿
    idx = next(i for i, e in enumerate(events)
               if e.kind == "team_message" and e.text == announcement.text)
    shape = [(e.kind, getattr(e, "from_member", "")) for e in events[idx + 1:]
             if e.kind in ("usage", "team_message")]
    assert shape == [
        ("usage", ""), ("team_message", "小研"),
        ("usage", ""), ("team_message", "写手"),
        ("usage", ""), ("team_message", "director"),
    ]
    reports = [e for e in events[idx + 1:]
               if e.kind == "team_message" and e.msg_kind == "report"]
    assert [e.from_member for e in reports] == ["小研", "写手"]
    assert all(e.to_member == "all" for e in reports)
    ruling = next(e for e in events[idx + 1:] if e.kind == "team_message"
                  and e.from_member == "director" and e.msg_kind == "ruling")
    assert "裁定：采用方案一" in ruling.text
    # 用量口径并账（评审项）：主席=总管 provider，其用量并入 _director_usage——
    # 快照的 director 合计（2 个总管轮 + 1 次主席融合 = 3 次调用 × 11/7）
    # 与 usage_log 的总管行同口径可对账；两名队员的 11/7 不混进 director
    assert orch.director_usage() == {"input_tokens": 33, "output_tokens": 21}
    snap = orch.snapshot()["team"]["director"]
    assert (snap["input_tokens"], snap["output_tokens"]) == (33, 21)


@pytest.mark.asyncio
async def test_user_interjection_is_injected_highest_priority_next_round():
    """插话权（设计 §2.1）：循环推进中 inject_user_message 只进频道与待注入清单
    （不重复驱动循环），下一总管轮以【用户插话（最高优先级）】段最先注入，且
    恰好消费一次（不丢、不重复）。"""
    gate = asyncio.Event()
    director = GatedDirector(gate)
    events, orch = make_ai_orch(director)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="做完了")]]))])

    task = asyncio.create_task(orch.run_auto_turn("开工"))
    deadline = asyncio.get_running_loop().time() + 5
    while not orch.loop_running:
        assert asyncio.get_running_loop().time() < deadline, "自动循环未在期限内启动"
        await asyncio.sleep(0.01)

    result = await orch.inject_user_message("插话：优先补充数据来源")
    assert result["loop_running"] is True
    assert result["posted_seqs"] and result["rounds_used"] == 1
    injected = orch.channel.messages[-1]
    assert injected.from_member == "user" and "优先补充数据来源" in injected.text

    gate.set()
    summary = await task
    assert summary["finished"] is None and orch.active  # 空闲退出，团队保持活动
    # 第二个总管轮的最高优先级段带插话原文，且先于板面段落
    wake2 = director.calls[2][-1].text
    assert "【用户插话（最高优先级，先于其他一切处理）】" in wake2
    assert "优先补充数据来源" in wake2
    assert wake2.index("【用户插话") < wake2.index("【工单板】")
    # 同文频道副本（from=user）已从总管未读剔除：同一条插话在总管上下文
    # 里恰好出现一次——不重复解读、不白耗 token（评审项）
    assert wake2.count("优先补充数据来源") == 1
    assert "优先补充数据来源" not in director.calls[3][-1].text  # 恰好消费一次
    user_msgs = [m for m in orch.channel.messages if m.from_member == "user"]
    assert len(user_msgs) == 2  # 两条用户消息都在频道里（不丢）
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_takeover_switches_to_user_mode_preserving_board_and_channel():
    """接管（设计 §2.1）：切回用户总管；工单板/频道/成员 Agent 全保留；未消费
    插话与待开小会作废、停滞标记清零；此后 handle_user_message 回到一期路径，
    AI 总管入口全部拒绝（模式互拒）。"""
    events, orch = make_ai_orch(FakeProvider([[TextBlock(text="待命")]]))
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    await orch.task_add("写调研报告", "小研", type="exec")
    await orch.task_update("T1", "in_progress")
    await orch.inject_user_message("别忘了附数据来源")  # 未消费插话
    orch._pending_huddles.append(HuddleRequest(topic="方案之争", members=["小研"]))
    orch._stalled.add("小研")  # 停滞标记（用户接管后可随意点名，一律清零）

    result = await orch.takeover()
    assert result["director_mode"] == "user"
    assert orch.board.get("T1").status == "in_progress"  # 工单板保留
    assert not orch._pending_user_notes and not orch._pending_huddles
    assert not orch._stalled
    note = orch.channel.messages[-1]
    assert note.from_member == "system" and "用户已接管" in note.text

    # 一期路径恢复：@点名唤醒照常（成员 Agent 原样复用），快照回到一期键集
    r = await orch.handle_user_message("@小研 报告进展")
    assert [w["member"] for w in r["woke"]] == ["小研"]
    assert r["woke"][0]["status"] == "done"
    assert "director" not in orch.snapshot()["team"]
    # 模式入口互拒：user 模式下 AI 总管入口全部拒绝
    with pytest.raises(TeamError, match="用户总管模式"):
        await orch.run_auto_turn("还有吗")
    with pytest.raises(TeamError, match="用户总管模式"):
        await orch.inject_user_message("还有吗")
    assert not any(e.kind.startswith("permission_") for e in events)


@pytest.mark.asyncio
async def test_director_mode_routing_guards_and_unavailable_director():
    """模式入口防误路由（active 校验先于 mode 校验）；总管不可用（provider=None）
    按失败隔离：频道注记 + TeamError，团队保持活动等插话重试 / 接管 / 收队。"""
    # user 模式（默认）：TeamStarted 总管字段为空串（一期事件形状不变）
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    started = events[-1]
    assert (started.director_mode, started.director_provider, started.director_model) == \
        ("user", "", "")
    with pytest.raises(TeamError, match="用户总管模式"):
        await orch.run_auto_turn("x")
    with pytest.raises(TeamError, match="用户总管模式"):
        await orch.inject_user_message("x")

    # AI 模式：handle_user_message 不允许绕过总管直接点名唤醒
    events2, orch2 = make_ai_orch(FakeProvider([[TextBlock(text="待命")]]))
    await orch2.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    with pytest.raises(TeamError, match="AI 总管主持"):
        await orch2.handle_user_message("@小研 干活")

    # 总管不可用：注记 + TeamError，团队不散；收队兜底照旧
    events3, orch3 = make_ai_orch(
        None,
        director_spec=DirectorSpec(provider_name="dp", model="dm", provider=None,
                                   build_error="还没有配置 API Key"),
    )
    await orch3.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    with pytest.raises(TeamError, match="还没有配置 API Key"):
        await orch3.run_auto_turn("开工")
    note = orch3.channel.messages[-1]
    assert note.from_member == "system" and "总管模型未能就位" in note.text
    assert orch3.active
    stopped = await orch3.stop("不折腾了")
    assert stopped["status"] == "aborted"

    # 终态团队：AI 总管入口报「没有进行中的团队」（active 校验先于 mode 校验）；
    # handle_user_message 的 mode 校验在前（user 模式终态的 active 报错一期已覆盖）
    with pytest.raises(TeamError, match="没有进行中的团队"):
        await orch3.run_auto_turn("x")
    with pytest.raises(TeamError, match="没有进行中的团队"):
        await orch3.inject_user_message("x")
    with pytest.raises(TeamError, match="AI 总管主持"):
        await orch3.handle_user_message("x")
    with pytest.raises(TeamError, match="团队已进入终态"):
        await orch3.takeover()


@pytest.mark.asyncio
async def test_member_prompt_names_director_by_mode():
    """成员系统提示词的总管句按 director_mode 取（评审项）：user 模式=用户
    本人；ai 模式=AI 总管（provider/model），队员知道向总管汇报——权限确认
    只认用户的红线两种形态都保留（设计 §6）。"""
    _, orch = make_orch()
    probe = OrderProbe("小研", [], [])
    await orch.create_team([spec("小研", probe)])
    await orch.handle_user_message("@小研 在吗")
    sys_user = probe.calls[0][0].text
    assert "Director: the user" in sys_user
    assert "AI director" not in sys_user

    probe_ai = OrderProbe("小研", [], [])
    _, orch2 = make_ai_orch(FakeProvider([
        [TextBlock(text="跟进")], [TextBlock(text="再跟进")], [TextBlock(text="又跟进")],
    ]))
    await orch2.create_team([spec("小研", probe_ai)])
    await orch2.task_add("写调研报告", "小研", type="exec")
    await orch2.task_update("T1", "in_progress")
    await orch2.run_auto_turn("推进")
    sys_ai = probe_ai.calls[0][0].text
    assert "the AI director (dirp/fake-1)" in sys_ai
    assert "Director: the user" not in sys_ai
    assert "ONLY by the user" in sys_ai  # 权限确认只认用户：AI 总管不能代批


@pytest.mark.asyncio
async def test_cancel_mid_director_turn_finalizes_partial_speech():
    """取消在飞总管轮：半截发言 shield 定稿进频道（from=director，沿用 ruling
    词汇）、seq 复用预占编号无空洞；团队保持活动——与成员轮同姿态。"""
    events, orch = make_ai_orch(DirectorDeltaThenHang())
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    task = asyncio.create_task(orch.run_auto_turn("开工"))
    deadline = asyncio.get_running_loop().time() + 5
    while not any(e.kind == "team_message_delta" and e.member_index == -1 for e in events):
        assert asyncio.get_running_loop().time() < deadline, "总管增量未在期限内到达"
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m.seq for m in orch.channel.messages] == [1, 2]  # 用户消息 + 总管发言，无空洞
    speech = orch.channel.messages[-1]
    assert speech.from_member == "director" and speech.msg_kind == "ruling"
    assert "部分交付说明" in speech.text and "已停止" in speech.text
    assert orch.active


@pytest.mark.asyncio
async def test_director_cannot_assign_to_itself_and_board_stays_member_only():
    """结构保证：总管不领工单——team_assign 指派 director 被名册校验拒绝
    （is_error 工具结果回给模型），板上不会出现总管名下工单；空板可直接交付。"""
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_assign",
                      input={"title": "我自己来", "assignee": "director"})],
        [TextBlock(text="被拒了，工单只能派给队员")],
        [TextBlock(text="《交付说明》：没有可交付的工单。"), ToolUseBlock(
            id="d2", name="team_deliver", input={})],
        [TextBlock(text="收口")],
    ])
    events, orch = make_ai_orch(director)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])

    r1 = await orch.run_auto_turn("目标：写报告")
    assert r1["finished"] is None and orch.board.tasks() == []  # 第一轮空闲退出
    errors = [e for e in events if e.kind == "tool_call_finished" and e.is_error]
    assert any(e.name == "team_assign" for e in errors)

    r2 = await orch.run_auto_turn("那就交付吧")
    assert r2["finished"]["status"] == "done"
    assert "没有登记过工单" in r2["finished"]["summary"]


async def test_stream_merger_buckets_director_deltas_by_negative_index():
    """总管增量 member_index=-1 在合并器按 (kind, member_index, seq) 独立分桶：
    与队员增量交错不互并、跨消息不串——负数键天然兼容，_MERGEABLE_DELTA_KINDS
    无需为总管新增条目。"""
    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(dict(ev))

    merger = StreamDeltaMerger(emit, window_s=10.0)
    await merger.send({"kind": "team_message_delta", "member_index": -1, "seq": 2, "text": "总"})
    await merger.send({"kind": "team_message_delta", "member_index": 0, "seq": 3, "text": "队"})
    await merger.send({"kind": "team_message_delta", "member_index": -1, "seq": 2, "text": "管"})
    await merger.send({"kind": "team_message", "seq": 2})  # 总管定稿（非增量：先冲刷）
    await merger.send({"kind": "team_message_delta", "member_index": -1, "seq": 4, "text": "二轮"})
    await merger.aclose()

    by_key: dict[tuple[int, int], str] = {}
    for e in events:
        if e.get("kind") == "team_message_delta":
            key = (e["member_index"], e["seq"])
            by_key[key] = by_key.get(key, "") + e["text"]
    assert by_key == {(-1, 2): "总管", (0, 3): "队", (-1, 4): "二轮"}


def test_team_ai_director_usage_and_snapshot_via_ws(home):
    """WS 全链路（TestClient 进程内）：AI 总管闭环建单 → 派发 → 验收 → 交付；
    TeamStarted 带总管字段、member_index=-1 增量与 from=director 定稿、meta
    快照带 director 块；usage_log 总管行与成员行分行入账（不混账）。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型（团队轮不消耗）
        [  # AI 总管：建单 → 验收 → 交付
            [TextBlock(text="分工方案"), ToolUseBlock(
                id="d1", name="team_assign",
                input={"title": "写调研报告", "assignee": "小研", "type": "exec"})],
            [TextBlock(text="已建单")],
            [TextBlock(text="合格"), ToolUseBlock(
                id="d2", name="team_accept", input={"task_id": "T1"})],
            [TextBlock(text="验收通过")],
            [TextBlock(text="《交付说明》：调研报告已完成。"), ToolUseBlock(
                id="d3", name="team_deliver", input={})],
            [TextBlock(text="交付收口")],
        ],
        [[TextBlock(text="调研完成：结论 A")]],  # 成员小研
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "目标：产出调研报告", "team": True,
            "director_mode": "ai", "director": {"provider": "fa", "model": "ma"},
            "members": [{"provider": "fm", "model": "mm", "name": "小研"}],
        }})
        events: list = []
        r1 = recv_until(ws, "r1", events)
        assert r1["ok"], r1
        started = next(e for e in events if e["event"] == "team_started")
        assert (started["data"]["director_mode"], started["data"]["director_provider"],
                started["data"]["director_model"]) == ("ai", "fa", "ma")
        deltas = [e["data"] for e in events if e["event"] == "team_message_delta"]
        assert any(d["member_index"] == -1 for d in deltas)  # 总管发言增量
        assert any(d["member_index"] == 0 for d in deltas)  # 队员发言增量
        finals = [e["data"] for e in events if e["event"] == "team_message"]
        assert any(m["from_member"] == "director" and m["msg_kind"] == "ruling"
                   for m in finals)
        assert not any(e["event"].startswith("permission_") for e in events)
        finished = next(e for e in events if e["event"] == "team_finished")
        assert finished["data"]["status"] == "done"
        assert "《交付说明》" in finished["data"]["summary"]
        meta = r1["result"]["team"]["team"]
        assert meta["status"] == "done" and meta["rounds_used"] == 4
        assert meta["director"] == {
            "mode": "ai", "provider": "fa", "model": "ma",
            "input_tokens": 66, "output_tokens": 42, "stalled": [],
        }
        ws.send_json({"id": "g1", "method": "team.get", "params": {}})
        g1 = recv_until(ws, "g1")
        assert g1["result"]["active"] is False and g1["result"]["team"] is None

    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT provider, model, in_tokens, out_tokens FROM usage_log").fetchall()
    # 成员行与总管行分行：总管用量（6 次调用 × 11/7）不挂任何队员名下
    assert rows == [("fm", "mm", 11, 7), ("fa", "ma", 66, 42)]


def test_team_huddle_fusion_failure_chair_usage_lands_on_director_row(home):
    """小会融合失败（清障项）：裁定定稿缺席、只有 from=system 的失败注记收尾
    ——主席用量经后端对 system 注记的主动结算归 usage_log 总管行，与
    _director_usage/snapshot.director 同口径；不随轮末兜底落到队尾成员名下。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型（团队轮不消耗）
        [  # AI 总管：发起小会 → 收口发言；第 3 次调用是主席融合（空产出 → 失败）
            [TextBlock(text="方案之争需要表决"), ToolUseBlock(
                id="d1", name="team_open_huddle",
                input={"topic": "方案一还是方案二", "members": ["小研", "写手"]})],
            [TextBlock(text="已排定小会")],
            [TextBlock(text="")],
        ],
        [[TextBlock(text="我建议方案一")], [TextBlock(text="坚持方案一")]],  # 小研
        [[TextBlock(text="我倾向方案二")], [TextBlock(text="改支持方案一")]],  # 写手
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "两个方案定哪个？", "team": True,
            "director_mode": "ai", "director": {"provider": "fa", "model": "ma"},
            "members": [
                {"provider": "fm", "model": "mm", "name": "小研"},
                {"provider": "fn", "model": "mn", "name": "写手"},
            ],
        }})
        events: list = []
        r1 = recv_until(ws, "r1", events)
        assert r1["ok"], r1
        # 事件流形状：队员先用量后定稿；主席用量之后没有裁定定稿，只有
        # from=system 的失败注记收尾（融合失败的频道语义不变）
        idx = next(i for i, e in enumerate(events)
                   if e["event"] == "team_message" and "召开小会" in e["data"]["text"])
        shape = [(e["event"], e["data"].get("from_member", "")) for e in events[idx + 1:]
                 if e["event"] in ("usage", "team_message")]
        assert shape == [
            ("usage", ""), ("team_message", "小研"),
            ("usage", ""), ("team_message", "写手"),
            ("usage", ""), ("team_message", "system"),
        ]
        # 失败注记之后再无任何定稿（裁定缺席，总管轮发言都在注记之前）
        note_idx = next(i for i, e in enumerate(events)
                        if e["event"] == "team_message" and "未能形成裁定" in e["data"]["text"])
        assert not any(e["event"] == "team_message"
                       and e["data"]["from_member"] == "director"
                       for e in events[note_idx + 1:])
        assert "以上意见供总管参考" in events[note_idx]["data"]["text"]
        meta = r1["result"]["team"]["team"]
        assert meta["rounds_used"] == 2  # 总管轮 + 一场小会
        # 快照口径：主席融合并入 director 合计（2 个总管轮调用 + 1 次融合
        # = 3 × 11/7），与 usage_log 的总管行可对账
        assert (meta["director"]["input_tokens"], meta["director"]["output_tokens"]) == (33, 21)

    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT provider, model, in_tokens, out_tokens FROM usage_log").fetchall()
    # 主席融合的 11/7 归总管行；队员行只有各自的 22/14（修前会落到队尾成员
    # 写手名下且缺总管行）
    assert rows == [("fm", "mm", 22, 14), ("fn", "mn", 22, 14), ("fa", "ma", 33, 21)]


def test_team_takeover_ws_round_trip(home):
    """team.takeover（TestClient 进程内）：在飞自动循环（挂在成员写文件的权限
    确认点上）被干净取消——半截成员发言 shield 定稿进频道——随后切回用户总管：
    工单板与频道保留、快照无 director 块、一期 handle_user_message 路径恢复；
    重复接管报错。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型
        [  # AI 总管：建执行型工单给队员
            [TextBlock(text="分工方案"), ToolUseBlock(
                id="d1", name="team_assign",
                input={"title": "落盘报告", "assignee": "小研", "type": "exec"})],
            [TextBlock(text="已建单")],
        ],
        [  # 小研：先说半句再发起写文件（权限确认点挂住自动循环）；接管后再应答一次
            [TextBlock(text="开始处理"), ToolUseBlock(
                id="t1", name="write_file", input={"path": "out.txt", "content": "hi"})],
            [TextBlock(text="好的，收到")],
        ],
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "目标：产出落盘报告", "team": True,
            "director_mode": "ai", "director": {"provider": "fa", "model": "ma"},
            "members": [{"provider": "fm", "model": "mm", "name": "小研"}],
        }})
        request_id = None
        while True:  # 读到权限请求为止：自动循环正挂在成员的写确认上
            frame = ws.receive_json()
            if "event" not in frame:
                continue
            if frame["event"] == "permission_request" and request_id is None:
                request_id = frame["data"]["request_id"]
                assert frame["data"]["note"].startswith("来自队员『小研』")  # 一期标注保留
                break
        assert request_id, "自动循环应停在成员写文件的权限确认上"

        ws.send_json({"id": "tk1", "method": "team.takeover", "params": {}})
        tk1 = recv_until(ws, "tk1")
        assert tk1["ok"], tk1
        assert tk1["result"]["director_mode"] == "user"
        team = tk1["result"]["team"]
        assert "director" not in team  # user 模式快照键集与一期完全一致
        assert team["tasks"][0]["status"] == "in_progress"  # 工单板保留
        stopped_report = next(m for m in team["messages"] if m["from"] == "小研")
        assert "开始处理" in stopped_report["text"] and "已停止" in stopped_report["text"]

        # 一期路径恢复：@点名直接唤醒（不进自动循环），成员 Agent 复用
        ws.send_json({"id": "r2", "method": "chat.send", "params": {"text": "@小研 报告进展"}})
        r2 = recv_until(ws, "r2")
        assert r2["ok"], r2
        meta = r2["result"]["team"]["team"]
        assert meta["director_mode"] == "user" and "director" not in meta
        assert meta["rounds_used"] == 3  # 总管轮 + 被取消的成员轮 + 接管后的唤醒
        reports = [m for m in meta["messages"] if m["from"] == "小研"]
        assert any("好的，收到" in m["text"] for m in reports)
        assert not (home / "proj" / "out.txt").exists()  # 被取消的写确认不会落盘

        ws.send_json({"id": "tk2", "method": "team.takeover", "params": {}})
        tk2 = recv_until(ws, "tk2")
        assert not tk2["ok"] and "用户总管模式" in tk2["error"]


# ---------- 二期评审收尾：发射链重绑例外与引擎前缀零泄漏 ----------


class RefProbe(Provider):
    """主模型探针：记录每轮收到的完整消息（含引擎前缀），供断言前缀只进普通轮。"""

    name, model = "probe", "p1"

    def __init__(self) -> None:
        self.calls: list = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls.append(list(messages))
        yield ProviderTextDelta("普通回答")
        yield ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7)


@pytest.mark.asyncio
async def test_team_body_rebinds_emit_only_when_loop_idle(home):
    """_team_body 的逐轮重绑（一期丢账修复）与插话例外并存（评审项）：空闲轮
    把编排器发射链重绑到本轮 team_emit（事件与用量归属本轮，不流进建队轮的
    旧闭包）；循环推进中的插话路径不重绑——在飞轮正持有发射链，重绑会把在飞
    轮的用量挪进早已冲刷完的账本。"""
    backend = ServerBackend(
        working_dir=home / "proj", provider_factory=lambda: FakeProvider([]),
    )
    sid = "sess-rebind"
    events1, emit1 = collector()  # 编排器现持的发射闭包（建队轮所给）
    orch = TeamOrchestrator(
        emit=emit1, director_mode="ai",
        director=DirectorSpec(provider_name="dp", model="dm",
                              provider=FakeProvider([[TextBlock(text="x")]])),
    )
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    backend._teams[sid] = orch
    stub_agent = SimpleNamespace(history=[])  # _team_body 只向 history 追加纪要

    # 插话路径（loop_running=True）：不重绑——频道消息仍走旧闭包
    orch._loop_running = True
    pipe1, pipe_emit1 = collector()  # 本轮 _team_body 的发射管道（收 dict）
    await backend._team_body("插话", pipe_emit1, None, agent=stub_agent, sid=sid)
    assert orch._emit is emit1
    assert any(e.kind == "team_message" and e.from_member == "user" for e in events1)

    # 空闲轮：重绑本轮发射链——总管轮事件落进本轮管道，旧闭包不再增长
    orch._loop_running = False
    pipe2, pipe_emit2 = collector()
    n_before = len(events1)
    await backend._team_body("推进", pipe_emit2, None, agent=stub_agent, sid=sid)
    assert orch._emit is not emit1
    assert any(e.get("kind") == "team_message" and e.get("from_member") == "director"
               for e in pipe2)
    assert len(events1) == n_before  # 旧闭包零增长：重绑生效


def test_team_auto_route_channel_text_stays_prefix_free(home):
    """引擎前缀不漏进团队频道（设计 §13.5，评审项）：团队轮（含自动路由轮
    ——消息不带 team 标志）的 refs 引用块与后台任务完成注记一律不拼进将进
    频道的用户文本；普通轮行为完全不变（前缀照旧），团队期间攒下的注记
    暂缓到收队后的下一普通轮注入。"""
    scripts: list = [
        RefProbe(),  # 主模型
        [[TextBlock(text="成员的汇报")]],  # 成员小研
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        probe = scripts[0]
        # 引用源：一个带真实消息的历史会话
        ws.send_json({"id": "a1", "method": "chat.send", "params": {"text": "历史会话的消息"}})
        ref_sid = recv_until(ws, "a1")["result"]["session_id"]
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        # 普通轮：refs 前缀照旧注入（被引用会话的内容也在上下文里）
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "看看引用", "refs": [ref_sid],
        }})
        assert recv_until(ws, "r1")["ok"]
        normal_text = probe.calls[-1][-1].text
        assert "[引用对话]" in normal_text and "历史会话的消息" in normal_text
        assert "看看引用" in normal_text

        # 建队 + 预置一条后台任务完成注记（构造注入，不真跑后台任务）
        ws.send_json({"id": "c1", "method": "chat.send", "params": {
            "text": "@小研 开工", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        assert recv_until(ws, "c1")["ok"]
        rec = TaskRecord("T-bg", "explore", "后台的", session_id=sid)
        rec.status = "done"
        backend.tasks._tasks[rec.id] = rec
        backend.tasks._turn_notes[sid] = [rec.id]

        # 自动路由团队轮（不带 team 标志）：refs 与后台注记都不进频道文本
        ws.send_json({"id": "r2", "method": "chat.send", "params": {
            "text": "@小研 继续", "refs": [ref_sid],
        }})
        events2: list = []
        r2 = recv_until(ws, "r2", events2)
        assert r2["ok"]
        user_msg = next(e for e in events2 if e["event"] == "team_message"
                        and e["data"]["from_member"] == "user")
        assert user_msg["data"]["text"] == "@小研 继续"  # 原文进频道，零引擎前缀
        assert "引用对话" not in user_msg["data"]["text"]
        assert "后台子代理任务" not in user_msg["data"]["text"]
        # 注记未被团队轮取走：暂缓（留在簿子里，等收队后的普通轮）
        assert "T-bg" in backend.tasks._turn_notes.get(sid, [])

        # 收队后恢复普通轮：注记与 refs 前缀照旧注入，注记随轮取走
        ws.send_json({"id": "s1", "method": "team.stop",
                      "params": {"session_id": sid, "reason": "收"}})
        assert recv_until(ws, "s1")["ok"]
        ws.send_json({"id": "r3", "method": "chat.send", "params": {
            "text": "收尾", "refs": [ref_sid],
        }})
        assert recv_until(ws, "r3")["ok"]
        after_text = probe.calls[-1][-1].text
        assert "后台子代理任务 T-bg" in after_text and "[引用对话]" in after_text
        assert "收尾" in after_text
        assert backend.tasks.pop_turn_note(sid) == ""


# ---------- 三期：team_id / 频道消息持久化钩子 ----------


@pytest.mark.asyncio
async def test_create_team_assigns_unique_team_id_into_snapshot():
    """三期：建队分配唯一 team_id（uuid4.hex），进快照随 meta 落库；重复建队
    换新 id——旧 id 名下的落库消息不与新团队混册。"""
    events, orch = make_orch()
    assert orch.team_id == ""  # 未建队无 id
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    first = orch.team_id
    assert len(first) == 32 and first != ""
    assert orch.snapshot()["team"]["team_id"] == first

    await orch.create_team([spec("写手", FakeProvider([[TextBlock(text="y")]]))])
    assert orch.team_id != first and len(orch.team_id) == 32


@pytest.mark.asyncio
async def test_message_sink_receives_finalized_messages_and_survives_errors():
    """持久化钩子（三期）：每条频道消息定稿回调 sink(team_id, msg)，同步/异步
    皆可；sink 中途异常只记日志不外溢——本轮照常完成、后续消息照常回调；
    未注入则零回调（既有行为不变）。"""
    sync_rows: list[tuple[str, object]] = []
    async_rows: list[tuple[str, object]] = []

    def sync_sink(team_id, msg):
        sync_rows.append((team_id, msg))
        if msg.seq == 2:  # 第二条（成员报告）：故意砸场子
            raise RuntimeError("库被锁了")

    async def async_sink(team_id, msg):
        async_rows.append((team_id, msg))

    events, emit = collector()
    orch = TeamOrchestrator(emit=emit, message_sink=sync_sink)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="报告")]]))])
    result = await orch.handle_user_message("@小研 看看进展")
    assert result["finished"] is None  # sink 异常没有打断团队轮
    assert len(sync_rows) == len(orch.channel.messages)  # 每条频道消息恰好回调一次
    assert {tid for tid, _ in sync_rows} == {orch.team_id}  # 全部带建队分配的 team_id
    kinds = [m.msg_kind for _, m in sync_rows]
    assert "ruling" in kinds and "report" in kinds  # 用户指令与成员报告都落了

    events2, emit2 = collector()
    orch2 = TeamOrchestrator(emit=emit2, message_sink=async_sink)
    await orch2.create_team([spec("写手", FakeProvider([[TextBlock(text="稿子")]]))])
    await orch2.handle_user_message("@写手 开写")
    assert len(async_rows) == len(orch2.channel.messages) >= 2

    events3, emit3 = collector()
    orch3 = TeamOrchestrator(emit=emit3)  # 未注入：不落库，行为与既有版本一致
    await orch3.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    await orch3.handle_user_message("@小研 在吗")
    assert len(orch3.channel.messages) == 2  # 频道照常


# ---------- 三期：团队模板（teams.json 读写助手） ----------


def test_team_template_store_roundtrip_and_upsert(tmp_path):
    """模板册 roundtrip：保存 → 重开加载一致；name 唯一键、同名覆盖（位置
    不变）；remove 真删且落盘。路径注入 tmp_path，不触真实 ~/.skysheep。"""
    path = tmp_path / "teams.json"
    st = TeamTemplateStore(path)
    st.load()
    assert st.list() == []  # 文件不存在：空册

    st.upsert(TeamTemplate(
        name="调研小队", director_mode="ai",
        director=TeamTemplateDirector(provider="p1", model="m1"),
        members=[
            TeamTemplateMember(provider="p2", model="m2", name="小研", persona="调研"),
            TeamTemplateMember(provider="p3", model="m3", name="写手"),
        ],
        created_at=123.0,
    ))
    st.upsert(TeamTemplate(
        name="速答组", members=[TeamTemplateMember(provider="p2", model="m2")],
    ))

    st2 = TeamTemplateStore(path)
    st2.load()
    assert [d["name"] for d in st2.list()] == ["调研小队", "速答组"]
    loaded = st2.list()[0]
    assert loaded["director_mode"] == "ai"
    assert loaded["director"] == {"provider": "p1", "model": "m1"}
    assert loaded["members"][0] == {
        "provider": "p2", "model": "m2", "name": "小研", "persona": "调研",
    }
    assert loaded["created_at"] == 123.0

    # 重名 = 覆盖：条目位置不变、内容换新
    st2.upsert(TeamTemplate(name="调研小队", members=[], created_at=456.0))
    assert [d["name"] for d in st2.list()] == ["调研小队", "速答组"]
    assert st2.list()[0]["created_at"] == 456.0 and st2.list()[0]["members"] == []

    st2.remove("调研小队")
    st3 = TeamTemplateStore(path)
    st3.load()
    assert [d["name"] for d in st3.list()] == ["速答组"]


def test_team_template_store_corrupt_file_and_bad_rows_tolerated(tmp_path):
    """坏文件容错（仿 subagents.json）：整份非 JSON 回空册；单条形状不对
    跳过不拖垮整册；下次保存整体覆盖修复。"""
    path = tmp_path / "teams.json"
    path.write_text("{ 不是 JSON", encoding="utf-8")
    st = TeamTemplateStore(path)
    st.load()
    assert st.list() == []

    path.write_text(
        '{"templates": [{"name": "好的", "members": []}, "垃圾", {"name": 42}]}',
        encoding="utf-8",
    )
    st.load()
    assert [d["name"] for d in st.list()] == ["好的"]  # 坏条目跳过（42 不能当名字）

    st.upsert(TeamTemplate(name="新的", members=[]))  # 保存覆盖修复
    st2 = TeamTemplateStore(path)
    st2.load()
    assert [d["name"] for d in st2.list()] == ["好的", "新的"]


def test_team_template_name_validation():
    """模板名校验（仿 skills 的 _validate_skill_name 精神）：空名/超长/路径
    分隔符/冒号/目录引用一律拒；首尾空白规范掉。"""
    assert validate_team_template_name(" 调研小队 ") == "调研小队"
    for bad in ["", "   ", "a/b", "a\\b", "C:模板", "a:b", "..", ".", " . "]:
        with pytest.raises(TeamError):
            validate_team_template_name(bad)
    with pytest.raises(TeamError):
        validate_team_template_name("名" * 61)  # 上限 60
    assert validate_team_template_name("名" * 60) == "名" * 60

    st = TeamTemplateStore(Path("unused") / "t.json")  # 路径不会被触达：校验先抛
    with pytest.raises(TeamError):
        st.remove("没有的")  # 删不存在的模板报错，不静默


def test_team_template_upsert_validates_name_and_mode(tmp_path):
    """upsert 走同一套校验：非法名/未知总管形态在保存前被拒（不写盘）。"""
    st = TeamTemplateStore(tmp_path / "teams.json")
    with pytest.raises(TeamError):
        st.upsert(TeamTemplate(name="a/b", members=[]))
    with pytest.raises(TeamError):
        st.upsert(TeamTemplate(name="合法名", director_mode="boss", members=[]))
    assert st.list() == [] and not (tmp_path / "teams.json").exists()


@pytest.mark.asyncio
async def test_forced_directive_stall_wording_matches_reassign_preconditions():
    """停滞强制裁定的指令措辞与 board.reassign 前置条件一致（三期修正）：
    team_reassign 只在「工单已待验收或失败」时点名；进行中的工单改派会被
    状态机拒绝（reassign 只收 review/error），指令须首选先砍后派。"""
    events, orch = make_orch()
    orch.roster = [spec("小研", None)]
    orch.board.members = ["小研"]
    task = orch.board.add("写调研报告", "小研", type="exec")
    task.status = "in_progress"
    orch._stalled.add("小研")
    orch._stall_counts["小研"] = 2

    directives = orch._forced_directives()
    assert len(directives) == 1
    text = directives[0]
    assert "没有任何新工具结果" in text and "已停止自动唤醒" in text
    # 条件表述：reassign 挂在「已待验收或失败」条件下，并明示待验收不要打回
    assert "其工单若已待验收或失败" in text
    assert "team_reassign 改派他人重做" in text and "不要打回" in text
    # 进行中：先砍后派（与 board.reassign 的前置条件一致）
    assert "若仍在进行中，先 team_drop 放弃" in text
    assert "team_assign 建新工单" in text


# ---------- 三期：WS 接线 —— 模板 / 频道回放（team.log）/ 设置卡（teamcfg） ----------


def test_team_template_ws_roundtrip_and_create_from_template(home):
    """team.template_* 三方法（TestClient 进程内）：保存落 teams.json（SKYSHEEP_HOME
    隔离下的 skysheep_home() 接线）、重名覆盖（原位一条、内容换新、created_at 刷新）、
    删除与删不存在报错、非法名错误帧不写盘；「从模板创建」= 模板成员照常拼进
    chat.send 既有解析路径，建出与模板一致的名册。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型
        [[TextBlock(text="按模板开工")]],  # 小研（从模板建队后被唤醒）
    ]
    teams_json = home / "home" / "teams.json"
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        # 保存：返回刚保存的模板（created_at 宿主盖戳、名字规范掉首尾空白）
        ws.send_json({"id": "s1", "method": "team.template_save", "params": {
            "name": " 调研小队 ", "director_mode": "user",
            "members": [
                {"provider": "fa", "model": "ma", "name": "小研", "persona": "调研"},
                {"provider": "fb", "model": "mb", "name": "写手"},
            ],
        }})
        s1 = recv_until(ws, "s1")
        assert s1["ok"], s1
        t1 = s1["result"]["template"]
        assert t1["name"] == "调研小队" and t1["director_mode"] == "user"
        assert t1["director"] == {"provider": "", "model": ""}
        assert [m["name"] for m in t1["members"]] == ["小研", "写手"]
        assert t1["created_at"] > 0
        assert teams_json.exists()  # 落在隔离的 SKYSHEEP_HOME，不触真实 ~/.skysheep

        # 重名覆盖：同名仍只一条，内容换新、created_at 刷新为本次保存时刻
        ws.send_json({"id": "s2", "method": "team.template_save", "params": {
            "name": "调研小队",
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        s2 = recv_until(ws, "s2")
        assert s2["ok"], s2
        t2 = s2["result"]["template"]
        assert t2["created_at"] >= t1["created_at"] and len(t2["members"]) == 1

        ws.send_json({"id": "l1", "method": "team.template_list", "params": {}})
        l1 = recv_until(ws, "l1")
        assert l1["ok"], l1
        assert [d["name"] for d in l1["result"]["templates"]] == ["调研小队"]
        assert l1["result"]["templates"][0]["members"] == t2["members"]  # 覆盖生效

        # 从模板创建：成员行照常进 chat.send 的既有解析路径（不校验服务存在性）
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "@小研 按模板开工", "team": True, "members": t2["members"],
        }})
        events: list = []
        r1 = recv_until(ws, "r1", events)
        assert r1["ok"], r1
        started = next(e for e in events if e["event"] == "team_started")
        assert [m["name"] for m in started["data"]["roster"]] == ["小研"]
        assert started["data"]["director_mode"] == "user"
        team_id = r1["result"]["team"]["team"]["team_id"]
        sid = r1["result"]["session_id"]

        # 非法名：中文 TeamError 原文回错误帧，且不写盘
        ws.send_json({"id": "e1", "method": "team.template_save", "params": {"name": "a/b"}})
        e1 = recv_until(ws, "e1")
        assert not e1["ok"] and "路径分隔符" in e1["error"]
        before = teams_json.read_text(encoding="utf-8")
        ws.send_json({"id": "e2", "method": "team.template_save", "params": {"name": "  "}})
        e2 = recv_until(ws, "e2")
        assert not e2["ok"] and "不能为空" in e2["error"]
        assert teams_json.read_text(encoding="utf-8") == before

        # 收队顺手校验 team.stop 结果块带 team_id（前端回放凭 meta 拿它）
        ws.send_json({"id": "st1", "method": "team.stop", "params": {
            "session_id": sid, "reason": "用完即收",
        }})
        st1 = recv_until(ws, "st1")
        assert st1["ok"] and st1["result"]["team"]["team_id"] == team_id

        # 删除：真删并落盘；删不存在报错不静默
        ws.send_json({"id": "d1", "method": "team.template_remove",
                      "params": {"name": "调研小队"}})
        d1 = recv_until(ws, "d1")
        assert d1["ok"] and d1["result"] == {"name": "调研小队"}
        ws.send_json({"id": "d2", "method": "team.template_remove",
                      "params": {"name": "没有的"}})
        d2 = recv_until(ws, "d2")
        assert not d2["ok"] and "找不到团队模板" in d2["error"]
        ws.send_json({"id": "l2", "method": "team.template_list", "params": {}})
        assert recv_until(ws, "l2")["result"]["templates"] == []

    assert json.loads(teams_json.read_text(encoding="utf-8")) == {"templates": []}


def test_team_log_ws_replays_persisted_channel_and_checks_ownership(home):
    """team.log（三期）：频道消息经 backend 注入的 sink 逐条落库（team_messages
    表，session_id 戳建队会话），按 meta 里的 team_id 全量回放（seq 升序、行
    字段见契约、text 全文）；会话归属 fail-closed——他人会话拒绝、缺 team_id
    报错、库内零记录的未知 id 回空（前端退回截断 meta 的容错退路）；收队后
    （编排器出册）仍凭落库行归属回放。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型（团队轮不消耗）
        [[TextBlock(text="调研完成：结论 A")]],  # 小研
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "@小研 开工", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        }})
        r1 = recv_until(ws, "r1")
        assert r1["ok"], r1
        sid = r1["result"]["session_id"]
        team_id = r1["result"]["team"]["team"]["team_id"]

        # 正常回放：全量频道消息（用户指令 + 成员报告），seq 升序、行结构契约
        ws.send_json({"id": "g1", "method": "team.log", "params": {"team_id": team_id}})
        g1 = recv_until(ws, "g1")
        assert g1["ok"], g1
        assert g1["result"]["team_id"] == team_id and g1["result"]["session_id"] == sid
        rows = g1["result"]["messages"]
        assert len(rows) >= 2 and [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
        assert rows[0]["from_member"] == "user" and rows[0]["msg_kind"] == "ruling"
        assert rows[0]["text"] == "@小研 开工"  # 全文原样（引擎前缀不进频道）
        assert any(r["from_member"] == "小研" and r["msg_kind"] == "report" for r in rows)
        assert all(set(r) == {"id", "team_id", "session_id", "seq", "from_member",
                              "to_member", "msg_kind", "task_ref", "text", "created_at"}
                   for r in rows)
        assert all(r["session_id"] == sid for r in rows)

        # 落库接线：行确实进了当前项目库的 team_messages 表（backend 的 sink）
        db = sqlite3.connect(home / "home" / "skysheep.db")
        db_rows = db.execute(
            "SELECT session_id, seq FROM team_messages WHERE team_id = ? ORDER BY seq",
            (team_id,),
        ).fetchall()
        db.close()
        assert len(db_rows) == len(rows) and {r[0] for r in db_rows} == {sid}
        assert [r[1] for r in db_rows] == [r["seq"] for r in rows]

        # 会话归属 fail-closed：他人会话拿这个 id 刺探 → 拒绝
        ws.send_json({"id": "g2", "method": "team.log", "params": {
            "team_id": team_id, "session_id": "sess-other",
        }})
        g2 = recv_until(ws, "g2")
        assert not g2["ok"] and "不属于当前会话" in g2["error"]
        # 缺 team_id：错误帧
        ws.send_json({"id": "g3", "method": "team.log", "params": {}})
        g3 = recv_until(ws, "g3")
        assert not g3["ok"] and "缺少 team_id" in g3["error"]
        # 未知 id（库内零记录）：不报错回空——前端两级容错就退回截断 meta
        ws.send_json({"id": "g4", "method": "team.log", "params": {"team_id": "无记录id"}})
        g4 = recv_until(ws, "g4")
        assert g4["ok"] and g4["result"]["messages"] == []

        # 收队后（编排器已出册）：仍凭库内行的 session_id 归属放行，回放不缺行
        ws.send_json({"id": "s1", "method": "team.stop", "params": {
            "session_id": sid, "reason": "收",
        }})
        assert recv_until(ws, "s1")["ok"]
        ws.send_json({"id": "g5", "method": "team.log", "params": {"team_id": team_id}})
        g5 = recv_until(ws, "g5")
        assert g5["ok"], g5
        assert [r["seq"] for r in g5["result"]["messages"]] == [r["seq"] for r in rows]


def test_teamcfg_ws_roundtrip_clamp_and_hot_effect(home):
    """teamcfg.get/save（设置页 [team] 配置卡，三期）：get 回五键 + 中文说明；
    save 走 config.toml（update_config_section，无手拼）并回传新状态——越界值
    int 化 + clamp 收口（仿 roundtable_save）；保存后**新建的团队**读到新值
    （max_members 截断名册 + 截断 Notice 是可见行为），配置卡说明「进行中的
    团队沿用建队时的值」不在本用例重复断言（编排器建队取值已覆盖）。"""
    scripts = [
        [[TextBlock(text="x")]],  # 主模型
        [[TextBlock(text="一")]], [[TextBlock(text="二")]], [[TextBlock(text="三")]],
    ]
    with make_team_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "q1", "method": "teamcfg.get", "params": {}})
        q1 = recv_until(ws, "q1")
        assert q1["ok"], q1
        got = q1["result"]
        assert set(got) == {"max_members", "member_timeout_s", "max_rounds",
                            "redo_limit", "stall_limit", "config_hint"}
        assert (got["max_members"], got["member_timeout_s"], got["max_rounds"],
                got["redo_limit"], got["stall_limit"]) == (3, 300, 40, 2, 2)
        assert isinstance(got["config_hint"], str) and got["config_hint"]

        # 越界收口：全部 clamp 进合法区间；字符串值 int 化
        ws.send_json({"id": "v1", "method": "teamcfg.save", "params": {
            "max_members": 99, "member_timeout_s": 1, "max_rounds": 0,
            "redo_limit": 50, "stall_limit": "3",
        }})
        v1 = recv_until(ws, "v1")
        assert v1["ok"], v1
        assert (v1["result"]["max_members"], v1["result"]["member_timeout_s"],
                v1["result"]["max_rounds"], v1["result"]["redo_limit"],
                v1["result"]["stall_limit"]) == (8, 10, 1, 5, 3)
        # 真落 config.toml（SKYSHEEP_HOME 隔离）：再 get 与回传一致
        ws.send_json({"id": "q2", "method": "teamcfg.get", "params": {}})
        q2 = recv_until(ws, "q2")
        assert q2["result"]["max_members"] == 8 and q2["result"]["stall_limit"] == 3
        cfg_text = (home / "home" / "config.toml").read_text(encoding="utf-8")
        assert "[team]" in cfg_text and "max_members = 8" in cfg_text

        # 下限同样收口；只传子集时其余键不动
        ws.send_json({"id": "v2", "method": "teamcfg.save", "params": {"max_members": 0}})
        v2 = recv_until(ws, "v2")
        assert v2["ok"] and v2["result"]["max_members"] == 1
        assert v2["result"]["stall_limit"] == 3  # 未传的键保持上次保存值

        # 热生效：保存后新建的团队读到新值——max_members=1 把三名队员截成一名
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "先广播一条", "team": True,
            "members": [
                {"provider": "fa", "model": "ma", "name": "小研"},
                {"provider": "fb", "model": "mb", "name": "写手"},
                {"provider": "fc", "model": "mc", "name": "画手"},
            ],
        }})
        events: list = []
        r1 = recv_until(ws, "r1", events)
        assert r1["ok"], r1
        started = next(e for e in events if e["event"] == "team_started")
        assert [m["name"] for m in started["data"]["roster"]] == ["小研"]
        notice = next(e for e in events if e["event"] == "notice")
        assert "上限为 1 名" in notice["data"]["message"]  # 截断发 Notice，不静默丢人


# ---------- 对抗审查修复回归（F1–F8） ----------


class UsageThenHold(Provider):
    """先报用量再挂住（等放行才收流）：构造「成员用量已进 pending、定稿未
    发出」的冲账竞态窗口（F8 回归用）。"""

    name, model = "hold", "h1"

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    async def stream(self, messages, tool_schemas, effort=None):
        yield ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7)
        await self.gate.wait()


def test_team_templates_loaded_at_setup_and_save_never_clobbers(home):
    """F1 回归：模板册在 setup() 装载——「重启」（新 backend/lifespan）后
    template_list 非空、template_remove 不误报找不到；此时再保存新模板是在
    已装载的册上追加，teams.json 里已存模板不被清光（不装载的话 upsert→save
    以空册整体覆盖落盘，用户模板被静默清掉）。"""
    teams_json = home / "home" / "teams.json"
    teams_json.parent.mkdir(parents=True, exist_ok=True)
    teams_json.write_text(json.dumps({"templates": [{
        "name": "重启前的模板", "director_mode": "user",
        "director": {"provider": "", "model": ""},
        "members": [{"provider": "fa", "model": "ma", "name": "小研", "persona": ""}],
        "created_at": 111.0,
    }]}, ensure_ascii=False), encoding="utf-8")

    with make_team_client(home, [[[TextBlock(text="x")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l0", "method": "team.template_list", "params": {}})
        l0 = recv_until(ws, "l0")
        assert l0["ok"], l0
        assert [d["name"] for d in l0["result"]["templates"]] == ["重启前的模板"]

        # 已装载的册上保存新模板：旧模板不被清光
        ws.send_json({"id": "s1", "method": "team.template_save", "params": {
            "name": "重启后新增", "members": []}})
        assert recv_until(ws, "s1")["ok"]
        ws.send_json({"id": "l1", "method": "team.template_list", "params": {}})
        l1 = recv_until(ws, "l1")
        assert [d["name"] for d in l1["result"]["templates"]] == ["重启前的模板", "重启后新增"]
        on_disk = {t["name"] for t in
                   json.loads(teams_json.read_text(encoding="utf-8"))["templates"]}
        assert on_disk == {"重启前的模板", "重启后新增"}

        # 删除磁盘上已有的模板不再误报「找不到」
        ws.send_json({"id": "d1", "method": "team.template_remove",
                      "params": {"name": "重启前的模板"}})
        d1 = recv_until(ws, "d1")
        assert d1["ok"], d1


@pytest.mark.asyncio
async def test_team_ws_methods_converge_foreign_session_for_remote_callers(home):
    """F2 回归：非本机调用把 session_id 收敛到其绑定的活动会话（tasks.* 同款
    口径）——拿枚举来的他人 sid 读团队快照、改工单板、收队、回放频道文本，
    一律落不到他人会话的团队上；本机调用显式传 sid 的既有用法照旧。"""
    from skysheep.server.app import _h_team_task_add

    store = await SessionStore(home / "f2.db").connect()
    backend = ServerBackend(working_dir=home / "proj",
                            provider_factory=lambda: FakeProvider([]))
    backend.store = store  # 直接挂库（构造参数 store 只在 setup() 消费，这里不跑 setup）
    events, emit = collector()
    orch = TeamOrchestrator(emit=emit)
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    foreign_sid = "sess-other"
    backend._teams[foreign_sid] = orch
    backend.session = SimpleNamespace(id="sess-mine")
    noop_emit = collector()[1]

    # 非本机：他人 sid 不可见（快照收敛到自己的会话）
    got = await _h_team_get(backend, {"session_id": foreign_sid}, noop_emit, local=False)
    assert got["active"] is False and got["session_id"] == "sess-mine"
    # 非本机：拿他人 sid 加单/收队被收敛拒绝，板原样
    with pytest.raises(TeamError, match="没有进行中的团队"):
        await _h_team_task_add(backend, {"session_id": foreign_sid,
                                         "title": "黑工单", "assignee": "小研"},
                               noop_emit, local=False)
    with pytest.raises(TeamError, match="没有进行中的团队"):
        await _h_team_stop(backend, {"session_id": foreign_sid, "reason": "越权"},
                           noop_emit, local=False)
    assert backend._teams.get(foreign_sid) is orch and orch.active

    # 非本机：他人团队的频道文本不可回放（收敛后的 sid 与落库行归属对不上）
    await store.add_team_message("tid-foreign", seq=1, from_member="user",
                                 to_member="all", msg_kind="ruling", task_ref="",
                                 text="他人频道的消息", session_id=foreign_sid)
    with pytest.raises(TeamError, match="不属于当前会话"):
        await _h_team_log(backend, {"team_id": "tid-foreign",
                                    "session_id": foreign_sid}, noop_emit, local=False)

    # 本机调用不受影响：显式 sid 照旧可达（前端本机传 sid 的既有用法）
    got2 = await _h_team_get(backend, {"session_id": foreign_sid}, noop_emit, local=True)
    assert got2["active"] is True and got2["session_id"] == foreign_sid
    await store.close()


@pytest.mark.asyncio
async def test_rounds_exhausted_mid_wake_loop_yields_summary_not_collapse():
    """F3 回归：轮次耗尽的 finish 落在唤醒循环中途（前一名成员轮触发）时，
    后续被点名成员不再以 not-active 的 TeamError 塌掉整个团队回合——记
    skipped 后照常走出终态摘要（《交付说明》随返回值交给宿主落 meta）。"""
    cfg = SimpleNamespace(member_timeout_s=30, max_rounds=1)
    events, orch = make_orch(cfg=cfg)
    await orch.create_team([
        spec("小研", OrderProbe("小研", [], [])),
        spec("写手", OrderProbe("写手", [], [])),
        spec("画手", OrderProbe("画手", [], [])),
    ])
    result = await orch.handle_user_message("@小研 @写手 @画手 都看看")
    # 小研正常跑完（耗掉最后一轮预算）；写手轮入口触发收队（error）；画手
    # 在修复前抛 TeamError 塌掉整个回合，修复后记 skipped
    assert result["finished"]["status"] == "rounds_exhausted"
    assert [w["status"] for w in result["woke"]] == ["done", "error", "skipped"]
    assert "未再唤醒" in result["woke"][2]["error"]
    assert result["snapshot"]["team"]["status"] == "rounds_exhausted"
    finished = [e for e in events if e.kind == "team_finished"]
    assert len(finished) == 1 and "未尽事项" in finished[0].summary


@pytest.mark.asyncio
async def test_team_body_teamerror_settles_finished_team(home):
    """F3 后端兜底：团队轮中抛 TeamError 且团队已进终态时，活动登记照常摘除
    ——不留「已收队却挂在 _teams 里、此后每条消息都报没有进行中的团队」的
    困局（用量结算同分支兜底，本场景无用量可结）。"""
    backend = ServerBackend(working_dir=home / "proj",
                            provider_factory=lambda: FakeProvider([]))
    sid = "sess-f3"
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    backend._teams[sid] = orch
    await orch.stop("先收队")  # 已终态（模拟轮中收队与消息轮撞上的竞态窗口）
    stub_agent = SimpleNamespace(history=[])
    pipe, pipe_emit = collector()  # _team_body 的发射管道（收 dict）
    with pytest.raises(TeamError, match="没有进行中的团队"):
        await backend._team_body("还在吗", pipe_emit, None, agent=stub_agent, sid=sid)
    assert sid not in backend._teams


@pytest.mark.asyncio
async def test_queued_roundtable_turn_revalidates_team_mutex(home):
    """F4 回归：排队轮交棒进 _run_turn_pipeline 时重验团队/圆桌互斥——消息
    在「建队轮已开跑、_teams 尚未登记」的窗口入队（roundtable=true 被接受）、
    交棒时团队已在板，圆桌标志不再被 team 分支静默吞掉，显式报错交还调用方
    （与不入队即被拒的口径对齐）。"""
    from skysheep.config import load_config

    backend = ServerBackend(working_dir=home / "proj",
                            provider_factory=lambda: FakeProvider([]))
    backend.cfg = load_config()
    backend.provider = FakeProvider([[TextBlock(text="x")]])
    store = await SessionStore(home / "f4.db").connect()
    backend.store = store
    backend.compose_system = lambda *a, **k: ""  # 桩掉系统词组装（skills 未装载）
    sid = "sess-f4"
    events, orch = make_orch()
    await orch.create_team([spec("小研", OrderProbe("小研", [], []))])
    backend._teams[sid] = orch  # 建队轮完成登记后的板面状态
    # 手工最小 runtime（不走 _get_runtime——那需要完整 setup 态）；桩 agent
    # 覆盖流水线收尾路径读到的面（本轮在互斥检查即报错，不会真跑模型）
    stub_agent = SimpleNamespace(
        history=[], registry=None, context_limit_tokens=None,
        total_in_tokens=0, total_out_tokens=0, total_cached_tokens=0,
        used_context_tokens=lambda: 0, set_system=lambda *_a, **_k: None,
    )
    runtime = SessionRuntime(sid=sid, agent=stub_agent, recorder=ChangeRecorder())
    fut = asyncio.get_running_loop().create_future()
    item = QueuedTurn(text="圆桌问题", emit=collector()[1], plan_mode=False,
                      fut=fut, roundtable=True)
    await backend._run_queued(item, runtime)
    with pytest.raises(RuntimeError, match="请先收队"):
        fut.result()
    # 团队原样：消息没有被吞进频道（频道里没有这条圆桌文本）
    assert backend._teams.get(sid) is orch and orch.active
    assert not any("圆桌问题" in m.text for m in orch.channel.messages)
    await store.close()


@pytest.mark.asyncio
async def test_team_reject_only_applies_to_review_tasks():
    """F5 回归：team_reject 只对「待验收」成立——把待办直接派发成进行中
    （绕过 notify_assign 正规派发注记）与把失败工单原地复活给原队员都被
    拒绝；待验收打回照常（redo+1、理由进频道）。"""
    director = FakeProvider([[TextBlock(text="x")]])
    events, orch = make_ai_orch(director)
    await orch.create_team([spec("小研", FakeProvider([[TextBlock(text="x")]]))])
    tool = orch._director_registry.get("team_reject")
    ctx = ToolContext(working_dir=None)

    # 越界边一：待办工单直接「打回」= 绕过正规派发
    await orch.task_add("还没开工的单", "小研", type="exec")
    with pytest.raises(Exception, match="不在待验收"):
        await tool.run(TeamRejectArgs(task_id="T1", reason="提前打回"), ctx)
    assert orch.board.get("T1").status == "pending"

    # 越界边二：失败工单原地复活给原队员（重派必须改派他人）
    await orch.task_update("T1", "in_progress")
    await orch.task_update("T1", "error")
    with pytest.raises(Exception, match="不在待验收"):
        await tool.run(TeamRejectArgs(task_id="T1", reason="复活它"), ctx)
    assert orch.board.get("T1").status == "error"

    # 正路：待验收打回照常（redo+1，理由进频道）
    await orch.task_update("T1", "in_progress")
    await orch.task_update("T1", "review")
    out = await tool.run(TeamRejectArgs(task_id="T1", reason="缺数据来源"), ctx)
    assert "已退回进行中" in out and orch.board.get("T1").redo == 1
    assert any(m.msg_kind == "ruling" and "缺数据来源" in m.text
               for m in orch.channel.messages)


@pytest.mark.asyncio
async def test_stall_guard_ignores_repeated_identical_tool_results(tmp_path):
    """F6 回归：停滞计数只认「新」工具结果——执行型成员每轮顺手调一次同一
    只读工具（同名同结果预览）不再把停滞计数清零，连续 N 轮无新产出照样
    停止自动唤醒移交总管（修复前 tools_used>0 即清零，守卫对这类队员失效）。"""
    (tmp_path / "notes.txt").write_text("素材", encoding="utf-8")
    registry = ToolRegistry([ListDirTool()])
    gate = PermissionGate(working_dir=tmp_path)
    director = FakeProvider([
        [ToolUseBlock(id="d1", name="team_reject",
                      input={"task_id": "T1", "reason": "还没结论"})],
        [TextBlock(text="打回一次")],
        [ToolUseBlock(id="d2", name="team_reject",
                      input={"task_id": "T1", "reason": "还是没有结论"})],
        [TextBlock(text="再打回一次")],
        [ToolUseBlock(id="d3", name="team_reject",
                      input={"task_id": "T1", "reason": "第三次打回"})],
        [TextBlock(text="继续等")],
    ])
    cfg = SimpleNamespace(member_timeout_s=300, max_rounds=40, redo_limit=5, stall_limit=2)
    events, orch = make_ai_orch(director, cfg=cfg, working_dir=tmp_path,
                                registry=registry, gate=gate)
    member = FakeProvider([
        [ToolUseBlock(id="t1", name="list_dir", input={"path": "."})],
        [TextBlock(text="第一轮：看了目录")],
        [ToolUseBlock(id="t2", name="list_dir", input={"path": "."})],
        [TextBlock(text="第二轮：又看了一遍，一样")],
        [ToolUseBlock(id="t3", name="list_dir", input={"path": "."})],
        [TextBlock(text="第三轮：还是一样")],
    ])
    await orch.create_team([spec("小研", member)])
    await orch.task_add("空转工单", "小研", type="exec")
    # 预置到「待验收」（与既有停滞守卫用例同款前置：每次打回置回进行中，
    # 成员轮毕落回待验收——reject 前置要求工单已待验收，F5 口径）
    await orch.task_update("T1", "in_progress")
    await orch.task_update("T1", "review")

    await orch.run_auto_turn("推进")
    # 每轮都有一次成功的 list_dir（旧口径会一直清零），但第 2、3 轮无新产出：
    # 停滞计数累积到上限，自动唤醒停止并注记进频道（总管不再 drop，停滞
    # 标记保持——处置留给下一轮总管按强制裁定走）
    assert orch._stalled == {"小研"} and orch._stall_counts["小研"] == 2
    assert len(member.calls) == 6  # 停滞后不再自动唤醒（3 轮 × 每轮 2 次模型调用）
    note = next(m for m in orch.channel.messages if m.from_member == "system"
                and "已停止自动唤醒" in m.text)
    assert "小研" in note.text


@pytest.mark.asyncio
async def test_report_file_size_cap_and_rolling_prune(tmp_path, monkeypatch):
    """F7 回归：落盘全文有单文件体积上限（超限截断并注记，洪泛消息不再无界
    写盘）；reports 目录的 team-*.md 按数量配额滚动清理（只动本频道命名，
    同目录子代理长报告不受波及）。"""
    import skysheep.core.team as team_mod

    monkeypatch.setattr(team_mod, "REPORT_FILE_CHAR_LIMIT", 1000)
    monkeypatch.setattr(team_mod, "TEAM_REPORT_KEEP_FILES", 3)
    reports = tmp_path / "reports"
    ch = TeamChannel(reports_dir=reports)

    huge = "字" * 5000
    msg = ch.post("小研", to_member="director", msg_kind="report", text=huge)
    assert msg.report_path, "落盘路径必须带回"
    saved = Path(msg.report_path)
    content = saved.read_text(encoding="utf-8")
    assert len(content) <= 1000 + 200  # 超上限的部分未保留（+注记余量）
    assert "超出落盘单文件上限 1000" in content

    # 同目录的子代理长报告不受滚动清理波及；team-*.md 超配额从旧到新删
    subagent_report = reports / "subagent-0001-小研.md"
    subagent_report.write_text("子代理的全文报告", encoding="utf-8")
    for i in range(5):
        ch.post("小研", to_member="director", msg_kind="report", text="x" * 3000 + str(i))
    team_files = sorted(p.name for p in reports.glob("team-*.md"))
    assert len(team_files) == 3  # 数量配额：只留最近 3 个
    assert subagent_report.exists()  # 子代理报告原样
    assert len(ch.messages[-1].text) < 3000  # 频道里仍是截断摘录


@pytest.mark.asyncio
async def test_user_mode_director_note_does_not_hijack_member_usage(home):
    """F8 回归：user 总管模式的 from="director" 系统代发消息（如派工注记）
    不冲账——落在「成员用量已入 pending_usage、定稿未发出」窗口里不再把成员
    用量劫进 director_acc：usage_log 只有一行成员账，不出现 provider=""/
    model="" 的幽灵总管行。"""
    store = await SessionStore(home / "f8.db").connect()
    backend = ServerBackend(working_dir=home / "proj",
                            provider_factory=lambda: FakeProvider([]))
    backend.store = store  # 直接挂库（构造参数 store 只在 setup() 消费，这里不跑 setup）
    gate = asyncio.Event()
    events, emit = collector()
    orch = TeamOrchestrator(emit=emit)
    await orch.create_team([spec("小研", UsageThenHold(gate))])
    sid = "sess-f8"
    backend._teams[sid] = orch
    stub_agent = SimpleNamespace(history=[])
    await orch.task_add("写报告", "小研", type="exec")

    async def inject_assign():
        # 等成员用量事件已进 pending（成员流挂住中），再让派工注记插进来
        deadline = asyncio.get_running_loop().time() + 5
        while not any(isinstance(e, dict) and e.get("kind") == "usage" for e in events):
            assert asyncio.get_running_loop().time() < deadline, "用量事件未在期限内到达"
            await asyncio.sleep(0.01)
        await orch.notify_assign("T1", note="赶紧行动")

    turn = asyncio.create_task(backend._team_body(
        "@小研 干活", emit, None, agent=stub_agent, sid=sid))
    injector = asyncio.create_task(inject_assign())
    await asyncio.sleep(0.25)  # 用量已进 pending、注记已插队、成员流仍挂住
    gate.set()
    meta = await turn
    await injector

    assert meta["mode"] == "team"
    db = sqlite3.connect(home / "f8.db")
    rows = db.execute(
        "SELECT provider, model, in_tokens, out_tokens FROM usage_log"
    ).fetchall()
    db.close()
    await store.close()
    # 成员名下不丢账；没有幽灵总管行（修复前这 11/7 会被劫进 director_acc，
    # 轮末结出 ("", "", 11, 7) 的 ghost 行、成员名下零账）
    assert rows == [("p-小研", "m", 11, 7)]
