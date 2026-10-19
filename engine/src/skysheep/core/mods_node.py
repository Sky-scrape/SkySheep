"""Mods Node.js 运行时宿主（P1）。

对标 Claude Code Mods：Mod 跑在 Node.js 进程内，**无沙箱**——可读写文件、
发起网络请求、启动子进程、import 任意 npm 包。这与 P1 之前基于 quickjs 的
沙箱模型是两套完全不同的实现：本模块是新增路径，旧 quickjs 路径保留以便灰度
迁移（见 core/mods.py 的 ModManager 选择逻辑，P2 接入）。

设计：

- 进程模型：一个 Mod 一个长期 Node.js 子进程。stdin/stdout 走 JSON-RPC（行分隔，
  UTF-8）。stderr 直接转发到 Python 日志，便于 Mod 打印 console.log 调试。
- 协议：`{"id":N,"method":"...","params":{...}}` → `{"id":N,"result":{...}}`
  或 `{"id":N,"error":{"message":"..."}}`。请求按 id 配对，异步等待。
- 崩溃处理：读到 EOF / 进程退出时，所有挂起请求立即失败；调用方决定是否重启。
- 关闭：发 shutdown 后等进程退出，超时强杀。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("skysheep.mods_node")

RUNTIME_FILE = Path(__file__).with_name("mods_runtime.mjs")
CALL_TIMEOUT_S = 15.0
LOAD_TIMEOUT_S = 30.0
SHUTDOWN_GRACE_S = 3.0
TOOL_INVOKE_TIMEOUT_S = 120.0  # Mod 工具可能做耗时 IO，单独给一个更宽的顶


class NodeRuntimeError(RuntimeError):
    """Node.js 运行时错误（含启动失败、协议错误、handler 抛错）。"""


def find_node() -> str | None:
    """定位 Node.js 可执行文件。优先 PATH，其次常见安装目录。"""
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    # Windows 兜底：Program Files 下的标准安装
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if not base:
                continue
            p = Path(base) / "nodejs" / "node.exe"
            if p.is_file():
                return str(p)
    return None


def probe_node() -> tuple[bool, str]:
    """探测 Node.js 可用性：返回 (可用, 版本号)。不可用时版本为空串。"""
    exe = find_node()
    if not exe:
        return False, ""
    try:
        out = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5.0,
            creationflags=_no_window_flags(),
        )
        if out.returncode != 0:
            return False, ""
        return True, (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def _no_window_flags() -> int:
    """Windows：不弹控制台窗口。其它平台返回 0。"""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _node_env() -> dict[str, str]:
    """Node 子进程环境：与 run_command / 终端同一份 child_environment()。

    延迟导入防导入环（core 不在导入期拉 tools.shell）；返回新 dict，不改
    os.environ。代理变量与 NODE_EXTRA_CA_CERTS 由 child_environment 按全局
    网络设置注入，Mod 里的 fetch 因此遵守设置页里的配置。
    """
    from ..tools.shell import child_environment  # noqa: PLC0415

    return child_environment()


class NodeRuntime:
    """单个 Mod 的 Node.js 子进程宿主。

    线程模型：整个类对外是异步 API（asyncio），内部用一个后台线程消费 stdout
    并按 id 分发到 asyncio 队列（`loop.call_soon_threadsafe`）。这样 stdin/stdout
    的阻塞读写不会卡住事件循环。
    """

    def __init__(
        self, mod_id: str, entry_path: Path, *, mod_dir: Path, session_id: str = "",
        host_handler: object = None,
    ) -> None:
        self.mod_id = mod_id
        self.entry_path = Path(entry_path)
        self.mod_dir = Path(mod_dir)
        self.session_id = session_id
        # host_handler: async (method: str, params: dict) -> Any | None
        # 处理来自 Node 的反向调用（model.complete / mcp.call / store.* 等）。
        # None 时这些能力全部返回错误。
        self._host_handler = host_handler
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._pending_lock = threading.Lock()
        self._id_counter = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._closed = False
        self._hooks: list[str] = []

    # ---- 生命周期 ----

    async def start(self) -> list[str]:
        """启动 Node 进程并加载 Mod 入口；返回入口注册的事件名列表。"""
        if self._proc is not None:
            raise NodeRuntimeError("运行时已启动")
        node = find_node()
        if not node:
            raise NodeRuntimeError("未找到 Node.js（请安装 Node.js 18+ 并加入 PATH）")
        if not RUNTIME_FILE.is_file():
            raise NodeRuntimeError(f"运行时文件缺失：{RUNTIME_FILE}")
        if not self.entry_path.is_file():
            raise NodeRuntimeError(f"入口文件不存在：{self.entry_path}")

        self._loop = asyncio.get_running_loop()

        try:
            self._proc = subprocess.Popen(
                [node, str(RUNTIME_FILE)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_no_window_flags(),
                cwd=str(self.mod_dir),
                # 子进程环境走与 run_command 同一份口径：剥密钥、按全局出网
                # 设置注入代理与 NODE_EXTRA_CA_CERTS。此前不传 env 时 Node
                # 整体继承 os.environ，代理设置对 Mod 的网络请求不生效。
                env=_node_env(),
            )
        except OSError as e:
            raise NodeRuntimeError(f"启动 Node.js 失败：{e}") from e

        self._reader_thread = threading.Thread(
            target=self._read_stdout, name=f"skymod-{self.mod_id}-stdout", daemon=True
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name=f"skymod-{self.mod_id}-stderr", daemon=True
        )
        self._stderr_thread.start()

        # 加载入口
        try:
            result = await asyncio.wait_for(
                self._request(
                    "load",
                    {
                        "modId": self.mod_id,
                        "modDir": str(self.mod_dir),
                        "entry": str(self.entry_path),
                        "sessionId": self.session_id,
                    },
                ),
                timeout=LOAD_TIMEOUT_S,
            )
        except TimeoutError as e:
            await self.close()
            raise NodeRuntimeError(f"加载 Mod 超时（>{LOAD_TIMEOUT_S:.0f}s）") from e
        except NodeRuntimeError:
            await self.close()
            raise

        self._hooks = list(result.get("hooks") or [])
        return self._hooks

    @property
    def hooks(self) -> list[str]:
        return list(self._hooks)

    async def close(self) -> None:
        """关闭：尽量优雅地让 Node 退出，超时强杀。"""
        if self._closed:
            return
        self._closed = True
        self._stopping = True
        proc = self._proc
        if proc is None:
            return
        # 尝试 shutdown
        if proc.poll() is None:
            try:
                await asyncio.wait_for(
                    self._request("shutdown", {}, check_stopped=False),
                    timeout=SHUTDOWN_GRACE_S,
                )
            except (TimeoutError, NodeRuntimeError):
                pass
        try:
            proc.wait(timeout=SHUTDOWN_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        self._fail_all_pending("运行时已关闭")

    async def invoke_tool(self, name: str, args: dict) -> tuple[str, str]:
        """调用 Mod 在 Node 侧注册的工具 handler，返回 (text, error)。

        handler 是 Node 进程内的函数（不能过 JSON），所以工具「实现」留在
        Node；Python 侧的 Tool 包装只负责转调这里。
        """
        if self._closed:
            return "", "运行时已关闭"
        try:
            result = await asyncio.wait_for(
                self._request("invoke_tool", {"name": name, "args": args}),
                timeout=TOOL_INVOKE_TIMEOUT_S,
            )
        except TimeoutError:
            return "", f"Mod 工具执行超时（>{TOOL_INVOKE_TIMEOUT_S:.0f}s）"
        except NodeRuntimeError as e:
            return "", str(e)
        if not result.get("ok", True):
            return "", str(result.get("error") or "工具执行失败")
        return str(result.get("text") or ""), ""

    async def ping(self) -> bool:
        try:
            await asyncio.wait_for(self._request("ping", {}), timeout=2.0)
            return True
        except (TimeoutError, NodeRuntimeError):
            return False

    # ---- 调用 ----

    async def call(
        self, event: str, payload: dict, state: dict, *, session_id: str = "",
    ) -> tuple[Any, dict, str]:
        """执行一次中间件链，返回 (verdict, new_state, error_message)。

        verdict: handler 链中最后一个非 undefined/null 返回值；无则 None。
        error_message: 非空时表示执行失败（verdict 为 None）。
        """
        if self._closed:
            return None, state, "运行时已关闭"
        try:
            result = await asyncio.wait_for(
                self._request(
                    "call",
                    {
                        "event": event,
                        "payload": payload,
                        "state": state,
                        "sessionId": session_id or self.session_id,
                    },
                ),
                timeout=CALL_TIMEOUT_S,
            )
        except TimeoutError:
            return None, state, f"{event} 执行超时（>{CALL_TIMEOUT_S:.0f}s）"
        except NodeRuntimeError as e:
            return None, state, str(e)
        if not result.get("ok", True):
            return None, state, str(result.get("error") or "handler 抛错")
        new_state = result.get("state")
        if not isinstance(new_state, dict):
            new_state = state
        return result.get("verdict"), new_state, ""

    # ---- 内部：请求/响应 ----

    async def _request(
        self, method: str, params: dict, *, check_stopped: bool = True,
    ) -> dict:
        if check_stopped and self._closed:
            raise NodeRuntimeError("运行时已关闭")
        if self._proc is None or self._proc.poll() is not None:
            raise NodeRuntimeError("Node 进程已退出")
        loop = self._loop or asyncio.get_running_loop()
        req_id = next(self._id_counter)
        fut: asyncio.Future = loop.create_future()
        with self._pending_lock:
            self._pending[req_id] = fut
        msg = json.dumps({"id": req_id, "method": method, "params": params}, ensure_ascii=False)
        try:
            self._proc.stdin.write(msg + "\n")  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError) as e:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise NodeRuntimeError(f"写入 Node stdin 失败：{e}") from e
        try:
            return await fut
        except asyncio.CancelledError:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    logger.warning("[mods:%s] 无法解析 Node 输出：%s", self.mod_id, text[:200])
                    continue
                self._dispatch_response(msg)
        except (OSError, ValueError) as e:
            logger.debug("[mods:%s] 读 stdout 结束：%s", self.mod_id, e)
        finally:
            self._fail_all_pending("Node 进程已退出")

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = line.rstrip()
                if text:
                    logger.info("[mods:%s] %s", self.mod_id, text)
        except (OSError, ValueError):
            pass

    def _dispatch_response(self, msg: dict) -> None:
        # 反向调用：Node 请求 Python 提供引擎能力
        if msg.get("type") == "host":
            req_id = msg.get("id")
            if not isinstance(req_id, int):
                return
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._serve_host_call(req_id, msg)),
            )
            return
        req_id = msg.get("id")
        if not isinstance(req_id, int):
            return
        with self._pending_lock:
            fut = self._pending.pop(req_id, None)
        if fut is None or fut.done():
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if "error" in msg:
            err = msg["error"] or {}
            message = str(err.get("message") or "未知错误")
            loop.call_soon_threadsafe(_set_exception, fut, NodeRuntimeError(message))
        else:
            result = msg.get("result") or {}
            loop.call_soon_threadsafe(_set_result, fut, result)

    async def _serve_host_call(self, req_id: int, msg: dict) -> None:
        """处理来自 Node 的反向调用，把结果写回 stdin。"""
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        result: object = None
        error: str = ""
        try:
            if self._host_handler is None:
                error = f"{method}: 未注入 host_handler"
            else:
                result = await self._host_handler(method, params)  # type: ignore[operator]
        except Exception as e:  # noqa: BLE001 - 任何异常都回给 Node，不炸宿主
            error = f"{type(e).__name__}: {e}"
        payload = {"type": "host_result", "id": req_id}
        if error:
            payload["error"] = error
        else:
            payload["result"] = result
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _fail_all_pending(self, message: str) -> None:
        loop = self._loop
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        if not pending or loop is None or loop.is_closed():
            return
        for fut in pending:
            loop.call_soon_threadsafe(_set_exception, fut, NodeRuntimeError(message))


def _set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
