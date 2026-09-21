"""SkySheep 双击入口（开发态）。

桌面快捷方式直接调用 ``.venv\\Scripts\\pythonw.exe`` 运行本文件，因此不会出现终端窗口。
如果系统把 .pyw 关联到别的 Python（缺少 skysheep 依赖），这里会自动改用项目虚拟环境重启一次。

源码态默认落到 ``dev`` 实例身份（数据在 ``~/.skysheep-dev``），与安装版的
``~/.skysheep`` 彼此隔离：源码是用来改代码、跑验证的，不应该污染日常在用的安装版。
已显式指定 ``SKYSHEEP_HOME``（完全自定目录）或 ``SKYSHEEP_INSTANCE`` 时不改，
尊重调用方意图。详见 ``src/skysheep/instance.py``。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELAUNCH_FLAG = "SKYSHEEP_RELAUNCHED"
# 源码态的默认身份：两个版本默认互不可见，是“源码只用于测试”这一用法的正确默认
SOURCE_INSTANCE = "dev"


def _apply_default_instance() -> None:
    """源码态默认身份；已指定 SKYSHEEP_HOME / SKYSHEEP_INSTANCE 时不动。

    必须在 import desktop 之前调用：desktop 的数据目录、互斥体名、窗口标题
    都在进程启动时读它，而引擎（同一进程内稍后导入）读到的是同一份环境变量。
    """
    if os.environ.get("SKYSHEEP_HOME") or os.environ.get("SKYSHEEP_INSTANCE"):
        return
    os.environ["SKYSHEEP_INSTANCE"] = SOURCE_INSTANCE


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
    _apply_default_instance()
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
