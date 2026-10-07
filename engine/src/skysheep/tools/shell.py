"""命令执行工具：run_command。

实现说明：
- 前台用 Popen + 读线程 + 线程池（asyncio.to_thread）而不是 asyncio 子进程，
  因为 Windows 上 prompt_toolkit 需要 Selector 事件循环，而 asyncio 的
  子进程 API 只支持 Proactor 循环；
- argv 列表 + 字面量 shell 可执行文件（Windows: cmd /c，POSIX: bash -c），
  shell=False；命令本身属于产品核心能力，由 Permission Gate 在调用前
  进行人工确认/白名单控制；
- 输出限量收集（头+尾），防止刷屏命令撑爆内存；最终再按上下文截断；
- 超时树杀并返回超时前已产生的输出（模型能据此诊断，不再盲猜）；
- background=true 立即返回进程号，输出由后台读线程持续收入缓冲，
  之后用 action=read 增量读取、action=kill 结束（树杀）、action=list 列出；
  三者都按启动会话做归属过滤，并行会话读/杀不到彼此的进程；
- 子进程纳入 Job Object 进程遏制（一期沙箱化，security/sandbox_win.py，
  仅 Windows、失败自动降级）：命令结束/超时 TerminateJobObject 一次收掉
  整棵树，后台常驻进程引擎退出由 kill-on-close 兜底防孤儿；
  配置 [shell] job_containment 可关。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time

from pydantic import BaseModel, Field

from .. import obs
from ..security.sandbox_win import (
    ContainmentJob,
    apply_ui_restrictions,
    assign_process,
    close_job,
    create_containment_job,
    terminate_job,
)
from .base import Safety, Tool, ToolContext, ToolError, require_working_dir, truncate_output

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
IS_WINDOWS = sys.platform == "win32"
BG_MAX_CHARS = 8_000  # 后台进程输出缓冲上限（字符），保留最近内容
# 前台输出收集上限：头 FG_MAX_BYTES + 尾 FG_TAIL_BYTES（超限继续读、只滚动尾窗，
# 不读管道子进程会被塞满的管道卡死）。此前 capture_output=True 无上限，
# 一条刷屏命令在超时窗口内能吃掉数百 MB 内存。
FG_MAX_BYTES = 4_000_000
FG_TAIL_BYTES = 64_000
_FG_CLIP_MARK = b"\n...\n"

# 后台进程注册表（进程级共享）：id -> 状态；跨轮读取。
# 每条记录带 owner（启动它的会话 id）：read/kill/list 按归属过滤——并行会话
# （或流水线/定时任务）不能读、杀别家起的服务进程。owner 为空串（CLI/旧态）
# 时只对同样为空的调用方可见，语义与「无会话态」一致。
_BG: dict[int, dict] = {}
_BG_NEXT_ID = [1]


# 子进程环境剥离（安全审查 M6）：以环境变量形态配置的 API Key 会随 os.environ
# 整体继承给 run_command 拉起的任何命令——被确认执行一次 `echo %OPENAI_API_KEY%`
# 就能把它读进对话（再由 web_fetch 外发）；「完全访问」档下连确认都没有。
# 这里按变量名里的密钥词剥掉这类变量。只影响 run_command 的子进程，不动本进程
# 自己的 os.environ（config.resolve_api_key 读密钥照常）。
# 已知局限：用户把 env_key 配成不含下列任何词的名字（如 MY_LLM_CRED）时剥不掉；
# 默认约定 <PROVIDER>_API_KEY 与常见服务商变量都在覆盖范围内。
_SECRET_ENV_WORDS = (
    "api_key", "apikey", "secret", "token", "passwd", "password",
    "credential", "private_key", "access_key", "auth_key", "_key",
)
# 名字里带这些词但不是密钥本体的常见变量：保留，别把开发环境弄坏
_SECRET_ENV_ALLOW = frozenset({"git_askpass", "ssh_askpass", "ssh_auth_sock"})


def _is_secret_env_name(name: str) -> bool:
    low = name.lower()
    if low in _SECRET_ENV_ALLOW:
        return False
    return any(w in low for w in _SECRET_ENV_WORDS)


def child_environment() -> dict[str, str]:
    """给引擎拉起的子进程用的环境：剥掉密钥类变量，其余原样继承。

    PATH / SystemRoot / ComSpec / TEMP / HOME / USERPROFILE 这些必须留着，
    否则 cmd.exe 与绝大多数命令直接跑不起来。返回新 dict，不改 os.environ。
    所有「引擎侧拉起子进程」的路径共用这一份口径——run_command 与终端面板
    （TerminalSlot.spawn）都必须走这里，任何一条新命令执行路径也不例外。
    """
    return {k: v for k, v in os.environ.items() if not _is_secret_env_name(k)}


def _child_env() -> dict[str, str]:
    """兼容别名：历史测试与内部调用点引用的旧名字。"""
    return child_environment()


def _windows_exe(name: str) -> str:
    """Windows 系统程序一律用绝对路径调用（安全审查低危项）。

    裸名（cmd.exe / taskkill.exe）走 PATH 与当前目录搜索：工作目录里放一个
    同名 exe 就能被优先执行。系统程序的位置是固定的，没有理由靠搜索。
    """
    root = os.environ.get("SystemRoot") or (chr(67) + ":") + os.sep + "Windows"
    if name.lower() in ("cmd.exe", "cmd"):
        comspec = os.environ.get("ComSpec") or ""
        if comspec and os.path.isfile(comspec):
            return comspec
    full = os.path.join(root, "System32", name)
    return full if os.path.isfile(full) else name  # 找不到就退回裸名（不阻断执行）


def _shell_argv(command: str) -> list[str]:
    """把命令字符串包装成固定 shell 的参数列表（不经过 shell=True）。"""
    if IS_WINDOWS:
        return [_windows_exe("cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/bash", "-c", command]


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(sys.getfilesystemencoding(), errors="replace")


def _collect(pipe, acc: dict) -> None:
    """前台读线程：头 FG_MAX_BYTES 全收，超限后只滚动保留最近 FG_TAIL_BYTES。"""
    try:
        for raw in iter(pipe.readline, b""):
            if len(acc["head"]) < FG_MAX_BYTES:
                acc["head"] += raw
            else:
                acc["capped"] = True
                acc["tail"] = (acc["tail"] + raw)[-FG_TAIL_BYTES:]
    except Exception:  # noqa: BLE001 - 进程被杀时管道关闭，静默收尾
        pass
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def _acc_text(acc: dict) -> str:
    data = acc["head"]
    if acc["capped"]:
        data = data[:FG_MAX_BYTES] + _FG_CLIP_MARK + acc["tail"]
    return _decode(data)


def _contain_job(proc: subprocess.Popen) -> ContainmentJob:
    """把已启动的子进程纳入独立 Job Object（一期沙箱化，security/sandbox_win.py）。

    建作业 → 挂进程 → 配 UI 限制（禁剪贴板读/写）。遏制不是命令执行的
    前置条件：任何一步失败都降级为 no-op（sandbox_win 不抛），命令照常
    运行，权限控制仍由 PermissionGate 把守；降级时返回 inactive 的
    ContainmentJob，reason 带可读原因，并记一条 obs 日志（降级不是静默
    放行：事后要能审计某次执行当时有没有遏制）。
    """
    job = create_containment_job()
    if not job.active:
        obs.warning(
            "job_containment_degraded",
            "进程遏制未生效：命令将在无遏制下运行（权限确认照常）",
            reason=job.reason,
        )
        return job
    proc_handle = getattr(proc, "_handle", None)
    if not proc_handle:
        close_job(job)
        job = ContainmentJob(None, "取不到子进程句柄，遏制未生效")
        obs.warning("job_containment_degraded", "进程遏制未生效：命令将在无遏制下运行",
                    reason=job.reason)
        return job
    if not assign_process(job, proc_handle):
        close_job(job)
        job = ContainmentJob(
            None, "AssignProcessToJobObject 失败（引擎可能在不支持嵌套的作业内），遏制未生效"
        )
        obs.warning("job_containment_degraded", "进程遏制未生效：命令将在无遏制下运行",
                    reason=job.reason)
        return job
    # UI 限制配不上只少一层（无剪贴板限制），进程遏制语义保留，不算降级
    apply_ui_restrictions(job)
    return job


def _tree_kill(proc: subprocess.Popen) -> None:
    """Windows 树杀（cmd /c 起的子进程一并结束，与终端面板同款实现），POSIX 直接杀。

    双层兜底：树杀失败（进程已退出/权限不足等）退回直接杀，再失败就放弃——
    调用方都在收尾路径上，终止失败不能打断主流程。前台超时与后台 kill 共用。
    """
    try:
        if IS_WINDOWS:
            subprocess.run(  # noqa: S603 - exe 固定为 taskkill
                [_windows_exe("taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=5,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_sync(argv: list[str], cwd: str, timeout_s: int, job_containment: bool = True):
    """前台执行：输出限量收集，超时树杀。返回 (status, stdout, stderr, returncode)。

    status: ok | timeout。超时也带回已产生的输出（模型才能据此诊断，不盲猜）。
    返回值第 5 位是遏制降级说明（正常生效为空串），供结果尾部注记。
    job_containment=False（用户在配置里主动关闭）不算降级，说明为空串——
    主动关闭不是故障，不该在每条命令的结果里告警。
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env(),
    )
    # 进程遏制（一期沙箱化）：子进程创建后立即入本次调用的 job（失败自动降级
    # no-op，降级带 obs 日志）
    job = _contain_job(proc) if job_containment else ContainmentJob(None, "")
    out_acc: dict = {"head": b"", "tail": b"", "capped": False}
    err_acc: dict = {"head": b"", "tail": b"", "capped": False}
    readers = [
        threading.Thread(target=_collect, args=(proc.stdout, out_acc), daemon=True),
        threading.Thread(target=_collect, args=(proc.stderr, err_acc), daemon=True),
    ]
    timed_out = False
    try:
        for t in readers:
            t.start()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _tree_kill(proc)  # 超时树杀：cmd /c 的子进程一并结束
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    finally:
        # 遏制收尾必须在 finally：读线程启动失败（线程耗尽）或 wait 抛出罕见
        # 异常时若跳过 terminate/close，泄漏的作业句柄既收不掉残留进程、也会
        # 把 KILL_ON_JOB_CLOSE 钉到引擎退出。terminate 放在读线程 join 之前：
        # 树死干净管道才到 EOF，join 不会被滞留孙进程拖满超时。
        terminate_job(job)
        close_job(job)
    for t in readers:
        if t.is_alive():
            t.join(timeout=5)
    return ("timeout" if timed_out else "ok",
            _acc_text(out_acc), _acc_text(err_acc), proc.returncode,
            "" if (job.active or not job_containment)
            else (job.reason or "进程遏制未生效"))


