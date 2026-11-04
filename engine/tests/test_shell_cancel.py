"""run_command 前台取消：线程杀不掉，进程必须真的被树杀。

用户点「停止」后，run_command 的同步执行线程不会随 asyncio 任务取消而中断；
若不主动杀进程，命令会在后台继续跑到 timeout_s（改文件、占端口）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from skysheep.tools.base import ToolContext
from skysheep.tools.shell import RunCommandArgs, RunCommandTool


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            # tasklist 输出是控制台代码页（中文系统 GBK），不能按 UTF-8 解码；
            # 只匹配 pid 的 ASCII 字节
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=10,
            ).stdout
        except Exception:  # noqa: BLE001 - 查询失败按「已死」处理，避免测试挂死
            return False
        return str(pid).encode("ascii") in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def test_cancel_kills_foreground_process(tmp_path):
    # cmd.exe 的引号解析会被 Python list2cmdline 的转义干扰（与取消逻辑无关），
    # 本用例只验证取消行为，因此避开带空格的解释器路径
    if " " in sys.executable:
        pytest.skip("解释器路径含空格：命令行引号会干扰本用例")
    NL = chr(10)
    (tmp_path / "child.py").write_text(
        NL.join([
            "import os, time, pathlib",
            "pathlib.Path('pid.txt').write_text(str(os.getpid()), encoding='utf-8')",
            "time.sleep(30)",
        ]) + NL,
        encoding="utf-8",
    )
    command = f"{sys.executable} child.py"
    ctx = ToolContext(working_dir=tmp_path)
    task = asyncio.create_task(
        RunCommandTool().run(RunCommandArgs(command=command, timeout_s=60), ctx)
    )

    pid_file = tmp_path / "pid.txt"
    for _ in range(200):
        await asyncio.sleep(0.05)
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
    else:  # pragma: no cover - 环境异常时给出可读失败
        task.cancel()
        raise AssertionError("子进程没有写出 pid.txt")
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _pid_alive(pid)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    for _ in range(100):
        if not _pid_alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(pid), "取消后前台命令必须被树杀，不能跑到 timeout_s"
    await asyncio.sleep(0.2)  # 让同步线程把 job/句柄收尾后再结束用例
