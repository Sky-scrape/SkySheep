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
        # 完全访问档放得更开，同样只许本机切换
        ws.send_json({"id": "m1b", "method": "permission.set_mode",
                      "params": {"mode": "full_access"}})
        frame = recv_until(ws, "m1b")
        assert frame["ok"] is False and "本机" in frame["error"]
        ws.send_json({"id": "m2", "method": "permission.mode"})
        assert recv_until(ws, "m2")["result"]["mode"] == "confirm"
        # 也不允许通过 ui.save 间接写入 accept_edits（含完全访问的 2 档）
        ws.send_json({"id": "u1", "method": "ui.save",
                      "params": {"prefs": {"accept_edits": 2, "sidebar_w": 300}}})
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


# ---- M3：permission.respond 的 decision 白名单（未知值按拒绝，不是按放行） ----


def test_normalize_decision_is_fail_closed():
    """白名单外的 decision 一律收敛成 deny：主循环只显式处理 ALLOW_ALWAYS/DENY，
    其余值都会落到「执行工具」那条路上，所以透传等于「乱码 = 放行」。"""
    from skysheep.security.gate import Decision, normalize_decision

    assert normalize_decision("allow_once") == Decision.ALLOW_ONCE
    assert normalize_decision("allow_always") == Decision.ALLOW_ALWAYS
    assert normalize_decision("deny") == Decision.DENY
    assert normalize_decision("  deny  ") == Decision.DENY  # 前后空白无妨
    assert normalize_decision("DENY") == Decision.DENY      # 大小写归一
    for bad in ("allow", "allowalways", "yes", "y", "ok", "true", "", "deny ",
                "None", "null", "\x00", None, 1, True, ["allow_once"], {"d": "allow_once"}):
        assert normalize_decision(bad) == Decision.DENY, repr(bad)


async def test_agent_respond_permission_denies_unknown(tmp_path):
    """Agent.respond_permission 落地的就是白名单值：未知值投递后按拒绝执行。"""
    from conftest import FakeProvider

    from skysheep.core import Agent
    from skysheep.messages import TextBlock, ToolUseBlock
    from skysheep.security.gate import Decision, PermissionGate
    from skysheep.tools import ToolRegistry, WriteFileTool

    prov = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file",
                      input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="done")],
    ])
    agent = Agent(provider=prov, registry=ToolRegistry([WriteFileTool()]),
                  gate=PermissionGate(), working_dir=tmp_path, max_iterations=5)
    resolved = []
    async for ev in agent.run_turn("写文件"):
        if ev.kind == "permission_request":
            # 前端回了个白名单外的字符串（大小写差异 / 乱码 / 被篡改）
            assert agent.respond_permission(ev.request_id, "Allow_Once_Typo")
        elif ev.kind == "permission_resolved":
            resolved.append(ev.decision)
    assert resolved == [Decision.DENY]
    assert not (tmp_path / "a.txt").exists(), "未知 decision 不得被当成放行执行"


def test_ws_permission_respond_unknown_decision_denies(home):
    """端到端：WS 上回未知 decision → 工具不执行，事件里如实报 deny。"""
    from skysheep.messages import TextBlock, ToolUseBlock

    script = [
        [ToolUseBlock(id="t1", name="write_file",
                      input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="done")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写文件"}})
        rid, decisions = None, []
        while True:
            frame = ws.receive_json()
            if frame.get("event") == "permission_request":
                rid = frame["data"]["request_id"]
                ws.send_json({"id": "pr", "method": "permission.respond",
                              "params": {"request_id": rid, "decision": "allow_always!"}})
            elif frame.get("event") == "permission_resolved":
                decisions.append(frame["data"]["decision"])
            elif frame.get("id") == "c1":
                break
        assert decisions == ["deny"]
        assert not (home / "proj" / "a.txt").exists()
