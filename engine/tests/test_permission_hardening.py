"""权限加固的端到端覆盖：本机专属开关 + 命令拼接不再被前缀白名单放行。

WS 层用 monkeypatch 模拟「局域网远端客户端」：starlette TestClient 的 scope client
固定是 ("testclient", 50000)，无法从外部换成局域网地址，所以直接把来源判定函数
打成 False，验证 dispatch 层的拦截行为。
"""

from __future__ import annotations

from test_server import make_client, recv_until  # noqa: F401

import skysheep.server.app as server_app
from skysheep.server.app import _client_is_local


def test_client_is_local_detects_loopback_and_lan():
    """环回（含 IPv6 与 IPv4 映射地址）算本机；局域网地址不算。"""
    assert _client_is_local(("127.0.0.1", 5000))
    assert _client_is_local(("::1", 5000))
    assert _client_is_local(("::ffff:127.0.0.1", 5000))
    assert _client_is_local(("testclient", 50000))  # 测试客户端
    assert _client_is_local(("localhost", 5000))
    assert not _client_is_local(("192.168.1.20", 5000))
    assert not _client_is_local(("10.0.0.7", 5000))
    assert not _client_is_local(None)
    assert not _client_is_local(("not-an-ip", 1))


def test_remote_client_cannot_enable_accept_edits(home, monkeypatch):
    """远端调用 permission.set_mode 开自动写入档：直接报错，档位不变。"""
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "permission.set_mode",
                      "params": {"mode": "accept_edits"}})
        frame = recv_until(ws, "m1")
        assert frame["ok"] is False and "本机" in frame["error"]
        ws.send_json({"id": "m2", "method": "permission.mode"})
        assert recv_until(ws, "m2")["result"]["mode"] == "confirm"
        # 也不允许通过 ui.save 间接写入 accept_edits
        ws.send_json({"id": "u1", "method": "ui.save",
                      "params": {"prefs": {"accept_edits": 1, "sidebar_w": 300}}})
        prefs = recv_until(ws, "u1")["result"]["prefs"]
        assert "accept_edits" not in prefs and prefs.get("sidebar_w") == 300
        assert "accept_edits" not in (home / "home" / "ui.json").read_text(encoding="utf-8")


def test_remote_client_can_still_switch_back_to_confirm(home, monkeypatch):
    """远端也能把档位收回确认档（只限制放宽，不限制收紧）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "permission.set_mode",
                      "params": {"mode": "accept_edits"}})
        assert recv_until(ws, "m1")["result"]["mode"] == "accept_edits"
    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m2", "method": "permission.set_mode",
                      "params": {"mode": "confirm"}})
        assert recv_until(ws, "m2")["result"]["mode"] == "confirm"


def test_permission_switch_is_logged(home, caplog):
    """降防护动作留痕：切到 accept_edits 会写一条 INFO 日志。"""
    import logging

    with caplog.at_level(logging.INFO, logger="skysheep.security"):
        with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "m1", "method": "permission.set_mode",
                          "params": {"mode": "accept_edits"}})
            assert recv_until(ws, "m1")["result"]["mode"] == "accept_edits"
    assert any("权限模式" in r.getMessage() for r in caplog.records)


def test_accept_edits_only_covers_workdir_writes(home):
    """端到端：开自动写入后，目录内写入免确认，目录外写入仍弹确认。"""
    from skysheep.messages import TextBlock, ToolUseBlock

    outside = home / "outside.txt"
    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "inside.txt", "content": "1"})],
        [ToolUseBlock(id="t2", name="write_file",
                      input={"path": str(outside), "content": "2"})],
        [TextBlock(text="done")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "permission.set_mode",
                      "params": {"mode": "accept_edits"}})
        recv_until(ws, "m1")
        events = []
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写两个文件"}})
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                events.append(frame)
                if frame["event"] == "permission_request":
                    assert frame["data"]["tool_name"] == "write_file"
                    # 弹出来的必须是目录外那次
                    assert frame["data"]["input"]["path"] == str(outside)
                    ws.send_json({"id": "pr", "method": "permission.respond",
                                  "params": {"request_id": frame["data"]["request_id"],
                                             "decision": "allow_once"}})
                continue
            if frame.get("id") == "c1":
                break
        kinds = [e["event"] for e in events]
        assert kinds.count("permission_request") == 1
        assert (home / "proj" / "inside.txt").read_text(encoding="utf-8") == "1"
        assert outside.read_text(encoding="utf-8") == "2"
