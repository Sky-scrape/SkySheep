"""轮次诊断（审查页「这一轮为什么慢」的 UI 兑现）。

后端：diagnostics.turn_breakdown 读桌面日志里 obs.py 轮末写的 ``ev=turn``
结构化行，按会话过滤、按 limit 截断；日志文件缺失返回空而不报错。
前端：审查页「轮次诊断」块的接线锁（源码子串，套件既有惯例）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from conftest import read_app_bundle
from test_server import make_client, recv_until

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.server import app as server_app
from skysheep.server.backend import (
    TURN_LOG_LIMIT_DEFAULT,
    _load_turn_rows,
    _log_line_ts,
    _turn_row,
)

# 用例可能从任意 cwd 启动，静态资源按本文件定位成绝对路径（与 test_frontend_wiring 同源）
_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static"


def _read_static(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def _write_log(home, text):
    """把夹具日志写到隔离 home 的 logs/desktop.log（desktop.py 的固定落点）。"""
    log_dir = home / "home" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "desktop.log"
    p.write_text(text, encoding="utf-8")
    return p


# 夹具行：真实格式（desktop.py _setup_logging 的 %(asctime)s + obs 的 |json| 尾巴）。
# 覆盖：全字段行（中文消息）、缺字段行、别的会话、非 turn 事件、纯文本行、坏 JSON。
FIXTURE_LOG = "\n".join([
    '2026-10-02 09:15:03,123 INFO skysheep.obs: 轮次结束 sid=s1 |json| '
    '{"ev":"turn","session_id":"s1","duration_ms":17000,"tool_calls":3,"tool_ms":2100,'
    '"tool_errors":1,"slowest_tool":"web_fetch","slowest_tool_ms":1800,'
    '"permission_waits":1,"permission_ms":800,"in_tokens":1200,"out_tokens":340}',
    '2026-10-02 09:16:10,456 INFO skysheep.obs: turn finished sid=s1 |json| '
    '{"ev":"turn","session_id":"s1"}',
    '2026-10-02 09:17:00,000 INFO skysheep.obs: turn finished sid=s2 |json| '
    '{"ev":"turn","session_id":"s2","duration_ms":900}',
    '2026-10-02 09:18:00,000 INFO skysheep.obs: 站点信誉计数写盘失败 |json| '
    '{"ev":"web_reputation_save_failed","err":"disk full"}',
    '2026-10-02 09:19:00,000 INFO skysheep.obs: 服务就绪',
    '2026-10-02 09:20:00,000 INFO skysheep.obs: turn finished sid=s1 |json| '
    '{"ev":"turn","session_id":"s1","broken',
]) + "\n"


# ---------- 行解析：字段齐全 / 缺字段容忍 / 时间戳 ----------

def test_turn_row_parses_full_line_with_chinese_message():
    line = FIXTURE_LOG.splitlines()[0]
    fields = _turn_row(line, json.loads(line.split(" |json| ")[1]))
    assert fields["duration_ms"] == 17000
    assert fields["tool_calls"] == 3
    assert fields["tool_ms"] == 2100
    assert fields["tool_errors"] == 1
    assert fields["slowest_tool"] == "web_fetch"
    assert fields["slowest_tool_ms"] == 1800
    assert fields["permission_waits"] == 1
    assert fields["permission_ms"] == 800
    assert fields["in_tokens"] == 1200
    assert fields["out_tokens"] == 340
    assert fields["error"] is None and fields["stopped"] is None


def test_turn_row_tolerates_missing_fields_as_null():
    """旧日志行只有 ev/session_id：其余字段一律 null，不抛异常。"""
    line = FIXTURE_LOG.splitlines()[1]
    row = _turn_row(line, {"ev": "turn", "session_id": "s1"})
    for key in ("duration_ms", "tool_calls", "tool_ms", "tool_errors", "slowest_tool",
                "slowest_tool_ms", "permission_waits", "permission_ms",
                "in_tokens", "out_tokens", "stopped", "error"):
        assert row[key] is None, key
    assert row["ts"] is not None  # 时间从行首 asctime 取，与 JSON 字段无关


def test_log_line_ts_parses_asctime_with_millis_as_local_epoch():
    ts = _log_line_ts("2026-10-02 09:15:03,123 INFO skysheep.obs: x |json| {}")
    assert ts is not None
    back = datetime.fromtimestamp(ts)
    assert (back.hour, back.minute, back.second) == (9, 15, 3)
    assert round((ts - int(ts)) * 1000) == 123
    # 没有毫秒段 / 根本不是日志行
    assert _log_line_ts("2026-10-02 09:15:03 INFO x") is not None
    assert _log_line_ts("不是日志行") is None


# ---------- 文件读取：会话过滤 / 非turn与坏行跳过 / 文件缺失 ----------

def test_load_turn_rows_filters_session_and_skips_non_turn_lines(home):
    p = _write_log(home, FIXTURE_LOG)
    rows = _load_turn_rows(p, "s1")
    assert len(rows) == 2, "s1 应有两条（坏 JSON 行与非 turn 行都不算）"
    assert rows[0]["duration_ms"] == 17000
    assert rows[1]["duration_ms"] is None
    # 别的会话过滤得掉
    assert len(_load_turn_rows(p, "s2")) == 1
    assert _load_turn_rows(p, "nope") == []


def test_load_turn_rows_missing_file_returns_empty(home, tmp_path):
    assert _load_turn_rows(home / "home" / "logs" / "desktop.log", "s1") == []
    assert _load_turn_rows(tmp_path / "不存在.log", "s1") == []


def test_turn_breakdown_missing_log_is_empty_result(home):
    """日志不存在（如 CLI 模式没接文件日志）：ok 结果 + 空列表，不是报错。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "diagnostics.turn_breakdown",
                      "params": {"session_id": "s1"}})
        r = recv_until(ws, "d1")
        assert r["ok"], r
        assert r["result"]["turns"] == []


