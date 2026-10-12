"""run_command 工具的内部助手：环境剥密、shell 包装、输出收集与受限进程代理。

都是纯函数 / 薄包装，不需要真起进程；_ProcProxy 用假的 sandbox_win 覆盖
poll / wait / kill / close / __del__ 五条路径，不依赖真实的受限令牌直启。
"""

from __future__ import annotations

import os

from skysheep.tools import shell
from skysheep.tools.shell import (
    _acc_text,
    _child_env,
    _collect,
    _decode,
    _is_secret_env_name,
    _shell_argv,
    _windows_exe,
    child_environment,
)

# ---- 环境剥密 ----


def test_is_secret_env_name_words_and_allowlist():
    assert _is_secret_env_name("OPENAI_API_KEY") is True
    assert _is_secret_env_name("MY_TOKEN") is True
    assert _is_secret_env_name("DB_PASSWORD") is True
    assert _is_secret_env_name("PATH") is False
    # 允许名单里的交互式凭据变量不剥（剥了 ssh / git 交互会失效）
    assert _is_secret_env_name("GIT_ASKPASS") is False
    assert _is_secret_env_name("SSH_AUTH_SOCK") is False


def test_child_environment_strips_secrets_keeps_plain(monkeypatch):
    monkeypatch.setenv("SKYSHEEP_TEST_SECRET_KEY", "s3cr3t")
    monkeypatch.setenv("SKYSHEEP_TEST_PLAIN", "keep")
    env = child_environment()
    assert "SKYSHEEP_TEST_SECRET_KEY" not in env
    assert env.get("SKYSHEEP_TEST_PLAIN") == "keep"
    assert env is not os.environ  # 返回新 dict，不改 os.environ
    assert _child_env() == env  # 兼容别名同一口径


# ---- Windows 系统程序绝对路径 ----


def test_windows_exe_prefers_comspec(monkeypatch, tmp_path):
    fake = tmp_path / "cmd.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("ComSpec", str(fake))
    assert _windows_exe("cmd.exe") == str(fake)
    assert _windows_exe("cmd") == str(fake)


