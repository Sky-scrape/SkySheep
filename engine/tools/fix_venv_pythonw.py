"""修复 uv 创建的虚拟环境：让 ``.venv\\Scripts\\pythonw.exe`` 真正是无终端的 GUI 程序。

**问题**：uv 生成的 venv 里 ``pythonw.exe`` 与 ``python.exe`` 是同一个可执行文件——
一个**控制台子系统**的转发器（trampoline），并且硬编码启动基础解释器的
``python.exe``。双击它时 Windows 会为它分配控制台窗口，于是"双击启动"永远跟着
一个黑框终端（实测：子进程镜像为 ``<base>\\python.exe``、控制台窗口可见）。

**修法**：改用 CPython 自带的 venv 启动器
（``<base>\\Lib\\venv\\scripts\\nt\\pythonw.exe``，GUI 子系统）。它按自身位置向上找
``pyvenv.cfg`` 定位虚拟环境，会以基础解释器的 ``pythonw.exe`` 运行脚本，全程没有
控制台窗口（实测控制台句柄为 0、不可见）。

用法（在 engine 目录）：

    .venv\\Scripts\\python.exe tools\\fix_venv_pythonw.py           # 修复并自检
    .venv\\Scripts\\python.exe tools\\fix_venv_pythonw.py --check   # 只检查，不改动
    .venv\\Scripts\\python.exe tools\\fix_venv_pythonw.py --no-probe

重建 venv（删除 .venv、``uv sync --reinstall`` 等）之后需要重新执行一次。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import struct
import sys
import tempfile
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHONW = ENGINE_DIR / ".venv" / "Scripts" / "pythonw.exe"
GUI_SUBSYSTEM = 2
CONSOLE_SUBSYSTEM = 3
# 官方启动器约 250 KB；uv 的控制台转发器只有 ~45 KB
MIN_LAUNCHER_BYTES = 100_000
# 3.12 的官方启动器叫 pythonw.exe，3.13+ 改叫 venvwlauncher.exe
LAUNCHER_NAMES = ("pythonw.exe", "venvwlauncher.exe")
WAIT_OBJECT_0 = 0


class STARTUPINFOW(ctypes.Structure):
    """ctypes.wintypes 里没有 STARTUPINFOW，自己定义（字段顺序必须与 Win32 一致）。"""

    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


def read_subsystem(path: Path) -> int:
    """读 PE 头里的 Subsystem 字段：2=GUI（无控制台），3=CONSOLE（弹终端）。"""
    with open(path, "rb") as fh:
        data = fh.read(0x400)
    if data[:2] != b"MZ":
        raise ValueError(f"{path} 不是 PE 文件")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off : pe_off + 4] != b"PE\0\0":
        raise ValueError(f"{path} 不是 PE 文件")
    return struct.unpack_from("<H", data, pe_off + 24 + 68)[0]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _base_dir(venv: Path) -> Path | None:
    """从 pyvenv.cfg 的 home 取基础解释器目录。"""
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home":
            return Path(value.strip())
    return None


def find_official_launcher(venv: Path) -> Path | None:
    """在基础解释器里找 CPython 自带的 GUI venv 启动器。"""
    base = _base_dir(venv)
    if base is None:
        return None
    for name in LAUNCHER_NAMES:
        candidate = base / "Lib" / "venv" / "scripts" / "nt" / name
        if not candidate.exists():
            continue
        try:
            if read_subsystem(candidate) != GUI_SUBSYSTEM:
                continue
        except ValueError:
            continue
        if candidate.stat().st_size < MIN_LAUNCHER_BYTES:
            continue
        return candidate
    return None


def _run_detached(argv: list[str], cwd: Path, timeout_ms: int = 30_000) -> bool:
    """用 CreateProcessW 启动进程并等待；返回是否正常退出。

    直接走 Win32，不经过 shell，避免把参数拼成命令行字符串。**刻意不传任何
    creation flag**：探针要如实反映"双击时的控制台行为"，屏蔽控制台就测不出问题。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateProcessW.restype = wt.BOOL
    kernel32.CreateProcessW.argtypes = [
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    cmdline = ctypes.create_unicode_buffer(" ".join(f'"{a}"' for a in argv))
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    info = PROCESS_INFORMATION()
    created = kernel32.CreateProcessW(
        None,
        cmdline,
        None,
        None,
        False,
        0,
        None,
        str(cwd),
        ctypes.byref(startup),
        ctypes.byref(info),
    )
    if not created:
        return False
    try:
        waited = kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        if waited != WAIT_OBJECT_0:
            return False
        code = wt.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value == 0
    finally:
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(info.hProcess)


def probe_console_visible(pythonw: Path) -> bool | None:
    """用 pythonw 跑一句探针，回报它是否拿到可见的控制台窗口。None=探测失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        out = tmp_dir / "probe.txt"
        script = tmp_dir / "probe.py"
        script.write_text(
            "import ctypes, pathlib\n"
            "hwnd = ctypes.windll.kernel32.GetConsoleWindow()\n"
            "visible = bool(hwnd) and bool(ctypes.windll.user32.IsWindowVisible(hwnd))\n"
            f"pathlib.Path(r'{out}').write_text(str(visible), encoding='utf-8')\n",
            encoding="utf-8",
        )
        if not _run_detached([str(pythonw), str(script)], cwd=tmp_dir):
            return None
        if not out.exists():
            return None
        return out.read_text(encoding="utf-8").strip() == "True"


def ensure_gui_launcher(venv_pythonw: Path = VENV_PYTHONW) -> tuple[bool, str]:
    """确保 venv 的 pythonw.exe 是无控制台的 GUI 启动器；返回 (是否成功, 说明)。"""
    if not venv_pythonw.exists():
        return False, f"找不到 {venv_pythonw}，先运行 uv sync 创建虚拟环境"

    try:
        current = read_subsystem(venv_pythonw)
    except (OSError, ValueError) as exc:
        return False, f"读取 {venv_pythonw.name} 失败：{exc}"

    source = find_official_launcher(venv_pythonw.parent.parent)
    if source is None:
        return (
            False,
            "基础解释器里没有官方 venv 启动器（Lib\\venv\\scripts\\nt\\pythonw.exe），"
            "无法修复；可改用打包版 dist\\SkySheep\\SkySheep.exe",
        )

    if current == GUI_SUBSYSTEM and venv_pythonw.stat().st_size >= MIN_LAUNCHER_BYTES:
        if _file_hash(venv_pythonw) == _file_hash(source):
            return True, "已经是无终端的 GUI 启动器，无需修复"
        return True, "已是无终端的 GUI 启动器（与官方文件不同，保留现有文件）"

    try:
        venv_pythonw.write_bytes(source.read_bytes())
    except OSError as exc:
        return False, f"写入 {venv_pythonw} 失败：{exc}"
    return True, f"已用官方 GUI 启动器替换（来源：{source}）"


def check(venv_pythonw: Path = VENV_PYTHONW) -> tuple[bool, str]:
    """只读检查当前 pythonw.exe 是否满足"双击不弹终端"。"""
    if not venv_pythonw.exists():
        return False, f"找不到 {venv_pythonw}"
    try:
        subsystem = read_subsystem(venv_pythonw)
    except (OSError, ValueError) as exc:
        return False, f"读取失败：{exc}"
    size = venv_pythonw.stat().st_size
    kind = {GUI_SUBSYSTEM: "GUI（无终端）", CONSOLE_SUBSYSTEM: "CONSOLE（会弹终端）"}.get(
        subsystem, f"未知({subsystem})"
    )
    ok = subsystem == GUI_SUBSYSTEM and size >= MIN_LAUNCHER_BYTES
    return ok, f"subsystem={subsystem} {kind}, size={size}"


def main() -> int:
    parser = argparse.ArgumentParser(description="make venv pythonw.exe a no-console launcher")
    parser.add_argument("--check", action="store_true", help="只检查，不修改文件")
    parser.add_argument("--no-probe", action="store_true", help="跳过启动自检")
    args = parser.parse_args()

    if args.check:
        ok, detail = check()
        print(f"{VENV_PYTHONW}\n  {detail}")
        print("判定：" + ("PASS" if ok else "FAIL —— 运行不带 --check 的命令修复"))
        return 0 if ok else 1

    ok, message = ensure_gui_launcher()
    print(("[完成] " if ok else "[失败] ") + message)
    if not ok:
        return 1

    if not args.no_probe:
        visible = probe_console_visible(VENV_PYTHONW)
        if visible is None:
            print("[自检] 探针未返回结果（跳过判定）")
        elif visible:
            print("[自检] FAIL —— pythonw 仍拿到可见控制台窗口")
            return 1
        else:
            print("[自检] PASS —— pythonw 无可见控制台窗口")
    return 0


if __name__ == "__main__":
    sys.exit(main())
