"""桌面窗口几何记忆：退出时保存、下次启动恢复上次的窗口大小与位置。

窗口几何存在 ``~/.skysheep/window_state.json``，刻意独立于 ui.json：
ui.json 由服务端聚合写入（备份导入时会整体替换），而桌面壳在退出瞬间
写窗口几何，两边各自读写不同文件，互不踩踏。

本模块只做纯文件操作与纯计算，不碰真实窗口——几何采集（Win32）在
desktop.py 里做；无窗口环境（测试 / --browser 兜底）也可安全导入。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import instance

FILE_NAME = "window_state.json"

# 恢复的窗口至少要有这么多逻辑像素落在某块现存屏幕里，否则视为"屏幕外"
# （显示器拔掉 / 换了分辨率布局），该维度作废、回退默认居中。
MIN_VISIBLE = 100


def state_path(home: Path | None = None) -> Path:
    base = home if home is not None else instance.data_home()
    return base / FILE_NAME


def save_state(geometry: dict, home: Path | None = None) -> bool:
    """保存窗口几何；失败静默返回 False（调用点都在退出路径上，不能阻塞/报错）。"""
    try:
        path = state_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(geometry, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def load_state(home: Path | None = None) -> dict | None:
    """读上次保存的几何；读不到 / 损坏 / 类型不对一律返回 None（走默认窗口逻辑）。"""
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    width, height = data.get("width"), data.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    if width <= 0 or height <= 0:
        return None
    x, y = data.get("x"), data.get("y")
    if not isinstance(x, int) or not isinstance(y, int):
        x = y = None
    return {
        "width": width,
        "height": height,
        "x": x,
        "y": y,
        "maximized": bool(data.get("maximized", False)),
    }


def resolve_geometry(
    state: dict | None,
    default_size: tuple[int, int],
    screens: list[tuple[int, int, int, int]],
    min_size: tuple[int, int],
) -> tuple[int, int, int | None, int | None, bool]:
    """把保存的几何落成 create_window 的可用参数，无效的部分回退默认。

    返回 ``(width, height, x, y, maximized)``；x/y 为 None 时交给 pywebview 居中。
    校验链：尺寸不小于 min_size（不认比最小值还小的损坏记录）；位置完整保存时
    窗口至少 MIN_VISIBLE×MIN_VISIBLE 落在现存屏幕内。screens 为逻辑像素矩形
    ``(x, y, w, h)`` 列表，与 pywebview 的 Screen 属性同一坐标系。
    """
    width, height = default_size
    x = y = None
    maximized = False
    if state:
        min_w, min_h = min_size
        if state["width"] >= min_w and state["height"] >= min_h:
            width, height = state["width"], state["height"]
            maximized = state["maximized"]
            if state["x"] is not None and _visible_on_any_screen(state, screens):
                x, y = state["x"], state["y"]
    return width, height, x, y, maximized


def _visible_on_any_screen(state: dict, screens: list[tuple[int, int, int, int]]) -> bool:
    x, y, w, h = state["x"], state["y"], state["width"], state["height"]
    for sx, sy, sw, sh in screens:
        overlap_w = min(x + w, sx + sw) - max(x, sx)
        overlap_h = min(y + h, sy + sh) - max(y, sy)
        if overlap_w >= MIN_VISIBLE and overlap_h >= MIN_VISIBLE:
            return True
    return False
