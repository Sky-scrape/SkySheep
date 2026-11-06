"""RunBudget / TeamCfgView 单测，以及圆桌·对抗两条链路的预算与提示词接入。

圆桌/对抗的接入点（每次 provider 调用前 spend、超限如实收尾、模板人设覆盖
默认系统提示词）在本文件用 FakeProvider 做端到端断言——它们是「默认行为
不变」之外新增的路径，必须有回归。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from skysheep.core.adversarial import (
    INVESTIGATOR_SYSTEM,
    PHASE_FINDER,
    PHASE_INVESTIGATOR,
    AdversarialOutcome,
    run_adversarial,
)
from skysheep.core.collab_budget import (
    BudgetExceeded,
    RunBudget,
    TeamCfgView,
)
from skysheep.core.roundtable import (
    DEBATE_SYSTEM,
    MEMBER_SYSTEM,
    MemberSpec,
    run_roundtable,
)
from skysheep.models.fake import FakeProvider


class FakeClock:
    """可控单调时钟：避免时长线测试真的 sleep。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _collect(_event) -> None:
    return None


# ---------------------------------------------------------------- RunBudget


def test_unlimited_budget_never_exceeds():
    budget = RunBudget()
    for _ in range(50):
        budget.spend()
    assert budget.calls == 50
    assert budget.reason() == ""
    assert budget.reason_label() == ""
    assert budget.exceeded() is False
    assert budget.remaining_calls is None


def test_call_limit_boundary_and_message():
    budget = RunBudget(max_calls=2)
    budget.spend()
    budget.spend()
    assert budget.exceeded() is False
    assert budget.remaining_calls == 0

    with pytest.raises(BudgetExceeded) as excinfo:
        budget.spend()
    assert excinfo.value.reason == "calls"
    assert str(excinfo.value) == (
        "已达模板调用次数上限（2 次），已停止后续调用并保留已产出内容"
    )
    assert budget.calls == 3


def test_take_call_is_alias_of_spend():
    budget = RunBudget(max_calls=1)
    budget.take_call()
    assert budget.calls == 1
    with pytest.raises(BudgetExceeded):
        budget.take_call()


