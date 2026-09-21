"""SkySheep 桌面启动器：不显示终端窗口的运行入口。

双击 SkySheep.pyw（开发态）或 SkySheep.exe（打包态）都会进到这里：

- 无控制台运行时 ``sys.stdout`` / ``sys.stderr`` 可能是 None，uvicorn 与 rich
  一旦调用 ``isatty()`` 就会崩，所以把它们指向日志文件。这里必须用**真实文件
  对象**：pywebview 启动时依赖标准流的完整接口，换成自定义包装器会让窗口
  静默创建失败（曾踩过）。
- 任何启动异常都不静默失败：完整堆栈写日志，并弹 Windows 消息框告诉用户日志在哪。
- 单实例：重复双击不新开服务端口，而是把已经在跑的窗口唤到前台。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

APP_TITLE = "SkySheep"
MUTEX_NAME = "Local\\SkySheepDesktopSingleton"
LOG_MAX_BYTES = 1_000_000
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3
# 旧实例"占着互斥体却一直没有可见窗口"的宽限期：正常启动 splash 2~3 秒内就可见，
# 超过这个时长仍看不到窗口就判定为卡死实例，结束它并接管（见 _stale_holder_pid）
STALE_GRACE_SECONDS = 12.0
# 窗口创建的兜底看门狗：超过它还没建出窗体就退出，把单实例互斥体还给系统
STARTUP_WATCHDOG_SECONDS = 30.0
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_MUTEX_HANDLE = None
_LAUNCH_T0 = time.monotonic()  # 启动计时基准：日志里记录"窗口多久后可见"


def app_dir() -> Path:
    """启动器所在目录：开发态是 engine/，打包态是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _home_dir() -> Path:
    home = os.environ.get("SKYSHEEP_HOME")
    return Path(home).expanduser() if home else Path.home() / ".skysheep"


def log_path() -> Path:
    return _home_dir() / "logs" / "desktop.log"


def _pid_path() -> Path:
    """单实例记录：当前桌面实例的 pid + 进程创建时刻（防 pid 复用）。"""
    return _home_dir() / "desktop.pid"


def _log(message: str) -> None:
    """直接追加写日志文件；失败也不能影响启动。"""
    try:
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def _rotate_log() -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            rotated = path.parent / (path.name + ".1")
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
    except OSError:
        pass


def _ensure_streams() -> None:
    """windowed 模式下标准流可能是 None，指到日志文件保证第三方库正常工作。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _setup_logging() -> None:
    """让 uvicorn / 引擎的 logging 也稳稳落到文件里。"""
    try:
        handler = logging.FileHandler(log_path(), encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except OSError:
        pass


def _acquire_single_instance() -> bool:
    """取得单实例互斥体；已在运行时返回 False（进程退出后系统自动释放）。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    # last error 必须紧跟调用读取，中间不能插入其它可能覆盖它的操作
    already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    if not handle:
        return True
    globals()["_MUTEX_HANDLE"] = handle  # 持有句柄，避免被回收后互斥体消失
    return not already_running


def _release_stale_mutex() -> None:
    """放弃本进程对旧实例互斥体的句柄。

    CreateMutexW 在互斥体已存在时也会返回一个句柄；不关掉它的话，即便旧进程
    已经退出，互斥体对象仍会被本进程撑着，重试 acquire 永远拿到
    ERROR_ALREADY_EXISTS。
    """
    global _MUTEX_HANDLE
    handle = _MUTEX_HANDLE
    if handle:
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass
        _MUTEX_HANDLE = None


