"""在桌面放一个 SkySheep 快捷方式：双击即开，不出终端窗口。

用法（在 engine 目录）：

    .venv\\Scripts\\python.exe tools\\install_shortcut.py           # 指向源码版（.venv + SkySheep.pyw）
    .venv\\Scripts\\python.exe tools\\install_shortcut.py --exe     # 指向打包版 dist\\SkySheep\\SkySheep.exe
    .venv\\Scripts\\python.exe tools\\install_shortcut.py --remove  # 删掉快捷方式

桌面路径用 SHGetFolderPathW 取，不能写死 ~/Desktop——桌面常被重定向到别的盘。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

from fix_venv_pythonw import ensure_gui_launcher

ENGINE_DIR = Path(__file__).resolve().parent.parent
ICON_PATH = ENGINE_DIR / "src" / "skysheep" / "server" / "static" / "skysheep.ico"
EXE_PATH = ENGINE_DIR / "dist" / "SkySheep" / "SkySheep.exe"
PYW_LAUNCHER = ENGINE_DIR / "SkySheep.pyw"
VENV_PYTHONW = ENGINE_DIR / ".venv" / "Scripts" / "pythonw.exe"
CSIDL_DESKTOPDIRECTORY = 0x0010


def desktop_dir() -> Path:
    """真实桌面目录（用户可能把桌面重定向到其它盘）。"""
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buf)
    return Path(buf.value)


def create_shortcut(link: Path, target: Path, arguments: str, workdir: Path) -> None:
    import win32com.client  # pywin32

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(link))
    shortcut.TargetPath = str(target)
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = str(workdir)
    if ICON_PATH.exists():
        shortcut.IconLocation = f"{ICON_PATH},0"
    shortcut.Description = "SkySheep - AI Agent Workbench"
    shortcut.WindowStyle = 1
    shortcut.save()


def main() -> int:
    parser = argparse.ArgumentParser(description="create a desktop shortcut for SkySheep")
    parser.add_argument("--exe", action="store_true", help="point at dist/SkySheep/SkySheep.exe")
    parser.add_argument("--name", default="SkySheep", help="shortcut name without .lnk")
    parser.add_argument("--remove", action="store_true", help="delete the shortcut instead")
    args = parser.parse_args()

    link = desktop_dir() / f"{args.name}.lnk"

    if args.remove:
        if link.exists():
            link.unlink()
            print(f"removed: {link}")
        else:
            print(f"nothing to remove: {link}")
        return 0

    if args.exe:
        if not EXE_PATH.exists():
            print("packaged exe not found; build it first:")
            print("  .venv\\Scripts\\pyinstaller.exe --noconfirm --clean SkySheep.spec")
            return 1
        target, arguments, workdir = EXE_PATH, "", EXE_PATH.parent
    else:
        if not VENV_PYTHONW.exists() or not PYW_LAUNCHER.exists():
            print("launcher not found; run `uv sync` in this directory first")
            return 1
        # uv 建 venv 时的 pythonw.exe 是控制台子系统转发器，双击会带出黑框终端；
        # 换成 CPython 自带的 GUI 启动器（详见 fix_venv_pythonw.py）。
        ok, message = ensure_gui_launcher(VENV_PYTHONW)
        print(("[venv] " if ok else "[venv][警告] ") + message)
        if not ok:
            print("       双击会弹出终端窗口；可先跑 tools\\fix_venv_pythonw.py 排查")
        target, arguments, workdir = VENV_PYTHONW, f'"{PYW_LAUNCHER}"', ENGINE_DIR

    try:
        create_shortcut(link, target, arguments, workdir)
    except ImportError:
        print("pywin32 is required to write .lnk files; run `uv pip install pywin32`")
        return 1

    print(f"created: {link}")
    print(f"  target : {target}")
    print(f"  args   : {arguments or '(none)'}")
    print(f"  workdir: {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
