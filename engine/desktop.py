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

_MUTEX_HANDLE = None


def app_dir() -> Path:
    """启动器所在目录：开发态是 engine/，打包态是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log_path() -> Path:
    home = os.environ.get("SKYSHEEP_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".skysheep"
    return base / "logs" / "desktop.log"


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


def _find_main_hwnd() -> int:
    """按标题找 SkySheep 主窗口句柄；找不到返回 0。"""
    user32 = ctypes.windll.user32
    found = [0]

    def _collect(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip() == APP_TITLE:
                found[0] = int(hwnd)
                return False  # 找到即停
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_collect)
    user32.EnumWindows(callback, None)
    return found[0]


def _focus_existing_window() -> bool:
    """把已经在跑的 SkySheep 主窗口唤到前台，成功返回 True。"""
    hwnd = _find_main_hwnd()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


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

    def _paint(mode: str) -> None:
        """标题栏 + 窗口底色一起按主题刷（失败静默：老系统没有 DWM 属性）。"""
        try:
            wintheme.apply_caption_theme(window, dark=(mode == "dark"))
        except Exception:
            pass
        try:
            from System.Drawing import ColorTranslator  # noqa: PLC0415  pywebview 自带

            native = getattr(window, "native", None)
            if native is not None:
                native.BackColor = ColorTranslator.FromHtml(wintheme.window_background(mode))
                wintheme.apply_window_icon(native, dark=(mode == "dark"))
        except Exception:
            pass

    def _thread() -> None:
        # 注册主窗口：前端切主题时 backend.apply_theme 会调 wintheme.refresh_now()
        # 事件驱动重刷（立即生效）；下面的轮询退化为兜底（窗口未就绪时错过的那次）。
        wintheme.register_window(window)
        wintheme.set_theme_mode(wintheme.read_ui_theme())
        last = wintheme.current_theme_mode()
        _paint(last)
        while True:
            time.sleep(1.0)
            cur = wintheme.current_theme_mode()
            if cur == last:
                continue
            last = cur
            _paint(cur)

    threading.Thread(target=_thread, name="skysheep-theme", daemon=True).start()


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
    # 提前初始化 GUI 库并取主屏：用 screen= 让 pywebview 按 DPI 正确地居中开窗
    # （不指定位置时 WinForms 的 CenterScreen 在 DPI 缩放下会把窗口推向右下）。
    # initialize() 只导入平台模块，start() 里再调用走模块缓存，无额外成本。
    try:
        screen = webview.screens[0]
    except Exception:
        screen = None

    width, height = 1360, 860
    x = y = None
    if screen is not None:
        # 小屏适配：窗口不许比屏幕大（此前 1360x860 在 1280 宽的屏上四周溢出，
        # 看起来"偏右下"——其实是窗口超出屏幕被裁掉了）。
        width = max(980, min(width, screen.width - 24))
        height = max(640, min(height, screen.height - 80))
        # 在屏幕内水平居中、纵向略偏上（给任务栏留空间，保证整窗都在工作区内）。
        # x/y 与 screen.width 同为逻辑像素，pywebview 会按窗口 DPI 换算。
        x = screen.x + (screen.width - width) // 2
        y = screen.y + max((screen.height - height) // 3, 16)

    window = webview.create_window(
        "SkySheep",
        html=splash_html(static),
        width=width,
        height=height,
        min_size=(980, 640),
        js_api=picker,
        x=x,
        y=y,
        # 窗口底色跟主题：pywebview 默认白底，页面刷新/首帧等尚未绘制的瞬间
        # 会露出它（WebView2 的 DefaultBackgroundColor 被设成透明）
        background_color=wintheme.window_background(wintheme.read_ui_theme()),
    )
    picker.attach(window)
    wintheme.hook_caption_theme(window)  # 标题栏染成纸墨主题色（老系统自动跳过）

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
            return True
        choice = _ask_close_choice()
        if choice == 6:  # 是：彻底退出
            state["quitting"] = True
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
                window.load_html(error_html(str(exc) or type(exc).__name__, str(log_path())))
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
        _start_theme_watchdog(window)

    # 窗口图标启动序列：先统一彩色方块（任务栏第一帧即彩色、不闪线稿），
    # 看板线程首刷时 apply_window_icon 只把标题栏 SMALL 换成主题线稿
    icon = static / "skysheep.ico"
    webview.start(_bootstrap, icon=str(icon) if icon.exists() else None)
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

    if not _acquire_single_instance():
        if _focus_existing_window():
            _log("already running; focused existing window")
            # 旧实例可能正在退出（刚点过彻底退出，窗口销毁要零点几秒）：聚焦后
            # 再观察一小会儿，窗口若消失就转入下面的等待逻辑、正常启动新实例，
            # 否则用户会面对"旧窗口关了、新窗口又没开"的空白。
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if not _focus_existing_window():
                    break
            else:
                return 0
            _log("focused window vanished; previous instance is exiting")
        # 互斥体还在、窗口却没了：旧实例正在收尾（窗口已销毁，但 WebView2 与
        # 服务的退出还要几秒）。这时双击不该报"已经在运行"——等旧进程真正
        # 退出、互斥体被系统释放后，作为新实例正常启动。
        _log("mutex held but no window; waiting for previous instance to exit")
        _release_stale_mutex()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _release_stale_mutex()
            time.sleep(0.25)
            if _acquire_single_instance():
                _log("previous instance exited; launching fresh")
                return _launch()
        alert("SkySheep 已经在运行了。\n\n请查看任务栏中已打开的 SkySheep 窗口。")
        return 0

    return _launch()


if __name__ == "__main__":
    sys.exit(main())
