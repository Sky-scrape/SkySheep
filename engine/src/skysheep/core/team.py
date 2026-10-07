"""团队：多模型分工协作（用户总管 MVP + AI 总管闭环，docs/团队模式设计.md）。

与圆桌（core/roundtable.py，会诊）并列的第二种多模型协作——开工：
总管拆解目标 → 派工单 → 队员执行 → 验收 → 交付。

- 一期 · 用户总管：总管固定为用户本人，用户的每条消息就是一道总管指令
  直达团队频道，被 @ 点名的队员按名册顺序逐个唤醒（非并行），广播不唤醒、
  只在其下次被唤醒前注入。
- 二期 · AI 总管闭环（设计 §11）：指定一个已配 Key 的模型担任总管（独立
  history、总管系统提示词、只挂内部团队工具），经 run_auto_turn 自动推进
  拆解→派工→收报→验收→交付；用户保留三权——插话（inject_user_message，
  最高优先级注入总管下一轮）、接管（takeover，切回用户总管）、收队。
  「小会」复用 run_roundtable（emit 全部内收，严禁外泄 Roundtable* 事件，
  只以频道消息呈现）。防失控上限见 _forced_directives 与 run_auto_turn。

三个角色件（复用对照见设计 §10；无模板落盘）：

- TeamChannel：全员共享的频道消息簿，seq 单调递增；单条 2000 字上限，
  超长全文落盘 <工作区>/.skysheep/reports/ 只投摘录（沿 subagent 的
  长报告落盘摘录做法与目录）；按成员维护未读游标，唤醒时只注入
  「频道未读（裁剪）+ 名下工单」，绝不注入他人 history 或全量会话历史。
- TeamTask + TeamBoard：工单与状态机（待办→进行中→待验收→完成；
  待验收打回→进行中且 redo+1）。板只由用户经 WS 方法变更（编排器只代发
  事件），依赖未完成不派发，redo 超上限拒绝打回。
- TeamOrchestrator：编排循环。成员 = 跨阶段存活的独立 Agent 实例（各自
  独立 history）：名下没有进行中的执行型工单按顾问型处理（空工具表）；
  有则挂注入的工作区工具注册表并走**会话既有 PermissionGate**——门经
  TeamMemberGate 代理，只在 PermissionRequest 的 note 上加「来自队员
  『成员名』」来源标注，authorize 判定与决策语义原样委托、零放松
  （设计 §6：权限确认只认用户，总管无权替队员放行）。

安全模型零放松：本模块不新增任何绕过；只读自动放行、写/执行需确认的
既有语义由注入的门原样承担；频道消息纯文本，任何情况下不直接执行。

可单测性：provider / 工具注册表 / 权限门 / emit 回调 / 配置对象全部构造
注入，模块层不触碰真实 ~/.skysheep（未注入工作目录时长文只截断不落盘）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..events import (
    AgentEvent,
    NoticeEvent,
    TeamFinished,
    TeamMessage,
    TeamMessageDelta,
    TeamStarted,
    TeamTaskUpdated,
    Usage,
)
from ..models.base import Provider
from ..security.gate import PermissionGate
from ..textio import write_text_atomic
from ..tools.base import Safety, Tool, ToolContext, ToolError, ToolRegistry
from .agent import Agent
from .prompt import TEAM_DIRECTOR_PROMPT, TEAM_HUDDLE_PROMPT, TEAM_MEMBER_PROMPT
from .roundtable import MemberSpec, run_roundtable

# 发给前端的事件回调（backend 负责转成 WS 帧广播；与 roundtable.EmitFn 同形）
EmitFn = Callable[[AgentEvent], Awaitable[None]]

# 单条频道消息的字符上限（设计 §9：截断 + 全文落盘摘录投递）
MSG_CHAR_LIMIT = 2_000
# 落盘摘录的长度（沿 core/subagent.py 的 REPORT_EXCERPT_CHARS）
REPORT_EXCERPT_CHARS = 1_200
# 落盘全文的单文件体积上限（字符）：超限截断并注记，洪泛消息不再无界写盘
#（无人值守的渠道模式下频道消息即远程输入面，单文件体积必须设防）
REPORT_FILE_CHAR_LIMIT = 100_000
# reports 目录 team-*.md 的滚动清理配额（数量 / 总字节）：超配额按 mtime 从
# 旧到新删。只动本频道的 team-*.md 命名，同目录子代理长报告不受波及
TEAM_REPORT_KEEP_FILES = 200
TEAM_REPORT_KEEP_TOTAL_BYTES = 20 * 1024 * 1024
# snapshot() 里频道消息保留的尾部条数与单条文本截断（meta 落库的经济性）
SNAPSHOT_MESSAGE_TAIL = 50
SNAPSHOT_MESSAGE_TEXT_CAP = 400

# 配置缺省（cfg 未注入时生效；字段与 config.py 的 [team] 段对应。stall_limit 的
# [team] 键归 config.py 管——补键前 getattr 回退这里的默认 2，行为一致）
_DEFAULT_MAX_MEMBERS = 3
_DEFAULT_MEMBER_TIMEOUT_S = 300
_DEFAULT_MAX_ROUNDS = 40
_DEFAULT_REDO_LIMIT = 2
# 成员连续多少轮发言没有任何新工具结果算停滞（设计 §9；cfg 可覆盖，下限 1）
_DEFAULT_STALL_LIMIT = 2

# AI 总管在 TeamMessageDelta.member_index 上的固定取值（名册位次从 0 起）
DIRECTOR_MEMBER_INDEX = -1
# 小会规模（设计 §4.3：2~3 名相关队员就单一争议各表一轮意见）
_HUDDLE_MIN_MEMBERS = 2
_HUDDLE_MAX_MEMBERS = 3

# 成员转发给前端直播的过程事件种类：工具调用（看得见队员在干活）、权限
# 请求/裁决（用户总管模式的确认通道，确认只认用户）、用量与过程提示。
# text_delta 不直接转发——转成 TeamMessageDelta（带 member_index/seq 分桶）；
# turn_started/turn_finished/assistant_message 等轮架件不转发——那是主会话
# 轮次协议的词汇，队员的发言由频道定稿消息承载。
_MEMBER_FORWARD_KINDS = frozenset({
    "tool_call_started", "tool_call_finished",
    "permission_request", "permission_resolved",
    "usage", "notice",
})

# 频道 from/to 的保留词汇（ChannelMessage 词汇表：from ∈ 成员名 / director /
# user / system；to ∈ all / 成员名 / director）：成员占用 from 会撞后端用量归属
# （按 from_member 记账）与前端渲染（按 from 分流气泡）；占用 to 的广播词 all
# 会让 @all 定向副本以 to_member="all" 落簿、语义退化成广播（评审项）。建队
# 入册时一律视同非法名处理
_RESERVED_MEMBER_NAMES = frozenset({"user", "director", "system", "all"})


class TeamError(Exception):
    """团队参数或状态非法（backend 原样回给 WS 调用方的错误文案）。"""


logger = logging.getLogger(__name__)

# ---- 团队模板（三期）：~/.skysheep/teams.json 的读写助手 ----
# 与子代理定义（subagent_store.SubagentStore，~/.skysheep/subagents.json）同姿态：
# 路径构造注入（backend 用 skysheep_home() / "teams.json" 装配）、坏文件容错、
# textio 原子写；模块层不触碰真实 ~/.skysheep，单测传 tmp_path。
# name 是唯一键，同名保存 = 覆盖（前端「另存为模板」对已有名直接更新）。

TEAM_TEMPLATE_NAME_MAX = 60  # 模板名长度上限（与 skills 的限长校验同一精神）


class TeamTemplateMember(BaseModel):
    """模板里的队员行：(provider, model) + 成员名 + 一句话人设。

    只存标识不存 Provider 实例——应用模板时宿主按 (provider, model) 走
    既有的成员解析路径（缺 Key 等构建失败照常降级 provider=None 入册）。
    """

    provider: str = ""
    model: str = ""
    name: str = ""
    persona: str = ""


class TeamTemplateDirector(BaseModel):
    """模板里的总管行；provider/model 均空 = 用户总管（director_mode=user 的常态）。"""

    provider: str = ""
    model: str = ""


class TeamTemplate(BaseModel):
    """团队模板（三期）：把一次建队配置存成可复用的具名条目。

    name 是唯一键；members 有序（应用时按此顺序走 create_team 的同名补序、
    保留字回退与 max_members 截断，模板本身不做成员数校验——上限由应用时
    的既有路径兜住）。
    """

    name: str
    director_mode: str = "user"  # user | ai
    director: TeamTemplateDirector = Field(default_factory=TeamTemplateDirector)
    members: list[TeamTemplateMember] = Field(default_factory=list)
    created_at: float = 0.0  # 模板创建/覆盖时刻（epoch 秒；宿主保存时填）


def validate_team_template_name(name: str) -> str:
    """校验模板名，返回规范后的名字（去首尾空白）；不合法抛 TeamError。

    仿 skills 的 _validate_skill_name 精神：非空、限长、不含路径分隔符与
    冒号（Windows 盘符 / NTFS 数据流分隔符）、不是 `.`/`..` 一类目录引用。
    模板虽住在单个 JSON 文件里不落目录，但名字直接当唯一键与文件语境的
    展示名，形状上挡住一切能被误读成路径的输入。
    """
    raw = (name or "").strip()
    if not raw:
        raise TeamError("团队模板名不能为空")
    if len(raw) > TEAM_TEMPLATE_NAME_MAX:
        raise TeamError(
            f"团队模板名过长（最多 {TEAM_TEMPLATE_NAME_MAX} 字符）：{raw[:80]}…"
        )
    if "/" in raw or "\\" in raw:
        raise TeamError(f"团队模板名不能包含路径分隔符（/ 或 \\）：{raw}")
    if ":" in raw:
        raise TeamError(f"团队模板名不能包含冒号（盘符/数据流）：{raw}")
    if raw.strip(". ") == "":
        # `.`、`..`、纯点纯空格：目录引用或无意义名
        raise TeamError(f"团队模板名不能是目录引用：{raw}")
    return raw


class TeamTemplateStore:
    """~/.skysheep/teams.json 的读写与内存态（三期：建队模板）。

    文件结构 {"templates": [TeamTemplate…]}；坏文件（不存在/非 JSON/条目
    形状不对）不致命：回空列表或跳过坏条目，下次保存整体覆盖（沿
    subagent_store 的容错姿态）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.templates: list[TeamTemplate] = []

    # ---- 读写 ----

    def load(self) -> None:
        self.templates = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # 坏文件不致命：回空列表，下次保存覆盖
        items = data.get("templates") if isinstance(data, dict) else None
        for item in items or []:
            if isinstance(item, dict):
                try:
                    self.templates.append(TeamTemplate(**item))
                except ValidationError:
                    continue  # 单条形状不对：跳过，不拖垮整份模板册

    def save(self) -> None:
        data = {"templates": [t.model_dump() for t in self.templates]}
        # 原子写（沿 subagents.json 同款）：写一半被中断不留半截 JSON
        write_text_atomic(
            self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )

    # ---- 模板增删查（name 唯一键）----

    def list(self) -> list[dict]:
        return [t.model_dump() for t in self.templates]

    def upsert(self, template: TeamTemplate) -> None:
        """保存模板：同名覆盖原条目（位置不变），新名追加。"""
        name = validate_team_template_name(template.name)
        if template.director_mode not in ("user", "ai"):
            raise TeamError(
                f"模板的总管形态只能是 user / ai，收到：{template.director_mode}"
            )
        template.name = name
        for i, old in enumerate(self.templates):
            if old.name == name:
                self.templates[i] = template
                self.save()
                return
        self.templates.append(template)
        self.save()

    def remove(self, name: str) -> None:
        """按名删除模板；不存在时抛 TeamError（与子代理删除同姿态，不静默）。"""
        before = len(self.templates)
        self.templates = [t for t in self.templates if t.name != name]
        if len(self.templates) == before:
            raise TeamError("找不到团队模板: " + name)
        self.save()


@dataclass
class ChannelMessage:
    """频道消息（设计 §4.1：纯文本，任何情况下不直接执行）。"""

    seq: int
    from_member: str  # 成员名 / director / user / system
    to_member: str  # all（广播）/ 成员名（@定向）/ director
    msg_kind: str = "system"  # assign / report / ask / help / object / ruling / system
    task_ref: str = ""
    text: str = ""
    ts: float = field(default_factory=time.time)
    report_path: str = ""  # 全文落盘位置（空 = 未落盘，正文就是截断后的 text）

    def to_dict(self, text_cap: int = 0) -> dict:
        text = self.text if text_cap <= 0 else self.text[:text_cap]
        return {
            "seq": self.seq, "from": self.from_member, "to": self.to_member,
            "msg_kind": self.msg_kind, "task_ref": self.task_ref,
            "text": text, "ts": self.ts, "report_path": self.report_path,
        }


