"""命令执行沙箱化一期（security/sandbox_win.py）的测试。

覆盖四层：
- 结构构造纯函数（build_extended_limits / build_ui_restrictions）：不触
  Win32 API、跨平台可跑，断言 LimitFlags 与字段值；
- 降级路径：降级的 ContainmentJob 上所有函数不抛、一律 False / no-op；
- 真机用例（Windows，skipif 非 win32）：cmd /c pause 入 job，
  TerminateJobObject 后带超时断言进程退出；run_command 前台/后台全链路
  在遏制开启时照常工作；
- config 链路：[shell] job_containment 默认开、写入 false 后读回生效
  （home fixture 隔离 SKYSHEEP_HOME，不碰真实 ~/.skysheep）。
所有等待都带超时，绝不无限挂起。
"""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from skysheep.config import load_config, update_config_section
from skysheep.security import sandbox_win
from skysheep.security.sandbox_win import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_UILIMIT_READCLIPBOARD,
    JOB_OBJECT_UILIMIT_WRITECLIPBOARD,
    ContainmentJob,
    apply_ui_restrictions,
    assign_process,
    build_extended_limits,
    build_ui_restrictions,
    close_job,
    create_containment_job,
    terminate_job,
)
from skysheep.tools.base import ToolContext
from skysheep.tools.shell import _BG, RunCommandTool

WIN = sys.platform == "win32"


# ---- 结构构造纯函数（跨平台） ----


def test_build_extended_limits_containment_only():
    """不传资源上限：只置 KILL_ON_JOB_CLOSE（一期 run_command 的用法）。"""
    info = build_extended_limits()
    assert info.BasicLimitInformation.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert info.ProcessMemoryLimit == 0
    assert info.BasicLimitInformation.ActiveProcessLimit == 0


def test_build_extended_limits_with_caps():
    """传资源上限：对应标志逐项置位，KILL_ON_JOB_CLOSE 恒在。"""
    cap = 1024 ** 3
    info = build_extended_limits(memory_cap_bytes=cap, max_processes=64)
    flags = info.BasicLimitInformation.LimitFlags
    assert flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY
    assert flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    assert info.ProcessMemoryLimit == cap
    assert info.BasicLimitInformation.ActiveProcessLimit == 64


def test_build_extended_limits_ignores_nonpositive_caps():
    """0 / 负数上限视同不设：不置位、不写值（防手滑配出 0 上限直接杀进程）。"""
    info = build_extended_limits(memory_cap_bytes=0, max_processes=-5)
    assert info.BasicLimitInformation.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert info.ProcessMemoryLimit == 0
    assert info.BasicLimitInformation.ActiveProcessLimit == 0


def test_build_ui_restrictions_clipboard_only():
    """只禁剪贴板读/写；其余位（尤其 HANDLES）必须为 0——禁了 HANDLES 连
    引擎给子进程的标准输出管道一起禁，命令输出会断流。Win32 里不存在 HOOKS
    位（钩子限制由 HANDLES 承担），「禁全局钩子」的掩码宣称是假实现，不设。"""
    ui = build_ui_restrictions()
    assert ui.UIRestrictionsClass == (
        JOB_OBJECT_UILIMIT_READCLIPBOARD | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
    )


def test_degraded_job_functions_do_not_raise():
    """降级 job（handle=None）上所有操作一律 False / no-op，绝不抛。"""
    job = ContainmentJob(None, "测试降级")
    assert not job.active
    assert assign_process(job, 0) is False
    assert apply_ui_restrictions(job) is False
    assert terminate_job(job) is False
    close_job(job)  # no-op
    assert terminate_job(None) is False
    assert apply_ui_restrictions(None) is False
    close_job(None)


@pytest.mark.skipif(WIN, reason="非 Windows 才走降级路径")
def test_create_job_degrades_on_non_windows():
    """非 Windows 上 create_containment_job 降级为 inactive 并带可读原因。"""
    job = create_containment_job()
    assert not job.active
    assert job.reason  # 降级必须带原因，不能静默装作生效


# ---- 真机用例（Windows） ----


