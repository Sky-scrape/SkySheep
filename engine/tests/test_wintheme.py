"""窗口配色：标题栏 / 窗口底色的主题取值（切项目整页刷新时露出的那一层）。

只测纯函数与文件读取，不碰真实窗口：apply_caption_theme 需要 HWND，
在无窗口的测试环境里本来就会静默返回 False。原生路径（DWM / 图标 / 麦克风
钩子）用注入的假模块与假 windll 覆盖，不依赖真装 WebView2。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from skysheep import wintheme


@pytest.fixture(autouse=True)
def _restore_module_state():
    """本文件大量改动 wintheme 的模块级状态（主题 / 注册窗口 / 图标句柄 / 麦克风标记），
    逐个用例后还原，避免用例间互相污染。"""
    saved = (
        wintheme._theme_key,
        wintheme._registered_window,
        dict(wintheme._icon_holder),
        wintheme._microphone_hooked,
    )
    yield
    wintheme._theme_key = saved[0]
    wintheme._registered_window = saved[1]
    wintheme._icon_holder.clear()
    wintheme._icon_holder.update(saved[2])
    wintheme._microphone_hooked = saved[3]


# ---- 测试替身（不碰真实窗口 / 真实 windll） ----


class _Win:
    """最小窗口替身：只带 native。"""

    def __init__(self, native=None):
        self.native = native


class _Handle:
    """WinForms IntPtr 风格句柄（带 ToInt64）。"""

    def __init__(self, value):
        self._value = value

    def ToInt64(self):
        return self._value


class _NativeWithHandle:
    def __init__(self, value):
        self.Handle = _Handle(value)


class _NativePlainHandle:
    """句柄不是 IntPtr 而是裸数字。"""

    def __init__(self, value):
        self.Handle = value


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


def test_read_ui_theme_non_dict_prefs_falls_back(home):
    """ui.json 是合法 JSON 但不是对象（数组/字符串）时也按纸墨，不炸。"""
    base = home / "home"
    base.mkdir(parents=True, exist_ok=True)
    (base / "ui.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert wintheme.read_ui_theme(base) == "paper"


# ---- 颜色转换与句柄解析（纯逻辑） ----


def test_colorref_is_bgr_uint():
    """#RRGGBB → COLORREF(0x00BBGGRR)：字节序反着来，写错会得到偏色标题栏。"""
    assert wintheme._colorref("1257C4").value == 0xC45712
    assert wintheme._colorref("000000").value == 0x000000
    assert wintheme._colorref("FFFFFF").value == 0xFFFFFF
    assert wintheme._colorref("E8DFC7").value == 0xC7DFE8


def test_hwnd_of_native_shapes():
    """窗口 → HWND 的三种形状：无原生 / IntPtr 风格 / 裸数字；非整数给 None。"""
    assert wintheme._hwnd_of(_Win(None)) is None
    assert wintheme._hwnd_of(_Win(_NativeWithHandle(4242))) == 4242
    assert wintheme._hwnd_of(_Win(_NativePlainHandle(7))) == 7
    assert wintheme._hwnd_of(_Win(object())) is None  # int() 抛 TypeError → 兼底 None


def test_static_dir_bundle_and_source(monkeypatch):
    """静态目录：源码态落包内路径；打包态走 sys._MEIPASS。"""
    monkeypatch.delattr(wintheme.sys, "_MEIPASS", raising=False)
    src = wintheme._static_dir()
    assert src.name == "static" and src.parent.name == "server"
    monkeypatch.setattr(wintheme.sys, "_MEIPASS", "C:/bundle", raising=False)
    assert wintheme._static_dir() == Path("C:/bundle") / "skysheep" / "server" / "static"


# ---- 系统深浅色判定 ----


def test_system_prefers_dark_non_win32(monkeypatch):
    """非 Windows 直接当浅色（不探注册表）。"""
    monkeypatch.setattr(wintheme.sys, "platform", "linux")
    assert wintheme._system_prefers_dark() is False


def test_system_prefers_dark_reads_registry(monkeypatch):
    """AppsUseLightTheme=0 为深色；=1 为浅色；读不到（键缺失）当浅色。"""
    monkeypatch.setattr(wintheme.sys, "platform", "win32")

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 1
    fake.OpenKey = lambda *a, **k: _Key()
    monkeypatch.setitem(sys.modules, "winreg", fake)

    fake.QueryValueEx = lambda key, name: (0, 1)
    assert wintheme._system_prefers_dark() is True
    fake.QueryValueEx = lambda key, name: (1, 1)
    assert wintheme._system_prefers_dark() is False

    def _boom(*_a, **_k):
        raise OSError("no such key")

    fake.QueryValueEx = _boom
    assert wintheme._system_prefers_dark() is False


