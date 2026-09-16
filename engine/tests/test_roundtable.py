"""圆桌功能测试：成员并行作答、主席融合、容错、持久化与协议接入。"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from skysheep.config import load_config
from skysheep.core.roundtable import (
    MemberSpec,
    run_roundtable,
)
from skysheep.messages import Message, TextBlock
from skysheep.models.base import Provider, ProviderTextDelta
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

    async def stream(self, messages, tool_schemas):
        raise RuntimeError("fatal boom")
        yield  # pragma: no cover


class HangingProvider(Provider):
    """永远不产出（用于超时测试）。"""

    name, model = "hang", "h1"

    async def stream(self, messages, tool_schemas):
        await asyncio.sleep(30)
        yield  # pragma: no cover


class FlakyProvider(Provider):
    """第一次调用抛瞬态错误（429），第二次正常作答。"""

    name, model = "flaky", "f1"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tool_schemas):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 429 rate limit exceeded")
        yield ProviderTextDelta("恢复后作答")


class PartialFusionProvider(Provider):
    """融合轮：吐出部分文本后抛非瞬态错误。"""

    name, model = "pf", "p1"

    async def stream(self, messages, tool_schemas):
        yield ProviderTextDelta("部分融合")
        raise RuntimeError("fatal mid-fusion")
        yield  # pragma: no cover


def make_member(spec_provider, text: str) -> MemberSpec:
    return MemberSpec(
        provider_name=spec_provider.name,
        model=spec_provider.model,
        provider=spec_provider,
    )


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
    assert outcome.output_tokens == 7 * 3  # 2 成员 + 1 融合（FakeProvider 每次 7）

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
        members=[],
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


# ---------- 协议级：WS 端到端 ----------


def make_rt_client(home, scripts):
    """scripts 顺序 = provider_factory 调用顺序：第 1 个是主席，之后是各成员。"""
    pool = [FakeProvider(s) for s in scripts]
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
            "members": [{"provider": "fa", "model": "ma"}, {"provider": "fb", "model": "mb"}],
        }})
        events = []
        frame = recv_until(ws, "r1", events)
    assert frame["ok"] and frame["result"]["done"]

    kinds = [e["event"] for e in events]
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

    # 融合答案 = 本轮正式助手消息，带圆桌元数据
    assistant = next(e for e in events if e["event"] == "assistant_message")
    msg = assistant["data"]["message"]
    assert msg["content"][0]["text"] == "最终融合答案：黑洞很致密"
    meta = msg["roundtable"]
    assert meta["mode"] == "roundtable"
    assert meta["chair"] == {"provider": "fake", "model": "fake-1"}
    assert meta["chair_answers"] is True
    assert [m["status"] for m in meta["members"]] == ["done", "done", "done"]
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
    assert "API key" in specs3[0].build_error


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