# ---------- WS 接线：limit 截断与缺省 ----------

def _send_turn_lines(n, sid="s1"):
    out = []
    for i in range(n):
        out.append(
            f"2026-10-02 10:{i // 60:02d}:{i % 60:02d},000 INFO skysheep.obs: "
            f"turn finished sid={sid} |json| "
            f'{{"ev":"turn","session_id":"{sid}","duration_ms":{1000 + i}}}'
        )
    return "\n".join(out) + "\n"


def test_turn_breakdown_ws_limit_and_filter(home):
    _write_log(home, _send_turn_lines(25) + _send_turn_lines(1, sid="s2"))
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "diagnostics.turn_breakdown",
                      "params": {"session_id": "s1", "limit": 2}})
        r = recv_until(ws, "d1")["result"]
        assert r["session_id"] == "s1"
        assert [t["duration_ms"] for t in r["turns"]] == [1023, 1024], "limit 取最近的 N 条"

        ws.send_json({"id": "d2", "method": "diagnostics.turn_breakdown",
                      "params": {"session_id": "s1"}})
        r2 = recv_until(ws, "d2")["result"]
        assert len(r2["turns"]) == TURN_LOG_LIMIT_DEFAULT == 20

        ws.send_json({"id": "d3", "method": "diagnostics.turn_breakdown",
                      "params": {"session_id": "s1", "limit": "abc"}})
        assert len(recv_until(ws, "d3")["result"]["turns"]) == 20, "非法 limit 回退缺省"

        ws.send_json({"id": "d4", "method": "diagnostics.turn_breakdown",
                      "params": {"session_id": "s1", "limit": 99999}})
        r4 = recv_until(ws, "d4")["result"]
        assert len(r4["turns"]) == 25, "上限截断后仍能取回全部会话内轮次"

        ws.send_json({"id": "d5", "method": "diagnostics.turn_breakdown",
                      "params": {"session_id": "s2"}})
        r5 = recv_until(ws, "d5")["result"]
        assert len(r5["turns"]) == 1 and r5["turns"][0]["duration_ms"] == 1000


# ---------- 远程收窄（B13 同款）：远端只看当前活动会话 ----------