def _pump(pipe, buf: dict) -> None:
    """后台读线程：持续把管道输出追加进缓冲（环形，保留最近 BG_MAX_CHARS）。"""
    try:
        for raw in iter(pipe.readline, b""):
            merged = (buf["out"] + _decode(raw))[-BG_MAX_CHARS:]
            buf["out"] = merged
    except Exception:  # noqa: BLE001 - 进程被杀时管道关闭，静默收尾
        pass
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def _popen_bg(argv: list[str], cwd: str):
    """启动后台进程（与 _run_sync 同款参数列表形式，shell=False）。"""
    if IS_WINDOWS:
        proc = subprocess.Popen(
            argv, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            env=_child_env(),
        )
    else:
        proc = subprocess.Popen(
            argv, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # 独立进程组，kill 时不会伤及自身
            env=_child_env(),
        )
    return proc


def _bg_start(argv: list[str], cwd: str, owner: str = "",
              job_containment: bool = True) -> dict:
    proc = _popen_bg(argv, cwd)
    # 常驻进程同样入 job（遏制失败自动降级 no-op）：action=kill 时一并终止，
    # 引擎退出由 kill-on-close 兜底（句柄随进程关闭，作业成员全被杀）
    job = _contain_job(proc) if job_containment else ContainmentJob(None, "")
    bid = _BG_NEXT_ID[0]
    _BG_NEXT_ID[0] += 1
    buf = {"out": ""}
    # 先登记再启动读线程：pump 线程 start 失败（线程耗尽）时进程/作业仍留在
    # _BG 里，read/kill/GC 路径都还够得着——先起线程后登记的话，start 一抛
    # 进程就成了读不到也杀不掉的孤儿
    _BG[bid] = {
        "proc": proc, "job": job, "command": argv[-1], "buf": buf,
        "started": time.time(), "owner": owner,
    }
    try:
        threading.Thread(target=_pump, args=(proc.stdout, buf), daemon=True).start()
        threading.Thread(target=_pump, args=(proc.stderr, buf), daemon=True).start()
    except Exception:
        _BG.pop(bid, None)
        _tree_kill(proc)
        terminate_job(job)
        close_job(job)
        raise
    return {"pid": bid, "os_pid": proc.pid,
            "containment_note": "" if (job.active or not job_containment)
            else (job.reason or "进程遏制未生效")}


