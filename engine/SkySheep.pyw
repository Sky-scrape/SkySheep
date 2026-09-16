"""SkySheep 双击入口（开发态）。

桌面快捷方式直接调用 ``.venv\\Scripts\\pythonw.exe`` 运行本文件，因此不会出现终端窗口。
如果系统把 .pyw 关联到别的 Python（缺少 skysheep 依赖），这里会自动改用项目虚拟环境重启一次。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELAUNCH_FLAG = "SKYSHEEP_RELAUNCHED"


def _venv_pythonw() -> Path | None:
    candidate = HERE / ".venv" / "Scripts" / "pythonw.exe"
    return candidate if candidate.exists() else None


def _relaunch_in_venv(pythonw: Path) -> int:
    env = dict(os.environ, **{RELAUNCH_FLAG: "1"})
    subprocess.Popen(
        [str(pythonw), str(Path(__file__).resolve())],
        cwd=str(HERE),
        env=env,
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
    )
    return 0


def main() -> int:
    pythonw = _venv_pythonw()
    if (
        pythonw is not None
        and os.environ.get(RELAUNCH_FLAG) != "1"
        and Path(sys.executable).resolve() != pythonw.resolve()
    ):
        return _relaunch_in_venv(pythonw)

    sys.path.insert(0, str(HERE))
    try:
        from desktop import main as desktop_main
    except BaseException:  # noqa: BLE001  无控制台运行时 import 失败必须可见，否则静默“打不开”
        import ctypes
        import traceback

        ctypes.windll.user32.MessageBoxW(
            None,
            "SkySheep 启动失败（加载启动器时出错）：\n\n"
            + traceback.format_exc()[-1200:],
            "SkySheep",
            0x10,
        )
        return 1

    return desktop_main()


if __name__ == "__main__":
    sys.exit(main())
