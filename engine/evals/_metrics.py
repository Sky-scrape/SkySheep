"""评测指标层：把「过 / 不过」补成可聚合、可设预算的数字。

评测基线（evals/）此前只有断言：场景红了知道，但「这一轮到底多贵、多慢、
调用了几次工具、等了几次确认」没人记。本模块补三件事：

1. ``TurnMetrics.from_events`` —— 从一轮真实事件流里抽出客观指标（耗时、
   token、工具次数与耗时、模型重试、确认次数与被拒次数）。只读事件，不改
   产品行为，也不依赖任何模型文本。
2. ``assert_metrics`` —— 给场景加**预算断言**：例如「这个压缩场景最多 3 次
   工具调用、5 秒内跑完」。预算写宽一点（CI 慢机），抓的是数量级退化。
3. ``aggregate`` / ``percentile`` —— 把多次运行聚合成报告：通过率、p50/p95
   耗时、token 总量、逐场景分桶。``tools/eval_report.py`` 用它出 Markdown。

为什么不评文本质量：那是 LLM-as-judge 的活（``evals/judge.py``），需要真实
模型、有随机性，不能进默认回归。指标层只做确定性统计——CI 里零成本、零网络。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "TurnMetrics",
    "aggregate",
    "assert_metrics",
    "load_records",
    "percentile",
    "write_records",
]

METRICS_DIRNAME = ".metrics"


@dataclass
class TurnMetrics:
    """一轮（或一个评测场景）的客观指标。字段全部可缺省，取不到就是 0。"""

    duration_ms: int = 0          # 引擎实测轮耗时（turn_finished.duration_ms）
    wall_ms: int = 0              # 事件流首末时间戳差（含排队/确认等待）
    iterations: int = 0           # 模型调用次数
    tool_calls: int = 0           # 工具调用次数
    tool_errors: int = 0          # 工具报错次数
    tools_total_ms: int = 0       # 工具执行总耗时
    model_retries: int = 0        # 模型调用自动重试次数
    permission_requests: int = 0  # 弹确认次数
    permission_denied: int = 0    # 被用户/策略拒绝次数
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    text_chars: int = 0           # 回答正文总字符数（只用于成本对照，不评质量）
    stop_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_events(cls, events: list) -> TurnMetrics:
        """从 agent 事件对象或后端 dict 事件里抽指标（两种形态都支持）。

        事件形态判别按 ``kind``：agent 侧是 pydantic 事件（属性访问），
        ServerBackend 侧是 dict（``e.get("kind")``）。两条路径的字段名一致。
        """
        m = cls()
        if not events:
            return m
        ts: list[float] = []
        for ev in events:
            kind = ev.get("kind") if isinstance(ev, dict) else getattr(ev, "kind", "")
            t = ev.get("ts") if isinstance(ev, dict) else getattr(ev, "ts", 0)
            if t:
                ts.append(float(t))
            if kind == "tool_call_finished":
                m.tool_calls += 1
                if ev.get("is_error") if isinstance(ev, dict) else getattr(ev, "is_error", False):
                    m.tool_errors += 1
                m.tools_total_ms += int(
                    (ev.get("duration_ms") if isinstance(ev, dict)
                     else getattr(ev, "duration_ms", 0)) or 0
                )
            elif kind == "permission_request":
                m.permission_requests += 1
            elif kind == "permission_resolved":
                decision = (
                    ev.get("decision") if isinstance(ev, dict)
                    else getattr(ev, "decision", "")
                )
                if decision == "deny":
                    m.permission_denied += 1
            elif kind == "usage":
                m.input_tokens += int(
                    (ev.get("input_tokens") if isinstance(ev, dict)
                     else getattr(ev, "input_tokens", 0)) or 0
                )
                m.output_tokens += int(
                    (ev.get("output_tokens") if isinstance(ev, dict)
                     else getattr(ev, "output_tokens", 0)) or 0
                )
                m.cached_tokens += int(
                    (ev.get("cached_tokens") if isinstance(ev, dict)
                     else getattr(ev, "cached_tokens", 0)) or 0
                )
            elif kind == "text_delta":
                text = ev.get("text") if isinstance(ev, dict) else getattr(ev, "text", "")
                m.text_chars += len(text or "")
            elif kind == "turn_finished":
                m.stop_reason = str(
                    (ev.get("stop_reason") if isinstance(ev, dict)
                     else getattr(ev, "stop_reason", "")) or ""
                )
                m.iterations = int(
                    (ev.get("iterations") if isinstance(ev, dict)
                     else getattr(ev, "iterations", 0)) or 0
                )
                m.tools_total_ms = int(
                    (ev.get("tools_total_ms") if isinstance(ev, dict)
                     else getattr(ev, "tools_total_ms", 0)) or m.tools_total_ms
                )
                m.model_retries = int(
                    (ev.get("model_retries") if isinstance(ev, dict)
                     else getattr(ev, "model_retries", 0)) or 0
                )
                m.duration_ms = int(
                    (ev.get("duration_ms") if isinstance(ev, dict)
                     else getattr(ev, "duration_ms", 0)) or 0
                )
        if ts:
            m.wall_ms = int((max(ts) - min(ts)) * 1000)
        if not m.duration_ms:
            m.duration_ms = m.wall_ms
        return m

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d


def assert_metrics(
    metrics: TurnMetrics, *, max_tool_calls: int | None = None,
    max_permission_requests: int | None = None,
    max_total_tokens: int | None = None,
    max_duration_ms: int | None = None,
    max_model_retries: int | None = None,
) -> None:
    """预算断言：超出上限直接 AssertionError，消息里带上实际值方便定位。

    刻意只设**上限**不设下限：下限（比如「至少要调一次工具」）属于行为断言，
    场景里显式写 counts（见 evals/README.md），不混进性能预算。
    """
    checks = [
        ("工具调用次数", metrics.tool_calls, max_tool_calls),
        ("权限确认次数", metrics.permission_requests, max_permission_requests),
        ("token 总量", metrics.total_tokens, max_total_tokens),
        ("轮耗时(ms)", metrics.duration_ms, max_duration_ms),
        ("模型重试次数", metrics.model_retries, max_model_retries),
    ]
    over = [
        f"{name} {actual} > 预算 {limit}"
        for name, actual, limit in checks
        if limit is not None and actual > limit
    ]
    if over:
        raise AssertionError("评测预算超限：" + "；".join(over))


def percentile(values: list[float], p: float) -> float:
    """最近秩百分位（p 取 0~100）；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _extra(r: dict) -> dict:
    """记录里场景附加的指标：插件写 ``extra``，手工脚本也接受 ``metrics``。"""
    for key in ("metrics", "extra"):
        v = r.get(key)
        if isinstance(v, dict):
            return v
    return {}


