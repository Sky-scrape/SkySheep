"""ServerBackend：把引擎接线（会话/模型/技能/MCP/子代理/权限）暴露给服务层。

与 CLI 的 ChatApp 共享同一套引擎 API，但不含任何 UI 逻辑——
FastAPI WebSocket 端点消费它并转发事件流。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess  # noqa: F401  测试按模块属性 patch Popen
import sys
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
    update_provider_in_config,
)
from ..core import Agent
from ..core.checkpoints import CheckpointStore
from ..core.collab_templates import (
    CollabTemplateStore,
)
from ..core.hooks import (
    HookRunner,
    hooks_from_config,
    load_raw_config,
)
from ..core.mods_tool import ModTool
from ..core.power import PowerKeeper
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
from ..core.team import TeamOrchestrator
from ..core.team_resident import ResidentTeamStore
from ..mcp import (
    MCPManager,
    load_mcp_configs,
    presets_public,
)
from ..messages import ImageBlock, Message
from ..models import Provider
from ..models.diagnostics import HealthRegistry
from ..models.factory import build_provider
from ..obs import warning as obs_warning
from ..security.gate import (
    PermissionGate,
)
from ..security.trust import WorkspaceTrust
from ..session import SessionStore
from ..skills import SkillLoader
from ..tools import (
    ChangeRecorder,
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
from .backend_parts.collab_body import (  # noqa: F401  两个对抗报告助手供测试从本模块引用
    CollabBodyMixin,
    _adversarial_fingerprints,
    _adversarial_report_markdown,
)
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
                   ProjectMgmtMixin,
                   # D2 多模型协作（批 6 外迁件，方法自主类按块落位）
                   CollabBodyMixin):
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
        # D2 字段所有权（拆分批 6 注记，设计 §3.2）：`_teams` / `_teams_interrupted`
        # / `_team_state_path` 由 CollabBodyMixin（backend_parts/collab_body.py）
        # 独占读写（域内裸访问，load/persist 也在那边）；其它域一律经它的
        # team_active / team_orchestrator / team_orchestrators（只读）与
        # team_detach_for_delete / team_interrupt_for_project_switch（两个写封装），
        # 不许再裸碰这两个 dict。这里只做一次铺齐。
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
        # 跨域口径：`_teams*` 归 D2（批 6 已收敛：五个入口见 CollabBodyMixin）、
        # `_cron_running` / `_pipeline_running` 归 D3（批 4 已收 automation_busy()），
        # D1 只读或经 accessor。
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
        # （lifecycle.py 文件头与 delete_session 行内注记；两域文件头互指）；
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
        for orch in self.team_orchestrators().values():
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
        # 运行中心的孤儿记录：旧版本删会话不清运行条目，备份回退也会让快照
        # 指向库里已不存在的会话——留着会让「打开会话」开出一张空标签，
        # 且 runs.json 继续存着已删会话的标题与原文首段。启动时按库实况清一次
        # （与库整体替换、删项目后的清理同一口径，见 prune_run_center_orphans）。
        dropped = await self.prune_run_center_orphans()
        if dropped:
            logging.getLogger("skysheep").info(
                "运行中心：清理 %d 条会话已不存在的运行记录", dropped)
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

# 批 6：CollabBodyMixin 的 _cancel_team_turn_and_settle 用内核的二次取消收割
# 兜底超时（_PERSIST_HARVEST_TIMEOUT，定义在本文件、批 5a 起同时供
# _run_turn_pipeline）。沿内核单向注入先例：方法体保持逐字搬移、零改写。
from .backend_parts import collab_body as _collab_body_module  # noqa: E402

_collab_body_module._PERSIST_HARVEST_TIMEOUT = _PERSIST_HARVEST_TIMEOUT
