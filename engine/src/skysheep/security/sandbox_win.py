"""命令执行沙箱化一期：Windows Job Object 进程遏制（run_command 子进程）。

**诚实边界：这不是完整安全沙箱。** Job Object 只做「进程遏制」——把一次
run_command 拉起的整棵进程树纳入一个作业对象，换来三件事：

- :func:`terminate_job`（TerminateJobObject）一次结束整棵树，比 taskkill /T
  更可靠（不会漏杀重父进程的孙进程），是超时/收尾的兜底手段；
- JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE：引擎进程退出（含崩溃）时句柄被系统
  关闭，作业内还在跑的进程一并被杀——后台常驻进程不再残留孤儿；
- 可选的内存 / 进程数资源上限（memory_cap_bytes / max_processes）。

它**不能**限制子进程的文件系统、网络、注册表访问。受限令牌（Restricted
Token）/ AppContainer 级别的真沙箱属二期，本模块不做假实现。命令能不能跑、
跑什么，仍由 PermissionGate 的人工确认 / 白名单把守；本模块只是把「放出去
的进程」关进一个随时可整体回收的笼子。

实现要点（纯标准库 ctypes 直连 kernel32，零新依赖）：

- CreateJobObjectW 建作业 → SetInformationJobObject 配
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION（LimitFlags 按传入参数逐项置位，
  KILL_ON_JOB_CLOSE 恒置位）；
- AssignProcessToJobObject 把子进程句柄挂进作业（Windows 8+ 支持嵌套作业，
  引擎自身已在某个作业里时同样可用）；
- :func:`apply_ui_restrictions` 配 JobObjectBasicUIRestrictions：禁剪贴板
  读 / 写（READCLIPBOARD / WRITECLIPBOARD）。钩子限制（防子进程装键盘 /
  鼠标钩子）实际由 UILIMIT_HANDLES 承担（Win32 文明文：作业外线程装不了
  钩子的前提是 HANDLES 限制生效），而禁 HANDLES 会连引擎传给子进程的
  标准输出管道句柄一起禁掉、命令输出直接断流——一期**不做**钩子限制，
  此处不做假实现；确需时二期以 UserHandleGrantAccess 对管道句柄逐一
  授权后再启用 HANDLES 位。

降级策略：非 Windows、kernel32 不可用、任何 API 调用失败，一律降级为
no-op（``ContainmentJob.active == False``，:attr:`ContainmentJob.reason`
带可读原因），**不抛异常**——遏制失败不能让命令执行本身失败，命令的权限
控制另有 PermissionGate。

已知局限（如实记录，不做假实现）：

- 挂入作业发生在 CreateProcess 返回之后（Popen 不暴露挂起创建 + 主线程
  句柄，重建 CreateProcess 流程超出一期范围），理论上存在子进程抢先派生
  孙进程、孙进程逃出作业的窗口；cmd /c 从启动到执行命令有数十毫秒解析期，
  窗口极小，但不是零。
- 一期 run_command 只启用 KILL_ON_JOB_CLOSE（进程遏制），不传资源上限：
  PROCESS_MEMORY 是按进程的提交内存上限，给低了会误杀重型构建（rustc /
  node 大项目轻松吃数 GB），ACTIVE_PROCESS_LIMIT 同理会打断大并行编译。
  参数留待后续按配置开放。
"""

from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

# SetInformationJobObject / QueryInformationJobObject 的 InformationClass
JobObjectExtendedLimitInformation = 9
JobObjectBasicUIRestrictions = 4

# JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimitInformation.LimitFlags
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# JOBOBJECT_BASIC_UI_RESTRICTIONS.UIRestrictionsClass
# 只用有真实效果的剪贴板两位。注意 Win32 里**不存在** HOOKS 位（合法位只有
# 0x1-0x80：HANDLES/READCLIPBOARD/WRITECLIPBOARD/SYSTEMPARAMETERS/
# DISPLAYSETTINGS/GLOBALATOMS/DESKTOP/EXITWINDOWS，微软文档成员表明文）；
# 钩子限制实际由 UILIMIT_HANDLES 承担（对 stdout 管道致命，一期不做）。
# 任何「禁全局钩子」的位掩码宣称在本模块都是假实现，不做。
JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x00000002
JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x00000004

