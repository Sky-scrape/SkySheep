"""上游 Provider 错误分类。

独立小模块：agent 主循环（重试流式调用）与上下文压缩（core/context.py，
重试摘要调用）都要用同一套「瞬态错误」判定。放在这里而不是 agent.py，
是为了避免 context↔agent 循环导入（agent.py 导入 context 的 compact_history）。
"""

from __future__ import annotations

TRANSIENT_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests",
    "timeout", "timed out", "temporarily unavailable", "overloaded", "overloaded_error",
    "502", "503", "504", "bad gateway", "service unavailable",
    "connection error", "connection reset", "connection aborted",
    "connection closed", "eof occurred", "incomplete", "apitimeout",
)


def is_transient_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(marker in msg for marker in TRANSIENT_MARKERS)