def _bg_owned(bid: int, st: dict, sid: str) -> bool:
    """归属校验：owner 与调用方任一为空（无会话态）时放行，规则与任务簿一致。"""
    owner = st.get("owner") or ""
    return not owner or not sid or owner == sid


def _bg_read(bid: int, clear: bool, sid: str = "") -> str:
    st = _BG.get(bid)
    if st is None:
        raise ToolError(f"后台进程 {bid} 不存在或已被清理（用 action=list 查看）")
    if not _bg_owned(bid, st, sid):
        raise ToolError(
            f"后台进程 {bid} 由其他会话启动，无法从这里读取"
            "（用启动它的那个会话查看，或改用终端面板）"
        )
    code = st["proc"].poll()
    out = st["buf"]["out"]
    if clear:
        st["buf"]["out"] = ""
    parts = [f"后台进程 {bid}（{st['command'][:120]}）"]
    if code is None:
        parts.append("状态: 仍在运行")
    else:
        parts.append(f"状态: 已退出，exit code: {code}")
    parts.append("--- 输出（最近内容） ---")
    parts.append(out.strip() or "(暂无输出)")
    return truncate_output("\n".join(parts))


def _bg_kill(bid: int, sid: str = "") -> str:
    st = _BG.get(bid, None)
    if st is not None and not _bg_owned(bid, st, sid):
        return (
            f"后台进程 {bid} 由其他会话启动，不能从这里终止"
            "（防止并行任务误杀彼此的进程；用启动它的那个会话操作）"
        )
    st = _BG.pop(bid, None)
    if st is None:
        return f"后台进程 {bid} 不存在（可能已结束并被清理）"
    proc = st["proc"]
    if proc.poll() is not None:
        close_job(st.get("job"))  # 已退出才走到这：顺手释放作业句柄
        return f"后台进程 {bid} 早已退出（exit code: {proc.returncode}）"
    _tree_kill(proc)
    # 遏制收尾：树杀可能漏网的进程由 TerminateJobObject 一次收掉，再关句柄
    terminate_job(st.get("job"))
    close_job(st.get("job"))
    return f"后台进程 {bid} 已终止（{st['command'][:120]}）"


