"""窗口配色：标题栏 / 窗口底色的主题取值（切项目整页刷新时露出的那一层）。

只测纯函数与文件读取，不碰真实窗口：apply_caption_theme 需要 HWND，
在无窗口的测试环境里本来就会静默返回 False。
"""

from __future__ import annotations

import json

from skysheep import wintheme


def test_window_background_follows_theme():
    """窗口底色跟主题：默认白底在整页刷新时会露出来，必须等于 app.css 的 --bg。"""
    assert wintheme.window_background("light") == "#E8DFC7"
    assert wintheme.window_background("dark") == "#1D1A16"


def test_window_background_uses_current_mode_by_default():
    wintheme.set_theme_mode("dark")
    assert wintheme.window_background() == "#1D1A16"
    wintheme.set_theme_mode("light")
    assert wintheme.window_background() == "#E8DFC7"


def test_read_ui_theme_explicit_modes(home):
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "dark"
    (base / "ui.json").write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "light"


def test_read_ui_theme_falls_back_to_light_when_missing_or_broken(home):
    """读不到/损坏一律按 light，绝不让窗口创建失败。"""
    assert wintheme.read_ui_theme(home / "nope") == "light"
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text("{ not json", encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "light"


def test_read_ui_theme_auto_resolves_via_system(home, monkeypatch):
    """auto 跟随系统：按 AppsUseLightTheme 解析（这里注入判定结果，不依赖真实注册表）。"""
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text(json.dumps({"theme": "auto"}), encoding="utf-8")
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: True)
    assert wintheme.read_ui_theme(base) == "dark"
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: False)
    assert wintheme.read_ui_theme(base) == "light"


def test_register_window_and_refresh_now(monkeypatch):
    """事件驱动重刷：注册窗口后 refresh_now 按当前主题重绘；未注册时 no-op。"""
    monkeypatch.setattr(wintheme, "_registered_window", None)
    assert wintheme.refresh_now() is False  # 没有窗口（浏览器模式）：no-op

    painted = []

    class _FakeWindow:
        native = None  # 无原生句柄：apply/BackColor 路径都会静默跳过

    monkeypatch.setattr(wintheme, "apply_caption_theme",
                        lambda w, dark=False: painted.append(("caption", dark)) or True)
    wintheme.register_window(_FakeWindow())
    wintheme.set_theme_mode("dark")
    assert wintheme.refresh_now() is True
    assert painted == [("caption", True)]
    wintheme.set_theme_mode("light")
    assert wintheme.refresh_now() is True
    assert painted == [("caption", True), ("caption", False)]
