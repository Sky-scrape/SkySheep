"""对抗功能测试：四角色流水线编排、失败隔离、取消、持久化与协议接入。

与 test_roundtable.py 同一套测试基建（FakeProvider 脚本 + TestClient WS）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from skysheep.core.adversarial import (
    EMPTY_RESULT_REPORT,
    extract_json_object,
    findings_meta,
    run_adversarial,
    stats,
    usage_rows,
)
from skysheep.core.roundtable import MemberSpec
from skysheep.messages import Message, TextBlock
from skysheep.models.base import Provider, ProviderTextDelta
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app


def recv_until(ws, wanted_id=None, events=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


# ---------- 单元级：JSON 提取 ----------


def test_extract_json_object_variants():
    fenced = "前置说明\n```json\n{\"findings\": []}\n```\n后缀"
    assert extract_json_object(fenced) == {"findings": []}
    plain = '  {"verdicts": []}  '
    assert extract_json_object(plain) == {"verdicts": []}
    junked = '好的，以下是结果：{"solutions": [{"id": "F-001"}]} 以上。'
    assert extract_json_object(junked) == {"solutions": [{"id": "F-001"}]}
    assert extract_json_object("完全不是 JSON") is None
    assert extract_json_object('[1, 2, 3]') is None  # 顶层必须是对象
    assert extract_json_object('{"findings": []') is None  # 括号不平衡


# ---------- 单元级：引擎编排 ----------


def make_member(provider) -> MemberSpec:
    return MemberSpec(
        provider_name=provider.name,
        model=provider.model,
        provider=provider,
    )


FINDER_JSON = json.dumps({
    "findings": [
        {
            "category": "bug", "severity": "high", "location": "a.py:10",
            "description": "空列表调用 pop 会越界", "evidence": "第 10 行没有判空",
        },
        {
            "category": "security", "severity": "critical", "location": "b.py:3",
            "description": "拼接 SQL 有注入风险", "evidence": "格式化字符串进查询",
        },
        {
            "category": "performance", "severity": "low", "location": "c.py:1",
            "description": "循环里重复编译正则", "evidence": "每次迭代都 re.compile",
        },
    ]
}, ensure_ascii=False)

INVESTIGATOR_JSON = json.dumps({
    "verdicts": [
        {"id": "F-001", "verdict": "confirmed", "reason": "确实没有判空"},
        {
            "id": "F-002", "verdict": "partial",
            "reason": "有参数化查询兜底，但日志里仍拼了原文",
            "corrected_description": "日志记录了原始查询串，可能泄露敏感参数",
            "corrected_severity": "low",
        },
        {"id": "F-003", "verdict": "refuted", "reason": "正则编译结果被外层 lru_cache 复用"},
    ]
}, ensure_ascii=False)

ADVISOR_JSON = json.dumps({
    "solutions": [
        {"id": "F-001", "fix": "先判空再取元素，或用 next(iter, default)", "risk": "低", "priority": "P0"},
        {"id": "F-002", "fix": "日志脱敏后再落盘", "risk": "低", "priority": "P1"},
    ]
}, ensure_ascii=False)

JUDGE_REPORT = "## 对抗审查报告\n\n### 1. a.py:10 空列表越界\n\n修复：先判空。\n\n总体：一处高危需立即处理。"


def scripted_providers():
    """按角色配好脚本的四件套。"""
    finder = FakeProvider([[TextBlock(text=FINDER_JSON)]])
    investigator = FakeProvider([[TextBlock(text=INVESTIGATOR_JSON)]])
    advisor = FakeProvider([[TextBlock(text=ADVISOR_JSON)]])
    judge = FakeProvider([[TextBlock(text=JUDGE_REPORT)]])
    return finder, investigator, advisor, judge


def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


@pytest.mark.asyncio
async def test_run_adversarial_full_flow():
    finder, investigator, advisor, judge = scripted_providers()
    events, emit = collector()
    history = [
        Message.system("SYS"),
        Message.user("早前问题"),
        Message.assistant([TextBlock(text="早前回答")]),
    ]

    outcome = await run_adversarial(
        finder=make_member(finder),
        investigator=make_member(investigator),
        advisor=make_member(advisor),
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=history,
        user_text="审查这段代码：print(pop([]))",
        timeout_s=30,
        emit=emit,
    )

    kinds = [e.kind for e in events]
    assert kinds[0] == "adversarial_started"
    assert kinds.count("adversarial_finding_proposed") == 3
    assert kinds.count("adversarial_verdict") == 3  # pending 的不发事件
    assert "text_delta" in kinds  # 裁判报告流式
    assert kinds[-1] == "adversarial_finished"
    finished = events[-1]
    assert finished.status == "done"
    assert "成立 1" in finished.summary and "推翻 1" in finished.summary

    # 角色名册随 started 下发
    started = next(e for e in events if e.kind == "adversarial_started")
    assert [r["role"] for r in started.roles] == [
        "finder", "investigator", "advisor", "judge",
    ]

    # 问题与裁决/方案就地写回；partial 的修正字段生效
    assert [f.verdict for f in outcome.findings] == ["confirmed", "partial", "refuted"]
    f1, f2, f3 = outcome.findings
    assert f1.solution and f1.priority == "P0"
    assert f2.effective_description.startswith("日志记录")
    assert f2.effective_severity == "low"
    assert f3.solution == ""  # 被推翻的问题不给方案

    # 阶段消息互不共享（上下文隔离）：四个角色各一次调用，系统提示词各自独立
    assert len(finder.calls) == 1 and len(judge.calls) == 1
    assert "对抗性审查中的发现者" in finder.calls[0][0].text
    assert "终审裁判" in judge.calls[0][0].text
    # 调查者只拿到问题清单，看不到发现者的原始推理（对齐键是编号）
    inv_user = investigator.calls[0][-1].text
    assert "F-001" in inv_user and "空列表调用 pop" in inv_user

    # 用量：FakeProvider 每次 in=11/out=7，四角色各一行
    rows = usage_rows(outcome)
    assert len(rows) == 4
    assert sum(r["output_tokens"] for r in rows) == 7 * 4
    assert [r["role"] for r in outcome.role_usage] == [
        "finder", "investigator", "advisor", "judge",
    ]

    s = stats(outcome)
    assert s == {"total": 3, "confirmed": 1, "refuted": 1, "partial": 1, "pending": 0}

    # findings_meta 是持久化形状（限长、裁决与方案齐备）
    meta = findings_meta(outcome)
    assert meta[0]["verdict"] == "confirmed" and meta[0]["solution"]
    assert meta[1]["severity"] == "low" and "日志" in meta[1]["description"]
    assert outcome.report == JUDGE_REPORT


@pytest.mark.asyncio
async def test_finder_failure_is_error():
    class Boom(Provider):
        name, model = "boom", "b1"

        async def stream(self, messages, tool_schemas, effort=None):
            raise RuntimeError("fatal boom")
            yield  # pragma: no cover

    events, emit = collector()
    outcome = await run_adversarial(
        finder=make_member(Boom()),
        investigator=make_member(FakeProvider([[TextBlock(text="{}")]])),
        advisor=None,
        judge=FakeProvider([[TextBlock(text="报告")]]),
        history=[], user_text="目标", timeout_s=30, emit=emit,
    )
    assert outcome.status == "error"
    assert "发现阶段失败" in outcome.error
    assert outcome.findings == [] and outcome.report == ""
    finished = events[-1]
    assert finished.status == "error"


@pytest.mark.asyncio
async def test_investigator_failure_marks_pending():
    class Boom(Provider):
        name, model = "boom", "b1"

        async def stream(self, messages, tool_schemas, effort=None):
            raise RuntimeError("fatal boom")
            yield  # pragma: no cover

    finder, _, advisor, judge = scripted_providers()
    events, emit = collector()
    outcome = await run_adversarial(
        finder=make_member(finder),
        investigator=make_member(Boom()),
        advisor=make_member(advisor),
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=[], user_text="目标", timeout_s=30, emit=emit,
    )
    # 调查失败：全部保持待定（不冒充成立也不冒充推翻），没有裁决事件
    assert outcome.status == "done"
    assert all(f.verdict == "pending" for f in outcome.findings)
    assert not any(e.kind == "adversarial_verdict" for e in events)
    # 没有成立问题 → 建议阶段跳过；裁判照常出报告（报告里如实带待定）
    assert not any(
        e.kind == "adversarial_phase" and e.phase == "advisor" for e in events
    )
    assert outcome.report == JUDGE_REPORT
    finished = events[-1]
    assert "待定 3" in finished.summary


@pytest.mark.asyncio
async def test_advisor_none_skips_phase():
    finder, investigator, _, judge = scripted_providers()
    events, emit = collector()
    outcome = await run_adversarial(
        finder=make_member(finder),
        investigator=make_member(investigator),
        advisor=None,  # 显式跳过建议阶段
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=[], user_text="目标", timeout_s=30, emit=emit,
    )
    assert outcome.status == "done"
    assert not any(
        e.kind == "adversarial_phase" and e.phase == "advisor" for e in events
    )
    assert all(f.solution == "" for f in outcome.findings)
    # started 名册里建议者为空
    started = next(e for e in events if e.kind == "adversarial_started")
    advisor_row = next(r for r in started.roles if r["role"] == "advisor")
    assert advisor_row["provider"] == ""


@pytest.mark.asyncio
async def test_json_fix_retry_recovers():
    junk = "我觉得这段代码问题很多，值得仔细审查。"
    finder = FakeProvider([[TextBlock(text=junk)], [TextBlock(text=FINDER_JSON)]])
    _, investigator, advisor, judge = scripted_providers()
    events, emit = collector()
    outcome = await run_adversarial(
        finder=make_member(finder),
        investigator=make_member(investigator),
        advisor=make_member(advisor),
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=[], user_text="目标", timeout_s=30, emit=emit,
    )
    assert outcome.status == "done"
    assert len(outcome.findings) == 3
    # 第二次调用喂回了原始回复 + 纠错指令
    assert len(finder.calls) == 2
    assert finder.calls[1][-1].role == "user"
    assert "JSON" in finder.calls[1][-1].text


@pytest.mark.asyncio
async def test_max_findings_truncates():
    finder = FakeProvider([[TextBlock(text=FINDER_JSON)]])
    _, investigator, advisor, judge = scripted_providers()
    events, emit = collector()
    outcome = await run_adversarial(
        finder=make_member(finder),
        investigator=make_member(investigator),
        advisor=make_member(advisor),
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=[], user_text="目标", timeout_s=30, emit=emit,
        max_findings=2,
    )
    assert len(outcome.findings) == 2
    notices = [e.message for e in events if e.kind == "notice"]
    assert any("超出单场上限" in m for m in notices)
    # 调查者只见到截断后的两条
    assert "F-003" not in investigator.calls[0][-1].text


@pytest.mark.asyncio
async def test_empty_findings_short_circuit():
    finder = FakeProvider([[TextBlock(text='{"findings": []}')]])
    judge = FakeProvider([[TextBlock(text="不应被调用")]])
    events, emit = collector()
    outcome = await run_adversarial(
        finder=make_member(finder),
        investigator=make_member(FakeProvider([[TextBlock(text="{}")]])),
        advisor=None,
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=[], user_text="干净的目标", timeout_s=30, emit=emit,
    )
    assert outcome.status == "done"
    assert outcome.findings == []
    assert outcome.report == EMPTY_RESULT_REPORT
    assert judge.calls == []  # 空结果短路：不调裁判
    assert events[-1].status == "done"
    assert "text_delta" in [e.kind for e in events]  # 兜底报告仍走流式落库


@pytest.mark.asyncio
async def test_cancel_during_judge_keeps_partial_report():
    finder, investigator, advisor = scripted_providers()[:3]

    class PartialThenHang(Provider):
        name, model = "ph", "p1"

        async def stream(self, messages, tool_schemas, effort=None):
            yield ProviderTextDelta("## 对抗审查报告\n\n开头部分")
            await asyncio.sleep(30)

    events, emit = collector()
    task = asyncio.create_task(run_adversarial(
        finder=make_member(finder),
        investigator=make_member(investigator),
        advisor=make_member(advisor),
        judge=PartialThenHang(), judge_provider="fake", judge_model="fake-1",
        history=[], user_text="目标", timeout_s=30, emit=emit,
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    outcome = await task
    assert outcome.status == "cancelled"
    assert outcome.report == "## 对抗审查报告\n\n开头部分"
    # 已裁决的结论保留
    assert outcome.findings[0].verdict == "confirmed"
    assert any(e.kind == "text_delta" for e in events)
    assert events[-1].status == "cancelled"


# ---------- 协议级：WS 端到端 ----------


def make_adv_client(home, scripts):
    """scripts 顺序 = provider_factory 调用顺序：第 1 个是裁判（主模型），
    之后按 members 列表顺序是 发现者 → 调查者 → 建议者。"""
    pool = [p if isinstance(p, Provider) else FakeProvider(p) for p in scripts]
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool else FakeProvider([[TextBlock(text="备用")]]),
    )
    return TestClient(app)


def test_adversarial_send_end_to_end(home):
    scripts = [
        [[TextBlock(text=JUDGE_REPORT)]],       # 裁判（当前主模型）
        [[TextBlock(text=FINDER_JSON)]],        # 发现者
        [[TextBlock(text=INVESTIGATOR_JSON)]],  # 调查者
        [[TextBlock(text=ADVISOR_JSON)]],       # 建议者
    ]
    with make_adv_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "chat.send", "params": {
            "text": "审查这段代码：print(pop([]))",
            "adversarial": True,
            "members": [
                {"provider": "fa", "model": "ma"},
                {"provider": "fb", "model": "mb"},
                {"provider": "fc", "model": "mc"},
            ],
        }})
        events = []
        frame = recv_until(ws, "a1", events)
    assert frame["ok"] and frame["result"]["done"]

    kinds = [e["event"] for e in events if e["event"] != "task_estimate"]
    assert kinds[0] == "turn_started"
    assert "adversarial_started" in kinds
    assert kinds.count("adversarial_finding_proposed") == 3
    assert kinds.count("adversarial_verdict") == 3
    assert "text_delta" in kinds
    assert kinds[-1] == "queue_updated"

    started = next(e for e in events if e["event"] == "adversarial_started")
    roles = {r["role"]: r for r in started["data"]["roles"]}
    assert roles["judge"]["provider"] == "fake"
    assert roles["finder"]["provider"] == "fa"
    assert roles["investigator"]["provider"] == "fb"
    assert roles["advisor"]["provider"] == "fc"

    # 报告 = 本轮正式助手消息，带对抗元数据
    assistant = next(e for e in events if e["event"] == "assistant_message")
    msg = assistant["data"]["message"]
    assert msg["content"][0]["text"] == JUDGE_REPORT
    meta = msg["adversarial"]
    assert meta["mode"] == "adversarial"
    assert meta["status"] == "done"
    assert meta["stats"] == {"total": 3, "confirmed": 1, "refuted": 1, "partial": 1, "pending": 0}
    assert len(meta["findings"]) == 3
    assert meta["findings"][0]["verdict"] == "confirmed"
    assert frame["result"]["adversarial"] == meta

    # 持久化：user + assistant（带元数据）落库；按角色逐条入账 usage_log
    db = sqlite3.connect(home / "home" / "skysheep.db")
    rows = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["user", "assistant"]
    saved = json.loads(rows[1][1])
    assert saved["adversarial"]["mode"] == "adversarial"
    usage = db.execute(
        "SELECT provider, out_tokens FROM usage_log ORDER BY id"
    ).fetchall()
    assert len(usage) == 4
    assert usage[0][0] == "fa" and usage[3][0] == "fake"


def test_adversarial_settings_roundtrip(home):
    with make_adv_client(home, [[[TextBlock(text="x")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g1", "method": "adversarial.get", "params": {}})
        d = recv_until(ws, "g1")["result"]
        assert d["max_findings"] == 20 and d["role_timeout_s"] == 300

        ws.send_json({"id": "s1", "method": "adversarial.save", "params": {
            "max_findings": 7, "role_timeout_s": 60,
        }})
        d2 = recv_until(ws, "s1")["result"]
        assert d2["max_findings"] == 7 and d2["role_timeout_s"] == 60

        # 越界值按 clamp 落盘（与 roundtable.save 同姿态）
        ws.send_json({"id": "s2", "method": "adversarial.save", "params": {
            "max_findings": 9999, "role_timeout_s": 1,
        }})
        d3 = recv_until(ws, "s2")["result"]
        assert d3["max_findings"] == 100 and d3["role_timeout_s"] == 10


def test_adversarial_explicit_roles(home):
    """members 条目带 role 键：逐角色显式指定（同一模型可身兼数角），
    未指定的角色从自动序列按序补位。"""
    scripts = [
        [[TextBlock(text=JUDGE_REPORT)]],       # 裁判（当前主模型）
        [[TextBlock(text=FINDER_JSON)]],        # 显式 finder = fb
        [[TextBlock(text=INVESTIGATOR_JSON)]],  # 显式 investigator = fb（同模型兼任）
        [[TextBlock(text=ADVISOR_JSON)]],       # 显式 advisor = fa
    ]
    with make_adv_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a2", "method": "chat.send", "params": {
            "text": "审查这段代码",
            "adversarial": True,
            "members": [
                {"provider": "fb", "model": "mb", "role": "finder"},
                {"provider": "fb", "model": "mb", "role": "investigator"},
                {"provider": "fa", "model": "ma", "role": "advisor"},
            ],
        }})
        events = []
        frame = recv_until(ws, "a2", events)
    assert frame["ok"] and frame["result"]["done"]
    started = next(e for e in events if e["event"] == "adversarial_started")
    roles = {r["role"]: r for r in started["data"]["roles"]}
    assert roles["finder"]["provider"] == "fb"
    assert roles["investigator"]["provider"] == "fb"  # 同一模型身兼两角（不去重）
    assert roles["advisor"]["provider"] == "fa"
    # 三角色全显式：流水线照常走完
    assert next(e for e in events if e["event"] == "adversarial_finished")["data"]["status"] == "done"


def test_adversarial_partial_explicit_falls_back_in_order(home):
    """只显式指定部分角色时，其余角色从自动序列按序补位（游标递进，不跳号）。"""
    scripts = [
        [[TextBlock(text=JUDGE_REPORT)]],       # 裁判
        [[TextBlock(text=FINDER_JSON)]],        # 显式 finder = fa
        [[TextBlock(text=INVESTIGATOR_JSON)]],  # 自动 1 = fb → investigator
        [[TextBlock(text=ADVISOR_JSON)]],       # 自动 2 = fc → advisor
    ]
    with make_adv_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a3", "method": "chat.send", "params": {
            "text": "审查这段代码",
            "adversarial": True,
            "members": [
                {"provider": "fa", "model": "ma", "role": "finder"},
                {"provider": "fb", "model": "mb"},
                {"provider": "fc", "model": "mc"},
            ],
        }})
        events = []
        frame = recv_until(ws, "a3", events)
    assert frame["ok"] and frame["result"]["done"]
    started = next(e for e in events if e["event"] == "adversarial_started")
    roles = {r["role"]: r for r in started["data"]["roles"]}
    assert roles["finder"]["provider"] == "fa"      # 显式指定
    assert roles["investigator"]["provider"] == "fb"  # 自动按序补位（第一个）
    assert roles["advisor"]["provider"] == "fc"       # 自动按序补位（第二个）


def test_collab_modes_mutually_exclusive(home):
    """对抗与圆桌/团队同轮互斥：后端多参数同给报参数错。"""
    with make_adv_client(home, [[[TextBlock(text="x")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "chat.send", "params": {
            "text": "hi", "adversarial": True, "roundtable": True,
        }})
        r = recv_until(ws, "m1")
        assert not r["ok"]
        assert "不能同时开启" in r["error"]

        ws.send_json({"id": "m2", "method": "chat.send", "params": {
            "text": "hi", "adversarial": True, "team": True,
        }})
        r2 = recv_until(ws, "m2")
        assert not r2["ok"]
        assert "不能同时开启" in r2["error"]


def test_normal_send_has_no_adversarial_events(home):
    with make_adv_client(home, [[[TextBlock(text="普通回答")]]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "chat.send", "params": {"text": "hi"}})
        events = []
        frame = recv_until(ws, "n1", events)
    assert frame["ok"]
    assert not any(e["event"].startswith("adversarial_") for e in events)