# 频道消息持久化钩子（三期）：sink(team_id, msg) 在每条频道消息定稿时被
# 调用（msg 是定稿的 ChannelMessage，team_id 是建队分配的唯一编号）；
# 同步或异步可调用（沿 budget_check 的双形态约定），None = 不落库。
TeamMessageSink = Callable[[str, ChannelMessage], Awaitable[None] | None]


class TeamChannel:
    """团队频道：全员共享消息簿，seq 单调递增，按成员维护未读游标。

    reports_dir 是长文落盘目录（<工作区>/.skysheep/reports/，由宿主注入）；
    None（无工作区 / 单测）时超长消息只截断不落盘。
    """

    def __init__(self, reports_dir: Path | None = None) -> None:
        self.messages: list[ChannelMessage] = []
        self._next_seq = 1
        self._cursors: dict[str, int] = {}  # 成员名 -> 已读到的最新 seq
        self._reports_dir = reports_dir

    # ---- 发布 ----

    def reserve_seq(self) -> int:
        """预占一个 seq：流式增量需要先于定稿知道消息编号（定稿用 post(seq=…)）。"""
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def post(
        self,
        from_member: str,
        to_member: str = "all",
        msg_kind: str = "system",
        task_ref: str = "",
        text: str = "",
        seq: int | None = None,
    ) -> ChannelMessage:
        """发布一条消息；seq 传入 reserve_seq() 预占的编号，缺省顺延分配。

        超过 MSG_CHAR_LIMIT 的消息截断进频道，全文 best-effort 落盘
        reports_dir，消息上带 report_path——读者凭截断正文（即开头摘录）
        与路径取全文（与子代理长报告同一机制，textio 原子写）。
        """
        if seq is None:
            seq = self.reserve_seq()
        report_path = ""
        if len(text) > MSG_CHAR_LIMIT:
            text, report_path = self._clip_with_fallback(text, from_member, seq)
        msg = ChannelMessage(
            seq=seq, from_member=from_member, to_member=to_member,
            msg_kind=msg_kind, task_ref=task_ref, text=text, report_path=report_path,
        )
        self.messages.append(msg)
        return msg

    def _clip_with_fallback(self, text: str, from_member: str, seq: int) -> tuple[str, str]:
        """超长文本 → (截断文本 + 落盘注记, 落盘路径)。落盘失败退回纯截断。

        两条退路（无落盘目录 / 落盘 OSError）也要补上注记的闭合括号——
        频道里留未闭合括号是可读性缺陷（评审项）。落盘全文另有单文件体积
        上限（REPORT_FILE_CHAR_LIMIT）：超限只保留前缀并注记，洪泛消息不
        无界写盘；写入前先按配额滚动清理旧文件，落盘总量有界。"""
        clipped = (
            text[:MSG_CHAR_LIMIT]
            + f"\n…（全文 {len(text)} 字符，超过频道单条上限 {MSG_CHAR_LIMIT}"
        )
        if self._reports_dir is None:
            return clipped + "）", ""
        try:
            self._reports_dir.mkdir(parents=True, exist_ok=True)
            path = self._reports_dir / f"team-{seq:04d}-{_safe_filename(from_member)}.md"
            stored = text
            if len(stored) > REPORT_FILE_CHAR_LIMIT:
                stored = (
                    stored[:REPORT_FILE_CHAR_LIMIT]
                    + f"\n\n…（全文共 {len(text)} 字符，超出落盘单文件上限 "
                    f"{REPORT_FILE_CHAR_LIMIT} 的部分未保留）"
                )
            write_text_atomic(path, stored)
            # 清理放在写入之后：目录里的 team-*.md 落盘后即满足配额（先清后写
            # 会恒多留一个，配额只在两次落盘之间短暂成立）
            self._prune_reports()
        except OSError:
            return clipped + "）", ""  # 落盘失败退回整段截断（不阻断频道）
        clipped += f"，全文已写入 {path.name}）"
        return clipped, str(path)

    def _prune_reports(self) -> None:
        """team-*.md 的滚动清理：文件数或总字节超配额时按 mtime 从旧到新删。

        只动本频道的 team-*.md 命名（同目录的子代理长报告不同名，不受波及）；
        best-effort——探测/删除失败只放弃本轮清理或留下该文件，不阻断落盘。
        """
        try:
            stats = sorted(
                ((p, p.stat()) for p in self._reports_dir.glob("team-*.md") if p.is_file()),
                key=lambda kv: kv[1].st_mtime,
            )
        except OSError:
            return
        total = sum(st.st_size for _, st in stats)
        remaining = len(stats)
        for path, st in stats:
            if remaining <= TEAM_REPORT_KEEP_FILES and total <= TEAM_REPORT_KEEP_TOTAL_BYTES:
                break
            try:
                path.unlink()
            except OSError:
                pass  # 删不掉（被占用等）：留下，下轮落盘时再试
            total -= st.st_size
            remaining -= 1

    # ---- 未读 ----

    def unread_for(
        self, member: str, exclude_seqs: set[int] | None = None,
    ) -> list[ChannelMessage]:
        """该成员的未读消息：广播或 @定向给 TA、且不是 TA 自己说的。

        to=director 的消息（队员汇报）只给总管，不进其他队员的未读；
        exclude_seqs 里的序号不进未读（总管轮注入插话原文后剔除同文频道
        副本用，见 TeamOrchestrator._build_director_wake——保证总管对一条
        插话只见一次原文）。
        """
        cursor = self._cursors.get(member, 0)
        return [
            m for m in self.messages
            if m.seq > cursor and m.from_member != member
            and m.to_member in ("all", member)
            and not (exclude_seqs and m.seq in exclude_seqs)
        ]

    def mark_read(self, member: str) -> None:
        if self.messages:
            self._cursors[member] = self.messages[-1].seq

    def render_unread(self, member: str, exclude_seqs: set[int] | None = None) -> str:
        """未读消息的注入文本（一期全量裁剪：不摘要、逐条带原文，设计 §13.1）。"""
        unread = self.unread_for(member, exclude_seqs)
        if not unread:
            return ""
        return "\n".join(
            f"[#{m.seq}] {m.from_member} → {_to_label(m.to_member, member)}：{m.text}"
            for m in unread
        )

    def tail(self, n: int = SNAPSHOT_MESSAGE_TAIL) -> list[ChannelMessage]:
        return self.messages[-n:] if n > 0 else list(self.messages)


def _to_label(to_member: str, member: str) -> str:
    return "我" if to_member == member else f"@{to_member}"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]", "_", name.strip()) or "member"
    return cleaned[:40]


def _status_label(status: str) -> str:
    return {
        "pending": "待办", "in_progress": "进行中", "review": "待验收",
        "done": "完成", "error": "失败",
    }.get(status, status)


@dataclass
class TeamTask:
    """工单（叫「工单」不叫「任务」——「任务」是右面板容器词，设计 §5）。"""

    id: str
    title: str
    assignee: str
    type: str = "exec"  # exec（执行型，带工具）/ advisor（顾问型，只出意见）——工单级属性
    deps: list[str] = field(default_factory=list)
    accept: str = ""  # 验收标准
    status: str = "pending"  # pending / in_progress / review / done / error
    redo: int = 0  # 被打回次数
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "assignee": self.assignee,
            "type": self.type, "deps": list(self.deps), "accept": self.accept,
            "status": self.status, "redo": self.redo, "created_at": self.created_at,
        }


