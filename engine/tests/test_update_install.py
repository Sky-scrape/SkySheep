"""应用内一键更新：源码版拒绝下载/安装；未下载时不能直接应用。"""

from __future__ import annotations

from test_server import make_client, recv_until  # home fixture 在 conftest.py


def test_install_update_refused_on_source_build(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u1", "method": "app.install_update", "params": {}})
        r = recv_until(ws, "u1")
        assert not r["ok"] and "源码版" in r["error"]


def test_apply_update_requires_downloaded_package(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u2", "method": "app.apply_update", "params": {}})
        r = recv_until(ws, "u2")
        assert not r["ok"] and "下载" in r["error"]


def test_check_update_reports_frozen_flag(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u3", "method": "app.check_update", "params": {}})
        r = recv_until(ws, "u3")
        # 网络可达与否都行：字段必须在（TestClient 进程永远非 frozen）
        assert r["result"].get("frozen") is False