def _window_pid(hwnd: int) -> int:
    """窗口所属进程 pid；失败返回 0。"""
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _is_hung(hwnd: int) -> bool:
    """窗口所属线程是否已无响应（Windows 判定：约 5 秒不泵消息）。"""
    try:
        user32 = ctypes.windll.user32
        user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
        return bool(user32.IsHungAppWindow(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def _find_main_hwnd() -> int:
    """按标题找 SkySheep 主窗口句柄（可见窗口优先）；找不到返回 0。

    读标题前先跳过卡死窗口：GetWindowTextW 会给目标窗口发 WM_GETTEXT，
    对方线程停转时本进程会被一起挂住——本次"双击打不开"的第二处卡点
    就在这里（py-spy：三个进程都停在 GetWindowText / ShowWindow）。
    """
    user32 = ctypes.windll.user32
    candidates: list[int] = []

    def _collect(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length and not _is_hung(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip() == APP_TITLE:
                candidates.append(int(hwnd))
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_collect)
    user32.EnumWindows(callback, None)
    # 多个窗口时可见的优先（托盘的隐藏辅助窗口同名也算命中）
    for hwnd in candidates:
        if user32.IsWindowVisible(hwnd):
            return hwnd
    return candidates[0] if candidates else 0


def _focus_existing_window() -> bool:
    """把已经在跑的 SkySheep 主窗口唤到前台，成功返回 True。

    只用不阻塞的调用：ShowWindow 是同步的，目标窗口线程卡死时本进程会被
    一起拖住（实测连一行日志都写不出）；ShowWindowAsync 只投递请求立即返回。
    """
    hwnd = _find_main_hwnd()
    if not hwnd or _is_hung(hwnd):
        return False
    user32 = ctypes.windll.user32
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.ShowWindowAsync(ctypes.c_void_p(hwnd), SW_RESTORE)
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    return True


def _wait_mutex_free(seconds: float) -> bool:
    """等旧实例退出、互斥体被系统释放；拿到互斥体返回 True。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _acquire_single_instance():
            return True
        _release_stale_mutex()
        time.sleep(0.2)
    return False


def _process_start_time(pid: int) -> float | None:
    """进程创建时刻（epoch 秒）；进程不存在返回 None。

    pid 会被系统回收复用，只有"pid + 创建时刻"才能唯一标识一个进程——
    接管前用它确认目标还是当初记下的那个 SkySheep，避免误杀同号 pid 的
    其它程序。
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, sys_t, user_t = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(sys_t), ctypes.byref(user_t),
        )
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600  # FILETIME(1601) → epoch(1970)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _write_pid_record() -> None:
    """记下当前实例的 pid 与创建时刻，供后来者识别卡死实例。"""
    try:
        pid = os.getpid()
        created = _process_start_time(pid) or 0.0
        path = _pid_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid} {created:.3f}", encoding="utf-8")
    except OSError:
        pass


def _read_pid_record() -> tuple[int, float] | None:
    try:
        raw = _pid_path().read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(raw) != 2:
        return None
    try:
        return int(raw[0]), float(raw[1])
    except ValueError:
        return None


def _stale_holder_pid() -> int | None:
    """找出"占着互斥体却不可用"的旧实例 pid；确认不了返回 None。

    两条证据链（都以创建时刻校验身份，防 pid 复用误杀）：
    ① 有标题为 SkySheep 的窗口 → 取窗口属主，且窗口已无响应（IsHungAppWindow）；
    ② 连窗口都没有 → 用 pid 记录，且实例存活已超过启动宽限期
      （正常启动 splash 2~3 秒可见，窗口迟迟不出现就是卡死信号）。
    """
    hwnd = _find_main_hwnd()
    if hwnd:
        pid = _window_pid(hwnd)
        if pid and pid != os.getpid() and _is_hung(hwnd) and _process_start_time(pid) is not None:
            return pid
        return None
    rec = _read_pid_record()
    if not rec:
        return None
    pid, created = rec
    if pid == os.getpid():
        return None
    actual = _process_start_time(pid)
    if actual is None or abs(actual - created) > 1.0:
        return None  # 记录过期或 pid 被复用
    age = time.time() - actual
    if age < STALE_GRACE_SECONDS:
        return None  # 可能只是还在启动
    return pid


def _terminate_process(pid: int) -> bool:
    """强杀指定进程（仅用于确认卡死的自家实例）。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return bool(kernel32.TerminateProcess(ctypes.c_void_p(handle), 1))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def alert(message: str) -> None:
    """无控制台时的可见报错通道。"""
    try:
        user32 = ctypes.windll.user32
        user32.MessageBoxW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        user32.MessageBoxW(None, message, APP_TITLE, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _report_failure(title: str) -> None:
    detail = traceback.format_exc()
    _log(detail)
    alert(
        f"{title}。\n\n"
        f"完整信息已写入日志：\n{log_path()}\n\n"
        f"出错位置：\n{detail.strip()[-900:]}"
    )


def static_dir() -> Path:
    """前端静态资源目录：开发态在源码树里，打包态在 _MEIPASS。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "skysheep" / "server" / "static"  # noqa: SLF001
    return app_dir() / "src" / "skysheep" / "server" / "static"


def paint_caption(hwnd: int, paper_rgb=(244, 236, 216), ink_rgb=(29, 26, 22)) -> None:
    """把窗口标题栏刷成纸色、文字与边框用墨色（Win11 DWM；旧系统自动跳过）。

    不处理的话标题栏是刺眼的白色，和纸色卡体割裂（用户实测反馈）。
    """
    try:
        if sys.getwindowsversion().build < 22000:
            return  # Win10 没有 DWMWA_CAPTION_COLOR
        dwm = ctypes.windll.dwmapi

        def _set(attr: int, rgb: tuple[int, int, int]) -> None:
            # COLORREF = 0x00BBGGRR
            value = ctypes.c_uint((rgb[2] << 16) | (rgb[1] << 8) | rgb[0])
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(value), 4)

        _set(35, paper_rgb)  # DWMWA_CAPTION_COLOR 标题栏底色
        _set(36, ink_rgb)  # DWMWA_TEXT_COLOR 标题文字
        _set(34, ink_rgb)  # DWMWA_BORDER_COLOR 窗口边框
        # pywebview 按系统深色模式设了沉浸式深色标题栏（20=1，深色系统下是黑）。
        # 不显式关掉的话，窗口最大化/还原等 DWM 重绘场景会把标题栏画回系统黑
        # （只在窗口以最大化创建时必现，普通路径不触发）。
        _set(20, (0, 0, 0))  # DWMWA_USE_IMMERSIVE_DARK_MODE = 关
    except Exception:
        pass


