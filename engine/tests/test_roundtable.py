"""圆桌功能测试：成员并行作答、主席融合、容错、持久化与协议接入。"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from skysheep.config import load_config
from skysheep.core.roundtable import (
    PER_PROVIDER_CONCURRENCY,
    MemberSpec,
    run_roundtable,
    usage_rows,
)
from skysheep.messages import Message, TextBlock
from skysheep.models.base import Provider, ProviderDone, ProviderTextDelta
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.server.backend import ServerBackend


def recv_until(ws, wanted_id=None, events=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


# ---------- 单元级：引擎编排 ----------


class BoomProvider(Provider):
    """第一次迭代就抛非瞬态错误。"""

    name, model = "boom", "b1"

    async def stream(self, messages, tool_schemas, effort=None):
        raise RuntimeError("fatal boom")
        yield  # pragma: no cover


class HangingProvider(Provider):
    """永远不产出（用于超时测试）。"""

    name, model = "hang", "h1"

    async def stream(self, messages, tool_schemas, effort=None):
        await asyncio.sleep(30)
        yield  # pragma: no cover


class FlakyProvider(Provider):
    """第一次调用抛瞬态错误（429），第二次正常作答。"""

    name, model = "flaky", "f1"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 429 rate limit exceeded")
        yield ProviderTextDelta("恢复后作答")


class PartialFusionProvider(Provider):
    """融合轮：吐出部分文本后抛非瞬态错误。"""

    name, model = "pf", "p1"

    async def stream(self, messages, tool_schemas, effort=None):
        yield ProviderTextDelta("部分融合")
        raise RuntimeError("fatal mid-fusion")
        yield  # pragma: no cover


def make_member(spec_provider, text: str) -> MemberSpec:
    return MemberSpec(
        provider_name=spec_provider.name,
        model=spec_provider.model,
        provider=spec_provider,
    )


class DeltaThenHang(Provider):
    """先吐一段草稿再挂住（用于取消回收测试）。"""

    name, model = "dh", "d1"

    async def stream(self, messages, tool_schemas, effort=None):
        yield ProviderTextDelta("部分草稿")
        await asyncio.sleep(30)


class AlwaysFail(Provider):
    """每次调用都抛非瞬态错误（如 Key 无效）。"""

    name, model = "af", "a1"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        raise RuntimeError("fatal always")
        yield  # pragma: no cover


class ConcurrencyProbe:
    """跨实例共享的并发观测器。"""

    def __init__(self) -> None:
        self.cur = 0
        self.peak = 0


class ThrottleProbeProvider(Provider):
    """同名服务探针：记录观测到的并发峰值。"""

    name, model = "same", "x"

    def __init__(self, probe: ConcurrencyProbe) -> None:
        super().__init__()
        self.probe = probe

    async def stream(self, messages, tool_schemas, effort=None):
        self.probe.cur += 1
        self.probe.peak = max(self.probe.peak, self.probe.cur)
        try:
            await asyncio.sleep(0.05)
        finally:
            self.probe.cur -= 1
        yield ProviderTextDelta("草稿")


def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


HISTORY = [
    Message.system("SYS"),
    Message.user("早前问题"),
    Message.assistant([TextBlock(text="早前回答")]),
]


@pytest.mark.asyncio
async def test_run_roundtable_flow_and_fusion_input(monkeypatch):
    monkeypatch.setattr("skysheep.core.roundtable.RETRY_BASE_DELAY_S", 0.01)
    chair = FakeProvider([[TextBlock(text="最终融合答案")]])
    member_a = FakeProvider([[TextBlock(text="成员A的草稿")]])
    member_b = FakeProvider([[TextBlock(text="成员B的草稿")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(member_a, "成员A的草稿"), make_member(member_b, "成员B的草稿")],
        chair=chair,
        system_text="SYS",
        history=HISTORY,
        user_text="什么是黑洞",
        timeout_s=30,
        emit=emit,
    )

    kinds = [e.kind for e in events]
    assert kinds[0] == "roundtable_started"
    assert kinds.count("roundtable_member_finished") == 2
    assert all(e.status == "done" for e in events if e.kind == "roundtable_member_finished")
    assert "text_delta" in kinds  # 融合流式
    assert kinds[-2] == "text_delta" or kinds[-1] == "usage"

    # 成员 delta 事件按成员分桶
    by_member: dict[int, str] = {}
    for e in events:
        if e.kind == "roundtable_member_delta":
            by_member[e.member_index] = by_member.get(e.member_index, "") + e.text
    assert by_member == {0: "成员A的草稿", 1: "成员B的草稿"}

    assert outcome.status == "done"
    assert outcome.fused_text == "最终融合答案"
    # 用量分两层记：成员轮合计 + 融合轮（主席名下），FakeProvider 每次 7
    assert outcome.output_tokens == 7 * 2
    assert outcome.chair_output_tokens == 7
    rows = usage_rows(outcome)
    assert sum(r["output_tokens"] for r in rows) == 7 * 3
    assert len(rows) == 3  # 2 成员 + 1 主席

    # 融合输入：主席只被调用一次（融合），消息序列规整
    assert len(chair.calls) == 1
    fusion_msgs = chair.calls[0]
    roles = [m.role for m in fusion_msgs]
    assert roles == ["system", "user", "assistant", "user"]  # 无连续同角色
    combined = fusion_msgs[-1].text
    assert "什么是黑洞" in combined
    assert "各成员独立草稿" in combined
    assert "成员A的草稿" in combined and "成员B的草稿" in combined

    # 成员消息上下文：成员系统提示词 + 主历史（去 system）+ 本次提问
    member_msgs = member_a.calls[0]
    assert member_msgs[0].role == "system" and "圆桌" in member_msgs[0].text
    assert [m.role for m in member_msgs] == ["system", "user", "assistant", "user"]
    assert member_msgs[-1].text == "什么是黑洞"


@pytest.mark.asyncio
async def test_member_failure_does_not_block_roundtable():
    chair = FakeProvider([[TextBlock(text="融合结果")]])
    ok_member = FakeProvider([[TextBlock(text="成员A的草稿")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(ok_member, "ok"), make_member(BoomProvider(), "boom")],
        chair=chair,
        system_text="SYS",
        history=HISTORY,
        user_text="问题",
        timeout_s=30,
        emit=emit,
    )

    assert outcome.status == "done" and outcome.fused_text == "融合结果"
    statuses = [r.status for r in outcome.members]
    assert statuses == ["done", "error"]
    err_result = outcome.members[1]
    assert "fatal boom" in err_result.error
    # 失败成员标注进融合草稿
    fusion_text = chair.calls[0][-1].text
    assert "该成员作答失败" in fusion_text
    finished = [e for e in events if e.kind == "roundtable_member_finished"]
    assert finished[1].status == "error" and "fatal boom" in finished[1].error


@pytest.mark.asyncio
async def test_member_timeout_is_contained():
    chair = FakeProvider([[TextBlock(text="融合结果")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(FakeProvider([[TextBlock(text="快的草稿")]]), "ok"),
                 make_member(HangingProvider(), "hang")],
        chair=chair,
        system_text="",
        history=HISTORY,
        user_text="问题",
        timeout_s=1,
        emit=emit,
    )

    assert outcome.status == "done"
    assert outcome.members[1].status == "error"
    assert "超时" in outcome.members[1].error


@pytest.mark.asyncio
async def test_transient_error_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("skysheep.core.roundtable.RETRY_BASE_DELAY_S", 0.01)
    flaky = FlakyProvider()
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(flaky, "flaky")],
        chair=FakeProvider([[TextBlock(text="融合")]]),
        system_text="",
        history=HISTORY,
        user_text="问题",
        timeout_s=30,
        emit=emit,
    )

    assert flaky.calls == 2
    assert outcome.members[0].status == "done"
    assert outcome.members[0].text == "恢复后作答"
    assert any(e.kind == "notice" for e in events)  # 重试提示可见


@pytest.mark.asyncio
async def test_fusion_partial_text_kept_on_error():
    events, emit = collector()
    outcome = await run_roundtable(
        # 得有一个可用草稿，融合才会真跑（全失败时不白耗主席一次调用）
        members=[make_member(FakeProvider([[TextBlock(text="成员草稿")]]), "成员草稿")],
        chair=PartialFusionProvider(),
        system_text="",
        history=HISTORY,
        user_text="问题",
        timeout_s=30,
        emit=emit,
    )
    assert outcome.status == "error"
    assert outcome.fused_text == "部分融合"  # 已流出的部分保留，交由上层落库
    assert "fatal mid-fusion" in outcome.error
    assert any(e.kind == "text_delta" for e in events)  # 部分文本已流式给用户


@pytest.mark.asyncio
async def test_no_usable_draft_skips_fusion():
    """全员都没产出草稿时不白跑主席融合，直接报失败。"""
    chair = FakeProvider([[TextBlock(text="不该被调用的融合")]])
    events, emit = collector()
    outcome = await run_roundtable(
        members=[make_member(BoomProvider(), "boom")],
        chair=chair,
        system_text="",
        history=HISTORY,
        user_text="问题",
        timeout_s=30,
        emit=emit,
    )
    assert outcome.status == "error"
    assert "均未产出回答" in outcome.error
    assert chair.calls == []  # 融合根本没跑
    assert not any(e.kind == "text_delta" for e in events)


# ---------- 协议级：WS 端到端 ----------


def make_rt_client(home, scripts):
    """scripts 顺序 = provider_factory 调用顺序：第 1 个是主席，之后是各成员。

    项可以是 FakeProvider 的脚本（list），也可以直接传 Provider 实例。
    """
    pool = [p if isinstance(p, Provider) else FakeProvider(p) for p in scripts]
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool else FakeProvider([[TextBlock(text="备用")]]),
    )
    return TestClient(app)


def test_roundtable_send_end_to_end(home):
    scripts = [
        [[TextBlock(text="主席草稿")], [TextBlock(text="最终融合答案：黑洞很致密")]],
        [[TextBlock(text="成员A草稿")]],
        [[TextBlock(text="成员B草稿")]],
    ]
    with make_rt_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "什么是黑洞",
            "roundtable": True,
            "members": [
                {"provider": "fa", "model": "ma", "role": "critic"},
                {"provider": "fb", "model": "mb"},
            ],
        }})
        events = []
        frame = recv_until(ws, "r1", events)
    assert frame["ok"] and frame["result"]["done"]

    # task_estimate 是本轮开工时先发的耗时预估事件（与本测试无关），滤掉保持序列断言干净
    kinds = [e["event"] for e in events if e["event"] != "task_estimate"]
    assert kinds[0] == "turn_started"
    assert "roundtable_started" in kinds
    assert "roundtable_member_delta" in kinds
    assert kinds.count("roundtable_member_finished") == 3
    assert kinds[-1] == "queue_updated"

    started = next(e for e in events if e["event"] == "roundtable_started")
    members = started["data"]["members"]
    assert [m["index"] for m in members] == [0, 1, 2]
    assert members[0]["provider"] == "fake"  # 主席作为成员 0 出草稿
    assert members[1]["provider"] == "fa" and members[2]["provider"] == "fb"
    assert members[1]["role"] == "critic" and members[2]["role"] == ""

    # 融合答案 = 本轮正式助手消息，带圆桌元数据
    assistant = next(e for e in events if e["event"] == "assistant_message")
    msg = assistant["data"]["message"]
    assert msg["content"][0]["text"] == "最终融合答案：黑洞很致密"
    meta = msg["roundtable"]
    assert meta["mode"] == "roundtable"
    assert meta["chair"] == {"provider": "fake", "model": "fake-1"}
    assert meta["chair_answers"] is True
    assert meta["debate_rounds"] == 0 and meta["rounds"] == 1
    assert [m["status"] for m in meta["members"]] == ["done", "done", "done"]
    # 身份随元数据持久化（回放徽标用）
    assert [m["role"] for m in meta["members"]] == ["", "critic", ""]
    # 草稿随消息元数据持久化（刷新/重进会话后圆桌卡可回看）；用量分成员记录
    assert [m["draft"] for m in meta["members"]] == ["主席草稿", "成员A草稿", "成员B草稿"]
    assert all(m["input_tokens"] > 0 and m["output_tokens"] > 0 for m in meta["members"])
    assert frame["result"]["roundtable"] == meta

    # 持久化：user + assistant（带元数据）落库
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    roles = [r[0] for r in rows]
    assert roles == ["user", "assistant"]
    saved = json.loads(rows[1][1])
    assert saved["content"][0]["text"] == "最终融合答案：黑洞很致密"
    assert saved["roundtable"]["mode"] == "roundtable"


def test_roundtable_default_members_from_config(home):
    """默认成员策略（真实构建路径）：主席跳过、缺 Key 跳过、上限截断。"""
    home_dir = home / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text(
        """
