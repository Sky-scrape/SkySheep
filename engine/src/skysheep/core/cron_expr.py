"""标准 5 段 Cron 表达式：校验与下次触发时间（纯函数，零依赖）。

字段：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-7，0 与 7 都表示周日)。

语法：``*``、单值、范围 ``a-b``、列表 ``a,b,c``、步长 ``*/n`` / ``a-b/n``
（``a/n`` 按 ``a-最大值/n`` 处理）。不支持月份/星期英文缩写与 ``?`` / ``L``
等扩展写法——定时任务的表单是中文界面，写错时给中文原因，不静默降级。

日与周同时受限时按标准 Cron 语义取「或」：``0 9 1 * 1`` = 每月 1 号
**或** 每周一 9 点。
"""

from __future__ import annotations

import time

FieldSet = frozenset[int]
Parsed = tuple[FieldSet, FieldSet, FieldSet, FieldSet, FieldSet]

# (名字, 最小值, 最大值)
_FIELDS = (
    ("分", 0, 59),
    ("时", 0, 23),
    ("日", 1, 31),
    ("月", 1, 12),
    ("周", 0, 7),
)

_MAX_DAYS_AHEAD = 366 * 2  # 闰年 + 极端组合（2 月 29 日）兜底


class CronExprError(ValueError):
    """表达式无法解析（message 是给用户看的中文原因）。"""


def _parse_int(raw: str, name: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CronExprError(f"{name}字段「{raw}」不是数字") from None
    if not lo <= value <= hi:
        raise CronExprError(f"{name}字段「{raw}」超出范围 {lo}-{hi}")
    return value


def _parse_field(spec: str, name: str, lo: int, hi: int) -> set[int]:
    spec = (spec or "").strip()
    if not spec:
        raise CronExprError(f"{name}字段为空")
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronExprError(f"{name}字段「{spec}」含空的列表项")
        step = 1
        base = part
        if "/" in part:
            base, _sep, raw_step = part.partition("/")
            step = _parse_int(raw_step, f"{name}步长", 1, hi - lo + 1 or 1)
            if not base:
                base = "*"
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            raw_a, _sep, raw_b = base.partition("-")
            start = _parse_int(raw_a, name, lo, hi)
            end = _parse_int(raw_b, name, lo, hi)
            if end < start:
                raise CronExprError(f"{name}字段范围「{base}」起点大于终点")
        else:
            start = _parse_int(base, name, lo, hi)
            # a/n：从 a 到上限按步长取（cron 惯例），无步长时就是一个单值
            end = hi if "/" in part else start
        values.update(range(start, end + 1, step))
    if not values:
        raise CronExprError(f"{name}字段「{spec}」没有可用取值")
    return values


def parse(expr: str) -> Parsed:
    """解析表达式；不合法抛 CronExprError（中文原因）。"""
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise CronExprError(
            "Cron 表达式需要 5 段（分 时 日 月 周），例如 0 9 * * 1-5"
        )
    parsed = tuple(
        _parse_field(part, name, lo, hi)
        for part, (name, lo, hi) in zip(parts, _FIELDS, strict=True)
    )
    minute, hour, dom, month, dow = parsed
    # 7 与 0 同为周日：归一到 0，避免集合里两个值让「是否受限」判断出错
    dow = frozenset(0 if d == 7 else d for d in dow)
    return minute, hour, dom, month, dow  # type: ignore[return-value]


def validate(expr: str) -> str | None:
    """校验表达式；合法返回 None，不合法返回中文原因。"""
    try:
        parse(expr)
    except CronExprError as e:
        return str(e)
    return None


def next_run(expr: str, now: float | None = None) -> float:
    """下一次触发时间戳（严格晚于 now）；表达式不合法抛 CronExprError。

    按本地时间逐日推进（目标日 → 允许的小时 → 允许的分钟），最多看两年。
    夏令时切换日按「当天 + 小时偏移」换算，可能与墙钟差一小时——国内无夏令时，
    影响可忽略。
    """
    minute, hour, dom, month, dow = parse(expr)
    n = time.time() if now is None else float(now)
    lt = time.localtime(n)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(0, 7))
    for delta in range(_MAX_DAYS_AHEAD + 1):
        day = day0 + delta * 86400
        d = time.localtime(day)
        if d.tm_mon not in month:
            continue
        dom_ok = d.tm_mday in dom
        dow_ok = (d.tm_wday + 1) % 7 in dow  # tm_wday: 0=周一 → Cron: 0=周日
        if dom_restricted and dow_restricted:
            if not (dom_ok or dow_ok):
                continue
        elif not (dom_ok and dow_ok):
            continue
        for h in sorted(hour):
            for m in sorted(minute):
                cand = day + h * 3600 + m * 60
                if cand > n:
                    return cand
    raise CronExprError("表达式在未来两年内没有可触发时间（请检查日/月/周组合）")
