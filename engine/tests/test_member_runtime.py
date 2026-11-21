"""成员轮运行时（core/member_runtime.py）行为契约测试。

run_member_text 收敛了「无工具纯文本模型调用」的流式收集 + 用量 + 超时 +
瞬态重试 + 取消协议；这里用 provider 桩逐条钉行为（真行为断言，不用源码
文本断言）：
- 流式收集：消息/空工具表原样透传、增量先入桶后经 on_delta 转发、
  on_delta=None 时静默收集；
- 超时截断：文案带 timeout_label；未吐内容可重试、吐过不重试、部分文本保留；
- 重试条件：瞬态错误且未吐内容才重试；非瞬态/已吐内容一律如实失败；
- BudgetExceeded 双策略：propagate 原样上抛（在真实调用前拦下）/
  turn_error 落本轮 error 且不当瞬态错误重试；
- 取消双协议：absorb 就地收敛（error="cancelled" + 部分文本 + uncancel），
  穿透上抛且共享桶保留部分草稿（圆桌回收协议）；
- 用量双口径：默认跨重试尝试累计，reset_usage_per_attempt=True 每次清零；
- 信号量消费：caller 持有的并发闸真的在闸流式调用；
- 单一实现溯源：圆桌成员与圆桌融合路径真的经 run_member_text
  （monkeypatch 计数 + is）。
"""

from __future__ import annotations

import asyncio

import pytest

