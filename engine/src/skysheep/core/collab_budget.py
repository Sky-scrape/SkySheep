"""协作模板的运行级预算：调用次数 / token 累计 / 墙钟时长。

为什么单独成模块：圆桌、对抗、团队三条链路都要在**最内层真实 provider 调用**
前计数（重试与格式纠错也计），但三条链路互不 import（团队还另有轮次预算），
把「数到哪、超了怎么报」收敛成一个不依赖任何引擎状态的纯对象，三处共用同一
套中文文案与同一套边界语义。

设计约定：
- 0 = 不限（与设置页其它限额字段同语义），负数在模板校验期已被拒；
- ``spend()`` 在真正发起一次模型调用前调用；调用次数**先加后判**，即
  ``max_calls=3`` 允许第 1/2/3 次、第 4 次抛 ``BudgetExceeded``；
- ``add_tokens(n)`` 接收的是**累计值**而不是增量：团队每轮把本轮累计 token
  回填进来，重复调用不会重复累加（取 max）；
- 时长用注入的 ``clock``（默认 monotonic 墙钟），测试可传假时钟；
- 超限一律抛 ``BudgetExceeded``，中文文案由 ``reason_label()`` 统一给出；
  调用方按「保留已产出内容、如实收尾」处理，不重试、不静默续跑。
"""

from __future__ import annotations

import time
from collections.abc import Callable

# 三条预算线对应的内部原因码与文案模板。
REASON_CALLS = "calls"
REASON_TOKENS = "tokens"
REASON_SECONDS = "seconds"

_LABELS = {
    REASON_CALLS: "已达模板调用次数上限（{max_calls} 次），已停止后续调用并保留已产出内容",
    REASON_TOKENS: "已达模板 token 预算上限（{max_tokens}），已停止后续调用并保留已产出内容",
    REASON_SECONDS: "已达模板运行时长上限（{max_seconds} 秒），已停止后续调用并保留已产出内容",
}


