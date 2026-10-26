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
import ipaddress
import json
import re
import shutil
import socket
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, create_model

from .. import obs
from ..bgtasks import spawn_bg
from ..messages import ImageBlock
from ..network import NetworkConfig, effective_proxy, make_async_httpx2_client
from ..sanitize import (
    EMPTY_DESCRIPTION_PLACEHOLDER,
    MAX_PROMPT_DESCRIPTION_CHARS,
    sanitize_description,
    untrusted_data_wrap,
)
from ..tools.base import Safety, Tool, ToolContext, ToolError, truncate_output
from ..tools.web import _resolve_public_ips

if TYPE_CHECKING:
    # 仅注解用：mcp SDK 导入约 0.4s，且启动期（splash/服务就绪）完全用不到——
    # 只有真正连接 MCP 服务器时才需要，运行时导入点见 _extract_text / connect
    from mcp import ClientSession
    from mcp.types import CallToolResult, ListToolsResult

MAX_MCP_OUTPUT_CHARS = 30_000
CONNECT_TIMEOUT_S = 20.0        # 单个服务器的连接上限；超时视为失败而不是无限等待
PACKAGE_RUNNER_CONNECT_TIMEOUT_S = 180.0  # 下载型命令（npx/uvx）的连接上限（见下）
PREFLIGHT_TIMEOUT_S = 3.0       # 连之前先探一次端口：地址写错时秒回，不用等 SDK 超时
CALL_TIMEOUT_S = 120.0          # 单次工具调用上限：SDK 默认无限等，一个挂死的服务器
                                # 会把整轮对话（含权限门之后的执行栈）停在原地
CLOSE_GRACE_S = 5.0             # 关连接的宽限：keeper 没在期限内退完就取消它

# 首连要现场拉包的启动命令（npx/uvx 一族）：第一次运行要从 npm/pypi 下载整棵
# 依赖树，常规 20 秒连接超时经常不够；超时收割子进程会把 npx 杀在半路，留下
# 装了一半的 _npx 缓存（npm 缓存无原子性也不校验，坏缓存此后每次启动原样重放、
# 秒崩——真实死因 ERR_MODULE_NOT_FOUND 藏在子进程 stderr 里，界面只见 TaskGroup
# 聚合错误）。对这类命令把连接预算放宽到 PACKAGE_RUNNER_CONNECT_TIMEOUT_S，
# 让下载跑完；已缓存的命令一两秒就连上，预算只是上限，不增加正常等待。
PACKAGE_RUNNER_COMMANDS = frozenset({"npx", "uvx", "bunx", "pnpx", "pipx", "uv"})


def _is_package_runner(command: str | None) -> bool:
    """启动命令是否为会现场拉包的运行器（npx/uvx 一族）。

    Windows 上 npx 实际是 npx.cmd，用户也可能填完整路径——按文件名（去扩展名）
    判断，不按整串。
    """
    if not command:
        return False
    base = Path(command).name.strip()
    base = re.sub(r"\.(cmd|bat|exe)$", "", base, flags=re.IGNORECASE)
    return base.lower() in PACKAGE_RUNNER_COMMANDS


def _connect_budget(cfg: MCPServerConfig) -> float:
    """这台服务器的连接预算：下载型命令放宽，其余用常规值。"""
    return (
        PACKAGE_RUNNER_CONNECT_TIMEOUT_S
        if _is_package_runner(cfg.command)
        else CONNECT_TIMEOUT_S
    )