def test_turn_breakdown_remote_narrows_to_active_session(home, monkeypatch):
    """远程连接忽略传入 sid、收窄到当前活动会话。

    诊断行含时间/耗时/工具名/token，不收窄的话 LAN 持令牌的远程客户端
    可凭任意 sid 读到别会话的轮次度量，还能借 turns 空/非空探测某 id
    是否在本机日志里出现过。
    """
    _write_log(home, _send_turn_lines(2, sid="s1") + _send_turn_lines(1, sid="s2"))
    # 第一个连接算本机，之后的连接算远程（_client_is_local 按调用次序分流）
    calls = {"n": 0}

    def fake_local(client):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(server_app, "_client_is_local", fake_local)
    with make_client(home, [[TextBlock(text="好的")]]) as client, \
         client.websocket_connect("/ws") as ws_local, \
         client.websocket_connect("/ws") as ws_remote:
        ws_remote.send_json({"id": "b1", "method": "boot"})
        assert recv_until(ws_remote, "b1")["ok"]
        # 本机先跑一轮：懒创建出活动会话（远程收窄的目标）
        ws_local.send_json({"id": "c1", "method": "chat.send", "params": {"text": "hi"}})
        frame = recv_until(ws_local, "c1")
        assert frame["ok"], frame
        active_sid = frame["result"]["session_id"]
        assert active_sid not in ("s1", "s2")

        # 远程传别人的 sid：被忽略，按活动会话返回
        ws_remote.send_json({"id": "r1", "method": "diagnostics.turn_breakdown",
                             "params": {"session_id": "s2"}})
        r = recv_until(ws_remote, "r1")["result"]
        assert r["session_id"] == active_sid
        assert r["turns"] == [], "日志里只有 s1/s2 的行，活动会话查不到它们的轮次"

        # 本机连接不受影响：照常按传入 sid 查
        ws_local.send_json({"id": "d1", "method": "diagnostics.turn_breakdown",
                            "params": {"session_id": "s2"}})
        r2 = recv_until(ws_local, "d1")["result"]
        assert r2["session_id"] == "s2" and len(r2["turns"]) == 1


# ---------- 轮末结构化日志带 slowest_tool（本轮最慢工具） ----------

def test_turn_log_records_slowest_tool(home, caplog):
    """真实跑一轮带工具调用的对话：轮末 obs 行里应有最慢工具的名字。"""
    script = [
        [ToolUseBlock(id="t1", name="list_dir", input={})],
        [TextBlock(text="看完了")],
    ]
    with caplog.at_level(logging.INFO, logger="skysheep.obs"):
        with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "看看目录"}})
            # 读到 c1 回执为止（回执在轮末收尾之后，此时 obs 行已落）；顺路取 session_id
            sid = None
            done = False
            while not done:
                fr = ws.receive_json()
                if "event" in fr:
                    sid = fr["data"].get("session_id") or sid
                elif fr.get("id") == "c1":
                    assert fr["ok"], fr
                    done = True
    payloads = [
        p for p in (_parse_record(r) for r in caplog.records)
        if p and p.get("ev") == "turn"
    ]
    assert payloads, "轮末应写一条 ev=turn 结构化日志"
    p = payloads[-1]
    assert p.get("session_id") == sid
    assert p.get("slowest_tool") == "list_dir", p
    assert isinstance(p.get("tool_calls"), int) and p["tool_calls"] >= 1


def _parse_record(record):
    from skysheep.obs import parse_structured

    try:
        return parse_structured(record.getMessage())
    except Exception:  # noqa: BLE001 - 别的 logger 的记录直接跳过
        return None


# ---------- 前端接线（源码子串锁，套件既有惯例） ----------

def test_frontend_review_page_has_turn_diag_block():
    js = read_app_bundle()
    assert 'request("diagnostics.turn_breakdown"' in js, "缺 WS 调用"
    assert "function refreshTurnDiag" in js
    assert "function fmtMs" in js
    assert 'getElementById("review-diag-refresh").onclick' in js
    # 审查列表刷新时一起重拉（切会话/自动刷新才不会各走各的）
    assert "refreshTurnDiag();" in js.split("async function refreshTurnDiag")[0]
    html = _read_static("index.html")
    assert 'id="review-diag"' in html
    assert 'id="review-diag-refresh"' in html
    css = _read_static("app.css")
    assert ".diag-row" in css
    assert "#review-diag" in css


def test_dispatch_registry_has_turn_breakdown():
    assert "diagnostics.turn_breakdown" in server_app._WS_METHODS
