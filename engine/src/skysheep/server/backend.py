"""ServerBackend：把引擎接线（会话/模型/技能/MCP/子代理/权限）暴露给服务层。

与 CLI 的 ChatApp 共享同一套引擎 API，但不含任何 UI 逻辑——
FastAPI WebSocket 端点消费它并转发事件流。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .. import __version__
from ..bgtasks import spawn_bg
from ..channels import ChannelGate, ChannelManager
from ..config import (
    PRESET_SIGNUP_URLS,
    PRESETS,
    REASONING_EFFORT_LABELS,
    REASONING_EFFORTS,
    ConfigError,
    ProviderConfig,
    SkySheepConfig,
    add_provider_to_config,
    config_path,
    db_path,
    disable_provider_in_config,
    disabled_providers_in_config,
    load_config,
    remove_provider_from_config,
    resolve_api_key,
    resolve_imagegen,
    resolve_speech,
    resolve_websearch,
    restore_provider_in_config,
    set_advanced_settings_in_config,
    set_hooks_in_config,
    set_memory_maintenance_in_config,
    set_memory_map_config,
    set_provider_models_in_config,
    set_subagent_settings_in_config,
    skysheep_home,
    update_config_section,
    update_provider_in_config,
)
from ..core import Agent, build_system_prompt
from ..core.checkpoints import CheckpointConflictError, CheckpointStore
from ..core.context import (
    compact_history,
    estimate_text_tokens,
    estimate_tokens,
    is_compaction_summary,
)
from ..core.estimate import estimate_task, format_range
from ..core.hooks import (
    HookRule,
    HookRunner,
    hooks_from_config,
    load_raw_config,
    recent_hook_runs,
)
from ..core.prompt import (
    MAX_INSTRUCTIONS_CHARS,
    PLAN_MODE_PREFIX,
    load_project_instructions,
    render_instructions_section,
)
from ..core.roundtable import (
    MEMBER_ROLES,
    MemberSpec,
    RoundtableOutcome,
    clip_draft,
    run_roundtable,
    usage_rows,
)
from ..core.subagent import (
    BUILTIN_ROLE_PROMPTS,
    CheckTaskTool,
    SpawnAgentTool,
    TaskManager,
    WaitTaskTool,
)
from ..core.subagent_store import (
    BUILTIN_AGENT_TYPES,
    BUILTIN_DESCRIPTIONS,
    BUILTIN_DISPLAY,
    SubagentDef,
    SubagentDefError,
    SubagentStore,
    validate_subagent_name,
)
from ..core.uptodate import fetch_latest_release as check_latest_release
from ..core.uptodate import is_newer_version
from ..events import (
    AssistantMessage,
    ErrorEvent,
    NoticeEvent,
    QueueUpdated,
    TaskEstimate,
    TurnFinished,
    TurnStarted,
)
from ..mcp import (
    MCPInstallError,
    MCPManager,
    import_servers,
    load_mcp_configs,
    load_servers,
    mcp_config_path,
    normalize_server,
    parse_file,
    parse_snippet,
    pending_stdio_commands,
    preset_by_name,
    presets_public,
    remove_server,
    save_servers,
)
from ..messages import ImageBlock, Message, TextBlock
from ..messages import system_text as history_system_text
from ..models import Provider
from ..models.base import ProviderDone, ProviderReasoning, ProviderTextDelta
from ..models.factory import build_provider
from ..models.probe import probe_context_limit, probe_provider_models
from ..obs import info as obs_info
from ..security import leases
from ..security.gate import RULE_KINDS, HeadlessGate, PermissionGate
from ..security.trust import STATE_PENDING, WorkspaceTrust, list_trusted, revoke_by_path
from ..session import SessionStore
from ..session.store import Project, export_messages_text
from ..skills import SkillLoader
from ..skills.installer import (
    LOCAL_SKILL_SOURCES,
    SkillInstallError,
    install_from_url,
    remove_skill,
    scan_computer_skills,
)
from ..skills.installer import install as install_skill
from ..textio import encode_text, read_text_file, write_text_atomic, write_text_file
from ..tools import (
    COMPUTER_TOOL_NAMES,
    ChangeRecorder,
    Safety,
    ToolRegistry,
    default_tools,
)
from ..tools.memory import (
    MAINTAIN_MIN_GLOBAL_CHARS,
    MAINTAIN_MIN_PROJECT_CHARS,
    MAINTENANCE_BACKUP_KEEP,
    MAX_MEMORY_FILE_CHARS,
    backup_before_maintain,
    build_digest_prompt,
    build_maintain_prompt,
    clean_maintained_text,
    digest_transcript,
    load_maintenance_state,
    maintenance_due,
    memory_path,
    parse_digest,
    parse_memory_entries,
    remember_lines,
    render_memory_section,
    save_maintenance_state,
)
from ..tools.pipeline import PipelineWriteTool, task_node_fields
from ..tools.shell import child_environment
from ..tools.skill import LoadSkillTool

logger = logging.getLogger("skysheep.security")
memory_log = logging.getLogger("skysheep.memory")

EmitFn = Callable[[dict], Awaitable[None]]

# 流式增量事件的合并窗口（秒）与单条合并上限（字符）。
#
# 上游 delta 的粒度由各家 provider 决定，常见是每个 SSE chunk 一个 token 级增量，
# 一轮长回答就是上千条事件——每条都要过 WS 的发送锁、单独 JSON 序列化一帧。
# 终端输出已经做过同类合并（见 TerminalManager.TERM_MERGE_S），对话流此前没有。
# 这里把连续的同类型增量拼成一条：首条立即发出（不动首字延迟），之后按窗口聚批。
STREAM_MERGE_S = 0.04
STREAM_MERGE_MAX_CHARS = 2_000

# 可不经合并直接发出的高频增量事件类型（其余事件一律先冲刷缓冲，保证顺序）。
_MERGEABLE_DELTA_KINDS = ("text_delta", "thinking_delta", "roundtable_member_delta")


class StreamDeltaMerger:
    """把连续的流式增量拼成批量事件，减少 WS 帧数。

    设计要点：
    - **顺序不变**：任何非增量事件（工具调用、权限、用量、轮末…）到达时先冲刷缓冲；
      不同类型增量（text ↔ thinking）互相切换也各自冲刷，不跨界合并。
    - **不增加首字延迟**：缓冲为空时首条增量立即发出，之后才进入窗口聚批。
      首条发出后记下已发长度（``_sent``），flush 只补发之后累积的部分。
    - **收尾必冲刷**：``flush()`` 必须在轮末、异常、取消三条路径上都调到，
      否则最后几十毫秒的增量会丢在前端。
    - ``roundtable_member_delta`` 带 member_index/round，只在同一成员同一轮内合并。
    """

    def __init__(self, emit: EmitFn, *, window_s: float = STREAM_MERGE_S) -> None:
        self._emit = emit
        self._window_s = window_s
        self._pending: dict | None = None
        self._key: tuple | None = None
        self._sent = 0  # 当前缓冲里已经发出去的字符数
        self._last_emit = 0.0
        self._timer: asyncio.Task | None = None

    @staticmethod
    def _delta_key(ev: dict) -> tuple | None:
        kind = ev.get("kind")
        if kind not in _MERGEABLE_DELTA_KINDS:
            return None
        if kind == "roundtable_member_delta":
            return (kind, ev.get("member_index"), ev.get("round"))
        return (kind,)

    def _schedule_flush(self) -> None:
        """预约一次定时冲刷。

        只靠「下一条增量到达时再发」会把尾巴揨住：模型中途停顿（限流、思考、
        长工具前奏）时，缓冲里那几十字符会一直不上屏，用户看到回答卡住。
        定时器把滞留上限固定在 window_s，与合并带来的帧数下降取得平衡。
        """
        if self._timer is not None:
            return
        try:
            self._timer = asyncio.get_running_loop().create_task(self._timed_flush())
        except RuntimeError:
            self._timer = None  # 无事件循环（纯同步调用）：交给下一次 send/flush

    async def _timed_flush(self) -> None:
        try:
            await asyncio.sleep(self._window_s)
        except asyncio.CancelledError:
            return
        # 先清句柄再冲刷：flush 里据此避免取消自己（取消当前任务会抛 CancelledError）
        self._timer = None
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 - 客户端断开不影响后续事件
            pass

    async def send(self, ev: dict) -> None:
        key = self._delta_key(ev)
        if key is None:
            # 非增量事件：先冲刷，再原样发出（工具/权限/轮末事件绝不能等）
            await self.flush()
            await self._emit(ev)
            return

        now = time.monotonic()
        text = ev.get("text", "") or ""
        can_merge = (
            self._pending is not None
            and self._key == key
            and now - self._last_emit < self._window_s
            and len(self._pending.get("text", "")) < STREAM_MERGE_MAX_CHARS
        )
        if can_merge:
            # 窗口内且同类型：只累积（这里无 await，与定时器不会交错）
            self._pending["text"] = self._pending.get("text", "") + text
            return

        # 新一段增量的首条（或超过窗口 / 超过上限）：先冲刷旧缓冲，再立即发这条
        await self.flush()
        self._pending = dict(ev)
        self._pending["text"] = text
        self._key = key
        self._sent = len(text)
        self._last_emit = now
        await self._emit(self._pending)  # 首条立即发：首字延迟与合并前一致
        self._schedule_flush()  # 预约冲刷，避免尾巴被无限期揨住

    async def flush(self) -> None:
        """补发缓冲里尚未发出的增量；无待发内容时是空操作。"""
        timer = self._timer
        if timer is not None:
            self._timer = None
            # 不要取消自己（定时器回调也走 flush）
            if timer is not asyncio.current_task():
                timer.cancel()
        pending = self._pending
        tail = ""
        if pending is not None:
            full = pending.get("text", "") or ""
            tail = full[self._sent:]
        self._pending = None
        self._key = None
        self._sent = 0
        if pending is None or not tail:
            return
        await self._emit({**pending, "text": tail})

    async def aclose(self) -> None:
        await self.flush()

MAX_DIFF_CHARS = 8000

# 内置示例快捷指令：首次启动时由 _seed_builtin_snippets 落成真实记录（设置页可见、
# 可编辑、可删除），之后与用户自建指令无异。删除是持久的——种过一次就不再补
# （ui.json 的 snippets_seeded 标记）；用户把指令删光后，/ 菜单仍用这批内容兜底
# （snippets.list 随响应下发，前端不再另存一份）。
BUILTIN_SNIPPETS = [
    {
        "name": "总结当前项目",
        "content": (
            "请浏览当前项目的结构和关键文件，用中文总结："
            "这个项目是做什么的、怎么运行、代码组织如何。"
        ),
    },
    {
        "name": "总结 PDF 文档",
        "content": (
            "请读取 @文件.pdf，提炼要点成一页速览："
            "核心结论（不超过 5 条）、关键数据、值得注意的风险或限制。"
        ),
    },
    {
        "name": "Excel 转图表报告",
        "content": (
            "请读取 @表格.xlsx，检查数据质量（缺失/重复/格式问题），"
            "清洗后生成一份汇总报告，并挑合适的维度画出趋势/分布图（保存为图片），"
            "告诉我有什么发现。"
        ),
    },
    {
        "name": "调研一个话题",
        "content": (
            "请联网调研「在此填写话题」，把最新进展整理成带信息来源的简报"
            "（重点、时间线、不同观点）。今天是 {{date}}，请优先采用近三个月内的信息来源。"
        ),
    },
    {
        "name": "帮我修报错",
        "content": (
            "我遇到了这个报错：\n\n{{clipboard}}\n\n"
            "请分析原因并给出修复方案；如果是代码问题，直接读文件帮我改好。"
        ),
    },
    {
        "name": "解释这段代码",
        "content": (
            "请解释下面这段代码：先一段话总览它做什么，再按关键步骤说明逻辑，"
            "最后指出潜在问题或可改进点。\n\n{{clipboard}}"
        ),
    },
    {
        "name": "写一份周报",
        "content": (
            "请把下面的工作内容整理成简洁的中文周报："
            "本周完成 / 进行中 / 风险与问题 / 下周计划。\n\n{{clipboard}}"
        ),
    },
]

# 用户自定义局域网令牌的最小长度：默认令牌是 secrets.token_urlsafe(16)（22 字符），
# 这里只挡住明显过弱的自定义值，不强制复杂度（令牌要方便输入与扫码）。
MIN_LAN_TOKEN_CHARS = 12


class TokenThrottle:
    """对令牌验证失败的来源做有界的即时节流。

    目标不是防住分布式爆破（默认令牌 128 bit 随机，本就不怕枚举），而是把
    「同一来源连续试错」变成得不偿失：连续失败达阈值后，该来源的后续请求
    一律立即拒绝（不再做比对），冷却时长指数递增、封顶 10 分钟；验证成功
    （含本机回环直连）即清零。状态只在内存里，重启即清空。不 sleep——
    中间件里阻塞事件循环的代价远大于省下的这一次比对。
    """

    def __init__(
        self, threshold: int = 5, base_delay: float = 30.0, max_delay: float = 600.0
    ) -> None:
        self.threshold = max(1, threshold)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._fails: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, ip: str, now: float | None = None) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        current = time.monotonic() if now is None else now
        if current >= until:
            self._blocked_until.pop(ip, None)
            return False
        return True

    def note_failure(self, ip: str, now: float | None = None) -> float:
        """记录一次失败；触发封锁时返回本次封锁的时长（秒），未触发返回 0。"""
        n = self._fails.get(ip, 0) + 1
        self._fails[ip] = n
        if n < self.threshold:
            return 0.0
        delay = min(self.base_delay * (2 ** (n - self.threshold)), self.max_delay)
        self._blocked_until[ip] = (time.monotonic() if now is None else now) + delay
        return delay

    def note_success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._blocked_until.pop(ip, None)

    def reset_failures(self, *, keep_blocks: bool = True) -> None:
        """清空失败计数；keep_blocks=True 时保留已生效的封锁。

        令牌轮换用（安全审查低危项）：旧实现直接换一个新 TokenThrottle，
        正在被封锁的爆破来源跟着一起解封——换了锁不等于要放人进来，
        已生效的封锁按原冷却时间走完更安全。
        """
        self._fails.clear()
        if not keep_blocks:
            self._blocked_until.clear()


# 令牌验证失败的留痕上限：设置页只需要「最近谁在敲门」，总次数单独计数。
TOKEN_FAILURE_LOG_MAX = 20

# Tailscale 分配的虚拟网段（IPv4 CGNAT 100.64.0.0/10 + 其 IPv6 ULA）。远程访问模式
# 靠它区分「tailnet 里的设备」与「物理局域网里的陌生设备」：后者连 IP 段都进不来。
TAILSCALE_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

# ui.json 的 theme 键值域：auto = 跟随系统（浅色默认落纸墨、深色默认落夜墨）；
# light / dark 是旧版两档值，读取时由前端与首帧注入分别按 paper / night 处理；
# 其余是主题 id——浅色：纸墨 paper、青瓷 celadon、秋柿 kaki；深色：夜墨 night、
# 黛夜 indigo、松烟 pine。与 app.js 的 THEMES 表、app.css 的 [data-theme=…] 段、
# index.html 设置页的主题卡片一一对应，改主题列表要四处同步。
THEME_PREFS = ("auto", "light", "dark", "paper", "celadon", "kaki", "night", "indigo", "pine")
# 「跟随系统」时的落点映射（设置页主题卡下方两个下拉）：浅色/深色各落一个具体主题，
# 与 wintheme.read_ui_theme、前端 themeAutoLight/themeAutoDark、首帧注入共用同一值域。
THEME_AUTO_LIGHT = ("paper", "celadon", "kaki")
THEME_AUTO_DARK = ("night", "indigo", "pine")


def client_origin(client) -> str:
    """把连接来源分成三类：local（本机回环）/ tailscale（tailnet 网段）/ other。

    远程访问（Tailscale）模式的 HTTP 守卫与 WS 验签都以此为准：other 一律拒绝，
    tailscale 必须验令牌，local 免令牌。入参兼容 ws.client 的 (host, port) 元组；
    TestClient 的 host 是 "testclient"，按本机对待（与既有测试约定一致）。
    """
    if not client:
        return "other"
    host = client[0] if isinstance(client, (tuple, list)) else str(client)
    host = str(host).split("%")[0]  # IPv6 zone id（fe80::1%eth0）先去掉
    if not host:
        return "other"
    if host in ("testclient", "localhost"):
        return "local"
    try:
        obj = ipaddress.ip_address(host)
    except ValueError:
        return "other"
    if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
        obj = obj.ipv4_mapped  # ::ffff:127.0.0.1 / ::ffff:100.64.x.x 这类映射地址
    if obj.is_loopback:
        return "local"
    if any(obj in net for net in TAILSCALE_NETS):
        return "tailscale"
    return "other"


def _decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _unified_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="改前", tofile="改后", lineterm=""
        )
    )


@dataclass
class QueuedTurn:
    """Agent 工作期间用户继续发来的消息：本轮结束后自动依次执行。"""

    text: str
    emit: EmitFn
    plan_mode: bool
    fut: asyncio.Future = field(repr=False)
    roundtable: bool = False
    members: list | None = None  # 圆桌成员 [{provider, model}]，None=默认策略
    images: list[ImageBlock] = field(default_factory=list)  # 本轮图片附件（圆桌轮不发给成员，仅随消息保留）
    refs: list[str] = field(default_factory=list)  # 本轮引用的会话 id（& 引用对话）
    compare: bool = False  # 圆桌 A/B 对比模式（不融合）
    debate_rounds: int | None = None  # 本轮辩论修订轮数（None=用配置值）
    chair_answers: bool | None = None  # 本轮主席是否出草稿（None=用配置值）

    def resolve(self, result: dict) -> None:
        if not self.fut.done():
            self.fut.set_result(result)

    def fail(self, e: BaseException) -> None:
        if not self.fut.done():
            self.fut.set_exception(e)


def _strip_image_blocks(history: list[Message]) -> list[Message]:
    """剥掉历史里的图片块（圆桌是纯文本协作）。

    成员模型与主席融合都不该看到图片：非多模态服务拿到图片块会直接报错，
    整张成员卡就废了。图片仍随 user 消息持久化，后续普通轮能正常用到。
    """
    out: list[Message] = []
    for m in history:
        if any(getattr(b, "type", "") == "image" for b in m.content):
            kept = [b for b in m.content if getattr(b, "type", "") != "image"]
            out.append(m.model_copy(update={"content": kept or [TextBlock(text="")]}))
        else:
            out.append(m)
    return out


def _msg_brief(m: Message) -> dict:
    """历史消息的轻量 JSON（前端渲染历史用）：role + 文本 + 图片占位；工具轮略。

    图片只带占位（media_type/seq/index），不带 base64 本体——boot/切会话此前
    把整个会话的历史原图整包下发，截图多的会话一次几十 MB；前端进入视口时
    用 session.image 按需拉取（见 backend.session_image）。
    """
    ordinal = 0
    images: list[dict] = []
    for b in m.content:
        if getattr(b, "type", "") == "image":
            images.append(
                {"media_type": b.media_type, "seq": m.seq,
                 "index": ordinal, "placeholder": True}
            )
            ordinal += 1
    return {
        "role": m.role,
        "text": m.text,
        "seq": m.seq,
        "images": images,
        "roundtable": m.roundtable,
        # 思考型模型的推理内容（前端渲染为可折叠块）；其他角色为空串
        "thinking": "".join(b.text for b in m.content if getattr(b, "type", "") == "thinking"),
        # 思考耗时（毫秒）：折叠标题显示「思考 X 秒」，与正文用时区分开
        "thinking_ms": max(
            (getattr(b, "duration_ms", 0) for b in m.content
             if getattr(b, "type", "") == "thinking"),
            default=0,
        ),
        # 本轮实测耗时与接手时下发的预估区间：前端历史恢复重建「用时」芯片。
        # 旧消息没有这两个字段（都是 0 / None），前端据此不渲染芯片。
        "duration_ms": getattr(m, "duration_ms", 0),
        "estimate": getattr(m, "estimate", None),
    }


def _fmt_export_dur(seconds: float) -> str:
    """导出里的时长文案：45 秒 / 3 分 20 秒 / 1 小时 5 分（与界面 fmtEtaDur 同口径）。"""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {sec} 秒" if sec else f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时 {mm} 分" if mm else f"{h} 小时"


def _export_eta_html(m: Message) -> str:
    """导出 HTML 的用时芯片（无实测耗时的旧消息返回空串，不凭空造记录）。"""
    if not getattr(m, "duration_ms", 0):
        return ""
    est = getattr(m, "estimate", None) or {}
    lo, hi = est.get("min_seconds", 0), est.get("max_seconds", 0)
    text = f"⏱ 用时 {_fmt_export_dur(m.duration_ms / 1000)}"
    if hi:
        text += f" · 预估 {format_range(lo, hi)}"
    title = (
        f' title="预估依据：{_html_escape(str(est.get("basis", "")))}"'
        if est.get("basis") else ""
    )
    return f'<div class="eta"{title}>{_html_escape(text)}</div>'


def _export_thinking_html(m: Message) -> str:
    """导出 HTML 的思考过程折叠块（<details> 无需 JS）；没有推理内容返回空串。"""
    think = m.thinking
    if not think:
        return ""
    ms = m.thinking_ms
    label = "💭 思考过程"
    if ms:
        label += f"（思考 {_fmt_export_dur(ms / 1000)}）"
    return (
        f'<details class="think"><summary>{_html_escape(label)}</summary>'
        f'<div class="think-body">{_html_escape(think)}</div></details>'
    )


def _export_body_text(m: Message) -> str:
    """导出正文：与 to_plain 一致，但省略已单独成段的思考块（避免重复）。

    只对带推理内容的 assistant 消息生效；其余直接走 to_plain。
    """
    if m.role != "assistant" or not m.thinking:
        return m.to_plain()
    kept = m.model_copy(update={
        "content": [b for b in m.content if getattr(b, "type", "") != "thinking"]
    })
    return kept.to_plain()


def _stamp_turn_estimate(new_msgs: list, estimate: dict | None, turn_t0: float = 0.0) -> None:
    """把本轮预估区间与实测耗时盖到轮末助手消息上（原地修改，落库前调用）。

    为什么需要这一步：`Agent.run_turn` 只知道自己花多久（它盖 duration_ms），
    预估区间是 backend 在开工时算的，两边分开。圆桌路径连 run_turn 都不走
    （成员并行 → 主席融合，没有 agent 主循环），它的最终回答也就没人盖耗时，
    刷新后看不到「用时」。所以统一在落库前从后往前找最后一条有正文的助手
    消息：缺耗时的按本轮墙钟补上，预估区间一并盖上；取消/出错轮没有最终回答
    则不盖（前端也不会显示）。
    """
    target = None
    for m in reversed(new_msgs):
        if getattr(m, "role", "") != "assistant":
            continue
        if target is None and (getattr(m, "text", "") or "").strip():
            target = m
        if getattr(m, "duration_ms", 0):
            # 引擎已盖过（普通路径的最终回答）：只需补预估
            if estimate:
                m.estimate = estimate
            return
    if target is None:
        return
    if turn_t0:
        target.duration_ms = int((time.monotonic() - turn_t0) * 1000)
    if estimate:
        target.estimate = estimate


# 无人值守门控：定时任务与 headless run 共用 security.gate.HeadlessGate
CronGate = HeadlessGate


def _normalize_id_list(raw) -> list[str]:
    """把界面传来的允许名单归一成字符串列表。

    界面用 textarea（每行一个）提交，备份/脚本可能传列表；数字 id 是常见笔误，
    而名单比较是字符串相等，不转换会静默失效——这种失败很难排查。
    """
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    if not isinstance(raw, (list, tuple)):
        raw = [raw] if raw is not None else []
    return [str(x).strip() for x in raw if str(x).strip()]


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 会话导出 HTML 模板：自包含单文件（无外部资源），纸墨配色与桌面端一致
EXPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · SkySheep 导出</title>
<style>
  body {{ background: #F5EFDE; color: #1D1A16;
          font: 15px/1.75 "Microsoft YaHei", system-ui, sans-serif; margin: 0; }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 40px 20px 80px; }}
  header {{ border-bottom: 2px solid #1D1A16; padding-bottom: 14px; margin-bottom: 26px; }}
  header h1 {{ font-size: 22px; margin: 0 0 6px; }}
  header .meta {{ color: #6b6459; font-size: 13px; }}
  .msg {{ border: 1.5px solid #c9bfa5; border-radius: 10px; padding: 12px 16px; margin: 12px 0;
          background: #fffdf6; white-space: pre-wrap; overflow-wrap: break-word;
          box-shadow: 0 1px 3px rgba(29,26,22,.08); }}
  .msg.user {{ background: #E8DFC7; border-color: #1D1A16; margin-left: 48px; }}
  .msg.ai {{ margin-right: 48px; }}
  .msg.tool {{ background: #faf7ee; color: #57503f; font-size: 13px; }}
  .msg.tool.err {{ border-color: #b03a2e; }}
  .msg pre {{ margin: 6px 0 0; white-space: pre-wrap; font: 12px/1.6 Consolas, monospace; }}
  .msg .body {{ white-space: pre-wrap; overflow-wrap: break-word; }}
  .eta {{ display: inline-block; margin: 0 0 8px; padding: 2px 9px; font-size: 12px;
          color: #6b6459; border: 1px solid #c9bfa5; border-radius: 999px; }}
  .think {{ margin: 0 0 8px; padding: 6px 10px; background: #faf7ee;
            border: 1px dashed #c9bfa5; border-radius: 8px; }}
  .think summary {{ cursor: pointer; font-size: 12px; color: #6b6459; }}
  .think-body {{ margin-top: 6px; font-size: 13px; color: #57503f;
                 white-space: pre-wrap; overflow-wrap: break-word; }}
</style>
</head>
<body>
<div class="wrap">
<header><h1>{title}</h1><div class="meta">由 SkySheep v{version} 导出于 {date} · 纯本地会话记录</div></header>
{body}
</div>
</body>
</html>
"""


@dataclass
class SessionRuntime:
    """一个会话的运行时状态：独立的 Agent、文件改动记录器、消息队列与运行任务。

    多会话并行的基础：每个会话的 turn 在自己的 runtime 里跑，互不占用；
    事件发出时带 session_id，前端按标签路由。
    """

    sid: str
    agent: Agent
    recorder: ChangeRecorder
    queue: list = field(default_factory=list)
    run_task: asyncio.Task | None = None


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


# 渠道预授权名单里出现这些工具时告警：它们在无人值守渠道里等于「任意命令 / 任意写」
_CHANNEL_DANGEROUS_TOOLS = (
    "run_command", "write_file", "edit_file", "delete_file", "move_file",
    "make_dir", "write_document", "generate_image",
)


def _channel_allowed_tools_warning(allowed) -> str:
    """渠道预授权名单里含写/执行类工具时返回告警文案，否则空串。"""
    names = {str(t).strip() for t in (allowed or []) if str(t).strip()}
    hit = sorted(names & set(_CHANNEL_DANGEROUS_TOOLS))
    if not hit:
        return ""
    return (
        "注意：" + "、".join(hit) + " 是写/执行类工具，加入预授权名单后，"
        "渠道会话在无人值守时也会直接执行、不再逐次确认。"
        "只在你完全信任该渠道的允许名单成员时这样做。"
    )


class ServerBackend:
    def __init__(
        self,
        working_dir: str | Path = ".",
        provider_name: str | None = None,
        provider_factory: Callable[[], Provider] | None = None,
        store: SessionStore | None = None,
    ) -> None:
        # 启动目录只作首启建项目用；真正的工作目录在 setup/_bind_project 里
        # 按记住的当前项目确定（无项目态是合法状态，working_dir 可为 None）
        self._startup_dir: Path | None = Path(working_dir).resolve()
        self.working_dir: Path | None = self._startup_dir
        self.provider_name_arg = provider_name
        self._provider_factory_override = provider_factory
        self._store_override = store
        self.cfg: SkySheepConfig | None = None
        self.store: SessionStore | None = None
        self.project = None
        self._remote_project_id_cache: int | None = None
        self.session = None
        self.provider: Provider | None = None
        # agent/queue/_run_task/_recorder 是 property（指向活动会话 runtime）；
        # 基底实例供无会话时兑底与全局刷新，runtimes 在 __init__ 尾部创建
        self._base_agent: Agent | None = None
        self._base_queue: list = []
        self._base_run_task: asyncio.Task | None = None
        self._base_recorder: ChangeRecorder | None = None
        # workspace trust 懒建：拿到 working_dir 后才能算指纹
        self._trust: WorkspaceTrust | None = None
        # 会话运行时池（OrderedDict：访问即移到末尾，最旧的在前面淘汰）
        self.runtimes: OrderedDict[str, SessionRuntime] = OrderedDict()
        self.gate: PermissionGate | None = None
        self.mcp: MCPManager | None = None
        self.mcp_configs: dict = {}
        self.mcp_tools: list = []
        self._mcp_connect_task: asyncio.Task | None = None  # 启动期的后台连接
        self.skills: SkillLoader | None = None
        self.tasks: TaskManager | None = None
        self.subagent_store = SubagentStore(skysheep_home() / "subagents.json")
        self.instructions_file: str | None = None
        self.instructions_text: str = ""
        self.mcp_warnings: list[str] = []
        self.checkpoints = CheckpointStore()
        self.hooks: HookRunner | None = None
        self.term = TerminalManager()
        self.aux_history: list[Message] = []
        self.ws_emitters: list = []  # 在线 WS 连接的 emit 函数（提醒/日程广播用）
        self._reminder_task: asyncio.Task | None = None
        self._cron_task: asyncio.Task | None = None
        self._cron_running: set[int] = set()  # 正在跑的定时任务 id（防重复触发）
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_running: set[int] = set()  # 正在跑的编排节点 id（并发约束）
        self._pipeline_node_tasks: dict[int, asyncio.Task] = {}  # 节点句柄（停止流水线用）
        # 会话续跑节点遇忙的退避表（node_id → 最早可重试时刻）：不占 DB，重启即清零，
        # 清零后重试也不伤——busy 预检还在，会再次退避。
        self._pipeline_busy_until: dict[int, float] = {}
        self._titling: set[str] = set()  # 正在自动生成标题的会话
        self._manually_named: set[str] = set()  # 用户手改过名字的会话（自动标题让路）
        self._default_model_sessions: set[str] = set()  # 适用「新会话默认模型」的会话
        self._digesting: set[str] = set()  # 正在归档提炼记忆的会话
        self._maintaining = False  # 定期整理进行中（全局+项目共用一把，防叠加）
        self._map_generating: set[int] = set()  # 正在生成演化摘要的项目 id（按项目单飞）
        # 记忆文件的写互斥：归档提炼的追加、定期整理的整稿覆盖、设置页整稿保存
        # 三条写路径共用（整理读旧稿 → 调模型的窗口里可能有人写入，见 _maintain_memory）
        self._memory_io_lock = asyncio.Lock()
        self.update_info: dict | None = None  # {"version","url","notes"}：发现的新版本
        self.update_error: str | None = None  # 手动检查时的失败原因（进设置 · 关于）
        self._pending_update: str | None = None  # 已下载待安装的更新包路径
        self._is_frozen: bool = bool(getattr(sys, "frozen", False))  # 安装版=True，源码版=False
        self.channels: ChannelManager | None = None  # 聊天软件渠道（Bot Channel）
        self._channel_gates: dict[str, ChannelGate] = {}  # 渠道会话 id → 门控
        self._channel_names: dict[str, str] = {}  # 渠道会话 id → 平台名（反向查找）
        self._channel_last_chat: dict[str, str] = {}  # 平台名 → 最近一次入站的 chat_id
        self._weixin_login_channel = None  # 未启用微信时，仅供扫码登录用的一次性实例
        # 访问令牌的失败节流与留痕：设置页展示「有谁在试你的门」，节流让连错
        # 变得不划算。都是内存态，重启即清空（真实排查看桌面日志）。
        self.token_throttle = TokenThrottle()
        self._text_meta_cache: dict[str, tuple[int, str, str]] = {}
        self._token_failures: list[dict] = []  # 最近 TOKEN_FAILURE_LOG_MAX 条 {ts, ip, where}
        self._token_failure_total = 0  # 自启动以来的失败总数（留痕列表只留最近几条）
        # 一键重启的两个钩子，由宿主注入（见 cli/app.py._start_backend 与 desktop.py）：
        # request_shutdown 让 uvicorn 优雅退出（走完 lifespan 收尾）；restart_hook
        # 是桌面专属——关窗口/托盘收尾，不注入时服务停了窗口还在（浏览器模式够用）。
        self.request_shutdown: Callable[[], None] | None = None
        self.restart_hook: Callable[[], None] | None = None

    # ---- 多会话运行时：agent/queue/_run_task/_recorder 指向活动会话的 runtime，
    # ---- 后台会话通过 runtimes[sid] 直接访问（并行 turn 不经 property）。

    # runtime 池上限：点开过的每个会话（含定时任务/流水线产物、渠道会话）都
    # 常驻一整套 Agent+注册表（含 MCP 工具实例）+全量历史，桌面应用一开数天
    # 会单调上涨——「用一天后变卡」的主因。超出上限从最旧开始回收空闲的；
    # 历史都在 SQLite，下次 activate/send 会带着历史重建。
    MAX_RUNTIMES = 12

    def _get_runtime(self, session_id: str) -> SessionRuntime:
        """取（或懒建）一个会话的运行时；新 runtime 自带系统提示词与完整工具集。

        LRU 淘汰：访问即移到末尾；超限后从最旧开始找「空闲」的回收
        （见 _evictable_runtime）——只回收空闲的，宁可超限也不丢运行状态。
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            self.runtimes.move_to_end(session_id)
        else:
            recorder = ChangeRecorder()
            rt = SessionRuntime(
                sid=session_id,
                agent=Agent(
                    provider=self._runtime_provider(session_id),
                    registry=self._build_full_registry(recorder),
                    gate=self.gate,
                    working_dir=self.working_dir,
                    max_iterations=self.cfg.max_iterations,
                    context_limit_tokens=self._context_limit(),
                    compaction_keep_recent=self.cfg.compaction_keep_recent,
                    compaction_trigger=self.cfg.compaction_trigger,
                    hooks=self.hooks,
                    restrict_to_workdir=self.cfg.restrict_to_workdir,
                    session_id=session_id,
                ),
                recorder=recorder,
            )
            rt.agent.set_system(self.compose_system())
            self.runtimes[session_id] = rt
        while len(self.runtimes) > self.MAX_RUNTIMES:
            victim = self._evictable_runtime()
            if victim is None:
                break
            self.runtimes.pop(victim.sid)
            self._forget_runtime(victim)
        return rt

    # ---- 新会话默认模型（ui.json 的 default_model） ----

    def _default_model_pref(self) -> tuple[str, str]:
        """读新会话默认模型偏好，返回 (provider, model)；未设置返回 ("", "")。"""
        try:
            prefs = self._read_ui_prefs()
        except Exception:
            return "", ""
        name = str(prefs.get(self.DEFAULT_MODEL_PROVIDER_KEY) or "").strip()
        if not name or name not in self.cfg.providers:
            return "", ""
        model = str(prefs.get("default_model_name") or "").strip()
        return name, model

    def _runtime_provider(self, session_id: str):
        """会话的起始 provider：设了「新会话默认模型」的新会话用专用实例，
        其余跟随全局。基底会话（懒创建窗口）与已存在 runtime 的老会话都走全局。

        判断「新会话」的口径：session_tabs 恢复/切项目重拉的老会话在 setup 时
        已建 runtime 或经 resume 建过；这里只在**首次建 runtime** 时生效，
        所以不会覆盖用户在会话中途手动 model.switch 的结果（switch 会直接
        改全局 provider 并同步所有 agent）。
        """
        name, model = self._default_model_pref()
        if not name:
            return self.provider
        # 只对新建的会话生效：懒创建的会话在 new_session() 里打标记
        if session_id not in getattr(self, "_default_model_sessions", set()):
            return self.provider
        try:
            return self._build_provider(name, model or None)
        except Exception:
            return self.provider  # 预设被删/停用时回退全局，不让发消息失败

    def _evictable_runtime(self) -> SessionRuntime | None:
        """最旧的空闲 runtime：非当前会话、没在跑的轮、没排队消息、
        没有待确认的权限、没有未落检查点的改动记录。"""
        cur_sid = self.session.id if self.session else ""
        for rt in self.runtimes.values():
            if rt.sid == cur_sid:
                continue
            if rt.run_task is not None and not rt.run_task.done():
                continue
            if rt.queue:
                continue
            if getattr(rt.agent, "_pending", None):
                continue
            if rt.recorder.pre:
                continue
            return rt
        return None

    # ---- 当前服务的模型能力（上下文窗口 / 图片输入） ----

    def _current_provider_cfg(self):
        """当前服务的 ProviderConfig（没有就返回 None）。"""
        if not self.provider_name:
            return None
        return self.cfg.providers.get(self.provider_name)

    def _context_limit(self) -> int:
        """上下文上限：服务级设置优先，否则用全局默认。"""
        pc = self._current_provider_cfg()
        if pc is None:
            return self.cfg.context_limit_tokens
        return pc.effective_context_limit(self.cfg.context_limit_tokens)

    def _supports_vision(self) -> bool:
        """当前服务是否支持图片输入（未配置的服务按支持处理，不拦人）。"""
        pc = self._current_provider_cfg()
        return True if pc is None else bool(pc.supports_vision)

    @property
    def agent(self) -> Agent:
        """活动会话的 Agent（无会话时用基底实例克底，仅供只读探针）。"""
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.agent if rt else self._base_agent

    @property
    def queue(self) -> list:
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.queue if rt else self._base_queue

    @queue.setter
    def queue(self, v: list) -> None:
        self._base_queue = v

    @property
    def _run_task(self):
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.run_task if rt else self._base_run_task

    @_run_task.setter
    def _run_task(self, v):
        if self.session and self.session.id in self.runtimes:
            self.runtimes[self.session.id].run_task = v
        else:
            self._base_run_task = v

    @property
    def _recorder(self) -> ChangeRecorder:
        rt = self.runtimes.get(self.session.id) if self.session else None
        return rt.recorder if rt else self._base_recorder

    @_recorder.setter
    def _recorder(self, v: ChangeRecorder) -> None:
        self._base_recorder = v

    @staticmethod
    def _forget_runtime(rt: SessionRuntime) -> None:
        """runtime 已从字典摘除后调用：释放它引用的全局资源（防泄漏）。"""
        if getattr(rt.agent, "registry", None) is not None:
            rt.agent.registry = None

    def _for_each_agent(self):
        """所有 runtime 的 agent（含基底），用于全局状态刷新（模型/注册表/系统词）。"""
        yield self._base_agent
        for rt in self.runtimes.values():
            yield rt.agent

    # ---- 生命周期 ----

    async def setup(self) -> None:
        self.cfg = load_config()
        self.store = self._store_override or await SessionStore(db_path()).connect()
        # 启动即确定「当前项目」：优先恢复上次用的（ui.json 的 active_project，
        # 0 = 明确的无项目态）；没有记住过（首次启动/老版本升级）才沿用启动目录。
        # 无项目态是合法状态：不建任何项目记录，快聊照常可用。
        prefs = self._read_ui_prefs()
        remembered = prefs.get("active_project")
        projects = await self.store.list_projects()
        target: Path | None = None
        if remembered:
            row = next((p for p in projects if p.id == remembered), None)
            if row is not None and Path(row.root_path).is_dir():
                target = Path(row.root_path)
        elif not projects:
            # 首次启动（没有任何项目记录、也没记住过当前项目）：把启动目录
            # 登记成第一个项目，保持「装完即用」的开箱体验
            if self._startup_dir is not None and self._startup_dir.is_dir():
                target = self._startup_dir
        if target is not None and not target.is_dir():
            target = None
        self.working_dir = target
        # 上次退出时正在跑的编排节点：标为错误等用户手动重跑（无人值守恢复执行有风险）
        _interrupted = await self.store.reset_interrupted_pipelines()
        if _interrupted:
            logging.getLogger("skysheep").info(
                "任务编排：%d 个节点因上次退出被中断，已标记待重跑", _interrupted
            )
        self.subagent_store.load()
        # 「远程连接」固定项目：启动即建（飞书/微信等渠道对话的归属），
        # 与是否配置渠道无关——侧栏里它是一个常驻分组
        await self.store.ensure_remote_project()
        # connect_mcp=False：启动不在这里同步连 MCP，统一交给下面的后台任务
        await self._bind_project(target, connect_mcp=False)
        # 分级权限模式：上次会话选的档位（0=安全执行 1=自动编辑 2=完全访问）重启后保持
        self._apply_gate_accept_pref(prefs.get("accept_edits", 0))
        # 内置示例快捷指令：首启落成真实记录（老用户已有自定义指令则跳过）
        await self._seed_builtin_snippets()
        # 缺 API Key 不阻塞启动：记录状态，界面里可见/可切换后再用
        self.provider_error: str | None = None
        try:
            self.provider = self._build_provider(self.provider_name_arg or self.cfg.default)
        except RuntimeError as e:
            self.provider_error = str(e)
            self.provider = None
            self.provider_name = ""
            self.provider_model = ""

        mcp_configs, mcp_config_warnings = load_mcp_configs(
            self._mcp_global_path(), self._project_mcp_path_if_trusted()
        )
        self.mcp_configs = mcp_configs
        self.mcp = MCPManager(
            mcp_configs,
            on_tools_changed=self._on_mcp_tools_changed,
            reconnect_gate=self._mcp_reconnect_gate,
        )
        # MCP 连接放后台：单台服务器连不上（uvx 冷启动拉包、代理没就绪、地址写错）
        # 各自最多烧 CONNECT_TIMEOUT_S，同步等它会把「服务就绪」拖过桌面端的启动
        # 预算，弹「启动失败」页（2026-09-22/23 实测踩过：代理拒连重试 15~19 秒，
        # 引擎 20.5s 才就绪，桌面 20s 已判死）。连完由 _connect_mcp_after_boot 注入。
        self.mcp_tools = []
        self.mcp_warnings = list(mcp_config_warnings)
        self._mcp_connect_task = asyncio.create_task(
            self._connect_mcp_after_boot(list(mcp_config_warnings))
        )

        self._base_agent = Agent(
            provider=self.provider,
            registry=self._build_full_registry(),
            gate=self.gate,
            working_dir=self.working_dir,
            max_iterations=self.cfg.max_iterations,
            context_limit_tokens=self._context_limit(),
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            compaction_trigger=self.cfg.compaction_trigger,
            hooks=self.hooks,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
        )
        # 后台查一次新版本：几秒超时、失败完全静默，结果随 boot 快照到前端
        self._update_task = asyncio.create_task(self._check_update_quietly())
        await self.open_initial_session()
        # 聊天软件渠道：按配置拉起已启用的平台（失败只记状态，不影响启动）
        self.channels = ChannelManager(self, self._channels_config)
        try:
            await self.channels.restart()
        except Exception as e:  # noqa: BLE001 - 渠道起不来不能让应用启动失败
            logger.warning("渠道初始化失败：%s", e)

    # ---- 新功能配置解析 / 检查点目录 ----

    @staticmethod
    def _checkpoint_root_for(working_dir: Path | None) -> Path:
        """按工作目录指纹算检查点根目录（隔离不同项目的快照）。"""
        tag = hashlib.sha256(
            (str(working_dir) if working_dir else "(no-project)").lower()
            .encode("utf-8")
        ).hexdigest()[:12]
        return skysheep_home() / "backups" / "checkpoints" / tag

    def _checkpoint_root(self) -> Path:
        """当前项目的检查点目录（按工作目录指纹隔离）。"""
        return self._checkpoint_root_for(self.working_dir)

    def _cur_project_id(self) -> int | None:
        """当前项目 id；无项目态返回 None（快聊/无项目语义）。"""
        return self.project.id if self.project is not None else None

    async def _remote_project_id(self) -> int:
        """「远程连接」固定项目的 id（幂等创建 + 实例内缓存）。

        渠道（飞书/微信）会话的归属项目：与桌面工作目录无关，见
        store.ensure_remote_project。缓存失效不需处理——该项目拒绝删除。
        """
        if self._remote_project_id_cache is None:
            proj = await self.store.ensure_remote_project()
            self._remote_project_id_cache = proj.id
        return self._remote_project_id_cache

    async def _reject_remote_project(self, project_id: int, action: str) -> None:
        """固定项目「远程连接」不可删除/切换的统一拦截。"""
        if project_id == await self._remote_project_id():
            raise RuntimeError(f"「远程连接」是固定项目（飞书/微信对话的归属），不能{action}")

    def is_remote_project(self, project) -> bool:
        """按哨兵路径判断是否「远程连接」固定项目（同步，project.list 等渲染用）。"""
        try:
            return project.root_path == self.store.REMOTE_PROJECT_PATH
        except AttributeError:
            return False

    def _require_project(self, action: str = "执行这个操作") -> None:
        """项目级能力（定时任务/编排/白名单/项目任务）的统一门槛：无项目给可读错误。"""
        if self.project is None:
            raise RuntimeError(
                f"当前没有项目，无法{action}——先在侧栏「项目」区点 ＋ 添加项目并选择一个文件夹。"
            )

    def _websearch_kwargs(self) -> dict | None:
        try:
            return resolve_websearch(self.cfg)
        except Exception:  # noqa: BLE001
            return None

    def _configured_services(self) -> list[dict]:
        """已配好 Key 的模型服务清单（设置页里“需要选服务”的下拉复用它们）。

        搜索 / 画图 / 语音这些能力都跑在 OpenAI 兼容接口上，用户不必再抄一遍
        Key 与地址——下拉里直接选已配置的服务即可。只回传掩码与地址，不回传明文。
        """
        out: list[dict] = []
        for name, pc in self.cfg.providers.items():
            if pc.kind == "fake":
                continue
            key = resolve_api_key(name, pc)
            if not key:
                continue
            out.append(
                {
                    "name": name,
                    "kind": pc.kind,
                    "base_url": (pc.base_url or "").rstrip("/"),
                    "model": pc.model,
                    "key_mask": self._mask_key(key),
                }
            )
        return out

    def _imagegen_kwargs(self) -> dict | None:
        try:
            return resolve_imagegen(self.cfg)
        except Exception:  # noqa: BLE001
            return None

    async def _check_update_quietly(self) -> None:
        # 用户关掉了自动检查（关于页开关）就不再请求：内网/离线用户不必等超时
        try:
            if self._read_ui_prefs().get("update_check", 1) != 1:
                return
        except Exception:
            pass
        try:
            info = await check_latest_release()
            if is_newer_version(info["version"], __version__):
                self.update_info = info
        except Exception:  # noqa: BLE001 - 离线/仓库不存在：完全静默
            pass

    def _build_full_registry(self, recorder: ChangeRecorder | None = None) -> ToolRegistry:
        """完整工具集：内置（write/edit/画图挂检查点记录器）+ 日程 + 技能 + 子代理 + MCP。

        子代理可在设置页整体关闭：关掉后 spawn_agent / check_task / wait_task
        不注册，模型看不到这些工具（配置里 subagent_enabled = false）。
        """
        registry = ToolRegistry(default_tools(
            recorder=recorder or self._recorder,
            store=self.store,
            websearch=self._websearch_kwargs(),
            imagegen=self._imagegen_kwargs(),
            computer_control=self.cfg.computer_control,
            browser_control=self.cfg.browser_control,
        ))
        registry.register(LoadSkillTool(self.skills))
        # 无项目态不注册子代理/编排：子代理要工作目录、流水线绑定项目，
        # 没项目时给了模型也只会空转；切回项目后重建注册表自动恢复
        if self.cfg.subagent_enabled and self.project is not None:
            registry.register(SpawnAgentTool(self.tasks))
            registry.register(CheckTaskTool(self.tasks))
            registry.register(WaitTaskTool(self.tasks))
        # 任务编排：Agent 可以排流水线（草稿），启动与否由用户在面板决定
        if self.project is not None:
            registry.register(PipelineWriteTool(
                self.store, lambda: self.project.id, lambda: self.tasks,
            ))
        for t in self.mcp_tools:
            registry.register(t)
        return registry

    def _apply_registry_to_agents(self, recorder: ChangeRecorder | None = None) -> None:
        """把按当前配置重建的工具集发给基础 Agent 与所有会话运行时。"""
        if self._base_agent is None:
            return  # 启动早期（MCP connect_all 触发工具变更回调时）基础 Agent 还没建
        self._base_agent.registry = self._build_full_registry()
        for rt in self.runtimes.values():
            rt.agent.registry = self._build_full_registry(rt.recorder)

    async def shutdown(self) -> None:
        # 崩溃哨兵在收尾一进来就清：这里是一切退出路径（uvicorn lifespan、
        # 程序内更新 apply_update 的直接调用）的必经点，且清在开头——收尾链
        # 越靠后越容易被截断（桌面壳给优雅退出只留 1.5s 上限、更新路径随后
        # os._exit），清在最后一步就会「明明是正常退出，下次启动却提示上次
        # 未正常关闭」。已开始优雅收尾就不再是崩溃，语义也成立。
        try:
            (skysheep_home() / "crash.flag").unlink(missing_ok=True)
        except OSError:
            pass
        if self.channels is not None:
            try:
                await self.channels.stop()
            except Exception:  # noqa: BLE001
                pass
        for rt in self.runtimes.values():
            t = rt.run_task
            if t and not t.done():
                t.cancel()
        self.cancel_run()
        self.stop_cron_loop()
        self.term.close_all()
        self.stop_reminder_loop()
        await self._cancel_mcp_boot_connect()
        if self.mcp:
            await self.mcp.shutdown()
        if self.tasks:
            self.tasks.cancel_all()
        if self.store and self._store_override is None:
            await self.store.close()

    # ---- 日程：增删改查 + 到点提醒广播 ----

    async def schedule_add(self, params: dict) -> dict:
        title = str(params.get("title", "")).strip()
        if not title:
            raise RuntimeError("日程标题不能为空")
        start_at = params.get("start_at")
        if start_at is None:
            raise RuntimeError("缺少开始时间 start_at")
        row = await self.store.add_schedule(
            title,
            float(start_at),
            notes=str(params.get("notes", "")),
            remind=bool(params.get("remind", True)),
            remind_before=int(params.get("remind_before", 0) or 0),
            end_at=float(params.get("end_at") or 0),
        )
        self.notify_schedule_changed()
        return row

    async def schedule_update(self, params: dict) -> dict:
        sid = int(params.get("id", 0))
        kw: dict = {}
        if params.get("title") is not None:
            kw["title"] = str(params["title"]).strip()
        if params.get("notes") is not None:
            kw["notes"] = str(params["notes"])
        if params.get("start_at") is not None:
            kw["start_at"] = float(params["start_at"])
        if params.get("end_at") is not None:
            kw["end_at"] = float(params["end_at"] or 0)
        if params.get("remind") is not None:
            kw["remind"] = bool(params["remind"])
        if params.get("remind_before") is not None:
            kw["remind_before"] = int(params["remind_before"])
        if params.get("done") is not None:
            kw["done"] = bool(params["done"])
        if not kw:
            raise RuntimeError("没有给出任何要修改的字段")
        row = await self.store.update_schedule(sid, **kw)
        if not row:
            raise RuntimeError("日程不存在")
        self.notify_schedule_changed()
        return row

    async def schedule_delete(self, schedule_id: int) -> dict:
        if not await self.store.delete_schedule(schedule_id):
            raise RuntimeError("日程不存在")
        self.notify_schedule_changed()
        return {"deleted": schedule_id}

    def notify_schedule_changed(self) -> None:
        """日程变化后广播事件（无在线连接时静默，如 CLI/测试）。"""
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(
                    ws_emit({"kind": "schedule_updated"})
                )
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    def _ws_broadcast(self, ev: dict) -> None:
        """引擎侧产生的广播事件（子代理直播 / 任务终态）发给在线前端。"""
        if isinstance(ev, dict) and ev.get("kind") == "task_finished":
            # 任务簿任务终态：挂接了它的流水线节点立即对账，不等下一个扫描周期
            try:
                spawn_bg(self._pipeline_kick())
            except RuntimeError:
                pass  # 无事件循环（如纯测试环境）
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit(ev))
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    async def _record_subagent_usage(
        self, session_id: str, provider: str, model: str,
        in_tokens: int, out_tokens: int, cached_tokens: int = 0,
    ) -> None:
        """子代理任务用量落库：归属派生它的会话（未知时记空串，聚合页仍可见）。"""
        await self.store.add_usage(
            session_id or "",
            provider or self.provider_name,
            model or self.provider_model,
            in_tokens, out_tokens, cached_tokens,
        )

    # ---- 到点提醒循环 ----

    REMINDER_INTERVAL = 20.0  # 秒

    def start_reminder_loop(self) -> None:
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    def stop_reminder_loop(self) -> None:
        if getattr(self, "_reminder_task", None):
            self._reminder_task.cancel()
            self._reminder_task = None

    async def _reminder_loop(self) -> None:
        """周期扫描到点未提醒的日程，向所有在线前端广播提醒事件。
        应用关闭期间的到期日程会在下次启动扫描时补提醒。"""
        while True:
            try:
                await self._reminder_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.REMINDER_INTERVAL)

    async def _reminder_pass(self) -> None:
        """单轮扫描：把到期未提醒的日程推给所有在线连接并标记已提醒。"""
        due = await self.store.due_schedules()
        for row in due:
            for ws_emit in list(self.ws_emitters):
                try:
                    await ws_emit({"kind": "schedule_reminder", **row})
                except Exception:
                    pass
            await self.store.mark_schedule_reminded(row["id"])

    # ---- 定时任务：无人值守的周期 Agent 运行 ----

    CRON_INTERVAL = 20  # 扫描周期（秒）

    def start_cron_loop(self) -> None:
        self._cron_task = asyncio.create_task(self._cron_loop())

    def stop_cron_loop(self) -> None:
        if getattr(self, "_cron_task", None):
            self._cron_task.cancel()
            self._cron_task = None

    async def _cron_loop(self) -> None:
        """周期扫描到点的定时任务，逐个后台运行。"""
        while True:
            try:
                await self._cron_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.CRON_INTERVAL)

    async def _cron_pass(self) -> None:
        for row in await self.store.due_cron_tasks():
            if row["id"] in self._cron_running:
                continue
            # 后台触发：任务跑多久都不阻塞扫描循环（下一轮扫描跳过 in-flight 任务）
            spawn_bg(self._run_cron_task(row["id"]))

    def _broadcast_cron(self, task: dict) -> None:
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(
                    ws_emit({"kind": "cron_updated", "task": task})
                )
            except Exception:
                pass

    async def cron_list(self) -> dict:
        self._require_project("列出定时任务")
        tasks = await self.store.list_cron_tasks(self.project.id)
        return {"tasks": tasks}

    async def cron_add(self, params: dict) -> dict:
        self._require_project("新建定时任务")  # 定时任务绑定项目的工作目录与白名单
        name = str(params.get("name", "")).strip()[:40] or "未命名任务"
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError("任务指令不能为空")
        stype = str(params.get("schedule_type", "interval"))
        if stype not in ("interval", "daily", "weekly"):
            raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
        interval = max(1, int(params.get("interval_minutes") or 1))
        tod = str(params.get("time_of_day") or "")
        wd = int(params.get("weekday") or -1)
        tools = [str(t).strip() for t in (params.get("allowed_tools") or []) if str(t).strip()]
        task = await self.store.add_cron_task(
            self.project.id, name, prompt, stype, interval, tod, wd, tools
        )
        task = await self.store.update_cron_task(
            task["id"], next_run_at=self.store.compute_next_run(task)
        )
        self._broadcast_cron(task)
        return task

    async def cron_update(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        self._check_cron_ownership(task)
        kw = {}
        if params.get("name") is not None:
            kw["name"] = str(params["name"]).strip()[:40] or task["name"]
        if params.get("prompt") is not None:
            kw["prompt"] = str(params["prompt"]).strip()
        if params.get("schedule_type") is not None:
            stype = str(params["schedule_type"])
            if stype not in ("interval", "daily", "weekly"):
                raise RuntimeError("schedule_type 只支持 interval / daily / weekly")
            kw["schedule_type"] = stype
        if params.get("interval_minutes") is not None:
            kw["interval_minutes"] = max(1, int(params["interval_minutes"]))
        if params.get("time_of_day") is not None:
            kw["time_of_day"] = str(params["time_of_day"])
        if params.get("weekday") is not None:
            kw["weekday"] = int(params["weekday"])
        if params.get("allowed_tools") is not None:
            kw["allowed_tools"] = [str(t).strip() for t in params["allowed_tools"] if str(t).strip()]
        if params.get("enabled") is not None:
            kw["enabled"] = 1 if bool(params["enabled"]) else 0
        # 调度/启用变化后重算下次运行时间
        if any(k in kw for k in ("schedule_type", "interval_minutes", "time_of_day",
                                 "weekday", "enabled")) and "next_run_at" not in kw:
            merged = {**task, **kw}
            enabled = kw.get("enabled", task["enabled"])
            kw["next_run_at"] = self.store.compute_next_run(merged) if enabled else 0
        task = await self.store.update_cron_task(tid, **kw)
        self._broadcast_cron(task)
        return task

    async def cron_delete(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is not None:
            # 归属校验（安全审查 B8）：凭枚举到的 task_id 不能删别的项目的任务
            self._check_cron_ownership(task)
        ok = await self.store.delete_cron_task(tid)
        return {"deleted": ok, "id": tid}

    async def cron_run_now(self, params: dict) -> dict:
        tid = int(params.get("id", 0))
        task = await self.store.get_cron_task(tid)
        if task is None:
            raise RuntimeError("任务不存在: " + str(tid))
        self._check_cron_ownership(task)
        await self._run_cron_task(tid, force=True)
        return {"started": True, "id": tid}

    def _check_cron_ownership(self, task: dict) -> None:
        """定时任务必须属于当前项目才能改/删/触发（安全审查 B8）。

        报错文案与其他归属校验一致（不区分「不存在/无权」，防枚举）。
        无项目态没有可归属的项目，同样按不存在处理。
        """
        if task.get("project_id") != self._cur_project_id():
            raise RuntimeError("任务不存在: " + str(task.get("id")))

    async def _cron_execution_context(self, task: dict) -> tuple[int | None, Path | None]:
        """定时任务的项目上下文：返回（project_id, working_dir）。

        扫描循环会看到所有项目的到点任务，但后端只挂在当前项目上；
        不区分归属的话，B 项目的任务会把 prompt 执行到 A 项目的文件上
        （会话/白名单/工作目录全部错位）。目录已不存在时回退当前项目，
        任务本身的错误由运行结果体现；无项目态（当前项目已删空）时返回
        (None, None)，由调用方把任务标成「所属项目已不存在」。
        """
        pid = task.get("project_id")
        if pid is not None and pid != self._cur_project_id():
            proj = await self.store.get_project(pid)
            if proj is not None and Path(proj.root_path).is_dir():
                return pid, Path(proj.root_path)
        if self.project is None:
            return None, None
        return self.project.id, self.working_dir

    async def _run_cron_task(self, task_id: int, force: bool = False) -> None:
        """跑一个定时任务：独立会话 + headless 门控；结果写回任务行并广播。"""
        if task_id in self._cron_running:
            return
        task = await self.store.get_cron_task(task_id)
        if task is None:
            return
        if not task["enabled"] and not force:
            return
        if not force:
            self._cron_running.add(task_id)
        try:
            if self.provider is None:
                raise RuntimeError("尚未配置可用的模型 API Key")

            # 按任务自身项目跑（见 _cron_execution_context）：会话归属、白名单、
            # 工作目录都要对上，不能把别的项目的任务挂到当前项目执行
            cron_pid, cron_workdir = await self._cron_execution_context(task)
            if cron_pid is None or cron_workdir is None:
                # 项目已删空（无项目态）：没有可落的工作目录，不再重排下一次运行
                raise RuntimeError("任务所属的项目已不存在，定时任务已停用")

            # 每个任务一个独立会话（同名），历史随运行累积
            sess = await self.store.create_session(cron_pid, title=f"⏰ {task['name']}")
            sid = sess.id
            gate = CronGate(allowed=task["allowed_tools"], store=self.store,
                            project_id=cron_pid, working_dir=cron_workdir)
            recorder = ChangeRecorder()
            runtime = SessionRuntime(
                sid=sid,
                agent=Agent(
                    provider=self.provider,
                    registry=self._build_full_registry(recorder),
                    gate=gate,
                    working_dir=cron_workdir,
                    max_iterations=self.cfg.max_iterations,
                    context_limit_tokens=self._context_limit(),
                    compaction_keep_recent=self.cfg.compaction_keep_recent,
                    compaction_trigger=self.cfg.compaction_trigger,
                    hooks=self.hooks,
                    restrict_to_workdir=self.cfg.restrict_to_workdir,
                    session_id=sid,
                ),
                recorder=recorder,
            )
            runtime.agent.set_system(self.compose_system_for(cron_workdir))

            async def cron_emit(ev: dict) -> None:
                pass  # 无人值守：流式/权限/队列事件不进前端，结果走任务行

            result = await self._run_turn_pipeline(
                task["prompt"], cron_emit, plan_mode=False,
                images=None, runtime=runtime, session_id=sid,
            )
            # 取本轮最后一条助手文本作为结果摘要
            last_text = ""
            for m in reversed(runtime.agent.history):
                if m.role == "assistant" and m.text.strip():
                    last_text = m.text.strip().replace("\n", " ")[:200]
                    break
            status = "ok" if (not result.get("stopped") and last_text) else (
                "error" if result.get("stopped") else "empty")
            task = await self.store.update_cron_task(
                task_id,
                last_run_at=time.time(),
                last_status=status,
                last_result=last_text,
                enabled=task["enabled"],
            )
            # 下次运行时间：interval 基于本次完成时刻；daily/weekly 基于当前时刻
            task = await self.store.update_cron_task(
                task_id,
                next_run_at=self.store.compute_next_run({**task, "last_run_at": task["last_run_at"]}),
            )
            self._broadcast_cron(task)
            if status != "error":
                await self.notify({"title": f"⏰ 定时任务完成：{task['name']}",
                                   "body": last_text or "本轮没有产出"})
            else:
                await self.notify({"title": f"⏰ 定时任务异常：{task['name']}",
                                   "body": "本轮被中断或没有结果"})
        except Exception as e:  # noqa: BLE001 - 任务失败也要回写状态
            try:
                task = await self.store.update_cron_task(
                    task_id,
                    last_run_at=time.time(),
                    last_status="error",
                    last_result=str(e)[:200],
                )
                task = await self.store.update_cron_task(
                    task_id,
                    next_run_at=self.store.compute_next_run(
                        {**task, "last_run_at": task["last_run_at"]}
                    ) if task["enabled"] else 0,
                )
                self._broadcast_cron(task)
                await self.notify({"title": f"⏰ 定时任务失败：{task['name']}",
                                   "body": str(e)[:160]})
            except Exception:
                pass
        finally:
            self._cron_running.discard(task_id)

    # ---- 任务编排（pipelines）：按依赖顺序自动跑的无人值守节点 ----
    # 与定时任务同一套执行底座（独立会话 + HeadlessGate 白名单 + 完整工具集），
    # 差别在触发方式：定时任务按时间到点触发，编排节点按「依赖的节点全部完成」
    # 触发——并行开发三个功能、最后一个审查汇总，就是它要解的问题。

    PIPELINE_INTERVAL = 5  # 扫描周期（秒）；节点结束会立刻补扫一次，不等周期
    PIPELINE_GLOBAL_CAP = 3  # 跨流水线同时在跑的节点数上限（保护机器）
    NODE_RESULT_INJECT_CHARS = 2000  # 依赖产出注入下游 prompt 的截断长度
    NODE_TIMEOUT_DEFAULT_S = 3600  # 节点默认超时（秒）；0 = 不限时。防止卡死占用并发槽
    NODE_TIMEOUT_MAX_S = 24 * 3600  # 超时上限：一天。超过这个量级说明指令有问题，不该靠等
    BUSY_RETRY_WAIT_S = 300  # 会话续跑遇忙的退避等待（秒）：不消耗重试次数，等空闲
    DEP_FAIL_MARK = "依赖的节点"  # 依赖失败型错误的固定前缀（重跑时识别可回退的下游）

    def start_pipeline_loop(self) -> None:
        self._pipeline_task = asyncio.create_task(self._pipeline_loop())

    def stop_pipeline_loop(self) -> None:
        if getattr(self, "_pipeline_task", None):
            self._pipeline_task.cancel()
            self._pipeline_task = None

    async def _pipeline_loop(self) -> None:
        while True:
            try:
                await self._pipeline_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮扫描失败不终止循环
            await asyncio.sleep(self.PIPELINE_INTERVAL)

    async def _pipeline_pass(self) -> None:
        for pipe in await self.store.list_pipelines():
            if pipe["status"] != "running":
                continue
            await self._pipeline_advance(pipe)

    async def _pipeline_advance(self, pipe: dict) -> None:
        """一轮推进：挂接节点对账 + 依赖判定（blocked → ready/error/skipped）+ 并发调度 + 终态收尾。"""
        await self._sync_task_nodes(pipe)
        by_id = {n["id"]: n for n in pipe["nodes"]}
        for node in pipe["nodes"]:
            if node["status"] != "blocked" or node["kind"] == "task":
                # 挂接节点跟随原任务（已在跑/已终态），不参与依赖释放，也不会被派跑
                continue
            deps = [by_id[d] for d in node["depends_on"] if d in by_id]
            failed = [d for d in deps if d["status"] in ("error", "cancelled")]
            skipped_dep = next((d for d in deps if d["status"] == "skipped"), None)
            gates = [d for d in deps if d.get("control") == "gate" and d["status"] == "done"]
            gate_open = all(self._gate_passed(d) for d in gates)
            if node["dep_mode"] == "any":
                # any 语义：任一依赖完成就跑。失败/跳过/门拦截只在「全部依赖都已
                # 终态且无一 done」时才定死——上游 A 挂了但 B 还在跑时，下游不能
                # 提前判死（否则 any 退化成 all 的失败敏感版）。
                if not deps:
                    satisfied = True  # 无依赖节点立即就绪（与 all 模式一致）
                else:
                    finished = [d for d in deps if d["status"] in
                                ("done", "error", "cancelled", "skipped")]
                    any_done = any(d["status"] == "done" for d in deps)
                    if any_done and gate_open:
                        satisfied = True  # 走下方 satisfied 分支（stop 节点也在其中）
                    else:
                        satisfied = False
                        if len(finished) == len(deps):
                            # 全部终态仍无一放行：定死原因按优先级取第一个能解释的
                            if failed:
                                reason = (f"{self.DEP_FAIL_MARK}「{failed[0]['title']}」"
                                          "未成功，且其余依赖无一完成")
                                await self.store.update_pipeline_node(
                                    node["id"], status="error", last_error=reason,
                                    finished_at=time.time(),
                                )
                                node["status"] = "error"
                                await self.notify({
                                    "title": f"⚙️ 流水线节点失败：{node['title']}",
                                    "body": reason[:160],
                                })
                            elif skipped_dep is not None:
                                await self.store.update_pipeline_node(
                                    node["id"], status="skipped",
                                    last_error=f"上游「{skipped_dep['title']}」被跳过",
                                    finished_at=time.time(),
                                )
                                node["status"] = "skipped"
                            elif gates and not gate_open:
                                await self.store.update_pipeline_node(
                                    node["id"], status="skipped",
                                    last_error=self._gate_skip_reason(gates[0]),
                                    finished_at=time.time(),
                                )
                                node["status"] = "skipped"
                        # 未到全终态：还有依赖在跑，继续等（下一轮扫描再判）
                        continue
            else:
                if failed:
                    await self.store.update_pipeline_node(
                        node["id"], status="error",
                        last_error=f"{self.DEP_FAIL_MARK}「{failed[0]['title']}」未成功，未自动运行",
                        finished_at=time.time(),
                    )
                    node["status"] = "error"
                    await self.notify({
                        "title": f"⚙️ 流水线节点失败：{node['title']}",
                        "body": f"依赖的「{failed[0]['title']}」没有成功，可在「任务编排」面板重跑",
                    })
                    continue
                # 条件门的级联：上游被跳过，本节点也无事可做（终态，不算失败）
                if skipped_dep is not None:
                    await self.store.update_pipeline_node(
                        node["id"], status="skipped",
                        last_error=f"上游「{skipped_dep['title']}」被跳过",
                        finished_at=time.time(),
                    )
                    node["status"] = "skipped"
                    continue
                # 条件门判定：门节点产出 FAIL → 依赖它的下游整体跳过；PASS 照常
                if gates and not gate_open:
                    await self.store.update_pipeline_node(
                        node["id"], status="skipped",
                        last_error=self._gate_skip_reason(gates[0]),
                        finished_at=time.time(),
                    )
                    node["status"] = "skipped"
                    continue
                satisfied = all(d["status"] == "done" for d in deps)
            if satisfied:
                # 终止节点：依赖满足即截停整条流水线（自身不派跑 Agent）
                if node.get("control") == "stop":
                    await self._terminate_pipeline_at(pipe, node)
                    return  # 流水线已被终止收尾，本轮到此为止
                await self.store.update_pipeline_node(node["id"], status="ready")
                node["status"] = "ready"
        # 调度：按 seq 顺序占并发额度；全局有上限，防止多条流水线一起跑拖垮机器。
        # 在跑数从句柄集合统计（而不是本轮 DB 快照）：扫描周期与节点结束的补扫可能
        # 并发推进，快照是调度前的旧状态，会把同一流水线的并发上限算小、短暂超跑。
        # _pipeline_running 兼做去重：DB 里还是 ready 而句柄未注册时不重复派跑。
        # 挂接节点（kind=task）不进调度：它不是编排启动的，不占并发额度。
        running = len(self._pipeline_running.intersection(by_id))
        for node in sorted(
            (n for n in pipe["nodes"] if n["status"] == "ready" and n["kind"] != "task"),
            key=lambda n: n["seq"],
        ):
            if node["id"] in self._pipeline_running:
                continue
            # 会话续跑节点遇忙的退避：未到重试时刻不派跑（不占并发额度）
            if self._pipeline_busy_until.get(node["id"], 0) > time.time():
                continue
            self._pipeline_busy_until.pop(node["id"], None)
            if running >= max(1, int(pipe["concurrency"] or 1)):
                break
            if len(self._pipeline_running) >= self.PIPELINE_GLOBAL_CAP:
                break
            node["status"] = "running"
            running += 1
            self._pipeline_running.add(node["id"])
            task = asyncio.get_running_loop().create_task(self._run_pipeline_node(pipe, node))
            self._pipeline_node_tasks[node["id"]] = task
        await self._maybe_finish_pipeline(pipe)

    async def _sync_task_nodes(self, pipe: dict) -> None:
        """挂接节点（kind=task）与任务簿对账：状态与产出跟随原任务。

        任务簿在内存里、随应用重启清空：还在跑的挂接节点标为错误等用户处理，
        已终态的不动（产出早已落进节点行）。对账有变化时广播一次流水线。
        """
        changed = False
        for node in pipe["nodes"]:
            if node["kind"] != "task" or not node["ref_id"]:
                continue
            rec = self.tasks.status(node["ref_id"]) if self.tasks else None
            if rec is None:
                if node["status"] == "running":
                    await self.store.update_pipeline_node(
                        node["id"], status="error",
                        last_error="任务簿任务已随应用重启丢失，无法继续跟踪",
                        finished_at=time.time(),
                    )
                    node["status"] = "error"
                    changed = True
                continue
            fields = task_node_fields(rec)
            if (node["status"], node["result"], node["last_error"]) != (
                fields["status"], fields["result"], fields["last_error"]
            ):
                await self.store.update_pipeline_node(node["id"], **fields)
                node.update(fields)
                changed = True
        if changed:
            self._broadcast_pipeline(await self.store.get_pipeline(pipe["id"]))

    def _broadcast_pipeline(self, pipe: dict | None) -> None:
        if pipe is None:
            return
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(
                    ws_emit({"kind": "pipeline_updated", "pipeline": pipe})
                )
            except Exception:
                pass

    @staticmethod
    def _gate_passed(node: dict) -> bool:
        """条件门的判定：产出首行以 PASS / FAIL 开头时直接采信；首行没有标记就看
        全文里哪个先出现；两者都没有 = 不放行（保守拦截，宁可跳过不可误跑）。"""
        text = (node.get("result") or "").strip()
        if not text:
            return False
        first = text.splitlines()[0].strip().upper()
        if first.startswith("PASS"):
            return True
        if first.startswith("FAIL"):
            return False
        upper = text.upper()
        p, f = upper.find("PASS"), upper.find("FAIL")
        if f == -1:
            return p != -1
        return p != -1 and p < f

    @staticmethod
    def _gate_marked(node: dict) -> bool:
        """门节点是否明确输出了 PASS / FAIL 标记（区分「判 FAIL」与「忘了输出」）。"""
        text = (node.get("result") or "").strip()
        if not text:
            return False
        first = text.splitlines()[0].strip().upper()
        return first.startswith("PASS") or first.startswith("FAIL") or \
            "PASS" in text.upper() or "FAIL" in text.upper()

    @staticmethod
    def _gate_skip_reason(gate: dict) -> str:
        """门拦截的下游错误文案：FAIL 与「没输出标记」分开说，避免后者被误读成真失败。"""
        if ServerBackend._gate_marked(gate):
            return f"条件门「{gate['title']}」判定未通过"
        return (f"条件门「{gate['title']}」没有输出 PASS/FAIL 标记，"
                f"按保守策略跳过下游（检查门节点产出，必要时重跑门节点）")

    async def _terminate_pipeline_at(self, pipe: dict, node: dict) -> None:
        """终止节点触发：自身记完成，未开始的其余节点全部取消，流水线置已停止。

        正在跑的节点不打断（自然跑完落状态），只是不再派新节点。
        """
        await self.store.update_pipeline_node(
            node["id"], status="done", result="在此终止流水线", finished_at=time.time(),
        )
        node["status"] = "done"
        for other in pipe["nodes"]:
            if other["id"] == node["id"]:
                continue
            if other["status"] in ("blocked", "ready"):
                await self.store.update_pipeline_node(
                    other["id"], status="cancelled",
                    last_error=f"被终止节点「{node['title']}」截停",
                    finished_at=time.time(),
                )
                other["status"] = "cancelled"
        updated = await self.store.update_pipeline(
            pipe["id"], status="cancelled", finished_at=time.time(),
        )
        self._broadcast_pipeline(updated)
        await self.notify({
            "title": f"⏹ 流水线已终止：{pipe['name']}",
            "body": f"终止节点「{node['title']}」条件满足，后续节点已停止。",
        })

    async def _pipeline_execution_context(self, pipe: dict) -> tuple[int | None, Path | None]:
        """流水线归属项目的执行上下文（对标 _cron_execution_context）。

        无项目态（流水线所属项目已删空）返回 (None, None)，调用方报错终止。"""
        pid = pipe.get("project_id")
        if pid is not None and pid != self._cur_project_id():
            proj = await self.store.get_project(pid)
            if proj is not None and Path(proj.root_path).is_dir():
                return pid, Path(proj.root_path)
        if self.project is None:
            return None, None
        return self.project.id, self.working_dir

    @staticmethod
    def _node_result_file(node_id: int) -> str:
        """节点产出全文的落盘路径（相对流水线所属项目的工作目录）。

        依赖产出注入下游的摘要只有 2000 字；全文写进约定文件，下游 prompt
        里给路径，Agent 用本就放行的只读工具自取——零新增安全面。
        """
        return f".skysheep/pipeline-results/node-{node_id}.md"

    async def _write_node_result_file(self, node: dict, result: str) -> None:
        """节点完成时把产出全文写进项目目录（供下游读全文；失败静默，摘要注入仍在）。"""
        try:
            path = Path(self._node_result_file(node["id"]))
            if not path.is_absolute():
                pipe = await self.store.get_pipeline(node["pipeline_id"])
                pid = pipe.get("project_id") if pipe else None
                if pid is not None and pid != self._cur_project_id():
                    proj = await self.store.get_project(pid)
                    base = Path(proj.root_path) if proj is not None else self.working_dir
                else:
                    base = self.working_dir
                if base is None:
                    return  # 无项目态：没有可落的工作目录，只留摘要注入
                path = base / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result, encoding="utf-8")
        except Exception:  # noqa: BLE001 - 产文件失败不扣主流程（下游还有摘要）
            pass

    async def _compose_node_prompt(self, node: dict, attempt: int = 1,
                                   prev_error: str = "") -> str:
        """组装节点 prompt：依赖产出摘要注入 + 产出全文路径 + 重试失败上下文。

        汇总/审查节点靠摘要看到上游结果；需要全量时按路径自己去读文件。
        attempt > 1 且 prev_error 非空时附上次失败原因，重试不再是盲目的原样再跑。
        """
        parts = []
        read_hint = []
        for d in node["depends_on"]:
            dep = await self.store.get_pipeline_node(int(d))
            if dep is not None and dep["status"] == "done" and dep["result"]:
                parts.append(
                    f"## 前置任务「{dep['title']}」的产出\n"
                    + dep["result"][: self.NODE_RESULT_INJECT_CHARS]
                )
                full = self._node_result_file(dep["id"])
                read_hint.append(
                    f"「{dep['title']}」的完整产出在 {full}（摘要截断时可用只读工具读全文）"
                )
        prompt = node["prompt"]
        if parts:
            head = (
                "你是任务编排流水线中的一个节点。以下是前置任务的产出，"
                "请结合它们完成自己的任务。\n\n" + "\n\n".join(parts) + "\n\n"
                + ("\n".join(read_hint) + "\n\n---\n\n你的任务：\n")
            )
            prompt = head + prompt
        if attempt > 1 and prev_error:
            prompt += (
                f"\n\n---\n\n（第 {attempt} 次尝试；上一次失败原因：{prev_error[:300]}。"
                "请换思路避免再犯同样的错。）"
            )
        return prompt

    async def _run_pipeline_node(self, pipe: dict, node: dict) -> None:
        """跑一个节点：独立会话 + headless 门控；产出写回节点行（对标 _run_cron_task）。

        超时：timeout_s > 0 时限时执行，超时按可重试失败处理（防止单节点卡死
        占住并发槽）。会话续跑遇忙：不消耗重试次数，退避后重排队。
        """
        sid = ""
        try:
            if self.provider is None:
                raise RuntimeError("尚未配置可用的模型 API Key")
            run_pid, run_workdir = await self._pipeline_execution_context(pipe)
            if run_pid is None or run_workdir is None:
                raise RuntimeError("流水线所属的项目已不存在，无法继续执行")
            prev_error = node.get("last_error") or ""  # 重试上下文要取更新前的快照
            if node["kind"] == "session":
                # 会话续跑预检：会话正被使用时不硬拒——不占并发、不消耗重试次数，
                # 退避后由扫描循环自动重试（并发额度不浪费在等会话上）
                sess = await self.store.get_session(node["ref_id"])
                if sess is None or sess.project_id != run_pid:
                    raise RuntimeError("会话不存在或不属于本流水线的项目")
                busy_rt = self.runtimes.get(node["ref_id"])
                busy_task = getattr(busy_rt, "run_task", None) if busy_rt else None
                if busy_task is not None and not busy_task.done():
                    self._pipeline_busy_until[node["id"]] = time.time() + self.BUSY_RETRY_WAIT_S
                    wait_min = max(1, self.BUSY_RETRY_WAIT_S // 60)
                    await self.store.update_pipeline_node(
                        node["id"],
                        last_error=f"会话正被使用，{wait_min} 分钟后自动重试（不消耗重试次数）",
                    )
                    return
            node = await self.store.update_pipeline_node(
                node["id"], status="running", started_at=time.time(),
                runs=(node["runs"] or 0) + 1, last_error="",
            )
            self._broadcast_pipeline(await self.store.get_pipeline(pipe["id"]))

            prompt = await self._compose_node_prompt(
                node, attempt=int(node.get("runs") or 1), prev_error=prev_error,
            )
            if node.get("control") == "loop" and (node.get("runs") or 0) > 0:
                # 迭代节点第 2 轮起：把上一轮产出带回去，让它接着推进而不是从零再来
                prompt += (
                    "\n\n---\n\n## 你上一轮的产出\n"
                    + (node.get("result") or "")[: self.NODE_RESULT_INJECT_CHARS]
                    + "\n\n请在此基础上继续推进；如果任务已经完成，把回复的第一行写成 DONE。"
                )
            if node["kind"] == "session":
                # 会话续跑：在本项目已有的会话里继续（带其历史上下文）；
                # 上方预检已确认空闲，这里直接加载历史
                sid = node["ref_id"]
            else:
                sess = await self.store.create_session(
                    run_pid, title=f"⚙️ {pipe['name']} · {node['title']}"
                )
                sid = sess.id
            gate = HeadlessGate(allowed=node["allowed_tools"], store=self.store,
                                project_id=run_pid, working_dir=run_workdir)
            recorder = ChangeRecorder()
            runtime = SessionRuntime(
                sid=sid,
                agent=Agent(
                    provider=self.provider,
                    registry=self._build_full_registry(recorder),
                    gate=gate,
                    working_dir=run_workdir,
                    max_iterations=self.cfg.max_iterations,
                    context_limit_tokens=self._context_limit(),
                    compaction_keep_recent=self.cfg.compaction_keep_recent,
                    compaction_trigger=self.cfg.compaction_trigger,
                    hooks=self.hooks,
                    restrict_to_workdir=self.cfg.restrict_to_workdir,
                    session_id=sid,
                ),
                recorder=recorder,
            )
            pipe_system = self.compose_system_for(run_workdir)
            if node["kind"] == "session":
                await self._reload_agent_history(runtime.agent, sid, pipe_system)
            else:
                runtime.agent.set_system(pipe_system)

            async def pipe_emit(ev: dict) -> None:
                pass  # 无人值守：流式/权限事件不进前端，产出走节点行

            timeout_s = max(0, int(node.get("timeout_s") or 0))
            run_coro = self._run_turn_pipeline(
                prompt, pipe_emit, plan_mode=False,
                images=None, runtime=runtime, session_id=sid,
            )
            if timeout_s > 0:
                timeout_txt = f"{timeout_s // 60} 分钟" if timeout_s >= 120 else f"{timeout_s} 秒"
                try:
                    result = await asyncio.wait_for(run_coro, timeout=timeout_s)
                except TimeoutError:
                    await self._pipeline_node_fail(
                        pipe, node, sid,
                        f"节点超时（超过 {timeout_txt}），已中止；可在节点上调大超时或拆小任务",
                        notify_body=f"超过 {timeout_txt} 未完成，自动中止",
                    )
                    return
            else:
                result = await run_coro
            last_text = ""
            for m in reversed(runtime.agent.history):
                if m.role == "assistant" and m.text.strip():
                    last_text = m.text.strip()
                    break
            if result.get("stopped"):
                await self._pipeline_node_fail(
                    pipe, node, sid, "运行被中断", retriable=False, notify_body="运行被中断",
                )
            elif not last_text:
                await self._pipeline_node_fail(
                    pipe, node, sid, "节点没有产出", notify_body="这一轮没有产出文本",
                )
            else:
                final_text, finished = self._loop_settle(node, last_text)
                if finished:
                    await self.store.update_pipeline_node(
                        node["id"], status="done", result=final_text,
                        session_id=sid, finished_at=time.time(),
                    )
                    # 产出全文落盘：下游需要全量时按路径自取（摘要注入只有 2000 字）
                    await self._write_node_result_file(node, final_text)
                else:
                    # 迭代节点：本轮未见 DONE 标记 → 重新排队，下一轮带上产出继续
                    await self.store.update_pipeline_node(
                        node["id"], status="ready", result=last_text, session_id=sid,
                    )
                    node["status"] = "ready"
        except asyncio.CancelledError:
            await self.store.update_pipeline_node(
                node["id"], status="cancelled", last_error="用户停止流水线",
                session_id=sid, finished_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001 - 节点失败也要落状态
            try:
                await self._pipeline_node_fail(pipe, node, sid, str(e)[:300],
                                               notify_body=str(e)[:160])
            except Exception:
                pass
        finally:
            self._pipeline_running.discard(node["id"])
            self._pipeline_node_tasks.pop(node["id"], None)
            # 立刻补扫：刚完成的节点可能解锁了下游，不等下一个扫描周期
            spawn_bg(self._pipeline_kick())

    def _loop_settle(self, node: dict, text: str) -> tuple[str, bool]:
        """迭代节点（control=loop）的收尾：产出首行 DONE = 完成；否则返回未完成、
        由调用方重新排队（下一轮会带上本轮产出继续）。返回 (最终产出, 是否完成)。"""
        if node.get("control") != "loop":
            return text, True
        lines = text.splitlines()
        if lines and lines[0].strip().upper().startswith("DONE"):
            rest = "\n".join(lines[1:]).strip()
            return (rest or text), True
        if (node.get("runs") or 0) >= max(2, int(node.get("max_runs") or 2)):
            return text + f"\n\n（已到最大迭代轮数 {node.get('max_runs')}，未见 DONE 标记）", True
        return text, False

    async def _pipeline_node_fail(
        self, pipe: dict, node: dict, sid: str, message: str,
        retriable: bool = True, notify_body: str = "",
    ) -> None:
        """节点失败落状态：还有重试余量（runs < max_runs）就重新排队自动再试，
        否则标失败并通知。「运行被中断」属于人为操作，不自动重试。"""
        max_runs = max(1, int(node.get("max_runs") or 1))
        runs = int(node.get("runs") or 0)
        if retriable and runs < max_runs:
            await self.store.update_pipeline_node(
                node["id"], status="ready",
                last_error=f"第 {runs}/{max_runs} 次尝试失败（将自动重试）：{message[:200]}",
            )
            node["status"] = "ready"
            return
        await self.store.update_pipeline_node(
            node["id"], status="error", last_error=message[:300],
            session_id=sid, finished_at=time.time(),
        )
        await self.notify({
            "title": f"⚙️ 流水线节点失败：{node['title']}",
            "body": notify_body or message[:160],
        })

    async def _pipeline_kick(self) -> None:
        try:
            await self._pipeline_pass()
        except Exception:
            pass

    async def _channel_push_pipeline(
        self, pipe: dict, ok: bool, total: int, failed: int, skipped: list,
    ) -> None:
        """流水线收尾时向聊天渠道推一条摘要（纯推送，不需要回复）。

        推送目标是渠道配置里的允许名单（allowed_ids）——它们本来就是主人的
        准入标识；名单为空的渠道拒发一切，这里同样不推。发送失败静默记日志，
        不影响桌面端通知与流水线状态。
        """
        mgr = self.channels
        if mgr is None:
            return
        status_word = "完成" if ok else "结束（有失败）"
        icon = "✅" if ok else "⚠️"
        body = f"{icon} 流水线「{pipe['name']}」{status_word}：共 {total} 个节点"
        if failed:
            body += f"，{failed} 个未成功"
        if skipped:
            body += f"，{len(skipped)} 个被跳过"
        for name, channel in list(mgr.channels.items()):
            if not channel.enabled or not channel.configured():
                continue
            for chat_id in sorted(channel.allowed_ids):
                try:
                    await channel.send_text(chat_id, body)
                except Exception as e:  # noqa: BLE001 - 推送失败只记日志
                    logger.warning("流水线摘要推送 %s(%s) 失败：%s", name, chat_id, e)

    async def _maybe_finish_pipeline(self, pipe: dict) -> None:
        """全部节点到终态时给流水线收尾：done/skipped 视为成功（skipped 是条件门
        主动跳过，不算失败），其余 → failed。通知带上跳过与失败计数，
        「门没输出标记」这类静默情况一眼可见。"""
        if pipe["status"] != "running":
            return
        nodes = await self.store.list_pipeline_nodes(pipe["id"])
        if not nodes or any(n["status"] in ("blocked", "ready", "running") for n in nodes):
            return
        all_done = all(n["status"] in ("done", "skipped") for n in nodes)
        updated = await self.store.update_pipeline(
            pipe["id"], status="done" if all_done else "failed", finished_at=time.time()
        )
        self._broadcast_pipeline(updated)
        skipped = [n for n in nodes if n["status"] == "skipped"]
        bad = [n for n in nodes if n["status"] not in ("done", "skipped")]
        if all_done:
            body = f"全部 {len(nodes)} 个节点完成"
            if skipped:
                body += f"，其中 {len(skipped)} 个被条件门跳过：{skipped[0]['title']}"
        else:
            body = f"{len(bad)} 个节点未成功：{bad[0]['title']}"
            if skipped:
                body += f"；另有 {len(skipped)} 个被跳过"
        await self.notify({
            "title": ("✅ 流水线完成：" if all_done else "⚠️ 流水线结束（有失败）：") + pipe["name"],
            "body": body,
        })
        # 有渠道在线时同步推送一份摘要（纯推送、不含执行，无人值守主场景）
        await self._channel_push_pipeline(pipe, all_done, len(nodes), len(bad), skipped)

    # ---- 任务编排：WS 方法 ----

    async def pipeline_list(self) -> dict:
        self._require_project("列出任务编排")  # 流水线绑定项目的工作目录
        return {"pipelines": await self.store.list_pipelines(self.project.id)}

    async def pipeline_get(self, params: dict) -> dict:
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None or pipe["project_id"] != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        # token 用量汇总：节点会话的 usage_log 聚合（无人值守批量跑的成本一眼可见）
        try:
            usage = await self.store.pipeline_usage(pipe["id"])
        except Exception:
            usage = {"in_tokens": 0, "out_tokens": 0}
        return {"pipeline": pipe, "usage": usage}

    async def pipeline_duplicate(self, params: dict) -> dict:
        """复制一条流水线为草稿（新名字加「副本」）：结构与配置原样，状态全部清零。

        挂接/会话节点保留 kind 与 ref_id 原样复制——原任务/会话若已不在，
        启动时对账会标错，用户可删；不在这里静默转 run（指令可能为空）。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None or pipe["project_id"] != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        by_old = {n["id"]: i for i, n in enumerate(pipe["nodes"])}
        nodes = []
        for n in pipe["nodes"]:
            nodes.append({
                "title": n["title"],
                "prompt": n["prompt"],
                "allowed_tools": n["allowed_tools"],
                # 依赖映射为新批次序号（store 物化回新 id）；指向已删节点的悬空依赖丢弃
                "depends_on": [by_old[d] for d in n["depends_on"] if d in by_old],
                "dep_mode": n["dep_mode"],
                "kind": n["kind"], "ref_id": n["ref_id"],
                "control": n["control"], "max_runs": n["max_runs"],
                "timeout_s": n["timeout_s"],
            })
        dup = await self.store.add_pipeline(
            pipe["project_id"], f"{pipe['name']}（副本）",
            nodes=nodes, concurrency=pipe["concurrency"],
        )
        self._broadcast_pipeline(dup)
        return {"pipeline": dup}

    async def pipeline_export(self, params: dict) -> dict:
        """导出流水线为可分享的 JSON（依赖转为同批次序号，导入时重新物化）。"""
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None or pipe["project_id"] != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        by_id = {n["id"]: i for i, n in enumerate(pipe["nodes"])}
        nodes = []
        for n in pipe["nodes"]:
            nodes.append({
                "title": n["title"], "prompt": n["prompt"],
                "after": [by_id[d] for d in n["depends_on"] if d in by_id],
                "dep_mode": n["dep_mode"], "allowed_tools": n["allowed_tools"],
                "kind": n["kind"], "ref_id": n["ref_id"],
                "control": n["control"], "max_runs": n["max_runs"],
                "timeout_s": n["timeout_s"],
            })
        return {
            "export": {
                "format": "skysheep-pipeline", "version": 1,
                "name": pipe["name"], "concurrency": pipe["concurrency"],
                "nodes": nodes,
            }
        }

    async def pipeline_import(self, params: dict) -> dict:
        """从导出的 JSON 建一条草稿流水线。节点校验与 create 同一套；
        挂接/会话节点校验 ref 存在性，不存在则拒收该节点（不静默转 run）。"""
        raw = params.get("export")
        if not isinstance(raw, dict) or raw.get("format") != "skysheep-pipeline":
            raise RuntimeError("不是 SkySheep 流水线导出文件（format 不符）")
        name = str(raw.get("name") or "").strip() or "导入的流水线"
        raw_nodes = raw.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise RuntimeError("导出内容里没有节点")
        self._require_project("新建任务编排")  # 流水线绑定项目的工作目录
        nodes = await self._normalize_pipeline_nodes(raw_nodes)
        pipe = await self.store.add_pipeline(
            self.project.id, name, nodes=nodes,
            concurrency=int(raw.get("concurrency") or 2),
        )
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe}

    async def _normalize_pipeline_nodes(self, raw_nodes) -> list[dict]:
        """create / import 共用的节点解析与收敛：校验控制流类型、重试/迭代上限、
        指令非空（终止节点除外）、挂接与续跑的 ref 存在性。

        接受两种引用写法：task_id / session_id（前端表单）与 kind+ref_id
        （导入 JSON），产出同一套 store 节点字典（depends_on 是同批次序号）。
        """
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise RuntimeError("流水线至少要有一个节点")
        nodes = []
        for i, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                continue
            prompt = str(raw.get("prompt") or "").strip()
            task_id = str(raw.get("task_id") or raw.get("ref_id") or "").strip() \
                if str(raw.get("kind") or "") == "task" else str(raw.get("task_id") or "").strip()
            session_id = str(raw.get("session_id") or raw.get("ref_id") or "").strip() \
                if str(raw.get("kind") or "") == "session" else str(raw.get("session_id") or "").strip()
            control = str(raw.get("control") or "")
            if control not in ("", "gate", "stop", "loop"):
                raise RuntimeError(f"第 {i + 1} 个节点的类型不认识：{control}")
            max_runs = max(1, int(raw.get("max_runs") or 1))
            if control == "loop":
                max_runs = max(2, min(10, max_runs))
            elif control in ("gate", "stop"):
                max_runs = 1  # 门/终止都是跑一次定生死的控制节点，没有重试概念
            else:
                max_runs = max(1, min(4, max_runs))
            # 超时（秒）：0 = 不限时；上限一天。默认 3600（1 小时）防卡死
            timeout_raw = raw.get("timeout_s")
            timeout_s = self.NODE_TIMEOUT_DEFAULT_S if timeout_raw is None else max(0, int(timeout_raw))
            timeout_s = min(timeout_s, self.NODE_TIMEOUT_MAX_S)
            # 终止节点是纯控制流（不派 Agent），允许没有指令
            if not prompt and control != "stop" and not task_id and not session_id:
                raise RuntimeError(f"第 {i + 1} 个节点的指令不能为空")
            base = {
                "title": str(raw.get("title") or "").strip(),
                "prompt": prompt,
                # after = 同批次序号（0 起），由 store 物化成节点 id
                "depends_on": [int(d) for d in raw.get("after") or raw.get("depends_on") or []],
                "allowed_tools": [str(t).strip() for t in raw.get("allowed_tools") or []],
                "dep_mode": "any" if raw.get("dep_mode") == "any" else "all",
                "control": control, "max_runs": max_runs, "timeout_s": timeout_s,
            }
            if task_id:
                rec = self.tasks.status(task_id) if self.tasks else None
                if rec is None:
                    raise RuntimeError(f"第 {i + 1} 个节点：任务簿里找不到任务 {task_id}"
                                       "（挂接只对本机任务簿有效）")
                base.update({
                    "title": base["title"] or f"挂接任务 {rec.prompt[:40]}",
                    "kind": "task", "ref_id": task_id,
                    "control": "", "max_runs": 1,
                    "timeout_s": 0,  # 挂接节点不派跑（跟随原任务），无超时概念
                })
            elif session_id:
                sess = await self.store.get_session(session_id)
                if sess is None or sess.project_id != self._cur_project_id():
                    raise RuntimeError(f"第 {i + 1} 个节点的会话不存在或不属于当前项目")
                base.update({
                    "title": base["title"] or f"会话续跑：{(sess.title or session_id)[:40]}",
                    "kind": "session", "ref_id": session_id,
                    "control": "", "max_runs": 1,
                    # 会话续跑会真实派跑，超时照常生效（与 run 节点一致）
                    "timeout_s": timeout_s,
                })
            else:
                base["title"] = base["title"] or f"节点 {i + 1}"
            nodes.append(base)
        if not nodes:
            raise RuntimeError("流水线至少要有一个节点")
        return nodes

    async def pipeline_create(self, params: dict) -> dict:
        self._require_project("新建任务编排")  # 流水线绑定项目的工作目录
        name = str(params.get("name") or "").strip() or "未命名流水线"
        nodes = await self._normalize_pipeline_nodes(params.get("nodes"))
        pipe = await self.store.add_pipeline(
            self.project.id, name, nodes=nodes,
            concurrency=int(params.get("concurrency") or 2),
        )
        # 挂接节点建立即跟随任务当前状态（不等下一轮扫描对账）
        for n in pipe["nodes"]:
            if n["kind"] == "task" and n["status"] == "blocked":
                rec = self.tasks.status(n["ref_id"]) if self.tasks else None
                if rec is not None:
                    await self.store.update_pipeline_node(n["id"], **task_node_fields(rec))
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe}

    async def pipeline_attach(self, params: dict) -> dict:
        """把任务簿后台任务挂接为节点：状态与产出跟随原任务，不占流水线并发额度。

        正在跑的任务挂进来，下游节点就能「等它完成」——已在飞的任务由此进入编排。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        task_id = str(params.get("task_id") or "").strip()
        rec = self.tasks.status(task_id) if self.tasks else None
        if rec is None:
            raise RuntimeError("任务簿里找不到任务 " + (task_id or "（空）"))
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"挂接任务 {rec.prompt[:40]}",
            str(params.get("prompt") or ""),
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
            kind="task", ref_id=task_id,
        )
        node = await self.store.update_pipeline_node(node["id"], **task_node_fields(rec))
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node}

    async def pipeline_import_cron(self, params: dict) -> dict:
        """把定时任务的指令与预授权名单复制成 run 节点；原任务默认照常周期运行。"""
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        cron = await self.store.get_cron_task(int(params.get("cron_id", 0)))
        if cron is None or cron["project_id"] != self._cur_project_id():
            raise RuntimeError("定时任务不存在（或不属于当前项目）")
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"定时任务：{cron['name']}",
            str(params.get("prompt") or "").strip() or cron["prompt"],
            allowed_tools=cron["allowed_tools"],
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
        )
        if params.get("disable_source"):
            await self.store.update_cron_task(cron["id"], enabled=0, next_run_at=0)
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node,
                "source_disabled": bool(params.get("disable_source"))}

    async def pipeline_add_task(self, params: dict) -> dict:
        """直接写一条指令新建 run 节点：不必先有定时任务/任务簿/会话，就地排进流水线。

        与 add_session 同样无人值守：按节点预授权名单执行（新建时名单为空，只读放行，
        其余在流水线面板里逐节点补预授权）。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("新任务要写清要做什么（指令不能为空）")
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"新任务：{prompt[:40]}",
            prompt,
            allowed_tools=params.get("allowed_tools") or [],
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
        )
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node}

    async def pipeline_add_session(self, params: dict) -> dict:
        """把已有会话纳入流水线：节点在该会话里续跑（带其历史上下文）。

        会话节点参与依赖释放（等上游完成才续跑）；仍按节点预授权名单无人值守执行，
        不沿用该会话界面上选的权限档。会话正在跑对话时节点会标失败，空闲后可重跑。
        """
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        sid = str(params.get("session_id") or "").strip()
        sess = await self.store.get_session(sid)
        if sess is None or sess.project_id != pipe["project_id"]:
            raise RuntimeError("会话不存在或不属于本流水线的项目")
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("会话节点要给出这一轮要做什么（指令不能为空）")
        depends_on = params.get("depends_on")
        node = await self.store.add_pipeline_node(
            pipe["id"],
            str(params.get("title") or "").strip() or f"会话续跑：{(sess.title or sid)[:40]}",
            prompt,
            depends_on=[int(d) for d in depends_on] if isinstance(depends_on, list) else [],
            kind="session", ref_id=sid,
        )
        pipe = await self.store.get_pipeline(pipe["id"])
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "node": node}

    def _check_pipeline_ownership(self, pipe: dict) -> None:
        """流水线必须属于当前项目才能改/删/启停（同 _check_cron_ownership，防枚举）。"""
        if pipe.get("project_id") != self._cur_project_id():
            raise RuntimeError("流水线不存在: " + str(pipe.get("id")))

    async def pipeline_start(self, params: dict) -> dict:
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        if pipe["status"] == "running":
            raise RuntimeError("流水线已在运行中")
        if not pipe["nodes"]:
            raise RuntimeError("流水线没有节点，先编辑再加节点")
        if all(n["status"] == "done" for n in pipe["nodes"]):
            raise RuntimeError("全部节点都已完成；要重跑某个节点请用节点上的重跑按钮")
        # 上次被停止的节点退回等待，重新参与调度
        for n in pipe["nodes"]:
            if n["status"] == "cancelled":
                await self.store.update_pipeline_node(n["id"], status="blocked", last_error="")
        # 全部停在终态（done/error 混合）且没有可重置的停止节点：直接启动只会
        # 立刻再收尾一次（多发一遍完成通知），明确告诉用户先重跑失败节点
        if all(n["status"] in ("done", "error") for n in pipe["nodes"]):
            raise RuntimeError("没有可运行的节点——失败节点请先在详情里点「重跑」，或删除后重建流水线")
        pipe = await self.store.update_pipeline(pipe["id"], status="running", finished_at=0)
        self._broadcast_pipeline(pipe)
        spawn_bg(self._pipeline_pass())
        return {"pipeline": pipe, "started": True}

    async def pipeline_cancel(self, params: dict) -> dict:
        pipe = await self.store.get_pipeline(int(params.get("id", 0)))
        if pipe is None:
            raise RuntimeError("流水线不存在: " + str(params.get("id")))
        self._check_pipeline_ownership(pipe)
        if pipe["status"] != "running":
            raise RuntimeError("流水线不在运行中")
        pipe = await self.store.update_pipeline(pipe["id"], status="cancelled",
                                                finished_at=time.time())
        for n in pipe["nodes"]:
            if n["status"] in ("blocked", "ready"):
                await self.store.update_pipeline_node(
                    n["id"], status="cancelled", last_error="用户停止流水线",
                    finished_at=time.time(),
                )
            elif n["status"] == "running":
                t = self._pipeline_node_tasks.get(n["id"])
                if t is not None and not t.done():
                    t.cancel()  # 协程收尾会把节点标为 cancelled
        self._broadcast_pipeline(pipe)
        return {"pipeline": pipe, "cancelled": True}

    async def pipeline_delete(self, params: dict) -> dict:
        pid = int(params.get("id", 0))
        pipe = await self.store.get_pipeline(pid)
        if pipe is not None:
            self._check_pipeline_ownership(pipe)
            for n in pipe["nodes"]:
                if n["status"] == "running":
                    t = self._pipeline_node_tasks.get(n["id"])
                    if t is not None and not t.done():
                        t.cancel()
        ok = await self.store.delete_pipeline(pid)
        return {"deleted": ok, "id": pid}

    async def pipeline_node_rerun(self, params: dict) -> dict:
        """重跑一个节点：节点退回等待；因依赖失败而挂掉的下游一并退回等待。

        重跑已完成（done）节点时，下游的产出基于旧结果：默认不自动级联，
        返回 needs_confirm 让前端问一句；用户确认后 cascade=true 把全部
        传递下游（不含挂接节点）一并退回重跑，避免新旧产出混用。
        """
        node = await self.store.get_pipeline_node(int(params.get("id", 0)))
        if node is None:
            raise RuntimeError("节点不存在: " + str(params.get("id")))
        pipe = await self.store.get_pipeline(node["pipeline_id"])
        if pipe is None:
            raise RuntimeError("流水线不存在")
        self._check_pipeline_ownership(pipe)
        if node["kind"] == "task":
            raise RuntimeError("挂接节点跟随原任务，不能单独重跑；请在「任务」里重新派任务后再挂接")
        if node["status"] == "running":
            raise RuntimeError("节点正在运行，不能重跑")
        cascade = bool(params.get("cascade"))
        if node["status"] == "done" and not cascade:
            # 找全部传递下游（不含挂接节点——它们跟随原任务，不重跑）
            downstream = self._pipeline_downstream(pipe["nodes"], node["id"])
            live = [n for n in downstream if n["kind"] != "task"]
            if live:
                return {
                    "needs_confirm": True,
                    "downstream": [n["title"] for n in live],
                }
        await self.store.update_pipeline_node(
            node["id"], status="blocked", result="", last_error="", finished_at=0,
        )
        if cascade and node["status"] == "done":
            # 级联：全部传递下游退回重跑（产出已基于旧上游结果，继续用会失真）
            for n in self._pipeline_downstream(pipe["nodes"], node["id"]):
                if n["kind"] == "task" or n["id"] == node["id"]:
                    continue
                if n["status"] in ("done", "error", "skipped", "cancelled"):
                    await self.store.update_pipeline_node(
                        n["id"], status="blocked", result="", last_error="", finished_at=0,
                    )
        else:
            # 只回退「因依赖失败」的错误下游（DEP_FAIL_MARK 前缀）；自身跑挂的不动
            for n in pipe["nodes"]:
                if (n["id"] != node["id"] and n["status"] == "error"
                        and node["id"] in n["depends_on"]
                        and n["last_error"].startswith(self.DEP_FAIL_MARK)):
                    await self.store.update_pipeline_node(
                        n["id"], status="blocked", last_error="", finished_at=0,
                    )
        if pipe["status"] in ("failed", "cancelled", "done"):
            # done 也要拉回：重跑已完成节点（含级联）后，流水线必须重新参与调度
            pipe = await self.store.update_pipeline(pipe["id"], status="running", finished_at=0)
        self._broadcast_pipeline(pipe)
        if pipe["status"] == "running":
            spawn_bg(self._pipeline_pass())
        return {"pipeline": pipe}

    @staticmethod
    def _pipeline_downstream(nodes: list[dict], node_id: int) -> list[dict]:
        """全部传递下游（依赖 node_id 的节点，含间接），按 seq 排序去重。"""
        out, seen = [], set()
        frontier = [node_id]
        while frontier:
            cur = frontier.pop(0)
            for n in nodes:
                if cur in n["depends_on"] and n["id"] not in seen:
                    seen.add(n["id"])
                    out.append(n)
                    frontier.append(n["id"])
        out.sort(key=lambda n: n["seq"])
        return out

    # ---- 模型 ----

    def _build_provider(self, name: str, model: str | None = None) -> Provider:
        if self._provider_factory_override is not None:  # 测试/演示注入
            self.provider_name = name or "fake"
            p = self._provider_factory_override()
            self.provider_model = model or getattr(p, "model", "fake-1")
            return p
        if name not in self.cfg.providers:
            raise ConfigError(
                "unknown provider: {}; available: {}".format(name, ", ".join(self.cfg.providers))
            )
        pc = self.cfg.providers[name]
        if model:
            pc = pc.model_copy(update={"model": model})
        # 先构建成功再记录状态：构建失败（如缺 API Key）时不能改动"当前使用的模型"，
        # 否则 provider_name 指向新名字、provider 仍是旧对象，界面与实际调用会不一致。
        try:
            provider = build_provider(name, pc)
        except ConfigError as e:
            # ConfigError 文案自带出路（设置页指引）；CLI 场景的 config init 提示不适用于图形界面
            raise RuntimeError(str(e)) from e
        self.provider_name = name
        self.provider_model = pc.model
        return provider

    # ---- 系统提示词 / 会话 ----

    def compose_system(self) -> str:
        return self.compose_system_for(self.working_dir)

    def compose_system_for(self, workdir: Path | None) -> str:
        """无人值守跨项目运行（定时任务/流水线节点）按目标目录组装系统提示词。

        工作目录与项目约定（AGENTS.md）必须取目标目录的——否则提示词里写着
        A 目录、实际却在 B 目录干活，Agent 会找错地方；技能段沿用当前装载的
        SkillLoader（按目录重挂载过重），全局记忆本就跨项目。
        workdir 为 None 是无项目态：提示词里说明没有工作目录，快聊不可读写文件。
        """
        instr_file, instr_text = (
            load_project_instructions(workdir) if workdir is not None else (None, "")
        )
        return (
            build_system_prompt(workdir)
            + self.skills.render_prompt_section()
            + render_instructions_section(instr_file, instr_text)
            + render_memory_section()
        )

    async def new_session(self, title: str = "") -> dict:
        # 无项目态新建的会话归入快聊（project_id 为 NULL）：没有项目可归属。
        # title：前端在空标签上预命名时随创建一起落库（标签命名功能的懒创建路径），
        # 带了名字就记为手动命名，首轮的自动标题不会再覆盖它。
        title = (title or "").strip()[:80]  # 预命名同 rename 的限长口径
        self.session = await self.store.create_session(self._cur_project_id(), title)
        if title:
            self._manually_named.add(self.session.id)
        # 标记为「新会话」：若设了新会话默认模型，首次建 runtime 时用专用 provider
        self._default_model_sessions.add(self.session.id)
        self._get_runtime(self.session.id)  # 预建 runtime（自带系统提示词）
        return {"id": self.session.id, "title": self.session.title, "summary": ""}

    async def create_task_chat(self) -> dict:
        """新建一个不绑定任何文件夹的「任务」会话（侧栏「任务」分组的 ＋）。

        只落库、不切换当前会话——前端拿到 id 自己打开（走普通会话激活路径）。
        """
        s = await self.store.create_session(None)
        return {"id": s.id, "title": s.title}

    async def open_initial_session(self) -> dict | None:
        """启动时接着上次的会话继续；完全没历史则不创建（懒创建：发第一条消息时才落库），
        避免每次启动都堆积空会话。无项目态接快聊最近的会话（latest_session(None)
        的语义就是 project_id IS NULL）。

        恢复目标优先 ui.json 的 session_active（上次激活的标签）；没有/失效才
        落到「最近会话」。标签列表（session_tabs）由前端按 snapshot 的 open_tabs
        恢复——后端只负责把活动指针指对，不代拉历史。"""
        prefs = self._read_ui_prefs()
        remembered = prefs.get("session_active")
        if isinstance(remembered, str) and remembered:
            try:
                return await self.resume_session(remembered)
            except Exception:
                pass  # 已删/跨项目：静默落回最近会话
        latest = await self.store.latest_session(self._cur_project_id())
        if latest is not None:
            return await self.resume_session(latest.id)
        self.session = None
        return None

    async def cleanup_empty_sessions(self) -> dict:
        """删除本项目下没有任何消息的空会话（保留当前会话与置顶会话）。

        无项目态清的是快聊的空会话（delete_empty_sessions(None) 的口径）。"""
        keep = self.session.id if self.session else None
        removed = await self.store.delete_empty_sessions(self._cur_project_id(), keep_id=keep)
        return {"removed": removed}

    async def _get_owned_session(self, session_id: str):
        """取属于当前项目的会话；不存在或属于其他项目一律报错。

        安全边界（安全审查 B 族）：store.get_session 只按 id 查询，所有
        按会话 id 的远程操作（chat.send/refs/export/delete/元数据…）必须
        先过这里，否则别的项目的会话会被挂进当前项目的工作目录与权限门下。
        报错不区分「不存在/无权」，避免给枚举探测提供区分信号。

        快聊会话（project_id IS NULL）不属于任何项目，却出现在侧栏的常驻
        「快聊」分组里，用户理应能像普通会话一样点开与删除：只按当前项目
        校验会让它们全部报「session not found」（列表看得见、点不动）。
        这里显式承认快聊的开放归属——它不带任何项目上下文，挂进当前项目的
        工作目录不构成跨项目越权；其他项目的会话仍然照旧拒绝。
        """
        pid = self.project.id if self.project is not None else None
        sess = await self.store.get_session_for_project(session_id, pid)
        if sess is None:
            # 快聊会话在项目查询下必然为 None：再确认它确实是无项目会话，
            # 而不是「属于别的项目」。两者放行的只有前者。
            sess = await self.store.get_session_for_project(session_id, None)
        if sess is None:
            # 「远程连接」项目的会话同理开放归属：渠道对话在侧栏可见可点，
            # 但它不属于任何桌面工作目录，挂进当前项目的门控不构成跨项目越权
            # （渠道会话的 runtime 自带 ChannelGate，不走这里的默认门控）。
            remote = await self._remote_project_id()
            if remote is not None:
                sess = await self.store.get_session_for_project(session_id, remote)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        return sess

    async def truncate_session(self, params: dict) -> dict:
        """消息级回退：为「重新生成 / 编辑重发」截断历史。

        mode=regen  ：删掉锚点（默认最后一条 user）之后的所有消息，保留用户消息；
        mode=edit   ：连锚点消息一起删（随后用户编辑后重发）。
        会话正在运行时拒绝（避免与进行中的 turn 互相踩踏）。
        """
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        if not sid:
            raise RuntimeError("missing session id")
        rt = self.runtimes.get(sid)
        if rt and rt.run_task and not rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再操作")
        await self._get_owned_session(sid)

        seq = params.get("seq")
        mode = str(params.get("mode", "regen"))
        if seq is not None:
            pivot = int(seq)
        else:
            pivot = await self.store.find_last_user_seq(sid)
            if pivot is None:
                raise RuntimeError("会话里没有可回退的用户消息")
        # 校验锚点是 user 消息（regen 的语义是「重跑这条用户消息」）
        anchor = await self.store.get_message_at(sid, pivot)
        if anchor is None:
            raise RuntimeError("锚点消息不存在")
        if mode == "regen" and anchor["role"] != "user":
            # 自动回退到它前面最近的 user 消息
            rows = await self.store.search_messages_by_seq(sid, pivot, role="user")
            if rows is None:
                raise RuntimeError("锚点之前没有用户消息")
            pivot = rows
            anchor = await self.store.get_message_at(sid, pivot)

        include = mode == "edit"
        deleted = await self.store.truncate_from(sid, pivot, include_self=include)
        # 同步 runtime 内的历史（存在则从存储重载）
        if sid in self.runtimes:
            await self._reload_agent_history(self.runtimes[sid].agent, sid)
        # 带 id：WS 层按返回里的 id 跟踪连接当前交互的会话（会话不变，仍是它）
        out = {"id": sid, "deleted": deleted, "pivot_seq": pivot, "mode": mode}
        if mode == "edit":
            out["text"] = anchor["text"]
        return out

    async def fork_session(self, params: dict) -> dict:
        """从某条消息分叉出新会话：复制 seq <= 锚点 的消息（缺省全部）。"""
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        sess = await self._get_owned_session(sid)
        # 与 truncate 同一守卫：轮末才批量落库，运行中分叉会复制到不完整的历史
        rt = self.runtimes.get(sid)
        if rt and rt.run_task and not rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再分叉")
        seq = int(params["seq"]) if params.get("seq") is not None else (
            await self.store.max_seq(sid) or 0)
        new_sess = await self.store.create_session(
            self._cur_project_id(), title=(f"└ {sess.title or '分叉'}")[:40]
        )
        n = await self.store.copy_messages_between(sid, new_sess.id, seq)
        await self.store.touch(new_sess.id)
        msgs = await self.store.load_messages(new_sess.id)
        return {
            "id": new_sess.id,
            "title": new_sess.title,
            "copied": n,
            "messages": [_msg_brief(m) for m in msgs if m.role in ("user", "assistant")],
        }

    async def activate_session(self, session_id: str) -> dict:
        """只切活动指针，不重载历史（标签切换用；runtime 已存在时不做任何重活）。"""
        sess = await self._get_owned_session(session_id)
        self.session = sess
        # 启动恢复：记住当前激活的标签，下次启动回到它。失败静默（偏好写不进
        # 不影响切换本身）。
        try:
            await self._write_ui_prefs({self.SESSION_ACTIVE_KEY: session_id})
        except Exception:
            pass
        if session_id not in self.runtimes:
            rt = self._get_runtime(session_id)
            await self._reload_agent_history(rt.agent, session_id)
        return {"id": sess.id, "title": sess.title}

    async def resume_session(self, session_id: str) -> dict:
        sess = await self._get_owned_session(session_id)
        self.session = sess
        try:
            await self._write_ui_prefs({self.SESSION_ACTIVE_KEY: session_id})
        except Exception:
            pass
        rt = self._get_runtime(session_id)
        msgs = await self._reload_agent_history(rt.agent, session_id)
        return {
            "id": sess.id, "title": sess.title, "summary": sess.summary,
            "messages": [_msg_brief(m) for m in msgs if m.role in ("user", "assistant")],
        }

    async def session_image(self, params: dict) -> dict:
        """按 (会话, seq, 图片序号) 取一张历史图片的 base64。

        历史消息里的图片只下发占位（见 _msg_brief），前端滚到可见时才来取——
        刷新/切会话不再为整段历史里的原图付几十 MB 的传输与内存。
        """
        sid = str(params.get("session_id", "") or "")
        seq = int(params.get("seq", 0) or 0)
        index = int(params.get("index", 0) or 0)
        await self._get_owned_session(sid)  # 归属校验：跨项目会话按不存在拒绝
        m = await self.store.get_message_at(sid, seq)
        if m is None:
            raise RuntimeError("消息不存在")
        imgs = [b for b in m.content if getattr(b, "type", "") == "image"]
        if index < 0 or index >= len(imgs):
            raise RuntimeError("图片不存在")
        b = imgs[index]
        return {"media_type": b.media_type, "data": b.data}

    # ---- 对话主流程 ----

    # 图片附件限制：类型白名单、单条最多 4 张、单张 base64 ≤ 6M 字符（约 4.5MB 原图）
    IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
    MAX_IMAGES_PER_TURN = 4
    MAX_IMAGE_B64_CHARS = 6_000_000

    def _sanitize_images(self, images) -> list[ImageBlock]:
        """清洗前端传来的图片附件：类型/大小/数量白名单，不合规静默丢弃。"""
        out: list[ImageBlock] = []
        for im in (images or [])[: self.MAX_IMAGES_PER_TURN]:
            if not isinstance(im, dict):
                continue
            mt = str(im.get("media_type", ""))
            data = str(im.get("data", ""))
            if mt not in self.IMAGE_MEDIA_TYPES or not data or len(data) > self.MAX_IMAGE_B64_CHARS:
                continue
            out.append(ImageBlock(media_type=mt, data=data))
        return out

    # ---- 「& 引用对话」：会话引用的清洗与上下文拼装 ----

    REF_MAX_SESSIONS = 3      # 单轮最多引用的对话数
    REF_MAX_CHARS = 4_000     # 每个被引用对话注入的最大字符数（保尾留新，最近的对话最相关）

    def _sanitize_refs(self, refs: list[str] | None, exclude: str | None) -> list[str]:
        """清洗引用清单：去重、剔除目标会话自己、截断数量上限。"""
        out: list[str] = []
        for r in refs or []:
            rid = str(r)
            if not rid or rid == exclude or rid in out:
                continue
            out.append(rid)
            if len(out) >= self.REF_MAX_SESSIONS:
                break
        return out

    async def _recent_turn_seconds(self, limit: int = 30) -> list[float]:
        """本项目近期实测轮耗时（秒），任务耗时预估的历史校准样本。

        store 不可用（纯测试环境等）时返回空列表，预估退化为纯启发式。
        """
        if self.store is None or self.project is None:
            return []
        try:
            return await self.store.recent_turn_seconds(self.project.id, limit=limit)
        except Exception:  # noqa: BLE001 - 校准样本拿不到不该挡住发消息
            return []

    async def _build_refs_context(self, refs: list[str]) -> str:
        """把被引用会话的记录拼成注入本轮的上下文块。

        会话不存在、不属于当前项目或没有可读消息时静默跳过；超长的保留尾部
        （最近的对话与当前任务最相关），并标注省略。格式与 /export 的导出文本
        一致。归属校验（安全审查 B2）：引用列表由客户端提交，不校验会把
        别的项目的历史注入当前 Agent 上下文。
        """
        blocks: list[str] = []
        for rid in refs:
            if await self.store.get_session_for_project(rid, self._cur_project_id()) is None:
                continue  # 不属于本项目的会话：当作不存在，不注入
            title = await self.store.get_session_title(rid)
            try:
                msgs = await self.store.load_messages(rid)
            except Exception:  # noqa: BLE001 - 引用的会话读不到就不注入，别挡住正常提问
                continue
            text = export_messages_text(msgs).strip()
            if not text:
                continue
            if len(text) > self.REF_MAX_CHARS:
                text = "（前文过长，此处省略——以下是对话的最近部分）\n" + text[-self.REF_MAX_CHARS:]
            blocks.append(f"──── 引用对话「{title or '未命名会话'}」 ────\n{text}")
        if not blocks:
            return ""
        return (
            "[引用对话] 用户在本轮点名引用了下面的历史对话记录，回答时可参考其中内容：\n\n"
            + "\n\n".join(blocks)
            + "\n\n[引用对话结束]\n\n"
        )

    async def send(self, text: str, emit: EmitFn, plan_mode: bool = False,
                   roundtable: bool = False, members: list | None = None,
                   images: list[dict] | None = None,
                   session_id: str | None = None,
                   wants_title: bool = False,
                   regenerate: bool = False,
                   compare: bool = False,
                   refs: list[str] | None = None,
                   debate_rounds: int | None = None,
                   chair_answers: bool | None = None) -> dict:
        """跑一轮对话；过程事件通过 emit 推送；结束后持久化新消息。

        Agent 正在工作时再次 send 不再报错，而是**排队**：等当前轮结束后
        自动依次执行（对标 Claude Code 的消息队列），fut 在该轮真正跑完时
        才返回结果。

        plan_mode=True 时本轮切换为只读工具集并附加规划指令（对标 Claude Code
        计划模式）：Agent 只做只读调研并输出实施计划，不改动任何文件。

        roundtable=True 时本轮走「圆桌」流程：多个模型并行独立作答，
        主席（当前主模型）融合成最终答案；members 为显式成员列表
        [{provider, model}]，缺省时自动选取（见 _resolve_members）。
        debate_rounds / chair_answers 为本轮覆盖值（None=用 config.toml 的
        [roundtable] 配置）：前者是额外辩论修订轮数（0-2），后者控制主席
        是否也出一份草稿。

        images 为用户消息携带的图片附件 [{media_type, data(base64)}]，
        只在普通轮生效（圆桌轮是纯文本协作，忽略图片）。

        refs 为「& 引用对话」选中的会话 id 列表：每轮最多 REF_MAX_SESSIONS
        个，会话记录会被注入本轮上下文（见 _build_refs_context）。

        session_id 指定目标会话（多会话并行时前端按标签传入）；缺省用活动会话。
        目标会话不是当前活动会话时先轻量激活（运行中的其他会话不受影响）。
        """
        # 轮次起点重验工作区信任（审查 P2-4）：会话运行期间项目级配置被外部
        # 改动（git pull 等）时，趁本轮开始断开项目级 MCP、重发现技能。
        await self.recheck_trust_before_turn()
        if session_id and (not self.session or self.session.id != session_id):
            await self.activate_session(session_id)
        # 引用排除目标会话自己：引用当前对话没有意义
        clean_refs = self._sanitize_refs(
            refs, exclude=self.session.id if self.session else None
        )
        if self.provider is None:
            raise RuntimeError(
                "尚未配置可用的模型 API Key——"
                "打开 ⚙ 设置 · 模型服务，选一个服务粘贴 API Key 即可开始对话。"
            )
        # 每日 token 预算护栏（设置 · 高级，默认 0 = 不限制）：超限即停，
        # 给可读的出路而不是让费用悄悄滚大。演示模式不受限（不产生真实费用）。
        if self.cfg.daily_token_budget > 0 and not getattr(self.provider, "demo_mode", False):
            used_today = await self.store.usage_today()
            if used_today >= self.cfg.daily_token_budget:
                raise RuntimeError(
                    f"已触发每日 token 预算护栏：今天已用约 {used_today:,} tokens，"
                    f"达到上限 {self.cfg.daily_token_budget:,}。\n"
                    "如需继续，请在 设置 · 高级 里调高或关闭「每日 token 预算」"
                    "（明天自动重置）。"
                )
        # 图片附件：当前服务声明"不支持图片输入"时提前给可读提示，
        # 而不是把图片塞给纯文本模型，换回一句上游报错（用户不知道是模型选错了）。
        clean_images = self._sanitize_images(images)
        if clean_images and not self._supports_vision():
            name = self.provider_name or "当前服务"
            raise RuntimeError(
                f"「{name} / {self.provider_model}」不支持图片输入，图片发不出去。\n"
                "请在输入框的模型选择器里换一个多模态模型（如 GLM-4V、Kimi、GPT-4o 等），"
                "或在 设置 · 模型服务 里把该服务的「支持图片输入」打开。"
            )
        # 在任何 await 之前先占住运行位：否则两条背靠背到达的消息会在
        # new_session() 的挂起点上双双判为空闲、并发执行（撞消息表唯一约束）。
        # 占位判断同时看基底位（会话懒创建窗口）与活动 runtime 位；后来者排队。
        cur = asyncio.current_task()
        rt_now = self.runtimes.get(self.session.id) if self.session else None
        holder = rt_now.run_task if rt_now else self._base_run_task
        if holder is not None and not holder.done() and holder is not cur:
            loop = asyncio.get_running_loop()
            item = QueuedTurn(
                text=text, emit=emit, plan_mode=plan_mode, fut=loop.create_future(),
                roundtable=roundtable, members=members,
                images=self._sanitize_images(images),
                refs=clean_refs, compare=compare,
                debate_rounds=debate_rounds, chair_answers=chair_answers,
            )
            if rt_now is not None:
                rt_now.queue.append(item)
                await emit(QueueUpdated(pending=len(rt_now.queue)).model_dump())
            else:
                self._base_queue.append(item)
                await emit(QueueUpdated(pending=len(self._base_queue)).model_dump())
            return await item.fut
        self._base_run_task = cur  # 抢占基底位，掩护随后的懒创建 await 窗口
        if self.session is None:
            await self.new_session()  # 懒创建：第一条消息才落库
        runtime = self._get_runtime(self.session.id)
        runtime.run_task = cur
        self._base_run_task = None  # 运行位已落到 runtime，基底占位清除
        try:
            return await self._run_turn_pipeline(
                text, emit, plan_mode, roundtable=roundtable, members_params=members,
                images=clean_images, runtime=runtime,
                session_id=self.session.id, wants_title=wants_title,
                regenerate=regenerate, compare=compare, refs=clean_refs,
                debate_rounds=debate_rounds, chair_answers=chair_answers,
            )
        except asyncio.CancelledError:
            # 用户在 turn 真正开始前点了停止：无产出，返回诚实的 stopped 结果，
            # 而不是让任务以异常结束（run_task 已经释放，后续消息不会进死队列）。
            if runtime.run_task is asyncio.current_task():
                runtime.run_task = None
            if self._base_run_task is asyncio.current_task():
                self._base_run_task = None
            return {
                "done": True,
                "stopped": True,
                "session_id": runtime.sid,
                "plan_mode": plan_mode,
                "roundtable": False,
                "context_tokens": runtime.agent.used_context_tokens(),
                "context_limit": runtime.agent.context_limit_tokens,
                "context_detail": self._context_detail(runtime.agent),
            }
        except BaseException:
            # pipeline 启动阶段就抛出（还没走到它自己的标志管理）时把位让出来，
            # 否则后续所有消息都会误入永远无人消费的队列。
            if runtime.run_task is asyncio.current_task():
                runtime.run_task = None
            if self._base_run_task is asyncio.current_task():
                self._base_run_task = None
            raise

    async def _auto_title(self, sid: str, first_user: str, first_reply: str) -> None:
        """首轮结束后用当前模型生成简短标题；失败保持原状，绝不影响主流程。"""
        if getattr(self.provider, "demo_mode", False):
            return  # 演示模式不额外消耗脚本组，标题保持首行截断即可
        if sid in self._titling:
            return
        if sid in self._manually_named:
            return  # 用户手改过名字：自动标题不覆盖，尊重命名
        self._titling.add(sid)
        try:
            cur_title = await self.store.get_session_title(sid)
            seed = f"用户：{first_user[:400]}"
            if first_reply:
                seed += f"\n助手：{first_reply[:300]}"
            prompt = (
                "给下面的对话起一个不超过12个字的中文标题，概括主题。"
                "只输出标题本身，不要引号、句号或任何解释。\n\n" + seed
            )
            parts: list[str] = []
            async for ev in self.provider.stream([Message.user(prompt)], []):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            raw = "".join(parts).strip()
            title = raw.splitlines()[0].strip(' \t"“”「」』【】。，；：')[:24] if raw else ""
            if not title or title == cur_title:
                return
            await self.store.set_title(sid, title)
            for ws_emit in list(self.ws_emitters):
                try:
                    await ws_emit({"kind": "session_updated",
                                   "session_id": sid, "title": title})
                except Exception:
                    pass
        except Exception:
            pass  # 标题生成失败完全静默
        finally:
            self._titling.discard(sid)

    async def _reload_agent_history(
        self, agent, sid: str, system: str | None = None
    ) -> list[Message]:
        """从存储重载会话历史，并保证系统提示词仍在最前面；返回重载到的消息。

        system 消息从不落库——它由 compose_system() 按当前技能/项目/工具实时生成，
        落库等于把过期提示词固化下来。所以 load_history(msgs) 之后必须补回：
        少了这一步，重载过的会话下一轮会带着「没有系统提示词」的历史去调模型，
        Agent 的工具用法约定、项目上下文、技能清单全部丢失。旧写法
        `load_history(msgs or [Message.system(...)])` 只兜住了空会话，非空会话
        （也就是真正需要重载的那些）正好漏掉。
        """
        msgs = await self.store.load_messages(sid)
        agent.load_history(msgs)
        agent.set_system(system if system is not None else self.compose_system())
        return msgs

    async def _persist_turn(self, sid: str, new_msgs: list) -> None:
        """一轮对话的落库（独立方法便于 shield 保护：取消时后台完成落库）。"""
        for m in new_msgs:
            await self.store.append_message(sid, m)
        await self.store.touch(sid)
        summary = next(
            (m.text.strip().replace("\n", " ")[:120]
             for m in reversed(new_msgs) if m.role == "assistant" and m.text.strip()),
            "",
        )
        if summary:
            await self.store.set_summary(sid, summary)

    async def _run_turn_pipeline(
        self, text: str, emit: EmitFn, plan_mode: bool,
        roundtable: bool = False, members_params: list | None = None,
        images: list[ImageBlock] | None = None,
        runtime: SessionRuntime | None = None,
        session_id: str | None = None,
        wants_title: bool = False,
        regenerate: bool = False,
        compare: bool = False,
        refs: list[str] | None = None,
        debate_rounds: int | None = None,
        chair_answers: bool | None = None,
    ) -> dict:
        """真正执行一轮对话（含持久化、规划模式切换、检查点保存）。

        多会话并行：全程使用 runtime 内的 agent/queue/recorder，
        不碰 self.agent 等活动会话指针；事件发出时注入 session_id 供前端路由。
        """
        if runtime is None:
            if self.session is None:
                await self.new_session()  # 懒创建：第一条消息才落库
            runtime = self._get_runtime(self.session.id)
        sid = session_id or runtime.sid
        agent = runtime.agent

        # 流式增量合并：emit_ev 下发给前端前先过一层缓冲，把连续的同类型增量
        # （text_delta / thinking_delta / roundtable_member_delta）拼成批量帧。
        # 其余事件在 merger 里会先冲刷缓冲再原样发出，顺序与合并前一致。
        merger = StreamDeltaMerger(emit)

        # 本轮工具/权限的耗时统计（给轮末结构化日志用）。
        # 从事件流里数而不是侵入 agent 循环：事件本就带 duration_ms，
        # 这里只做累加，不在热路径上加任何计算。
        turn_stats: dict[str, int] = {
            "tool_calls": 0, "tool_ms": 0, "tool_errors": 0,
            "permission_waits": 0, "permission_ms": 0,
        }
        _perm_pending: dict[str, float] = {}

        async def emit_ev(ev: dict) -> None:
            kind = ev.get("kind")
            if kind == "tool_call_finished":
                turn_stats["tool_calls"] += 1
                turn_stats["tool_ms"] += int(ev.get("duration_ms") or 0)
                if ev.get("is_error"):
                    turn_stats["tool_errors"] += 1
            elif kind == "permission_request":
                rid = ev.get("request_id") or ""
                if rid:
                    _perm_pending[rid] = time.monotonic()
            elif kind == "permission_resolved":
                rid = ev.get("request_id") or ""
                t0 = _perm_pending.pop(rid, None)
                if t0 is not None:
                    turn_stats["permission_waits"] += 1
                    turn_stats["permission_ms"] += int((time.monotonic() - t0) * 1000)
            await merger.send({**ev, "session_id": sid})

        # 「用户消息」广播给其余在线客户端：轮次事件只发给发起连接，其他窗口 /
        # 手机端此前既看不到用户消息也没有任何「有人发了话」的来源。发起连接的
        # 前端已在 send() 里本地渲染过气泡，按对象身份排除，避免重复渲染。
        # 重新生成（text 为空串）不广播。
        others = [w for w in list(self.ws_emitters) if w is not emit]
        if others and (text or images):
            u_ev = {
                "kind": "user_message", "session_id": sid, "text": text,
                "images": [
                    {"media_type": b.media_type, "data": b.data}
                    for b in (images or []) if getattr(b, "type", "") == "image"
                ],
            }
            for ws_emit in others:
                try:
                    spawn_bg(ws_emit(u_ev))
                except RuntimeError:
                    continue  # 无事件循环（如纯测试环境）

        sess_title = await self.store.get_session_title(sid)
        if not sess_title and not regenerate:
            sess_title = text[:40] or ("[图片]" if images else "")
            await self.store.set_title(sid, sess_title)

        # 任务耗时预估：接手任务时按启发式 + 本项目近期实测给出预计区间，
        # 先于本轮任何输出事件到达，前端显示「预计 X~Y 分钟」并对照已用时。
        # 重新生成轮（text 为空）不算接手新任务，不发。
        # turn_estimate 随轮末消息一起落库：刷新/重进会话后「用时 X · 预估 Y」
        # 芯片仍能重建（旧消息没有该字段时前端不渲染）。
        turn_estimate: dict | None = None
        if text or images:
            est = estimate_task(
                text,
                history=agent.history,
                images=len(images or []),
                members=len(members_params or []) if roundtable else 0,
                debate_rounds=(debate_rounds or 0) if roundtable else 0,
                recent=await self._recent_turn_seconds(),
            )
            turn_estimate = {
                "min_seconds": est.min_seconds,
                "max_seconds": est.max_seconds,
                "level": est.level,
                "basis": est.basis,
            }
            await emit_ev(TaskEstimate(
                min_seconds=est.min_seconds,
                max_seconds=est.max_seconds,
                level=est.level,
                basis=est.basis,
            ).model_dump())

        readonly_registry = None
        if plan_mode:
            from ..tools.base import Safety

            readonly_registry = ToolRegistry(
                [t for t in agent.registry.all() if t.safety == Safety.READONLY]
            )
            agent.registry = readonly_registry
        # 「& 引用对话」：把被引用会话的记录拼在消息最前面注入本轮上下文
        #（与 PLAN_MODE_PREFIX 同一套做法，随用户消息一起持久化）
        if refs:
            refs_ctx = await self._build_refs_context(refs)
            if refs_ctx:
                text = refs_ctx + text
        if plan_mode:
            text = PLAN_MODE_PREFIX + text
        # 自上一轮以来结束、还没人取报告的后台子代理任务 → 注入一条系统提示，
        # 主 Agent 开轮就知道「有任务做完了」，不用用户来催（后台模式闭环）
        if self.tasks:
            bg_note = self.tasks.pop_turn_note(sid)
            if bg_note:
                text = bg_note + text

        # 轮末落库靠「消息身份」挑出本轮新增，不靠下标切片：compact_history 会在
        # 本轮内把 history 整体换成更短的新列表（core/context.py），旧下标随即失效：
        # 新长度 ≤ n_before 时 `history[n_before:]` 为空（本轮用户消息、工具调用、
        # 回答全部不落库，重启即丢），略大时又会把早已入库的旧消息重复插入。
        # 消息 id 由 Message 默认工厂生成、随序列化保留，压缩后 recent 段仍是原对象
        # （id 不变），所以「不在轮前 id 集合里」正好等于「本轮新增」。
        pre_ids = {m.id for m in agent.history}
        # 本轮墙钟起点：圆桌路径没有 agent 主循环计时，这里兜底（见 _stamp_turn_estimate）
        turn_t0 = time.monotonic()
        stopped = False
        rt_meta: dict | None = None
        tin0, tout0 = agent.total_in_tokens, agent.total_out_tokens
        tcached0 = agent.total_cached_tokens
        runtime.run_task = asyncio.current_task()
        turn_exc: BaseException | None = None
        try:
            if roundtable:
                rt_meta = await self._roundtable_body(
                    text, emit_ev, members_params, agent=agent, compare=compare,
                    sid=sid, images=images,
                    debate_rounds=debate_rounds, chair_answers=chair_answers,
                    regenerate=regenerate,
                )
                # 圆桌的取消在引擎层收敛（保留已产出的部分文本），这里同步停止位
                if (rt_meta or {}).get("status") == "cancelled":
                    stopped = True
            else:
                async for ev in agent.run_turn(text, images=images, append_user=not regenerate):
                    await emit_ev(ev.model_dump())
                    if ev.kind == "permission_request":
                        await self.store.touch(sid)
        except asyncio.CancelledError:
            stopped = True
        except Exception as e:  # noqa: BLE001
            # 意外错误也要走完整收尾（落库/用量/队列交棒/释放运行位），
            # 否则 runtime.run_task 悬挂、后续消息永远排队。收尾后原样上抛，
            # 让 WS 层返回 ok=false 的诚实错误。
            turn_exc = e
        finally:
            # 先冲刷流式增量缓冲：取消/异常/正常结束三条路径都要走到，
            # 否则最后几十毫秒的正文会丢在前端（用户看到回答缺尾）。
            # 放在恢复工具集之前，保证 flush 出去的事件仍带本轮 session_id 顺序。
            try:
                await merger.aclose()
            except Exception:  # noqa: BLE001 - 客户端断开不影响收尾
                pass
            if plan_mode and readonly_registry is not None:
                # 恢复完整工具集（只读注册表只在本轮生效）
                agent.registry = self._build_full_registry(runtime.recorder)

        # ---- 收尾必须严格串行：先落库，再交棒给排队轮，否则两条持久化
        # ---- 并发会撞 messages(session_id, seq) 唯一约束。
        # 取消保护：用户点停止时，已产出的消息仍要落库、运行位必须释放；
        # shield 让落库在后台继续，CancelledError 被捕获后正常走完收尾返回 stopped 结果，
        # 避免 send() 抛异常导致 runtime.run_task 悬挂、后续消息误入死队列。
        # 压缩摘要不落库：它是引擎生成的上下文产物，入库会被当成一条 user 消息
        # 回传前端（渲染成假的用户气泡），也会进 FTS 与会话导出；不落库的代价
        # 只是重启后长会话重新压缩一次，比上面两类污染便宜得多。
        new_msgs = [
            m for m in agent.history
            if m.id not in pre_ids and not is_compaction_summary(m)
        ]
        _stamp_turn_estimate(new_msgs, turn_estimate, turn_t0)
        try:
            await asyncio.shield(self._persist_turn(sid, new_msgs))
        except asyncio.CancelledError:
            stopped = True
            asyncio.current_task().uncancel()

        # 用量记录：本轮实际消耗的输入/输出 tokens（含缓存命中数，费用按缓存价拆算）
        try:
            await self.store.add_usage(
                sid, self.provider_name, self.provider_model,
                agent.total_in_tokens - tin0, agent.total_out_tokens - tout0,
                agent.total_cached_tokens - tcached0,
            )
        except Exception:
            pass

        # 结构化耗时记录：回答「这轮为什么慢」
        # （总耗时 / 工具次数与总耗时 / 权限等待——这三项最容易看出卡在哪一段）。
        try:
            obs_info(
                "turn",
                f"turn finished sid={sid}",
                session_id=sid,
                duration_ms=int((time.monotonic() - turn_t0) * 1000),
                stopped=stopped or None,
                roundtable=bool(rt_meta) or None,
                tool_calls=turn_stats["tool_calls"] or None,
                tool_ms=turn_stats["tool_ms"] or None,
                tool_errors=turn_stats["tool_errors"] or None,
                permission_waits=turn_stats["permission_waits"] or None,
                permission_ms=turn_stats["permission_ms"] or None,
                in_tokens=agent.total_in_tokens - tin0 or None,
                out_tokens=agent.total_out_tokens - tout0 or None,
                error=type(turn_exc).__name__ if turn_exc else None,
            )
        except Exception:  # noqa: BLE001 - 日志不偿能影响主流程
            pass

        # 首轮对话：后台用模型生成简短标题（8-12 字），替换首行截断文本
        if wants_title and self.provider is not None and sid not in self._titling:
            first_user = text
            first_reply = next(
                (m.text.strip() for m in new_msgs if m.role == "assistant" and m.text.strip()), ""
            )
            if first_user:
                spawn_bg(
                    self._auto_title(sid, first_user, first_reply)
                )

        # 本轮改动了文件 → 存检查点（落盘 + 内存索引）；结果里带给前端做「撤销本轮改动」
        checkpoint = await asyncio.to_thread(
            self.checkpoints.save, sid, dict(runtime.recorder.pre)
        )
        runtime.recorder.reset()

        def _pop_next() -> bool:
            """启动下一个排队轮，把"运行中"的接力棒递给它；有交棒返回 True。"""
            if self._base_queue:
                # 会话懒创建期间排进基底队列的消息，并入本会话队列
                runtime.queue.extend(self._base_queue)
                self._base_queue.clear()
            if not runtime.queue:
                return False
            nxt = runtime.queue.pop(0)
            runtime.run_task = asyncio.get_running_loop().create_task(
                self._run_queued(nxt, runtime)
            )
            return True

        # queue_updated 是前端运行状态的唯一事实源：
        # pending = 仍排队轮数 +（1 如果刚接棒了一轮），归零才真正空闲
        handed_off = _pop_next()
        try:
            await emit_ev(
                QueueUpdated(pending=len(runtime.queue) + (1 if handed_off else 0)).model_dump()
            )
        except Exception:  # noqa: BLE001 - 客户端断开不影响收尾
            pass

        result = {
            "done": True,
            "stopped": stopped,
            "session_id": sid,
            "plan_mode": plan_mode,
            "roundtable": rt_meta,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
            "context_detail": self._context_detail(agent),
        }
        if checkpoint is not None:
            result["checkpoint"] = checkpoint
        # 没有交棒（无排队轮）才释放运行标志
        if runtime.run_task is asyncio.current_task():
            runtime.run_task = None
        # 兜底：交棒之后、释放标志前后又进来了排队请求 → 这里再拉起一次并广播
        if runtime.queue and (runtime.run_task is None or runtime.run_task.done()):
            handed_off2 = _pop_next()
            try:
                await emit_ev(
                    QueueUpdated(
                        pending=len(runtime.queue) + (1 if handed_off2 else 0)
                    ).model_dump()
                )
            except Exception:  # noqa: BLE001
                pass
        if turn_exc is not None:
            raise turn_exc  # 收尾已做完，原样上抛交给 WS 层
        return result

    async def _run_queued(self, item: QueuedTurn, runtime: SessionRuntime) -> None:
        # 状态由 _run_turn_pipeline finally 里的 queue_updated 统一播报，这里不重复发
        try:
            item.resolve(await self._run_turn_pipeline(
                item.text, item.emit, item.plan_mode,
                roundtable=item.roundtable, members_params=item.members,
                images=item.images, runtime=runtime, refs=item.refs,
                compare=item.compare, debate_rounds=item.debate_rounds,
                chair_answers=item.chair_answers,
            ))
        except Exception as e:  # noqa: BLE001 - 错误要送回等待中的请求
            item.fail(e)

    def cancel_run(self, session_id: str | None = None) -> bool:
        """停止指定会话（缺省 = 活动会话）正在跑的 turn。"""
        rt = self.runtimes.get(session_id) if session_id else None
        task = (rt.run_task if rt else None) or (self._run_task if not session_id else None)
        if task and not task.done():
            task.cancel()
            # 取消时立刻清未决权限：权限弹窗可能还留在屏上，但这一轮已经结束，
            # 残留的 request_id 再被投递会「成功」（前端以为决策生效）。
            # 安全审查 M12——Agent 侧还有一层 finally 兜底
            agent = getattr(rt, "agent", None) if rt is not None else None
            if agent is None:
                agent = getattr(self, "agent", None)
            if agent is not None:
                try:
                    agent.clear_pending()
                except Exception:  # noqa: BLE001 - 清理失败不挡停止
                    pass
            return True
        return False

    # ---- 圆桌：多模型并行独立作答 + 主席融合 ----

    def _build_member_provider(self, name: str, model: str | None) -> Provider:
        """为圆桌成员构建独立 Provider 实例（无副作用：不改当前使用的模型状态）。"""
        if self._provider_factory_override is not None:  # 测试/演示注入
            return self._provider_factory_override()
        if name not in self.cfg.providers:
            raise ConfigError(
                "unknown provider: {}; available: {}".format(name, ", ".join(self.cfg.providers))
            )
        pc = self.cfg.providers[name]
        if model:
            pc = pc.model_copy(update={"model": model})
        return build_provider(name, pc)

    def _resolve_members(
        self, members_params: list | None, chair_answers: bool | None = None,
    ) -> list[MemberSpec]:
        """解析圆桌成员：显式列表优先；缺省时取所有已配置 Key 的服务的当前模型。

        规则：
        - 去重（同名同模型只留一个）；上限 cfg.roundtable.max_members（不含主席）；
        - chair_answers 为 None 时用配置值；为 True 时跳过与主席重复的成员
          （主席会被单独插到队列最前）；
        - 单个成员构建失败（缺 Key/未知服务）不阻断，作答时以错误卡片呈现。
        """
        specs: list[MemberSpec] = []
        seen: set[tuple[str, str]] = set()
        limit = self.cfg.roundtable.max_members
        chair_in = (
            self.cfg.roundtable.chair_answers if chair_answers is None
            else bool(chair_answers)
        )
        chair_key = (self.provider_name, self.provider_model)

        def _add(name: str, model: str, role: str = "") -> None:
            key = (name, model)
            if key in seen or len(seen) >= limit:
                return
            seen.add(key)
            if role not in MEMBER_ROLES:
                role = ""  # 未知身份 id 一律按普通成员处理
            try:
                provider = self._build_member_provider(name, model)
                specs.append(MemberSpec(
                    provider_name=name,
                    model=model or getattr(provider, "model", ""),
                    provider=provider,
                    role=role,
                ))
            except Exception as e:  # noqa: BLE001 - 构建失败降级为错误成员卡片
                specs.append(MemberSpec(
                    provider_name=name, model=model, build_error=str(e)[:200], role=role,
                ))

        if members_params:
            for m in members_params:
                name = str((m or {}).get("provider", "")).strip()
                if not name:
                    continue
                model = str((m or {}).get("model", "") or "").strip()
                role = str((m or {}).get("role", "") or "").strip()
                if chair_in and (name, model) == chair_key:
                    continue
                _add(name, model, role)
        else:
            for name, pc in self.cfg.providers.items():
                if resolve_api_key(name, pc) is None:
                    continue
                if chair_in and (name, pc.model) == chair_key:
                    continue
                _add(name, pc.model)
        return specs

    async def _roundtable_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent | None = None, compare: bool = False,
        sid: str | None = None, images: list[ImageBlock] | None = None,
        debate_rounds: int | None = None, chair_answers: bool | None = None,
        regenerate: bool = False,
    ) -> dict:
        """圆桌轮主体：成员并行作答（+ 可选辩论）→ 主席融合 → 结果并入主历史。

        返回随轮次结果回传前端的圆桌元数据。历史追加 user(问题) + assistant
        (融合答案)；成员草稿不入主历史，但随融合消息的 roundtable 元数据持久化
        （历史回放时圆桌卡仍可展开回看）。

        取消语义与 agent.run_turn 对齐：user 消息在成员开跑前就入历史，
        中途点停止问题不会丢；融合中途取消时已流出的部分文本也照样落库。
        agent 缺省时用活动会话的（单会话调用路径兼容）。
        """
        if agent is None:
            agent = self.agent
        if self.provider is None:
            raise RuntimeError("圆桌需要当前主模型可用；请先在模型下拉中选择一个已配置 Key 的服务")

        cfg = self.cfg.roundtable
        debate = cfg.debate_rounds if debate_rounds is None else max(0, min(int(debate_rounds), 2))
        chair_in = cfg.chair_answers if chair_answers is None else bool(chair_answers)

        async def emit_ev(ev) -> None:
            await emit(ev.model_dump())

        # 圆桌轮耗时起点：成员并行 + 辩论 + 主席融合的总墙钟，随 TurnFinished
        # 下发，前端据此把「已用时」芯片定格成真实值（与落库后显示一致）。
        rt_t0 = time.monotonic()

        def _rt_ms() -> int:
            return int((time.monotonic() - rt_t0) * 1000)

        await emit_ev(TurnStarted(iteration=1))

        # 圆桌是纯文本协作：图片不会发给成员/主席，但得让用户知道，不能静默丢弃
        if images:
            await emit_ev(NoticeEvent(
                message=f"圆桌轮为纯文本协作：本轮的 {len(images)} 张图片不会发给成员模型"
                        "（已随消息保留）；需要模型看图请关闭圆桌后重发。"
            ))

        # 用户消息先入历史（与普通轮一致）：中途停止问题也能落库，不丢上下文。
        # 重新生成时提问已在历史末尾（session.truncate 已删掉旧回答），不再重复追加。
        if regenerate:
            question = text
            for m in reversed(agent.history):
                if m.role == "user" and m.text.strip():
                    question = m.text
                    break
        else:
            question = text
            agent.history.append(Message.user(text, images))

        # 上下文压缩护栏：成员与主席都要吃全量历史 + 全部草稿，超限时先压缩
        #（与 agent.run_turn 开头一致），否则最容易爆的反而是主席自己的上下文
        if agent.used_context_tokens() > agent.context_limit_tokens:
            ev = await compact_history(agent, keep_recent=self.cfg.compaction_keep_recent)
            if ev is not None:
                await emit_ev(ev)

        members = self._resolve_members(members_params, chair_in)
        # 面板选了超过上限的成员会被静默截断，明确告知而不是让用户猜
        if members_params:
            wanted = len({
                (str((m or {}).get("provider", "")).strip(),
                 str((m or {}).get("model", "")).strip())
                for m in members_params
                if str((m or {}).get("provider", "")).strip()
            })
            if wanted > cfg.max_members:
                await emit_ev(NoticeEvent(
                    message=f"圆桌成员上限为 {cfg.max_members} 个（设置 · 圆桌 可调），"
                            f"本轮只用了面板顺序前 {cfg.max_members} 个。"
                ))
        if not members:
            await emit_ev(TurnFinished(stop_reason="error", iterations=1, duration_ms=_rt_ms()))
            raise RuntimeError(
                "圆桌没有可用成员：请先在成员面板选择，或给更多模型服务配置 API Key"
            )
        if chair_in:
            members.insert(0, MemberSpec(
                provider_name=self.provider_name,
                model=self.provider_model,
                provider=self.provider,
            ))

        # 成员/主席都不看图片（纯文本协作），历史里的图片块单独剥离；
        # history[:-1] 排除刚追加的本次提问（由 run_roundtable 自己拼在末尾）
        member_history = _strip_image_blocks(agent.history[:-1])

        system = history_system_text(agent.history) or self.compose_system()
        outcome: RoundtableOutcome = await run_roundtable(
            members=members,
            chair=self.provider,
            system_text=system,
            history=member_history,
            user_text=question,
            timeout_s=cfg.member_timeout_s,
            emit=emit_ev,
            debate_rounds=debate,
            fuse=not compare,
            chair_provider=self.provider_name or "",
            chair_model=self.provider_model or "",
            chair_context_tokens=agent.context_limit_tokens,
            member_history_turns=cfg.member_history_turns,
        )

        def member_meta(r) -> dict:
            return {
                "provider": r.spec.provider_name,
                "model": r.spec.model,
                "role": r.spec.role,
                "status": r.status,
                "error": r.error,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                # 草稿随消息持久化（限长）：刷新/重进会话后圆桌卡仍可展开回看
                "draft": clip_draft(r.text.strip()) if r.text.strip() else "",
            }

        meta = {
            "mode": "compare" if compare else "roundtable",
            "chair": {"provider": self.provider_name, "model": self.provider_model},
            "chair_answers": chair_in,
            "debate_rounds": debate,
            "rounds": 1 + debate,
            "members": [member_meta(r) for r in outcome.members],
        }

        # 用量入账：圆桌不走 agent.run_turn，agent.total_* 不会动（pipeline 那行
        # add_usage 记的是 0）。这里按成员逐条写 usage_log、融合记主席名下，
        # 统计页与每日 token 预算护栏才看得见圆桌的真实成本。
        if sid:
            try:
                for row in usage_rows(outcome):
                    await self.store.add_usage(
                        sid, row["provider"], row["model"],
                        row["input_tokens"], row["output_tokens"],
                        row.get("cached_tokens", 0),
                    )
            except Exception:  # noqa: BLE001 - 记账失败不影响本轮结果
                pass

        if compare:
            # A/B 对比：每个成员的回答单独成一条消息（带归属徽标），不融合
            kept = 0
            for r in outcome.members:
                if r.status != "done" or not r.text.strip():
                    continue
                m = Message.assistant([TextBlock(text=r.text.strip())])
                m.roundtable = {
                    "mode": "compare",
                    "chair": meta["chair"],
                    "members": [member_meta(r)],
                }
                agent.history.append(m)
                await emit_ev(AssistantMessage(message=m.model_dump()))
                kept += 1
            meta["compared"] = kept
            await emit_ev(TurnFinished(
                stop_reason="end_turn" if kept else "error", iterations=1, duration_ms=_rt_ms()
            ))
            return meta

        if outcome.status == "cancelled":
            # 用户中途停止：已流出的部分融合文本照样落库（用户已经看到了，
            # 不落库刷新就没了）。TurnFinished 由 pipeline 的 stopped 结果收尾。
            meta["status"] = "cancelled"
            if outcome.fused_text.strip():
                assistant = Message.assistant([TextBlock(text=outcome.fused_text)])
                assistant.roundtable = meta
                agent.history.append(assistant)
                await emit_ev(AssistantMessage(message=assistant.model_dump()))
            else:
                # 成员阶段取消：流式时用户已经看到过这些成员卡，草稿按对比式
                # 落库（与融合失败降级同思路——已花的钱和已看的内容不随刷新消失）。
                # 部分草稿成员的状态是 error（引擎标注「已取消」），如实呈现。
                kept = [r for r in outcome.members if r.text.strip()]
                if kept:
                    meta["mode"] = "compare"
                    for r in kept:
                        m = Message.assistant([TextBlock(text=r.text.strip())])
                        m.roundtable = {
                            "mode": "compare", "chair": meta["chair"],
                            "cancelled": True,
                            "members": [member_meta(r)],
                        }
                        agent.history.append(m)
                        await emit_ev(AssistantMessage(message=m.model_dump()))
            await emit_ev(TurnFinished(
                stop_reason="cancelled", iterations=1, duration_ms=_rt_ms()
            ))
            return meta

        if outcome.fused_text:
            assistant = Message.assistant([TextBlock(text=outcome.fused_text)])
            assistant.roundtable = meta
            agent.history.append(assistant)
            await emit_ev(AssistantMessage(message=assistant.model_dump()))
            await emit_ev(TurnFinished(
                stop_reason="end_turn", iterations=1, duration_ms=_rt_ms()
            ))
            return meta

        # 融合失败：成员草稿已经花了钱，降级成对比式逐条保留，不至于空手而归
        drafts = [r for r in outcome.members if r.status == "done" and r.text.strip()]
        await emit_ev(ErrorEvent(
            message=f"圆桌融合失败：{outcome.error or '所有成员均未产出回答'}"
        ))
        if drafts:
            await emit_ev(NoticeEvent(
                message=f"已保留 {len(drafts)} 份成员草稿（见下方消息），融合结果未能生成。"
            ))
            meta["mode"] = "compare"
            meta["degraded"] = True
            meta["status"] = "error"
            meta["error"] = outcome.error
            for r in drafts:
                m = Message.assistant([TextBlock(text=r.text.strip())])
                m.roundtable = {
                    "mode": "compare", "chair": meta["chair"], "degraded": True,
                    "members": [member_meta(r)],
                }
                agent.history.append(m)
                await emit_ev(AssistantMessage(message=m.model_dump()))
            await emit_ev(TurnFinished(
                stop_reason="end_turn", iterations=1, duration_ms=_rt_ms()
            ))
        else:
            meta["status"] = "error"
            meta["error"] = outcome.error
            await emit_ev(TurnFinished(
                stop_reason="error", iterations=1, duration_ms=_rt_ms()
            ))
        return meta

    # ---- 圆桌设置（设置 · 圆桌）：读回当前值 → 编辑 → 保存后热生效 ----

    def roundtable_detail(self) -> dict:
        """设置页圆桌卡片的数据。"""
        rt = self.cfg.roundtable
        return {
            "max_members": rt.max_members,
            "member_timeout_s": rt.member_timeout_s,
            "chair_answers": rt.chair_answers,
            "debate_rounds": rt.debate_rounds,
            "member_history_turns": rt.member_history_turns,
            "configured_services": len(self._configured_services()),
            "config_hint": (
                "圆桌让多个模型并行独立作答，再由主席（当前主模型）融合成一份答案，"
                "token 成本约为单模型的「成员数 + 1」倍；每多一轮辩论修订，成员成本"
                "再翻一倍（草稿已收敛的成员会自动跳过，雷同草稿在融合时自动去重）。"
                "成员来自「模型服务」里已配好 Key 的服务，可在输入框的圆桌面板逐个"
                "勾选并指定身份（批评者 / 事实核查员等）。圆桌全程不调用工具、不写文件。"
            ),
        }

    async def roundtable_save(self, params: dict) -> dict:
        """保存圆桌设置并热生效（写 config.toml 的 [roundtable] 段）。"""
        updates: dict = {}
        if params.get("max_members") is not None:
            updates["max_members"] = max(1, min(8, int(params["max_members"])))
        if params.get("member_timeout_s") is not None:
            updates["member_timeout_s"] = max(10, int(params["member_timeout_s"]))
        if params.get("chair_answers") is not None:
            updates["chair_answers"] = bool(params["chair_answers"])
        if params.get("debate_rounds") is not None:
            updates["debate_rounds"] = max(0, min(2, int(params["debate_rounds"])))
        if params.get("member_history_turns") is not None:
            updates["member_history_turns"] = max(0, int(params["member_history_turns"]))
        if updates:
            update_config_section("roundtable", updates)
            self.cfg = load_config()
        return self.roundtable_detail()

    # ---- Slash 命令支撑（/compact /status /todos，前端输入 / 唤出菜单） ----

    async def compact_now(self) -> dict:
        """手动触发上下文压缩（/compact）。没有可压缩内容时返回 compacted=False。"""
        agent = self.agent
        ev = await compact_history(agent, keep_recent=self.cfg.compaction_keep_recent)
        return {
            "compacted": ev is not None,
            "before": ev.before_messages if ev else 0,
            "after": ev.after_messages if ev else 0,
            "summary_chars": ev.summary_chars if ev else 0,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
            "context_detail": self._context_detail(agent),
        }

    def _context_detail(self, agent: Agent) -> dict:
        """上下文构成明细（输入栏环形仪表的悬停弹层）：按消息 / 系统提示词 /
        技能 / 工具分桶估算 token 占用，附会话累计的平均缓存命中率。
        纯估算、仅供界面参考；「其他」吸收图片输入与各家 tokenizer 的估算偏差
        （真实上报 − 各桶估算之和，负值归零）。"""
        sys_text = (
            build_system_prompt(self.working_dir)
            + render_instructions_section(self.instructions_file, self.instructions_text)
            + render_memory_section()
        )
        skills_text = self.skills.render_prompt_section() if self.skills else ""
        mcp_names = {t.name for t in (self.mcp_tools or [])}
        schemas = agent.registry.schemas()
        buckets = [
            ("消息", estimate_tokens([m for m in agent.history if m.role != "system"])),
            ("系统工具", estimate_text_tokens(json.dumps(
                [s for s in schemas if s["name"] not in mcp_names], ensure_ascii=False))),
            ("技能", estimate_text_tokens(skills_text)),
            ("系统提示词", estimate_text_tokens(sys_text)),
            ("MCP 工具", estimate_text_tokens(json.dumps(
                [s for s in schemas if s["name"] in mcp_names], ensure_ascii=False))),
        ]
        total = agent.used_context_tokens()
        rest = total - sum(n for _, n in buckets)
        buckets.append(("其他", max(0, rest)))
        denom = sum(n for _, n in buckets) or 1
        rows = [
            {"label": label, "tokens": n, "pct": round(100 * n / denom, 1)}
            for label, n in sorted(buckets, key=lambda kv: -kv[1])
        ]
        rate = None
        if agent.total_cached_tokens > 0 and agent.total_in_tokens > 0:
            # 服务从未上报过缓存明细时保持 None（前端显示「—」），不冒充 0%
            rate = round(100 * agent.total_cached_tokens / agent.total_in_tokens, 1)
        return {"tokens": total, "limit": agent.context_limit_tokens,
                "rows": rows, "cache_rate": rate}

    def status(self) -> dict:
        """/status：模型、上下文占用、工具数、任务清单一览。"""
        todo_tool = self.agent.registry.get("todo_write")
        return {
            "version": __version__,
            "working_dir": str(self.working_dir or ""),
            "provider": self.provider_name,
            "model": self.provider_model,
            "provider_error": self.provider_error,
            "session_id": self.session.id if self.session else None,
            "context_tokens": self.agent.used_context_tokens(),
            "context_limit": self.agent.context_limit_tokens,
            "context_detail": self._context_detail(self.agent),
            "history_messages": len(self.agent.history),
            "tool_count": len(self.agent.registry),
            "queued": len(self.queue),
            "todos": list(getattr(todo_tool, "items", []) or []),
        }

    async def tasks_list(self, params: dict | None = None, *, session_id: str | None = None) -> dict:
        """任务簿列表；session_id 给出时只返回该会话的任务（远程客户端隔离，B13）。"""
        sid = session_id
        if sid is None:
            raw = (params or {}).get("session_id")
            sid = str(raw) if raw else None
        return {"tasks": self.tasks.list_tasks(session_id=sid) if self.tasks else []}

    async def tasks_get(self, params: dict, *, session_id: str | None = None) -> dict:
        task_id = str((params or {}).get("task_id", ""))
        detail = self.tasks.get_detail(task_id, session_id=session_id) if self.tasks else None
        if detail is None:
            raise ValueError("unknown task_id: " + task_id)
        return {"task": detail}

    async def tasks_cancel_all(self, *, session_id: str | None = None) -> dict:
        """取消运行中的子代理任务；session_id 给出时只取消该会话的（B13）。"""
        if self.tasks:
            self.tasks.cancel_all(session_id=session_id)
        return {"cancelled": True}

    async def tasks_cancel(self, params: dict, *, session_id: str | None = None) -> dict:
        """取消单个子代理任务（排队中直接落终态，运行中的真取消）。"""
        task_id = str((params or {}).get("task_id", ""))
        if self.tasks is None:
            raise ValueError("no task manager")
        # 远程客户端只能取消自己会话的任务（与 tasks_list/tasks_get 同款隔离）
        ok = self.tasks.cancel_task(task_id, session_id=session_id)
        if not ok:
            raise ValueError("unknown or finished task_id: " + task_id)
        return {"cancelled": True, "task_id": task_id}

    async def usage_stats(self, params: dict) -> dict:
        """用量统计：按天/会话/服务聚合 + 按 provider 单价估算费用。

        只统计当前项目的用量（安全审查 B14：by_session 带会话标题，
        跨项目聚合会借远程接口泄露）；每日预算护栏用的 usage_today 仍是
        全局口径（预算本身是全局设置）。
        """
        days = min(90, max(1, int(params.get("days", 14))))
        # 无项目态没有可归属的项目：不传过滤目标 = 全局口径（此时库里也没有
        # 别的项目，等价于快聊用量；带会话标题的 by_session 只在有项目时下发）
        if self.project is not None:
            st = await self.store.usage_stats(days, project_id=self.project.id)
        else:
            st = await self.store.usage_stats(days)
            st["by_session"] = []  # 无项目态不展示按会话用量（B14：防跨项目标题枚举）
        prices = {
            name: {
                "price_in": pc.price_in,
                "price_out": pc.price_out,
                "price_cache": pc.price_cache,
            }
            for name, pc in self.cfg.providers.items()
        }
        cost = 0.0
        for row in st["by_provider"]:
            pr = prices.get(row["provider"] or "", {})
            it, ot = row["it"] or 0, row["ot"] or 0
            # 缓存命中价（>0）时把命中部分从输入价里拆出来单算：
            # 多数服务命中提示词缓存的输入便宜得多（DeepSeek 约输入价 1/10）
            cached = row.get("cached") or 0
            price_cache = pr.get("price_cache", 0.0)
            if price_cache > 0 and cached > 0:
                cost += max(0, it - cached) / 1e6 * pr.get("price_in", 0.0)
                cost += cached / 1e6 * price_cache
            else:
                cost += it / 1e6 * pr.get("price_in", 0.0)
            cost += ot / 1e6 * pr.get("price_out", 0.0)
        total_in = sum(r["it"] or 0 for r in st["by_provider"])
        total_out = sum(r["ot"] or 0 for r in st["by_provider"])
        total_cached = sum(r.get("cached") or 0 for r in st["by_provider"])
        return {
            "days": days,
            "by_day": st["by_day"],
            "by_session": st["by_session"],
            "by_provider": st["by_provider"],
            "total_in": total_in,
            "total_out": total_out,
            "total_cached": total_cached,
            # 真实去重会话数：无项目态与 by_session 被截断时它才是准确口径
            "session_count": st.get("session_count", 0),
            "cost": round(cost, 4),
            "has_price": any(p["price_in"] or p["price_out"] for p in prices.values()),
            # 每日预算与当日用量：用量页据此显示「今日已用 / 预算」，0 = 未设上限
            "budget": self.cfg.daily_token_budget,
            "today": await self.store.usage_today(),
        }

    def _workspace_path(self, raw: str) -> Path:
        """把相对/绝对路径解析到工作目录内的真实路径；越界一律拒绝。

        fs_read / fs_write 共用：resolve 消掉 .. 后必须仍在工作目录里，
        Windows 非法字符（盘符冒号等）在 resolve/relative_to 时抛错同样拦下。
        无项目态（没有工作目录）直接给可读拒绝。
        """
        if self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，文件面板不可用——先在侧栏「项目」区点 ＋ 添加项目并选择一个文件夹。"
            )
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target = target.resolve()
            target.relative_to(self.working_dir)
        except (OSError, ValueError):
            raise RuntimeError("只能访问工作目录内的文件") from None
        return target

    # Windows 文件名非法字符与控制字符（建新文件时逐段校验）
    _BAD_NAME_CHARS = '<>:"|?*'
    # Windows 保留设备名（含带扩展名形式，如 con.txt：旧系统上这类文件无法正常打开/删除）
    _RESERVED_NAMES = {"con", "prn", "aux", "nul",
                       *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))}

    def _validate_rel_path(self, raw: str) -> str:
        """新建文件的相对路径校验：禁绝对路径、.. 段、非法字符、结尾点/空格。"""
        raw = raw.replace("\\", "/").strip("/")
        parts = [p for p in raw.split("/") if p]
        if not parts:
            raise RuntimeError("文件名不能为空")
        for seg in parts:
            if seg in (".", ".."):
                raise RuntimeError("路径里不能有 .. 段")
            if any(c in seg for c in self._BAD_NAME_CHARS) or any(ord(c) < 32 for c in seg):
                raise RuntimeError("文件名含非法字符：" + seg)
            if seg.endswith((" ", ".")):
                raise RuntimeError("Windows 文件名不能以空格或点结尾：" + seg)
            if seg.split(".", 1)[0].lower() in self._RESERVED_NAMES:
                raise RuntimeError("文件名是 Windows 保留设备名：" + seg)
        rel = "/".join(parts)
        if len(rel) > 240:
            raise RuntimeError("路径过长（上限 240 字符）")
        return rel

    async def fs_read(self, params: dict) -> dict:
        """只读预览工作区文件（文件树用）：文本直接读（自动识别编码）；
        PDF/Word/Excel/PPT 提取文本；其余二进制拒绝。沙箱限制在工作目录内。
        返回 mtime 供编辑器保存时做冲突检测，editable 标记「这份文本能不能写回」
        （提取文本 / 截断 / 编码不明的都不可写回）。"""
        raw = str(params.get("path", "") or "").strip()
        if not raw:
            raise RuntimeError("missing path")
        target = self._workspace_path(raw)
        if not target.is_file():
            raise RuntimeError("文件不存在（或这是目录）: " + raw)
        try:
            size = target.stat().st_size
            # 毫秒精度：ns 级时间戳(约 1.7e18)超出 JS Number 安全整数(9e15)，
            # 经 JSON 往返会丢精度导致冲突检测永远误报
            mtime = target.stat().st_mtime_ns // 1_000_000
        except OSError as e:
            raise RuntimeError("读取失败: " + str(e)) from None

        # 文档类文件：提取文本预览（与 read_document 工具同一套解析器）
        if target.suffix.lower() in (".pdf", ".docx", ".xlsx", ".pptx"):
            if size > 50_000_000:
                raise RuntimeError(f"文件太大（{size / 1048576:.0f}MB），上限 50MB")
            from ..tools.docs import extract_document_text

            try:
                text = await asyncio.to_thread(extract_document_text, target, 60_000)
            except Exception as e:  # noqa: BLE001 - ToolError/解析错误统一转可读提示
                raise RuntimeError(str(e)) from None
            return {
                "path": str(target.relative_to(self.working_dir)).replace("\\", "/"),
                "text": text,
                "size": size,
                "truncated": False,
                "doc": True,
                "mtime": mtime,
                "editable": False,
            }

        try:
            # 只读预览需要的 400KB 前缀：先整个 read_bytes 再切片会把几百 MB 的
            # 日志/数据集全量吞进内存，同步读盘还会卡住事件循环（UI 全部停摆）
            with target.open("rb") as fh:
                data = fh.read(400_000)
        except OSError as e:
            raise RuntimeError("读取失败: " + str(e)) from None
        if b"\x00" in data:
            raise RuntimeError("二进制文件不支持预览")
        # 文本文件：探测编码，GBK / GB18030 文件不再显示成替换字符
        from ..textio import decode_bytes

        loaded = decode_bytes(data)
        truncated = size > 400_000
        return {
            "path": str(target.relative_to(self.working_dir)).replace("\\", "/"),
            "text": loaded.text,
            "size": size,
            "truncated": truncated,
            "mtime": mtime,
            # 编码探不出来时不允许写回：用替换字符覆盖原文等于损坏文件
            "editable": not truncated and loaded.certain,
            "encoding": loaded.encoding,
            "encoding_text": "UTF-8" if loaded.encoding == "utf-8" else loaded.encoding.upper(),
            "newline": loaded.newline,
        }

    async def fs_write(self, params: dict) -> dict:
        """右侧文件面板编辑器的「保存」：把用户改的文本写回工作区文件。

        这是用户本人在界面上点保存（与 project.save_instructions 同级的
        「用户主动写」，不走 PermissionGate——那道门管的是 Agent 工具调用），
        但同样严格锁死在工作目录内、只收文本、限 2MB。base_mtime 与磁盘当前
        不一致时不落盘、返回 conflict=True，由前端让用户选覆盖（force=True）
        或放弃，避免无意盖掉 Agent / 其他程序正在做的改动。

        落盘沿用文件原本的编码与行尾符：缓存里只存了探测结果的小字典（不读全文，
        避免每次保存多读一遍盘）；拿不到时回退 UTF-8 + LF。
        """
        raw = str(params.get("path", "") or "").strip().replace("\\", "/")
        if not raw:
            raise RuntimeError("missing path")
        text = str(params.get("text", "") or "")
        force = bool(params.get("force"))
        base_mtime = params.get("base_mtime")
        if base_mtime is not None:
            try:
                base_mtime = int(base_mtime)
            except (TypeError, ValueError):
                raise RuntimeError("base_mtime 必须是整数（fs.read 返回的 mtime）") from None
        rel = self._validate_rel_path(raw)
        target = self._workspace_path(rel)
        # 沿用文件原本的编码与行尾符：探测结果有小缓存，拿不到时回退 UTF-8 + LF。
        # 前端编辑器按 HTML 规范会把内容统一成 LF，所以写回时必须还原行尾符，
        # 否则保存一次 CRLF 文件就变成 LF（或反之），整文件 diff 全是噪声。
        enc, nl = self._file_text_meta(target)
        try:
            data = encode_text(text, enc, nl)
        except (UnicodeEncodeError, LookupError):
            raise RuntimeError(
                f"内容无法用原编码（{enc}）保存：里面出现了该编码不支持的字符。"
                "请改用 UTF-8 另存，或删掉这些字符后重试。"
            ) from None
        if len(data) > 2_000_000:
            raise RuntimeError("文件太大（超过 2MB），请用系统编辑器处理")

        exists = target.is_file()
        if exists and base_mtime is not None and not force:
            try:
                cur = target.stat().st_mtime_ns // 1_000_000  # 毫秒精度，见 fs_read
            except OSError as e:
                raise RuntimeError("读取失败: " + str(e)) from None
            if int(base_mtime) != cur:
                # 不落盘，交给前端弹「覆盖 / 放弃」
                return {"saved": False, "conflict": True, "mtime": cur}
        # 写租约：用户保存也要和并行 Agent 写同一文件错开（短等待，面板是交互场景）；
        # 冲突注记随结果带回（前端当前未展示，agent 侧写完会带同样的注记给模型）
        hub = leases.hub_for(self.working_dir)
        lease = None
        if hub is not None:
            try:
                lease = await hub.claim(
                    [(target, rel)],
                    owner=(self.session.id if self.session else ""),
                    wait_s=3.0,
                )
            except Exception:  # noqa: BLE001 - 租约是协调不是闸门，故障不挡保存
                lease = None
        try:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                mtime = target.stat().st_mtime_ns // 1_000_000
            except OSError as e:
                raise RuntimeError("写入失败: " + str(e)) from None
        finally:
            note = lease.release() if lease is not None else ""
        self._text_meta_cache[str(target)] = (mtime, enc, nl)
        return {"saved": True, "mtime": mtime, "size": len(data),
                "encoding_text": enc.upper(), **({"lease_note": note} if note else {})}

    # 文本文件编码 / 行尾符探测结果缓存：fs.read 写入、fs.write 读回。
    # 存的是探测结果（mtime + 两个短字符串），不存文件内容，避免每次保存多读一遍盘。
    # 实例属性而不是类属性（安全审查低危项）：类属性会被所有实例共享——测试里
    # 两个后端实例互相看到对方的探测结果，多工作区/多实例场景下也会串味。

    def _file_text_meta(self, target: Path) -> tuple[str, str]:
        """取文件原本的（编码, 行尾符）；缓存过期或文件已变时重探。

        探不出确凿编码时回退 UTF-8 + LF（与 fs_read 返回 editable=False 一致：
        那种文件前端不会让用户编辑，走不到这里）。
        """
        from ..textio import decode_bytes

        try:
            mtime = target.stat().st_mtime_ns // 1_000_000
        except OSError:
            return "utf-8", "\n"
        key = str(target)
        cached = self._text_meta_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]
        try:
            with target.open("rb") as fh:  # 同 fs_read：只探前缀，不整读大文件
                loaded = decode_bytes(fh.read(400_000))
        except OSError:
            return "utf-8", "\n"
        if loaded.binary or not loaded.certain:
            return "utf-8", "\n"
        self._text_meta_cache[key] = (mtime, loaded.encoding, loaded.newline)
        return loaded.encoding, loaded.newline

    async def workspace_files(self) -> dict:
        """/@ 文件提及的数据源：项目内文件相对路径清单（跳过依赖与构建目录）。"""
        if self.working_dir is None:
            return {"files": [], "dirs": [], "no_project": True}
        return await asyncio.to_thread(self._walk_workspace_files)

    SKIP_DIRS = {
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "dist", "build", ".skysheep", ".mimosa",
        ".next", "target", ".idea", ".vscode", ".preview", ".zcode", ".agents",
    }
    MAX_FILES = 400

    def _walk_workspace_files(self) -> dict:
        from ..core.ignore import IgnoreRules

        out: list[str] = []
        dirs: list[str] = []
        root = self.working_dir
        ignore = IgnoreRules.load(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in self.SKIP_DIRS and not d.startswith(".git")
                and not ignore.matches((Path(dirpath) / d).relative_to(root).as_posix(), is_dir=True)
            ]
            rel_base = Path(dirpath).relative_to(root)
            if rel_base != Path("."):
                # 目录也进索引（@补全里以 / 结尾）；只收录直接子层，避免清单爆炸
                dirs.append(rel_base.as_posix() + "/")
            for fn in filenames:
                rel = (rel_base / fn).as_posix()
                if ignore.matches(rel):
                    continue
                if len(out) >= self.MAX_FILES:
                    return {"files": sorted(out), "root": str(root), "truncated": True}
                out.append(rel)
        # 目录排在文件前（@菜单分组展示），各自排序
        return {"files": sorted(dirs)[: self.MAX_FILES // 4] + sorted(out),
                "root": str(root), "truncated": False}

    # ---- 检查点（撤销本轮改动） ----

    async def list_checkpoints(self) -> dict:
        sid = self.session.id if self.session else ""
        return {"checkpoints": self.checkpoints.list_for(sid)}

    async def restore_checkpoint(self, checkpoint_id: str, force: bool = False) -> dict:
        """把某轮的文件改动回滚到改前状态；并告知模型「文件已被回滚」。

        回滚前引擎会比对「快照保存之后文件有没有又被改过」（并行会话/任务
        写同一文件的场景）：有冲突且未 force 时不动磁盘，返回 conflict 结果，
        前端把冲突文件列给用户确认后再带 force=true 重试。
        """
        cp = self.checkpoints.get(checkpoint_id)
        if cp is None:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            )
        # 归属校验（安全审查 B12）：检查点 id 顺序可枚举，不属于当前项目的
        # 快照不能凭 id 恢复（否则别的项目的文件内容会被写回磁盘）
        await self._check_checkpoint_ownership(cp)
        # 会话运行中拒绝回滚（与 truncate 同一守卫）：回滚会改写正在跑的
        # 历史并往 runtime 追加系统提示，轮末落库会把这条提示再插一遍
        cp_sid = (cp or {}).get("session_id")
        cp_rt = self.runtimes.get(cp_sid) if cp_sid else None
        if cp_rt and cp_rt.run_task and not cp_rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再回滚")
        try:
            files = self.checkpoints.restore(checkpoint_id, force=force)
        except CheckpointConflictError as e:
            return {
                "conflict": True,
                "checkpoint_id": checkpoint_id,
                "files": e.conflicts,
            }
        except KeyError:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            ) from None
        note = Message.user(
            "(系统提示) 用户执行了「撤销本轮改动」，以下文件已恢复到本轮改动前的状态："
            + ", ".join(files)
            + "。后续如需引用这些文件请先重新读取。"
        )
        sid = (cp or {}).get("session_id") or (self.session.id if self.session else None)
        agent = self.runtimes[sid].agent if sid and sid in self.runtimes else self.agent
        agent.history.append(note)
        if sid:
            await self.store.append_message(sid, note)
        return {"restored": checkpoint_id, "files": files}

    async def _check_checkpoint_ownership(self, cp: dict) -> None:
        """检查点必须属于当前活动会话（B12：cp id 顺序可枚举）。

        同项目里其他会话的快照也不能凭枚举到的 id 恢复/对比——恢复是写盘原语，
        对比会返回文件内容。无会话归属的旧检查点（meta 里没写 session_id）
        无法校验，同样拒绝。
        """
        sid = (cp or {}).get("session_id") or ""
        active = self.session.id if self.session else ""
        if not sid or not active or sid != active:
            raise RuntimeError("该检查点不属于当前会话，无法校验归属")

    async def checkpoint_diff(self, checkpoint_id: str) -> dict:
        """「审查」标签页：某个检查点里每个文件的 改前快照 vs 磁盘现状 的 unified diff。"""
        cp = self.checkpoints.get(checkpoint_id)
        if cp is None:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            )
        # 同 restore：凭枚举 id 不能读别的项目会话的文件快照（B12）
        await self._check_checkpoint_ownership(cp)
        files: list[dict] = []
        for path_s, pre in cp["files"].items():
            try:
                cur: bytes | None = Path(path_s).read_bytes()
            except OSError:
                cur = None
            if b"\x00" in (pre or b"") or b"\x00" in (cur or b""):
                files.append({"path": path_s, "status": "binary", "diff": ""})
                continue
            if pre is None and cur is None:
                status, before, after = "gone", "", ""  # 新建的文件后来又被删了
            elif pre is None:
                status, before, after = "created", "", _decode_bytes(cur)
            elif cur is None:
                status, before, after = "deleted", _decode_bytes(pre), ""
            elif pre == cur:
                status, before, after = "unchanged", _decode_bytes(pre), _decode_bytes(cur)
            else:
                status, before, after = "modified", _decode_bytes(pre), _decode_bytes(cur)
            diff = "" if status in ("unchanged", "gone", "binary") else _unified_diff(before, after)
            if len(diff) > MAX_DIFF_CHARS:
                diff = diff[:MAX_DIFF_CHARS] + "\n…（diff 过长，已截断）"
            files.append({"path": path_s, "status": status, "diff": diff})
        return {"id": checkpoint_id, "ts": cp["ts"], "files": files}

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

    # ---- 分级权限模式（对标 Codex / Claude Code：confirm / accept_edits / full_access） ----

    def permission_mode(self) -> str:
        if self.gate is None:
            return "confirm"
        if self.gate.auto_accept_all:
            return "full_access"
        return "accept_edits" if self.gate.auto_accept_write else "confirm"

    def _apply_gate_accept_pref(self, value) -> None:
        """把 ui.json 的 accept_edits（0=安全执行 1=自动编辑 2=完全访问）应用到门控。

        启动恢复 / 换项目 / 导入设置包三处共用；脏值一律落回安全执行。
        """
        if self.gate is None:
            return
        v = value if value in (0, 1, 2) else 0
        self.gate.auto_accept_write = v in (1, 2)
        self.gate.auto_accept_all = v == 2

    async def _seed_builtin_snippets(self) -> None:
        """首启把内置示例快捷指令落成真实记录：设置页里可见、可编辑、可删除。

        只做一次：哨兵文件放 SkySheep home（与指令库同生命周期，不进 ui.json、
        不随设置导出走）；已有自定义指令的老用户不种——保持「添加了自己的指令后
        示例不再出现」的旧约定；种过之后删光也不复活，删光是用户的明确决定。
        """
        mark = skysheep_home() / "snippets-seeded"
        if mark.exists():
            return
        if not await self.store.list_snippets():
            for s in BUILTIN_SNIPPETS:
                await self.store.add_snippet(s["name"], s["content"])
            logging.getLogger("skysheep").info(
                "首次启动：已把 %d 条内置示例提示词写入指令库（设置 · 提示词 里可编辑/删除）",
                len(BUILTIN_SNIPPETS),
            )
        try:
            mark.write_text("1", encoding="utf-8")
        except OSError:
            pass  # 写不进哨兵（只读盘等）：下次启动重查一遍，无副作用

    async def restore_builtin_snippets(self) -> int:
        """把内置示例提示词加回指令库（按名称去重，已存在的跳过），返回新增条数。

        与首启播种不同：这是用户的显式动作，删过的示例可以被主动找回；
        哨兵文件不动，重启仍不会自动补种。
        """
        existing = {s["name"] for s in await self.store.list_snippets()}
        added = 0
        for s in BUILTIN_SNIPPETS:
            if s["name"] in existing:
                continue
            await self.store.add_snippet(s["name"], s["content"])
            added += 1
        return added

    async def export_snippets(self) -> dict:
        """导出全部提示词为可分享的 JSON（只带名称与内容，不含排序/统计）。"""
        rows = await self.store.list_snippets()
        return {
            "format": "skysheep-snippets",
            "version": 1,
            "snippets": [{"name": s["name"], "content": s["content"]} for s in rows],
        }

    async def import_snippets(self, params: dict) -> dict:
        """合并导入提示词：params.data 为导出 JSON（对象或字符串），或 params.path 为文件路径。

        去重规则：名称与内容都相同的条目跳过；其余追加到列表最前（与新建一致）。
        条数上限 200、文件上限 1MB——提示词库是手写资产，超过这个量级不是正常用法。
        """
        raw = params.get("data")
        path = str(params.get("path") or "").strip()
        if raw is None and path:
            p = Path(path)
            if not p.is_file():
                raise RuntimeError("文件不存在：" + path)
            if p.stat().st_size > 1_000_000:
                raise RuntimeError("文件过大，不像是提示词导出文件")
            try:
                tf = read_text_file(p)
            except OSError as e:
                raise RuntimeError(f"读取失败：{e}") from None
            if tf.binary:
                raise RuntimeError("这是一个二进制文件，不是提示词导出 JSON")
            try:
                raw = json.loads(tf.text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"不是有效的 JSON：{e}") from None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"不是有效的 JSON：{e}") from None
        if raw is None:
            raise RuntimeError("缺少导入内容")
        items = raw.get("snippets") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise RuntimeError("不是 SkySheep 提示词导出文件（缺少 snippets 列表）")
        existing = {(s["name"], s["content"]) for s in await self.store.list_snippets()}
        added = skipped = 0
        for it in items[:200]:
            if not isinstance(it, dict):
                skipped += 1
                continue
            name = str(it.get("name") or "").strip()[:40]
            content = str(it.get("content") or "").strip()[:8000]
            if not name or not content or (name, content) in existing:
                skipped += 1
                continue
            await self.store.add_snippet(name, content)
            existing.add((name, content))
            added += 1
        return {"added": added, "skipped": skipped}

    async def set_permission_mode(self, mode: str) -> dict:
        """confirm = 安全执行，写入/命令都确认（默认）；accept_edits = 自动编辑，
        工作目录内写入自动放行、命令仍确认；full_access = 完全访问，写入与命令
        都不再征询。

        档位存在 ui.json，下次启动沿用；放宽档只由本机用户在界面上切换（远端
        调用在 server/app.py 的 dispatch 层拦下，需测试或脚本驱动时直接调用本方法）。
        切换记一条日志：这是降低防护的动作，事后能从 ~/.skysheep/logs/desktop.log 追溯。
        """
        if mode not in ("confirm", "accept_edits", "full_access"):
            raise RuntimeError("权限模式只支持 confirm / accept_edits / full_access")
        if self.gate is None:
            raise RuntimeError("引擎尚未就绪")
        self.gate.auto_accept_write = mode in ("accept_edits", "full_access")
        self.gate.auto_accept_all = mode == "full_access"
        await self.save_ui_prefs(
            {"accept_edits": {"confirm": 0, "accept_edits": 1, "full_access": 2}[mode]}
        )
        logger.info(
            "权限模式切换为 %s（自动允许写入=%s，完全访问=%s，工作目录：%s）",
            mode,
            self.gate.auto_accept_write,
            self.gate.auto_accept_all,
            self.working_dir,
        )
        return {"mode": self.permission_mode()}

    # ---- 白名单（设置页）：手动添加 / 清空 / 测试 / 导入导出 ----

    @staticmethod
    def _validate_whitelist_rule(tool: str, kind: str, pattern: str) -> tuple[str, str, str]:
        """手动添加 / 导入共用的规则校验，返回规范化后的 (tool, kind, pattern)。"""
        tool = (tool or "").strip()
        pattern = (pattern or "").strip()
        if not tool:
            raise RuntimeError("工具名不能为空")
        if kind not in RULE_KINDS:
            raise RuntimeError("规则类型不合法")
        if kind == "always":
            pattern = ""  # always 不携带参数
        elif not pattern:
            raise RuntimeError("该类型需要填写匹配内容")
        if len(pattern) > 500:
            raise RuntimeError("匹配内容过长（上限 500 字符）")
        return tool, kind, pattern

    async def add_whitelist_rule(self, tool: str, kind: str, pattern: str = "") -> dict:
        """手动添加一条项目级规则（设置页入口）。写库后立即 reload，与 remove 同一路径。"""
        self._require_project("保存白名单规则")  # 白名单按项目持久化
        tool, kind, pattern = self._validate_whitelist_rule(tool, kind, pattern)
        rules = await self.store.list_rules(self.project.id)
        if any(
            r["tool"] == tool and r["kind"] == kind and r["pattern"] == pattern
            for r in rules
        ):
            raise RuntimeError("已存在完全相同的规则")
        await self.store.add_rule(self.project.id, tool, kind, pattern)
        await self.gate.load_project_rules()
        return {"rules": await self.store.list_rules(self.project.id)}

    async def clear_whitelist_rules(self, kind: str = "") -> dict:
        """清空项目白名单（kind 为空 = 全部；收紧动作，远端也放行）。

        无项目态没有可清的白名单：直接返回 0（收紧动作不报错，远端也放行）。"""
        if self.project is None:
            return {"removed": 0}
        removed = await self.store.clear_rules(
            self.project.id, kind if kind in RULE_KINDS else ""
        )
        await self.gate.load_project_rules()
        return {"removed": removed}

    async def remove_whitelist_rule(self, rule_id: int) -> dict:
        """删一条项目级规则（设置页入口）。带归属校验：凭枚举到的 rule_id
        不能删其他项目的规则（安全审查 B11），store 层把 project_id 写进 DELETE。
        """
        self._require_project("删除白名单规则")  # 白名单按项目持久化
        rules = await self.store.list_rules(self.project.id)
        if not any(r["id"] == rule_id for r in rules):
            raise RuntimeError("规则不存在，可能已被删除")
        await self.store.remove_rule(rule_id, project_id=self.project.id)
        await self.gate.load_project_rules()
        return {"removed": rule_id}

    async def set_whitelist_rule_enabled(self, rule_id: int, enabled: bool) -> dict:
        """启停一条项目级规则（设置页开关）：临时停用不必删配置。

        停用是收紧（远端也允许）；启用是放宽，与 whitelist.add 同姿态，
        由 app.py 限本机调用。归属校验在 store 层（同 remove_rule）。
        """
        self._require_project("修改白名单规则")
        await self.store.set_rule_enabled(rule_id, self.project.id, bool(enabled))
        await self.gate.load_project_rules()
        return {"id": rule_id, "enabled": bool(enabled),
                "rules": await self.store.list_rules(self.project.id)}

    def check_whitelist_rule(self, tool: str, text: str) -> dict:
        """规则测试器：当前规则会让这条调用直接放行、还是弹确认。"""
        tool = (tool or "").strip()
        if not tool:
            raise RuntimeError("工具名不能为空")
        return self.gate.explain(tool, text or "")

    async def export_whitelist(self) -> dict:
        self._require_project("导出白名单")  # 白名单按项目持久化
        rules = await self.store.list_rules(self.project.id)
        return {
            "version": 1,
            "project": self.project.name,
            "exported_at": time.time(),
            "rules": [
                {"tool": r["tool"], "kind": r["kind"], "pattern": r["pattern"]}
                for r in rules
            ],
        }

    async def import_whitelist(self, rules: list) -> dict:
        """导入规则（合并模式）：逐条走与手动添加相同的校验，重复或不合法的跳过。"""
        self._require_project("导入白名单规则")  # 白名单按项目持久化
        items = rules if isinstance(rules, list) else []
        existing = await self.store.list_rules(self.project.id)
        seen = {(r["tool"], r["kind"], r["pattern"]) for r in existing}
        added = skipped = 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                tool, kind, pattern = self._validate_whitelist_rule(
                    str(item.get("tool", "")),
                    str(item.get("kind", "")),
                    str(item.get("pattern", "") or ""),
                )
            except RuntimeError:
                skipped += 1
                continue
            if (tool, kind, pattern) in seen:
                skipped += 1
                continue
            await self.store.add_rule(self.project.id, tool, kind, pattern)
            seen.add((tool, kind, pattern))
            added += 1
        await self.gate.load_project_rules()
        return {
            "added": added,
            "skipped": skipped,
            "rules": await self.store.list_rules(self.project.id),
        }

    # ---- 模型/配置操作 ----

    async def switch_model(self, name: str, model: str | None = None) -> dict:
        self.provider = self._build_provider(name, model)
        self.provider_error = None
        for ag in self._for_each_agent():
            ag.provider = self.provider
        # 换服务/换模型 = 换上下文窗口：上限与视觉能力都跟着新的服务走
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.context_limit_tokens = limit
        return {
            "provider": name, "model": self.provider_model,
            "reasoning": self.reasoning_state(),
            "context_limit": limit,
            "supports_vision": self._supports_vision(),
        }

    # ---- 新会话默认模型（设置/清除走 WS：default_model.set / default_model.get） ----

    async def set_default_model(self, params: dict) -> dict:
        """设定 / 清除「新会话默认模型」。

        name 传空串 = 清除（新会话回到跟随全局）；name+model = 只对这些
        **新创建**的会话用专用 provider，已存在的会话（含当前对话）不受影响。
        服务名必须在 config.toml 里存在，模型名必须是该服务已登记的模型
        （或留空 = 用该服务的默认模型）。"""
        name = str(params.get("name") or "").strip()
        model = str(params.get("model") or "").strip()
        if name:
            if name not in self.cfg.providers:
                raise RuntimeError(f"未知服务：{name}")
            pc = self.cfg.providers[name]
            if model and pc.models and model not in pc.models:
                raise RuntimeError(f"服务 {name} 没有登记模型 {model}，先在模型服务里添加")
        elif model:
            raise RuntimeError("清除默认模型时不能只留模型名")
        try:
            await self._write_ui_prefs({
                self.DEFAULT_MODEL_PROVIDER_KEY: name or None,
                "default_model_name": model or None,
            })
        except Exception as e:
            raise RuntimeError(f"写入偏好失败：{e}") from e
        return self.default_model_state()

    def default_model_state(self) -> dict:
        name, model = self._default_model_pref()
        return {
            "provider": name,
            "model": model,
            "label": (f"{name} / {model}" if name and model
                      else name if name else ""),
        }

    def reasoning_state(self) -> dict:
        """当前思考强度状态：是否支持 + 当前档位 + 各服务自己的档位。"""
        cur = getattr(self.provider, "reasoning_effort", "auto")
        return {
            "supported": bool(getattr(self.provider, "supports_reasoning", False)),
            "effort": cur,
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "providers": {
                name: {
                    "supports_reasoning": bool(pc.supports_reasoning),
                    "effort": pc.reasoning_effort,
                }
                for name, pc in self.cfg.providers.items()
            },
        }

    async def set_reasoning_effort(self, effort: str, name: str | None = None) -> dict:
        """调整思考强度并热生效。

        name 缺省 = 当前使用的服务；写入 config.toml 的对应 provider 后重建当前
        provider，让下一轮对话立即用新档位。不支持该能力的服务直接报错。
        """
        target = name or self.provider_name
        effort = str(effort or "auto").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 " + " / ".join(REASONING_EFFORTS))
        pc = self.cfg.providers.get(target)
        # 当前 provider（含测试注入/自定义装配）即使不在配置表里也允许直接调档
        is_current = bool(self.provider is not None and target == self.provider_name)
        if pc is None and not is_current:
            raise RuntimeError("unknown provider: " + str(target))
        supports = bool(pc.supports_reasoning) if pc is not None else bool(
            getattr(self.provider, "supports_reasoning", False)
        )
        if not supports:
            raise RuntimeError(f"「{target}」不支持调整思考强度（未声明该能力）")
        if pc is not None:
            update_provider_in_config(target, reasoning_effort=effort)
            self.cfg = load_config()
        if is_current and self.provider is not None:
            # 不重建整个 provider（避免丢 Key/连接），直接改档位即可生效
            self.provider.set_reasoning_effort(effort)
            for ag in self._for_each_agent():
                ag.provider = self.provider
        return {"provider": target, "reasoning": self.reasoning_state()}

    async def add_provider_model(self, name: str, model: str) -> dict:
        """把一个模型加入该服务的已启用列表（不动当前使用的模型）。"""
        model = (model or "").strip()
        if not model:
            raise RuntimeError("模型 ID 不能为空")
        pc = self.cfg.providers.get(name)
        if pc is None:
            raise RuntimeError("unknown provider: " + name)
        models = list(pc.models)
        if model in models:
            return {"name": name, "models": models, "added": False}
        models.append(model)
        set_provider_models_in_config(name, models)
        self.cfg = load_config()
        # 不变量：当前使用的模型必须 ∈ 已启用列表。比如预设自带的占位模型
        # 不在拉取到的列表里时，就把新加的设为当前模型。
        current = self.provider_model if name == self.provider_name else self.cfg.providers[name].model
        activated = False
        if current not in models:
            update_provider_in_config(name, model=model)
            self.cfg = load_config()
            if name == self.provider_name:
                try:
                    await self.switch_model(name, model)
                    activated = True
                except RuntimeError:
                    pass
        return {"name": name, "models": models, "added": True, "activated": activated}

    async def remove_provider_model(self, name: str, model: str) -> dict:
        """从已启用列表里删除一个模型；列表至少要留一个。"""
        pc = self.cfg.providers.get(name)
        if pc is None:
            raise RuntimeError("unknown provider: " + name)
        models = list(pc.models)
        if model not in models:
            raise RuntimeError(f"「{model}」不在该服务的已启用列表里")
        if len(models) == 1:
            raise RuntimeError("至少保留一个启用的模型；先添加新的，再删除旧的")
        models.remove(model)
        set_provider_models_in_config(name, models)
        self.cfg = load_config()
        switched_to = None
        # 删的是该服务的当前模型 → 把它的当前模型换成剩下的第一个
        if self.cfg.providers[name].model == model:
            update_provider_in_config(name, model=models[0])
            self.cfg = load_config()
            if name == self.provider_name:
                await self.switch_model(name, models[0])
                switched_to = models[0]
        return {"name": name, "removed": model, "models": models, "switched_to": switched_to}

    async def toggle_skill(self, name: str, enabled: bool) -> dict:
        if not self.skills.set_enabled(name, enabled):
            raise RuntimeError("skill not found: " + name)
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        return {"name": name, "enabled": enabled}

    async def set_skill_scope(
        self, name: str, mode: str, projects: list[str] | None = None
    ) -> dict:
        """设置全局技能的使用范围（跨项目生效，改完立即重建系统提示词）。"""
        try:
            result = self.skills.set_scope(name, mode, projects)
        except KeyError as e:
            raise RuntimeError(str(e).strip("'\"")) from e
        self.skills.discover()  # 重新套用范围（set_scope 只改了内存里的当前实例）
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        return {"name": name, **result}

    def skill_body(self, name: str) -> dict:
        """读 SKILL.md 原文供界面预览（不受启用/范围限制，停用的技能也要能看）。"""
        try:
            text = self.skills.raw_text(name)
        except KeyError as e:
            raise RuntimeError(str(e).strip("'\"")) from e
        except OSError as e:
            raise RuntimeError(f"读取技能文件失败：{e}") from e
        return {"name": name, "text": text}

    # ---- 技能：程序内导入 / 删除 ----

    def _skill_root(self, scope: str) -> Path:
        """技能安装位置：global = 所有项目共用，project = 只在当前项目生效。"""
        if scope == "project":
            if self.working_dir is None:
                raise RuntimeError(
                    "项目技能需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。"
                )
            return self.working_dir / ".skysheep" / "skills"
        if scope != "global":
            raise RuntimeError("scope 只能是 global 或 project: " + str(scope))
        return skysheep_home() / "skills"

    async def install_skill(self, source: str, scope: str = "global", overwrite: bool = False) -> dict:
        """把技能装进技能目录，装完立即生效（无需重启）。

        source 可以是本机文件夹、.zip 路径，或 GitHub / Gitee 的仓库链接与
        .zip 直链（网址下载放到工作线程，不卡事件循环）。
        overwrite=True 供技能广场的「更新/重装」用：同名技能整目录替换；
        网址安装会同时记录来源 url，供广场判定「已安装/可更新」。
        """
        root = self._skill_root(scope)
        existing = {s.name for s in self.skills.all()}
        try:
            if str(source).strip().lower().startswith(("http://", "https://")):
                result = await asyncio.to_thread(
                    install_from_url, source, root, existing=existing, overwrite=overwrite
                )
            else:
                result = await asyncio.to_thread(
                    install_skill, source, root, existing=existing, overwrite=overwrite
                )
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        # 重新发现 + 重建系统提示词：新技能马上出现在清单里
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        # 用户在界面上主动装技能：已信任的项目同步指纹，避免刚装完就回到「待确认」。
        # 指明本次动的来源（项目技能目录）：别的来源若也被改过（git pull 塞进来的
        # 项目级 mcp.json 等），不跟着一起洗白（安全审查低危项）
        if scope == "project":
            self.trust.refresh(touched=self._project_skills_dir())
        result["scope"] = scope
        result["skills"] = [
            {
                "name": s.name, "description": s.description,
                "source": s.source, "enabled": s.enabled,
                "scope": s.scope, "scope_projects": list(s.scope_projects),
                "version": s.version,
                "applies": self.skills.applies(s.name),
            }
            for s in self.skills.all()
        ]
        return result

    async def scan_local_skills(self) -> dict:
        """探测本机常见的别家技能目录，找出可以复用的技能候选。

        纯只读探测（Claude Code / agents / Codex 的 home 目录 + 本项目 .claude）：
        找到的候选交给设置页技能页就地列出（「本机现存」面板）勾选，真正的安装
        走既有 skills.install 复制流程，这里不移动、不删除、不修改探测到的任何文件。
        """
        roots = [(label, Path(p).expanduser()) for label, p in LOCAL_SKILL_SOURCES]
        if self.working_dir is not None:
            roots.append(("本项目", self.working_dir / ".claude" / "skills"))
        existing = {s.name for s in self.skills.all()}
        candidates = await asyncio.to_thread(scan_computer_skills, roots, existing)
        return {"candidates": candidates}

    async def delete_skill(self, name: str) -> dict:
        """删除技能目录。全局技能与项目技能都可能重名，按当前加载到的那一份删。

        同名技能两边各存一份时，发现逻辑让全局版遮蔽项目版（见 SkillLoader.discover）
        ——用户看到、点删除的是全局版，所以全局根必须排在候选首位；否则会静默删掉
        被遮蔽的项目版，清单里的技能纹丝不动。项目版被加载时反过来，项目根优先。
        """
        skill = self.skills.get(name)
        if skill is None:
            raise RuntimeError("skill not found: " + name)
        scope = "project" if skill.source == "project" else "global"
        roots = [skysheep_home() / "skills"]
        if self.working_dir is not None:
            roots.append(self.working_dir / ".skysheep" / "skills")
        if scope == "project":
            roots.reverse()
        try:
            result = remove_skill(name, roots)
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        # 顺手抹掉该技能的遗留状态（停用名单 / 使用范围）：不清理的话，
        # 同名技能重新安装后会莫名“装上了却是停用”，用户很难自己定位。
        self.skills.forget(name)
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        if scope == "project":
            # 删掉项目技能也是用户自己的改动；来源限定在项目技能目录
            self.trust.refresh(touched=self._project_skills_dir())
        result["scope"] = scope
        return result

    # ---- MCP：程序内导入 / 删除 / 重连 ----

    def _reload_mcp_configs(self) -> None:
        configs, warnings = load_mcp_configs(
            self._mcp_global_path(),
            self._project_mcp_path_if_trusted(),
        )
        self.mcp_configs = configs
        self.mcp_warnings = warnings

    def _project_skills_dir(self) -> Path | None:
        """当前项目的技能目录（trust.refresh 指明来源用；无项目时为 None）。

        与 _project_skills_dir_if_trusted 的区别：这个不要求项目已信任——
        refresh 的场景恰恰是「用户刚动过它、要判断能否延续信任」。
        """
        if self.working_dir is None:
            return None
        return self.working_dir / ".skysheep" / "skills"

    def _mcp_global_path(self) -> Path:
        return mcp_config_path(skysheep_home())

    def _mcp_project_path(self) -> Path | None:
        """项目级 mcp.json 路径；无项目态返回 None（没有可指向的项目目录）。"""
        if self.working_dir is None:
            return None
        return self.working_dir / ".skysheep" / "mcp.json"

    # ---- workspace trust：项目级配置在获信任前不得自动生效 ----

    @property
    def trust(self) -> WorkspaceTrust:
        """当前项目的信任状态（按项目路径记忆在用户主目录）。"""
        if self._trust is None:
            self._trust = WorkspaceTrust(skysheep_home(), self.working_dir)
        return self._trust

    def _project_mcp_path_if_trusted(self) -> Path | None:
        """未获信任时返回 None：项目级 MCP 配置不得在本轮启动时被读取/拉起。"""
        if self.working_dir is None or not self.trust.is_trusted():
            return None
        return self._mcp_project_path()

    def _project_skills_dir_if_trusted(self) -> Path | None:
        """未获信任时返回 None：项目级技能不得自动发现并注入系统提示词。"""
        if self.working_dir is None or not self.trust.is_trusted():
            return None
        return self.working_dir / ".skysheep" / "skills"

    def _mcp_reconnect_gate(self, name: str) -> bool:
        """自动重连前的放行门（注入 MCPManager，审查 P2-5）。

        显式 trust.revoke 走 _sync_mcp_changes 的 forget 路径，断线任务会被
        取消/查无配置，不经过这里；这里防的是另一条缝：会话运行期间项目配置
        被外部改动导致信任跌成 pending（尚未触发任何配置同步）时，断线的
        项目级服务器仍会按 manager 内存里的旧配置自动重连。按名字判断它是否
        来自项目级 mcp.json，是则要求信任仍然有效；全局配置的服务器不受影响。
        """
        path = self._mcp_project_path()
        if path is None or not path.is_file():
            return True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = (data or {}).get("mcpServers") or {}
        except (OSError, ValueError):
            return True
        if name not in servers:
            return True
        return self._project_mcp_path_if_trusted() is not None

    async def trust_status(self) -> dict:
        """当前项目的信任状态（给前端渲染确认横幅）。"""
        return self.trust.state()

    def trust_list(self) -> dict:
        """全部已信任项目的清单（设置页 · 安全与后台 管理用）。

        记录里是归一化路径（Windows 上小写），当前项目单独标出来；
        含绝对路径，与 session.backups 同理只给本机界面。
        """
        current = str(self.working_dir) if self.working_dir else ""
        items = list_trusted(skysheep_home())
        for e in items:
            e["is_current"] = bool(current) and os.path.normcase(current) == e["path"]
        return {"items": items}

    async def trust_revoke_path(self, path: str) -> dict:
        """按路径撤销信任（收紧动作，远端也允许）。

        撤的是「当前项目」时，还要断开项目级 MCP、重载技能——与 trust.revoke
        同一套收尾；撤别的项目只删记录（它没在跑，无需断开任何东西）。
        """
        root = str(path or "").strip()
        if not root:
            raise RuntimeError("缺少项目路径")
        removed = revoke_by_path(skysheep_home(), root)
        if not removed:
            raise RuntimeError("该路径不在已信任清单里")
        is_current = (
            self.working_dir is not None
            and os.path.normcase(str(self.working_dir)) == os.path.normcase(root)
        )
        if is_current:
            self._trust = None  # 失效缓存，下次访问按已撤销重算
            state = self.trust.state()
            await self._sync_mcp_changes()
            self._reload_project_skills()
            return {**state, "revoked": root}
        return {"revoked": root}

    async def trust_grant(self) -> dict:
        """用户确认信任本项目：记录指纹，并把项目级 MCP/技能接上（无需重启）。"""
        state = self.trust.grant()
        await self._sync_mcp_changes()
        self._reload_project_skills()
        return {**state, "mcp_warnings": list(self.mcp_warnings)}

    async def trust_revoke(self) -> dict:
        """取消信任：断开项目级 MCP 服务器，并重新发现技能（项目级不再生效）。"""
        state = self.trust.revoke()
        await self._sync_mcp_changes()
        self._reload_project_skills()
        return {**state, "mcp_warnings": list(self.mcp_warnings)}

    def _reload_project_skills(self) -> None:
        """按当前信任状态重新发现技能，并把系统提示词刷到所有 agent。"""
        self.skills.project_dir = self._project_skills_dir_if_trusted()
        self.skills.discover()
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())

    async def recheck_trust_before_turn(self) -> None:
        """轮次起点重验工作区信任（审查 P2-4）。

        信任只在启动/切项目/信任操作这些「入口」被求值：会话运行期间项目级
        配置被外部改动（git pull、被注入进程写入）后，已连的项目级 MCP 会
        保持连接、load_skill 也照读磁盘上的新正文——引擎自身没有重查点。
        这里趁每轮对话开始补一次：`state()=="pending" 且 changed=True` 恰好
        表达「曾经信任过、现在配置变了」，此时走与 trust.revoke 同一套差量
        收口（断项目级 MCP、重发现技能），只影响当前项目。
        """
        if self.working_dir is None:
            return
        state = self.trust.state()
        if state.get("state") != STATE_PENDING or not state.get("changed"):
            return
        logger.warning(
            "工作区信任在会话运行期间失效（项目级配置被外部改动），收口项目级 MCP 与技能：%s",
            self.working_dir,
        )
        self._trust = None  # 失效缓存，后续访问按 pending 重算
        await self._sync_mcp_changes()
        self._reload_project_skills()

    async def _connect_mcp_after_boot(self, cfg_warnings: list[str]) -> None:
        """启动后的后台 MCP 连接：不阻塞「服务就绪」，连完注入注册表并广播状态。

        与 setup 共用同一台 manager；配置差量同步/全部重连/关机会先取消本任务
        （_cancel_mcp_boot_connect），避免半路的 connect_all 与它们互踩。
        """
        manager = self.mcp
        if manager is None:
            return
        try:
            tools = await manager.connect_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 单台错误已记进 statuses，这里是兜底
            logger.warning("MCP 后台连接出错：%s", e)
            tools = []
        self.mcp_tools = tools
        self.mcp_warnings = [
            *cfg_warnings,
            *(f"{name}: {st.error}" for name, st in manager.statuses.items() if st.error),
        ]
        self._apply_registry_to_agents()
        self.notify_mcp_updated()

    def notify_mcp_updated(self) -> None:
        """MCP 状态变化后广播事件（无在线连接时静默，如 CLI/测试）。"""
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit({"kind": "mcp_updated"}))
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    async def _cancel_mcp_boot_connect(self) -> None:
        """取消启动期的后台连接并等它收尾（配置同步/全部重连/关机前调用）。

        connect_all 取消时 _connect_one 会走 _close_connection 收割半开的
        keeper 与子进程（见其 except BaseException 路径），不会留孤儿。
        """
        task = self._mcp_connect_task
        self._mcp_connect_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 收尾不挡配置同步/关机
            pass

    async def _sync_mcp_changes(self) -> list[str]:
        """按配置差量同步 MCP 连接（改完配置后调用，无需重启）。

        对比新旧配置，只连接新增/变更的服务器、断开被移除的：改一个预设不再
        把其它已连接的服务器（尤其是带鉴权、重连要重新握手的远程 HTTP）全部
        拽下来重连一遍。工具注册表每次按 manager 的最新状态整体重建——注册表
        本身只是对象图，重建不费时，费时的网络连接已经被差量化了。
        """
        await self._cancel_mcp_boot_connect()
        old = self.mcp_configs or {}
        configs, cfg_warnings = load_mcp_configs(
            self._mcp_global_path(), self._project_mcp_path_if_trusted()
        )
        if self.mcp is None:
            self.mcp = MCPManager(
                configs,
                on_tools_changed=self._on_mcp_tools_changed,
                reconnect_gate=self._mcp_reconnect_gate,
            )
        manager = self.mcp
        for name in [n for n in old if n not in configs]:
            await manager.disconnect_server(name)
            manager.forget_server(name)
        for name in [n for n in configs if n not in old or old[n] != configs[n]]:
            await manager.connect_server(name, configs[name])
        self.mcp_configs = configs
        self.mcp_tools = [t for n in configs for t in manager.tools_for(n)]
        self.mcp_warnings = cfg_warnings + [
            f"{name}: {st.error}" for name, st in manager.statuses.items() if st.error
        ]
        self._apply_registry_to_agents()
        return self.mcp_warnings

    async def _reconnect_mcp(self) -> list[str]:
        """全部重连：整台 manager 重建，顺带清零各服务器的重连计数。

        只给设置页「重连」按钮和信任状态翻转这类「用户明确要重来」的入口用；
        配置增删改走 _sync_mcp_changes 的差量路径，不惊动无关服务器。
        """
        await self._cancel_mcp_boot_connect()
        if self.mcp is not None:
            await self.mcp.shutdown()
        configs, cfg_warnings = load_mcp_configs(
            self._mcp_global_path(), self._project_mcp_path_if_trusted()
        )
        self.mcp_configs = configs
        self.mcp = MCPManager(
            configs,
            on_tools_changed=self._on_mcp_tools_changed,
            reconnect_gate=self._mcp_reconnect_gate,
        )
        self.mcp_tools = await self.mcp.connect_all()
        self.mcp_warnings = cfg_warnings + [
            f"{name}: {st.error}" for name, st in self.mcp.statuses.items() if st.error
        ]
        self._apply_registry_to_agents()
        return self.mcp_warnings

    async def _on_mcp_tools_changed(self, server_name: str) -> None:
        """MCP 服务器推送 tools/list_changed：换上最新工具清单并刷新注册表。"""
        if self.mcp is None:
            return
        self.mcp_tools = [t for n in self.mcp_configs for t in self.mcp.tools_for(n)]
        self._apply_registry_to_agents()

    @staticmethod
    def _mcp_status_list(manager: MCPManager | None) -> list[dict]:
        if manager is None:
            return []
        configs = getattr(manager, "_configs", {})
        out = []
        for n, st in manager.statuses.items():
            cfg = configs.get(n)
            # 审查 S-09：http 明文 + 鉴权头 = Bearer 凭据可被中间人截获。
            # 只给前端一个布尔，headers 内容（含密钥）不下发。
            insecure = bool(
                cfg is not None and cfg.headers
                and str(cfg.url or "").lower().startswith("http://")
            )
            out.append({
                "name": n, "connected": st.connected, "enabled": st.enabled,
                "error": st.error, "tools": st.tool_names,
                "connecting": getattr(st, "connecting", False),
                "reconnecting": getattr(st, "reconnecting", False),
                "restarts": getattr(st, "restarts", 0),
                "insecure_http": insecure,
            })
        return out

    async def import_mcp_servers(
        self,
        *,
        snippet: str = "",
        path: str = "",
        scope: str = "global",
        overwrite: bool = False,
        reconnect: bool = True,
        confirmed: bool = False,
    ) -> dict:
        """导入 MCP 服务：从粘贴的 JSON（snippet）或一个本机 .json 文件（path）。

        安全审查 M10：含 stdio（command）定义时先返回 needs_confirm + 命令清单
        让前端弹一次显式确认，confirmed=true 才真正写盘——连接即执行本机命令，
        不能粘贴一段 JSON 就等于声明允许在本机运行任意程序。
        """
        if snippet.strip():
            servers = parse_snippet(snippet)
        elif path.strip():
            servers = parse_file(path)
        else:
            raise RuntimeError("请粘贴 MCP 配置，或指定一个 .json 文件路径")
        target = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
        if target is None:
            raise RuntimeError("项目级 MCP 配置需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。")
        if not confirmed:
            pending = pending_stdio_commands(servers, target, overwrite=overwrite)
            if pending:
                return {"needs_confirm": True, "pending": pending}
        try:
            result = import_servers(servers, target, overwrite=overwrite, confirmed=True)
        except MCPInstallError as e:
            raise RuntimeError(str(e)) from e

        if reconnect and result["added"]:
            await self._sync_mcp_changes()
        result["needs_confirm"] = False
        result["scope"] = scope
        result["mcp"] = self._mcp_status_list(self.mcp)
        result["mcp_warnings"] = self.mcp_warnings
        if scope == "project":
            # 用户自己在界面上写的项目级配置；来源限定项目级 mcp.json
            self.trust.refresh(touched=self._mcp_project_path())
        self._annotate_untrusted_project_scope(result, scope)
        if result["added"]:
            result["hint"] = "已接入，可直接对话使用" + (
                "（有服务没连上时看下面的错误信息）" if self.mcp_warnings else ""
            )
        elif result["skipped"]:
            result["hint"] = "同名服务已存在，勾选「覆盖同名服务」后重试即可替换"
        return result

    async def save_mcp_server(
        self,
        name: str,
        *,
        command: str = "",
        args: list[str] | None = None,
        url: str = "",
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        readonly: bool = False,
        timeout: float = 0.0,
        scope: str = "global",
        overwrite: bool = True,
    ) -> dict:
        """手工填写一个 MCP 服务（分字段表单），存进 mcp.json。"""
        raw: dict = {}
        if command.strip():
            raw["command"] = command.strip()
            if args:
                raw["args"] = [str(a) for a in args if str(a).strip()]
        if url.strip():
            raw["url"] = url.strip()
        if env:
            raw["env"] = {str(k): str(v) for k, v in env.items()}
        # 请求头仅对 HTTP 传输有意义：stdio 服务带上它只会让人困惑
        if headers and url.strip():
            raw["headers"] = {
                str(k): str(v) for k, v in headers.items() if str(k).strip()
            }
        if readonly:
            raw["readonly"] = True
        if timeout and timeout > 0:
            raw["timeout"] = timeout
        try:
            cfg = normalize_server(raw)
            target = self._mcp_global_path() if scope == "global" else self._mcp_project_path()
            if target is None:
                raise RuntimeError(
                    "项目级 MCP 配置需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。"
                )
            # 表单没有「停用」字段：覆盖一个已停用的同名服务时保留停用状态，
            # 否则只是改个参数保存，服务就被顺手重新启用并拉起（stdio 等于
            # 立即执行本机命令），超出用户这次操作表达的意思
            prior = load_servers(target).get(name.strip())
            if isinstance(prior, dict) and prior.get("enabled") is False:
                raw["enabled"] = False
                cfg = normalize_server(raw)
            # 分字段表单：用户看着 command/args 输入框亲手填的，保存动作本身
            # 就是明确意图（M10 的确认只针对粘贴/导入这种「命令不可见」的路径）
            result = import_servers(
                {name.strip(): cfg}, target, overwrite=overwrite, confirmed=True
            )
        except MCPInstallError as e:
            raise RuntimeError(str(e)) from e
        if result["added"]:
            await self._sync_mcp_changes()
        result["scope"] = scope
        result["mcp"] = self._mcp_status_list(self.mcp)
        result["mcp_warnings"] = self.mcp_warnings
        if scope == "project":
            # 同上：用户自己的改动延续既有信任，来源限定项目级 mcp.json
            self.trust.refresh(touched=self._mcp_project_path())
        self._annotate_untrusted_project_scope(result, scope)
        return result

    def _annotate_untrusted_project_scope(self, result: dict, scope: str) -> None:
        """项目级配置写完了但还没信任本项目时，把「为什么没连上」说清楚。

        否则用户会在设置页加了服务却看不到连接，以为是坏了。这里只补一句提示，
        不自动授信任——项目里可能同时还躺着别的（仓库带来的）服务，一并放行
        不是用户在这一次操作里表达的意思。
        """
        if scope != "project" or self.trust.is_trusted():
            return
        result["needs_trust"] = True
        result["hint"] = (
            "已写入项目配置，但本项目尚未信任：项目自带的配置在确认前不会自动执行。"
            "在顶部横幅点「信任本项目」后即会连接。"
        )

    async def add_mcp_preset(self, name: str, scope: str = "global") -> dict:
        """一键添加内置预设 MCP 服务：按预设原文写入 mcp.json 并立即连接。

        args 里的 {dir} 占位符替换为当前工作目录；同名已存在时不覆盖
        （save_mcp_server 用 overwrite=False，走 skipped 通道）。
        """
        preset = preset_by_name(name)
        if preset is None:
            raise RuntimeError(f"没有这个内置预设：{name}")
        if self.working_dir is None and any("{dir}" in str(a) for a in preset["args"]):
            raise RuntimeError(
                "该预设要用当前项目的工作目录——先在侧栏「项目」区点 ＋ 添加项目。"
            )
        args = [str(a).replace("{dir}", str(self.working_dir or "")) for a in preset["args"]]
        result = await self.save_mcp_server(
            preset["name"],
            command=preset["command"],
            args=args,
            readonly=preset["readonly"],
            scope=scope,
            overwrite=False,
        )
        result["preset"] = preset["name"]
        if result.get("skipped"):
            result["hint"] = f"「{preset['label']}」已经添加过了，直接用即可（或删除后重新添加）"
        elif result.get("added"):
            st = next((m for m in result["mcp"] if m["name"] == preset["name"]), None)
            if st and st["connected"]:
                result["hint"] = (
                    f"✓ 已添加「{preset['label']}」，连上 {len(st['tools'])} 个工具，可直接对话使用"
                )
            elif st:
                result["hint"] = f"已添加「{preset['label']}」，但没连上：{st['error'] or '未知错误'}"
        return result

    def mcp_installed_names(self) -> list[str]:
        """当前 mcp.json（全局+项目）里已有的服务名，前端据此标「已添加」。"""
        return sorted(self.mcp_configs.keys())

    async def delete_mcp_server(self, name: str, scope: str = "global") -> dict:
        """删除一个 MCP 服务并断开它的连接。"""
        project_path = self._mcp_project_path()
        path = self._mcp_global_path() if scope == "global" else project_path
        if path is None:
            raise RuntimeError("项目级 MCP 配置需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。")
        try:
            result = remove_server(name, path)
        except MCPInstallError as e:
            # 全局/项目两边都试试，用户不必知道它当初存在哪
            other = project_path if scope == "global" else self._mcp_global_path()
            if other is None:
                raise RuntimeError(str(e)) from e
            try:
                result = remove_server(name, other)
                path = other
            except MCPInstallError:
                raise RuntimeError(str(e)) from e
        await self._sync_mcp_changes()
        result["scope"] = "project" if path == project_path else "global"
        result["mcp"] = self._mcp_status_list(self.mcp)
        return result

    async def set_mcp_enabled(self, name: str, enabled: bool, scope: str = "global") -> dict:
        """停用/启用一个 MCP 服务：配置原样保留（env/headers 不用重填），停用只是不连接。

        找配置文件时按请求的 scope 优先、另一边兜底（与 delete_mcp_server 一致，
        用户不必知道服务当初存在哪边）。
        """
        project_path = self._mcp_project_path()
        first = self._mcp_global_path() if scope == "global" else project_path
        second = project_path if scope == "global" else self._mcp_global_path()
        candidates = [p for p in (first, second) if p is not None]
        hit: Path | None = None
        for p in candidates:
            servers = load_servers(p)
            if name not in servers:
                continue
            section = dict(servers[name])
            if enabled:
                section.pop("enabled", None)  # 默认即启用，不写冗余字段
            else:
                section["enabled"] = False
            servers[name] = section
            save_servers(p, servers)
            hit = p
            break
        if hit is None:
            raise RuntimeError(f"找不到 MCP 服务：{name}")
        await self._sync_mcp_changes()
        result: dict = {
            "name": name,
            "enabled": enabled,
            "scope": "project" if hit == project_path else "global",
            "mcp": self._mcp_status_list(self.mcp),
            "mcp_warnings": self.mcp_warnings,
        }
        if not enabled:
            result["hint"] = f"已停用「{name}」：它的工具已从 Agent 移除，配置保留，随时可重新启用"
        else:
            st = next((m for m in result["mcp"] if m["name"] == name), None)
            if st and st["connected"]:
                result["hint"] = f"✓ 已启用「{name}」，连上 {len(st['tools'])} 个工具"
            else:
                result["hint"] = f"已启用「{name}」，但没连上：{(st or {}).get('error') or '未知错误'}"
        return result

    # ---- 设置页 ----

    # 子代理设置：读当前值 / 保存并热生效

    def subagent_settings(self) -> dict:
        return {
            "enabled": self.cfg.subagent_enabled,
            "max_iterations": self.cfg.subagent_max_iterations,
            "max_concurrent": self.cfg.subagent_max_concurrent,
            "main_max_iterations": self.cfg.max_iterations,
        }

    async def save_subagent_settings(
        self, *, enabled: bool | None = None, max_iterations: int | None = None,
        max_concurrent: int | None = None,
    ) -> dict:
        """保存子代理设置并立即生效：开关控注册表、轮数与并发热更新任务簿，不用重启。"""
        if max_iterations is not None:
            try:
                max_iterations = int(max_iterations)
            except (TypeError, ValueError):
                raise RuntimeError("迭代轮数要是 1–100 之间的整数") from None
            if not 1 <= max_iterations <= 100:
                raise RuntimeError("迭代轮数要在 1–100 之间")
        if max_concurrent is not None:
            try:
                max_concurrent = int(max_concurrent)
            except (TypeError, ValueError):
                raise RuntimeError("并发上限要是 1–8 之间的整数") from None
            if not 1 <= max_concurrent <= 8:
                raise RuntimeError("并发上限要在 1–8 之间")
        try:
            set_subagent_settings_in_config(
                enabled=enabled, max_iterations=max_iterations,
                max_concurrent=max_concurrent,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        self.tasks.set_max_iterations(self.cfg.subagent_max_iterations)
        self.tasks.set_max_concurrent(self.cfg.subagent_max_concurrent)
        if enabled is not None:
            self._apply_registry_to_agents()
        return self.subagent_settings()

    # ---- 高级设置（主循环轮数 / 上下文上限 / 压缩保留 / 目录限制 / 开机自启） ----

    def _apply_advanced_to_agents(self) -> None:
        """把改动推给所有活着的 Agent，不用重启应用。"""
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.max_iterations = self.cfg.max_iterations
            ag.context_limit_tokens = limit
            ag.compaction_keep_recent = self.cfg.compaction_keep_recent
            ag.compaction_trigger = self.cfg.compaction_trigger
            ag.restrict_to_workdir = self.cfg.restrict_to_workdir

    def _startup_status(self) -> dict:
        from .. import startup

        try:
            return startup.status()
        except Exception as e:  # noqa: BLE001 - 注册表异常不该让设置页打不开
            return {"supported": startup.is_supported(), "enabled": False,
                    "command": "", "current": "", "stale": False, "error": str(e)}

    def _hotkey_pref(self) -> str:
        """全局唤起热键偏好（校验在 desktop._parse_hotkey，注册时非法值自带回退，
        这里只读不验避免两处规则漂移）。desktop 是打包壳、服务进程里没有，
        所以独立实现读取，不 import。"""
        try:
            return str(self._read_ui_prefs().get("hotkey") or "")
        except Exception:
            return ""

    def hooks_settings(self) -> dict:
        """当前 config.toml 里的钩子规则（设置页 · Hooks 面板）。

        同时回传启用中的规则条数：未生效与没配置是两回事，界面要能区分；
        另带最近执行记录——钩子自身故障（非 0 非 2 退出码）按设计静默放行，
        但必须让用户在设置页看到它失效了，而不是「配了像没配一样」。
        """
        raw = load_raw_config()
        pre, post, stop = hooks_from_config(raw)
        hooks = raw.get("hooks") if isinstance(raw.get("hooks"), dict) else {}

        def _pub(key: str) -> list[dict]:
            out: list[dict] = []
            for item in hooks.get(key) or []:
                if isinstance(item, dict) and str(item.get("command", "")).strip():
                    out.append({
                        "match": str(item.get("match", "*") or "*"),
                        "command": str(item["command"]).strip(),
                        "timeout_s": float(item.get("timeout_s") or 10),
                        "enabled": bool(item.get("enabled", True)),
                    })
            return out

        return {
            "pre": _pub("pre_tool_use"),
            "post": _pub("post_tool_use"),
            "stop": _pub("stop"),
            "active_pre": len([r for r in pre if r.enabled]),
            "active_post": len([r for r in post if r.enabled]),
            "active_stop": len([r for r in stop if r.enabled]),
            "recent": recent_hook_runs(),
            "config_path": str(config_path()),
            "tool_names": sorted(t.name for t in self._build_full_registry().all()),
        }

    async def save_hooks_settings(self, params: dict) -> dict:
        """保存钩子规则并热生效（重建 HookRunner 并推给所有活动 Agent）。

        钩子命令等价于用户自己敲的命令（来自用户配置），不走权限门——但它是
        「工具调用前的最后一道人工闸门」，所以保存动作本身写进日志便于回查。
        """
        pre = params.get("pre")
        post = params.get("post")
        stop = params.get("stop")
        try:
            set_hooks_in_config(
                pre=None if pre is None else list(pre),
                post=None if post is None else list(post),
                stop=None if stop is None else list(stop),
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self._reload_hooks()
        return self.hooks_settings()

    async def test_hook(self, params: dict) -> dict:
        """钩子测试器：拿示例参数把一条钩子命令实跑一遍，回完整结果。

        与 hooks.save 同一信任模型——命令来自本机界面上的用户输入，等价于手敲，
        不经过权限门，因此同列为本机专属方法（远程客户端不可调用）。
        只执行、不落配置：测试的命令不保存，也不进「最近执行」记录。
        """
        kind = str(params.get("kind") or "pre")
        if kind not in ("pre", "post", "stop"):
            raise RuntimeError("钩子类型只能是 pre / post / stop")
        command = str(params.get("command") or "").strip()
        if not command:
            raise RuntimeError("请先填写要测试的命令")
        if "\n" in command:
            raise RuntimeError("命令不能包含换行")
        try:
            timeout_s = float(params.get("timeout_s") or 10)
        except (TypeError, ValueError):
            raise RuntimeError("超时秒数必须是数字") from None
        tool = str(params.get("tool") or "").strip() or "write_file"
        raw_input = params.get("input")
        input_dict = raw_input if isinstance(raw_input, dict) else {}
        rule = HookRule(match="*", command=command, timeout_s=timeout_s)
        runner = HookRunner(
            [rule] if kind == "pre" else [],
            [rule] if kind == "post" else [],
            working_dir=self.working_dir,
            stop_rules=[rule] if kind == "stop" else [],
        )
        return await runner.test_run(kind, rule, tool, input_dict)

    def _reload_hooks(self) -> None:
        """重新读 hooks 配置并推给基础 Agent 与所有会话 Agent（不重启即生效）。"""
        raw_cfg = load_raw_config()
        pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
        # 无规则时置 None：Agent 循环里的判断是「hooks is not None」，
        # 空 Runner 也会走一遍调用链，没必要
        self.hooks = HookRunner(pre_rules, post_rules, working_dir=self.working_dir,
                                stop_rules=stop_rules) \
            if (pre_rules or post_rules or stop_rules) else None
        if hasattr(self, "_base_agent") and self._base_agent is not None:
            self._base_agent.hooks = self.hooks
        for ag in self._for_each_agent():
            ag.hooks = self.hooks

    def advanced_settings(self) -> dict:

        return {
            "max_iterations": self.cfg.max_iterations,
            "context_limit_tokens": self.cfg.context_limit_tokens,
            "compaction_keep_recent": self.cfg.compaction_keep_recent,
            "compaction_trigger": self.cfg.compaction_trigger,
            "restrict_to_workdir": self.cfg.restrict_to_workdir,
            "computer_control": self.cfg.computer_control,
            "browser_control": self.cfg.browser_control,
            "daily_token_budget": self.cfg.daily_token_budget,
            "context_limit_tokens_effective": self._context_limit(),
            "current_provider": self.provider_name,
            "current_provider_context_limit": (
                self._current_provider_cfg().context_limit if self._current_provider_cfg() else 0
            ),
            "home": str(skysheep_home()),
            "logs_dir": str(skysheep_home() / "logs"),
            "working_dir": str(self.working_dir or ""),
            "autostart": self._startup_status(),
            # 全局唤起热键（仅 Windows 桌面版生效；其它平台 supported=False）
            "hotkey": self._hotkey_pref(),
            "hotkey_supported": sys.platform == "win32",
        }

    async def save_advanced_settings(self, params: dict) -> dict:
        """保存高级设置并热生效；含开机自启开关（Windows 当前用户 Run 项）。"""
        ints: dict[str, int] = {}
        for key in ("max_iterations", "context_limit_tokens", "compaction_keep_recent"):
            if params.get(key) is not None and params.get(key) != "":
                try:
                    ints[key] = int(params[key])
                except (TypeError, ValueError):
                    raise RuntimeError(f"{key} 需要一个整数") from None
        # 压缩触发比例：前端按百分数编辑，传比例（0.5–0.98）；越界在 config 层报错
        trigger = params.get("compaction_trigger")
        if trigger is not None and trigger != "":
            try:
                trigger = float(trigger)
            except (TypeError, ValueError):
                raise RuntimeError("压缩触发比例需要一个数字") from None
        else:
            trigger = None
        restrict = params.get("restrict_to_workdir")
        budget = params.get("daily_token_budget")
        if budget is not None and budget != "":
            try:
                budget = int(budget)
            except (TypeError, ValueError):
                raise RuntimeError("每日 token 预算需要一个整数") from None
        else:
            budget = None
        try:
            set_advanced_settings_in_config(
                max_iterations=ints.get("max_iterations"),
                context_limit_tokens=ints.get("context_limit_tokens"),
                compaction_keep_recent=ints.get("compaction_keep_recent"),
                compaction_trigger=trigger,
                restrict_to_workdir=None if restrict is None else bool(restrict),
                computer_control=(
                    None if params.get("computer_control") is None
                    else bool(params.get("computer_control"))
                ),
                browser_control=(
                    None if params.get("browser_control") is None
                    else bool(params.get("browser_control"))
                ),
                daily_token_budget=budget,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        self._apply_advanced_to_agents()
        # 电脑/浏览器控制开关改变了工具清单：重建基础 Agent 与所有会话运行时的注册表
        # （与 MCP 热生效同一套机制；运行中的轮次下一次调用时自然使用新清单）
        self._apply_registry_to_agents()
        self.tasks.set_restrict_to_workdir(self.cfg.restrict_to_workdir)

        # 全局唤起热键改键：存 ui.json 的 hotkey 键（白名单校验在 desktop._parse_hotkey，
        # 非法值注册时静默回退默认）。生效在下次启动（热键线程随窗口创建）。空串 = 回默认。
        hotkey = params.get("hotkey")
        if hotkey is not None:
            combo = str(hotkey).strip()
            try:
                await self._write_ui_prefs({"hotkey": combo or None})
            except Exception:
                pass

        autostart = params.get("autostart")
        if autostart is not None:
            from .. import startup

            try:
                startup.set_enabled(bool(autostart))
            except Exception as e:  # noqa: BLE001 - 写注册表失败：回可读错误，设置项不落
                raise RuntimeError(f"开机自启设置失败：{e}") from None
        return self.advanced_settings()

    # ---- 本地目录 / 诊断（设置页的系统集成入口） ----

    def _home_subdir(self, kind: str) -> Path:
        """设置页允许打开的固定目录（kind 是枚举，不收任意路径）。"""
        base = skysheep_home()
        mapping = {
            "home": base,
            "logs": base / "logs",
            "backups": base / "backups",
            "skills": base / "skills",
            "workdir": self.working_dir,
        }
        target = mapping.get(str(kind or ""))
        if target is None:
            raise RuntimeError("未知的目录类型：" + str(kind))
        if target == self.working_dir and self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，没有工作目录可打开——先在侧栏「项目」区点 ＋ 添加项目。"
            )
        return target

    def open_path(self, kind: str) -> dict:
        """用系统文件管理器打开一个固定目录（日志/数据/工作目录）。"""
        from .. import support

        target = self._home_subdir(kind)
        try:
            support.open_folder(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开目录失败：{e}") from None
        return {"path": str(target)}

    def open_external(self, target: str, local: bool = True) -> dict:
        """用系统默认程序打开一个 http(s) 链接（反馈页/下载页/注册页）。

        只接受 http(s) URL；目标由前端界面写死或用户在向导里点选，
        不作为任意跳转接口。远端调用（审查 P1-3 收口）不在服务端代开
        ——那会让持令牌设备在别人桌面上弹任意网页——而是把 URL 原样
        返回（remote=True），由远端自己的浏览器打开。
        """
        from .. import support

        target = str(target or "").strip()
        if not target.startswith(("http://", "https://")):
            raise RuntimeError("只允许打开 http(s) 链接")
        if not local:
            return {"url": target, "remote": True}
        try:
            support.open_external(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开链接失败：{e}") from None
        return {"url": target}

    # ---- 首启激活：演示模式 / 本地 Ollama 检测（无 Key 用户的两条出路） ----

    async def demo_enable(self) -> dict:
        """开启「演示模式」：写入 kind=fake 的 demo 服务并切换过去（无需任何 Key）。

        FakeProvider 脚本化回放一轮真实工具调用（list_dir），让用户在配 Key 之前
        看到完整 Agent 工作方式；正式使用时在设置里删除该服务或直接配 Key 切换。
        """
        update_provider_in_config(
            "demo",
            kind="fake",
            model="演示模式",
            api_key="demo",
            set_default=True,
            supports_vision=False,
            supports_reasoning=False,
        )
        self.cfg = load_config()
        await self.switch_model("demo")
        return {"provider": "demo", "model": "演示模式"}

    async def ollama_detect(self) -> dict:
        """探测本机 Ollama（localhost:11434）是否在运行、有哪些模型。

        首启向导用它把「装过 Ollama 的用户」从「必须去注册 API Key」的
        路径里解救出来——检测到即提示一键启用，零 Key 零费用。
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                resp = await client.get("http://localhost:11434/api/tags")
            resp.raise_for_status()
            models = [
                str(m.get("name") or "")
                for m in (resp.json().get("models") or [])
                if m.get("name")
            ]
        except Exception:  # noqa: BLE001 - 没装/没起/端口不通：都视为不可用
            return {"available": False, "models": []}
        return {"available": True, "models": models}

    async def ollama_enable(self, model: str = "") -> dict:
        """启用本地 Ollama：把检测到的模型写入 ollama 预设并切换过去。"""
        if not model:
            det = await self.ollama_detect()
            models = det.get("models") or []
            if not det.get("available") or not models:
                raise RuntimeError("没有检测到本机 Ollama（或其中没有任何模型）。"
                                   "请先安装并启动 Ollama，再回来点「检测」。")
            model = models[0]
        update_provider_in_config("ollama", model=model, set_default=True)
        self.cfg = load_config()
        await self.switch_model("ollama", model=model)
        return {"provider": "ollama", "model": model}

    def export_diagnostics(self) -> dict:
        """打包诊断 zip（版本/环境/日志/打码后的配置）并打开所在目录。"""
        from .. import support

        try:
            target = support.build_diagnostic_zip(skysheep_home())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"生成诊断包失败：{e}") from None
        try:
            support.reveal(target)
        except Exception:  # noqa: BLE001 - 打不开文件夹不是失败
            pass
        return {"path": str(target), "size": target.stat().st_size}

    async def open_workspace_file(self, path: str) -> dict:
        """用系统默认程序打开工作目录内的文件（文件预览面板的「打开」按钮）。

        只接受工作目录内的路径；可执行/脚本类扩展名一律退化为「在资源管理器里
        定位」，避免点一下文件名就把工作目录里的脚本跑起来。
        """
        from .. import support

        if self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，文件预览不可用——先在侧栏「项目」区点 ＋ 添加项目。"
            )
        raw = str(path or "").strip()
        if not raw:
            raise RuntimeError("缺少文件路径")
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target = target.resolve()
            target.relative_to(Path(self.working_dir).resolve())
        except (OSError, ValueError):
            raise RuntimeError("只能打开工作目录内的文件") from None
        try:
            action = support.open_with_default_app(target)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开失败：{e}") from None
        return {"path": str(target), "action": action}

    # ---- 子代理：定义管理（独立设置页） ----

    def _subagent_provider(self, provider_name: str, model: str, reasoning: str):
        """按子代理定义解析出 Provider。

        provider 留空 = 跟随主对话当前模型（用临时实例，不污染主 provider 的
        思考强度状态）；指定了服务就按 (服务, 模型, 思考强度) 现场构建。
        """
        provider_name = (provider_name or "").strip()
        model = (model or "").strip()
        reasoning = (reasoning or "").strip().lower()
        if provider_name and provider_name in self.cfg.providers:
            data = self.cfg.providers[provider_name].model_dump()
            if model:
                data["model"] = model
            if reasoning:
                data["reasoning_effort"] = reasoning
            return build_provider(provider_name, ProviderConfig(**data))
        if self.provider is None:
            raise RuntimeError("主对话当前没有可用模型，子代理无法启动——先到模型服务里配好 Key")
        if reasoning and self.provider_name and self.provider_name in self.cfg.providers:
            pc = self.cfg.providers[self.provider_name].model_copy(
                update={"reasoning_effort": reasoning}
            )
            return build_provider(self.provider_name, pc)
        return self.provider

    def _subagent_registry(self, policy):
        """把工具范围策略解析成子代理注册表。

        派生工具（spawn_agent/check_task/wait_task）永远不进子代理——禁止递归派生；
        就算给了写工具，SubagentGate 也会自动拒绝需要确认的操作。
        电脑控制七件套不随「全部工具 / 仅只读」顺手进子代理（截屏/窗口列表属于
        隐私敏感只读）；勾选模式是用户显式点名，不受此限。
        """
        base = self._base_agent.registry
        exclude = {"spawn_agent", "check_task", "wait_task"}
        if isinstance(policy, str):
            policy = (policy or "").strip().lower() or "readonly"
            exclude |= set(COMPUTER_TOOL_NAMES)  # 桶装策略：电脑控制一律不带
            if policy == "all":
                tools = [t for t in base.all() if t.name not in exclude]
            else:
                tools = [
                    t for t in base.all()
                    if t.safety == Safety.READONLY and t.name not in exclude
                ]
        else:
            want = {str(n).strip() for n in (policy or []) if str(n).strip()}
            tools = [t for t in base.all() if t.name in want and t.name not in exclude]
        if not tools:
            raise RuntimeError("子代理工具集为空：至少要有一个可用工具")
        return ToolRegistry(tools)

    def subagents_detail(self) -> dict:
        """「子代理」设置页的一次性数据：全局设置 + 定义 + 可选项。"""
        providers = []
        for name, pc in self.cfg.providers.items():
            models = list(pc.models) or ([pc.model] if pc.model else [])
            providers.append({
                "name": name,
                "model": pc.model,
                "models": models,
                "has_key": resolve_api_key(name, pc) is not None,
            })
        tools = [
            {"name": t.name, "safety": t.safety.value, "description": t.description}
            for t in self._base_agent.registry.all()
            if t.name not in ("spawn_agent", "check_task", "wait_task")
        ]
        return {
            **self.subagent_settings(),
            "builtin": {t: ov.model_dump() for t, ov in self.subagent_store.builtin.items()},
            "builtin_display": dict(BUILTIN_DISPLAY),
            # 生效描述（覆盖值优先，否则内置默认）+ 默认角色提示词（编辑框占位展示）
            "builtin_desc": {
                t: (
                    ov.description.strip()
                    or BUILTIN_DESCRIPTIONS.get(t, "")
                )
                for t, ov in self.subagent_store.builtin.items()
            },
            "builtin_default_prompt": dict(BUILTIN_ROLE_PROMPTS),
            "custom": [d.model_dump() for d in self.subagent_store.custom],
            "providers": providers,
            "tools": tools,
            "reasoning_efforts": [
                {"value": v, "label": REASONING_EFFORT_LABELS.get(v, v)}
                for v in REASONING_EFFORTS
            ],
        }

    async def save_subagent_builtin(
        self, agent_type: str, *, provider: str = "", model: str = "", reasoning: str = "",
        description: str = "", prompt: str = "",
    ) -> dict:
        """内置子代理的定制：模型/思考强度覆盖 + 说明与角色提示词的改写。

        description/prompt 留空 = 恢复该内置型的内置默认（不是删掉功能）。"""
        if provider and provider not in self.cfg.providers:
            raise RuntimeError("未知的模型服务: " + provider)
        if agent_type not in BUILTIN_AGENT_TYPES:
            raise RuntimeError("未知的内置子代理: " + str(agent_type))
        try:
            ov = self.subagent_store.set_override(
                agent_type, provider=provider, model=model, reasoning=reasoning,
                description=description, prompt=prompt,
            )
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        self._apply_registry_to_agents()
        return {"agent_type": agent_type, **ov.model_dump()}

    async def save_subagent_custom(
        self,
        *,
        name: str,
        description: str = "",
        prompt: str = "",
        tools="readonly",
        provider: str = "",
        model: str = "",
        reasoning: str = "",
        enabled: bool = True,
    ) -> dict:
        """新建或更新一个自定义子代理（同名即更新），保存后立即可用。"""
        try:
            name = validate_subagent_name(name)
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        description = (description or "").strip()[:500]
        prompt = (prompt or "").strip()[:20_000]
        reasoning = (reasoning or "").strip().lower()
        if reasoning and reasoning not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 留空 / " + " / ".join(REASONING_EFFORTS))
        if provider and provider not in self.cfg.providers:
            raise RuntimeError("未知的模型服务: " + provider)
        known = {
            t.name for t in self._base_agent.registry.all()
        } - {"spawn_agent", "check_task", "wait_task"}
        if isinstance(tools, str):
            tools = tools.strip().lower() or "readonly"
            if tools not in ("all", "readonly"):
                raise RuntimeError("工具范围只能是 全部工具 / 仅只读 / 勾选工具列表")
        else:
            want = [str(n).strip() for n in (tools or []) if str(n).strip()]
            if not want:
                raise RuntimeError("自定义工具范围至少要勾选一个工具")
            unknown = sorted(set(want) - known)
            if unknown:
                raise RuntimeError("未知工具: " + "、".join(unknown))
            tools = want
        d = SubagentDef(
            name=name,
            description=description,
            prompt=prompt,
            tools=tools,
            provider=provider.strip(),
            model=model.strip(),
            reasoning=reasoning,
            enabled=bool(enabled),
        )
        self.subagent_store.upsert_custom(d)
        self._apply_registry_to_agents()  # 刷新 spawn_agent 的说明文字
        return d.model_dump()

    async def delete_subagent_custom(self, name: str) -> dict:
        try:
            self.subagent_store.remove_custom((name or "").strip())
        except SubagentDefError as e:
            raise RuntimeError(str(e)) from e
        self._apply_registry_to_agents()
        return {"removed": name}

    @staticmethod
    def _mask_key(key: str | None) -> str:
        if not key:
            return ""
        if len(key) <= 10:
            return "已设置"
        return key[:6] + "…" + key[-4:]

    async def providers_detail(self) -> dict:
        """设置页的模型服务清单（Key 只回传掩码，绝不回传全文）。"""
        detail = {}
        for name, pc in self.cfg.providers.items():
            key = resolve_api_key(name, pc)
            detail[name] = {
                "kind": pc.kind,
                "label": pc.label or name,  # 界面显示名（预设如「智谱」「小米 Mimo」）
                "base_url": pc.base_url or "",
                "model": pc.model,
                "has_key": key is not None,
                "key_mask": self._mask_key(key),
                "key_from_env": bool(key and not pc.api_key),
                "is_default": name == self.cfg.default,
                "is_active": name == self.provider_name,
                "active_model": self.provider_model if name == self.provider_name else "",
                # 内置预设不可删除：删掉 config 里的条目它也仍会被合并回来
                "is_preset": name in PRESETS,
                "models": list(pc.models),
                "supports_reasoning": bool(pc.supports_reasoning),
                "supports_vision": bool(pc.supports_vision),
                "reasoning_effort": pc.reasoning_effort,
                "context_limit": pc.context_limit,
                "effective_context_limit": pc.effective_context_limit(
                    self.cfg.context_limit_tokens
                ),
                "global_context_limit": self.cfg.context_limit_tokens,
                "temperature": pc.temperature,
                "proxy": pc.proxy or "",
                "price_in": pc.price_in,
                "price_out": pc.price_out,
                "price_cache": pc.price_cache,
            }
        return {
            "config_path": str(config_path()),
            "home_dir": str(skysheep_home()),
            "db_path": str(db_path()),
            "default": self.cfg.default,
            "active": self.provider_name,
            "provider_error": self.provider_error,
            "disabled": disabled_providers_in_config(),  # 已隐藏的内置服务，可恢复
            "providers": detail,
        }

    async def add_provider(
        self,
        name: str,
        *,
        kind: str = "openai",
        base_url: str = "",
        model: str = "",
        api_key: str | None = None,
        set_default: bool = False,
        models: list[str] | None = None,
    ) -> dict:
        """新增自定义模型服务（写入 config.toml）；当前没有可用模型时顺带热启用。

        models 为添加时一并启用的模型列表（可多选），model 是其中当前使用的那个。
        """
        try:
            add_provider_to_config(
                name,
                kind=kind,
                base_url=base_url,
                model=model,
                api_key=api_key or None,
                set_default=set_default,
                models=models,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        clean = name.strip()
        activated = False
        # 缺模型时顺手把新服务用起来；已有可用模型就不打断用户当前选择
        if self.provider is None:
            try:
                self.provider = self._build_provider(clean)
                self.provider_error = None
                for ag in self._for_each_agent():
                    ag.provider = self.provider
                activated = True
            except RuntimeError as e:
                self.provider_error = str(e)
        return {
            "added": clean,
            "activated": activated,
            "provider_error": self.provider_error,
            "model": getattr(self.provider, "model", "") if activated else "",
        }

    async def delete_provider(self, name: str) -> dict:
        """从列表里删掉模型服务：自定义服务彻底删除，内置服务改为隐藏（可恢复）。"""
        try:
            remove_provider_from_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        was_active = name == self.provider_name
        if was_active:
            self.provider = None
            self.provider_name = ""
            # 让界面说明白"为什么现在没有模型可用"，而不是显示空的 provider/model
            self.provider_error = f"已删除正在使用的模型服务「{name}」，请重新选择一个"
        return {
            "removed": name,
            "default": self.cfg.default,
            "was_active": was_active,
            "hidden": name in PRESETS,  # 内置服务只是隐藏，可恢复
        }

    async def restore_provider(self, name: str) -> dict:
        """把隐藏的服务放回模型服务列表（内置服务按最新出厂预设重建）。"""
        try:
            restore_provider_in_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"restored": name}

    async def set_provider_enabled(self, name: str, enabled: bool) -> dict:
        """启用/停用一个模型服务（停用只隐藏，配置数据保留）。"""
        try:
            if enabled:
                restore_provider_in_config(name)
            else:
                disable_provider_in_config(name)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        was_active = not enabled and name == self.provider_name
        if was_active:
            self.provider = None
            self.provider_name = ""
            self.provider_error = f"已停用正在使用的模型服务「{name}」，请重新选择一个"
        return {"name": name, "enabled": enabled, "was_active": was_active}

    async def probe_models(
        self,
        *,
        name: str = "",
        kind: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> dict:
        """检测某个模型服务当前可用的模型名（用于界面里一键选取，免手输）。

        界面上可能改了地址/Key 还没保存，所以优先用传入的值；没传的部分
        回落到该服务已保存的配置（含环境变量里的 Key）。
        """
        pc = self.cfg.providers.get(name) if name else None
        eff_kind = (kind or (pc.kind if pc else "openai")).strip()
        eff_url = (base_url or (pc.base_url if pc else "") or "").strip()
        eff_key = (api_key or "").strip()
        if not eff_key and pc is not None:
            eff_key = resolve_api_key(name, pc) or ""

        models = await probe_provider_models(
            kind=eff_kind, base_url=eff_url or None, api_key=eff_key or None
        )
        return {
            "provider": name,
            "kind": eff_kind,
            "base_url": eff_url,
            "count": len(models),
            "models": models,
        }

    async def probe_context(
        self,
        *,
        name: str = "",
        kind: str = "",
        base_url: str = "",
        api_key: str = "",
        model: str = "",
    ) -> dict:
        """探测某个模型服务的上下文窗口上限（供「上下文上限」一键填入）。

        与 probe_models 同一套取值口径：界面改了还没保存的地址/Key 优先，
        没传的部分回落到已保存配置（含环境变量里的 Key）；模型名同样优先
        用传入值，回落到该服务当前启用的模型。
        """
        pc = self.cfg.providers.get(name) if name else None
        eff_kind = (kind or (pc.kind if pc else "openai")).strip()
        eff_url = (base_url or (pc.base_url if pc else "") or "").strip()
        eff_key = (api_key or "").strip()
        if not eff_key and pc is not None:
            eff_key = resolve_api_key(name, pc) or ""
        eff_model = (model or (pc.model if pc else "") or "").strip()

        out = await probe_context_limit(
            kind=eff_kind, base_url=eff_url or None, api_key=eff_key or None, model=eff_model
        )
        return {"provider": name, "kind": eff_kind, "model": eff_model, **out}

    async def save_provider(
        self,
        name: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        kind: str | None = None,
        set_default: bool = False,
        supports_reasoning: bool | None = None,
        supports_vision: bool | None = None,
        context_limit: int | None = None,
        temperature: float | str | None = None,
        price_in: float | None = None,
        price_out: float | None = None,
        price_cache: float | None = None,
        proxy: str | None = None,
    ) -> dict:
        """保存 provider 字段到 config.toml 并热生效（若是当前使用的模型）。"""
        if name not in self.cfg.providers:
            raise RuntimeError("unknown provider: " + name)
        # 设为默认时把「当前正在用的模型」也固化进 config，下次启动即用它
        if set_default and not model and name == self.provider_name and self.provider_model:
            model = self.provider_model
        update_provider_in_config(
            name,
            base_url=(base_url or None),
            model=(model or None),
            api_key=(api_key or None),
            kind=(kind or None),
            set_default=set_default,
            supports_reasoning=supports_reasoning,
            supports_vision=supports_vision,
            context_limit=context_limit,
            temperature=temperature,
            price_in=price_in,
            price_out=price_out,
            price_cache=price_cache,
            proxy=proxy,
        )
        self.cfg = load_config()
        rebuilt = False
        if name == self.provider_name or self.provider is None:
            try:
                self.provider = self._build_provider(name)
                self.provider_error = None
                for ag in self._for_each_agent():
                    ag.provider = self.provider
                rebuilt = True
            except RuntimeError as e:
                self.provider_error = str(e)
        # 上下文上限跟服务走：改了当前服务的上限要立刻作用到活着的 Agent
        limit = self._context_limit()
        for ag in self._for_each_agent():
            ag.context_limit_tokens = limit
        return {
            "saved": name,
            "rebuilt": rebuilt,
            "provider_error": self.provider_error,
            "model": getattr(self.provider, "model", ""),
            "reasoning": self.reasoning_state(),
        }

    async def _switch_after_removal(self) -> dict | None:
        """当前会话被删除/移走后：优先切到最近的其他会话；一个都不剩就回到"待新建"状态。"""
        latest = await self.store.latest_session(self._cur_project_id())
        if latest is not None:
            await self.resume_session(latest.id)
            return {"id": latest.id, "title": latest.title}
        self.session = None
        self._base_agent.load_history([Message.system(self.compose_system())])
        return None

    async def delete_session(self, session_id: str) -> dict:
        # 归属校验：别的项目的会话不能凭枚举到的 id 删除（安全审查 B6）
        await self._get_owned_session(session_id)
        # 先停掉该会话正在跑的 turn，再清理 runtime，最后删数据
        self.cancel_run(session_id)
        rt = self.runtimes.pop(session_id, None)
        was_active = self.session is not None and self.session.id == session_id
        if rt is not None:
            # 排队中的轮次随 runtime 一起消失，Future 必须逐个落空：
            # 不然那些发消息的请求永远等不到响应（请求挂死 + 协程泄漏）
            for item in list(rt.queue):
                item.fail(RuntimeError("会话已删除"))
            rt.queue.clear()
            if was_active and self._base_queue:
                # 懒创建窗口排进基底队列的消息同属这个会话，一并落空
                for item in list(self._base_queue):
                    item.fail(RuntimeError("会话已删除"))
                self._base_queue.clear()
            self._forget_runtime(rt)
            task = rt.run_task
            if task is not None and not task.done():
                # 等被取消的轮次收尾（含 shield 保护的落库）跑完再删行：
                # 落库在取消后仍会继续执行，删早了消息会插在删除之后，留下孤儿行
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10)
                except Exception:  # noqa: BLE001 - 超时/任务异常都不拦删除本身
                    pass
        await self.store.delete_session(session_id)
        # 检查点（改前文件快照）随会话一起清：会话没了，快照不该继续占磁盘
        await asyncio.to_thread(self.checkpoints.forget_session, session_id)
        self._manually_named.discard(session_id)
        self._default_model_sessions.discard(session_id)
        switched = await self._switch_after_removal() if was_active else None
        # 广播给其它连接（另一窗口 / 手机端）：删掉的会话从列表与标签里消失
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "deleted": True})
        return {
            "deleted": session_id,
            "switched_to": switched,
            "new_active": self.session.id if self.session else None,
        }

    async def rename_session(self, session_id: str, title: str) -> dict:
        await self._get_owned_session(session_id)
        title = title.strip()[:80]  # 限长：超长标题撑爆侧栏/标签布局
        if not title:
            raise RuntimeError("标题不能为空")
        await self.store.set_title(session_id, title)
        # 用户手改过名字：自动标题不再覆盖（含首轮在跑时才改名的竞态窗口）
        self._manually_named.add(session_id)
        if self.session and self.session.id == session_id:
            self.session.title = title
        # 广播给其它连接（另一窗口 / 手机端），让它们的标签与侧栏同步改名
        for ws_emit in list(self.ws_emitters):
            try:
                await ws_emit({"kind": "session_updated",
                               "session_id": session_id, "title": title})
            except Exception:
                pass
        return {"id": session_id, "title": title}

    async def pin_session(self, session_id: str, pinned: bool) -> dict:
        sess = await self._get_owned_session(session_id)
        await self.store.set_pinned(session_id, pinned)
        # 广播给其它连接：另一窗口的侧栏同步置顶位（列表整条刷新拿新状态）
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title})
        return {"id": session_id, "pinned": pinned}

    async def archive_session(self, session_id: str, archived: bool) -> dict:
        """归档/取消归档：归档后会话从侧栏与搜索里消失，可在归档弹窗恢复。

        归档（且此前未归档、未提炼过）时后台自动提炼用户记忆：归档通常意味着
        「这事完了」，是判断哪些信息值得长期记住的自然时机；提炼失败静默，
        不影响归档本身。提炼过一次就不再重来（取消归档再归档也不重复花钱）。
        """
        sess = await self._get_owned_session(session_id)
        already = bool(sess.archived)
        await self.store.set_archived(session_id, archived)
        if archived and not already \
                and not await self.store.get_session_memory_digested(session_id):
            self._schedule_memory_digest(session_id)
        # 广播给其它连接：归档/恢复后另一窗口的侧栏与搜索同步隐/现。
        # archived 标记供前端把该会话的标签一并收掉——归档＝这条对话收摊，
        # 侧栏行与标签栏保持同步（恢复后从归档弹窗重新点开即可）。
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title,
                            "archived": archived})
        return {"id": session_id, "archived": archived}

    # ---- 归档自动记忆（提炼纯函数在 tools/memory.py） ----

    def _schedule_memory_digest(self, session_id: str) -> None:
        if not self.cfg.memory_digest:
            return  # 设置 · 全局记忆里关掉了归档自动记忆（保存即热生效）
        if session_id in self._digesting:
            return
        self._digesting.add(session_id)
        try:
            spawn_bg(self._memory_digest(session_id))
        except RuntimeError:
            self._digesting.discard(session_id)  # 无事件循环（如纯测试环境）

    async def _memory_digest(self, sid: str) -> None:
        """归档后用当前模型通读会话，提炼可长期记住的信息写入用户记忆。

        与 _auto_title 同一套边界：走当前 provider 但失败静默，绝不影响主流程；
        写入成功后像 save_instructions 一样全局刷新系统提示词，让所有会话
        下一轮就带上新记忆。
        """
        try:
            if getattr(self.provider, "demo_mode", False):
                return  # 演示模式不消耗脚本组
            transcript = digest_transcript(await self.store.load_messages(sid))
            if not transcript:
                return  # 寒暄/单条短问答，不值得提炼
            parts: list[str] = []
            async for ev in self.provider.stream(
                [Message.user(build_digest_prompt(transcript))], []
            ):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            added = await self._remember_digest_entries(parts)
            # 提炼跑完（无论有没有提出新条目）就标记：模型调用已经花过钱，
            # 取消归档再归档不应再来一遍；上面任何一步抛错则不标记，下次可重试
            await self.store.mark_session_memory_digested(sid)
            if not added:
                return
            for ag in self._for_each_agent():
                ag.set_system(self.compose_system())
            self._ws_broadcast({
                "kind": "memory_digest", "session_id": sid, "added": len(added),
                "message": f"归档后新记住 {len(added)} 条：" + "；".join(added)[:200],
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            memory_log.warning("archive memory digest failed: %s", e)
        finally:
            self._digesting.discard(sid)

    async def _remember_digest_entries(self, parts: list[str]) -> list[str]:
        """提炼稿解析落盘：走记忆写锁，避免与定期整理/设置页保存交错写 memory.md。"""
        async with self._memory_io_lock:
            return remember_lines(parse_digest("".join(parts)))

    # ---- 定期自动整理：按周期用模型合并去重全局/项目记忆（纯函数在 tools/memory.py） ----

    MEMORY_MAINTENANCE_INTERVAL = 600  # 巡检间隔秒数：到期后最多再等 10 分钟

    def start_memory_maintenance_loop(self) -> None:
        self._memory_maintenance_task = asyncio.create_task(self._memory_maintenance_loop())

    def stop_memory_maintenance_loop(self) -> None:
        if getattr(self, "_memory_maintenance_task", None):
            self._memory_maintenance_task.cancel()
            self._memory_maintenance_task = None

    async def _memory_maintenance_loop(self) -> None:
        # 先睡再巡检：启动时刻不做整理（刚打开应用不该立刻起模型调用，
        # 也避免与首归档提炼/手动整理抢同一个 provider 脚本）
        while True:
            await asyncio.sleep(self.MEMORY_MAINTENANCE_INTERVAL)
            try:
                await self._memory_maintenance_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单轮巡检失败不终止循环（与 cron/提醒循环同一姿态）

    async def _memory_maintenance_tick(self) -> None:
        st = self.cfg.memory_maintenance
        if self.provider is None or getattr(self.provider, "demo_mode", False):
            return
        due_g, due_p = maintenance_due(
            load_maintenance_state(),
            global_enabled=st.global_enabled, project_enabled=st.project_enabled,
            interval_hours=st.interval_hours, workdir=str(self.working_dir or ""),
            now=time.time(),
        )
        if due_g:
            await self._maintain_memory("global")
        if due_p:
            await self._maintain_memory("project")
        await self._map_auto_digest_tick()

    async def memory_maintain_now(self) -> dict:
        """手动「立即整理」：无视周期与开关（明确点击即用户意图），仍守阈值与规模。"""
        if self.provider is None:
            raise RuntimeError("还没有可用的模型服务，先在 设置 · 模型服务 配置 API Key")
        if getattr(self.provider, "demo_mode", False):
            # 与归档提炼/定时巡检同一守卫：演示模式只有脚本文本，真整理会拿脚本
            # 内容去覆盖 memory.md / AGENTS.md
            raise RuntimeError("演示模式没有真实模型，整理不了记忆；先在 设置 · 模型服务 配置")
        g = await self._maintain_memory("global", force=True)
        p = await self._maintain_memory("project", force=True)
        if "conflict" in (g, p):
            return {"ran": False, "message":
                    "记忆刚被归档提炼写入过新条目，本次整理已让路以免覆盖；稍后再点一次即可"}
        if "changed" in (g, p):
            return {"ran": True, "global": g == "changed", "project": p == "changed"}
        if g == "skipped" and p == "skipped":
            return {"ran": False, "message": "两份记忆都还没到需要整理的规模（全局 ≥400 字、项目 ≥600 字）"}
        if g == "unchanged" and p == "unchanged":
            return {"ran": False, "message": "整理完成：没有发现需要合并或删除的条目，记忆维持原样"}
        return {"ran": False, "message": "整理完成：跑过的部分无需改动（另一份还没到整理规模）"}

    async def _maintain_memory(self, scope: str, force: bool = False) -> str:
        """整理一份记忆文件：模型重写 → 备份原件 → 落盘 → 刷新系统提示词。

        返回 "changed"（整理并落盘）/ "unchanged"（模型认为无需改动或输出无效）
        / "skipped"（没到整理规模或上一轮还在跑）/ "conflict"（读旧稿后文件被
        并发写过，让路不写，不推进「上次整理时间」，下一轮巡检自动重试）。
        失败静默记日志（整理是锦上添花，绝不打扰主流程）；「立即整理」按四态
        分别给用户反馈。
        """
        if self._maintaining:
            return "skipped"  # 上一轮还没跑完（手动+定时叠加时直接让路）
        if scope == "global":
            path = memory_path()
            min_chars = MAINTAIN_MIN_GLOBAL_CHARS
            max_chars = MAX_MEMORY_FILE_CHARS
        else:
            path = Path(self.instructions_file) if self.instructions_file else None
            min_chars = MAINTAIN_MIN_PROJECT_CHARS
            max_chars = MAX_INSTRUCTIONS_CHARS
        old_text = ""
        if path is not None:
            try:
                old_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old_text = ""
        if len(old_text.strip()) < min_chars:
            return "skipped"  # 还没到值得整理的规模，也不消耗「上次整理时间」
        self._maintaining = True
        try:
            parts: list[str] = []
            async for ev in self.provider.stream(
                [Message.user(build_maintain_prompt(scope, old_text))], []
            ):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            new_text = clean_maintained_text("".join(parts), old_text, max_chars)
            if new_text is None:
                return "unchanged"  # 输出为空/与原文一致/超长失控/结构破坏：一律不动原文件
            async with self._memory_io_lock:
                # 读旧稿之后文件可能已被并发写过（归档提炼追加了新条目、设置页
                # 保存）：整理稿按旧快照生成，直接覆盖会把新内容静默抹掉。对不上
                # 就让路——整理时间不推进，巡检下一轮会带着新内容重新整理。
                try:
                    cur = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    cur = ""
                if cur != old_text:
                    return "conflict"
                try:
                    # 先备份后写：备份失败（磁盘满/权限）就放弃本次整理，不裸写
                    backup_before_maintain(path, old_text)
                    # 原子写（textio 同族）：写一半被杀不能留下半截记忆文件
                    write_text_atomic(path, new_text + "\n")
                except OSError as e:
                    memory_log.warning("memory maintain write failed (%s): %s", scope, e)
                    return "skipped"
            if scope == "project":
                self.instructions_text = new_text
            for ag in self._for_each_agent():
                ag.set_system(self.compose_system())
            state = load_maintenance_state()
            if scope == "global":
                state["global_last"] = time.time()
                msg = f"已整理全局记忆（原件已备份，同目录保留最近 {MAINTENANCE_BACKUP_KEEP} 份）"
            else:
                if self.working_dir is not None:
                    proj = dict(state.get("project_last") or {})
                    proj[str(self.working_dir)] = time.time()
                    state["project_last"] = proj
                msg = f"已整理项目记忆（AGENTS.md，原件已备份，同目录保留最近 {MAINTENANCE_BACKUP_KEEP} 份）"
            save_maintenance_state(state)
            if not force:  # 手动触发的结果在按钮状态行里看，不弹通知
                self._ws_broadcast({"kind": "memory_maintain", "scope": scope, "message": msg})
            return "changed"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            memory_log.warning("memory maintain failed (%s): %s", scope, e)
            return "skipped"
        finally:
            self._maintaining = False

    async def list_archived_sessions(self) -> dict:
        """归档弹窗列表（当前项目 + 快聊，最近活跃在前）。

        带上快聊：侧栏的快聊分组常驻，它的会话归档后如果不在这个弹窗里，
        用户就再也没地方恢复或删除它（归档弹窗是唯一入口）。
        """
        sessions = await self.store.list_archived_sessions(
            self._cur_project_id(), include_projectless=True
        )
        return {
            "sessions": [
                {
                    "id": s.id, "title": s.title, "updated_at": s.updated_at,
                    "pinned": bool(s.pinned), "project_id": s.project_id,
                    "summary": s.summary,
                    "tags": [t for t in str(s.tags or "").split(",") if t],
                    "archived": bool(s.archived),
                }
                for s in sessions
            ]
        }

    async def set_session_tags(self, session_id: str, tags: list[str] | str) -> dict:
        """给会话打标签（侧栏分组用）；传空清空。"""
        sess = await self._get_owned_session(session_id)
        value = await self.store.set_tags(session_id, tags)
        # 广播给其它连接：另一窗口的侧栏同步标签分组
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title})
        return {
            "id": session_id,
            "tags": [t for t in value.split(",") if t],
            "all_tags": await self.store.list_all_tags(self._cur_project_id()),
        }

    async def move_session(self, session_id: str, project_id: int | None) -> dict:
        """把会话移动到另一个项目；project_id=None 移入快聊。

        源会话必须属于当前项目（安全审查 B7）：旧实现只验目标项目存在，
        凭枚举到的 session_id 可以把别的项目的会话改归属。
        """
        sess = await self._get_owned_session(session_id)
        if project_id is not None:
            projects = {p.id: p for p in await self.store.list_projects()}
            if project_id not in projects:
                raise RuntimeError(f"project not found: {project_id}")
        await self.store.move_session(session_id, project_id)
        # 移出当前项目时同步丢弃 runtime：它带着旧项目的门控/工作目录，
        # 留着会让后续轮次错在别的项目上下文里执行（与 delete_session 对齐）；
        # 无项目态当前归属是快聊（None），移进任何项目都算移出
        if project_id != self._cur_project_id():
            self.cancel_run(session_id)
            rt = self.runtimes.pop(session_id, None)
            if rt is not None:
                # 排队轮随 runtime 消失：Future 逐个落空，请求不能挂死
                for item in list(rt.queue):
                    item.fail(RuntimeError("会话已移出当前项目"))
                rt.queue.clear()
                self._forget_runtime(rt)
        was_active = self.session and self.session.id == session_id
        moved_to_active = was_active and project_id == self._cur_project_id()
        switched = await self._switch_after_removal() if (was_active and not moved_to_active) else None
        # 广播给其它连接：另一窗口的侧栏按新归属刷新（本项目列表不再有它）
        self._ws_broadcast({"kind": "session_updated",
                            "session_id": session_id, "title": sess.title})
        return {"id": session_id, "project_id": project_id, "switched_to": switched}

    # ---- 工作项目切换（应用内设定工作目录，对标 Claude Code /add-dir 等） ----

    async def _bind_project(self, target: Path | None, *, connect_mcp: bool = True) -> None:
        """把引擎整体绑定到一个工作目录（None = 释放项目，进入无项目态）。

        switch_project / 删除项目 / setup 共用这一条重绑路径：白名单（权限门）、
        项目技能、项目记忆、项目级 MCP、子代理工作目录、检查点、钩子全部跟着
        target 走；无项目态只有全局技能/全局 MCP/全局记忆可用，快照会话归属
        快聊（project_id 为 NULL）。模型服务是全局配置，不受影响。

        connect_mcp=False 只给启动路径（setup）用：项目绑定本身不同步连 MCP，
        连接由 setup 的后台任务统一做——同步连会把「服务就绪」拖过桌面端启动
        预算（挂死/慢的服务器各烧一个连接超时），而且 setup 随后还要整体重建
        manager 再连一遍，白费一倍进程。
        """
        # 旧项目的子代理还引用着旧工作目录，先全部停掉
        if self.tasks:
            self.tasks.cancel_all()

        self.working_dir = target
        self.project = (
            await self.store.get_or_create_project(str(target)) if target is not None else None
        )
        # 信任按项目记忆：换了目录必须丢掉旧实例，否则会沿用上一个项目的信任状态
        self._trust = None
        # 辅助对话的历史里带着旧项目的 cwd（系统消息只在历史为空时注入），
        # 换项目不清掉的话，模型仍以为还在上一个目录里干活
        self.aux_history = []
        # 白名单按项目隔离：换项目 = 换一套规则；权限档位跨项目保持
        prev_accept = self.gate.auto_accept_write if self.gate else False
        prev_full = self.gate.auto_accept_all if self.gate else False
        self.gate = PermissionGate(store=self.store, project_id=self._cur_project_id(),
                                   working_dir=self.working_dir)
        self.gate.auto_accept_write = prev_accept
        self.gate.auto_accept_all = prev_full
        await self.gate.load_project_rules()
        if self._base_agent is not None:
            self._base_agent.gate = self.gate

        self.skills = SkillLoader(
            global_dir=skysheep_home() / "skills",
            project_dir=self._project_skills_dir_if_trusted(),
            state_path=(target / ".skysheep" / "skills.json") if target else None,
            scope_path=skysheep_home() / "skills-scope.json",
            project_root=target,
        )
        self.skills.discover()

        self.instructions_file, self.instructions_text = (
            load_project_instructions(target) if target is not None else (None, "")
        )

        self.tasks = TaskManager(
            provider_factory=lambda: self.provider,
            working_dir=target,
            max_iterations=self.cfg.subagent_max_iterations,
            store=self.subagent_store,
            provider_resolver=self._subagent_provider,
            registry_resolver=self._subagent_registry,
            max_concurrent=self.cfg.subagent_max_concurrent,
            usage_recorder=self._record_subagent_usage,
            event_emitter=self._ws_broadcast,
            state_path=skysheep_home() / "subagent_tasks.json",
        )

        # 项目级 mcp.json 指向新目录 → 差量接入新目录的服务器；顺带用新技能/子代理重建完整注册表。
        # 启动路径（connect_mcp=False）跳过：连接统一走 setup 的后台任务，避免同步等待与双重连接
        if connect_mcp:
            await self._sync_mcp_changes()

        # 钩子与检查点跟项目走：钩子换工作目录，检查点换目录树，避免把
        # A 项目的文件快照回滚到 B 项目
        raw_cfg = load_raw_config()
        pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
        self.hooks = HookRunner(pre_rules, post_rules, working_dir=target,
                                stop_rules=stop_rules) \
            if (pre_rules or post_rules or stop_rules) else None
        self.checkpoints = CheckpointStore(root=self._checkpoint_root())

        # 旧项目的会话 runtime 全部失效：停任务、释放、清空
        for rt in list(self.runtimes.values()):
            t = rt.run_task
            if t and not t.done():
                t.cancel()
            self._forget_runtime(rt)
        self.runtimes.clear()

        if self._base_agent is not None:
            self._base_agent.working_dir = target
            self._base_agent.hooks = self.hooks
            for ag in self._for_each_agent():
                ag.working_dir = target
            for ag in self._for_each_agent():
                ag.set_system(self.compose_system())

        # 记住当前项目（无项目态记 0）：重启后回到同一个状态
        await self._write_ui_prefs({"active_project": self._cur_project_id() or 0})

    async def switch_project(self, path: str) -> dict:
        """把引擎整体切到另一个工作目录：项目记录（含白名单）、项目技能、
        项目级 MCP、AGENTS.md 项目记忆、子代理工作目录全部跟着换，
        然后接上该项目最近的会话。模型服务是全局配置，不受影响。
        """
        if self._run_task and not self._run_task.done():
            raise RuntimeError("当前有任务正在运行，请先停止再切换项目")
        target = Path(str(path).strip().strip('"')).expanduser()
        if not target.is_absolute():
            raise RuntimeError("请使用文件夹的完整路径，例如 D:\\works\\demo")
        target = target.resolve()
        if not target.is_dir():
            raise RuntimeError("目录不存在：" + str(target))
        if self.project is not None and str(target) == str(self.working_dir):
            return {
                "switched": False,
                "path": str(self.working_dir),
                "project": self.project.name,
                "reason": "这已经是当前项目",
            }

        await self._bind_project(target)

        latest = await self.store.latest_session(self.project.id)
        if latest is not None:
            session_info = await self.resume_session(latest.id)
        else:
            self.session = None
            self._base_agent.load_history([Message.system(self.compose_system())])
            session_info = None
        return {
            "switched": True,
            "path": str(target),
            "project": self.project.name,
            "session": session_info,
            "mcp_warnings": self.mcp_warnings,
        }

    async def delete_project(self, project_id: int) -> dict:
        """把一个项目从列表里移除：项目记录、它的会话、白名单、任务清单与
        定时任务/流水线一并删除；电脑上的文件夹不受影响。

        删除是真实的删除：当前项目删掉后**不再重建一条同目录的新记录**——
        还有别的项目就切到最近的一个，一个都不剩就进入无项目态（列表为空，
        快聊照常可用）。无项目态也是合法状态，重启后保持。
        「远程连接」是固定项目（渠道会话的归属），拒绝删除。"""
        await self._reject_remote_project(project_id, "删除")
        is_current = self.project is not None and project_id == self.project.id
        if is_current and self._run_task and not self._run_task.done():
            raise RuntimeError("当前有任务正在运行，请先停止再删除项目")
        proj = await self.store.get_project(project_id)
        removed = await self.store.delete_project(project_id)
        if not removed:
            raise RuntimeError("项目不存在，可能已被删除")
        # 该项目的检查点目录（改前文件快照）一并清掉：项目没了，留着它的
        # 完整文件快照只会占磁盘——且对应 store 已被替换，永远不会再被淘汰
        if proj is not None and proj.root_path:
            cp_root = self._checkpoint_root_for(Path(proj.root_path))
            await asyncio.to_thread(shutil.rmtree, cp_root, True)
        if not is_current:
            return {"removed": project_id, "was_current": False}
        # 删的是当前项目：不再为同一目录重建记录；还有别的项目就接上最近的，
        # 一个都不剩就进入无项目态（列表为空，快聊继续可用）。
        # 「远程连接」是固定项目（无真实目录），不计入候选——否则删完所有
        # 真实项目后会把工作目录绑到哨兵路径上
        remaining = [p for p in self.ordered_projects(await self.store.list_projects())
                     if not self.is_remote_project(p)]
        if remaining:
            next_path = remaining[0].root_path
            await self._bind_project(Path(next_path))
            self.session = None
            latest = await self.store.latest_session(self.project.id)
            if latest is not None:
                await self.resume_session(latest.id)
            else:
                self._base_agent.load_history([Message.system(self.compose_system())])
            return {
                "removed": project_id, "was_current": True,
                "switched_to": {"path": str(self.project.root_path), "name": self.project.name},
            }
        await self._bind_project(None)
        self.session = None
        self._base_agent.load_history([Message.system(self.compose_system())])
        return {"removed": project_id, "was_current": True, "switched_to": None}

    # ---- 项目任务清单（右侧「任务清单」页签；任务与项目绑定，删项目一并删） ----

    def _target_project_id(self, project_id) -> int:
        pid = int(project_id) if project_id else (self.project.id if self.project else 0)
        if not pid:
            raise RuntimeError("没有当前项目")
        return pid

    async def project_task_list(self, project_id: int | None = None) -> dict:
        pid = self._target_project_id(project_id)
        return {"project_id": pid, "tasks": await self.store.list_project_tasks(pid)}

    async def project_task_add(self, params: dict) -> dict:
        title = str(params.get("title") or "").strip()
        if not title:
            raise RuntimeError("任务内容不能为空")
        pid = self._target_project_id(params.get("project_id"))
        task = await self.store.add_project_task(pid, title[:200], str(params.get("detail") or "")[:2000])
        return {"task": task}

    async def project_task_update(self, params: dict) -> dict:
        tid = int(params.get("id") or 0)
        if not tid:
            raise RuntimeError("缺少任务 id")
        title = params.get("title")
        detail = params.get("detail")
        task = await self.store.update_project_task(
            tid,
            title=(str(title).strip()[:200] or None) if title is not None else None,
            detail=None if detail is None else str(detail)[:2000],
            done=(bool(params["done"]) if "done" in params else None),
        )
        if not task:
            raise RuntimeError("任务不存在")
        return {"task": task}

    async def project_task_delete(self, params: dict) -> dict:
        tid = int(params.get("id") or 0)
        if not tid:
            raise RuntimeError("缺少任务 id")
        removed = await self.store.delete_project_task(tid)
        if not removed:
            raise RuntimeError("任务不存在")
        return {"removed": tid}

    async def export_session(self, session_id: str, *, fmt: str = "md") -> dict:
        """导出会话：fmt=md 为 Markdown 原文；fmt=html 为带样式的自包含单文件。

        归属校验（安全审查 B3）：别的项目的会话不能凭枚举到的 id 导出。
        """
        sess = await self._get_owned_session(session_id)
        msgs = await self.store.load_messages(session_id)
        safe_title = re.sub(r"[^\w\-]+", "_", sess.title or sess.id)[:40] or sess.id
        if fmt == "html":
            body = []
            for m in msgs:
                if m.role == "system":
                    continue
                if m.role == "user":
                    body.append(f'<div class="msg user">{_html_escape(m.text)}</div>')
                elif m.role == "assistant":
                    # 用时芯片：有实测耗时的消息才加（旧消息没有）
                    chip = _export_eta_html(m)
                    # 思考过程：折叠块（<details> 无需 JS，与界面上的可折叠块对应）
                    think = _export_thinking_html(m)
                    body.append(
                        f'<div class="msg ai">{chip}{think}'
                        f'<div class="body">{_html_escape(m.text)}</div></div>'
                    )
                elif m.role == "tool":
                    for b in m.content:
                        content = getattr(b, "content", "")
                        err = " err" if getattr(b, "is_error", False) else ""
                        body.append(
                            f'<div class="msg tool{err}">🔧 工具结果 · '
                            f'<pre>{_html_escape(content[:1500])}</pre></div>'
                        )
            html = EXPORT_HTML_TEMPLATE.format(
                title=_html_escape(sess.title or sess.id),
                date=date.today().isoformat(),
                version=__version__,
                body="\n".join(body),
            )
            return {"filename": f"skysheep-{safe_title}.html", "html": html}
        lines = [f"# SkySheep 会话 · {sess.title or sess.id}", ""]
        for m in msgs:
            if m.role == "system":
                continue
            lines.append(f"## {m.role}")
            # 用时：随正文标题行一起给出（旧消息没有实测耗时则省略）
            if m.role == "assistant" and getattr(m, "duration_ms", 0):
                est = getattr(m, "estimate", None) or {}
                lo, hi = est.get("min_seconds", 0), est.get("max_seconds", 0)
                eta = f"用时 {_fmt_export_dur(m.duration_ms / 1000)}"
                if hi:
                    eta += f" · 预估 {format_range(lo, hi)}"
                lines.append("")
                lines.append(f"> ⏱ {eta}")
            # 思考过程：导出时保留完整推理文本（界面里是折叠块，导出用引用块区分）。
            # 单独成段后，正文不再重复带 [thinking]（见 _export_body_text）。
            think = m.thinking if m.role == "assistant" else ""
            if think:
                lines.append("")
                lines.append("### 💭 思考过程")
                lines.append("")
                lines.extend(f"> {ln}" for ln in think.splitlines())
            lines.append("")
            lines.append(_export_body_text(m))
            lines.append("")
        return {
            "filename": f"skysheep-{safe_title}.md",
            "markdown": "\n".join(lines),
        }

    # ---- 项目记忆（AGENTS.md 等，前端侧栏「项目记忆」入口） ----

    async def get_instructions(self) -> dict:
        text = ""
        enc_text = ""
        certain = True
        if self.instructions_file:
            p = Path(self.instructions_file)
            if p.is_file():
                try:
                    # 走 textio 探测编码：GBK 等非 UTF-8 的 AGENTS.md 不再被读成
                    # 一串替换字符（此前 utf-8+replace 读完一旦保存就把乱码写死了）
                    loaded = read_text_file(p)
                    text = loaded.text
                    if loaded.encoding != "utf-8":
                        enc_text = loaded.encoding.upper()
                    certain = loaded.certain
                except OSError:
                    text = ""
        mtime = 0.0
        if self.instructions_file:
            try:
                mtime = round(Path(self.instructions_file).stat().st_mtime, 3)
            except OSError:
                mtime = 0.0
        # mtime 供右侧面板保存时比对：编辑期间定期整理重写过 AGENTS.md 就拒绝覆盖
        return {"path": self.instructions_file, "text": text, "mtime": mtime,
                "encoding_text": enc_text, "editable": certain}

    async def save_instructions(self, text: str, base_mtime: float | None = None) -> dict:
        """保存项目记忆并立即刷新系统提示词（对当前会话也生效）。

        落盘走 textio：沿用文件原本的编码与行尾符（新建文件 UTF-8 + LF），
        原子写；超出 MAX_INSTRUCTIONS_CHARS 的部分截掉，但不静默——truncated /
        original_chars / limit 随结果带回，由界面明示。"""
        if base_mtime is not None and float(base_mtime) > 0 and self.instructions_file:
            try:
                cur = round(Path(self.instructions_file).stat().st_mtime, 3)
            except OSError:
                cur = 0.0
            if cur != float(base_mtime):
                raise RuntimeError(
                    "项目记忆在你编辑期间被更新过（如定期整理已重写 AGENTS.md），"
                    "本次保存已阻止；请点「重读」后再编辑保存"
                )
        original_chars = len(text)
        truncated = original_chars > MAX_INSTRUCTIONS_CHARS
        text = text[:MAX_INSTRUCTIONS_CHARS]
        if self.instructions_file:
            path = Path(self.instructions_file)
        else:
            path = Path(self.working_dir) / "AGENTS.md"
            self.instructions_file = str(path)
        # 沿用原编码与行尾符；文件还不存在（首次创建）时按 UTF-8 + LF。
        # 编码探不确定意味着原文件读出来就带替换字符——按它写回等于把乱码固化，
        # 按项目规范「宁可拒绝写」，直接报错指路
        enc, nl = "utf-8", "\n"
        if path.is_file():
            try:
                loaded = read_text_file(path)
            except OSError:
                loaded = None
            if loaded is not None and not loaded.certain:
                raise RuntimeError(
                    "这份 AGENTS.md 的编码无法识别（读出来是替换字符），"
                    "为避免写坏原文件已拒绝保存；请在外部编辑器里把它转成 UTF-8 后重试"
                )
            if loaded is not None:
                enc, nl = loaded.encoding, loaded.newline
        try:
            write_text_file(path, text, enc, nl)
        except (OSError, UnicodeEncodeError) as e:
            raise RuntimeError(f"写入失败: {e}") from None
        self.instructions_text = text
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        try:
            mtime = round(path.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        return {"saved": True, "path": str(path), "chars": len(text), "mtime": mtime,
                "truncated": truncated, "original_chars": original_chars,
                "limit": MAX_INSTRUCTIONS_CHARS}

    # ---- 会话库备份：列出 / 手动备份 / 删除 / 恢复 ----

    def list_session_backups(self) -> dict:
        """可恢复的会话库备份（含"当前"一项，便于对照时间）。"""
        items = self.store.list_backups()
        return {
            "backups": items,
            "dir": str(self.store.backup_dir()),
            "keep": self.store.BACKUP_KEEP,
        }

    async def create_session_backup(self) -> dict:
        """手动备份会话库（设置 · 关于的「立即备份」）。

        与恢复同一道防线：有会话在跑时不动手——checkpoint 与 copy 之间若还有
        并发写，可能拷出撕裂的库文件。
        """
        running = [
            rt for rt in self.runtimes.values() if rt.run_task and not rt.run_task.done()
        ]
        if running:
            raise RuntimeError("还有会话正在运行，先停止（Esc）或等它结束再备份")
        try:
            return await self.store.backup_now()
        except OSError as e:
            raise RuntimeError(f"备份失败：{e}") from None

    async def delete_session_backup(self, name: str) -> dict:
        """删除单份会话库备份（文件名校验在 store 层，与恢复同一套）。"""
        try:
            return await self.store.delete_backup(str(name or "").strip())
        except (OSError, ValueError, FileNotFoundError) as e:
            raise RuntimeError(f"删除失败：{e}") from None

    async def restore_session_backup(self, name: str) -> dict:
        """从备份恢复会话库：先关库、换文件、重开，再重建内存状态。

        恢复后所有会话 runtime 作废（历史与消息 id 都可能对不上），
        重新挂到最新会话上；前端收到结果后重载列表即可。
        """
        name = str(name or "").strip()
        if not name:
            raise RuntimeError("缺少备份文件名")
        running = [
            rt for rt in self.runtimes.values() if rt.run_task and not rt.run_task.done()
        ]
        if running:
            raise RuntimeError("还有会话正在运行，先停止（Esc）或等它结束再恢复")
        try:
            result = await self.store.restore_backup(name)
        except (OSError, ValueError, FileNotFoundError) as e:
            raise RuntimeError(f"恢复失败：{e}") from None

        # 内存状态全部重建：旧 runtime 的历史/消息 seq 都可能与新库不一致
        for rt in list(self.runtimes.values()):
            self._forget_runtime(rt)
        self.runtimes.clear()
        self._base_queue.clear()
        self.session = None
        # 项目列表可能也随库回退了（备份里的项目集合是当时的样子）
        projects = await self.store.list_projects()
        cur = next(
            (p for p in projects if str(p.root_path).lower() == str(self.working_dir).lower()),
            None,
        )
        if cur is None:
            cur = await self.store.get_or_create_project(str(self.working_dir), self.working_dir.name)
        self.project = cur
        info = await self.open_initial_session()
        self._base_agent.load_history([Message.system(self.compose_system())])
        return {
            "restored": result["restored"],
            "safety_copy": result["safety_copy"],
            "session": info,
            "projects": [{"id": p.id, "name": p.name, "path": p.root_path} for p in projects],
        }

    # ---- 界面偏好（手调布局：侧栏宽 / 输入区高，前端拖拽后落盘 ui.json） ----
    # pywebview 默认 private_mode，前端 localStorage 每次启动都会清空，所以存后端文件。

    UI_PREFS_LIMITS = {
        "sidebar_w": (200, 460),
        "composer_h": (74, 520),
        "right_w": (240, 720),
        "right_collapsed": (0, 1),  # 右侧面板是否收起（1=收起，标签列表保留）
        "left_collapsed": (0, 1),  # 左侧栏是否折叠（1=折叠，折叠钮/Ctrl+B 切换）
        "ui_scale": (70, 120),  # 界面缩放百分比（存 70–120 的整数，100 = 默认大小）
        "notify": (0, 1),  # Windows 系统通知开关（1=开，默认开）
        "pet": (0, 1),  # 对话区宠物「云朵小羊」开关（1=显示，默认显示）
        # 空白会话的欢迎卡片（1=显示，默认显示）：设置 · 界面与通知 里可关
        "welcome_card": (0, 1),
        "accept_edits": (0, 2),  # 分级权限模式：0=安全执行 1=自动编辑（写入免确认）2=完全访问
        # 宠物拖放位置（#chat 内布局像素）；上限给足，前端拖拽时已按容器收敛
        "pet_x": (0, 4000),
        "pet_y": (0, 4000),
        # 首次启动配置向导已完成标记（1=完成，不再自动弹出）
        "onboarded": (0, 1),
        # 自动检查更新（1=开，默认开）：关于页开关，关掉后启动不再请求 GitHub
        "update_check": (0, 1),
        # 发送键：1 = Ctrl+Enter 发送、Enter 换行（默认 0 = Enter 发送）
        "ctrl_enter_send": (0, 1),
        # 通知提示音（1=开，默认关）：任务完成 / 等确认时 WebAudio 合成短音
        "notify_sound": (0, 1),
        # 提示音作用域（1=仅窗口失焦时响，默认 1）：前台人已在看，响一声反而吵
        "notify_sound_focus": (0, 1),
        # 通知类型开关（1=开，默认开）：关掉的类型不弹系统通知、不响提示音，
        # 仍进应用内通知中心（那里是「错过提醒」的聚合日志，不该有死角）
        "notify_kind_done": (0, 1),
        "notify_kind_perm": (0, 1),
        # 对话区宠物大小百分比（60–140，100 = 默认 76px 宽；null/缺省 = 默认）
        "pet_scale": (60, 140),
        # 阅读行宽：对话区消息卡最大宽度 px（680–1400；null/缺省 = 主题默认 880）
        "read_width": (680, 1400),
        # 当前项目 id（0 = 无项目态）：后端在切项目/删项目/首启建项目时写入，
        # 重启后回到同一个状态；上限给足任意合法 SQLite rowid
        "active_project": (0, 2_147_483_647),
    }
    # 右侧面板：打开了哪些标签、激活的是哪个（id 白名单见前端 TAB_META）。
    # 标签分「容器」与「分段」两层：任务是子代理任务/任务清单/项目任务的容器，
    # 自动化是定时任务/任务编排的容器。两层 id 都收——right_tabs 只写容器，但
    # right_active 写的是分段 id，且旧 ui.json 里整份都是分段 id（前端按 TAB_OF
    # 映射回容器）。漏收哪个，升级后面板就会静默少一个标签（ptasks/pipeline
    # 在合并前就漏收过，一并补上）；terminal 是更早的终端标签，留着不碍事。
    RIGHT_TAB_IDS = (
        "aux", "review", "browser", "files", "tasks", "agenda", "auto", "memory", "ext",
        "todo", "ptasks", "cron", "pipeline", "terminal",
    )
    # 会话标签恢复：session.tabs 存 sid 数组（与 tab_order 同款上限），
    # session.active 存激活的 sid。都在前端写（标签开/关/切换时），
    # snapshot() 读出来校验归属后下发给前端恢复。
    SESSION_TABS_KEY = "session_tabs"
    SESSION_ACTIVE_KEY = "session_active"
    SESSION_TABS_MAX = 200
    # 应用内通知中心的持久化日志（notif_log）：错过提醒要扛得住进程重启
    NOTIF_LOG_MAX = 60
    # 字符串型偏好（值域白名单）：theme 值域见模块级 THEME_PREFS
    STRING_PREFS = {
        "theme": THEME_PREFS,
        # 侧栏呈现形式：classic=项目/会话两区（原样式），grouped=会话收进各自项目下
        "sidebar_view": ("classic", "grouped"),
        # 用量页 Token 活动的图表类型：bar=柱状（默认），line=折线
        "usage_chart": ("bar", "line"),
        # 「跟随系统」的深浅落点（浅/深各一个具体主题；缺省 = 纸墨 / 夜墨）
        "theme_auto_light": THEME_AUTO_LIGHT,
        "theme_auto_dark": THEME_AUTO_DARK,
    }
    # 新会话默认模型（ui.json 的 default_model）：provider 键用字符串白名单校验
    # （只能是已配置的服务名），模型名随 provider 一起存进 value（"name::model"）
    DEFAULT_MODEL_PROVIDER_KEY = "default_model_provider"
    DEFAULT_MODEL_VALUE_SEP = "::"

    def _ui_prefs_path(self) -> Path:
        return skysheep_home() / "ui.json"

    @staticmethod
    def _clean_notif_log(val) -> list[dict]:
        """清洗通知中心日志：字段白名单 + 截断，封顶 60 条；无有效条目返回空表。

        前端存的是 [{ts, title, body, kind}]（sid 等跳转元数据只活在内存里，
        跨进程无意义不收）；title 空的条目是脏数据，直接丢弃。
        """
        if not isinstance(val, list):
            return []
        cleaned: list[dict] = []
        for item in val:
            if not isinstance(item, dict):
                continue
            entry = {
                "ts": float(item.get("ts") or 0.0),
                "title": str(item.get("title") or "")[:120],
                "body": str(item.get("body") or "")[:300],
                "kind": str(item.get("kind") or "")[:16],
            }
            if entry["title"]:
                cleaned.append(entry)
            if len(cleaned) >= 60:
                break
        return cleaned

    def _read_ui_prefs(self) -> dict:
        p = self._ui_prefs_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        prefs = {
            k: int(data[k])
            for k in self.UI_PREFS_LIMITS
            if isinstance(data.get(k), int) and not isinstance(data.get(k), bool)
        }
        tabs = data.get("right_tabs")
        if isinstance(tabs, list):
            cleaned: list[str] = []
            for t in tabs:
                if t in self.RIGHT_TAB_IDS and t not in cleaned:
                    cleaned.append(t)
            if cleaned:
                prefs["right_tabs"] = cleaned
        active = data.get("right_active")
        if isinstance(active, str) and active in self.RIGHT_TAB_IDS:
            prefs["right_active"] = active
        order = data.get("project_order")
        if isinstance(order, list) and all(
            isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in order
        ) and order:
            prefs["project_order"] = order
        sorder = data.get("session_order")
        if isinstance(sorder, dict) and sorder:
            cleaned_sorder: dict[str, list[str]] = {}
            for gk, ids in sorder.items():
                gks = str(gk)
                if not gks or not isinstance(ids, list):
                    continue
                gseen = [x for x in ids if isinstance(x, str) and x]
                if gseen:
                    cleaned_sorder[gks] = gseen
            if cleaned_sorder:
                prefs["session_order"] = cleaned_sorder
        torder = data.get("tab_order")
        if isinstance(torder, list) and torder and all(
            isinstance(x, str) and x for x in torder
        ):
            seen: set[str] = set()
            deduped = [x for x in torder if not (x in seen or seen.add(x))]
            prefs["tab_order"] = deduped
        nlog = self._clean_notif_log(data.get("notif_log"))
        if nlog:
            prefs["notif_log"] = nlog
        # 自由字符串键（启动恢复 / 新会话默认模型 / 全局热键）：
        # 读取时与写入同一套清洗（session_tabs 去重封顶，其余非空字符串直取）
        stabs = data.get("session_tabs")
        if isinstance(stabs, list) and stabs:
            seen_st: set[str] = set()
            deduped_st = [x for x in stabs
                          if isinstance(x, str) and x and not (x in seen_st or seen_st.add(x))]
            if deduped_st:
                prefs["session_tabs"] = deduped_st[: self.SESSION_TABS_MAX]
        for key in ("session_active", "hotkey", "default_model_name",
                    "default_model_provider"):
            v = data.get(key)
            if isinstance(v, str) and v:
                prefs[key] = v
        for key, allowed in self.STRING_PREFS.items():
            val = data.get(key)
            if isinstance(val, str) and val in allowed:
                prefs[key] = val
        return prefs

    # ---- Windows 系统通知（winotify toast；失败静默，非 Windows 平台不可用） ----

    def notify_enabled(self) -> bool:
        try:
            return self._read_ui_prefs().get("notify", 1) == 1
        except Exception:  # noqa: BLE001
            return True

    async def notify(self, params: dict) -> dict:
        """发一条 Windows toast（在独立线程里跑，不阻塞事件循环；失败静默）。"""
        title = str(params.get("title", ""))[:60] or "SkySheep"
        body = str(params.get("body", ""))[:160]
        if not self.notify_enabled():
            return {"sent": False, "reason": "disabled"}
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._toast_blocking, title, body)
            return {"sent": True}
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            return {"sent": False}

    @staticmethod
    def _toast_launch_uri() -> str:
        """点击 toast 时的激活 URI（skysheep://focus）；协议未注册返回空串（点击无动作）。

        协议由桌面壳（desktop.py，仅打包版）写进 HKCU\\Software\\Classes\\<scheme>：
        点击通知系统按协议再拉起一次 exe，单实例互斥让第二个进程自动转成
        「聚焦已有窗口」，通知才算从「发得出」变成「收得回」。这里只读注册表
        判断有没有——浏览器兜底模式没有桌面壳，读不到，行为与旧版一致。
        """
        if sys.platform != "win32":
            return ""
        try:
            import winreg

            from .. import instance

            name = instance.instance_name()
            scheme = "skysheep" if name is None else f"skysheep-{name}"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}"
            ) as key:
                winreg.QueryValueEx(key, "URL Protocol")  # 有这个值才算注册完整
            return f"{scheme}://focus"
        except Exception:  # noqa: BLE001 - 读取失败按未注册处理，不影响弹通知
            return ""

    @staticmethod
    def _toast_blocking(title: str, body: str) -> None:
        from winotify import Notification, audio

        launch = ServerBackend._toast_launch_uri()
        toast = Notification(app_id="SkySheep", title=title, msg=body, launch=launch)
        toast.set_audio(audio.Silent, loop=False)
        toast.show()

    def ordered_projects(self, projects: list) -> list:
        """按用户拖动保存的顺序排项目（ui.json 的 project_order，见 save_ui_prefs）。

        只把名单里的项目按保存序提前，其余（新增项目、名单外的）仍按原序（
        created_at DESC）排在后面；名单里已不存在的 id 自然忽略。这样旧偏好
        不会把新项目藏到看不见的位置，拖动只固定用户明确排过序的那部分。
        """
        try:
            order = self._read_ui_prefs().get("project_order")
        except Exception:  # noqa: BLE001
            order = None
        if not isinstance(order, list) or not order:
            return projects
        rank = {pid: i for i, pid in enumerate(order)}
        known = [p for p in projects if p.id in rank]
        unknown = [p for p in projects if p.id not in rank]
        return sorted(known, key=lambda p: rank[p.id]) + unknown

    async def get_ui_prefs(self) -> dict:
        return {"prefs": self._frontend_prefs(self._read_ui_prefs())}

    @staticmethod
    def _frontend_prefs(prefs: dict) -> dict:
        """WS prefs 面只暴露前端自己的偏好。active_project 由后端在切项目/
        删项目/首启时自写自读（0 = 无项目态），不给前端看、也不许前端写。"""
        return {k: v for k, v in prefs.items() if k != "active_project"}

    def first_paint_prefs(self) -> dict:
        """首帧外观偏好：由 server/app.py 注入到 index.html 的 <html> 标签。

        项目切换是整页 reload，若等 ui.get 回来才应用偏好，首帧必然是默认外观、
        随后再跳一次；这里提前给服务端用。读失败静默返回空（保持默认外观）。
        """
        try:
            return self._frontend_prefs(self._read_ui_prefs())
        except Exception:  # noqa: BLE001
            return {}

    async def save_ui_prefs(self, prefs: dict) -> dict:
        """WS 入口：前端偏好合并保存。active_project 后端独占，前端传了也忽略。"""
        cleaned = {k: v for k, v in (prefs or {}).items() if k != "active_project"}
        return await self._write_ui_prefs(cleaned)

    async def _write_ui_prefs(self, prefs: dict) -> dict:
        """合并保存；某项传 null 表示恢复默认（删除该项）。未知键忽略、越界值收敛到合法区间。

        后端自己也走这里写偏好（如 _bind_project 落 active_project），因此
        不在这里过滤键——过滤只发生在 save_ui_prefs 入口与读取面（_frontend_prefs）。
        """
        current = self._read_ui_prefs()
        for key, val in (prefs or {}).items():
            known = ("right_tabs", "right_active", "project_order", "session_order",
                     "tab_order", "session_tabs", "session_active",
                     "hotkey", "default_model_name", "default_model_provider",
                     "notif_log")
            if key not in self.UI_PREFS_LIMITS and key not in known \
                    and key not in self.STRING_PREFS:
                continue
            if val is None:
                current.pop(key, None)
                continue
            if key in ("hotkey", "default_model_name", "default_model_provider"):
                # 自由字符串键（热键组合串 / 新会话默认模型）：非空收、空串删。
                # 值域校验在各自消费方（desktop._parse_hotkey / set 时校验服务名）
                if isinstance(val, str) and val.strip():
                    current[key] = val.strip()
                else:
                    current.pop(key, None)
                continue
            if key in self.STRING_PREFS:
                if isinstance(val, str) and val in self.STRING_PREFS[key]:
                    current[key] = val
                else:
                    current.pop(key, None)
                continue
            if key == "right_tabs":
                if isinstance(val, list):
                    cleaned: list[str] = []
                    for t in val:
                        if t in self.RIGHT_TAB_IDS and t not in cleaned:
                            cleaned.append(t)
                    if cleaned:
                        current["right_tabs"] = cleaned
                    else:
                        current.pop("right_tabs", None)
                continue
            if key == "right_active":
                if isinstance(val, str) and val in self.RIGHT_TAB_IDS:
                    current["right_active"] = val
                else:
                    current.pop("right_active", None)
                continue
            if key == "project_order":
                # 分组视图拖动排序的项目 id 顺序（项目列表的展示序，随 ui.json 持久化）。
                # 只收正整数；去重防同一 id 重复占位，封顶防异常超大载荷；
                # 一个有效 id 都没有时删键（空数组与脏数据都视为「未自定义排序」）
                if isinstance(val, list):
                    seen: list[int] = []
                    for x in val:
                        if isinstance(x, int) and not isinstance(x, bool) and x > 0 and x not in seen:
                            seen.append(x)
                            if len(seen) >= 200:
                                break
                    if seen:
                        current["project_order"] = seen
                    else:
                        current.pop("project_order", None)
                else:
                    current.pop("project_order", None)
                continue
            if key == "session_order":
                # 分组视图组内会话的拖动序：{ 项目 key: [会话 id, ...] }。项目 key 用
                # 字符串（数字项目 id 与 quick/loose 伪组同构）；id 是非空字符串，
                # 每组去重封顶 200、整体封顶 100 组——一个组都没有时删键
                if isinstance(val, dict):
                    cleaned_order: dict[str, list[str]] = {}
                    for gk, ids in val.items():
                        gks = str(gk)
                        if not gks or not isinstance(ids, list):
                            continue
                        gseen: list[str] = []
                        for x in ids:
                            if isinstance(x, str) and x and x not in gseen:
                                gseen.append(x)
                                if len(gseen) >= 200:
                                    break
                        if gseen:
                            cleaned_order[gks] = gseen
                        if len(cleaned_order) >= 100:
                            break
                    if cleaned_order:
                        current["session_order"] = cleaned_order
                    else:
                        current.pop("session_order", None)
                else:
                    current.pop("session_order", None)
                continue
            if key == "tab_order":
                # 标签栏页签的拖动序：sid 字符串数组（无 sid 的空标签不进序——
                # 它没有稳定 id，始终排在最后）。去重封顶 200；一个都没有时删键。
                if isinstance(val, list):
                    seen_tab: list[str] = []
                    for x in val:
                        if isinstance(x, str) and x and x not in seen_tab:
                            seen_tab.append(x)
                            if len(seen_tab) >= 200:
                                break
                    if seen_tab:
                        current["tab_order"] = seen_tab
                    else:
                        current.pop("tab_order", None)
                else:
                    current.pop("tab_order", None)
                continue
            if key == "session_tabs":
                # 启动恢复：上次开着的会话标签（sid 数组，序即标签序）。去重封顶同
                # tab_order；空数组 = 全关了，删键（下次启动回到欢迎页）
                if isinstance(val, list):
                    seen_st: list[str] = []
                    for x in val:
                        if isinstance(x, str) and x and x not in seen_st:
                            seen_st.append(x)
                            if len(seen_st) >= self.SESSION_TABS_MAX:
                                break
                    if seen_st:
                        current["session_tabs"] = seen_st
                    else:
                        current.pop("session_tabs", None)
                else:
                    current.pop("session_tabs", None)
                continue
            if key == "session_active":
                # 启动恢复：激活的是哪张标签（sid）。空串/非法删键
                if isinstance(val, str) and val:
                    current["session_active"] = val
                else:
                    current.pop("session_active", None)
                continue
            if key == "notif_log":
                # 通知中心的持久化日志：同一套清洗；清空数组 = 删键（下次启动从零开始）
                cleaned_nl = self._clean_notif_log(val)
                if cleaned_nl:
                    current["notif_log"] = cleaned_nl
                else:
                    current.pop("notif_log", None)
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            lo, hi = self.UI_PREFS_LIMITS[key]
            current[key] = int(min(hi, max(lo, round(val))))
        # 原子写（M13 同族）：ui.json 写一半会让启动读偏好直接失败
        write_text_atomic(self._ui_prefs_path(), json.dumps(current, ensure_ascii=False))
        return {"prefs": self._frontend_prefs(current)}

    # ---- 全局记忆：设置页直接查看/编辑 memory.md ----

    async def memory_get(self) -> dict:
        from ..tools.memory import MAX_MEMORY_FILE_CHARS, inject_text, memory_path

        p = memory_path()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        try:
            mtime = round(p.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        st = self.cfg.memory_maintenance
        state = load_maintenance_state()
        return {
            "path": str(p),
            "text": text[:MAX_MEMORY_FILE_CHARS],
            # mtime 供设置页保存时比对：编辑期间后台提炼/整理改过文件就拒绝覆盖
            "mtime": mtime,
            # 系统提示词实际注入的字数（超 4000 字保最新条目按行截断），页面据此提示
            "inject_chars": len(inject_text(text)),
            "digest_enabled": self.cfg.memory_digest,
            "maintain": {
                "global_enabled": st.global_enabled,
                "project_enabled": st.project_enabled,
                "interval_hours": st.interval_hours,
                "global_last": float(state.get("global_last") or 0),
                "project_last": float(
                    (state.get("project_last") or {}).get(str(self.working_dir or "")) or 0
                ),
            },
        }

    async def memory_save(self, text: str, base_mtime: float | None = None) -> dict:
        from ..tools.memory import MAX_MEMORY_FILE_CHARS, inject_text, memory_path

        p = memory_path()
        full = text[:MAX_MEMORY_FILE_CHARS]
        async with self._memory_io_lock:
            if base_mtime is not None and float(base_mtime) > 0:
                # 防覆盖：用户打开编辑器期间，归档提炼/定期整理可能已写入新条目；
                # 拿旧编辑整体落盘会把它们静默抹掉，mtime 对不上就拒绝
                try:
                    cur = round(p.stat().st_mtime, 3)
                except OSError:
                    cur = 0.0
                if cur != float(base_mtime):
                    raise RuntimeError(
                        "记忆在你编辑期间被更新过（归档提炼或定期整理已写入），"
                        "本次保存已阻止；请刷新本页重新编辑，以免丢掉新记忆"
                    )
            # 原子写（textio 同族）：写一半被杀不能留下半截记忆文件
            write_text_atomic(p, full)
        # 记忆注入系统提示词：保存后立刻对当前所有会话生效
        for ag in self._for_each_agent():
            ag.set_system(self.compose_system())
        try:
            mtime = round(p.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        return {"saved": True, "path": str(p), "chars": len(text),
                "mtime": mtime, "inject_chars": len(inject_text(full))}

    async def memory_digest_save(self, enabled: bool) -> dict:
        """归档自动记忆总闸：写 config.toml 并热生效（下一次归档即按新值决定）。"""
        try:
            set_advanced_settings_in_config(memory_digest=bool(enabled))
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"enabled": self.cfg.memory_digest}

    async def memory_maintain_save(
        self, global_enabled: bool | None = None,
        project_enabled: bool | None = None,
        interval_hours: int | None = None,
    ) -> dict:
        """定期整理设置：写 [memory_maintenance] 并热生效（巡检每轮读最新配置）。"""
        try:
            set_memory_maintenance_in_config(
                global_enabled=global_enabled,
                project_enabled=project_enabled,
                interval_hours=interval_hours,
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        st = self.cfg.memory_maintenance
        return {
            "global_enabled": st.global_enabled,
            "project_enabled": st.project_enabled,
            "interval_hours": st.interval_hours,
        }

    # ---- 记忆地图：项目演化的可视化（时间线 + 主题图谱 + LLM 阶段摘要） ----

    MAP_SESSION_LIMIT = 500          # 时间线单次装载的会话上限（防超大项目撑爆载荷）
    MAP_FILE_LIMIT = 20              # 文件足迹 Top N
    MAP_HEAT_WEEKS = 260             # 热力图铺的周数上限（前端按底栏宽度自适应 26~260 周）
    MAP_MATERIAL_SESSIONS = 200      # 喂给摘要生成的会话条数上限
    MAP_MATERIAL_CHARS = 24_000      # 摘要材料的字符预算（超出丢最旧的会话）
    MAP_AUTO_MIN_SESSIONS = 8        # 自动生成：距上次摘要以来的新增会话数门槛
    MAP_AUTO_COOLDOWN_S = 24 * 3600  # 自动生成的冷却期

    async def _map_resolve_project(
        self, project_id: int | None, *, local: bool = True
    ) -> Project:
        """解析记忆地图的目标项目：缺省取当前项目；跨项目查看只给本机。

        会话标题/摘要/文件路径是跨项目的枚举面，与 session.search scope=all
        同一安全口径（远程客户端只看它被绑定到的当前项目）。
        """
        pid = int(project_id) if project_id else self._cur_project_id()
        if pid is None:
            raise RuntimeError("当前没有项目：先在侧栏「项目」区添加项目，才有演化可看")
        proj = await self.store.get_project(pid)
        if proj is None:
            raise RuntimeError("项目不存在或已被删除")
        if pid != self._cur_project_id() and not local:
            raise RuntimeError("查看其他项目的记忆地图只能在本机界面上操作")
        return proj

    @staticmethod
    def _map_range(params: dict) -> tuple[float, float]:
        """解析查询窗口：缺省近 90 天，显式 start_ts=0 表示不限起点（「全部」）；
        非法值夹回合法区间而不是报错。0 是合法取值，不能走 `or` 缺省（会被吞）。"""
        now = time.time()
        try:
            end = float(params.get("end_ts") or now)
        except (TypeError, ValueError):
            end = now
        raw_start = params.get("start_ts")
        if raw_start is None:
            start = now - 90 * 86400
        else:
            try:
                start = max(0.0, float(raw_start))  # 负数夹回 0（同为不限起点）
            except (TypeError, ValueError):
                start = now - 90 * 86400
        end = min(max(end, start), now + 86400)  # 未来最多放宽一天（时区误差兜底）
        return start, end

    def _map_checkpoint_metas(self, proj) -> list[dict]:
        """目标项目的检查点元数据（文件足迹数据源）。

        当前项目直接用常驻的 store 实例（启动时已加载，零盘扫）；跨项目查看
        才临时开一个只读实例按该项目的指纹目录加载。远程连接项目没有真实
        目录，直接给空（它本来也没有检查点）。
        """
        if proj.id == self._cur_project_id() and self.checkpoints is not None:
            return self.checkpoints.list_project_metas()
        if not proj.root_path or proj.root_path == self.store.REMOTE_PROJECT_PATH:
            return []
        try:
            root = self._checkpoint_root_for(Path(proj.root_path))
            return CheckpointStore(root).list_project_metas()
        except (OSError, ValueError):
            return []

    async def map_get(self, params: dict | None = None, *, local: bool = True) -> dict:
        """记忆地图载荷：一次装配时间线/热力图/文件足迹/事件/记忆/摘要全部维度。"""
        params = params or {}
        proj = await self._map_resolve_project(params.get("project_id"), local=local)
        start, end = self._map_range(params)
        sessions = await self.store.map_sessions_with_stats(proj.id, start, end)
        if len(sessions) > self.MAP_SESSION_LIMIT:
            sessions = sessions[-self.MAP_SESSION_LIMIT:]  # 保最新（已按时间升序）
        # 热力天数与时间线窗口解耦：格子周数随底栏宽度自适应，天数始终备足到
        # 上限跨度（外加一周对齐富余），否则远端格子因「没查到」画成假「无活动」
        heat_start = min(start, end - (self.MAP_HEAT_WEEKS * 7 + 7) * 86400)
        days = await self.store.map_day_stats(proj.id, heat_start, end)
        events = await self.store.map_project_events(proj.id, start, end)

        # 文件足迹：检查点 meta 按 path 聚合（count/首末时间/涉及会话），Top N
        files: dict[str, dict] = {}
        for cp in self._map_checkpoint_metas(proj):
            ts = float(cp.get("ts") or 0)
            if not (start <= ts <= end):
                continue
            sid = cp.get("session_id")
            for path_s in cp.get("paths") or []:
                ent = files.setdefault(path_s, {
                    "path": path_s, "count": 0, "first_ts": ts, "last_ts": ts,
                    "session_ids": [],
                })
                ent["count"] += 1
                ent["first_ts"] = min(ent["first_ts"], ts)
                ent["last_ts"] = max(ent["last_ts"], ts)
                if sid and sid not in ent["session_ids"]:
                    ent["session_ids"].append(sid)
        top_files = sorted(files.values(), key=lambda f: (-f["count"], f["path"]))
        for f in top_files:
            f["session_ids"] = f["session_ids"][:20]
        top_files = top_files[: self.MAP_FILE_LIMIT]

        # 全局记忆条目（跨项目，前端标「全局」徽记）：日期字符串与窗口的本地
        # 日期串直接比较，免去逐条转时区
        try:
            mem_text = memory_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            mem_text = ""
        start_day = time.strftime("%Y-%m-%d", time.localtime(start))
        end_day = time.strftime("%Y-%m-%d", time.localtime(end))
        memories = [
            e for e in parse_memory_entries(mem_text, 400)
            if start_day <= e["date"] <= end_day
        ][-30:]

        digests = await self.store.list_map_digests(proj.id)
        pname = proj.name or (Path(proj.root_path).name if proj.root_path else "")
        return {
            "project": {
                "id": proj.id,
                "name": pname or "未命名项目",
                "root_path": (proj.root_path if local and proj.root_path
                              != self.store.REMOTE_PROJECT_PATH else ""),
                "created_at": proj.created_at,
            },
            "range": {"start_ts": start, "end_ts": end},
            "sessions": sessions,
            "days": days,
            "events": events,
            "files": top_files,
            "memories": memories,
            "digests": digests,
            "generating": proj.id in self._map_generating,
            "config": {"auto_digest": bool(self.cfg.memory_map.auto_digest)},
        }

    async def map_generate(self, params: dict | None = None, *, local: bool = True) -> dict:
        """「生成演化摘要」：解析项目后起后台任务，立即返回（完成经事件广播）。

        按项目单飞：同一项目进行中再点直接让路，不叠加并发模型调用。
        """
        params = params or {}
        proj = await self._map_resolve_project(params.get("project_id"), local=local)
        if proj.id in self._map_generating:
            return {"started": False, "reason": "这个项目的摘要在生成中，稍等片刻"}
        if self.provider is None:
            raise RuntimeError("还没有可用的模型服务，先在 设置 · 模型服务 配置 API Key")
        if getattr(self.provider, "demo_mode", False):
            raise RuntimeError("演示模式没有真实模型，生成不了演化摘要；先在 设置 · 模型服务 配置")
        spawn_bg(self._map_generate_run(proj.id))
        return {"started": True, "project_id": proj.id}

    async def _map_generate_run(self, project_id: int) -> None:
        """生成演化摘要的主体：拼材料 → 调模型 → 宽容解析 → 校验落库 → 广播。

        与 _auto_title/_memory_digest 同一边界：失败不影响主流程，但会通过
        map_updated 事件把原因带给前端（按钮要能显示失败并可重试）。
        """
        self._map_generating.add(project_id)
        try:
            sessions = await self.store.map_sessions_with_stats(project_id, 0, time.time() + 1)
            if not sessions:
                self._ws_broadcast({
                    "kind": "map_updated", "project_id": project_id, "ok": False,
                    "message": "这个项目还没有会话，先聊出一点历史再来生成演化摘要",
                })
                return
            sessions = sessions[-self.MAP_MATERIAL_SESSIONS:]
            proj = await self.store.get_project(project_id)
            pname = (proj.name if proj and proj.name else "") or "未命名项目"
            prompt = self._map_build_material(pname, sessions)
            parts: list[str] = []
            async for ev in self.provider.stream([Message.user(prompt)], []):
                if isinstance(ev, ProviderTextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, ProviderDone):
                    break
            data = self._parse_map_digest_json("".join(parts))
            rows = self._map_digest_rows(data, sessions)
            if not rows:
                raise ValueError("模型没有给出有效的阶段划分")
            await self.store.replace_map_digests(project_id, rows)
            self._ws_broadcast({
                "kind": "map_updated", "project_id": project_id, "ok": True,
                "message": "演化摘要已生成：" + (
                    f"{sum(1 for r in rows if r['kind'] == 'phase')} 个阶段"
                    if any(r["kind"] == "phase" for r in rows) else "项目总览"
                ),
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 生成失败要让前端看到原因
            logger.warning("map digest generate failed: %s", e)
            self._ws_broadcast({
                "kind": "map_updated", "project_id": project_id, "ok": False,
                "message": f"演化摘要生成失败：{e}",
            })
        finally:
            self._map_generating.discard(project_id)

    @staticmethod
    def _map_build_material(project_name: str, sessions: list[dict]) -> str:
        """把会话流水压成摘要生成的材料：每会话一行编号条目 + 可选摘要。

        超出字符预算时丢最旧的会话（近期历史对「当前处于什么阶段」最有用）。
        编号 [n] 是模型回报阶段覆盖范围的句柄，不把 12 位会话 id 塞进提示词。
        """
        lines: list[str] = []
        used = 0
        # 从最新往旧收，超出预算即止；输出时恢复时间正序
        picked: list[str] = []
        picked_n = 0
        for s in reversed(sessions):
            day = time.strftime("%Y-%m-%d", time.localtime(s["created_at"]))
            bits = [f"[{len(sessions) - picked_n}] {day} 「{(s['title'] or '（未命名）')[:60]}」"]
            picked_n += 1
            stats = []
            if s["msg_count"]:
                stats.append(f"{s['msg_count']}条消息")
            if s["in_tokens"] or s["out_tokens"]:
                stats.append(f"{(s['in_tokens'] + s['out_tokens']) // 1000}k tokens")
            if s["tags"]:
                stats.append("标签:" + ",".join(s["tags"][:4]))
            if stats:
                bits.append(" ·" + " ·".join(stats))
            ln = bits[0] + ("".join(" " + b for b in bits[1:]))
            if s["summary"]:
                ln += f"\n    摘要：{s['summary'][:160]}"
            if used + len(ln) > ServerBackend.MAP_MATERIAL_CHARS and picked:
                break
            picked.append(ln)
            used += len(ln) + 1
        lines = list(reversed(picked))
        return (
            f"你是项目演化记录员。下面是项目「{project_name}」在 SkySheep（AI Agent 工作台）"
            f"里的会话流水（按时间先后，[n] 是会话编号）。\n"
            "请把它划分成 2-5 个连续的演化阶段（每个阶段覆盖一段连续编号区间），"
            "并给项目一个一句话总览。\n\n"
            "只输出 JSON，不要 markdown 代码围栏，不要解释：\n"
            '{"overview": "不超过120字的项目总览",\n'
            ' "phases": [{"title": "阶段标题，不超过16字",\n'
            '   "summary": "这个阶段做了什么、为什么，不超过200字",\n'
            '   "highlights": ["要点1", "要点2"],\n'
            '   "topics": ["主题词1", "主题词2"],\n'
            '   "session_ids": [1, 2, 3]}]}\n'
            "要求：\n"
            "- 阶段按时间先后排列；session_ids 用方括号里的编号，"
            "所有编号都要被覆盖且不重叠\n"
            "- highlights 最多 5 条、每条不超过 40 字；topics 最多 6 个、每个不超过 12 字\n"
            "- 用中文；忠实于材料，不要编造没有出现的功能或事件\n\n"
            "会话流水：\n" + "\n".join(lines)
        )

    @staticmethod
    def _parse_map_digest_json(raw: str) -> dict:
        """宽容解析模型输出：剥代码围栏、掐首尾说明文字，取第一个完整 JSON 对象。"""
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("输出里没有找到 JSON")
        data = json.loads(text[i:j + 1])
        if not isinstance(data, dict):
            raise ValueError("JSON 不是对象")
        return data

    @staticmethod
    def _map_digest_rows(data: dict, sessions: list[dict]) -> list[dict]:
        """校验模型输出并折算成摘要行：编号映射回真实会话、补时间窗、去重叠。

        材料里的 [n] 对应 sessions 的第 n 条（1 起）。模型可能漏盖或重叠：
        编号已出现的阶段整体丢弃（时间上更晚的胜出没有依据，先到先得即可），
        编号越界的忽略。阶段时间窗取覆盖会话的 created_at/updated_at 极值。
        """
        by_idx = {i + 1: s for i, s in enumerate(sessions)}
        rows: list[dict] = []
        covered: set[int] = set()

        def _ints(raw_vals) -> list[int]:
            out: list[int] = []
            for v in raw_vals or []:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out

        overview = str(data.get("overview") or "").strip()
        phases = data.get("phases") if isinstance(data.get("phases"), list) else []
        phase_rows: list[dict] = []
        for ph in phases[:8]:
            if not isinstance(ph, dict):
                continue
            idxs = [n for n in dict.fromkeys(_ints(ph.get("session_ids"))) if n in by_idx]
            idxs = [n for n in idxs if n not in covered]
            if not idxs:
                continue
            covered.update(idxs)
            sub = [by_idx[n] for n in idxs]
            phase_rows.append({
                "kind": "phase",
                "title": str(ph.get("title") or "").strip()[:40] or "未命名阶段",
                "summary": str(ph.get("summary") or "").strip()[:500],
                "highlights": [str(h).strip()[:100] for h in (ph.get("highlights") or [])[:10]
                               if str(h).strip()],
                "topics": [str(t).strip()[:24] for t in (ph.get("topics") or [])[:12]
                           if str(t).strip()],
                "session_ids": [by_idx[n]["id"] for n in idxs],
                "start_ts": min(s["created_at"] for s in sub),
                "end_ts": max(s["updated_at"] for s in sub),
            })
        # 阶段按实际时间窗排序后重排编号顺序（模型偶尔乱序），总览行放最前
        phase_rows.sort(key=lambda r: r["start_ts"])
        if overview:
            all_ids = [r["session_ids"] for r in phase_rows]
            flat_ids = [sid for ids in all_ids for sid in ids] or [s["id"] for s in sessions]
            rows.append({
                "kind": "overview", "title": "项目总览", "summary": overview[:120],
                "highlights": [], "topics": [],
                "session_ids": flat_ids[:500],
                "start_ts": min((r["start_ts"] for r in phase_rows),
                                default=sessions[0]["created_at"]),
                "end_ts": max((r["end_ts"] for r in phase_rows),
                              default=sessions[-1]["updated_at"]),
            })
        rows.extend(phase_rows)
        return rows

    async def map_save_config(self, params: dict) -> dict:
        """记忆地图设置（自动生成开关）：写 [memory_map] 并热生效。"""
        auto = params.get("auto_digest")
        try:
            set_memory_map_config(auto_digest=None if auto is None else bool(auto))
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        return {"auto_digest": bool(self.cfg.memory_map.auto_digest)}

    async def _map_auto_digest_tick(self) -> None:
        """演化摘要的自动巡检：挂在记忆整理巡检里，不另起循环。

        开关默认关；开启后当前项目「距上次摘要超过冷却期且新增会话达到
        门槛」时后台补一次（与手动按钮共用单飞锁与失败广播）。
        """
        if not self.cfg.memory_map.auto_digest:
            return
        if self.provider is None or getattr(self.provider, "demo_mode", False):
            return
        pid = self._cur_project_id()
        if pid is None or pid in self._map_generating:
            return
        last = await self.store.latest_map_digest_ts(pid)
        if time.time() - last < self.MAP_AUTO_COOLDOWN_S:
            return
        if await self.store.count_project_sessions_since(pid, last) < self.MAP_AUTO_MIN_SESSIONS:
            return
        spawn_bg(self._map_generate_run(pid))

    # ---- 联网搜索 / AI 画图：设置页配置（写 config.toml + 热更新工具实例） ----

    async def websearch_detail(self) -> dict:
        ws = self.cfg.websearch
        resolved = resolve_websearch(self.cfg)
        key = resolved.get("api_key") if resolved else ""
        return {
            "provider": ws.provider,
            "providers": ["auto", "custom"],  # 前端只渲染「自动 / 自定义 / 已配置」三档
            "configured_services": self._configured_services(),
            "base_url": ws.base_url,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "config_hint": (
                "「自动」会优先复用你已配置 Key 的服务；也可选博查（bocha.cn）或 "
                "Tavily 并填入对应 API Key；选「自定义」则填入自建搜索服务地址"
                "（如 SearXNG，无需 Key）；右侧「已配置」则列出你已在「模型服务」里"
                "配好的服务，选中即复用它的地址与 Key（前提是它本身提供搜索接口）。"
                "保存后立即生效。"
            ),
        }

    async def websearch_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "bocha", "tavily", "zhipu", "custom") and (
            provider not in self.cfg.providers
        ):
            raise RuntimeError("联网搜索服务商只支持 auto / custom，或已配置的模型服务")
        updates: dict = {"provider": provider or None}
        api_key = params.get("api_key")
        if api_key is not None:
            updates["api_key"] = str(api_key).strip()
        if params.get("base_url") is not None:
            updates["base_url"] = str(params["base_url"]).strip()
        update_config_section("websearch", updates)
        self.cfg = load_config()
        self._refresh_web_tool_configs()
        return await self.websearch_detail()

    async def imagegen_detail(self) -> dict:
        ig = self.cfg.imagegen
        resolved = resolve_imagegen(self.cfg)
        key = resolved.get("api_key") if resolved else ""
        return {
            "provider": ig.provider,
            "providers": ["auto", "custom"],  # 前端只渲染「自动 / 自定义 / 已配置」三档
            "configured_services": self._configured_services(),
            "model": ig.model,
            "base_url": ig.base_url,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "resolved_model": (resolved or {}).get("model", ""),
            "config_hint": (
                "「自动」会优先复用你已配置 Key 的服务；自定义需填 "
                "OpenAI 兼容的 /images/generations 接口地址与 Key；右侧「已配置」"
                "列出你已在「模型服务」里配好的服务，选中即复用它的地址与 Key，"
                "此时请在「模型」里填它支持的画图模型名。保存后立即生效。"
            ),
        }

    async def imagegen_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "zhipu", "siliconflow", "custom") and (
            provider not in self.cfg.providers
        ):
            raise RuntimeError("画图服务商只支持 auto / custom，或已配置的模型服务")
        updates: dict = {"provider": provider or None}
        for field_name in ("api_key", "model", "base_url"):
            if params.get(field_name) is not None:
                updates[field_name] = str(params[field_name]).strip()
        update_config_section("imagegen", updates)
        self.cfg = load_config()
        self._refresh_web_tool_configs()
        return await self.imagegen_detail()

    # ---- 语音输入（麦克风转文字） ----

    async def speech_detail(self) -> dict:
        """设置页语音输入卡片的数据（不返回明文 Key）。"""
        sp = self.cfg.speech
        resolved = resolve_speech(self.cfg)
        key = (resolved or {}).get("api_key", "")
        return {
            "provider": sp.provider,
            "providers": ["auto", "custom"],  # 前端只渲染「自动 / 自定义 / 已配置」三档
            "configured_services": self._configured_services(),
            "base_url": sp.base_url,
            "model": sp.model,
            "language": sp.language,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "resolved_model": (resolved or {}).get("model", ""),
            "config_hint": (
                "决定输入框里的麦克风按钮用哪个服务把语音转成文字。"
                "「自动」会复用你已配置 Key 的同协议服务（智谱 / 硅基流动 / OpenAI）；"
                "也可选「自定义」填任何 OpenAI 兼容的 /audio/transcriptions 地址"
                "（如本地 faster-whisper 服务，无需 Key）；右侧「已配置」列出你已在"
                "「模型服务」里配好的服务，选中即复用它的地址与 Key，"
                "但需要它本身提供语音识别接口。未配置时麦克风按钮会提示来这里配置。"
            ),
        }

    async def speech_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "zhipu", "siliconflow", "openai", "custom") and (
            provider not in self.cfg.providers
        ):
            raise RuntimeError("语音服务商只支持 auto / custom，或已配置的模型服务")
        updates: dict = {"provider": provider or None}
        for field_name in ("api_key", "model", "base_url", "language"):
            if params.get(field_name) is not None:
                updates[field_name] = str(params[field_name]).strip()
        update_config_section("speech", updates)
        self.cfg = load_config()
        return await self.speech_detail()

    async def speech_transcribe(self, audio_b64: str, mime: str = "") -> dict:
        """把一段录音（base64）交给配置好的服务转成文字。

        走 OpenAI 协议的 multipart /audio/transcriptions（智谱 GLM-ASR、硅基流动
        SenseVoice、OpenAI Whisper、本地 faster-whisper 服务都兼容）。
        没配置服务时抛可读错误——前端会把「去哪配」告诉用户，不静默失败。
        """
        import base64

        import httpx

        resolved = resolve_speech(self.cfg)
        if not resolved:
            raise RuntimeError(
                "还没配置语音转写服务。打开 设置 · 语音输入，选一个服务商并填入 API Key"
                "（或在「自定义」里填本地转写服务地址），保存后再试。"
            )
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("录音数据无效（base64 解码失败）") from e
        if not audio:
            raise RuntimeError("没有收到录音数据")
        if len(audio) > 25 * 1024 * 1024:
            raise RuntimeError("录音过长（上限 25 MB），请缩短后重试")

        suffix = ".wav"
        low = (mime or "").lower()
        if "webm" in low:
            suffix = ".webm"
        elif "ogg" in low:
            suffix = ".ogg"
        elif "mp4" in low or "m4a" in low:
            suffix = ".m4a"
        elif "mpeg" in low:
            suffix = ".mp3"
        files = {"file": ("audio" + suffix, audio, mime or "application/octet-stream")}
        data = {"model": resolved["model"]}
        if resolved.get("language"):
            data["language"] = resolved["language"]

        base = (resolved.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("语音服务缺少接口地址，请到 设置 · 语音输入 里补全")
        headers = {}
        if resolved.get("api_key"):
            headers["Authorization"] = "Bearer " + resolved["api_key"]
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=True) as client:
                resp = await client.post(
                    base + "/audio/transcriptions", headers=headers, files=files, data=data
                )
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"连接语音转写服务失败：{e}（可在 设置 · 语音输入 里检查接口地址/网络）"
            ) from e
        if resp.status_code >= 400:
            detail = resp.text.strip()[:300]
            raise RuntimeError(
                f"语音转写失败（HTTP {resp.status_code}）：{detail or '服务未返回内容'}"
            )
        try:
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("语音转写服务返回的不是 JSON，检查接口地址是否指向 OpenAI 兼容服务") from e
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("result") or "").strip()
        if not text:
            raise RuntimeError("没有识别到文字（可能是静音或语种不符），请靠近麦克风重试")
        return {"text": text, "provider": resolved["provider"], "model": resolved["model"]}

    def _refresh_web_tool_configs(self) -> None:
        """把新的搜索/画图配置热更新到所有会话的工具实例上（不重建注册表）。"""
        ws_kw = self._websearch_kwargs() or {}
        ig_kw = self._imagegen_kwargs() or {}
        for ag in self._for_each_agent():
            if ag.registry is None:
                continue
            ws_tool = ag.registry.get("web_search")
            if ws_tool is not None:
                ws_tool.provider = ws_kw.get("provider", "")
                ws_tool.api_key = ws_kw.get("api_key", "")
                ws_tool.base_url = ws_kw.get("base_url", "")
            ig_tool = ag.registry.get("generate_image")
            if ig_tool is not None:
                ig_tool.provider = ig_kw.get("provider", "")
                ig_tool.api_key = ig_kw.get("api_key", "")
                ig_tool.base_url = ig_kw.get("base_url", "")
                ig_tool.model = ig_kw.get("model", "")

    # ---- 局域网访问：绑定开关 + 令牌（重启服务后生效；前端拼 URL 与二维码） ----

    def note_token_failure(self, ip: str, where: str) -> None:
        """留痕一次令牌验证失败（HTTP 守卫与 WS 握手共用）。"""
        self._token_failure_total += 1
        self._token_failures.append({"ts": time.time(), "ip": ip, "where": where})
        del self._token_failures[:-TOKEN_FAILURE_LOG_MAX]

    def token_failure_summary(self) -> dict:
        """给设置页的失败概览：总次数 + 最近几条（ip / 时间）。"""
        recent = [
            {"ip": str(f.get("ip", "")), "ts": float(f.get("ts", 0) or 0)}
            for f in self._token_failures[-5:]
        ]
        return {"total": self._token_failure_total, "recent": recent}

    async def lan_status(self, include_token: bool = True) -> dict:
        """局域网访问状态；include_token=False 时不回传令牌（安全审查 A9）。

        令牌就是远程访问凭据本身：手机端设置页只需要知道「已设置」，
        不需要（也不应该）拿到明文。
        """
        server = self.cfg.server
        return {
            "enabled": bool(server.lan),
            "token": server.token if include_token else "",
            "has_token": bool(server.token),
            "ips": self._lan_ips(),
            "token_failures": self.token_failure_summary(),
            "note": "" if server.lan else "局域网访问当前关闭：服务只监听本机 127.0.0.1。",
        }

    async def lan_enable(self, params: dict) -> dict:
        server = self.cfg.server
        supplied = str(params.get("token") or "").strip()
        if supplied:
            self._check_lan_token(supplied)
        token = supplied or server.token or secrets.token_urlsafe(16)
        update_config_section("server", {"lan": True, "token": token})
        self.cfg = load_config()
        return {
            **await self.lan_status(),
            "note": "已开启局域网访问：重启 SkySheep 后生效（服务会监听全部网卡，"
                    "同一 Wi-Fi 下的设备凭令牌访问）。手机连不上时先看 Windows 防火墙——"
                    "首次弹出的「是否允许访问网络」要点允许；已错过的话在防火墙设置里"
                    "放行 SkySheep 后再试。",
        }

    async def lan_rotate_token(self) -> dict:
        """重新生成访问令牌，立即生效（守卫每次请求都读最新配置，不用重启）。

        旧令牌与已种下的 cookie 随即作废：之前发给手机的地址、二维码全部失效，
        手机要用新地址重新打开。失败计数与节流也一并清零——换了锁，旧的敲门
        记录不再有意义。
        """
        token = secrets.token_urlsafe(16)
        update_config_section("server", {"token": token})
        self.cfg = load_config()
        # 只清失败计数，已生效的封锁保留（见 reset_failures 的说明）
        self.token_throttle.reset_failures(keep_blocks=True)
        return {
            **await self.lan_status(),
            "note": "已生成新令牌并立即生效：之前的地址与二维码作废，"
                    "手机需要用下方新地址重新打开。",
        }

    @staticmethod
    def _check_lan_token(token: str) -> None:
        """自定义令牌的最小强度校验。

        默认令牌由 secrets.token_urlsafe(16) 生成（22 字符 / 128 bit 熵），无需校验。
        但 lan_enable 允许调用方传入自定义 token——这是降低整道防护强度的入口，
        弱令牌（"123456"、"password"）配上传二维码分享等于把服务开给同网段所有人。
        这里只查长度与字符多样性，不强制复杂度规则：令牌要能方便地输入/扫码。
        """
        if len(token) < MIN_LAN_TOKEN_CHARS:
            raise RuntimeError(
                f"令牌太短（{len(token)} 位）：至少 {MIN_LAN_TOKEN_CHARS} 位，"
                "建议直接留空由系统生成随机令牌"
            )
        if len(set(token)) < 4:
            raise RuntimeError(
                "令牌字符重复度过高（几乎全是同一个字符）：请换一个更随机的令牌，"
                "或留空由系统生成"
            )

    async def lan_disable(self) -> dict:
        update_config_section("server", {"lan": False})
        self.cfg = load_config()
        return {
            **await self.lan_status(),
            "note": "已关闭局域网访问：重启 SkySheep 后恢复仅本机监听。",
        }

    # ---- 远程访问（Tailscale）：手机不在同一网络也能连回这台电脑 ----
    # 与局域网访问共用令牌；绑定同样要等重启生效。守卫逻辑在 server/app.py：
    # 物理局域网等非 tailnet 来源直接拒绝，tailnet 来源必须验令牌。

    async def remote_status(self, include_token: bool = True) -> dict:
        """远程访问状态；include_token=False 时不回传令牌（同 lan_status，A9）。"""
        server = self.cfg.server
        return {
            "enabled": bool(server.tailscale),
            "token": server.token if include_token else "",
            "has_token": bool(server.token),
            "ips": self._tailscale_ips(),
            "token_failures": self.token_failure_summary(),
            "note": "" if server.tailscale else "远程访问当前关闭。",
        }

    async def remote_enable(self, params: dict) -> dict:
        server = self.cfg.server
        supplied = str(params.get("token") or "").strip()
        if supplied:
            self._check_lan_token(supplied)  # 与局域网访问同一把令牌、同一强度要求
        token = supplied or server.token or secrets.token_urlsafe(16)
        update_config_section("server", {"tailscale": True, "token": token})
        self.cfg = load_config()
        return {
            **await self.remote_status(),
            "note": "已开启远程访问：重启 SkySheep 后生效。手机需安装 Tailscale 并登录同一账号，"
                    "之后在任意网络（含手机流量）都能用下面的地址访问。",
        }

    async def remote_disable(self) -> dict:
        update_config_section("server", {"tailscale": False})
        self.cfg = load_config()
        return {
            **await self.remote_status(),
            "note": "已关闭远程访问：重启 SkySheep 后恢复仅本机监听。",
        }

    # ---- 一键重启：拉起等价的新进程，再请求当前进程优雅退出 ----
    # 新的桌面实例会先等旧进程的互斥体释放（desktop.py 的 _wait_mutex_free），
    # 所以这里不用掐着点：先发人再退场。退出走正常收尾（lifespan shutdown、
    # crash.flag 清除、窗口几何保存），不用 os._exit——那会留下「上次未正常
    # 关闭」的崩溃标记，下次启动吓用户一跳。

    @staticmethod
    def _relaunch_command() -> list[str] | None:
        """构造与当前进程等价的重启命令；无法确定时返回 None。

        * 打包态（PyInstaller）：重跑当前 exe，原样带上参数；
        * 脚本态（.py / .pyw 直接跑，如 SkySheep.pyw）：同一解释器重跑同一脚本；
        * 开发态（console script / -m，argv[0] 形态不定）：统一用 ``-c`` 调
          ``skysheep.cli.app.main()``，命令行参数原样透传。
        """
        if getattr(sys, "frozen", False):
            return [sys.executable, *sys.argv[1:]]
        arg0 = sys.argv[0] or ""
        if arg0.lower().endswith((".py", ".pyw")) and os.path.isfile(arg0):
            return [sys.executable, arg0, *sys.argv[1:]]
        return [
            sys.executable,
            "-c",
            "import sys; from skysheep.cli.app import main; sys.exit(main())",
            *sys.argv[1:],
        ]

    async def app_restart(self) -> dict:
        """重启应用。仅本机可调（dispatch 层 LOCAL_ONLY 门禁）。"""
        cmd = self._relaunch_command()
        if not cmd:
            raise RuntimeError("无法确定重启命令，请手动关闭后重新打开 SkySheep")
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": os.getcwd(),
        }
        if os.name == "nt":
            # DETACHED_PROCESS 脱离当前控制台；CREATE_NEW_PROCESS_GROUP 隔离
            # Ctrl+C/信号传播——新实例必须活得比当前进程久。不弹新窗口。
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | (  # type: ignore[attr-defined]
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 命令由 _relaunch_command 构造
        except OSError as e:
            raise RuntimeError(f"重启失败：{e}") from None
        logger.info("应用重启：新进程已拉起（%s），当前进程即将退出", cmd[0])
        spawn_bg(self._shutdown_soon())
        return {"ok": True, "note": "正在重启 SkySheep，窗口会自动恢复。"}

    async def _shutdown_soon(self) -> None:
        """稍等回包发出、前端有机会提示后，再走退出路径。"""
        await asyncio.sleep(0.6)
        hook = self.restart_hook
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 - 桌面钩子失败还有服务级兜底
                logger.warning("重启钩子（桌面收尾）执行失败", exc_info=True)
        shutdown = self.request_shutdown
        if shutdown is not None:
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                logger.warning("请求服务退出失败", exc_info=True)

    # ---- 聊天软件渠道（Bot Channel）：设置、会话、运行 ----
    # 安全姿态与 computer_control / browser_control 同类：默认关、属降低防护的开关，
    # 因此 channel.* 的写操作在 server/app.py 的 dispatch 层仅允许本机调用。

    def _channels_config(self) -> dict:
        """给 ChannelManager 的配置提供者：每次都读最新 cfg，改完设置即时生效。"""
        if self.cfg is None:
            return {}
        return dict(self.cfg.channels.platforms or {})

    def _channel_credentials_ready(self, name: str, section: dict) -> bool:
        """该平台的凭据是否齐全（未启用、未配置的渠道也要能正确判定）。

        各平台凭据形状不同：飞书是 app_id + app_secret 两个字段，微信是扫码换来的
        bot_token（运行态而非手填），其余历史平台用单个 token。
        """
        if name == "weixin":
            return bool(str(section.get("bot_token", "")).strip())
        if name == "feishu":
            return bool(
                str(section.get("app_id", "")).strip()
                and str(section.get("app_secret", "")).strip()
            )
        return bool(str(section.get("token", "")).strip())

    async def channel_status(self) -> dict:
        """渠道运行态 + 见过的来源（设置页渲染用）。"""
        cfg = self.cfg.channels if self.cfg is not None else None
        platforms = dict((cfg.platforms if cfg else {}) or {})
        manager_status = self.channels.status() if self.channels else {
            "channels": [], "supported": ["feishu", "weixin"],
        }
        # 把配置里与支持的平台都并进来：未启动、未配置的渠道也要在界面上可见可编辑，
        # 否则用户得先“添加”才能看到入口（体验上多一步且不像开关）。
        supported = set(manager_status.get("supported") or [])
        known = {c["name"] for c in manager_status["channels"]}
        for name in manager_status.get("supported") or []:
            if name not in known:
                manager_status["channels"].append({
                    "name": name,
                    "enabled": False,
                    "running": False,
                    "configured": False,
                    "error": "",
                    "seen_sources": [],
                })
                known.add(name)
        for name, section in platforms.items():
            # 只列当前版本真的支持（有适配器）的平台：升级后配置里可能残留已下线的
            # 平台（如旧的 telegram 段），把它当卡片列出来会得到一个永远启不动的
            # 死入口；这里直接跳过，不动用户的配置文件。
            if name not in supported or name in known:
                continue
            manager_status["channels"].append({
                "name": name,
                "enabled": bool(section.get("enabled", False)),
                "running": False,
                "configured": self._channel_credentials_ready(name, section),
                "error": "",
                "seen_sources": [],
            })
        # 允许名单回显（含凭据是否就绪，不回显凭据本身）
        for item in manager_status["channels"]:
            section = platforms.get(item["name"]) or {}
            item["allowed_ids"] = [str(x) for x in (section.get("allowed_ids") or [])]
            item["has_token"] = bool(str(section.get("token", "")).strip())
            item["approve_enabled"] = bool(section.get("approve_enabled", False))
            # 微信：凭据来自扫码，且会失效，界面要显示得更具体
            item["has_login"] = bool(str(section.get("bot_token", "")).strip())
            item["needs_qr"] = item["name"] == "weixin" and not item["has_login"]
            # 飞书：凭据是两个字段，界面要分别回显“已保存”而不是只认一个 token
            item["has_app_id"] = bool(str(section.get("app_id", "")).strip())
            item["has_app_secret"] = bool(str(section.get("app_secret", "")).strip())
            # 预授权名单（只读回显 + 危险工具告警）：配置里手写的 run_command
            # 等于「无人值守任意命令」，界面上必须看得见（安全审查低危项）
            allowed_tools = [str(x) for x in (section.get("allowed_tools") or [])]
            item["allowed_tools"] = allowed_tools
            item["tools_warning"] = _channel_allowed_tools_warning(allowed_tools)
        manager_status["approve_timeout"] = int(cfg.approve_timeout) if cfg else 120
        # 见过的来源合并持久化记录（重启后不丢，方便事后认领）
        if self.store is not None:
            try:
                seen = await self.store.list_channel_sources()
            except Exception:  # noqa: BLE001
                seen = []
            by_channel: dict[str, list] = {}
            for row in seen:
                by_channel.setdefault(row["channel"], []).append({
                    "actor": row["actor"], "chat_id": row["chat_id"],
                    "ts": row["last_seen"], "count": row["hits"],
                })
            for item in manager_status["channels"]:
                memory = {s["chat_id"] for s in item.get("seen_sources") or []}
                for row in by_channel.get(item["name"], []):
                    if row["chat_id"] not in memory:
                        item.setdefault("seen_sources", []).append(row)
        return manager_status

    async def channel_save(self, params: dict) -> dict:
        """保存一个平台的配置（不启停，启停走 channel_enable / channel_disable）。

        只在显式传入凭据时才覆盖已存值：界面保存允许名单时不该把凭据清掉。
        """
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("缺少平台名 name")
        platforms = dict(self.cfg.channels.platforms or {})
        section = dict(platforms.get(name) or {})
        if params.get("token") is not None:
            section["token"] = str(params["token"]).strip()
        # 飞书的凭据是两个字段（App ID / App Secret），不像微信那样是扫码换来的
        # 单个 bot_token。与 token 一样只在显式传入时覆盖，避免保存名单时把已存凭据清掉。
        if params.get("app_id") is not None:
            section["app_id"] = str(params["app_id"]).strip()
        if params.get("app_secret") is not None:
            section["app_secret"] = str(params["app_secret"]).strip()
        # 飞书依赖外部 lark-cli；允许显式指定路径（留空即用 PATH 查找）
        if params.get("cli_path") is not None:
            section["cli_path"] = str(params["cli_path"]).strip()
        if params.get("allowed_ids") is not None:
            section["allowed_ids"] = _normalize_id_list(params["allowed_ids"])
        if params.get("approve_enabled") is not None:
            section["approve_enabled"] = bool(params["approve_enabled"])
        if params.get("allowed_tools") is not None:
            raw_tools = params["allowed_tools"]
            if isinstance(raw_tools, str):
                raw_tools = [x for x in raw_tools.replace(",", "\n").splitlines()]
            section["allowed_tools"] = [str(x).strip() for x in (raw_tools or []) if str(x).strip()]
        platforms[name] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        # 预授权名单是整工具级放行：把 run_command / write_file 这类写进渠道配置，
        # 等于「无人值守时任意命令/任意写入」——保存成功时明确告警（安全审查低危项）
        warning = _channel_allowed_tools_warning(section.get("allowed_tools"))
        # 名单/凭据改动必须重建适配器才能热生效：适配器拿的是构造时那份 section，
        # 不重建的话「加入允许名单」后机器人仍用旧名单判断，消息继续被忽略——
        # 这是真实发生过的 bug（名单存进去了，机器人却永远不回话）。
        # 微信的运行态 token 由 channel_save_state 持久化，重建不会丢登录态。
        if self.channels is not None:
            await self.channels.restart()
        self._refresh_channel_gates()
        out = await self.channel_status()
        if warning:
            out["warning"] = warning
        return out

    async def channel_save_state(self, name: str, state: dict) -> None:
        """把适配器的运行时状态（微信的 bot_token / 游标）落盘。

        与 channel_save 分开：这是适配器自己触发的（扫码成功、游标推进），
        不是用户在界面上的操作。写前重读最新配置再合并，避免并发覆盖用户的改动。
        """
        if not isinstance(state, dict):
            return
        try:
            fresh = load_config()
            platforms = dict(fresh.channels.platforms or {})
            section = dict(platforms.get(name) or {})
            for key in ("bot_token", "base_url", "cursor"):
                if key in state and state[key] is not None:
                    section[key] = str(state[key])
            platforms[name] = section
            update_config_section("channels", {"platforms": platforms})
            self.cfg = load_config()
        except Exception as e:  # noqa: BLE001 - 状态落盘失败不应影响对话
            logger.warning("渠道 %s 状态落盘失败：%s", name, e)

    # ---- 微信扫码登录（交互式流程，桌面端驱动） ----

    async def channel_weixin_login_start(self) -> dict:
        """生成微信登录二维码。"""
        channel = self._channel_obj("weixin")
        if channel is None:
            # 尚未建渠道实例（未启用过）时临时建一个，只用于走登录流程
            from ..channels.weixin import WeixinChannel

            section = dict((self.cfg.channels.platforms or {}).get("weixin") or {})
            channel = WeixinChannel(section, lambda _m: None)
            self._weixin_login_channel = channel
        info = await channel.fetch_qrcode()
        return {
            "qrcode": info["qrcode"],
            "url": info["url"],
            "note": "用手机微信扫码并在手机上确认，确认后点下方「我已完成扫码」。",
        }

    async def channel_weixin_login_poll(self, params: dict) -> dict:
        """轮询扫码状态；confirmed 时保存凭据并重建渠道。"""
        qrcode = str(params.get("qrcode", "")).strip()
        if not qrcode:
            raise RuntimeError("缺少 qrcode")
        channel = self._channel_obj("weixin") or getattr(self, "_weixin_login_channel", None)
        if channel is None:
            raise RuntimeError("微信渠道尚未初始化，请先点「生成二维码」")
        result = await channel.poll_qrcode(
            qrcode, verify_code=str(params.get("verify_code", "") or "")
        )
        if result.get("status") != "confirmed":
            return result
        token = str(result.get("bot_token") or "").strip()
        if not token:
            raise RuntimeError(
                "服务器返回已确认，但没有拿到 bot_token（响应结构已记入引擎日志），"
                "请重新生成二维码再试一次；仍失败请带日志反馈"
            )
        base_url = str(result.get("base_url") or "")
        # 落盘（含 base_url，服务器可能下发不同的接入点）
        await self.channel_save_state("weixin", {
            "bot_token": token, **({"base_url": base_url} if base_url else {}),
        })
        if channel is not None:
            await channel.apply_login(token, base_url)
        # 重建渠道使新 token 生效
        if self.channels is not None:
            await self.channels.restart()
        self._refresh_channel_gates()
        return {"status": "confirmed", "saved": True}

    async def channel_weixin_logout(self) -> dict:
        """清除微信登录态（用户主动退出或 token 失效后重登）。"""
        fresh = load_config()
        platforms = dict(fresh.channels.platforms or {})
        section = dict(platforms.get("weixin") or {})
        section.pop("bot_token", None)
        section.pop("cursor", None)
        platforms["weixin"] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        if self.channels is not None:
            await self.channels.restart()
        self._refresh_channel_gates()
        return await self.channel_status()

    def _channel_obj(self, name: str):
        if self.channels is None:
            return None
        return self.channels.channels.get(name)

    async def channel_enable(self, params: dict) -> dict:
        """启用一个平台并立即重建渠道（不再要求重启整个应用）。

        只校验凭据（飞书是手填的 App ID + App Secret，微信是扫码换来的 bot_token）。
        允许名单**允许为空**：chat id 只能由运行中的机器人记进「发现的来源」，
        不先启用就永远拿不到第一条消息——这里的名单检查曾把首次配置锁死。
        空名单的安全语义（拒绝一切、未授权只记录不回复）由消息层强制，不在这一步。
        """
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("缺少平台名 name")
        platforms = dict(self.cfg.channels.platforms or {})
        section = dict(platforms.get(name) or {})
        if name == "weixin":
            if not str(section.get("bot_token", "")).strip():
                raise RuntimeError("微信还没登录：先在下方点「生成二维码」并扫码确认")
        elif name == "feishu":
            if not str(section.get("app_id", "")).strip():
                raise RuntimeError("飞书还没填 App ID，先填好再启用")
            if not str(section.get("app_secret", "")).strip():
                raise RuntimeError("飞书还没填 App Secret，先填好再启用")
        elif not str(section.get("token", "")).strip():
            raise RuntimeError(f"「{name}」还没填凭据，先填好再启用")
        # 注意：这里**不能**再要求允许名单非空。chat id 只能由运行中的机器人
        # 收到第一条消息后记进「发现的来源」（见 manager.note_seen），而机器人
        # 只有启用后才会轮询——先启用再认领是唯一能走通的顺序，把名单检查
        # 放在这里就是一个引导死锁（永远拿不到第一个 chat id）。
        # 安全性不受影响：空名单 = 拒绝一切由消息层的 manager._on_message 强制，
        # 未授权来源只记录不回复，机器人跑着也不会应答陌生人。
        section["enabled"] = True
        platforms[name] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        await self.channels.restart()
        self._refresh_channel_gates()
        return await self.channel_status()

    async def channel_disable(self, params: dict) -> dict:
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("缺少平台名 name")
        platforms = dict(self.cfg.channels.platforms or {})
        section = dict(platforms.get(name) or {})
        section["enabled"] = False
        platforms[name] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        await self.channels.restart()
        self._refresh_channel_gates()
        return await self.channel_status()

    async def channel_set_timeout(self, params: dict) -> dict:
        value = max(10, min(3600, int(params.get("approve_timeout") or 120)))
        update_config_section("channels", {"approve_timeout": value})
        self.cfg = load_config()
        return await self.channel_status()

    async def channel_test(self, params: dict) -> dict:
        """试发一条消息，验证 token 与 chat_id 是否都对。"""
        name = str(params.get("name", "")).strip()
        chat_id = str(params.get("chat_id", "")).strip()
        if not name or not chat_id:
            raise RuntimeError("需要平台名与 chat_id")
        if self.channels is None:
            raise RuntimeError("渠道管理器尚未就绪")
        channel = self.channels.channels.get(name)
        if channel is None:
            raise RuntimeError(f"平台「{name}」还没启用")
        ok = await channel.send_text(chat_id, "🐑 SkySheep 测试消息：这条能收到，说明配置通了。")
        return {"ok": ok, "error": channel.error}

    # ---- 渠道会话与运行（ChannelManager 的回调面） ----

    async def channel_ensure_session(self, channel_name: str) -> str:
        """取（或建）某个渠道绑定的会话。不切活动会话指针——渠道与桌面可并行。

        渠道会话固定归到「远程连接」项目（不落当前项目或快聊）：渠道对话的
        工作目录与桌面项目无关。绑定指向的会话必须属于该项目：会话可能被
        桌面端移到别的项目或删除，此时并到下方的自愈路径重开一个，而不是把
        别的项目的会话挂进渠道的工作目录与门控下（归属校验，同 B 族）。
        """
        remote_pid = await self._remote_project_id()
        bound = await self.store.get_channel_binding(channel_name)
        if bound and await self.store.get_session_for_project(bound, remote_pid) is not None:
            # 绑定存在但 runtime 可能不在：重启后首次使用、或该会话被桌面端切走时。
            # 这里必须补建，否则 channel_run 会因「会话不存在」直接失败。
            await self._get_channel_runtime(bound, channel_name)
            return bound
        sess = await self.store.create_session(remote_pid, title=f"🤖 {channel_name}")
        await self.store.set_channel_binding(channel_name, sess.id)
        await self._get_channel_runtime(sess.id, channel_name)
        return sess.id

    async def channel_new_session(self, channel_name: str) -> str:
        """给渠道开一个新会话（/new），旧的保留可查。固定挂在「远程连接」项目下。"""
        remote_pid = await self._remote_project_id()
        sess = await self.store.create_session(remote_pid, title=f"🤖 {channel_name}")
        await self.store.set_channel_binding(channel_name, sess.id)
        await self._get_channel_runtime(sess.id, channel_name)
        return sess.id

    async def _get_channel_runtime(self, session_id: str, channel_name: str) -> SessionRuntime:
        """渠道会话的 runtime：用 ChannelGate 而非默认门控。

        与 _get_runtime 的差别只在 gate——渠道的权限姿态必须由渠道决定：
        默认门控会产出 PermissionRequest 并无限期等前端，渠道场景下没有前端。
        工作目录与白名单归属都走「远程连接」项目（渠道对话与桌面工作目录无关；
        文件/命令类工具本就在 ChannelGate 允许清单默认拒绝之列）。
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            return rt
        remote_pid = await self._remote_project_id()
        remote_dir = Path.home()
        section = dict((self.cfg.channels.platforms or {}).get(channel_name) or {})
        gate = ChannelGate(
            allowed=list(section.get("allowed_tools") or []),
            approve_enabled=bool(section.get("approve_enabled", False)),
            approve_timeout=int(self.cfg.channels.approve_timeout),
            store=self.store,
            project_id=remote_pid,
            working_dir=remote_dir,
        )
        # notify 绑定建好的 gate：审批卡优先回本轮发起消息所在的聊天（审查 P3-15）
        gate.notify = lambda pending, _g=gate: self._notify_channel_approval(
            channel_name, pending, _g
        )
        recorder = ChangeRecorder()
        rt = SessionRuntime(
            sid=session_id,
            agent=Agent(
                provider=self.provider,
                registry=self._build_full_registry(recorder),
                gate=gate,
                working_dir=remote_dir,
                max_iterations=self.cfg.max_iterations,
                context_limit_tokens=self._context_limit(),
                compaction_keep_recent=self.cfg.compaction_keep_recent,
                compaction_trigger=self.cfg.compaction_trigger,
                hooks=self.hooks,
                restrict_to_workdir=self.cfg.restrict_to_workdir,
                session_id=session_id,
            ),
            recorder=recorder,
        )
        await self._reload_agent_history(rt.agent, session_id)
        self.runtimes[session_id] = rt
        self._channel_gates[session_id] = gate
        self._channel_names[session_id] = channel_name
        return rt

    def _refresh_channel_gates(self) -> None:
        """配置保存/启停后刷新已缓存渠道会话的无人值守门控（审查 P2-11）。

        门控参数（approve_enabled / allowed_tools）是会话首次使用时从配置快照
        构造的：不刷新的话，改完配置要等新渠道会话（或重启）才生效，窗口期内
        界面显示与实际放行口径不一致。正在跑的轮次不动（换门会丢掉在等的审批），
        空闲会话就地换新门；新会话本就走 _get_channel_runtime 重建，不受影响。
        """
        for sid, name in list(self._channel_names.items()):
            rt = self.runtimes.get(sid)
            if rt is None:
                continue
            run_task = getattr(rt, "run_task", None)
            if run_task is not None and not run_task.done():
                continue
            old = self._channel_gates.get(sid)
            section = dict((self.cfg.channels.platforms or {}).get(name) or {})
            gate = ChannelGate(
                allowed=list(section.get("allowed_tools") or []),
                approve_enabled=bool(section.get("approve_enabled", False)),
                approve_timeout=int(self.cfg.channels.approve_timeout),
                store=self.store,
                project_id=getattr(old, "project_id", None),
                working_dir=getattr(old, "working_dir", None),
            )
            gate.notify = lambda pending, _g=gate, _n=name: self._notify_channel_approval(
                _n, pending, _g
            )
            rt.agent.gate = gate
            self._channel_gates[sid] = gate

    async def _notify_channel_approval(self, channel_name: str, pending, gate=None) -> None:
        """把审批卡片推到聊天窗口。

        gate 带 turn_chat_id（本轮发起消息所在的聊天）时优先回它——回信地址
        若取「最近一条入站消息」，发起轮之后其它名单内聊天来一条消息就会把
        卡片带偏（审查 P3-15）。
        """
        if self.channels is None:
            return
        chat_id = str(getattr(gate, "turn_chat_id", "") or "") or self._channel_last_chat.get(
            channel_name, ""
        )
        channel = self.channels.channels.get(channel_name)
        if not chat_id or channel is None:
            return
        lines = [
            "⚠ 需要你确认一个操作",
            "",
            f"工具：{pending.tool_name}",
            f"内容：{pending.detail}",
        ]
        if pending.diff:
            lines += ["", "改动预览：", "```", pending.diff[:1500], "```"]
        lines += [
            "",
            # 渠道端没有「总是允许」：白名单规则是持久化的，从聊天窗口写入后
            # 所有渠道会话都不再询问，代价与便利不成比例（ChannelGate 里强制
            # 降级为单次），需要预授权时在桌面端本机界面操作。
            "回复 allow（允许一次）或 deny（拒绝），只有发起这一轮的账号能决定；"
            f"超过 {self.cfg.channels.approve_timeout} 秒未回复会自动拒绝。",
        ]
        try:
            await channel.send_text(chat_id, "\n".join(lines))
        except Exception as e:  # noqa: BLE001 - 推卡片失败由 Gate 退化成拒绝
            logger.warning("推送审批卡片失败：%s", e)
            raise

    async def channel_run(self, session_id: str, text: str, actor: str = "",
                          chat_id: str = "") -> dict:
        """跑一轮渠道对话，返回回复文本。不劫持活动会话。

        actor 是这一轮的发起人（渠道消息里的 sender 标识）：写进门控后，
        本轮的审批决定只认他，群聊里其他成员的 allow/deny 不生效。
        chat_id 是发起消息所在的聊天：审批卡回这个聊天，而不是「最近一条
        入站消息」的聊天（审查 P3-15）。
        """
        if self.provider is None:
            return {"error": "尚未配置可用的模型 API Key"}
        rt = self.runtimes.get(session_id)
        if rt is None:
            # 自愈：runtime 可能尚未建立（重启后首次、或已被回收）。反查渠道绑定补建，
            # 而不是直接把「会话不存在」丢给聊天窗口——用户看到这句无从下手。
            channel_name = self._channel_names.get(session_id, "")
            if not channel_name:
                try:
                    channel_name = await self.store.find_channel_by_session(session_id) or ""
                except Exception:  # noqa: BLE001 - 反查失败退化成下方提示
                    channel_name = ""
            if not channel_name:
                return {"error": "这个会话已不是渠道会话了，发送 /new 开一个新的"}
            # 自愈前先验归属：会话被移到别的项目后不能挂回渠道执行（同 B 族）。
            # 渠道会话的归属项目是「远程连接」，不再随当前项目走。
            if await self.store.get_session_for_project(
                session_id, await self._remote_project_id()
            ) is None:
                return {"error": "这个会话已不在「远程连接」项目里，发送 /new 开一个新的"}
            rt = await self._get_channel_runtime(session_id, channel_name)
        # 本轮发起人写进门控：审批决定只认他（见 ChannelGate.submit_latest）。
        gate = self._channel_gates.get(session_id)
        if gate is not None:
            gate.turn_actor = str(actor or "")
            gate.turn_chat_id = str(chat_id or "")
        collected: list[str] = []
        think_text = ""
        think_ms = 0
        turn_ms = 0

        async def collect(ev: dict) -> None:
            nonlocal think_text, think_ms, turn_ms
            if ev.get("kind") != "assistant_message":
                return
            msg = ev.get("message")
            if not isinstance(msg, dict):
                return
            text_out = msg.get("text", "")
            if text_out:
                collected.append(str(text_out))
            # 思考与耗时只在最终回答上取（中间迭代是过程消息，没有耗时）
            if msg.get("duration_ms"):
                turn_ms = int(msg.get("duration_ms") or 0)
                think_ms = int(msg.get("thinking_ms") or 0)
                think_text = "".join(
                    str(b.get("text", "")) for b in (msg.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "thinking"
                )

        try:
            result = await self._run_turn_pipeline(
                text, collect, plan_mode=False, runtime=rt, session_id=session_id,
            )
        except asyncio.CancelledError:
            return {"error": "这一轮被中断"}
        except Exception as e:  # noqa: BLE001 - 把原因回给聊天窗口
            return {"error": str(e)}
        if result.get("stopped") and not collected:
            return {"error": "这一轮被中断"}
        # assistant_message 事件带的是完整消息；没有则回落到历史里最后一条助手文本
        reply = collected[-1] if collected else ""
        if not reply:
            for m in reversed(rt.agent.history):
                if m.role == "assistant" and m.text.strip():
                    reply = m.text.strip()
                    if getattr(m, "duration_ms", 0):
                        turn_ms = int(m.duration_ms)
                        think_ms = int(m.thinking_ms)
                        think_text = m.thinking
                    break
        return {
            "text": reply,
            "session_id": session_id,
            # 渠道端也透出思考与用时（遥控时看不到桌面界面，这是唯一的进度感）
            "thinking_chars": len(think_text),
            "thinking_ms": think_ms,
            "duration_ms": turn_ms,
        }

    async def channel_stop(self, session_id: str) -> None:
        self.cancel_run(session_id)

    async def channel_status_text(self, session_id: str) -> str:
        """给 /status 命令用的简短状态。"""
        sess = await self.store.get_session(session_id)
        title = (sess.title if sess else "") or "（无标题）"
        return (
            f"会话：{title}\n"
            f"模型：{self.provider_name or '未配置'} / {self.provider_model or '-'}\n"
            "项目：远程连接（渠道会话的固定项目）\n"
            f"上下文：{self._context_limit():,} tokens"
        )

    async def channel_list_sessions(self) -> list[dict]:
        """渠道端 /sessions：只列「远程连接」项目的会话（审查 P3-14）。

        会话标题默认由首条消息生成，属于桌面用户的输入内容——桌面当前项目
        的会话清单不该透给渠道侧（那里只该看到渠道自己的会话）。
        """
        remote_pid = await self._remote_project_id()
        rows = (
            await self.store.list_sessions(remote_pid)
            if remote_pid is not None
            else []
        )
        current_ids = set(self._channel_gates.keys())
        return [
            {
                "id": s.id,
                "title": s.title,
                "current": s.id in current_ids,
            }
            for s in rows
        ]

    async def channel_submit_decision(
        self, channel_name: str, decision: str, actor: str = ""
    ) -> dict:
        """把聊天窗口的审批回复投给等待中的门控。

        返回 ``{"hit": bool, "actor_mismatch": bool}``：hit=True 表示决定已生效；
        actor_mismatch=True 表示有待决策项、但回复者不是这一轮的发起人（群聊
        场景），决定不生效——调用方据此提示，而不是把 "allow" 当新消息再跑一轮。
        """
        session_id = await self.store.get_channel_binding(channel_name)
        if not session_id:
            return {"hit": False}
        gate = self._channel_gates.get(session_id)
        if gate is None or not gate.waiting:
            return {"hit": False}
        expected = gate.turn_actor or ""
        actor_s = str(actor or "")
        # 与 gate.submit_latest 同一收紧（审查 S-08 + P3-17）：发起人 id 是
        # 校验的根本依据，任一侧为空都视为不匹配——「绑定或回复缺 id」不再
        # 退回旧行为（否则丢失 actor 的消息可替发起人批准）。
        if not expected or expected != actor_s:
            return {"hit": False, "actor_mismatch": True}
        return {"hit": bool(gate.submit_latest(decision, actor))}

    async def channel_has_waiting_decision(self, channel_name: str) -> bool:
        """该渠道是否有等待中的审批（manager 用它区分「回应确认卡」与普通消息）。"""
        session_id = await self.store.get_channel_binding(channel_name)
        if not session_id:
            return False
        gate = self._channel_gates.get(session_id)
        return bool(gate is not None and gate.waiting)

    def note_channel_chat(self, channel_name: str, chat_id: str) -> None:
        self._channel_last_chat[channel_name] = chat_id

    @staticmethod
    def _hostname_ipv4s() -> list[str]:
        """本机所有非回环网卡的 IPv4（getaddrinfo 枚举，含 Tailscale 虚拟网卡）。"""
        ips: list[str] = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                obj = ipaddress.ip_address(ip)
                if not obj.is_loopback and not obj.is_link_local and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        return ips

    @classmethod
    def _lan_ips(cls) -> list[str]:
        """本机在物理局域网里的 IPv4（排除 Tailscale 虚拟网段；UDP connect 技巧兜底）。"""

        def keep(ip: str) -> bool:
            obj = ipaddress.ip_address(ip)
            if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
                obj = obj.ipv4_mapped
            return isinstance(obj, ipaddress.IPv4Address) and obj not in TAILSCALE_NETS[0]

        ips = [ip for ip in cls._hostname_ipv4s() if keep(ip)]
        if not ips:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("10.255.255.255", 1))
                    ip = s.getsockname()[0]
                if ip and not ipaddress.ip_address(ip).is_loopback and keep(ip):
                    ips.append(ip)
            except OSError:
                pass
        return ips

    @classmethod
    def _tailscale_ips(cls) -> list[str]:
        """本机 Tailscale 虚拟网卡的 IPv4（100.64.0.0/10；没装/没运行则为空）。"""
        return [ip for ip in cls._hostname_ipv4s() if ipaddress.ip_address(ip) in TAILSCALE_NETS[0]]

    # ---- 更新检查（设置 · 关于可手动触发；启动时后台已查过一次） ----

    async def check_update(self) -> dict:
        try:
            info = await check_latest_release()
        except Exception as e:  # noqa: BLE001 - 手动检查要把失败原因说清楚
            self.update_error = str(e)
            self.update_info = None
            return {"available": False, "current": __version__, "frozen": self._is_frozen,
                    "error": str(e)}
        self.update_error = None
        if is_newer_version(info["version"], __version__):
            self.update_info = info
            return {"available": True, "current": __version__, "frozen": self._is_frozen, **info}
        self.update_info = None
        return {"available": False, "current": __version__, "frozen": self._is_frozen,
                "latest": info["version"]}

    async def install_update(self) -> dict:
        """应用内一键更新（仅安装版）：下载最新版安装包到临时目录。

        源码版直接拒绝——源码的更新方式是 git pull，静默装安装包反而会把
        运行中的源码目录搅乱。下载完成后并不自动安装，等 apply_update 确认。
        """
        if not self._is_frozen:
            raise RuntimeError("源码版不支持应用内更新：请在仓库里执行 git pull 后重启")
        info = await check_latest_release()
        if not is_newer_version(info["version"], __version__):
            return {"update_available": False, "current": __version__, "latest": info["version"]}
        # setup_url 是按发布约定拼的（releases/download/<tag>/SkySheep-<版本>-setup.exe），
        # 附件真缺要到下载时才以 404 暴露
        url = info["setup_url"]
        # 落在随机名的私有临时目录里，不用可预测的固定文件名（同机同用户抢置）
        dest = _update_dir() / f"SkySheep-{info['version']}-setup.exe"
        import httpx

        expected = await asyncio.to_thread(_fetch_setup_sha256, url)
        try:
            await asyncio.to_thread(_download_setup, url, dest, expected)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise RuntimeError(
                    "最新版没有 Windows 安装包附件，请到 GitHub Releases 手动下载") from e
            raise
        self._pending_update = str(dest)
        return {"update_available": True, "version": info["version"], "path": str(dest),
                "verified": bool(expected),
                # 本机安装登记的侧别决定装的时候要不要提权（HKLM=所有用户=弹 UAC，
                # HKCU=当前用户=静默直装）。前端据此在退出前就把「留意系统弹窗」
                # 讲清楚——应用退出后提示文本就看不见了。
                "uac": _setup_privilege_override() != "/CURRENTUSER"}

    async def apply_update(self) -> dict:
        """退出本应用并静默运行已下载的安装包。安装包声明了与本应用相同的
        单实例互斥体，且批处理延迟 2 秒再拉起它——届时本进程已退出、互斥体已释放。

        执行走 apply-update.cmd（见 _write_update_helper 的注释：直接拼命令串
        会被 Popen 的参数转义弄坏，安装器根本起不来）。
        """
        pending = self._pending_update or ""
        if not pending or not Path(pending).is_file():
            raise RuntimeError("还没有下载好的更新包，请先执行「下载更新」")
        log_path = _prepare_update_log(pending)
        override = _setup_privilege_override()
        script = _write_update_helper(pending, log_path, override)
        if script is None:
            raise RuntimeError("无法写出更新脚本（临时目录不可写），请到 GitHub Releases 手动下载安装")
        flags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                ["cmd", "/c", script.name],
                creationflags=flags, close_fds=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(script.parent),  # 相对名执行，彻底避开路径引号问题
            )
        except OSError as e:
            raise RuntimeError(f"无法启动安装程序：{e}") from e

        async def _quit_soon() -> None:
            await asyncio.sleep(1.0)  # 给 WS 回复留出送达时间
            try:
                await self.shutdown()
            except Exception:  # noqa: BLE001 - 退出路径不再抛
                pass
            os._exit(0)

        spawn_bg(_quit_soon())
        return {"quitting": True, "installer": pending, "log": str(log_path) if log_path else "",
                "uac": override != "/CURRENTUSER"}

    # ---- 主题：标题栏联动（前端把解析后的主题回传给桌面壳） ----

    async def apply_theme(self, params: dict) -> dict:
        from .. import wintheme

        # 前端把解析后的主题回传：优先用主题 id（标题栏 / 窗口底色逐主题精确一致），
        # 旧版字段 resolved（light/dark 两档）兜底；未知值回退 paper。
        theme = str(params.get("theme") or "").strip().lower()
        if theme in wintheme.THEME_PALETTE:
            wintheme.set_theme_mode(theme)
        else:
            mode = "dark" if str(params.get("resolved", "light")) == "dark" else "light"
            wintheme.set_theme_mode(mode)
        # 事件驱动重刷标题栏：前端 CSS 变量是瞬时变色，而桌面壳看板线程每秒才
        # 轮询一次——不等这里推一把，页面和系统标题栏的变色时间肉眼可见地不一致。
        try:
            wintheme.refresh_now()
        except Exception:  # noqa: BLE001 - 浏览器模式无窗口 / 老系统无 DWM：静默
            pass
        return {"mode": wintheme.current_theme_mode()}

    # ---- 快照 ----

    # 设置导出/导入覆盖的文件（~/.skysheep 下；会话数据库与检查点不导，体积大且含隐私对话）
    SETTINGS_EXPORT_FILES = (
        "config.toml", "mcp.json", "ui.json", "memory.md", "subagents.json",
        "skills-scope.json",
    )

    async def settings_export(self) -> dict:
        """把配置类文件打包为 zip（base64 回传前端下载）。用于换机迁移/备份。"""
        import base64
        import io
        import zipfile

        buf = io.BytesIO()
        included = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self.SETTINGS_EXPORT_FILES:
                p = skysheep_home() / name
                if p.is_file():
                    try:
                        zf.writestr(name, p.read_bytes())
                        included.append(name)
                    except OSError:
                        pass
            # 项目级 AGENTS.md 不在 home 下，跳过；技能目录打包清单文件（体积考虑，不含技能正文）
            skills = skysheep_home() / "skills"
            if skills.is_dir():
                manifest = [
                    {"name": s.name, "enabled": s.enabled}
                    for s in self.skills.all() if s.source == "global"
                ]
                zf.writestr("skills-manifest.json", json.dumps(manifest, ensure_ascii=False))
                included.append("skills-manifest.json")
        return {
            "filename": f"skysheep-settings-{date.today().isoformat()}.zip",
            "b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "included": included,
        }

    async def settings_import(self, params: dict) -> dict:
        """从导出的 zip 恢复设置（base64 上传）。覆盖同名文件，缺失的跳过。

        返回恢复清单；config.toml 恢复后由前端 boot() 重载生效。
        """
        import base64
        import io
        import zipfile

        raw = str(params.get("b64", "") or "")
        if not raw:
            raise RuntimeError("缺少 zip 内容（b64）")
        try:
            data = base64.b64decode(raw, validate=True)
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:
            raise RuntimeError(f"zip 解析失败: {e}") from None
        restored, skipped = [], []
        for name in zf.namelist():
            if name not in self.SETTINGS_EXPORT_FILES:
                continue  # skills-manifest.json 等：暂不自动恢复，避免覆盖现有技能状态
            try:
                target = skysheep_home() / name
                if name == "ui.json":
                    # 「自动允许写入」属于降低防护的开关，只能由本机用户主动切换：
                    # 导入包里带来的该字段一律不生效（换机/别人给的包不应该顺手放宽写入）
                    self._strip_accept_edits_from_ui_export(target, zf.read(name))
                else:
                    target.write_bytes(zf.read(name))
                restored.append(name)
            except OSError:
                skipped.append(name)
        if "config.toml" in restored:
            try:
                self.cfg = load_config()
            except Exception as e:
                raise RuntimeError(f"配置已写入但加载失败，请检查 config.toml：{e}") from None
        if "ui.json" in restored:
            # 导入后同步内存里的档位（否则要等重启才与文件一致）
            self._apply_gate_accept_pref(self._read_ui_prefs().get("accept_edits", 0))
        return {"restored": restored, "skipped": skipped}

    @staticmethod
    def _strip_accept_edits_from_ui_export(target: Path, blob: bytes) -> None:
        """写入导入的 ui.json 时去掉 accept_edits（其余偏好照常恢复）。"""
        try:
            data = json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            return  # 内容不是合法 JSON：保留原行为，不写入半个文件
        if data.pop("accept_edits", None) is not None:
            logger.info("导入的 ui.json 里带着 accept_edits，已丢弃（该开关只能在本机切换）")
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    async def snapshot(self, local: bool = True) -> dict:
        """首屏快照。local=False（局域网 / Tailscale 远端）时打码本机信息。

        安全审查 M7：旧实现把本机绝对路径（working_dir / skill_dirs / mcp_config）
        与服务 endpoint（providers[*].base_url）无条件发给任何连上来的客户端，
        与 project.list 对远程打码 root_path 的口径自相矛盾——同一份信息从
        另一个方法就能拿全，打码等于没打。这里统一口径：远程拿到的是空串，
        「能不能用」的信息（model / has_key / connected / 工具与技能清单）照给，
        远程控制要显示当前服务与可用能力；「装在哪、连的哪个私有中转」不给。
        """
        def _p(v) -> str:
            """路径字段：本机原样，远程空串（与 project.list 的 root_path 同口径）。"""
            return str(v or "") if local else ""

        sessions = (
            await self.store.list_sessions(self.project.id) if self.project is not None
            else await self.store.list_quick_sessions()
        )
        project_mcp = self._mcp_project_path()
        # 启动恢复：优先把活动会话指回上次激活的那张（session_active）。
        # 只做指针校验与切换，不拉历史——历史由前端按 open_tabs 恢复标签时自行
        # session.resume 拉取；记录的 sid 已被删/属于其他项目时静默落回现状。
        prefs = self._read_ui_prefs()
        want_active = prefs.get("session_active")
        if isinstance(want_active, str) and want_active \
                and (self.session is None or self.session.id != want_active):
            try:
                await self.activate_session(want_active)
            except Exception:
                pass
        # 上次开着的标签列表：逐个校验归属（别的项目/已删的丢弃），保序去重
        open_tabs: list[dict] = []
        want_tabs = prefs.get("session_tabs")
        if isinstance(want_tabs, list):
            for sid in want_tabs:
                if not isinstance(sid, str) or not sid \
                        or any(t["id"] == sid for t in open_tabs):
                    continue
                try:
                    s = await self._get_owned_session(sid)
                except Exception:
                    continue
                open_tabs.append({"id": s.id, "title": s.title})
                if len(open_tabs) >= self.SESSION_TABS_MAX:
                    break
        return {
            "version": __version__,
            "frozen": self._is_frozen,  # 安装版可应用内一键更新；源码版提示 git pull
            "open_tabs": open_tabs,  # 启动恢复标签列表（空 = 前端走欢迎页路径）
            "working_dir": _p(self.working_dir),
            "project": self.project.name if self.project else "（未选择项目）",
            "project_id": self.project.id if self.project else None,
            "provider": self.provider_name,
            "model": getattr(self.provider, "model", ""),
            "provider_error": self.provider_error,
            "supports_vision": self._supports_vision(),
            "context_limit": self._context_limit(),
            "providers": {
                name: {
                    "kind": pc.kind,
                    "model": pc.model,
                    "base_url": (pc.base_url if local else ""),
                    "has_key": resolve_api_key(name, pc) is not None,
                    "supports_vision": bool(pc.supports_vision),
                }
                for name, pc in self.cfg.providers.items()
            },
            "session": (
                {
                    "id": self.session.id, "title": self.session.title,
                    "summary": getattr(self.session, "summary", ""),
                    "messages": (
                        [_msg_brief(m) for m in await self.store.load_messages(self.session.id)
                         if m.role in ("user", "assistant")]
                        if self.session else []
                    ),
                } if self.session else None
            ),
            # 启动恢复标签列表（归属校验后的 sid+标题，按记录序）。只在至少
            # 记录了一张时下发；空列表让前端走原有的「开一张欢迎页」路径
            "instructions_file": (self.instructions_file if local else ""),
            "sessions": [
                {"id": s.id, "title": s.title, "updated_at": s.updated_at}
                for s in sessions[:30]
            ],
            "skills": [
                {
                    "name": s.name, "description": s.description,
                    "source": s.source, "enabled": s.enabled,
                    "scope": s.scope, "scope_projects": list(s.scope_projects),
                    "applies": self.skills.applies(s.name),
                }
                for s in self.skills.all()
            ],
            "skill_dirs": {
                "global": _p(skysheep_home() / "skills"),
                "project": _p(self.working_dir / ".skysheep" / "skills"
                              if self.working_dir is not None else ""),
                "scope_config": _p(skysheep_home() / "skills-scope.json"),
            },
            "mcp": self._mcp_status_list(self.mcp),
            "mcp_config": {
                "global": _p(self._mcp_global_path()),
                "project": _p(project_mcp),
                "global_exists": self._mcp_global_path().exists(),
                "project_exists": project_mcp is not None and project_mcp.exists(),
                "project_active": self._project_mcp_path_if_trusted() is not None,
            },
            "workspace_trust": self.trust.state(),
            "mcp_presets": presets_public(),
            "mcp_installed": self.mcp_installed_names(),
            "mcp_warnings": self.mcp_warnings,
            # 首启向导：需要 Key 的内置服务预设（供前端渲染选择卡片），
            # signup 是该服务注册/建 Key 的控制台入口（无 Key 用户的第一跳）
            "provider_presets": [
                {"name": name, "base_url": pc.base_url or "", "model": pc.model,
                 "env_key": pc.env_key or "", "kind": pc.kind,
                 "signup": PRESET_SIGNUP_URLS.get(name, "")}
                for name, pc in PRESETS.items() if name != "ollama"
            ],
            # 崩溃哨兵：上次运行没有正常退出（崩溃/强杀）时为 True，
            # 前端据此提示用户导出诊断包（见 server/app.py 的 lifespan）
            "crashed_last_run": bool(getattr(self, "crashed_last_run", False)),
            "subagent": self.subagent_settings(),
            "update": self.update_info,
            "tools": [
                {"name": t.name, "safety": t.safety.value, "description": t.description}
                for t in self._base_agent.registry.all()
            ],
        }


# 安装包体积上限：本项目安装包在百 MB 量级，超出说明下载被换成了别的东西。
# 限流式边下边校，不先全量进内存（否则上限形同虚设）。
MAX_SETUP_BYTES = 300 << 20


def _download_setup(url: str, dest: Path, expected_sha256: str = "") -> None:
    """把安装包下载到 dest（.part 暂存、完成后改名）。同步阻塞，须在线程里跑。

    follow_redirects 必开：browser_download_url 会 302 到 objects.githubusercontent.com。
    trust_env=False 与 web_fetch 同口径：不让环境变量里的代理/netrc 插手更新链。
    """
    import httpx

    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True, trust_env=False) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                digest = hashlib.sha256()
                total = 0
                with open(part, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        total += len(chunk)
                        if total > MAX_SETUP_BYTES:
                            raise RuntimeError(
                                f"安装包超过 {MAX_SETUP_BYTES // (1 << 20)}MB 上限，已中止下载")
                        digest.update(chunk)
                        f.write(chunk)
        # 校验是 PE 可执行文件（MZ 头）再落正式名：防中途断流留下半截文件被误装
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("下载的内容不是 Windows 安装包（头部校验失败）")
        # 发布方附带了 .sha256 就按它校验；没附带时上面两道（MZ 头 + 体积上限）兜底
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise RuntimeError("安装包校验和不匹配，已丢弃（下载可能被篡改）")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _fetch_setup_sha256(url: str) -> str:
    """取同名 `.sha256` 附件；没有就返回空串（不阻断更新）。

    本项目的更新检查走 releases/latest 页面跳转而不走 api.github.com（后者
    匿名限流 60 次/小时/IP，国内共享出口很容易被耗尽），所以拿不到附件清单，
    只能按发布约定试探同目录下的 .sha256。因此这项是「有则必校」：发布方
    上传了校验和就能防住篡改，没上传就退回 MZ 头 + 体积上限两道。
    """
    import httpx

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, trust_env=False) as client:
            resp = client.get(url + ".sha256")
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    # 兼容 `<hex>` 与 `<hex>  <filename>` 两种常见写法
    m = re.fullmatch(r"([0-9a-fA-F]{64}).*", resp.text.strip(), re.S)
    return m.group(1).lower() if m else ""


def _update_dir() -> Path:
    """为本轮更新开一个随机名的私有临时目录，并顺手清掉旧目录。

    不能落到固定可预测的 `%TEMP%\\SkySheep-<版本>-setup.exe`：那个路径同机同
    用户的任何进程都能提前占位（写一个恶意 exe 在那里），下载层只会看到
    「文件已存在」而照装不误。随机目录名使抢置需要先猜中 128 位随机串。
    旧目录按修改时间清（>24h），避免每轮更新都往 %TEMP% 里堆几十 MB。
    """
    tmp = Path(tempfile.gettempdir())
    for old in tmp.glob("skysheep-update-*"):
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="skysheep-update-"))


_SETUP_APPID = "{7C1B6E9A-52C4-4B7D-9A34-A1B2C3D4E5F6}"  # 与 tools/installer.iss 的 AppId 保持一致


def _setup_privilege_override() -> str:
    """按现有安装登记的侧别决定要不要给安装器传权限覆盖。

    安装包默认要管理员权限（PrivilegesRequired=admin），标准用户下静默安装
    会卡在 UAC 等确认——程序内更新的窗口期用户往往没注意到弹窗，表现成
    「更新了但版本没变」。装在用户目录（登记在 HKCU）的安装完全不需要提权：
    传 /CURRENTUSER 即可静默直装；登记在 HKLM（装时选了「为所有用户安装」）
    的必须提权，维持默认行为；查询失败或无登记也不传，不改变现状。
    """
    try:
        import winreg
    except ImportError:  # 非 Windows：apply_update 走不到这里
        return ""
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{_SETUP_APPID}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
            return "/CURRENTUSER"
    except OSError:
        return ""


def _prepare_update_log(pending: str) -> Path | None:
    """在用户日志目录准备一份更新安装日志（固定名，每次覆盖只留最近一次）。

    安装器从此不再是黑箱：/LOG 让 Inno 记录自己的安装过程，本函数先写入一行
    头部（何时、装哪个包、走哪条权限路径），安装结束后 cmd 侧再追加退出码。
    目录/文件建不出来就返回 None，安装命令里相应省略日志参数——日志失败
    不阻断更新。
    """
    log_path = Path.home() / ".skysheep" / "logs" / "update-setup.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        side = "HKCU，无需 UAC" if _setup_privilege_override() else "需管理员授权，可能弹 UAC"
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始静默安装更新：{pending}"
                    f"（权限：{side}）\n")
    except OSError:
        return None
    return log_path


def _write_update_helper(pending: str, log_path: Path | None, override: str = "") -> Path | None:
    """把「延迟等应用退出 → 静默安装 → 记录退出码」写成 apply-update.cmd。

    以前是把命令串直接交给 `cmd /c`（Popen 传列表）：Python 会把串里的引号转义成
    `\\"`，而 cmd.exe 不认反斜杠转义——安装包路径与 `/LOG=`、退出码重定向整段被
    解析坏（"文件名、目录名或卷标语法不正确"），安装器从未真正启动，日志里也
    只剩一行头部，表现为「点了更新、应用退出后再没下文」。改成先落一个批处理、
    再 `cmd /c apply-update.cmd` 执行：批处理里没有跨层转义，引号按 cmd 的规则
    原样生效，路径含空格/中文也安全。

    延迟用 ping 而不是 timeout：更新以 DETACHED_PROCESS 拉起 cmd，进程没有
    控制台，timeout 的输入重定向检查会立即报错返回，2 秒等待名存实亡——届时
    本应用还没退出、单实例互斥体未释放，安装器会当作已有实例在跑而放弃。
    ping 没有控制台依赖，-n 3 恰好约 2 秒。/RESTARTAPP 让安装器装完自动拉起
    新版（installer.iss 的 [Run] 按此参数决定，手动静默安装不受影响）。

    批处理用 GBK 写（cmd 按 OEM 代码页读 .cmd，中文 Windows 即 cp936），只有
    提示文案受影响，命令本身是 ASCII。写不出文件返回 None（调用方保持原行为）。
    """
    script = Path(pending).parent / "apply-update.cmd"
    args = "/SILENT /CLOSEAPPLICATIONS /RESTARTAPP"
    if override:
        args += f" {override}"
    if log_path is not None:
        args += f' /LOG="{log_path}"'
    lines = [
        "@echo off",
        "rem SkySheep 程序内更新助手（由应用生成；安装完成后可整个目录删除）",
        "ping -n 3 127.0.0.1 >nul",
    ]
    if log_path is not None:
        lines += [
            f'if not exist "{pending}" (',
            f'  >>"{log_path}" echo [更新失败] 安装包不存在：{pending}',
            "  exit /b 1",
            ")",
            f'>>"{log_path}" echo [%DATE% %TIME%] 开始运行安装包',
        ]
    lines.append(f'call "{pending}" {args}')
    if log_path is not None:
        lines.append(f'>>"{log_path}" echo [setup 退出码: %errorlevel%]')
    try:
        script.write_text("\r\n".join(lines) + "\r\n", encoding="gbk", errors="replace")
    except OSError:
        return None
    return script
