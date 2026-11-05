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
import sys
from pathlib import Path

# 二十六套主题（app.js THEMES）的标题栏 / 窗口底色 / 标题文字 / 边线，逐主题取自
# app.css 的 --bg / --text / --ink-line（改主题色时两处要一起改）。
# 标题栏与页面底色同源，深浅两族都不再是近似色。
# 除标题栏三色外还包含桌面壳各页面共用的 UI 色（card/raised/sunken/text2/dim/
# line2/blue/blue-deep），逐项取自 app.css 同名变量：启动动画页、启动失败页、
# 退出选择框都用这份色板渲染，保证与界面内主题逐像素一致。
THEME_PALETTE = {
    "paper":   {"bg": "E8DFC7", "text": "1D1A16", "line": "1D1A16",
                "card": "F4ECD8", "raised": "FBF5E6", "sunken": "ECE3CB",
                "text2": "4A4336", "dim": "6A6252", "line2": "C0B394",
                "blue": "1257C4", "blue_deep": "0E429B"},
    "celadon": {"bg": "DCE4DA", "text": "1C221D", "line": "1C221D",
                "card": "E9EFE4", "raised": "F2F6EE", "sunken": "DFE7DC",
                "text2": "46504A", "dim": "5F6A60", "line2": "A9B8A5",
                "blue": "0E7A68", "blue_deep": "0A5F52"},
    "kaki":    {"bg": "EAD9BF", "text": "2B1D12", "line": "2B1D12",
                "card": "F6E9D2", "raised": "FDF3DD", "sunken": "EDDCC0",
                "text2": "57432E", "dim": "6F5A44", "line2": "C3AC85",
                "blue": "B45309", "blue_deep": "8F4006"},
    "mint":    {"bg": "D9E6E3", "text": "14211D", "line": "14211D",
                "card": "ECF5F1", "raised": "F6FBF8", "sunken": "E2EFE9",
                "text2": "3D5049", "dim": "5C7068", "line2": "A5C3B8",
                "blue": "0F7A62", "blue_deep": "0A5E4B"},
    "bean":    {"bg": "E3D8D4", "text": "241C1A", "line": "241C1A",
                "card": "F0E7E3", "raised": "F8F1ED", "sunken": "E7DCD7",
                "text2": "54453F", "dim": "6F5D57", "line2": "BFA9A2",
                "blue": "5A6F4C", "blue_deep": "465839"},
    "mustard": {"bg": "E7E1D2", "text": "201E18", "line": "201E18",
                "card": "F4EFE0", "raised": "FCF8EC", "sunken": "EAE4D4",
                "text2": "4E4A3E", "dim": "6B6557", "line2": "BDB49C",
                "blue": "846400", "blue_deep": "6A5000"},
    "sprout":  {"bg": "EDE4D0", "text": "1E2116", "line": "1E2116",
                "card": "F7EFDD", "raised": "FDF8EA", "sunken": "F0E7D4",
                "text2": "4A4C37", "dim": "676A52", "line2": "C5B99A",
                "blue": "4D7018", "blue_deep": "3C5A10"},
    "silver":  {"bg": "E4E3DE", "text": "1B1B19", "line": "1B1B19",
                "card": "F1F0EC", "raised": "FAFAF7", "sunken": "E8E7E2",
                "text2": "46463F", "dim": "63635C", "line2": "B9B8B0",
                "blue": "B03A4E", "blue_deep": "8E2C3D"},
    "blade":   {"bg": "E4E9EF", "text": "161B22", "line": "161B22",
                "card": "F1F4F8", "raised": "F9FBFD", "sunken": "E7ECF2",
                "text2": "3F4A58", "dim": "5C6878", "line2": "B3BFCD",
                "blue": "1F6CBD", "blue_deep": "185796"},
    "sakura":  {"bg": "F6E6EA", "text": "23171B", "line": "23171B",
                "card": "FDF0F3", "raised": "FFF8FA", "sunken": "F8E9ED",
                "text2": "574249", "dim": "78626A", "line2": "D3B2BD",
                "blue": "266F93", "blue_deep": "1E5A78"},
    "meadow":  {"bg": "E6F0DF", "text": "1A2116", "line": "1A2116",
                "card": "F2F8EC", "raised": "FAFDF6", "sunken": "E9F2E2",
                "text2": "414B3A", "dim": "5F6B57", "line2": "ADC4A1",
                "blue": "4A7733", "blue_deep": "3A5F27"},
    "heather": {"bg": "E9E4F1", "text": "1D1826", "line": "1D1826",
                "card": "F4F1F9", "raised": "FBF9FE", "sunken": "ECE7F4",
                "text2": "4B4359", "dim": "685E78", "line2": "BFB3D4",
                "blue": "6B5595", "blue_deep": "57447A"},
    "caramel": {"bg": "EFE3D2", "text": "241A11", "line": "241A11",
                "card": "F8EFDF", "raised": "FDF8EC", "sunken": "F2E7D6",
                "text2": "54432F", "dim": "6F5C46", "line2": "C6B196",
                "blue": "6F4523", "blue_deep": "5A3719"},
    "night":   {"bg": "171410", "text": "F4ECD8", "line": "0E0C09",
                "card": "211D17", "raised": "2B261E", "sunken": "1B1813",
                "text2": "D3C8AD", "dim": "A89D84", "line2": "574C3A",
                "blue": "5B93EE", "blue_deep": "4077D6"},
    "indigo":  {"bg": "12161F", "text": "E3E8F2", "line": "0A0D13",
                "card": "1A1F2B", "raised": "232A39", "sunken": "161B25",
                "text2": "B7C1D5", "dim": "93A0B8", "line2": "414D66",
                "blue": "7FA3F2", "blue_deep": "5F86E0"},
    "pine":    {"bg": "101613", "text": "E8EDE6", "line": "080D0A",
                "card": "18201B", "raised": "212B24", "sunken": "131A16",
                "text2": "BEC9BF", "dim": "98A89C", "line2": "3C4C40",
                "blue": "52C79D", "blue_deep": "35A87F"},
    "abyss":   {"bg": "080B16", "text": "E6ECF7", "line": "04060D",
                "card": "101527", "raised": "161C31", "sunken": "0C1120",
                "text2": "B9C4DA", "dim": "8E9BB5", "line2": "34415E",
                "blue": "5C7CFF", "blue_deep": "4265E8"},
    "scarlet": {"bg": "160E0E", "text": "F3E6E2", "line": "0C0707",
                "card": "211515", "raised": "291A1A", "sunken": "1A1010",
                "text2": "D4BAB3", "dim": "A8908A", "line2": "4D3331",
                "blue": "2FD6D6", "blue_deep": "1FB3B3"},
    "lilac":   {"bg": "130F19", "text": "ECE4F4", "line": "0A0710",
                "card": "1D1628", "raised": "251C32", "sunken": "181122",
                "text2": "C9BCD9", "dim": "A193B4", "line2": "463657",
                "blue": "C47AE0", "blue_deep": "A95CC4"},
    "lime":    {"bg": "0D130C", "text": "EAF3E4", "line": "070C06",
                "card": "162112", "raised": "1C2A17", "sunken": "111A0F",
                "text2": "C2D2BA", "dim": "95A88C", "line2": "3D5036",
                "blue": "8FE35C", "blue_deep": "74C445"},
    "rouge":   {"bg": "170D12", "text": "F7E9ED", "line": "0D070A",
                "card": "24151D", "raised": "2D1B25", "sunken": "1C1017",
                "text2": "D9BCC6", "dim": "B0919C", "line2": "50313F",
                "blue": "FF5C86", "blue_deep": "E33A68"},
    "obsidian": {"bg": "121212", "text": "F2F2F2", "line": "080808",
                 "card": "1E1E1E", "raised": "262626", "sunken": "171717",
                 "text2": "CCCCCC", "dim": "A3A3A3", "line2": "4F4F4F",
                 "blue": "FFDD00", "blue_deep": "D9BB00"},
    "inkred":  {"bg": "101815", "text": "E8EDE9", "line": "070C09",
                "card": "1A251F", "raised": "213029", "sunken": "141D19",
                "text2": "BCC9C3", "dim": "94A69E", "line2": "40564B",
                "blue": "D47777", "blue_deep": "BD6161"},
    "glowsea": {"bg": "0C1722", "text": "E3EDF5", "line": "040B12",
                "card": "132331", "raised": "1A2D3E", "sunken": "0F1C29",
                "text2": "B4C7D6", "dim": "8AA2B5", "line2": "37526B",
                "blue": "62D2C2", "blue_deep": "45B3A4"},
    "cocoa":   {"bg": "17100C", "text": "F5E9DF", "line": "0A0603",
                "card": "241A13", "raised": "2D211A", "sunken": "1C140F",
                "text2": "D8C2B1", "dim": "B39A86", "line2": "55402F",
                "blue": "F6B896", "blue_deep": "D99873"},
    "cinnabar": {"bg": "150B0B", "text": "F5E8E2", "line": "0A0505",
                 "card": "221414", "raised": "2B1B1B", "sunken": "1A0F0F",
                 "text2": "D8BEB6", "dim": "B1968C", "line2": "523432",
                 "blue": "D45A5A", "blue_deep": "B84242"},
}
# 旧版两档值与二十六套主题同义；ui.json 存的是主题 id；
# "auto"（存为 null）与未知值跟随系统深浅（深 → night / 浅 → paper）
LEGACY_MODES = {"light": "paper", "dark": "night"}
DARK_THEME_IDS = {
    "night", "indigo", "pine", "abyss", "scarlet", "lilac", "lime", "rouge",
    "obsidian", "inkred", "glowsea", "cocoa", "cinnabar", "dark",
}