import skysheep.core.roundtable as rt
from skysheep.core import member_runtime as mr
from skysheep.core.collab_budget import BudgetExceeded, RunBudget
from skysheep.core.member_runtime import MemberTurnPolicy, run_member_text
from skysheep.events import NoticeEvent
from skysheep.messages import Message, TextBlock
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
    固定用量；异常/挂起项之后的产出自然到不了。"""

    name, model = "script", "s1"

    def __init__(self, script) -> None:
        super().__init__()
        self._script = list(script)
        self.calls = 0
        self.stream_args: list[tuple] = []

    async def stream(self, messages, tool_schemas, effort=None):
        self.calls += 1
        self.stream_args.append((messages, tool_schemas, effort))
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


# ---------- 流式收集 ----------


@pytest.mark.asyncio
async def test_stream_collection_forwards_deltas_and_passthrough():
    events, emit = collector()
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    provider = ScriptProvider(["你", "好"])
    messages = [Message.user("问题")]
    turn = await run_member_text(
        provider, messages, timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(), on_delta=on_delta,
    )

    assert turn.error == "" and turn.text == "你好"
    assert deltas == ["你", "好"]  # 逐段回调（先入桶后转发）
    # 纯文本协作约定：消息原样透传、工具表恒为空
    assert provider.stream_args[0][0] is messages
    assert provider.stream_args[0][1] == []
    assert provider.stream_args[0][2] is None  # 桩未声明思考强度 → 不覆盖档位
    assert not any(isinstance(e, NoticeEvent) for e in events)


@pytest.mark.asyncio
async def test_silent_collection_without_on_delta_still_gathers_text_usage():
    events, emit = collector()
    provider = ScriptProvider(["静默", "收集"])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(),
    )

    assert turn.text == "静默收集" and turn.error == ""
    assert (turn.input_tokens, turn.output_tokens, turn.cached_tokens) == (13, 9, 5)


# ---------- 超时截断 ----------


@pytest.mark.asyncio
async def test_timeout_retries_while_empty_and_labels_message():
    events, emit = collector()
    # 每次尝试一段挂起（脚本跨调用一次性消费）：3 次尝试全部超时
    provider = ScriptProvider([None, None, None])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=0.05, emit=emit,
        policy=MemberTurnPolicy(
            retries=2, base_delay_s=0.0,
            retry_subject="圆桌成员", retry_label="script/s1",
            timeout_label="作答",
        ),
    )

    assert provider.calls == 3  # 1 次尝试 + 2 次重试（未吐内容可重试）
    assert turn.text == ""
    assert turn.error == "作答超时（>0.05 秒）"
    notices = [e for e in events if isinstance(e, NoticeEvent)]
    assert len(notices) == 2
    assert notices[0].message == (
        "圆桌成员 script/s1 调用失败（作答超时（>0.05 秒）），"
        "0 秒后自动重试（第 1/3 次）…"
    )


@pytest.mark.asyncio
async def test_timeout_after_content_keeps_partial_text_without_retry():
    events, emit = collector()
    provider = ScriptProvider(["部分", None])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=0.05, emit=emit,
        policy=MemberTurnPolicy(retries=3, base_delay_s=0.0, timeout_label="作答"),
    )

    assert provider.calls == 1  # 已吐内容：不重试
    assert turn.text == "部分"
    assert "作答超时" in turn.error
    assert not any(isinstance(e, NoticeEvent) for e in events)


# ---------- 重试条件：只在未吐内容时 ----------


@pytest.mark.asyncio
async def test_transient_error_retries_then_succeeds():
    events, emit = collector()
    provider = ScriptProvider([
        RuntimeError("HTTP 429 rate limit exceeded"), "恢复后作答",
    ])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(
            retries=3, base_delay_s=0.0,
            retry_subject="圆桌成员", retry_label="script/s1",
        ),
    )

    assert provider.calls == 2
    assert turn.error == "" and turn.text == "恢复后作答"
    notices = [e for e in events if isinstance(e, NoticeEvent)]
    assert len(notices) == 1
    assert "圆桌成员 script/s1" in notices[0].message
    assert "HTTP 429 rate limit exceeded" in notices[0].message


@pytest.mark.asyncio
async def test_non_transient_error_fails_without_retry():
    events, emit = collector()
    provider = ScriptProvider([RuntimeError("fatal boom")])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(retries=3, base_delay_s=0.0),
    )

    assert provider.calls == 1
    assert turn.text == "" and turn.error == "fatal boom"
    assert not any(isinstance(e, NoticeEvent) for e in events)


@pytest.mark.asyncio
async def test_error_after_content_keeps_partial_text_without_retry():
    events, emit = collector()
    provider = ScriptProvider(["部分", RuntimeError("HTTP 429 rate limit exceeded")])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(retries=3, base_delay_s=0.0),
    )

    # 重放会导致卡片文本重复：吐过内容就不重试，部分文本如实保留
    assert provider.calls == 1
    assert turn.text == "部分"
    assert "429" in turn.error
    assert not any(isinstance(e, NoticeEvent) for e in events)


# ---------- BudgetExceeded 双策略 ----------


@pytest.mark.asyncio
async def test_budget_exceeded_propagates_when_policy_says_so():
    events, emit = collector()
    budget = RunBudget(max_calls=1)
    budget.spend()  # 预先用掉唯一一次：下一轮 spend() 必抛
    provider = ScriptProvider(["x"])

    with pytest.raises(BudgetExceeded):
        await run_member_text(
            provider, [Message.user("q")], timeout_s=30, emit=emit,
            policy=MemberTurnPolicy(budget_policy="propagate"),
            budget=budget,
        )

    assert provider.calls == 0  # spend() 在最内层真实调用前拦下
    assert not any(isinstance(e, NoticeEvent) for e in events)


@pytest.mark.asyncio
async def test_budget_exceeded_lands_as_turn_error_by_default():
    events, emit = collector()
    budget = RunBudget(max_calls=1)
    budget.spend()
    provider = ScriptProvider(["x"])

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(),  # 默认 turn_error（圆桌口径）
        budget=budget,
    )

    assert provider.calls == 0
    assert turn.text == ""
    assert turn.error == "已达模板调用次数上限（1 次），已停止后续调用并保留已产出内容"
    # 预算文案不是瞬态错误：不重试、不发重试通知
    assert not any(isinstance(e, NoticeEvent) for e in events)


# ---------- 取消双协议 ----------


@pytest.mark.asyncio
async def test_absorb_cancel_returns_partial_text_and_uncancels():
    provider = ScriptProvider(["部分", None])
    events, emit = collector()

    async def scenario():
        return await run_member_text(
            provider, [Message.user("q")], timeout_s=30, emit=emit,
            policy=MemberTurnPolicy(absorb_cancel=True),
        )

    task = asyncio.create_task(scenario())
    await asyncio.sleep(0.1)  # 等 delta 流出、进入挂起
    task.cancel()
    turn = await task  # 取消被就地吸收，不再上抛

    assert turn.error == "cancelled"
    assert turn.text == "部分"
    assert task.cancelling() == 0  # uncancel 已消除悬挂的取消状态
    assert provider.calls == 1  # 不重试
    assert not any(isinstance(e, NoticeEvent) for e in events)


@pytest.mark.asyncio
async def test_penetrating_cancel_propagates_and_keeps_shared_bucket():
    provider = ScriptProvider(["部分", None])
    events, emit = collector()
    bucket: list[str] = []  # caller 持有的共享草稿桶（圆桌 live_parts 协议）

    async def scenario():
        return await run_member_text(
            provider, [Message.user("q")], timeout_s=30, emit=emit,
            policy=MemberTurnPolicy(absorb_cancel=False), parts=bucket,
        )

    task = asyncio.create_task(scenario())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # 穿透协议：取消原样上抛，由 caller 外层统一收

    assert bucket == ["部分"]  # 圆桌回收协议：caller 从共享桶收回部分草稿


# ---------- 用量双口径 ----------


def _retry_after_usage_provider() -> ScriptProvider:
    # 「ProviderDone 后又抛瞬态错」的病态服务：用量与重试同时发生，
    # 只有这里才暴露两种口径的差异（正常服务吐 Done 即成功）
    return ScriptProvider([
        ProviderDone(input_tokens=11, output_tokens=7, cached_tokens=3),
        RuntimeError("HTTP 429 rate limit exceeded"),
        "恢复",
    ])


@pytest.mark.asyncio
async def test_usage_accumulates_across_attempts_by_default():
    events, emit = collector()
    provider = _retry_after_usage_provider()

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(retries=3, base_delay_s=0.0),
    )

    assert provider.calls == 2
    assert turn.text == "恢复" and turn.error == ""
    # 圆桌成员口径：跨重试尝试累计（11+13 / 7+9 / 3+5）
    assert (turn.input_tokens, turn.output_tokens, turn.cached_tokens) == (24, 16, 8)


@pytest.mark.asyncio
async def test_usage_resets_per_attempt_when_policy_says_so():
    events, emit = collector()
    provider = _retry_after_usage_provider()

    turn = await run_member_text(
        provider, [Message.user("q")], timeout_s=30, emit=emit,
        policy=MemberTurnPolicy(retries=3, base_delay_s=0.0, reset_usage_per_attempt=True),
    )

    assert provider.calls == 2
    assert turn.text == "恢复" and turn.error == ""
    # 融合/对抗口径：每次尝试清零重计，只留最后一次
    assert (turn.input_tokens, turn.output_tokens, turn.cached_tokens) == (13, 9, 5)


# ---------- 信号量消费 ----------


@pytest.mark.asyncio
async def test_semaphore_gates_concurrent_calls():
    events, emit = collector()
    probe = {"cur": 0, "peak": 0}

    class ProbeProvider(Provider):
        name, model = "probe", "p1"

        async def stream(self, messages, tool_schemas, effort=None):
            probe["cur"] += 1
            probe["peak"] = max(probe["peak"], probe["cur"])
            try:
                await asyncio.sleep(0.05)
            finally:
                probe["cur"] -= 1
            yield ProviderTextDelta("x")
            yield ProviderDone(stop_reason="end_turn", input_tokens=1, output_tokens=1)

    sem = asyncio.Semaphore(1)  # caller 持有的并发闸（圆桌 per-provider 字典的一格）

    async def one() -> mr.TextTurnResult:
        return await run_member_text(
            ProbeProvider(), [Message.user("q")], timeout_s=30, emit=emit,
            policy=MemberTurnPolicy(), semaphore=sem,
        )

    first, second = await asyncio.gather(one(), one())
    assert first.error == "" and second.error == ""
    assert probe["peak"] == 1  # 同一信号量下并发被限流


# ---------- 单一实现溯源（圆桌成员 / 圆桌融合路径） ----------


@pytest.mark.asyncio
async def test_roundtable_member_path_dispatches_runtime(monkeypatch):
    """圆桌成员路径真的经 run_member_text——替换共享名后计数必须为 1，
    模块里没有私藏的循环副本（monkeypatch 计数形态，非源码文本断言）。"""
    real = mr.run_member_text
    seen: list[MemberTurnPolicy] = []

    async def spy(provider, messages, **kw):
        seen.append(kw["policy"])
        return await real(provider, messages, **kw)

    monkeypatch.setattr(rt, "run_member_text", spy)

    spec = rt.MemberSpec(
        provider_name="fake", model="fake-1",
        provider=FakeProvider([[TextBlock(text="草稿")]]),
    )
    events, emit = collector()
    result = await rt._run_member(spec, 0, [Message.user("问题")], "问题", 30, emit)

    assert result.status == "done" and result.text == "草稿"
    assert any(e.kind == "roundtable_member_finished" for e in events)
    assert len(seen) == 1
    policy = seen[0]
    assert policy.timeout_label == "作答"
    assert policy.retry_subject == "圆桌成员"
    assert policy.absorb_cancel is False
    assert policy.reset_usage_per_attempt is False
    assert policy.budget_policy == "turn_error"


@pytest.mark.asyncio
async def test_roundtable_fusion_path_dispatches_runtime(monkeypatch):
    """圆桌融合路径真的经 run_member_text——替换共享名后计数必须为 1，
    模块里没有私藏的循环副本（monkeypatch 计数形态，非源码文本断言）。
    融合策略：取消就地收敛、用量每次尝试清零、预算耗尽落本轮 error，
    对外保持五元组返回形状。"""
    real = mr.run_member_text
    seen: list[MemberTurnPolicy] = []

    async def spy(provider, messages, **kw):
        seen.append(kw["policy"])
        return await real(provider, messages, **kw)

    monkeypatch.setattr(rt, "run_member_text", spy)

    events, emit = collector()
    text, error, u_in, u_out, u_cached = await rt._run_fusion(
        FakeProvider([[TextBlock(text="融合稿")]]),
        [Message.user("问题")], 30, emit,
    )

    assert text == "融合稿" and error == ""
    assert any(e.kind == "text_delta" for e in events)
    assert len(seen) == 1
    policy = seen[0]
    assert policy.timeout_label == "融合"
    assert policy.retry_label == "融合"
    assert policy.retry_subject == "圆桌成员"
    assert policy.absorb_cancel is True
    assert policy.reset_usage_per_attempt is True
    assert policy.budget_policy == "turn_error"


def test_roundtable_imports_runtime_single_source():
    """is 溯源：圆桌引用的就是 member_runtime 的同一对象，无私有副本
    （成员与融合两条路径共用本模块命名空间的同一个 run_member_text）。"""
    assert rt.run_member_text is mr.run_member_text
    assert rt.MemberTurnPolicy is mr.MemberTurnPolicy
