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
  之后用 action=read 增量读取、action=kill 结束（树杀）、action=list 列出。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError, truncate_output

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

# 后台进程注册表（进程级共享）：id -> 状态；跨轮读取
_BG: dict[int, dict] = {}
_BG_NEXT_ID = [1]


def _shell_argv(command: str) -> list[str]:
    """把命令字符串包装成固定 shell 的参数列表（不经过 shell=True）。"""
    if IS_WINDOWS:
        return ["cmd.exe", "/d", "/s", "/c", command]
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


def _fg_kill(proc: subprocess.Popen) -> None:
    """前台超时收尾：Windows 树杀（cmd /c 的子进程一并结束），POSIX 直接杀。"""
    try:
        if IS_WINDOWS:
            subprocess.run(  # noqa: S603 - exe 固定为 taskkill
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=5,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_sync(argv: list[str], cwd: str, timeout_s: int):
    """前台执行：输出限量收集，超时树杀。返回 (status, stdout, stderr, returncode)。

    status: ok | timeout。超时也带回已产生的输出（模型才能据此诊断，不盲猜）。
    """
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_acc: dict = {"head": b"", "tail": b"", "capped": False}
    err_acc: dict = {"head": b"", "tail": b"", "capped": False}
    readers = [
        threading.Thread(target=_collect, args=(proc.stdout, out_acc), daemon=True),
        threading.Thread(target=_collect, args=(proc.stderr, err_acc), daemon=True),
    ]
    for t in readers:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _fg_kill(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for t in readers:
        t.join(timeout=5)
    return ("timeout" if timed_out else "ok",
            _acc_text(out_acc), _acc_text(err_acc), proc.returncode)


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
        )
    else:
        proc = subprocess.Popen(
            argv, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,  # 独立进程组，kill 时不会伤及自身
        )
    return proc


def _bg_start(argv: list[str], cwd: str) -> dict:
    proc = _popen_bg(argv, cwd)
    bid = _BG_NEXT_ID[0]
    _BG_NEXT_ID[0] += 1
    buf = {"out": ""}
    threading.Thread(target=_pump, args=(proc.stdout, buf), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, buf), daemon=True).start()
    _BG[bid] = {"proc": proc, "command": argv[-1], "buf": buf, "started": time.time()}
    return {"pid": bid, "os_pid": proc.pid}


def _bg_read(bid: int, clear: bool) -> str:
    st = _BG.get(bid)
    if st is None:
        raise ToolError(f"后台进程 {bid} 不存在或已被清理（用 action=list 查看）")
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


def _bg_kill(bid: int) -> str:
    st = _BG.pop(bid, None)
    if st is None:
        return f"后台进程 {bid} 不存在（可能已结束并被清理）"
    proc = st["proc"]
    if proc.poll() is not None:
        return f"后台进程 {bid} 早已退出（exit code: {proc.returncode}）"
    try:
        if IS_WINDOWS:
            # 树杀：cmd /c 起的子进程要一并结束（与终端面板同款实现）
            subprocess.run(  # noqa: S603 - exe 固定为 taskkill
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=5,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    return f"后台进程 {bid} 已终止（{st['command'][:120]}）"


def _bg_list() -> str:
    if not _BG:
        return "当前没有后台进程记录"
    lines = []
    for bid, st in sorted(_BG.items()):
        code = st["proc"].poll()
        alive = "运行中" if code is None else f"已退出(code {code})"
        lines.append(f"{bid}: {st['command'][:100]} [{alive}]")
    return "\n".join(lines)


def _bg_gc() -> None:
    """清理已退出超过 30 分钟的后台记录（缓冲随记录一起丢弃）。"""
    now = time.time()
    for bid in [b for b, s in _BG.items()
                if s["proc"].poll() is not None and now - s["started"] > 1800]:
        _BG.pop(bid, None)


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
        "高危操作，会先请求用户确认。"
    )
    safety = Safety.DANGEROUS
    args_model = RunCommandArgs

    def arg_text(self, input_dict: dict) -> str:
        # 白名单按命令前缀匹配，语义文本就是命令本身。
        # action=read/kill/list 这类没有命令的调用如果返回空串，会得到一条 pattern 为空的
        # 规则——空串能和任何空命令文本相等，等于把整个工具放行了。改为带上动作名，
        # 粒度就落在「某个动作」上（与鼠标/键盘的动作级白名单同一档）。
        command = str(input_dict.get("command", ""))
        if command.strip():
            return command
        action = str(input_dict.get("action", "") or "run")
        return f"action={action}"

    async def run(self, args: RunCommandArgs, ctx: ToolContext) -> str:
        if args.action == "read":
            return _bg_read(args.id, args.clear)
        if args.action == "kill":
            return _bg_kill(args.id)
        if args.action == "list":
            return _bg_list()
        if not args.command.strip():
            raise ToolError("command 不能为空")
        _bg_gc()
        argv = _shell_argv(args.command)  # 参数列表形式（shell=False），与前台路径一致
        if args.background:
            info = await asyncio.to_thread(_bg_start, argv, str(ctx.working_dir))
            return (
                f"后台进程已启动: id={info['pid']}（系统 PID {info['os_pid']}）\n"
                f"命令: {args.command}\n"
                f"用 run_command(action=\"read\", id={info['pid']}) 查看输出，"
                f"action=\"kill\" 结束。"
            )

        status, out, err, code = await asyncio.to_thread(
            _run_sync, argv, str(ctx.working_dir), args.timeout_s
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
            raise ToolError("\n".join(parts))

        parts = [f"exit code: {code}"]
        if out.strip():
            parts.append("--- stdout ---\n" + out.rstrip())
        if err.strip():
            parts.append("--- stderr ---\n" + err.rstrip())
        return truncate_output("\n".join(parts))
