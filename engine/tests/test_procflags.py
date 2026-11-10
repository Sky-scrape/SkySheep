"""子进程隐藏控制台的回归：桌面壳是 GUI 进程（无控制台），AI 每执行一条
命令，cmd.exe / git / taskkill 都会各弹一个黑窗口。

这些 spawn 点必须带 CREATE_NO_WINDOW（Windows 上 0x08000000）：
Popen 前台、Popen 后台、taskkill 树杀、worktree 的 git 调用。断言的是
传给进程创建 API 的标志，不是「窗口看起来有没有闪」——后者没法在无人值守
的环境里稳定测。
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from skysheep import procflags
from skysheep.core import worktree
from skysheep.tools import shell as shell_mod

WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


class _FakeProc:
    """subprocess.Popen 的最小替身：够 _run_sync / _popen_bg 走完。"""

    _handle = None
    pid = 4242
    returncode = 0

    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"ok\n")
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass


def test_no_window_flag_value():
    assert procflags.NO_WINDOW == (CREATE_NO_WINDOW if WIN else 0)


def test_foreground_spawn_hides_console(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(shell_mod.subprocess, "Popen", fake_popen)
    status, out, _err, code, _note = shell_mod._run_sync(
        shell_mod._shell_argv("echo ok"), str(tmp_path), 5, job_containment=False
    )
    assert status == "ok" and code == 0
    assert "ok" in out
    assert seen.get("creationflags", 0) == procflags.NO_WINDOW


def test_background_spawn_hides_console_and_keeps_group(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(shell_mod.subprocess, "Popen", fake_popen)
    shell_mod._popen_bg(shell_mod._shell_argv("ping -n 30 127.0.0.1"), str(tmp_path))
    flags = seen.get("creationflags", 0)
    if WIN:
        assert flags & CREATE_NO_WINDOW, "后台命令也要隐藏控制台"
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP, "独立进程组不能丢"
    else:
        assert seen.get("start_new_session") is True


@pytest.mark.skipif(not WIN, reason="taskkill 树杀仅 Windows")
def test_tree_kill_hides_taskkill_console(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    shell_mod._tree_kill(_FakeProc())
    assert seen["argv"][0].lower().endswith("taskkill.exe")
    assert seen.get("creationflags", 0) & CREATE_NO_WINDOW


def test_worktree_git_hides_console(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worktree.subprocess, "run", fake_run)
    done = worktree._git(["status", "--porcelain"], Path(tmp_path))
    assert done.returncode == 0
    assert seen["argv"][0] == "git"
    assert seen.get("creationflags", 0) == procflags.NO_WINDOW
