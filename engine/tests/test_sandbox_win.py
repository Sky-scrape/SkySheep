"""命令执行沙箱化（security/sandbox_win.py）的测试：一期 Job Object 遏制 +
二期受限令牌。

覆盖五层：
- 结构构造纯函数（build_extended_limits / build_ui_restrictions /
  build_low_integrity_label / build_unicode_env_block / build_startupinfo /
  launch_flags_for_restricted）：不触 Win32 API、跨平台可跑，断言结构与
  字段值；
- 降级路径：降级的 ContainmentJob / RestrictedToken / RestrictedSpawn 上
  所有函数不抛、一律 False / no-op；
- 真机用例（Windows，skipif 非 win32）：cmd /c pause 入 job，
  TerminateJobObject 后带超时断言进程退出；run_command 前台/后台全链路
  在遏制开启时照常工作；受限令牌真机 smoke（能启动的环境断言 exit 0 与
  低完整性 4096，普通用户态环境按实测平台限制断言 fail-visible 降级）；
- config 链路：[shell] job_containment / sandbox_level 默认值、写入后读回
  生效（home fixture 隔离 SKYSHEEP_HOME，不碰真实 ~/.skysheep）；
- 故障注入：任一步失败 → 降级 + reason 传播 + 结果注记，命令照常执行。
所有等待都带超时，绝不无限挂起。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from skysheep.config import load_config, update_config_section
from skysheep.security import sandbox_win
from skysheep.security.sandbox_win import (
    CREATE_UNICODE_ENVIRONMENT,
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_UILIMIT_READCLIPBOARD,
    JOB_OBJECT_UILIMIT_WRITECLIPBOARD,
    STARTF_USESTDHANDLES,
    ContainmentJob,
    apply_ui_restrictions,
    assign_process,
    build_extended_limits,
    build_low_integrity_label,
    build_startupinfo,
    build_ui_restrictions,
    build_unicode_env_block,
    close_job,
    create_containment_job,
    launch_flags_for_restricted,
    terminate_job,
)
from skysheep.tools.base import ToolContext
from skysheep.tools.shell import _BG, RunCommandTool, _ProcProxy

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


def test_shell_config_sandbox_level_default_and_roundtrip(home):
    """[shell] sandbox_level（二期）：默认 "job"；写入 "restricted" 读回生效；
    删字段回默认。未知值不在这里拦（读取侧按 "job" 处理，见 ShellConfig）。"""
    assert load_config().shell.sandbox_level == "job"
    update_config_section("shell", {"sandbox_level": "restricted"})
    assert load_config().shell.sandbox_level == "restricted"
    update_config_section("shell", {"sandbox_level": ""})  # 空值 = 删字段回默认
    assert load_config().shell.sandbox_level == "job"


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


# ============================================================
# 二期：受限令牌（restricted token）
# ============================================================

# ---- 纯函数（结构构造，跨平台） ----


def test_build_low_integrity_label_sids():
    """TokenIntegrityLevel 载荷 = S-1-16-4096：修订 1、1 个子权限、授权机构
    16（0x10）、子权限 4096；label.Label.Sid 指向缓冲（非空）。"""
    label, sid_buf = build_low_integrity_label()
    raw = bytes(sid_buf)
    assert sid_buf[0] == 1, "SID Revision = 1"
    assert sid_buf[1] == 1, "SubAuthorityCount = 1"
    assert raw[2:8] == b"\x00\x00\x00\x00\x00\x10", "授权机构 SECURITY_MANDATORY_LABEL_AUTHORITY=16"
    sub = int.from_bytes(raw[8:12], "little")
    assert sub == 4096, "子权限 = SECURITY_MANDATORY_LOW_RID (0x1000)"
    assert label.Label.Sid, "label 必须指向 SID 缓冲"
    assert label.Label.Attributes == 0


def test_build_unicode_env_block():
    """Unicode 环境块：每条 key=value\\0，整体再补一个 \\0；空 key 跳过。"""
    block = build_unicode_env_block({"A": "1", "B": "x y", "": "skipped"})
    assert block == "A=1\x00B=x y\x00\x00"
    assert build_unicode_env_block({}) == "\x00"


def test_build_startupinfo_std_handles():
    """STARTUPINFOW：cb 按 STARTUPINFOW 取、STARTF_USESTDHANDLES、三句柄就位。"""
    import ctypes

    si = build_startupinfo(0x11, 0x22, 0x33)
    assert si.cb == ctypes.sizeof(si), "cb = sizeof(STARTUPINFOW)（AsUser 无属性表）"
    assert si.dwFlags == STARTF_USESTDHANDLES
    assert si.hStdInput == 0x11 and si.hStdOutput == 0x22 and si.hStdError == 0x33
    si2 = build_startupinfo(None, None, None)
    assert si2.hStdInput is None and si2.hStdOutput is None and si2.hStdError is None


def test_launch_flags_for_restricted():
    """受限路径 creationflags：叠加 CREATE_UNICODE_ENVIRONMENT，保留基础位。"""
    assert launch_flags_for_restricted() == CREATE_UNICODE_ENVIRONMENT
    base = 0x200  # CREATE_NEW_PROCESS_GROUP
    assert launch_flags_for_restricted(base) == base | CREATE_UNICODE_ENVIRONMENT


def test_restricted_degraded_objects_do_not_raise():
    """降级对象（RestrictedToken/RestrictedSpawn）上所有操作不抛、语义为空。"""
    tok = sandbox_win.RestrictedToken(None, "测试降级")
    assert not tok.active
    sandbox_win.close_token(tok)  # no-op
    sandbox_win.close_token(None)  # no-op
    sp = sandbox_win.RestrictedSpawn()
    assert not sp.ok
    assert sandbox_win.process_exit_code(0) is None
    assert sandbox_win.terminate_process(0) is False
    assert sandbox_win.wait_process(0, 100) != sandbox_win.WAIT_OBJECT_0
    sandbox_win.close_process_handle(None)  # no-op


def test_tool_context_sandbox_level_default():
    """ToolContext.sandbox_level 默认 "job"（一期现状，行为不变）。"""
    assert ToolContext(working_dir=None).sandbox_level == "job"


# ---- 故障注入：令牌构造任一步失败 → 降级 + reason 指明步骤 ----


def _set_handle(byref_arg, value: int) -> None:
    """往 byref(c_void_p) 参数里写句柄值（测试替身用）。"""
    import ctypes

    ctypes.cast(byref_arg, ctypes.POINTER(ctypes.c_void_p))[0] = value


class _FakeA32:
    """advapi32 替身：按 fail_at 在指定步骤返回失败。

    伪句柄用奇数（Windows 句柄恒为 4 的倍数）——CloseHandle 对它们只会
    静默失败，绝不会误关测试进程的真实句柄。
    """

    H1, H2, H3 = 0xDEAD0001, 0xDEAD0003, 0xDEAD0005

    def __init__(self, fail_at: str) -> None:
        self.fail_at = fail_at

    def OpenProcessToken(self, _cur, _access, out):
        if self.fail_at == "OpenProcessToken":
            return False
        _set_handle(out, self.H1)
        return True

    def DuplicateTokenEx(self, *_a):
        if self.fail_at == "DuplicateTokenEx":
            return False
        _set_handle(_a[5], self.H2)
        return True

    def CreateRestrictedToken(self, *_a):
        if self.fail_at == "CreateRestrictedToken":
            return False
        _set_handle(_a[8], self.H3)
        return True

    def SetTokenInformation(self, *_a):
        return self.fail_at != "SetTokenInformation"


@pytest.mark.skipif(not WIN, reason="注入点针对 Windows 上的 advapi32 路径")
@pytest.mark.parametrize(
    "step", ["OpenProcessToken", "DuplicateTokenEx", "CreateRestrictedToken", "SetTokenInformation"]
)
def test_build_restricted_token_degrades_per_step(monkeypatch, step):
    """令牌构造任一步失败：降级为 inactive，reason 指明失败步骤，不抛。"""
    monkeypatch.setattr(sandbox_win, "_advapi32", lambda: _FakeA32(step))
    tok = sandbox_win.build_restricted_token()
    assert not tok.active
    assert step in tok.reason, f"reason 应指明失败步骤：{tok.reason}"


@pytest.mark.skipif(not WIN, reason="注入点针对 Windows 上的 advapi32 路径")
def test_build_restricted_token_degrades_without_api(monkeypatch):
    """advapi32 加载失败：降级 + 可读原因（fail-visible 的最小单元）。"""
    monkeypatch.setattr(sandbox_win, "_advapi32", lambda: None)
    tok = sandbox_win.build_restricted_token()
    assert not tok.active
    assert tok.reason


@pytest.mark.skipif(not WIN, reason="注入点针对 Windows 上的 kernel32 路径")
async def test_restricted_degrade_full_path_fg_and_bg(tmp_path, monkeypatch):
    """spawn 失败 → 自动降回 job-only：命令照常执行、obs.warning 留痕、
    前台与 background=true 两条路径的结果尾部都带「受限令牌未生效」注记。"""
    calls = _patch_obs_warning(monkeypatch)
    monkeypatch.setattr(sandbox_win, "_advapi32", lambda: _FakeA32("OpenProcessToken"))

    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path, sandbox_level="restricted")
    out = await tool.run(tool.args_model(command="echo restricted_degrade_ok"), ctx)
    assert "restricted_degrade_ok" in out, "降级时命令照常执行"
    assert "受限令牌未生效" in out, "结果必须如实呈现降级（fail-visible）"
    assert any(
        ev == "restricted_token_degraded" and f.get("reason")
        for ev, _m, f in calls
    ), "降级必须留 obs 日志（带 reason，不含命令内容）"

    out2 = await tool.run(
        tool.args_model(command="echo restricted_bg_ok", background=True), ctx
    )
    assert "restricted_bg_ok" in out2 and "受限令牌未生效" in out2


async def test_restricted_ignored_when_containment_off(tmp_path):
    """job_containment=false 是总开关：sandbox_level="restricted" 不复活沙箱，
    命令照常执行且不得出现降级注记（主动关闭不是故障）。"""
    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path, job_containment=False,
                      sandbox_level="restricted")
    out = await tool.run(tool.args_model(command="echo plain_restricted"), ctx)
    assert "plain_restricted" in out
    assert "未生效" not in out


@pytest.mark.skipif(WIN, reason="非 Windows 上受限令牌路径按平台降级")
async def test_restricted_degrades_on_non_windows(tmp_path):
    """非 Windows：sandbox_level="restricted" 降回 job-only 并附注记。"""
    tool = RunCommandTool()
    out = await tool.run(
        tool.args_model(command="echo posix_restricted"),
        ToolContext(working_dir=tmp_path, sandbox_level="restricted"),
    )
    assert "posix_restricted" in out, "降级时命令照常执行"
    assert "受限令牌未生效" in out


@pytest.mark.skipif(not WIN, reason="受限令牌路径的异常收尾针对 Windows")
async def test_restricted_proxy_closed_when_reader_thread_fails(tmp_path, monkeypatch):
    """受限令牌启动成功的环境：读线程启动失败（线程耗尽形态）时，异常路径
    也要释放受限进程句柄（finally 收尾）。普通用户态环境无特权启动必然
    降级为 Popen 路径，此用例按平台限制 skip（如实标注）。"""
    from skysheep.tools import shell as shell_mod

    cap_ok, cap_reason = _restricted_launch_capability(str(tmp_path))
    if not cap_ok:
        pytest.skip(f"本环境无特权启动受限令牌（普通用户态实测限制）：{cap_reason}")

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
    closed: list[int] = []
    real_close_h = sandbox_win.close_process_handle

    def spy_close(handle):
        closed.append(handle)
        real_close_h(handle)

    monkeypatch.setattr(sandbox_win, "close_process_handle", spy_close)
    with pytest.raises(RuntimeError):
        shell_mod._run_sync(
            shell_mod._shell_argv("echo proxy-guard"), str(tmp_path), 30,
            job_containment=True, sandbox_level="restricted",
        )
    assert closed, "异常路径也要释放受限进程句柄（finally 收尾）"


# ---- 真机用例（Windows） ----


def _cmd_argv(command: str) -> list[str]:
    root = os.environ.get("SystemRoot") or os.sep.join(("", "Windows"))
    return [os.path.join(root, "System32", "cmd.exe"), "/d", "/s", "/c", command]


def _spawn_env() -> dict:
    return {
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "PATH": os.environ.get("PATH", ""),
    }


def _restricted_launch_capability(cwd: str) -> tuple[bool, str]:
    """探测当前环境能否真启动受限令牌（管理员提权 = 能；普通用户态 = 1314）。"""
    sp = sandbox_win.spawn_with_restricted_token(
        _cmd_argv("exit 0"), cwd, _spawn_env()
    )
    if sp.ok:
        sandbox_win.wait_process(sp.proc_handle, 10000)
        sandbox_win.close_process_handle(sp.proc_handle)
        return True, ""
    return False, sp.reason


def _child_integrity(proc_handle: int) -> int | None:
    """GetTokenInformation(TokenIntegrityLevel) 读子进程完整性子权限。"""
    import ctypes

    a32 = sandbox_win._advapi32()
    if a32 is None:
        return None
    tok = ctypes.c_void_p(0)
    if not a32.OpenProcessToken(proc_handle, sandbox_win.TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        need = ctypes.c_uint32(0)
        a32.GetTokenInformation(tok, sandbox_win.TokenIntegrityLevel, None, 0,
                                ctypes.byref(need))
        if not need.value:
            return None
        buf = (ctypes.c_ubyte * need.value)()
        if not a32.GetTokenInformation(tok, sandbox_win.TokenIntegrityLevel, buf,
                                       need.value, ctypes.byref(need)):
            return None
        label = sandbox_win._TOKEN_MANDATORY_LABEL.from_buffer(buf)
        sid = (ctypes.c_ubyte * 12).from_address(label.Label.Sid)
        return int.from_bytes(bytes(sid[8:12]), "little")
    finally:
        from skysheep.security.sandbox_win import close_process_handle
        close_process_handle(tok.value)


@pytest.mark.skipif(not WIN, reason="受限令牌是 Windows 专属能力")
def test_real_restricted_token_builds():
    """真机验证令牌构造四步（OpenProcessToken→Duplicate→Restrict→低完整性）
    在普通权限下即可成功——限制只在启动那一步。"""
    tok = sandbox_win.build_restricted_token()
    assert tok.active, f"令牌构造失败：{tok.reason}"
    sandbox_win.close_token(tok)
    assert not tok.active and tok.reason == "句柄已关闭"


@pytest.mark.skipif(not WIN, reason="真机受限令牌 smoke")
async def test_real_restricted_spawn_smoke(tmp_path):
    """cmd /c echo 在受限令牌档下执行：
    - 能启动的环境（管理员提权）：exit 0、拿到输出、无降级注记；
    - 普通用户态环境（实测平台限制，启动需 SeAssignPrimaryTokenPrivilege）：
      自动降回 job-only，命令照常执行、结果带「受限令牌未生效」+ 可读原因
      （fail-visible），绝不静默、绝不拒绝执行。"""
    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path, sandbox_level="restricted")
    out = await tool.run(tool.args_model(command="echo restricted_ok"), ctx)
    assert "restricted_ok" in out, "无论降级与否命令都照常执行"
    if "受限令牌未生效" in out:
        assert "1314" in out, "普通用户态降级原因应指明特权缺失（WinError 1314）"
    else:
        assert "exit code: 0" in out, "能启动的环境必须完整生效"


@pytest.mark.skipif(not WIN, reason="真机受限令牌后台 smoke")
async def test_real_restricted_background_smoke(tmp_path):
    """background=true 同样走受限令牌档：能启动的环境下 _BG 记录带
    _ProcProxy 与生效的作业句柄；普通用户态环境带降级注记（fail-visible）。
    两条路径都带超时收尾，不挂起。"""
    tool = RunCommandTool()
    ctx = ToolContext(working_dir=tmp_path, sandbox_level="restricted")
    out = await tool.run(
        tool.args_model(command="ping -n 30 127.0.0.1 >nul", background=True), ctx
    )
    bid = int(out.split("id=")[1].split("（")[0])
    try:
        if "受限令牌未生效" in out:
            assert "1314" in out, "降级注记须带可读原因"
            return
        st = _BG.get(bid)
        assert st is not None
        assert isinstance(st["proc"], _ProcProxy), "能启动的环境必须走受限令牌启动路径"
        assert st["job"] is not None and st["job"].active, "作业遏制照常叠加"
    finally:
        # 收尾带超时语义（kill 内部树杀 + terminate + close，不等待退出）
        await tool.run(tool.args_model(command="", action="kill", id=bid), ctx)


@pytest.mark.skipif(not WIN, reason="真机低完整性验证")
def test_real_restricted_child_integrity(tmp_path):
    """受限令牌下启动的子进程完整性确认为低档 4096（能力自证，不是「能跑
    就算」）。普通用户态环境无法启动 → skip 并如实标注平台限制。"""
    cap_ok, cap_reason = _restricted_launch_capability(str(tmp_path))
    if not cap_ok:
        pytest.skip(f"本环境无特权启动受限令牌（普通用户态实测限制）：{cap_reason}")
    sp = sandbox_win.spawn_with_restricted_token(
        _cmd_argv("exit 0"), str(tmp_path), _spawn_env()
    )
    assert sp.ok, sp.reason
    try:
        rc = sandbox_win.wait_process(sp.proc_handle, 15000)
        assert rc == sandbox_win.WAIT_OBJECT_0, "带超时等待子进程退出"
        assert sandbox_win.process_exit_code(sp.proc_handle) == 0
        il = _child_integrity(sp.proc_handle)
        assert il == 4096, f"子进程完整性应为低档 4096，实测 {il}"
    finally:
        sandbox_win.close_process_handle(sp.proc_handle)
