"""结构化日志（obs）测试。

目标：既有文本日志的阅读方式不受影响，同时能用固定标记切出机器可读的字段。
"""

from __future__ import annotations

import json
import logging

from skysheep import obs


def _capture(caplog):
    """把 skysheep.obs 的记录收进 caplog。"""
    return caplog.at_level(logging.INFO, logger="skysheep.obs")


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
    # 通过公开接口验证：None 不该出现
    import io
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    lg = logging.getLogger("skysheep.obs")
    old_handlers, old_level = lg.handlers, lg.level
    lg.handlers, lg.level = [handler], logging.INFO
    try:
        obs.info("turn", "t", session_id="s1", error=None)
    finally:
        lg.handlers, lg.level = old_handlers, old_level
    payload = obs.parse_structured(stream.getvalue().strip())
    assert "error" not in payload
    assert payload["session_id"] == "s1"


def test_warning_uses_warning_level(caplog):
    with caplog.at_level(logging.WARNING, logger="skysheep.obs"):
        obs.warning("mcp", "server down", server="fetch")
    assert caplog.records[-1].levelno == logging.WARNING
    assert obs.parse_structured(caplog.records[-1].getMessage())["server"] == "fetch"


def test_unserializable_field_does_not_raise():
    """字段塞进不可序列化对象时，日志不能把主流程带崩。"""
    import io
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    lg = logging.getLogger("skysheep.obs")
    old_handlers, old_level = lg.handlers, lg.level
    lg.handlers, lg.level = [handler], logging.INFO
    try:
        obs.info("turn", "weird", obj=object())
    finally:
        lg.handlers, lg.level = old_handlers, old_level
    payload = obs.parse_structured(stream.getvalue().strip())
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
