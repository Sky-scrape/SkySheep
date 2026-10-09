"""ServerBackend：把引擎接线（会话/模型/技能/MCP/子代理/权限）暴露给服务层。

与 CLI 的 ChatApp 共享同一套引擎 API，但不含任何 UI 逻辑——
FastAPI WebSocket 端点消费它并转发事件流。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess  # noqa: F401  测试按模块属性 patch Popen
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

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
    set_provider_models_in_config,
    set_subagent_settings_in_config,
    skysheep_home,
    update_config_section,
    update_provider_in_config,
)
from ..core import Agent, build_system_prompt
from ..core.adversarial import (
    AdversarialOutcome,
    run_adversarial,
)
from ..core.adversarial import (
    findings_meta as adversarial_findings_meta,
)
from ..core.adversarial import (
    stats as adversarial_stats,
)
from ..core.adversarial import (
    usage_rows as adversarial_usage_rows,
)
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
from ..core.team import (
    ChannelMessage,
    DirectorSpec,
    TeamError,
    TeamMemberSpec,
    TeamMessageSink,
    TeamOrchestrator,
    TeamTemplate,
    TeamTemplateDirector,
    TeamTemplateMember,
    TeamTemplateStore,
)
from ..events import (
    AssistantMessage,
    ErrorEvent,
    NoticeEvent,
    PermissionResolved,
    QueueUpdated,
    TaskEstimate,
    TurnFinished,
    TurnStarted,
)
from ..mcp import (
    MCPManager,
    load_mcp_configs,
    presets_public,
)
from ..messages import ImageBlock, Message, TextBlock
from ..messages import system_text as history_system_text
from ..models import Provider
from ..models.factory import build_provider
from ..models.probe import _is_local, probe_context_limit, probe_provider_models
from ..obs import info as obs_info
from ..obs import parse_structured
from ..obs import warning as obs_warning
from ..security import leases
from ..security.gate import _RUN_COMMAND, RULE_KINDS, PermissionGate, _is_arbitrary_exec_prefix
from ..security.trust import STATE_PENDING, WorkspaceTrust, list_trusted, revoke_by_path
from ..session import SessionStore
from ..session.store import export_messages_text
from ..skills import SkillLoader
from ..skills.draft import build_skill_draft, save_skill_draft
from ..skills.gallery import bundled_dir_for, bundled_update_available, load_gallery_manifest
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
    render_memory_section,
)
from ..tools.memory_embed import schedule_warmup
from ..tools.pipeline import PipelineWriteTool
from ..tools.skill import LoadSkillTool
from .backend_parts._shared import EmitFn, ProjectCtx, SessionRuntime, collect_stream_text
from .backend_parts.automation import AutomationMixin
from .backend_parts.channels import (
    ChannelsMixin,
    _channel_allowed_tools_warning,  # noqa: F401  测试从本模块引用
)
from .backend_parts.data_retention import RetentionMixin
from .backend_parts.lifecycle import LifecycleMixin
from .backend_parts.mcp import McpMixin
from .backend_parts.memory import MemoryMixin
from .backend_parts.mods import ModsMixin
from .backend_parts.preferences import PreferencesMixin
from .backend_parts.remote import (  # noqa: F401  部分名字供 app.py / 测试从本模块引用
    _SETUP_APPID,
    MAX_SETUP_BYTES,
    THEME_AUTO_DARK,
    THEME_AUTO_LIGHT,
    THEME_PREFS,
    RemoteMixin,
    TokenThrottle,
    _download_setup,
    _fetch_setup_sha256,
    _prepare_update_log,
    _setup_privilege_override,
    _update_dir,
    _write_update_helper,
    client_origin,
)
from .backend_parts.terminal import TerminalManager, TerminalPanelMixin

logger = logging.getLogger("skysheep.security")
memory_log = logging.getLogger("skysheep.memory")

# 流式增量事件的合并窗口（秒）与单条合并上限（字符）。
#
# 上游 delta 的粒度由各家 provider 决定，常见是每个 SSE chunk 一个 token 级增量，
# 一轮长回答就是上千条事件——每条都要过 WS 的发送锁、单独 JSON 序列化一帧。
# 终端输出已经做过同类合并（见 TerminalManager.TERM_MERGE_S），对话流此前没有。
# 这里把连续的同类型增量拼成一条：首条立即发出（不动首字延迟），之后按窗口聚批。
STREAM_MERGE_S = 0.04
STREAM_MERGE_MAX_CHARS = 2_000

# 可不经合并直接发出的高频增量事件类型（其余事件一律先冲刷缓冲，保证顺序）。
_MERGEABLE_DELTA_KINDS = (
    "text_delta", "thinking_delta", "roundtable_member_delta", "team_message_delta",
)

# 二次取消后收割后台落库的兜底超时（秒）：正常落库毫秒级完成，超时说明库被
# 锁死等异常，不能拖着整个收尾无限等（排队轮还等着交棒）。
_PERSIST_HARVEST_TIMEOUT = 10.0


class StreamDeltaMerger:
    """把连续的流式增量拼成批量事件，减少 WS 帧数。

    设计要点：
    - **顺序不变**：任何非增量事件（工具调用、权限、用量、轮末…）到达时先冲刷全部
      缓冲；同一 key 的增量只在各自缓冲里按到达序合并，不跨界合并。
    - **不增加首字延迟**：某类缓冲为空时首条增量立即发出，之后才进入窗口聚批。
      首条发出后记下已发长度（``sent``），flush 只补发之后累积的部分。
    - **收尾必冲刷**：``flush()`` 必须在轮末、异常、取消三条路径上都调到，
      否则最后几十毫秒的增量会丢在前端。
    - **按 key 分桶**：普通对话只有 (kind,) 一个桶，行为与旧单缓冲一致；
      圆桌并行成员（member_index/round 各成一桶）交错流式时，A 成员的增量
      不再把 B 的缓冲整个冲掉——否则并行度放大帧率的场景里合并完全失效。
    """

    def __init__(self, emit: EmitFn, *, window_s: float = STREAM_MERGE_S) -> None:
        self._emit = emit
        self._window_s = window_s
        # key -> 缓冲桶 {ev, text, sent, last_emit}；dict 保序，flush 按首到序冲刷
        self._buckets: dict[tuple, dict] = {}
        self._timer: asyncio.Task | None = None

    @staticmethod
    def _delta_key(ev: dict) -> tuple | None:
        kind = ev.get("kind")
        if kind not in _MERGEABLE_DELTA_KINDS:
            return None
        if kind == "roundtable_member_delta":
            return (kind, ev.get("member_index"), ev.get("round"))
        if kind == "team_message_delta":
            # 团队成员发言增量：member_index + seq 双键分桶——同一成员跨消息的
            # 增量绝不互并（不同频道消息并成一帧会串消息），同一条消息内的
            # 连续增量照常聚批
            return (kind, ev.get("member_index"), ev.get("seq"))
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

    @staticmethod
    def _bucket_tail(bucket: dict) -> str:
        full = bucket.get("text", "") or ""
        return full[bucket["sent"]:]

    async def _flush_bucket(self, bucket: dict) -> None:
        tail = self._bucket_tail(bucket)
        bucket["sent"] = len(bucket.get("text", "") or "")
        if tail:
            await self._emit({**bucket["ev"], "text": tail})

    async def send(self, ev: dict) -> None:
        key = self._delta_key(ev)
        if key is None:
            # 非增量事件：先冲刷全部缓冲，再原样发出（工具/权限/轮末事件绝不能等）
            await self.flush()
            await self._emit(ev)
            return

        now = time.monotonic()
        text = ev.get("text", "") or ""
        bucket = self._buckets.get(key)
        can_merge = (
            bucket is not None
            and now - bucket["last_emit"] < self._window_s
            and len(bucket["text"]) < STREAM_MERGE_MAX_CHARS
        )
        if can_merge:
            # 窗口内且同类型：只累积（这里无 await，与定时器不会交错）
            bucket["text"] = bucket["text"] + text
            return

        # 新一段增量的首条（或超过窗口 / 超过上限）：先补发该 key 旧缓冲的尾巴
        # （其他 key 的桶不受影响——并行成员各自聚批），再立即发这条。
        # await 前先摘桶：补尾的 emit 会挂起，旧桶若还挂在 _buckets 里，并发
        # send() 的同 key 增量会合进这个已标记 sent 的桶、随后随替换整段丢失；
        # 摘掉后并发增量必走「新段首条立即发」路径，一字不丢（同步合并路径
        # 上面已 return，不经过这里，桶序不受影响）。
        if bucket is not None:
            del self._buckets[key]
            await self._flush_bucket(bucket)
        first = dict(ev)
        first["text"] = text
        self._buckets[key] = {
            "ev": first, "text": text, "sent": len(text), "last_emit": now,
        }
        await self._emit(first)  # 首条立即发：首字延迟与合并前一致
        self._schedule_flush()  # 预约冲刷，避免尾巴被无限期揨住

    async def flush(self) -> None:
        """补发所有缓冲里尚未发出的增量；无待发内容时是空操作。"""
        timer = self._timer
        if timer is not None:
            self._timer = None
            # 不要取消自己（定时器回调也走 flush）
            if timer is not asyncio.current_task():
                timer.cancel()
        # 边冲边摘：冲刷要 await emit，若桶留在 _buckets 里（快照循环 + 末尾统一
        # clear），挂起期间并发 send() 的同 key 增量仍在合并窗口内、会合进已标记
        # sent 的桶，随后随 clear() 整段丢失且无定时器兜底；先摘掉再冲，并发增量
        # 看不到旧桶、必走「新段首条立即发」路径，恢复旧单缓冲「await 前先摘除」
        # 的不变式。
        # pop 带默认值容忍嵌套 flush：圆桌并行成员的「结束」事件在本冲刷挂起期间
        # 到达时，会经 send() 的非增量路径触发嵌套 flush 把某个桶先冲掉——外层
        # 快照里该 key 的桶已不在（内容已由嵌套冲刷发出，不丢不重），跳过即可。
        # 分桶改造后在 Python 3.11 的 CI 上首次暴露（3.12 的任务调度恰好错开）。
        for key in list(self._buckets.keys()):
            bucket = self._buckets.pop(key, None)
            if bucket is not None:
                await self._flush_bucket(bucket)

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



def _decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _unified_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="改前", tofile="改后", lineterm=""
        )
    )


def _checkpoint_diff_files(cp: dict) -> list[dict]:
    """检查点「审查」的逐文件对比（同步函数，调用方用 to_thread 离开事件循环）。

    逐文件读磁盘现状 + difflib 差异计算都是重量级同步活，大检查点回滚/审查
    直接跑在事件循环上会把整个引擎（所有会话的流式输出）卡住数秒。
    """
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
    return files


# ---- 轮次诊断（审查页）：从桌面日志的结构化行里取回每轮耗时拆解 ----

# diagnostics.turn_breakdown 的 limit 缺省与上限（上限挡住误传的大数，
# 避免一次拖回整本日志的历史轮次）
TURN_LOG_LIMIT_DEFAULT = 20
TURN_LOG_LIMIT_MAX = 200

# 桌面日志行首的时间戳（desktop.py _setup_logging 的 %(asctime)s 默认格式：
# 「2026-09-21 19:30:01,123」本地时间）。ev=turn 的 JSON 里没有时间字段，
# 时间只能从行首取。
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:,(\d{3}))?")


def _log_line_ts(line: str) -> float | None:
    """行首 asctime → epoch 秒（本地时区，与写入侧同一口径）；解析不了返回 None。"""
    m = _LOG_TS_RE.match(line)
    if m is None:
        return None
    try:
        base = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return base.timestamp() + (int(m.group(3)) / 1000 if m.group(3) else 0)


def _turn_int(fields: dict, key: str) -> int | None:
    """结构化字段 → int；缺失或类型不对返回 None（旧日志行/手改日志都容忍）。"""
    v = fields.get(key)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _turn_row(line: str, fields: dict) -> dict:
    """一条 ev=turn 日志行 → 前端一行的诊断数据。字段缺失一律 None。"""
    stopped = fields.get("stopped")
    return {
        "ts": _log_line_ts(line),
        "duration_ms": _turn_int(fields, "duration_ms"),
        "tool_calls": _turn_int(fields, "tool_calls"),
        "tool_ms": _turn_int(fields, "tool_ms"),
        "tool_errors": _turn_int(fields, "tool_errors"),
        "slowest_tool": str(fields["slowest_tool"]) if fields.get("slowest_tool") else None,
        "slowest_tool_ms": _turn_int(fields, "slowest_tool_ms"),
        "permission_waits": _turn_int(fields, "permission_waits"),
        "permission_ms": _turn_int(fields, "permission_ms"),
        "in_tokens": _turn_int(fields, "in_tokens"),
        "out_tokens": _turn_int(fields, "out_tokens"),
        "stopped": bool(stopped) if stopped is not None else None,
        "error": str(fields["error"]) if fields.get("error") else None,
    }


def _load_turn_rows(log_path: Path, sid: str) -> list[dict]:
    """读日志文件 → 过滤出目标会话的 ev=turn 行（同步函数，调用方 to_thread）。

    只读当前 desktop.log，不追轮转旧文件（desktop.log.*）；文本走 textio
    读（编码安全），解析不了的行直接跳过。
    """
    try:
        text = read_text_file(log_path).text
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        fields = parse_structured(line)
        if not fields or fields.get("ev") != "turn":
            continue
        if fields.get("session_id") != sid:
            continue
        rows.append(_turn_row(line, fields))
    return rows


@dataclass
class QueuedTurn:
    """Agent 工作期间用户继续发来的消息：本轮结束后自动依次执行。"""

    text: str
    emit: EmitFn
    plan_mode: bool
    fut: asyncio.Future = field(repr=False)
    roundtable: bool = False
    members: list | None = None  # 圆桌成员 [{provider, model}]，None=默认策略
    adversarial: bool = False  # 对抗轮：四角色流水线审查（发现→调查→建议→裁判）
    images: list[ImageBlock] = field(default_factory=list)  # 本轮图片附件（圆桌轮不发给成员，仅随消息保留）
    refs: list[str] = field(default_factory=list)  # 本轮引用的会话 id（& 引用对话）
    compare: bool = False  # 圆桌 A/B 对比模式（不融合）
    debate_rounds: int | None = None  # 本轮辩论修订轮数（None=用配置值）
    chair_answers: bool | None = None  # 本轮主席是否出草稿（None=用配置值）
    team: bool = False  # 团队轮（用户总管）：消息进团队频道，唤醒被点名成员
    # AI 总管（二期）：建队轮生效——director_mode="ai" 时 director 指定担任
    # 总管的 {provider, model}；仅建队路径消费，后续轮忽略
    director_mode: str = "user"
    director: dict | None = None

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






def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 团队轮 assistant 气泡里成员报告摘录的长度（全文在频道卡片与 meta 的 messages 里）
TEAM_DIGEST_EXCERPT_CHARS = 300


def _team_digest(orch: TeamOrchestrator, result: dict) -> str:
    """团队轮的 assistant 气泡文本：本轮被唤醒成员的纪要（+ 终态说明）。

    成员发言的正文由 TeamMessage 事件直播、随 meta 的 messages 回放；气泡只
    承载「这轮谁应答了、结果如何」的纪要，终态时附《交付说明》全文。AI 总管
    模式下总管发言同样直播进频道，气泡按推进情况给一句话纪要。
    """
    lines: list[str] = []
    for w in result.get("woke") or []:
        name, status = str(w.get("member", "")), str(w.get("status", ""))
        seq = int(w.get("seq") or 0)
        if status == "done" and seq:
            report = next((m.text for m in orch.channel.messages if m.seq == seq), "")
            excerpt = report[:TEAM_DIGEST_EXCERPT_CHARS]
            if len(report) > TEAM_DIGEST_EXCERPT_CHARS:
                excerpt += f"…（全文见频道 #{seq}）"
            lines.append(f"【{name}】{excerpt}")
        elif status == "error":
            lines.append(f"【{name}】本轮失败（{w.get('error') or '未知原因'}）")
        elif status == "cancelled":
            lines.append(f"【{name}】已停止（已产出的部分已定稿进频道）")
        elif status == "skipped":
            # 轮次耗尽/预算越线在前一名成员轮上触发收队后，剩余点名不再唤醒
            lines.append(f"【{name}】未唤醒（{w.get('error') or '团队已进入终态'}）")
    if not lines:
        if orch.director_mode == "ai":
            # inject_user_message 的返回没有 woke 键（区别于自动循环的空唤醒）
            injected = "woke" not in result
            lines.append(
                "（插话已进入团队频道：总管推进中，将在下一轮优先处理。）" if injected
                else "（总管已处理本轮消息：过程与分工见团队频道。）"
            )
        else:
            lines.append("（消息已进入团队频道；未被点名的队员会在下次被唤醒时看到。）")
    finished = result.get("finished")
    if finished:
        lines.append(str(finished.get("summary") or ""))
    return "\n\n".join(line for line in lines if line)


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




class ServerBackend(AutomationMixin, ChannelsMixin, MemoryMixin,
                   TerminalPanelMixin, RemoteMixin,
                   LifecycleMixin, McpMixin, ModsMixin, PreferencesMixin,
                   RetentionMixin):
    def __init__(
        self,
        working_dir: str | Path = ".",
        provider_name: str | None = None,
        provider_factory: Callable[[], Provider] | None = None,
        store: SessionStore | None = None,
        team_state_path: Path | None = None,
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
        # 各会话在飞的后台落库任务（sid → persist_task）：二次取消后主轮任务
        # 可能已结束而落库还在独立任务里跑，delete_session 等的必须是它本身
        # （等主轮任务等不到它），否则消息插在删除之后留下孤儿行
        self._persist_tasks: dict[str, asyncio.Task] = {}
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
        # 团队模板（三期）：~/.skysheep/teams.json，与 subagents.json 同姿态
        # （路径随 SKYSHEEP_HOME，测试隔离；坏文件容错在 store 内部）
        self.team_templates = TeamTemplateStore(skysheep_home() / "teams.json")
        self.instructions_file: str | None = None
        self.instructions_text: str = ""
        self.mcp_warnings: list[str] = []
        self.checkpoints = CheckpointStore()
        self.hooks: HookRunner | None = None
        # 跨项目分屏列的私有项目上下文缓存（_own_ctx 构建）：每个目标项目一份
        # 子代理管理器与检查点库，按 normcase 工作目录为键；进程内常驻，数量
        # 以用户添加的项目数为上界。引擎当前项目的仍走 self.tasks/self.checkpoints。
        self._project_tasks: dict[str, TaskManager] = {}
        self._project_checkpoints: dict[str, CheckpointStore] = {}
        # Mods 扩展（实验性，core/mods.py）：setup/_bind_project 里经 _reload_mods
        # 重建并挂到 Agent 与权限门上；无已装 Mod 时为 None（零开销直通）
        self.mods = None
        self._pending_mod_install: dict | None = None  # 两段安装的待确认项
        self.term = TerminalManager()
        self.aux_history: list[Message] = []
        # 会话级模型覆盖（分屏独立对话）：sid → (服务名, 模型名)。有记录的会话
        # runtime 用覆盖模型（_runtime_provider 首建时、session.model_switch 热切），
        # 未记录的跟随全局；session.model_switch 写、delete_session 清
        self._session_models: dict[str, tuple[str, str]] = {}
        # 会话级权限三档覆盖（分屏/主栏按会话独立调档）：sid → 0/1/2。
        # 引擎档（self.gate + ui.json 的 accept_edits）只是新会话的默认值；
        # 有覆盖的会话 runtime 用自己的门（_runtime_gate 私有实例），重建重放。
        self._session_accepts: dict[str, int] = {}
        # 会话级思考强度档位：sid → effort（内存态，沿 _session_models 先例）。
        # 有记录的会话 runtime 挂独立 provider 并带该档位（session.reasoning_set
        # 写、热切模型后重放或清除、runtime 重建时恢复），未记录的跟随服务配置
        self._session_efforts: dict[str, str] = {}
        # 团队（用户总管 MVP）：会话级活动团队（sid → 编排器，内存态，不跨进程）；
        # 活动登记落 team_active.json（沿子代理任务簿先例：state_path 可注入、
        # textio 原子写、重启时一律标「已中断」不自动恢复）。
        # 默认指向 SKYSHEEP_HOME（测试经 home 夹具隔离），也可显式注入临时路径。
        if team_state_path is not None:
            self._team_state_path: Path | None = team_state_path
        else:
            self._team_state_path = skysheep_home() / "team_active.json"
        self._teams: dict[str, TeamOrchestrator] = {}
        self._teams_interrupted: dict[str, dict] = {}
        self._load_team_state()
        self.ws_emitters: list = []  # 在线 WS 连接的 emit 函数（提醒/日程广播用）
        self._reminder_task: asyncio.Task | None = None
        self._cron_task: asyncio.Task | None = None
        self._cron_running: set[int] = set()  # 正在跑的定时任务 id（防重复触发）
        # schtasks 执行入口的注入点（测试替换成假 runner，绝不真建系统计划任务）
        self._schtasks_runner = None
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_running: set[int] = set()  # 正在跑的编排节点 id（并发约束）
        self._pipeline_node_tasks: dict[int, asyncio.Task] = {}  # 节点句柄（停止流水线用）
        # 会话续跑节点遇忙的退避表（node_id → 最早可重试时刻）：不占 DB，重启即清零，
        # 清零后重试也不伤——busy 预检还在，会再次退避。
        self._pipeline_busy_until: dict[int, float] = {}
        self._titling: set[str] = set()  # 正在自动生成标题的会话
        self._manually_named: set[str] = set()  # 用户手改过名字的会话（自动标题让路）
        # 适用「新会话默认模型」的会话 → 创建时刻的 (服务, 模型) 快照：
        # 只在创建时偏好已生效才记，三段解析与 runtime 实际挂的 provider 都按
        # 同一份快照回读（读时再验偏好会把「先建会话、后设偏好」的会话追溯成
        # 默认模型会话——徽章显示偏好模型、runtime 却还挂着全局 provider）
        self._default_model_sessions: dict[str, tuple[str, str]] = {}
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

    # 交互轮的全局并发上限：跨会话并行不受约束时，一个客户端可以同时拉起
    # 几十个会话的轮次空烧 token（每日预算护栏只限费用不限并发）。定时任务有
    # _cron_running 去重、流水线有 PIPELINE_GLOBAL_CAP，交互轮此前没有闸。
    MAX_INTERACTIVE_TURNS = 4

    def _running_turn_count(self) -> int:
        """当前真正在跑的交互轮数（含会话懒创建窗口占住基底位的那一轮）。

        定时任务/流水线的轮用本地 runtime（不进 self.runtimes、不占运行位），
        天然不计入；done 的任务也不计（运行位在轮末释放）。
        """
        n = 0
        for rt in self.runtimes.values():
            t = rt.run_task
            if t is not None and not t.done():
                n += 1
        b = self._base_run_task
        if b is not None and not b.done():
            n += 1
        return n

    def _build_agent(
        self,
        *,
        provider: Provider,
        gate: PermissionGate,
        working_dir: Path | None,
        session_id: str = "",
        mods=None,
        recorder: ChangeRecorder | None = None,
        ctx: ProjectCtx | None = None,
    ) -> Agent:
        """Agent 构造的单一入口：基底/会话/定时任务/流水线/渠道五处共用。

        配置 kwarg（迭代上限、上下文上限、压缩三件套、钩子、工作区限制）
        五处构造点完全一致，收敛在这里——加一个 Agent 配置项只改这一处；
        provider/gate/working_dir 各不相同由调用方传。mods 只挂桌面侧的
        基底与会话 Agent（无人值守/渠道派生不传，天然不挂，见 core/agent.py）；
        session_id 透传给钩子命令的 stdin JSON。ctx 传入时会话 runtime 的
        工具注册表按该项目上下文装配（子代理/流水线绑定会话所属项目）。
        """
        return Agent(
            provider=provider,
            registry=self._build_full_registry(recorder, ctx=ctx),
            gate=gate,
            working_dir=working_dir,
            max_iterations=self.cfg.max_iterations,
            context_limit_tokens=self._context_limit(),
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            compaction_trigger=self.cfg.compaction_trigger,
            compaction_auto=self.cfg.compaction_auto,
            hooks=self.hooks,
            mods=mods,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
            session_id=session_id,
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
        )

    # ---- 会话的项目上下文（跨项目分屏列的 per-runtime 绑定） ----

    async def _runtime_gate(self, project_id: int | None,
                            workdir: Path | None, session_id: str) -> PermissionGate:
        """会话 runtime 的私有权限门：白名单按项目、工作目录边界跟着走，
        三档先取引擎默认（ui.json 的 accept_edits），再叠会话级覆盖
        （session.accept_set 写入 _session_accepts，runtime 重建后在此重放）。

        不再与引擎共享同一实例——三档按会话独立后，共享实例会把一个会话的
        调档泄漏给所有会话（与思考档位「换私有副本」同一道理）。"""
        gate = PermissionGate(store=self.store, project_id=project_id, working_dir=workdir)
        gate.auto_accept_write = self.gate.auto_accept_write if self.gate else False
        gate.auto_accept_all = self.gate.auto_accept_all if self.gate else False
        if self.mods is not None:
            gate.extra_confirm = self.mods.extra_confirm
        await gate.load_project_rules()
        saved = self._session_accepts.get(session_id)
        if saved in (0, 1, 2):
            gate.auto_accept_write = saved in (1, 2)
            gate.auto_accept_all = saved == 2
        return gate

    def _fixed_workdir(self, name: str) -> Path:
        """快聊 / 远程连接的固定工作目录：数据目录（skysheep_home()）下的真实文件夹。

        取用时 ensure 一次（exist_ok 幂等）：启动建过之后用户中途删掉也能自愈；
        建不出来（只读介质等）不抛错——路径照常返回，会话仍可用，文件工具
        真正读写时才报错。
        """
        d = skysheep_home() / name
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d

    def _quick_workdir(self) -> Path:
        """快聊会话的工作目录：数据目录下的 default 文件夹。"""
        return self._fixed_workdir("default")

    def _remote_workdir(self) -> Path:
        """远程连接（渠道）会话的工作目录：数据目录下的 remote-control 文件夹。"""
        return self._fixed_workdir("remote-control")

    async def _session_project(self, session_id: str) -> tuple[int | None, Path | None]:
        """会话所属项目 (project_id, 工作目录)。

        工作目录解析口径：快聊（无项目）→ 数据目录 default 文件夹；远程连接
        项目 → 数据目录 remote-control 文件夹（两类会话都有真实、稳定的工作
        目录，不再借用引擎当前目录）；项目记录缺失 → 回退引擎当前工作目录；
        目录已消失 → 可读报错而不是静默换目录干活——那等于让对话在错误的
        项目里改文件。
        """
        sess = await self.store.get_session(session_id)
        if sess is None:
            return None, self.working_dir
        if sess.project_id is None:
            # 快聊：数据目录 default 文件夹（真实、稳定的工作目录）
            return None, self._quick_workdir()
        proj = await self.store.get_project(sess.project_id)
        if proj is None or not proj.root_path:
            return sess.project_id, self.working_dir
        if proj.root_path == self.store.REMOTE_PROJECT_PATH:
            # 远程连接（渠道）会话：数据目录 remote-control 文件夹
            return sess.project_id, self._remote_workdir()
        p = Path(proj.root_path)
        if not p.is_dir():
            raise RuntimeError(
                f"该会话所属项目的目录已不存在（{proj.root_path}），无法在这里继续对话"
            )
        return sess.project_id, p

    def _tasks_for_workdir(self, workdir: Path) -> TaskManager:
        """目标项目的子代理管理器（懒建缓存）：工作目录与停启都独立于引擎
        当前项目——跨项目列派生的子代理必须在会话所属项目里干活。"""
        key = os.path.normcase(str(workdir))
        tm = self._project_tasks.get(key)
        if tm is None:
            tm = TaskManager(
                provider_factory=lambda: self.provider,
                working_dir=workdir,
                max_iterations=self.cfg.subagent_max_iterations,
                store=self.subagent_store,
                provider_resolver=self._subagent_provider,
                registry_resolver=self._subagent_registry,
                max_concurrent=self.cfg.subagent_max_concurrent,
                usage_recorder=self._record_subagent_usage,
                event_emitter=self._ws_broadcast,
                state_path=skysheep_home() / "subagent_tasks.json",
                job_containment=self.cfg.shell.job_containment,
                sandbox_level=self.cfg.shell.sandbox_level,
                use_ripgrep=self.cfg.search.use_ripgrep,
            )
            self._project_tasks[key] = tm
        return tm

    def _checkpoint_store_for_root(self, workdir: Path) -> CheckpointStore:
        """目标项目的检查点库（按工作目录指纹隔离，进程内缓存单例）。"""
        key = os.path.normcase(str(workdir))
        store = self._project_checkpoints.get(key)
        if store is None:
            store = CheckpointStore(root=self._checkpoint_root_for(workdir))
            self._project_checkpoints[key] = store
        return store

    def _hooks_for_workdir(self, workdir: Path) -> HookRunner | None:
        """目标项目的钩子（同一份配置、换工作目录执行，与 _bind_project 同款）。"""
        raw_cfg = load_raw_config()
        pre_rules, post_rules, stop_rules = hooks_from_config(raw_cfg)
        if not (pre_rules or post_rules or stop_rules):
            return None
        return HookRunner(pre_rules, post_rules, working_dir=workdir,
                          stop_rules=stop_rules)

    async def _own_ctx(self, project_id: int | None, workdir: Path,
                       session_id: str) -> ProjectCtx:
        """为「会话所属项目 ≠ 引擎当前项目」的会话构建私有项目上下文。

        权限门按该项目建（白名单按项目隔离、工作目录边界跟着走，与定时任务
        的 CronGate 同口径）；界面级的安全档位偏好跨项目保持（_bind_project
        同一款）。技能与 MCP 仍是引擎级单例（compose_system_for 的既有取舍，
        已知限制：跨项目列用的是当前装载的技能段与当前项目的 MCP 服务）。
        """
        return ProjectCtx(
            workdir=workdir,
            project_id=project_id,
            gate=await self._runtime_gate(project_id, workdir, session_id),
            checkpoints=self._checkpoint_store_for_root(workdir),
            tasks=self._tasks_for_workdir(workdir),
            hooks=self._hooks_for_workdir(workdir),
            own=True,
        )

    async def _project_ctx_for(self, session_id: str) -> ProjectCtx:
        """会话 runtime 应绑定的项目上下文：归属当前项目（或开放归属）走
        引擎单例；真正的跨项目会话建私有上下文——这是「分屏列当完整会话」
        的地基：fs/命令工具经 ctx.working_dir 解析、权限走 ctx.gate。"""
        project_id, workdir = await self._session_project(session_id)
        cur = self.working_dir
        same = (workdir is None and cur is None) or (
            workdir is not None and cur is not None
            and os.path.normcase(str(workdir)) == os.path.normcase(str(cur))
        )
        if same:
            # 归属当前项目/快聊/远程连接：检查点/子代理/钩子仍用引擎实时单例，
            # 权限门也按会话私有（三档按会话独立的地基）
            return ProjectCtx(
                workdir=self.working_dir,
                project_id=self._cur_project_id(),
                gate=await self._runtime_gate(self._cur_project_id(), self.working_dir, session_id),
                checkpoints=self.checkpoints,
                tasks=self.tasks,
                hooks=self.hooks,
                own=False,
            )
        return await self._own_ctx(project_id, workdir, session_id)

    async def _get_runtime(self, session_id: str) -> SessionRuntime:
        """取（或懒建）一个会话的运行时；新 runtime 自带系统提示词与完整工具集。

        LRU 淘汰：访问即移到末尾；超限后从最旧开始找「空闲」的回收
        （见 _evictable_runtime）——只回收空闲的，宁可超限也不丢运行状态。
        runtime 按会话所属项目绑定上下文（_project_ctx_for）：跨项目分屏列
        的会话在自己的项目里干活，不挂在引擎当前项目下。
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            self.runtimes.move_to_end(session_id)
        else:
            ctx = await self._project_ctx_for(session_id)
            recorder = ChangeRecorder()
            rt = SessionRuntime(
                sid=session_id,
                agent=self._build_agent(
                    provider=self._runtime_provider(session_id),
                    gate=ctx.gate,
                    working_dir=ctx.workdir,
                    session_id=session_id,
                    mods=self.mods,
                    recorder=recorder,
                    ctx=ctx,
                ),
                recorder=recorder,
                ctx=ctx,
            )
            rt.agent.set_system(
                self.compose_system_for(ctx.workdir) if ctx.own else self.compose_system()
            )
            # 会话级思考档位恢复：runtime 被淘汰重建后，把档位重新挂回
            # （重建的）provider。恢复前重验能力：新 provider 不支持思考则
            # 跳过并清除该条目（不报错——能力以当前生效服务为准）。
            # 档位是会话私有状态：无覆盖会话重建出的 provider 可能仍是全局
            # 共享对象（_runtime_provider 的全局腿），直接在共享对象上调档会
            # 把所有会话一起改掉——先换 detached 私有副本再回放（与
            # session_reasoning_set 同款处理）。
            saved_effort = self._session_efforts.get(session_id)
            if saved_effort is not None:
                if getattr(rt.agent.provider, "supports_reasoning", False):
                    if rt.agent.provider is self.provider:
                        name, model = self._session_effective_model(session_id)
                        try:
                            rt.agent.provider = self._build_detached_provider(name, model or None)
                        except Exception:  # noqa: BLE001 - 私有化失败宁可弃档位也不能污染全局
                            self._session_efforts.pop(session_id, None)
                            saved_effort = None
                    if saved_effort is not None:
                        rt.agent.provider.set_reasoning_effort(saved_effort)
                else:
                    self._session_efforts.pop(session_id, None)
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
        """会话的起始 provider：会话级覆盖（分屏独立选的模型）优先，其次新会话
        默认模型，最后跟随全局。覆盖模型不可用（服务被删等）时回退全局，
        不让发消息失败。"""
        override = self._session_models.get(session_id)
        if override:
            try:
                return self._build_detached_provider(override[0], override[1] or None)
            except Exception:  # noqa: BLE001
                self._session_models.pop(session_id, None)  # 覆盖失效即清除
        # 「新会话默认模型」腿只看创建时刻的快照（new_session() 记下）：偏好
        # 后来被清除/更改也不影响已存在的会话（set_default_model 的文档语义）
        baked = getattr(self, "_default_model_sessions", {}).get(session_id)
        if not baked:
            return self.provider
        # detached 构建：不改写 provider_name / provider_model（「当前使用服务」
        # 的全局记账）。此前这条腿走 _build_provider，每次新会话预建 runtime 都把
        # 记账改指默认服务、provider 对象却还是旧服务——与 set_default_model
        # 「已存在的会话（含当前对话）不受影响」的文档语义相悖，也让三段解析的
        # 全局腿（self.provider_name）失真。改用 detached 后，全局记账只在
        # 真全局切换（model.switch）时变化。
        try:
            return self._build_detached_provider(baked[0], baked[1] or None)
        except Exception:
            return self.provider  # 预设被删/停用时回退全局，不让发消息失败

    def _session_effective_model(self, session_id: str) -> tuple[str, str]:
        """会话当前生效的 (服务名, 模型名)，三段解析的单处实现：

        1. 会话级模型覆盖（分屏独立选的模型）；
        2. 「新会话默认模型」会话（创建时刻的 (服务, 模型) 快照）；
        3. 全局记账（provider_name / provider_model）。

        回退口径与 _runtime_provider 逐字一致：覆盖指向的服务已不存在即清除
        覆盖并落往下一段；默认模型快照里的服务已不存在同样清除并落往下段。
        全局腿可靠的前提是 _runtime_provider 全程 detached 构建（见上）。"""
        override = self._session_models.get(session_id)
        if override:
            if override[0] in self.cfg.providers:
                return override
            self._session_models.pop(session_id, None)  # 覆盖失效即清除
        # 默认模型腿按创建时刻的快照回读（与 _runtime_provider 同源）；快照里的
        # 服务已不存在（被删/停用）即清除标记、回落全局
        baked = getattr(self, "_default_model_sessions", {}).get(session_id)
        if baked:
            if baked[0] in self.cfg.providers:
                return baked
            self._default_model_sessions.pop(session_id, None)
        return self.provider_name or "", self.provider_model or ""

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

    def _refresh_system_prompts(self) -> None:
        """全局刷新系统提示词（技能/项目说明等变更后）：走 _for_each_agent
        这一既有接缝；跨项目分屏列（own=True）的 runtime 按它绑定的项目目录
        组装——cwd 与项目约定必须留在会话自己的项目里；其余（含基底）与
        旧写法一致用 compose_system()。"""
        own_workdirs = {
            id(rt.agent): rt.ctx.workdir
            for rt in self.runtimes.values()
            if rt.ctx is not None and rt.ctx.own
        }
        for ag in self._for_each_agent():
            if id(ag) in own_workdirs:
                ag.set_system(self.compose_system_for(own_workdirs[id(ag)]))
            else:
                ag.set_system(self.compose_system())

    def respond_permission(self, request_id: str, decision: str) -> bool:
        """权限决策路由：普通 runtime agents + 各会话团队的成员 agents。

        覆盖 TerminalPanelMixin 的同名方法（本类定义优先于 mixin）。成员 Agent
        不并进 _for_each_agent——那是全局状态刷新（模型/注册表/系统词）的枚举面，
        把成员刷成主会话系统词会毁掉成员人设；权限路由单独并入（设计 §6：确认
        只认用户）。决策值仍过 Agent.respond_permission 的 normalize 白名单，
        乱码一律按 deny，与主 Agent 同一 fail-closed 语义。
        """
        for ag in self._for_each_agent():
            if ag.respond_permission(request_id, decision):
                return True
        for orch in self._teams.values():
            if orch.respond_permission(request_id, decision):
                return True
        return False

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
        # 团队模板册同款启动装载：不 load 的话 template_list 恒回空、
        # template_remove 对磁盘已有模板误报「找不到」，此后任意一次
        # template_save 会以仅含新模板的内存册整体覆盖 teams.json——已存
        # 模板被静默清光（upsert→save 是全量覆盖落盘）
        self.team_templates.load()
        # Mods 暂存目录清扫：install 与 confirm 之间重启遗留的 .importing-*
        # （只启动期调一次，运行期可能正有未确认的安装预览占着 staging）
        self.sweep_mod_staging()
        # 「远程连接」固定项目：启动即建（飞书/微信等渠道对话的归属），
        # 与是否配置渠道无关——侧栏里它是一个常驻分组
        await self.store.ensure_remote_project()
        # 快聊 / 远程连接的固定工作目录：数据目录下真实文件夹（default /
        # remote-control），启动即建——两类会话从此有稳定的工作目录
        self._quick_workdir()
        self._remote_workdir()
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

        self._base_agent = self._build_agent(
            provider=self.provider,
            gate=self.gate,
            working_dir=self.working_dir,
            mods=self.mods,
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


    def _build_full_registry(
        self, recorder: ChangeRecorder | None = None, ctx: ProjectCtx | None = None
    ) -> ToolRegistry:
        """完整工具集：内置（write/edit/画图挂检查点记录器）+ 日程 + 技能 + 子代理 + MCP。

        子代理可在设置页整体关闭：关掉后 spawn_agent / check_task / wait_task
        不注册，模型看不到这些工具（配置里 subagent_enabled = false）。

        ctx 传入且 own=True（跨项目分屏列的会话 runtime）时，子代理与流水线
        绑定会话所属项目——子代理跑在该项目的工作目录、流水线落该项目，
        而不是引擎当前项目。技能/MCP 仍是引擎级单例（compose_system_for 的
        定时任务先例：按目录重挂技能过重，跨项目列沿用当前装载的技能段）。
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
        if ctx is not None and ctx.own:
            # 跨项目列：绑定会话所属项目（ctx.tasks/project_id 在 _own_ctx 已定）
            if self.cfg.subagent_enabled and ctx.project_id is not None and ctx.tasks is not None:
                registry.register(SpawnAgentTool(ctx.tasks))
                registry.register(CheckTaskTool(ctx.tasks))
                registry.register(WaitTaskTool(ctx.tasks))
            if ctx.project_id is not None:
                registry.register(PipelineWriteTool(
                    self.store, lambda: ctx.project_id, lambda: ctx.tasks,
                ))
        else:
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
            rt.agent.registry = self._build_full_registry(rt.recorder, ctx=rt.ctx)

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
                # 渠道收尾最坏要几十秒（taskkill / proc.wait / lark-cli event stop
                # 各带超时上限），而桌面壳只给优雅退出 1.5 秒、更新路径随后
                # os._exit：这里必须给 wait_for 上限，别让渠道清理拖垮整个收尾
                # 链。截断后剩下的交给子进程自己的超时与进程退出兜底。
                await asyncio.wait_for(self.channels.stop(), timeout=5.0)
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
        # 跨项目分屏列私有的子代理管理器一并收掉（引擎退出不区分主栏项目）
        for tm in self._project_tasks.values():
            tm.cancel_all()
        if self.mods is not None:
            self.mods.close()  # 收掉每个 Mod 的沙箱执行线程（quickjs runtime 不可并发）
        if self.store and self._store_override is None:
            await self.store.close()


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

    def _build_detached_provider(self, name: str, model: str | None) -> Provider:
        """构建不接管「当前模型」状态的独立 provider（辅助对话专用）。

        _build_provider 会顺带改写 provider_name / provider_model——那是主
        对话的状态面（底部徽章、model.list 的 current 都读它），辅助对话换
        模型不能碰；这里按同款校验只构建不登记。"""
        if self._provider_factory_override is not None:  # 测试/演示注入
            return self._provider_factory_override()
        pc = self.cfg.providers[name]
        if model:
            pc = pc.model_copy(update={"model": model})
        try:
            return build_provider(name, pc)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e

    # ---- 系统提示词 / 会话 ----

    def _memory_retrieval_query(self, text: str, agent: Agent) -> str:
        """记忆检索化（第一期）的查询串：本轮用户消息 + 最近几条对话摘要。

        截断只为控查询体积（词元化是 O(len)），不改变相关性的来源构成；重新
        生成轮 text 为空串，查询自然落在历史末尾被重跑的那条用户消息上。
        system/tool 消息不是对话语义的来源，跳过；拿不到任何内容返回空串，
        render_memory_section 按约定整块注入。
        """
        parts = [text.strip()] if text.strip() else []
        recent: list[str] = []
        for m in reversed(agent.history):
            if m.role not in ("user", "assistant"):
                continue
            t = (m.text or "").strip()
            if not t:
                continue
            recent.append(t[:300])
            if len(recent) >= 3:
                break
        parts.extend(reversed(recent))
        return "\n".join(parts)[:4000]

    def compose_system(self, memory_query: str = "") -> str:
        return self.compose_system_for(self.working_dir, memory_query=memory_query)

    def compose_system_for(self, workdir: Path | None, memory_query: str = "") -> str:
        """无人值守跨项目运行（定时任务/流水线节点）按目标目录组装系统提示词。

        工作目录与项目约定（AGENTS.md）必须取目标目录的——否则提示词里写着
        A 目录、实际却在 B 目录干活，Agent 会找错地方；技能段沿用当前装载的
        SkillLoader（按目录重挂载过重），全局记忆本就跨项目。
        workdir 为 None 是无项目态（兜底：引擎没开项目且会话缺归属记录），
        提示词里说明没有工作目录、文件工具会拒绝。快聊/远程连接固定在数据
        目录 default / remote-control 文件夹，不落 None 分支，文件工具可用。
        memory_query 是记忆检索化（第一期）的查询串（「本轮用户消息 + 最近对话
        摘要」，见 _memory_retrieval_query）：记忆超过 MEMORY_RETRIEVAL_THRESHOLD
        时只注入相关条目；空串表示拿不到本轮查询（轮外的 set_system 场景），
        render_memory_section 按约定整块注入、行为与检索化之前一致。
        """
        instr_file, instr_text = (
            load_project_instructions(workdir) if workdir is not None else (None, "")
        )
        return (
            build_system_prompt(workdir)
            + self.skills.render_prompt_section()
            + render_instructions_section(instr_file, instr_text)
            + render_memory_section(query_text=memory_query)
        )

    async def fork_session(self, params: dict, local: bool = True) -> dict:
        """从某条消息分叉出新会话：复制 seq <= 锚点 的消息（缺省全部）。

        local=True（本机，默认）跨项目会话放行；分叉的新会话落回源会话
        所属的项目——跨项目分屏列的分叉不能把别家项目的对话复制进当前
        项目（快聊/当前项目会话保持既有口径）。"""
        sid = str(params.get("id", "") or (self.session.id if self.session else ""))
        sess = await self._get_owned_session(sid, local=local)
        # 与 truncate 同一守卫：轮末才批量落库，运行中分叉会复制到不完整的历史
        rt = self.runtimes.get(sid)
        if rt and rt.run_task and not rt.run_task.done():
            raise RuntimeError("该会话正在运行，等当前轮结束再分叉")
        seq = int(params["seq"]) if params.get("seq") is not None else (
            await self.store.max_seq(sid) or 0)
        cur_pid = self._cur_project_id()
        fork_pid = sess.project_id if (
            sess.project_id is not None and sess.project_id != cur_pid
        ) else cur_pid
        new_sess = await self.store.create_session(
            fork_pid, title=(f"└ {sess.title or '分叉'}")[:40]
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

    async def resume_session(self, session_id: str) -> dict:
        sess = await self._get_owned_session(session_id)
        self.session = sess
        try:
            await self._write_ui_prefs({self.SESSION_ACTIVE_KEY: session_id})
        except Exception:
            pass
        rt = await self._get_runtime(session_id)
        msgs = await self._reload_agent_history(rt.agent, session_id)
        return {
            "id": sess.id, "title": sess.title, "summary": sess.summary,
            "messages": [_msg_brief(m) for m in msgs if m.role in ("user", "assistant")],
        }

    async def session_model_switch(self, session_id: str, name: str, model: str) -> dict:
        """按会话切换模型（分屏独立对话）：只影响该会话 runtime 的 provider，
        不动全局——别的会话（含主栏）保持自己的模型。

        会话还没建 runtime 时先懒建（保证覆盖立即落在 agent 上）；覆盖记进
        _session_models，runtime 被淘汰重建后由 _runtime_provider 恢复。

        本机专属（WS 层 local_only）：跨项目分屏列换模型放行，runtime 绑定
        会话所属项目干活（_project_ctx_for）。"""
        await self._get_owned_session(session_id, local=True)
        name = str(name or "").strip()
        model = str(model or "").strip()
        if name not in self.cfg.providers:
            raise RuntimeError(f"未知服务：{name}")
        pc = self.cfg.providers[name]
        if model and pc.models and model not in pc.models:
            raise RuntimeError(f"服务 {name} 没有登记模型 {model}，先在模型服务里添加")
        existed = session_id in self.runtimes
        rt = await self._get_runtime(session_id)
        if not existed:
            # 懒建的是空壳 runtime（只有系统提示词）：照 resume_session 同款补拉
            # 历史。不补的话 runtime 已在内存，activate_session 的「不在 runtimes
            # 才重载」分支永不触发，此后该会话所有轮次都在空上下文上跑（重启或
            # LRU 淘汰后在分屏列换模型即触发——静默丢全部历史）。
            await self._reload_agent_history(rt.agent, session_id)
        rt.agent.provider = self._build_detached_provider(name, model or None)
        pc_eff = self.cfg.providers.get(name)
        rt.agent.context_limit_tokens = (
            pc_eff.effective_context_limit(self.cfg.context_limit_tokens)
            if pc_eff else self.cfg.context_limit_tokens
        )
        self._session_models[session_id] = (name, model)
        # 已存档位随热切迁移：新 provider 支持思考则重放；不支持则清除
        # （build_provider 已按新服务 config 设初值，两条路径命运一致）
        saved_effort = self._session_efforts.get(session_id)
        if saved_effort is not None:
            if getattr(rt.agent.provider, "supports_reasoning", False):
                rt.agent.provider.set_reasoning_effort(saved_effort)
            else:
                self._session_efforts.pop(session_id, None)
        return {
            "session_id": session_id,
            "provider": name,
            "model": model or pc.model,
            "supports_vision": True if pc_eff is None else bool(pc_eff.supports_vision),
            "context_limit": rt.agent.context_limit_tokens,
        }

    def session_model_get(self, session_id: str) -> dict:
        """会话当前生效的模型：有覆盖给覆盖，否则按三段解析（覆盖 → 新会话
        默认模型 → 全局）；supports_vision 随生效服务给（前端不再自行推导）。"""
        override = self._session_models.get(session_id)
        if override:
            name, model = override
            overridden = True
        else:
            name, model = self._session_effective_model(session_id)
            overridden = False
        pc = self.cfg.providers.get(name)
        return {
            "session_id": session_id,
            "name": name,
            "model": model or (pc.model if pc is not None else ""),
            "overridden": overridden,
            "supports_vision": True if pc is None else bool(pc.supports_vision),
        }

    async def session_reasoning_get(self, session_id: str) -> dict:
        """会话当前生效的思考强度：不建 runtime，按三段解析出的服务读档位。

        有 runtime 读其 provider 实例（含热调未落盘的档位），无则读服务配置。
        overridden 表示该会话有会话级档位覆盖（session.reasoning_set 写入）。"""
        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        rt = self.runtimes.get(session_id)
        name, _model = self._session_effective_model(session_id)
        pc = self.cfg.providers.get(name)
        if rt is not None:
            supported = bool(getattr(rt.agent.provider, "supports_reasoning", False))
            effort = getattr(rt.agent.provider, "reasoning_effort", "auto")
        elif pc is not None:
            supported = bool(pc.supports_reasoning)
            effort = pc.reasoning_effort
        else:
            supported, effort = False, "auto"
        return {
            "session_id": session_id,
            "supported": supported,
            "effort": self._session_efforts.get(session_id, effort),
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "overridden": session_id in self._session_efforts,
        }

    async def session_reasoning_set(self, session_id: str, effort: str) -> dict:
        """按会话设置思考强度：不动全局、绝不静默更换会话模型。

        按三段解析出的 (服务, 模型) 用 detached 副本建独立 provider 挂回该
        会话 runtime（还没建就懒建，保证立即生效）——无覆盖会话的 agent
        原本与全局共享同一个 provider 对象，直接在共享对象上调档会把所有
        会话一起改掉，所以这里必须换成会话私有副本。能力校验 fail-closed
        （与 model.set_reasoning 同口径）；记入 _session_efforts，runtime
        重建后由 _get_runtime 恢复。"""
        sess = await self.store.get_session(session_id)
        if sess is None:
            raise RuntimeError("session not found: " + session_id)
        effort = str(effort or "auto").strip().lower()
        if effort not in REASONING_EFFORTS:
            raise RuntimeError("思考强度只支持 " + " / ".join(REASONING_EFFORTS))
        name, model = self._session_effective_model(session_id)
        if not name:
            raise RuntimeError("当前没有可用的模型服务，先在模型服务里配置")
        pc = self.cfg.providers.get(name)
        existed = session_id in self.runtimes
        rt = await self._get_runtime(session_id)
        if not existed:
            # 懒建的是空壳 runtime：先补历史再校验（同 session_model_switch）。
            # 校验不过就把刚建的空壳回收——留着的话 activate_session 的重载
            # 分支不再触发，空壳会吃掉该会话之后的全部历史。
            await self._reload_agent_history(rt.agent, session_id)
        # 能力校验 fail-closed（与 model.set_reasoning 同口径）：配置里没有的
        # 服务（测试注入等）按 provider 实例判。校验不过就不动 runtime 的
        # provider——先换副本再发现不支持，会白白把共享 provider 私有化
        if pc is not None:
            supports = bool(pc.supports_reasoning)
        else:
            supports = bool(getattr(rt.agent.provider, "supports_reasoning", False))
        if not supports:
            if not existed:
                self.runtimes.pop(session_id, None)
                self._forget_runtime(rt)
            raise RuntimeError(f"「{name}」不支持调整思考强度（未声明该能力）")
        rt.agent.provider = self._build_detached_provider(name, model or None)
        rt.agent.provider.set_reasoning_effort(effort)
        pc_eff = pc if pc is not None else None
        rt.agent.context_limit_tokens = (
            pc_eff.effective_context_limit(self.cfg.context_limit_tokens)
            if pc_eff else self.cfg.context_limit_tokens
        )
        self._session_efforts[session_id] = effort
        return {
            "session_id": session_id,
            "supported": True,
            "effort": effort,
            "efforts": list(REASONING_EFFORTS),
            "labels": dict(REASONING_EFFORT_LABELS),
            "overridden": True,
        }

    async def peek_session_messages(self, session_id: str, local: bool = True) -> dict:
        """只读历史快照（分屏查看用）：与 resume_session 同款回包，但不切引擎
        的当前会话、不建 runtime、不写 ui 偏好——分屏看另一个会话不能动主栏。

        归属口径：本机界面（local=True）按 id 直查、跨项目可读——分屏的入口
        是侧栏各项目分组下的会话行，右键时引擎可能停在另一个项目上，按当前
        项目校验会把别的项目的会话误报「不存在」；远程仍走 _get_owned_session
        的当前项目口径（历史内容是枚举面，不向远程放开跨项目）。"""
        if local:
            sess = await self.store.get_session(session_id)
            if sess is None:
                raise RuntimeError("session not found: " + session_id)
        else:
            sess = await self._get_owned_session(session_id)
        # 会话能否在分屏列里交互（发送/换模型）：本机一律可交互——分屏列是
        # 完整的会话，跨项目 runtime 绑定会话所属项目干活（_project_ctx_for）；
        # 远程客户端保持旧口径：只放开当前项目与快聊/远程连接这些开放归属，
        # 跨项目的列仍按 interactive=False 只读（chat.send/model_switch 的
        # local=False 归属校验是这道边界的后端兜底）。
        cur_pid = self._cur_project_id()
        interactive = bool(local) or (
            sess.project_id is None
            or sess.project_id == cur_pid
            or sess.project_id == await self._remote_project_id()
        )
        msgs = await self.store.load_messages(session_id)
        return {
            "id": sess.id, "title": sess.title, "summary": sess.summary,
            "interactive": interactive,
            "messages": [_msg_brief(m) for m in msgs if m.role in ("user", "assistant")],
        }

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

    async def _build_refs_context(self, refs: list[str], project_id: int | None = None) -> str:
        """把被引用会话的记录拼成注入本轮的上下文块。

        会话不存在、不属于目标项目或没有可读消息时静默跳过；超长的保留尾部
        （最近的对话与当前任务最相关），并标注省略。格式与 /export 的导出文本
        一致。归属校验（安全审查 B2）：引用列表由客户端提交，不校验会把
        别的项目的历史注入当前 Agent 上下文。目标项目取「本轮会话所属项目」
        （runtime 的项目上下文）：跨项目分屏列引用它自己项目的对话照常可用，
        引用别的项目仍然剔除。
        """
        target_pid = project_id if project_id is not None else self._cur_project_id()
        blocks: list[str] = []
        for rid in refs:
            if await self.store.get_session_for_project(rid, target_pid) is None:
                continue  # 不属于目标项目的会话：当作不存在，不注入
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
                   chair_answers: bool | None = None,
                   team: bool = False,
                   adversarial: bool = False,
                   director_mode: str = "user",
                   director: dict | None = None,
                   local: bool = True) -> dict:
        """跑一轮对话；过程事件通过 emit 推送；结束后持久化新消息。

        local=True（本机，默认）跨项目会话放行——分屏列是完整会话，runtime
        绑定会话所属项目；远程客户端（False）仍按当前项目归属校验（B 族）。

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

        team=True 时本轮走「团队」流程：该会话没有活动团队则先建队
        （成员来自 members 参数，缺省自动解析，上限 cfg.team.max_members），
        随后消息作为总管指令进团队频道、被 @ 点名的成员逐个唤醒。团队进行中的
        会话，后续消息自动路由进团队（即便没带 team 标志）；team 与 roundtable
        同轮互斥。director_mode="ai"（二期）建队时指定 AI 总管：director 为
        担任总管的 {provider, model}，用户消息经 run_auto_turn 驱动自动闭环
        （拆解 → 派工 → 验收 → 交付）；缺省 "user" 保持一期用户总管行为。

        adversarial=True 时本轮走「对抗」流程：四角色流水线审查——发现者穷举
        问题 → 调查者逐条对抗验证 → 建议者给修复方案 → 裁判（当前主模型）
        终审报告。members 按序承担发现/调查/建议三个角色（不足时复用同一个）；
        与 team/roundtable 三者同轮互斥。

        refs 为「& 引用对话」选中的会话 id 列表：每轮最多 REF_MAX_SESSIONS
        个，会话记录会被注入本轮上下文（见 _build_refs_context）。

        session_id 指定目标会话（多会话并行时前端按标签传入）；缺省用活动会话。
        目标会话不是当前活动会话时先就位它的 runtime（校验 + 补历史，见
        _ready_runtime_for_send）——不切换活动指针：定向发送（分屏列）不能
        劫持「当前会话」，伴生面板的取数口径不被带走。
        """
        # 团队/圆桌/对抗同轮互斥（设计 §13.4；对抗同规则）：前端开关联动互斥，
        # 后端多参数同给报参数错
        if sum(bool(m) for m in (team, roundtable, adversarial)) > 1:
            raise RuntimeError(
                "「团队」「圆桌」「对抗」不能同时开启：同一条消息只能选一种协作模式。"
            )
        # 轮次起点重验工作区信任（审查 P2-4）：会话运行期间项目级配置被外部
        # 改动（git pull 等）时，趁本轮开始断开项目级 MCP、重发现技能。
        await self.recheck_trust_before_turn()
        # 钉住目标会话（并发正确性）：send 执行期间有多个 await（激活、用量、
        # 引用清洗…），期间并发请求可以改写全局活动指针 self.session（另一条
        # send / session.activate 都会）。之后一律用入口钉住的 target_sid 推导
        # runtime 与落库，不再从 self.session 二次读取——否则 A 会话的轮会跑到
        # B 会话的 runtime 里并落库到 B。
        target_sid = str(session_id) if session_id else (
            self.session.id if self.session else None
        )
        if session_id and (not self.session or self.session.id != session_id):
            # 目标会话就位（归属校验 + 建 runtime + 补历史）但不动活动指针：
            # 定向发送不劫持「当前会话」，主栏伴生面板（审查/任务清单/上下文
            # 浮层）读到的仍是主栏会话，ui.json 的活动会话也不被覆写
            await self._ready_runtime_for_send(session_id, local=local)
        # 会话级活动团队：进行中的团队把该会话的后续消息全部吸进团队频道
        #（设计 §3——团队进行中不再进普通回合），即便前端没带 team 标志。
        active_team = target_sid is not None and target_sid in self._teams
        if active_team and (roundtable or adversarial):
            mode_name = "圆桌" if roundtable else "对抗"
            raise RuntimeError(
                f"当前会话有进行中的团队，消息会进入团队频道；请先收队再使用{mode_name}。"
            )
        team_bound = team or active_team
        # 引用排除目标会话自己：引用当前对话没有意义
        clean_refs = self._sanitize_refs(refs, exclude=target_sid)
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
        # 图片附件：本轮实际生效的服务声明"不支持图片输入"时提前给可读提示，
        # 而不是把图片塞给纯文本模型，换回一句上游报错（用户不知道是模型选错了）。
        # 按目标会话解析（三段：覆盖 → 新会话默认 → 全局）——会话级换过模型的
        # 分屏列/标签，判的是它自己的服务；拿全局记账判会双向误判（全局不支持/
        # 覆盖支持被误拒、全局支持/覆盖不支持放行给上游报错），错误文案也会报成
        # 没在用的服务名。
        clean_images = self._sanitize_images(images)
        # 团队轮是纯文本协作，图片本就不会发给队员（编排器会发 Notice 并忽略），
        # 不因主模型不支持图片拦人
        if clean_images and not team_bound:
            eff_name, eff_model = (
                self._session_effective_model(target_sid) if target_sid
                else (self.provider_name or "", self.provider_model or "")
            )
            pc_eff = self.cfg.providers.get(eff_name) if eff_name else None
            # 未配置的服务（测试注入等）按支持处理，不拦人（与 _supports_vision 同口径）
            if pc_eff is not None and not pc_eff.supports_vision:
                raise RuntimeError(
                    f"「{eff_name} / {eff_model or pc_eff.model}」不支持图片输入，图片发不出去。\n"
                    "请在输入框的模型选择器里换一个多模态模型（如 GLM-4V、Kimi、GPT-4o 等），"
                    "或在 设置 · 模型服务 里把该服务的「支持图片输入」打开。"
                )
        # 在任何 await 之前先占住运行位：否则两条背靠背到达的消息会在
        # new_session() 的挂起点上双双判为空闲、并发执行（撞消息表唯一约束）。
        # 占位判断同时看基底位（会话懒创建窗口）与活动 runtime 位；后来者排队。
        # 全程用钉住的 target_sid，不重读 self.session（见上）。
        cur = asyncio.current_task()
        rt_now = self.runtimes.get(target_sid) if target_sid else None
        holder = rt_now.run_task if rt_now else self._base_run_task
        if holder is not None and not holder.done() and holder is not cur:
            loop = asyncio.get_running_loop()
            item = QueuedTurn(
                text=text, emit=emit, plan_mode=plan_mode, fut=loop.create_future(),
                roundtable=roundtable, members=members,
                images=self._sanitize_images(images),
                refs=clean_refs, compare=compare,
                debate_rounds=debate_rounds, chair_answers=chair_answers,
                team=team, adversarial=adversarial,
                director_mode=director_mode, director=director,
            )
            if rt_now is not None:
                rt_now.queue.append(item)
                await emit(QueueUpdated(pending=len(rt_now.queue)).model_dump())
            else:
                self._base_queue.append(item)
                await emit(QueueUpdated(pending=len(self._base_queue)).model_dump())
            return await item.fut
        # 全局并行轮数上限（评审加固）：单会话排队（上面）不增加全局并发——交棒
        # 接替的是刚结束的那轮；真正新起一轮的 send 才受这道闸。定时任务/流水线
        # 用本地 runtime，不进 self.runtimes，不受也不占这额度。
        if self._running_turn_count() >= self.MAX_INTERACTIVE_TURNS:
            raise RuntimeError(
                f"当前并行任务太多（已有 {self.MAX_INTERACTIVE_TURNS} 个会话同时在跑），"
                "请等部分会话的当前轮结束，或先停止不需要的轮次再发送。"
            )
        self._base_run_task = cur  # 抢占基底位，掩护随后的懒创建 await 窗口
        if target_sid is None:
            await self.new_session()  # 懒创建：第一条消息才落库
            target_sid = self.session.id if self.session else None
        runtime = await self._get_runtime(target_sid)
        runtime.run_task = cur
        self._base_run_task = None  # 运行位已落到 runtime，基底占位清除
        try:
            return await self._run_turn_pipeline(
                text, emit, plan_mode, roundtable=roundtable, members_params=members,
                images=clean_images, runtime=runtime,
                session_id=target_sid, wants_title=wants_title,
                regenerate=regenerate, compare=compare, refs=clean_refs,
                debate_rounds=debate_rounds, chair_answers=chair_answers,
                team=team, adversarial=adversarial,
                director_mode=director_mode, director=director,
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
                "adversarial": False,
                "team": None,
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
            raw = (await collect_stream_text(self.provider, prompt)).strip()
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
        if system is None:
            # 跨项目分屏列的 runtime 绑定会话所属项目：系统提示词按它自己的
            # 工作目录组装（cwd 与项目约定对齐）；其余走 compose_system()
            # 既有接缝（输出等价）。
            rt = self.runtimes.get(sid)
            if rt is not None and rt.ctx is not None and rt.ctx.own:
                system = self.compose_system_for(rt.ctx.workdir)
            else:
                system = self.compose_system()
        agent.set_system(system)
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
        team: bool = False,
        adversarial: bool = False,
        director_mode: str = "user",
        director: dict | None = None,
    ) -> dict:
        """真正执行一轮对话（含持久化、规划模式切换、检查点保存）。

        多会话并行：全程使用 runtime 内的 agent/queue/recorder，
        不碰 self.agent 等活动会话指针；事件发出时注入 session_id 供前端路由。
        """
        if runtime is None:
            if self.session is None:
                await self.new_session()  # 懒创建：第一条消息才落库
            runtime = await self._get_runtime(self.session.id)
        sid = session_id or runtime.sid
        agent = runtime.agent

        # 流式增量合并：emit_ev 下发给前端前先过一层缓冲，把连续的同类型增量
        # （text_delta / thinking_delta / roundtable_member_delta）拼成批量帧。
        # 其余事件在 merger 里会先冲刷缓冲再原样发出，顺序与合并前一致。
        merger = StreamDeltaMerger(emit)

        # 本轮工具/权限的耗时统计（给轮末结构化日志用）。
        # 从事件流里数而不是侵入 agent 循环：事件本就带 duration_ms，
        # 这里只做累加，不在热路径上加任何计算。
        # slowest_tool(_ms) 记本轮最慢的一次工具调用——「这轮为什么慢」最常
        # 是某一个慢工具，只有一个总数看不出是谁。
        turn_stats: dict[str, int] = {
            "tool_calls": 0, "tool_ms": 0, "tool_errors": 0,
            "permission_waits": 0, "permission_ms": 0,
            "slowest_tool": "", "slowest_tool_ms": 0,
        }
        _perm_pending: dict[str, float] = {}

        async def emit_ev(ev: dict) -> None:
            kind = ev.get("kind")
            if kind == "tool_call_finished":
                _ms = int(ev.get("duration_ms") or 0)
                turn_stats["tool_calls"] += 1
                turn_stats["tool_ms"] += _ms
                # 首次调用先占位（0ms 的瞬时工具也要有名字），之后严格更慢的才替换
                if turn_stats["slowest_tool"] == "" or _ms > turn_stats["slowest_tool_ms"]:
                    turn_stats["slowest_tool_ms"] = _ms
                    turn_stats["slowest_tool"] = str(ev.get("name") or "")
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
        # chat.send 在 WS 层用 request_emit 包一层以便懒绑定远程会话；包装器
        # 仍代表同一条连接。优先取它挂载的稳定源 emitter，否则普通内部调用
        # 仍按传入的 emit 身份排除发送方。
        source_emit = getattr(emit, "_source_emit", emit)
        others = [w for w in list(self.ws_emitters) if w is not source_emit]
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
                members=len(members_params or []) if (roundtable or adversarial) else 0,
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
        # 记忆检索化（第一期）：轮首用「本轮用户消息 + 最近对话摘要」作查询重组
        # 系统提示词——超过 MEMORY_RETRIEVAL_THRESHOLD 的记忆只注入相关条目
        # （tools/memory.py 的 render_memory_section）；不超过阈值（或全部 0 分）
        # 时组装结果与原先逐字节一致，这里的 set_system 只是刷新同文本的
        # system 消息（system 从不落库，本就每处实时生成，见 _reload_agent_history）。
        memory_query = self._memory_retrieval_query(text, agent)
        if memory_query:
            # 嵌入是增强，绝不能阻塞事件循环（memory_embed 的性能不变量）：
            # 检索分支可能同步探测 / 嵌入 Ollama 向量，整体丢 worker 线程组装
            # （墙钟预算在 memory_embed 内部约束，慢 Ollama 也只拖慢本轮几秒），
            # 同时后台预热词条向量——暖齐后轮首只剩查询向量一次请求
            schedule_warmup()
            if runtime.ctx is not None and runtime.ctx.own:
                # 跨项目分屏列的会话按它自己的工作目录组装
                agent.set_system(await asyncio.to_thread(
                    self.compose_system_for, runtime.ctx.workdir,
                    memory_query=memory_query,
                ))
            else:
                agent.set_system(await asyncio.to_thread(
                    self.compose_system, memory_query=memory_query,
                ))
        # 团队轮例外含自动路由轮：引擎前缀一律不拼——频道消息保持用户原文
        #（§13.5 一律进频道、不做意图识别）——按 team_bound 判，不按本轮
        # flag 判（活动团队把没带标志的消息也吸进频道，前缀漏进频道文本会
        # 误导队员）。团队期间攒下的注记等收队/交付后的下一普通轮照常注入。
        team_bound = team or sid in self._teams
        # 「& 引用对话」：把被引用会话的记录拼在消息最前面注入本轮上下文
        #（与 PLAN_MODE_PREFIX 同一套做法，随用户消息一起持久化）。
        if refs and not team_bound:
            refs_ctx = await self._build_refs_context(
                refs, runtime.ctx.project_id if runtime.ctx is not None else None,
            )
            if refs_ctx:
                text = refs_ctx + text
        if plan_mode and not team_bound:
            text = PLAN_MODE_PREFIX + text
        # 自上一轮以来结束、还没人取报告的后台子代理任务 → 注入一条系统提示，
        # 主 Agent 开轮就知道「有任务做完了」，不用用户来催（后台模式闭环）。
        # 团队轮不注入：那是主会话轮次的词汇，暂缓到团队结束后的下一普通轮
        # （注记留在簿子里不取走，收队/交付后 _teams 移出，自然轮到它）。
        if self.tasks and not team_bound:
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
        tm_meta: dict | None = None
        adv_meta: dict | None = None
        tin0, tout0 = agent.total_in_tokens, agent.total_out_tokens
        tcached0 = agent.total_cached_tokens
        runtime.run_task = asyncio.current_task()
        turn_exc: BaseException | None = None
        try:
            if team or sid in self._teams:
                if roundtable or adversarial:
                    # 排队轮重验团队/圆桌/对抗同轮互斥（send() 的守卫只在入队
                    # 时刻判定）：消息在「建队轮已开跑、_teams 尚未登记」的窗口
                    # 入队时 roundtable/adversarial=true 被接受，交棒到这里团队
                    # 已在板——不重验的话协作标志会被本分支静默吞掉（0 个事件、
                    # 无任何报错）。与不入队即被拒的口径对齐，显式报错交还调用方。
                    raise RuntimeError(
                        "当前会话有进行中的团队，消息会进入团队频道；请先收队再使用协作模式。"
                    )
                # 团队轮：进行中的团队把消息吸进频道（即便 flag 未带），team=true
                # 且无活动团队则先建队。取消在编排器内收敛成「半截发言定稿进频道」
                # 后原样上抛，由外层 except 收口为 stopped。
                tm_meta = await self._team_body(
                    text, emit_ev, members_params, agent=agent, sid=sid,
                    images=images, allow_create=team, recorder=runtime.recorder,
                    plan_mode=plan_mode,
                    director_mode=director_mode, director=director,
                )
            elif roundtable:
                rt_meta = await self._roundtable_body(
                    text, emit_ev, members_params, agent=agent, compare=compare,
                    sid=sid, images=images,
                    debate_rounds=debate_rounds, chair_answers=chair_answers,
                    regenerate=regenerate,
                )
                # 圆桌的取消在引擎层收敛（保留已产出的部分文本），这里同步停止位
                if (rt_meta or {}).get("status") == "cancelled":
                    stopped = True
            elif adversarial:
                adv_meta = await self._adversarial_body(
                    text, emit_ev, members_params, agent=agent, sid=sid,
                    images=images, regenerate=regenerate,
                )
                # 对抗的取消同样在引擎层收敛（已裁决结论与部分报告文本保留）
                if (adv_meta or {}).get("status") == "cancelled":
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
            # 先恢复完整工具集（只读注册表只在本轮生效）——同步操作必须放在
            # finally 里第一个 await 之前：第二次取消（用户连点两次停止，或
            # 停止与轮末竞态）只能在 await 点投递，同步段不会被跳过。若恢复
            # 被跳过，runtime 常驻，该会话之后所有轮次都拿着只读注册表跑。
            if plan_mode and readonly_registry is not None:
                agent.registry = self._build_full_registry(runtime.recorder, ctx=runtime.ctx)
            # 轮被取消/异常中止时，未决权限的决策永远不会到来（CancelledError
            # 从 pending.wait() 穿透，agent 侧不再产出 resolved 事件）：这里补发
            # 带 cancelled 语义的 resolved，让前端把可能残留的确认卡收掉——否则
            # 卡上按钮的投递只会静默拿到 delivered=false，切标签还会把死卡重新
            # 弹出。正常结束路径 _perm_pending 已空（决策路径各自发过 resolved），
            # 一条不发；emit_ev 会顺带摘掉对应条目，故用快照遍历、只清一次。
            for rid in list(_perm_pending):
                try:
                    await emit_ev(
                        PermissionResolved(request_id=rid, decision="cancelled").model_dump()
                    )
                except asyncio.CancelledError:
                    asyncio.current_task().uncancel()
                except Exception:  # noqa: BLE001 - 客户端断开不影响收尾
                    pass
            # 再冲刷流式增量缓冲：取消/异常/正常结束三条路径都要走到，
            # 否则最后几十毫秒的正文会丢在前端（用户看到回答缺尾）。
            # 收尾期的重复取消在此吞掉（首个取消已在上面捕获、语义已定为
            # stopped；与下方落库 shield 的 uncancel 取向一致），保证后面的
            # 落库/用量/交棒总是执行，排队中的消息不会永远等不到响应。
            try:
                await merger.aclose()
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()
            except BaseException:  # noqa: BLE001 - 客户端断开不影响收尾
                pass

        # ---- 收尾必须严格串行：先落库，再交棒给排队轮，否则两条持久化
        # ---- 并发会撞 messages(session_id, seq) 唯一约束。
        # 取消保护：用户点停止时，已产出的消息仍要落库、运行位必须释放；
        # shield 让落库在后台继续，CancelledError 被捕获后先收割后台落库再走
        # 完收尾返回 stopped 结果（二次取消下「先落库再交棒」同样成立，见下），
        # 避免 send() 抛异常导致 runtime.run_task 悬挂、后续消息误入死队列。
        # 压缩摘要不落库：它是引擎生成的上下文产物，入库会被当成一条 user 消息
        # 回传前端（渲染成假的用户气泡），也会进 FTS 与会话导出；不落库的代价
        # 只是重启后长会话重新压缩一次，比上面两类污染便宜得多。
        new_msgs = [
            m for m in agent.history
            if m.id not in pre_ids and not is_compaction_summary(m)
        ]
        _stamp_turn_estimate(new_msgs, turn_estimate, turn_t0)
        # 落库建成显式任务（shield 的内层）：被停时它在后台继续跑完；登记进
        # _persist_tasks 供 delete_session 等待（等主轮任务等不到后台落库）。
        persist_task = asyncio.get_running_loop().create_task(
            self._persist_turn(sid, new_msgs)
        )
        self._persist_tasks[sid] = persist_task
        persist_task.add_done_callback(
            lambda t, _sid=sid: self._persist_tasks.pop(_sid, None)
            if self._persist_tasks.get(_sid) is t else None
        )
        try:
            await asyncio.shield(persist_task)
        except asyncio.CancelledError:
            stopped = True
            asyncio.current_task().uncancel()
            # 二次取消（用户连点两次停止）落在这里时，persist_task 已在后台
            # 独立继续跑。不等它完成就走 _pop_next 交棒，交棒轮自己的落库会
            # 与这份后台旧落库并发「SELECT MAX(seq)+1 → INSERT」，撞
            # messages(session_id, seq) 唯一约束——要么交棒轮报 IntegrityError，
            # 要么被停轮的 INSERT 失败被 shield 回调静默吞掉、消息无声丢失。
            # 所以必须先收割后台落库、再继续收尾与交棒；截止时间兜底防卡死，
            # 等待期间再来的取消同样吞掉（语义已定为 stopped）。
            deadline = time.monotonic() + _PERSIST_HARVEST_TIMEOUT
            while not persist_task.done() and time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(persist_task),
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                except asyncio.CancelledError:
                    asyncio.current_task().uncancel()
                except TimeoutError:
                    pass  # 到点再看一眼：deadline 兜底，不无限等
            if not persist_task.done():
                # 落库卡死（如库被锁）不能拖着收尾无限等：取消后台落库，
                # 宁可这轮消息不落库也不让排队轮永远起不来
                persist_task.cancel()
                obs_warning(
                    "persist",
                    f"turn persist harvest timed out, cancelled sid={sid}",
                    session_id=sid,
                )
            elif not persist_task.cancelled():
                exc = persist_task.exception()
                if exc is not None:
                    # 收割异常（不再被 shield 回调静默吞掉）：停止语义下不上抛，
                    # 只记日志——上抛会把「停止」变成报错；消息仍在 agent 历史，
                    # 下次正常落库可补上
                    obs_warning(
                        "persist",
                        f"turn persist failed after cancel sid={sid}: {exc!r}",
                        session_id=sid,
                    )

        # 用量记录：本轮实际消耗的输入/输出 tokens（含缓存命中数，费用按缓存价拆算）
        try:
            await self.store.add_usage(
                sid, self.provider_name, self.provider_model,
                agent.total_in_tokens - tin0, agent.total_out_tokens - tout0,
                agent.total_cached_tokens - tcached0,
            )
        except Exception:
            pass
        # 预算告警：越线档位经渠道推一条（内部不抛、推送 spawn_bg 不阻塞收尾）
        await self._check_budget_alerts()

        # 结构化耗时记录：回答「这轮为什么慢」
        # （总耗时 / 工具次数与最慢工具 / 权限等待——这几项最容易看出卡在哪一段）。
        try:
            obs_info(
                "turn",
                f"turn finished sid={sid}",
                session_id=sid,
                duration_ms=int((time.monotonic() - turn_t0) * 1000),
                stopped=stopped or None,
                roundtable=bool(rt_meta) or None,
                adversarial=bool(adv_meta) or None,
                team=bool(tm_meta) or None,
                tool_calls=turn_stats["tool_calls"] or None,
                tool_ms=turn_stats["tool_ms"] or None,
                tool_errors=turn_stats["tool_errors"] or None,
                slowest_tool=turn_stats["slowest_tool"] or None,
                slowest_tool_ms=turn_stats["slowest_tool_ms"] or None,
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

        # 记忆二期：轮次收尾后抽「值得长期记住」的候选（开关默认关；
        # spawn_bg + 失败静默 + 按会话单飞都在方法内，绝不影响主流程）
        self.schedule_turn_distill(sid, new_msgs)

        # 本轮改动了文件 → 存检查点（落盘 + 内存索引）；结果里带给前端做「撤销本轮改动」。
        # 检查点库按会话所属项目取（跨项目分屏列的快照落它自己项目的目录树，
        # 不与引擎当前项目混放——与 _bind_project 的「检查点跟项目走」同口径）。
        ckpt_store = runtime.ctx.checkpoints if runtime.ctx is not None and runtime.ctx.own \
            else self.checkpoints
        checkpoint = await asyncio.to_thread(
            ckpt_store.save, sid, dict(runtime.recorder.pre)
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
            "adversarial": adv_meta,
            "team": tm_meta,
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
                chair_answers=item.chair_answers, team=item.team,
                adversarial=item.adversarial,
                director_mode=item.director_mode, director=item.director,
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
        chair_key: tuple[str, str] | None = None,
    ) -> list[MemberSpec]:
        """解析圆桌成员：显式列表优先；缺省时取所有已配置 Key 的服务的当前模型。

        规则：
        - 去重（同名同模型只留一个）；上限 cfg.roundtable.max_members（不含主席）；
        - chair_answers 为 None 时用配置值；为 True 时跳过与主席重复的成员
          （主席会被单独插到队列最前）；
        - chair_key 是主席的 (服务, 模型) 去重键：缺省按全局记账（团队等
          非会话轮），圆桌轮按本轮会话解析传入（见 _roundtable_body）；
        - 单个成员构建失败（缺 Key/未知服务）不阻断，作答时以错误卡片呈现。
        """
        specs: list[MemberSpec] = []
        seen: set[tuple[str, str]] = set()
        limit = self.cfg.roundtable.max_members
        chair_in = (
            self.cfg.roundtable.chair_answers if chair_answers is None
            else bool(chair_answers)
        )
        if chair_key is None:
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
        # 主席按本轮会话解析：会话级模型覆盖/默认模型会话的 runtime 挂的是
        # detached provider，全局记账（self.provider*）跟它无关——拿全局当主席，
        # 融合就由「界面上根本没显示的模型」做了，会话自己的模型反而以成员身份
        # 重复出场，轮末 meta 与用量也记错名下。三段解析与 runtime 挂的
        # provider 同源（见 _runtime_provider），这里按它取主席实例与 (服务, 模型)。
        chair = agent.provider if agent.provider is not None else self.provider
        if sid:
            chair_name, chair_model = self._session_effective_model(sid)
        else:
            chair_name, chair_model = self.provider_name or "", self.provider_model or ""

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

        members = self._resolve_members(members_params, chair_in, chair_key=(chair_name, chair_model))
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
                provider_name=chair_name,
                model=chair_model,
                provider=chair,
            ))

        # 成员/主席都不看图片（纯文本协作），历史里的图片块单独剥离；
        # history[:-1] 排除刚追加的本次提问（由 run_roundtable 自己拼在末尾）
        member_history = _strip_image_blocks(agent.history[:-1])

        system = history_system_text(agent.history) or self.compose_system()
        outcome: RoundtableOutcome = await run_roundtable(
            members=members,
            chair=chair,
            system_text=system,
            history=member_history,
            user_text=question,
            timeout_s=cfg.member_timeout_s,
            emit=emit_ev,
            debate_rounds=debate,
            fuse=not compare,
            chair_provider=chair_name or "",
            chair_model=chair_model or "",
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
            "chair": {"provider": chair_name, "model": chair_model},
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
            # 预算告警：圆桌烧的真金白银也要触发越线提醒（内部不抛）
            await self._check_budget_alerts()

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

    # ---- 对抗：四角色流水线审查（docs/对抗模式设计.md） ----
    # 与圆桌（会诊融合）、团队（分工协作）并列的第三种多模型协作。角色解析：
    # members 条目可带 role="finder"/"investigator"/"advisor" 逐角色显式指定
    # 模型（弹层三个下拉），同一模型可身兼数角（「同一 AI 自我对抗」的路径）；
    # 未指定的角色回退按序自动映射（缺省取所有已配置 Key 的服务）。裁判缺省用
    # 当前主模型（同圆桌主席）。编排本体在 core/adversarial.py，这里只做装配与落库。

    async def _adversarial_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent | None = None, sid: str | None = None,
        images: list[ImageBlock] | None = None, regenerate: bool = False,
    ) -> dict:
        """对抗轮主体：发现 → 调查 → 建议 → 裁判 → 报告并入主历史。

        返回随轮次结果回传前端的对抗元数据（角色名册、裁决统计、问题清单）。
        历史追加 user(被审内容) + assistant(最终报告)；中间产物（问题、裁决、
        方案）随报告消息的 adversarial 元数据持久化，历史回放时对抗卡可展开
        回看。取消语义与圆桌对齐：user 消息先入历史，中途点停止内容不丢；
        裁判中途取消时已流出的部分报告文本照样落库。
        """
        if agent is None:
            agent = self.agent
        if self.provider is None:
            raise RuntimeError("对抗需要当前主模型可用；请先在模型下拉中选择一个已配置 Key 的服务")

        cfg = self.cfg.adversarial
        # 裁判按本轮会话解析（同 _roundtable_body 的主席口径）：会话级换过模型
        # 的标签/分屏列，终审必须由会话自己的模型做，而不是全局记账的模型
        judge = agent.provider if agent.provider is not None else self.provider
        if sid:
            judge_name, judge_model = self._session_effective_model(sid)
        else:
            judge_name, judge_model = self.provider_name or "", self.provider_model or ""

        async def emit_ev(ev) -> None:
            await emit(ev.model_dump())

        adv_t0 = time.monotonic()

        def _adv_ms() -> int:
            return int((time.monotonic() - adv_t0) * 1000)

        await emit_ev(TurnStarted(iteration=1))

        # 对抗是纯文本协作：图片不发给角色模型，但要让用户知道，不静默丢弃
        if images:
            await emit_ev(NoticeEvent(
                message=f"对抗轮为纯文本协作：本轮的 {len(images)} 张图片不会发给角色模型"
                        "（已随消息保留）；需要模型看图请关闭对抗后重发。"
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

        # 上下文压缩护栏：四个角色都吃会话上下文，超限先压缩（同圆桌）
        if agent.used_context_tokens() > agent.context_limit_tokens:
            ev = await compact_history(agent, keep_recent=self.cfg.compaction_keep_recent)
            if ev is not None:
                await emit_ev(ev)

        # 角色解析：members 条目可带 role="finder"/"investigator"/"advisor"
        # 显式指定该角色的模型（弹层逐角色下拉）；带角色的条目直接构建、不参与
        # 去重（同一模型可身兼数角正是常态），其余角色回退到自动策略——无角色
        # 条目按序映射（chair 不回避：裁判与角色同服务是正常形态，不剔除）。
        explicit: dict[str, tuple[str, str]] = {}
        auto_params: list = []
        for m in members_params or []:
            entry = m if isinstance(m, dict) else {}
            role_key = str(entry.get("role", "") or "").strip()
            name = str(entry.get("provider", "") or "").strip()
            if role_key in ("finder", "investigator", "advisor") and name \
                    and role_key not in explicit:
                explicit[role_key] = (name, str(entry.get("model", "") or "").strip())
            elif name:
                auto_params.append(entry)

        def _explicit_spec(name: str, model: str) -> MemberSpec:
            try:
                provider = self._build_member_provider(name, model)
                return MemberSpec(
                    provider_name=name,
                    model=model or getattr(provider, "model", ""),
                    provider=provider,
                )
            except Exception as e:  # noqa: BLE001 - 构建失败降级为错误角色卡
                return MemberSpec(provider_name=name, model=model, build_error=str(e)[:200])

        # 自动序列：未带 members（纯自动）或带普通条目时才解析；三个角色全部
        # 显式指定时不白建 Provider
        specs_auto = (
            self._resolve_members(
                auto_params, False, chair_key=(judge_name, judge_model),
            )
            if (members_params is None or auto_params) else []
        )

        # 未显式指定的角色按 固定角色顺序 从自动序列依次取（游标递进）：全不指定
        # 时等价于旧的 0/1/2 取模复用（自动服务不足时同一模型身兼数角）；
        # 部分指定时剩余角色也按序补位，不会跳号。
        auto_cursor = 0

        def _role_spec(role: str) -> MemberSpec:
            nonlocal auto_cursor
            if role in explicit:
                return _explicit_spec(*explicit[role])
            if not specs_auto:
                return MemberSpec(
                    provider_name="", model="",
                    build_error="未指定模型且没有已配置 Key 的服务",
                )
            spec = specs_auto[auto_cursor % len(specs_auto)]
            auto_cursor += 1
            return spec

        finder = _role_spec("finder")
        investigator = _role_spec("investigator")
        advisor = _role_spec("advisor")
        if all(s.provider is None for s in (finder, investigator, advisor)):
            await emit_ev(TurnFinished(stop_reason="error", iterations=1, duration_ms=_adv_ms()))
            raise RuntimeError(
                "对抗没有可用角色：请先在「设置 · 模型服务」配置 API Key，"
                "或在对抗面板为角色指定模型"
            )

        # 角色都不看图片（纯文本协作），历史里的图片块单独剥离；
        # history[:-1] 排除刚追加的本次提问（由 run_adversarial 自己拼在末尾）。
        # 角色消息不接会话系统提示词：角色系统提示词自足（core/adversarial.py
        # 的 _phase_messages），dialogue() 会把 history 里的 system 剥掉。
        role_history = _strip_image_blocks(agent.history[:-1])

        outcome: AdversarialOutcome = await run_adversarial(
            finder=finder,
            investigator=investigator,
            advisor=advisor,
            judge=judge,
            judge_provider=judge_name or "",
            judge_model=judge_model or "",
            history=role_history,
            user_text=question,
            timeout_s=cfg.role_timeout_s,
            emit=emit_ev,
            max_findings=cfg.max_findings,
        )

        meta = {
            "mode": "adversarial",
            "judge": {"provider": judge_name, "model": judge_model},
            "roles": [
                {"role": "finder", "provider": finder.provider_name, "model": finder.model},
                {"role": "investigator", "provider": investigator.provider_name, "model": investigator.model},
                {"role": "advisor", "provider": advisor.provider_name, "model": advisor.model},
                {"role": "judge", "provider": judge_name, "model": judge_model},
            ],
            "stats": adversarial_stats(outcome),
            "findings": adversarial_findings_meta(outcome),
            "status": outcome.status,
            "error": outcome.error,
        }

        # 用量入账：对抗不走 agent.run_turn，agent.total_* 不会动。按角色逐条
        # 写 usage_log（裁判记当前主模型名下），统计页与预算护栏才看得见真实成本。
        if sid:
            try:
                for row in adversarial_usage_rows(outcome):
                    await self.store.add_usage(
                        sid, row["provider"], row["model"],
                        row["input_tokens"], row["output_tokens"],
                        row.get("cached_tokens", 0),
                    )
            except Exception:  # noqa: BLE001 - 记账失败不影响本轮结果
                pass
            await self._check_budget_alerts()

        report = outcome.report
        if not report.strip() and outcome.findings:
            # 裁判失败/取消且没流出任何报告文本，但问题清单在：用中间结果拼一份
            # 如实的兜底报告（不再调模型）——已花的钱与已得的裁决不随刷新消失。
            s = adversarial_stats(outcome)
            lines = ["## 对抗审查报告（终审未完成，以下为复核后的中间结果）\n"]
            for f in outcome.findings:
                verdict = {
                    "confirmed": "成立", "refuted": "已推翻",
                    "partial": "部分成立", "pending": "待定",
                }.get(f.verdict, f.verdict)
                lines.append(
                    f"- **{f.finding_id}**（{verdict}）[{f.effective_severity}] "
                    f"{f.category} @ {f.location or '未标注'}：{f.effective_description}"
                )
                if f.solution:
                    lines.append(f"  - 修复方案：{f.solution}")
            lines.append(
                f"\n共 {s['total']} 条候选：成立 {s['confirmed']}、部分成立 "
                f"{s['partial']}、推翻 {s['refuted']}、待定 {s['pending']}。"
            )
            report = "\n".join(lines)
            meta["degraded"] = True

        if report.strip():
            assistant = Message.assistant([TextBlock(text=report)])
            assistant.adversarial = meta
            agent.history.append(assistant)
            await emit_ev(AssistantMessage(message=assistant.model_dump()))

        if outcome.status == "cancelled":
            # 用户中途停止：已流出的部分报告已落库；TurnFinished 由 pipeline
            # 的 stopped 结果收尾。
            await emit_ev(TurnFinished(
                stop_reason="cancelled", iterations=1, duration_ms=_adv_ms(),
            ))
            return meta

        if outcome.status == "error" and not report.strip():
            # 发现者失败且没有任何可保留的产出：如实报错收尾
            await emit_ev(TurnFinished(
                stop_reason="error", iterations=1, duration_ms=_adv_ms(),
            ))
            return meta

        await emit_ev(TurnFinished(
            stop_reason="end_turn", iterations=1, duration_ms=_adv_ms(),
        ))
        return meta

    # ---- 对抗设置（设置 · 对抗）：读回当前值 → 编辑 → 保存后热生效 ----

    def adversarial_detail(self) -> dict:
        """设置页对抗卡片的数据。"""
        adv = self.cfg.adversarial
        return {
            "max_findings": adv.max_findings,
            "role_timeout_s": adv.role_timeout_s,
            "configured_services": len(self._configured_services()),
            "config_hint": (
                "对抗让多个模型以攻防立场接力审查：发现者穷尽式找问题（宁滥勿缺），"
                "调查者逐条对抗验证（力求推翻误报），建议者给代码级修复方案，裁判"
                "（当前主模型）过滤噪音并产出终审报告。成员按勾选顺序承担前三个"
                "角色，不足时自动复用同一个模型——单一模型也能自我对抗。全程纯"
                "文本协作，不调用工具、不写文件。成本约为「角色数 + 1」次调用。"
            ),
        }

    async def adversarial_save(self, params: dict) -> dict:
        """保存对抗设置并热生效（写 config.toml 的 [adversarial] 段）。"""
        updates: dict = {}
        if params.get("max_findings") is not None:
            updates["max_findings"] = max(1, min(100, int(params["max_findings"])))
        if params.get("role_timeout_s") is not None:
            updates["role_timeout_s"] = max(10, int(params["role_timeout_s"]))
        if updates:
            update_config_section("adversarial", updates)
            self.cfg = load_config()
        return self.adversarial_detail()

    # ---- 团队：用户总管的多模型分工协作（一期 MVP，docs/团队模式设计.md） ----
    # 与圆桌并列的第二种多模型协作。活动团队是会话级状态（_teams：sid → 编排器，
    # 内存态）：团队进行中该会话的消息全部进团队频道（用户总管逐个唤醒被点名
    # 成员；AI 总管经 run_auto_turn 驱动自动闭环），工单板只由用户经
    # team.get / team.task_add / team.task_update / team.takeover / team.stop
    # / team.deliver 触达。三期另开三面：频道消息逐条落库（message_sink →
    # team_messages 表，team.log 按 team_id 回放）、建队模板（teams.json，
    # team.template_list/save/remove）、设置页配置卡（teamcfg.get/save）。

    def _load_team_state(self) -> None:
        """重启恢复：上次进程退出时还活动的团队一律标「已中断」（沿任务簿先例）。

        团队的运行时（成员 Agent / 频道 / 工单板）只在内存，不跨进程恢复；
        这里只恢复「曾有个团队」的事实供 team.get 展示，状态文件当场清账。
        """
        if self._team_state_path is None or not self._team_state_path.exists():
            return
        try:
            data = json.loads(read_text_file(self._team_state_path).text)
        except (OSError, ValueError):
            return  # 坏文件按没有活动团队处理（write_text_atomic 保证不会有半个文件）
        for item in (data or {}).get("teams") or []:
            if not isinstance(item, dict) or not item.get("session_id"):
                continue
            rec = dict(item)
            rec["status"] = "interrupted"
            rec["interrupt_reason"] = "应用重启，团队已中断"
            self._teams_interrupted[str(item["session_id"])] = rec
        if self._teams_interrupted:
            self._persist_team_state()  # 已中断的不会再恢复：登记当场清账

    def _persist_team_state(self) -> None:
        """活动团队登记落盘（textio 原子写；失败不拖垮团队本身）。"""
        if self._team_state_path is None:
            return
        data = {"teams": [
            {"session_id": sid,
             "roster": [m.name for m in orch.roster],
             "director_mode": orch.director_mode}
            for sid, orch in self._teams.items()
        ]}
        try:
            write_text_atomic(
                self._team_state_path,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError:
            pass

    def _teams_interrupt_all(self, reason: str) -> None:
        """把所有活动团队就地标中断（项目切换：旧项目 runtime 全部失效，团队一并失效）。"""
        for sid, orch in self._teams.items():
            self._teams_interrupted[sid] = {
                "session_id": sid,
                "roster": [m.name for m in orch.roster],
                "director_mode": orch.director_mode,
                "status": "interrupted",
                "interrupt_reason": reason,
            }
        self._teams.clear()
        self._persist_team_state()

    def _resolve_team_members(self, members_params: list | None) -> list[TeamMemberSpec]:
        """解析团队队员：显式列表优先（可带 name / persona）；缺省沿圆桌的自动解析。

        显式成员与圆桌同名同模型去重；单个成员构建失败（缺 Key/未知服务）不阻断，
        入册为错误成员、唤醒时按失败隔离呈现（与圆桌错误卡片同姿态）。缺省路径
        chair_answers=False：团队没有主席模型，当前主模型不参与过滤。
        """
        specs: list[TeamMemberSpec] = []
        if members_params:
            seen: set[tuple[str, str]] = set()
            for m in members_params:
                name = str((m or {}).get("provider", "")).strip()
                if not name:
                    continue
                model = str((m or {}).get("model", "") or "").strip()
                key = (name, model)
                if key in seen:
                    continue
                seen.add(key)
                spec = TeamMemberSpec(
                    name=str((m or {}).get("name", "") or "").strip(),
                    provider_name=name,
                    model=model,
                    persona=str((m or {}).get("persona", "") or "").strip(),
                )
                try:
                    provider = self._build_member_provider(name, model)
                    spec.provider = provider
                    if not spec.model:
                        spec.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 构建失败降级为错误成员
                    spec.build_error = str(e)[:200]
                specs.append(spec)
        else:
            for ms in self._resolve_members(None, chair_answers=False):
                specs.append(TeamMemberSpec(
                    provider_name=ms.provider_name, model=ms.model,
                    provider=ms.provider, build_error=ms.build_error,
                ))
        return specs

    def _team_budget_check(self) -> Callable[[], bool]:
        """团队的预算越线判定钩子（二期）：接既有每日 token 预算护栏路径。

        与 send() 入口的护栏同口径：cfg.daily_token_budget > 0 且当日
        usage_log 汇总（store.usage_today）已达上限即「越线」；演示模式不产生
        真实费用，豁免同既有判定。读不到用量按未越线放行——坏掉的仪表不该
        停掉团队，费用护栏由宿主其余路径兜底（编排器对钩子异常同样按未越线
        处理，这里不吞第二次）。异步实现：usage_today 是 async，编排器的
        _budget_over 对 awaitable 返回值照常等待。
        """

        async def check() -> bool:
            budget = int(self.cfg.daily_token_budget or 0)
            if budget <= 0:
                return False
            if getattr(self.provider, "demo_mode", False):
                return False
            used_today = await self.store.usage_today()
            return used_today >= budget

        return check

    def _team_message_sink(self, sid: str) -> TeamMessageSink:
        """团队频道消息落库钩子（三期）：接到当前项目库的 team_messages 表。

        编排器在每条频道消息定稿后回调（team.py 的 _persist_message，含被
        停止的取消收尾路径）；每会话一个闭包，session_id 在建队时定死。store
        在回调时才取（晚绑定：测试经 _store_override 注入的实例、setup 的
        重建都能取到当前那一个）。落库是 best-effort 旁路：失败记 obs 警告
        不外溢（编排器对 sink 异常同样只记日志）——不能让落库问题拖垮团队
        轮；被用户停止取消打断的那次按容忍丢失处理（CancelledError 原样
        上抛，不吞取消）。
        """

        async def sink(team_id: str, msg: ChannelMessage) -> None:
            store = self.store
            if store is None:
                return
            try:
                await store.add_team_message(
                    team_id, seq=msg.seq, from_member=msg.from_member,
                    to_member=msg.to_member, msg_kind=msg.msg_kind,
                    task_ref=msg.task_ref, text=msg.text,
                    session_id=sid, created_at=msg.ts,
                )
            except Exception as e:  # noqa: BLE001 - 落库失败不拖垮团队轮
                obs_warning(
                    "team", "team message persist failed",
                    session_id=sid, team_id=team_id, seq=msg.seq,
                    error=str(e)[:200],
                )

        return sink

    async def _team_create(
        self, sid: str, emit: EmitFn, members_params: list | None, recorder: ChangeRecorder,
        director_mode: str = "user", director_params: dict | None = None,
    ) -> TeamOrchestrator:
        """为本会话建队（chat.send team=true 且无活动团队）：发 TeamStarted 并登记。

        emit 是编排器形状的事件回调（收 pydantic 事件，_team_body 传入的
        team_emit 已带用量归属）。成员 Agent 的工具表用本会话同款全量注册表
        （改动记录进同一 recorder，队员写的文件照常进会话检查点）；权限门注入
        会话既有门，编排器经 TeamMemberGate 只加「来自队员」来源标注、判定零改变。
        成员 Agent 另透传本会话 id（写租约归属）与用户钩子 / Mods 拦截——
        对队员与主会话同款生效，都只能收紧。

        director_mode="ai"（二期）：director_params 指定担任总管的
        {provider, model}，用 _build_member_provider 独立实例化（缺 Key 等
        构建失败降级 provider=None + build_error，不阻断建队——首个总管轮
        触达时按失败隔离发注记，团队保持活动等插话重试/接管/收队）；总管
        未指定（provider 名为空）按 fail-closed 在建队期报 TeamError。预算
        越线判定注入 _team_budget_check（越线强制交付 budget_exhausted）。
        频道消息落库（三期）注入 _team_message_sink：每条定稿消息进项目库
        team_messages 表，team_id 进快照随 meta 落库（team.log 凭它回放）。
        """
        director: DirectorSpec | None = None
        if director_mode == "ai":
            d_provider = str((director_params or {}).get("provider", "") or "").strip()
            d_model = str((director_params or {}).get("model", "") or "").strip()
            if not d_provider:
                raise TeamError(
                    'director_mode="ai" 需要指定担任总管的模型服务（director.provider）'
                )
            director = DirectorSpec(provider_name=d_provider, model=d_model)
            try:
                provider = self._build_member_provider(d_provider, d_model)
                director.provider = provider
                if not director.model:
                    director.model = getattr(provider, "model", "")
            except Exception as e:  # noqa: BLE001 - 构建失败降级，不阻断建队
                director.build_error = str(e)[:200]
        specs = self._resolve_team_members(members_params)
        orch = TeamOrchestrator(
            emit=emit,
            working_dir=self.working_dir,
            registry=self._build_full_registry(recorder),
            gate=self.gate,
            cfg=self.cfg.team,
            max_iterations=self.cfg.subagent_max_iterations,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
            session_id=sid,
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
            hooks=self.hooks,
            mods=self.mods,
            director_mode=director_mode,
            director=director,
            budget_check=self._team_budget_check(),
            # 频道消息逐条落库（三期）：收队/重启后仍可按 team_id 回放全量
            message_sink=self._team_message_sink(sid),
        )
        await orch.create_team(specs)  # TeamStarted 在这里发出；超上限截断发 Notice
        self._teams[sid] = orch
        self._teams_interrupted.pop(sid, None)
        self._persist_team_state()
        return orch

    @staticmethod
    def _team_meta(orch: TeamOrchestrator) -> dict:
        """assistant 消息的团队 meta：{"mode": "team", "team": {…}}，仿 roundtable meta。

        Message 的元数据槽只有 roundtable 一个（messages.py 未设通用 meta 字段），
        团队摘要搭这个槽随会话落库/下发，前端按 mode == "team" 与圆桌卡分流。
        team 块自带建队分配的 team_id（三期）：前端回放全量频道消息
        （team.log）就凭 meta 里这个 id，收队/重启后仍然可用。
        """
        return {"mode": "team", "team": orch.snapshot()["team"]}

    async def _team_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent, sid: str, images: list[ImageBlock] | None = None,
        allow_create: bool = False, recorder: ChangeRecorder | None = None,
        plan_mode: bool = False,
        director_mode: str = "user", director: dict | None = None,
    ) -> dict:
        """团队轮主体：用户消息进团队频道 → 总管推进（成员唤醒 / 自动闭环）。

        - 该会话已有活动团队：消息按「@成员名」定向 / 广播进频道；
        - 没有活动团队且 allow_create：先建队（TeamStarted）再进频道。

        总管形态（二期）：user（默认）走一期用户总管——被 @ 点名的成员逐个
        唤醒；ai 走自动闭环——消息经 run_auto_turn 驱动总管（拆解 → 派工 →
        验收 → 交付）至空闲或终态；循环推进中（loop_running，渠道入站等不占
        运行位的消息源会撞上）改走 inject_user_message：只进频道并排进总管
        下一轮的最高优先级段，不重复驱动循环（在飞轮任务负责推进）。

        规划模式对团队轮同样生效（plan_mode 本轮值）：激活时成员一律按只读
        工具表唤醒（即使名下有执行型工单，set_plan_mode 现筛只读表），并发
        Notice 说明——「本轮不执行任何写操作」的用户契约不因协作模式破例。

        成员/总管发言以 TeamMessageDelta 流式下发（StreamDeltaMerger 按
        member_index + seq 分桶合并，member_index=-1 即总管），定稿
        TeamMessage；转发的工具调用 / 权限请求等过程事件是非增量，merger
        自动先冲刷缓冲再发出，顺序与普通轮一致。取消语义：编排器把半截发言
        shield 定稿进频道后原样上抛，这里补齐用量入账与 meta 落库再交还外层
        收口为 stopped。

        返回随轮次结果回传前端的团队元数据（同时作为 assistant 消息 meta 落库）。
        """
        orch = self._teams.get(sid)

        async def emit_ev(ev) -> None:
            await emit(ev.model_dump())

        # 用量按事件流归属：usage 事件先于其定稿 TeamMessage 到达，攒到定稿时
        # 记到名下（MemberTurnResult 的用量不随返回值交给宿主，事件流是逐成员
        # 归属的唯一来源）。总管轮与小会主席的 usage 先于 from="director" 的
        # 定稿到达——冲进 director_acc，行键 provider/model 取 director_info
        # （_flush_team_usage），不与队员混账。极端取消时序下剩余用量挂给最后
        # 定稿的归属方，不凭空丢账。
        usage_acc: dict[str, list[int]] = {}  # 成员名 -> [input, output]
        director_acc = [0, 0]  # 总管（AI 模式）累计 [input, output]
        pending_usage: list = []

        async def team_emit(ev) -> None:
            if ev.kind == "usage":
                pending_usage.append(ev)
            elif ev.kind == "team_message" and ev.from_member == "director":
                if orch is not None and orch.director_mode == "ai":
                    # 总管轮与小会主席的 usage 先于 from="director" 的定稿到达
                    # ——冲进 director_acc，行键 provider/model 取 director_info
                    # （_flush_team_usage），不与队员混账。仅在 AI 总管模式生效：
                    # user 模式的 director 消息是系统代发的派工/验收话术（没有
                    # 总管模型），不冲账——窗口内的成员用量留给成员自己的定稿或
                    # 轮末 _flush_team_usage 按最后归属结算，不结出 provider=""
                    # /model="" 的幽灵总管账、成员名下不丢账。
                    for u in pending_usage:
                        director_acc[0] += int(u.input_tokens)
                        director_acc[1] += int(u.output_tokens)
                    pending_usage.clear()
            elif (ev.kind == "team_message"
                  and ev.from_member not in ("user", "system", "director")):
                bucket = usage_acc.setdefault(ev.from_member, [0, 0])
                for u in pending_usage:
                    bucket[0] += int(u.input_tokens)
                    bucket[1] += int(u.output_tokens)
                pending_usage.clear()
            elif (ev.kind == "team_message" and ev.from_member == "system"
                  and pending_usage and orch is not None and orch.director_mode == "ai"):
                # 小会融合失败只有 from=system 的注记收尾（没有裁定定稿冲账）：
                # 此时未冲账的用量只可能是小会主席的——成员/总管轮的用量都随
                # 各自定稿消息冲账。主动结算进总管账，别让轮末兜底把主席的
                # token 记到队尾成员头上（与 _director_usage/snapshot 失协）。
                for u in pending_usage:
                    director_acc[0] += int(u.input_tokens)
                    director_acc[1] += int(u.output_tokens)
                pending_usage.clear()
            await emit_ev(ev)

        t0 = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        await emit_ev(TurnStarted(iteration=1))
        # 规划模式对团队轮同样生效（一期评审遗留）：本轮值同步给编排器（建队
        # 路径在下方建完再同步），激活时并发 Notice 说明只读执行——每轮都发，
        # 团队进行中切换规划模式的行为对用户始终可见。
        if orch is not None:
            orch.set_plan_mode(plan_mode)
        if plan_mode:
            await emit_ev(NoticeEvent(
                message="规划模式已开启：本轮队员以只读方式执行（不写文件、不运行命令），"
                        "只做调研与汇报；需要队员动手请切回执行模式后再发消息。"
            ))
        # 用户消息先入主历史（与圆桌同姿态）：团队期间的问题不丢，收队恢复
        # 普通回合后后续轮仍有上下文；图片随消息保留（队员不看图）。
        agent.history.append(Message.user(text, images))

        try:
            if orch is None:
                if not allow_create:
                    # 理论不可达（pipeline 入口保证 team 或活动团队至少一个成立）
                    raise RuntimeError("当前会话没有进行中的团队")
                orch = await self._team_create(
                    sid, team_emit, members_params, recorder or ChangeRecorder(),
                    director_mode=director_mode, director_params=director,
                )
                orch.set_plan_mode(plan_mode)
            else:
                # 逐轮重绑发射链（仅空闲轮）：编排器只在建队轮拿到过 emit，
                # 第二条消息起事件与用量会流进建队轮早已收尾的旧闭包——前端因
                # 同连接碰巧可见，usage_log 的归属却静默丢失。本轮要驱动编排器
                # （唤醒/自动循环），事件与用量就必须挂本轮的 team_emit。
                # 插话路径例外（loop_running=True，在飞轮正持有发射链）：重绑
                # 会把在飞轮的用量挪进本轮早已冲刷完的账本——保持不重绑。
                if not orch.loop_running:
                    orch._emit = team_emit
            if orch.director_mode == "ai":
                if orch.loop_running:
                    # 循环推进中的插话：只进频道并排进总管下一轮（最高优先级段），
                    # 不重复驱动循环。若 inject 返回时循环恰好已收束（竞态窗口），
                    # 插话留在待注入清单里，随本会话下一条消息一并处理
                    result = await orch.inject_user_message(text, images=images)
                else:
                    result = await orch.run_auto_turn(text, images=images)
            else:
                result = await orch.handle_user_message(text, images=images)
        except asyncio.CancelledError:
            # 用户停止：半截发言已在编排器里 shield 定稿进频道（seq 复用无空洞）。
            # 这里把已花费的用量入账、meta 与纪要落进主历史，再上抛交给 pipeline
            # 的 stopped 收尾（冲刷合并缓冲 + 落库）。
            await self._flush_team_usage(
                sid, orch, usage_acc, pending_usage, director_acc=director_acc,
            )
            if orch is not None:
                stopped_msg = Message.assistant([TextBlock(
                    text="（本轮已停止：正在发言的队员已把已产出的部分定稿进团队频道。）",
                )])
                stopped_msg.roundtable = self._team_meta(orch)
                agent.history.append(stopped_msg)
                for ev in (
                    AssistantMessage(message=stopped_msg.model_dump()),
                    TurnFinished(stop_reason="cancelled", iterations=1, duration_ms=_ms()),
                ):
                    try:
                        await emit_ev(ev)
                    except asyncio.CancelledError:
                        pass  # 收尾帧发不出就算了：历史与落库不丢
            raise
        except TeamError:
            # 参数/状态非法（如解析不出任何队员）：与圆桌同姿态补 TurnFinished(error)
            # 让前端轮次状态收口，错误原文上抛给 WS 层。若团队已在轮中进终态
            # （轮次耗尽/预算越线等），用量照常结算、活动登记照常摘除——否则
            # 已收队的团队永远挂在 _teams 里，该会话此后每条消息都报「当前
            # 没有进行中的团队」，只能手动收队解困。
            await self._flush_team_usage(
                sid, orch, usage_acc, pending_usage, director_acc=director_acc,
            )
            if orch is not None and orch.finished is not None:
                self._teams.pop(sid, None)
                self._teams_interrupted.pop(sid, None)
                self._persist_team_state()
            await emit_ev(TurnFinished(stop_reason="error", iterations=1, duration_ms=_ms()))
            raise

        await self._flush_team_usage(
            sid, orch, usage_acc, pending_usage, director_acc=director_acc,
        )
        finished = result.get("finished")
        if finished is not None:
            # 终态（交付确认 / 收队 / 轮次耗尽）：团队关闭，会话恢复普通回合
            self._teams.pop(sid, None)
            self._teams_interrupted.pop(sid, None)
            self._persist_team_state()

        meta = self._team_meta(orch)
        digest = Message.assistant([TextBlock(text=_team_digest(orch, result))])
        digest.roundtable = meta
        agent.history.append(digest)
        await emit_ev(AssistantMessage(message=digest.model_dump()))
        await emit_ev(TurnFinished(stop_reason="end_turn", iterations=1, duration_ms=_ms()))
        return meta

    async def _flush_team_usage(
        self, sid: str | None, orch: TeamOrchestrator | None,
        usage_acc: dict[str, list[int]], pending_usage: list,
        director_acc: list[int] | None = None,
    ) -> None:
        """团队轮的用量入账 usage_log（归属当前会话，仿圆桌 usage_rows）。

        逐成员各一行；AI 总管模式另结一行挂总管名下（provider/model 取
        director_info，不挂在任何队员名下——总管轮与小会主席的用量经
        team_emit 的 from="director" 分支攒进 director_acc）。
        """
        if orch is None or not sid:
            return
        if pending_usage:
            bucket: list[int] | None = None
            if usage_acc:
                last = next(reversed(usage_acc))
                bucket = usage_acc[last]
            elif director_acc is not None and orch.director_mode == "ai":
                # AI 模式下还没有任何成员定稿过：剩余用量多半是被取消的总管轮的
                bucket = director_acc
            if bucket is not None:
                for u in pending_usage:
                    bucket[0] += int(u.input_tokens)
                    bucket[1] += int(u.output_tokens)
                pending_usage.clear()
        rows = [
            (spec.provider_name, spec.model, acc[0], acc[1])
            for spec in orch.roster
            if (acc := usage_acc.get(spec.name)) and (acc[0] or acc[1])
        ]
        if director_acc is not None and (director_acc[0] or director_acc[1]):
            info = orch.director_info()
            rows.append((info["provider"], info["model"], director_acc[0], director_acc[1]))
        if not rows:
            return
        try:
            for provider, model, tin, tout in rows:
                await self.store.add_usage(sid, provider, model, tin, tout, 0)
        except Exception:  # noqa: BLE001 - 记账失败不影响本轮结果
            pass
        # 团队烧的真金白银也要触发越线提醒（内部不抛）
        await self._check_budget_alerts()

    def _team_for(self, params: dict) -> tuple[str, TeamOrchestrator]:
        """按 params.session_id（缺省活动会话）取活动团队；没有就报参数错。"""
        sid = str(params.get("session_id") or "") or (self.session.id if self.session else "")
        orch = self._teams.get(sid)
        if orch is None:
            raise TeamError("当前会话没有进行中的团队")
        return sid, orch

    async def _cancel_team_turn_and_settle(self, sid: str) -> None:
        """取消该会话正在跑的轮，并等它把取消收尾跑完（team.stop / team.deliver 专用）。

        时序前提（一期评审遗留）：被取消成员轮的半截发言要 shield 定稿进频道、
        team_message 帧要先于 team_finished 到达、终态 snapshot 不能缺这条发言
        ——所以先「取消 + 等收尾」（定稿 / 合并缓冲冲刷 / 落库都落地），再走
        finish() 生成快照，不靠前端幂等兜底。等待期间的再次取消吞掉（收队语义
        已定，与 pipeline 收尾的取向一致）；兜底超时不无限等——收队不能被卡死
        的轮拖住，超时记警告后照常收队。
        """
        rt = self.runtimes.get(sid)
        task = rt.run_task if rt else None
        if task is None or task.done():
            return
        self.cancel_run(sid)  # 上面的 done 检查保证这里必是活任务
        deadline = time.monotonic() + _PERSIST_HARVEST_TIMEOUT
        while not task.done() and time.monotonic() < deadline:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()  # 收队请求被再次取消：吞掉继续等
            except TimeoutError:
                pass  # 到点再看一眼：deadline 兜底，不无限等
            except Exception:  # noqa: BLE001 - 被取消轮自身的错误在它的调用链上处理
                pass
        if not task.done():
            obs_warning(
                "team",
                f"cancelled turn did not settle in time before team finish, sid={sid}",
                session_id=sid,
            )

    def team_get(self, params: dict) -> dict:
        """team.get：团队快照（名册 / 工单 / 频道尾部）。

        无活动团队时返回空态；该会话有「已中断」记录（重启/切项目前留过团队）
        时附带 interrupted 记录供前端提示。
        """
        sid = str(params.get("session_id") or "") or (self.session.id if self.session else "")
        orch = self._teams.get(sid)
        if orch is not None:
            return {"active": True, "session_id": sid, "team": orch.snapshot()["team"]}
        out: dict = {"active": False, "session_id": sid, "team": None}
        if sid in self._teams_interrupted:
            out["interrupted"] = self._teams_interrupted[sid]
        return out

    async def team_task_add(self, params: dict) -> dict:
        """team.task_add：登记工单（待办）。指派对象必须在名册内，依赖必须存在。"""
        _, orch = self._team_for(params)
        deps = params.get("deps")
        task = await orch.task_add(
            str(params.get("title", "")),
            str(params.get("assignee", "")),
            type=str(params.get("type") or "exec"),
            accept=str(params.get("accept", "") or ""),
            deps=[str(d) for d in deps] if isinstance(deps, list) else None,
        )
        return {"task": task}

    async def team_task_update(self, params: dict) -> dict:
        """team.task_update：工单状态流转（板只由用户变更，成员不自行改板）。

        待办→进行中→待验收→完成；待验收→进行中视为打回（redo+1，超上限拒绝）；
        status="error" 走失败收尾（用户放弃，待办/进行中/待验收均可）。置为
        进行中（派工/重派/打回重做）后代发一条 director 的 assign 频道消息，
        让被派队员在频道里看到派工。
        """
        _, orch = self._team_for(params)
        status = str(params.get("status", ""))
        task = await orch.task_update(str(params.get("task_id", "")), status)
        assign_msg = None
        if status == "in_progress":
            assign_msg = await orch.notify_assign(
                str(task["id"]), note=str(params.get("note", "") or "")
            )
        return {"task": task, "assign_message": assign_msg}

    async def team_stop(self, params: dict) -> dict:
        """team.stop：收队（立即终止）——TeamFinished(aborted) + 未尽事项。

        正在跑的成员轮先取消并**等收尾完成**（半截发言 shield 定稿进频道、
        team_message 帧先于 team_finished 到达，快照不缺这条发言，见
        _cancel_team_turn_and_settle）；收队后该会话恢复普通回合。
        """
        sid, orch = self._team_for(params)
        await self._cancel_team_turn_and_settle(sid)
        result = await orch.stop(reason=str(params.get("reason", "") or ""))
        self._teams.pop(sid, None)
        self._persist_team_state()
        return {
            "status": result["status"],
            "summary": result["summary"],
            "session_id": sid,
            "team": result["snapshot"]["team"],
        }

    async def team_deliver(self, params: dict) -> dict:
        """team.deliver：交付（用户确认）——TeamFinished(done) + 《交付说明》。

        设计 §3 的停机条件「总管宣布交付」，一期总管是用户本人：这是全部
        工单完成后团队唯一的正常终态路径（收队恒为 aborted，轮次耗尽是
        强制交付）。未完成的工单不拦——按《交付说明》记未尽事项；正在跑的
        成员轮先取消并等收尾完成（同收队的时序前提，见
        _cancel_team_turn_and_settle），交付后该会话恢复普通回合。
        """
        sid, orch = self._team_for(params)
        await self._cancel_team_turn_and_settle(sid)
        result = await orch.finish(status="done")
        self._teams.pop(sid, None)
        self._persist_team_state()
        return {
            "status": result["status"],
            "summary": result["summary"],
            "session_id": sid,
            "team": result["snapshot"]["team"],
        }

    async def team_takeover(self, params: dict) -> dict:
        """team.takeover：用户接管（二期 AI 总管）——切回用户总管模式。

        正在推进的自动闭环（总管轮/成员唤醒轮）先取消并**等收尾完成**（半截
        发言 shield 定稿进频道、team_message 帧先于后续帧到达，快照不缺这条
        发言，见 _cancel_team_turn_and_settle）；随后编排器切 director_mode
        ="user"：未消费的插话与待开小会作废、停滞标记清零，工单板与团队频道
        原样保留，成员 Agent（独立 history）全部复用——此后 handle_user_message
        照常（一期行为）。
        """
        sid, orch = self._team_for(params)
        await self._cancel_team_turn_and_settle(sid)
        result = await orch.takeover()
        self._persist_team_state()  # director_mode 变了：活动登记同步落盘
        return {
            "director_mode": result["director_mode"],
            "session_id": sid,
            "team": result["snapshot"]["team"],
        }

    # ---- 团队设置（设置 · 团队）：读回当前值 → 编辑 → 保存后热生效 ----

    def team_config_detail(self) -> dict:
        """设置页团队卡片的数据（三期，仿 roundtable_detail）。"""
        t = self.cfg.team
        return {
            "max_members": t.max_members,
            "member_timeout_s": t.member_timeout_s,
            "max_rounds": t.max_rounds,
            "redo_limit": t.redo_limit,
            "stall_limit": t.stall_limit,
            "config_hint": (
                "团队让多个模型分工协作：总管拆解目标、登记工单，队员按工单真实"
                "读写文件、运行命令（照常走权限确认），完成后交付。成员来自「模型"
                "服务」里已配好 Key 的服务，在输入框的团队面板勾选并写一句话人设；"
                "下面的上限用于防止自动循环失控。改动对之后新建的团队生效，进行中"
                "的团队沿用建队时的值。"
            ),
        }

    async def team_config_save(self, params: dict) -> dict:
        """保存团队设置（写 config.toml 的 [team] 段）并回传新状态（clamp 仿
        roundtable_save）。热生效指此后新建的团队取新值；进行中团队的轮次
        上限等在建队时已取值，不回改。"""
        updates: dict = {}
        if params.get("max_members") is not None:
            updates["max_members"] = max(1, min(8, int(params["max_members"])))
        if params.get("member_timeout_s") is not None:
            updates["member_timeout_s"] = max(10, int(params["member_timeout_s"]))
        if params.get("max_rounds") is not None:
            updates["max_rounds"] = max(1, int(params["max_rounds"]))
        if params.get("redo_limit") is not None:
            updates["redo_limit"] = max(0, min(5, int(params["redo_limit"])))
        if params.get("stall_limit") is not None:
            updates["stall_limit"] = max(1, int(params["stall_limit"]))
        if updates:
            update_config_section("team", updates)
            self.cfg = load_config()
        return self.team_config_detail()

    # ---- 团队模板（三期）：把一次建队配置存成可复用的具名条目 ----
    # 全局册（与会话无关，teams.json 单文件）：前端建队浮层「从模板创建」、
    # 团队卡「另存为模板」。名字/总管形态的校验在 TeamTemplateStore.upsert，
    # TeamError 原文回 WS 调用方。

    def team_template_list(self) -> dict:
        """team.template_list：全部模板（按保存顺序；member 字段见契约）。"""
        return {"templates": self.team_templates.list()}

    async def team_template_save(self, params: dict) -> dict:
        """team.template_save：保存模板。name 是唯一键——同名覆盖原条目
        （位置不变、内容换新，created_at 刷新为本次保存时刻），新名追加。
        成员行只存 (provider, model, name, persona) 标识，不校验服务存在性
        （应用模板时走 chat.send 既有解析路径，缺 Key 照常降级错误成员）。"""
        raw_members = params.get("members")
        members = [
            TeamTemplateMember(
                provider=str((m or {}).get("provider", "") or "").strip(),
                model=str((m or {}).get("model", "") or "").strip(),
                name=str((m or {}).get("name", "") or "").strip(),
                persona=str((m or {}).get("persona", "") or "").strip(),
            )
            for m in (raw_members if isinstance(raw_members, list) else [])
            if isinstance(m, dict)
        ]
        raw_director = params.get("director")
        template = TeamTemplate(
            name=str(params.get("name", "") or ""),
            director_mode=str(params.get("director_mode") or "user"),
            director=TeamTemplateDirector(
                provider=str((raw_director or {}).get("provider", "") or "").strip(),
                model=str((raw_director or {}).get("model", "") or "").strip(),
            ),
            members=members,
            created_at=time.time(),  # 宿主盖戳（模板册自身不感知时钟）
        )
        self.team_templates.upsert(template)
        return {"template": template.model_dump()}

    async def team_template_remove(self, params: dict) -> dict:
        """team.template_remove：按名删模板（名字精确匹配；不存在报 TeamError）。"""
        name = str(params.get("name", "") or "").strip()
        self.team_templates.remove(name)
        return {"name": name}

    # ---- 频道回放（三期）：收队/重启后按 team_id 拉全量频道消息 ----

    async def team_log(self, params: dict) -> dict:
        """team.log：按 team_id 回放某团队的全部频道消息（team_messages 表）。

        会话归属一致性校验（一致性校验，安全收敛在 WS 入口——非本机调用的
        session_id 已在 app.py 收敛到该连接绑定的活动会话，本方法按服务端侧
        认定的 sid 比对，请求原值不参与）：活动团队直接比对 sid → orch.team_id；
        团队已收队/应用重启后凭 assistant meta 里的 team.team_id 回放，按库内
        落库行的 session_id 归属判定（落库行由 _team_message_sink 戳上建队
        会话）。两证皆无（旧版本团队没落过库 / 未知 id）不拒绝——回空列表，
        前端退回截断 meta；空结果没有可泄露的内容。行字段与列序即
        store.list_team_messages。
        """
        sid = str(params.get("session_id") or "") or (self.session.id if self.session else "")
        team_id = str(params.get("team_id", "") or "").strip()
        if not team_id:
            raise TeamError("缺少 team_id：请从团队 meta 的 team.team_id 取回放主轴")
        orch = self._teams.get(sid)
        active_hit = orch is not None and bool(orch.team_id) and orch.team_id == team_id
        rows = (
            await self.store.list_team_messages(team_id) if self.store is not None else []
        )
        owned = bool(sid) and any(r.get("session_id") == sid for r in rows)
        foreign = any(r.get("session_id") and r.get("session_id") != sid for r in rows)
        if not active_hit and not owned and foreign:
            raise TeamError("team_id 不属于当前会话：不能回放其他会话的团队频道")
        return {"team_id": team_id, "session_id": sid, "messages": rows}

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
        # registry 为 None = runtime 已被 _forget_runtime 释放（切项目/删会话时
        # 被取消的轮还在收尾）：此时拿不到工具清单，按空表估算，别让收尾炸掉
        schemas = agent.registry.schemas() if agent.registry is not None else []
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

    async def status(self, session_id: str | None = None) -> dict:
        """/status：模型、上下文占用、工具数、任务清单一览。

        session_id 给出时查那个会话的 runtime（分屏列辅路：peek 不建 runtime）。
        无 runtime（重启 / LRU 淘汰后）不直接报错：会话在库时按落库消息估算
        占用回给上下文徽标——否则打开旧会话的列要等该列发过一条消息才有读数；
        估算口径与 agent.used_context_tokens 的估算半边一致。会话不在库仍显式
        报错，绝不静默回退当前会话（前端据此保持徽标空态）。"""
        if session_id:
            rt = self.runtimes.get(session_id)
            if rt is None:
                est = await self._estimate_session_context(session_id)
                if est is None:
                    raise RuntimeError(
                        f"该会话没有正在运行的上下文（可能已被回收）：{session_id}")
                tokens, history_n = est
                return {
                    "version": __version__,
                    "working_dir": str(self.working_dir or ""),
                    "provider": self.provider_name,
                    "model": self.provider_model,
                    "provider_error": self.provider_error,
                    "session_id": session_id,
                    "context_tokens": tokens,
                    "context_limit": self._context_limit(),
                    # 明细桶（系统提示词/工具/技能分摊）离线还原不了：给 None，
                    # 前端容量弹层按无明细处理，环读数本身不受影响
                    "context_detail": None,
                    "history_messages": history_n,
                    "tool_count": len(self.agent.registry),
                    "queued": 0,
                    "todos": [],
                }
            agent, sid, queued = rt.agent, session_id, len(rt.queue)
        else:
            agent = self.agent
            sid = self.session.id if self.session else None
            queued = len(self.queue)
        todo_tool = agent.registry.get("todo_write")
        return {
            "version": __version__,
            "working_dir": str(self.working_dir or ""),
            "provider": self.provider_name,
            "model": self.provider_model,
            "provider_error": self.provider_error,
            "session_id": sid,
            "context_tokens": agent.used_context_tokens(),
            "context_limit": agent.context_limit_tokens,
            "context_detail": self._context_detail(agent),
            "history_messages": len(agent.history),
            "tool_count": len(agent.registry),
            "queued": queued,
            "todos": list(getattr(todo_tool, "items", []) or []),
        }

    async def _estimate_session_context(self, session_id: str) -> tuple[int, int] | None:
        """无 runtime 会话的上下文占用估算（分屏列徽标辅路）。

        会话不在库、或没有任何落库消息（刚建的空会话 / 任务壳会话）返回 None，
        由调用方维持显式报错的空态契约；有历史则按落库消息以 estimate_tokens
        估算（与 agent.used_context_tokens 的估算半边同口径），返回 (tokens, 条数)。"""
        if self.store is None:
            return None
        session = await self.store.get_session(session_id)
        if session is None:
            return None
        msgs = await self.store.load_messages(session_id)
        if not msgs:
            return None
        return estimate_tokens(msgs), len(msgs)

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
        # get/restore 都是重量级同步磁盘活（读 blob、全文件哈希比对、原子写回；
        # 单条上限 256MB），与 save 一样包进线程跑，大检查点不冻结事件循环。
        # CheckpointConflictError / KeyError 可正常穿过 to_thread 传回。
        cp = await asyncio.to_thread(self.checkpoints.get, checkpoint_id)
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
            files = await asyncio.to_thread(
                self.checkpoints.restore, checkpoint_id, force=force
            )
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
        # get（读 blob）与逐文件对比（读磁盘 + difflib）都离环跑，见 restore_checkpoint
        cp = await asyncio.to_thread(self.checkpoints.get, checkpoint_id)
        if cp is None:
            raise RuntimeError(
                f"检查点 {checkpoint_id} 不存在或已过期（每会话保留最近 50 轮，更早的会被淘汰）"
            )
        # 同 restore：凭枚举 id 不能读别的项目会话的文件快照（B12）
        await self._check_checkpoint_ownership(cp)
        files = await asyncio.to_thread(_checkpoint_diff_files, cp)
        return {"id": checkpoint_id, "ts": cp["ts"], "files": files}

    # ---- 轮次诊断（审查页：「这一轮为什么慢」的 UI 兑现） ----

    async def turn_breakdown(self, session_id: str = "", limit: int = TURN_LOG_LIMIT_DEFAULT) -> dict:
        """从桌面日志的结构化行里取回本会话最近 N 轮的耗时拆解。

        数据源是 obs.py 轮末记的 ``ev=turn`` 行（~/.skysheep/logs/desktop.log）：
        只读当前文件、不追轮转旧文件；日志文件不存在（如 CLI 模式没接文件日志）
        返回空列表而不是报错。字段缺失容忍——旧版本日志行没有 slowest_tool 等
        字段时相应位置就是 null。
        """
        sid = str(session_id or "").strip() or (self.session.id if self.session else "")
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = TURN_LOG_LIMIT_DEFAULT
        n = max(1, min(n, TURN_LOG_LIMIT_MAX))
        log_path = skysheep_home() / "logs" / "desktop.log"
        turns: list[dict] = []
        if log_path.exists():
            turns = await asyncio.to_thread(_load_turn_rows, log_path, sid)
        return {"session_id": sid, "limit": n, "turns": turns[-n:]}


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
        self._sync_default_accept_to_runtimes()

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

    @staticmethod
    def snippet_initials(name: str) -> str:
        """提示词名称的拼音首字母串（~ 菜单首字母过滤用）。

        pypinyin 缺席或意外失败时退空串：过滤退化为按名称/内容子串，不影响其他功能。
        """
        try:
            from pypinyin import Style, lazy_pinyin

            return "".join(lazy_pinyin(name or "", style=Style.FIRST_LETTER)).lower()
        except Exception:  # noqa: BLE001 - 首字母是锦上添花，不值得为它报错
            return ""

    async def snippets_polish(self, params: dict) -> dict:
        """让当前模型把一条提示词改写得更清晰、更结构化（一次性调用，不落库）。

        与 _map_generate_run 同款边界：没有可用模型直接报错；结果只回给前端
        展示——用户看过、改过、点保存才生效。{{...}} 占位符要求模型原样保留。
        """
        text = str(params.get("content", "") or "").strip()
        if not text:
            raise RuntimeError("没有可润色的内容")
        if not self.provider:
            raise RuntimeError("没有可用模型——先在设置里配好提供商再试")
        prompt = (
            "请把下面这条给 AI 助手的提示词改写得更清晰、更结构化：保留原意与全部细节，"
            "补齐缺失的背景交代，可按「背景 / 任务 / 要求」等小块组织；"
            "{{...}} 形式的占位符必须原样保留、一个都不能删改。"
            "只输出改写后的提示词正文，不要任何解释或代码块围栏。\n\n---\n\n" + text
        )
        try:
            # 整段限时：模型挂住时不能让前端「✨ AI 润色」永远停在「润色中…」。
            # 超时转成可读错误走普通失败路径（TimeoutError 是 asyncio.timeout 的取消信号，
            # 不在这里拦截会让 async for 的清理也跟着丢）。
            async with asyncio.timeout(60):
                out = (await collect_stream_text(self.provider, prompt)).strip()
        except TimeoutError:
            raise RuntimeError("润色超时：模型 60 秒没有返回内容，请稍后重试") from None
        # 宽容剥壳：模型偶尔无视「不要围栏」的叮嘱
        if out.startswith("```"):
            out = out.split("\n", 1)[-1]
        if out.endswith("```"):
            out = out.rsplit("```", 1)[0]
        out = out.strip()
        if not out:
            raise RuntimeError("模型没有返回内容")
        return {"content": out}

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

    def _sync_default_accept_to_runtimes(self) -> None:
        """引擎默认档变化后，同步到「没有会话级覆盖」的 runtime 私有门。

        默认档（permission.set_mode / ui.json 的 accept_edits）是所有未覆盖
        会话的当前值；只有 session.accept_set 显式覆盖过的会话才脱离默认
        （那正是「按会话独立」的语义）。runtime 的门在 _runtime_gate 里是
        私有副本，不随 self.gate 原地变，所以默认档变化要在这里显式追平。"""
        for sid, rt in self.runtimes.items():
            if sid in self._session_accepts:
                continue
            g = rt.agent.gate
            if g is not None and g is not self.gate:
                g.auto_accept_write = self.gate.auto_accept_write
                g.auto_accept_all = self.gate.auto_accept_all

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
        self._sync_default_accept_to_runtimes()
        return {"mode": self.permission_mode()}

    def _accept_mode_of(self, session_id: str) -> str:
        """会话当前生效的权限三档：会话级覆盖优先，未覆盖回引擎默认。"""
        saved = self._session_accepts.get(session_id)
        if saved in (0, 1, 2):
            return {0: "confirm", 1: "accept_edits", 2: "full_access"}[saved]
        return self.permission_mode()

    async def session_accept_get(self, session_id: str) -> dict:
        """会话当前生效的权限三档（主栏/分屏盾牌按面显示用）。

        会话级覆盖（session.accept_set）优先；未覆盖回引擎默认档
        （ui.json 的 accept_edits）。本机专属（local_only）——降防护的
        开关不向远程开放，与 permission.set_mode 同一面。"""
        await self._get_owned_session(session_id, local=True)
        return {
            "session_id": session_id,
            "mode": self._accept_mode_of(session_id),
            "overridden": session_id in self._session_accepts,
            "engine_mode": self.permission_mode(),
        }

    async def session_accept_set(self, session_id: str, mode: str) -> dict:
        """按会话设置权限三档：只影响该会话 runtime 的门，不动引擎默认——
        别的会话（含主栏）保持自己的档位。runtime 已建则热调其私有门；
        还没建只记覆盖，_runtime_gate 在建门时重放。会话删除随清理，
        引擎默认档（permission.set_mode）仍是新会话的起点。

        本机专属（local_only）：放宽档只由本机用户切换（安全审查根因一，
        与 permission.set_mode 同一收口）；调档记日志，事后可追溯。"""
        if mode not in ("confirm", "accept_edits", "full_access"):
            raise RuntimeError("权限模式只支持 confirm / accept_edits / full_access")
        await self._get_owned_session(session_id, local=True)
        v = {"confirm": 0, "accept_edits": 1, "full_access": 2}[mode]
        self._session_accepts[session_id] = v
        rt = self.runtimes.get(session_id)
        if rt is not None and rt.agent.gate is not None:
            rt.agent.gate.auto_accept_write = v in (1, 2)
            rt.agent.gate.auto_accept_all = v == 2
        logger.info(
            "会话 %s 权限档位切换为 %s（自动允许写入=%s，完全访问=%s，工作目录：%s）",
            session_id, mode, v in (1, 2), v == 2, self.working_dir,
        )
        return {"session_id": session_id, "mode": mode}

    # ---- 白名单（设置页）：手动添加 / 清空 / 测试 / 导入导出 ----

    async def list_whitelist_rules(self) -> list[dict]:
        """项目白名单规则列表（whitelist.list），每条带 stale 失效标记。

        stale 判定与 gate 匹配侧 fail-closed 同源，不造第二套判定：
        2.3.0 收紧后 docker/ssh/scp 两词前缀规则不再命中（_is_arbitrary_exec_prefix）；
        更早的审查 S-12 起 delete_file 整工具 always 规则也不再认（gate 的
        _matches_core）——列表把这类「留着也不会再放行」的规则亮出来，提示清理。
        """
        return await self._enriched_rules()

    async def _enriched_rules(self) -> list[dict]:
        """规则列表回读的唯一出口：凡回传 rules 的读路径（whitelist.list /
        whitelist.add / whitelist.enable / whitelist.import）都走这里，
        字段集才一致——add 回传同条规则与随后 list 完全相同。
        """
        if self.project is None:
            return []
        rules = await self.store.list_rules(self.project.id)
        return [self._stale_fields(r) for r in rules]

    @staticmethod
    def _stale_fields(rule: dict) -> dict:
        """就地补 stale / stale_reason 字段（设置页失效徽章的数据源）。"""
        stale = (
            rule.get("tool") == _RUN_COMMAND
            and rule.get("kind") == "prefix"
            and _is_arbitrary_exec_prefix(rule.get("pattern") or "")
        )
        reason = "安全收紧（2.3.0）：该类前缀已回落逐次确认，规则不再命中" if stale else ""
        if not stale and rule.get("tool") == "delete_file" and rule.get("kind") == "always":
            # 与 gate 匹配侧同口径（gate._matches_core 对 delete_file 的整工具
            # 规则一律不认，审查 S-12）：这类存量死规则留着也不会再放行，
            # 亮出来提示清理——放行删除面请按具体路径固化 exact 规则
            stale = True
            reason = ("安全收紧（审查 S-12）：delete_file 不再整工具放行，规则不再命中；"
                      "请按具体路径固化 exact 规则")
        rule["stale"] = stale
        rule["stale_reason"] = reason
        return rule

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
        # 回传与 whitelist.list 同一富化：stale 标记随写即回，字段集两处一致
        return {"rules": await self._enriched_rules()}

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
                "rules": await self._enriched_rules()}

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
            "rules": await self._enriched_rules(),
        }

    # ---- 模型/配置操作 ----

    def _model_private_sessions(self) -> set[str]:
        """模型身份私有的会话：会话级模型覆盖 ∪ 生效的「新会话默认模型」会话。

        这些会话的模型不跟全局（全局切换 model.switch 对它们完全跳过）；
        「只覆盖档位」的会话（_session_efforts 有、这里没有）不在此列——
        它们的模型跟全局，切换时档位随迁。"""
        sids = set(self._session_models)
        # 默认模型会话按创建时刻的快照记（不随偏好现状翻转）
        sids |= set(getattr(self, "_default_model_sessions", {}))
        return sids

    async def switch_model(self, name: str, model: str | None = None) -> dict:
        self.provider = self._build_provider(name, model)
        self.provider_error = None
        limit = self._context_limit()
        private = self._model_private_sessions()
        # 单一循环按 sid 分派（避免两处循环口径漂移）：
        # - 模型身份私有（覆盖 ∪ 默认模型会话）→ 完全跳过，模型与上限都不动；
        # - 只覆盖档位 → 模型跟新全局：建 detached 副本并把已存档位重放
        #   （新服务不支持思考则清除档位，provider 回归共享全局对象）；
        # - 无覆盖（含基底）→ 共享全局 provider，上限刷新
        pairs: list[tuple[str | None, Agent]] = [(None, self._base_agent)]
        pairs.extend((rt.sid, rt.agent) for rt in self.runtimes.values())
        for sid, ag in pairs:
            if sid is not None and sid in private:
                continue
            saved_effort = self._session_efforts.get(sid) if sid is not None else None
            if saved_effort is not None:
                prov = self._build_detached_provider(name, model)
                if getattr(prov, "supports_reasoning", False):
                    prov.set_reasoning_effort(saved_effort)
                    ag.provider = prov
                else:
                    self._session_efforts.pop(sid, None)
                    ag.provider = self.provider
            else:
                ag.provider = self.provider
            # 换服务/换模型 = 换上下文窗口：上限与视觉能力都跟着新的服务走
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

    # ---- 辅助对话模型（设置/清除走 WS：aux.model.set / aux.model.get） ----

    def _aux_model_pref(self) -> tuple[str, str]:
        """辅助对话的专用模型偏好（ui.json）：(provider, model)，空 = 跟随主对话。

        服务可能已被删除、模型可能已被下架，读取时校验，失效即视为未设置。"""
        try:
            prefs = self._read_ui_prefs()
        except Exception:
            return "", ""
        name = str(prefs.get("aux_model_provider") or "").strip()
        if not name or name not in self.cfg.providers:
            return "", ""
        model = str(prefs.get("aux_model_name") or "").strip()
        pc = self.cfg.providers[name]
        if model and pc.models and model not in pc.models:
            model = ""
        return name, model

    def aux_model_state(self) -> dict:
        name, model = self._aux_model_pref()
        return {
            "provider": name,
            "model": model,
            "label": (f"{name} / {model}" if name and model
                      else name if name else ""),
            # 跟随主对话时入口徽章画主对话服务的 logo：给服务名而不是让前端
            # 拆 main_label 字符串；主模型切换后重开面板即刷新
            "main_provider": self.provider_name or "",
            "main_label": (f"{self.provider_name} / {self.provider_model}"
                           if self.provider_name else ""),
        }

    async def set_aux_model(self, params: dict) -> dict:
        """设定 / 清除「辅助对话模型」。

        name 传空串 = 清除（辅助对话回到跟随主对话）；name+model = 辅助面板
        用专用 provider 一问一答，不影响主对话与各会话的 runtime。服务名必须
        在 config.toml 里存在，模型名必须是该服务已登记的模型（或留空 = 用
        该服务的默认模型）。"""
        name = str(params.get("name") or "").strip()
        model = str(params.get("model") or "").strip()
        if name:
            if name not in self.cfg.providers:
                raise RuntimeError(f"未知服务：{name}")
            pc = self.cfg.providers[name]
            if model and pc.models and model not in pc.models:
                raise RuntimeError(f"服务 {name} 没有登记模型 {model}，先在模型服务里添加")
        elif model:
            raise RuntimeError("清除辅助对话模型时不能只留模型名")
        try:
            await self._write_ui_prefs({
                "aux_model_provider": name or None,
                "aux_model_name": model or None,
            })
        except Exception as e:
            raise RuntimeError(f"写入偏好失败：{e}") from e
        return self.aux_model_state()

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
            # 会话已私有（模型身份或档位任一）的会话不施全局档位：它们挂的
            # 不是共享的 provider 对象，各自档位走 session.reasoning_set 面。
            # 基底（无会话）与无覆盖会话照旧共享全局 provider，直接换档。
            private = self._model_private_sessions() | set(self._session_efforts)
            self._base_agent.provider = self.provider
            for rt in self.runtimes.values():
                if rt.sid in private:
                    continue
                rt.agent.provider = self.provider
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
        self._refresh_system_prompts()
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
        self._refresh_system_prompts()
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
        overwrite=True 供「更新/重装」用：同名技能整目录替换；
        网址安装会同时记录来源 url，供技能清单展示安装来源。
        """
        root = self._skill_root(scope)
        existing = {s.name for s in self.skills.all()}
        try:
            if str(source).strip().lower().startswith(("http://", "https://")):
                # 官方场景模板优先用打包内副本：装一个模板在线要直连下载整仓
                # 归档（无缓存复用），中文网络环境经常失败——包内副本离线秒装；
                # bundled_dir_for 返回 None（非官方来源 / 包内没有或坏副本）才
                # 回落在线下载，兼作更新回退
                bundled = bundled_dir_for(str(source))
                if bundled is not None:
                    result = await asyncio.to_thread(
                        install_skill, str(bundled), root,
                        existing=existing, overwrite=overwrite,
                    )
                else:
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
        self._refresh_system_prompts()
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

    def gallery_skills(self) -> dict:
        """场景模板：打包内官方技能清单 + 逐条比对本机是否已装。

        纯本地只读（与技能清单同级，不进本机专属表）：数据来自打包内的
        gallery_manifest.json，不联网、不动技能目录；installed 按技能名
        与当前清单（skills.all()）比对得出，前端据此标「已安装」或给
        一键安装按钮。安装走既有 skills.install（scope=global），这里不管。

        已装的条目再与打包内副本做内容比对（skills.gallery.
        bundled_update_available：相对路径 + 逐文件字节哈希），有差异即
        update_available——官方更新了模板、或本地技能被改过时提示「可更新」，
        更新动作复用 skills.install（overwrite=true，包内副本覆盖）。
        """
        installed_by_name = {s.name: s for s in self.skills.all()}
        entries = load_gallery_manifest()
        templates = []
        for s in entries:
            name = str(s.get("name", ""))
            skill = installed_by_name.get(name)
            update_available = False
            update_hint = ""
            if skill is not None:
                update_available = bundled_update_available(
                    str(s.get("source", "")), skill.path.parent
                )
                if update_available:
                    update_hint = (
                        "更新会用随应用内置的模板副本覆盖本地已装技能"
                        "（无需联网，本地改动会被替换）"
                    )
            templates.append({
                "name": name,
                "description": str(s.get("description", "")),
                "source": str(s.get("source", "")),
                "dir": str(s.get("dir", "")),
                "installed": skill is not None,
                "update_available": update_available,
                "update_hint": update_hint,
            })
        return {"templates": templates, "count": len(templates)}

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
        self._refresh_system_prompts()
        if scope == "project":
            # 删掉项目技能也是用户自己的改动；来源限定在项目技能目录
            self.trust.refresh(touched=self._project_skills_dir())
        result["scope"] = scope
        return result

    # ---- 技能：会话存为草稿（/save-skill） ----

    def _skill_safety_lookup(self) -> Callable[[str], str]:
        """工具名 → safety 分级的查询函数（草稿生成的分类依据）。

        查不到的名字（MCP 服务器未连接 / 注册表构建失败）一律按写入/执行类
        保守处理：宁可把一条只读调用列进注意事项，也不把写操作漏成「无风险」。
        """
        try:
            registry = self._build_full_registry()
        except Exception:  # noqa: BLE001  启动早期 cfg 未就绪等；退化为全保守
            registry = None

        def safety_of(tool_name: str) -> str:
            tool = registry.get(tool_name) if registry is not None else None
            return tool.safety.value if tool is not None else Safety.WRITE.value

        return safety_of

    async def save_skill_from_session(self, session_id: str = "") -> dict:
        """把一个会话的做法整理成技能草稿（skills.save_from_session）。

        不调模型：生成逻辑在 skills/draft.py，从 SessionStore 的持久化消息
        机械提取（目标 = 首条用户消息、步骤 = 实际发生过的工具调用按轮次排列，
        只读与写入/执行分开）。返回的 name / description / 正文都会先过前端
        编辑框，用户改完确认才经 skills.save_draft 落盘。
        """
        sid = session_id or (self.session.id if self.session else "")
        if not sid:
            raise RuntimeError("当前没有活动会话，先聊出一轮任务再保存技能草稿")
        sess = await self._get_owned_session(sid)
        msgs = await self.store.load_messages(sid)
        draft = build_skill_draft(
            msgs,
            safety_of=self._skill_safety_lookup(),
            project_root=self.working_dir,
        )
        draft["session_title"] = sess.title or ""
        return draft

    async def save_skill_draft(self, name: str, content: str, scope: str = "global") -> dict:
        """保存技能草稿（skills.save_draft）：与技能安装同一套落点防线。

        名字形状（_validate_skill_name）与落点包含性（_skill_target）都过一遍，
        文件经 textio 原子写进技能目录；重名不覆盖。写完立即重新发现并重建
        系统提示词，新技能马上出现在清单里（与 install_skill 的收尾一致）；
        项目级保存同样同步工作区信任指纹，避免刚存完就回到「待确认」。
        """
        root = self._skill_root(scope)
        existing = {s.name for s in self.skills.all()}
        try:
            result = await asyncio.to_thread(
                save_skill_draft, root, name, content, existing=existing
            )
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        self.skills.discover()
        self._refresh_system_prompts()
        if scope == "project":
            self.trust.refresh(touched=self._project_skills_dir())
        result["scope"] = scope
        return result

    def _project_skills_dir(self) -> Path | None:
        """当前项目的技能目录（trust.refresh 指明来源用；无项目时为 None）。

        与 _project_skills_dir_if_trusted 的区别：这个不要求项目已信任——
        refresh 的场景恰恰是「用户刚动过它、要判断能否延续信任」。
        """
        if self.working_dir is None:
            return None
        return self.working_dir / ".skysheep" / "skills"

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
        self._refresh_system_prompts()

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
            ag.compaction_auto = self.cfg.compaction_auto
            ag.restrict_to_workdir = self.cfg.restrict_to_workdir
            ag.job_containment = self.cfg.shell.job_containment
            # 沙箱级别与遏制同一条热更通道（run_command 每次执行时从工具
            # 上下文取值；Agent 未带该属性的旧构造点赋值也无害）
            ag.sandbox_level = self.cfg.shell.sandbox_level

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
        # 团队成员 Agent 的钩子同步热更（成员不在 _for_each_agent，构造时
        # 注入的 HookRunner 不跟着刷新就会对队员悄悄失效）
        for orch in self._teams.values():
            for ag in orch.agents():
                ag.hooks = self.hooks

    def advanced_settings(self) -> dict:

        return {
            "max_iterations": self.cfg.max_iterations,
            "context_limit_tokens": self.cfg.context_limit_tokens,
            "compaction_keep_recent": self.cfg.compaction_keep_recent,
            "compaction_trigger": self.cfg.compaction_trigger,
            "compaction_auto": self.cfg.compaction_auto,
            "restrict_to_workdir": self.cfg.restrict_to_workdir,
            "computer_control": self.cfg.computer_control,
            "browser_control": self.cfg.browser_control,
            "daily_token_budget": self.cfg.daily_token_budget,
            # 「安全与后台」的三项：定时调度总开关与命令沙箱（读取点都在各自
            # 执行路径上按当前配置取值，改完即热生效，无需重启）
            "system_schedule": self.cfg.cron.system_schedule,
            "job_containment": self.cfg.shell.job_containment,
            "sandbox_level": self.cfg.shell.sandbox_level,
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
        # 自动压缩总开关：不传 = 不动（与其它布尔项同一姿态）
        compaction_auto = params.get("compaction_auto")
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
                compaction_auto=None if compaction_auto is None else bool(compaction_auto),
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
                # 「安全与后台」三项：不传 = 不动（与其它布尔项同一姿态）；
                # 读取点在执行路径上按配置取值，self.cfg 重载后即热生效
                system_schedule=(
                    None if params.get("system_schedule") is None
                    else bool(params.get("system_schedule"))
                ),
                job_containment=(
                    None if params.get("job_containment") is None
                    else bool(params.get("job_containment"))
                ),
                sandbox_level=(
                    None if params.get("sandbox_level") is None
                    else str(params.get("sandbox_level"))
                ),
                use_ripgrep=(
                    None if params.get("use_ripgrep") is None
                    else bool(params.get("use_ripgrep"))
                ),
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
        # Mods 收紧查询接到新门上（切项目 = 换门；运行期改动由 _reload_mods 兜住）
        if self.mods is not None:
            self.gate.extra_confirm = self.mods.extra_confirm
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
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
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
        # Mods 与钩子同位重建：工作目录变了，草稿落点/收紧声明语境随之刷新；
        # Agent 构造点（setup 的 base agent、_get_runtime）在之后读 self.mods
        self._reload_mods()
        self.checkpoints = CheckpointStore(root=self._checkpoint_root())

        # 旧项目的活动团队一并失效：成员 Agent 挂着旧项目的工具表与权限门，
        # 不能让切项目后的消息继续路由进旧团队（登记转「已中断」供 team.get 展示）。
        # 跨项目分屏列的团队除外：成员挂的是会话所属项目的工具表与权限门
        # （_own_ctx），不随主栏切换失效。
        if self._teams:
            for sid in [
                sid for sid in list(self._teams)
                if not ((rt := self.runtimes.get(sid)) is not None
                        and rt.ctx is not None and rt.ctx.own)
            ]:
                orch = self._teams.pop(sid)
                self._teams_interrupted[sid] = {
                    "session_id": sid,
                    "roster": [m.name for m in orch.roster],
                    "director_mode": orch.director_mode,
                    "status": "interrupted",
                    "interrupt_reason": "项目已切换，团队已中断",
                }
            if self._teams or self._teams_interrupted:
                self._persist_team_state()
        # 旧项目的会话 runtime 全部失效：停任务、落空排队轮、释放、清空。
        # 排队轮的 Future 必须逐个落空（与 delete_session 同一口径）：否则那些
        # 发消息的请求要么等到被取消的轮在旧上下文里交棒空跑一轮后拿到裸
        # 内部错误，要么在取消落在 pipeline try 之前的窄竞态里永远挂死。
        # 显式落空给等待方一条可读错误，队列清空也让交棒找不到旧轮次。
        # 跨项目分屏列的 runtime 除外：它绑定会话所属项目（自己的 gate/钩子/
        # 检查点/子代理），主栏切项目不动它——这正是分屏并排看两个项目的意义。
        for rt in list(self.runtimes.values()):
            if rt.ctx is not None and rt.ctx.own:
                continue
            for item in list(rt.queue):
                item.fail(RuntimeError("项目已切换，本次请求未执行"))
            rt.queue.clear()
            t = rt.run_task
            if t and not t.done():
                t.cancel()
            self._forget_runtime(rt)
            self.runtimes.pop(rt.sid, None)
        for item in list(self._base_queue):
            item.fail(RuntimeError("项目已切换，本次请求未执行"))
        self._base_queue.clear()

        if self._base_agent is not None:
            self._base_agent.working_dir = target
            self._base_agent.hooks = self.hooks
            self._base_agent.set_system(self.compose_system())
            # 幸存的 runtime 只剩跨项目列：工作目录与系统提示词留在会话自己的
            # 项目里，不跟随主栏切换
            for rt in self.runtimes.values():
                if rt.ctx is not None and rt.ctx.own:
                    continue
                rt.agent.working_dir = target
                rt.agent.set_system(self.compose_system())

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
        self._refresh_system_prompts()
        try:
            mtime = round(path.stat().st_mtime, 3)
        except OSError:
            mtime = 0.0
        return {"saved": True, "path": str(path), "chars": len(text), "mtime": mtime,
                "truncated": truncated, "original_chars": original_chars,
                "limit": MAX_INSTRUCTIONS_CHARS}

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

    @staticmethod
    def _toast_blocking(title: str, body: str) -> None:
        from winotify import Notification, audio

        launch = ServerBackend._toast_launch_uri()
        toast = Notification(app_id="SkySheep", title=title, msg=body, launch=launch)
        toast.set_audio(audio.Silent, loop=False)
        toast.show()


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
            # 本地地址不走系统代理（与 models/probe.py 的「本地直连」约定同口径）：
            # Windows 上 httpx 默认 trust_env=True 会读注册表里的 WinINET 系统代理，
            # 系统代理进程没在跑时，发往 127.0.0.1 本地转写服务的请求会被转给代理
            # 然后连不上。远端维持默认（信任环境代理，用户可能需要经代理出网）。
            async with httpx.AsyncClient(timeout=120, trust_env=not _is_local(base)) as client:
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