class TeamBoard:
    """工单板与状态机。板由三方触达，成员仍不改板（频道消息不可执行）：

    - 用户：WS 方法（team.task_add / task_update，一期）；
    - AI 总管：内部团队工具（team_assign / accept / reject / reassign / drop，
      二期，全部只能落在状态机允许的边上）；
    - 自动循环：派发（待办→进行中）与「成员交报告→待验收」（二期）。

    状态机（设计 §5）：待办 ──派工──▶ 进行中 ──交报告──▶ 待验收
    ──验收通过──▶ 完成；待验收 ──打回(redo+1)──▶ 进行中。
    另有 待办/进行中/待验收 ──▶ 失败（放弃；待办的失败出口用于依赖链卡死
    工单的强制裁定「砍掉」）、失败 ──▶ 进行中（重新派工/改派）；完成是终态。
    """

    _TRANSITIONS: dict[str, set[str]] = {
        "pending": {"in_progress", "error"},
        "in_progress": {"review", "error"},
        "review": {"done", "in_progress", "error"},
        "error": {"in_progress"},
        "done": set(),
    }

    def __init__(
        self, redo_limit: int = _DEFAULT_REDO_LIMIT, members: list[str] | None = None,
    ) -> None:
        self._tasks: dict[str, TeamTask] = {}
        self._counter = 0
        self.redo_limit = max(0, int(redo_limit))
        self.members = list(members or [])  # 指派校验用（建队后由编排器填）

    # ---- 查询 ----

    def get(self, task_id: str) -> TeamTask | None:
        return self._tasks.get(task_id)

    def tasks(self) -> list[TeamTask]:
        return list(self._tasks.values())

    def by_assignee(self, member: str) -> list[TeamTask]:
        return [t for t in self._tasks.values() if t.assignee == member]

    def active_exec_tasks(self, member: str) -> list[TeamTask]:
        """名下进行中的执行型工单：成员本轮挂工具表（执行型）还是空表（顾问型）。"""
        return [
            t for t in self._tasks.values()
            if t.assignee == member and t.type == "exec" and t.status == "in_progress"
        ]

    def dispatchable(self) -> list[TeamTask]:
        """可派发的待办工单（依赖全部完成）。"""
        return [t for t in self._tasks.values() if t.status == "pending" and self.deps_satisfied(t)]

    def deps_satisfied(self, task: TeamTask) -> bool:
        return all(
            (dep := self._tasks.get(task_id)) is not None and dep.status == "done"
            for task_id in task.deps
        )

    def incomplete(self) -> list[TeamTask]:
        return [t for t in self._tasks.values() if t.status != "done"]

    def all_done(self) -> bool:
        return bool(self._tasks) and not self.incomplete()

    # ---- 变更（全部只应由用户的 WS 方法触达）----

    def add(
        self, title: str, assignee: str, type: str = "exec",
        accept: str = "", deps: list[str] | None = None,
    ) -> TeamTask:
        title = (title or "").strip()
        if not title:
            raise TeamError("工单标题不能为空")
        if assignee not in self.members:
            raise TeamError(f"指派对象「{assignee}」不在团队名册中")
        if type not in ("exec", "advisor"):
            raise TeamError(f"工单类型只能是 exec（执行型）或 advisor（顾问型），收到：{type}")
        dep_ids = [d for d in dict.fromkeys(deps or []) if d]
        for dep in dep_ids:
            if dep not in self._tasks:
                raise TeamError(f"依赖的工单 {dep} 不存在")
        self._counter += 1
        task = TeamTask(
            id=f"T{self._counter}", title=title, assignee=assignee,
            type=type, deps=dep_ids, accept=(accept or "").strip(),
        )
        self._tasks[task.id] = task
        return task

    def set_status(self, task_id: str, status: str) -> TeamTask:
        """状态流转。review → in_progress 视为打回（redo+1，受 redo_limit 约束）。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        if status == task.status:
            return task
        if status not in self._TRANSITIONS.get(task.status, set()):
            raise TeamError(
                f"工单 {task_id} 不能从「{_status_label(task.status)}」"
                f"流转到「{_status_label(status)}」"
            )
        if status == "in_progress":
            # 派工/打回/重派都要过依赖关：依赖未完成不派发（设计 §5）
            waiting = [
                dep for dep in task.deps
                if (t := self._tasks.get(dep)) is None or t.status != "done"
            ]
            if waiting:
                raise TeamError(
                    f"工单 {task_id} 的依赖工单未完成（{'、'.join(waiting)}），不能派发"
                )
            if task.status == "review":
                if task.redo >= self.redo_limit:
                    raise TeamError(
                        f"工单 {task_id} 已被打回 {task.redo} 次，达到上限（{self.redo_limit}）："
                        "请改派他人、换顾问型工单替代，或标记 error 放弃"
                    )
                task.redo += 1
        task.status = status
        return task

    def mark_error(self, task_id: str, reason: str = "") -> TeamTask:
        """失败收尾（放弃；用户的 WS 方法或 AI 总管的 team_drop 工具触达）。

        待办也可放弃：依赖链卡死的待办工单落「砍掉」裁定（设计 §9），否则
        强制裁定指令里的 team_drop 对这类工单无单可落。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        if task.status not in ("pending", "in_progress", "review"):
            raise TeamError(f"工单 {task_id} 当前不在待办/进行中/待验收，不能标记失败")
        task.status = "error"
        if reason:
            task.accept = (task.accept + "\n" if task.accept else "") + f"失败原因：{reason}"
        return task

    def reassign(
        self, task_id: str, new_assignee: str, type: str | None = None, note: str = "",
    ) -> TeamTask:
        """改派（AI 总管的 team_reassign 工具触达；用户改派走 task_update + 重派）。

        只接受 待验收/失败 两种在板状态：失败是重派（error → 进行中的既有边），
        待验收是验收不合格的换人。待验收改派的 redo 口径：
        - 未超打回上限：视同打回（redo+1）；
        - 已超上限（强制裁定出口）：redo 清零——新队员拿全新的打回预算，
          「换人再换人」的滥用由全局轮次上限兜底（设计 §9 的兜底层）。
        new_assignee 必须不同于当前指派（改派他人，设计 §5）；想给原队员
        新的机会 = 放弃后用新工单重来。type 传入时同时改工单类型
        （降级为顾问型的强制裁定出口）。依赖必然已满足（派发时已过关，
        完成是终态不会回退），不再重复校验。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        if task.status not in ("review", "error"):
            raise TeamError(f"工单 {task_id} 当前不在待验收/失败，不能改派")
        if new_assignee not in self.members:
            raise TeamError(f"改派对象「{new_assignee}」不在团队名册中")
        if new_assignee == task.assignee:
            raise TeamError(
                f"改派对象必须不同于当前指派队员「{task.assignee}」"
                "（给原队员新的机会：先放弃该工单，再建新工单指派给他）"
            )
        if type is not None and type not in ("exec", "advisor"):
            raise TeamError(f"工单类型只能是 exec（执行型）或 advisor（顾问型），收到：{type}")
        over_limit = task.status == "review" and task.redo >= self.redo_limit
        task.assignee = new_assignee
        if type is not None:
            task.type = type
        if task.status == "review":
            task.redo = 0 if over_limit else task.redo + 1
        task.status = "in_progress"
        if note:
            task.accept = (task.accept + "\n" if task.accept else "") + f"改派注记：{note}"
        return task


@dataclass
class TeamMemberSpec:
    """一名队员：(provider, model) + 成员名 + 一句话人设（设计 §2.2）。

    provider 是已构建的 Provider 实例（宿主复用其成员 Provider 实例化注入）；
    构建失败（缺 Key 等）时 provider=None 且 build_error 带原因，唤醒时按
    失败隔离处理，不拖垮团队。
    """

    name: str = ""
    provider_name: str = ""
    model: str = ""
    provider: Provider | None = None
    persona: str = ""
    build_error: str = ""


@dataclass
class DirectorSpec:
    """AI 总管（二期）：(provider, model) 标识 + 已构建的 Provider 实例。

    构建失败（缺 Key 等）时 provider=None 且 build_error 带原因；总管轮
    触达时发频道注记并报错（团队保持活动，等用户插话重试 / 接管 / 收队），
    不拖垮工单板与频道。
    """

    provider_name: str = ""
    model: str = ""
    provider: Provider | None = None
    build_error: str = ""


@dataclass
class _MemberRuntime:
    """队员的运行时：跨阶段存活的 Agent 实例 + 名册位次。"""

    spec: TeamMemberSpec
    index: int
    agent: Agent | None = None  # 首次唤醒时创建，之后全程复用（独立 history）


@dataclass
class MemberTurnResult:
    """一次成员唤醒轮的结果（宿主据此记用量/推进频道/落 meta）。"""

    member: str
    index: int
    status: str = "done"  # done | error | cancelled
    error: str = ""
    seq: int = 0  # 本轮报告的频道消息编号（未产出报告时为 0）
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tools_used: int = 0
    # 本轮「新」工具结果数（name+结果预览与上一轮比对，完全重复的不算）：
    # 停滞守卫只认它——每轮顺手调一次同一只读工具的混时长动作不冲抵停滞
    new_tools_used: int = 0


@dataclass
class DirectorTurnResult:
    """一次总管轮的结果（宿主据此记 meta/推进；用量同时入 _director_usage）。"""

    status: str = "done"  # done | error | cancelled（cancelled 只在取消上抛前存在）
    error: str = ""
    seq: int = 0  # 本轮发言的频道消息编号
    text: str = ""
    tool_calls: list[str] = field(default_factory=list)  # 本轮板面动作摘要（按发生序）
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class HuddleRequest:
    """总管经开小会工具排定的小会（单争议 + 2~3 名相关队员，设计 §4.3）。"""

    topic: str
    members: list[str] = field(default_factory=list)
    context: str = ""


class TeamMemberGate:
    """会话既有 PermissionGate 的来源标注代理：**决策完全委托，只加标注**。

    authorize 原样转调内部门（返回 None 的放行、PendingPermission 的生成、
    normalize_decision 的 fail-closed 全在门里，本代理不碰）；仅对需要确认
    的请求在 note 前缀「来自队员『成员名』」，让用户知道确认卡是谁触发的
    （设计 §6.2）。其余属性/方法（needs_confirm、rule_for、persist_rule、
    claim_write、白名单与档位开关…）一律透传，读写都落在内部门上——宿主
    对会话门的档位调整对队员同样生效，不存在第二套判定。
    """

    def __init__(self, inner: PermissionGate, member: str) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_member", member)

    async def authorize(self, tool, input_dict):
        pending = await self._inner.authorize(tool, input_dict)
        if pending is not None:
            prefix = f"来自队员『{self._member}』"
            pending.note = f"{prefix}：{pending.note}" if pending.note else f"{prefix}的权限请求"
        return pending

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value) -> None:
        if name in ("_inner", "_member"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


class TeamOrchestrator:
    """用户总管编排循环（一期 MVP）。

    注入面（全部构造参数，模块层不触碰真实 ~/.skysheep）：
    - emit：事件回调（五个团队事件 + 转发的成员过程事件都从这里出）；
    - working_dir：工作区（长报告落 <工作区>/.skysheep/reports/；None = 不落盘）；
    - registry：工作区工具注册表（执行型工单的成员挂它；None = 全员顾问型）；
    - gate：会话既有 PermissionGate（经 TeamMemberGate 代理；None = 全员顾问型，
      执行型工单不会拿到无门的工具表——fail-closed）；
    - cfg：含 max_members / member_timeout_s / max_rounds / redo_limit 属性的
      配置对象（config.py 的 TeamConfig 或任意同形对象；None = 内置缺省）；
    - director / director_mode / budget_check（二期 AI 总管闭环）：director 是
      总管的 DirectorSpec（provider 由宿主构建注入）；director_mode="ai" 时
      总管经 run_auto_turn 自动推进闭环，"user"（默认）保持一期行为；budget_check
      是越线判定钩子（同步或异步可调用，真值=已越线，由宿主接既有每日预算
      路径），越线即 finish("budget_exhausted") 强制交付。
    - message_sink（三期频道消息全量落库）：构造注入的 TeamMessageSink，
      每条频道消息定稿时回调 sink(team_id, msg)（同步或异步皆可）；未注入
      则不落库，行为与既有版本完全一致（单测零负担）。落库是 best-effort
      旁路：sink 异常只记日志，绝不拖垮频道与协作。team_id 在 create_team
      时分配（uuid4.hex），快照与 meta 随之带出，宿主凭它调 store 的
      add_team_message / list_team_messages。
    - max_iterations / restrict_to_workdir / session_id / hooks / mods：成员
      Agent 的既有参数透传。session_id 让成员写与主会话共用同一租约归属
      （security/leases 的并行写协调对队员不因空 owner 被静默降级）；hooks /
      mods 让用户的 pre_tool_use 钩子与 Mod 拦截对队员同样生效（两者都只能
      收紧，权限语义零放松）。总管 Agent 不透传 hooks/mods——总管只有内部
      团队工具，会话级拦截手段对板面操作没有意义。
    """

    def __init__(
        self,
        *,
        emit: EmitFn,
        working_dir: Path | None = None,
        registry: ToolRegistry | None = None,
        gate: PermissionGate | None = None,
        cfg: object | None = None,
        max_iterations: int = 25,
        restrict_to_workdir: bool = False,
        session_id: str = "",
        job_containment: bool = True,
        sandbox_level: str = "job",
        use_ripgrep: bool = True,
        hooks=None,
        mods=None,
        director: DirectorSpec | None = None,
        director_mode: str = "user",
        budget_check: Callable[[], bool] | None = None,
        message_sink: TeamMessageSink | None = None,
    ) -> None:
        if director_mode not in ("user", "ai"):
            raise TeamError(f"未知的总管形态：{director_mode}（只能是 user / ai）")
        if director_mode == "ai" and director is None:
            raise TeamError('director_mode="ai" 需要提供总管模型（DirectorSpec）')
        self._emit = emit
        self._working_dir = working_dir
        self._registry = registry
        self._gate = gate
        self._max_iterations = max(1, int(max_iterations))
        self._restrict_to_workdir = bool(restrict_to_workdir)
        self._job_containment = bool(job_containment)
        # 二期：沙箱档位随主配置透传给队员 Agent（"restricted" = 叠加受限令牌）
        self._sandbox_level = sandbox_level
        # 内容搜索（grep）可选加速：随主配置透传（rg 不可用/失败自动回退内置）
        self._use_ripgrep = bool(use_ripgrep)
        self._session_id = session_id
        self._hooks = hooks
        self._mods = mods
        self._empty_registry = ToolRegistry([])
        self._plan_registry: ToolRegistry | None = None  # 规划模式的只读工具表（set_plan_mode 现筛）
        self._reports_dir = (
            working_dir / ".skysheep" / "reports" if working_dir is not None else None
        )
        self._timeout_s = self._cfg_value(cfg, "member_timeout_s", _DEFAULT_MEMBER_TIMEOUT_S)
        self._max_rounds = self._cfg_value(cfg, "max_rounds", _DEFAULT_MAX_ROUNDS)
        self._max_members = self._cfg_value(cfg, "max_members", _DEFAULT_MAX_MEMBERS)
        self._redo_limit = self._cfg_value(cfg, "redo_limit", _DEFAULT_REDO_LIMIT)
        self._stall_limit = max(1, self._cfg_value(cfg, "stall_limit", _DEFAULT_STALL_LIMIT))

        self.channel = TeamChannel(self._reports_dir)
        self.board = TeamBoard(self._redo_limit)
        self.roster: list[TeamMemberSpec] = []
        # 建队时分配的唯一编号（uuid4.hex；空 = 尚未建队）：频道消息落库的
        # 归组键（team_messages.team_id）与回放主轴（team.log），随快照进 meta
        self.team_id: str = ""
        self.director_mode = director_mode
        self.plan_mode = False  # 本轮规划模式（backend 每个团队轮同步，见 set_plan_mode）
        # 全局轮数：总管轮 + 成员唤醒轮合计（小会一场计 1；频道纯文本不计入，设计 §13.6）
        self.rounds_used = 0
        self._runtimes: dict[str, _MemberRuntime] = {}
        self._finished: dict | None = None  # {"status", "summary"} 终态后非 None

        # ---- AI 总管闭环（director_mode="ai" 时启用）----
        self._director = director or DirectorSpec()
        self._budget_check = budget_check
        self._message_sink = message_sink  # 频道消息持久化钩子（None = 不落库）
        self._director_agent: Agent | None = None  # 首个总管轮创建，之后复用（独立 history）
        # 总管只挂内部团队工具（全部 READONLY、只操作工单板与编排状态，
        # 不触工作区、不经权限门确认）——见模块底部 _DirectorToolBase 一族
        self._director_registry = (
            self._build_director_registry() if director_mode == "ai" else ToolRegistry([])
        )
        # 用户插话（下一总管轮最高优先级注入）：(原文, 进频道的 seq 列表)。
        # seq 一并记录——注入原文时把同文频道消息从总管未读剔除，保证总管
        # 对一条插话只见一次原文（不重复解读、不白耗 token）
        self._pending_user_notes: list[tuple[str, list[int]]] = []
        self._pending_huddles: list[HuddleRequest] = []  # 总管本轮排定的小会
        self._deliver_requested = False  # 总管经 team_deliver 请求交付
        self._deliver_prompted = False  # 已催过「全部完成请交付」（有新工单时复位）
        self._stall_counts: dict[str, int] = {}  # 成员连续无新工具结果的轮数
        self._stalled: set[str] = set()  # 已停滞（强制移交总管处置、自动唤醒跳过）
        # 成员上一轮的工具结果签名集（name+结果预览）：停滞守卫的「新产出」
        # 比对基准——与上一轮完全重复的调用不算新工具结果
        self._last_tool_sigs: dict[str, frozenset[str]] = {}
        self._loop_running = False  # run_auto_turn 正在推进（跨任务注入插话前先看）
        self._director_usage = [0, 0]  # 总管累计用量 [input, output]（snapshot 落库口径）
        self._director_round_tools: list[str] = []  # 当前总管轮的板面动作摘要

    @staticmethod
    def _cfg_value(cfg: object | None, key: str, default: int) -> int:
        raw = getattr(cfg, key, default) if cfg is not None else default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    # ---- AI 总管：状态查询与小件 ----

    def director_info(self) -> dict:
        """总管标识（宿主用量入账与展示用；用户总管模式 provider/model 为空串）。"""
        return {
            "mode": self.director_mode,
            "provider": self._director.provider_name,
            "model": self._director.model,
        }

    def director_usage(self) -> dict:
        """总管累计用量（内部记账，snapshot 落库口径；事件流归属由宿主 team_emit 做）。"""
        return {"input_tokens": self._director_usage[0], "output_tokens": self._director_usage[1]}

    @property
    def loop_running(self) -> bool:
        """自动循环是否正在推进；跨任务注入插话（inject_user_message）前先看它。"""
        return self._loop_running

    async def _budget_over(self) -> bool:
        """预算钩子判定（同步/异步可调用均可）。钩子故障按未越线放行——
        坏掉的仪表不该停掉团队，费用护栏由宿主其余路径兜底。"""
        check = self._budget_check
        if check is None:
            return False
        try:
            result = check()
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
            return bool(result)
        except Exception:  # noqa: BLE001 - 钩子异常不捏造越线
            return False

    def _note_director_tool(self, summary: str) -> None:
        """记录当前总管轮的板面动作（循环进展判定与 DirectorTurnResult 用）。"""
        self._director_round_tools.append(summary)

    def _clear_stall(self, name: str) -> None:
        """清除成员的停滞标记（重新领单 / 改派 / 放弃其工单时复位）。"""
        self._stalled.discard(name)
        self._stall_counts.pop(name, None)

    # ---- 状态 ----

    @property
    def active(self) -> bool:
        return self._finished is None and bool(self.roster)

    @property
    def finished(self) -> dict | None:
        return dict(self._finished) if self._finished else None

    def agents(self) -> list[Agent]:
        """已唤醒过的成员 Agent（宿主可并入 respond_permission 的路由面）。"""
        return [rt.agent for rt in self._runtimes.values() if rt.agent is not None]

    def respond_permission(self, request_id: str, decision: str) -> bool:
        """把权限决策投递给持有该 request_id 的成员 Agent（决策仍过白名单）。"""
        for agent in self.agents():
            if agent.respond_permission(request_id, decision):
                return True
        return False

    # ---- 组队 ----

    async def create_team(self, members: list[TeamMemberSpec]) -> list[dict]:
        """建队：去重、限量、补名，分配 team_id，发 TeamStarted。返回名册 dict。

        members 来自宿主的成员解析（缺 Key 的成员 provider=None 照常入册，
        唤醒时按失败隔离呈现错误，与圆桌同姿态）。缺名沿 provider 名、同名
        自动补序号、保留字成员名（user/director/system/all，频道词汇表）
        视同非法名回退；超过 max_members 截断并发 Notice（不静默丢人）。
        team_id（uuid4.hex）在入册前分配：频道消息落库（message_sink）与
        快照/meta（snapshot.team_id）都凭它归组；重复建队换新 id——旧 id
        名下的落库消息原样留在库中，不与新团队混册。
        """
        if not members:
            raise TeamError("团队至少需要一名队员；请先选择成员或配置模型服务")
        self.team_id = uuid.uuid4().hex
        roster: list[TeamMemberSpec] = []
        seen_specs: set[tuple[str, str]] = set()
        seen_names: set[str] = set()
        for spec in members:
            key = (spec.provider_name, spec.model)
            if key in seen_specs:
                continue
            name = (spec.name or spec.provider_name or "队员").strip() or "队员"
            if name in _RESERVED_MEMBER_NAMES:
                # 保留字视同非法名：回退 provider 名，仍撞再交下方补序号兜底
                name = (spec.provider_name or "").strip() or "队员"
                if name in _RESERVED_MEMBER_NAMES:
                    name = "队员"
            base, n = name, 1
            while name in seen_names:
                n += 1
                name = f"{base}{n}"
            seen_specs.add(key)
            seen_names.add(name)
            roster.append(TeamMemberSpec(
                name=name, provider_name=spec.provider_name, model=spec.model,
                provider=spec.provider, persona=(spec.persona or "").strip(),
                build_error=spec.build_error,
            ))
        if len(roster) > self._max_members:
            dropped = len(roster) - self._max_members
            roster = roster[: self._max_members]
            await self._fire(NoticeEvent(
                message=f"团队成员上限为 {self._max_members} 名（设置 · 团队 可调），"
                        f"已截去后面的 {dropped} 名。"
            ))
        self.roster = roster
        self.board.members = [m.name for m in roster]
        self._runtimes = {m.name: _MemberRuntime(spec=m, index=i) for i, m in enumerate(roster)}
        roster_dicts = self._roster_dicts()
        await self._fire(TeamStarted(
            roster=roster_dicts,
            director_mode=self.director_mode,
            director_provider=self._director.provider_name,
            director_model=self._director.model,
        ))
        return roster_dicts

    def _roster_dicts(self) -> list[dict]:
        return [
            {"index": i, "name": m.name, "provider": m.provider_name,
             "model": m.model, "persona": m.persona}
            for i, m in enumerate(self.roster)
        ]

    # ---- 用户总管回合 ----

    async def handle_user_message(self, text: str, images: list | None = None) -> dict:
        """用户消息进频道（from=user）并唤醒被点名的成员（按名册顺序逐个）。

        - 「@成员名」解析定向（单人一条定向消息；多人各发一条定向副本），
          未点名则 to=all 广播；广播不唤醒成员，只在其下次被唤醒前注入。
        - 团队回合是纯文本协作：收到图片发 Notice 提示暂不支持并忽略（不静默丢）。
        - max_rounds 耗尽时自动收队（rounds_exhausted），终态随返回值交给宿主落 meta。
        - 仅用户总管模式：AI 总管模式的用户消息走 run_auto_turn / inject_user_message，
          不允许绕过总管直接点名唤醒（角色纯粹，设计 §2.1）。
        """
        if self.director_mode == "ai":
            raise TeamError("当前团队由 AI 总管主持：用户消息请经 run_auto_turn / inject_user_message 进入。")
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        text = (text or "").strip()
        if not text:
            raise TeamError("消息内容为空")
        if images:
            await self._fire(NoticeEvent(
                message=f"团队回合暂不支持图片：本轮的 {len(images)} 张图片已忽略；"
                        "需要队员看图请先收队，关闭团队后重发。"
            ))

        posted_seqs: list[int] = await self._post_user_message(text)
        mentioned = self._parse_mentions(text)

        woke: list[dict] = []
        for name in mentioned:  # 已按名册顺序排列；逐个唤醒，非并行
            try:
                result = await self.run_member_turn(name)
            except asyncio.CancelledError:
                # 用户停止：取消向上传播给宿主（团队保持活动，部分报告已定稿
                # 进频道）；这里只补齐 woke 记录再抛，语义与正常返回对齐
                woke.append({"member": name, "status": "cancelled",
                             "error": "用户停止", "seq": 0})
                raise
            except TeamError:
                # 前一名成员的轮已在入口触发收队（轮次耗尽/预算越线，finish
                # 幂等）：not-active 的 TeamError 不外抛——外抛会把整个团队
                # 回合塌成错误，宿主侧终态摘要不落、用量不入账、已收队的团队
                # 永远挂在活动登记里。剩余点名记 skipped 后 break，照常走
                # _maybe_finish/_turn_summary 把终态交还宿主。
                woke.append({"member": name, "status": "skipped",
                             "error": "团队已进入终态，本轮未再唤醒", "seq": 0})
                break
            woke.append({
                "member": result.member, "status": result.status,
                "error": result.error, "seq": result.seq,
            })

        finished = await self._maybe_finish()
        return self._turn_summary(posted_seqs, woke, finished=finished)

    def _parse_mentions(self, text: str) -> list[str]:
        """解析「@成员名」；返回按名册顺序去重后的被点名成员（无则空表）。"""
        hits = set(re.findall(r"@([^\s@，。；！？、,.;!?]+)", text))
        return [m.name for m in self.roster if m.name in hits]

    async def _post_user_message(self, text: str) -> list[int]:
        """用户消息进频道（from=user）：@点名各发一条定向副本，未点名广播。"""
        mentioned = self._parse_mentions(text)
        posted_seqs: list[int] = []
        if mentioned:
            for name in mentioned:
                msg = self.channel.post("user", to_member=name, msg_kind="ruling", text=text)
                await self._emit_message(msg)
                posted_seqs.append(msg.seq)
        else:
            msg = self.channel.post("user", to_member="all", msg_kind="ruling", text=text)
            await self._emit_message(msg)
            posted_seqs.append(msg.seq)
        return posted_seqs

    def _turn_summary(
        self, posted_seqs: list[int], woke: list[dict], finished: dict | None = None,
    ) -> dict:
        return {
            "posted_seqs": posted_seqs,
            "woke": woke,
            "rounds_used": self.rounds_used,
            "max_rounds": self._max_rounds,
            "finished": finished,
            "snapshot": self.snapshot() if finished is not None else None,
        }

    # ---- 成员唤醒轮 ----

    def set_plan_mode(self, on: bool) -> None:
        """设置本轮规划模式（backend 在每个团队轮按本轮 plan_mode 同步）。

        激活时成员一律按只读工具表唤醒（即使名下有执行型工单）——「本轮不
        执行任何写操作」的用户契约对团队轮同样成立，与普通轮的只读注册表
        切换（backend 的 plan_mode 分支）同语义；只读表从编排器持有的工作区
        注册表现筛（与成员全表同源同刻）。注册表为 None（全员顾问型）时只读
        表保持 None，唤醒按 fail-closed 落到空表。
        """
        self.plan_mode = bool(on)
        if self.plan_mode and self._registry is not None:
            self._plan_registry = ToolRegistry(
                [t for t in self._registry.all() if t.safety == Safety.READONLY]
            )
        elif not self.plan_mode:
            self._plan_registry = None

    async def run_member_turn(self, member_name: str) -> MemberTurnResult:
        """唤醒一名成员跑一轮：注入未读+名下工单 → Agent 循环 → 报告定稿进频道。

        成员是跨阶段存活的独立 Agent（首次唤醒创建，之后复用同一 history）；
        本轮挂空工具表（顾问型）还是工作区工具表（执行型）按名下是否有进行中
        的执行型工单判定——工单级属性，同一队员逐单切换。单轮超时按失败隔离：
        该轮标 error、部分发言照常定稿进频道，不拖垮团队。
        """
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        rt = self._runtimes.get(member_name)
        if rt is None:
            raise TeamError(f"队员「{member_name}」不在团队名册中")
        if self.rounds_used >= self._max_rounds:
            await self.finish(status="rounds_exhausted")
            return MemberTurnResult(
                member=member_name, index=rt.index, status="error",
                error=f"已达全局轮次上限（{self._max_rounds} 轮），团队已收队",
            )
        if await self._budget_over():
            await self.finish(status="budget_exhausted")
            return MemberTurnResult(
                member=member_name, index=rt.index, status="error",
                error="已达团队 token 预算，团队已强制交付",
            )
        self.rounds_used += 1

        # 唤醒上下文：人设（系统提示词）+ 频道未读裁剪 + 名下工单，仅此三样
        wake_prompt = self._build_wake_prompt(rt)
        self.channel.mark_read(member_name)

        exec_mode = bool(
            self.board.active_exec_tasks(member_name)
            and self._registry is not None and self._gate is not None
        )
        agent = self._ensure_agent(rt, exec_mode)
        if agent is None:
            # Provider 构建失败（缺 Key 等）：失败隔离，错误注记留在频道里
            error = rt.spec.build_error or "模型服务不可用"
            await self._post_member_note(rt, f"（本轮未能执行：{error}）")
            return MemberTurnResult(
                member=member_name, index=rt.index, status="error", error=error,
            )

        seq = self.channel.reserve_seq()  # 流式增量需要先于定稿知道消息编号
        parts: list[str] = []
        tool_sigs: set[str] = set()  # 本轮工具结果签名（name+预览，停滞守卫比对用）
        result = MemberTurnResult(member=member_name, index=rt.index, seq=seq)

        async def consume() -> None:
            async for ev in agent.run_turn(wake_prompt):
                if ev.kind == "text_delta":
                    parts.append(ev.text)
                    await self._emit(TeamMessageDelta(
                        member_index=rt.index, seq=seq, text=ev.text,
                    ))
                elif ev.kind in _MEMBER_FORWARD_KINDS:
                    if ev.kind == "usage":
                        result.input_tokens += ev.input_tokens
                        result.output_tokens += ev.output_tokens
                    elif ev.kind == "tool_call_finished" and not ev.is_error:
                        result.tools_used += 1
                        tool_sigs.add(f"{ev.name}\n{ev.preview}")
                    await self._emit(ev)

        try:
            await asyncio.wait_for(consume(), timeout=self._timeout_s)
        except TimeoutError:
            result.status = "error"
            result.error = f"成员单轮超时（>{self._timeout_s} 秒），按失败隔离"
        except asyncio.CancelledError:
            # 用户停止：部分发言先定稿进频道（用户已看到的不凭空消失），再上抛。
            # 频道定稿是同步的（状态先一致）；事件帧经 shield 尽力发出——若
            # emit 再次被取消就放弃发帧，定稿仍在频道与 snapshot 里不丢。
            result.status = "cancelled"
            result.error = "用户停止"
            await self._finalize_speech(
                from_member=rt.spec.name, to_member="director", msg_kind="report",
                empty_note="（本轮没有产出）", seq=seq, parts=parts,
                suffix="…（已停止）", shield=True,
            )
            raise
        except Exception as e:  # noqa: BLE001 - 单成员故障不上抛（失败隔离）
            result.status = "error"
            result.error = str(e)[:200]

        suffix = f"\n\n（本轮失败：{result.error}）" if result.status == "error" else ""
        # 停滞守卫的「新产出」口径：与上一轮完全相同的工具结果（同名同预览，
        # 如每轮顺手 list_dir 一次）不算新工具结果；签名集跨轮保留供下一轮
        # 比对。取消路径在上方已原样上抛，不经过这里。
        prev_sigs = self._last_tool_sigs.get(member_name, frozenset())
        result.new_tools_used = len(tool_sigs - prev_sigs)
        self._last_tool_sigs[member_name] = frozenset(tool_sigs)
        result.text = await self._finalize_speech(
            from_member=rt.spec.name, to_member="director", msg_kind="report",
            empty_note="（本轮没有产出）", seq=seq, parts=parts, suffix=suffix,
        )
        return result

    def _member_director_line(self) -> str:
        """成员提示词的总管句（按当前 director_mode 取）：AI 模式不写死成用户，
        队员才知道向总管汇报；权限确认只认用户的红线两种形态都不变（设计 §6）。
        """
        if self.director_mode == "user":
            return (
                "Director: the user. They assign tasks, wake you, review your "
                "reports, and are the ONLY authority for permission confirmations "
                "and task-board changes."
            )
        return (
            f"Director: the AI director ({self._director.provider_name}/"
            f"{self._director.model}). It assigns tasks, wakes you, and reviews "
            "your reports; it changes the task board only through its internal "
            "tools. Permission confirmations are still decided ONLY by the user "
            "- it cannot approve them for you."
        )

    def _member_system_prompt(self, spec: TeamMemberSpec) -> str:
        """成员系统提示词（人设 + 名册 + 当前总管形态的总管句）。

        Agent 创建时与 takeover 切换总管形态后都会调用：已存在的成员 Agent
        按新形态同步刷新（agent.set_system 有先例），不残留误导旧句。
        """
        return TEAM_MEMBER_PROMPT.format(
            name=spec.name,
            persona=self._persona_paragraph(spec),
            director=self._member_director_line(),
            teammates="、".join(
                f"{m.name}（{m.provider_name}/{m.model}）" for m in self.roster
            ),
            workdir=str(self._working_dir) if self._working_dir is not None
            else "(none - quick chat only)",
        )

    def _ensure_agent(self, rt: _MemberRuntime, exec_mode: bool) -> Agent | None:
        """取或建成员 Agent，并按本轮模式切换工具表（Agent.history 跨轮保留）。"""
        if rt.spec.provider is None:
            return None
        agent = rt.agent
        if agent is None:
            # gate 理论上不会是 None（顾问型没有工具可调，门不会被触达；
            # 执行型在 exec_mode 里已要求门在场才挂工具表）——兜一个惰性
            # 空门只为满足 Agent 的类型契约，不构成第二套判定。
            gate = TeamMemberGate(self._gate, rt.spec.name) if self._gate is not None \
                else PermissionGate(working_dir=self._working_dir)
            agent = Agent(
                provider=rt.spec.provider,
                registry=self._empty_registry,  # 下方按本轮模式再切
                gate=gate,
                working_dir=self._working_dir,
                max_iterations=self._max_iterations,
                restrict_to_workdir=self._restrict_to_workdir,
                # 会话归属：claim_write 的租约 owner 用它（空串会被并行写
                # 协调按「无归属」放行）；用户钩子与 Mod 拦截照主会话生效
                session_id=self._session_id,
                job_containment=self._job_containment,
                sandbox_level=self._sandbox_level,
                use_ripgrep=self._use_ripgrep,
                hooks=self._hooks,
                mods=self._mods,
            )
            agent.set_system(self._member_system_prompt(rt.spec))
            rt.agent = agent
        # 顾问型空表 / 执行型工作区表（工单级属性逐单切换，同一 Agent 实例复用）；
        # 规划模式激活时一律只读表——执行型工单也降级（见 set_plan_mode），
        # 只读表缺失（理论不可达）按 fail-closed 落空表
        if self.plan_mode:
            agent.registry = (
                self._plan_registry if self._plan_registry is not None
                else self._empty_registry
            )
        else:
            agent.registry = self._registry if exec_mode else self._empty_registry
        return agent

    @staticmethod
    def _persona_paragraph(spec: TeamMemberSpec) -> str:
        persona = spec.persona.strip()
        if persona:
            return f"- Persona: {persona}"
        return "- Persona:（未特别设定，尽职协作即可）"

    def _build_wake_prompt(self, rt: _MemberRuntime) -> str:
        """唤醒消息：频道未读（裁剪）+ 名下工单。绝不注入他人 history 或会话历史。"""
        unread = self.channel.render_unread(rt.spec.name)
        sections: list[str] = []
        if unread:
            sections.append(f"【团队频道 · 未读消息】\n{unread}")
        mine = self.board.by_assignee(rt.spec.name)
        if mine:
            lines = [
                f"- {t.id}「{t.title}」 类型：{'执行型' if t.type == 'exec' else '顾问型'}"
                f" 状态：{_status_label(t.status)}"
                + (f"（已被打回 {t.redo} 次）" if t.redo else "")
                + (f"\n  验收标准：{t.accept}" if t.accept else "")
                + (f"\n  依赖工单：{'、'.join(t.deps)}" if t.deps else "")
                for t in mine
            ]
            sections.append("【你名下的工单】\n" + "\n".join(lines))
        if not sections:
            sections.append("（总管唤醒你：暂无新的频道消息与工单，请简短应答。）")
        sections.append(
            "请基于以上信息行动：有进行中的执行型工单就动手完成并汇报；"
            "被点名提问就直接回答。你的这条回复会作为报告发到团队频道。"
        )
        return "\n\n".join(sections)

    async def _finalize_speech(
        self, from_member: str, to_member: str, msg_kind: str, empty_note: str,
        seq: int, parts: list[str], suffix: str = "",
        shield: bool = False,
    ) -> str:
        """一轮发言定稿：TeamMessage（from=from_member, to=to_member）进频道。

        成员报告轮与总管裁定轮共用一条定稿路径，四参定形态：
        成员轮 (成员名, director, report, 「（本轮没有产出）」)、
        总管轮 (director, all, ruling, 「（总管本轮没有发言）」)。
        长报告按频道单条上限截断 + 全文落盘 .skysheep/reports/ 只投摘录
        （沿子代理做法）；空发言（超时/故障且无输出）也落一条系统注记，
        频道时间线不缺格。一期 to=director：用户总管模式下总管就是用户
        （事件契约的 to 词汇表是 all/成员名/director，见 events.py）。
        shield=True（取消收尾路径）：频道定稿同步完成，事件帧尽力发出。
        """
        text = "".join(parts).strip()
        if not text:
            note = suffix.strip() or empty_note
            msg = self.channel.post(
                from_member, to_member=to_member, msg_kind="system",
                text=note, seq=seq,  # 复用预占编号：一轮恰好占一个 seq，不留空洞
            )
            await self._emit_message(msg, shield=shield)
            return ""
        msg = self.channel.post(
            from_member, to_member=to_member, msg_kind=msg_kind,
            text=text + suffix, seq=seq,
        )
        await self._emit_message(msg, shield=shield)
        return msg.text

    async def _post_member_note(self, rt: _MemberRuntime, note: str) -> None:
        """失败/空发言的频道注记。"""
        msg = self.channel.post(
            rt.spec.name, to_member="director", msg_kind="system", text=note,
        )
        await self._emit_message(msg)

    # ---- 工单板（宿主的 WS 方法直接调这三个；板只由用户变更）----

    async def task_add(
        self, title: str, assignee: str, type: str = "exec",
        accept: str = "", deps: list[str] | None = None,
    ) -> dict:
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        task = self.board.add(title, assignee, type=type, accept=accept, deps=deps)
        await self._fire_task(task)
        return task.to_dict()

    async def task_update(self, task_id: str, status: str) -> dict:
        """用户改板：状态流转 / 打回（review→in_progress，redo+1）/ 失败标记。

        status 传 "error" 走失败收尾（待办/进行中/待验收均可放弃）；其余走
        状态机（打回即 待验收→进行中）。
        """
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        if status == "error":
            task = self.board.mark_error(task_id)
        else:
            task = self.board.set_status(task_id, status)
        await self._fire_task(task)
        return task.to_dict()

    async def notify_assign(self, task_id: str, note: str = "") -> dict:
        """派工注记：发一条 assign 频道消息（from=director）。

        宿主在把工单置为 in_progress 后调用，让被派队员在频道里看到派工；
        note 可携带验收标准或补充说明。
        """
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        task = self.board.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        text = f"派工 {task.id}「{task.title}」"
        if task.accept:
            text += f"（验收标准：{task.accept}）"
        if note:
            text += f"\n{note}"
        msg = self.channel.post(
            "director", to_member=task.assignee, msg_kind="assign",
            task_ref=task.id, text=text,
        )
        await self._emit_message(msg)
        return msg.to_dict()

    # ---- AI 总管：装配与总管轮（director_mode="ai"，二期闭环）----

    def _build_director_registry(self) -> ToolRegistry:
        """总管的内部工具注册表：只操作工单板与编排状态，不触工作区。

        七件套全部 safety=READONLY（不经权限门确认、不发权限事件），工具实现
        见模块底部；集合与参数是后端接线的契约面（ StageContract 同步维护）。
        """
        return ToolRegistry([
            TeamAssignTool(self), TeamAcceptTool(self), TeamRejectTool(self),
            TeamReassignTool(self), TeamDropTool(self), TeamOpenHuddleTool(self),
            TeamDeliverTool(self),
        ])

    def _ensure_director_agent(self) -> Agent:
        """取或建总管 Agent：独立 history + 总管系统提示词 + 只挂内部团队工具。

        门用全新 PermissionGate 兜类型契约：工具表里只有内部团队工具（全部
        READONLY、不触工作区、不落引擎主目录），authorize 恒放行、不发权限
        事件；不透传用户钩子与 Mods——板面操作不该被会话级拦截手段卡死
        （那些手段本就只对工作区写与执行有意义）。
        """
        if self._director_agent is None:
            agent = Agent(
                provider=self._director.provider,
                registry=self._director_registry,
                gate=PermissionGate(working_dir=self._working_dir),
                working_dir=self._working_dir,
                max_iterations=self._max_iterations,
            )
            agent.set_system(TEAM_DIRECTOR_PROMPT.format(
                workdir=str(self._working_dir) if self._working_dir is not None
                else "(none - board only)",
                roster="、".join(
                    f"{m.name}（{m.provider_name}/{m.model}）" for m in self.roster
                ) or "（还没有队员）",
            ))
            self._director_agent = agent
        return self._director_agent

    async def _run_director_round(self) -> DirectorTurnResult:
        """唤醒总管跑一轮：注入插话/裁定/板面/频道 → 工具改板 → 发言定稿进频道。

        总管轮计入全局轮次上限（与成员唤醒轮合计，二期口径）；预算越线在此
        强制收队。单轮超时/故障与成员轮同姿态：部分发言 shield 定稿进频道，
        按失败隔离、团队不散伙。取消时半截发言定稿后原样上抛。工具调用
        改板并发 TeamTaskUpdated / 频道消息（都发生在本轮发言定稿之前，
        经 StreamDeltaMerger 的非增量冲刷保证与增量流的先后顺序）。
        """
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        if self.rounds_used >= self._max_rounds:
            await self.finish(status="rounds_exhausted")
            return DirectorTurnResult(
                status="error", error=f"已达全局轮次上限（{self._max_rounds} 轮），团队已收队",
            )
        if await self._budget_over():
            await self.finish(status="budget_exhausted")
            return DirectorTurnResult(status="error", error="已达团队 token 预算，团队已强制交付")
        if self._director.provider is None:
            error = self._director.build_error or "总管模型不可用"
            msg = self.channel.post(
                "system", to_member="all", msg_kind="system",
                text=f"（总管模型未能就位：{error}。可再次发送消息重试、接管为用户总管，或收队。）",
            )
            await self._emit_message(msg)
            raise TeamError(f"总管模型不可用：{error}")
        self.rounds_used += 1

        wake = self._build_director_wake()
        self.channel.mark_read("director")
        self._director_round_tools = []
        agent = self._ensure_director_agent()
        seq = self.channel.reserve_seq()  # 流式增量先于定稿知道消息编号
        parts: list[str] = []
        result = DirectorTurnResult(seq=seq)

        async def consume() -> None:
            async for ev in agent.run_turn(wake):
                if ev.kind == "text_delta":
                    parts.append(ev.text)
                    await self._emit(TeamMessageDelta(
                        member_index=DIRECTOR_MEMBER_INDEX, seq=seq, text=ev.text,
                    ))
                elif ev.kind in _MEMBER_FORWARD_KINDS:
                    # 权限事件结构性不可能出现（内部工具全 READONLY + 独立门），
                    # 过滤集合与成员轮共用只为少一份维护面
                    if ev.kind == "usage":
                        result.input_tokens += ev.input_tokens
                        result.output_tokens += ev.output_tokens
                        self._director_usage[0] += ev.input_tokens
                        self._director_usage[1] += ev.output_tokens
                    await self._emit(ev)

        try:
            await asyncio.wait_for(consume(), timeout=self._timeout_s)
        except TimeoutError:
            result.status = "error"
            result.error = f"总管单轮超时（>{self._timeout_s} 秒），按失败隔离"
        except asyncio.CancelledError:
            # 用户停止：部分发言先定稿进频道（与成员轮同姿态），再上抛
            result.status = "cancelled"
            result.error = "用户停止"
            await self._finalize_speech(
                from_member="director", to_member="all", msg_kind="ruling",
                empty_note="（总管本轮没有发言）", seq=seq, parts=parts,
                suffix="…（已停止）", shield=True,
            )
            raise
        except Exception as e:  # noqa: BLE001 - 总管故障不上抛（失败隔离）
            result.status = "error"
            result.error = str(e)[:200]

        suffix = f"\n\n（本轮失败：{result.error}）" if result.status == "error" else ""
        result.text = await self._finalize_speech(
            from_member="director", to_member="all", msg_kind="ruling",
            empty_note="（总管本轮没有发言）", seq=seq, parts=parts, suffix=suffix,
        )
        result.tool_calls = list(self._director_round_tools)
        return result

    def _build_director_wake(self) -> str:
        """总管唤醒消息：用户插话（最高优先级）→ 强制裁定 → 待验收 → 工单板 → 频道未读。

        插话原文在最高优先级段注入；同文的频道消息（_post_user_message 落簿的
        from=user 副本）从总管未读剔除——一条插话只见一次原文，最高优先级语义
        不丢，频道里用户原文照常保留给队员。频道部分用总管自己的未读游标
        （上次总管轮以来的新增，即尾部增量）；mark_read 在拼装后由
        _run_director_round 执行。绝不注入会话历史。
        """
        sections: list[str] = []
        notes = self._pending_user_notes
        note_seqs = {seq for _, seqs in notes for seq in seqs}
        if notes:
            self._pending_user_notes = []
            sections.append(
                "【用户插话（最高优先级，先于其他一切处理）】\n"
                + "\n".join(f"- {t}" for t, _ in notes)
            )
        forced = self._forced_directives()
        if forced:
            sections.append("【必须裁定（防失控上限已触发）】\n" + "\n".join(f"- {t}" for t in forced))
        reviews = [t for t in self.board.tasks() if t.status == "review"]
        if reviews:
            lines = [
                f"- {t.id}「{t.title}」（{t.assignee}，{'执行型' if t.type == 'exec' else '顾问型'}"
                + (f"，已打回 {t.redo} 次" if t.redo else "") + "）"
                for t in reviews
            ]
            sections.append("【待验收工单（队员报告见下方频道未读）】\n" + "\n".join(lines))
        tasks = self.board.tasks()
        if tasks:
            lines = [
                f"- {t.id}「{t.title}」 指派：{t.assignee}"
                f" 类型：{'执行型' if t.type == 'exec' else '顾问型'} 状态：{_status_label(t.status)}"
                + (f"（打回 {t.redo}/{self.board.redo_limit}）" if t.redo else "")
                + (f" 依赖：{'、'.join(t.deps)}" if t.deps else "")
                + (f" 验收标准：{t.accept}" if t.accept else "")
                for t in tasks
            ]
            sections.append("【工单板】\n" + "\n".join(lines))
        else:
            sections.append("【工单板】（空——还没有登记任何工单。）")
        unread = self.channel.render_unread("director", exclude_seqs=note_seqs)
        if unread:
            sections.append(f"【团队频道 · 未读消息】\n{unread}")
        sections.append(self._director_closing())
        return "\n\n".join(sections)

    def _director_closing(self) -> str:
        """本轮收尾指令：按板面状态给总管一句话的行动取向（验收 / 交付 / 推进）。"""
        tasks = self.board.tasks()
        if tasks and self.board.all_done() and not self._deliver_prompted:
            return (
                "全部工单已完成：请面向用户产出《交付说明》（成果汇总、各队员贡献、"
                "未尽事项），发言结束后调用 team_deliver 收口交付。"
            )
        if any(t.status == "review" for t in tasks):
            return (
                "请逐单验收待验收工单：对照验收标准调用 team_accept（通过）或 "
                "team_reject（打回附理由）。只发言不动板解决不了问题。"
            )
        return (
            "请推进团队目标：需要队员做的活用 team_assign 建单上板（依赖用 deps 表达，"
            "验收标准写具体），与队员的沟通直接写进发言（会进团队频道，队员下次唤醒可见）；"
            "没有要做的就简短说明现状。"
        )

    def _forced_directives(self) -> list[str]:
        """防失控上限触发的强制裁定指令（设计 §9；注入总管轮，必须经工具落板）。

        覆盖三类：打回超上限的待验收工单（三选一：改派/降级/砍掉）、停滞队员
        （连续 N 轮无新工具结果，自动唤醒已停）、依赖已失败的卡死待办工单。
        """
        out: list[str] = []
        limit = self.board.redo_limit
        for t in self.board.tasks():
            if t.status == "review" and t.redo >= limit:
                out.append(
                    f"工单 {t.id}「{t.title}」已被打回 {t.redo} 次（上限 {limit}），不能再打回："
                    "必须三选一经工具落板——team_reassign 改派他人重做 / "
                    'team_reassign(type="advisor") 降级为顾问型重派 / '
                    "team_drop 砍掉并记入未尽事项。"
                )
        for m in self.roster:
            if m.name in self._stalled and any(
                t.assignee == m.name and t.status in ("in_progress", "review")
                for t in self.board.tasks()
            ):
                out.append(
                    f"队员「{m.name}」连续 {self._stall_counts.get(m.name, self._stall_limit)}"
                    f" 轮发言没有任何新工具结果（停滞上限 {self._stall_limit}），"
                    "已停止自动唤醒：请处置——其工单若已待验收或失败，"
                    "team_reassign 改派他人重做（待验收的不要打回，打回后无人被唤醒）；"
                    "若仍在进行中，先 team_drop 放弃，再用 team_assign 建新工单"
                    "改派他人（新工单会复位停滞标记）。"
                )
        done_ids = {t.id for t in self.board.tasks() if t.status == "done"}
        for t in self.board.tasks():
            if t.status == "pending" and t.deps and any(
                d not in done_ids and (dep := self.board.get(d)) is not None
                and dep.status == "error"
                for d in t.deps
            ):
                out.append(
                    f"工单 {t.id}「{t.title}」的依赖工单已失败，依赖链无法满足：请处置"
                    "（重派/改派失败的依赖工单恢复依赖链，或 team_drop 砍掉本工单"
                    "并记入未尽事项）。"
                )
        return out

    # ---- AI 总管：自动循环（用户一句话目标 → 拆解 → 派工 → 收报 → 验收 → 交付）----

    async def run_auto_turn(self, text: str, images: list | None = None) -> dict:
        """AI 总管自动闭环的一轮（用户消息驱动）：消息进频道 → 循环推进至空闲或终态。

        循环骨架：总管轮（拆解/派单/验收/裁定/交付，工具改板）→ 小会 →
        派发依赖就绪的待办工单 → 按名册唤醒名下有进行中工单的队员（轮毕
        正常完成置待验收、失败/超时标 error，设计 §9）→ 有待验收回总管轮
        ……直到：总管交付（TeamFinished done）/ 轮次耗尽 / 预算越线 /
        总管轮故障 / 连续两轮无进展（空闲，等用户插话或下一次消息）。
        返回值形状与 handle_user_message 对齐
        （posted_seqs / woke / rounds_used / finished / snapshot）。
        """
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        if self.director_mode != "ai":
            raise TeamError("当前是用户总管模式：消息直接发送即可（每条消息就是一道总管指令）。")
        if self._loop_running:
            raise TeamError("总管正在推进中：请稍候再发消息，或使用接管 / 收队。")
        text = (text or "").strip()
        if not text:
            raise TeamError("消息内容为空")
        if images:
            await self._fire(NoticeEvent(
                message=f"团队回合暂不支持图片：本轮的 {len(images)} 张图片已忽略；"
                        "需要队员看图请先收队，关闭团队后重发。"
            ))
        posted_seqs = await self._post_user_message(text)
        # 下一总管轮最高优先级注入（设计 §2.1 插话权）；seqs 一并带上供未读剔除
        self._pending_user_notes.append((text, list(posted_seqs)))

        self._loop_running = True
        woke: list[dict] = []
        idle_strikes = 0
        try:
            while self._finished is None:
                turn = await self._run_director_round()
                if self._finished is not None:
                    break
                huddle_ran = False
                if turn.status == "done":
                    huddle_ran = await self._run_pending_huddles()
                    if self._finished is not None:
                        break
                    if self._deliver_requested:
                        self._deliver_requested = False
                        await self.finish(status="done")
                        break
                dispatched = await self._dispatch_ready()
                round_woke = await self._wake_actionable()
                woke.extend(round_woke)
                if self._finished is not None:
                    break
                # 本轮有成员轮失败落板（工单标 error，设计 §9 失败隔离）：总管
                # 下一轮必须看到失败工单好改派/放弃——既有 error→重派路径照用，
                # 不靠「依赖失败」的强制裁定才醒来（失败工单没有下游时不触发它）
                error_landed = any(w.get("status") == "error" for w in round_woke)
                if turn.status != "done":
                    # 总管轮超时/故障：频道已有注记；已就绪的工单照常派发（上面），
                    # 本轮到此为止——团队保持活动，下条消息重试
                    break
                progressed = bool(dispatched or round_woke or turn.tool_calls or huddle_ran)
                reviews = [t for t in self.board.tasks() if t.status == "review"]
                if reviews:
                    idle_strikes = 0 if progressed else idle_strikes + 1
                    if idle_strikes >= 2:
                        break  # 总管对着待验收连续两轮不动板：空闲退出防打转
                    continue
                if self.board.tasks() and self.board.all_done() and not self._deliver_prompted:
                    self._deliver_prompted = True  # 催一轮交付；再不动板由下方空闲出口收束
                    idle_strikes = 0
                    continue
                if progressed:
                    idle_strikes = 0
                    if self._forced_directives() or error_landed:
                        continue  # 停滞/卡死待裁定，或有失败工单待处置：总管还有必须处理的事
                    break  # 剩余工单在等依赖，或已无总管可做之事
                idle_strikes += 1
                if idle_strikes < 2 and self._forced_directives():
                    continue
                break
        finally:
            self._loop_running = False
            self._deliver_requested = False
            self._pending_huddles.clear()  # 未跑成的小会作废（用户插话/接管后由总管重排）
        return self._turn_summary(posted_seqs, woke, finished=self.finished)

    async def inject_user_message(self, text: str, images: list | None = None) -> dict:
        """用户插话（AI 总管模式，设计 §2.1）：消息进频道 + 最高优先级注入总管下一轮。

        跨任务安全：循环推进中也可调用——只动频道与待注入清单（两侧都是
        同步段，无竞态），循环在下一个总管轮取走。空闲时返回
        loop_running=False，由宿主尽快调 run_auto_turn 唤醒总管。
        """
        if not self.active:
            raise TeamError("当前没有进行中的团队")
        if self.director_mode != "ai":
            raise TeamError("当前是用户总管模式：消息直接发送即可（每条消息就是一道总管指令）。")
        text = (text or "").strip()
        if not text:
            raise TeamError("消息内容为空")
        if images:
            await self._fire(NoticeEvent(
                message=f"团队回合暂不支持图片：本轮的 {len(images)} 张图片已忽略；"
                        "需要队员看图请先收队，关闭团队后重发。"
            ))
        posted_seqs = await self._post_user_message(text)
        self._pending_user_notes.append((text, list(posted_seqs)))
        return {
            "posted_seqs": posted_seqs,
            "loop_running": self._loop_running,
            "rounds_used": self.rounds_used,
        }

    async def takeover(self) -> dict:
        """用户接管（设计 §2.1）：切回用户总管模式；工单板与频道原样保留。

        在飞的总管轮由宿主先取消并等收尾（team.stop 同款的时序前提），这里
        只做状态切换与清场：未消费的插话/待开小会作废（用户亲自接管后由用户
        决定），停滞标记清零（用户可随意点名唤醒）。此后 handle_user_message
        照常工作，成员历史 / Agent 实例全部复用。
        """
        if self._finished is not None:
            raise TeamError("团队已进入终态，无需接管")
        if self.director_mode != "ai":
            raise TeamError("当前已是用户总管模式")
        self.director_mode = "user"
        # 成员系统提示词按新总管形态同步刷新（旧句写的是 AI 总管及其模型，
        # 接管后队员应向用户本人汇报）；成员 Agent 实例与 history 原样复用
        for rt in self._runtimes.values():
            if rt.agent is not None:
                rt.agent.set_system(self._member_system_prompt(rt.spec))
        self._pending_user_notes.clear()
        self._pending_huddles.clear()
        self._deliver_requested = False
        self._stall_counts.clear()
        self._stalled.clear()
        msg = self.channel.post(
            "system", to_member="all", msg_kind="system",
            text="（用户已接管：切换为用户总管模式，工单板与团队频道保留。）",
        )
        await self._emit_message(msg)
        return {"director_mode": self.director_mode, "snapshot": self.snapshot()}

    async def _dispatch_ready(self) -> list[TeamTask]:
        """派发依赖已满足的待办工单（置进行中 + 派工注记，复用一期 notify_assign）。"""
        dispatched: list[TeamTask] = []
        for task in self.board.dispatchable():
            self.board.set_status(task.id, "in_progress")
            await self._fire_task(task)
            await self.notify_assign(task.id)
            dispatched.append(task)
        if dispatched:
            self._deliver_prompted = False  # 有新活派出去，「催交付」状态复位
        return dispatched

    async def _wake_actionable(self) -> list[dict]:
        """按名册顺序唤醒名下有进行中工单的队员（复用一期唤醒与权限路径）。

        轮毕按成员轮结果落板（设计 §9）：正常完成 → 名下进行中工单置待验收
        （成员轮的回复就是报告）；失败/超时 → 工单标 error（失败隔离，沿圆桌
        先例），下游依赖卡死交给既有的「依赖失败强制裁定」分支处置。停滞队员
        跳过自动唤醒（强制移交总管，指令经 _forced_directives 注入总管轮）；
        停滞计数只对执行型唤醒轮生效——顾问型本就无工具可用，不算停滞。
        """
        woke: list[dict] = []
        for spec in self.roster:
            if self._finished is not None:
                break
            if spec.name in self._stalled:
                continue
            mine = [t for t in self.board.by_assignee(spec.name) if t.status == "in_progress"]
            if not mine:
                continue
            had_exec = any(t.type == "exec" for t in mine)
            try:
                result = await self.run_member_turn(spec.name)
            except asyncio.CancelledError:
                woke.append({"member": spec.name, "status": "cancelled",
                             "error": "用户停止", "seq": 0})
                raise
            woke.append({"member": result.member, "status": result.status,
                         "error": result.error, "seq": result.seq})
            if self._finished is not None:
                break  # 预算/轮次在 run_member_turn 内触发收队：不再动板
            # 轮毕落板按成员轮结果分流（设计 §9）：正常完成 → 待验收（review →
            # 总管验收/打回的既有状态机边）；失败/超时 → 标 error（失败隔离），
            # 依赖它的待办工单由「依赖失败强制裁定」分支交总管处置
            for t in mine:
                if t.status == "in_progress":
                    if result.status == "done":
                        task = self.board.set_status(t.id, "review")
                    else:
                        task = self.board.mark_error(t.id, reason=result.error)
                    await self._fire_task(task)
            # 停滞守卫（设计 §9）：连续 N 轮发言没有任何「新」工具结果 →
            # 强制移交总管（与上一轮完全相同的重复调用不算新产出，见
            # run_member_turn 的 new_tools_used 口径）
            if had_exec and result.status == "done":
                if result.new_tools_used == 0:
                    count = self._stall_counts.get(spec.name, 0) + 1
                    self._stall_counts[spec.name] = count
                    if count >= self._stall_limit:
                        self._stalled.add(spec.name)
                        note = self.channel.post(
                            "system", to_member="all", msg_kind="system",
                            text=f"（队员「{spec.name}」连续 {count} 轮发言没有任何新工具结果，"
                                 "已停止自动唤醒，移交总管处置。）",
                        )
                        await self._emit_message(note)
                else:
                    self._stall_counts[spec.name] = 0
        return woke

    # ---- 小会（设计 §4.3：run_roundtable 只读复用，事件全内收）----

    async def _run_pending_huddles(self) -> bool:
        """跑掉总管本轮排定的小会（多张逐场串行）。返回是否有小会实际开过。"""
        ran = False
        while self._pending_huddles and self._finished is None:
            req = self._pending_huddles.pop(0)
            await self._run_huddle(req)
            ran = True
        return ran

    async def _run_huddle(self, req: HuddleRequest) -> None:
        """开一场小会：队员=相关队员的 provider、主席=总管 provider、辩论 1 轮。

        run_roundtable 的 emit 全部换成内部收集——Roundtable* / 过程 Notice
        绝不外发（严禁向前端泄漏圆桌事件），结论只以频道消息呈现：队员意见
        TeamMessage(from=成员, msg_kind=report)、最终裁定 TeamMessage
        (from=director, msg_kind=ruling)。各参与者的用量在定稿前合成一条
        Usage 事件发出，宿主的 team_emit 按「先用量后定稿」归属到名下
        （总管行按 director_info 的 provider/model 入账）；主席用量同步累计
        进 _director_usage，snapshot.director 与 usage_log 两口径一致。
        """
        if self._director.provider is None:
            note = self.channel.post(
                "system", to_member="all", msg_kind="system",
                text="（小会未能召开：总管模型不可用，无法收口裁定。）",
            )
            await self._emit_message(note)
            return
        if self.rounds_used >= self._max_rounds:
            await self.finish(status="rounds_exhausted")
            return
        if await self._budget_over():
            await self.finish(status="budget_exhausted")
            return
        self.rounds_used += 1  # 一场小会计 1 个全局轮（防总管刷小会烧预算）

        specs = [self._runtimes[name].spec for name in req.members]
        announcement = self.channel.post(
            "system", to_member="all", msg_kind="system",
            text=f"总管就「{req.topic}」召开小会：{'、'.join(req.members)} 各表意见。",
        )
        await self._emit_message(announcement)
        dispute = req.topic if not req.context else f"{req.topic}\n\n相关背景：\n{req.context}"

        async def swallow(_ev: AgentEvent) -> None:
            return None  # 内部收集：圆桌事件一律不出编排器

        outcome = await run_roundtable(
            members=[
                MemberSpec(provider_name=s.provider_name, model=s.model,
                           provider=s.provider, build_error=s.build_error)
                for s in specs
            ],
            chair=self._director.provider,
            system_text=TEAM_HUDDLE_PROMPT,
            history=[],
            user_text=dispute,
            timeout_s=self._timeout_s,
            emit=swallow,
            debate_rounds=1,
            fuse=True,
            chair_provider=self._director.provider_name,
            chair_model=self._director.model,
        )
        if outcome.status == "cancelled":
            # 用户停止：无任何增量外发过，频道保持干净；保持取消语义上抛
            raise asyncio.CancelledError
        for r in outcome.members:
            if r.input_tokens or r.output_tokens:
                await self._emit(Usage(input_tokens=r.input_tokens, output_tokens=r.output_tokens))
            text = (r.text.strip() if r.status == "done" and r.text.strip()
                    else f"（小会发言未能产出：{r.error or '未知错误'}）")
            msg = self.channel.post(specs[r.index].name, to_member="all", msg_kind="report", text=text)
            await self._emit_message(msg)
        if outcome.chair_input_tokens or outcome.chair_output_tokens:
            # 用量口径并账（裁定）：小会主席=总管 provider，其用量除经 Usage
            # 事件进宿主 usage_log 的总管行外，同步累计进 _director_usage——
            # snapshot.director 的合计与 usage_log 保持可对账
            self._director_usage[0] += outcome.chair_input_tokens
            self._director_usage[1] += outcome.chair_output_tokens
            await self._emit(Usage(
                input_tokens=outcome.chair_input_tokens,
                output_tokens=outcome.chair_output_tokens,
            ))
        if outcome.fused_text.strip():
            ruling = self.channel.post(
                "director", to_member="all", msg_kind="ruling", text=outcome.fused_text.strip(),
            )
            await self._emit_message(ruling)
        else:
            note = self.channel.post(
                "system", to_member="all", msg_kind="system",
                text=f"（小会未能形成裁定：{outcome.error or '融合失败'}；以上意见供总管参考。）",
            )
            await self._emit_message(note)

    # ---- 终态 ----

    async def _maybe_finish(self) -> dict | None:
        """轮次预算耗尽则强制交付；正常交付（全部完成 + 用户确认）由宿主显式调 finish。"""
        if self.rounds_used >= self._max_rounds:
            return await self.finish(status="rounds_exhausted")
        return None

    async def stop(self, reason: str = "") -> dict:
        """收队（用户立即终止）：TeamFinished(aborted) + 未尽事项。"""
        return await self.finish(status="aborted", reason=reason)

    async def finish(self, status: str = "done", reason: str = "") -> dict:
        """进入终态：发 TeamFinished 并生成摘要（《交付说明》）。

        status：done（交付）/ aborted（收队）/ rounds_exhausted（轮次耗尽）/
        budget_exhausted（token 预算越线强制交付，二期）。
        返回 {"status", "summary", "snapshot"}——snapshot 即落进 assistant
        消息 meta 的团队摘要（仿 roundtable meta，随会话持久化）。
        """
        if self._finished is not None:
            return {
                "status": self._finished["status"],
                "summary": self._finished["summary"],
                "snapshot": self.snapshot(),
            }
        if status not in ("done", "aborted", "rounds_exhausted", "budget_exhausted"):
            raise TeamError(f"未知的团队终态：{status}")
        summary = self._build_summary(status, reason)
        self._finished = {"status": status, "summary": summary}
        await self._fire(TeamFinished(status=status, summary=summary))
        return {"status": status, "summary": summary, "snapshot": self.snapshot()}

    def _build_summary(self, status: str, reason: str) -> str:
        head = {
            "done": "《交付说明》：全部工单已完成。",
            "aborted": "《收队说明》：团队已由用户收队，以下为未尽事项。",
            "rounds_exhausted": (
                f"《交付说明》：已达全局轮次上限（{self._max_rounds} 轮），强制交付，"
                "以下为未尽事项。"
            ),
            "budget_exhausted": (
                "《交付说明》：已达团队 token 预算，强制交付，以下为未尽事项。"
            ),
        }[status]
        lines = [head]
        if reason:
            lines.append(f"原因：{reason}")
        done = [t for t in self.board.tasks() if t.status == "done"]
        if done:
            lines.append("已完成工单：")
            lines.extend(f"- {t.id}「{t.title}」（{t.assignee}）" for t in done)
        left = self.board.incomplete()
        if left:
            lines.append("未尽事项：")
            lines.extend(
                f"- {t.id}「{t.title}」（{t.assignee}，{_status_label(t.status)}"
                f"{'，打回 ' + str(t.redo) + ' 次' if t.redo else ''}）"
                for t in left
            )
        if not done and not left:
            lines.append("（没有登记过工单。）")
        return "\n".join(lines)

    # ---- 快照（meta 落库与回放）----

    def snapshot(self) -> dict:
        """团队摘要：{team: {roster, tasks, messages(截断), status, …}}，仿 roundtable meta。

        AI 总管模式额外带 director 块（标识 + 累计用量 + 停滞名单），供 meta
        落库与回放；用户总管模式保持一期键集不变（既有回放/断言不受影响）。
        """
        status = self._finished["status"] if self._finished is not None else "active"
        team: dict = {
            "team_id": self.team_id,  # 建队分配的唯一编号（前端回放 team.log 的主轴）
            "roster": self._roster_dicts(),
            "director_mode": self.director_mode,
            "status": status,
            "rounds_used": self.rounds_used,
            "max_rounds": self._max_rounds,
            "tasks": [t.to_dict() for t in self.board.tasks()],
            "messages": [
                m.to_dict(text_cap=SNAPSHOT_MESSAGE_TEXT_CAP)
                for m in self.channel.tail(SNAPSHOT_MESSAGE_TAIL)
            ],
        }
        if self.director_mode == "ai":
            team["director"] = {
                **self.director_info(),
                "input_tokens": self._director_usage[0],
                "output_tokens": self._director_usage[1],
                "stalled": sorted(self._stalled),
            }
        return {"team": team}

    # ---- 事件（全部经构造注入的 emit 回调发出）----

    async def _fire(self, ev: AgentEvent) -> None:
        await self._emit(ev)

    async def _persist_message(self, msg: ChannelMessage) -> None:
        """频道消息落库钩子（三期）：构造注入的 message_sink，未注入不落库。

        sink(team_id, msg) 在消息定稿（channel.post 已完成）后、事件帧发出
        前调用——事件发不出去不影响落库，频道时间线以库内记录为准。落库是
        best-effort 旁路：sink 异常只记日志不外溢（与 _budget_over 的仪表
        姿态一致）；用户停止瞬间被取消打断的落库按容忍丢失处理（极端边角，
        换取取消收尾路径的简单可靠）。
        """
        sink = self._message_sink
        if sink is None or not self.team_id:
            return
        try:
            result = sink(self.team_id, msg)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                await result
        except Exception as e:  # noqa: BLE001 - 落库失败不拖垮协作
            logger.warning(
                "团队频道消息落库失败（team_id=%s seq=%s）：%s",
                self.team_id, msg.seq, e,
            )

    async def _emit_message(self, msg: ChannelMessage, shield: bool = False) -> None:
        await self._persist_message(msg)
        ev = TeamMessage(
            seq=msg.seq, from_member=msg.from_member, to_member=msg.to_member,
            msg_kind=msg.msg_kind, task_ref=msg.task_ref, text=msg.text,
        )
        if shield:
            # 取消收尾：shield 让发帧不被二次取消打断；真被取消就放弃——
            # 定稿已同步进频道与 snapshot，回放侧不缺这条消息
            try:
                await asyncio.shield(self._fire(ev))
            except asyncio.CancelledError:
                pass
        else:
            await self._fire(ev)

    async def _fire_task(self, task: TeamTask) -> None:
        await self._fire(TeamTaskUpdated(
            task_id=task.id, title=task.title, assignee=task.assignee,
            type=task.type, status=task.status, redo=task.redo,
        ))


# ---- AI 总管的内部团队工具（二期，设计 §2.1/§11）----
# 七件套只操作工单板与编排状态：不触工作区、不落任何文件、不经权限门确认、
# 不发权限事件。safety=READONLY 是「免确认」分级（与成员执行型工具同一分级
# 词汇）；MCP 注解按实际语义如实声明——会改团队（内存）状态的工具不谎报
# read_only_hint，对环境（工作区/外部世界）零接触故 destructive/open_world 为假。
# 集合与参数是后端接线的契约面；总管不领工单（board.add 只接受名册成员），
# accept 结构上只能针对队员名下工单，不存在自验收。

class _DirectorToolBase(Tool):
    """总管内部工具基类：持有编排器引用，统一前置活动校验与 TeamError 转译。"""

    def __init__(self, orch: TeamOrchestrator) -> None:
        self.orch = orch

    def _require_active(self) -> None:
        if not self.orch.active:
            raise ToolError("当前没有进行中的团队")


class TeamAssignArgs(BaseModel):
    title: str = Field(description="工单标题：一句话说清要交付什么")
    assignee: str = Field(description="指派队员名（必须是名册内的成员，不能是总管自己）")
    type: str = Field(
        default="exec",
        description="exec=执行型（队员动手读写文件/跑命令）/ advisor=顾问型（只出分析与建议）",
    )
    accept: str = Field(default="", description="验收标准（建议必填，验收时逐条对照）")
    deps: list[str] = Field(default_factory=list, description="依赖工单编号（如 [\"T1\"]），依赖完成后才派发")


class TeamAssignTool(_DirectorToolBase):
    name = "team_assign"
    description = (
        "建单上板：把分工方案里的一项工作登记为工单并指派队员。一次调用建一单；"
        "有先后依赖用 deps 表达（依赖完成才派发）。建好的单由编排器自动派发并唤醒队员。"
    )
    safety = Safety.READONLY  # 只改团队工单板（内存态），不触工作区、无需权限确认
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamAssignArgs

    async def run(self, args: TeamAssignArgs, ctx: ToolContext) -> str:
        self._require_active()
        try:
            task = await self.orch.task_add(
                args.title, args.assignee, type=args.type, accept=args.accept, deps=args.deps,
            )
        except TeamError as e:
            raise ToolError(str(e)) from None
        self.orch._note_director_tool(f"建单 {task['id']}「{task['title']}」→ {task['assignee']}")
        self.orch._deliver_prompted = False
        return (
            f"工单 {task['id']} 已上板（{task['assignee']}，"
            f"{'执行型' if task['type'] == 'exec' else '顾问型'}，待办）。"
            "依赖满足后编排器会自动派发并唤醒队员，不需要再做别的。"
        )


class TeamAcceptArgs(BaseModel):
    task_id: str = Field(description="要验收的工单编号（须处于待验收）")
    note: str = Field(default="", description="验收意见（可空，会进团队频道）")


class TeamAcceptTool(_DirectorToolBase):
    name = "team_accept"
    description = (
        "验收通过：对照验收标准确认队员的报告合格，把待验收工单置为完成。"
        "板上只有队员名下的工单（总管不领工单），不存在自验收。"
    )
    safety = Safety.READONLY
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamAcceptArgs

    async def run(self, args: TeamAcceptArgs, ctx: ToolContext) -> str:
        self._require_active()
        try:
            task = await self.orch.task_update(args.task_id, "done")
        except TeamError as e:
            raise ToolError(str(e)) from None
        self.orch._note_director_tool(f"验收通过 {task['id']}")
        text = f"验收通过 {task['id']}「{task['title']}」"
        if args.note.strip():
            text += f"：{args.note.strip()}"
        msg = self.orch.channel.post(
            "director", to_member=task["assignee"], msg_kind="ruling",
            task_ref=task["id"], text=text,
        )
        await self.orch._emit_message(msg)
        return f"{task['id']} 已完成，验收结论已发频道。"


class TeamRejectArgs(BaseModel):
    task_id: str = Field(description="要打回的工单编号（须处于待验收）")
    reason: str = Field(description="打回理由：哪里不合格、重做要注意什么（必填）")


class TeamRejectTool(_DirectorToolBase):
    name = "team_reject"
    description = (
        "打回：对照验收标准认为报告不合格时，把待验收工单退回进行中（redo+1）"
        "并附理由，队员下次唤醒可见。打回次数超上限时本工具会被拒绝，"
        "须按强制裁定改派 / 降级 / 放弃。"
    )
    safety = Safety.READONLY
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamRejectArgs

    async def run(self, args: TeamRejectArgs, ctx: ToolContext) -> str:
        self._require_active()
        reason = args.reason.strip()
        if not reason:
            raise ToolError("打回必须附理由（说清哪里不合格、重做要注意什么）")
        existing = self.orch.board.get(args.task_id)
        if existing is None:
            raise ToolError(f"工单 {args.task_id} 不存在")
        if existing.status != "review":
            # 契约前置（打回只对「待验收」成立）：堵住状态机的两条越界边——
            # 把待办直接派发成进行中（绕过正规派发注记）与把失败工单原地
            # 复活给原队员（重派须经 team_reassign 改派他人，板不允许原队员
            # 原地重做）。「打回」话术不得掩盖派发/复活动作。
            raise ToolError(
                f"工单 {args.task_id} 当前不在待验收（{_status_label(existing.status)}），"
                "team_reject 只用于打回待验收工单：待办工单依赖满足后编排器会"
                "自动派发；失败工单要重做请用 team_reassign 改派他人。"
            )
        try:
            task = await self.orch.task_update(args.task_id, "in_progress")
        except TeamError as e:
            raise ToolError(str(e)) from None
        self.orch._note_director_tool(f"打回 {task['id']}")
        msg = self.orch.channel.post(
            "director", to_member=task["assignee"], msg_kind="ruling",
            task_ref=task["id"], text=f"打回 {task['id']}「{task['title']}」：{reason}",
        )
        await self.orch._emit_message(msg)
        return (
            f"{task['id']} 已退回进行中（已打回 {task['redo']} 次，"
            f"上限 {self.orch.board.redo_limit}）；编排器会重新唤醒队员。"
        )


class TeamReassignArgs(BaseModel):
    task_id: str = Field(description="要改派的工单编号（须处于待验收或失败）")
    assignee: str = Field(description="新指派队员名（必须不同于当前指派）")
    type: str = Field(
        default="",
        description="可同时改工单类型：advisor=降级为顾问型（超限强制裁定的降级出口）",
    )
    note: str = Field(default="", description="改派说明（可空，会记入工单与频道）")


class TeamReassignTool(_DirectorToolBase):
    name = "team_reassign"
    description = (
        "改派：把待验收/失败的工单换给另一名队员重做。这是超打回上限后强制"
        "裁定的出口之一（此时新队员的打回预算清零）；可同时用 type=\"advisor\" "
        "降级为顾问型。想给原队员新的机会请改用 team_drop + 重建新工单。"
    )
    safety = Safety.READONLY
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamReassignArgs

    async def run(self, args: TeamReassignArgs, ctx: ToolContext) -> str:
        self._require_active()
        existing = self.orch.board.get(args.task_id)
        try:
            task = self.orch.board.reassign(
                args.task_id, args.assignee, type=args.type or None, note=args.note.strip(),
            )
        except TeamError as e:
            raise ToolError(str(e)) from None
        self.orch._note_director_tool(f"改派 {task.id} → {task.assignee}")
        self.orch._deliver_prompted = False
        if existing is not None:
            self.orch._clear_stall(existing.assignee)  # 原队员脱离该工单，停滞标记复位
        self.orch._clear_stall(task.assignee)  # 新队员从零计停滞
        await self.orch._fire_task(task)
        await self.orch.notify_assign(task.id, note=args.note.strip())
        return f"{task.id} 已改派给 {task.assignee}（进行中），编排器会唤醒新队员。"


class TeamDropArgs(BaseModel):
    task_id: str = Field(description="要放弃的工单编号（须处于待办、进行中或待验收）")
    reason: str = Field(default="", description="放弃原因（记入工单与《交付说明》未尽事项）")


class TeamDropTool(_DirectorToolBase):
    name = "team_drop"
    description = (
        "放弃工单：把待办/进行中/待验收的工单标记失败并记入未尽事项（超打回"
        "上限强制裁定的出口之一；依赖链卡死的待办工单也经此砍掉）。想换人重做"
        "请改用 team_reassign；想给原队员新的机会可放弃后用 team_assign 重建"
        "新工单指派给他（新工单会复位停滞标记）。"
    )
    safety = Safety.READONLY
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamDropArgs

    async def run(self, args: TeamDropArgs, ctx: ToolContext) -> str:
        self._require_active()
        existing = self.orch.board.get(args.task_id)
        try:
            task = self.orch.board.mark_error(args.task_id, reason=args.reason.strip())
        except TeamError as e:
            raise ToolError(str(e)) from None
        self.orch._note_director_tool(f"放弃 {task.id}")
        if existing is not None:
            self.orch._clear_stall(existing.assignee)
        await self.orch._fire_task(task)
        text = f"放弃 {task.id}「{task.title}」"
        if args.reason.strip():
            text += f"：{args.reason.strip()}"
        msg = self.orch.channel.post(
            "director", to_member="all", msg_kind="ruling", task_ref=task.id, text=text,
        )
        await self.orch._emit_message(msg)
        return f"{task.id} 已标记失败并记入未尽事项。"


class TeamOpenHuddleArgs(BaseModel):
    topic: str = Field(description="争议议题：单一、具体、可表决（不要一次塞多个问题）")
    members: list[str] = Field(description="参与队员名（2~3 名相关队员）")
    context: str = Field(default="", description="议题背景（可空：相关工单、分歧点、已有结论）")


class TeamOpenHuddleTool(_DirectorToolBase):
    name = "team_open_huddle"
    description = (
        "开小会：就单一争议征询 2~3 名相关队员的意见（各表一轮、互相可见），"
        "总管的裁定随后以频道消息公布。重大分歧谈不拢时用，不要用于普通推进。"
    )
    safety = Safety.READONLY
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamOpenHuddleArgs

    async def run(self, args: TeamOpenHuddleArgs, ctx: ToolContext) -> str:
        self._require_active()
        topic = args.topic.strip()
        if not topic:
            raise ToolError("小会议题不能为空")
        roster_names = [m.name for m in self.orch.roster]
        seen: list[str] = []
        for raw in args.members:
            name = raw.strip()
            if not name:
                continue
            if name not in roster_names:
                raise ToolError(f"小会成员「{name}」不在团队名册中")
            if name not in seen:
                seen.append(name)
        seen.sort(key=roster_names.index)  # 按名册序表决，与唤醒顺序同规
        if not (_HUDDLE_MIN_MEMBERS <= len(seen) <= _HUDDLE_MAX_MEMBERS):
            raise ToolError(
                f"小会需要 {_HUDDLE_MIN_MEMBERS}~{_HUDDLE_MAX_MEMBERS} 名队员，收到 {len(seen)} 名"
            )
        self.orch._pending_huddles.append(
            HuddleRequest(topic=topic, members=seen, context=args.context.strip())
        )
        self.orch._note_director_tool(f"发起小会「{topic[:30]}」（{'、'.join(seen)}）")
        return (
            f"小会已排定：就「{topic}」征询 {'、'.join(seen)} 的意见（辩论一轮）。"
            "本轮发言结束后自动进行，意见与裁定会依次进频道。"
        )


class TeamDeliverArgs(BaseModel):
    """team_deliver 无参数：交付说明以总管本轮发言为准。"""


class TeamDeliverTool(_DirectorToolBase):
    name = "team_deliver"
    description = (
        "交付收口：确认《交付说明》已（或即将）随本轮发言发出后调用；团队随即"
        "进入终态，未完成的工单会记入未尽事项。每轮最多调用一次。"
    )
    safety = Safety.READONLY
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = TeamDeliverArgs

    async def run(self, args: TeamDeliverArgs, ctx: ToolContext) -> str:
        self._require_active()
        self.orch._deliver_requested = True
        self.orch._note_director_tool("请求交付")
        return "交付请求已登记：本轮发言结束后团队收口（TeamFinished done）。不要重复调用。"
