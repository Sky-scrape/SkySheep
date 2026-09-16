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
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, create_model

from ..tools.base import Safety, Tool, ToolContext, ToolError, truncate_output

MAX_MCP_OUTPUT_CHARS = 30_000
CONNECT_TIMEOUT_S = 20.0        # 单个服务器的连接上限；超时视为失败而不是无限等待
PREFLIGHT_TIMEOUT_S = 3.0       # 连之前先探一次端口：地址写错时秒回，不用等 SDK 超时


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
    readonly: bool = False       # true=工具自动放行；false=调用前需确认

    @property
    def transport(self) -> str:
        if self.url:
            return "http"
        if self.command:
            return "stdio"
        return "invalid"


def load_mcp_configs(global_path: Path | None, project_path: Path | None) -> dict[str, MCPServerConfig]:
    """合并全局与项目 MCP 配置；项目级同名服务器覆盖全局。"""
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
    parts = []
    for item in result.content or []:
        if isinstance(item, TextContent):
            parts.append(item.text)
        else:  # 非文本内容（图片/资源等）退化为 JSON 表示
            dump = getattr(item, "model_dump_json", None)
            parts.append(dump() if callable(dump) else str(item))
    return "\n".join(parts)


class MCPTool(Tool):
    """一个 MCP 服务器工具在 SkySheep 工具系统中的包装。"""

    def __init__(self, server_name: str, tool_name: str, description: str,
                 input_schema: dict, session: ClientSession, readonly: bool) -> None:
        self.name = f"mcp__{server_name}__{tool_name}"
        self.description = description or "(no description)"
        self.safety = Safety.READONLY if readonly else Safety.WRITE
        self.args_model = _permissive_model(self.name)
        self._raw_name = tool_name
        self._schema = input_schema or {"type": "object"}
        self._session = session

    def to_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self._schema}

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        try:
            result = await self._session.call_tool(self._raw_name, arguments=args.model_dump())
        except Exception as e:
            raise ToolError(f"MCP call failed: {e}") from e
        if getattr(result, "is_error", False):
            raise ToolError(_extract_text(result) or "MCP tool returned an error")
        return truncate_output(_extract_text(result), MAX_MCP_OUTPUT_CHARS)


class MCPServerStatus:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.error: str | None = None
        self.tool_names: list[str] = []


class MCPManager:
    """管理所有 MCP 服务器连接；会话结束时 shutdown()。

    每个服务器一个独立的退出栈：某一个连不上（地址写错、命令不存在）时，
    只关掉它自己的资源，已经连上的服务器不受影响。
    """

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._configs = servers
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self.statuses: dict[str, MCPServerStatus] = {n: MCPServerStatus(n) for n in servers}

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
            if cfg.transport == "stdio":
                params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None)
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                read, write = await stack.enter_async_context(streamable_http_client(cfg.url))
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
        for t in listing.tools:
            status.tool_names.append(t.name)
            tools.append(
                MCPTool(
                    server_name=name,
                    tool_name=t.name,
                    description=t.description or "",
                    input_schema=t.input_schema,
                    session=session,
                    readonly=cfg.readonly,
                )
            )
        return tools

    async def connect_all(self) -> list[Tool]:
        """连接全部服务器，返回已注册的 MCPTool 列表。

        每个服务器单独限时：地址不通时不能让设置页一直转圈。
        """
        tools: list[Tool] = []
        if not self._configs:
            return tools
        for name, cfg in self._configs.items():
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    tools.extend(await self._connect_one(name, cfg))
            except TimeoutError:
                self.statuses[name].error = (
                    f"连接超时（{CONNECT_TIMEOUT_S:g} 秒无响应）：地址或启动命令可能不对"
                )
            except Exception as e:
                self.statuses[name].error = _friendly_error(e)
        return tools

    async def shutdown(self) -> None:
        for name, stack in list(self._stacks.items()):
            try:
                await stack.aclose()
            except Exception:
                pass
            self.statuses[name].connected = False
        self._stacks.clear()
        self._sessions.clear()