class _WINDOWPLACEMENT(ctypes.Structure):
    """GetWindowPlacement 出参：showCmd + rcNormalPosition（物理像素工作区坐标）。"""

    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def _capture_window_geometry(window) -> dict | None:
    """采集主窗口几何（逻辑像素 + 是否最大化），供退出前落盘；失败返回 None。

    统一取 GetWindowPlacement 的 rcNormalPosition：窗口最大化时它仍记录着
    "还原后"的正常位置，配合 maximized 标志就能完整还原上次状态；而且它
    不像 GetWindowRect 那样含 Win10/11 DWM 阴影的隐形边框偏差。物理像素
    按窗口 DPI 折算回逻辑像素，与 create_window 的 width/height/x/y 同一
    坐标系（pywebview 会按窗口所在屏的 DPI 换算）。
    """
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        hwnd = int(native.Handle.ToInt64())
        user32 = ctypes.windll.user32
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if not user32.GetWindowPlacement(ctypes.c_void_p(hwnd), ctypes.byref(wp)):
            return None
        dpi = 96
        try:
            dpi = user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 96
        except Exception:
            pass
        scale = dpi / 96.0
        if scale <= 0:
            scale = 1.0
        r = wp.rcNormalPosition
        width = round((r.right - r.left) / scale)
        height = round((r.bottom - r.top) / scale)
        x = round(r.left / scale)
        y = round(r.top / scale)
        if width <= 0 or height <= 0:
            return None
        return {
            "width": width,
            "height": height,
            "x": x,
            "y": y,
            "maximized": wp.showCmd == SW_SHOWMAXIMIZED,
        }
    except Exception:
        return None


def _remember_window(window) -> None:
    """彻底退出前把窗口几何存进 window_state.json；任何失败都静默，不能挡退出。"""
    try:
        from skysheep import windowstate  # noqa: PLC0415  引擎导入在启动后期才发生

        geometry = _capture_window_geometry(window)
        if geometry is not None:
            windowstate.save_state(geometry)
    except Exception:
        pass


# ---------------- 系统托盘（原生 Win32，独立线程） ----------------
# 不用 WinForms 的 NotifyIcon：它的事件在 pywebview 的消息泵下不可靠
# （右键菜单弹不出，实测）。这里用 Shell_NotifyIcon + TrackPopupMenu 的
# 正统写法：托盘线程自建回调窗口，鼠标事件直达 WndProc，不依赖任何
# .NET 事件层与 pywebview 的消息循环。

WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
MF_STRING = 0x0
IDM_OPEN, IDM_QUIT = 1, 2
_TRAY_MSG = 0x8000 + 0x5F5  # WM_APP 范围内的托盘回调消息
_TRAY_QUIT_MSG = 0x8000 + 0x5F6  # 请求托盘线程收尾退出
_TRAY_WND_CLASS = "SkySheepTrayWnd"

_TRAY_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,  # LRESULT（wintypes 没有 LRESULT，用 c_ssize_t）
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _TRAY_WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def _bind_tray_api() -> tuple:
    """声明托盘用到的 Win32 函数签名（64 位下句柄不能按默认 int 传）。"""
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    user32.DefWindowProcW.restype = ctypes.c_ssize_t  # LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.LoadImageW.restype = wintypes.HICON
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadIconW.restype = wintypes.HICON
    user32.TrackPopupMenuEx.restype = wintypes.BOOL
    user32.TrackPopupMenuEx.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, ctypes.c_void_p,
    ]
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
    ]
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    return user32, shell32


def _tray_worker(icon_path: str, holder: dict) -> None:
    """托盘线程：建回调窗口、注册图标，消息循环里处理点击。失败只记日志。"""
    log = holder["log"]
    try:
        user32, shell32 = _bind_tray_api()

        def _popup_menu(hwnd) -> None:
            # 先 SetForegroundWindow：菜单才能在点击外部时自动关闭（Win32 惯例）
            user32.SetForegroundWindow(hwnd)
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, IDM_OPEN, "打开 SkySheep")
            user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "退出 SkySheep")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            cmd = user32.TrackPopupMenuEx(
                menu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                pt.x, pt.y, hwnd, None,
            )
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            user32.DestroyMenu(menu)
            if cmd == IDM_OPEN:
                log("tray: open requested")
                holder["open"]()
            elif cmd == IDM_QUIT:
                log("tray: quit requested")
                holder["quit"]()

        def _wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == _TRAY_MSG:
                    click = lparam & 0xFFFF  # Version 0 语义：lParam = 鼠标消息
                    if click in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        log("tray: open requested")
                        holder["open"]()
                    elif click == WM_RBUTTONUP:
                        _popup_menu(hwnd)
                    return 0
                if msg == _TRAY_QUIT_MSG:
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    nid = holder.get("nid")
                    if nid is not None:
                        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                    user32.PostQuitMessage(0)
                    return 0
            except Exception as exc:
                log(f"tray wndproc error: {exc!r}")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        callback = _TRAY_WNDPROC(_wndproc)
        holder["wndproc"] = callback  # 保活：ctypes 回调被回收会崩进程

        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = callback
        wc.hInstance = hinst
        wc.lpszClassName = _TRAY_WND_CLASS
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            log("tray: RegisterClassExW failed")
            return
        hwnd = user32.CreateWindowExW(
            0, _TRAY_WND_CLASS, "SkySheep tray", 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not hwnd:
            log("tray: CreateWindowExW failed")
            return
        holder["hwnd_tray"] = int(hwnd)

        hicon = user32.LoadImageW(
            None, icon_path, IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXSMICON),
            user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE,
        )
        if not hicon:
            hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION 兜底
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = _TRAY_MSG
        nid.hIcon = hicon
        nid.szTip = "SkySheep（双击打开，右键菜单）"
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            log("tray: Shell_NotifyIconW(NIM_ADD) failed")
            return
        holder["nid"] = nid
        log("tray icon created (native)")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        log("tray thread exited")
    except Exception as exc:
        log(f"tray thread crashed: {exc!r}")