class _MCPPinnedBackend:
    """把远程 MCP 的 TCP 连接固定到已校验的 IP（防 DNS rebinding）。

    与 tools/web.py 的 _PinnedBackend 同语义，但走 httpcore2——MCP SDK 用的
    HTTP 栈是 httpx2/httpcore2（httpx 的 asyncio 分支变体），不是 httpx/httpcore。
    只在建连阶段介入：命中被固定的主机名直接连已校验 IP，否则退回内层 backend
    默认解析（如代理转发场景）。httpx2/httpcore2 惰性导入，模块本身不依赖 mcp。

    Host 头与 TLS SNI 仍由 httpcore2 用 URL 里的原主机名生成，虚拟主机与证书
    校验照常生效——固定的只是「连到哪台机器」。
    """

    def __init__(self, host: str, ips: list[str]) -> None:
        import httpcore2  # noqa: PLC0415  mcp 可选依赖的传递依赖

        self._host = host.lower()
        self._ips = list(ips)
        self._inner = httpcore2.AnyIOBackend()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        import httpcore2  # noqa: PLC0415

        if host.lower() != self._host:
            return await self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address,
                socket_options=socket_options,
            )
        last: Exception | None = None
        for ip in self._ips:
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as e:
                last = e
        if last is not None:
            raise last
        raise httpcore2.ConnectError(f"没有可用的已校验地址: {host}")

    async def connect_unix_socket(self, *args, **kwargs):
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


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
    if "taskgroup" in low and "sub-exception" in low:
        # anyio TaskGroup 的聚合错误：stdio 子进程启动即崩时，真实原因（如
        # ERR_MODULE_NOT_FOUND）在子进程 stderr 里，SDK 只抛这个没有信息量的壳。
        # 最常见成因是 npx 首装被超时打断留下损坏缓存（旧版本超时收紧时遗留）。
        return (
            "子进程启动后立即退出：先确认启动命令能在终端直接运行。"
            "npx/uvx 类服务多半是首次下载被打断留下了损坏缓存——"
            "删掉对应缓存目录再重连（npx：%LOCALAPPDATA%\\npm-cache\\_npx；"
            "uvx：终端跑 uv cache clean）"
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
    problem, _pins = await _preflight_with_pins(cfg)
    return problem


async def _preflight_with_pins(cfg: MCPServerConfig) -> tuple[str | None, list[str]]:
    """同 _preflight，并带回「本次解析校验通过的 IP 批次」给建连钉连复用（M12）。

    返回 ``(错误说明, pinned_ips)``：错误非 None 时 pins 无意义；错误为 None 时
    pins 一定非空——公网目标是 ``_resolve_public_ips`` 校验通过的那批，回环目标
    （本机 MCP 是最常见形态）是 getaddrinfo 解析出的回环批次。``_connect_one``
    把**同一批** IP 钉给 SDK 建连，使「校验/探测」与「SDK 建连」之间不存在
    第二次解析；解析失败在这里已经拒绝，建连侧不再有回退默认解析的 fail-open。
    """
    if cfg.transport == "http":
        parsed = urlparse(cfg.url or "")
        host = parsed.hostname
        if not host:
            return "地址不合法（需要 http:// 主机:端口/路径）：" + str(cfg.url), []
        # 内网防护（审查 P2-10）：preflight 本是一次裸 TCP 探测，此前对任意
        # 目标都放行——口径与 web_fetch 不一致。现在非回环目标必须解析为公网
        # 地址（复用 web_fetch 的同一份校验，含 NAT64/6to4 翻译段）；回环地址
        # 放行——本机 MCP 服务是最常见形态。错误只回可读结论，不带底层异常
        # 细节，避免内网拓扑经 mcp.status 回显到（持令牌的）远端界面。
        #
        # 校验并把解析结果拿在手里：下面的端口探测直接连已校验的 IP，不再按
        # 主机名二次解析——「校验」与「探测」之间不给 DNS rebinding 留窗口
        # （web.py 的 _resolve_public_ips 返回 IP 列表正是给这类复用用的）。
        # 建连阶段把同一批 IP 钉给 SDK（见 _connect_one 的 pin_host / pin_ips 与
        # _MCPPinnedBackend），三次「解析→使用」用的是同一批已校验地址。
        try:
            pinned = await asyncio.to_thread(_resolve_public_ips, host)
        except ToolError as e:
            try:
                infos = await asyncio.to_thread(
                    socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP
                )
                loopback = all(
                    ipaddress.ip_address(info[4][0]).is_loopback for info in infos
                )
                # 回环目标钉到刚解析出的同一批地址（去重保序）
                pinned = list(dict.fromkeys(info[4][0] for info in infos))
            except (OSError, ValueError):
                loopback = False
                pinned = []
            if not loopback:
                return str(e), []
        except OSError:
            return f"无法解析主机 {host}：请检查远程地址是否拼写正确", []
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        writer = None
        for ip in pinned:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout=PREFLIGHT_TIMEOUT_S
                )
                break
            except TimeoutError:
                return (
                    f"连不上 {host}:{port}（{PREFLIGHT_TIMEOUT_S:g} 秒无响应）"
                    "——检查地址和端口，服务是否已启动"
                ), []
            except OSError:
                continue  # 这个已校验地址不通：试下一个（与按主机名连时逐地址尝试同义）
        if writer is None:
            return (
                f"连不上 {host}:{port}（连接被拒绝或网络不可达）"
                "——检查地址与端口是否正确、服务是否已启动"
            ), []
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return None, pinned
    if cfg.transport == "stdio":
        command = cfg.command or ""
        if not shutil.which(command) and not Path(command).exists():
            return f"启动命令不存在：{command}（需要先安装它，或把完整路径填进 command）", []
        return None, []
    return "配置里需要有 command（本地命令）或 url（远程地址）", []


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
    # ---- 供应链溯源（可选，展示用）----
    # 这两项由 SkySheep 在添加/导入时写入 mcp.json：记录「从哪来」与「什么时候装」。
    # 保持可选与宽容：Claude Desktop 等外部工具手编的 mcp.json 没有这两项也能正常
    # 加载；未知字段被 pydantic 默认行为忽略，不会报错。
    installed_from: str = ""   # manual（手动表单）| preset（官方预设）| import（粘贴/文件导入）
    installed_at: str = ""    # ISO 时间串

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
        # 顶层不是 JSON 对象（手编成数组/字符串等合法 JSON）：按空配置处理并
        # 告警，别让 data.get 的 AttributeError 冒出去炸掉启动/重载
        if not isinstance(data, dict):
            warnings.append(f"{label}配置 {path.name} 顶层不是 JSON 对象，已按空配置处理")
            continue
        # mcpServers 段本身不是 JSON 对象（手编成数组/字符串/数字等合法 JSON）：
        # 非空值是真值，``or {}`` 兜不住，直接 .items() 会 AttributeError 炸掉
        # 启动/重载（发现 M14，口径同 installer.load_servers）——按空配置处理
        # 并告警；null/空数组等空值保持既有降级（静默当空配置）
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            if servers:
                warnings.append(
                    f"{label}配置 {path.name} 的 mcpServers 段不是 JSON 对象，已按空配置处理"
                )
            continue
        for name, section in servers.items():
            if not isinstance(section, dict):
                continue
            try:
                merged[name] = MCPServerConfig(**section)
            except Exception as e:  # noqa: BLE001
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
# 夹带 prompt injection payload——过长描述会挤占上下文、也更容易藏指令。长度截断
# 与换行归一属于协议层的通用缓解，不针对某个具体服务器；实现与技能描述共用
# 一份（sanitize.sanitize_description），防止将来发现新绕过时改一漏一。
MAX_MCP_DESCRIPTION_CHARS = MAX_PROMPT_DESCRIPTION_CHARS


