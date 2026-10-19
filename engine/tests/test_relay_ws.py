"""接力运行期 WS 契约：本机 RPC 闭环、幂等、严格参数、远程拒绝。

走进程内 ASGI 传输（与 test_relay_settings 同一夹具），非真实 socket e2e。
"""
import time

import pytest
from test_server import make_client, recv_until

from skysheep.messages import TextBlock

_WORKFLOW = {
    "start_node": "n1",
    "nodes": [
        {"id": "n1", "slot": "A", "prompt": "研究", "terminal": False},
        {"id": "n2", "slot": "B", "prompt": "复核", "terminal": True},
    ],
    "edges": [{"source": "n1", "target": "n2"}],
}


def rpc(ws, method, params=None):
    ws.send_json({"id": method, "method": method, "params": params or {}})
    return recv_until(ws, method)


def _poll(ws, task_id, state, tries=200):
    for _ in range(tries):
        resp = rpc(ws, "relay.get", {"task_id": task_id})
        if resp["ok"] and resp["result"]["task"]["state"] == state:
            return resp["result"]
        time.sleep(0.01)
    raise AssertionError(f"任务未进入状态 {state}")


def test_ws_relay_serial_flow(home):
    from conftest import FakeProvider

    provider = FakeProvider([
        [TextBlock(text="A 报告")], [TextBlock(text="B 报告")],
    ])
    with make_client(home, [], provider=provider) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        sid = recv_until(ws, "n1")["result"]["id"]

        started = rpc(ws, "relay.start", {
            "session_id": sid, "goal": "目标", "workflow": _WORKFLOW,
            "request_id": "ws-start", "expected_revision": 0,
        })
        assert started["ok"], started
        task_id = started["result"]["task"]["id"]

        snap = _poll(ws, task_id, "waiting_confirmation")
        n1 = next(n for n in snap["nodes"] if n["node_id"] == "n1")
        assert n1["report"] == "A 报告"

        approved = rpc(ws, "relay.approve", {
            "task_id": task_id, "request_id": "ws-approve",
            "expected_revision": snap["task"]["revision"],
            "report_id": n1["report_id"], "report_hash": n1["report_hash"],
        })
        assert approved["ok"], approved
        snap = _poll(ws, task_id, "completed")
        assert next(n for n in snap["nodes"] if n["node_id"] == "n2")["report"] == "B 报告"

        listed = rpc(ws, "relay.list", {})
        assert listed["ok"]
        assert any(t["id"] == task_id for t in listed["result"]["tasks"])

        # 启动幂等：同一 request_id 返回同一任务
        again = rpc(ws, "relay.start", {
            "session_id": sid, "goal": "目标", "workflow": _WORKFLOW,
            "request_id": "ws-start", "expected_revision": 0,
        })
        assert again["ok"] and again["result"].get("replayed")
        assert again["result"]["task"]["id"] == task_id


def test_ws_relay_strict_params(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        bad = rpc(ws, "relay.start", {"goal": "x", "session_id": "s", "surprise": 1})
        assert not bad["ok"] and "不支持" in bad["error"]
        missing = rpc(ws, "relay.stop", {"task_id": "abc", "expected_revision": 0})
        assert not missing["ok"] and "request_id" in missing["error"]


@pytest.mark.parametrize(
    "method", ["relay.list", "relay.get", "relay.start", "relay.approve",
               "relay.pause", "relay.resume", "relay.stop",
               "relay.dual_open", "relay.dual_save", "relay.dual_get",
               "relay.dual_run", "relay.dual_stop"]
)
def test_ws_relay_remote_forbidden(home, method, monkeypatch):
    monkeypatch.setattr("skysheep.server.app._client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        response = rpc(ws, method, {})
    assert not response["ok"]
    assert "本机" in response["error"]