def _bg_list(sid: str = "") -> str:
    mine = {b: st for b, st in _BG.items() if _bg_owned(b, st, sid)}
    if not mine:
        return "当前没有后台进程记录"
    lines = []
    for bid, st in sorted(mine.items()):
        code = st["proc"].poll()
        alive = "运行中" if code is None else f"已退出(code {code})"
        lines.append(f"{bid}: {st['command'][:100]} [{alive}]")
    return "\n".join(lines)


def _bg_gc() -> None:
    """清理已退出超过 30 分钟的后台记录（缓冲与作业句柄随记录一起丢弃）。"""
    now = time.time()
    for bid in [b for b, s in _BG.items()
                if s["proc"].poll() is not None and now - s["started"] > 1800]:
        st = _BG.pop(bid, None)
        if st is not None:
            close_job(st.get("job"))


class RunCommandArgs(BaseModel):
    command: str = Field(default="", description="要执行的命令行（action=run 时必填）")
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=MAX_TIMEOUT_S, description="超时秒数")
    background: bool = Field(
        default=False,
        description="后台运行：立即返回进程 ID，不等待结束；"
        "适合 dev server / 长安装。之后用 action=read 读输出。",
    )
    action: str = Field(
        default="run",
        description=(
            "run=执行新命令（默认）；read=读后台进程输出（id=进程号，可带 clear=true 清空缓冲）；"
            "kill=结束后台进程树（id=进程号）；list=列出在跑的后台进程"
        ),
    )
    id: int = Field(default=0, description="read/kill 动作的后台进程号")
    clear: bool = Field(default=False, description="read 时顺带清空已读缓冲")


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在工作目录执行命令。默认同步执行并返回 stdout/stderr/退出码，长输出截断；"
        "background=true 立即返回进程号不等待（dev server / 长安装 / 交互式程序），"
        "之后用 action=\"read\" 读输出、action=\"kill\" 结束、action=\"list\" 列出。"
        "高危操作，会先请求用户确认。子进程环境里已剥掉密钥类变量（*_API_KEY / "
        "*_TOKEN / *_SECRET 等），需要凭据的命令请走该工具自己的登录态或配置文件。"
    )
    safety = Safety.DANGEROUS
    read_only_hint = False
    destructive_hint = True
    idempotent_hint = False
    open_world_hint = True
    args_model = RunCommandArgs

    def arg_text(self, input_dict: dict) -> str:
        # 白名单按命令前缀匹配，语义文本就是命令本身。
        # action=read/kill/list 这类没有命令的调用如果返回空串，会得到一条 pattern 为空的
        # 规则——空串能和任何空命令文本相等，等于把整个工具放行了。改为带上动作名，
        # 粒度就落在「某个动作」上（与鼠标/键盘的动作级白名单同一档）。
        # kill 再带上进程号（审查 P3-16）：exact 规则按整串相等匹配，不带 id 的
        # "action=kill" 固化一次等于放行「杀本会话任意后台进程」；带上 id 后每次
        # 杀别的进程都要重新确认（与 keyboard 固化当次内容同一处理）。
        # action 优先于 command（审查 P-9）：run() 先按 action 分派，action≠run 时
        # command 根本不会被执行。语义文本若仍返回命令文本，白名单与确认弹窗
        # 展示的是命令、实际执行的却是 read/kill——「确认面板展示的 = 实际执行的」
        # 被打破。所以 action≠run 一律按动作语义生成，忽略 command。
        action = str(input_dict.get("action", "") or "run")
        if action != "run":
            if action == "kill":
                rid = str(input_dict.get("id", 0) or 0)
                if rid:
                    return f"action=kill id={rid}"
            return f"action={action}"
        command = str(input_dict.get("command", ""))
        if command.strip():
            return command
        return "action=run"

    async def run(self, args: RunCommandArgs, ctx: ToolContext) -> str:
        # action 白名单（审查 P-9）：未知 action 若静默落到命令分支，arg_text 给出的
        # 「action=xxx」与实际执行的命令文本会对不上——展示必须等于执行。
        if args.action not in ("run", "read", "kill", "list"):
            raise ToolError(
                f"未知 action: {args.action}（可选 run / read / kill / list）"
            )
        if args.action == "read":
            return _bg_read(args.id, args.clear, ctx.session_id)
        if args.action == "kill":
            return _bg_kill(args.id, ctx.session_id)
        if args.action == "list":
            return _bg_list(ctx.session_id)
        if not args.command.strip():
            raise ToolError("command 不能为空")
        workdir = require_working_dir(ctx)  # 无项目态：没有 cwd 可落，给可读拒绝
        _bg_gc()
        argv = _shell_argv(args.command)  # 参数列表形式（shell=False），与前台路径一致
        if args.background:
            info = await asyncio.to_thread(
                _bg_start, argv, str(workdir), ctx.session_id, ctx.job_containment
            )
            text = (
                f"后台进程已启动: id={info['pid']}（系统 PID {info['os_pid']}）\n"
                f"命令: {args.command}\n"
                f"用 run_command(action=\"read\", id={info['pid']}) 查看输出，"
                f"action=\"kill\" 结束。"
            )
            if info.get("containment_note"):
                # 遏制降级如实呈现（安全审查残留发现）：用户与模型都该知道
                # 本次进程没有进作业笼子，事后也能据此审计
                text += "\n⚠️ 本次进程遏制未生效：" + info["containment_note"]
            return text

        status, out, err, code, containment_note = await asyncio.to_thread(
            _run_sync, argv, str(workdir), args.timeout_s, ctx.job_containment
        )
        if status == "timeout":
            parts = [f"command timed out after {args.timeout_s}s（进程树已终止，以下是超时前的输出）"]
            if out.strip():
                parts.append("--- stdout ---\n" + out.rstrip())
            if err.strip():
                parts.append("--- stderr ---\n" + err.rstrip())
            if not out.strip() and not err.strip():
                parts.append("(没有任何输出——命令可能在等待交互输入，"
                             "考虑用 background=true 后台运行再读输出)")
            if containment_note:
                parts.append("⚠️ 本次进程遏制未生效：" + containment_note)
            raise ToolError("\n".join(parts))

        parts = [f"exit code: {code}"]
        if out.strip():
            parts.append("--- stdout ---\n" + out.rstrip())
        if err.strip():
            parts.append("--- stderr ---\n" + err.rstrip())
        if containment_note:
            parts.append("⚠️ 本次进程遏制未生效：" + containment_note)
        return truncate_output("\n".join(parts))
