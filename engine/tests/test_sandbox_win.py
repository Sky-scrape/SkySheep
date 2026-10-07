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

import pytest

from skysheep.config import load_config, update_config_section
from skysheep.security.sandbox_win import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_UILIMIT_HOOKS,
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


def test_build_ui_restrictions_clipboard_and_hooks_only():
    """只禁剪贴板读/写与全局钩子；其余位（尤其 HANDLES）必须为 0——
    禁了 HANDLES 连引擎给子进程的标准输出管道一起禁，命令输出会断流。"""
    ui = build_ui_restrictions()
    assert ui.UIRestrictionsClass == (
        JOB_OBJECT_UILIMIT_READCLIPBOARD
        | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
        | JOB_OBJECT_UILIMIT_HOOKS
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
