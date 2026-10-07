"""命令执行沙箱化：Windows Job Object 进程遏制（一期）+ 受限令牌（二期）。

**诚实边界：这不是完整安全沙箱。** Job Object 只做「进程遏制」——把一次
run_command 拉起的整棵进程树纳入一个作业对象，换来三件事：

- :func:`terminate_job`（TerminateJobObject）一次结束整棵树，比 taskkill /T
  更可靠（不会漏杀重父进程的孙进程），是超时/收尾的兜底手段；
- JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE：引擎进程退出（含崩溃）时句柄被系统
  关闭，作业内还在跑的进程一并被杀——后台常驻进程不再残留孤儿；
- 可选的内存 / 进程数资源上限（memory_cap_bytes / max_processes）。

二期在 Job Object 之上叠加**受限令牌**层（[shell] sandbox_level="restricted"
时启用）：特权全剥（DISABLE_MAX_PRIVILEGE）+ 低完整性（S-1-16-4096），
子进程以「无任何特权、低完整性」的令牌启动。**前提（真机实测）**：令牌
启动走 CreateProcessAsUserW，需要引擎持有 SeAssignPrimaryTokenPrivilege
——即引擎以管理员（提权）运行；普通用户态引擎令牌构造照常成功、但启动
会被系统拒绝（WinError 1314），此时自动降回 job-only 并在命令结果尾部
附「受限令牌未生效」注记，绝不静默。如实边界（对成功启动的子进程）——

- 能：特权一个都拿不到（SeBackup/SeRestore/SeDebug 等提权类操作直接失败）；
  Windows 完整性策略默认 No-Write-Up，低完整性进程**写不了**中/高完整性
  对象——用户目录与项目文件默认中完整性，写文件的命令会失败（这是限制
  的一部分而非故障）；也不能打开中完整性进程做注入/改内存。
- 不能：**读**不受限（默认策略不管读，用户可读的文件照样可读）；网络
  出站不受限（完整性机制不管网络）；对同为低完整性/无完整性标签的资源
  访问照旧。它不是 AppContainer，不提供文件系统/注册表/网络的路径级隔离。
- 命令能不能跑、跑什么，仍由 PermissionGate 的人工确认 / 白名单把守；
  本模块只是给「放出去的进程」多上一道枷锁。

实现要点（纯标准库 ctypes 直连 kernel32/advapi32，零新依赖）：

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
  授权后再启用 HANDLES 位（二期未做，本说明仅记录设想）；
- 二期 :func:`build_restricted_token` 四步构造受限令牌：OpenProcessToken
  → DuplicateTokenEx(TokenPrimary) → CreateRestrictedToken(DISABLE_MAX_
  PRIVILEGE) → SetTokenInformation(TokenIntegrityLevel, 低完整性)；
  :func:`spawn_with_restricted_token` 以 CreateProcessAsUserW 用它启动子
  进程，stdio 用新建匿名管道（STARTF_USESTDHANDLES），环境块按剥好的 env
  显式构造（UTF-16）。启动原语的取舍如实记录：设想中更优的 CreateProcessW
  属性表路径（PROC_THREAD_ATTRIBUTE_TOKEN）在目标平台**不存在**——真机
  实测 Windows 11 24H2（10.0.26100）全量属性号 0..255 无一个能以令牌启动
  子进程，社区流传的 0x00020004 实为 PROC_THREAD_ATTRIBUTE_PREFERRED_NODE；
  故走 CreateProcessAsUserW，而它要求调用方持有（并启用）
  SeAssignPrimaryTokenPrivilege / SeIncreaseQuotaPrivilege——**管理员提权
  运行的引擎满足（会先尽力启用）；普通用户态引擎不持有，受限令牌无法
  启动**（真机实测 WinError 1314，含带限制 SID 的令牌变体），按降级契约
  回落 job-only 并附注记（fail-visible）。「AsUser 的环境块难题」以显式
  构造环境块的方式解决（lpEnvironment 传 NULL 子进程拿不到引擎环境，
  显式构造顺带保证被剥掉的密钥变量不外泄）。

降级策略：非 Windows、kernel32/advapi32 不可用、任何 API 调用失败，一律
降级为 no-op（``ContainmentJob.active == False`` /
``RestrictedToken.active == False``，reason 带可读原因），**不抛异常**——
沙箱失败不能让命令执行本身失败，命令的权限控制另有 PermissionGate；降级
不静默：调用方（tools/shell.py）负责记 obs.warning 并在命令结果尾部附
「未生效」注记（fail-visible）。

已知局限（如实记录，不做假实现）：

- 挂入作业发生在 CreateProcess 返回之后（受限令牌路径按一期同样的
  非挂起顺序启动，未引入 CREATE_SUSPENDED 先挂后入作业的改造），理论上
  存在子进程抢先派生孙进程、孙进程逃出作业的窗口；cmd /c 从启动到执行
  命令有数十毫秒解析期，窗口极小，但不是零。
- run_command 只启用 KILL_ON_JOB_CLOSE（进程遏制），不传资源上限：
  PROCESS_MEMORY 是按进程的提交内存上限，给低了会误杀重型构建（rustc /
  node 大项目轻松吃数 GB），ACTIVE_PROCESS_LIMIT 同理会打断大并行编译。
  参数留待后续按配置开放。
- 受限令牌只约束「直接以它启动的那一个进程」；cmd /c 派生的孙进程继承
  受限令牌（令牌随进程派生），但继承后若有句柄/资源已被授予，令牌不
  追溯收回。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
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
            # ---- 二期（受限令牌路径） ----
            k32.GetCurrentProcess.argtypes = []
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.GetStdHandle.argtypes = [ctypes.c_uint32]
            k32.GetStdHandle.restype = ctypes.c_void_p
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.WaitForSingleObject.restype = ctypes.c_uint32
            k32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
            ]
            k32.GetExitCodeProcess.restype = ctypes.c_bool
            k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.TerminateProcess.restype = ctypes.c_bool
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


# ============================================================
# 二期：受限令牌（restricted token）
#
# 剥离全部特权（CreateRestrictedToken 的 DISABLE_MAX_PRIVILEGE）+ 低完整性
# （TokenIntegrityLevel 标为 S-1-16-4096）。能限制什么、不能限制什么见模块
# docstring——特别地：读不受限、网络不受限，低完整性只拦「向中/高完整性
# 对象写」。降级纪律与一期相同：任一步失败 → inactive + reason，不抛、
# 不静默（调用方记 obs 日志并在命令结果尾部附注记）。
# ============================================================

# OpenProcessToken 对引擎自身令牌要的访问位：能复制、能查询，复制出的新
# 令牌要能挂给新进程并调默认完整性（后四位是 CreateProcess 对主令牌的
# 文档要求）
TOKEN_ASSIGN_PRIMARY = 0x00000001
TOKEN_DUPLICATE = 0x00000002
TOKEN_QUERY = 0x00000008
TOKEN_ADJUST_DEFAULT = 0x00000080
TOKEN_ADJUST_SESSIONID = 0x00000100
TOKEN_ADJUST_PRIVILEGES = 0x00000020
_LAUNCH_TOKEN_ACCESS = (
    TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY
    | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID
)

TokenPrimary = 1            # TOKEN_TYPE：主令牌（要拿去 CreateProcess）
TokenIntegrityLevel = 25    # TOKEN_INFORMATION_CLASS：完整性级别
SecurityAnonymous = 0       # SECURITY_IMPERSONATION_LEVEL（主令牌下该参数无效果，按惯例传它）

DISABLE_MAX_PRIVILEGE = 0x00000001  # CreateRestrictedToken dwFlags：剥离全部特权

# 低完整性 SID：S-1-16-4096（授权机构 SECURITY_MANDATORY_LABEL_AUTHORITY=16，
# 子权限 SECURITY_MANDATORY_LOW_RID=0x1000）
_SECURITY_MANDATORY_LABEL_AUTHORITY = 16
_SECURITY_MANDATORY_LOW_RID = 0x1000

# CreateProcessAsUserW 启动用常量。（不做 PROC_THREAD_ATTRIBUTE_*：CreateProcessW
# 属性表里没有令牌属性——真机全量属性号扫描证实，见 spawn_with_restricted_token。）
CREATE_UNICODE_ENVIRONMENT = 0x00000400     # 环境块按 UTF-16 解析
STARTF_USESTDHANDLES = 0x00000100           # hStdInput/hStdOutput/hStdError 生效

_STD_INPUT_HANDLE = 0xFFFFFFF6  # (DWORD)-10
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259  # 退出码占位值：读命令极少真返回它，按「仍在跑」处理（保守）

# 启动权限：CreateProcessAsUserW 需要调用方持有（并启用）这两个特权。
# 管理员提权令牌默认「持有但禁用」，AdjustTokenPrivileges 可直接启用；
# 普通用户令牌根本不持有——此时受限令牌无法启动，按降级处理（fail-visible）。
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300
ERROR_PRIVILEGE_NOT_HELD = 1314


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_long)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", ctypes.c_uint32)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", ctypes.c_uint32),
        ("Privileges", _LUID_AND_ATTRIBUTES * 1),
    ]


class _SID_IDENTIFIER_AUTHORITY(ctypes.Structure):
    _fields_ = [("Value", ctypes.c_ubyte * 6)]


class _SID(ctypes.Structure):
    """Win32 SID。固定 1 个子权限——低完整性 S-1-16-4096 只需一个，
    省掉变长数组的内存管理（TOKEN_MANDATORY_LABEL 载荷长度同步按它算）。"""

    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("SubAuthorityCount", ctypes.c_ubyte),
        ("IdentifierAuthority", _SID_IDENTIFIER_AUTHORITY),
        ("SubAuthority", ctypes.c_uint32 * 1),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


# ---- 二期纯函数（结构构造，不触 API，跨平台可测） ----


def build_low_integrity_label() -> tuple[_TOKEN_MANDATORY_LABEL, ctypes.Array]:
    """构造 SetTokenInformation(TokenIntegrityLevel) 的载荷：S-1-16-4096 低完整性。

    返回 ``(label, sid 缓冲)``——sid 缓冲必须保持引用存活到 SetTokenInformation
    返回（label.Label.Sid 指向它的内存）。纯结构构造，跨平台可测。
    """
    sid_buf = (ctypes.c_ubyte * ctypes.sizeof(_SID))()
    sid = _SID.from_buffer(sid_buf)
    sid.Revision = 1  # SID_REVISION
    sid.SubAuthorityCount = 1
    sid.IdentifierAuthority.Value[5] = _SECURITY_MANDATORY_LABEL_AUTHORITY
    sid.SubAuthority[0] = _SECURITY_MANDATORY_LOW_RID
    label = _TOKEN_MANDATORY_LABEL()
    label.Label.Sid = ctypes.cast(sid_buf, ctypes.c_void_p)
    label.Label.Attributes = 0
    return label, sid_buf


def build_unicode_env_block(env: dict[str, str]) -> str:
    """构造 Unicode 环境块：每条 ``key=value\\0``，整体再补一个 ``\\0``。

    属性表路径走 CreateProcessW：lpEnvironment 传 NULL 会继承引擎**完整**
    环境（含 tools/shell.child_environment 已剥掉的密钥类变量），所以受限
    令牌路径必须用剥好的 env 显式构造。纯字符串拼接，跨平台可测。
    """
    return "".join(f"{k}={v}\x00" for k, v in env.items() if k) + "\x00"


def build_startupinfo(
    h_stdin: int | None, h_stdout: int | None, h_stderr: int | None
) -> _STARTUPINFOW:
    """构造 STARTUPINFOW：STARTF_USESTDHANDLES + 三个标准句柄。

    cb 按 STARTUPINFOW 取（CreateProcessAsUserW 走常规启动信息，无属性表）。
    纯结构构造，跨平台可测。
    """
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = h_stdin
    si.hStdOutput = h_stdout
    si.hStdError = h_stderr
    return si


def launch_flags_for_restricted(base_flags: int = 0) -> int:
    """受限令牌路径的 creationflags：叠加 CREATE_UNICODE_ENVIRONMENT
    （环境块按 UTF-16 解析）。纯函数。"""
    return base_flags | CREATE_UNICODE_ENVIRONMENT


# ---- advapi32 装载（惰性 + 缓存；非 Windows / 加载失败返回 None，不抛） ----

_A32 = None
_A32_TRIED = False


def _advapi32():
    global _A32, _A32_TRIED
    if not IS_WINDOWS:
        return None
    if not _A32_TRIED:
        _A32_TRIED = True
        try:
            a32 = ctypes.WinDLL("advapi32", use_last_error=True)
            a32.OpenProcessToken.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
            ]
            a32.OpenProcessToken.restype = ctypes.c_bool
            a32.DuplicateTokenEx.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
            ]
            a32.DuplicateTokenEx.restype = ctypes.c_bool
            a32.CreateRestrictedToken.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            a32.CreateRestrictedToken.restype = ctypes.c_bool
            a32.SetTokenInformation.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ]
            a32.SetTokenInformation.restype = ctypes.c_bool
            # TokenIntegrityLevel 查询：真机用例验证子进程完整性确实落到
            # 低档用（能力自证），生产路径不依赖它
            a32.GetTokenInformation.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ]
            a32.GetTokenInformation.restype = ctypes.c_bool
            a32.CreateProcessAsUserW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
            ]
            a32.CreateProcessAsUserW.restype = ctypes.c_bool
            a32.LookupPrivilegeValueW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(_LUID),
            ]
            a32.LookupPrivilegeValueW.restype = ctypes.c_bool
            a32.AdjustTokenPrivileges.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
            ]
            a32.AdjustTokenPrivileges.restype = ctypes.c_bool
            _A32 = a32
        except Exception:  # noqa: BLE001 - 加载失败按降级处理，绝不抛
            _A32 = None
    return _A32


class RestrictedToken:
    """一次受限令牌构造的结果（语义对齐 ContainmentJob）。

    active 为 True 时 handle 是可挂给 CreateProcessW 属性表的新主令牌句柄
    （int），用完（启动完子进程）应调 close_token 释放；降级时 handle 为
    None，reason 带是哪一步失败的可读原因。
    """

    __slots__ = ("handle", "reason")

    def __init__(self, handle: int | None, reason: str = "") -> None:
        self.handle = handle
        self.reason = reason

    @property
    def active(self) -> bool:
        return self.handle is not None

    def __del__(self) -> None:
        # 兜底收尾（与 ContainmentJob.__del__ 同一纪律）：调用方漏掉
        # close_token 时防令牌句柄泄漏。令牌泄漏不杀进程、只漏句柄，
        # 关闭即释放，语义安全。
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
        return f"<RestrictedToken {state}>"


def build_restricted_token() -> RestrictedToken:
    """构造受限令牌：特权全剥 + 低完整性（「restricted」档的核心）。

    四步（任何一步失败即降级为 inactive + 可读原因，绝不抛）：

    1. OpenProcessToken——取引擎自身的主令牌；
    2. DuplicateTokenEx——复制成新的主令牌（访问位按 CreateProcess 对主
       令牌的文档要求取）；
    3. CreateRestrictedToken(DISABLE_MAX_PRIVILEGE)——剥离全部特权；
    4. SetTokenInformation(TokenIntegrityLevel)——标为低完整性 S-1-16-4096。

    令牌一经 SetTokenInformation 标低就不能再抬高；它在引擎进程内构造、
    只经属性表交给 CreateProcessW 一次（见 spawn_with_restricted_token），
    用完即关。
    """
    a32 = _advapi32()
    k32 = _kernel32()
    if a32 is None or k32 is None:
        return RestrictedToken(None, "受限令牌不可用（非 Windows 或 API 库加载失败）")
    src = dup = low = 0
    success = False
    try:
        cur = k32.GetCurrentProcess()
        tok = ctypes.c_void_p(0)
        if not a32.OpenProcessToken(cur, _LAUNCH_TOKEN_ACCESS, ctypes.byref(tok)) or not tok.value:
            return RestrictedToken(
                None, f"OpenProcessToken 失败（WinError {ctypes.get_last_error()}）"
            )
        src = tok.value
        dup_tok = ctypes.c_void_p(0)
        if not a32.DuplicateTokenEx(
            src, _LAUNCH_TOKEN_ACCESS, None, SecurityAnonymous, TokenPrimary,
            ctypes.byref(dup_tok),
        ) or not dup_tok.value:
            return RestrictedToken(
                None, f"DuplicateTokenEx 失败（WinError {ctypes.get_last_error()}）"
            )
        k32.CloseHandle(src)
        src = 0
        dup = dup_tok.value
        low_tok = ctypes.c_void_p(0)
        if not a32.CreateRestrictedToken(
            dup, DISABLE_MAX_PRIVILEGE,
            0, None, 0, None, 0, None, ctypes.byref(low_tok),
        ) or not low_tok.value:
            return RestrictedToken(
                None, f"CreateRestrictedToken 失败（WinError {ctypes.get_last_error()}）"
            )
        k32.CloseHandle(dup)
        dup = 0
        low = low_tok.value
        label, sid_buf = build_low_integrity_label()
        length = ctypes.sizeof(_TOKEN_MANDATORY_LABEL) + ctypes.sizeof(_SID)
        if not a32.SetTokenInformation(low, TokenIntegrityLevel, ctypes.byref(label), length):
            return RestrictedToken(
                None,
                f"SetTokenInformation(TokenIntegrityLevel) 失败（WinError {ctypes.get_last_error()}）",
            )
        success = True
        return RestrictedToken(low)
    except Exception as e:  # noqa: BLE001 - 沙箱失败不能拖垮命令执行
        return RestrictedToken(None, f"受限令牌构造异常：{e}")
    finally:
        # 失败路径上把中间/产物令牌一并关掉（成功路径 low 是返回值，src/dup
        # 已在上面各自归位关闭）
        if not success:
            for h in (src, dup, low):
                if h:
                    try:
                        k32.CloseHandle(h)
                    except Exception:  # noqa: BLE001
                        pass


def close_token(token: RestrictedToken | None) -> None:
    """CloseHandle 释放受限令牌句柄（启动完子进程后调用）。未生效/已关 no-op。"""
    if token is None or not token.active:
        return
    k32 = _kernel32()
    if k32 is not None:
        try:
            k32.CloseHandle(token.handle)
        except Exception:  # noqa: BLE001
            pass
    token.handle = None
    token.reason = "句柄已关闭"


def enable_launch_privileges() -> list[str]:
    """尽力启用 CreateProcessAsUserW 所需的两个特权（启动权限）。

    只能把「已持有但禁用」的特权翻开（管理员提权令牌的默认状态）；普通
    用户令牌根本不持有，AdjustTokenPrivileges 会按 ERROR_NOT_ALL_ASSIGNED
    谢绝——那种环境下受限令牌无法启动，由调用方按降级处理（fail-visible），
    这里绝不抛、也不做假。返回实际启用的特权名列表（供日志/诊断）。
    """
    a32 = _advapi32()
    k32 = _kernel32()
    if a32 is None or k32 is None:
        return []
    tok = ctypes.c_void_p(0)
    enabled: list[str] = []
    try:
        if not a32.OpenProcessToken(
            k32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(tok),
        ) or not tok.value:
            return []
        for name in ("SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege"):
            luid = _LUID()
            if not a32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                continue
            tp = _TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ctypes.set_last_error(0)
            if a32.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None) \
                    and ctypes.get_last_error() != ERROR_NOT_ALL_ASSIGNED:
                enabled.append(name)
        return enabled
    except Exception:  # noqa: BLE001 - 权限启用失败不阻断降级路径
        return enabled
    finally:
        if tok.value:
            try:
                k32.CloseHandle(tok.value)
            except Exception:  # noqa: BLE001
                pass


class RestrictedSpawn:
    """受限令牌路径的一次进程启动结果。

    ok 为 False 时只有 reason 有意义（调用方整体降级回普通 Popen 路径）；
    ok 为 True 时 proc_handle 是进程句柄（int，调用方用完 close_process_
    handle 释放）、pid 是进程 id、stdout_fd/stderr_fd 是输出管道父侧读端
    fd（读端归调用方 fdopen/关闭；子进程写端已在本模块内关闭）。
    """

    __slots__ = ("proc_handle", "pid", "stdout_fd", "stderr_fd", "reason")

    def __init__(
        self,
        proc_handle: int = 0,
        pid: int = 0,
        stdout_fd: int = -1,
        stderr_fd: int = -1,
        reason: str = "",
    ) -> None:
        self.proc_handle = proc_handle
        self.pid = pid
        self.stdout_fd = stdout_fd
        self.stderr_fd = stderr_fd
        self.reason = reason

    @property
    def ok(self) -> bool:
        return bool(self.proc_handle) and not self.reason


def spawn_with_restricted_token(
    argv: list[str], cwd: str, env: dict[str, str], creationflags: int = 0
) -> RestrictedSpawn:
    """以受限令牌启动子进程（CreateProcessAsUserW，显式 Unicode 环境块）。

    仅 Windows。令牌来自 build_restricted_token（特权全剥 + 低完整性）；
    stdio 用新建匿名管道——子侧写端标可继承并经 STARTF_USESTDHANDLES 挂给
    子进程，父侧读端 fd 返回给调用方；stdin 传引擎自己的标准输入句柄
    （拿不到就置 NULL，子进程读到 EOF）；环境块由 env 显式构造
    （见 build_unicode_env_block：lpEnvironment 传 NULL 子进程拿不到引擎
    环境，必须显式构造；顺带保证被剥掉的密钥类变量不外泄）。

    启动原语如实记录：设想中更优的 CreateProcessW 属性表路径
    （PROC_THREAD_ATTRIBUTE_TOKEN）在目标平台不存在——真机实测 Windows 11
    24H2（10.0.26100）属性号 0..255 无一个能以令牌启动子进程，社区流传的
    0x00020004 实为 PROC_THREAD_ATTRIBUTE_PREFERRED_NODE。故走
    CreateProcessAsUserW——它要求调用方持有（并启用）SeAssignPrimaryToken
    Privilege / SeIncreaseQuotaPrivilege：管理员提权运行的引擎满足（本函数
    会先尽力启用，见 enable_launch_privileges）；**普通用户态引擎不持有，
    启动必以 WinError 1314 失败**（真机实测，含带限制 SID 的变体），此时
    按降级契约返回 ok=False，调用方回落 job-only。

    任何一步失败：关闭本次已创建的全部资源，返回 ok=False + 可读原因，
    绝不抛——调用方降级回普通 Popen + Job Object 路径，命令照常执行。
    """
    k32 = _kernel32()
    a32 = _advapi32()
    if k32 is None or a32 is None:
        return RestrictedSpawn(reason="非 Windows 平台，受限令牌启动路径不可用")
    enable_launch_privileges()  # 尽力启用；没启到不拦，AsUser 自己会给准确错误
    token = build_restricted_token()
    if not token.active:
        return RestrictedSpawn(reason=token.reason)
    out_r = out_w = err_r = err_w = -1
    proc_h = 0
    result: RestrictedSpawn | None = None
    try:
        import msvcrt  # Windows 专属，放函数内避免非 Windows 导入错误

        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        os.set_inheritable(out_w, True)
        os.set_inheritable(err_w, True)
        out_w_h = msvcrt.get_osfhandle(out_w)
        err_w_h = msvcrt.get_osfhandle(err_w)
        stdin_h = k32.GetStdHandle(_STD_INPUT_HANDLE)
        if stdin_h in (None, _INVALID_HANDLE):
            stdin_h = None
        si = build_startupinfo(stdin_h, out_w_h, err_w_h)
        pi = _PROCESS_INFORMATION()
        # 环境块含嵌入式 NUL，必须以缓冲区形式传（c_wchar_p 直传字符串会被
        # ctypes 以 embedded null character 拒绝）
        env_buf = ctypes.create_unicode_buffer(build_unicode_env_block(env))
        if not a32.CreateProcessAsUserW(
            token.handle, None, subprocess.list2cmdline(argv),
            None, None, True, launch_flags_for_restricted(creationflags),
            ctypes.cast(env_buf, ctypes.c_void_p), cwd or None,
            ctypes.byref(si), ctypes.byref(pi),
        ) or not pi.hProcess:
            err = ctypes.get_last_error()
            hint = ""
            if err == ERROR_PRIVILEGE_NOT_HELD:
                # 真机实测：普通用户态引擎必然走到这（含带限制 SID 的令牌
                # 变体）——如实把可行条件写进原因，不糊弄「稍后再试」
                hint = ("——引擎未持有创建进程级令牌所需特权"
                        "（SeAssignPrimaryTokenPrivilege），受限令牌启动需以管理员运行")
            return RestrictedSpawn(
                reason=f"CreateProcessAsUserW 失败（WinError {err}）{hint}"
            )

        # 成功收尾：父侧写端必须关（否则读端永远等不到 EOF）；令牌与主线程
        # 句柄一并释放（子进程已拿到各自的拷贝/引用）
        os.close(out_w)
        os.close(err_w)
        out_w = err_w = -1
        k32.CloseHandle(pi.hThread)
        close_token(token)
        # 注意：ctypes 结构体字段访问已把 c_void_p 转成 int（或 None），没有 .value
        proc_h = pi.hProcess or 0
        result = RestrictedSpawn(
            proc_handle=proc_h, pid=int(pi.dwProcessId),
            stdout_fd=out_r, stderr_fd=err_r,
        )
        out_r = err_r = -1  # 读端归调用方所有
        return result
    except Exception as e:  # noqa: BLE001 - 启动失败降级，绝不抛
        return RestrictedSpawn(reason=f"受限令牌启动异常：{e}")
    finally:
        if result is None:
            # 失败收尾：关闭本次已创建的全部资源（顺序无关，句柄/描述符独立）
            for fd in (out_r, out_w, err_r, err_w):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if proc_h:
                close_process_handle(proc_h)
            close_token(token)


def wait_process(handle: int, timeout_ms: int) -> int:
    """WaitForSingleObject 等进程退出（毫秒；0xFFFFFFFF = 无限等）。

    返回等待码（WAIT_OBJECT_0 = 已退出 / WAIT_TIMEOUT = 超时），内部错误
    返回 0xFFFFFFFF（调用方按超时处理，方向安全）。不抛。
    """
    k32 = _kernel32()
    if k32 is None or not handle:
        return 0xFFFFFFFF
    try:
        return int(k32.WaitForSingleObject(handle, int(timeout_ms) & 0xFFFFFFFF))
    except Exception:  # noqa: BLE001
        return 0xFFFFFFFF


def process_exit_code(handle: int) -> int | None:
    """GetExitCodeProcess：进程已退出返回退出码；仍在跑 / 取不到返回 None。

    STILL_ACTIVE（259）按「仍在跑」处理：真返回 259 的命令极罕见，且
    _tree_kill / TerminateJobObject 收尾强制 1，宁可保守不多报退出。
    """
    k32 = _kernel32()
    if k32 is None or not handle:
        return None
    try:
        code = ctypes.c_uint32(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE else int(code.value)
    except Exception:  # noqa: BLE001
        return None


def terminate_process(handle: int, exit_code: int = 1) -> bool:
    """TerminateProcess 直接终止进程（受限令牌路径 kill 的兜底）。不抛。"""
    k32 = _kernel32()
    if k32 is None or not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, exit_code))
    except Exception:  # noqa: BLE001
        return False


def close_process_handle(handle: int | None) -> None:
    """CloseHandle 释放进程句柄（受限令牌路径收尾）。未传/已关 no-op。"""
    if not handle:
        return
    k32 = _kernel32()
    if k32 is not None:
        try:
            k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