def _start_tray(static_dir_path: Path, holder: dict, actions: dict) -> None:
    """启动原生托盘线程；托盘不可用时只记日志，不阻塞主流程。"""
    icon_path = Path(static_dir_path) / "skysheep.ico"
    if not icon_path.exists():
        actions["log"]("tray icon unavailable: skysheep.ico missing")
        return
    holder.update(actions)
    thread = threading.Thread(
        target=_tray_worker, args=(str(icon_path), holder), daemon=True, name="SkySheepTray"
    )
    holder["thread"] = thread
    thread.start()


def _read_clipboard_text() -> str:
    """读当前剪贴板的纯文本（CF_UNICODETEXT）；失败返回空串。"""
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        if not user32.OpenClipboard(None):
            return ""
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def _start_global_hotkey(window, on_trigger) -> None:
    """注册全局热键：Ctrl+Alt+Space 唤起窗口 + 预填剪贴板。

    在独立线程里跑 GetMessage 循环；注册失败（冲突/无权限）静默跳过。
    仅 Windows 桌面模式调用；--browser 兜底路径不启用。
    """
    MOD_CONTROL, MOD_ALT, MOD_NOREPEAT = 0x0002, 0x0001, 0x4000
    VK_SPACE = 0x20
    WM_HOTKEY = 0x0312

    def _thread() -> None:
        try:
            ok = ctypes.windll.user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_SPACE)
        except Exception:
            ok = False
        if not ok:
            _log("全局热键注册失败（可能与其他程序冲突），已跳过")
        msg = ctypes.wintypes.MSG()
        try:
            while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == 1:
                    try:
                        window.show()
                    except Exception:
                        pass
                    try:
                        text = _read_clipboard_text().strip()
                        on_trigger(text)
                    except Exception:
                        pass
        finally:
            try:
                ctypes.windll.user32.UnregisterHotKey(None, 1)
            except Exception:
                pass

    threading.Thread(target=_thread, name="skysheep-hotkey", daemon=True).start()


def _start_theme_watchdog(window) -> None:
    """看板线程：前端切主题（app.apply_theme）后把标题栏与窗口底色刷成对应深浅。

    前端 → app.apply_theme → 后端 wintheme.set_theme_mode() 回写模式，这里每秒
    看一眼，变了就重刷 DWM 标题栏与窗口背景（背景色是整页刷新时露出的那一层，
    不跟着走的话夜墨主题下刷新会白闪一下）。初始值按 ui.json 存的偏好解析。
    """
    from skysheep import wintheme

    def _paint() -> None:
        """标题栏 + 窗口底色一起按当前主题刷（失败静默：老系统没有 DWM 属性）。"""
        try:
            wintheme.apply_caption_theme(window)
        except Exception:
            pass
        try:
            from System.Drawing import ColorTranslator  # noqa: PLC0415  pywebview 自带

            native = getattr(window, "native", None)
            if native is not None:
                native.BackColor = ColorTranslator.FromHtml(wintheme.window_background())
                wintheme.apply_window_icon(native, dark=wintheme.theme_is_dark())
        except Exception:
            pass

    def _thread() -> None:
        # 注册主窗口：前端切主题时 backend.apply_theme 会调 wintheme.refresh_now()
        # 事件驱动重刷（立即生效）；下面的轮询退化为兜底（窗口未就绪时错过的那次）。
        wintheme.register_window(window)
        resolved = wintheme.read_ui_theme()
        wintheme.set_theme_mode(resolved)
        _log(f"主题看板：启动解析主题 {resolved}（ui.json 偏好 / auto 跟随系统）")
        last = wintheme.current_theme_mode()
        # native 由 GUI 循环创建：等它就绪再首刷，保证底色与标题栏小图标不缺席
        deadline = time.monotonic() + 30
        while getattr(window, "native", None) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        _paint()
        while True:
            time.sleep(1.0)
            cur = wintheme.current_theme_mode()
            if cur != last:
                _log(f"主题看板：主题变化 {last} → {cur}（apply_theme 回写）")
            # 每秒重设自管标题栏颜色：DWM 在窗口最大化/还原、系统主题重绘等时机
            # 可能把 DWMWA_CAPTION_COLOR 画回系统默认色（深色系统下是黑），
            # pywebview 也会按系统深色模式设沉浸式深色标志。同值重设无视觉变化，
            # 成本可忽略；主题真的变了才动底色与图标。
            try:
                wintheme.apply_caption_theme(window)
            except Exception:
                pass
            if cur == last:
                continue
            last = cur
            _paint()

    threading.Thread(target=_thread, name="skysheep-theme", daemon=True).start()


