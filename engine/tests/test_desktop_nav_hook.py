"""桌面壳 APPCOMMAND 后退/前进吞并（_install_no_history_nav）回归测试。

背景：2026-11-07 评审发现 SetWindowLongPtrW 的 argtypes 第三参误声明成
c_ssize_t，传入 WINFUNCTYPE 回调实例必抛 ArgumentError（TypeError: 'WinFunctionType'
object cannot be interpreted as an integer），异常被 _bootstrap 的 except 吞成日志
——滑动历史导航的 APPCOMMAND 吞并从未真正装上。本文件把当时的独立 ctypes
验证片段固化为自动化测试：在真实 message-only 窗口上实跑安装函数，守住三条——
  1. 装得上：不抛异常、回调与旧窗口过程引用进 _NAV_HOOKS（防 GC 悬空回归）；
  2. 吞得对：鼠标侧键路径（HIWORD 打 FAPPCOMMAND_MOUSE）的后退/前进被吞，
     原窗口过程收不到、返回 0；签名再被改错时这里直接红；
  3. 只吞后退/前进：其余 APPCOMMAND 与普通消息照常转发到原窗口过程。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="desktop 启动器仅 Windows")

_ENGINE = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("skysheep_desktop_nav_hook", _ENGINE / "desktop.py")
desktop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desktop)

WM_NULL = 0x0000
WM_APPCOMMAND = 0x0319
APPCOMMAND_BROWSER_BACKWARD = 1
APPCOMMAND_BROWSER_FORWARD = 2
APPCOMMAND_BROWSER_REFRESH = 3
FAPPCOMMAND_MOUSE = 0x8000  # 鼠标侧键注入的 APPCOMMAND 在 HIWORD 打的设备标记
HWND_MESSAGE = -3  # message-only 窗口：不显示、不抢焦点，CI 会话里也能建

_REC_PROC_TYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", _REC_PROC_TYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


def _appcommand_lparam(cmd: int) -> int:
    """构造系统注入样子的 WM_APPCOMMAND lparam：HIWORD = 设备标记 | 命令码。"""
    return (FAPPCOMMAND_MOUSE | cmd) << 16


@pytest.fixture
def recorded_window():
    """建一个 message-only 窗口，原窗口过程把收到的消息记进 seen；用完恢复销毁。

    挂钩本身由各用例自行调用 desktop._install_no_history_nav，这里只提供
    干净的底座窗口。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    kernel32.GetModuleHandleW.restype = wt.HINSTANCE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

    seen: list = []

    def _rec(h, msg, wparam, lparam):
        seen.append((msg, wparam, lparam))
        return user32.DefWindowProcW(h, msg, wparam, lparam)

    # 夹具局部量：挂起的生成器帧把它和 wc 一起持活，直到 finally 销毁窗口之后帧才退出
    rec_proc = _REC_PROC_TYPE(_rec)
    hinst = kernel32.GetModuleHandleW(None)
    cls_name = f"SSNavHookTestWnd-{id(seen)}"  # 每次唯一，防上轮崩溃残留类名顶号
    wc = _WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wc.lpfnWndProc = rec_proc
    wc.hInstance = hinst
    wc.lpszClassName = cls_name
    assert user32.RegisterClassExW(ctypes.byref(wc)), "RegisterClassExW 失败"

    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
    ]
    hwnd = user32.CreateWindowExW(
        0, cls_name, "nav-hook-test", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None
    )
    assert hwnd, "CreateWindowExW 失败"

    # 挂钩前的原窗口过程：收尾按它无条件还原，不依赖 _NAV_HOOKS 的登记顺序
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_ssize_t, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    orig_proc = user32.GetWindowLongPtrW(hwnd, -4)

    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

    def _send(h, msg, wparam, lparam):
        return user32.SendMessageW(h, msg, wparam, lparam)

    try:
        yield hwnd, seen, _send
    finally:
        # 无条件还原挂钩前的原窗口过程（整数收尾，第三参临时按整数声明）
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_ssize_t, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW(hwnd, -4, orig_proc)
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
        user32.UnregisterClassW(cls_name, hinst)


def test_install_swallows_mouse_side_button_appcommand(recorded_window):
    """装得上 + 吞得对：鼠标侧键的后退/前进被吞（返回 0、原过程收不到）。"""
    hwnd, seen, send = recorded_window
    hooks_before = len(desktop._NAV_HOOKS)
    desktop._install_no_history_nav(hwnd)  # 签名被改错时这里必抛
    # 回调与旧窗口过程引用都已登记（防局部量被 GC 导致窗口过程悬空）
    assert len(desktop._NAV_HOOKS) == hooks_before + 2
    assert isinstance(desktop._NAV_HOOKS[-1][0], int)  # 旧窗口过程句柄

    for cmd in (APPCOMMAND_BROWSER_BACKWARD, APPCOMMAND_BROWSER_FORWARD):
        seen.clear()
        ret = send(hwnd, WM_APPCOMMAND, 0, _appcommand_lparam(cmd))
        assert ret == 0, f"APPCOMMAND {cmd} 应吞成 0，实际返回 {ret}"
        assert WM_APPCOMMAND not in [m for m, _, _ in seen], (
            f"APPCOMMAND {cmd} 被转交给了原窗口过程——吞并没装上"
        )


def test_install_only_swallows_backward_and_forward(recorded_window):
    """只吞后退/前进：其余 APPCOMMAND 与普通消息照常转发到原窗口过程。"""
    hwnd, seen, send = recorded_window
    desktop._install_no_history_nav(hwnd)

    seen.clear()
    lparam = _appcommand_lparam(APPCOMMAND_BROWSER_REFRESH)
    send(hwnd, WM_APPCOMMAND, 0, lparam)
    assert (WM_APPCOMMAND, 0, lparam) in seen, "刷新等其它 APPCOMMAND 不应被吞"

    seen.clear()
    send(hwnd, WM_NULL, 0x1234, 0x5678)
    assert (WM_NULL, 0x1234, 0x5678) in seen, "普通消息应按原参数转发"
