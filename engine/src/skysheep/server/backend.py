"""ServerBackend：把引擎接线（会话/模型/技能/MCP/子代理/权限）暴露给服务层。

与 CLI 的 ChatApp 共享同一套引擎 API，但不含任何 UI 逻辑——
FastAPI WebSocket 端点消费它并转发事件流。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess  # noqa: F401  测试按模块属性 patch Popen
import sys
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .. import __version__
from ..bgtasks import spawn_bg
from ..channels import ChannelGate, ChannelManager
from ..config import (
    PRESET_SIGNUP_URLS,
    PRESETS,
    ConfigError,
    SkySheepConfig,
    db_path,
    load_config,
    resolve_api_key,
    resolve_imagegen,
    resolve_websearch,
    set_provider_models_in_config,
    skysheep_home,
    update_config_section,
    update_provider_in_config,
)
from ..core import Agent
from ..core.adversarial import (
    PHASE_ADVISOR,
    PHASE_FINDER,
    PHASE_INVESTIGATOR,
    PHASE_JUDGE,
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
from ..core.checkpoints import CheckpointStore
from ..core.collab_budget import BudgetedProvider
from ..core.collab_templates import (
    CollabTemplate,
    CollabTemplateStore,
    RunBudget,
    TeamCfgView,
    collect_command_prefixes,
    derive_needs,
    effective_tool_entries,
    is_builtin_template_name,
    validate_collab_template,
)
from ..core.context import (
    compact_history,
)
from ..core.hooks import (
    HookRunner,
    hooks_from_config,
    load_raw_config,
)
from ..core.mods_tool import ModTool
from ..core.power import PowerKeeper
from ..core.roundtable import (
    MEMBER_ROLES,
    MemberSpec,
)
from ..core.run_ledger import RunLedger
from ..core.runs import (
    RunCenter,
)
from ..core.subagent import (
    TaskManager,
)
from ..core.subagent_store import (
    SubagentStore,
)
from ..core.team import (
    ChannelMessage,
    DirectorSpec,
    TeamError,
    TeamMemberSpec,
    TeamMessageSink,
    TeamOrchestrator,
    TeamTemplateDirector,
    TeamTemplateMember,
    validate_team_template_name,
)
from ..core.team_resident import ResidentTeamEntry, ResidentTeamStore
from ..core.team_store import TEAM_TEMPLATE_PERSONA_MAX
from ..events import (
    AssistantMessage,
    NoticeEvent,
    TurnFinished,
    TurnStarted,
)
from ..mcp import (
    MCPManager,
    load_mcp_configs,
    presets_public,
)
from ..messages import ImageBlock, Message, TextBlock
from ..models import Provider
from ..models.diagnostics import HealthRegistry
from ..models.factory import build_provider
from ..obs import warning as obs_warning
from ..security.gate import (
    _RUN_COMMAND,
    GrantedGate,
    PermissionGate,
)
from ..security.trust import WorkspaceTrust
from ..session import SessionStore
from ..skills import SkillLoader
from ..textio import read_text_file, write_text_atomic
from ..tools import (
    ChangeRecorder,
    Safety,
    ToolRegistry,
    default_tools,
)
from ..tools.assembly import register_subagent_tools
from ..tools.pipeline import PipelineWriteTool
from ..tools.shell import set_child_network
from ..tools.skill import LoadSkillTool
from .backend_parts._shared import (
    EmitFn,
    ProjectCtx,
    SessionRuntime,
    _dump_emit,
    _strip_image_blocks,
)
from .backend_parts.advanced import SystemMixin
from .backend_parts.archive_mgmt import ArchiveMgmtMixin
from .backend_parts.automation import AutomationMixin
from .backend_parts.browser_data import BrowserDataMixin
from .backend_parts.channels import (
    ChannelsMixin,
    _channel_allowed_tools_warning,  # noqa: F401  测试从本模块引用
)
from .backend_parts.chat_pipeline import ChatPipelineMixin
from .backend_parts.checkpoints import CheckpointsMixin
from .backend_parts.collab_gallery import CollabGalleryMixin
from .backend_parts.collab_team_templates import (  # noqa: F401  re-export
    _CollabTeamTemplate,
    _CollabTeamTemplateBudget,
    _CollabTeamTemplateStore,
)
from .backend_parts.data_retention import RetentionMixin
from .backend_parts.factory import FactoryMixin
from .backend_parts.lifecycle import LifecycleMixin
from .backend_parts.macros import MacrosMixin
from .backend_parts.mcp import McpMixin
from .backend_parts.memory import MemoryMixin
from .backend_parts.migration import MigrationMixin
from .backend_parts.mods import ModsMixin
from .backend_parts.permission_modes import PermissionModesMixin
from .backend_parts.preferences import PreferencesMixin
from .backend_parts.project_data import ProjectDataMixin
from .backend_parts.project_mgmt import ProjectMgmtMixin
from .backend_parts.providers import ProvidersMixin
from .backend_parts.relay import RelaySettingsMixin
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
from .backend_parts.roundtable_body import RoundtableBodyMixin
from .backend_parts.session_model import SessionModelMixin
from .backend_parts.settings_io import SettingsIOMixin
from .backend_parts.skills import SkillsMixin
from .backend_parts.spectate import SpectateMixin
from .backend_parts.subagent import SubagentMixin
from .backend_parts.terminal import TerminalManager, TerminalPanelMixin
from .backend_parts.trust import TrustMixin
from .backend_parts.usage import (  # noqa: F401  轮次日志件定义在 usage.py，测试从本模块取用
    TURN_LOG_LIMIT_DEFAULT,
    TURN_LOG_LIMIT_MAX,
    UsageMixin,
    _load_turn_rows,
    _log_line_ts,
    _turn_row,
)
from .backend_parts.webtools import WebtoolsMixin
from .backend_parts.whitelist import WhitelistMixin
from .backend_parts.workspace_panel import (  # noqa: F401  BUILTIN_SNIPPETS 供 _dispatch_files 与测试从本模块引用
    BUILTIN_SNIPPETS,
    WorkspacePanelMixin,
)

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

# 断线重连补帧的缓冲：每会话只留当前轮最近这么多帧；超过 _EVENT_LOG_SESSIONS
# 个会话时按最早创建淘汰（长跑进程不无限涨内存）。
_EVENT_LOG_MAX = 1_200
_EVENT_LOG_SESSIONS = 20

# 二次取消后收割后台落库的兜底超时（秒）：正常落库毫秒级完成，超时说明库被
# 锁死等异常，不能拖着整个收尾无限等（排队轮还等着交棒）。
_PERSIST_HARVEST_TIMEOUT = 10.0

# team_active.json 里「已中断」记录的保留上限：团队运行时本就不跨进程恢复，
# 这些记录只用于历史里的只读频道回放（凭 team_id 拉全量），按最近保留即可。
_TEAM_INTERRUPTED_KEEP = 50


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
    # 建队来源模板名（四期常驻）：仅建队路径消费，记进编排器快照供
    # team.resident_set 绑定回模板（定时触发按模板匹配复用的依据）
    team_template: str = ""
    # 协作模板（本期）：解析后的模板 dict（含 kind / roles / budget…）；None = 普通轮。
    # 入队时随发送固定，交棒轮不再回查模板册（模板可在排队期间被删/改名）。
    template: dict | None = None
    # 模板的运行级授权（仅团队模板消费）：{scope: run|ask_each, read_only: bool}
    grant: dict | None = None
    # 本机/远程客户端（入队时随发送固定）：决定引用注入的项目归属口径
    local: bool = True

    def resolve(self, result: dict) -> None:
        if not self.fut.done():
            self.fut.set_result(result)

    def fail(self, e: BaseException) -> None:
        if not self.fut.done():
            self.fut.set_exception(e)


# ---- 大会话历史窗口化（切会话/恢复/分叉/boot 的 messages 只下发尾部窗口） ----
# 与前端 app.js 的 HISTORY_SYNC_LIMIT / HISTORY_CHUNK 同值——两层各自定义，
# 不跨层 import（前端零构建无模块边界，后端也不该依赖前端常量）。
# 此前 5000 条会话一帧下发全部 brief（实测 176ms 组装 + 3.4MB WS 载荷），窗口化
# 后单帧成本与 HISTORY_SYNC_LIMIT 恒定；更早的历史由前端经 messages.earlier 按
# HISTORY_CHUNK 向前翻页，单页上限 HISTORY_EARLIER_MAX 防滥用。
HISTORY_SYNC_LIMIT = 200
HISTORY_CHUNK = 100
HISTORY_EARLIER_MAX = 500


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
        # 对抗轮元数据同样必须随历史下发：前端的历史回放靠它重建审查卡
        # （buildAdvReplayCard 读 m.adversarial）。只带实时响应的话，刷新或
        # 重开会话后卡就没了、只剩正文；团队与圆桌的 meta 寄在 roundtable
        # 键下，所以只有对抗漏了这一格。findings 正文在落库时已逐字段限长；
        # report 键是终审报告原文（与消息正文同源落库），是 adversarial.export
        # 组装「## 终审报告」节的事实源，随 meta 原样回传同量级。
        "adversarial": m.adversarial,
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
        # 本轮来源表（引用溯源）：历史恢复时渲染「来源 [1] [2]」chip，
        # 刷新/重开会话后仍可点回原网页核对。旧消息没有该字段，前端按空处理。
        "sources": getattr(m, "sources", None),
        # 系统补记（如「撤销本轮改动」的回滚说明）：前端不挂用户消息操作，
        # 避免误点「编辑重发」按 seq 截断会话
        "system_note": bool(getattr(m, "system_note", False)),
    }


_ADV_VERDICT_LABELS = {"confirmed": "成立", "partial": "部分成立", "refuted": "推翻", "pending": "待定"}
# 严重度四档与流水线的合法值集（core/adversarial.py _VALID_SEVERITIES）及前端
# ADV_SEVERITY_LABEL（static/app.js）对齐：致命级在导出文件里渲染中文。
# 旧表里的 "info" 键移除：流水线从产出过 severity 起（git 追溯到对抗功能首个
# 提交）白名单就只有 critical/high/medium/low，落库前非法值已归为 medium，
# "info" 是无来源的死键——渲染对未知值本就回退原文（.get 第二参），删掉与
# 前端行为完全一致。
_ADV_SEV_LABELS = {"critical": "致命", "high": "高", "medium": "中", "low": "低"}
_ADV_ROLE_LABELS = {"finder": "发现者", "investigator": "调查者", "advisor": "建议者", "judge": "裁判"}


def _adversarial_fingerprints(meta: dict) -> dict:
    """对抗结果的「指纹视图」：计数 + 每条发现的稳定键（P3 历史对比用）。

    键取「位置 + 描述首行」压空白后的文本，不含 id/序号：两次审查是两次独立
    流水线，id 不跨轮稳定，而同一个 bug 在两轮里位置与描述首行几乎一样——
    这正是「上次那个问题还在不在」要回答的问题。认不出来的（无位置无描述）
    退化成整段描述的前 80 字，宁可对不上也不错配。
    """
    meta = meta if isinstance(meta, dict) else {}
    stats = meta.get("stats") or {}
    findings = [f for f in (meta.get("findings") or []) if isinstance(f, dict)]
    keys: set[str] = set()
    titles: dict[str, str] = {}
    for f in findings:
        raw_desc = str(f.get("description") or "").strip()
        first_line = raw_desc.splitlines()[0] if raw_desc else ""
        title = " ".join(first_line.split())[:80] or str(f.get("id") or "（无描述）")
        location = " ".join(str(f.get("location") or "").split())
        key = (location + "｜" + title).lower()
        keys.add(key)
        titles[key] = title
    summary = {
        "total": int(stats.get("total") or len(findings)),
        "confirmed": int(stats.get("confirmed") or 0),
        "partial": int(stats.get("partial") or 0),
        "refuted": int(stats.get("refuted") or 0),
        "pending": int(stats.get("pending") or 0),
    }
    return {"summary": summary, "keys": keys, "titles": titles}


def _adversarial_report_markdown(session_title: str, meta: dict, seq: int) -> tuple[str, str]:
    """把落库的对抗 meta 组装成结构化 Markdown 报告（P3 导出）。

    结构：报告头（来源/裁判/角色名册/裁决统计）→ 终审报告原文（report 字段，
    失败或中止可能缺失）→ 发现清单（每条含严重度/类别/位置/描述/依据/裁决/
    裁决理由/修复建议）。全字段容错：键缺失按缺省渲染，正文里不改写内容
    （本来就是 Markdown 文本，原样嵌入）。
    """
    meta = meta if isinstance(meta, dict) else {}
    stats = meta.get("stats") or {}
    roles = [r for r in (meta.get("roles") or []) if isinstance(r, dict)]
    judge = meta.get("judge") or {}
    findings = [f for f in (meta.get("findings") or []) if isinstance(f, dict)]
    status = str(meta.get("status") or "unknown")
    status_label = {"done": "完成", "error": "失败", "cancelled": "已中止"}.get(status, status)
    title = (session_title or "").strip() or "未命名会话"

    lines = [f"# 对抗审查报告：{title}", ""]
    lines.append(f"- 来源：消息 seq {seq} · 状态 {status_label}")
    if judge.get("provider") or judge.get("model"):
        lines.append(f"- 裁判：{judge.get('provider') or '?'} / {judge.get('model') or '?'}")
    if roles:
        lines.append("- 角色名册：" + "、".join(
            f"{_ADV_ROLE_LABELS.get(str(r.get('role')), str(r.get('role') or '?'))}"
            f"（{r.get('provider') or '?'} / {r.get('model') or '?'}）" for r in roles))
    if stats.get("total"):
        bits = [f"共 {stats['total']} 条"]
        for key, label in (("confirmed", "成立"), ("partial", "部分成立"),
                           ("refuted", "推翻"), ("pending", "待定")):
            if stats.get(key):
                bits.append(f"{label} {stats[key]}")
        lines.append("- 裁决统计：" + "，".join(bits))
    report = str(meta.get("report") or "").strip()
    if report:
        lines += ["", "## 终审报告", "", report]
    if findings:
        lines += ["", "## 发现清单", ""]
        for i, f in enumerate(findings, 1):
            head = str(f.get("description") or "").strip().splitlines()
            title = (head[0] if head else "") or str(f.get("id") or "") or "（无描述）"
            lines.append(f"### {i}. {title}")
            lines.append("")
            bits = []
            if f.get("severity"):
                bits.append(f"严重度：{_ADV_SEV_LABELS.get(str(f['severity']), str(f['severity']))}")
            if f.get("category"):
                bits.append(f"类别：{f['category']}")
            if f.get("location"):
                bits.append(f"位置：{f['location']}")
            if bits:
                lines.append("- " + "｜".join(bits))
            if f.get("verdict"):
                verdict = _ADV_VERDICT_LABELS.get(str(f["verdict"]), str(f["verdict"]))
                reason = str(f.get("reason") or "").strip()
                lines.append(f"- 裁决：{verdict}" + (f"（{reason}）" if reason else ""))
            desc = str(f.get("description") or "").strip()
            if desc:
                lines += ["", desc, ""]
            for label, key in (("依据", "evidence"), ("修复建议", "solution")):
                body = str(f.get(key) or "").strip()
                if body:
                    lines += [f"- {label}：", "", "  > " + body.replace("\n", "\n  > "), ""]
    if not report and not findings:
        lines += ["", "（本次对抗没有产出终审报告或发现清单。）"]
    filename = f"对抗审查报告-{seq:04d}.md"
    return "\n".join(lines).rstrip() + "\n", filename


def _window_briefs(ua: list[Message]) -> tuple[list[dict], int, int]:
    """全量 user/assistant 列表就地窗口化：返回 (尾部 briefs, 总数, 窗口前条数)。

    resume 路径的历史已为 runtime 全量装配（模型上下文需要，省不掉），回包
    下发时就地切片即可，不必再回库查一遍窗口。offset 语义与 store.load_messages_window
    一致：窗口之前还有多少条 user/assistant（0 = 已经是最早）。"""
    total = len(ua)
    start = max(0, total - HISTORY_SYNC_LIMIT)
    return [_msg_brief(m) for m in ua[start:]], total, start



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



_ROOM_TASK_STATUSES = ("pending", "in_progress", "review", "done", "error")


def _room_empty_stats() -> dict:
    """team.room 空态的 stats：五个状态全零 + 空成员用量（形状与活动态一致）。"""
    return {"tasks_by_status": {k: 0 for k in _ROOM_TASK_STATUSES}, "member_usage": {}}


def _room_stats(team: dict) -> dict:
    """从团队快照派生团队室首画 stats：工单按状态计数 + 成员用量。

    快照里 status 在五态之外的工单不计入（前端看板把它们折进完成/失败区，
    这里的计数只服务成员带与列头首画，缺一个数比数错一个好）。
    """
    counts = {k: 0 for k in _ROOM_TASK_STATUSES}
    for t in team.get("tasks") or []:
        st = str((t or {}).get("status") or "")
        if st in counts:
            counts[st] += 1
    usage = (team.get("usage") or {}).get("members")
    return {
        "tasks_by_status": counts,
        "member_usage": dict(usage) if isinstance(usage, dict) else {},
    }


class ServerBackend(AutomationMixin, ChannelsMixin, MemoryMixin,
                   SpectateMixin, SystemMixin, RelaySettingsMixin, TerminalPanelMixin,
                   RemoteMixin, WebtoolsMixin, WorkspacePanelMixin,
                   LifecycleMixin, McpMixin, ModsMixin, PreferencesMixin,
                   ProvidersMixin, ProjectDataMixin, RetentionMixin,
                   ArchiveMgmtMixin, BrowserDataMixin, FactoryMixin, MigrationMixin,
                   MacrosMixin, CollabGalleryMixin, RoundtableBodyMixin,
                   # D6 基础设施与迁移（批 2 外迁件，方法自主类按块落位）
                   SkillsMixin, WhitelistMixin, SubagentMixin, TrustMixin,
                   SettingsIOMixin,
                   # D1 会话与运行时（批 5a 外迁件，方法自主类按块落位）
                   ChatPipelineMixin, CheckpointsMixin,
                   # D1 会话与运行时（批 5b 外迁件，方法自主类按块落位）
                   SessionModelMixin, PermissionModesMixin, UsageMixin,
                   ProjectMgmtMixin):
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
        self.team_templates = _CollabTeamTemplateStore(skysheep_home() / "teams.json")
        # 协作模板册（本期）：~/.skysheep/collab_templates.json。只存用户自建的
        # 圆桌 / 对抗模板；团队模板仍住 teams.json（不迁移），内置三张只在代码里
        self.collab_templates = CollabTemplateStore(
            skysheep_home() / "collab_templates.json"
        )
        # 常驻团队注册表（T3.2）：~/.skysheep/team_resident.json，同姿态——
        # 哪支队伍跨会话常驻、复用所需的工单板快照与频道 seq 水位
        self.resident_teams = ResidentTeamStore(skysheep_home() / "team_resident.json")
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
        # Claude Code 兼容模式的 Mods 管理器（core/mods_claude.py）：
        # 与 self.mods（quickjs 沙箱模式）二选一。由 _reload_mods_claude
        # 按 config [mods_claude] 表创建；启用时 self.mods 留空。
        self.mods_claude = None
        self._pending_mod_install: dict | None = None  # 两段安装的待确认项
        # D5 字段所有权（拆分批 1 注记）：term 由 terminal.py 的 TerminalPanelMixin 消费
        self.term = TerminalManager()
        # 保持电脑运行（防空闲睡眠）：设置页 ui.json 的 prevent_sleep 控制；
        # 启动与每次保存偏好时应用，退出时停掉（Windows daemon 线程）
        self.power = PowerKeeper()
        # D5 字段所有权 + 双域写分区（拆分批 1 注记，设计 §3.3 次雷）：key 空间按
        # sid 分区——D1（会话模型语义）只写 aux_histories[sid] 元数据（切辅助模型
        # 建/删条目），D5 terminal.py 写消息体；若后续冲突再收只读 accessor
        self.aux_histories: dict[str, list[Message]] = {}
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
        # 事件回放（断线重连补帧）：seq 全局单调，缓冲按会话只保留当前轮
        self._event_seq = 0
        self._event_logs: dict[str, deque] = {}
        self._event_log_order: list[str] = []
        # 在途落库的全局写锁：多会话并行时进度写彼此串行，减少与其它
        # 单语句写的锁碰撞（真正的一致性由 store 的写事务兜底）
        self._progress_write_lock = asyncio.Lock()
        # D3 字段所有权（拆分批 4 注记，设计 §3.2）：无人值守自动化的十个字段
        # 由 AutomationMixin（backend_parts/automation.py）消费——
        # _reminder_task/_cron_task（提醒与 cron 扫描循环句柄，
        # start/stop_*_loop 写）、_cron_running（在跑登记表：task_id → 运行
        # 句柄，防重复触发 + 工厂「叫停」）、_cron_skip_mark（长跑任务到点
        # 跳过去重）、_schtasks_runner（schtasks 执行入口注入点，测试替换）、
        # _pipeline_task/_pipeline_running/_pipeline_node_tasks/
        # _pipeline_manual_triggers/_pipeline_busy_until（编排循环句柄、节点
        # 并发账、节点句柄、手动重排标记与续跑退避表）。跨域只读（不收敛，
        # 注记，设计 §3.3 雷区收敛①）：_cron_running/_pipeline_running 的
        # 「忙」语义经批 4 新增的 automation_busy() accessor（automation.py）
        # 供 D1 LifecycleMixin._backup_busy_message 合判，D1 不再裸读这两表。
        self._reminder_task: asyncio.Task | None = None
        self._cron_task: asyncio.Task | None = None
        # 正在跑的定时任务：task_id → 运行句柄（防重复触发 + 供工厂「叫停」）。
        # 值是 asyncio.Task；只有 spawn_bg 建出来的后台运行允许取消（取消一个
        # 正在等回执的请求任务会连带掐掉 WS 回执，见 bgtasks.is_bg_task）。
        self._cron_running: dict[int, asyncio.Task | None] = {}
        # 长跑任务反复到点时的跳过去重：task_id → 上次记过的 next_run_at
        # （同一到点批次只记一条 skipped，见 AutomationMixin._cron_pass）
        self._cron_skip_mark: dict[int, float] = {}
        # 工厂运行账本（automation_runs）：store 在 setup() 里才连上，实例也在
        # 那里创建；先占位，属性名保持不变（流水线/扫描循环按它记账）。
        # 所有权划 D3（拆分批 4 注记，设计 §3.2）：D3 域内消费——automation
        # 记账（cron 扫描/编排节点）、factory 热更保留策略（factory.save）；
        # 内核 setup 只做构造与启动收尾（mark_interrupted/maybe_prune）。
        self.run_ledger: RunLedger | None = None
        # schtasks 执行入口的注入点（测试替换成假 runner，绝不真建系统计划任务）
        self._schtasks_runner = None
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_running: set[int] = set()  # 正在跑的编排节点 id（并发约束）
        self._pipeline_node_tasks: dict[int, asyncio.Task] = {}  # 节点句柄（停止流水线用）
        self._pipeline_manual_triggers: set[int] = set()  # 用户明确重排、尚未开始尝试的节点
        # 会话续跑节点遇忙的退避表（node_id → 最早可重试时刻）：不占 DB，重启即清零，
        # 清零后重试也不伤——busy 预检还在，会再次退避。
        self._pipeline_busy_until: dict[int, float] = {}
        # D1 字段所有权（拆分批 5b 注记，设计 §3.2）：D1 域十三个字段由会话与
        # 运行时各 Mixin 消费，构造仍在本类一次铺齐——
        #   _session_models（会话级模型覆盖：session.model_switch 写，
        #     _runtime_provider / _session_effective_model / _apply_model_routing
        #     读，LifecycleMixin 删除/清空空会话时清）；
        #   _default_model_sessions（新会话默认模型的创建时刻快照：new_session 写，
        #     SessionModelMixin 三段解析读、失效清除，LifecycleMixin 清）；
        #   _session_efforts（会话级思考档位：SessionModelMixin 读写，内核
        #     _get_runtime 重建时重放，LifecycleMixin 清）；
        #   _session_accepts（会话级权限档位：PermissionModesMixin 读写，内核
        #     _runtime_gate 建私有门时重放，LifecycleMixin 清）；
        #   _titling（自动标题单飞集合）/_manually_named（手改名后自动标题让路）：
        #     _titling 由 ChatPipelineMixin 读写；_manually_named 由 ChatPipelineMixin
        #     只读、LifecycleMixin 改名/删除时写（add/discard）；
        #   _persist_tasks（在飞落库任务：ChatPipelineMixin._run_turn_pipeline
        #     建任务并登记，任务体是 _persist_turn；LifecycleMixin.delete_session
        #     等它落库再删）；
        #   _digesting / _maintaining / _map_generating / _memory_io_lock（记忆
        #     提炼、定期整理、演化摘要单飞与记忆文件写互斥）：MemoryMixin 独占；
        #   instructions_file / instructions_text（项目说明 AGENTS.md 装载结果）：
        #     _bind_project 装载写、SessionModelMixin._context_detail 与内核
        #     snapshot 读、project_data.py 项目记忆读写面与 MemoryMixin 整理后刷新。
        # 跨域口径：`_teams*` 归 D2（批 6 收敛）、`_cron_running` / `_pipeline_running`
        # 归 D3（批 4 已收 automation_busy()），D1 只读或经 accessor。
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
        # D4 字段所有权（拆分批 3 注记，设计 §3.2）：更新链路九个字段由
        # RemoteMixin（backend_parts/remote.py）消费——update_info（发现的新
        # 版本：静默检查 _check_update_quietly 与手动 check_update 写）、
        # update_error（手动检查失败原因，check_update 写）、update_success/
        # update_failure（更新对账终态：consume_update_marker 启动结算写、
        # dismiss_update_failure 清）、_pending_update/_pending_update_version/
        # _pending_update_verified（下载完成写、_update_ready 读）、
        # _auto_update_task（自动下载句柄，_check_update_quietly 写）、
        # _is_frozen（安装版判定，getattr(sys,"frozen") 一次性铺定）。
        # 跨域只读（不收敛，注记）：内核 snapshot 读 update_info/
        # update_success/update_failure/_is_frozen（boot 契约口径，
        # snapshot 聚合返回）。
        self.update_info: dict | None = None  # {"version","url","notes"}：发现的新版本
        self.update_error: str | None = None  # 手动检查时的失败原因（进设置 · 关于）
        # 更新结果对账（更新可信度）：consume_update_marker 在启动时结算，
        # success = 上次更新闭环成功（提示一次），failure = 装没成（亮现场直到确认）
        self.update_success: dict | None = None
        self.update_failure: dict | None = None
        self._pending_update: str | None = None  # 已下载待安装的更新包路径
        self._pending_update_version: str = ""  # 待安装包的版本（复用下载时校验）
        self._pending_update_verified: bool = False  # 下载时是否核对了 .sha256
        self._auto_update_task: asyncio.Task | None = None  # 后台自动下载句柄
        self._is_frozen: bool = bool(getattr(sys, "frozen", False))  # 安装版=True，源码版=False
        # D4 字段所有权（拆分批 3 注记，设计 §3.2）：渠道五字段由 ChannelsMixin
        # （backend_parts/channels.py）消费——channels（实例在 setup 建：
        # ChannelManager(self, self._channels_config)）、_channel_gates/
        # _channel_names（渠道入站登记：会话 id → 门控/平台名）、
        # _channel_last_chat（平台名 → 最近入站 chat_id，回执复用）、
        # _weixin_login_channel（未启用微信时的扫码登录一次性实例）。
        # 跨域读写（不收敛，注记，设计 §3.3 雷区表）：_channel_gates/
        # _channel_names 由 D1 LifecycleMixin.delete_session 清登记残键
        # （lifecycle.py delete_session 行内注记约 :312-317；互指留待后续批）；
        # _channel_names 另被内核
        # _reload_agent_history 读（渠道会话系统提示词加 CHANNEL_STYLE，
        # 批 5 后随 ChatPipelineMixin 落 D1）。
        self.channels: ChannelManager | None = None  # 聊天软件渠道（Bot Channel）
        self._channel_gates: dict[str, ChannelGate] = {}  # 渠道会话 id → 门控
        self._channel_names: dict[str, str] = {}  # 渠道会话 id → 平台名（反向查找）
        self._channel_last_chat: dict[str, str] = {}  # 平台名 → 最近一次入站的 chat_id
        self._weixin_login_channel = None  # 未启用微信时，仅供扫码登录用的一次性实例
        # D4 字段所有权（拆分批 3 注记，设计 §3.2）：令牌与访问留痕五字段由
        # RemoteMixin（backend_parts/remote.py）消费——token_throttle（验证
        # 失败的即时节流）、token_generation（令牌世代，轮换 +1）、
        # _token_failures/_token_failure_total（失败留痕与总数）、_access_log
        # （最近成功的远程访问）。跨域调用（不收敛，注记）：server/app.py 验
        # 令牌路径（_lan_token_guard 中间件与 ws_endpoint 握手）调
        # token_throttle.blocked/note_success/note_failure，握手读
        # token_generation 记世代快照（轮换后旧世代连接在端内踢线）——
        # 记账在 app 侧调用，策略与留痕落点都在本域。
        # 访问令牌的失败节流与留痕：设置页展示「有谁在试你的门」，节流让连错
        # 变得不划算。都是内存态，重启即清空（真实排查看桌面日志）。
        self.token_throttle = TokenThrottle()
        # 令牌世代：每次轮换 +1。WS 连接在握手时记下当时世代，轮换后旧世代
        # 连接会被主动关闭（旧令牌只能阻止新连接，已建立的连接照常发 RPC
        # 是实际缺口）。非远程连接（本机）不受影响。
        self.token_generation = 0
        # 运行中心：全产品可观测运行（主轮/子代理任务）统一登记 + 崩溃恢复。
        # 快照落 ~/.skysheep/runs.json（write_text_atomic），启动装载把上次
        # 在跑的记为 interrupted（见 core/runs.py）。
        # D3 字段所有权（拆分批 4 注记，设计 §3.2/§4.1 批 4②）：呈现口径在
        # D3——factory.live/factory_state 读 RunCenter 快照（backend_parts/
        # factory.py，getattr 防御读）；跨域消费（不收敛，注记）：内核主轮
        # 状态机写状态（_run_turn_pipeline，批 5 后随 ChatPipelineMixin 落
        # D1）、项目任务簿与 _bind_project 注入 run_center（内核）。
        self.runs = RunCenter(skysheep_home() / "runs.json")
        # 供应商健康登记：agent 循环上报成功/失败，设置页状态灯读它
        self.provider_health = HealthRegistry()
        # D5 字段所有权（拆分批 1 注记）：文本文件编码/行尾符探测缓存，
        # 消费在 backend_parts/workspace_panel.py（fs.read 写入、fs.write 读回）
        self._text_meta_cache: dict[str, tuple[int, str, str]] = {}
        self._token_failures: list[dict] = []  # 最近 TOKEN_FAILURE_LOG_MAX 条 {ts, ip, where}
        self._token_failure_total = 0  # 自启动以来的失败总数（留痕列表只留最近几条）
        # 最近成功的远程访问（HTTP/WS 验令牌通过）：只留最近的，重启清零。
        # 与失败留痕对称，设置页展示「最近访问」；只记 ip/时间/渠道，不记令牌。
        self._access_log: list[dict] = []  # {ts, ip, where}
        # D4 字段所有权（拆分批 3 注记，设计 §3.2）：ws_connections 由
        # RemoteMixin 按语义消费——connections_summary/lan_kick 读、
        # revoke_remote_connections 踢线（无条件踢全部非本机连接；token_generation
        # 世代比对是另一条机制，在 app.py 分发前置拦截）。
        # 跨域读写（不收敛，注记）：server/app.py 记账——ws_endpoint 握手
        # append、断连 remove；广播仍走 ws_emitters。
        # 在线 WS 连接登记：与 ws_emitters 平行维护（广播仍走 emitters，零侵入），
        # 条目 {emit, ip, origin, connected_at, ua, kick}。仅内存态。
        self.ws_connections: list[dict] = []
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
        mods_claude=None,
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
        agent = Agent(
            provider=provider,
            registry=self._build_full_registry(recorder, ctx=ctx),
            gate=gate,
            working_dir=working_dir,
            max_iterations=self.cfg.max_iterations,
            context_limit_tokens=self._context_limit_for(provider),
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            compaction_trigger=self.cfg.compaction_trigger,
            compaction_auto=self.cfg.compaction_auto,
            hooks=self.hooks,
            mods=mods,
            mods_claude=mods_claude,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
            session_id=session_id,
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
            permission_timeout_s=self.cfg.permission_timeout_s,
            drift_check_steps=self.cfg.agent.drift_check_steps,
        )
        agent.on_provider_ok = self._provider_on_ok
        agent.on_provider_error = self._provider_on_error
        return agent

    def _provider_on_ok(self, name: str) -> None:
        """Agent 循环上报「模型调用成功」：健康灯转绿（延迟拿不到精确值，记 0）。"""
        if name:
            try:
                self.provider_health.record_ok(name)
            except Exception:  # noqa: BLE001 - 健康灯不影响主流程
                pass

    def _provider_on_error(self, name: str, diag) -> None:
        """Agent 循环上报「模型调用失败」：健康灯按分类转黄/红。"""
        if name:
            try:
                self.provider_health.record_error(name, diag)
            except Exception:  # noqa: BLE001
                pass

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
        """快聊会话的工作目录：数据目录下专用的 fast-talk 文件夹。

        不与 default 混用（用户指定）：快聊的文件操作全部落在自己的隔离目录里，
        与其他默认目录互不干扰。"""
        return self._fixed_workdir("fast-talk")

    def _remote_workdir(self) -> Path:
        """远程连接（渠道）会话的工作目录：数据目录下的 remote-control 文件夹。

        D4 批注记（拆分批 3，设计 §2.1 外迁块批内决策）：留内核——与
        _quick_workdir 同属固定工作目录族，被内核 _session_project 调用，
        setup 启动即建，跨域只读另有 D1 lifecycle.py 的 session 详情路径；
        随 RemoteMixin 外迁会把内核工作目录解析倒挂成对 D4 的依赖。"""
        return self._fixed_workdir("remote-control")

    async def _session_project(self, session_id: str) -> tuple[int | None, Path | None]:
        """会话所属项目 (project_id, 工作目录)。

        工作目录解析口径：快聊（无项目）→ 数据目录 fast-talk 专用文件夹；远程连接
        项目 → 数据目录 remote-control 文件夹（两类会话都有真实、稳定的工作
        目录，不再借用引擎当前目录）；项目记录缺失 → 回退引擎当前工作目录；
        目录已消失 → 可读报错而不是静默换目录干活——那等于让对话在错误的
        项目里改文件。
        """
        sess = await self.store.get_session(session_id)
        if sess is None:
            return None, self.working_dir
        if sess.project_id is None:
            # 快聊：数据目录 fast-talk 专用文件夹（真实、稳定、独立于 default）
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
            tm.run_center = self.runs
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
                    mods_claude=self.mods_claude,
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

    def _context_limit_for(self, provider) -> int:
        """按「这一轮实际使用的 provider」取上下文上限。

        会话级模型覆盖、产线级模型（定时任务）都是独立实例：拿全局上限会
        让跑在 32k 服务上的长任务以为自己有 200k，压缩触发得太晚。
        provider.name 就是配置键；认不出来（fake / 测试桩）回落全局口径。
        """
        name = str(getattr(provider, "name", "") or "")
        pc = self.cfg.providers.get(name) if name else None
        if pc is None:
            return self._context_limit()
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

    def _forget_runtime(self, rt: SessionRuntime) -> None:
        """runtime 已从字典摘除后调用：释放它引用的全局资源（防泄漏）。

        provider 只放会话私有副本：runtime 常驻的 detached provider（会话级
        模型覆盖/档位回放/默认模型快照）各自建了 httpx 连接池，不随 runtime
        回收就把连接一直挂到进程退出；LRU 淘汰反复发生就是慢性积累。
        """
        self._close_provider_after(
            getattr(rt.agent, "provider", None), getattr(rt, "run_task", None)
        )
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

    # ---- 会话事件回放（断线重连补帧） ----

    def log_session_event(self, session_id: str, payload: dict) -> dict:
        """给事件打上全局单调 seq 并存入该会话的「当前轮」缓冲。

        只保留当前轮：轮首（turn_started）清空上一轮——重连要补的是「这次
        断线期间错过的帧」，历史内容由消息表兜底（在途轮已边跑边落库）。
        """
        self._event_seq += 1
        # 键名 ev_seq 而不是 seq：seq 在团队频道消息等事件里已有自己的语义
        # （频道内序号），覆盖它会破坏前端按频道 seq 聚合增量。
        ev = {**payload, "ev_seq": self._event_seq}
        buf = self._event_logs.get(session_id)
        if buf is None:
            buf = deque(maxlen=_EVENT_LOG_MAX)
            self._event_logs[session_id] = buf
            self._event_log_order.append(session_id)
            while len(self._event_log_order) > _EVENT_LOG_SESSIONS:
                old = self._event_log_order.pop(0)
                self._event_logs.pop(old, None)
        if payload.get("kind") == "turn_started":
            buf.clear()
        buf.append(ev)
        return ev

    def replay_events(
        self, session_id: str, after_seq: int = 0, limit: int = 1000,
    ) -> dict:
        """返回 after_seq 之后本会话已下发的事件帧。

        gap=True 表示请求的位置已经被挤出缓冲（不是当前轮，或帧数超出容量），
        前端应改走历史重载，而不是拿一段不连续的帧去续画。
        """
        buf = self._event_logs.get(session_id)
        events: list[dict] = []
        gap = False
        if buf:
            first = int(buf[0].get("ev_seq") or 0)
            gap = first > int(after_seq) + 1
            events = [
                ev for ev in buf if int(ev.get("ev_seq") or 0) > int(after_seq)
            ]
        return {
            "events": events[-max(1, int(limit)):],
            "gap": gap,
            "latest_seq": int(buf[-1].get("ev_seq") or 0) if buf else 0,
        }

    # ---- 生命周期 ----

    async def setup(self) -> None:
        self.cfg = load_config()
        # 全局出网设置先落到子进程环境口径上：run_command / 终端 / 钩子 /
        # MCP stdio / Mods 的 Node 进程都经 child_environment() 取它。
        # 后续在设置页保存网络配置时同样会热更新（见 save_network_settings）。
        set_child_network(self.cfg.network)
        self.store = self._store_override or await SessionStore(db_path()).connect()
        # 工厂运行账本：automation_runs 的写入口 + factory_run 事件出口。
        # 保留策略取 [factory] 配置；factory.save 保存后热更新这两项。
        # 事件出口 _emit_factory_run 自拆分批 4 起由 FactoryMixin 提供
        # （backend_parts/factory.py），此处经 MRO 取到。
        _factory_cfg = getattr(self.cfg, "factory", None)
        self.run_ledger = RunLedger(
            self.store, emit=self._emit_factory_run,
            keep=int(getattr(_factory_cfg, "history_keep", 2000)),
            days=int(getattr(_factory_cfg, "history_days", 30)),
        )
        # 接力运行期：取数据目录独占运行权；持锁者才把上次非终态任务标为中断。
        # 第二个后端取不到锁时不做恢复，绝不打断别的进程正在跑的活任务。
        await self._relay_startup()
        # 启动即确定「当前项目」：优先恢复上次用的（ui.json 的 active_project，
        # 0 = 明确的无项目态）；没有记住过（首次启动/老版本升级）才沿用启动目录。
        # 无项目态是合法状态：不建任何项目记录，快聊照常可用。
        prefs = self._read_ui_prefs()
        remembered = prefs.get("active_project")
        projects = await self.store.list_projects()
        # 软移除的项目不参与「当前项目」恢复，但也不能触发「首次启动自动建项目」：
        # 用户主动移除唯一的项目后重启，应当保持无项目态，而不是把同一个目录
        # 经 get_or_create_project 恢复回来。
        has_removed = bool(await self.store.list_projects(include_removed=True)) and not projects
        target: Path | None = None
        if remembered:
            row = next((p for p in projects if p.id == remembered), None)
            if row is not None and Path(row.root_path).is_dir():
                target = Path(row.root_path)
        elif not projects and not has_removed:
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
        # 工厂运行账本收尾：上次强退残留的 running 统一标为 interrupted（与
        # reset_interrupted_pipelines 同一姿态），再按保留策略清一次历史。
        # 记账失败不拦启动——工厂少几张旧包裹，不影响应用本身。
        try:
            _stale_runs = await self.run_ledger.mark_interrupted()
            if _stale_runs:
                logging.getLogger("skysheep").info(
                    "工厂：%d 条残留的运行记录已标为中断", _stale_runs
                )
            await self.run_ledger.maybe_prune()
        except Exception as e:  # noqa: BLE001
            logging.getLogger("skysheep").warning("运行账本启动收尾失败（忽略）：%s", e)
        self.subagent_store.load()
        # 团队模板册同款启动装载：不 load 的话 template_list 恒回空、
        # template_remove 对磁盘已有模板误报「找不到」，此后任意一次
        # template_save 会以仅含新模板的内存册整体覆盖 teams.json——已存
        # 模板被静默清光（upsert→save 是全量覆盖落盘）
        self.team_templates.load()
        # 协作模板册同款启动装载：不 load 的话 template_list 恒缺用户模板、
        # remove 对磁盘已有模板误报「找不到」，此后任意一次 upsert→save 会以
        # 仅含新条目的内存册整体覆盖 collab_templates.json
        self.collab_templates.load()
        # 常驻团队注册表同款启动装载（不 load 的话 resident_set/clear 对磁盘
        # 已有登记误判，此后任意一次 upsert 会以仅含新条目的内存册整体覆盖
        # team_resident.json——已登记的常驻队伍被静默清光）
        self.resident_teams.load()
        # Mods 暂存目录清扫：install 与 confirm 之间重启遗留的 .importing-*
        # （只启动期调一次，运行期可能正有未确认的安装预览占着 staging）
        self.sweep_mod_staging()
        # 无头浏览器残留清扫：上次强杀/崩溃时 atexit 不执行，skysheep-edge-*
        # 临时配置目录残留在系统 TEMP——只在「上次未正常关闭」时扫（正常退出
        # 已由 atexit 清理，不用多此一举；crash.flag 在 lifespan 启动时会被重写，
        # 必须用 setup 之前读好的 crashed_last_run 判断）。进程还活着的目录
        # rmtree 失败自然跳过，误删正在使用的实例风险为零；测试环境
        # （SKYSHEEP_HOME 隔离）通常没有 crash.flag，不会触发。
        try:
            if getattr(self, "crashed_last_run", False):
                from ..tools.browser_cdp import sweep_orphan_browser_profiles

                removed = sweep_orphan_browser_profiles()
                if removed:
                    logging.getLogger("skysheep").info(
                        "启动清扫：清理 %d 个无头浏览器残留目录", removed
                    )
        except Exception as e:  # noqa: BLE001 - 清扫失败不影响启动
            # 与上面成功路径的 info 对称：清扫失效（残留目录会继续占盘）
            # 至少留一条线索，不挡启动。
            logging.getLogger("skysheep").warning(
                "启动清扫：无头浏览器残留目录清扫失败（%s: %s）", type(e).__name__, e
            )
        # 「远程连接」固定项目不再启动即建：没有渠道会话时侧栏不出现这个分组，
        # 删除后也不再复活；渠道真正收到消息时经 _remote_project_id 幂等重建
        # 快聊 / 远程连接的固定工作目录：数据目录下真实文件夹（fast-talk /
        # remote-control），启动即建——两类会话从此有稳定的工作目录
        self._quick_workdir()
        self._remote_workdir()
        # connect_mcp=False：启动不在这里同步连 MCP，统一交给下面的后台任务
        await self._bind_project(target, connect_mcp=False)
        # 分级权限模式：上次会话选的档位（0=安全执行 1=自动编辑 2=完全访问）重启后保持
        self._apply_gate_accept_pref(prefs.get("accept_edits", 0))
        # 保持电脑运行：按上次保存的偏好决定是否阻止空闲睡眠（Windows）
        self._apply_power_pref()
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
            network=self.cfg.network,
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

        # 启动期先加载 Claude 兼容模式（若配置启用），再建 base agent
        self._reload_mods_claude()
        self._base_agent = self._build_agent(
            provider=self.provider,
            gate=self.gate,
            working_dir=self.working_dir,
            mods=self.mods,
            mods_claude=self.mods_claude,
        )
        # 上次应用内更新的结果对账（成功清标记 / 失败亮现场），要赶在 boot
        # 快照组装前结算完，前端首帧就能看到结果
        self.consume_update_marker()
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

    async def _remote_project_id(self, ensure: bool = True) -> int | None:
        """「远程连接」固定项目的 id（幂等创建 + 实例内缓存）。

        渠道（飞书/微信）会话的归属项目：与桌面工作目录无关，见
        store.ensure_remote_project。ensure=False 用于归属比对类的只读查询
        （_get_owned_session / 分屏 interactive 判定）：项目不存在时不创建、
        返回 None——没有项目就没有渠道会话，比对自然不命中。缓存失效：
        项目被删除时在 delete_project 里重置，下次渠道使用经 ensure 重建。

        D4 批注记（拆分批 3，设计 §2.1 外迁块批内决策）：留内核——被内核
        peek_session_messages（session.peek_messages 分屏 interactive 判定）调用，跨域只读另有 D1
        lifecycle.py 的会话列表路径；缓存 _remote_project_id_cache 本身
        就是内核字段（设计 §3.2），失效点在 delete_project/remove_project/
        restore_project（批 5 后落 D1 ProjectMgmtMixin）。"""
        if self._remote_project_id_cache is not None:
            return self._remote_project_id_cache
        proj = (await self.store.ensure_remote_project()) if ensure \
            else await self.store.get_remote_project()
        if proj is None:
            return None
        self._remote_project_id_cache = proj.id
        return proj.id

    def is_remote_project(self, project) -> bool:
        """按哨兵路径判断是否「远程连接」固定项目（同步，project.list 等渲染用）。

        D4 批注记（拆分批 3，设计 §2.0/§2.1）：留内核——设计 §2.0 把它列进
        前门契约（project.list 渲染消费，分发在 _dispatch_files.py）；
        delete_project/remove_project/restore_project 的哨兵路径分支同属
        内核项目管理面，批 5 后随 ProjectMgmtMixin 落 D1。"""
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
        except Exception as e:  # noqa: BLE001
            # 解析失败 ≠ 未配置：后者本就返回 None 且无异常。这里吞掉的是
            # 意外故障，warning 留痕——否则用户配错后联网搜索静默消失，
            # 模型只会说「工具不存在」，无从排查。
            obs_warning(
                "capability_resolve_failed",
                "联网搜索配置解析失败，该能力未启用（工具不注册）",
                capability="websearch",
                error=f"{type(e).__name__}: {e}",
            )
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
        except Exception as e:  # noqa: BLE001
            # 解析失败 ≠ 未配置：后者本就返回 None 且无异常。这里吞掉的是
            # 意外故障，warning 留痕——否则用户配错后画图静默消失，
            # 模型只会说「工具不存在」，无从排查。
            obs_warning(
                "capability_resolve_failed",
                "画图配置解析失败，该能力未启用（工具不注册）",
                capability="imagegen",
                error=f"{type(e).__name__}: {e}",
            )
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
            network=self.cfg.network,
        ))
        registry.register(LoadSkillTool(self.skills))
        if ctx is not None and ctx.own:
            # 跨项目列：绑定会话所属项目（ctx.tasks/project_id 在 _own_ctx 已定）
            register_subagent_tools(
                registry, ctx.tasks,
                enabled=self.cfg.subagent_enabled and ctx.project_id is not None,
            )
            if ctx.project_id is not None:
                registry.register(PipelineWriteTool(
                    self.store, lambda: ctx.project_id, lambda: ctx.tasks,
                ))
        else:
            # 无项目态不注册子代理/编排：子代理要工作目录、流水线绑定项目，
            # 没项目时给了模型也只会空转；切回项目后重建注册表自动恢复
            register_subagent_tools(
                registry, self.tasks,
                enabled=self.cfg.subagent_enabled and self.project is not None,
            )
            # 任务编排：Agent 可以排流水线（草稿），启动与否由用户在面板决定
            if self.project is not None:
                registry.register(PipelineWriteTool(
                    self.store, lambda: self.project.id, lambda: self.tasks,
                ))
        for t in self.mcp_tools:
            registry.register(t)
        # Mod 注册的工具（Claude 兼容模式）：handler 在 Node 侧，这里只是转调包装。
        # 一律 DANGEROUS 分级（第三方代码 + 无沙箱），不会因 Mod 自报 safety 免确认。
        manager = getattr(self, "mods_claude", None)
        if manager is not None:
            for mod in manager.mods.values():
                for spec in mod.registered_tools:
                    name = str(spec.get("name") or "").strip()
                    if not name or registry.get(name) is not None:
                        continue  # 重名不覆盖内置/别的 Mod（先到先得）
                    try:
                        registry.register(ModTool(
                            mod_id=mod.id, name=name,
                            description=str(spec.get("description") or ""),
                            schema=spec.get("schema") or {},
                            loaded_mod=mod,
                        ))
                    except Exception:  # noqa: BLE001 - 单个 Mod 工具坏掉不拖垮注册表
                        logger.warning(
                            "Mod 工具注册失败：%s/%s", mod.id, name, exc_info=True,
                        )
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
        # 保持电脑运行：退出时务必清掉 ES_CONTINUOUS（daemon 线程随进程退出
        # 也会清，但正常收尾要显式关，避免更新器拉起的新进程继承空闲豁免）
        try:
            self.power.stop()
        except Exception:  # noqa: BLE001 - 收尾失败不阻断退出
            pass
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
        # 接力：优先停止 worker 并等待收尾，再释放数据目录独占锁；最后才关 store
        await self._relay_shutdown()
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
            provider = build_provider(name, pc, self.cfg.network)
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
            return build_provider(name, pc, self.cfg.network)
        except ConfigError as e:
            raise RuntimeError(str(e)) from e

    # ---- 会话只读历史窗口（分屏 peek / messages.earlier；不在批 5b 外迁清单） ----

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
            or sess.project_id == await self._remote_project_id(ensure=False)
        )
        msgs, total, offset = await self.store.load_messages_window(
            session_id, limit=HISTORY_SYNC_LIMIT)
        return {
            "id": sess.id, "title": sess.title, "summary": sess.summary,
            "interactive": interactive,
            "messages": [_msg_brief(m) for m in msgs],
            "messages_total": total,
            "messages_offset": offset,
        }

    async def session_messages_earlier(self, params: dict, local: bool = True) -> dict:
        """messages.earlier：大会话历史窗口化的向前翻页半边。

        前端打开会话只拿到最后 HISTORY_SYNC_LIMIT 条 brief（session.resume /
        session.peek_messages / boot / session.fork 同此窗口），点「加载更早」
        凭已加载最旧一条的 seq（before_seq，不含）向前翻一页。归属口径与
        peek_session_messages 完全一致：本机界面（local=True）按 id 直查、跨
        项目可读——历史窗口是分屏列的常规阅读面；远程客户端（local=False）
        走 _get_owned_session 的当前项目口径，历史内容是枚举面，不向远程放开
        跨项目，越权报「session not found」不给探测区分信号。

        limit 缺省 HISTORY_CHUNK（与前端每页份量同值），上限
        HISTORY_EARLIER_MAX 防滥用；返回页升序、与 session.open 同 _msg_brief
        形状，附 messages_total / messages_offset / has_more（offset>0 即还有
        更早的，前端据此决定「加载更早」按钮去留）。"""
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        if params.get("before_seq") is None:
            raise RuntimeError("缺少 before_seq（已加载最旧一条的 seq）")
        try:
            before_seq = int(params["before_seq"])
        except (TypeError, ValueError):
            raise RuntimeError("before_seq 必须是整数") from None
        raw_limit = params.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else HISTORY_CHUNK
        except (TypeError, ValueError):
            raise RuntimeError("limit 必须是整数") from None
        limit = max(1, min(limit, HISTORY_EARLIER_MAX))
        msgs, total, offset = await self.store.load_messages_window(
            sid, before_seq=before_seq, limit=limit)
        return {
            "session_id": sess.id,
            "messages": [_msg_brief(m) for m in msgs],
            "messages_total": total,
            "messages_offset": offset,
            "has_more": offset > 0,
        }

    def _broadcast_ws(self, payload: dict, exclude=None) -> None:
        """把一帧事件广播给所有 WS 连接（exclude：该连接已由轮自身 emit 链覆盖）。

        运行态（queue_updated）是多端共同事实：只发发起连接，其他端（手机遥控、
        第二个窗口）的运行态停在旧值——发起者断开后更是永远等不到归零帧。
        广播帧不带 ev_seq：它不进会话事件日志，断线补帧只重放发起连接自己那份
        带 seq 的链路，补到的 queue_updated 经 log_session_event 自带 session_id。
        """
        for ws_emit in list(self.ws_emitters):
            if ws_emit is exclude:
                continue
            try:
                spawn_bg(ws_emit(payload))
            except RuntimeError:
                break  # 无事件循环（纯测试环境）

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
        return build_provider(name, pc, self.cfg.network)

    # 自动池「坏服务降权」：鉴权/余额类失败不是偶发，重试同一个服务只会
    # 再失败一次。配置里留着一个 401 Key 时，对抗的发现者游标一旦挑到它，
    # 整场会按 fail-fast 直接报错（真实模型走查）。这里只排序不剔除——
    # 全都坏时仍要给出错误角色卡，而不是假装没有可用服务。
    _BAD_POOL_CATEGORIES = frozenset({"auth", "balance"})

    def _auto_pool_rank(self, name: str) -> int:
        """自动池排序权重：0 可用/未测、1 有隐患（限流等）、2 鉴权/余额坏。"""
        try:
            health = self.provider_health.get(name)
        except Exception:  # noqa: BLE001 - 健康灯不可用不拦自动挑模型
            return 0
        if health is None:
            return 0
        if health.level == "down" and health.category in self._BAD_POOL_CATEGORIES:
            return 2
        if health.level != "ok":
            return 1
        return 0

    def _auto_pool_services(self) -> list[tuple[str, str]]:
        """已配置 Key 的服务池（按健康度稳定排序：能用的排前面）。"""
        pool: list[tuple[str, str]] = []
        for name, pc in self.cfg.providers.items():
            try:
                if resolve_api_key(name, pc) is None:
                    continue
            except Exception:  # noqa: BLE001 - 探测失败按未配置处理
                continue
            pool.append((name, pc.model or ""))
        # sorted 是稳定排序：同档服务保持配置顺序（自动挑模型的确定性不变）
        return sorted(pool, key=lambda item: self._auto_pool_rank(item[0]))

    def _resolve_members(
        self, members_params: list | None, chair_answers: bool | None = None,
        chair_key: tuple[str, str] | None = None, limit: int | None = None,
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
        # 模板可收紧自动成员池的上限：0 / None 都按 [roundtable] 配置走
        limit = self.cfg.roundtable.max_members if not limit else max(0, int(limit))
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
            for name, model in self._auto_pool_services():
                if chair_in and (name, model) == chair_key:
                    continue
                _add(name, model)
        return specs

    # 「提到就带上」预取与圆桌轮主体已拆到 backend_parts/roundtable_body.py（O5）
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
                "圆桌让多个模型并行独立作答，再由主席（当前主模型）汇总成一份答案，"
                "token 成本约为单模型的「成员数 + 1」倍；每多一轮辩论修订，成员成本"
                "再翻一倍（观点已一致的成员会自动跳过，雷同草稿在融合时自动去重）。"
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
        template: dict | None = None,
    ) -> dict:
        """对抗轮主体：发现 → 调查 → 建议 → 裁判 → 报告并入主历史。

        返回随轮次结果回传前端的对抗元数据（角色名册、裁决统计、问题清单、
        终审报告原文）。
        历史追加 user(被审内容) + assistant(最终报告)；中间产物（问题、裁决、
        方案）随报告消息的 adversarial 元数据持久化，历史回放时对抗卡可展开
        回看。取消语义与圆桌对齐：user 消息先入历史，中途点停止内容不丢；
        裁判中途取消时已流出的部分报告文本照样落库。
        """
        if agent is None:
            agent = self.agent
        if self.provider is None:
            raise RuntimeError("对抗需要当前主模型可用；请先在模型下拉中选择一个已配置 Key 的服务")

        tpl = template if isinstance(template, dict) else None
        tpl_limits = (tpl or {}).get("limits") or {}
        tpl_agg = (tpl or {}).get("aggregator") or {}
        cfg = self.cfg.adversarial
        # 模板限额：0 = 不覆盖 [adversarial] 配置
        max_findings = int(tpl_limits.get("max_findings") or 0) or cfg.max_findings
        role_timeout_s = int(tpl_limits.get("role_timeout_s") or 0) or cfg.role_timeout_s
        # 裁判按本轮会话解析（同 _roundtable_body 的主席口径）：会话级换过模型
        # 的标签/分屏列，终审必须由会话自己的模型做，而不是全局记账的模型
        resident_judge = agent.provider if agent.provider is not None else self.provider
        judge = resident_judge
        if sid:
            judge_name, judge_model = self._session_effective_model(sid)
        else:
            judge_name, judge_model = self.provider_name or "", self.provider_model or ""
        # 模板的收口角色（aggregator）非空时替换裁判（临时 provider，轮末释放）
        template_judge = None
        if tpl is not None and str(tpl_agg.get("provider") or "").strip():
            judge_name = str(tpl_agg.get("provider") or "").strip()
            judge_model = str(tpl_agg.get("model") or "").strip()
            try:
                template_judge = self._build_member_provider(judge_name, judge_model)
            except Exception as e:  # noqa: BLE001 - 裁判不可用要可读报错
                raise RuntimeError(
                    f"模板的收口模型「{judge_name}/{judge_model or '默认模型'}」不可用：{e}"
                ) from e
            judge = template_judge
            if not judge_model:
                judge_model = getattr(template_judge, "model", "") or ""
        budget = RunBudget(**((tpl or {}).get("budget") or {})) if tpl is not None else None

        emit_ev = _dump_emit(emit)

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

        # 纯文本对抗：角色没有文件工具，「审查 calc.py」这类提问若上下文没读过
        # 该文件，角色只能拒绝——发起前把点名文件只读预取进被审目标（不改落库
        # 的用户消息，也不写盘）。
        adv_text = question
        preload = self._preload_mentioned_files(
            question, agent.history, getattr(agent, "working_dir", None)
        )
        if preload:
            adv_text = f"{question}\n\n{preload}"
            await emit_ev(NoticeEvent(message=(
                "对抗角色没有文件工具：已把你点名的项目文件（只读）带给角色，"
                "最多 3 个，过长会截取。"
            )))

        # 上下文压缩护栏：四个角色都吃会话上下文，超限先压缩（同圆桌）
        if agent.used_context_tokens() > agent.context_limit_tokens:
            ev = await compact_history(
                agent, keep_recent=self.cfg.compaction_keep_recent, budget=budget,
            )
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

        # 模板的角色落位（带边界）：seat 0/1/2 = 发现/调查/建议。带 phase key
        # 的角色优先归位；没有 phase key 的按出现顺序补空位；超过三个的角色
        # 本期不参与（对抗只有三个角色），如实计入 dropped（preview 同口径）。
        tpl_seat_roles: dict[str, dict] = {}
        tpl_extra_roles: list[dict] = []
        for role in (tpl or {}).get("roles") or []:
            if not isinstance(role, dict):
                continue
            key = str(role.get("key") or "").strip()
            if key in (PHASE_FINDER, PHASE_INVESTIGATOR, PHASE_ADVISOR) \
                    and key not in tpl_seat_roles:
                tpl_seat_roles[key] = role
            else:
                tpl_extra_roles.append(role)
        for phase in (PHASE_FINDER, PHASE_INVESTIGATOR, PHASE_ADVISOR):
            if phase not in tpl_seat_roles and tpl_extra_roles:
                tpl_seat_roles[phase] = tpl_extra_roles.pop(0)

        # 自动序列：未带 members（纯自动）或带普通条目时才解析；模板已把三个
        # 角色全部指定为具体服务时不白建 Provider（也避免占掉测试注入的
        # provider 工厂池）。
        need_auto_pool = tpl is None or any(
            phase not in tpl_seat_roles
            or not str((tpl_seat_roles[phase] or {}).get("provider") or "").strip()
            for phase in (PHASE_FINDER, PHASE_INVESTIGATOR, PHASE_ADVISOR)
        )
        specs_auto = (
            self._resolve_members(
                auto_params, False, chair_key=(judge_name, judge_model),
            )
            if (members_params is None or auto_params) and need_auto_pool else []
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

        def _template_role_spec(role: dict) -> MemberSpec:
            """模板角色 → MemberSpec：指定服务则新建，留空按自动池游标补位。"""
            nonlocal auto_cursor
            name = str(role.get("name") or "").strip()
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            if provider_name:
                spec = _explicit_spec(provider_name, model)
                spec.name = name
                return spec
            if not specs_auto:
                return MemberSpec(
                    name=name, provider_name="", model="",
                    build_error="未指定模型且没有已配置 Key 的服务",
                )
            base = specs_auto[auto_cursor % len(specs_auto)]
            auto_cursor += 1
            return MemberSpec(
                name=name, provider_name=base.provider_name, model=base.model,
                provider=base.provider, build_error=base.build_error,
            )

        finder = (
            _template_role_spec(tpl_seat_roles[PHASE_FINDER])
            if PHASE_FINDER in tpl_seat_roles else _role_spec(PHASE_FINDER)
        )
        investigator = (
            _template_role_spec(tpl_seat_roles[PHASE_INVESTIGATOR])
            if PHASE_INVESTIGATOR in tpl_seat_roles else _role_spec(PHASE_INVESTIGATOR)
        )
        advisor = (
            _template_role_spec(tpl_seat_roles[PHASE_ADVISOR])
            if PHASE_ADVISOR in tpl_seat_roles else _role_spec(PHASE_ADVISOR)
        )
        # 角色人设：只放非空值（空串必须回落 adversarial.py 的内置默认提示词，
        # 不能让「模板留空」把不对称激励整段抹掉）；收口 prompt 归裁判。
        prompts: dict[str, str] = {}
        for phase, role in tpl_seat_roles.items():
            text = str((role or {}).get("prompt") or "").strip()
            if text:
                prompts[phase] = text
        if tpl is not None:
            agg_prompt = str(tpl_agg.get("prompt") or "").strip()
            if agg_prompt:
                prompts[PHASE_JUDGE] = agg_prompt
        if all(s.provider is None for s in (finder, investigator, advisor)):
            await emit_ev(TurnFinished(stop_reason="error", iterations=1, duration_ms=_adv_ms()))
            raise RuntimeError(
                "对抗没有可用角色：请先在「设置 · 模型服务」配置 API Key，"
                "或在对抗面板为角色指定模型"
            )

        # 发现者替补（只给自动挑选路径）：发现阶段失败=整场 fail-fast，而
        # 自动池可能正好挑到坏 Key 的服务。显式指定/模板指定服务的角色不给
        # 替补——用户的选择不该被静默替换。具体换谁由 core 过滤同服务后决定。
        finder_fallbacks: list[MemberSpec] = []
        finder_is_auto = PHASE_FINDER not in explicit and (
            PHASE_FINDER not in tpl_seat_roles
            or not str((tpl_seat_roles.get(PHASE_FINDER) or {}).get("provider") or "").strip()
        )
        if finder_is_auto:
            finder_fallbacks = [s for s in specs_auto if s.provider is not None]

        # 角色都不看图片（纯文本协作），历史里的图片块单独剥离；
        # history[:-1] 排除刚追加的本次提问（由 run_adversarial 自己拼在末尾）。
        # 角色消息不接会话系统提示词：角色系统提示词自足（core/adversarial.py
        # 的 _phase_messages），dialogue() 会把 history 里的 system 剥掉。
        role_history = _strip_image_blocks(agent.history[:-1])

        try:
            outcome: AdversarialOutcome = await run_adversarial(
                finder=finder,
                investigator=investigator,
                advisor=advisor,
                judge=judge,
                judge_provider=judge_name or "",
                judge_model=judge_model or "",
                history=role_history,
                user_text=adv_text,
                timeout_s=role_timeout_s,
                emit=emit_ev,
                max_findings=max_findings,
                prompts=prompts,
                budget=budget,
                finder_fallbacks=finder_fallbacks,
            )
        finally:
            # 角色临时 provider 随轮释放（同圆桌：裁判是用主 Agent 的常驻实例；
            # 模板收口裁判是本轮临时构建的，必须进释放清单）
            release = [finder.provider, investigator.provider, advisor.provider]
            if template_judge is not None:
                release.append(template_judge)
            self._release_turn_providers(release, keep=(resident_judge,))

        meta = {
            "mode": "adversarial",
            "judge": {"provider": judge_name, "model": judge_model},
            "roles": [
                {"role": "finder", "provider": finder.provider_name,
                 "model": finder.model, "name": getattr(finder, "name", "")},
                {"role": "investigator", "provider": investigator.provider_name,
                 "model": investigator.model,
                 "name": getattr(investigator, "name", "")},
                {"role": "advisor", "provider": advisor.provider_name,
                 "model": advisor.model, "name": getattr(advisor, "name", "")},
                {"role": "judge", "provider": judge_name, "model": judge_model},
            ],
            "stats": adversarial_stats(outcome),
            "findings": adversarial_findings_meta(outcome),
            # 终审报告原文随 meta 落库（与消息正文同源）：adversarial.export
            # 的「## 终审报告」节只认库内 meta（_adversarial_report_markdown），
            # 不落这里导出文件就永远只有发现清单。裁判失败的兜底报告在下方
            # 生成后覆写同键（degraded=True 已注明非终审）。
            "report": outcome.report,
            "status": outcome.status,
            "error": outcome.error,
        }
        if tpl is not None:
            # 模板身份随消息 meta 落库：历史回放卡头仍能显示「模板：xxx」
            meta["template"] = str(tpl.get("name") or "")
            meta["template_kind"] = "adversarial"
        if budget is not None and budget.reason():
            # 预算耗尽：报告（若有）照常保留在 outcome.report / meta["report"]，
            # 状态如实标 error 并带上原因码
            meta["budget_exceeded"] = budget.reason()

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
            # 兜底报告同样落 meta 的 report 键：导出的「终审报告」节在裁判
            # 失败/取消路径也有事实源（degraded=True 已如实标注非终审）
            meta["report"] = report
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
            # 发现者失败且没有任何可保留的产出：如实报错收尾。
            # 落一条标注失败的留痕消息再收尾——只发 TurnFinished(error) 的话
            # 历史里没有 assistant 行，刷新后本轮对抗经历完全消失（用户感知
            # 是「发出去的话石沉大海」）。留痕正文不含编造的结论；meta 照常
            # 带 status=error，历史回放按 err 样式重建对抗卡。
            note = "本轮对抗审查未完成"
            err_text = str(outcome.error or "").strip()
            if err_text:
                note += f"：{err_text}"
            assistant = Message.assistant([TextBlock(text=note)])
            assistant.adversarial = meta
            agent.history.append(assistant)
            await emit_ev(AssistantMessage(message=assistant.model_dump()))
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
                "对抗让多个模型接力审查：发现者只管找问题（宁滥勿缺），"
                "调查者逐条验证（力求推翻误报），建议者给修复方案，裁判"
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

    async def adversarial_export(self, params: dict, local: bool = True) -> dict:
        """adversarial.export：对抗审查报告导出（P3），返回 {filename, markdown}。

        来源校验与 messages.earlier 同口径：seq + 会话归属（本机任意会话可读，
        远程只认自己名下/当前项目的会话）；报告内容一律从库内 meta 组装，
        不接受客户端提交的正文。
        """
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise RuntimeError("无效的对抗消息序号") from None
        if seq <= 0:
            raise RuntimeError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise RuntimeError("指定消息不是对抗审查结果")
        markdown, filename = _adversarial_report_markdown(sess.title, message.adversarial, seq)
        return {"filename": filename, "markdown": markdown}

    async def adversarial_fix_prompt(self, params: dict, local: bool = True) -> dict:
        """adversarial.fix_prompt：把「成立 / 部分成立」的发现拼成一段修复指令。

        返回 {prompt, count}，前端回填输入框（不自动发送——修不修、在哪个会话
        修、用普通回合还是团队，由用户决定，不制造意外开销与权限动作）。
        事实源与导出同口径：只认库内 meta，不接受客户端提交的正文；seq 与
        会话归属校验也一致。
        """
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise RuntimeError("无效的对抗消息序号") from None
        if seq <= 0:
            raise RuntimeError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise RuntimeError("指定消息不是对抗审查结果")
        wanted = {k for k in str(params.get("verdicts", "") or "").split(",") if k}
        if not wanted:
            wanted = {"confirmed", "partial"}
        findings = [
            f for f in (message.adversarial.get("findings") or [])
            if isinstance(f, dict) and str(f.get("verdict") or "") in wanted
        ]
        if not findings:
            raise RuntimeError("本次审查没有「成立 / 部分成立」的发现可修复")
        lines = [
            "以下是对抗审查认定**成立**的问题（来自本会话第 "
            f"{seq} 条审查结果）。请逐条修复，修完用一句话回报每条的处理结果：",
            "",
        ]
        for i, f in enumerate(findings, start=1):
            verdict = {"confirmed": "成立", "partial": "部分成立"}.get(
                str(f.get("verdict") or ""), str(f.get("verdict") or "")
            )
            lines.append(
                f"{i}. [{f.get('severity') or 'medium'}·{verdict}] {f.get('category') or ''}"
                f" @ {f.get('location') or '未标注'}"
            )
            lines.append(f"   问题：{str(f.get('description') or '').strip()}")
            if f.get("solution"):
                lines.append(f"   建议方案：{str(f.get('solution')).strip()}")
            lines.append("   验收：改完后能说明「改了什么、为什么这样就修好了」")
        lines.append("")
        lines.append("只改这些成立项，不要顺手重构其他部分；改文件与跑命令照常走确认。")
        return {"prompt": "\n".join(lines), "count": len(findings)}

    async def adversarial_compare(self, params: dict, local: bool = True) -> dict:
        """adversarial.compare：本次对抗审查与「同会话上一次」的对比（P3 剩余半边）。

        对比口径只看落库 meta（与导出同一事实源），不猜：
        - 计数：发现总数、各裁决档位（成立/部分成立/推翻/待定）的增减；
        - 清单：按「位置 + 描述首行」认指纹，算出**新增**与**已消失**的发现
          （第二次审查把上次的问题修掉了，就是「已消失」）。
        找上一次的方式：取该消息之前最近窗口里的对抗结果（最多回看 200 条），
        找不到就返回 previous=None（首次审查，前端只说「这是第一次」）。
        来源校验与导出同口径：seq + 会话归属。
        """
        sid = str(params.get("session_id", "") or "")
        if local:
            sess = await self.store.get_session(sid)
            if sess is None:
                raise RuntimeError("session not found: " + sid)
        else:
            sess = await self._get_owned_session(sid)
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise RuntimeError("无效的对抗消息序号") from None
        if seq <= 0:
            raise RuntimeError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise RuntimeError("指定消息不是对抗审查结果")

        previous = None
        window, _first, _last = await self.store.load_messages_window(
            sid, before_seq=seq, limit=200,
        )
        for candidate in reversed(window):
            if candidate.adversarial:
                previous = candidate
                break
        cur = _adversarial_fingerprints(message.adversarial)
        if previous is None:
            return {
                "current": cur["summary"], "current_seq": seq,
                "previous": None, "previous_seq": 0,
                "delta": {}, "added": [], "resolved": [], "common": len(cur["keys"]),
            }
        prev = _adversarial_fingerprints(previous.adversarial)
        added_keys = cur["keys"] - prev["keys"]
        resolved_keys = prev["keys"] - cur["keys"]
        delta = {
            key: cur["summary"][key] - prev["summary"][key]
            for key in ("total", "confirmed", "partial", "refuted", "pending")
        }
        return {
            "current": cur["summary"], "current_seq": seq,
            "previous": prev["summary"], "previous_seq": int(getattr(previous, "seq", 0) or 0),
            "delta": delta,
            "added": [cur["titles"][k] for k in sorted(added_keys)],
            "resolved": [prev["titles"][k] for k in sorted(resolved_keys)],
            "common": len(cur["keys"] & prev["keys"]),
        }

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
        这里恢复「曾有个团队」的事实（含 team_id，供只读频道回放）供 team.get
        展示。已中断记录本身也持久保留（回写进同一文件的 interrupted 段）——
        否则第二次重启后团队卡就失去 team_id，历史里的频道无法再全量回放。
        记录按会话去重，最多保留 _TEAM_INTERRUPTED_KEEP 条。
        """
        if self._team_state_path is None or not self._team_state_path.exists():
            return
        try:
            data = json.loads(read_text_file(self._team_state_path).text)
        except (OSError, ValueError):
            return  # 坏文件按没有活动团队处理（write_text_atomic 保证不会有半个文件）
        for item in (data or {}).get("interrupted") or []:
            if not isinstance(item, dict) or not item.get("session_id"):
                continue
            rec = dict(item)
            rec["status"] = "interrupted"
            rec.setdefault("interrupt_reason", "应用重启，团队已中断")
            self._teams_interrupted[str(item["session_id"])] = rec
        for item in (data or {}).get("teams") or []:
            if not isinstance(item, dict) or not item.get("session_id"):
                continue
            rec = dict(item)
            rec["status"] = "interrupted"
            rec["interrupt_reason"] = "应用重启，团队已中断"
            self._teams_interrupted[str(item["session_id"])] = rec
        if self._teams_interrupted:
            self._persist_team_state()  # 活动段清账，中断段（含 team_id）回写保留

    def _persist_team_state(self) -> None:
        """活动团队与已中断团队登记落盘（textio 原子写；失败不拖垮团队本身）。"""
        if self._team_state_path is None:
            return
        # 已中断记录按插入序保留最近的若干条（同一会话只留最新一份）
        interrupted = list(self._teams_interrupted.values())[
            -_TEAM_INTERRUPTED_KEEP:
        ]
        data = {
            "teams": [
                {"session_id": sid,
                 "team_id": orch.team_id,
                 "roster": [m.name for m in orch.roster],
                 "director_mode": orch.director_mode}
                for sid, orch in self._teams.items()
            ],
            "interrupted": interrupted,
        }
        try:
            write_text_atomic(
                self._team_state_path,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError:
            pass

    def _release_team_providers(self, orch) -> None:
        """释放团队持有的成员 provider（httpx 连接池）。

        队员 provider 在建队时构建一次、跨整个团队生命周期常驻（成员 Agent
        复用同一实例），团队关闭时必须释放，否则每开一队泄漏一批连接池。
        AI 总管的 provider 同理（DirectorSpec.provider）。主持/常驻对象（如
        team.takeover 后 AI 总管转顾问）仍按本方法统一释放——团队已关，无处
        再用。
        """
        if orch is None:
            return
        provs = []
        for spec in getattr(orch, "roster", []) or []:
            provs.append(getattr(spec, "provider", None))
        director = getattr(orch, "_director", None)
        if director is not None:
            provs.append(getattr(director, "provider", None))
        for ag in orch.agents():
            provs.append(getattr(ag, "provider", None))
        self._release_turn_providers(provs)

    def _team_provider_context_limit(self, name: str) -> int:
        """成员/总管模型的上下文窗口（按 provider 配置；0 = 编排器全局回退）。

        ``_resolve_team_members`` 可能在 setup 之前被直接调用（测试/工具），
        此时 cfg 尚未装载：回 0 让编排器用全局值，不要因缺配置炸掉解析。
        """
        cfg = self.cfg
        if cfg is None:
            return 0
        pc = cfg.providers.get(name)
        return pc.effective_context_limit(cfg.context_limit_tokens) if pc \
            else cfg.context_limit_tokens

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
                member_name = str((m or {}).get("name", "") or "").strip()
                persona = str((m or {}).get("persona", "") or "").strip()
                if len(persona) > TEAM_TEMPLATE_PERSONA_MAX:
                    raise TeamError(
                        f"成员「{member_name or name}」的人设过长"
                        f"（最多 {TEAM_TEMPLATE_PERSONA_MAX} 字符）"
                    )
                spec = TeamMemberSpec(
                    name=member_name,
                    provider_name=name,
                    model=model,
                    persona=persona,
                    context_limit_tokens=self._team_provider_context_limit(name),
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
                    context_limit_tokens=self._team_provider_context_limit(ms.provider_name),
                ))
        return specs

    # ---- 协作模板：成员实例化 / 工具表裁剪 / 运行级授权（本期）----

    def _template_member_specs(
        self, roles: list[dict], chair_key: tuple[str, str] = ("", ""),
        limit: int = 0,
    ) -> list[MemberSpec]:
        """模板角色 → 圆桌 MemberSpec（按角色顺序，不走去重）。

        指定服务的角色新建 Provider；留空的角色按角色顺序从「已配好 Key 的
        服务」池里轮转补位（与团队模板同口径）。同一模型承担多个角色是允许的
        ——角色靠人设（system prompt）区分，不是身份预设（role 留空，避免
        MEMBER_ROLES 的内置身份提示词被再拼一遍）。
        """
        specs: list[MemberSpec] = []
        auto: list[MemberSpec] | None = None
        cursor = 0
        for role in roles or []:
            if not isinstance(role, dict):
                continue
            name = str(role.get("name") or "").strip()
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            prompt = str(role.get("prompt") or "").strip()
            if provider_name:
                try:
                    provider = self._build_member_provider(provider_name, model)
                    specs.append(MemberSpec(
                        provider_name=provider_name,
                        model=model or getattr(provider, "model", ""),
                        provider=provider,
                        role="", name=name, prompt=prompt,
                    ))
                except Exception as e:  # noqa: BLE001 - 构建失败降级错误成员卡
                    specs.append(MemberSpec(
                        provider_name=provider_name, model=model,
                        build_error=str(e)[:200],
                        role="", name=name, prompt=prompt,
                    ))
                continue
            if auto is None:
                auto = self._resolve_members(
                    None, False, chair_key=chair_key, limit=limit,
                )
            if not auto:
                specs.append(MemberSpec(
                    provider_name="", model="",
                    build_error="未指定模型且没有已配置 Key 的服务",
                    role="", name=name, prompt=prompt,
                ))
                continue
            src = auto[cursor % len(auto)]
            cursor += 1
            specs.append(MemberSpec(
                provider_name=src.provider_name, model=src.model,
                provider=src.provider, build_error=src.build_error,
                role="", name=name, prompt=prompt,
            ))
        return specs

    def _expand_auto_pool(self, auto: list[MemberSpec]) -> list[MemberSpec]:
        """把自动池按「服务 × 配置的 models 列表」展开成补位池。

        自动池每个服务只贡献当前模型一条；团队模板的多个角色（provider 全空、
        自动挑选）从这样的池轮转补位，(服务, 模型) 键全部相同，随即被去重
        合并成一名队员——单 Key 配了多模型（如一个网关挂多个上游）时三角色
        小组会缩成一人。展开后角色尽量各得一个独立键：第一个角色仍拿当前
        模型（行为与旧自动池前段一致），后续角色依次取同服务的其余 models；
        某个 model 构建失败降级为 build_error 成员，不阻断建队。
        """
        pool: list[MemberSpec] = []
        seen: set[tuple[str, str]] = set()
        for base in auto or []:
            if not base.provider_name:
                continue
            models: list[str] = []
            pc = self.cfg.providers.get(base.provider_name)
            if pc is not None and pc.models:
                models = [str(m) for m in pc.models if str(m).strip()]
            if base.model and base.model not in models:
                models.insert(0, base.model)
            for m in models:
                key = (base.provider_name, m)
                if key in seen:
                    continue
                seen.add(key)
                if m == base.model and base.provider is not None:
                    # 当前模型条目直接复用自动池已构建的 provider 实例：
                    # 重建会多消耗一次构建（测试注入的脚本池因此整体错位）
                    pool.append(MemberSpec(
                        provider_name=base.provider_name, model=m,
                        provider=base.provider,
                    ))
                    continue
                spec = MemberSpec(provider_name=base.provider_name, model=m)
                try:
                    provider = self._build_member_provider(base.provider_name, m)
                    spec.provider = provider
                    if not spec.model:
                        spec.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 展开失败降级，不阻断建队
                    spec.build_error = str(e)[:200]
                pool.append(spec)
        return pool or list(auto or [])

    def _template_team_specs(self, template: dict) -> list[TeamMemberSpec]:
        """团队模板 → TeamMemberSpec：先补 model 再去重（与 _resolve_team_members
        同口径），被（服务, 模型）重复合并掉的角色由调用方发 Notice。

        模板的 provider 全空是常态（内置「团队·交付小组」就是）：此时实际
        入册人数受已配置 Key 的服务数限制，重复角色在这里合并，而不是建队时
        静默截断。
        """
        roles = (template or {}).get("roles") or []
        auto: list[MemberSpec] | None = None
        cursor = 0
        specs: list[TeamMemberSpec] = []
        seen: set[tuple[str, str]] = set()
        for role in roles:
            if not isinstance(role, dict):
                continue
            name = str(role.get("name") or "").strip()
            persona = str(role.get("prompt") or "").strip()
            provider_name = str(role.get("provider") or "").strip()
            model = str(role.get("model") or "").strip()
            if provider_name:
                spec = TeamMemberSpec(
                    name=name, provider_name=provider_name, model=model,
                    persona=persona,
                    context_limit_tokens=self._team_provider_context_limit(provider_name),
                )
                try:
                    provider = self._build_member_provider(provider_name, model)
                    spec.provider = provider
                    if not spec.model:
                        spec.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 构建失败降级错误成员
                    spec.build_error = str(e)[:200]
            else:
                if auto is None:
                    auto = self._resolve_members(None, False, chair_key=("", ""))
                    # 单 Key 多模型（服务配了 models 列表）时按「服务 × 模型」展开
                    # 补位池：角色尽量各得一个独立（服务, 模型）键，少被去重合并。
                    # 展开序 = 各服务当前模型在前、其余 models 依次在后，只有
                    # 单模型服务时与旧自动池逐项一致。
                    auto = self._expand_auto_pool(auto)
                if not auto:
                    spec = TeamMemberSpec(
                        name=name, persona=persona,
                        build_error="未指定模型且没有已配置 Key 的服务",
                    )
                else:
                    src = auto[cursor % len(auto)]
                    cursor += 1
                    spec = TeamMemberSpec(
                        name=name, provider_name=src.provider_name, model=src.model,
                        provider=src.provider, build_error=src.build_error,
                        persona=persona,
                        context_limit_tokens=self._team_provider_context_limit(
                            src.provider_name
                        ),
                    )
            key = (spec.provider_name, spec.model)
            if key in seen:
                continue  # 与 team.py 的 (服务, 模型) 同键去重一致（含空键）
            seen.add(key)
            specs.append(spec)
        return specs

    @staticmethod
    def _template_allowed_tools(template: dict) -> list[str]:
        """模板声明的具名工具（run_command 走前缀通道，不进 allowed_tools）。"""
        names: list[str] = []
        for entry in effective_tool_entries((template or {}).get("roles") or []):
            base = entry.split(":", 1)[0]
            if base == _RUN_COMMAND:
                continue
            if base not in names:
                names.append(base)
        return names

    def _team_registry_for_template(
        self, full: ToolRegistry, template: dict,
    ) -> ToolRegistry:
        """按模板的 team 级工具并集 + needs 上限裁剪执行型工具表。

        这是**可见性**筛选，不是安全边界（团队每个成员共用同一 registry，
        本期不做逐成员隔离）：真正的放行/拦截仍由 GrantedGate / 会话门判定。
        needs=read 只留 READONLY；needs=write 只留 READONLY + WRITE；
        needs=exec 才可能把 DANGEROUS 工具挂上（run_command 仍只在命令前缀
        命中时由 GrantedGate 放行，delete_file 等永远逐次确认）。
        """
        needs = derive_needs((template or {}).get("roles") or [])
        entries = effective_tool_entries((template or {}).get("roles") or [])
        names = [entry.split(":", 1)[0] for entry in entries]
        subset = full.subset(names)
        if needs == "read":
            return ToolRegistry(
                [t for t in subset.all() if t.safety == Safety.READONLY]
            )
        if needs == "write":
            return ToolRegistry(
                [t for t in subset.all()
                 if t.safety in (Safety.READONLY, Safety.WRITE)]
            )
        return subset

    def _team_budget_check(
        self, run_budget: RunBudget | None = None,
        run_tokens: dict | None = None,
    ) -> Callable[[], bool]:
        """团队的预算越线判定钩子（二期）：接既有每日 token 预算护栏路径。

        与 send() 入口的护栏同口径：cfg.daily_token_budget > 0 且当日
        usage_log 汇总（store.usage_today）已达上限即「越线」；演示模式不产生
        真实费用，豁免同既有判定。读不到用量（usage_today 回落 None，2026-11
        except 审计 #9 取严口径下护栏/团队钩子仍按放行）按未越线放行——坏掉
        的仪表不该停掉团队，费用护栏由宿主其余路径兜底（编排器对钩子异常同样
        按未越线处理，这里不吞第二次）。异步实现：usage_today 是 async，编排器的
        _budget_over 对 awaitable 返回值照常等待。

        模板预算（本期）：run_budget 非空时先把跨轮累计的 token（run_tokens
        同一个 dict，挂在编排器上跨轮复用）回填进去，再判 calls/tokens/seconds
        三线；模板预算与每日预算任一越线都算越线。团队只在三个推进点检查
        （成员轮/总管轮/自动循环），不宣称成员轮中途熔断。
        """

        async def check() -> bool:
            if run_budget is not None:
                if run_tokens is not None:
                    run_budget.add_tokens(int(run_tokens.get("total", 0) or 0))
                if run_budget.reason():
                    return True
            budget = int(self.cfg.daily_token_budget or 0)
            if budget <= 0:
                return False
            if getattr(self.provider, "demo_mode", False):
                return False
            used_today = await self.store.usage_today()
            # None = 用量库读不到（审计 #9 取严口径下护栏/团队钩子按放行）
            return used_today is not None and used_today >= budget

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
        gate: PermissionGate | None = None, working_dir: Path | None = None,
        director_mode: str = "user", director_params: dict | None = None,
        template_name: str = "",
        template: dict | None = None, grant: dict | None = None,
        collab_tokens: dict | None = None,
    ) -> TeamOrchestrator:
        """为本会话建队（chat.send team=true 且无活动团队）：发 TeamStarted 并登记。

        emit 是编排器形状的事件回调（收 pydantic 事件，_team_body 传入的
        team_emit 已带用量归属）。成员 Agent 的工具表用本会话同款全量注册表
        （改动记录进同一 recorder，队员写的文件照常进会话检查点）；gate /
        working_dir 由调用点传会话自己的（_team_body 传 runtime 的 agent.gate
        与 agent.working_dir）：权限门注入会话既有门——会话级权限档覆盖更严时
        队员同步更严，不回退引擎默认档；工作目录用会话自己的（快聊 / 跨项目
        分屏列的团队落在本会话目录里干活，不借引擎当前目录）。编排器经
        TeamMemberGate 只加「来自队员」来源标注、判定零改变（team.py 的
        TeamMemberGate 契约）。gate 缺省 None 按编排器 fail-closed 降级为
        全员顾问型（没有执行工具表），不构成第二套判定。
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
        template_name（四期常驻）：建队来源的模板名（前端「从模板创建」带上），
        记进编排器快照，供 team.resident_set 把队伍绑回模板（定时触发按
        模板匹配复用的依据）；手选成员建队传空串。
        """
        tpl = template if isinstance(template, dict) else None
        tpl_agg = (tpl or {}).get("aggregator") or {}
        grant_info = grant if isinstance(grant, dict) else {}
        read_only = bool(grant_info.get("read_only"))
        grant_scope = str(grant_info.get("scope") or "ask_each")
        if grant_scope not in ("run", "ask_each"):
            grant_scope = "ask_each"
        run_budget = RunBudget(**((tpl or {}).get("budget") or {})) if tpl is not None else None
        tokens = collab_tokens if isinstance(collab_tokens, dict) else {"total": 0}

        # 总管形态：模板 aggregator.provider 非空 = AI 总管（独立 temp provider），
        # 模板自己的收口配置优先；模板没配收口时显式传入的 director_mode="ai"
        # 仍然生效——此前无条件被覆盖成 "user"，显式参数静默吞掉、无任何提示。
        # 两者都没有才是用户总管（模板明确「没人替你验收」）。
        director: DirectorSpec | None = None
        effective_director_mode = director_mode

        def _build_director(d_provider: str, d_model: str) -> DirectorSpec:
            spec = DirectorSpec(
                provider_name=d_provider, model=d_model,
                context_limit_tokens=self._team_provider_context_limit(d_provider),
            )
            try:
                provider = self._build_member_provider(d_provider, d_model)
                spec.provider = provider
                if not spec.model:
                    spec.model = getattr(provider, "model", "")
            except Exception as e:  # noqa: BLE001 - 构建失败降级，不阻断建队
                spec.build_error = str(e)[:200]
            return spec

        if tpl is not None:
            d_provider = str(tpl_agg.get("provider") or "").strip()
            d_model = str(tpl_agg.get("model") or "").strip()
            if d_provider:
                effective_director_mode = "ai"
                director = DirectorSpec(
                    provider_name=d_provider, model=d_model,
                    context_limit_tokens=self._team_provider_context_limit(d_provider),
                    system_prompt=str(tpl_agg.get("prompt") or "").strip(),
                )
                try:
                    provider = self._build_member_provider(d_provider, d_model)
                    director.provider = provider
                    if not director.model:
                        director.model = getattr(provider, "model", "")
                except Exception as e:  # noqa: BLE001 - 构建失败降级，不阻断建队
                    director.build_error = str(e)[:200]
            elif director_mode == "ai":
                d_provider = str((director_params or {}).get("provider", "") or "").strip()
                d_model = str((director_params or {}).get("model", "") or "").strip()
                if not d_provider:
                    raise TeamError(
                        'director_mode="ai" 需要指定担任总管的模型服务（director.provider）'
                    )
                director = _build_director(d_provider, d_model)
                effective_director_mode = "ai"
        elif director_mode == "ai":
            d_provider = str((director_params or {}).get("provider", "") or "").strip()
            d_model = str((director_params or {}).get("model", "") or "").strip()
            if not d_provider:
                raise TeamError(
                    'director_mode="ai" 需要指定担任总管的模型服务（director.provider）'
                )
            director = _build_director(d_provider, d_model)
        specs = (
            self._template_team_specs(tpl)
            if tpl is not None else self._resolve_team_members(members_params)
        )
        if tpl is not None:
            declared = len([r for r in (tpl.get("roles") or []) if isinstance(r, dict)])
            if len(specs) < declared:
                await emit(NoticeEvent(
                    message=f"模板的 {declared} 个角色去重后组成 "
                            f"{len(specs)} 名队员：有的服务没配 Key，或角色重复"
                ))
        # 模板 max_calls 是 provider 调用次数（含重试/格式纠错），不是轮次：
        # 给每个成员与 AI 总管的 provider 套一层计数代理，团队链路（含小会）
        # 的每次真实 stream 都会 spend()（审查 finding 15）。
        if run_budget is not None:
            for spec in specs:
                if spec.provider is not None:
                    spec.provider = BudgetedProvider(spec.provider, run_budget)
            if director is not None and director.provider is not None:
                director.provider = BudgetedProvider(director.provider, run_budget)
        full_registry = self._build_full_registry(recorder)
        registry = full_registry
        if tpl is not None:
            registry = self._team_registry_for_template(full_registry, tpl)
            if read_only:
                # 「先只读跑一遍」：工具表只留 READONLY（不挂写/执行），且不授予
                # 任何具名写工具或命令前缀
                registry = ToolRegistry(
                    [t for t in full_registry.all() if t.safety == Safety.READONLY]
                )
        # 运行级授权（grant.scope=run）：只包一层 GrantedGate——写工具限工作
        # 目录内、命令限模板声明的前缀；删除语义 / 引擎主目录 / 目录外写入 /
        # DANGEROUS 一律回退会话门逐次确认。成员 Agent 在建队时缓存 gate，
        # 授权范围因此按团队生命周期固定（不做逐轮变更）。
        team_gate = gate
        if gate is not None and (read_only or grant_scope == "run"):
            if read_only:
                team_gate = GrantedGate(
                    gate, allowed_tools=(), command_prefixes=(),
                    run_id=uuid4().hex[:12],
                    label=(
                        f"模板「{tpl.get('name', '')}」只读运行"
                        if tpl is not None else "团队只读运行"
                    ),
                )
            elif grant_scope == "run":
                if tpl is not None:
                    team_gate = GrantedGate(
                        gate,
                        allowed_tools=self._template_allowed_tools(tpl),
                        command_prefixes=collect_command_prefixes(tpl.get("roles") or []),
                        run_id=uuid4().hex[:12],
                        label=f"模板「{tpl.get('name', '')}」",
                    )
                else:
                    # team=true 直发（无模板声明）：按「本次运行免确认」的同一
                    # 安全边界放行全部常规写工具；GrantedGate 仍收口——删除
                    # 语义 / 引擎主目录 / 工作目录外 / DANGEROUS 一律回退会话门
                    # 逐次确认。命令不预授权（没有模板声明的前缀清单可依）。
                    team_gate = GrantedGate(
                        gate,
                        allowed_tools=[
                            t.name for t in full_registry.all()
                            if t.safety == Safety.WRITE
                        ],
                        command_prefixes=(),
                        run_id=uuid4().hex[:12],
                        label="团队（显式成员）",
                    )
        if tpl is not None and read_only:
            await emit(NoticeEvent(
                message="本次按「只读」运行模板：队员只挂只读工具，不写文件、不运行命令。"
            ))
        team_cfg = (
            TeamCfgView(self.cfg.team, run_budget)
            if run_budget is not None else self.cfg.team
        )
        budget_note = (
            (lambda: run_budget.reason_label()) if run_budget is not None else None
        )
        orch_kwargs = dict(
            emit=emit,
            working_dir=working_dir,
            registry=registry,
            gate=team_gate,
            cfg=team_cfg,
            max_iterations=self.cfg.subagent_max_iterations,
            restrict_to_workdir=self.cfg.restrict_to_workdir,
            session_id=sid,
            job_containment=self.cfg.shell.job_containment,
            sandbox_level=self.cfg.shell.sandbox_level,
            use_ripgrep=self.cfg.search.use_ripgrep,
            hooks=self.hooks,
            mods=self.mods,
            director_mode=effective_director_mode,
            director=director,
            budget_check=self._team_budget_check(run_budget, tokens),
            context_limit_tokens=self.cfg.context_limit_tokens,
            compaction_keep_recent=self.cfg.compaction_keep_recent,
            compaction_trigger=self.cfg.compaction_trigger,
            compaction_auto=self.cfg.compaction_auto,
            # 频道消息逐条落库（三期）：收队/重启后仍可按 team_id 回放全量
            message_sink=self._team_message_sink(sid),
            source_template=str((tpl or {}).get("name") or template_name),
            isolate_exec=bool(getattr(self.cfg.team, "isolated_tasks", False)),
        )
        # 预算越线文案：模板预算注入精确原因（次数 / 时长 / token），
        # 未带模板时团队沿用内置的 token 预算句
        if budget_note is not None:
            orch_kwargs["budget_note"] = budget_note
        orch = TeamOrchestrator(**orch_kwargs)
        # 模板预算与跨轮 token 累计挂编排器：同一支队伍的第二条消息起不再
        # 重读模板，预算线必须随团队生命周期延续
        orch.collab_budget = run_budget
        orch.collab_tokens = tokens
        await orch.create_team(specs)  # TeamStarted 在这里发出；超上限截断发 Notice
        self._teams[sid] = orch
        self._teams_interrupted.pop(sid, None)
        self._persist_team_state()
        return orch

    @staticmethod
    def _team_meta(orch: TeamOrchestrator, template_name: str = "") -> dict:
        """assistant 消息的团队 meta：{"mode": "team", "team": {…}}，仿 roundtable meta。

        Message 的元数据槽只有 roundtable 一个（messages.py 未设通用 meta 字段），
        团队摘要搭这个槽随会话落库/下发，前端按 mode == "team" 与圆桌卡分流。
        team 块自带建队分配的 team_id（三期）：前端回放全量频道消息
        （team.log）就凭 meta 里这个 id，收队/重启后仍然可用。

        模板身份（本期）同时放在顶层 template / template_kind：卡头与历史回放
        不依赖 team 快照的字段演进（快照里的 template 是常驻绑定的同一来源）。
        """
        meta: dict = {"mode": "team", "team": orch.snapshot()["team"]}
        name = str(template_name or getattr(orch, "source_template", "") or "").strip()
        if name:
            meta["template"] = name
            meta["template_kind"] = "team"
        return meta

    async def _team_body(
        self, text: str, emit: EmitFn, members_params: list | None,
        agent: Agent, sid: str, images: list[ImageBlock] | None = None,
        allow_create: bool = False, recorder: ChangeRecorder | None = None,
        plan_mode: bool = False,
        director_mode: str = "user", director: dict | None = None,
        template_name: str = "",
        template: dict | None = None, grant: dict | None = None,
        local: bool = True,
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

        emit_ev = _dump_emit(emit)

        # 远程驱动已有团队：GrantedGate 是建队时按「本机用户点了本次运行免
        # 确认」发的，不能由后续远程消息继续搭乘（审查 finding 6）。永久
        # 收回为内部门逐次确认（fail-closed；本机要恢复免确认需重新建队）。
        if orch is not None and not local:
            team_gate = getattr(orch, "_gate", None)
            if isinstance(team_gate, GrantedGate):
                orch.set_gate(object.__getattribute__(team_gate, "_inner"))
                await emit_ev(NoticeEvent(
                    message="本次由远程连接操作该团队，模板的免确认授权已收回，"
                            "后续操作改为逐次确认。"
                ))

        # 用量按事件流归属：usage 事件先于其定稿 TeamMessage 到达，攒到定稿时
        # 记到名下（MemberTurnResult 的用量不随返回值交给宿主，事件流是逐成员
        # 归属的唯一来源）。总管轮与小会主席的 usage 先于 from="director" 的
        # 定稿到达——冲进 director_acc，行键 provider/model 取 director_info
        # （_flush_team_usage），不与队员混账。极端取消时序下剩余用量挂给最后
        # 定稿的归属方，不凭空丢账。
        # 模板预算：创建轮从模板构造；同一支队伍后续消息复用编排器上挂着的
        # 那份（预算线随团队生命周期延续，不因每轮新建而重置）。token 计数用
        # 同一个 dict 跨轮累计，并回填进 RunBudget（add_tokens 取 max，
        # 重复回填不重复累加）。
        run_tokens = getattr(orch, "collab_tokens", None) if orch is not None else None
        if not isinstance(run_tokens, dict):
            run_tokens = {"total": 0}
        run_budget = getattr(orch, "collab_budget", None) if orch is not None else None

        usage_acc: dict[str, list[int]] = {}  # 成员名 -> [input, output]
        director_acc = [0, 0]  # 总管（AI 模式）累计 [input, output]
        pending_usage: list = []

        async def team_emit(ev) -> None:
            if ev.kind == "usage":
                pending_usage.append(ev)
                # 模板预算的 token 线吃**全部** provider 用量（成员 + 总管 +
                # 小会主席），不依赖定稿归属结算的时序——预算检查点随时能读到
                run_tokens["total"] = int(run_tokens.get("total", 0) or 0) + int(
                    ev.input_tokens
                ) + int(ev.output_tokens)
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
                    # 会话私有门与会话自己的工作目录（本轮 agent 即 runtime.agent）：
                    # 队员判定跟会话走，不回退引擎默认档（会话档更严时队员同步更严）
                    gate=agent.gate, working_dir=agent.working_dir,
                    director_mode=director_mode, director_params=director,
                    template_name=template_name,
                    template=template, grant=grant,
                    collab_tokens=run_tokens,
                )
                # _team_create 内已经建好并被预算钩子引用的那一份才是执法
                # 实例：这里绝不能再用本轮临时构造的 run_budget 覆盖它，
                # 否则 orch.collab_budget 的 calls/tokens 永远是 0，meta 也
                # 读不到越线原因（审查 finding 16）。
                orch.collab_tokens = run_tokens
                run_budget = orch.collab_budget
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
                # 总管不挂文件工具：用户点名的项目文件在这里只读预取进总管
                # 下一轮唤醒（与圆桌/对抗同一套 _preload_mentioned_files）。
                # 不进频道、不落库——频道里仍是用户原文，队员看不到快照。
                director_context = ""
                if text:
                    try:
                        director_context = self._preload_mentioned_files(
                            text, agent.history, getattr(agent, "working_dir", None)
                        )
                    except Exception:  # noqa: BLE001 - 预取失败不拦团队轮
                        director_context = ""
                if director_context:
                    await emit_ev(NoticeEvent(message=(
                        "团队总管没有文件工具：已把你点名的项目文件（只读）带给总管，"
                        "最多 3 个，过长会截取；频道里仍是你的原文。"
                    )))
                if orch.loop_running:
                    # 循环推进中的插话：只进频道并排进总管下一轮（最高优先级段），
                    # 不重复驱动循环。若 inject 返回时循环恰好已收束（竞态窗口），
                    # 插话留在待注入清单里，随本会话下一条消息一并处理
                    result = await orch.inject_user_message(
                        text, images=images, director_context=director_context,
                    )
                else:
                    result = await orch.run_auto_turn(
                        text, images=images, director_context=director_context,
                    )
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
                stopped_msg.roundtable = self._team_meta(
                    orch,
                    str((template or {}).get("name") or template_name or ""),
                )
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
                if self._teams.pop(sid, None) is not None:
                    self._release_team_providers(orch)
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
            if self._teams.pop(sid, None) is not None:
                self._release_team_providers(orch)
            self._teams_interrupted.pop(sid, None)
            self._persist_team_state()

        meta = self._team_meta(
            orch, str((template or {}).get("name") or template_name or "")
        )
        if run_budget is not None and run_budget.reason():
            meta["budget_exceeded"] = run_budget.reason()
            meta["budget_note"] = run_budget.reason_label()
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
        """按 params.session_id（缺省活动会话）取活动团队；没有就报参数错。

        会话归属兜底（安全审查 M03）：session_id 键存在但为空串——远程连接
        未绑定会话时由 app 层收敛注入（_team_scoped_params）——按「没有团队」
        处理，不回退全局活动会话。空串落进 ``or`` 链会重新拿到
        backend.session，全局指针切走后收敛对象随之漂移，fail closed 形同虚设。
        """
        raw = params.get("session_id")
        sid = str(raw) if raw is not None else (self.session.id if self.session else "")
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
        时附带 interrupted 记录供前端提示。会话归属兜底见 _team_for（M03）：
        显式空串按「无会话」处理，不回退全局活动会话。
        """
        raw = params.get("session_id")
        sid = str(raw) if raw is not None else (self.session.id if self.session else "")
        orch = self._teams.get(sid)
        if orch is not None:
            team = orch.snapshot()["team"]
            # 常驻标记（T3.2）：只贴在 team.get 的回包上，不进 snapshot——
            # meta 落库形状保持不变，前端团队卡凭它点亮「常驻」开关
            team["resident"] = self.resident_teams.is_resident(orch.team_id)
            return {"active": True, "session_id": sid, "team": team}
        out: dict = {"active": False, "session_id": sid, "team": None}
        if sid in self._teams_interrupted:
            out["interrupted"] = self._teams_interrupted[sid]
        return out

    async def team_room(self, params: dict) -> dict:
        """team.room：团队室聚合探针（页签激活 / 会话切换 / 断线重连各调一次）。

        一次带回 team.get 形状的快照块 + latest_seq（频道最新消息序号，供前端
        校准未读 badge）+ stats（工单按状态计数与成员用量，供成员带与工单板
        首画免二次推断），替代「team.get + 从频道尾部推断最新 seq」的拼装。
        只读方法、零新安全语义：session_id 轴与 team.get 同一口径（非本机连接
        的 session_id 已在 WS 入口被 _team_scoped_params 收敛）；team_id 轴为
        常驻团队（T3.2）预留的主轴，一期前端不传——收到时沿用 team.log 的
        三态归属判定（活动命中 / 落库行归属 / 外来），判定不过按同文报
        TeamError。无活动团队时 active=false、team=null、latest_seq=0，有中断
        记录时附 interrupted（同 team.get 口径）。
        """
        raw = params.get("session_id")
        sid = str(raw) if raw is not None else (self.session.id if self.session else "")
        team_id = str(params.get("team_id", "") or "").strip()

        def _room_active(orch) -> dict:
            team = orch.snapshot()["team"]
            # 常驻标记（T3.2）：与 team.get 回包同款贴法——「同一快照源，
            # 页签与卡不两套」的契约对常驻开关同样成立
            team["resident"] = self.resident_teams.is_resident(orch.team_id)
            msgs = orch.channel.messages
            return {
                "active": True, "session_id": sid, "team": team,
                "latest_seq": msgs[-1].seq if msgs else 0,
                "stats": _room_stats(team),
            }

        if team_id:
            # team_id 轴（T3.2 预留）：归属判定沿用 team.log 的三态口径（§15.2
            # 逐字同款）——活动命中（活动团队 team_id 相等）或落库行归属本会话，
            # 任一命中即放行；库内存在外来行且两证皆无，按 team.log 同文报
            # TeamError；完全未知 / 未落库的 team_id 不拒绝——照 team.log 回空
            # （空结果没有可泄露的内容）。判定过了但团队不在册（已收队/重启）
            # 同样回空态——频道回放走 team.log，本方法只服务活动团队的首画。
            orch = self._teams.get(sid)
            active_hit = orch is not None and bool(orch.team_id) and orch.team_id == team_id
            rows = (
                await self.store.list_team_messages(team_id)
                if self.store is not None else []
            )
            owned = bool(sid) and any(r.get("session_id") == sid for r in rows)
            foreign = any(r.get("session_id") and r.get("session_id") != sid for r in rows)
            if not active_hit and not owned and foreign:
                raise TeamError("team_id 不属于当前会话：不能回放其他会话的团队频道")
            if not active_hit:
                return {"active": False, "session_id": sid, "team": None,
                        "latest_seq": 0, "stats": _room_empty_stats()}
            return _room_active(orch)

        out: dict = {"active": False, "session_id": sid, "team": None,
                     "latest_seq": 0, "stats": _room_empty_stats()}
        orch = self._teams.get(sid)
        if orch is not None:
            return _room_active(orch)
        if sid in self._teams_interrupted:
            out["interrupted"] = self._teams_interrupted[sid]
        return out

    async def _push_team_task_event(self, sid: str, task: dict) -> None:
        """轮外广播工单板变化（team_task_updated，含 session_id）。

        用户在工单卡上的改板（team.task_add / team.task_update）不经过任何
        轮次发射链：编排器手里那份 emit 是上一轮的旧闭包，闲置时经它发出的
        帧到不了前端——主会话团队卡靠请求响应就地更新，右侧团队室面板没有
        这条路，就停在旧状态。这里按轮内事件同形状直接广播给在线连接；与
        轮内事件重复时前端按工单编号幂等覆盖，无害。
        """
        ev = {
            "kind": "team_task_updated",
            "session_id": sid,
            "task_id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "assignee": str(task.get("assignee") or ""),
            "type": str(task.get("type") or "exec"),
            "status": str(task.get("status") or "pending"),
            "redo": int(task.get("redo") or 0),
            "detail": str(task.get("detail") or ""),
            "source": dict(task.get("source") or {}),
            "auto_dispatch": bool(task.get("auto_dispatch") or False),
        }
        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit(ev))
            except RuntimeError:
                pass

    async def team_task_add(self, params: dict) -> dict:
        """team.task_add：登记工单（待办）。指派对象必须在名册内，依赖必须存在。"""
        sid, orch = self._team_for(params)
        deps = params.get("deps")
        task = await orch.task_add(
            str(params.get("title", "")),
            str(params.get("assignee", "")),
            type=str(params.get("type") or "exec"),
            accept=str(params.get("accept", "") or ""),
            deps=[str(d) for d in deps] if isinstance(deps, list) else None,
        )
        await self._push_team_task_event(sid, task)
        return {"task": task}

    async def adversarial_to_team_tasks(self, params: dict) -> dict:
        """从已落库对抗元数据创建受控顾问工单，不接受客户端提交发现正文。

        来源校验只认服务端持久化的消息（seq + 当前会话归属）；发现的裁决必须
        是 confirmed / partial，正文与依据一律从库内 meta 取，客户端只能选 id
        与指派对象。只登记待办、不自动派工：自动循环在飞时整单拒绝，避免
        「转单即被派发」绕过用户的显式操作。
        """
        sid, orch = self._team_for(params)
        if self.store is None:
            raise TeamError("当前会话存储不可用，无法验证对抗发现来源")
        if orch.loop_running:
            raise TeamError("总管正在自动推进中：请等本轮结束后再转为工单")
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise TeamError("无效的对抗消息序号") from None
        if seq <= 0:
            raise TeamError("缺少对抗消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.adversarial:
            raise TeamError("指定消息不是已保存的对抗审查结果")
        meta = message.adversarial
        if meta.get("status") != "done":
            raise TeamError("仅已完成的对抗审查可以转为工单")
        wanted = params.get("finding_ids")
        ids = list(dict.fromkeys(str(v) for v in wanted if v)) if isinstance(wanted, list) else []
        if not ids or len(ids) > 20:
            raise TeamError("请选择 1 至 20 条发现")
        selected = []
        for finding in meta.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            fid = str(finding.get("id") or "")
            if fid not in ids:
                continue
            verdict = finding.get("verdict")
            if verdict not in ("confirmed", "partial"):
                raise TeamError(f"发现 {fid} 尚未裁定成立或部分成立")
            selected.append(finding)
        if {str(f.get("id") or "") for f in selected} != set(ids):
            raise TeamError("部分发现不属于该对抗结果")
        assignee = str(params.get("assignee") or "")
        tasks = await orch.tasks_from_adversarial(seq=seq, findings=selected, assignee=assignee)
        return {"tasks": tasks}

    async def roundtable_to_team_task(self, params: dict) -> dict:
        """把已落库的圆桌融合结论登记为一张工单（P5 跨形态流转：圆桌 → 团队）。

        来源校验只认服务端持久化的消息（seq + 当前会话归属）：工单 detail=
        融合结论全文（库内 assistant 正文）、title=结论首行截断，不接受客户端
        提交的正文。对照模式没有融合结论、已取消的圆桌不受理。只登记待办、
        不派工（自动循环在飞时整单拒绝，与对抗转工单同姿态）；同一来源幂等。
        """
        sid, orch = self._team_for(params)
        if self.store is None:
            raise TeamError("当前会话存储不可用，无法验证圆桌结论来源")
        if orch.loop_running:
            raise TeamError("总管正在自动推进中：请等本轮结束后再转为工单")
        try:
            seq = int(params.get("seq", 0))
        except (TypeError, ValueError):
            raise TeamError("无效的圆桌消息序号") from None
        if seq <= 0:
            raise TeamError("缺少圆桌消息序号")
        message = await self.store.get_full_message_at(sid, seq)
        if message is None or not message.roundtable:
            raise TeamError("指定消息不是已保存的圆桌结论")
        if message.roundtable.get("mode") == "compare":
            raise TeamError("对照模式没有融合结论，不能转为工单")
        if message.roundtable.get("status") == "cancelled":
            raise TeamError("该圆桌已取消，不能转为工单")
        text = message.text.strip()
        if not text:
            raise TeamError("该圆桌没有可用的融合结论")
        title = text.splitlines()[0].strip()[:60] or f"圆桌结论（seq {seq}）"
        task = await orch.task_from_roundtable(
            seq=seq, title=title, detail=text,
            assignee=str(params.get("assignee") or ""),
        )
        return {"task": task}

    async def team_task_update(self, params: dict) -> dict:
        """team.task_update：工单状态流转（板只由用户变更，成员不自行改板）。

        待办→进行中→待验收→完成；待验收→进行中视为打回（redo+1，超上限拒绝）；
        status="error" 走失败收尾（用户放弃，待办/进行中/待验收均可）。置为
        进行中（派工/重派/打回重做）后代发一条 director 的 assign 频道消息，
        让被派队员在频道里看到派工。
        """
        sid, orch = self._team_for(params)
        status = str(params.get("status", ""))
        task = await orch.task_update(str(params.get("task_id", "")), status)
        assign_msg = None
        if status == "in_progress":
            assign_msg = await orch.notify_assign(
                str(task["id"]), note=str(params.get("note", "") or "")
            )
        await self._push_team_task_event(sid, task)
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
        if self._teams.pop(sid, None) is not None:
            self._release_team_providers(orch)
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
        if self._teams.pop(sid, None) is not None:
            self._release_team_providers(orch)
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
            "cron_resume_limit": t.cron_resume_limit,
            "isolated_tasks": t.isolated_tasks,
            "config_hint": (
                "团队让多个模型分工协作：总管拆解目标、登记工单，队员按工单真实"
                "读写文件、运行命令（照常走权限确认），完成后交付。成员来自「模型"
                "服务」里已配好 Key 的服务，在输入框的团队面板勾选并写一句话人设；"
                "下面的上限用于防止自动循环失控。「无人值守续跑」是定时团队任务里"
                "总管空闲时自动催续跑的次数（0 = 空闲即收队）。改动对之后新建"
                "的团队生效，进行中的团队沿用建队时的值。"
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
        if params.get("cron_resume_limit") is not None:
            updates["cron_resume_limit"] = max(0, min(3, int(params["cron_resume_limit"])))
        if params.get("isolated_tasks") is not None:
            updates["isolated_tasks"] = bool(params.get("isolated_tasks"))
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
        （应用模板时走 chat.send 既有解析路径，缺 Key 照常降级错误成员）。

        协作模板扩展（本期，全部可选）：needs / tools_allow / tools_deny /
        budget——缺省时同名沿用旧值（旧前端/旧调用方保存不会把已声明的授权
        面抹掉）。内置名保留（内置团队模板只在代码里、不落盘）；与协作模板册
        重名拒绝（两册的 remove/preview 都按 source 消歧）。
        """
        # 先用团队模板册自己的名称校验：空名/路径/冒号等错误按团队模板口径
        # 报（与旧接口文案一致），再跑协作扩展字段的校验
        name_raw = validate_team_template_name(str(params.get("name", "") or ""))
        if is_builtin_template_name(name_raw):
            raise TeamError(
                f"「{name_raw}」是内置模板名，不能覆盖；请改名后另存"
                f"（例如「{name_raw} · 我的版本」）"
            )
        if any(t.name == name_raw for t in self.collab_templates.templates):
            raise TeamError(f"已有同名的协作模板：{name_raw}，请换一个名字")
        old = next(
            (t for t in self.team_templates.templates if t.name == name_raw), None
        )
        raw_members = params.get("members")
        members: list[TeamTemplateMember] = []
        for m in (raw_members if isinstance(raw_members, list) else []):
            if not isinstance(m, dict):
                continue
            member_name = str(m.get("name", "") or "").strip()
            persona = str(m.get("persona", "") or "").strip()
            if len(persona) > TEAM_TEMPLATE_PERSONA_MAX:
                raise TeamError(
                    f"成员「{member_name or '未命名'}」的人设过长"
                    f"（最多 {TEAM_TEMPLATE_PERSONA_MAX} 字符）"
                )
            members.append(TeamTemplateMember(
                provider=str(m.get("provider", "") or "").strip(),
                model=str(m.get("model", "") or "").strip(),
                name=member_name,
                persona=persona,
            ))
        raw_director = params.get("director")
        # 可选协作字段：缺省沿用旧值（同名覆盖时不丢授权声明）
        needs = (
            str(params.get("needs") or "").strip()
            if params.get("needs") is not None
            else str(getattr(old, "needs", "read") or "read")
        )
        if isinstance(params.get("tools_allow"), list):
            tools_allow = [str(x) for x in params["tools_allow"]]
        else:
            tools_allow = list(getattr(old, "tools_allow", []) or [])
        if isinstance(params.get("tools_deny"), list):
            tools_deny = [str(x) for x in params["tools_deny"]]
        else:
            tools_deny = list(getattr(old, "tools_deny", []) or [])
        raw_budget = params.get("budget")
        if isinstance(raw_budget, dict):
            budget = _CollabTeamTemplateBudget(**raw_budget)
        elif old is not None and getattr(old, "budget", None) is not None:
            old_budget = old.budget
            budget = (
                old_budget.model_copy(deep=True)
                if hasattr(old_budget, "model_copy")
                else _CollabTeamTemplateBudget(**dict(old_budget))
            )
        else:
            budget = _CollabTeamTemplateBudget()
        # 复用协作模板的条目校验（needs 取值、run_command:<前缀> 的安全提炼、
        # 条目长度/逗号等）与注册表感知检查：团队模板不另起一套宽松口径
        probe = CollabTemplate(
            name=name_raw or "团队模板",
            kind="team",
            roles=[{
                "key": "team", "name": name_raw or "团队",
                "needs": needs, "tools_allow": tools_allow,
                "tools_deny": tools_deny,
            }],
            budget=budget.model_dump(),
        )
        validate_collab_template(probe, allow_builtin=False)
        role = probe.roles[0]
        warnings = self._collab_template_warnings(probe)
        template = _CollabTeamTemplate(
            name=str(params.get("name", "") or ""),
            director_mode=str(params.get("director_mode") or "user"),
            director=TeamTemplateDirector(
                provider=str((raw_director or {}).get("provider", "") or "").strip(),
                model=str((raw_director or {}).get("model", "") or "").strip(),
            ),
            members=members,
            created_at=time.time(),  # 宿主盖戳（模板册自身不感知时钟）
            needs=role.needs,
            tools_allow=list(role.tools_allow),
            tools_deny=list(role.tools_deny),
            budget=_CollabTeamTemplateBudget(**probe.budget.model_dump()),
        )
        self.team_templates.upsert(template)
        return {"template": template.model_dump(), "warnings": warnings}

    async def team_template_remove(self, params: dict) -> dict:
        """team.template_remove：按名删模板（名字精确匹配；不存在报 TeamError）。"""
        name = str(params.get("name", "") or "").strip()
        self.team_templates.remove(name)
        return {"name": name}

    # ---- 常驻团队（T3.2）：设为常驻 / 解除常驻 ----
    # 全局册（与会话无关，team_resident.json 单文件）：登记「哪支队伍跨会话
    # 常驻」与复用所需的工单板快照、频道 seq 水位。写面仅本机（与模板册同
    # 姿态，dispatch 表 local_only=True）；定时团队触发时按（模板，项目）匹配
    # 复用（automation 的 _execute_cron_team_turn），频道历史与工单板延续。

    async def team_resident_set(self, params: dict) -> dict:
        """team.resident_set：把当前会话的活动团队登记为常驻（幂等，重复设刷新）。

        template 的取值优先级：调用方显式带的 > 编排器记录的建队来源模板
        （快照 meta 的 template 键，前端「从模板创建」随 chat.send 的
        team_template 记下——UI 口径不传 template 也绑得回去，定时团队触发
        才能按（模板，项目）匹配复用）> 原登记条目的旧值。绑了模板的常驻
        队伍才会被定时团队任务按（模板，项目）匹配复用；未绑的只登记（界面
        上显示常驻，但没有定时触发面）。非空的 template 必须在模板册里存在，
        防止绑定到永不匹配的悬空名。工单板快照与频道水位取登记时刻的实时
        状态，定时复用前的最后一次刷新以最近一次运行收尾的回写为准。
        """
        sid, orch = self._team_for(params)
        if not orch.team_id:
            raise TeamError("该团队还没有分配 team_id，不能登记常驻")
        template = str(params.get("template") or "").strip() \
            or str(getattr(orch, "source_template", "") or "").strip()
        if template and is_builtin_template_name(template):
            # 内置团队模板只在代码里、不落盘：常驻/定时复用都要按名字回查模板册，
            # 给可执行的出路而不是一句「不存在」
            raise TeamError(
                f"「{template}」是内置团队模板，不能直接设为常驻；请先在 设置 · 多模型协作 里"
                "「复制」成自己的模板再设为常驻"
            )
        if template and not any(
            t.name == template for t in self.team_templates.templates
        ):
            raise TeamError(f"团队模板不存在：{template}")
        sess = await self.store.get_session(sid) if self.store is not None else None
        old = self.resident_teams.get(orch.team_id)
        entry = ResidentTeamEntry(
            team_id=orch.team_id,
            name=str((sess.title if sess else "") or ""),
            template=template or (old.template if old else ""),
            project_id=sess.project_id if sess else None,
            session_id=sid,
            director_mode=orch.director_mode,
            roster=[m.name for m in orch.roster],
            board=[t.to_dict() for t in orch.board.tasks()],
            last_seq=orch.channel.messages[-1].seq if orch.channel.messages else 0,
            wake_count=old.wake_count if old else 0,
        )
        self.resident_teams.upsert(entry)
        return {"ok": True, "team_id": orch.team_id, "resident": True,
                "entry": entry.model_dump()}

    async def team_resident_clear(self, params: dict) -> dict:
        """team.resident_clear：解除常驻登记（未登记报错，不静默）。

        两种口径：缺省按当前会话的活动团队（与 resident_set 对偶）；显式传
        team_id 则直接按全局册解除——已交付/已收队的队伍没有活动团队可认，
        终态团队卡仍要能解除常驻（评审③：否则只能手改 team_resident.json）。
        全局册仅本机写面（dispatch 表 local_only），按 id 解除与模板按名删除
        同姿态，不做会话归属收敛。
        """
        team_id = str(params.get("team_id") or "").strip()
        if not team_id:
            _sid, orch = self._team_for(params)
            team_id = orch.team_id
        self.resident_teams.remove(team_id)
        return {"ok": True, "team_id": team_id, "resident": False}

    def team_resident_get(self, params: dict) -> dict:
        """team.resident_get：按 team_id 探询常驻登记状态（只读）。

        终态团队卡（回放/中断重建的卡没有活动快照）凭它决定是否亮出「解除
        常驻」入口；查无登记回 resident=False，不报错（探询语义）。team_id
        是全局册主轴，无会话归属面可收敛——读面也仅本机（dispatch 表
        local_only），不给远程客户端枚举本机常驻登记的入口。
        """
        team_id = str(params.get("team_id") or "").strip()
        if not team_id:
            raise TeamError("缺少 team_id：常驻登记以 team_id 为主轴")
        entry = self.resident_teams.get(team_id)
        return {"team_id": team_id, "resident": entry is not None,
                "entry": entry.model_dump() if entry is not None else None}

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
        sid = str(params["session_id"]) if params.get("session_id") is not None \
            else (self.session.id if self.session else "")
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

    # ---- 模型/配置操作 ----

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



    # ---- 设置页 ----

    # 字符串型偏好（值域白名单）：theme 值域见模块级 THEME_PREFS
    STRING_PREFS = {
        "theme": THEME_PREFS,
        # 侧栏呈现形式：classic=项目/会话两区（原样式），grouped=会话收进各自项目下
        "sidebar_view": ("classic", "grouped"),
        # 用量页 Token 活动的图表类型：bar=柱状（默认），line=折线
        "usage_chart": ("bar", "line"),
        # 用量页统计口径：all=全部项目 + 快聊（默认），project=仅当前项目
        "usage_scope": ("all", "project"),
        # 「跟随系统」的深浅落点（浅/深各一个具体主题；缺省 = 纸墨 / 夜墨）
        "theme_auto_light": THEME_AUTO_LIGHT,
        "theme_auto_dark": THEME_AUTO_DARK,
        # 更新渠道：stable=稳定版（/releases/latest，默认）、beta=测试版（releases.atom，
        # 含预发布）。值域与 core/uptodate.py 的 CHANNELS 保持一致
        "update_channel": ("stable", "beta"),
        # 终端 Shell：auto=沿用 PowerShell（历史行为），其余显式指定；
        # gitbash/wsl 在系统里找不到时终端给出可读错误，不回退成别的 Shell。
        "term_shell": ("auto", "powershell", "cmd", "gitbash", "wsl"),
        # 终端字体：只收这几个常见等宽字体，值直接进 xterm 的 fontFamily
        "term_font": (
            "Cascadia Mono", "Consolas", "Courier New",
            "JetBrains Mono", "Fira Code", "Sarasa Mono SC",
        ),
        # 交互行为：queue=运行中发送排队（默认），steer=作为引导注入当前轮
        "interaction_mode": ("queue", "steer"),
        # 代码高亮配色（浅色/深色界面各选一套；auto=跟随界面主题）
        "code_theme_light": (
            "auto", "github-light", "solarized-light", "one-light",
        ),
        "code_theme_dark": (
            "auto", "github-dark", "solarized-dark", "one-dark",
        ),
    }

    # ---- 快照 ----


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
            except Exception:  # noqa: BLE001
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
                except Exception:  # noqa: BLE001
                    continue
                open_tabs.append({"id": s.id, "title": s.title})
                if len(open_tabs) >= self.SESSION_TABS_MAX:
                    break
        # 活动会话历史同样窗口化：刷新大会话时 boot 不再整包下发（与
        # session.resume 同款形状——messages 尾窗 + messages_total/messages_offset，
        # 前端 openTabForSession({withMessages}) 原样走窗口渲染，无感）
        boot_session: dict | None = None
        if self.session is not None:
            msgs, total, offset = await self.store.load_messages_window(
                self.session.id, limit=HISTORY_SYNC_LIMIT)
            boot_session = {
                "id": self.session.id, "title": self.session.title,
                "summary": getattr(self.session, "summary", ""),
                "messages": [_msg_brief(m) for m in msgs],
                "messages_total": total,
                "messages_offset": offset,
            }
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
            "session": boot_session,
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
                    "version": s.version,
                    "origin": dict(s.origin),
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
                for name, pc in PRESETS.items()
            ],
            # 崩溃哨兵：上次运行没有正常退出（崩溃/强杀）时为 True，
            # 前端据此提示用户导出诊断包（见 server/app.py 的 lifespan）
            "crashed_last_run": bool(getattr(self, "crashed_last_run", False)),
            "subagent": self.subagent_settings(),
            "update": self.update_info,
            # 自动下载（关于页开关）是否已把该版本的安装包准备好：前端直接
            # 把「一键更新」按钮换成「退出并安装」，不需要重新下载
            "update_downloaded": bool(
                self.update_info
                and self._update_ready(str((self.update_info or {}).get("version") or ""))
            ),
            "update_success": self.update_success,
            "update_failure": self.update_failure,
            "tools": [
                {"name": t.name, "safety": t.safety.value, "description": t.description}
                for t in self._base_agent.registry.all()
            ],
        }