def _show_on_first_paint(window) -> None:
    """记录启动动画页首帧时刻（窗口已改为创建后立即显示，见 create_window 处注释）。

    历史：1.8 之前窗口先 Show、WebView2 后初始化，空窗期露出未设置背景色的
    底色，被用户投诉"启动黑屏"，于是改成 hidden=True + 首帧后再 show。
    现在窗体 BackColor 已是主题纸色（与 splash 页同色），提前显示无缝，
    hidden=True 的理由不再成立——show 逻辑保留为兜底（幂等）与首帧计时。
    """
    shown = threading.Event()

    def _on_before_show(*_args, **_kwargs) -> None:
        _log(f"窗口已显示（窗体就绪，启动后 {time.monotonic() - _LAUNCH_T0:.2f}s）")

    try:
        window.events.before_show += _on_before_show
    except Exception:
        pass

    def _show(*_args, **_kwargs) -> None:
        if shown.is_set():
            return
        shown.set()
        _log(f"splash 首帧就绪（启动后 {time.monotonic() - _LAUNCH_T0:.2f}s）")
        try:
            window.show()  # 幂等：窗口通常已由创建流程显示
        except Exception:
            pass

    try:
        window.events.loaded += _show
    except Exception:
        _show()
        return

    def _fallback() -> None:
        if not window.events.loaded.wait(4):
            _log("splash 首帧超时（4s），直接显示窗口")
            _show()

    threading.Thread(target=_fallback, name="skysheep-first-paint", daemon=True).start()


def _start_launch_watchdog(window) -> None:
    """窗口迟迟建不出来就退出，把单实例互斥体还给系统。

    pywebview 的 create_window 在 WebView2/.NET 初始化异常时会永久阻塞：
    主线程停在等 GUI 线程的轮询里，既不报错也不建窗口，进程却一直握着
    互斥体——之后每次双击都打不开，只能去任务管理器杀进程（实测踩过）。
    窗口正常 1~3 秒内就绪（native 有值），这里只在超时后兜底退出。
    """
    def _ready() -> bool:
        try:
            return getattr(window, "native", None) is not None
        except Exception:
            return False

    def _watch() -> None:
        deadline = time.monotonic() + STARTUP_WATCHDOG_SECONDS
        while time.monotonic() < deadline:
            if _ready():
                return
            time.sleep(0.5)
        _log(f"窗口创建超时（{STARTUP_WATCHDOG_SECONDS:.0f}s），退出以释放单实例锁")
        os._exit(3)

    threading.Thread(target=_watch, name="skysheep-launch-watchdog", daemon=True).start()