# CreateJobObjectW 失败时可能返回的两种值：NULL（ctypes c_void_p restype 转 None）
# 与 INVALID_HANDLE_VALUE（按指针宽度回读的 -1）
_INVALID_HANDLE = ctypes.c_void_p(-1).value

# ---- Win32 结构体（结构定义与平台无关，非 Windows 也能安全导入/构造，供测试） ----


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", ctypes.c_uint32)]


# ---- 纯函数（结构构造，不触 API，跨平台可测） ----


def build_extended_limits(
    memory_cap_bytes: int | None = None, max_processes: int | None = None
) -> _JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    """构造 JOBOBJECT_EXTENDED_LIMIT_INFORMATION。

    KILL_ON_JOB_CLOSE 恒置位（进程遏制的底线：句柄关闭即杀整棵树）；
    memory_cap_bytes / max_processes 传正值时才置 PROCESS_MEMORY /
    ACTIVE_PROCESS_LIMIT 对应位并填值——不传就不设该上限。
    """
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_cap_bytes is not None and memory_cap_bytes > 0:
        info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = memory_cap_bytes
    if max_processes is not None and max_processes > 0:
        info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = max_processes
    return info


def build_ui_restrictions() -> _JOBOBJECT_BASIC_UI_RESTRICTIONS:
    """构造 JOBOBJECT_BASIC_UI_RESTRICTIONS：禁剪贴板读/写。

    刻意不含 UILIMIT_HANDLES（钩子限制的实际承担者）：禁了 HANDLES 连引擎
    传给子进程的标准输出管道一起禁，命令输出直接断流——一期不做钩子限制。
    """
    ui = _JOBOBJECT_BASIC_UI_RESTRICTIONS()
    ui.UIRestrictionsClass = (
        JOB_OBJECT_UILIMIT_READCLIPBOARD | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
    )
    return ui


# ---- kernel32 装载（惰性 + 缓存；非 Windows / 加载失败返回 None，不抛） ----

_K32 = None
_K32_TRIED = False


def _kernel32():
    global _K32, _K32_TRIED
    if not IS_WINDOWS:
        return None
    if not _K32_TRIED:
        _K32_TRIED = True
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # 64 位下句柄/指针按指针宽度走，截断成 32 位会坏；BOOL 显式 1 字节
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ]
            k32.SetInformationJobObject.restype = ctypes.c_bool
            k32.AssignProcessToJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
            ]
            k32.AssignProcessToJobObject.restype = ctypes.c_bool
            k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.TerminateJobObject.restype = ctypes.c_bool
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_bool
            _K32 = k32
        except Exception:  # noqa: BLE001 - 加载失败按降级处理，绝不抛
            _K32 = None
    return _K32


class ContainmentJob:
    """一次命令执行的进程遏制域。

    active 为 True 时 handle 是作业句柄（int），terminate/close/assign 都可用；
    降级时 handle 为 None，reason 带可读原因（非 Windows / API 失败 / 已关闭）。
    """

    __slots__ = ("handle", "reason")

    def __init__(self, handle: int | None, reason: str = "") -> None:
        self.handle = handle
        self.reason = reason

    @property
    def active(self) -> bool:
        return self.handle is not None

    def __del__(self) -> None:
        # 兜底收尾（调用方异常路径漏掉 close_job 时防句柄泄漏）：对象失去引用
        # 意味着调用方已无法再对它 terminate，此时关闭句柄触发的 KILL_ON_JOB_CLOSE
        # 整树终止正是本模块定义的清理语义，不会误杀仍被管理的进程。
        # close_job 的既有路径都先 terminate 再 close，与此兜底语义一致。
        handle = getattr(self, "handle", None)
        if not handle:
            return
        try:
            k32 = _kernel32()
            if k32 is not None:
                k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001  终结器里绝不抛
            pass
        self.handle = None

    def __repr__(self) -> str:
        state = f"handle={self.handle:#x}" if self.active else f"degraded({self.reason})"
        return f"<ContainmentJob {state}>"