@pytest.mark.skipif(not WIN, reason="Job Object 是 Windows 专属能力")
def test_real_job_object_terminates_process():
    """真机小用例：起 cmd /c pause 入 job，TerminateJobObject 后带超时断言退出。"""
    proc = subprocess.Popen(
        ["cmd.exe", "/d", "/s", "/c", "pause"],
        stdin=subprocess.DEVNULL,  # pause 等不到按键必然阻塞，不会自己退出
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    job = create_containment_job()
    try:
        assert job.active, f"遏制未生效：{job.reason}"
        assert assign_process(job, proc._handle), "AssignProcessToJobObject 失败"
        # 本机构建若拒绝 HOOKS 位，apply_ui_restrictions 回退只设剪贴板限制，
        # 仍返回 True（有真实效果的子集）；完全没配上才算失败
        assert apply_ui_restrictions(job), "UI 限制配置失败"
        assert proc.poll() is None  # 还活着
        assert terminate_job(job), "TerminateJobObject 失败"
        proc.wait(timeout=10)  # 带超时：遏制失效时用例失败而不是挂死
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        close_job(job)
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.skipif(not WIN, reason="真机验证后台记录携带生效的作业句柄")
async def test_background_record_carries_active_job(tmp_path):
    """background=true 的常驻进程同样入 job：_BG 记录带 active 的 ContainmentJob，
    kill（既有 action 清理）之后作业句柄一并释放。"""
    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path)
    out = await tool.run(
        tool.args_model(command="ping -n 30 127.0.0.1 >nul", background=True), ctx
    )
    bid = int(out.split("id=")[1].split("（")[0])
    try:
        st = _BG.get(bid)
        assert st is not None
        assert st["job"] is not None and st["job"].active
    finally:
        await tool.run(tool.args_model(command="", action="kill", id=bid), ctx)


async def test_run_command_works_with_containment(tmp_path):
    """全链路：遏制开启（默认）时 run_command 照常执行并拿到输出。

    Windows 上这条路径真建了 Job Object（建/挂/收尾都在 _run_sync 内）；
    非 Windows 上遏制自动降级，命令同样照常——两条路径都过这条用例。
    """
    tool = RunCommandTool()
    out = await tool.run(
        tool.args_model(command="echo containment_ok"),
        ToolContext(working_dir=tmp_path),
    )
    assert "exit code: 0" in out
    assert "containment_ok" in out


# ---- config 链路（SKYSHEEP_HOME 已被 home fixture 隔离） ----


def test_shell_config_default_and_roundtrip(home):
    """[shell] 段：job_containment 默认开；写入 false 读回生效；删字段回默认。"""
    assert load_config().shell.job_containment is True
    update_config_section("shell", {"job_containment": False})
    assert load_config().shell.job_containment is False
    update_config_section("shell", {"job_containment": ""})  # 空值 = 删字段回默认
    assert load_config().shell.job_containment is True


# ---- 故障注入：遏制降级必须留痕（日志 + 结果注记），命令照常执行 ----


def _patch_obs_warning(monkeypatch):
    import skysheep.obs as obs_mod

    calls: list[tuple] = []
    monkeypatch.setattr(
        obs_mod, "warning", lambda ev, msg, **fields: calls.append((ev, msg, fields))
    )
    return calls


@pytest.mark.skipif(not WIN, reason="注入点针对 Windows 上的 kernel32 路径")
async def test_containment_degradation_logged_and_noted(tmp_path, monkeypatch):
    """CreateJobObjectW 失败：降级记 obs 日志、run_command 结果尾部带
    「遏制未生效」注记、命令输出照常返回（降级不是静默放行）。"""

    calls = _patch_obs_warning(monkeypatch)

    class _FailingK32:
        @staticmethod
        def CreateJobObjectW(_attrs, _name):
            return None

    monkeypatch.setattr(sandbox_win, "_kernel32", lambda: _FailingK32())

    tool = RunCommandTool()
    out = await tool.run(
        tool.args_model(command="echo degrade_ok"), ToolContext(working_dir=tmp_path)
    )
    assert "degrade_ok" in out, "遏制降级时命令照常执行"
    assert "遏制未生效" in out, "结果必须如实呈现降级"
    assert any(ev == "job_containment_degraded" and f.get("reason") for ev, _m, f in calls), \
        "降级必须留 obs 日志（带 reason，不含命令内容）"

    # 后台路径同样带注记
    out2 = await tool.run(
        tool.args_model(command="echo bg_ok", background=True),
        ToolContext(working_dir=tmp_path),
    )
    assert "bg_ok" in out2 and "遏制未生效" in out2


