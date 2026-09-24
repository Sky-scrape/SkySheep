"""MCP (Model Context Protocol) 客户端：接入社区工具服务器。

配置：全局 ~/.skysheep/mcp.json + 项目 <root>/.skysheep/mcp.json（项目覆盖同名项）。
格式兼容 Claude Desktop：

    { "mcpServers": {
        "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
        "fs":    { "url": "http://localhost:8000/mcp", "readonly": true }
    } }

安全：MCP 工具默认 WRITE 级（调用前需用户确认）；配置中标记 "readonly": true
的升级为自动放行。工具命名 mcp__<server>__<tool> 避免与内置工具冲突。
服务器级 readonly 授权还会被工具自身的注解收窄：协议规定每个工具可带
readOnlyHint，凡显式声明 False 的（如 mcp-server-git 的 git_add/git_commit），
即使服务器标了 readonly 也回到逐次确认——注解只收窄、不放宽，缺注解不回收
服务器级授权（避免没有注解的纯只读服务被误伤）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..bgtasks import spawn_bg
from ..messages import ImageBlock
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
CLOSE_GRACE_S = 5.0             # 关连接的宽限：keeper 没在期限内退完就取消它


def _friendly_error(e: Exception) -> str:
    """把底层异常转成用户能看懂的一句话（界面里直接显示这个）。"""
    text = str(e) or e.__class__.__name__
    low = text.lower()
    if "unauthorized" in low or "forbidden" in low or "authentication" in low:
        # 远程服务的 token 多半过期了：这类错误用户自己能修，要说清去哪修
        return (
            "鉴权失败（401/403）：检查配置里 headers 的凭证是否有效"
            "——托管服务的 token 常会过期，更新后重新连接即可"
        )
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
    # 单次工具调用的超时秒数：缺省用全局 CALL_TIMEOUT_S。重索引/深研究类
    # 工具可能合法地跑很久，全局 120 秒会把它们腰斩，这里按服务器放开。
    timeout: float | None = None
    # false=停用：保留配置（env/headers 不用重填）但启动/重连一律跳过
    enabled: bool = True

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


def load_mcp_configs(
    global_path: Path | None, project_path: Path | None
) -> tuple[dict[str, MCPServerConfig], list[str]]:
    """合并全局与项目 MCP 配置；项目级同名服务器覆盖全局。

    返回 ``(configs, warnings)``：解析失败、单个服务定义不合法都会以可读告警
    返回而不是静默吞掉——配置文件坏掉时若只是当它不存在，设置页会显示
    「还没有 MCP 服务」，用户根本不知道自己手改的 JSON 少了个逗号。

    注意 ``project_path``：项目级配置是随仓库分发的数据，会在启动阶段直接
    ``subprocess`` 拉起命令，必须先经过 workspace trust（见 security/trust.py）。
    未获信任时调用方应传 ``None`` 跳过它，而不是靠本函数自己判断——信任状态
    需要用户交互（确认弹窗），不适合藏在一个纯读配置的函数里。
    """
    merged: dict[str, MCPServerConfig] = {}
    warnings: list[str] = []
    for label, path in (("全局", global_path), ("项目", project_path)):
        if not path or not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            warnings.append(f"{label}配置 {path.name} 解析失败（第 {e.lineno} 行）：{e.msg}，已按空配置处理")
            continue
        except OSError as e:
            warnings.append(f"{label}配置 {path.name} 读取失败：{e}")
            continue
        for name, section in (data.get("mcpServers") or {}).items():
            if not isinstance(section, dict):
                continue
            try:
                merged[name] = MCPServerConfig(**section)
            except Exception as e:
                # 单个坏配置不影响其他，但要让用户看得见它被跳过了
                first = str(e).strip().splitlines()[0] if str(e).strip() else str(e)
                warnings.append(f"{label}配置里的服务「{name}」定义不合法，已跳过：{first}")
    return merged, warnings


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


def _effective_readonly(server_readonly: bool, tool_name: str, annotations: object) -> bool:
    """服务器级 readonly 授权 × 工具自身注解，得出这个工具的最终安全级。

    注解只收窄、不放宽（协议自己也警告注解不可信，见 ToolAnnotations 文档）：
    - 服务器没标 readonly → 一律 WRITE，注解说什么都改变不了；
    - 服务器标了 readonly 且工具注解显式 read_only_hint=False → WRITE：
      服务器自己承认会改环境（如 mcp-server-git 的 git_add/git_commit），
      这种「标着只读的服务器带写工具」正是最容易翻车的组合；
    - 注解缺失或 True → 沿用服务器级授权。fetch 这类不声明注解的纯只读
      服务不能因为缺注解就被回收自动放行（否则一次任务十几次调用全在弹窗）。
    """
    if not server_readonly:
        return False
    hint = getattr(annotations, "read_only_hint", None)
    if hint is False:
        return False
    return True


class MCPTool(Tool):
    """一个 MCP 服务器工具在 SkySheep 工具系统中的包装。

    会话不在这里固持：保存 manager + 服务器名，调用时向 manager 取**当前**会话。
    否则服务器崩溃重连后，已经注册进 registry 的工具实例会永远指向那个已死的 session，
    重连再成功也修不好这些工具对象。
    """

    def __init__(self, manager: MCPManager, server_name: str, tool_name: str,
                 description: str, input_schema: dict, readonly: bool,
                 call_timeout: float | None = None,
                 annotations: object | None = None) -> None:
        self.name = f"mcp__{server_name}__{tool_name}"
        self.description = _sanitize_description(description)
        self.safety = Safety.READONLY if readonly else Safety.WRITE
        self.args_model = _permissive_model(self.name)
        self._raw_name = tool_name
        self._schema = input_schema or {"type": "object"}
        self._manager = manager
        self._server = server_name
        # 每服务器可配的单次调用上限；没配用全局默认
        self._call_timeout = call_timeout if call_timeout and call_timeout > 0 else CALL_TIMEOUT_S
        # MCP 四注解：read_only_hint 用收窄后的最终判定（= 本项目权限门的实际
        # 口径）；其余三项透传服务器显式声明的值，未声明落 Tool 基类的保守缺省
        # （可疑其有写、有破坏性）——注解只进目录展示，权限判定看 safety 字段。
        self.read_only_hint = readonly
        for attr, default in (
            ("destructive_hint", True),
            ("idempotent_hint", False),
            ("open_world_hint", True),
        ):
            declared = getattr(annotations, attr, None)
            setattr(self, attr, default if declared is None else bool(declared))

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._schema,
            "annotations": {
                "readOnlyHint": self.read_only_hint,
                "destructiveHint": self.destructive_hint,
                "idempotentHint": self.idempotent_hint,
                "openWorldHint": self.open_world_hint,
            },
        }

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
                timeout=self._call_timeout,
            )
        except TimeoutError:
            # 超时后该 session 可能已不可用：标记断线并预约重连，但**不重放本次调用**——
            # 服务器可能已经执行过这条工具（副作用已发生），重放会重复写入。
            self._manager.note_call_failure(self._server)
            raise ToolError(
                f"MCP call timed out after {self._call_timeout:g}s: {self._raw_name}"
            ) from None
        except Exception as e:
            # JSON-RPC 层的错误（未知工具、服务端参数校验失败）说明连接是好的：
            # 当作工具层报错抛出，不断线、不重连。只有传输层异常才走断线上报。
            from mcp.shared.exceptions import MCPError  # noqa: PLC0415  运行时延迟导入

            if isinstance(e, MCPError):
                raise ToolError(f"MCP call failed: {e.message or e}") from e
            self._manager.note_call_failure(self._server)
            raise ToolError(f"MCP call failed: {e}") from e
        if getattr(result, "is_error", False):
            # 工具层报错（如参数不对）不是连接问题：不当作断线，不触发重连
            raise ToolError(_extract_text(result) or "MCP tool returned an error")
        return _extract_result(result, ctx)


def _extract_result(result: CallToolResult, ctx: ToolContext) -> str:
    """把 MCP 工具结果转成模型可见的文本；图片内容转成 ctx.images 附件。

    MCP 的 ImageContent 自带 base64（无前缀），与 ImageBlock 的约定一致，
    直接挂上即可——agent 循环会把 ctx.images 转成 user 消息，模型就能真看到
    图（截图类服务器的结果不再退化成一坨 JSON）。其余非文本内容仍退化为
    JSON 表示。
    """
    from mcp.types import ImageContent, TextContent  # noqa: PLC0415  运行时延迟导入

    parts: list[str] = []
    images = 0
    for item in result.content or []:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif isinstance(item, ImageContent):
            # SDK 2.x 的字段名是 mime_type（线上名仍为 mimeType）；兼容 1.x 的字面属性
            media = getattr(item, "mime_type", None) or getattr(item, "mimeType", None) or "image/png"
            if not media.startswith("image/"):
                media = "image/png"
            ctx.images.append(ImageBlock(media_type=media, data=item.data))
            images += 1
        else:  # 资源等其余内容：JSON 表示
            dump = getattr(item, "model_dump_json", None)
            parts.append(dump() if callable(dump) else str(item))
    text = "\n".join(parts)
    if images:
        text = (text + "\n" if text else "") + f"（另附加 {images} 张图片）"
    return truncate_output(text, MAX_MCP_OUTPUT_CHARS)


class MCPServerStatus:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.enabled = True         # false=已停用：保留配置但不连接（前端显示「已停用」）
        self.error: str | None = None
        self.tool_names: list[str] = []
        self.reconnecting = False   # 正在后台重连（前端可显示「重连中」）
        self.connecting = False     # 正在建立首连（启动后台连接/手动连接期间）
        self.restarts = 0           # 自动重连成功次数（诊断用）
        self.attempts = 0           # 本轮重连链里已失败的尝试次数（达到上限就停手）


class MCPManager:
    """管理所有 MCP 服务器连接；会话结束时 shutdown()。

    每个服务器一个独立的退出栈：某一个连不上（地址写错、命令不存在）时，
    只关掉它自己的资源，已经连上的服务器不受影响。

    重连：服务器崩溃/超时后不再只能靠用户手动改配置重建。断线事件（调用失败）
    会交给后台按指数退避重连；**连续失败次数**达到上限就停手（配置写错时无限
    重试只是白耗进程与日志），成功一次即清零计数。重连成功后已注册的工具实例
    照常可用，因为工具不固持 session，而是每次向 manager 取当前的那个（见 MCPTool）。

    配置变更不需要整台推倒重来：connect_server / disconnect_server / forget_server
    支持按服务器增删改（backend 对比新旧配置后只动有变化的条目），改一个预设
    不会把其它已连接的远程服务全部断开。
    """

    # 自动重连上限：**连续失败**达到上限就停手，把状态留给用户在设置页手动重连。
    # 成功重连一次即清零：长期会话里偶尔断一次的服务器不该被历史失败次数误伤。
    MAX_AUTO_RESTARTS = 3
    RESTART_BASE_DELAY_S = 2.0
    RESTART_MAX_DELAY_S = 20.0

    def __init__(
        self,
        servers: dict[str, MCPServerConfig],
        on_tools_changed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._configs = dict(servers)
        # 连接生命周期归「keeper 任务」独占（见 _connect_one）：anyio 的流上下文
        # 必须在进入它的那个任务里退出，否则取消会打偏。栈/会话都在 keeper 里，
        # 外部只持有引用；关闭 = 置位 stop 事件，由 keeper 自己展开自己的作用域。
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._keepers: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}
        # 每台服务器当前的工具包装（连接/工具列表变化时更新，backend 据此重建注册表）
        self._tools: dict[str, list[Tool]] = {}
        self.statuses: dict[str, MCPServerStatus] = {n: MCPServerStatus(n) for n in servers}
        for n, cfg in servers.items():
            self.statuses[n].enabled = cfg.enabled
        # 服务器推送 tools/list_changed 通知时的回调（backend 重建注册表用）
        self.on_tools_changed = on_tools_changed
        # 后台重连任务句柄（每台一个）：去重避免同台服务器开出多个重连链
        self._restart_tasks: dict[str, asyncio.Task] = {}
        # 已放弃自动重连的服务器（连续失败达上限），不再重复预约
        self._gave_up: set[str] = set()

    # ---- 会话获取与断线上报（供 MCPTool 调用） ----

    def session_for(self, name: str) -> ClientSession | None:
        """取该服务器当前可用的会话；未连上时返回 None。"""
        if not self.statuses.get(name, MCPServerStatus(name)).connected:
            return None
        return self._sessions.get(name)

    def tools_for(self, name: str) -> list[Tool]:
        """该服务器当前注册的工具包装（连接后才有；工具列表变化时更新）。"""
        return list(self._tools.get(name, []))

    def note_call_failure(self, name: str) -> None:
        """调用失败（超时/连接错）：标记断线、关掉连接并预约后台重连。

        由 MCPTool 在捕获到传输层异常时调用；工具层语义错误（is_error / MCPError）
        不走这里。同步入口：关连接安排到后台做，不阻塞调用方。
        """
        status = self.statuses.get(name)
        if status is None or not status.enabled:
            return
        status.connected = False
        status.error = status.error or "连接已断开，正在尝试重连"
        if name in self._keepers:
            # 旧连接要真正关掉：不关会积累子进程与 socket 句柄
            spawn_bg(self._close_connection(name))
        self.request_reconnect(name)

    async def _close_connection(self, name: str) -> None:
        """关掉一台服务器的连接：通知 keeper 退出，由它在自己任务里展开作用域。

        绝不能从别的任务直接 aclose 栈——anyio 的 cancel scope 被跨任务退出时
        取消会打偏（曾把无辜调用方的 await 点一起取消掉）。宽限期内没退完就
        取消 keeper 本身：取消打进所有者任务是合法路径，作用域由它自己展开。
        """
        stop = self._stops.pop(name, None)
        task = self._keepers.pop(name, None)
        self._sessions.pop(name, None)
        self._stacks.pop(name, None)
        self._tools.pop(name, None)
        if stop is None:
            return
        stop.set()
        if task is None or task.done():
            return
        done, _ = await asyncio.wait({task}, timeout=CLOSE_GRACE_S)
        if not done:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - 关闭路径上任何结局都接受
                pass

    def request_reconnect(self, name: str) -> None:
        """预约一次后台重连（已有限 / 已放弃 / 已停用 / 已在跑的不重复安排）。"""
        if name not in self._configs or name in self._gave_up:
            return
        status = self.statuses.get(name)
        if status is not None and not status.enabled:
            return
        existing = self._restart_tasks.get(name)
        if existing is not None and not existing.done():
            return
        try:
            self._restart_tasks[name] = asyncio.get_running_loop().create_task(
                self._restart_loop(name)
            )
        except RuntimeError:
            pass  # 无事件循环（纯同步/测试环境）：跳过自动重连

    async def _restart_loop(self, name: str) -> None:
        """指数退避重连一台服务器：**连续失败**达上限就停手，成功即清零。"""
        status = self.statuses.get(name)
        cfg = self._configs.get(name)
        if status is None or cfg is None:
            return  # 服务器已被移除（delete/forget）：没什么可重连的
        status.reconnecting = True
        try:
            while status.attempts < self.MAX_AUTO_RESTARTS and not status.connected:
                delay = min(
                    self.RESTART_BASE_DELAY_S * (2 ** status.attempts),
                    self.RESTART_MAX_DELAY_S,
                )
                await asyncio.sleep(delay)
                # 先记账再尝试：失败也计入连续失败次数，否则一台永远起不来的
                # 服务器会以恒定间隔无限重试下去
                status.attempts += 1
                # 返回的工具列表这里不用：注册表由 backend 在配置变更/重连入口重建，
                # 重连的职责只是把连接恢复成可用
                await self._connect_one(name, cfg)
                if status.connected:
                    status.attempts = 0
                    status.restarts += 1
                    status.error = None
                    # 工具列表可能已变（服务器升级/配置改动）：更新 status 里的名字
                    # 但**不**改已注册的 registry——注册表属于 backend 的职责，
                    # 它会随下次配置变更/重连入口重建；这里只保证连接可用。
                    return
            # 连续失败达上限：写清结论并停手，等用户在设置页手动处理
            self._gave_up.add(name)
            status.error = (
                f"已自动重试 {self.MAX_AUTO_RESTARTS} 次仍连不上，已停止重试："
                "请在设置 · MCP 里检查配置后手动重连"
            )
        except asyncio.CancelledError:
            raise
        finally:
            status.reconnecting = False

    # ---- 按服务器的连接管理（backend 配置差量同步用） ----

    async def connect_server(self, name: str, cfg: MCPServerConfig) -> list[Tool]:
        """连接（或按新配置重连）一台服务器，返回它的工具。

        与 _restart_loop 共享状态但先取消已有重连链：手动/配置驱动的连接
        是用户的明确意图，不能让后台链稍后再拿旧配置重连一次。显式发起的
        连接同时清零连续失败计数与放弃标记。
        """
        await self._cancel_restart(name)
        status = self.statuses.setdefault(name, MCPServerStatus(name))
        self._configs[name] = cfg
        status.enabled = cfg.enabled
        status.attempts = 0
        self._gave_up.discard(name)
        if not cfg.enabled:
            await self.disconnect_server(name)
            return []
        status.connecting = True
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                return await self._connect_one(name, cfg)
        except TimeoutError:
            status.error = f"连接超时（{CONNECT_TIMEOUT_S:g} 秒无响应）：地址或启动命令可能不对"
        except Exception as e:
            status.error = _friendly_error(e)
        finally:
            status.connecting = False
        return []

    async def disconnect_server(self, name: str) -> None:
        """断开一台服务器并停掉它的重连链（配置保留）。"""
        await self._cancel_restart(name)
        self._gave_up.discard(name)
        st = self.statuses.get(name)
        if st is not None:
            st.connected = False
            st.attempts = 0
            st.reconnecting = False
        await self._close_connection(name)

    def forget_server(self, name: str) -> None:
        """服务器被删除：断开痕迹全部抹掉（状态条目也移除，前端不再显示）。

        连接的关闭交给调用方先走 disconnect_server（backend 的差量同步就是这个
        顺序）；这里只负责把引用清干净。
        """
        st = self.statuses.pop(name, None)
        self._configs.pop(name, None)
        self._tools.pop(name, None)
        self._sessions.pop(name, None)
        self._stacks.pop(name, None)
        self._stops.pop(name, None)
        self._keepers.pop(name, None)
        self._gave_up.discard(name)
        if st is not None:
            st.connected = False

    async def _cancel_restart(self, name: str) -> None:
        task = self._restart_tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _connect_one(self, name: str, cfg: MCPServerConfig) -> list[Tool]:
        """连一个服务器并返回它的工具；失败时把错误写进 status 并返回空。

        连接的打开/关闭都发生在专门拉起的 keeper 任务里：stdio / streamable HTTP
        的传输上下文基于 anyio，cancel scope 必须在进入它的那个任务里退出，
        否则跨任务关闭时取消会打偏（曾把无辜调用方的 await 点一起取消掉）。
        连上后 keeper 停在 stop 事件上；关闭（_close_connection）只置位事件并
        等它自己收尾，宽限期内没退完才取消 keeper 本身（取消打进所有者任务是
        anyio 认可的展开路径）。
        """
        status = self.statuses[name]
        if not cfg.enabled:
            status.enabled = False
            return []
        if cfg.transport == "invalid":
            status.error = "config needs either 'command' (stdio) or 'url' (http)"
            return []
        problem = await _preflight(cfg)
        if problem:
            status.error = problem
            return []
        # 同名旧 keeper 还在（重连竞态）：先关掉，保证一台服务器同时只有一条连接
        if name in self._keepers or name in self._stops:
            await self._close_connection(name)
        ready: asyncio.Event = asyncio.Event()
        outcome: dict = {}

        async def keeper() -> None:
            # 事件在任务外创建、启动前就在字典里：先捕获再等，避免关闭方先 pop 走
            stop_event = self._stops[name]
            try:
                async with AsyncExitStack() as stack:
                    # mcp SDK 运行时延迟导入：启动期用不到（见 TYPE_CHECKING），首次连接时才加载
                    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
                    from mcp.client.stdio import stdio_client  # noqa: PLC0415
                    from mcp.client.streamable_http import streamable_http_client  # noqa: PLC0415

                    if cfg.transport == "stdio":
                        params = StdioServerParameters(
                            command=cfg.command, args=cfg.args, env=cfg.env or None
                        )
                        read, write = await stack.enter_async_context(stdio_client(params))
                    else:
                        # 带鉴权的远程 MCP：把配置里的 headers 注入 HTTP 客户端。
                        # streamable_http_client 本身不收 headers（签名只有 url / http_client），
                        # 所以要在 httpx.AsyncClient 上带默认头。
                        extra_headers = _headers_only(cfg)
                        http_client = None
                        if extra_headers:
                            import httpx  # 局部导入：无鉴权场景不必碰 httpx

                            http_client = httpx.AsyncClient(
                                headers=extra_headers, timeout=CONNECT_TIMEOUT_S
                            )
                            await stack.enter_async_context(http_client)
                        read, write = await stack.enter_async_context(
                            streamable_http_client(cfg.url, http_client=http_client)
                        )
                    session = await stack.enter_async_context(ClientSession(
                        read, write, message_handler=self._make_message_handler(name),
                    ))
                    # M11：initialize/list_tools 各带独立超时。挂死的服务器会停在这两
                    # 个 await 上，对 stop 事件无响应——不设独立超时的话，调用方
                    # CONNECT_TIMEOUT_S 到点返回后，半开的连接与子进程就没人收了
                    async with asyncio.timeout(CONNECT_TIMEOUT_S):
                        await session.initialize()
                    async with asyncio.timeout(CONNECT_TIMEOUT_S):
                        listing = await session.list_tools()
                    self._stacks[name] = stack
                    outcome["session"] = session
                    outcome["listing"] = listing
                    ready.set()  # 连接可用：调用方返回，keeper 留在栈内常驻
                    await stop_event.wait()  # 常驻：直到断开/替换/关机（栈保持打开）
            except Exception as e:  # noqa: BLE001 - 连接失败转成 status.error
                outcome["error"] = e
            finally:
                ready.set()

        self._stops[name] = asyncio.Event()
        task = asyncio.get_running_loop().create_task(keeper())
        self._keepers[name] = task
        try:
            await ready.wait()
        except BaseException:
            # 调用方超时/取消：只 set stop 事件对卡在 initialize/list_tools 里的
            # keeper 无效（它没在等这个事件）——必须走 _close_connection 把 keeper
            # 收割掉（宽限 → cancel），否则半开的连接与 stdio 子进程会泄漏
            # （安全审查 M11：挂死服务器泄漏 keeper 任务与子进程）
            await self._close_connection(name)
            raise
        if "error" in outcome:
            # keeper 已退出（失败路径）：引用清掉
            self._keepers.pop(name, None)
            self._stops.pop(name, None)
            status.error = _friendly_error(outcome["error"])
            return []
        # 成功路径的 keeper 仍常驻（停在 stop 事件上）：引用保留给 _close_connection
        session = outcome["session"]
        listing = outcome["listing"]
        self._sessions[name] = session
        status.connected = True
        status.enabled = True
        status.tool_names = [t.name for t in listing.tools]
        tools: list[Tool] = []
        for t in listing.tools:
            tools.append(
                MCPTool(
                    manager=self,
                    server_name=name,
                    tool_name=t.name,
                    description=t.description or "",
                    input_schema=t.input_schema,
                    # 服务器级授权被工具自身注解收窄：声明了会改环境（hint=False）
                    # 的工具回到逐次确认，其余沿用服务器级 readonly
                    readonly=_effective_readonly(cfg.readonly, t.name, t.annotations),
                    call_timeout=cfg.timeout,
                    annotations=t.annotations,
                )
            )
        self._tools[name] = tools
        return tools

    def _make_message_handler(self, name: str) -> Callable[[object], Awaitable[None]]:
        """收到服务器 tools/list_changed 通知时刷新工具清单并回调 backend。

        处理器在 SDK 的分发路径里内联执行，不能做重活：只安排一个后台任务。
        回调缺失（纯 manager 自用）时只更新 status 里的工具名。
        """
        async def handler(message: object) -> None:
            from mcp.types import ToolListChangedNotification  # noqa: PLC0415

            if not isinstance(message, ToolListChangedNotification):
                return
            spawn_bg(self._refresh_tool_list(name))

        return handler

    async def _refresh_tool_list(self, name: str) -> None:
        """工具列表变了：重新拉取，换上新的工具包装，并通知 backend 重建注册表。"""
        session = self._sessions.get(name)
        status = self.statuses.get(name)
        if session is None or status is None or not status.connected:
            return
        try:
            listing = await session.list_tools()
        except Exception:
            return  # 列不出来多半是连接也快没了：交给断线重连路径处理
        cfg = self._configs.get(name)
        if cfg is None:
            return
        status.tool_names = [t.name for t in listing.tools]
        tools = [
            MCPTool(
                manager=self,
                server_name=name,
                tool_name=t.name,
                description=t.description or "",
                input_schema=t.input_schema,
                readonly=_effective_readonly(cfg.readonly, t.name, t.annotations),
                call_timeout=cfg.timeout,
                annotations=t.annotations,
            )
            for t in listing.tools
        ]
        self._tools[name] = tools
        if self.on_tools_changed is not None:
            try:
                await self.on_tools_changed(name)
            except Exception:  # noqa: BLE001 - 回调失败不影响连接本身
                pass

    async def connect_all(self) -> list[Tool]:
        """并发连接全部启用的服务器，返回已注册的 MCPTool 列表。

        每个服务器单独限时（地址不通时不能让设置页一直转圈），并且并发连：
        串行时每台坏服务器独占最长 preflight+连接上限，几台配置错误的服务器
        会让启动/切换项目白等一分钟；statuses 按名各写各的，无共享状态冲突。
        停用的服务器不连，但状态条目保留（前端要能显示「已停用」并重新启用）。
        """
        tools: list[Tool] = []
        if not self._configs:
            return tools
        for n, cfg in self._configs.items():
            if not cfg.enabled:
                self.statuses[n].enabled = False

        async def connect_limited(name: str, cfg: MCPServerConfig) -> list[Tool]:
            if not cfg.enabled:
                return []
            self.statuses[name].connecting = True
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    return await self._connect_one(name, cfg)
            except TimeoutError:
                self.statuses[name].error = (
                    f"连接超时（{CONNECT_TIMEOUT_S:g} 秒无响应）：地址或启动命令可能不对"
                )
            except Exception as e:
                self.statuses[name].error = _friendly_error(e)
            finally:
                self.statuses[name].connecting = False
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
        # 再逐台关连接：keeper 在自己任务里展开作用域（见 _close_connection）
        for name in list(self._keepers):
            try:
                await self._close_connection(name)
            except Exception:  # noqa: BLE001 - 关不掉的连接不挡关机
                pass
        for st in self.statuses.values():
            st.connected = False
