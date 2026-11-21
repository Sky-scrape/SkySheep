"""对抗迁移到统一成员轮运行时（core/member_runtime.py）的回归钉。

_run_text_turn 已是 member_runtime.run_member_text 的薄壳（与圆桌成员/
融合同源，签名与五元组返回对外零感知）；这里钉迁移最容易被改坏的三类
行为（design-member-loop.md §3.2 风险与回归重点）：

- BudgetExceeded 必须原样上抛（policy.budget_policy="propagate"）——
  误设 turn_error 会把预算耗尽吞成本阶段失败、继续烧下一阶段（原
  adversarial.py 预算分支注释明说的原始缺陷）；
- 裁判 TextDelta 经 on_delta 转发；取消就地收敛（absorb_cancel=True），
  已流出的部分报告文本随五元组返回（与圆桌融合取消语义同源）；
- 用量每次尝试清零重计（reset_usage_per_attempt=True）：跨重试只留最后
  一次尝试的量；_call_json 的原答 + 纠错两次调用合计 = 各自清零后相加。

溯源（薄壳内部 is run_member_text + 策略字段）由 test_collab_shared_helpers
钉；预算耗尽在发现阶段拦下整场的既有集成行为由 test_collab_budget.py 钉
（该用例耗尽点在发现阶段，propagate / turn_error 都能让它过）——这里补
「调查阶段才耗尽」的判别变体：裁判阶段不得再进入，错误必须是预算原文
（误设 turn_error 时会发裁判阶段事件、错误被「裁判阶段失败」前缀顶掉）。
"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.core.adversarial import _call_json, _run_text_turn, run_adversarial
from skysheep.core.collab_budget import BudgetExceeded, RunBudget
from skysheep.core.roundtable import MemberSpec
from skysheep.events import NoticeEvent, TextDelta
from skysheep.messages import Message
from skysheep.models.base import Provider, ProviderDone, ProviderTextDelta
from skysheep.models.fake import FakeProvider


def collector():
    events: list = []

    async def emit(ev) -> None:
        events.append(ev)

    return events, emit


class ScriptProvider(Provider):
    """按脚本逐项产出（跨调用一次性消费：重试的第 2 次调用从上次中断处
    继续，不重放）：str = 文本增量、ProviderDone = 用量上报、Exception =
    抛出、None = 挂起不产出（超时/取消用）。脚本走完后正常收尾并上报一次
    固定用量（13/9/5）；异常/挂起项之后的产出自然到不了。"""

    name, model = "script", "s1"

    def __init__(self, script) -> None:
        super().__init__()
        self._script = list(script)
        self.calls = 0

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        while self._script:
            item = self._script.pop(0)
            if isinstance(item, str):
                yield ProviderTextDelta(item)
            elif isinstance(item, ProviderDone):
                yield item
            elif isinstance(item, Exception):
                raise item
            else:  # None = 挂起
                await asyncio.sleep(30)
        yield ProviderDone(
            stop_reason="end_turn", input_tokens=13, output_tokens=9, cached_tokens=5,
        )


# ---------- BudgetExceeded 透传（budget_policy="propagate"） ----------


@pytest.mark.asyncio
async def test_run_text_turn_propagates_budget_exhaustion():
    """预算耗尽必须从 _run_text_turn 原样上抛：吞成五元组 error 会继续烧
    下一阶段（对抗原始缺陷）；spend() 在最内层真实调用前拦下。"""
    budget = RunBudget(max_calls=1)
    budget.spend()  # 预先用掉唯一一次：下一次 spend() 必抛
    provider = ScriptProvider(["x"])
    events, emit = collector()

    with pytest.raises(BudgetExceeded):
        await _run_text_turn(
            provider, [Message.user("被审内容")], 30, emit,
            label="调查员", budget=budget,
        )

    assert provider.calls == 0  # spend() 在 provider.stream 之前拦下
    assert not any(isinstance(e, NoticeEvent) for e in events)  # 不发重试通知


@pytest.mark.asyncio
async def test_budget_exhausted_at_investigator_stops_pipeline_before_judge():
    """调查阶段才耗尽预算的判别变体：外层统一收尾，裁判阶段不得再进入。

    误设 budget_policy="turn_error" 时：预算错误被吞成调查阶段失败 →
    流水线继续走进裁判阶段（发 adversarial_phase(judge) 事件）、裁判的
    spend 再次抛错被吞 → outcome.error 被「裁判阶段失败：」前缀顶掉。
    现状（propagate）：错误就是预算原文，judge 一次都不调，已完成阶段的
    发现清单照常保留。
    """
    finder_json = (
        '{"findings": [{"category": "bug", "severity": "high", '
        '"location": "a.py:1", "description": "空列表越界", "evidence": "未判空"}]}'
    )
    finder = FakeProvider([finder_json])
    investigator = FakeProvider(['{"verdicts": []}'])
    judge = FakeProvider(["不应被调用"])
    events, emit = collector()

    outcome = await run_adversarial(
        finder=MemberSpec(provider_name="fake", model="fake-1", provider=finder),
        investigator=MemberSpec(
            provider_name="fake", model="fake-1", provider=investigator,
        ),
        advisor=None,
        judge=judge, judge_provider="fake", judge_model="fake-1",
        history=[], user_text="被审代码", timeout_s=30, emit=emit,
        budget=RunBudget(max_calls=1),  # 发现阶段用掉唯一一次
    )

    assert outcome.status == "error"
    assert outcome.error == "已达模板调用次数上限（1 次），已停止后续调用并保留已产出内容"
    assert len(outcome.findings) == 1  # 发现阶段的产出照常保留
    assert investigator.calls == [] and judge.calls == []  # spend 先于真实调用
    # 裁判阶段没有进入：误设 turn_error 时这里会发出 adversarial_phase(judge)
    assert not any(
        e.kind == "adversarial_phase" and e.phase == "judge" for e in events
    )
    finished = events[-1]
    assert finished.status == "error" and finished.summary == outcome.error


# ---------- 裁判 TextDelta 转发与取消（absorb_cancel=True） ----------


@pytest.mark.asyncio
async def test_judge_stream_out_forwards_text_deltas_and_returns_tuple():
    """stream_out=True：文本增量逐段作为 TextDelta 转发（先入桶后回调），
    五元组返回形状不变。"""
    provider = ScriptProvider(["报", "告"])
    events, emit = collector()

    text, error, i, o, c = await _run_text_turn(
        provider, [Message.user("被审内容")], 30, emit, label="裁判", stream_out=True,
    )

    assert (text, error) == ("报告", "")
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.text for d in deltas] == ["报", "告"]
    assert (i, o, c) == (13, 9, 5)


@pytest.mark.asyncio
async def test_judge_stream_out_cancel_absorbed_keeps_partial_report_text():
    """裁判流式中取消：就地收敛（uncancel 由运行时承担），error="cancelled"，
    已流出的部分报告文本照常返回（现状语义：emit 在 consume 内，取消经
    wait_for 抛 CancelledError 由循环外层吸收），取消前已转发的增量不受影响。"""
    provider = ScriptProvider(["## 对抗审查报告\n\n开头部分", None])
    events, emit = collector()

    async def scenario():
        return await _run_text_turn(
            provider, [Message.user("被审内容")], 30, emit,
            label="裁判", stream_out=True,
        )

    task = asyncio.create_task(scenario())
    await asyncio.sleep(0.1)  # 等第一段流出、进入挂起
    task.cancel()
    text, error, *_ = await task  # 取消被就地吸收，不再上抛

    assert error == "cancelled"
    assert text == "## 对抗审查报告\n\n开头部分"
    assert any(isinstance(e, TextDelta) for e in events)
    assert provider.calls == 1  # 不重试


# ---------- 用量口径（reset_usage_per_attempt=True） ----------


@pytest.mark.asyncio
async def test_run_text_turn_usage_resets_per_attempt():
    """「ProviderDone 后又抛瞬态错」的病态服务才暴露口径差异：对抗每次
    尝试清零重计，只留最后一次（与圆桌成员跨尝试累计口径相反）。"""
    provider = ScriptProvider([
        ProviderDone(stop_reason="end_turn", input_tokens=11, output_tokens=7, cached_tokens=3),
        RuntimeError("HTTP 429 rate limit exceeded"),
        "恢复",
    ])
    events, emit = collector()

    text, error, i, o, c = await _run_text_turn(
        provider, [Message.user("被审内容")], 30, emit, label="调查员",
    )

    assert provider.calls == 2  # 瞬态错误重试了一次
    assert (text, error) == ("恢复", "")
    assert (i, o, c) == (13, 9, 5)  # 不是两次尝试相加的 24/16/8


@pytest.mark.asyncio
async def test_call_json_accumulates_usage_across_fix_retry():
    """_call_json 的原答 + 纠错两次调用：各自清零重计后合计口径不变。

    用 FakeProvider（每次 stream() 弹一组脚本）：第一组是非 JSON 的散话，
    触发一次格式纠错重试；FakeProvider 每次调用上报 (11, 7, 0)。
    """
    junk = "我觉得这段代码问题很多，值得仔细审查。"
    provider = FakeProvider([junk, '{"findings": []}'])
    events, emit = collector()

    data, error, i, o, c = await _call_json(
        provider, [Message.user("被审内容")], 30, emit,
        label="发现者", expect_key="findings",
    )

    assert error == "" and data == {"findings": []}
    assert len(provider.calls) == 2  # 原答一次 + 格式纠错一次
    # 两次调用各 (11, 7, 0)，合计口径 = 相加
    assert (i, o, c) == (22, 14, 0)