async def test_containment_disabled_by_config_adds_no_note(tmp_path):
    """[shell] job_containment = false 是用户主动关闭，不是故障降级：
    命令照常执行，结果不得误报「遏制未生效」注记。"""

    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path, job_containment=False)
    out = await tool.run(tool.args_model(command="echo plain_ok"), ctx)
    assert "plain_ok" in out, "关闭遏制时命令照常执行"
    assert "遏制未生效" not in out, "主动关闭不是降级，不得误报注记"

    out2 = await tool.run(tool.args_model(command="echo plain_bg", background=True), ctx)
    assert "plain_bg" in out2 and "遏制未生效" not in out2


def test_run_sync_closes_job_when_reader_thread_cannot_start(tmp_path, monkeypatch):
    """前台收尾必须在 finally：读线程启动失败（线程耗尽形态）时，作业句柄
    也要被 close（异常路径不得泄漏，泄漏期 kill-on-close 不会触发）。"""
    from skysheep.tools import shell as shell_mod

    real_thread = threading.Thread
    starts = {"n": 0}

    def flaky_thread(*a, **k):
        t = real_thread(*a, **k)
        real_start = t.start

        def start():
            starts["n"] += 1
            if starts["n"] == 2:  # 第二个读线程启动失败（计数跨线程共享）
                raise RuntimeError("无法启动新线程（模拟线程耗尽）")
            real_start()

        t.start = start
        return t

    monkeypatch.setattr(shell_mod.threading, "Thread", flaky_thread)
    closed: list[ContainmentJob] = []
    real_close = shell_mod.close_job

    def spy_close(job):
        closed.append(job)
        real_close(job)

    monkeypatch.setattr(shell_mod, "close_job", spy_close)
    with pytest.raises(RuntimeError):
        shell_mod._run_sync(shell_mod._shell_argv("echo guard"), str(tmp_path), 30)
    assert closed and closed[0].handle is None, "异常路径也要关作业句柄（finally 收尾）"


def test_bg_start_registers_before_pump_and_cleans_up_on_failure(tmp_path, monkeypatch):
    """后台启动先登记再起读线程：pump 线程 start 失败时从 _BG 摘除、树杀、
    关作业句柄再抛——进程不能成为读不到也杀不掉的孤儿。"""
    from skysheep.tools import shell as shell_mod

    real_thread = threading.Thread
    starts = {"n": 0}

    def flaky_thread(*a, **k):
        t = real_thread(*a, **k)
        real_start = t.start

        def start():
            starts["n"] += 1
            if starts["n"] == 2:  # 第二个 pump 线程启动失败（stderr pump）
                raise RuntimeError("无法启动新线程（模拟线程耗尽）")
            real_start()

        t.start = start
        return t

    monkeypatch.setattr(shell_mod.threading, "Thread", flaky_thread)
    closed: list[ContainmentJob] = []
    real_close = shell_mod.close_job

    def spy_close(job):
        closed.append(job)
        real_close(job)

    monkeypatch.setattr(shell_mod, "close_job", spy_close)
    before = dict(shell_mod._BG)
    bid_hint = shell_mod._BG_NEXT_ID[0]
    with pytest.raises(RuntimeError):
        shell_mod._bg_start(shell_mod._shell_argv("echo bg-guard"), str(tmp_path))
    assert bid_hint not in shell_mod._BG, "失败后记录必须从 _BG 摘除"
    assert set(shell_mod._BG) == set(before)
    assert closed and closed[0].handle is None, "作业句柄一并释放"