def _sanitize_description(text: str) -> str:
    """工具描述的展示清理：限长 + 压掉多余空白行（共享实现见 sanitize 模块）。

    空文本回退为占位符（工具目录里不留空串）；截断会附一句提示，便于用户
    看出描述不全。
    """
    return sanitize_description(text, empty_fallback=EMPTY_DESCRIPTION_PLACEHOLDER)


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
            if self._manager.gave_up(self._server):
                # 已放弃自动重连（连续失败达上限 / 重连门拒绝）：重连链已不存在，
                # 再说「正在尝试重连」只会让用户空等——如实指向设置页手动重连
                raise ToolError(
                    f"MCP 服务器「{self._server}」当前未连接，无法调用 {self._raw_name}。"
                    "（已停止自动重连，请在设置 · MCP 里手动重连）"
                )
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
    if text:
        # 提示注入纵深防御（机械标注层）：MCP 工具的返回内容来自外部服务器
        # （远程服务或第三方包），与 web_fetch / read_document / web_page 同
        # 口径包上「数据不是指令」标注。此前 MCP 结果是唯一裸交付外部文本的
        # 入口——而它恰是最容易引入不可信内容的一条路（打开项目就自动连接的
        # 项目级服务器、随手装的社区包）。
        text = untrusted_data_wrap(text)
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
        reconnect_gate: Callable[[str], bool] | None = None,
        network: NetworkConfig | None = None,
    ) -> None:
        self._configs = dict(servers)
        # 全局出网设置：远程 MCP 的 HTTP 客户端按它决定代理 / 自签 CA，
        # stdio 子进程按它注入代理环境变量。None = 直连且只信系统证书库
        # （旧调用路径与纯测试不必关心全局配置）。
        self.network = network
        # 自动重连前的放行门（backend 注入）：返回 False 的服务器不再自动重连
        # ——项目级服务器在信任跌落后不该按内存里的旧配置被悄悄拉起（审查 P2-5）
        self.reconnect_gate = reconnect_gate
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

    def gave_up(self, name: str) -> bool:
        """该服务器是否已放弃自动重连（重试达上限 / 重连门拒绝）。

        供 MCPTool 组织报错文案：此时再说「正在尝试重连」会误导用户空等
        一次不会发生的重连。
        """
        return name in self._gave_up

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
        if self.reconnect_gate is not None and not self.reconnect_gate(name):
            # 信任/放行门拒绝（审查 P2-5）：不重连、不留重试链，状态交还用户
            status.error = "该项目级服务器的信任已失效，已停止自动重连；重新信任项目后请手动重连"
            self._gave_up.add(name)
            return
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

    async def _connect_with_deadline(self, name: str, cfg: MCPServerConfig) -> list[Tool]:
        """限时连一台服务器：connecting 旗标只亮期间，超时/异常写进 status.error。

        connect_server（单台显式连接）与 connect_all 的并发包装（connect_limited）
        走的都是这一段「旗标 + 连接预算限时 + 失败落 status + finally
        复位」的收尾，此前两处各写一遍，提取在这里收敛。
        """
        status = self.statuses[name]
        status.connecting = True
        budget = _connect_budget(cfg)
        try:
            async with asyncio.timeout(budget):
                return await self._connect_one(name, cfg, connect_timeout=budget)
        except TimeoutError:
            status.error = (
                f"连接超时（{budget:g} 秒无响应）：地址或启动命令可能不对"
                + (
                    "——npx/uvx 首次运行要现场下载依赖，网络慢时先在终端手动跑一次"
                    "启动命令把缓存烧热，再回来重连"
                    if _is_package_runner(cfg.command)
                    else ""
                )
            )
        except Exception as e:  # noqa: BLE001
            status.error = _friendly_error(e)
        finally:
            status.connecting = False
        return []

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
        return await self._connect_with_deadline(name, cfg)

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

    async def _connect_one(
        self, name: str, cfg: MCPServerConfig, connect_timeout: float | None = None
    ) -> list[Tool]:
        """连一个服务器并返回它的工具；失败时把错误写进 status 并返回空。

        connect_timeout 缺省按配置自动取（下载型命令 npx/uvx 放宽，见
        _connect_budget）；keeper 内 initialize/list_tools 的独立超时同源，
        两者一致才不会出现「外层还没到、内层先把 initialize 掐了」的错位。

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
        problem, preflight_pins = await _preflight_with_pins(cfg)
        if problem:
            status.error = problem
            return []
        # 同名旧 keeper 还在（重连竞态）：先关掉，保证一台服务器同时只有一条连接
        if name in self._keepers or name in self._stops:
            await self._close_connection(name)
        ready: asyncio.Event = asyncio.Event()
        outcome: dict = {}
        # 远程 MCP 的 DNS 钉连目标：直接复用 preflight 解析并探测过的那批已校验
        # IP（_preflight_with_pins 带回）。公网目标是 _resolve_public_ips 校验通过
        # 的批次，回环目标是 preflight 解析出的回环批次（本机 MCP 是最常见形态，
        # 放行）。解析失败在 preflight 已拒绝，建连侧不再回退默认解析（M12：
        # fail-open → fail-closed），「校验/探测」与「SDK 建连」之间不存在第二次解析。
        pin_host: str | None = None
        pin_ips: list[str] = []
        if cfg.transport == "http":
            pin_host = urlparse(cfg.url or "").hostname
            if pin_host and preflight_pins:
                pin_ips = list(preflight_pins)

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
                        # 子进程环境与 run_command / 终端面板同一份口径：剥密钥、
                        # 按全局出网设置注入代理与自签 CA。配置里显式给的 env
                        # 优先（用户可以覆盖某台服务器自己的代理）。
                        from ..tools.shell import child_environment  # noqa: PLC0415

                        merged_env = child_environment()
                        merged_env.update(cfg.env or {})
                        params = StdioServerParameters(
                            command=cfg.command, args=cfg.args, env=merged_env or None
                        )
                        read, write = await stack.enter_async_context(stdio_client(params))
                    else:
                        # 远程 MCP：始终自管 httpx2 客户端——一是把配置里的
                        # headers 注入进去（streamable_http_client 本身不收
                        # headers），二是把 TCP 连接钉到已校验的 IP 列表上，
                        # 防 DNS rebinding（与 web_fetch 同一道防线）。
                        #
                        # 此前是「无 headers 且非本地地址」时交给 SDK 自建客户端，
                        # 建连阶段会二次解析 DNS；预检校验过的那次解析与 SDK 这次
                        # 解析之间就是 rebinding 窗口（对抗审查修项 2）。
                        #
                        # 本地地址不走系统代理（与 models/probe.py 的「本地直连」
                        # 约定同口径）：Windows 上 httpx2 默认 trust_env=True 会读
                        # 注册表里的 WinINET 系统代理，系统代理进程没在跑时，发往
                        # 127.0.0.1 的请求会被转给代理然后连不上。
                        extra_headers = _headers_only(cfg)
                        import httpx2  # noqa: PLC0415  mcp 可选依赖的传递依赖
                        from mcp.shared._httpx_utils import (  # noqa: PLC0415
                            MCP_DEFAULT_SSE_READ_TIMEOUT,
                            MCP_DEFAULT_TIMEOUT,
                        )

                        network = self.network or NetworkConfig()
                        http_client = make_async_httpx2_client(
                            network,
                            url=cfg.url,
                            headers=extra_headers or None,
                            timeout=httpx2.Timeout(
                                MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT
                            ),
                        )
                        # M11 口径修正：走代理时目标域名由代理解析（HTTP CONNECT
                        # 与 SOCKS 握手都把主机名原样交给代理端），钉连后端只
                        # 作用于「本机→代理」的 TCP 段，固定不了「代理→目标」——
                        # 代理路径无法钉连，如实留结构化日志，不写「威胁面不存在」。
                        proxied = bool(
                            effective_proxy(network, url=cfg.url)
                        )
                        if proxied:
                            obs.info(
                                "mcp_proxy_egress",
                                "远程 MCP 经代理出网：目标域名由代理解析，DNS 钉连不适用",
                                server=name, host=pin_host or "",
                            )
                        elif pin_host and pin_ips:
                            # 钉连后端：只用 preflight 拿到并探测过的那批已校验 IP。
                            # 安装失败时不再回退默认解析（M12 fail-closed）：公网
                            # 目标退回默认解析正是 _MCPPinnedBackend 要堵的
                            # rebinding 窗口，宁可拒绝建连；回环目标（本机 MCP）
                            # 默认解析仍只会得到回环地址、地址类别不会改变，放行
                            # 并留告警。
                            pin_error = ""
                            try:
                                pool = getattr(
                                    getattr(http_client, "_transport", None),
                                    "_pool", None,
                                )
                                if pool is None or not hasattr(pool, "_network_backend"):
                                    pin_error = "httpx2 私有结构变化，钉连后端无法安装"
                                else:
                                    pool._network_backend = _MCPPinnedBackend(
                                        pin_host, pin_ips
                                    )
                            except Exception as e:  # noqa: BLE001 - 失败按下面口径分流
                                pin_error = str(e)
                            if pin_error:
                                loopback_target = bool(pin_ips) and all(
                                    ipaddress.ip_address(ip).is_loopback
                                    for ip in pin_ips
                                )
                                if loopback_target:
                                    obs.warning(
                                        "mcp_pin_unavailable",
                                        "本机目标的 DNS 钉连安装失败，退回默认解析"
                                        "（地址类别仍是回环，不构成改换）",
                                        server=name, error=pin_error,
                                    )
                                else:
                                    raise RuntimeError(
                                        "无法固定已校验地址（DNS 钉连安装失败），已拒绝"
                                        "连接：退回默认解析会重新打开 DNS rebinding 窗口"
                                    )
                        else:
                            # 防御性告警（不改变行为）：preflight 契约是「错误为 None
                            # 时 pins 必非空」（见 _preflight_with_pins 文档）——公网
                            # 目标 pins 为空在上游就该已报错，走不到这里；能到这里
                            # 说明契约被破坏（隐性 fail-open 回归点），不能静默不装
                            # 钉连。如实记一条告警，维持现行为：按回环目标降级放行
                            # 的口径默认解析建连。
                            obs.warning(
                                "mcp_pin_skipped",
                                "远程 MCP 的 DNS 钉连被跳过：preflight 带回的"
                                "已校验 IP 批次为空（契约上不应发生）",
                                server=name, host=pin_host or "",
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
                    # 连接预算到点返回后，半开的连接与子进程就没人收了。
                    # 预算按服务器取（下载型命令放宽，同调用方外层一致）。
                    budget = connect_timeout or _connect_budget(cfg)
                    async with asyncio.timeout(budget):
                        await session.initialize()
                    async with asyncio.timeout(budget):
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
        return self._wrap_tools(name, cfg, listing)

    def _wrap_tools(self, name: str, cfg: MCPServerConfig, listing: ListToolsResult) -> list[Tool]:
        """把工具清单包成 MCPTool 列表，挂到本服务器的注册表并返回。

        首连（_connect_one）与 tools/list_changed 刷新（_refresh_tool_list）两条
        路径产出的工具必须逐字段一致（readonly 收窄、注解透传、每服务器超时），
        此前两处各写一遍容易改一漏一——权限口径分叉就出在这里。
        """
        tools = [
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
            for t in listing.tools
        ]
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
        except Exception:  # noqa: BLE001
            return  # 列不出来多半是连接也快没了：交给断线重连路径处理
        cfg = self._configs.get(name)
        if cfg is None:
            return
        status.tool_names = [t.name for t in listing.tools]
        self._wrap_tools(name, cfg, listing)
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
            return await self._connect_with_deadline(name, cfg)

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