default = "p1"
disabled_providers = ["ollama"]   # 排除内置预设（自带占位 Key）对默认策略的干扰

[roundtable]
max_members = 1

[providers.p1]
kind = "openai"
base_url = "https://p1.example"
api_key = "k1"
model = "m1"

[providers.p2]
kind = "openai"
base_url = "https://p2.example"
api_key = "k2"
model = "m2"

[providers.p3]
kind = "openai"
base_url = "https://p3.example"
api_key = "k3"
model = "m3"

[providers.p4]
kind = "openai"
base_url = "https://p4.example"
model = "m4"
""",
        encoding="utf-8",
    )
    backend = ServerBackend(working_dir=home / "proj")
    backend.cfg = load_config()
    backend.provider_name = "p1"
    backend.provider_model = "m1"

    # 默认策略：p1 是主席（跳过）、p4 缺 Key（跳过）、p2/p3 有 Key 但上限 1 → 只取 p2
    specs = backend._resolve_members(None)
    assert [(s.provider_name, s.model) for s in specs] == [("p2", "m2")]
    assert specs[0].provider is not None  # 真实构建成功（有 Key）

    # 显式成员：p1 与主席同名同模型 → 跳过；p3 正常入选
    specs2 = backend._resolve_members(
        [{"provider": "p3", "model": "m3"}, {"provider": "p1", "model": "m1"}]
    )
    assert [(s.provider_name, s.model) for s in specs2] == [("p3", "m3")]

    # 构建失败（缺 Key）的成员降级为错误卡片而不是拖垮整场
    specs3 = backend._resolve_members([{"provider": "p4", "model": "m4"}])
    assert specs3[0].provider is None
    assert "还没有配置 API Key" in specs3[0].build_error


def test_roundtable_without_members_errors_cleanly(home):
    # 无显式成员且没有其他可用 provider（ollama 预设自带占位 Key，这里禁掉）
    # → 默认策略选不到成员，应给出明确错误而不是空跑
    home_dir = home / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text(
        'disabled_providers = ["ollama"]\n',
        encoding="utf-8",
    )
    with make_rt_client(home, [[[TextBlock(text="x")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r3", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True,
        }})
        frame = recv_until(ws, "r3")
    assert not frame["ok"]
    assert "圆桌没有可用成员" in frame["error"]


def test_normal_send_has_no_roundtable_events(home):
    with make_rt_client(home, [[[TextBlock(text="普通回答")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r4", "method": "chat.send", "params": {"text": "你好"}})
        events = []
        frame = recv_until(ws, "r4", events)
    assert frame["ok"]
    kinds = [e["event"] for e in events]
    assert not any(k.startswith("roundtable") for k in kinds)
    assistant = next(e for e in events if e["event"] == "assistant_message")
    assert assistant["data"]["message"]["roundtable"] is None


# ---------- 多轮辩论 / 取消 / 用量 / 降级（新增行为） ----------


@pytest.mark.asyncio
async def test_debate_rounds_revise_with_peer_drafts():
    """debate_rounds=1：成员看到彼此草稿后修订，融合用修订后的草稿。"""
    member_a = FakeProvider([[TextBlock(text="A1")], [TextBlock(text="A2修订")]])
    member_b = FakeProvider([[TextBlock(text="B1")], [TextBlock(text="B2修订")]])
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(member_a, "A"), make_member(member_b, "B")],
        chair=chair, system_text="SYS", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit, debate_rounds=1,
    )

    assert outcome.status == "done"
    started = next(e for e in events if e.kind == "roundtable_started")
    assert started.rounds == 2
    # 第二轮：B 的修订提示词里能看到 A 的上一轮草稿
    b_round2 = member_b.calls[1]
    assert "A1" in b_round2[-1].text
    assert "修订" in b_round2[-1].text
    # 事件带 round 字段：两轮 delta 都在
    assert {e.round for e in events if e.kind == "roundtable_member_delta"} == {0, 1}
    # 融合用修订后的最终草稿，不再包含上一轮文本
    fusion = chair.calls[0][-1].text
    assert "A2修订" in fusion and "B2修订" in fusion
    assert "A1" not in fusion and "B1" not in fusion


@pytest.mark.asyncio
async def test_debate_converged_member_is_skipped():
    """debate_rounds=2：草稿没改动的成员在下一轮跳过（不再花钱），并发出 skipped 事件。"""
    member_a = FakeProvider([[TextBlock(text="A1")], [TextBlock(text="A1")]])
    member_b = FakeProvider([
        [TextBlock(text="B1")], [TextBlock(text="B2")], [TextBlock(text="B3")],
    ])
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(member_a, "A"), make_member(member_b, "B")],
        chair=chair, system_text="SYS", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit, debate_rounds=2,
    )

    assert outcome.status == "done"
    assert len(member_a.calls) == 2  # 第 3 轮被跳过
    assert len(member_b.calls) == 3  # B 每轮都在改，照常修订
    skipped = [e for e in events if e.kind == "roundtable_member_finished" and e.skipped]
    assert len(skipped) == 1
    assert skipped[0].member_index == 0 and skipped[0].round == 2


@pytest.mark.asyncio
async def test_cancel_during_members_returns_cancelled():
    """成员作答阶段取消：引擎吞掉 CancelledError 并返回 cancelled 状态，
    由调用方把已入历史的 user 消息落库。"""
    events, emit = collector()
    task = asyncio.create_task(run_roundtable(
        members=[make_member(HangingProvider(), "hang")],
        chair=FakeProvider([[TextBlock(text="融合")]]),
        system_text="", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    outcome = await task
    assert outcome.status == "cancelled"
    assert outcome.fused_text == ""
    assert not any(e.kind == "text_delta" for e in events)


@pytest.mark.asyncio
async def test_cancel_during_fusion_keeps_partial_text():
    """融合阶段取消：已流出的部分文本保留在 outcome 里，供调用方落库。"""

    class PartialThenHang(Provider):
        name, model = "ph", "p1"

        async def stream(self, messages, tool_schemas, effort=None):
            yield ProviderTextDelta("部分融合")
            await asyncio.sleep(30)

    events, emit = collector()
    task = asyncio.create_task(run_roundtable(
        members=[make_member(FakeProvider([[TextBlock(text="草稿")]]), "m")],
        chair=PartialThenHang(),
        system_text="", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit,
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    outcome = await task
    assert outcome.status == "cancelled"
    assert outcome.fused_text == "部分融合"
    assert any(e.kind == "text_delta" for e in events)


def test_roundtable_usage_logged_per_member(home):
    """成员逐条 + 融合记主席名下写入 usage_log（此前圆桌轮记的是 0）。"""
    scripts = [
        [[TextBlock(text="主席草稿")], [TextBlock(text="融合答案")]],
        [[TextBlock(text="成员A草稿")]],
    ]
    with make_rt_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
        }})
        frame = recv_until(ws, "u1")
    assert frame["ok"]
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute(
        "SELECT provider, model, in_tokens, out_tokens, cached_tokens FROM usage_log"
    ).fetchall()
    # 3 条：主席草稿 + 成员A + 融合（都记在各自 provider/model 名下）
    assert len(rows) == 3
    assert all(r[2] > 0 and r[3] > 0 for r in rows)
    models = {r[1] for r in rows}
    assert "ma" in models and "fake-1" in models
    # FakeProvider 不报缓存明细 → cached_tokens 落 0，但列必须在
    assert all(r[4] == 0 for r in rows)


def test_usage_rows_carries_cached_tokens():
    """usage_rows 把成员与主席的缓存命中数一并透出，供 usage_log 入账。"""
    from skysheep.core.roundtable import MemberResult, RoundtableOutcome

    spec = MemberSpec(provider_name="pa", model="m1")
    member = MemberResult(spec=spec, index=0,
                          input_tokens=100, output_tokens=20, cached_tokens=80)
    outcome = RoundtableOutcome(members=[member])
    outcome.chair_provider, outcome.chair_model = "pb", "m2"
    outcome.chair_input_tokens = 50
    outcome.chair_output_tokens = 10
    outcome.chair_cached_tokens = 40

    rows = usage_rows(outcome)
    assert rows[0] == {
        "provider": "pa", "model": "m1",
        "input_tokens": 100, "output_tokens": 20, "cached_tokens": 80,
    }
    assert rows[1]["cached_tokens"] == 40


def test_fusion_failure_degrades_to_member_drafts(home):
    """融合失败时降级：成员草稿按对比式保留，不白白浪费已花的 token。"""

    class ChairDraftThenFatal(Provider):
        name, model = "fake", "fake-1"

        def __init__(self):
            super().__init__()
            self.calls = 0

        async def stream(self, messages, tool_schemas, effort=None):
            self.calls += 1
            if self.calls == 1:
                yield ProviderTextDelta("主席草稿")
                yield ProviderDone(stop_reason="end_turn", input_tokens=3, output_tokens=3)
            else:
                raise RuntimeError("fatal fusion boom")

    with make_rt_client(
        home, [ChairDraftThenFatal(), [[TextBlock(text="成员A草稿")]]]
    ) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
        }})
        events = []
        frame = recv_until(ws, "d1", events)
    assert frame["ok"]
    kinds = [e["event"] for e in events]
    assert "error" in kinds  # 融合失败如实上报
    msgs = [e for e in events if e["event"] == "assistant_message"]
    assert len(msgs) == 2  # 主席草稿 + 成员A草稿，降级保留
    for m in msgs:
        meta = m["data"]["message"]["roundtable"]
        assert meta["degraded"] is True and meta["mode"] == "compare"
    db = sqlite3.connect(home / "home" / "skysheep.db")
    roles = [r[0] for r in db.execute("SELECT role FROM messages ORDER BY id").fetchall()]
    assert roles == ["user", "assistant", "assistant"]


def test_compare_mode_skips_fusion(home):
    """A/B 对比：只跑成员作答，主席不再白跑一次融合。"""
    chair = FakeProvider([[TextBlock(text="主席草稿")]])
    with make_rt_client(
        home,
        [chair, [[TextBlock(text="甲的回答")]], [[TextBlock(text="乙的回答")]]],
    ) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True, "compare": True,
            "members": [{"provider": "fa", "model": "ma"}, {"provider": "fb", "model": "mb"}],
        }})
        events = []
        frame = recv_until(ws, "c1", events)
    assert frame["ok"] and frame["result"]["roundtable"]["mode"] == "compare"
    assert len(chair.calls) == 1  # 只作为成员出了一份草稿，没有融合调用
    assert not any(e["event"] == "text_delta" for e in events)  # 没有融合流式
    msgs = [e for e in events if e["event"] == "assistant_message"]
    assert len(msgs) == 3  # 主席 + 甲 + 乙，各自成消息


def test_member_limit_notice(home):
    """选了超过上限的成员：静默截断改为明确提示。"""
    home_dir = home / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text("[roundtable]\nmax_members = 1\n", encoding="utf-8")
    scripts = [
        [[TextBlock(text="主席草稿")], [TextBlock(text="融合")]],
        [[TextBlock(text="成员A草稿")]],
    ]
    with make_rt_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}, {"provider": "fb", "model": "mb"}],
        }})
        events = []
        frame = recv_until(ws, "m1", events)
    assert frame["ok"]
    notice = next(e for e in events if e["event"] == "notice")
    assert "上限" in notice["data"]["message"]
    meta = frame["result"]["roundtable"]
    assert len(meta["members"]) == 2  # 主席 + 截断后仅存的 1 个成员


def test_roundtable_images_notice(home):
    """图片附件被圆桌忽略：发提示而不是静默丢弃，且不发给成员。"""
    member = FakeProvider([[TextBlock(text="成员草稿")]])
    scripts = [
        [[TextBlock(text="主席草稿")], [TextBlock(text="融合")]],
        member,
    ]
    with make_rt_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i1", "method": "chat.send", "params": {
            "text": "看看这张图", "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
            "images": [{"media_type": "image/png", "data": "aGVsbG8="}],
        }})
        events = []
        frame = recv_until(ws, "i1", events)
    assert frame["ok"]
    notice = next(e for e in events if e["event"] == "notice")
    assert "纯文本协作" in notice["data"]["message"]
    # 图片不发给成员
    assert all(
        getattr(b, "type", "") != "image"
        for msg in member.calls[0]
        for b in msg.content
    )
    # 图片随 user 消息保留（后续普通轮能用）
    db = sqlite3.connect(home / "home" / "skysheep.db")
    row = db.execute("SELECT content FROM messages WHERE role='user'").fetchone()
    blocks = json.loads(row[0])["content"]
    assert any(b.get("type") == "image" for b in blocks)


def test_roundtable_settings_roundtrip(home):
    """设置页圆桌卡片：读回当前值 → 保存 → 写回 config.toml 并热生效。"""
    import tomllib

    with make_rt_client(home, [[[TextBlock(text="x")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g1", "method": "roundtable.get"})
        got = recv_until(ws, "g1")
        assert got["ok"]
        assert got["result"]["max_members"] == 3
        assert got["result"]["debate_rounds"] == 0

        ws.send_json({"id": "s1", "method": "roundtable.save", "params": {
            "max_members": 5, "member_timeout_s": 90,
            "debate_rounds": 1, "chair_answers": False,
            "member_history_turns": 2,
        }})
        saved = recv_until(ws, "s1")
        assert saved["ok"]
        assert saved["result"]["max_members"] == 5
        assert saved["result"]["debate_rounds"] == 1
        assert saved["result"]["chair_answers"] is False

        ws.send_json({"id": "g2", "method": "roundtable.get"})
        again = recv_until(ws, "g2")
        assert again["result"]["member_timeout_s"] == 90
        assert again["result"]["member_history_turns"] == 2

    raw = (home / "home" / "config.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    assert data["roundtable"]["max_members"] == 5
    assert data["roundtable"]["debate_rounds"] == 1
    assert data["roundtable"]["chair_answers"] is False
    assert data["roundtable"]["member_history_turns"] == 2


def test_roundtable_regenerate_reruns_roundtable(home):
    """重新生成圆桌消息：沿用原配置重跑圆桌，不产生重复的 user 消息。"""
    chair = FakeProvider([
        [TextBlock(text="主草稿1")], [TextBlock(text="融合1")],
        [TextBlock(text="主草稿2")], [TextBlock(text="融合2")],
    ])
    member = FakeProvider([[TextBlock(text="甲1")], [TextBlock(text="甲2")]])
    # 池里放两份同一成员实例：重跑时成员 provider 会重新构建（从池里再取一次）
    with make_rt_client(home, [chair, member, member]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
        }})
        first = recv_until(ws, "r1")
        assert first["ok"]
        sid = first["result"]["session_id"]
        # 重新生成：先截断旧回答，再按原圆桌配置重跑（前端 msgRegenerate 的行为）
        ws.send_json({"id": "t1", "method": "session.truncate", "params": {
            "id": sid, "mode": "regen",
        }})
        assert recv_until(ws, "t1")["ok"]
        ws.send_json({"id": "r2", "method": "chat.send", "params": {
            "text": "", "session_id": sid, "regenerate": True,
            "roundtable": True, "members": [{"provider": "fa", "model": "ma"}],
            "chair_answers": True, "debate_rounds": 0,
        }})
        second = recv_until(ws, "r2")
    assert second["ok"]
    assert second["result"]["roundtable"]["mode"] == "roundtable"
    # 重跑后的成员草稿来自第二轮脚本
    assert second["result"]["roundtable"]["members"][-1]["draft"] == "甲2"
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    roles = [r[0] for r in rows]
    # user 只有一条（重新生成不重复追加），末条是重新生成的融合 2
    assert roles.count("user") == 1
    assert json.loads(rows[-1][1])["content"][0]["text"] == "融合2"
    # 成员参与了第二轮圆桌（实例被重用，脚本的第二条被用掉）
    assert len(member.calls) == 2


# ---------- 取消回收 / 草稿总预算 / 并发闸 / 辩论轮跳过（健壮性修订） ----------


@pytest.mark.asyncio
async def test_cancel_during_members_keeps_partial_drafts():
    """成员阶段取消：进行中成员已流出的部分草稿回收进 outcome，
    状态 error（标注已取消）——用户已看到的内容不凭空消失。"""
    events, emit = collector()
    task = asyncio.create_task(run_roundtable(
        members=[make_member(DeltaThenHang(), "part")],
        chair=FakeProvider([[TextBlock(text="融合")]]),
        system_text="", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit,
    ))
    await asyncio.sleep(0.1)  # 等第一条 delta 流出
    task.cancel()
    outcome = await task
    assert outcome.status == "cancelled"
    assert len(outcome.members) == 1
    r = outcome.members[0]
    assert r.status == "error" and "已取消" in r.error
    assert r.text == "部分草稿"
    assert any(e.kind == "roundtable_member_delta" for e in events)


@pytest.mark.asyncio
async def test_fusion_drafts_clipped_to_total_budget():
    """主席上下文有限时：融合草稿按总预算等比截断（每个成员都保留开头），
    而不是把整场融合撑爆。"""
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()
    outcome = await run_roundtable(
        members=[
            make_member(FakeProvider([[TextBlock(text="甲" * 5000)]]), "a"),
            make_member(FakeProvider([[TextBlock(text="乙" * 5000)]]), "b"),
            make_member(FakeProvider([[TextBlock(text="丙" * 5000)]]), "c"),
        ],
        chair=chair, system_text="SYS", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit, chair_context_tokens=10_000,
    )
    assert outcome.status == "done"
    combined = chair.calls[0][-1].text
    # 预算 ≈ 10000 − 历史/问题/指令估算 − 答案余量（下限随上下文 5% 缩放，
    # 10000 上下文落到绝对下限 2000 档之上、由剩余空间决定）
    assert len(combined) < 11_000  # 草稿总量被压进预算 + 三节标题 + 融合指令
    assert combined.count("已截断") == 3
    assert all(f"### 成员{i}" in combined for i in (1, 2, 3))  # 每个成员的观点都还在


@pytest.mark.asyncio
async def test_fusion_budget_floor_scales_with_context():
    """小上下文主席：预算下限随上下文缩放（5%，绝对下限 2000），
    不再把 8000 字草稿硬塞进塞不下的上下文。"""
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()
    outcome = await run_roundtable(
        members=[
            make_member(FakeProvider([[TextBlock(text="甲" * 5000)]]), "a"),
            make_member(FakeProvider([[TextBlock(text="乙" * 5000)]]), "b"),
        ],
        chair=chair, system_text="SYS", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit, chair_context_tokens=6_000,
    )
    assert outcome.status == "done"
    combined = chair.calls[0][-1].text
    # 6000 上下文 − 历史与指令估算 − 4000 答案余量为负 → 落到下限
    # max(2000, 6000//20=300) = 2000 字符：融合仍成功，草稿总量远小于旧行为的 8000
    assert len(combined) < 4_000
    assert outcome.fused_text == "融合"


@pytest.mark.asyncio
async def test_same_provider_members_are_throttled():
    """同一 provider_name 的多个成员共享并发闸：4 个成员只允许 2 路并发，
    避免一把 API Key 并发多路触发限流（流式吐到一半的 429 是不可重试的）。"""
    probe = ConcurrencyProbe()
    events, emit = collector()
    outcome = await run_roundtable(
        members=[make_member(ThrottleProbeProvider(probe), f"m{i}") for i in range(4)],
        chair=FakeProvider([[TextBlock(text="融合")]]),
        system_text="", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit,
    )
    assert outcome.status == "done"
    assert probe.peak == PER_PROVIDER_CONCURRENCY


@pytest.mark.asyncio
async def test_debate_skips_hopeless_members():
    """辩论轮跳过规则：构建失败成员从第一个辩论轮起零成本跳过；运行期失败
    的成员在第一个辩论轮还有一次重试机会，连败两轮后不再重试；正常成员照常。"""
    ok_member = FakeProvider([[TextBlock(text="C1")], [TextBlock(text="C1")]])
    boom = AlwaysFail()
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()
    dead_spec = MemberSpec(
        provider_name="dead", model="d", provider=None, build_error="no key",
    )

    outcome = await run_roundtable(
        members=[dead_spec, make_member(boom, "boom"), make_member(ok_member, "ok")],
        chair=chair,
        system_text="", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit, debate_rounds=2,
    )

    assert outcome.status == "done"
    assert [r.status for r in outcome.members] == ["error", "error", "done"]
    assert boom.calls == 2  # 首轮 + 辩论轮的一次重试机会，之后跳过
    assert len(ok_member.calls) == 2  # 第 3 轮已收敛跳过
    skipped = [e for e in events if e.kind == "roundtable_member_finished" and e.skipped]
    # 第 2 轮跳过：dead；第 3 轮跳过：dead + boom + ok
    assert {(e.member_index, e.round) for e in skipped} == {
        (0, 1), (0, 2), (1, 2), (2, 2),
    }
    # 融合照常进行：可用草稿与失败占位并存
    fusion = chair.calls[0][-1].text
    assert "C1" in fusion and fusion.count("该成员作答失败") == 2


def test_cancel_persists_member_drafts_as_compare(home):
    """成员阶段取消（无融合文本）：已完成的成员草稿按对比式落库，
    刷新/重进会话后不消失——用户已经看到过这些卡。"""

    class ChairDraftThenHang(Provider):
        """第 1 次调用（主席草稿）正常完成，第 2 次（融合）永远挂住。"""

        name, model = "fake", "fake-1"

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def stream(self, messages, tool_schemas, effort=None):
            self.calls += 1
            if self.calls == 1:
                yield ProviderTextDelta("主席草稿")
                yield ProviderDone(stop_reason="end_turn", input_tokens=3, output_tokens=3)
                return
            await asyncio.sleep(30)
            yield  # pragma: no cover

    with make_rt_client(
        home, [ChairDraftThenHang(), [[TextBlock(text="成员A草稿")]]]
    ) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "x1", "method": "chat.send", "params": {
            "text": "问题", "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
        }})
        finished = 0
        while True:
            frame = ws.receive_json()
            if frame.get("event") == "roundtable_member_finished":
                finished += 1
                if finished == 2:  # 主席草稿 + 成员A 已产出，融合（挂住）进行中
                    ws.send_json({"id": "s1", "method": "stop", "params": {}})
            if frame.get("id") == "x1":
                break
    assert frame["ok"] and frame["result"]["stopped"] is True
    meta = frame["result"]["roundtable"]
    assert meta["mode"] == "compare" and meta["status"] == "cancelled"
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["user", "assistant", "assistant"]
    texts = [json.loads(r[1])["content"][0]["text"] for r in rows[1:]]
    assert texts == ["主席草稿", "成员A草稿"]
    for r in rows[1:]:
        saved = json.loads(r[1])["roundtable"]
        assert saved["cancelled"] is True and saved["mode"] == "compare"


# ---------- 成员身份 / 轻上下文 / 近重去重（token 三件套） ----------


@pytest.mark.asyncio
async def test_member_role_changes_system_prompt():
    """身份预设：角色提示词拼进成员系统提示词（独立作答轮与辩论轮都在），
    并随 roundtable_started 事件透出（前端徽标用）。"""
    member = FakeProvider([[TextBlock(text="甲1")], [TextBlock(text="甲2修订")]])
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()
    spec = MemberSpec(provider_name="fake", model="fake-1", provider=member, role="critic")

    outcome = await run_roundtable(
        members=[spec],
        chair=chair, system_text="SYS", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit, debate_rounds=1,
    )

    assert outcome.status == "done"
    for call in member.calls:  # 两轮的系统提示词都带身份
        assert "批评者" in call[0].text
        assert "圆桌讨论的成员之一" in call[0].text  # 圆桌成员提示词仍在
    started = next(e for e in events if e.kind == "roundtable_started")
    assert started.members[0]["role"] == "critic"


@pytest.mark.asyncio
async def test_member_light_history_trims_member_context():
    """member_history_turns=1：成员只看最近一轮历史，主席融合仍吃全量。"""
    long_history = [
        Message.system("SYS"),
        Message.user("第一轮问题"), Message.assistant([TextBlock(text="第一轮回答")]),
        Message.user("第二轮问题"), Message.assistant([TextBlock(text="第二轮回答")]),
    ]
    member = FakeProvider([[TextBlock(text="草稿")]])
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(member, "m")],
        chair=chair, system_text="SYS", history=long_history, user_text="新问题",
        timeout_s=30, emit=emit, member_history_turns=1,
    )

    assert outcome.status == "done"
    member_msgs = member.calls[0]
    texts = [m.text for m in member_msgs]
    assert "第二轮问题" in texts  # 最近一轮保留
    assert "第一轮问题" not in texts  # 更早的轮次被裁掉
    assert member_msgs[-1].text == "新问题"
    # 主席融合仍吃全量历史（历史在独立消息里，不在合并后的草稿消息里）
    fusion_all = "\n".join(m.text for m in chair.calls[0])
    assert "第一轮问题" in fusion_all and "第二轮问题" in fusion_all


@pytest.mark.asyncio
async def test_fusion_dedupes_similar_drafts():
    """近重去重：雷同的长草稿在融合提示词里替换为指回占位，不再逐字重复；
    短草稿不参与（相似度高是常态、语义可能相反）。"""
    same = "黑洞是引力坍缩的产物。" * 60  # 720 字，超过参与门槛
    member_a = FakeProvider([[TextBlock(text=same)]])
    member_b = FakeProvider([[TextBlock(text=same + "（微调）")]])
    chair = FakeProvider([[TextBlock(text="融合")]])
    events, emit = collector()

    outcome = await run_roundtable(
        members=[make_member(member_a, "a"), make_member(member_b, "b")],
        chair=chair, system_text="", history=HISTORY, user_text="Q",
        timeout_s=30, emit=emit,
    )

    assert outcome.status == "done"
    fusion = chair.calls[0][-1].text
    assert "与成员1基本一致" in fusion  # 成员B 被替换为占位
    assert "（微调）" not in fusion  # 雷同正文不再逐字进入提示词


@pytest.mark.asyncio
async def test_dedupe_keeps_short_drafts_untouched():
    """短草稿即使完全相同也不去重（语义可能相反，替换反而丢信息）。"""
    from skysheep.core.roundtable import dedupe_similar_bodies

    bodies = dedupe_similar_bodies([(0, "是的"), (1, "是的")])
    assert bodies == ["是的", "是的"]