def aggregate(records: list[dict]) -> dict[str, Any]:
    """把逐条运行记录聚合为报告：通过率、耗时分位、token 总量、逐场景分桶。

    记录形状（conftest 插件写的 JSONL）::

        {"id": "test_eval_xxx[case]", "outcome": "passed|failed|skipped",
         "wall_ms": 123, "extra": {...TurnMetrics.as_dict()}}

    ``extra`` 可缺省（收集期/夹具失败的记录没有它）；手工脚本写 ``metrics``
    键也接受。
    """
    rows = [r for r in records if isinstance(r, dict)]
    passed = sum(1 for r in rows if r.get("outcome") == "passed")
    failed = sum(1 for r in rows if r.get("outcome") == "failed")
    skipped = sum(1 for r in rows if r.get("outcome") == "skipped")
    executed = passed + failed
    walls = [float(r.get("wall_ms") or 0) for r in rows if r.get("outcome") != "skipped"]
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = str(r.get("id") or "")
        bucket = name.split("::", 1)[0].split("[", 1)[0] or "(unknown)"
        b = buckets.setdefault(bucket, {"passed": 0, "failed": 0, "skipped": 0, "wall_ms": []})
        outcome = r.get("outcome")
        if outcome in ("passed", "failed", "skipped"):
            b[outcome] += 1
        b["wall_ms"].append(float(r.get("wall_ms") or 0))
    for b in buckets.values():
        b["p50_ms"] = round(percentile(b["wall_ms"], 50), 1)
        b["p95_ms"] = round(percentile(b["wall_ms"], 95), 1)
        b.pop("wall_ms", None)
    tokens = sum(int(_extra(r).get("total_tokens") or 0) for r in rows)
    tool_calls = sum(int(_extra(r).get("tool_calls") or 0) for r in rows)
    return {
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(passed / executed, 4) if executed else None,
        "p50_ms": round(percentile(walls, 50), 1),
        "p95_ms": round(percentile(walls, 95), 1),
        "max_ms": round(max(walls), 1) if walls else 0.0,
        "total_tokens": tokens,
        "tool_calls": tool_calls,
        "buckets": dict(sorted(buckets.items())),
    }


def write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_records(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


@dataclass
class RunRecorder:
    """进程内累计一次 pytest 会话的记录（conftest 插件用）。"""

    records: list[dict] = field(default_factory=list)

    def add(self, record: dict) -> None:
        self.records.append(record)
