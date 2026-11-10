"""保持电脑运行：按用户偏好阻止系统空闲睡眠（Windows）。

设置 · 高级的「保持电脑运行」开关写 ui.json 的 ``prevent_sleep``；后端在
启动、保存偏好与退出三处调用 :class:`PowerKeeper`。实现是独占一个 daemon
线程持有 ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``：
该 API 的作用域是调用线程，线程退出时系统自动清掉，所以「开」= 起线程，
「关」= 置停止事件并让线程自己清状态。不碰电源计划、不阻止手动睡眠/合盖，
与设置页文案一致。

非 Windows 平台整体 no-op（CLI 在 Linux/macOS 上照常运行）。
"""

from __future__ import annotations

import ctypes
import sys
import threading

# SetThreadExecutionState 的位标志（WinBase.h）
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# 每 30 秒重申一次：部分驱动/电源策略会清掉空闲计时，周期重申比一次到底稳。
_REASSERT_S = 30.0


class PowerKeeper:
    """一个进程内单例式的「阻止空闲睡眠」开关；线程安全、可重复调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def enabled(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def set_enabled(self, enabled: bool) -> None:
        """开/关阻止睡眠；重复调用幂等。平台不支持时静默 no-op。"""
        if not self.available():
            return
        thread: threading.Thread | None = None
        stop: threading.Event | None = None
        with self._lock:
            if enabled:
                if self._thread is not None and self._thread.is_alive():
                    return
                stop = threading.Event()
                self._stop = stop
                self._thread = threading.Thread(
                    target=self._run, args=(stop,), name="skysheep-power", daemon=True
                )
                self._thread.start()
                return
            stop = self._stop
            thread = self._thread
            self._stop = None
            self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            # 等它清掉 ES_CONTINUOUS 再返回：设置页关掉开关后马上查状态，
            # 不该还看到「仍然阻止睡眠」的中间态。1.5 秒足够，超时也不阻塞退出。
            thread.join(timeout=1.5)

    def stop(self) -> None:
        self.set_enabled(False)

    @staticmethod
    def _run(stop: threading.Event) -> None:
        kernel32 = ctypes.windll.kernel32
        # ctypes 句柄/返回值必须显式声明（AGENTS.md）：默认按 int 处理，
        # 64 位下返回值截断没有实际影响，但声明后类型契约稳定。
        kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
        try:
            while not stop.is_set():
                kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                stop.wait(_REASSERT_S)
        finally:
            # 清状态必须在本线程执行（API 按线程记账）：线程退出前恢复默认，
            # 否则某些 Windows 版本会延续到下一个调用者。
            try:
                kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:  # noqa: BLE001 - 退出路径不抛
                pass
