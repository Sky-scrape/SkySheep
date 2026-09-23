"""应用内一键更新：源码版拒绝下载/安装；未下载时不能直接应用。"""

from __future__ import annotations

import pytest
from test_server import make_client, recv_until  # home fixture 在 conftest.py

import skysheep.server.app as server_app
from skysheep.server.app import LOCAL_ONLY_METHODS


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


def test_update_methods_are_local_only(home, monkeypatch):
    """M1：持令牌的局域网设备不得远程触发「下载安装包 + 静默安装 + 强退」。

    只读的 app.check_update 不在门禁表里：它不拉起任何本机进程。
    """
    assert {"app.install_update", "app.apply_update"} <= LOCAL_ONLY_METHODS
    assert "app.check_update" not in LOCAL_ONLY_METHODS

    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i, method in enumerate(("app.install_update", "app.apply_update")):
            ws.send_json({"id": f"r{i}", "method": method, "params": {}})
            r = recv_until(ws, f"r{i}")
            assert r["ok"] is False, method
            assert "本机" in r["error"], method


def test_release_tag_is_sanitized_before_reaching_paths():
    """M8：tag 会拼进本地文件路径与 `cmd /c` 命令行，必须先过白名单。

    更新源被控（或 DNS 劫持）时，带 `"` `&` 的 tag 能借 helper 命令注入，
    带 `/` `\\` `..` 的 tag 能把安装包写到 %TEMP% 之外。
    """
    from skysheep.core.uptodate import RELEASES_LATEST, is_safe_tag, release_from_redirect

    for bad in (
        'v1.0"&calc.exe&"',       # 引号闭合 + 命令拼接
        "v1.0/../../evil",        # 路径穿越
        "v1.0\\..\\evil",          # Windows 分隔符
        "v1.0%22",                # URL 编码的引号（unquote 后才露馅）
        "v1.0 `whoami`",          # 空格 + 反引号
        "",                       # 空
        "v" + "0" * 80,           # 过长
    ):
        assert not is_safe_tag(bad), bad
        with pytest.raises(RuntimeError):
            release_from_redirect(
                RELEASES_LATEST, f"/Sky-scrape/SkySheep/releases/tag/{bad}")

    # 正常发布 tag 不受影响（含预发布后缀与 build 元数据）
    for good in ("v1.7", "v0.6.0-beta1", "v1.7.0+build.2"):
        assert is_safe_tag(good), good
    info = release_from_redirect(RELEASES_LATEST, "/Sky-scrape/SkySheep/releases/tag/v1.7")
    assert info["setup_url"].endswith("/v1.7/SkySheep-1.7-setup.exe")
