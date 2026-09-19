"""Windows 窗口标题栏配色：把系统标题栏染成纸墨主题色（DWM 窗口属性）。

用户看到的最上方"控制窗口的那一行"是 Windows 系统标题栏，默认跟随系统
深色主题呈黑色，与纸墨界面割裂。Windows 11 22000+ 支持：

    DWMWA_BORDER_COLOR(34)  窗口边框色
    DWMWA_CAPTION_COLOR(35) 标题栏底色
    DWMWA_TEXT_COLOR(36)    标题文字色

更老的系统（Win10 等）调用返回非 0 错误码——就地忽略、保留系统默认标题栏，
绝不影响启动。颜色逐主题取自前端 app.css 的 --bg / --text / --ink-line
（见 THEME_PALETTE），改主题色时两处要一起改。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# 六套主题（app.js THEMES）的标题栏 / 窗口底色 / 标题文字 / 边线，逐主题取自
# app.css 的 --bg / --text / --ink-line（改主题色时两处要一起改）。
# 标题栏与页面底色同源，深浅两族都不再是近似色。
THEME_PALETTE = {
    "paper":   {"bg": "E8DFC7", "text": "1D1A16", "line": "1D1A16"},
    "celadon": {"bg": "DCE4DA", "text": "1C221D", "line": "1C221D"},
    "kaki":    {"bg": "EAD9BF", "text": "2B1D12", "line": "2B1D12"},
    "night":   {"bg": "171410", "text": "F4ECD8", "line": "0E0C09"},
    "indigo":  {"bg": "12161F", "text": "E3E8F2", "line": "0A0D13"},
    "pine":    {"bg": "101613", "text": "E8EDE6", "line": "080D0A"},
}
# 旧版两档值与六套主题同义；ui.json 存的是主题 id；
# "auto"（存为 null）与未知值跟随系统深浅（深 → night / 浅 → paper）
LEGACY_MODES = {"light": "paper", "dark": "night"}
LIGHT_THEME_IDS = {"paper", "celadon", "kaki", "light"}
DARK_THEME_IDS = {"night", "indigo", "pine", "dark"}

# 前端主题切换的回写点：apply_theme WS 方法设置，桌面壳的看板线程轮询
_theme_key = "paper"  # 六套主题 id 之一

# 桌面壳注册的主窗口：apply_theme 切主题时 refresh_now() 立即重刷标题栏，
# 不必等看板线程的下一秒轮询——否则页面先变色、标题栏晚一拍，肉眼可见。
_registered_window = None


def set_theme_mode(mode: str) -> None:
    """设置当前主题：接受主题 id，也兼容旧版 light/dark 两档值；未知值回退 paper。"""
    global _theme_key
    key = str(mode or "").strip().lower()
    key = LEGACY_MODES.get(key, key)
    _theme_key = key if key in THEME_PALETTE else "paper"


def current_theme_mode() -> str:
    """当前主题 id（paper / celadon / … / pine）。"""
    return _theme_key


def theme_is_dark(key: str | None = None) -> bool:
    """主题是否深色系；不传参时看当前主题。"""
    return (key or _theme_key) in DARK_THEME_IDS


def register_window(window) -> None:
    """桌面壳启动后注册主窗口，供 refresh_now() 事件驱动重刷（无窗口时 no-op）。"""
    global _registered_window
    _registered_window = window


def refresh_now() -> bool:
    """按当前主题立即重刷标题栏与窗口底色；没有注册窗口（浏览器模式等）返回 False。

    与看板线程的 _paint 同一套动作：DWM 标题栏三属性 + WinForms BackColor，
    并同步切换窗口标题栏小图标（浅色=墨线 / 深色=白线，见 apply_window_icon）。
    调用方（backend.apply_theme）在 WS 线程——与看板线程一样是非 UI 线程，
    BackColor 直接赋值在 pywebview 的 WinForms 窗体上已验证可行。
    """
    window = _registered_window
    if window is None:
        return False
    try:
        apply_caption_theme(window)
    except Exception:
        pass
    try:
        from System.Drawing import ColorTranslator  # noqa: PLC0415  pywebview 自带

        native = getattr(window, "native", None)
        if native is not None:
            native.BackColor = ColorTranslator.FromHtml(window_background())
            apply_window_icon(native, dark=theme_is_dark())
    except Exception:
        pass
    return True


# 窗口图标（System.Drawing.Icon / HICON）持引用：被 GC/销毁后标题栏图标会失效
_icon_holder: dict = {}

WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10


def apply_window_icon(native, dark: bool = False) -> bool:
    """窗口图标分离设置（纯 Win32，不碰 Form.Icon 以免任务栏一起被改）：

    - 标题栏小图标 WM_SETICON(ICON_SMALL) = 主题线稿（浅色=墨线 / 深色=白线）
    - 任务栏/Alt-Tab WM_SETICON(ICON_BIG) = 彩色方块（恒定，深浅任务栏都醒目）
    启动序列：webview.start(icon=彩色) 让任务栏第一帧就是彩色，本函数随后只把
    标题栏 SMALL 换成线稿——任务栏不再出现"先线稿后彩色"的闪变。
    图标文件来自应用 static 目录（打包态走 sys._MEIPASS）。
    """
    try:
        static = _static_dir()
        line_path = static / ("skysheep-line-white.ico" if dark else "skysheep-line-ink.ico")
        color_path = static / "skysheep.ico"
        if not line_path.exists() or not color_path.exists():
            return False
        user32 = ctypes.windll.user32
        hwnd = int(native.Handle.ToInt64())
        small_size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON，随 DPI

        small = user32.LoadImageW(None, str(line_path), IMAGE_ICON, small_size, small_size, LR_LOADFROMFILE)
        big = user32.LoadImageW(None, str(color_path), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        if not small or not big:
            return False
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        # 旧句柄销毁防泄漏（首帧时 holder 里还没有旧值）
        for key, new in (("small", small), ("big", big)):
            old = _icon_holder.pop(key, None)
            if old:
                user32.DestroyIcon(old)
            _icon_holder[key] = new
        return True
    except Exception:
        return False


def _static_dir() -> Path:
    """应用静态资源目录（与 server/app.py 同一套打包兼容逻辑）。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "skysheep" / "server" / "static"
    return Path(__file__).parent / "server" / "static"


