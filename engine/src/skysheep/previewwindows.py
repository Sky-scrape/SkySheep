"""独立预览窗控制器：桌面壳注册窗口工厂，WS 层只负责转交 URL。

为什么单独一层：
- 后端（WS 线程）不该直接 import pywebview——CLI / `--browser` / 无头运行都没有
  GUI 循环，import 本身也会拖重依赖；
- 两个桌面入口（`engine/desktop.py` 与 `cli/app.py`）各自管理窗口生命周期，
  预览窗必须跟它们共用一个进程、一套 WebView2 profile，不能另起第二份服务。

所以这里只存一个「创建窗口」的回调：桌面壳启动后 `register(factory)`，退出时
`close_all()`；没有注册（浏览器模式 / 手机远端）时 `open_window` 返回
`{"mode": "browser"}`，由前端退回 `window.open`（pywebview 默认把
window.open 交给系统浏览器，不会顶掉应用页）。

窗口对象只在这里留强引用（pywebview 自己也持有），关闭后由 `close_all` 清理。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

_lock = threading.Lock()
_factory: Callable[[str, str], Any] | None = None
_windows: list[Any] = []


def register(factory: Callable[[str, str], Any] | None) -> None:
    """桌面壳注册窗口工厂：factory(url, title) -> pywebview Window | None。"""
    global _factory
    with _lock:
        _factory = factory


def unregister() -> None:
    global _factory
    with _lock:
        _factory = None


def available() -> bool:
    with _lock:
        return _factory is not None


def _alive(win: Any) -> bool:
    """pywebview 窗口是否还开着（测试桩没有 events 时按存活处理）。"""
    events = getattr(win, "events", None)
    closed = getattr(events, "closed", None) if events is not None else None
    try:
        return closed is None or not closed.is_set()
    except Exception:  # noqa: BLE001
        return True


def open_window(url: str, title: str = "SkySheep 预览") -> dict:
    """开一个独立预览窗。

    返回 {"mode": "native"} 表示桌面壳已开出 WebView2 子窗口；
    {"mode": "browser"} 表示没有可用窗口工厂（或创建失败），调用方应改用
    系统浏览器打开。失败不抛异常：预览按钮不该因为窗口层故障变成死键。
    """
    with _lock:
        factory = _factory
        # 顺手清掉已经关掉的窗口，避免长会话里列表越攒越多
        _windows[:] = [w for w in _windows if _alive(w)]
    if factory is None:
        return {"mode": "browser"}

    try:
        win = factory(url, title)
    except Exception as exc:  # noqa: BLE001
        return {"mode": "browser", "error": str(exc)[:200]}
    if win is None:
        return {"mode": "browser"}

    with _lock:
        _windows[:] = [w for w in _windows if _alive(w)]
        _windows.append(win)
    return {"mode": "native"}


def close_all() -> None:
    """关闭所有独立预览窗（应用退出时调用；主窗口隐藏到托盘时不调用）。

    只对已经显示出来的窗口调 destroy：pywebview 的窗口 API 会等 shown 事件
    （最长 20 秒），创建到一半的窗口不该把退出流程卡住。
    """
    with _lock:
        wins = list(_windows)
        _windows.clear()
    for win in wins:
        events = getattr(win, "events", None)
        shown = getattr(events, "shown", None) if events is not None else None
        try:
            if shown is not None and not shown.wait(0.2):
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass


def close_all_async() -> None:
    """在后台线程收窗：主窗口的 closed 事件在 GUI 线程里触发，就地销毁
    另一个窗口容易和消息泵重入，挪到独立线程更稳。"""
    threading.Thread(target=close_all, name="skysheep-preview-close", daemon=True).start()


def count() -> int:
    """当前开着的预览窗数量（测试与诊断用）。"""
    with _lock:
        _windows[:] = [w for w in _windows if _alive(w)]
        return len(_windows)
