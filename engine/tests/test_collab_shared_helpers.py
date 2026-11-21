"""协作共享小件（core/collab_common.py）契约测试：重试通知行为 + 单一实现。

notice_retry 此前在圆桌与对抗两处逐字级重复（仅「圆桌成员/对抗」称谓差两字），
抽为 collab_common 共享函数后用行为断言锁定：
- 通知文案按 subject 称谓拼装、错误截 120 字符、退避自 base_delay_s 指数翻倍
  封顶 10 秒、次数以 MAX_STREAM_RETRIES 计；
- 圆桌/对抗的真实重试路径确实走同一份实现（is 溯源 + monkeypatch 计数），
  模块里不许再长出私有副本；成员循环本体三处（圆桌成员/圆桌融合/对抗
  文本轮）迁到共享运行时 member_runtime（run_member_text）后，溯源同步
  钉到它（对抗差异以 MemberTurnPolicy 字段显式表达）；
- MemberSpec / clip_draft / EmitFn 同样单源（圆桌、对抗、团队取到同一对象）；
- TeamOrchestrator 显式声明 collab_budget 字段（RunBudget | None，默认 None），
  不再是 backend 建队时运行时贴上去的动态属性。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_type_hints

import pytest

import skysheep.core.adversarial as adv
import skysheep.core.member_runtime as mr
import skysheep.core.roundtable as rt
import skysheep.core.team as team_mod
from skysheep.core import collab_common as cc
from skysheep.core.adversarial import _run_text_turn
from skysheep.core.collab_budget import RunBudget
from skysheep.core.roundtable import MemberSpec, run_roundtable
from skysheep.core.team import TeamOrchestrator
from skysheep.events import NoticeEvent
from skysheep.messages import Message, TextBlock
from skysheep.models.base import Provider, ProviderTextDelta
from skysheep.models.fake import FakeProvider

_RETRY_LABEL_MAX = 120  # 通知文案里错误串的截断长度（collab_common 的契约）


# ---------------------------------------------------------------- 共享基建

def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


class FlakyProvider(Provider):
    """第一次调用抛瞬态错误（429），第二次正常作答（与 test_roundtable 同款）。"""

    name, model = "flaky", "f1"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 429 rate limit exceeded")
        yield ProviderTextDelta("恢复后作答")


async def _flaky_roundtable(flaky: FlakyProvider, emit) -> None:
    await run_roundtable(
        members=[MemberSpec(provider_name="flaky", model="f1", provider=flaky)],
        chair=FakeProvider([[TextBlock(text="融合")]]),
        system_text="",
        history=[Message.user("问题")],
        user_text="问题",
        timeout_s=30,
        emit=emit,
    )


# ---------------------------------------------------------------- 通知行为

@pytest.mark.asyncio
async def test_notice_retry_text_and_backoff(monkeypatch):
    """共享函数本体：称谓参数化、错误截 120 字符、指数退避封顶 10 秒。"""
    events, emit = collector()
    sleeps: list[float] = []

    async def fake_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    monkeypatch.setattr(
        cc, "asyncio", SimpleNamespace(sleep=fake_sleep),
    )  # 只替换 collab_common 命名空间里的 asyncio 绑定，不碰全局 asyncio

    await cc.notice_retry(emit, "gpt/o3", "e" * 300, 1, subject="圆桌成员")
    await cc.notice_retry(emit, "调查员", "boom", 2, subject="对抗")
    await cc.notice_retry(emit, "融合", "x", 2, subject="圆桌成员", base_delay_s=6.0)

    assert sleeps == [1.5, 3.0, 10.0]  # 1.5 起步 → 1.5*2=3 → 6*2=12 封顶 10
    msgs = [e.message for e in events if isinstance(e, NoticeEvent)]
    assert msgs[0] == (
        "圆桌成员 gpt/o3 调用失败（" + "e" * _RETRY_LABEL_MAX
        + "），2 秒后自动重试（第 1/3 次）…"
    )
    assert msgs[1] == "对抗 调查员 调用失败（boom），3 秒后自动重试（第 2/3 次）…"
    assert msgs[2] == "圆桌成员 融合 调用失败（x），10 秒后自动重试（第 2/3 次）…"


@pytest.mark.asyncio
async def test_roundtable_member_retry_notice(monkeypatch):
    """圆桌成员瞬态失败经真实 run_roundtable 路径：文案与退避节奏逐字保持。"""
    monkeypatch.setattr("skysheep.core.roundtable.RETRY_BASE_DELAY_S", 0.0)
    flaky = FlakyProvider()
    events, emit = collector()

    await _flaky_roundtable(flaky, emit)

    assert flaky.calls == 2  # 重试确实发生（同一 provider 第二次成功）
    notices = [e.message for e in events if isinstance(e, NoticeEvent)]
    assert (
        "圆桌成员 flaky/f1 调用失败（HTTP 429 rate limit exceeded），"
        "0 秒后自动重试（第 1/3 次）…"
    ) in notices


@pytest.mark.asyncio
async def test_adversarial_turn_retry_notice(monkeypatch):
    """对抗阶段瞬态失败经真实 _run_text_turn 路径：文案与退避节奏逐字保持。"""
    monkeypatch.setattr("skysheep.core.adversarial.RETRY_BASE_DELAY_S", 0.0)
    flaky = FlakyProvider()
    events, emit = collector()

    text, error, *_ = await _run_text_turn(
        flaky, [Message.user("被审内容")], 30, emit, label="调查员",
    )

    assert flaky.calls == 2
    assert text == "恢复后作答" and error == ""
    notices = [e.message for e in events if isinstance(e, NoticeEvent)]
    assert (
        "对抗 调查员 调用失败（HTTP 429 rate limit exceeded），"
        "0 秒后自动重试（第 1/3 次）…"
    ) in notices


# ---------------------------------------------------------------- 单一实现

@pytest.mark.asyncio
async def test_roundtable_retry_dispatches_shared_impl(monkeypatch):
    """替换共享名后圆桌真实重试路径必须经它——模块若私藏副本则计数为 0。"""
    calls: list[dict] = []

    async def spy(emit, label, error, attempt, **kw):
        calls.append({"label": label, "error": error, "attempt": attempt, **kw})

    monkeypatch.setattr(rt, "notice_retry", spy)
    flaky = FlakyProvider()
    events, emit = collector()

    await _flaky_roundtable(flaky, emit)

    assert flaky.calls == 2
    assert len(calls) == 1
    assert calls[0]["label"] == "flaky/f1"
    assert calls[0]["attempt"] == 1
    assert calls[0]["subject"] == "圆桌成员"
    assert "HTTP 429" in calls[0]["error"]


@pytest.mark.asyncio
async def test_adversarial_retry_dispatches_shared_impl(monkeypatch):
    """替换共享名后对抗真实重试路径必须经它——模块若私藏副本则计数为 0。

    成员循环本体迁到 member_runtime 后，溯源同步钉过去：_run_text_turn 内部
    调的就是 member_runtime.run_member_text 的同一对象，且对抗与圆桌的差异
    以 MemberTurnPolicy 字段显式表达（文案前缀用阶段名、用量清零重计、
    BudgetExceeded 上抛、取消就地收敛）。重试节奏常量按本模块命名空间传入，
    monkeypatch adv.RETRY_BASE_DELAY_S / adv.notice_retry 的锚点保持生效。
    """
    calls: list[dict] = []

    async def spy(emit, label, error, attempt, **kw):
        calls.append({"label": label, "error": error, "attempt": attempt, **kw})

    monkeypatch.setattr(adv, "notice_retry", spy)

    real = mr.run_member_text
    turns: list[mr.MemberTurnPolicy] = []

    async def runtime_spy(provider, messages, **kw):
        turns.append(kw["policy"])
        return await real(provider, messages, **kw)

    monkeypatch.setattr(adv, "run_member_text", runtime_spy)
    flaky = FlakyProvider()
    events, emit = collector()

    await _run_text_turn(flaky, [Message.user("被审内容")], 30, emit, label="调查员")

    assert flaky.calls == 2  # 委托给真实运行时：重试确实发生
    assert len(turns) == 1
    policy = turns[0]
    assert policy.retry_label == "调查员" and policy.timeout_label == "调查员"
    assert policy.retry_subject == "对抗"
    assert policy.absorb_cancel is True
    assert policy.reset_usage_per_attempt is True
    assert policy.budget_policy == "propagate"
    assert policy.retries == adv.MAX_STREAM_RETRIES
    assert policy.base_delay_s == adv.RETRY_BASE_DELAY_S
    assert len(calls) == 1
    assert calls[0]["label"] == "调查员"
    assert calls[0]["attempt"] == 1
    assert calls[0]["subject"] == "对抗"
    assert "HTTP 429" in calls[0]["error"]


def test_single_implementation_identity():
    """圆桌、对抗、团队引用的都是 collab_common 的同一对象；私有副本不复存在。

    成员循环本体三处（圆桌成员/圆桌融合/对抗文本轮）同样单源：_run_text_turn
    （薄壳再导出）与 _run_fusion（薄壳）内部都是 member_runtime.run_member_text
    ——模块绑定 is 溯源，行为经上面的 monkeypatch 计数断言钉。
    """
    assert rt.notice_retry is cc.notice_retry
    assert adv.notice_retry is cc.notice_retry
    assert cc.notice_retry.__module__ == "skysheep.core.collab_common"  # import 溯源
    assert not hasattr(rt, "_notice_retry")
    assert not hasattr(adv, "_notice_retry")

    assert rt.clip_draft is cc.clip_draft
    assert adv.clip_draft is cc.clip_draft
    assert rt.MemberSpec is cc.MemberSpec
    assert adv.MemberSpec is cc.MemberSpec
    assert team_mod.MemberSpec is cc.MemberSpec
    assert rt.EmitFn is cc.EmitFn
    assert adv.EmitFn is cc.EmitFn

    # 成员循环本体三条路径同源：薄壳引用的就是运行时的同一对象
    assert rt.run_member_text is mr.run_member_text
    assert adv.run_member_text is mr.run_member_text
    assert adv.MemberTurnPolicy is mr.MemberTurnPolicy


def test_clip_draft_marker_single_source():
    """移动后的截断小件行为不变：上限 12000 字符 + 同一枚省略标记。"""
    long = "字" * (cc.DRAFT_CHAR_LIMIT + 5)
    clipped = cc.clip_draft(long)
    assert clipped[: cc.DRAFT_CHAR_LIMIT] == "字" * cc.DRAFT_CHAR_LIMIT
    assert clipped.endswith(cc.DRAFT_CLIP_MARK)
    assert rt.clip_draft(long) == clipped  # 圆桌引用同一实现，行为逐字一致


# ---------------------------------------------------------------- 团队字段

def test_team_orchestrator_declares_collab_budget():
    """collab_budget 是 TeamOrchestrator 显式声明的字段（RunBudget | None，默认 None）。"""
    hints = get_type_hints(TeamOrchestrator)
    assert hints["collab_budget"] == RunBudget | None
    assert TeamOrchestrator.collab_budget is None

    async def _emit(ev) -> None:
        return None

    orch = TeamOrchestrator(emit=_emit)
    assert orch.collab_budget is None  # 未带模板的队伍默认无预算
    orch.collab_budget = RunBudget(max_calls=2)  # backend 建队后的挂载写法
    assert orch.collab_budget.max_calls == 2