def test_windows_exe_falls_back_to_system32_then_bare(monkeypatch, tmp_path):
    monkeypatch.delenv("ComSpec", raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    # System32 下没有该程序 → 退回裸名（不阻断执行）
    assert _windows_exe("taskkill.exe") == "taskkill.exe"
    sys32 = tmp_path / "System32"
    sys32.mkdir()
    (sys32 / "taskkill.exe").write_text("", encoding="utf-8")
    assert _windows_exe("taskkill.exe") == str(sys32 / "taskkill.exe")


# ---- shell 包装 ----


def test_shell_argv_wraps_per_platform(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell, "_windows_exe", lambda _name: "CMD")
    assert _shell_argv("echo hi") == ["CMD", "/d", "/s", "/c", "echo hi"]
    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    assert _shell_argv("echo hi") == ["/bin/bash", "-c", "echo hi"]


# ---- 输出解码与收集 ----


def test_decode_paths():
    assert _decode("already") == "already"
    assert _decode("中文".encode()) == "中文"
    out = _decode(b"\xff\xfe\x00abc")  # 非法 UTF-8：按文件系统编码替换，不抛
    assert isinstance(out, str)


class _Pipe:
    """最小管道替身：按脚本吐 chunk，读空后返回 b""。"""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self.closed = False

    def readline(self):
        return next(self._it, b"")

    def close(self):
        self.closed = True


def test_collect_caps_head_and_keeps_tail():
    acc = {"head": b"", "tail": b"", "capped": False}
    big = b"x" * (shell.FG_MAX_BYTES + 10)
    pipe = _Pipe([big, b"TAIL-MARKER"])
    _collect(pipe, acc)
    assert acc["capped"] is True
    assert pipe.closed is True
    text = _acc_text(acc)
    assert shell._FG_CLIP_MARK.decode() in text
    assert text.endswith("TAIL-MARKER")


def test_collect_within_limit_has_no_clip_mark():
    acc = {"head": b"", "tail": b"", "capped": False}
    _collect(_Pipe([b"short output"]), acc)
    assert acc["capped"] is False
    assert _acc_text(acc) == "short output"


def test_collect_swallows_pipe_errors():
    class _BadPipe:
        def readline(self):
            raise RuntimeError("process killed")

        def close(self):
            raise RuntimeError("already closed")

    acc = {"head": b"", "tail": b"", "capped": False}
    _collect(_BadPipe(), acc)  # 读/关都抛也不外溢
    assert acc["head"] == b""


# ---- 受限令牌路径的进程代理 ----


def _pipe_pair():
    return os.pipe()


def test_proc_proxy_poll_wait_kill_close(monkeypatch):
    from skysheep.security import sandbox_win
    from skysheep.tools.shell import _ProcProxy

    out_r, out_w = _pipe_pair()
    err_r, err_w = _pipe_pair()
    state = {"exit": None, "waited": None, "killed": False, "closed": False}

    monkeypatch.setattr(sandbox_win, "process_exit_code", lambda _h: state["exit"])

    def _wait(_h, ms):
        state["waited"] = ms
        return sandbox_win.WAIT_OBJECT_0

    monkeypatch.setattr(sandbox_win, "wait_process", _wait)
    monkeypatch.setattr(sandbox_win, "terminate_process", lambda _h: state.update(killed=True))
    monkeypatch.setattr(sandbox_win, "close_process_handle", lambda _h: state.update(closed=True))

    p = _ProcProxy(proc_handle=99, pid=1234, out_fd=out_w, err_fd=err_w)
    assert p.poll() is None  # 进程还没退出
    assert p.wait(timeout=0.5) is None
    assert state["waited"] == 500

    state["exit"] = 7
    assert p.poll() == 7
    assert p.returncode == 7
    assert p.wait() == 7  # 已知 returncode：不再等

    p.kill()
    assert state["killed"] is True

    p.close()
    assert state["closed"] is True
    assert p._handle == 0
    p.close()  # 幂等：再关一次不报错

    os.close(out_r)
    os.close(err_r)


def test_proc_proxy_wait_timeout_raises(monkeypatch):
    import subprocess

    from skysheep.security import sandbox_win
    from skysheep.tools.shell import _ProcProxy

    out_r, out_w = _pipe_pair()
    err_r, err_w = _pipe_pair()
    monkeypatch.setattr(sandbox_win, "process_exit_code", lambda _h: None)
    monkeypatch.setattr(sandbox_win, "wait_process", lambda _h, _ms: sandbox_win.WAIT_TIMEOUT)

    p = _ProcProxy(proc_handle=1, pid=2, out_fd=out_w, err_fd=err_w)
    try:
        p.wait(timeout=0.01)
    except subprocess.TimeoutExpired:
        pass
    else:  # pragma: no cover - 明确失败信息
        raise AssertionError("超时应当抛 subprocess.TimeoutExpired")
    finally:
        p._handle = 0  # 避免 __del__ 调真实句柄
        p.stdout.close()
        p.stderr.close()
        os.close(out_r)
        os.close(err_r)


def test_proc_proxy_del_swallows_close_errors(monkeypatch):
    from skysheep.security import sandbox_win
    from skysheep.tools.shell import _ProcProxy

    out_r, out_w = _pipe_pair()
    err_r, err_w = _pipe_pair()

    def _boom(_h):
        raise RuntimeError("handle gone")

    monkeypatch.setattr(sandbox_win, "close_process_handle", _boom)

    p = _ProcProxy(proc_handle=5, pid=6, out_fd=out_w, err_fd=err_w)
    p.__del__()  # 终结器里绝不抛

    os.close(out_r)
    os.close(err_r)