def test_token_budget_uses_cumulative_max_not_incremental_sum():
    budget = RunBudget(max_tokens=100)
    budget.add_tokens(40)
    budget.add_tokens(30)  # 仍是累计值：不重复累加
    assert budget.tokens == 40

    budget.add_tokens(101)
    assert budget.reason() == "tokens"
    assert budget.reason_label() == (
        "已达模板 token 预算上限（100），已停止后续调用并保留已产出内容"
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        budget.raise_if_exceeded()
    assert excinfo.value.reason == "tokens"


def test_seconds_budget_uses_injected_clock():
    clock = FakeClock()
    budget = RunBudget(max_seconds=10, clock=clock)
    budget.spend()  # t=0，未超
    clock.now = 10.1
    assert budget.seconds_used == pytest.approx(10.1)
    assert budget.reason() == "seconds"
    assert budget.reason_label() == (
        "已达模板运行时长上限（10 秒），已停止后续调用并保留已产出内容"
    )
    with pytest.raises(BudgetExceeded):
        budget.spend()


def test_raise_if_exceeded_is_noop_under_limit():
    budget = RunBudget(max_calls=5, max_tokens=100, max_seconds=10)
    budget.spend()
    budget.add_tokens(20)
    budget.raise_if_exceeded()  # 不抛即通过


def test_remaining_calls_and_zero_limits():
    assert RunBudget().remaining_calls is None
    budget = RunBudget(max_calls=3)
    assert budget.remaining_calls == 3
    budget.spend()
    assert budget.remaining_calls == 2


def test_real_clock_monotonic_is_used_by_default():
    budget = RunBudget(max_seconds=3600)
    assert budget.seconds_used >= 0.0
    assert budget.seconds_used < 5.0  # 不做 sleep 也应远小于上限
    assert isinstance(time.monotonic(), float)


# ---------------------------------------------------------------- TeamCfgView


def test_team_cfg_view_maps_budget_to_max_rounds():
    base = SimpleNamespace(
        max_members=4, member_timeout_s=120, max_rounds=40,
        redo_limit=1, stall_limit=2, cron_resume_limit=1,
    )
    view = TeamCfgView(base, RunBudget(max_calls=20))
    assert view.max_members == 4
    assert view.member_timeout_s == 120
    assert view.max_rounds == 20
    assert view.redo_limit == 1 and view.stall_limit == 2

    # budget=0 → 沿用全局 max_rounds
    assert TeamCfgView(base, RunBudget()).max_rounds == 40


def test_team_cfg_view_keeps_explicit_zero_and_ignores_limits():
    base = SimpleNamespace(
        max_members=0, member_timeout_s=0, max_rounds=0,
        redo_limit=0, stall_limit=0, cron_resume_limit=0,
        limits=SimpleNamespace(max_members=99, debate_rounds=9),
    )
    view = TeamCfgView(base, RunBudget(max_calls=7))
    assert view.max_members == 0
    assert view.member_timeout_s == 0
    assert view.max_rounds == 7  # 全局 0 + 预算 7 → 取正数里较小者
    assert view.redo_limit == 0 and view.stall_limit == 0
    # 刻意不读 limits
    assert view.max_members != 99


# ---------------------------------------------------------------- 圆桌接入


def _member(provider: FakeProvider, **overrides) -> MemberSpec:
    data = {"provider_name": provider.name, "model": provider.model, "provider": provider}
    data.update(overrides)
    return MemberSpec(**data)


@pytest.mark.asyncio
async def test_roundtable_template_prompt_overrides_member_system():
    member = FakeProvider(["成员草稿"])
    chair = FakeProvider(["融合答案"])
    events = []

    async def emit(event):
        events.append(event)

    outcome = await run_roundtable(
        members=[_member(member, name="甲")],
        chair=chair,
        system_text="主系统提示词",
        history=[],
        user_text="问题",
        timeout_s=10,
        emit=emit,
        prompts={0: "模板人设：只挑错"},
    )

    assert outcome.status == "done"
    assert outcome.members[0].text == "成员草稿"
    assert outcome.fused_text == "融合答案"
    system = member.calls[0][0].text
    assert system == "模板人设：只挑错"
    assert MEMBER_SYSTEM not in system
    # 融合主席仍是原 system_text（模板人设只作用于成员）
    assert chair.calls[0][0].text == "主系统提示词"
    # 事件里的成员带展示名
    started = next(e for e in events if e.kind == "roundtable_started")
    assert started.members[0]["name"] == "甲"


@pytest.mark.asyncio
async def test_roundtable_member_spec_prompt_and_alias():
    direct = FakeProvider(["一"])
    alias = FakeProvider(["二"])
    chair = FakeProvider(["融合"])

    out1 = await run_roundtable(
        members=[_member(direct, prompt="字段人设")],
        chair=chair, system_text="系统", history=[], user_text="问题",
        timeout_s=10, emit=_collect,
    )
    assert out1.status == "done"
    assert direct.calls[0][0].text == "字段人设"

    out2 = await run_roundtable(
        members=[_member(alias, system_prompt="别名字段人设")],
        chair=FakeProvider(["融合二"]), system_text="系统", history=[],
        user_text="问题", timeout_s=10, emit=_collect,
    )
    assert out2.status == "done"
    assert alias.calls[0][0].text == "别名字段人设"


@pytest.mark.asyncio
async def test_roundtable_debate_round_keeps_template_prompt_then_debate_system():
    member = FakeProvider(["第一轮草稿", "第二轮修订"])
    chair = FakeProvider(["融合"])

    outcome = await run_roundtable(
        members=[_member(member, prompt="模板人设")],
        chair=chair, system_text="系统", history=[], user_text="问题",
        timeout_s=10, emit=_collect, debate_rounds=1,
    )

    assert outcome.status == "done"
    assert member.calls[0][0].text == "模板人设"
    assert member.calls[1][0].text == f"模板人设\n\n{DEBATE_SYSTEM}"


@pytest.mark.asyncio
async def test_roundtable_budget_stops_before_fusion_and_keeps_drafts():
    member = FakeProvider(["可保留的草稿"])
    chair = FakeProvider(["不该出现"])

    outcome = await run_roundtable(
        members=[_member(member)],
        chair=chair, system_text="系统", history=[], user_text="问题",
        timeout_s=10, emit=_collect, budget=RunBudget(max_calls=1),
    )

    # 成员那次调用用掉了唯一额度；融合前超限 → 草稿保留、融合不发起
    assert outcome.status == "error"
    assert "调用次数上限" in outcome.error
    assert outcome.members[0].status == "done"
    assert outcome.members[0].text == "可保留的草稿"
    assert chair.calls == []


@pytest.mark.asyncio
async def test_roundtable_budget_exhausted_after_round_aborts_debate():
    first = FakeProvider(["甲草稿"])
    second = FakeProvider(["乙草稿"])
    chair = FakeProvider(["不该出现"])

    outcome = await run_roundtable(
        members=[_member(first), _member(second)],
        chair=chair, system_text="系统", history=[], user_text="问题",
        timeout_s=10, emit=_collect, debate_rounds=1,
        budget=RunBudget(max_calls=1),
    )

    assert outcome.status == "error"
    assert "调用次数上限" in outcome.error
    assert chair.calls == []  # 不融合
    assert len(outcome.members) == 2  # 已产出的成员结果照常保留


# ---------------------------------------------------------------- 对抗接入

FINDER_JSON = (
    '{"findings": [{"category": "bug", "severity": "high", '
    '"location": "a.py:1", "description": "空列表越界", "evidence": "未判空"}]}'
)
VERDICTS_JSON = (
    '{"verdicts": [{"id": "F-001", "verdict": "refuted", "reason": "外层已判空"}]}'
)


def _adversarial_kwargs(**overrides):
    finder = FakeProvider([FINDER_JSON])
    investigator = FakeProvider([VERDICTS_JSON])
    judge = FakeProvider(["终审报告"])
    data = {
        "finder": _member(finder),
        "investigator": _member(investigator),
        "advisor": None,
        "judge": judge,
        "judge_provider": judge.name,
        "judge_model": judge.model,
        "history": [],
        "user_text": "被审代码",
        "timeout_s": 10,
        "emit": _collect,
    }
    data.update(overrides)
    return data, finder, investigator, judge


@pytest.mark.asyncio
async def test_adversarial_prompts_override_and_blank_falls_back():
    kwargs, finder, investigator, _judge = _adversarial_kwargs(
        prompts={
            PHASE_FINDER: "自定义发现者",
            PHASE_INVESTIGATOR: "",  # 空串必须回落默认
        },
    )
    outcome: AdversarialOutcome = await run_adversarial(**kwargs)

    assert outcome.status == "done"
    assert finder.calls[0][0].text == "自定义发现者"
    assert investigator.calls[0][0].text == INVESTIGATOR_SYSTEM


@pytest.mark.asyncio
async def test_adversarial_budget_exhausted_keeps_findings_and_stops_phases():
    kwargs, finder, investigator, judge = _adversarial_kwargs(
        budget=RunBudget(max_calls=1),
    )
    events = []

    async def emit(event):
        events.append(event)

    kwargs["emit"] = emit
    outcome = await run_adversarial(**kwargs)

    assert outcome.status == "error"
    assert "调用次数上限" in outcome.error
    assert outcome.findings, "发现阶段已完成，产出必须保留"
    assert investigator.calls == []  # 后续阶段不再发起
    assert judge.calls == []
    finished = [e for e in events if e.kind == "adversarial_finished"]
    assert len(finished) == 1