def _run_windowed() -> int:
    """主窗口第一页是启动动画；引擎在动画可见之后才导入并启动，就绪后原地切换。

    顺序刻意如此：import webview（0.1s）+ picker（懒加载后 0.1s）就建窗口，
    引擎的重导入（约 2.4s）发生在 webview.start 之后的线程里——用户立刻能看到动画。
    """
    import time as _time

    import webview

    from skysheep import wintheme
    from skysheep.bootpages import error_html, splash_html
    from skysheep.server.picker import FilePicker

    static = static_dir()
    picker = FilePicker()
    # 提前初始化 GUI 库并取屏幕列表：默认开窗位置用主屏（不指定位置时 WinForms
    # 的 CenterScreen 在 DPI 缩放下会把窗口推向右下）；恢复窗口几何时的屏幕
    # 边界校验也用它。initialize() 只导入平台模块，start() 里再调用走模块缓存，
    # 无额外成本。矩形用逻辑像素 (x, y, w, h)，与 Screen 属性同一坐标系。
    try:
        screens = [(s.x, s.y, s.width, s.height) for s in webview.screens]
    except Exception:
        screens = []

    width, height = 1360, 860
    x = y = None
    if screens:
        # 小屏适配：窗口不许比屏幕大（此前 1360x860 在 1280 宽的屏上四周溢出，
        # 看起来"偏右下"——其实是窗口超出屏幕被裁掉了）。
        sx, sy, sw, sh = screens[0]
        width = max(980, min(width, sw - 24))
        height = max(640, min(height, sh - 80))
        # 在屏幕内水平居中、纵向略偏上（给任务栏留空间，保证整窗都在工作区内）。
        # x/y 与 screen.width 同为逻辑像素，pywebview 会按窗口 DPI 换算。
        x = sx + (sw - width) // 2
        y = sy + max((sh - height) // 3, 16)

    # 上次退出时记下的窗口几何：完整有效就原样恢复，最大化关的这次仍按最大化开。
    # 没有可靠记录（首次启动 / 记录损坏 / 换了屏幕配置）走上面的默认居中逻辑。
    maximized = False
    try:
        from skysheep import windowstate  # noqa: PLC0415  引擎导入尽量后置

        width, height, x, y, maximized = windowstate.resolve_geometry(
            windowstate.load_state(), (width, height), screens, (980, 640),
        )
    except Exception:
        pass

    # 主题尽量提前定下来：窗口底色 / 启动页 / 标题栏用同一份解析结果——
    # 「跟随系统」深色下若启动页还是纸色而标题栏已变深，首屏几秒很割裂
    wintheme.set_theme_mode(wintheme.read_ui_theme())
    window = webview.create_window(
        "SkySheep",
        html=splash_html(static, dark=wintheme.theme_is_dark()),
        width=width,
        height=height,
        min_size=(980, 640),
        js_api=picker,
        x=x,
        y=y,
        # 允许选中页面文字（pywebview 默认给 body 注入 user-select:none）：
        # 「引用回答片段」等选区交互依赖它
        text_select=True,
        # 上次是最大化关的就按最大化开（normal 态几何仍一并传给上面，
        # 用户"还原"后回到的是上次的正常大小，而不是默认尺寸）。
        maximized=maximized,
        # 创建后立即显示：窗体构造时 pywebview 已把 BackColor 设为主题纸色
        # （winforms.py:292，与 splash 页背景同色），提前 Show 不会白闪/黑闪。
        # 窗体在启动后约 1 秒就创建完成，而 WebView2 初始化+渲染要再花约 0.8 秒——
        # 若等首帧再显示（旧做法 hidden=True），用户多盯着空桌面干等一秒。
        # 提前显示的视觉：窗口先出现（纯纸色），动画紧接着开始，接近无缝。
        hidden=False,
        # 窗口底色跟主题：pywebview 默认白底，页面刷新/首帧等尚未绘制的瞬间
        # 会露出它（WebView2 的 DefaultBackgroundColor 被设成透明）
        background_color=wintheme.window_background(),
    )
    _show_on_first_paint(window)
    _start_launch_watchdog(window)
    picker.attach(window)
    wintheme.hook_caption_theme(window)  # 标题栏染成纸墨主题色（老系统自动跳过）
    wintheme.allow_microphone(window)  # 放行麦克风（语音输入用；WebView2 默认静默拒绝）
    # 看板线程提前到启动期就在跑（原先在引擎加载完成后才启动）：启动期也每秒
    # 兜底重刷标题栏颜色，且底色/标题栏图标的首刷不再依赖引擎加载完成。
    _start_theme_watchdog(window)

    state = {"quitting": False}
    tray_holder: dict[str, object] = {}

    def _ask_close_choice() -> int:
        """纸墨主题的退出选择框。返回 6=彻底退出 7=缩到系统托盘 2=取消。

        在 closing 事件里同步弹出（GUI 线程），owner 挂主窗口保证置前。
        注意：System.Windows.Forms 的类型必须用模块属性访问（WinForms.X），
        pythonnet 的 `from System.Windows.Forms import 枚举` 会报 unknown location。
        """
        import System.Windows.Forms as WinForms
        from System.Drawing import Color, Font, FontStyle, Icon, Point, Size

        # 应用是 DPI 感知的：WinForms 把窗体缩放基准自动记录成当前 DPI，
        # 对 ClientSize/Location 不做任何二次缩放——所以所有几何尺寸要自己
        # 乘上 DPI 倍率（字体用磅值，随 DPI 自动渲染，不用乘）。
        # 倍率用 Win32 取主窗口的真实 DPI：Control.DeviceDpi 在部分 .NET 版本
        # 上不存在，会悄悄落回 1.0 导致对话框缩成一半（实测踩过）。
        scale = 1.0
        try:
            main_hwnd = int(window.native.Handle.ToInt32())
            scale = ctypes.windll.user32.GetDpiForWindow(main_hwnd) / 96.0
        except Exception:
            scale = 1.0
        if not scale or scale < 0.5:
            scale = 1.0

        def px(v: int) -> int:
            return int(v * scale)

        ink = Color.FromArgb(29, 26, 22)  # #1d1a16
        paper = Color.FromArgb(244, 236, 216)  # #f4ecd8
        paper2 = Color.FromArgb(232, 223, 199)  # #e8dfc7
        ink_soft = Color.FromArgb(107, 98, 85)  # #6b6255
        blue = Color.FromArgb(18, 87, 196)  # #1257c4
        blue_dark = Color.FromArgb(15, 72, 163)

        dlg = WinForms.Form()
        dlg.Text = "SkySheep"
        dlg.FormBorderStyle = WinForms.FormBorderStyle.FixedDialog
        dlg.MaximizeBox = False
        dlg.MinimizeBox = False
        dlg.ShowInTaskbar = False
        dlg.BackColor = paper
        dlg.ForeColor = ink
        dlg.Font = Font("Microsoft YaHei UI", 10)
        dlg.ClientSize = Size(px(436), px(172))
        dlg.StartPosition = (
            WinForms.FormStartPosition.CenterParent
            if window.native
            else WinForms.FormStartPosition.CenterScreen
        )
        try:
            dlg.Icon = Icon(str(static / "skysheep.ico"))
        except Exception:
            pass
        try:
            paint_caption(int(dlg.Handle.ToInt64()))
        except Exception:
            pass

        pic = WinForms.PictureBox()
        pic.Image = Icon(str(static / "skysheep.ico"), px(48), px(48)).ToBitmap()
        pic.SizeMode = WinForms.PictureBoxSizeMode.Zoom
        pic.BackColor = paper
        pic.Location = Point(px(26), px(24))
        pic.Size = Size(px(48), px(48))
        dlg.Controls.Add(pic)

        title = WinForms.Label()
        title.Text = "要退出 SkySheep 吗？"
        title.ForeColor = ink
        title.AutoSize = True
        title.Font = Font("Microsoft YaHei UI", 12, FontStyle.Bold)
        title.Location = Point(px(88), px(26))
        dlg.Controls.Add(title)

        hint = WinForms.Label()
        hint.Text = "请选择关闭窗口后程序的去向"
        hint.ForeColor = ink_soft
        hint.AutoSize = True
        hint.Font = Font("Microsoft YaHei UI", 9)
        hint.Location = Point(px(90), px(60))
        dlg.Controls.Add(hint)

        result = {"v": 2}

        def _stamp(btn, back, fore, hover):
            btn.FlatStyle = WinForms.FlatStyle.Flat
            btn.FlatAppearance.BorderColor = ink
            btn.FlatAppearance.BorderSize = px(1)
            btn.FlatAppearance.MouseOverBackColor = hover
            btn.BackColor = back
            btn.ForeColor = fore
            btn.Height = px(36)
            btn.Cursor = WinForms.Cursors.Hand
            return btn

        btn_quit = _stamp(WinForms.Button(), ink, paper, Color.FromArgb(45, 41, 36))
        btn_quit.Text = "彻底退出"
        btn_quit.Size = Size(px(118), px(36))
        btn_quit.Location = Point(px(52), px(110))

        btn_tray = _stamp(WinForms.Button(), blue, Color.White, blue_dark)
        btn_tray.Text = "缩到系统托盘"
        btn_tray.Size = Size(px(132), px(36))
        btn_tray.Location = Point(px(180), px(110))

        btn_cancel = _stamp(WinForms.Button(), paper, ink, paper2)
        btn_cancel.Text = "取消"
        btn_cancel.Size = Size(px(88), px(36))
        btn_cancel.Location = Point(px(322), px(110))

        def _on_quit(_s, _a) -> None:
            result["v"] = 6
            dlg.Close()

        def _on_tray(_s, _a) -> None:
            result["v"] = 7
            dlg.Close()

        def _on_cancel(_s, _a) -> None:
            result["v"] = 2
            dlg.Close()

        btn_quit.Click += _on_quit
        btn_tray.Click += _on_tray
        btn_cancel.Click += _on_cancel
        dlg.Controls.Add(btn_quit)
        dlg.Controls.Add(btn_tray)
        dlg.Controls.Add(btn_cancel)
        dlg.AcceptButton = btn_tray  # 回车 = 缩托盘（安全默认）
        dlg.CancelButton = btn_cancel  # Esc = 取消

        if window.native:
            dlg.ShowDialog(window.native)
        else:
            dlg.ShowDialog()
        return result["v"]

    def _dispose_tray() -> None:
        """请托盘线程收尾（NIM_DELETE 后退出），最多等 2 秒。"""
        hwnd_tray = tray_holder.get("hwnd_tray")
        thread = tray_holder.get("thread")
        if hwnd_tray:
            try:
                ctypes.windll.user32.PostMessageW(hwnd_tray, _TRAY_QUIT_MSG, 0, 0)
            except Exception:
                pass
        if thread is not None:
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass

    def _stop_server_early() -> None:
        """窗口已确定要关：让服务并行开始收尾，缩短旧进程持有互斥体的时间。"""
        server = running.get("server")
        if server is not None:
            try:
                server.should_exit = True
            except Exception:
                pass

    def _quit_from_tray() -> None:
        """托盘菜单「退出」：置退出标志并请求关闭主窗口（托盘线程调用）。

        不直接 window.destroy()——pywebview 的窗口方法不保证跨线程安全；
        PostMessage(WM_CLOSE) 让关闭事件回到 GUI 线程，quitting 标志保证
        _on_closing 直接放行，不再弹退出选择框。
        """
        state["quitting"] = True
        _stop_server_early()
        hwnd = _find_main_hwnd()
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        else:
            try:
                window.destroy()
            except Exception:
                pass

    def _on_closing() -> bool:
        """返回 False = 阻止关闭（pywebview closing 事件语义）。"""
        if state["quitting"]:
            # 彻底退出前记住窗口几何（本分支覆盖托盘「退出」路径）
            _remember_window(window)
            return True
        choice = _ask_close_choice()
        if choice == 6:  # 是：彻底退出
            state["quitting"] = True
            _remember_window(window)
            _stop_server_early()
            _dispose_tray()
            return True
        if choice == 7:  # 否：缩到系统托盘
            try:
                window.hide()
            except Exception:
                pass
            nid = tray_holder.get("nid")
            if nid is not None:
                try:
                    nid.uFlags |= NIF_INFO
                    nid.szInfo = "双击托盘图标恢复窗口；右键图标可打开或退出。"
                    nid.szInfoTitle = "SkySheep 已在后台运行"
                    nid.dwInfoFlags = NIIF_INFO
                    ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
                except Exception:
                    pass
            return False
        return False  # 取消：留在当前窗口

    window.events.closing += _on_closing
    _start_tray(static, tray_holder, {
        "open": _focus_existing_window,
        "quit": _quit_from_tray,
        "log": _log,
    })

    errors: list[BaseException] = []
    running: dict[str, object] = {}
    shown_at = _time.monotonic()

    def _bootstrap() -> None:
        # window.native 由 GUI 循环创建（实测约 0.5s 后才有值），等它就绪后
        # 把主窗口标题栏也刷成纸墨色；弹对话框时刷色早已完成。
        deadline = _time.monotonic() + 5
        while window.native is None and _time.monotonic() < deadline:
            _time.sleep(0.1)
        try:
            if window.native is not None:
                paint_caption(int(window.native.Handle.ToInt64()))
        except Exception:
            pass
        try:
            # 第一次真正加载引擎（重导入，约 2.4s）发生在动画已经可见之后
            from skysheep.cli.app import run_desktop_backend

            server, url = run_desktop_backend(str(app_dir()))
        except BaseException as exc:  # noqa: BLE001  失败页就地显示，关窗后再走弹框上报
            errors.append(exc)
            try:
                window.load_html(error_html(str(exc) or type(exc).__name__, str(log_path()),
                                             dark=wintheme.theme_is_dark()))
            except Exception:
                pass
            return
        # 动画至少停留片刻，热启动时避免一闪而过
        _time.sleep(max(0.0, 0.8 - (_time.monotonic() - shown_at)))
        try:
            window.load_url(url)
        except Exception:  # noqa: BLE001  窗口被用户提前关掉：无处可切，静默收场
            pass
        running["server"] = server

        # 全局热键：Ctrl+Alt+Space 唤起 + 剪贴板预填
        # （页面就绪后再注册，on_trigger 经 evaluate_js 打进已加载的前端）
        def _hotkey_fire(text: str) -> None:
            import json as _json

            payload = _json.dumps(text or "", ensure_ascii=False)
            try:
                window.evaluate_js(
                    "window.__quickFocus && window.__quickFocus(" + payload + ")"
                )
            except Exception:
                pass

        _start_global_hotkey(window, _hotkey_fire)

    # 窗口图标启动序列：先统一彩色方块（任务栏第一帧即彩色、不闪线稿），
    # 看板线程首刷时 apply_window_icon 只把标题栏 SMALL 换成主题线稿
    icon = static / "skysheep.ico"
    # WebView2 用固定用户数据目录并关闭私有模式：默认私有模式每次启动都拿一个
    # 全新临时 profile，浏览器环境每次从零初始化（splash 首帧 2~3 秒的主要构成）；
    # 固定目录后 profile 复用，二次启动明显变快，cookie/localStorage 也随之持久化。
    # 临时 profile 目录原先还从不清理，每次启动都在 TEMP 漏一个（实测踩过）。
    # 单实例互斥体保证不会有两个进程同时使用同一个 profile 目录。
    webview.start(
        _bootstrap,
        private_mode=False,
        storage_path=str(_home_dir() / "webview"),
        icon=str(icon) if icon.exists() else None,
    )
    _dispose_tray()

    if errors:
        raise errors[0]
    server = running.get("server")
    if server is not None:
        server.should_exit = True
        # 给服务最多 1.5s 优雅收尾，然后直接结束进程：解释器/.NET 的收尾会拖住
        # 单实例互斥体好几秒，用户立刻重开就会被误报"已经在运行"（实测踩过）。
        _time.sleep(1.5)
    _log("app exited normally")
    os._exit(0)


def _launch() -> int:
    """单实例检查通过后的正常启动（启动失败弹框上报）。"""
    try:
        code = _run_windowed()
        _log("app exited normally")
        return code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code:
            _report_failure(f"SkySheep 启动失败（退出码 {code}）")
        return code
    except BaseException:
        _report_failure("SkySheep 启动失败")
        return 1


def main() -> int:
    _rotate_log()
    _ensure_streams()
    _setup_logging()
    _log(f"=== SkySheep desktop launch (pid {os.getpid()}) ===")

    if _acquire_single_instance():
        _write_pid_record()
        return _launch()

    # 互斥体被占：先看旧实例的窗口是否可用（含刚点过彻底退出的收尾场景）
    if _focus_existing_window():
        _log("already running; focused existing window")
        # 旧实例可能正在退出（窗口销毁要零点几秒）：聚焦后再观察一小会儿，
        # 窗口若消失就转入等待逻辑、正常启动新实例，否则用户会面对
        # "旧窗口关了、新窗口又没开"的空白。
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if not _focus_existing_window():
                break
        else:
            return 0
        _log("focused window vanished; previous instance is exiting")

    # 没有可聚焦的窗口：两种可能。① 旧实例正在启动/收尾——等它让出互斥体；
    # ② 旧实例卡死（WebView2 初始化阻塞，进程握着锁却迟迟不出窗口）——
    # 它永远不会自己退出，继续等只会让用户反复双击、堆积僵尸进程，
    # 必须结束它并接管（2026-09-21 实测：一天内三次"双击打不开"）。
    _log("mutex held but no usable window; probing previous instance")
    _release_stale_mutex()
    if _wait_mutex_free(STALE_GRACE_SECONDS):
        _log("previous instance exited; launching fresh")
        _write_pid_record()
        return _launch()
    stale = _stale_holder_pid()
    if stale:
        _log(f"previous instance (pid {stale}) is unresponsive; taking over")
        _terminate_process(stale)
        if _wait_mutex_free(5.0):
            _log("stale instance terminated; launching fresh")
            _write_pid_record()
            return _launch()
        _log("stale instance did not release mutex in time")
    alert("SkySheep 已经在运行了。\n\n请查看任务栏中已打开的 SkySheep 窗口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
