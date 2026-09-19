"""窗口几何记忆：退出时保存、启动时恢复上次的窗口大小与位置。

只测纯函数与文件读写，不碰真实窗口：Win32 采集（GetWindowPlacement 等）
在 desktop.py 里，需要真窗口句柄，测试环境本来就静默失败。
"""

from __future__ import annotations

import json

from skysheep import windowstate

SCREENS = [(0, 0, 1920, 1040), (1920, 0, 1280, 720)]  # 主屏 + 右侧副屏（逻辑像素）
MIN_SIZE = (980, 640)


def test_state_path_uses_home_argument(home):
    assert windowstate.state_path(home / "home") == home / "home" / "window_state.json"


def test_save_and_load_roundtrip(home):
    base = home / "home"
    geometry = {"width": 1360, "height": 860, "x": 120, "y": 80, "maximized": False}
    assert windowstate.save_state(geometry, base) is True
    assert windowstate.load_state(base) == geometry


def test_load_state_missing_or_broken_returns_none(home):
    """读不到 / 不是 JSON / 不是对象 / 尺寸非法，一律回 None 走默认窗口逻辑。"""
    base = home / "home"
    assert windowstate.load_state(base) is None
    base.mkdir(parents=True)
    (base / "window_state.json").write_text("{broken", encoding="utf-8")
    assert windowstate.load_state(base) is None
    (base / "window_state.json").write_text('["width"]', encoding="utf-8")
    assert windowstate.load_state(base) is None
    (base / "window_state.json").write_text(
        json.dumps({"width": -5, "height": 600}), encoding="utf-8"
    )
    assert windowstate.load_state(base) is None


def test_load_state_without_position_keeps_size(home):
    """x/y 缺失时仍恢复尺寸，位置交给 pywebview 居中。"""
    base = home / "home"
    base.mkdir(parents=True)
    (base / "window_state.json").write_text(
        json.dumps({"width": 1200, "height": 700}), encoding="utf-8"
    )
    state = windowstate.load_state(base)
    assert state == {
        "width": 1200,
        "height": 700,
        "x": None,
        "y": None,
        "maximized": False,
    }


def test_resolve_geometry_without_state_uses_defaults(home):
    got = windowstate.resolve_geometry(None, (1360, 860), SCREENS, MIN_SIZE)
    assert got == (1360, 860, None, None, False)


def test_resolve_geometry_restores_valid_state():
    state = {"width": 1200, "height": 700, "x": 2000, "y": 100, "maximized": False}
    got = windowstate.resolve_geometry(state, (1360, 860), SCREENS, MIN_SIZE)
    assert got == (1200, 700, 2000, 100, False)


def test_resolve_geometry_rejects_size_below_min():
    """比最小尺寸还小的记录视为损坏，整体回默认。"""
    state = {"width": 400, "height": 300, "x": 10, "y": 10, "maximized": False}
    got = windowstate.resolve_geometry(state, (1360, 860), SCREENS, MIN_SIZE)
    assert got == (1360, 860, None, None, False)


def test_resolve_geometry_drops_offscreen_position():
    """位置完全落在任何屏幕之外（显示器拔掉）时丢弃位置、保留尺寸。"""
    state = {"width": 1200, "height": 700, "x": 9000, "y": 5000, "maximized": False}
    got = windowstate.resolve_geometry(state, (1360, 860), SCREENS, MIN_SIZE)
    assert got == (1200, 700, None, None, False)


def test_resolve_geometry_keeps_mostly_visible_window():
    """窗口大部分可见只算有效：负坐标拖出一半（>= MIN_VISIBLE 仍可见）保留。"""
    state = {"width": 1200, "height": 700, "x": -500, "y": -200, "maximized": False}
    got = windowstate.resolve_geometry(state, (1360, 860), SCREENS, MIN_SIZE)
    assert got == (1200, 700, -500, -200, False)


def test_resolve_geometry_keeps_maximized_flag():
    """最大化关的窗口：按 normal 态几何 + maximized 标志恢复。"""
    state = {"width": 1360, "height": 860, "x": 100, "y": 60, "maximized": True}
    got = windowstate.resolve_geometry(state, (1360, 860), SCREENS, MIN_SIZE)
    assert got == (1360, 860, 100, 60, True)


def test_save_state_fails_silently(home):
    """写不进去（父目录被文件占位）只返回 False，退出路径上绝不抛异常。"""
    base = home / "occupied"
    base.write_text("not a dir", encoding="utf-8")
    assert windowstate.save_state({"width": 100, "height": 100}, base) is False
