"""MCP (Model Context Protocol) 客户端：接入社区工具服务器。

配置：全局 ~/.skysheep/mcp.json + 项目 <root>/.skysheep/mcp.json（项目覆盖同名项）。
格式兼容 Claude Desktop：

    { "mcpServers": {
        "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
        "fs":    { "url": "http://localhost:8000/mcp", "readonly": true }
    } }

安全：MCP 工具默认 WRITE 级（调用前需用户确认）；配置中标记 "readonly": true
的升级为自动放行。工具命名 mcp__<server>__<tool> 避免与内置工具冲突。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..tools.base import Safety, Tool, ToolContext, ToolError, truncate_output

if TYPE_CHECKING:
    # 仅注解用：mcp SDK 导入约 0.4s，且启动期（splash/服务就绪）完全用不到——
    # 只有真正连接 MCP 服务器时才需要，运行时导入点见 _extract_text / connect
    from mcp import ClientSession
    from mcp.types import CallToolResult

MAX_MCP_OUTPUT_CHARS = 30_000
CONNECT_TIMEOUT_S = 20.0        # 单个服务器的连接上限；超时视为失败而不是无限等待
PREFLIGHT_TIMEOUT_S = 3.0       # 连之前先探一次端口：地址写错时秒回，不用等 SDK 超时
CALL_TIMEOUT_S = 120.0          # 单次工具调用上限：SDK 默认无限等，一个挂死的服务器
                                # 会把整轮对话（含权限门之后的执行栈）停在原地


def _friendly_error(e: Exception) -> str:
    """把底层异常转成用户能看懂的一句话（界面里直接显示这个）。"""
    text = str(e) or e.__class__.__name__
    low = text.lower()
    if "filenotfound" in low or "no such file" in low:
        return f"启动命令不存在：{text}"
    if "connect" in low and ("refused" in low or "error" in low):
        return f"连不上该地址：{text}"
    if "timeout" in low or "timed out" in low:
        return f"连接超时：{text}"
    return text


async def _preflight(cfg: MCPServerConfig) -> str | None:
    """连接前的快速体检：返回错误说明，None 表示可以继续。

    SDK 的 HTTP 客户端在地址不通时可能卡很久（还会走系统代理），所以先自己
    探一次端口、检查一下启动命令是否存在——这两种错误占实际配置错误的绝大多数，
    提前拦掉能让设置页立刻给出「哪里填错了」而不是转圈。
    """
    if cfg.transport == "http":
        parsed = urlparse(cfg.url or "")
        host = parsed.hostname
        if not host:
            return "地址不合法（需要 http:// 主机:端口/路径）：" + str(cfg.url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=PREFLIGHT_TIMEOUT_S
            )
        except TimeoutError:
            return f"连不上 {host}:{port}（{PREFLIGHT_TIMEOUT_S:g} 秒无响应）——检查地址和端口，服务是否已启动"
        except OSError as e:
            return f"连不上 {host}:{port}：{e}"
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return None
    if cfg.transport == "stdio":
        command = cfg.command or ""
        if not shutil.which(command) and not Path(command).exists():
            return f"启动命令不存在：{command}（需要先安装它，或把完整路径填进 command）"
        return None
    return "配置里需要有 command（本地命令）或 url（远程地址）"


class MCPServerConfig(BaseModel):
    command: str | None = None   # stdio 传输：可执行命令
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None       # streamable HTTP 传输
    # 远程 MCP 的鉴权头（如 {"Authorization": "Bearer xxx"}）。很多托管服务
    # （Notion / Linear / GitHub 官方 Remote MCP）用 Bearer 或自定义头做鉴权，
    # 没有这个字段就只能连匿名服务器。stdio 传输忽略它。
    headers: dict[str, str] = Field(default_factory=dict)
    readonly: bool = False       # true=工具自动放行；false=调用前需确认

    @property
    def transport(self) -> str:
        if self.url:
            return "http"
        if self.command:
            return "stdio"
        return "invalid"


def _headers_only(cfg: MCPServerConfig) -> dict[str, str]:
    """过滤出合法的请求头（名与值都必须是非空字符串）。"""
    out: dict[str, str] = {}
    for k, v in (cfg.headers or {}).items():
        if isinstance(k, str) and isinstance(v, str) and k.strip():
            out[k.strip()] = v
    return out


def load_mcp_configs(global_path: Path | None, project_path: Path | None) -> dict[str, MCPServerConfig]:
    """合并全局与项目 MCP 配置；项目级同名服务器覆盖全局。

    注意 ``project_path``：项目级配置是随仓库分发的数据，会在启动阶段直接
    ``subprocess`` 拉起命令，必须先经过 workspace trust（见 security/trust.py）。
    未获信任时调用方应传 ``None`` 跳过它，而不是靠本函数自己判断——信任状态
    需要用户交互（确认弹窗），不适合藏在一个纯读配置的函数里。
    """
    merged: dict[str, MCPServerConfig] = {}
    for path in (global_path, project_path):
        if not path or not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for name, section in (data.get("mcpServers") or {}).items():
            if isinstance(section, dict):
                try:
                    merged[name] = MCPServerConfig(**section)
                except Exception:
                    continue  # 单个坏配置不影响其他
    return merged


def _permissive_model(tool_name: str) -> type[BaseModel]:
    """MCP 工具入参不本地强校验（schema 由服务器提供），宽松透传。"""
    model = create_model(
        "MCPInput_" + tool_name.replace("-", "_").replace(".", "_"),
        __config__=ConfigDict(extra="allow"),
    )
    return model


def _extract_text(result: CallToolResult) -> str:
    from mcp.types import TextContent  # noqa: PLC0415  运行时延迟导入（见 TYPE_CHECKING）

    parts = []
    for item in result.content or []:
        if isinstance(item, TextContent):
            parts.append(item.text)
        else:  # 非文本内容（图片/资源等）退化为 JSON 表示
            dump = getattr(item, "model_dump_json", None)
            parts.append(dump() if callable(dump) else str(item))
    return "\n".join(parts)


# MCP 工具的 description 会原样拼进模型可见的工具列表（系统提示词层）。协议本身
# 不限制长度与内容，而服务器（包括打开项目就自动连接的项目级服务器）可以在这里
# 夹带 prompt injection payload——过长描述会挤占上下文、也更容易藏指令。这里做
# 长度截断与换行归一，属于协议层的通用缓解，不针对某个具体服务器。
MAX_MCP_DESCRIPTION_CHARS = 1000


def _sanitize_description(text: str) -> str:
    """工具描述的展示清理：限长 + 压掉多余空白行。

    只做形状约束（长度、空白），不尝试识别「恶意指令」——那种判断不适合放在按
    长度/格式的过滤里，会既漏又误伤；真正的边界是 connection 的可信来源（见
    workspace trust）。截断会附一句提示，便于用户看出描述不全。
    """
    raw = (text or "").strip()
    if not raw:
        return "(no description)"
    # 连续空行压成一个，避免用大量空行把注入内容推到看不见的位置
    lines = [ln.rstrip() for ln in raw.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if not ln:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(ln)
    text_out = "\n".join(out).strip()
    if len(text_out) > MAX_MCP_DESCRIPTION_CHARS:
        text_out = text_out[:MAX_MCP_DESCRIPTION_CHARS] + " …（描述过长已截断）"
    return text_out


class MCPTool(Tool):
    """一个 MCP 服务器工具在 SkySheep 工具系统中的包装。

    会话不在这里固持：保存 manager + 服务器名，调用时向 manager 取**当前**会话。
    否则服务器崩溃重连后，已经注册进 registry 的工具实例会永远指向那个已死的 session，
    重连再成功也修不好这些工具对象。
    """

    def __init__(self, manager: MCPManager, server_name: str, tool_name: str,
                 description: str, input_schema: dict, readonly: bool) -> None:
        self.name = f"mcp__{server_name}__{tool_name}"
        self.description = _sanitize_description(description)
        self.safety = Safety.READONLY if readonly else Safety.WRITE
        self.args_model = _permissive_model(self.name)
        self._raw_name = tool_name
        self._schema = input_schema or {"type": "object"}
        self._manager = manager
        self._server = server_name

    def to_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self._schema}

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        session = self._manager.session_for(self._server)
        if session is None:
            # 服务器当前不可用（未连上 / 正在重连）：告知实情并触发一次重连预约，
            # 不要在这里同步重连——那是启动路径，会把工具调用卡到连接超时。
            self._manager.request_reconnect(self._server)
            raise ToolError(
                f"MCP 服务器「{self._server}」当前未连接，无法调用 {self._raw_name}。"
                "（正在尝试重连；也可在设置 · MCP 里手动重连）"
            )
        try:
            # SDK 的 call_tool 默认无限等读；挂死的半死服务器会把整轮对话卡在
            # 这里（连取消前的兜底都走不到），所以调用层自己拧表。
            result = await asyncio.wait_for(
                session.call_tool(self._raw_name, arguments=args.model_dump()),
                timeout=CALL_TIMEOUT_S,
            )
        except TimeoutError:
            # 超时后该 session 可能已不可用：标记断线并预约重连，但**不重放本次调用**——
            # 服务器可能已经执行过这条工具（副作用已发生），重放会重复写入。
            self._manager.note_call_failure(self._server)
            raise ToolError(
                f"MCP call timed out after {CALL_TIMEOUT_S:g}s: {self._raw_name}"
            ) from None
        except Exception as e:
            self._manager.note_call_failure(self._server)
            raise ToolError(f"MCP call failed: {e}") from e
        if getattr(result, "is_error", False):
            # 工具层报错（如参数不对）不是连接问题：不当作断线，不触发重连
            raise ToolError(_extract_text(result) or "MCP tool returned an error")
        return truncate_output(_extract_text(result), MAX_MCP_OUTPUT_CHARS)


class MCPServerStatus:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.error: str | None = None
        self.tool_names: list[str] = []
        self.reconnecting = False   # 正在后台重连（前端可显示「重连中」）
        self.restarts = 0           # 已自动重连成功次数（诊断用）


class MCPManager:
    """管理所有 MCP 服务器连接；会话结束时 shutdown()。

    每个服务器一个独立的退出栈：某一个连不上（地址写错、命令不存在）时，
    只关掉它自己的资源，已经连上的服务器不受影响。

    重连：服务器崩溃/超时后不再只能靠用户手动改配置重建。断线事件（调用失败）
    会交给后台按指数退避重连，次数有上限；重连成功后已注册的工具实例照常可用，
    因为工具不固持 session，而是每次向 manager 取当前的那个（见 MCPTool）。
    """

    # 自动重连上限：连到上限就停手，把状态留给用户在设置页手动重连。
    # 不无限重试：配置写错（命令不存在、地址填错）时无限重连只是白耗进程与日志。
    MAX_AUTO_RESTARTS = 3
    RESTART_BASE_DELAY_S = 2.0
    RESTART_MAX_DELAY_S = 20.0

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._configs = servers
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self.statuses: dict[str, MCPServerStatus] = {n: MCPServerStatus(n) for n in servers}
        # 后台重连任务句柄（每台一个）：去重避免同台服务器开出多个重连链
        self._restart_tasks: dict[str, asyncio.Task] = {}
        # 已放弃自动重连的服务器（达到上限 / 配置根本没救），不再重复预约
        self._gave_up: set[str] = set()

    # ---- 会话获取与断线上报（供 MCPTool 调用） ----

    def session_for(self, name: str) -> ClientSession | None:
        """取该服务器当前可用的会话；未连上时返回 None。"""
        if not self.statuses.get(name, MCPServerStatus(name)).connected:
            return None
        return self._sessions.get(name)

    def note_call_failure(self, name: str) -> None:
        """调用失败（超时/连接错）：标记断线并预约后台重连。

        由 MCPTool 在捕获到传输层异常时调用；工具层语义错误（is_error）不走这里。
        """
        status = self.statuses.get(name)
        if status is None:
            return
        status.connected = False
        status.error = status.error or "连接已断开，正在尝试重连"
        self._sessions.pop(name, None)
        stack = self._stacks.pop(name, None)
        if stack is not None:
            # 旧栈要真正关掉：不关会积累子进程与 socket 句柄
            asyncio.ensure_future(self._close_stack(stack))
        self.request_reconnect(name)

    async def _close_stack(self, stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 - 已死的连接关不掉不影响后续重连
            pass

    def request_reconnect(self, name: str) -> None:
        """预约一次后台重连（已有限 / 已达上限 / 已在跑的服务器不重复安排）。"""
        if name not in self._configs or name in self._gave_up:
            return
        existing = self._restart_tasks.get(name)
        if existing is not None and not existing.done():
            return
        status = self.statuses.get(name)
        if status is not None and status.restarts >= self.MAX_AUTO_RESTARTS:
            self._gave_up.add(name)
            status.error = (
                f"已自动重连 {status.restarts} 次仍失败，已停止重试："
                "请在设置 · MCP 里检查配置后手动重连"
            )
            return
        try:
            self._restart_tasks[name] = asyncio.get_running_loop().create_task(
                self._restart_loop(name)
            )
        except RuntimeError:
            pass  # 无事件循环（纯同步/测试环境）：跳过自动重连

    async def _restart_loop(self, name: str) -> None:
        """指数退避重连一台服务器，直到成功或达到次数上限。"""
        status = self.statuses[name]
        cfg = self._configs[name]
        status.reconnecting = True
        try:
            while status.restarts < self.MAX_AUTO_RESTARTS and not status.connected:
                delay = min(
                    self.RESTART_BASE_DELAY_S * (2 ** status.restarts),
                    self.RESTART_MAX_DELAY_S,
                )
                await asyncio.sleep(delay)
                # 返回的工具列表这里不用：注册表由 backend 在配置变更/重连入口重建，
                # 重连的职责只是把连接恢复成可用
                await self._connect_one(name, cfg)
                if status.connected:
                    status.restarts += 1
                    status.error = None
                    status.reconnecting = False
                    # 工具列表可能已变（服务器升级/配置改动）：更新 status 里的名字
                    # 但**不**改已注册的 registry——注册表属于 backend 的职责，
                    # 它会随下次配置变更/重连入口重建；这里只保证连接可用。
                    return
        except asyncio.CancelledError:
            raise
        finally:
            status.reconnecting = False

    async def _connect_one(self, name: str, cfg: MCPServerConfig) -> list[Tool]:
        """连一个服务器并返回它的工具；失败时把错误写进 status 并返回空。"""
        status = self.statuses[name]
        if cfg.transport == "invalid":
            status.error = "config needs either 'command' (stdio) or 'url' (http)"
            return []
        problem = await _preflight(cfg)
        if problem:
            status.error = problem
            return []
        tools: list[Tool] = []
        stack = AsyncExitStack()
        try:
            # mcp SDK 运行时延迟导入：启动期用不到（见 TYPE_CHECKING），首次连接时才加载
            from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
            from mcp.client.stdio import stdio_client  # noqa: PLC0415
            from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

            if cfg.transport == "stdio":
                params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None)
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                # 带鉴权的远程 MCP：把配置里的 headers 注入 HTTP 客户端。
                # streamable_http_client 本身不收 headers（签名只有 url / http_client），
                # 所以要在 httpx.AsyncClient 上带默认头。
                extra_headers = _headers_only(cfg)
                http_client = None
                if extra_headers:
                    import httpx  # 局部导入：无鉴权场景不必碰 httpx

                    http_client = httpx.AsyncClient(headers=extra_headers, timeout=CONNECT_TIMEOUT_S)
                    await stack.enter_async_context(http_client)
                read, write = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listing = await session.list_tools()
        except Exception as e:
            await stack.aclose()  # 半开的连接不能留着
            status.error = _friendly_error(e)
            return []
        self._stacks[name] = stack
        self._sessions[name] = session
        status.connected = True
        status.tool_names = [t.name for t in listing.tools]
        for t in listing.tools:
            tools.append(
                MCPTool(
                    manager=self,
                    server_name=name,
                    tool_name=t.name,
                    description=t.description or "",
                    input_schema=t.input_schema,
                    readonly=cfg.readonly,
                )
            )
        return tools

    async def connect_all(self) -> list[Tool]:
        """并发连接全部服务器，返回已注册的 MCPTool 列表。

        每个服务器单独限时（地址不通时不能让设置页一直转圈），并且并发连：
        串行时每台坏服务器独占最长 preflight+连接上限，几台配置错误的服务器
        会让启动/切换项目白等一分钟；statuses 按名各写各的，无共享状态冲突。
        """
        tools: list[Tool] = []
        if not self._configs:
            return tools

        async def connect_limited(name: str, cfg: MCPServerConfig) -> list[Tool]:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    return await self._connect_one(name, cfg)
            except TimeoutError:
                self.statuses[name].error = (
                    f"连接超时（{CONNECT_TIMEOUT_S:g} 秒无响应）：地址或启动命令可能不对"
                )
            except Exception as e:
                self.statuses[name].error = _friendly_error(e)
            return []

        results = await asyncio.gather(
            *(connect_limited(n, c) for n, c in self._configs.items())
        )
        for part in results:
            tools.extend(part)
        return tools

    async def shutdown(self) -> None:
        # 先停后台重连：它会在 sleep 后重建连接，不先取消会与 shutdown 抢资源
        for task in list(self._restart_tasks.values()):
            if not task.done():
                task.cancel()
        self._restart_tasks.clear()
        for name, stack in list(self._stacks.items()):
            try:
                await stack.aclose()
            except Exception:
                pass
            self.statuses[name].connected = False
        self._stacks.clear()
        self._sessions.clear()
