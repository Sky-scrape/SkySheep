"""右侧面板：终端标签（常驻 PowerShell 伪终端）与辅助对话。

从 backend.py 按职责注释整段搬入：TerminalSlot / TerminalManager 是
模块级类（backend 经由本模块取用），类方法挂在 TerminalPanelMixin 上，
由 Backend 继承组装。方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...bgtasks import spawn_bg
from ...messages import Message, TextBlock
from ...models.base import ProviderDone, ProviderReasoning, ProviderTextDelta
from ...tools.shell import child_environment
from ._shared import EmitFn


class TerminalSlot:
    """单个终端标签的常驻 PowerShell（ConPTY 伪终端，pywinpty）。

    与旧「命令执行器」的区别：这是真终端——提示符、ANSI 颜色、↑↓ 历史、
    Ctrl+C、交互程序（python / git commit 等）都按真实控制台工作，cd 与
    环境变量跨命令保留。命令由用户在 xterm 界面亲自敲，不经过权限门
    （同旧 term.run 的约定，见 TerminalManager 注释）。
    """

    def __init__(self) -> None:
        self.proc: Any | None = None  # winpty.PtyProcess（仅 Windows 有实现）
        self.pump_task: asyncio.Task | None = None
        self.pump_proc: Any | None = None  # 泵正在读的进程（重启后区别新旧会话）

    def alive(self) -> bool:
        return self.proc is not None and self.proc.isalive()

    def spawn(self, cwd: Path, rows: int, cols: int) -> None:
        """启动常驻 shell（已存活时幂等）；cwd 用调用时刻的项目工作目录。"""
        if self.alive():
            return
        try:
            from winpty import PtyProcess  # noqa: PLC0415  仅 Windows 提供
        except Exception as e:  # ImportError / 底层 DLL 缺失
            raise RuntimeError("终端组件不可用（ConPTY 仅支持 Windows）") from e
        try:
            Path(cwd).mkdir(parents=True, exist_ok=True)
            # 环境剥密钥与 run_command 同一口径（审查 P1-1）：不传 env 的话
            # ConPTY 子进程整体继承 os.environ，终端里 echo 一下就能读走
            # *_API_KEY/*_TOKEN，且输出会广播给所有连接。
            self.proc = PtyProcess.spawn(
                "powershell.exe -NoLogo",
                cwd=str(cwd),
                env=child_environment(),
                dimensions=(max(2, int(rows)), max(10, int(cols))),
            )
        except Exception as e:
            raise RuntimeError(f"无法启动 PowerShell：{e}") from None

    def write(self, data: str) -> None:
        if not self.alive():
            raise RuntimeError("终端进程未运行")
        self.proc.write(data)

    def resize(self, rows: int, cols: int) -> None:
        if self.alive():
            try:
                self.proc.setwinsize(max(2, int(rows)), max(10, int(cols)))
            except Exception:  # noqa: BLE001 - 尺寸超界等：终端照常工作
                pass

    def interrupt(self) -> bool:
        """发 Ctrl+C（sendintr）；进程不在时返回 False。"""
        if not self.alive():
            return False
        try:
            self.proc.sendintr()
        except Exception:  # noqa: BLE001
            return False
        return True

    def kill(self) -> bool:
        """结束 shell；泵任务不取消——它读到 EOF 后自己收尾并广播 term_exit。"""
        proc, self.proc = self.proc, None
        if proc is None:
            return False
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:  # noqa: BLE001 - 进程已退出等：忽略
            pass
        # 别留着旧 PtyProcess 引用：ConPTY 句柄不关，对应的 conhost 宿主
        # 进程就一直活着（每次切项目泄漏一个）。泵收尾时会再清一次。
        self.pump_proc = None
        return True


class TerminalManager:
    """底部终端面板的后端：每个标签一个常驻 PowerShell（ConPTY 伪终端）。

    与 run_command 工具的两点不同：命令由用户亲自输入，不经过权限门；
    输出由每槽的 pump 任务持续读取，经 backend.ws_emitters 广播
    term_data / term_exit（term_id 路由，页面刷新后的新连接照常收到）。
    关闭标签或服务退出时结束对应进程；标签数有上限，防进程开满一圈。
    """

    MAX_TERMINALS = 8
    _ID_LEN = 64

    def __init__(self) -> None:
        self.terms: dict[str, TerminalSlot] = {}

    def _key(self, term_id: str) -> str:
        return (term_id or "default")[: self._ID_LEN]

    def slot(self, term_id: str) -> TerminalSlot:
        key = self._key(term_id)
        slot = self.terms.get(key)
        if slot is None:
            if len(self.terms) >= self.MAX_TERMINALS:
                raise RuntimeError(f"终端标签最多同时开 {self.MAX_TERMINALS} 个，先关掉不用的再新建")
            slot = TerminalSlot()
            self.terms[key] = slot
        return slot

    def peek(self, term_id: str) -> TerminalSlot | None:
        return self.terms.get(self._key(term_id))

    def spawn(self, term_id: str, cwd: Path, rows: int, cols: int, backend: Any) -> dict:
        key = self._key(term_id)
        slot = self.slot(term_id)
        slot.spawn(cwd, rows, cols)
        self._ensure_pump(key, slot, backend)
        return {"spawned": True, "alive": slot.alive()}

    def _ensure_pump(self, key: str, slot: TerminalSlot, backend: Any) -> None:
        # 泵跟随具体那次 spawn 的进程：shell 退出后自动重启时，旧泵可能还没
        # 收尾完（阻塞在读线程里），按 pump_proc 区分新旧，别误跳过新泵
        if (
            slot.pump_task is not None
            and not slot.pump_task.done()
            and slot.pump_proc is slot.proc
        ):
            return
        slot.pump_proc = slot.proc
        slot.pump_task = asyncio.create_task(self._pump(key, slot, backend))

    # 终端输出广播的合并窗口与单窗上限：逐 4KB 分片对每个连接 create_task +
    # JSON 序列化，npm install / 构建这类高频输出每秒几百片会把 CPU 与 WS
    # 帧数打爆，还在发送锁后面挤占对话流式事件；40ms 攒一条肉眼无感
    TERM_MERGE_S = 0.04
    TERM_MERGE_MAX = 262_144

    async def _pump(self, key: str, slot: TerminalSlot, backend: Any) -> None:
        """持续读 PTY 输出并广播；阻塞 recv 放线程池，不堵事件循环。"""
        proc = slot.pump_proc
        buf: dict = {"chunks": [], "size": 0, "timer": None}

        async def broadcast(chunks: list[str]) -> None:
            ev = {"kind": "term_data", "term_id": key, "text": "".join(chunks)}
            for ws_emit in list(backend.ws_emitters):
                try:
                    spawn_bg(ws_emit(ev))
                except RuntimeError:
                    return  # 无事件循环（纯测试环境）：丢弃输出

        async def flush_later() -> None:
            await asyncio.sleep(self.TERM_MERGE_S)
            buf["timer"] = None
            chunks = buf["chunks"]
            if chunks:
                buf["chunks"] = []
                buf["size"] = 0
                await broadcast(chunks)

        while proc is not None and proc.isalive():
            try:
                data = await asyncio.to_thread(proc.read, 4096)
            except Exception:  # EOFError（进程退出）/ 底层异常：收尾广播
                break
            if not data:
                continue
            buf["chunks"].append(data)
            buf["size"] += len(data)
            # 窗口未到先攒着；攒太猛（>256KB）就立刻发，不再等窗口
            if buf["timer"] is None:
                if buf["size"] >= self.TERM_MERGE_MAX:
                    chunks = buf["chunks"]
                    buf["chunks"] = []
                    buf["size"] = 0
                    await broadcast(chunks)
                else:
                    buf["timer"] = asyncio.create_task(flush_later())
        if buf["timer"] is not None:
            buf["timer"].cancel()
            buf["timer"] = None
        if buf["chunks"]:
            await broadcast(buf["chunks"])
            buf["chunks"] = []
        # 释放旧 PtyProcess 的最后一份引用：ConPTY 句柄随 GC 关闭，对应的
        # conhost 宿主进程才能退出（否则每次关标签/切项目泄漏一个 conhost）。
        # 只在泵读的仍是自己那个进程时清（期间 shell 可能已自动重启换新）。
        if slot.pump_proc is proc:
            slot.pump_proc = None
        for ws_emit in list(backend.ws_emitters):
            try:
                spawn_bg(ws_emit({"kind": "term_exit", "term_id": key}))
            except RuntimeError:
                break

    def input(self, term_id: str, cwd: Path, data: str, rows: int, cols: int, backend: Any) -> dict:
        """向标签的 shell 写入按键；shell 已退出时自动重启（pump 一并续上）。"""
        key = self._key(term_id)
        slot = self.slot(term_id)
        if not slot.alive():
            slot.spawn(cwd, rows, cols)
        self._ensure_pump(key, slot, backend)
        slot.write(data)
        return {"ok": True}

    def resize(self, term_id: str, rows: int, cols: int) -> dict:
        slot = self.peek(term_id)
        if slot is not None:
            slot.resize(rows, cols)
        return {"ok": True}

    def stop(self, term_id: str | None = None) -> bool:
        """向前台进程发 Ctrl+C（sendintr）；不带 term_id 时发给所有标签。"""
        if term_id:
            slot = self.peek(term_id)
            return slot.interrupt() if slot else False
        sent = False
        for slot in self.terms.values():
            sent = slot.interrupt() or sent
        return sent

    def close(self, term_id: str) -> bool:
        """关闭标签：结束 shell 并丢弃执行槽。"""
        slot = self.terms.pop(self._key(term_id), None)
        return slot.kill() if slot else False

    def close_all(self) -> None:
        """服务退出 / 切项目：结束全部标签的 shell。"""
        for slot in self.terms.values():
            slot.kill()
        self.terms.clear()


class TerminalPanelMixin:
    """右侧面板：终端 / 辅助对话（方法自 backend.py 逐字搬入）。"""

    # ---- 右侧面板：终端 / 辅助对话 ----

    def term_spawn(self, term_id: str, rows: int, cols: int) -> dict:
        """在当前项目工作目录里开一个常驻 PowerShell 标签。"""
        if self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，终端不可用——先在侧栏「项目」区点 ＋ 添加项目并选择一个文件夹。"
            )
        return self.term.spawn(term_id, self.working_dir, rows, cols, self)

    def term_input(self, term_id: str, data: str, rows: int, cols: int) -> dict:
        """向标签的 shell 写按键（shell 已退出时自动重启）。"""
        if self.working_dir is None:
            raise RuntimeError("当前没有项目，终端不可用——先添加一个项目。")
        return self.term.input(term_id, self.working_dir, data, rows, cols, self)

    def term_resize(self, term_id: str, rows: int, cols: int) -> dict:
        return self.term.resize(term_id, rows, cols)

    def term_stop(self, term_id: str = "") -> dict:
        """向前台进程发 Ctrl+C；不带 term_id 时发给所有标签。"""
        return {"stopped": self.term.stop(term_id or None)}

    def term_close(self, term_id: str) -> dict:
        return {"closed": self.term.close(term_id)}

    AUX_SYSTEM = (
        "你是 SkySheep 侧边面板中的辅助助手，负责回答主对话之外的快速小问题。"
        "保持简短、直接、可操作，不调用任何工具。当前工作目录：{cwd}"
    )
    AUX_HISTORY_CAP = 31  # system + 15 轮问答

    async def chat_aux(self, text: str, emit: EmitFn, local: bool = True) -> dict:
        """辅助对话：独立于主会话的轻量一问一答（不落库、不带工具、内存历史）。

        local=False（局域网/远程调用，审查 P1-3 收口）时使用一次性历史：
        共享的 aux_history 是本机侧栏的面板语义，远端不该借「重复上面的内容」
        类提问读出本机用户问过什么，也不该把自己的问答写进本机面板；
        远端仍可正常提问，只是每次都是无状态的。
        """
        text = (text or "").strip()
        if not text:
            raise RuntimeError("empty text")
        if self.provider is None:
            detail = self.provider_error or "请先在 设置 · 模型服务 里启用一个模型"
            raise RuntimeError(f"模型服务未配置或不可用：{detail}")
        if not local:
            history = [Message.system(
                self.AUX_SYSTEM.format(cwd=str(self.working_dir or "（未选择项目）"))),
                Message.user(text),
            ]
            parts: list[str] = []
            async for pe in self.provider.stream(history, []):
                if isinstance(pe, ProviderTextDelta):
                    parts.append(pe.text)
                    await emit({"kind": "aux_delta", "text": pe.text})
                elif isinstance(pe, ProviderReasoning):
                    await emit({"kind": "aux_thinking", "text": pe.text})
                elif isinstance(pe, ProviderDone):
                    pass
            return {"text": "".join(parts), "stateless": True}
        if not self.aux_history:
            self.aux_history.append(Message.system(
                self.AUX_SYSTEM.format(cwd=str(self.working_dir or "（未选择项目）"))))
        self.aux_history.append(Message.user(text))
        parts: list[str] = []
        think_parts: list[str] = []
        try:
            async for pe in self.provider.stream(list(self.aux_history), []):
                if isinstance(pe, ProviderTextDelta):
                    parts.append(pe.text)
                    await emit({"kind": "aux_delta", "text": pe.text})
                elif isinstance(pe, ProviderReasoning):
                    # 思考模型的推理增量：面板实时灰显，不进历史与回复
                    think_parts.append(pe.text)
                    await emit({"kind": "aux_thinking", "text": pe.text})
                elif isinstance(pe, ProviderDone):
                    pass  # 辅助对话不计入用量统计
        except Exception:
            # 失败的这轮不入历史，避免污染后续上下文
            self.aux_history.pop()
            raise
        reply = "".join(parts)
        self.aux_history.append(Message.assistant([TextBlock(text=reply)]))
        overflow = len(self.aux_history) - self.AUX_HISTORY_CAP
        if overflow > 0:
            del self.aux_history[1 : 1 + overflow]  # system 永远保留在首位
        return {"text": reply}

    def aux_clear(self) -> dict:
        self.aux_history = []
        return {"cleared": True}

    def respond_permission(self, request_id: str, decision: str) -> bool:
        for ag in self._for_each_agent():
            if ag.respond_permission(request_id, decision):
                return True
        return False