# 前端主题切换的回写点：apply_theme WS 方法设置，桌面壳的看板线程轮询
_theme_key = "paper"  # 二十六套主题 id 之一

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
    except Exception:  # noqa: BLE001
        pass
    try:
        from System.Drawing import ColorTranslator  # noqa: PLC0415  pywebview 自带

        native = getattr(window, "native", None)
        if native is not None:
            native.BackColor = ColorTranslator.FromHtml(window_background())
            apply_window_icon(native, dark=theme_is_dark())
    except Exception:  # noqa: BLE001
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
        # 两个尺寸都必须问系统要（SM_CXSMICON=标题栏、SM_CXICON=任务栏/Alt-Tab，都随 DPI 放大）：
        # 写死 48 时 200% DPI 下系统要 64，于是又把 48 放大一遍——糊上加糊（2026-11-04 实测）。
        small_size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
        big_size = user32.GetSystemMetrics(11) or 32  # SM_CXICON

        small = user32.LoadImageW(None, str(line_path), IMAGE_ICON, small_size, small_size, LR_LOADFROMFILE)
        big = user32.LoadImageW(None, str(color_path), IMAGE_ICON, big_size, big_size, LR_LOADFROMFILE)
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
    except Exception:  # noqa: BLE001
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
    auto（存为 null）与未知值跟随系统深浅，落点按 ui.json 的
    theme_auto_light / theme_auto_dark（设置页「跟随系统时」两个下拉，
    缺省 = 纸墨 / 夜墨），文件缺失/损坏按纸墨（不依赖注册表，探不到就当浅色）。
    """
    import json

    from . import instance

    base = home if home is not None else instance.data_home()
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
    dark = _system_prefers_dark()
    auto_val = str(prefs.get("theme_auto_dark" if dark else "theme_auto_light") or "")
    auto_val = auto_val.strip().lower()
    if auto_val in THEME_PALETTE:
        return auto_val
    return "night" if dark else "paper"


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
    except Exception:  # noqa: BLE001
        return False


def hook_caption_theme(window) -> None:
    """窗口显示后应用标题栏配色（shown 事件时原生句柄已创建）；失败静默。"""

    def _on_shown(*_args, **_kwargs) -> None:
        # 按窗口创建前就已解析的主题着色，深色主题用户不会先闪一下浅色标题栏
        apply_caption_theme(window)

    try:
        window.events.shown += _on_shown
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        return

    def _on_permission(sender, args) -> None:
        try:
            if str(args.PermissionKind) == "Microphone":
                args.State = CoreWebView2PermissionState.Allow
        except Exception:  # noqa: BLE001
            pass

    original = edgechromium.EdgeChrome.on_webview_ready

    def patched(self, sender, args):
        outcome = original(self, sender, args)
        try:
            sender.CoreWebView2.PermissionRequested += _on_permission
        except Exception:  # noqa: BLE001
            pass
        return outcome

    edgechromium.EdgeChrome.on_webview_ready = patched
    _microphone_hooked = True
