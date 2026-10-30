"""结构化日志（obs）测试。

目标：既有文本日志的阅读方式不受影响，同时能用固定标记切出机器可读的字段。
"""

from __future__ import annotations

import json
import logging

import pytest

from skysheep import obs

# skysheep.obs 是进程级共享 logger：并行跑（xdist）时同一 worker 内的换装顺序
# 与串行不同，会让本模块的「改 handler → 抓窗口 → 还回去」相互踩到（表现为
# 窗口内一条记录都没抓到）。整模块钉到同一个 worker 并按文件顺序执行：并行
# 仍有收益（其余模块自由分派），本模块保持串行语义。
pytestmark = pytest.mark.xdist_group("obs_logs")


def _capture(caplog):
    """把 skysheep.obs 的记录收进 caplog。"""
    return caplog.at_level(logging.INFO, logger="skysheep.obs")


class _RecordCapture(logging.Handler):
    """逐条留存窗口内记录的原始消息文本（不走 StringIO 的字节流）。

    skysheep.obs 是进程级共享 logger：xdist 下同一 worker 里前序用例遗留的
    后台线程若恰好在换装窗口内也发 obs 记录，StringIO 里就会混进别人的行，
    parse_structured 对「首条标记之后的全部字节」做 JSON 解析便被带歪
    （表现为 payload 变 None）。逐条存消息后每条记录独立解析，互不污染；
    自己那条按「消息体 + 标记」前缀认领，别人的行抢不走断言。
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _swap_capture(capture: _RecordCapture):
    """把 skysheep.obs 的 handler 换成 capture，返回恢复函数（调用方 finally 用）。"""
    lg = logging.getLogger("skysheep.obs")
    old_handlers, old_level = lg.handlers, lg.level
    lg.handlers, lg.level = [capture], logging.INFO

    def _restore() -> None:
        lg.handlers, lg.level = old_handlers, old_level

    return _restore


def test_info_appends_parseable_json_payload(caplog):
    with _capture(caplog):
        obs.info("turn", "turn finished", session_id="s1", duration_ms=17000)

    line = caplog.records[-1].getMessage()
    assert line.startswith("turn finished")
    payload = obs.parse_structured(line)
    assert payload is not None
    assert payload["ev"] == "turn"
    assert payload["session_id"] == "s1"
    assert payload["duration_ms"] == 17000


def test_none_fields_are_dropped():
    """可选字段为 None 时不写进 JSON：日志行更短，语义是「没有这项」。"""
    line = "msg" + obs.STRUCT_TAG + json.dumps({"ev": "x", "a": 1})
    assert obs.parse_structured(line)["a"] == 1
    # 通过公开接口验证：None 不该出现（按消息体认领自己那条，不依赖字节流独占）
    cap = _RecordCapture()
    restore = _swap_capture(cap)
    try:
        obs.info("turn", "t", session_id="s1", error=None)
    finally:
        restore()
    ours = [m for m in cap.messages if m.startswith("t" + obs.STRUCT_TAG)]
    assert ours, f"本用例自己的日志行必须被接住（窗口内共 {len(cap.messages)} 条）"
    payload = obs.parse_structured(ours[-1])
    assert payload is not None
    assert "error" not in payload
    assert payload["session_id"] == "s1"


def test_warning_uses_warning_level(caplog):
    with caplog.at_level(logging.WARNING, logger="skysheep.obs"):
        obs.warning("mcp", "server down", server="fetch")
    assert caplog.records[-1].levelno == logging.WARNING
    assert obs.parse_structured(caplog.records[-1].getMessage())["server"] == "fetch"


def test_unserializable_field_does_not_raise():
    """字段塞进不可序列化对象时，日志不能把主流程带崩。"""
    cap = _RecordCapture()
    restore = _swap_capture(cap)
    try:
        obs.info("turn", "weird", obj=object())
    finally:
        restore()
    ours = [m for m in cap.messages if m.startswith("weird" + obs.STRUCT_TAG)]
    assert ours, f"本用例自己的日志行必须被接住（窗口内共 {len(cap.messages)} 条）"
    payload = obs.parse_structured(ours[-1])
    assert payload is not None
    assert payload["ev"] == "turn"


def test_plain_log_line_has_no_structured_payload():
    """普通日志行（没有标记）解析出 None：不会把无关日志误判成结构化事件。"""
    assert obs.parse_structured("2026-09-21 19:00:00 INFO skysheep: 服务就绪") is None


def test_span_measures_elapsed_ms():
    import time

    with obs.span() as s:
        time.sleep(0.05)
    # 阈值取宽：Windows 下 sleep 精度有限（实测 sleep(0.02) 可能只过 14ms），
    # 这里验证的是「计时器确实在计」，不是守时精度。
    assert 20 <= s.ms <= 2000, s.ms


def test_span_reports_running_elapsed_before_exit():
    import time

    s = obs.span()
    time.sleep(0.05)
    # 阈值取得宽松：这里验证的是「未退出上下文时返回当前已耗时」这个语义，
    # 不该因为守时粒度或首轮导入抖动而变成脆弱用例。
    assert s.ms >= 20, "未退出上下文时应返回从进入算起的当前耗时"
