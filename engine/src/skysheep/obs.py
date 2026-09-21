"""结构化日志：给「这一轮为什么慢」类问题留下可检索的证据。

现状与动机
----------
桌面日志是固定文本格式 ``%(asctime)s %(levelname)s %(name)s: %(message)s``
（见 ``desktop.py`` 的 ``_setup_logging``），只有人类可读的一行字。回答
「这个任务为什么卡了 30 秒」只能靠翻几千行文本、肉眼估时间差；用量表
（``usage_log``）只有 token，没有耗时与阶段划分。

做法
----
不引入新依赖、不改变既有行格式，而是在消息尾部追加一段以固定标记开头的 JSON：

    2026-09-21 19:30:01 INFO skysheep.run: turn finished |json|{"ev":"turn","duration_ms":17000}

好处：既有日志阅读方式与诊断包的尾读逻辑都不受影响；需要机器分析时按标记
切出 JSON 即可。字段命名统一为 ``ev`` + 各事件自己的维度。

使用约定
--------
- 只在**阶段边界的收尾处**记一条，不要在循环里逐次调用（日志本身不该成为热点）；
- ``duration_ms`` 用 ``time.monotonic()`` 的差值算（不受系统时间校准影响）；
- 不要记录提示词正文、文件内容、密钥等敏感信息：``fields`` 只放标识与度量。
"""

from __future__ import annotations

import json
import logging
import time

# 结构化行的标记：出现在消息尾部，便于从文本日志里切出 JSON
STRUCT_TAG = " |json| "

_logger = logging.getLogger("skysheep.obs")


def _log(level: int, ev: str, message: str, **fields) -> None:
    payload = {"ev": ev}
    payload.update({k: v for k, v in fields.items() if v is not None})
    try:
        tail = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        # 字段不可序列化（塞进了奇怪对象）时不能让日志本身炸掉主流程
        tail = json.dumps({"ev": ev, "note": "unserializable fields"}, ensure_ascii=False)
    _logger.log(level, "%s%s%s", message, STRUCT_TAG, tail)


def info(ev: str, message: str, **fields) -> None:
    _log(logging.INFO, ev, message, **fields)


def warning(ev: str, message: str, **fields) -> None:
    _log(logging.WARNING, ev, message, **fields)


class span:
    """阶段耗时计时器：``with span() as s: ...`` 后读 ``s.ms``。

    用 monotonic 计时，并在异常路径上照样可用（调用方自己决定要不要记）。
    """

    __slots__ = ("_t0", "_ms")

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._ms = 0

    def __enter__(self) -> span:
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc) -> None:
        self._ms = int((time.monotonic() - self._t0) * 1000)

    @property
    def ms(self) -> int:
        """已耗时（毫秒）。未退出上下文时返回从进入算起的当前值。"""
        if self._ms:
            return self._ms
        return int((time.monotonic() - self._t0) * 1000)


def parse_structured(line: str) -> dict | None:
    """从一行日志里取回结构化字段；没有则返回 None。

    给诊断/分析脚本用，让「按 session_id 捞本轮各阶段耗时」这类查询成立。
    """
    idx = line.find(STRUCT_TAG)
    if idx < 0:
        return None
    try:
        return json.loads(line[idx + len(STRUCT_TAG):])
    except json.JSONDecodeError:
        return None
