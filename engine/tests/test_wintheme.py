"""窗口配色：标题栏 / 窗口底色的主题取值（切项目整页刷新时露出的那一层）。

只测纯函数与文件读取，不碰真实窗口：apply_caption_theme 需要 HWND，
在无窗口的测试环境里本来就会静默返回 False。
"""

from __future__ import annotations

import json

from skysheep import wintheme


def test_window_background_follows_theme_per_theme():
    """窗口底色逐主题精确取值（默认白底在整页刷新时会露出来）。"""
    assert wintheme.window_background("paper") == "#E8DFC7"
    assert wintheme.window_background("celadon") == "#DCE4DA"
    assert wintheme.window_background("kaki") == "#EAD9BF"
    assert wintheme.window_background("night") == "#171410"
    assert wintheme.window_background("indigo") == "#12161F"
    assert wintheme.window_background("pine") == "#101613"
    # 旧版两档值映射到默认浅色 / 深色（纸墨 / 夜墨）
    assert wintheme.window_background("light") == "#E8DFC7"
    assert wintheme.window_background("dark") == "#171410"


def test_window_background_uses_current_mode_by_default():
    wintheme.set_theme_mode("night")
    assert wintheme.window_background() == "#171410"
    wintheme.set_theme_mode("paper")
    assert wintheme.window_background() == "#E8DFC7"


def test_theme_is_dark_by_family():
    for key in ("night", "indigo", "pine", "dark"):
        assert wintheme.theme_is_dark(key), key
    for key in ("paper", "celadon", "kaki", "light"):
        assert not wintheme.theme_is_dark(key), key


def test_read_ui_theme_returns_theme_id(home):
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text(json.dumps({"theme": "kaki"}), encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "kaki"
    # 旧版两档值分别映射纸墨 / 夜墨
    (base / "ui.json").write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "paper"
    (base / "ui.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "night"


def test_read_ui_theme_all_theme_ids(home):
    """六套主题 id 直接命中，不再落进「跟随系统」兜底。"""
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    for theme_id in ("paper", "celadon", "kaki", "night", "indigo", "pine"):
        (base / "ui.json").write_text(json.dumps({"theme": theme_id}), encoding="utf-8")
        assert wintheme.read_ui_theme(base) == theme_id


def test_read_ui_theme_falls_back_to_paper_when_missing_or_broken(home):
    """读不到/损坏一律按 paper 浅色，绝不让窗口创建失败、也不探注册表。"""
    assert wintheme.read_ui_theme(home / "nope") == "paper"
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text("{ not json", encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "paper"


def test_read_ui_theme_auto_resolves_via_system(home, monkeypatch):
    """auto 跟随系统：按 AppsUseLightTheme 解析（这里注入判定结果，不依赖真实注册表）。"""
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text(json.dumps({"theme": "auto"}), encoding="utf-8")
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: True)
    assert wintheme.read_ui_theme(base) == "night"
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: False)
    assert wintheme.read_ui_theme(base) == "paper"


def test_register_window_and_refresh_now(monkeypatch):
    """事件驱动重刷：注册窗口后 refresh_now 按当前主题重绘；未注册时 no-op。"""
    monkeypatch.setattr(wintheme, "_registered_window", None)
    assert wintheme.refresh_now() is False  # 没有窗口（浏览器模式）：no-op

    painted = []

    class _FakeWindow:
        native = None  # 无原生句柄：apply/BackColor 路径都会静默跳过

    monkeypatch.setattr(
        wintheme,
        "apply_caption_theme",
        lambda w: painted.append(wintheme.current_theme_mode()) or True,
    )
    wintheme.register_window(_FakeWindow())
    wintheme.set_theme_mode("night")
    assert wintheme.refresh_now() is True
    assert painted == ["night"]
    wintheme.set_theme_mode("paper")
    assert wintheme.refresh_now() is True
    assert painted == ["night", "paper"]


def test_read_ui_theme_auto_mapping(home, monkeypatch):
    """auto 的深浅落点可配（theme_auto_light / theme_auto_dark），非法值回退纸墨/夜墨。"""
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text(
        json.dumps({"theme": "auto", "theme_auto_light": "celadon", "theme_auto_dark": "pine"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: False)
    assert wintheme.read_ui_theme(base) == "celadon"
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: True)
    assert wintheme.read_ui_theme(base) == "pine"
    # 非法/缺失映射：回默认纸墨 / 夜墨
    (base / "ui.json").write_text(
        json.dumps({"theme": "auto", "theme_auto_light": "neon", "theme_auto_dark": ""}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: False)
    assert wintheme.read_ui_theme(base) == "paper"
    monkeypatch.setattr(wintheme, "_system_prefers_dark", lambda: True)
    assert wintheme.read_ui_theme(base) == "night"
    # 选中具体主题时映射不参与
    (base / "ui.json").write_text(
        json.dumps({"theme": "kaki", "theme_auto_dark": "pine"}), encoding="utf-8"
    )
    assert wintheme.read_ui_theme(base) == "kaki"
