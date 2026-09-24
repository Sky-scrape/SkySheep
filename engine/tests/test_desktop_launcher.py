"""桌面启动器（desktop.py）单实例与卡死恢复逻辑测试。

背景：2026-09-21 一天内出现三次"双击打不开"——卡死实例握着单实例互斥体
却不出窗口，后续双击进程又被卡死窗口的同步 Win32 调用（GetWindowText /
ShowWindow）一起拖住，进程堆积。这里守住四条：
  1. 聚焦只用不阻塞的调用，窗口无响应时直接放弃聚焦；
  2. 卡死实例按"pid + 进程创建时刻"双重校验身份后才接管，防 pid 复用误杀；
  3. 启动宽限期内不接管（可能只是还在启动）；
  4. 窗口创建超时由看门狗退出，把互斥体还给系统。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="desktop 启动器仅 Windows")

_ENGINE = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("skysheep_desktop_launcher", _ENGINE / "desktop.py")
desktop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desktop)


class FakeTime:
    """可控时钟：monotonic 每次调用推进 step，sleep 立即返回。"""

    def __init__(self, step: float = 1.0) -> None:
        self.t = 1000.0
        self.step = step

    def monotonic(self) -> float:
        self.t += self.step
        return self.t

    def sleep(self, _seconds: float) -> None:
        return None

    def time(self) -> float:
        return self.t


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(desktop, "time", ft)
    return ft


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """pid/日志文件都落在临时目录，绝不碰真实 ~/.skysheep。"""
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_write_and_read_pid_record(isolated_home):
    desktop._write_pid_record()
    rec = desktop._read_pid_record()
    assert rec is not None
    pid, created = rec
    assert pid == desktop.os.getpid()
    assert created > 0


def test_find_main_hwnd_skips_hung_window(monkeypatch):
    """卡死窗口不能读标题：读之前必须被 IsHungAppWindow 挡住。"""
    calls = []

    class FakeUser32:
        def GetWindowTextLengthW(self, hwnd):
            calls.append(("len", hwnd))
            return 8  # 有长度（假装标题匹配），验证 hung 守卫先于取标题

        def GetWindowTextW(self, hwnd, buf, max_len):
            calls.append(("text", hwnd))
            return 8

        def IsWindowVisible(self, hwnd):
            return False

        def EnumWindows(self, cb, _lp):
            cb(111, None)
            return True

    monkeypatch.setattr(desktop.ctypes, "windll", type("W", (), {"user32": FakeUser32()})())
    monkeypatch.setattr(desktop, "_is_hung", lambda hwnd: True)

    assert desktop._find_main_hwnd() == 0
    assert ("text", 111) not in calls  # 卡死窗口没有被读标题


def test_focus_existing_window_gives_up_on_hung(monkeypatch):
    """窗口无响应时聚焦必须放弃（旧实现会同步 ShowWindow 跟着挂死）。"""
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 12345)
    monkeypatch.setattr(desktop, "_is_hung", lambda hwnd: True)
    assert desktop._focus_existing_window() is False


def test_find_hung_sheep_hwnd_matches_by_image(monkeypatch):
    """卡死实例证据链①：按属主进程镜像认亲找到卡死窗口（全程不读标题）。"""
    monkeypatch.setattr(desktop, "_window_pid", lambda hwnd: 4242)
    monkeypatch.setattr(desktop, "_is_hung", lambda hwnd: True)
    monkeypatch.setattr(desktop.os, "getpid", lambda: 999)

    class FakeKernel32:
        @staticmethod
        def OpenProcess(_access, _inherit, pid):
            return 77 if pid == 4242 else 0

        @staticmethod
        def QueryFullProcessImageNameW(handle, _flags, buf, size_ptr):
            # 生产代码传 c_void_p 句柄与 byref(size)，fake 里还原取值
            if getattr(handle, "value", handle) != 77:
                return 0
            buf.value = desktop.sys.executable
            size_ptr._obj.value = len(desktop.sys.executable)
            return 1

        @staticmethod
        def CloseHandle(_handle):
            return True

    class FakeUser32:
        def EnumWindows(self, cb, _lp):
            cb(555, None)
            return True

    monkeypatch.setattr(
        desktop.ctypes, "windll",
        type("W", (), {"user32": FakeUser32(), "kernel32": FakeKernel32()})(),
    )
    assert desktop._find_hung_sheep_hwnd() == 555


def test_find_hung_sheep_hwnd_skips_healthy_window(monkeypatch):
    """没停转的窗口不算卡死证据：返回 0（交给标题匹配/聚焦那条路）。"""
    monkeypatch.setattr(desktop, "_is_hung", lambda hwnd: False)
    monkeypatch.setattr(
        desktop.ctypes, "windll",
        type("W", (), {
            "user32": type("U", (), {"EnumWindows": lambda self, cb, lp: True})(),
            "kernel32": object(),
        })(),
    )
    assert desktop._find_hung_sheep_hwnd() == 0


def test_stale_holder_from_hung_window(monkeypatch, isolated_home):
    """链①：记录进程的窗口已停转 → 不等宽限期直接按窗口属主接管。"""
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 0)
    monkeypatch.setattr(desktop, "_find_hung_sheep_hwnd", lambda: 12345)
    monkeypatch.setattr(desktop, "_window_pid", lambda hwnd: 4242)
    monkeypatch.setattr(desktop, "_process_start_time", lambda pid: 100.0 if pid == 4242 else None)
    monkeypatch.setattr(desktop.os, "getpid", lambda: 999)
    desktop._pid_path().parent.mkdir(parents=True, exist_ok=True)
    desktop._pid_path().write_text("4242 100.0", encoding="utf-8")
    assert desktop._stale_holder_pid() == 4242


def test_stale_holder_none_when_window_healthy(monkeypatch):
    """窗口活着（未卡死）说明实例健康，绝不能接管。"""
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 12345)
    monkeypatch.setattr(desktop, "_window_pid", lambda hwnd: 4242)
    monkeypatch.setattr(desktop, "_is_hung", lambda hwnd: False)
    assert desktop._stale_holder_pid() is None


def test_stale_holder_respects_startup_grace(monkeypatch, fake_time, isolated_home):
    """没有窗口 + 实例还年轻：可能只是启动中，不允许接管。"""
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 0)
    monkeypatch.setattr(desktop, "_find_hung_sheep_hwnd", lambda: 0)
    desktop._write_pid_record()
    assert desktop._stale_holder_pid() is None


def test_stale_holder_from_pid_record_after_grace(monkeypatch, fake_time, isolated_home):
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 0)
    monkeypatch.setattr(desktop, "_find_hung_sheep_hwnd", lambda: 0)
    # 模拟“另一个实例”写的记录：pid 不能是本进程（本进程会被视为自己而拒接管）
    other_pid = desktop.os.getpid() + 1
    desktop._pid_path().parent.mkdir(parents=True, exist_ok=True)
    desktop._pid_path().write_text(f"{other_pid} 100.0", encoding="utf-8")
    monkeypatch.setattr(desktop, "_process_start_time", lambda x: 100.0 if x == other_pid else None)
    monkeypatch.setattr(desktop.os, "getpid", lambda: other_pid - 5)
    # 走过宽限期（FakeTime 每次调用 +1s，age = time.time() - created）
    assert desktop._stale_holder_pid() == other_pid


def test_stale_holder_rejects_pid_reuse(monkeypatch, fake_time, isolated_home):
    """pid 被复用（创建时刻对不上）：绝不接管，防误杀无关进程。"""
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 0)
    monkeypatch.setattr(desktop, "_find_hung_sheep_hwnd", lambda: 0)
    other_pid = desktop.os.getpid() + 1
    desktop._pid_path().parent.mkdir(parents=True, exist_ok=True)
    desktop._pid_path().write_text(f"{other_pid} 100.0", encoding="utf-8")
    # 同号 pid 但创建时刻对不上 → 记录过期，拒绝接管
    monkeypatch.setattr(desktop, "_process_start_time", lambda x: 999.0 if x == other_pid else None)
    monkeypatch.setattr(desktop.os, "getpid", lambda: other_pid - 5)
    assert desktop._stale_holder_pid() is None


def test_stale_holder_ignores_foreign_hung_window(monkeypatch, fake_time, isolated_home):
    """卡死窗口属主与 pid 记录不符：不按窗口属主加速接管（宁可等宽限期，
    按记录本身接管——记录才是身份锚点，窗口只是加速证据）。"""
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 0)
    monkeypatch.setattr(desktop, "_find_hung_sheep_hwnd", lambda: 12345)
    monkeypatch.setattr(desktop, "_window_pid", lambda hwnd: 777)  # 与记录不一致
    other_pid = desktop.os.getpid() + 1
    desktop._pid_path().parent.mkdir(parents=True, exist_ok=True)
    desktop._pid_path().write_text(f"{other_pid} 100.0", encoding="utf-8")
    monkeypatch.setattr(desktop, "_process_start_time", lambda x: 100.0 if x == other_pid else None)
    monkeypatch.setattr(desktop.os, "getpid", lambda: other_pid - 5)
    # FakeTime 每次调用 +1s：走过宽限期后按记录接管（绝不是杀窗口属主 777）
    assert desktop._stale_holder_pid() == other_pid


def test_terminate_process_targets_only_recorded(monkeypatch, fake_time, isolated_home):
    """接管的杀进程路径：只杀 _stale_holder_pid 确认过的 pid。"""
    killed: list[int] = []
    monkeypatch.setattr(desktop, "_find_main_hwnd", lambda: 0)
    monkeypatch.setattr(desktop, "_find_hung_sheep_hwnd", lambda: 0)
    other_pid = desktop.os.getpid() + 1
    desktop._pid_path().parent.mkdir(parents=True, exist_ok=True)
    desktop._pid_path().write_text(f"{other_pid} 100.0", encoding="utf-8")
    monkeypatch.setattr(desktop, "_process_start_time", lambda x: 100.0 if x == other_pid else None)
    monkeypatch.setattr(desktop.os, "getpid", lambda: other_pid - 5)
    monkeypatch.setattr(desktop, "_terminate_process", lambda p: killed.append(p) or True)

    stale = desktop._stale_holder_pid()
    assert stale == other_pid
    desktop._terminate_process(stale)
    assert killed == [other_pid]


def test_wait_mutex_free_acquires_when_released(monkeypatch, fake_time):
    """旧实例退出（第 2 次 acquire 成功）→ 等待循环拿到互斥体。"""
    results = iter([False, True])
    monkeypatch.setattr(desktop, "_acquire_single_instance", lambda: next(results))
    monkeypatch.setattr(desktop, "_release_stale_mutex", lambda: None)
    assert desktop._wait_mutex_free(10.0) is True


def test_wait_mutex_free_times_out(monkeypatch, fake_time):
    monkeypatch.setattr(desktop, "_acquire_single_instance", lambda: False)
    monkeypatch.setattr(desktop, "_release_stale_mutex", lambda: None)
    assert desktop._wait_mutex_free(3.0) is False


def test_watchdog_exits_when_window_never_ready(monkeypatch, tmp_path):
    """窗口创建超时 → 看门狗以退出码 3 结束进程（释放互斥体）。"""
    exited: list[int] = []
    monkeypatch.setattr(desktop, "STARTUP_WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr(desktop.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(desktop, "_log", lambda msg: None)

    class FakeWindow:
        @property
        def native(self):  # 永远创建不出来
            raise RuntimeError("not created yet")

    desktop._start_launch_watchdog(FakeWindow())
    deadline = time.monotonic() + 3
    while not exited and time.monotonic() < deadline:
        time.sleep(0.02)
    assert exited == [3]


def test_watchdog_returns_when_window_ready(monkeypatch):
    """窗口正常就绪 → 看门狗静默返回，不退出。"""
    exited: list[int] = []
    monkeypatch.setattr(desktop, "STARTUP_WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr(desktop.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(desktop, "_log", lambda msg: None)

    class FakeWindow:
        native = object()  # 窗体已创建

    desktop._start_launch_watchdog(FakeWindow())
    time.sleep(0.1)  # 等看门狗线程进入轮询
    deadline = time.monotonic() + 2
    finished = False
    while time.monotonic() < deadline:
        alive = any(
            t.name == "skysheep-launch-watchdog" and t.is_alive()
            for t in threading.enumerate()
        )
        if not alive:
            finished = True
            break
        time.sleep(0.02)
    assert finished, "watchdog thread did not finish"
    assert not exited


def test_main_focuses_healthy_instance(monkeypatch, fake_time, isolated_home):
    """健康实例在跑：聚焦并返回 0，绝不 `_launch` 也绝不杀进程。"""
    launched: list[int] = []
    killed: list[int] = []
    monkeypatch.setattr(desktop, "_acquire_single_instance", lambda: False)
    monkeypatch.setattr(desktop, "_focus_existing_window", lambda: True)
    monkeypatch.setattr(desktop, "_release_stale_mutex", lambda: None)
    monkeypatch.setattr(desktop, "_launch", lambda: launched.append(1) or 0)
    monkeypatch.setattr(desktop, "_stale_holder_pid", lambda: killed.append(2) or 4242)
    monkeypatch.setattr(desktop, "_terminate_process", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(desktop, "_log", lambda msg: None)
    assert desktop.main() == 0
    assert launched == []
    assert killed == []


def test_main_takes_over_hung_instance(monkeypatch, fake_time, isolated_home):
    """卡死实例：接管（杀掉）后正常启动新实例。"""
    launched: list[int] = []
    acquire_results = iter([False, False, True])  # 首次失败 → 宽限等待失败 → 接管后成功
    monkeypatch.setattr(desktop, "_acquire_single_instance", lambda: next(acquire_results))
    monkeypatch.setattr(desktop, "_focus_existing_window", lambda: False)
    monkeypatch.setattr(desktop, "_wait_mutex_free", lambda seconds: next(acquire_results, True))
    monkeypatch.setattr(desktop, "_stale_holder_pid", lambda: 4242)
    killed: list[int] = []
    monkeypatch.setattr(desktop, "_terminate_process", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(desktop, "_launch", lambda: launched.append(1) or 0)
    monkeypatch.setattr(desktop, "_log", lambda msg: None)
    monkeypatch.setattr(desktop, "_write_pid_record", lambda: None)
    monkeypatch.setattr(desktop, "alert", lambda msg: None)

    assert desktop.main() == 0
    assert killed == [4242]
    assert launched == [1]


def test_main_alerts_when_cannot_confirm(monkeypatch, fake_time, isolated_home):
    """无法确认卡死实例（无记录/无窗口）：不杀任何进程，退回提示弹窗。"""
    launched: list[int] = []
    alerts: list[str] = []
    monkeypatch.setattr(desktop, "_acquire_single_instance", lambda: False)
    monkeypatch.setattr(desktop, "_focus_existing_window", lambda: False)
    monkeypatch.setattr(desktop, "_wait_mutex_free", lambda seconds: False)
    monkeypatch.setattr(desktop, "_stale_holder_pid", lambda: None)
    monkeypatch.setattr(desktop, "_launch", lambda: launched.append(1) or 0)
    monkeypatch.setattr(desktop, "_log", lambda msg: None)
    monkeypatch.setattr(desktop, "alert", lambda msg: alerts.append(msg))

    assert desktop.main() == 0
    assert launched == []
    assert len(alerts) == 1


def test_url_scheme_matches_instance_identity(monkeypatch):
    """协议名与实例身份同步：默认 skysheep，命名身份 skysheep-<name>。

    引擎侧 backend._toast_launch_uri 按同一拼法读注册表，两边不一致的话
    点击系统通知就唤不回窗口——所以这里锁死拼法。
    """
    monkeypatch.delenv("SKYSHEEP_INSTANCE", raising=False)
    assert desktop.url_scheme() == "skysheep"
    monkeypatch.setenv("SKYSHEEP_INSTANCE", "dev")
    assert desktop.url_scheme() == "skysheep-dev"


def test_ensure_url_protocol_registers_command(monkeypatch, isolated_home):
    """打包态注册 skysheep:// 协议：URL Protocol 值 + shell/open/command 指向 exe。"""
    import winreg

    created: list[str] = []
    values: dict = {}
    current = {"path": ""}

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_create_key(_root, path, *_args, **_kwargs):
        created.append(path)
        current["path"] = path
        return FakeKey()

    def fake_set_value(_key, name, _reserved, _type, value):
        values[(current["path"], name)] = value

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\app\\SkySheep.exe", raising=False)
    monkeypatch.setattr(winreg, "CreateKey", fake_create_key)
    monkeypatch.setattr(winreg, "SetValueEx", fake_set_value)
    monkeypatch.delenv("SKYSHEEP_INSTANCE", raising=False)
    desktop._ensure_url_protocol()
    root = "Software\\Classes\\skysheep"
    # 子键以键对象为父创建，记录的是相对路径
    cmd = "shell\\open\\command"
    assert created == [root, cmd]
    # 根键：空 REG_SZ 的 URL Protocol 值（协议注册的标志）+ 友好名
    assert values[(root, "URL Protocol")] == ""
    assert values[(root, None)] == "SkySheep"
    # command 键：点击 toast 后系统执行的命令行——指向当前 exe，%1 接 URI
    assert values[(cmd, None)] == '"C:\\app\\SkySheep.exe" "%1"'


def test_ensure_url_protocol_noop_without_freeze(monkeypatch):
    """开发态不注册协议：注册表不该留下指向临时虚拟环境的路径。"""
    import winreg

    def fail_create(*_args, **_kwargs):
        raise AssertionError("开发态不应触碰注册表")

    monkeypatch.setattr(winreg, "CreateKey", fail_create)
    monkeypatch.delenv("SKYSHEEP_INSTANCE", raising=False)
    desktop._ensure_url_protocol()  # 不抛错即通过（早退，没碰 winreg）
