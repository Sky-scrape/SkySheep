"""电脑控制工具：screenshot / window_list / clipboard_read / clipboard_write / mouse / keyboard / window。

设计说明：
- 截屏用 Pillow ImageGrab（pillow 已是依赖），图像以 ImageBlock 附加进对话（agent 循环
  把 ctx.images 转成 user 消息），模型可直接「看见」屏幕；副本存 skysheep_home()/screenshots；
- 鼠标/键盘/窗口/剪贴板用 ctypes 直调 Win32（与 desktop.py 同一路数），零新增依赖；
  SendInput 注入放 asyncio.to_thread，不阻塞事件循环；
- 坐标系统一为「虚拟屏幕像素坐标」：主屏左上角为原点，副屏可为负。screenshot 的返回
  文本会声明截图覆盖区域的 origin，模型用「图内坐标 + origin」换算即可；
- 纯逻辑（_to_abs / parse_hotkeys / args 校验）与 Win32 调用分离，测试不注入真实输入。
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..config import skysheep_home
from ..messages import ImageBlock
from .base import Safety, Tool, ToolContext, ToolError, truncate_output

IS_WINDOWS = sys.platform == "win32"

# typing 单字符超过 2000 个时改用 clipboard_write + ctrl+v（逐键注入太慢）
MAX_TYPE_CHARS = 2000
# 附加图像超过该宽度时等比缩小（控制 base 体积；缩放比会在返回文本中声明）
MAX_IMAGE_WIDTH = 2400

# 截图副本保留上限（~/.skysheep/screenshots）：每次截屏都会存一份，
# 但没有上限时它会无声涨下去（与检查点不同，那里有 MAX_CHECKPOINTS 淘汰）。
# 副本只用于事后回看，保留最近若干张足够用。
KEEP_SCREENSHOTS = 60


def _require_windows(what: str) -> None:
    if not IS_WINDOWS:
        raise ToolError(f"{what} 目前仅支持 Windows 平台。")


# ---- 纯逻辑（无 Win32 依赖，测试直接覆盖） ----


def _to_abs(px: int, py: int, vx: int, vy: int, cx: int, cy: int) -> tuple[int, int]:
    """虚拟屏幕像素坐标 → SendInput 的 0..65535 绝对坐标。"""
    nx = round((px - vx) * 65535 / max(cx - 1, 1))
    ny = round((py - vy) * 65535 / max(cy - 1, 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


_VK_NAMED: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "printscreen": 0x2C, "prtsc": 0x2C, "capslock": 0x14, "numlock": 0x90,
}
for _i in range(1, 13):
    _VK_NAMED[f"f{_i}"] = 0x6F + _i  # F1=0x70 .. F12=0x7B

_MOD_VKS = {0x11, 0x12, 0x10, 0x5B}  # ctrl / alt / shift / win


def parse_hotkeys(keys: str) -> list[int]:
    """解析 "ctrl+shift+esc" 形式的组合键为 VK 序列（修饰键在前，最后一个是主键）。"""
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    if not parts:
        raise ToolError("keys 参数为空，例如 \"ctrl+s\"、\"win+r\"、\"enter\"。")
    vks: list[int] = []
    for p in parts:
        vk = _VK_NAMED.get(p)
        if vk is None:
            if len(p) == 1 and p.isalnum():
                vk = ord(p.upper())
            else:
                raise ToolError(
                    f"不支持的按键名：{p}（支持 ctrl/alt/shift/win/enter/esc/tab/"
                    "方向键/f1-f12/单个字母数字 等）"
                )
        vks.append(vk)
    return vks


# ---- Win32 层（仅 Windows） ----

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_UNICODE = 0x0004
    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010
    _MOUSEEVENTF_MIDDLEDOWN = 0x0020
    _MOUSEEVENTF_MIDDLEUP = 0x0040
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSEEVENTF_VIRTUALDESK = 0x4000
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _WHEEL_DELTA = 120
    _WM_CLOSE = 0x0010
    _SW_MINIMIZE, _SW_MAXIMIZE, _SW_RESTORE = 6, 3, 9

    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    _user32.GetSystemMetrics.restype = ctypes.c_int
    # 64 位下句柄/指针必须声明完整类型，否则按 c_int 返回会被截断
    _user32.GetClipboardData.argtypes = (wintypes.UINT,)
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    _user32.IsIconic.argtypes = (wintypes.HWND,)
    _user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    _user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    _user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _virtual_screen() -> tuple[int, int, int, int]:
    """(vx, vy, cx, cy)：虚拟屏幕原点与尺寸。"""
    vx = _user32.GetSystemMetrics(76)
    vy = _user32.GetSystemMetrics(77)
    cx = _user32.GetSystemMetrics(78)
    cy = _user32.GetSystemMetrics(79)
    if cx <= 0 or cy <= 0:  # 异常兜底：退化为主屏
        return 0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return vx, vy, cx, cy


def _send(events: list[Any]) -> None:
    arr = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise OSError(f"SendInput 只注入了 {sent}/{len(events)} 个事件")


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    inp = _INPUT(type=_INPUT_MOUSE)
    inp.mi = _MOUSEINPUT(
        dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0
    )
    return inp


def _key_event(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    inp = _INPUT(type=_INPUT_KEYBOARD)
    inp.ki = _KEYBDINPUT(
        wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0
    )
    return inp


def _flush_move(ev: list[Any], px: int, py: int, vx: int, vy: int, cx: int, cy: int) -> None:
    ax, ay = _to_abs(px, py, vx, vy, cx, cy)
    ev.append(
        _mouse_event(
            _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK, dx=ax, dy=ay
        )
    )
    _send(ev)


def _mouse_action_sync(
    action: str, x: int | None, y: int | None, x2: int | None, y2: int | None, amount: int
) -> str:
    vx, vy, cx, cy = _virtual_screen()
    ev: list[Any] = []
    if x is not None and y is not None:
        _flush_move(ev, x, y, vx, vy, cx, cy)

    if action == "move":
        if x is None or y is None:
            raise ToolError("move 需要 x 和 y")
        return f"已移动鼠标到 ({x}, {y})"

    if action == "scroll":
        ev.append(_mouse_event(_MOUSEEVENTF_WHEEL, data=amount * _WHEEL_DELTA))
        _send(ev)
        return f"已滚动 {amount} 格（正=上/负=下）" + (f"，位置 ({x}, {y})" if x is not None else "")

    if action == "drag":
        if x is None or y is None or x2 is None or y2 is None:
            raise ToolError("drag 需要 起点 x/y 和 终点 x2/y2")
        ev.append(_mouse_event(_MOUSEEVENTF_LEFTDOWN))
        _send(ev)
        time.sleep(0.05)
        steps = 24
        for i in range(1, steps + 1):
            ix = round(x + (x2 - x) * i / steps)
            iy = round(y + (y2 - y) * i / steps)
            ev = []
            _flush_move(ev, ix, iy, vx, vy, cx, cy)
            time.sleep(0.008)
        ev = [_mouse_event(_MOUSEEVENTF_LEFTUP)]
        _send(ev)
        return f"已从 ({x}, {y}) 拖拽到 ({x2}, {y2})"

    down, up = {
        "click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
        "right_click": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
        "middle_click": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
    }[action]
    clicks = 2 if action == "double_click" else 1
    for i in range(clicks):
        if i:
            time.sleep(0.04)  # 系统双击间隔内的第二次点击
        ev = [_mouse_event(down), _mouse_event(up)]
        _send(ev)
        time.sleep(0.02)
    at = f" @ ({x}, {y})" if x is not None and y is not None else ""
    return f"已执行 {action}{at}"


def _type_text_sync(text: str, aborted: asyncio.Event | None) -> str:
    done = 0
    ev: list[Any] = []
    for ch in text:
        if aborted is not None and aborted.is_set():
            return f"已输入 {done}/{len(text)} 字符后被用户中止"
        if ch == "\n":
            ev = [_key_event(vk=0x0D), _key_event(vk=0x0D, flags=_KEYEVENTF_KEYUP)]
        elif ch == "\t":
            ev = [_key_event(vk=0x09), _key_event(vk=0x09, flags=_KEYEVENTF_KEYUP)]
        else:
            ev = [
                _key_event(scan=ord(ch), flags=_KEYEVENTF_UNICODE),
                _key_event(scan=ord(ch), flags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP),
            ]
        _send(ev)
        done += 1
        time.sleep(0.002)
    return f"已输入 {done} 个字符"


def _vk_name(vk: int) -> str:
    for k, v in _VK_NAMED.items():
        if v == vk:
            return k
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:  # A-Z / 0-9
        return chr(vk)
    return hex(vk)


def _hotkey_sync(vks: list[int]) -> str:
    names = "+".join(_vk_name(v) for v in vks)
    if len(vks) == 1:
        _send([_key_event(vk=vks[0]), _key_event(vk=vks[0], flags=_KEYEVENTF_KEYUP)])
        return f"已按下 {names}"
    mods, main = vks[:-1], vks[-1]
    ev = [_key_event(vk=v) for v in mods]
    if ev:
        _send(ev)
        time.sleep(0.03)
    _send([_key_event(vk=main), _key_event(vk=main, flags=_KEYEVENTF_KEYUP)])
    ev = [_key_event(vk=v, flags=_KEYEVENTF_KEYUP) for v in reversed(mods)]
    if ev:
        _send(ev)
    return "已按下组合键 " + "+".join(names)


# ---- 窗口 / 剪贴板 的 Win32 同步实现 ----


def _window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _proc_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
    finally:
        _kernel32.CloseHandle(h)


def _list_windows_sync() -> list[dict]:
    rows: list[dict] = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):  # noqa: ANN001 - ctypes 回调签名
        if _user32.IsWindowVisible(hwnd):
            title = _window_title(hwnd)
            if title.strip():
                rows.append(
                    {
                        "hwnd": hwnd,
                        "title": title,
                        "proc": _proc_name(hwnd),
                        "minimized": bool(_user32.IsIconic(hwnd)),
                    }
                )
        return True

    _user32.EnumWindows(_cb, 0)
    fg = _user32.GetForegroundWindow()
    for r in rows:
        r["active"] = r["hwnd"] == fg
    return rows


def _find_window(title_sub: str) -> dict:
    needle = title_sub.strip().casefold()
    if not needle:
        raise ToolError("title 不能为空")
    rows = _list_windows_sync()
    hits = [r for r in rows if needle in r["title"].casefold()]
    if not hits:
        raise ToolError(f"找不到标题包含 {title_sub!r} 的可见窗口（可先用 window_list 查看）")
    return hits[0]


def _activate_window_sync(hwnd: int) -> bool:
    """前置激活窗口；Windows 不允许后台进程直接抢前台，用 AttachThreadInput 绕过。"""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.08)
    fg = _user32.GetForegroundWindow()
    cur = _kernel32.GetCurrentThreadId()
    fg_tid = _user32.GetWindowThreadProcessId(fg, None)
    tgt_tid = _user32.GetWindowThreadProcessId(hwnd, None)
    attached = False
    if fg and fg_tid and fg_tid != cur:
        attached = bool(_user32.AttachThreadInput(fg_tid, cur, True))
    if tgt_tid and tgt_tid != cur:
        _user32.AttachThreadInput(tgt_tid, cur, True)
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if tgt_tid and tgt_tid != cur:
            _user32.AttachThreadInput(tgt_tid, cur, False)
        if attached:
            _user32.AttachThreadInput(fg_tid, cur, False)
    if _user32.GetForegroundWindow() != hwnd:
        # 兜底：模拟一次 ALT 按键满足前台锁的「最近输入」条件后再试
        _send([_key_event(vk=0x12), _key_event(vk=0x12, flags=_KEYEVENTF_KEYUP)])
        _user32.SetForegroundWindow(hwnd)
    return _user32.GetForegroundWindow() == hwnd


def _clipboard_open(retries: int = 5) -> bool:
    for i in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.05 * (i + 1))
    return False


def _clipboard_read_sync() -> str:
    CF_UNICODETEXT = 13
    if not _clipboard_open():
        raise ToolError("无法打开剪贴板（被其他程序占用），请稍后重试")
    try:
        if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = _kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def _clipboard_write_sync(text: str) -> None:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    if not _clipboard_open():
        raise ToolError("无法打开剪贴板（被其他程序占用），请稍后重试")
    try:
        _user32.EmptyClipboard()
        size = (len(text) + 1) * 2
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            raise ToolError("剪贴板内存分配失败")
        p = _kernel32.GlobalLock(h)
        if not p:
            _kernel32.GlobalFree(h)
            raise ToolError("剪贴板内存锁定失败")
        try:
            ctypes.memmove(p, ctypes.create_unicode_buffer(text), size)
        finally:
            _kernel32.GlobalUnlock(h)
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):  # 成功后 h 归系统所有
            _kernel32.GlobalFree(h)
            raise ToolError("写入剪贴板失败")
    finally:
        _user32.CloseClipboard()


# ---- 抓屏（模块级函数，便于测试替换） ----


def _grab_image(all_screens: bool):
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=all_screens)


def _encode_png(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _prune_screenshots(shots: Path) -> None:
    """截图副本保留策略：超过 KEEP_SCREENSHOTS 时删最旧的。

    只处理 .png 文件（不动用户自己丢进来的其它东西），删除失败静默——
    清目录是附带的维护动作，不该让截图本身报错。
    """
    try:
        files = [f for f in shots.glob("*.png") if f.is_file()]
    except OSError:
        return
    if len(files) <= KEEP_SCREENSHOTS:
        return
    try:
        files.sort(key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    for old in files[: len(files) - KEEP_SCREENSHOTS]:
        try:
            old.unlink()
        except OSError:
            pass


def _capture(screen: str) -> tuple[Any, tuple[int, int], tuple[int, int]]:
    """抓屏，返回 (图像, 覆盖区域原点, 虚拟屏幕尺寸)。"""
    if IS_WINDOWS:
        vx, vy, cx, cy = _virtual_screen()
        origin = (0, 0) if screen == "primary" else (vx, vy)
    else:
        origin, (cx, cy) = (0, 0), (0, 0)
    img = _grab_image(all_screens=(screen == "all"))
    return img, origin, (cx, cy)


def _active_window_line() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        title = _window_title(hwnd)
        proc = _proc_name(hwnd)
        return f"- 活动窗口: {title}" + (f" ({proc})" if proc else "")
    except Exception:  # noqa: BLE001 - 活动窗口信息尽力而为
        return ""


# ---- 工具定义 ----


class ScreenshotArgs(BaseModel):
    screen: Literal["primary", "all"] = Field(
        default="primary", description="截取主屏还是全部屏幕（多显示器时用 all）"
    )


class ScreenshotTool(Tool):
    name = "screenshot"
    description = (
        "截取屏幕画面，图像会附加到对话中（可直接查看），并返回活动窗口等信息。"
        "操作电脑前先截图定位；返回文本会说明坐标原点与缩放比。"
    )
    safety = Safety.READONLY
    args_model = ScreenshotArgs

    async def run(self, args: ScreenshotArgs, ctx: ToolContext) -> str:
        if not getattr(ctx, "supports_vision", True):
            raise ToolError(
                "当前模型不支持图片输入，截图它看不到，无法据此操作界面。"
                "请在输入框的模型选择器里换一个多模态模型（如 GLM-4V、Kimi、GPT-4o 等），"
                "或改用 window_list / clipboard_read 等纯文本方式了解屏幕状态。"
            )
        try:
            img, origin, _vs = await asyncio.to_thread(_capture, args.screen)
        except Exception as e:
            raise ToolError(f"截屏失败：{e}") from e
        w, h = img.size
        scale = 1.0
        if w > MAX_IMAGE_WIDTH:
            scale = MAX_IMAGE_WIDTH / w
            img = img.resize((MAX_IMAGE_WIDTH, round(h * scale)))
        png = await asyncio.to_thread(_encode_png, img)

        ctx.images.append(ImageBlock(media_type="image/png", data=base64.b64encode(png).decode()))

        shots = skysheep_home() / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        try:
            path.write_bytes(png)
            _prune_screenshots(shots)
        except OSError:
            pass  # 副本保存失败不影响主流程

        lines = [
            f"截图完成（{args.screen}）: {w}x{h} px，图像已附加在本条消息之后（模型可直接查看）",
            f"- 副本已保存: {path}",
        ]
        if scale < 1.0:
            lines.append(
                f"- 注意: 图像已从 {w}x{h} 缩放为 {img.size[0]}x{img.size[1]} 显示"
                f"（比例 {scale:.3f}）；屏幕坐标 = 图内坐标 ÷ {scale:.3f} + origin"
            )
        if origin == (0, 0):
            lines.append("- 坐标系: 图内坐标即虚拟屏幕坐标（origin=(0,0)），mouse 的 x/y 直接使用")
        else:
            lines.append(f"- 坐标系: 本图覆盖多屏，origin={origin}；屏幕坐标 = 图内坐标 + origin")
        active = _active_window_line()
        if active:
            lines.append(active)
        lines.append("- 提示: 点击前先确认活动窗口是否为目标窗口；需要时用 window activate 先置前")
        return truncate_output("\n".join(lines))


class WindowListArgs(BaseModel):
    pass


class WindowListTool(Tool):
    name = "window_list"
    description = (
        "列出当前所有可见的顶层窗口（标题、进程名、是否最小化、哪个是活动窗口），"
        "用于在 window activate/close 前找到目标窗口。只读操作。"
    )
    safety = Safety.READONLY
    args_model = WindowListArgs

    async def run(self, args: WindowListArgs, ctx: ToolContext) -> str:
        _require_windows("window_list")
        rows = await asyncio.to_thread(_list_windows_sync)
        if not rows:
            return "当前没有可见的顶层窗口。"
        limit = 40
        lines = [f"共 {len(rows)} 个可见窗口（按 Z 序，★ 为当前活动窗口）："]
        for r in rows[:limit]:
            mark = "★" if r["active"] else " "
            extra = "（最小化）" if r["minimized"] else ""
            proc = f" ｜ {r['proc']}" if r["proc"] else ""
            lines.append(f"{mark} {r['title']}{proc}{extra}")
        if len(rows) > limit:
            lines.append(f"…（其余 {len(rows) - limit} 个省略）")
        lines.append("提示: 用 window 工具的 title 参数按标题子串匹配操作。")
        return truncate_output("\n".join(lines))


class ClipboardReadArgs(BaseModel):
    pass


class ClipboardReadTool(Tool):
    name = "clipboard_read"
    description = (
        "读取当前剪贴板中的文本内容。剪贴板可能刚被用户复制过密码等敏感"
        "信息，属于高敏感数据源，调用前会请求用户确认。"
    )
    # 安全审查 D4：剪贴板常驻密码管理器刚复制的凭据，不能与普通只读工具一样
    # 自动放行——与 clipboard_write 同一确认姿态。
    safety = Safety.WRITE
    args_model = ClipboardReadArgs

    async def run(self, args: ClipboardReadArgs, ctx: ToolContext) -> str:
        _require_windows("clipboard_read")
        text = await asyncio.to_thread(_clipboard_read_sync)
        if not text:
            return "剪贴板中没有文本内容。"
        return truncate_output(f"剪贴板文本（{len(text)} 字符）：\n{text}")


class ClipboardWriteArgs(BaseModel):
    text: str = Field(description="要写入剪贴板的文本")


class ClipboardWriteTool(Tool):
    name = "clipboard_write"
    description = (
        "把文本写入剪贴板（之后可用 keyboard 的 keys=ctrl+v 粘贴到应用里，"
        "适合粘贴长文本）。写操作，会先请求用户确认。"
    )
    safety = Safety.WRITE
    args_model = ClipboardWriteArgs

    def arg_text(self, input_dict: dict) -> str:
        text = str(input_dict.get("text", ""))
        return "clipboard " + (text[:120] + "…" if len(text) > 120 else text)

    async def run(self, args: ClipboardWriteArgs, ctx: ToolContext) -> str:
        _require_windows("clipboard_write")
        await asyncio.to_thread(_clipboard_write_sync, args.text)
        return f"已写入剪贴板（{len(args.text)} 字符）"


class MouseArgs(BaseModel):
    action: Literal[
        "move", "click", "double_click", "right_click", "middle_click", "drag", "scroll"
    ] = Field(description="鼠标动作")
    x: int | None = Field(default=None, description="目标 X（虚拟屏幕像素坐标，截图文本有说明）")
    y: int | None = Field(default=None, description="目标 Y（虚拟屏幕像素坐标）")
    x2: int | None = Field(default=None, description="drag 终点 X")
    y2: int | None = Field(default=None, description="drag 终点 Y")
    amount: int = Field(default=3, description="scroll 滚动格数，正=向上/负=向下")

    @model_validator(mode="after")
    def _check(self) -> MouseArgs:
        if (self.x is None) != (self.y is None):
            raise ValueError("x 和 y 必须同时提供或同时省略")
        if self.action in ("move", "drag") and self.x is None:
            raise ValueError(f"{self.action} 需要提供起点 x/y")
        if self.action == "drag" and (self.x2 is None or self.y2 is None):
            raise ValueError("drag 需要提供终点 x2/y2")
        return self


class MouseTool(Tool):
    name = "mouse"
    description = (
        "控制鼠标：移动/单击/双击/右键/中键/拖拽/滚轮。x/y 用虚拟屏幕像素坐标"
        "（先 screenshot 定位，其返回文本说明了图内坐标与屏幕坐标的换算）。"
        "直接操作用户电脑，高危操作，会先请求用户确认。"
    )
    safety = Safety.DANGEROUS
    args_model = MouseArgs

    def arg_text(self, input_dict: dict) -> str:
        d = dict(input_dict)
        action = str(d.get("action", ""))
        parts = [action]
        if d.get("x") is not None:
            parts.append(f"x={d['x']} y={d['y']}")
        if d.get("x2") is not None:
            parts.append(f"x2={d['x2']} y2={d['y2']}")
        if action == "scroll":
            parts.append(f"amount={d.get('amount', 3)}")
        return " ".join(parts)

    async def run(self, args: MouseArgs, ctx: ToolContext) -> str:
        _require_windows("mouse")
        try:
            return await asyncio.to_thread(
                _mouse_action_sync,
                args.action, args.x, args.y, args.x2, args.y2, args.amount,
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"鼠标操作失败：{e}") from e


class KeyboardArgs(BaseModel):
    text: str | None = Field(default=None, description="要输入的文本（支持中文；\\n=回车，\\t=Tab）")
    keys: str | None = Field(
        default=None, description='组合键，如 "ctrl+s"、"win+r"、"enter"、"alt+f4"'
    )

    @model_validator(mode="after")
    def _check(self) -> KeyboardArgs:
        if (self.text is None) == (self.keys is None):
            raise ValueError("text 与 keys 必须二选一")
        return self


class KeyboardTool(Tool):
    name = "keyboard"
    description = (
        "向当前活动窗口发送键盘输入：text 逐字符输入（支持中文，先确认焦点在正确"
        "的输入框），或 keys 发送组合键。直接操作用户电脑，高危操作，会先请求用户确认。"
    )
    safety = Safety.DANGEROUS
    args_model = KeyboardArgs

    def arg_text(self, input_dict: dict) -> str:
        if input_dict.get("keys") is not None:
            return f"hotkey {input_dict['keys']}"
        return f"type {input_dict.get('text', '')}"

    async def run(self, args: KeyboardArgs, ctx: ToolContext) -> str:
        _require_windows("keyboard")
        if args.text is not None:
            if len(args.text) > MAX_TYPE_CHARS:
                raise ToolError(
                    f"文本过长（{len(args.text)} 字符，上限 {MAX_TYPE_CHARS}）。"
                    "请改用 clipboard_write 写入后按 keys=\"ctrl+v\" 粘贴。"
                )
            try:
                return await asyncio.to_thread(_type_text_sync, args.text, ctx.aborted)
            except Exception as e:
                raise ToolError(f"键盘输入失败：{e}") from e
        try:
            vks = parse_hotkeys(args.keys or "")
            return await asyncio.to_thread(_hotkey_sync, vks)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"组合键发送失败：{e}") from e


class WindowArgs(BaseModel):
    action: Literal["activate", "minimize", "maximize", "close"] = Field(
        description="窗口操作：activate=置前，minimize=最小化，maximize=最大化，close=关闭"
    )
    title: str = Field(description="窗口标题（不区分大小写的子串匹配）")


class WindowTool(Tool):
    name = "window"
    description = (
        "管理桌面窗口：按标题子串找到窗口后置前/最小化/最大化/关闭。"
        "close 会向窗口发送关闭消息（应用可能弹保存确认框）。"
        "影响用户桌面，高危操作，会先请求用户确认。"
    )
    safety = Safety.DANGEROUS
    args_model = WindowArgs

    def arg_text(self, input_dict: dict) -> str:
        return f"{input_dict.get('action', '')} {input_dict.get('title', '')}".strip()

    async def run(self, args: WindowArgs, ctx: ToolContext) -> str:
        _require_windows("window")
        try:
            target = await asyncio.to_thread(_find_window, args.title)
        except ToolError:
            raise
        hwnd, title = target["hwnd"], target["title"]

        def _sync() -> str:
            if args.action == "activate":
                ok = _activate_window_sync(hwnd)
                if not ok:
                    return f"已尝试置前「{title}」，但系统未确认其成为前台窗口"
                return f"已把「{title}」置为前台窗口"
            if args.action == "minimize":
                _user32.ShowWindow(hwnd, _SW_MINIMIZE)
                return f"已最小化「{title}」"
            if args.action == "maximize":
                _user32.ShowWindow(hwnd, _SW_MAXIMIZE)
                return f"已最大化「{title}」"
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            return f"已向「{title}」发送关闭消息（若应用弹保存确认框，可再截图查看）"

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            raise ToolError(f"窗口操作失败：{e}") from e