# ---- 标题栏着色（注入假 windll，不碰真 DWM） ----


def test_apply_caption_theme_non_win32(monkeypatch):
    monkeypatch.setattr(wintheme.sys, "platform", "linux")
    assert wintheme.apply_caption_theme(_Win(None)) is False


def test_apply_caption_theme_without_hwnd():
    """无原生句柄（浏览器模式 / 窗口未建）：False，不抛。"""
    if wintheme.sys.platform != "win32":
        pytest.skip("仅 Windows 走这条真实分支")
    assert wintheme.apply_caption_theme(_Win(None)) is False


def test_apply_caption_theme_sets_four_dwm_attributes(monkeypatch):
    """四个 DWM 属性按顺序设置（标题栏底/文字/边框 + 关掉沉浸式深色），全成功返 True。"""
    import ctypes

    calls = []

    class _Dwm:
        @staticmethod
        def DwmSetWindowAttribute(hwnd, attr, ref, size):
            calls.append((hwnd, attr, size))
            return 0

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(dwmapi=_Dwm()), raising=False)
    monkeypatch.setattr(wintheme.sys, "platform", "win32")
    wintheme.set_theme_mode("night")

    assert wintheme.apply_caption_theme(_Win(_NativeWithHandle(99))) is True
    assert [c[1] for c in calls] == [
        wintheme.DWMWA_CAPTION_COLOR,
        wintheme.DWMWA_TEXT_COLOR,
        wintheme.DWMWA_BORDER_COLOR,
        wintheme.DWMWA_USE_IMMERSIVE_DARK_MODE,
    ]
    assert all(c[0] == 99 and c[2] == 4 for c in calls)


def test_apply_caption_theme_reports_unsupported(monkeypatch):
    """老系统属性不支持（返回非 0）：如实返 False，保留系统默认标题栏。"""
    import ctypes

    class _Dwm:
        @staticmethod
        def DwmSetWindowAttribute(*_a):
            return 1

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(dwmapi=_Dwm()), raising=False)
    monkeypatch.setattr(wintheme.sys, "platform", "win32")
    assert wintheme.apply_caption_theme(_Win(_NativeWithHandle(7))) is False


def test_apply_caption_theme_swallows_dwm_exception(monkeypatch):
    """DWM 调用抛异常（无该 API）也不外溢，返 False。"""
    import ctypes

    class _Dwm:
        @staticmethod
        def DwmSetWindowAttribute(*_a):
            raise OSError("dwmapi unavailable")

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(dwmapi=_Dwm()), raising=False)
    monkeypatch.setattr(wintheme.sys, "platform", "win32")
    assert wintheme.apply_caption_theme(_Win(_NativeWithHandle(7))) is False


# ---- 窗口图标 ----


def test_apply_window_icon_missing_files_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(wintheme, "_static_dir", lambda: tmp_path)
    assert wintheme.apply_window_icon(_NativeWithHandle(1)) is False


def test_apply_window_icon_sets_small_big_and_destroys_old(monkeypatch, tmp_path):
    """小图标=线稿 / 大图标=彩色；第二次调用销毁旧句柄防泄漏。"""
    import ctypes

    for name in ("skysheep-line-ink.ico", "skysheep-line-white.ico", "skysheep.ico"):
        (tmp_path / name).write_bytes(b"icon")
    monkeypatch.setattr(wintheme, "_static_dir", lambda: tmp_path)
    monkeypatch.setattr(wintheme, "_icon_holder", {})

    calls = {"load": [], "send": [], "destroy": []}

    class _User32:
        @staticmethod
        def GetSystemMetrics(_idx):
            return 16

        @staticmethod
        def LoadImageW(_inst, path, _kind, _cx, _cy, _flags):
            calls["load"].append(path)
            return 1001 if "line" in path else 2002

        @staticmethod
        def SendMessageW(hwnd, msg, which, icon):
            calls["send"].append((hwnd, msg, which, icon))

        @staticmethod
        def DestroyIcon(icon):
            calls["destroy"].append(icon)

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=_User32()), raising=False)

    assert wintheme.apply_window_icon(_NativeWithHandle(55), dark=False) is True
    assert calls["send"] == [
        (55, wintheme.WM_SETICON, wintheme.ICON_SMALL, 1001),
        (55, wintheme.WM_SETICON, wintheme.ICON_BIG, 2002),
    ]
    assert wintheme._icon_holder == {"small": 1001, "big": 2002}
    assert calls["destroy"] == []  # 首帧没有旧句柄

    assert wintheme.apply_window_icon(_NativeWithHandle(55), dark=True) is True
    assert sorted(calls["destroy"]) == [1001, 2002]