# ---- 内核共用件接线：ChatPipelineMixin / SessionModelMixin / UsageMixin
# ---- 运行期依赖的内核模块级名字 ----
# backend_parts 各模块不得 import backend（backend_parts/__init__.py 的文件头
# 约定：会成环）。ChatPipelineMixin 的 send / _run_turn_pipeline 在运行期使用
# 内核单一事实来源的 StreamDeltaMerger（流式合并契约，设计 §2.0/§6：永不随
# 域迁移）、QueuedTurn、_stamp_turn_estimate 与 _PERSIST_HARVEST_TIMEOUT；
# 这些名字由内核在自身定义完成后单向注入该模块的命名空间，方法体因此保持
# 逐字搬移、零改写（批 5a 硬验证③）。StreamDeltaMerger / QueuedTurn 等的
# 定义仍在本文件（grep 可验），模块路径与测试锚点（tests/test_stream_merge.py、
# tests/test_multi_session.py 从 skysheep.server.backend 取 StreamDeltaMerger）不变。
from .backend_parts import chat_pipeline as _chat_pipeline_module  # noqa: E402

_chat_pipeline_module.StreamDeltaMerger = StreamDeltaMerger
_chat_pipeline_module.QueuedTurn = QueuedTurn
_chat_pipeline_module._stamp_turn_estimate = _stamp_turn_estimate
_chat_pipeline_module._PERSIST_HARVEST_TIMEOUT = _PERSIST_HARVEST_TIMEOUT

# 批 5b：SessionModelMixin 的 fork_session / resume_session 用内核的历史窗口口径
# （HISTORY_SYNC_LIMIT）与历史 brief 组装（_msg_brief / _window_briefs，
# tests/test_message_brief_contract.py 锚定）——定义留本文件，运行期单向注入。
# 轮次日志件（TURN_LOG_LIMIT_* / _load_turn_rows / _turn_row / _log_line_ts）
# 不在此列：turn_breakdown 的形参默认值在导入期求值，必须与定义同模块，故随
# 方法落 usage.py，本文件在导入块里转发这几个名字（tests/test_turn_breakdown.py
# 与 tests/test_app_file_logging.py 的既有导入锚点不变）。
from .backend_parts import session_model as _session_model_module  # noqa: E402

_session_model_module.HISTORY_SYNC_LIMIT = HISTORY_SYNC_LIMIT
_session_model_module._msg_brief = _msg_brief
_session_model_module._window_briefs = _window_briefs
