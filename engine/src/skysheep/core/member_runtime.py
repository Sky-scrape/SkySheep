"""协作成员轮运行时：一次「无工具纯文本模型调用」的流式收集 + 用量 +
超时 + 瞬态重试 + 取消协议。

圆桌成员（core/roundtable.py 的 _run_member）、圆桌融合（同模块的
_run_fusion）与对抗各阶段（core/adversarial.py 的 _run_text_turn）都已
迁到这里的 ``run_member_text``，差异全部由 :class:`MemberTurnPolicy`
参数化（三处 caller 都是薄壳）。per-provider 信号量字典、
fail_counts 连败跳过、辩论轮调度、relay 的 CAS、终态/用量事件的发射顺序
一律留在 caller——本模块只负责单次成员轮本身的容错语义：

- 流式收集：``ProviderTextDelta`` 逐段入桶并经 ``on_delta`` 回调转发
  （None = 静默收集）；
- 超时：``asyncio.wait_for`` 截断，文案前缀由 ``policy.timeout_label`` 给；
- 重试：只在还没吐出任何内容时（否则重放会导致卡片文本重复），且仅瞬态
  错误（provider_errors.is_transient_error）；通知与退避经
  collab_common.notice_retry（单一实现）；
- 用量：``ProviderDone`` 累加；``reset_usage_per_attempt=False`` 时跨重试
  尝试累计（圆桌成员口径），True 时每次尝试清零重计（融合/对抗口径）；
- 取消：``absorb_cancel=True`` 就地收敛（uncancel + error="cancelled"，
  部分文本随返回值交还 caller 落库）；False 时取消穿透上抛，caller 从共享
  ``parts`` 桶回收部分草稿（圆桌成员的回收协议）。team 的「定稿+上抛」
  协议不在这里（team 消费的是 Agent 轮事件流，不经本模块）。
- 预算：循环只消费 ``RunBudget.spend()``（最内层真实调用前，含重试）；
  tokens/seconds 线的判定由 caller 的 ``budget.exceeded()`` 分支承担，
  本模块不感知。

不导入 Agent/权限门/工具注册表——纯文本协作不经过权限门（与圆桌/对抗
既有约定一致）。依赖面刻意收窄：collab_common（通知/事件形状）、
collab_budget（预算）、provider_errors（瞬态判定，零依赖叶模块）、
effort（自动思考档）、messages、models.base 与 agent 的重试常量。

与 relay 的关系（防「顺手统一」）：core/relay.py 的 ``run_node_text`` 不迁入
本模块——relay 无事件面、函数内零自动重试（重试在 caller 层，试满即暂停
等人）、错误带结构化 code（RelayLimitError）、部分文本不落库是 CAS epoch
原子性的一部分；它用 ``asyncio.timeout`` 而非 ``wait_for`` 也是故意的
（要把 TimeoutError 转成 RelayLimitError）。两边超时/错误协议的差异是设计
决策，不要互相靠近。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..messages import Message
from ..models.base import Provider, ProviderDone, ProviderTextDelta
from .agent import MAX_STREAM_RETRIES, RETRY_BASE_DELAY_S
from .collab_budget import BudgetExceeded, RunBudget
from .collab_common import EmitFn, notice_retry
from .effort import resolve_auto_effort
from .provider_errors import is_transient_error

# 重试通知函数的形状（即 collab_common.notice_retry 的签名）。caller 可注入
# 自己模块命名空间里的绑定：实现挪进本模块后，「monkeypatch caller 模块属性
# 再断言真实路径经过它」的既有测试契约（如 test_collab_shared_helpers 对
# rt.notice_retry 的 spy）不需要搬家。
RetryNoticeFn = Callable[..., Awaitable[None]]


@dataclass
class TextTurnResult:
    """一次成员轮文本调用的结果。

    text 是本次（含最后一次尝试）收集到的全部文本；error 为空 = 成功，
    "cancelled" = 取消就地收敛（absorb_cancel=True），其余为失败文案。
    input/output/cached_tokens 的口径受 policy.reset_usage_per_attempt 影响：
    False 时跨重试尝试累计，True 时只含最后一次尝试的量。
    """

    text: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class MemberTurnPolicy:
    """成员轮的容错策略（各协作形态的差异全部在这里参数化）。

    retries 是允许的重试次数（非尝试总数，至多 retries+1 次尝试）；
    base_delay_s / retry_subject / retry_label 原样传给重试通知；
    timeout_label 是超时文案前缀（「作答」/「融合」/对抗阶段名）；
    absorb_cancel=True 时取消就地收敛，False 时穿透上抛（caller 从共享桶
    回收部分草稿）；reset_usage_per_attempt 控制用量跨尝试口径；
    budget_policy="turn_error" 把 BudgetExceeded 落成本轮 error（圆桌现状，
    与普通失败同路径）、"propagate" 原样上抛（对抗现状：吞掉会继续烧下一
    阶段）。base_delay_s / retries 由 caller 显式传各自模块常量，保住
    「monkeypatch caller 模块常量」的既有测试契约。
    """

    retries: int = MAX_STREAM_RETRIES
    base_delay_s: float = RETRY_BASE_DELAY_S
    retry_subject: str = ""
    retry_label: str = ""
    timeout_label: str = ""
    absorb_cancel: bool = False
    reset_usage_per_attempt: bool = False
    budget_policy: str = "turn_error"


async def run_member_text(
    provider: Provider,
    messages: list[Message],
    *,
    timeout_s: int,
    emit: EmitFn,
    policy: MemberTurnPolicy,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    budget: RunBudget | None = None,
    semaphore: asyncio.Semaphore | None = None,
    parts: list[str] | None = None,
    notice: RetryNoticeFn | None = None,
) -> TextTurnResult:
    """跑一次成员轮：流式收集 + 用量 + 超时 + 瞬态重试 + 取消协议。

    provider.stream 恒以空工具表调用（纯文本协作，不经权限门）；每段文本
    先入 ``parts`` 桶再经 ``on_delta`` 回调转发，保证 caller 看到的桶与回调
    一致。``parts`` 传 caller 持有的共享桶时（圆桌成员的取消回收协议），
    每次尝试开头 clear——重试只在还没有任何内容时发生，不会丢已提交文本；
    不传时用内部桶。``semaphore`` 是 caller 持有的并发闸，循环只消费；
    ``notice`` 缺省用 collab_common.notice_retry（单一实现）。

    BudgetExceeded 按策略落 error（turn_error，不重试）或原样上抛
    （propagate）；其余异常一律归入本轮失败文案（截 200 字符），不穿透。
    取消（absorb_cancel=False）不在本层吞，直接上抛。
    """
    bucket: list[str] = parts if parts is not None else []
    notify = notice if notice is not None else notice_retry
    usage_in = usage_out = usage_cached = 0

    async def _stream_once() -> None:
        nonlocal usage_in, usage_out, usage_cached
        # 自动档：纯文本调用同样按任务复杂度实时估档
        async for pe in provider.stream(
            messages, [], effort=resolve_auto_effort(provider, messages),
        ):
            if isinstance(pe, ProviderTextDelta):
                bucket.append(pe.text)
                if on_delta is not None:
                    await on_delta(pe.text)
            elif isinstance(pe, ProviderDone):
                usage_in += pe.input_tokens
                usage_out += pe.output_tokens
                usage_cached += pe.cached_tokens

    async def _consume() -> None:
        # 预算计数点在最内层真实调用前（重试的每次真实请求都计一次）；
        # BudgetExceeded 的去向由 policy.budget_policy 决定。
        if budget is not None:
            budget.spend()
        if semaphore is not None:
            async with semaphore:
                await _stream_once()
        else:
            await _stream_once()

    def _failure(error: str) -> TextTurnResult:
        # 超时 / 非瞬态错误 / 已吐出内容：如实失败，保留已收集的部分文本
        return TextTurnResult(
            "".join(bucket), error, usage_in, usage_out, usage_cached,
        )

    try:
        for attempt in range(1, policy.retries + 2):
            # 重试只在还没有任何内容时发生，clear() 不会丢已提交的文本
            # （共享桶场景同样成立：caller 据此在取消时回收部分草稿）
            bucket.clear()
            if policy.reset_usage_per_attempt:
                usage_in = usage_out = usage_cached = 0
            error = ""
            retryable = False
            try:
                await asyncio.wait_for(_consume(), timeout=timeout_s)
                return TextTurnResult(
                    "".join(bucket), "", usage_in, usage_out, usage_cached,
                )
            except BudgetExceeded as e:
                if policy.budget_policy == "propagate":
                    # 预算耗尽必须原样上抛让主流程统一收尾；若落进下面的
                    # 通用失败会被吞成本轮失败、继续烧下一阶段。
                    raise
                # "turn_error"（圆桌现状：预算异常不特判，与普通失败同路径）：
                # 按通用分支的语义就地等价处理——文案进 error、是否重试同判。
                error = str(e)[:200]
                retryable = (
                    not bucket and attempt <= policy.retries and is_transient_error(e)
                )
            except TimeoutError:
                error = f"{policy.timeout_label}超时（>{timeout_s} 秒）"
                retryable = not bucket and attempt <= policy.retries
            except Exception as e:  # noqa: BLE001 - 任何异常都归入本轮失败，不上抛
                error = str(e)[:200]
                retryable = (
                    not bucket and attempt <= policy.retries and is_transient_error(e)
                )
            if retryable:
                await notify(
                    emit, policy.retry_label, error, attempt,
                    subject=policy.retry_subject, base_delay_s=policy.base_delay_s,
                )
                continue
            return _failure(error)
        # 循环耗尽兜底（理论上不可达：最后一次尝试的重试条件必为假），
        # 防止 caller 拿到 error 为空却按失败收尾
        return _failure("重试次数用尽")  # pragma: no cover - 防御性兜底
    except asyncio.CancelledError:
        if not policy.absorb_cancel:
            # 穿透协议（圆桌成员）：取消不在本层吞，caller 外层统一收，
            # 并从共享桶回收已流出的部分草稿
            raise
        # 吸收协议（融合/对抗）：uncancel 消除悬挂的取消状态，
        # 已流出的部分文本随返回值交还 caller 落库
        asyncio.current_task().uncancel()
        return TextTurnResult(
            "".join(bucket), "cancelled", usage_in, usage_out, usage_cached,
        )