def test_apply_window_icon_returns_false_when_load_fails(monkeypatch, tmp_path):
    """LoadImageW 返回 0（资源坏）时如实返 False，不发 WM_SETICON。"""
    import ctypes

    for name in ("skysheep-line-ink.ico", "skysheep.ico"):
        (tmp_path / name).write_bytes(b"icon")
    monkeypatch.setattr(wintheme, "_static_dir", lambda: tmp_path)

    sent = []

    class _User32:
        @staticmethod
        def GetSystemMetrics(_idx):
            return 0  # 拿不到尺寸：走 `or 16` 兼底

        @staticmethod
        def LoadImageW(*_a):
            return 0

        @staticmethod
        def SendMessageW(*a):
            sent.append(a)

        @staticmethod
        def DestroyIcon(_a):
            pass

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=_User32()), raising=False)
    assert wintheme.apply_window_icon(_NativeWithHandle(1)) is False
    assert sent == []


def test_apply_window_icon_swallows_exception(monkeypatch):
    """静态目录取不到（异常）时如实返 False，不外溢。"""
    def _boom():
        raise OSError("static dir unavailable")

    monkeypatch.setattr(wintheme, "_static_dir", _boom)
    assert wintheme.apply_window_icon(_NativeWithHandle(1)) is False


# ---- 事件驱动重刷 ----


def test_refresh_now_swallows_caption_failure(monkeypatch):
    """标题栏着色抛错不影响 refresh_now 的整体返回（窗口底色照旧重刷）。"""
    monkeypatch.setattr(wintheme, "_registered_window", _Win(None))

    def _boom(_w):
        raise RuntimeError("dwm gone")

    monkeypatch.setattr(wintheme, "apply_caption_theme", _boom)
    assert wintheme.refresh_now() is True


def test_refresh_now_paints_backcolor_and_icon(monkeypatch):
    """注册窗口后 refresh_now 走原生分支：设 BackColor + 换窗口图标（注入假模块）。"""
    class _ColorTranslator:
        @staticmethod
        def FromHtml(html):
            return html

    drawing = types.ModuleType("System.Drawing")
    drawing.ColorTranslator = _ColorTranslator
    monkeypatch.setitem(sys.modules, "System.Drawing", drawing)

    class _Native:
        def __init__(self):
            self.Handle = _Handle(123)
            self.BackColor = None

    class _WinWithNative:
        def __init__(self):
            self.native = _Native()

    icons = []
    monkeypatch.setattr(wintheme, "apply_caption_theme", lambda _w: True)
    monkeypatch.setattr(
        wintheme, "apply_window_icon",
        lambda native, dark=False: icons.append(dark) or True,
    )

    win = _WinWithNative()
    monkeypatch.setattr(wintheme, "_registered_window", win)

    wintheme.set_theme_mode("night")
    assert wintheme.refresh_now() is True
    assert win.native.BackColor == wintheme.window_background("night")
    assert icons == [True]  # 深色主题用白线图标

    wintheme.set_theme_mode("paper")
    assert wintheme.refresh_now() is True
    assert win.native.BackColor == wintheme.window_background("paper")
    assert icons == [True, False]


# ---- 事件钩子与麦克风 ----


def test_hook_caption_theme_registers_and_fires(monkeypatch):
    """挂在 window.events.shown 上；shown 触发时按当前主题着色。"""
    painted = []
    monkeypatch.setattr(wintheme, "apply_caption_theme", lambda w: painted.append(w) or True)

    class _Signal:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    class _Events:
        def __init__(self):
            self.shown = _Signal()

    class _WinWithEvents:
        def __init__(self):
            self.events = _Events()

    win = _WinWithEvents()
    wintheme.hook_caption_theme(win)
    assert len(win.events.shown.handlers) == 1
    win.events.shown.handlers[0]()  # 模拟 shown 事件
    assert painted == [win]


