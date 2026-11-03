"""独立预览窗（preview.open_window）与预览分栏的偏好存续。

窗口层本身靠桌面壳的真机验收（本机 WebView2 子窗口），这里锁住两件容易
回归的事：
- RPC 边界：只收 http(s)、没有桌面窗口工厂时回 mode=browser（前端因此能退回
  系统浏览器，而不是拿到一个静默失败的按钮）；
- ui.json：preview_w / preview_split / preview_url 能存回来，越界值与
  非 http(s) 的地址被丢掉。
"""

from __future__ import annotations

import asyncio

import pytest
from test_server import make_client, recv_until

from skysheep import previewwindows


@pytest.fixture(autouse=True)
def _reset_preview_factory():
    previewwindows.unregister()
    yield
    previewwindows.close_all()
    previewwindows.unregister()


def call_ws(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": method, "params": params or {}})
        return recv_until(ws, "p1")


def test_preview_window_without_desktop_returns_browser_mode(home):
    """浏览器模式 / CLI：没有窗口工厂时不让按钮变成死键，回 browser 由前端开系统浏览器。"""
    frame = call_ws(home, "preview.open_window", {"url": "http://127.0.0.1:8000/app"})
    assert frame["ok"]
    assert frame["result"]["mode"] == "browser"
    assert frame["result"]["url"] == "http://127.0.0.1:8000/app"


def test_preview_window_uses_registered_factory(home):
    seen = {}

    def factory(url, title):
        seen["url"] = url
        seen["title"] = title
        return object()  # 窗口桩：只有存活判定，不碰 events

    previewwindows.register(factory)
    frame = call_ws(home, "preview.open_window", {"url": "http://localhost:5173/"})
    assert frame["ok"]
    assert frame["result"]["mode"] == "native"
    assert seen["url"] == "http://localhost:5173/"
    assert seen["title"].startswith("预览 · ")
    # 控制器自己留强引用，供退出时统一关闭
    assert previewwindows.count() == 1


def test_preview_window_rejects_non_http_scheme(home):
    frame = call_ws(home, "preview.open_window", {"url": "javascript:alert(1)"})
    assert frame["ok"] is False
    assert "http(s)" in frame["error"]


def test_preview_prefs_roundtrip_and_validation(home):
    frame = call_ws(home, "ui.save", {
        "prefs": {
            "preview_split": 1,
            "preview_w": 640,
            "preview_url": "http://127.0.0.1:8000/preview?p=a.html",
        },
    })
    assert frame["ok"]
    prefs = frame["result"]["prefs"]
    assert prefs["preview_split"] == 1
    assert prefs["preview_w"] == 640
    assert prefs["preview_url"].endswith("/preview?p=a.html")

    frame = call_ws(home, "ui.get")
    assert frame["result"]["prefs"]["preview_w"] == 640

    # 越界宽度收敛到上限；非法协议与超长地址直接丢键
    frame = call_ws(home, "ui.save", {
        "prefs": {"preview_w": 99999, "preview_url": "javascript:alert(1)"},
    })
    assert frame["result"]["prefs"]["preview_w"] == 1400
    assert "preview_url" not in frame["result"]["prefs"]

    frame = call_ws(home, "ui.save", {"prefs": {"preview_url": "https://" + "a" * 2100}})
    assert "preview_url" not in frame["result"]["prefs"]


def test_preview_open_window_runs_off_event_loop(home, monkeypatch):
    """建 WebView2 窗会同步 Invoke 到 GUI 线程：必须挪进 executor，别卡事件循环。"""
    seen = {"loop": True}
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        def fake(url, local=True):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen["loop"] = False
            return {"mode": "browser", "url": url}

        monkeypatch.setattr(backend, "open_preview_window", fake)
        ws.send_json({"id": "pv-loop", "method": "preview.open_window",
                      "params": {"url": "http://127.0.0.1:8000/x"}})
        frame = recv_until(ws, "pv-loop")
        assert frame["ok"]
        assert seen["loop"] is False, "窗口创建仍在事件循环线程上同步执行"


def test_preview_frame_wrapper_keeps_iframe_sandbox(home):
    """独立窗装的 /preview?frame=1 必须是沙箱 iframe 壳，不能直出用户文件。"""
    proj = home / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")
    with make_client(home, []) as client:
        r = client.get("/preview", params={"p": "page.html", "frame": 1})
        assert r.status_code == 200
        assert "<iframe" in r.text and 'sandbox="' in r.text
        assert "frame-src 'self'" in r.headers["content-security-policy"]
        # 非 frame 档仍是原文件（应用内 iframe 用）
        raw = client.get("/preview", params={"p": "page.html"})
        assert raw.text == "<h1>hi</h1>"
        assert raw.headers["content-security-policy"].startswith("sandbox")
