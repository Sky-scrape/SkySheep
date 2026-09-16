"""Windows 窗口标题栏配色：把系统标题栏染成纸墨主题色（DWM 窗口属性）。

用户看到的最上方"控制窗口的那一行"是 Windows 系统标题栏，默认跟随系统
深色主题呈黑色，与纸墨界面割裂。Windows 11 22000+ 支持：

    DWMWA_BORDER_COLOR(34)  窗口边框色
    DWMWA_CAPTION_COLOR(35) 标题栏底色
    DWMWA_TEXT_COLOR(36)    标题文字色

更老的系统（Win10 等）调用返回非 0 错误码——就地忽略、保留系统默认标题栏，
绝不影响启动。颜色取自前端 app.css 的 :root 变量，改主题时两处要一起改。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PAPER_BG = "E8DFC7"  # app.css --bg 桌面深纸（标题栏与界面融为一体）
INK = "1D1A16"       # app.css --ink-line 硬墨描边（标题文字/边框）

# 夜墨主题（app.css [data-theme="dark"] 对应值）：深底浅字
DARK_BG = "1D1A16"
DARK_TEXT = "F4ECD8"

# 前端主题切换的回写点：apply_theme WS 方法设置，桌面壳的看板线程轮询
_theme_mode = "light"  # "light" | "dark"

# 桌面壳注册的主窗口：apply_theme 切主题时 refresh_now() 立即重刷标题栏，
# 不必等看板线程的下一秒轮询——否则页面先变色、标题栏晚一拍，肉眼可见。
_registered_window = None


def set_theme_mode(mode: str) -> None:
    global _theme_mode
    _theme_mode = "dark" if mode == "dark" else "light"


def current_theme_mode() -> str:
    return _theme_mode


def register_window(window) -> None:
    """桌面壳启动后注册主窗口，供 refresh_now() 事件驱动重刷（无窗口时 no-op）。"""
    global _registered_window
    _registered_window = window


def refresh_now() -> bool:
    """按当前主题立即重刷标题栏与窗口底色；没有注册窗口（浏览器模式等）返回 False。

    与看板线程的 _paint 同一套动作：DWM 标题栏三属性 + WinForms BackColor。
    调用方（backend.apply_theme）在 WS 线程——与看板线程一样是非 UI 线程，
    BackColor 直接赋值在 pywebview 的 WinForms 窗体上已验证可行。
    """
    window = _registered_window
    if window is None:
        return False
    dark = _theme_mode == "dark"
    try:
        apply_caption_theme(window, dark=dark)
    except Exception:
        pass
    try:
        from System.Drawing import ColorTranslator  # noqa: PLC0415  pywebview 自带

        native = getattr(window, "native", None)
        if native is not None:
            native.BackColor = ColorTranslator.FromHtml(window_background())
    except Exception:
        pass
    return True


def window_background(theme: str | None = None) -> str:
    """pywebview 窗口底色（#RRGGBB），跟当前主题走。

    pywebview 默认 background_color="#FFFFFF"，而它又把 WebView2 的
    DefaultBackgroundColor 设成透明——页面任何还没画出来的瞬间（首帧、
    整页刷新、切换项目）露出的是这层纯白。夜墨主题下就是一次刺眼白闪，
    所以这里让窗口底色始终等于 app.css 的 --bg。
    """
    dark = (theme or _theme_mode) == "dark"
    return "#" + (DARK_BG if dark else PAPER_BG)


def read_ui_theme(home: Path | None = None) -> str:
    """从 ui.json 读主题偏好（light/dark），用于窗口创建前就定好底色。

    启动时窗口比服务先建，拿不到前端状态，只能直接读文件；读失败按 light。
    """
    import json

    base = home
    if base is None:
        env = os.environ.get("SKYSHEEP_HOME")
        base = Path(env).expanduser() if env else Path.home() / ".skysheep"
    try:
        prefs = json.loads((base / "ui.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "light"
    if not isinstance(prefs, dict):
        return "light"
    # auto 跟随系统：Windows 下用注册表判断，拿不到就按 light
    mode = prefs.get("theme")
    if mode == "dark":
        return "dark"
    if mode == "light":
        return "light"
    return "dark" if _system_prefers_dark() else "light"


def _system_prefers_dark() -> bool:
    """系统是否处于深色主题（Win10 1809+ 的 AppsUseLightTheme）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:  # noqa: BLE001
        return False


DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def _colorref(hex_rgb: str):
    """#RRGGBB → Windows COLORREF(0x00BBGGRR)。"""
    import ctypes

    r, g, b = int(hex_rgb[0:2], 16), int(hex_rgb[2:4], 16), int(hex_rgb[4:6], 16)
    return ctypes.c_uint(r | (g << 8) | (b << 16))


def _hwnd_of(window) -> int | None:
    """pywebview 窗口 → 原生 HWND；窗口未创建/平台不支持时返回 None。"""
    native = getattr(window, "native", None)
    if native is None:
        return None
    handle = getattr(native, "Handle", None)  # WinForms BrowserForm → IntPtr
    if handle is None:
        try:
            return int(native)  # 后端直接给 int 句柄的情况
        except (TypeError, ValueError):
            return None
    if hasattr(handle, "ToInt64"):
        return int(handle.ToInt64())
    return int(handle)


def apply_caption_theme(window, dark: bool = False) -> bool:
    """把窗口标题栏底色/文字/边框染成主题色；全部成功返回 True。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = _hwnd_of(window)
        if not hwnd:
            return False
        bg = DARK_BG if dark else PAPER_BG
        text = DARK_TEXT if dark else INK
        dwm = ctypes.windll.dwmapi
        ok = True
        for attr, color in (
            (DWMWA_CAPTION_COLOR, _colorref(bg)),
            (DWMWA_TEXT_COLOR, _colorref(text)),
            (DWMWA_BORDER_COLOR, _colorref(INK)),
        ):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(color), 4) != 0:
                ok = False  # 老系统不支持该属性 → 保留系统默认
        return ok
    except Exception:
        return False


def hook_caption_theme(window) -> None:
    """窗口显示后应用标题栏配色（shown 事件时原生句柄已创建）；失败静默。"""

    def _on_shown(*_args, **_kwargs) -> None:
        apply_caption_theme(window)

    try:
        window.events.shown += _on_shown
    except Exception:
        pass