def window_background(theme: str | None = None) -> str:
    """pywebview 窗口底色（#RRGGBB），跟当前主题走（可传主题 id 或旧版 light/dark）。

    pywebview 默认 background_color="#FFFFFF"，而它又把 WebView2 的
    DefaultBackgroundColor 设成透明——页面任何还没画出来的瞬间（首帧、
    整页刷新、切换项目）露出的是这层纯白。夜墨主题下就是一次刺眼白闪，
    所以这里让窗口底色始终等于 app.css 的 --bg。
    """
    key = str(theme or "").strip().lower() if theme else ""
    key = LEGACY_MODES.get(key, key)
    if key not in THEME_PALETTE:
        key = _theme_key
    return "#" + THEME_PALETTE[key]["bg"]


def read_ui_theme(home: Path | None = None) -> str:
    """从 ui.json 读主题偏好，返回主题 id（paper / celadon / … / pine）。

    启动时窗口比服务先建，拿不到前端状态，只能直接读文件；
    auto（存为 null）与未知值跟随系统深浅（深 → night / 浅 → paper），
    文件缺失/损坏按 paper（不依赖注册表，探不到就当浅色）。
    """
    import json

    base = home
    if base is None:
        env = os.environ.get("SKYSHEEP_HOME")
        base = Path(env).expanduser() if env else Path.home() / ".skysheep"
    try:
        prefs = json.loads((base / "ui.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "paper"
    if not isinstance(prefs, dict):
        return "paper"
    # auto（存为 null）与未知值：跟随系统深浅（Windows 下看注册表）
    mode = str(prefs.get("theme") or "").strip().lower()
    mode = LEGACY_MODES.get(mode, mode)
    if mode in THEME_PALETTE:
        return mode
    return "night" if _system_prefers_dark() else "paper"


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


DWMWA_USE_IMMERSIVE_DARK_MODE = 20
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


def apply_caption_theme(window) -> bool:
    """把窗口标题栏底色/文字/边框染成当前主题色；全部成功返回 True。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = _hwnd_of(window)
        if not hwnd:
            return False
        pal = THEME_PALETTE[_theme_key]
        dwm = ctypes.windll.dwmapi
        ok = True
        for attr, color in (
            (DWMWA_CAPTION_COLOR, _colorref(pal["bg"])),
            (DWMWA_TEXT_COLOR, _colorref(pal["text"])),
            (DWMWA_BORDER_COLOR, _colorref(pal["line"])),
            # pywebview 构造窗体时按系统深色模式设了沉浸式深色标题栏（20=1，
            # 深色系统下标题栏走系统黑）。我们自管颜色，必须显式关掉它：
            # 否则窗口最大化/还原等 DWM 重绘场景会把标题栏画回系统黑。
            (DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.c_uint(0)),
        ):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(color), 4) != 0:
                ok = False  # 老系统不支持该属性 → 保留系统默认
        return ok
    except Exception:
        return False


def hook_caption_theme(window) -> None:
    """窗口显示后应用标题栏配色（shown 事件时原生句柄已创建）；失败静默。"""

    def _on_shown(*_args, **_kwargs) -> None:
        # 按窗口创建前就已解析的主题着色，深色主题用户不会先闪一下浅色标题栏
        apply_caption_theme(window)

    try:
        window.events.shown += _on_shown
    except Exception:
        pass


_microphone_hooked = False


def allow_microphone(window) -> None:
    """放行 WebView2 里的麦克风权限（语音输入用）。

    WebView2 对 getUserMedia 默认静默不响应（不弹窗也不放行），必须挂
    CoreWebView2.PermissionRequested 把 Microphone 请求置为 Allow。
    挂载动作必须发生在 UI 线程——钩在 pywebview 的 EdgeChrome.on_webview_ready
    上（页面就绪回调本来就在 UI 线程跑），只挂一次。失败静默：语音输入不可用时
    前端会给出「麦克风不可用」的提示，不影响应用其它功能。
    """
    global _microphone_hooked
    if _microphone_hooked:
        return
    try:
        from Microsoft.Web.WebView2.Core import (  # noqa: PLC0415
            CoreWebView2PermissionState,
        )
        from webview.platforms import edgechromium  # noqa: PLC0415
    except Exception:
        return

    def _on_permission(sender, args) -> None:
        try:
            if str(args.PermissionKind) == "Microphone":
                args.State = CoreWebView2PermissionState.Allow
        except Exception:
            pass

    original = edgechromium.EdgeChrome.on_webview_ready

    def patched(self, sender, args):
        outcome = original(self, sender, args)
        try:
            sender.CoreWebView2.PermissionRequested += _on_permission
        except Exception:
            pass
        return outcome

    edgechromium.EdgeChrome.on_webview_ready = patched
    _microphone_hooked = True