# ---- 作业生命周期（全部不抛：失败降级返回 False / None + reason） ----


def create_containment_job(
    memory_cap_bytes: int | None = None, max_processes: int | None = None
) -> ContainmentJob:
    """建作业对象并按参数配好限制；任何失败降级为 inactive 的 ContainmentJob。

    memory_cap_bytes / max_processes 传 None = 不设对应资源上限，只保留
    KILL_ON_JOB_CLOSE 的遏制语义（一期 run_command 的用法）。
    """
    k32 = _kernel32()
    if k32 is None:
        return ContainmentJob(None, "非 Windows 平台，Job Object 进程遏制不可用")
    handle: int | None = None
    try:
        handle = k32.CreateJobObjectW(None, None)
        if not handle or handle == _INVALID_HANDLE:
            return ContainmentJob(
                None, f"CreateJobObjectW 失败（WinError {ctypes.get_last_error()}）"
            )
        limits = build_extended_limits(memory_cap_bytes, max_processes)
        if not k32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            code = ctypes.get_last_error()
            k32.CloseHandle(handle)
            return ContainmentJob(None, f"SetInformationJobObject 失败（WinError {code}）")
        return ContainmentJob(handle)
    except Exception as e:  # noqa: BLE001 - 遏制失败不能拖垮命令执行
        if handle:
            try:
                k32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
        return ContainmentJob(None, f"Job Object 创建异常：{e}")


def assign_process(job: ContainmentJob | None, proc_handle: int) -> bool:
    """把进程句柄挂进作业（AssignProcessToJobObject）。

    job 未生效 / 句柄无效 / 调用失败都返回 False，不抛。同一进程只能挂进
    一个作业链（Win8+ 支持嵌套）；失败通常是引擎自身被放在不支持嵌套的
    作业里（老系统），此时遏制未生效。
    """
    if job is None or not job.active or not proc_handle:
        return False
    k32 = _kernel32()
    if k32 is None:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(job.handle, proc_handle))
    except Exception:  # noqa: BLE001
        return False


def apply_ui_restrictions(job: ContainmentJob | None) -> bool:
    """配 BASIC_UI_RESTRICTIONS：作业内进程禁读/写剪贴板。

    只影响挂进作业之后的进程，引擎自身不受影响。一期不含钩子限制（它实际
    由 UILIMIT_HANDLES 承担，会切断 stdout 管道——见模块说明，不做假实现）。
    返回 False = 限制没配上（降级 / 调用失败），不抛。
    """
    if job is None or not job.active:
        return False
    k32 = _kernel32()
    if k32 is None:
        return False
    try:
        ui = build_ui_restrictions()
        return bool(
            k32.SetInformationJobObject(
                job.handle,
                JobObjectBasicUIRestrictions,
                ctypes.byref(ui),
                ctypes.sizeof(ui),
            )
        )
    except Exception:  # noqa: BLE001
        return False


def terminate_job(job: ContainmentJob | None, exit_code: int = 1) -> bool:
    """TerminateJobObject：一次结束作业内全部进程（含整棵子进程树）。

    作业已空（成员都正常退出了）时是 no-op 返回 True。降级 / 失败返回
    False，不抛——调用方都在收尾路径上，终止失败不能打断主流程。
    """
    if job is None or not job.active:
        return False
    k32 = _kernel32()
    if k32 is None:
        return False
    try:
        return bool(k32.TerminateJobObject(job.handle, exit_code))
    except Exception:  # noqa: BLE001
        return False


def close_job(job: ContainmentJob | None) -> None:
    """CloseHandle 释放作业句柄。

    KILL_ON_JOB_CLOSE 生效时，这会杀掉作业内仍在跑的进程——引擎退出时
    系统统一关闭句柄，同一机制兜底防孤儿。已关闭 / 未生效时是 no-op。
    """
    if job is None or not job.active:
        return
    k32 = _kernel32()
    if k32 is not None:
        try:
            k32.CloseHandle(job.handle)
        except Exception:  # noqa: BLE001
            pass
    job.handle = None
    job.reason = "句柄已关闭"