def test_hook_caption_theme_swallows_broken_window():
    """拿不到 events 的窗口（老 pywebview / 假窗口）不抛异常。"""
    class _Broken:
        @property
        def events(self):
            raise RuntimeError("no events")

    wintheme.hook_caption_theme(_Broken())


def _install_fake_webview_modules(monkeypatch):
    """把 WebView2 与 webview.platforms.edgechromium 换成假模块（不依赖真装）。"""
    class _PermissionState:
        Allow = "allow"
        Deny = "deny"

    core = types.ModuleType("Microsoft.Web.WebView2.Core")
    core.CoreWebView2PermissionState = _PermissionState

    class _EdgeChrome:
        @staticmethod
        def on_webview_ready(_self, _sender, _args):
            return "orig"

    edge = types.ModuleType("webview.platforms.edgechromium")
    edge.EdgeChrome = _EdgeChrome

    for name in ("Microsoft", "Microsoft.Web", "Microsoft.Web.WebView2"):
        pkg = types.ModuleType(name)
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, name, pkg)
    monkeypatch.setitem(sys.modules, "Microsoft.Web.WebView2.Core", core)
    for name in ("webview", "webview.platforms"):
        pkg = types.ModuleType(name)
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, name, pkg)
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", edge)
    return _EdgeChrome


def test_allow_microphone_skips_when_webview2_absent(monkeypatch):
    """WebView2 不可用时静默返回，且不改标记（下次可重试）。"""
    monkeypatch.setattr(wintheme, "_microphone_hooked", False)
    monkeypatch.setitem(sys.modules, "Microsoft.Web.WebView2.Core", None)
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", None)
    wintheme.allow_microphone(object())
    assert wintheme._microphone_hooked is False


def test_allow_microphone_patches_permission_handler(monkeypatch):
    """挂上 PermissionRequested：只把麦克风置 Allow，其余不动；重复调用只挂一次。"""
    monkeypatch.setattr(wintheme, "_microphone_hooked", False)
    edge_chrome = _install_fake_webview_modules(monkeypatch)

    wintheme.allow_microphone(object())
    assert wintheme._microphone_hooked is True
    assert edge_chrome.on_webview_ready.__name__ == "patched"

    class _PermSignal:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    class _Core:
        def __init__(self):
            self.PermissionRequested = _PermSignal()

    class _Sender:
        def __init__(self):
            self.CoreWebView2 = _Core()

    sender = _Sender()
    assert edge_chrome.on_webview_ready(None, sender, None) == "orig"
    assert len(sender.CoreWebView2.PermissionRequested.handlers) == 1

    class _Args:
        def __init__(self, kind):
            self.PermissionKind = kind
            self.State = None

    handler = sender.CoreWebView2.PermissionRequested.handlers[0]
    mic = _Args("Microphone")
    handler(sender, mic)
    assert mic.State == "allow"
    cam = _Args("Camera")
    handler(sender, cam)
    assert cam.State is None  # 非麦克风不动

    # 已挂过：第二次调用直接返回，不重复包裹
    patched_before = edge_chrome.on_webview_ready
    wintheme.allow_microphone(object())
    assert edge_chrome.on_webview_ready is patched_before


def test_allow_microphone_swallows_inner_errors(monkeypatch):
    """权限回调与包裹层内部出错（拿不到 PermissionKind / CoreWebView2）都不外溢。"""
    monkeypatch.setattr(wintheme, "_microphone_hooked", False)
    edge_chrome = _install_fake_webview_modules(monkeypatch)
    wintheme.allow_microphone(object())

    class _BadSender:
        @property
        def CoreWebView2(self):
            raise RuntimeError("webview gone")

    # 包裹层内部拿不到 CoreWebView2：异常被吞，原返回值照旧
    assert edge_chrome.on_webview_ready(None, _BadSender(), None) == "orig"

    class _PermSignal:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    class _Core:
        def __init__(self):
            self.PermissionRequested = _PermSignal()

    class _Sender:
        def __init__(self):
            self.CoreWebView2 = _Core()

    sender = _Sender()
    edge_chrome.on_webview_ready(None, sender, None)

    class _BadArgs:
        @property
        def PermissionKind(self):
            raise RuntimeError("unexpected args")

    # 权限回调内部出错：被吞，不抛
    sender.CoreWebView2.PermissionRequested.handlers[0](sender, _BadArgs())