class BudgetExceeded(Exception):
    """模板预算耗尽。

    ``reason`` 是内部原因码（calls / tokens / seconds），``message`` 是可直接
    展示给用户的中文文案；``str(exc)`` 返回中文文案，便于各链路把它当普通
    错误串收尾（圆桌成员卡、对抗阶段失败等已有路径都只读 str(e)）。
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class RunBudget:
    """一次模板运行（一轮圆桌 / 一场对抗 / 一次建队）的预算计数器。

    参数全部可选、0 表示不限；传入时钟便于单测在不 sleep 的情况下验证时长线。
    """

    def __init__(
        self,
        max_calls: int = 0,
        max_tokens: int = 0,
        max_seconds: int = 0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_calls = max(0, int(max_calls or 0))
        self.max_tokens = max(0, int(max_tokens or 0))
        self.max_seconds = max(0, int(max_seconds or 0))
        self._clock = clock
        self._started = clock()
        self.calls = 0
        self.tokens = 0

    # ---- 计数 ----

    def spend(self) -> None:
        """记一次真实 provider 调用；超限立即抛 BudgetExceeded。

        调用方应在 ``provider.stream(...)`` 之前调用。抛异常后本次调用不再
        发生；已发生的调用与已产出的文本都由上层保留。
        """
        self.calls += 1
        self.raise_if_exceeded()

    # 契约里的旧名字（take_call）保留为别名：圆桌/对抗/团队三条链路的写法
    # 可以各自沿用，语义完全一致。
    def take_call(self) -> None:
        self.spend()

    def add_tokens(self, total_tokens: int) -> None:
        """回填**累计** token 用量（取 max，重复回填不重复累加）。"""
        self.tokens = max(self.tokens, int(total_tokens or 0))

    def add_tokens_delta(self, delta_tokens: int) -> None:
        """累加**增量** token 用量（与 ``add_tokens`` 的累计值语义区分）。

        圆桌 / 对抗 / 压缩摘要这类分段调用各自只知道本段用量，用增量累加
        才能把多段真实调用加总；团队仍用 ``add_tokens`` 回填跨轮累计值。
        """
        self.tokens += max(0, int(delta_tokens or 0))

    # ---- 判定 ----

    @property
    def seconds_used(self) -> float:
        return max(0.0, self._clock() - self._started)

    @property
    def remaining_calls(self) -> int | None:
        """剩余可调用次数；不限时返回 None（UI 显示「不限」用）。"""
        if not self.max_calls:
            return None
        return max(0, self.max_calls - self.calls)

    def reason(self) -> str:
        """返回首个命中的原因码；都没超返回空串。顺序：次数 → token → 时长。"""
        if self.max_calls and self.calls > self.max_calls:
            return REASON_CALLS
        if self.max_tokens and self.tokens > self.max_tokens:
            return REASON_TOKENS
        if self.max_seconds and self.seconds_used > self.max_seconds:
            return REASON_SECONDS
        return ""

    def exceeded(self) -> bool:
        return bool(self.reason())

    def reason_label(self) -> str:
        """超限的中文文案；没超返回空串。"""
        reason = self.reason()
        if not reason:
            return ""
        return _LABELS[reason].format(
            max_calls=self.max_calls,
            max_tokens=self.max_tokens,
            max_seconds=self.max_seconds,
        )

    def raise_if_exceeded(self) -> None:
        reason = self.reason()
        if reason:
            raise BudgetExceeded(reason, self.reason_label())


class BudgetedProvider:
    """Provider 代理：每次真实 ``stream()`` 前记一次模板预算调用。

    团队链路（成员 / AI 总管 / 小会）的 provider 由真实 Agent 直接驱动，
    没有圆桌/对抗那样的统一调用点；在宿主装配时套一层此代理，就能让
    ``budget.max_calls`` 覆盖「provider 调用次数（含重试与格式纠错）」，
    而不是只把 max_calls 当成轮次上限（审查 finding 15）。其余属性读写
    全部透传给真实 provider，不构成第二套模型状态。
    """

    def __init__(self, inner, budget: RunBudget) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_budget", budget)

    def stream(self, messages, tool_schemas, effort=None):
        # spend() 先加后判：超限在发起真实调用前抛 BudgetExceeded，
        # Agent 的通用异常路径会把它收成该成员/总管轮的失败，团队预算钩子
        # 随后按 calls 越线收队。
        self._budget.spend()
        return self._inner.stream(messages, tool_schemas, effort=effort)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_inner"), name, value)


def _min_positive(*values: int) -> int:
    """取所有正数里的最小值；全为 0/None 时返回 0（视为不限）。"""
    positives = [int(v) for v in values if v and int(v) > 0]
    return min(positives) if positives else 0


def _int_attr(base: object, name: str, default: int) -> int:
    """取整型配置项，保留显式的 0（0 在这些字段里是合法值，不能被 or 吞掉）。"""
    value = getattr(base, name, default)
    return int(default if value is None else value)


class TeamCfgView:
    """把团队配置包一层「模板预算收紧」的只读视图。

    团队模板不持久化 limits（上限只来自 ``[team]`` 配置），``budget.max_calls``
    映射到既有的全局轮次上限 ``max_rounds``：取两者较小值，0 表示不收紧。
    真实的 provider 调用次数另由 ``BudgetedProvider`` 在每次 ``stream()``
    前计数（审查 finding 15），本视图的轮次映射只是额外的轮边界收紧。
    刻意不读 ``limits``：团队编排没有「成员超时 / 辩论轮」这些圆桌概念，
    把 limits 传进来会制造两套口径。
    """

    def __init__(self, base: object, budget: RunBudget) -> None:
        self.max_members = _int_attr(base, "max_members", 3)
        self.member_timeout_s = _int_attr(base, "member_timeout_s", 300)
        self.max_rounds = _min_positive(
            _int_attr(base, "max_rounds", 40), budget.max_calls,
        )
        self.redo_limit = _int_attr(base, "redo_limit", 2)
        self.stall_limit = _int_attr(base, "stall_limit", 2)
        self.cron_resume_limit = _int_attr(base, "cron_resume_limit", 1)
