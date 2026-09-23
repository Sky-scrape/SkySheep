"""子代理：主 Agent 可派生的独立上下文 Agent。

安全模型（M2）：
- 子代理只做只读研究（read/list/glob/grep/load_skill）；
- 写工具对 task 型子代理开放，但 SubagentGate 会自动拒绝所有需确认的操作——
  因为子代理运行期间主循环被占住，无法弹确认（会死锁）；
  需要写文件/跑命令时子代理应在报告中说明，由主 Agent 请求用户确认后执行；
- 子代理工具集里没有 spawn_agent，禁止递归派生。

两种运行方式：
- 同步（explore 默认）：spawn_agent 阻塞到完成，报告作为 tool_result 返回；
- 后台（background=true）：立即返回 task_id；并发满员时自动排队（queued），
  空位出来按序开跑；主 Agent 用 check_task 查询、wait_task 等待完成。

任务簿：
- 后台任务完成且报告未投递时记一条待办提示，宿主在下一轮开始时注入上下文，
  让主 Agent 知道「有任务做完了」（不用用户来催）；
- 超过内联阈值的报告自动落盘到 <workdir>/.skysheep/reports/<task_id>-<type>.md，
  投递时只给路径 + 开头摘录，长报告不再整份挤进主上下文；
- 终态任务持久化到 state_path（默认 ~/.skysheep/subagent_tasks.json），重启后
  仍可查询；重启时处于 running/queued 的记录标为中断。

宿主可注入的钩子：
- usage_recorder：任务结束后把 token 用量记入 usage_log（子代理是独立 Agent
  实例，不记账就绕过了用量页与每日预算护栏）；
- event_emitter：把子代理的事件流（过滤后）转发给前端直播；
- max_concurrent：后台任务并发上限，防止模型一口气派出大量任务烧钱。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field

from ..models.base import Provider
from ..security.gate import Decision, PermissionGate
from ..textio import write_text_atomic
from ..tools import GlobTool, GrepTool, ListDirTool, ReadFileTool, Safety, Tool, ToolRegistry
from ..tools.base import ToolContext
from .agent import Agent
from .prompt import SUBAGENT_PROMPT
from .subagent_store import BUILTIN_AGENT_TYPES, BUILTIN_DESCRIPTIONS
from .worktree import commit_all, ensure_worktree, remove_worktree

# 只读研究型工具集（所有内置型共用基础）
RESEARCH_TOOL_NAMES = ("read_file", "list_dir", "glob", "grep")

# 各内置型在基础只读集之上追加的工具（按名字从主注册表的工具里挑）。
# SubagentGate 仍会拦截一切需确认的操作——这里给多少都越不过安全边界。
EXTRA_TOOL_NAMES_BY_TYPE = {
    "task": ("write_file", "edit_file", "write_document"),
    "researcher": ("web_search", "web_fetch"),
}

# 报告超过内联阈值就落盘，投递只带路径 + 开头摘录（上下文经济性）
REPORT_INLINE_LIMIT = 2400
REPORT_EXCERPT_CHARS = 1200
# 任务簿持久化保留的终态记录条数（超出按创建时间淘汰最旧的）
TASK_BOOK_KEEP = 50
# 后台任务排队上限（并发满员后还能收下多少个排队任务）
TASK_QUEUE_KEEP = 20

# 内置型的角色提示词（追加在 SUBAGENT_PROMPT 之后）。task/explore 不用额外说明。
BUILTIN_ROLE_PROMPTS = {
    "reviewer": (
        "\nRole: 代码/文档审查员。仔细审阅任务指定的文件或改动，从正确性、边界条件、"
        "安全隐患、可读性四个维度找问题；输出按「严重 / 建议 / 疑问」分级的审查清单，"
        "每条给出文件与行号和一句话修改建议。不要动手改任何文件。"
    ),
    "researcher": (
        "\nRole: 联网调研员。优先用 web_search / web_fetch 搜集公开资料（可读本地文件补充上下文）；"
        "交叉核对多个来源，结论注明来源链接；明确区分「已证实的事实」与「你的推断」。"
    ),
    "writer": (
        "\nRole: 写作助手。根据任务产出结构清晰、可直接使用的成稿（文档/公告/README/报告等），"
        "先想清楚读者与用途再动笔；用任务的语言写作；只输出成稿本体与必要的简短说明，"
        "不要输出写作过程流水账。"
    ),
    "planner": (
        "\nRole: 规划师。先调研现状（读文件/搜索），再把任务拆成可执行的分步计划："
        "每步写清做什么、动哪些文件、怎么验证；标注步骤间的依赖与风险点。不改动任何文件。"
    ),
}


def build_subagent_registry(agent_type: str, extra_tools: list[Tool] | None = None) -> ToolRegistry:
    by_name = {t.name: t for t in (ReadFileTool(), ListDirTool(), GlobTool(), GrepTool())}
    tools: list[Tool] = [by_name[n] for n in RESEARCH_TOOL_NAMES]
    if extra_tools:
        wanted = EXTRA_TOOL_NAMES_BY_TYPE.get(agent_type, ())
        have = {t.name: t for t in extra_tools}
        tools.extend(have[n] for n in wanted if n in have)
    return ToolRegistry(tools)


class SubagentGate(PermissionGate):
    """只读自动放行；需确认的操作生成"已预拒绝"的 PendingPermission。

    预拒绝（future 已完成）让主循环的 `await pending.wait()` 立即返回 DENY，
    既不死锁，又能在事件流里留下"子代理尝试了被拒操作"的痕迹。
    deny_note 把话说死（子代理内永远没有确认通道），防止模型反复重试白烧轮数。
    """

    DENY_NOTE = (
        "子代理内无法执行需要确认的操作（写文件 / 跑命令等都会被自动拒绝）："
        "请不要重试。把计划写入的文件与内容、或要执行的命令写进最终报告，"
        "由主 Agent 处理。"
    )

    async def authorize(self, tool, input_dict):
        if tool.safety == Safety.READONLY:
            return None
        pending = await super().authorize(tool, input_dict)
        if pending is not None:
            pending.deny_note = self.DENY_NOTE
            pending.resolve(Decision.DENY)
        return pending


class IsolatedGate(PermissionGate):
    """隔离 worktree 任务门：只读放行；落在工作区内的写入自动放行。

    工作区是引擎为这个任务建的专用 git worktree（分支可整体丢弃），隔离本身
    就是「用户确认」的替代——所以工作目录内的写不需要逐次确认；配合工具层
    restrict_to_workdir，越出工作区的写入在路径解析时就被拒绝。
    命令执行等高危操作仍预拒绝：后台任务没有确认通道，且命令能越出工作区，
    隔离不能为它背书——验证步骤让任务写进报告，由主会话合并后执行。
    """

    DENY_NOTE = (
        "隔离子任务内无法执行需要确认的操作（跑命令等会被自动拒绝）："
        "工作区隔离只覆盖文件写入。请把要执行的命令与验证步骤写进最终报告，"
        "由主 Agent 合并后在主工作区执行。"
    )

    async def authorize(self, tool, input_dict):
        if tool.safety == Safety.READONLY:
            return None
        if tool.safety == Safety.WRITE and self._write_target_inside_workdir(tool, input_dict):
            return None
        pending = await super().authorize(tool, input_dict)
        if pending is not None:
            pending.deny_note = self.DENY_NOTE
            pending.resolve(Decision.DENY)
        return pending


class SubagentPlan:
    """一次子代理运行的完整输入。

    宿主（server backend）把「自定义子代理定义 / 内置子代理的定制」解析成它，
    TaskManager 拿到非 None 的计划就照此运行：registry=None 表示沿用
    agent_type 的内置工具集，system_extra 追加到子代理系统提示词之后。
    role_prompt=None 表示用该内置型的默认角色提示词；自定义子代理传 ""
    （它们有自己的专项指令，不套内置角色）。
    """

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry | None = None,
        system_extra: str = "",
        role_prompt: str | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.system_extra = system_extra
        self.role_prompt = role_prompt


async def run_subagent(
    *,
    provider: Provider,
    working_dir: Path | None,
    agent_type: str,
    prompt: str,
    max_iterations: int = 25,
    extra_tools: list[Tool] | None = None,
    registry: ToolRegistry | None = None,
    system_extra: str = "",
    role_prompt: str | None = None,
    restrict_to_workdir: bool = False,
    on_event: Callable[[object], Awaitable[None]] | None = None,
    gate: PermissionGate | None = None,
) -> tuple[str, Agent]:
    """运行一个子代理到结束，返回（最终报告文本, agent 实例）。

    registry 不传时按 agent_type 用内置只读工具集；宿主（设置页的自定义
    子代理）可以传入自己组装的注册表。system_extra 追加在子代理系统提示词
    之后，用来注入自定义子代理的描述与专项指令。role_prompt=None 时用该
    内置型的默认角色提示词（BUILTIN_ROLE_PROMPTS），传 "" 则不拼角色词——
    这是设置页「编辑内置子代理」覆盖默认提示词的通道。on_event 传入时每
    产生一个引擎事件就回调一次（宿主据此向前端直播子代理的工作过程）。
    gate 不传时用 SubagentGate（只读 + 预拒绝一切需确认操作）；隔离任务传
    IsolatedGate（工作区内写入放行）。
    """
    agent = Agent(
        provider=provider,
        registry=registry or build_subagent_registry(agent_type, extra_tools),
        gate=gate or SubagentGate(),
        working_dir=working_dir,
        max_iterations=max_iterations,
        restrict_to_workdir=restrict_to_workdir,
    )
    role = BUILTIN_ROLE_PROMPTS.get(agent_type, "") if role_prompt is None else role_prompt
    # task 型额外拿了写工具，但子代理内必被拒绝：提前说死，省模型白试一轮
    write_note = (
        "\nWrite-tools note: your toolset includes write tools, but inside a subagent "
        "they are ALWAYS auto-denied (no confirmation channel). Do not call them. "
        "If the task needs file changes, describe in your final report exactly what "
        "to write where, so the main agent can do it with the user's confirmation."
        if agent_type == "task" else ""
    )
    agent.set_system(
        SUBAGENT_PROMPT.format(agent_type=agent_type, workdir=str(working_dir))
        + role
        + write_note
        + (system_extra or "")
    )
    final_text = ""
    async for ev in agent.run_turn(prompt):
        if on_event is not None:
            try:
                await on_event(ev)
            except Exception:
                pass  # 直播失败不影响子代理本身
        if ev.kind == "assistant_message":
            content = ev.message.get("content", [])
            final_text = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
    if not final_text:
        final_text = "(subagent produced no text; tool results were: {})".format(
            "; ".join(m.to_plain()[:500] for m in agent.history[-3:])
        )
    return final_text, agent


class TaskRecord:
    def __init__(self, task_id: str, agent_type: str, prompt: str, session_id: str = "") -> None:
        self.id = task_id
        self.agent_type = agent_type
        self.prompt = prompt
        self.session_id = session_id  # 派生时的会话（用量记账归属）
        self.status = "queued"  # queued | running | done | error | cancelled
        self.result: str | None = None
        self.result_delivered = False  # check_task 已把完整报告投递进主上下文
        self.error: str | None = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cached = 0  # 命中提示词缓存的部分（含在 tokens_in 里）
        self.provider_name = ""
        self.provider_model = ""
        self.report_path = ""  # 长报告落盘位置（空 = 未落盘，整份内联投递）
        self.isolated = False  # 隔离派生：跑在专用 git worktree 里（写不碰主工作区）
        self.worktree_dir = ""  # 隔离工作区绝对路径（空 = 非隔离任务）
        self.branch = ""  # 隔离分支名（skysheep/task-<id>）
        self.commit_id = ""  # 完成时自动提交的短 id（空 = 未提交：任务未正常结束或提交失败）
        self.created_at = time.time()
        self.started_at = 0.0  # 从排队转入真正运行的时刻
        self.finished_at = 0.0
        self.done_event = asyncio.Event()  # 终态一次性置位：wait_task 靠它高效等待
        self.asyncio_task: object | None = None  # 后台任务句柄（cancel_all 真取消用）

    @property
    def duration_s(self) -> float:
        """运行耗时（秒）：排队不算；未结束的用已运行时间。"""
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)


class SubagentLimitError(Exception):
    """后台子代理并发达到上限。"""


# 转发给前端直播的事件种类：过程（工具调用）与产出（文本流/最终消息）。
# permission_request 不转发——子代理内已被预拒绝，弹确认框只会误导。
FORWARD_KINDS = frozenset(
    {"tool_call_started", "tool_call_finished", "text_delta", "assistant_message"}
)


class TaskManager:
    """后台子代理任务簿。

    plan_resolver / registry_resolver / provider_resolver 由宿主（server backend）
    注入，用来把「设置页里定义的自定义子代理」和「内置子代理的模型/思考强度
    覆盖」解析成一次真实运行；不注入时全部走内置默认（explore/task + 主 provider）。
    usage_recorder(session_id, provider, model, in_tok, out_tok, cached_tok) 在任务结束后记账；
    event_emitter(dict) 把直播事件广播给前端（同步回调，内部自行 create_task）。
    """

    def __init__(
        self,
        provider_factory: Callable[[], Provider],
        working_dir: Path | None,
        max_iterations: int = 25,
        store=None,
        provider_resolver: Callable[[str, str, str], Provider] | None = None,
        registry_resolver=None,
        max_concurrent: int = 3,
        usage_recorder: Callable[..., object] | None = None,
        event_emitter: Callable[[dict], None] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._provider_factory = provider_factory
        self._working_dir = working_dir
        self._max_iterations = max_iterations
        self._store = store
        self._provider_resolver = provider_resolver
        self._registry_resolver = registry_resolver
        self._max_concurrent = max(1, int(max_concurrent))
        self._usage_recorder = usage_recorder
        self._event_emitter = event_emitter
        self._state_path = state_path  # 任务簿持久化位置（None = 不持久化）
        self._extra: list[Tool] | None = None
        self._extra_research: list[Tool] | None = None
        self._tasks: dict[str, TaskRecord] = {}
        self._queue: list[TaskRecord] = []  # 并发满员时排队的后台任务（先进先出）
        self._turn_notes: dict[str, list[str]] = {}  # 会话 → 待注入下一轮的完成提示
        self._restrict_to_workdir = False
        self._load_state()

    def set_max_iterations(self, value: int) -> None:
        """设置页改了「子代理迭代轮数」后热更新，不用重启。"""
        self._max_iterations = max(1, min(100, int(value)))

    def set_max_concurrent(self, value: int) -> None:
        """设置页改了「并发上限」后热更新，不用重启。"""
        self._max_concurrent = max(1, min(8, int(value)))

    def set_restrict_to_workdir(self, value: bool) -> None:
        """主 Agent 的「仅允许访问工作目录」开关同样约束子代理。"""
        self._restrict_to_workdir = bool(value)

    # 会话归属说明：任务属于哪个会话在派生时随调用传入（spawn_agent 从
    # ctx.session_id 取），不存「当前会话」指针——两个会话并行跑轮时，
    # 单指针会被后开轮的会话覆盖，用量记账与归属校验都会串台。

    # ---- 自定义子代理的解析 ----

    def known_agent_type(self, agent_type: str) -> bool:
        """内置型（task/explore/reviewer/researcher/writer/planner），或启用中的自定义子代理名。"""
        if agent_type in BUILTIN_AGENT_TYPES:
            return True
        return self._store is not None and self._store.get_custom(agent_type, enabled_only=True) is not None

    def list_custom(self) -> list[tuple[str, str]]:
        """启用中的自定义子代理 [(名称, 描述)]，给 spawn_agent 的说明文字用。"""
        if self._store is None:
            return []
        return [(d.name, d.description) for d in self._store.custom if d.enabled]

    def list_builtin_desc(self) -> list[tuple[str, str]]:
        """内置子代理 [(显示名, 生效描述)]，给 spawn_agent 的说明文字用。

        描述被用户改过就用覆盖值，否则用内置默认。
        """
        out = []
        for t in BUILTIN_AGENT_TYPES:
            desc = ""
            if self._store is not None:
                ov = self._store.builtin.get(t)
                if ov is not None and ov.description.strip():
                    desc = ov.description.strip()
            if not desc:
                desc = BUILTIN_DESCRIPTIONS.get(t, "")
            out.append((t, desc))
        return out

    def _plan_for(self, agent_type: str):
        """把定义解析成一次运行所需的 (provider, registry, system_extra)。

        返回 None 表示走内置默认路径。
        """
        if self._store is None:
            return None
        d = self._store.get_custom(agent_type)
        if d is not None and d.enabled:
            parts = []
            if d.description.strip():
                parts.append("任务背景：" + d.description.strip())
            if d.prompt.strip():
                parts.append("专项指令：\n" + d.prompt.strip())
            return SubagentPlan(
                provider=self._provider_resolver(d.provider, d.model, d.reasoning),
                registry=self._registry_resolver(d.tools),
                system_extra="\n\n".join(parts),
            )
        ov = self._store.builtin.get(agent_type)
        if ov is not None and (
            ov.provider or ov.model or ov.reasoning
            or ov.description.strip() or ov.prompt.strip()
        ):
            # 只改了说明/指令、没指定模型时：沿用主对话默认 provider
            if (ov.provider or ov.model or ov.reasoning) and self._provider_resolver is not None:
                provider = self._provider_resolver(ov.provider, ov.model, ov.reasoning)
            else:
                provider = self._provider_factory()
            return SubagentPlan(
                provider=provider,
                registry=None,
                system_extra="",
                role_prompt=(
                    ov.prompt.strip() if ov.prompt.strip()
                    else BUILTIN_ROLE_PROMPTS.get(agent_type, "")
                ),
            )
        return None

    def _builtin_extra_tools(self, agent_type: str) -> list[Tool] | None:
        """内置型在只读基础集之上可尝试的额外工具（写入动作仍会被 SubagentGate 拒）。"""
        if agent_type == "task":
            if self._extra is None:
                from ..tools import EditFileTool, WriteDocumentTool, WriteFileTool

                self._extra = [WriteFileTool(), EditFileTool(), WriteDocumentTool()]
            return self._extra
        if agent_type == "researcher":
            if self._extra_research is None:
                from ..tools import WebFetchTool, WebSearchTool

                self._extra_research = [WebSearchTool(), WebFetchTool()]
            return self._extra_research
        return None

    def _new_record(self, agent_type: str, prompt: str, session_id: str = "") -> TaskRecord:
        task_id = uuid.uuid4().hex[:10]
        rec = TaskRecord(task_id, agent_type, prompt, session_id=session_id)
        self._tasks[task_id] = rec
        return rec

    def _running_count(self) -> int:
        return sum(1 for r in self._tasks.values() if r.status == "running")

    def _check_concurrency(self) -> None:
        """同步派生的并发护栏（后台任务满员会排队，不在这里挡）。"""
        if self._running_count() >= self._max_concurrent:
            raise SubagentLimitError(
                f"子代理并发已达上限（{self._max_concurrent} 个）："
                "后台任务会自动排队，同步派生请等运行中的任务完成"
            )

    def announce(self, rec: TaskRecord, background: bool) -> None:
        """广播 subagent_spawned：前端靠它把直播卡片和 task_id 精确绑定
        （不靠「第一个到达的事件」，避免后台任务的事件抢先串台）。"""
        if self._event_emitter is None:
            return
        try:
            self._event_emitter({
                "kind": "subagent_spawned",
                "task_id": rec.id,
                "session_id": rec.session_id,
                "agent_type": rec.agent_type,
                "background": background,
                "queued": rec.status == "queued",
                "prompt": rec.prompt[:60],
            })
        except Exception:
            pass  # 直播失败不影响子代理本身

    def _make_forwarder(self, rec: TaskRecord):
        """把子代理事件包装成 subagent_event 直播事件（过滤噪音种类）。

        事件带 session_id（安全审查 B15）：服务层据此只推给正在看该会话的
        远程客户端，不让一个会话的子代理过程泄进另一个客户端。
        """
        emitter = self._event_emitter

        async def forward(ev) -> None:
            if emitter is None or ev.kind not in FORWARD_KINDS:
                return
            try:
                emitter({
                    "kind": "subagent_event",
                    "task_id": rec.id,
                    "session_id": rec.session_id,
                    "agent_type": rec.agent_type,
                    "event": ev.model_dump(),
                })
            except Exception:
                pass  # 直播失败不影响子代理本身

        return forward

    async def _record_usage(self, rec: TaskRecord) -> None:
        if self._usage_recorder is None or (rec.tokens_in <= 0 and rec.tokens_out <= 0):
            return
        try:
            await self._usage_recorder(
                rec.session_id, rec.provider_name, rec.provider_model,
                rec.tokens_in, rec.tokens_out, rec.tokens_cached,
            )
        except Exception:
            pass  # 记账失败不拖垮任务本身

    async def run_sync(self, agent_type: str, prompt: str, session_id: str = "") -> str:
        self._check_concurrency()
        rec = self._new_record(agent_type, prompt, session_id=session_id)
        rec.status = "running"
        rec.started_at = time.time()
        self.announce(rec, background=False)
        try:
            result, _ = await self._run_one(rec)
            rec.result = result
            rec.status = "done"
            self._on_terminal(rec)
            return self.result_payload(rec)
        except asyncio.CancelledError:
            # 主轮被「停止」取消：CancelledError 不是 Exception，不落终态的话
            # 记录会永远停在 running——占着并发名额直到重启（僵尸任务）
            rec.status = "cancelled"
            rec.error = "用户取消"
            self._on_terminal(rec)
            raise
        except Exception as e:
            rec.status = "error"
            rec.error = str(e)
            self._on_terminal(rec)
            raise

    def can_isolate(self) -> bool:
        """隔离派生的前提：有工作目录且是 git 仓库（派生时即校验，fail fast）。"""
        from .worktree import is_git_repo

        return is_git_repo(self._working_dir)

    def start_background(
        self, agent_type: str, prompt: str, session_id: str = "", isolated: bool = False,
    ) -> str:
        rec = self._new_record(agent_type, prompt, session_id=session_id)
        if isolated:
            if self._working_dir is None:
                raise SubagentLimitError("没有工作目录，无法隔离派生")
            rec.isolated = True
            rec.worktree_dir = str(
                self._working_dir / ".skysheep" / "worktrees" / rec.id
            )
            rec.branch = f"skysheep/task-{rec.id}"
        if self._running_count() >= self._max_concurrent:
            # 并发满员不再直接报错：收进队列，空位出来按序自动开跑
            if len(self._queue) >= TASK_QUEUE_KEEP:
                del self._tasks[rec.id]
                raise SubagentLimitError(
                    f"后台排队也已满（{TASK_QUEUE_KEEP} 个），请等运行中的任务完成再派"
                )
            rec.status = "queued"
            self._queue.append(rec)
            self.announce(rec, background=True)
            self._persist()
            return rec.id
        self.announce(rec, background=True)
        self._launch(rec)
        return rec.id

    def _launch(self, rec: TaskRecord) -> None:
        rec.status = "running"
        rec.started_at = time.time()
        task = asyncio_create(self._run_background(rec))
        rec.asyncio_task = task
        # 任务还没来得及开跑就被取消时，协程收尾不会执行——在这里兜底落终态
        task.add_done_callback(lambda t: self._finalize_if_unfinished(rec, t))

    def _promote_queued(self) -> None:
        """并发有空位时把排队任务按序拉起（终态补位时调用）。"""
        while self._queue and self._running_count() < self._max_concurrent:
            rec = self._queue.pop(0)
            if rec.status != "queued":
                continue  # 排队期间被取消/清理过
            self._launch(rec)

    def _on_terminal(self, rec: TaskRecord) -> None:
        """所有终态的唯一出口：计时、唤醒等待者、落盘、广播、队列补位。"""
        if not rec.finished_at:
            rec.finished_at = time.time()
        rec.done_event.set()
        self._persist()
        self._notify_finished(rec)
        self._promote_queued()

    def _finalize_if_unfinished(self, rec: TaskRecord, task) -> None:
        if rec.status == "running" and task.cancelled():
            rec.status = "cancelled"
            rec.error = "用户取消"
            self._on_terminal(rec)

    def _note_for_next_turn(self, rec: TaskRecord) -> None:
        """后台任务做完且报告没人来取 → 记一条提示，宿主在下一轮开始时注入，
        主 Agent 就知道「有任务做完了」，不用用户来催。"""
        if rec.status not in ("done", "error") or rec.result_delivered:
            return
        bucket = self._turn_notes.setdefault(rec.session_id, [])
        bucket.append(rec.id)
        del bucket[:-10]  # 一轮最多提醒 10 条，防积压

    def pop_turn_note(self, session_id: str) -> str:
        """取走该会话累积的「后台任务完成」提示（每轮至多注入一次）。

        注入时再核对一遍状态：报告已被 check_task 取走的任务不再提示。"""
        ids = self._turn_notes.pop(session_id or "", None)
        if not ids:
            return ""
        lines = []
        for tid in ids:
            r = self._tasks.get(tid)
            if r is None or r.result_delivered or r.status not in ("done", "error"):
                continue
            label = "已完成" if r.status == "done" else f"失败（{r.error}）"
            lines.append(
                f"后台子代理任务 {r.id}（{r.agent_type}）{label}，"
                f"用 check_task(task_id=\"{r.id}\") 获取报告。"
            )
        if not lines:
            return ""
        return "（系统提示：以下是自上一轮以来结束的后台子代理任务）\n" + "\n".join(lines) + "\n\n"

    # ---- 任务簿持久化（重启后仍可查询；running/queued 重启即标中断） ----

    _PERSIST_FIELDS = (
        "id", "agent_type", "session_id", "prompt", "status", "result", "error",
        "tokens_in", "tokens_out", "tokens_cached", "provider_name", "provider_model",
        "report_path", "created_at", "started_at", "finished_at",
        "isolated", "worktree_dir", "branch", "commit_id",
    )

    def _persist(self) -> None:
        if self._state_path is None:
            return
        recs = sorted(self._tasks.values(), key=lambda r: r.created_at)[-TASK_BOOK_KEEP:]
        data = {"tasks": [
            {f: getattr(r, f) for f in self._PERSIST_FIELDS} for r in recs
        ]}
        try:
            write_text_atomic(
                self._state_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            )
        except OSError:
            pass  # 持久化失败不拖垮任务本身

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in (data or {}).get("tasks") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            rec = TaskRecord(
                str(item["id"]), str(item.get("agent_type") or ""), str(item.get("prompt") or "")
            )
            rec.session_id = str(item.get("session_id") or "")
            for f in self._PERSIST_FIELDS:
                if f in item:
                    setattr(rec, f, item[f])
            if rec.status in ("running", "queued"):
                # 上次进程退出时还没跑完：任务已随进程消失，标成中断而不是假运行
                rec.status = "error"
                rec.error = "应用重启，任务中断"
                if not rec.finished_at:
                    rec.finished_at = time.time()
            rec.done_event.set()
            self._tasks[rec.id] = rec

    async def _run_one(self, rec: TaskRecord) -> tuple[str, Agent]:
        """按定义跑一次：有解析结果（自定义/覆盖）就照计划跑，否则走内置默认。

        结束后把 token 用量记到任务上并交给宿主记账（含被取消时的已耗部分）。
        隔离任务先建专用 worktree（工作目录指向它、restrict_to_workdir 硬边界、
        IsolatedGate 门控），成功收尾时把改动自动提交到隔离分支——提交失败
        不判任务失败，改动都在工作区里。
        """
        workdir = self._working_dir
        if rec.isolated:
            try:
                await asyncio.to_thread(
                    ensure_worktree, self._working_dir, Path(rec.worktree_dir), rec.branch,
                )
            except Exception as e:
                raise RuntimeError(f"隔离工作区创建失败：{e}") from e
            workdir = Path(rec.worktree_dir)
        plan = None
        try:
            plan = self._plan_for(rec.agent_type)
        except Exception as e:  # 定义解析失败（如模型没配 Key）→ 记为任务错误
            raise RuntimeError(f"子代理「{rec.agent_type}」启动失败：{e}") from e
        if plan is not None:
            provider = plan.provider
        else:
            provider = self._provider_factory()
        rec.provider_name = getattr(provider, "name", "") or ""
        rec.provider_model = getattr(provider, "model", "") or ""
        extra = None if plan is not None else self._builtin_extra_tools(rec.agent_type)

        agent: Agent | None = None
        try:
            result, agent = await run_subagent(
                provider=provider,
                working_dir=workdir,
                agent_type=rec.agent_type,
                prompt=rec.prompt,
                max_iterations=self._max_iterations,
                registry=plan.registry if plan is not None else None,
                system_extra=plan.system_extra if plan is not None else "",
                role_prompt=plan.role_prompt if plan is not None else None,
                extra_tools=extra,
                restrict_to_workdir=True if rec.isolated else self._restrict_to_workdir,
                on_event=self._make_forwarder(rec),
                gate=IsolatedGate(working_dir=workdir) if rec.isolated else None,
            )
            rec.tokens_in = agent.total_in_tokens
            rec.tokens_out = agent.total_out_tokens
            rec.tokens_cached = agent.total_cached_tokens
            await self._record_usage(rec)
            if rec.isolated and workdir is not None:
                try:
                    rec.commit_id = await asyncio.to_thread(
                        commit_all, workdir,
                        f"SkySheep 隔离任务 {rec.id}（{rec.agent_type}）：{rec.prompt[:60]}",
                    )
                except Exception:
                    rec.commit_id = ""  # 提交失败不拖垮任务：改动留在工作区可手动处理
            self._maybe_write_report(rec, result)
            return result, agent
        except asyncio.CancelledError:
            # 被取消：已消耗的 token 也要入账，然后原样上抛
            if agent is not None:
                rec.tokens_in = agent.total_in_tokens
                rec.tokens_out = agent.total_out_tokens
                rec.tokens_cached = agent.total_cached_tokens
                await self._record_usage(rec)
            raise

    def _maybe_write_report(self, rec: TaskRecord, result: str) -> None:
        """长报告落盘到 <workdir>/.skysheep/reports/：投递时只给路径 + 摘录，
        几千 token 的报告不再整份挤进主上下文（引擎直接写，不过权限门）。"""
        if len(result) <= REPORT_INLINE_LIMIT or self._working_dir is None:
            return
        try:
            rdir = self._working_dir / ".skysheep" / "reports"
            rdir.mkdir(parents=True, exist_ok=True)
            path = rdir / f"{rec.id}-{rec.agent_type}.md"
            path.write_text(result, encoding="utf-8")
            rec.report_path = str(path)
        except OSError:
            rec.report_path = ""  # 落盘失败退回整份内联投递

    def _isolated_note(self, rec: TaskRecord) -> str:
        """隔离任务的收尾说明：分支 / 提交 / 工作区位置与合并、清理指引。"""
        if rec.status == "cancelled":
            # 取消后 worktree 已被清理（见 _cleanup_isolated）：说明里给分支
            # 的去向，别再指向一个已经不存在的目录
            if rec.commit_id:
                return (
                    f"（隔离任务已取消：取消前的部分改动已提交到分支 {rec.branch}"
                    f"（commit {rec.commit_id}），专用工作区已清理。"
                    f"查看/合并：git log {rec.branch}；不要就 git branch -D {rec.branch}。）"
                )
            return (
                f"（隔离任务已取消：没有可保留的改动，专用工作区已清理"
                f"（分支 {rec.branch} 无新提交，不需要时 git branch -D {rec.branch}）。）"
            )
        if rec.commit_id:
            return (
                f"（隔离任务：改动已提交到分支 {rec.branch}（commit {rec.commit_id}），"
                f"主工作区未被触碰。审查后合并：git merge {rec.branch}；"
                f"合并后清理：git worktree remove {rec.worktree_dir} 并 "
                f"git branch -d {rec.branch}。）"
            )
        return (
            f"（隔离任务：改动未自动提交，保留在独立工作区 {rec.worktree_dir}"
            f"（分支 {rec.branch}）。可手动检查后提交/合并，不需要时直接删除该目录。）"
        )

    def result_payload(self, rec: TaskRecord) -> str:
        """把结果投递进主上下文的形态：长报告 = 路径 + 开头摘录；隔离任务附合并指引。"""
        text = rec.result or ""
        if rec.report_path and len(text) > REPORT_INLINE_LIMIT:
            text = (
                f"（报告全文 {len(text)} 字符已写入 {rec.report_path}，"
                "需要全文用 read_file 读取；以下为开头摘录）\n"
                + text[:REPORT_EXCERPT_CHARS]
            )
        if rec.isolated:
            text += "\n\n" + self._isolated_note(rec)
        return text

    async def _cleanup_isolated(self, rec: TaskRecord) -> None:
        """取消隔离任务时收拾 worktree（安全审查 M15）。

        先尽力把取消前的部分改动提交到隔离分支——取消不等于要把人做的活扔掉；
        再移除 worktree 目录与注册（否则主仓 git worktree list 一直挂着它，
        要手动 prune）。分支保留，便于事后查看/合并，或由用户自行删除。
        全程 best-effort：清理失败不该影响取消收尾。
        """
        if not rec.isolated or not rec.worktree_dir or self._working_dir is None:
            return
        wt = Path(rec.worktree_dir)
        try:
            rec.commit_id = await asyncio.to_thread(
                commit_all, wt,
                f"SkySheep 隔离任务 {rec.id}（{rec.agent_type}）：取消时保留的部分改动",
            )
        except Exception:  # noqa: BLE001
            rec.commit_id = ""
        try:
            await asyncio.to_thread(
                remove_worktree, self._working_dir, wt, rec.branch,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _run_background(self, rec: TaskRecord) -> None:
        try:
            result, _ = await self._run_one(rec)
            rec.result = result
            rec.status = "done"
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.error = "用户取消"
            await self._cleanup_isolated(rec)
        except Exception as e:
            rec.status = "error"
            rec.error = str(e)
        self._note_for_next_turn(rec)
        self._on_terminal(rec)

    def _notify_finished(self, rec: TaskRecord) -> None:
        """后台任务终态广播给前端（无连接/无循环时静默）。"""
        if self._event_emitter is None:
            return
        try:
            self._event_emitter({
                "kind": "task_finished",
                "task_id": rec.id,
                "session_id": rec.session_id,
                "agent_type": rec.agent_type,
                "status": rec.status,
                "prompt": rec.prompt[:60],
            })
        except Exception:
            pass

    def list_tasks(self, limit: int = 30, session_id: str | None = None) -> list[dict]:
        """任务簿快照：running/queued 在前，其余按创建序倒序（新任务先看到）。

        session_id 给出时只返回该会话的任务（安全审查 B13：远程客户端只能
        看到自己正在交互的会话，不能枚举其他会话的 prompt/result）；
        None = 不过滤（本机任务簿保持全局视图）。
        """
        recs = [
            r for r in self._tasks.values()
            if session_id is None or r.session_id == session_id
        ]
        recs.sort(key=lambda r: (r.status not in ("running", "queued"),))
        out = []
        for r in recs[-limit:][::-1]:
            out.append({
                "id": r.id,
                "agent_type": r.agent_type,
                "session_id": r.session_id,
                "prompt": r.prompt[:160],
                "status": r.status,
                "result": (r.result or "")[:800],
                "error": (r.error or "")[:300],
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "created_at": r.created_at,
                "duration_s": round(r.duration_s, 1),
                "report_path": r.report_path,
            })
        return out

    def get_detail(self, task_id: str, session_id: str | None = None) -> dict | None:
        """单个任务的完整信息（不截断），任务簿详情弹窗用。

        session_id 给出时只允许查该会话的任务（归属校验，同 list_tasks）。
        """
        r = self._tasks.get(task_id)
        if r is None:
            return None
        if session_id is not None and r.session_id != session_id:
            return None
        return {
            "id": r.id,
            "agent_type": r.agent_type,
            "session_id": r.session_id,
            "prompt": r.prompt,
            "status": r.status,
            "result": r.result or "",
            "error": r.error or "",
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "provider": " / ".join(x for x in (r.provider_name, r.provider_model) if x),
            "created_at": r.created_at,
            "finished_at": r.finished_at,
            "duration_s": round(r.duration_s, 1),
            "report_path": r.report_path,
        }

    def status(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def accessible(self, task_id: str, session_id: str) -> TaskRecord | None:
        """工具侧的归属校验：任务存在、且属于当前正在跑的会话（B13 同款规则）。

        任一方会话为空（旧记录 / 无会话态）时放行，保持向后兼容。
        """
        rec = self._tasks.get(task_id)
        if rec is None:
            return None
        if rec.session_id and session_id and rec.session_id != session_id:
            return None
        return rec

    def cancel_task(self, task_id: str, session_id: str | None = None) -> bool:
        """取消单个任务：排队的直接落终态，运行中的真取消（同 cancel_all 语义）。"""
        rec = self._tasks.get(task_id)
        if rec is None or rec.status in ("done", "error", "cancelled"):
            return False
        if session_id is not None and rec.session_id != session_id:
            return False
        if rec.status == "queued":
            self._queue = [r for r in self._queue if r.id != rec.id]
            rec.status = "cancelled"
            rec.error = "用户取消"
            self._on_terminal(rec)
            return True
        t = rec.asyncio_task
        if t is not None and not t.done():
            t.cancel()
        else:
            # 没有句柄（如同步任务）：只能标状态
            rec.status = "cancelled"
            rec.error = "用户取消"
            self._on_terminal(rec)
        return True

    def cancel_all(self, session_id: str | None = None) -> None:
        """真取消：后台任务拿到 CancelledError 后收尾（记账 + 广播终态）。

        同步任务跑在调用方（主轮）的任务里，随聊天框「停止」自然中断，这里不碰。
        session_id 给出时只取消该会话派生的任务（远程客户端的隔离，B13）。
        """
        for rec in list(self._tasks.values()):
            if rec.status == "queued":
                if session_id is None or rec.session_id == session_id:
                    self._queue = [r for r in self._queue if r.id != rec.id]
                    rec.status = "cancelled"
                    rec.error = "用户取消"
                    self._on_terminal(rec)
                continue
            if rec.status != "running":
                continue
            if session_id is not None and rec.session_id != session_id:
                continue
            t = rec.asyncio_task
            if t is not None and not t.done():
                t.cancel()  # _run_background 收尾：已耗 token 入账 + 标 cancelled + 广播
            else:
                # 没有句柄（如同步任务）：只能标状态
                rec.status = "cancelled"
                rec.error = "用户取消"
                self._on_terminal(rec)


def asyncio_create(coro: Awaitable) -> object:
    return asyncio.get_running_loop().create_task(coro)


# ---- 给主 Agent 用的工具 ----


class SpawnAgentArgs(BaseModel):
    agent_type: str = Field(
        default="explore",
        description="内置型：explore=只读调研（推荐）；task=通用多步任务（写入会被自动拒绝，"
        "把写入方案写进报告）；reviewer=代码/文档审查；researcher=联网调研（带搜索/网页工具）；"
        "writer=文档/报告成稿写作；planner=拆解任务出分步计划。"
        "子代理内的写/命令操作会被自动拒绝（无确认通道）",
    )
    prompt: str = Field(description="给子代理的完整任务说明，要自包含（子代理看不到当前对话）")
    background: bool = Field(
        default=False,
        description="true=后台运行立即返回 task_id（并发满员时自动排队），"
        "用 check_task 查询或 wait_task 等待",
    )
    isolated: bool = Field(
        default=False,
        description=(
            "true=在专用 git worktree 里隔离运行（需 background=true，且项目是 git 仓库）："
            "工作目录是独立工作区+独立分支，写入不碰主工作区，完成时自动提交到该分支，"
            "报告里给合并指引。适合会改文件的并行子任务（配合 task 型）；"
            "普通项目目录不受隔离保护时才需要它"
        ),
    )


class SpawnAgentTool(Tool):
    name = "spawn_agent"
    safety = Safety.READONLY
    # 派生的子代理会真实执行任务，对外按「有写动作」如实标注
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = SpawnAgentArgs

    def __init__(self, tasks: TaskManager) -> None:
        self._tasks = tasks
        self.description = (
            "派生一个子代理去完成独立子任务（如：调研代码结构、在多个文件里搜集信息）。"
            "子代理看不到主对话，所以 prompt 必须自包含。"
            "适合耗时的探索性工作，避免污染主上下文。"
            "多个会改文件的并行任务用 isolated=true：各自在独立 git 分支/工作区里干活，"
            "互不覆盖，完成自动提交。"
            "\n内置子代理："
            + "；".join(f"{name}（{desc}）" if desc else name for name, desc in tasks.list_builtin_desc())
        )
        custom = tasks.list_custom()
        if custom:
            self.description += "\n可用的自定义子代理：" + "；".join(
                f"{name}（{desc}）" if desc else name for name, desc in custom
            )

    async def run(self, args: SpawnAgentArgs, ctx: ToolContext) -> str:
        from ..tools.base import ToolError

        if not self._tasks.known_agent_type(args.agent_type):
            available = list(BUILTIN_AGENT_TYPES) + [n for n, _ in self._tasks.list_custom()]
            raise ToolError(
                f"未知的子代理类型「{args.agent_type}」，可用：{('、'.join(available))}"
            )
        if args.isolated:
            if not args.background:
                raise ToolError(
                    "隔离派生只支持 background=true：隔离的价值在并行，"
                    "同步派生会占住主轮，没有隔离意义"
                )
            if not self._tasks.can_isolate():
                raise ToolError(
                    "当前项目不是 git 仓库（未找到 .git），无法隔离派生；"
                    "请改用普通派生（isolated 不传或为 false）"
                )
        try:
            if args.background:
                task_id = self._tasks.start_background(
                    args.agent_type, args.prompt,
                    session_id=ctx.session_id, isolated=args.isolated,
                )
                rec = self._tasks.status(task_id)
                queued = rec is not None and rec.status == "queued"
                extra = "（并发已满，正在排队，开始运行后无需重新派发）" if queued else ""
                iso = "（隔离工作区 + 分支 " + rec.branch + "）" if rec is not None and rec.isolated else ""
                return (
                    f"background subagent started, task_id={task_id}; "
                    f"check_task to poll, wait_task to block until done{extra}{iso}"
                )
            result = await self._tasks.run_sync(
                args.agent_type, args.prompt, session_id=ctx.session_id
            )
        except SubagentLimitError as e:
            raise ToolError(str(e)) from e
        return result


class CheckTaskArgs(BaseModel):
    task_id: str = Field(description="spawn_agent(background=true) 返回的 task_id")


def _format_task_report(tasks: TaskManager, rec: TaskRecord) -> str:
    """check_task / wait_task 共用的结果渲染（含已投递去重与长报告摘要）。"""
    lines = [f"task_id: {rec.id}", f"type: {rec.agent_type}", f"status: {rec.status}"]
    if rec.worktree_dir:
        state = f"commit {rec.commit_id}" if rec.commit_id else "改动未提交"
        lines.append(f"隔离工作区: {rec.worktree_dir}（分支 {rec.branch}，{state}）")
    if rec.status == "done" and rec.result is not None:
        if rec.result_delivered:
            # 长报告会反复挤占上下文；模型「稍后再查」的惯性会让同一份
            # 报告被重复注入——已投递就只回显开头防丢线索
            lines.append(
                "（完整结果已在此前的查询里投递进上下文，不再重复。开头回显："
                + rec.result[:200] + "）"
            )
        else:
            lines.append("--- result ---\n" + tasks.result_payload(rec))
            rec.result_delivered = True
    if rec.error:
        lines.append("--- error ---\n" + rec.error)
    return "\n".join(lines)


class CheckTaskTool(Tool):
    name = "check_task"
    description = "查询后台子代理任务的状态与结果。status=running 时可稍后再查，或改用 wait_task 等它做完。"
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = CheckTaskArgs

    def __init__(self, tasks: TaskManager) -> None:
        self._tasks = tasks

    async def run(self, args: CheckTaskArgs, ctx: ToolContext) -> str:
        from ..tools.base import ToolError

        # 归属校验（与任务簿 B13 同款）：别的会话派生的任务不能跨会话取报告。
        # 校验基准是 ctx.session_id（本次调用所属会话），不依赖任何全局指针。
        rec = self._tasks.accessible(args.task_id, ctx.session_id)
        if rec is None:
            raise ToolError("unknown task_id: " + args.task_id)
        return _format_task_report(self._tasks, rec)


class WaitTaskArgs(BaseModel):
    task_id: str = Field(description="spawn_agent(background=true) 返回的 task_id")
    timeout_seconds: int = Field(
        default=60, ge=1, le=300,
        description="最长等待秒数（1-300）。到点还没结束就返回当前状态，可再次调用",
    )


class WaitTaskTool(Tool):
    name = "wait_task"
    description = (
        "等待后台子代理任务结束（带超时）：任务完成/失败/取消时立即返回状态与结果，"
        "超时则返回当前进度。比反复 check_task 轮询省轮次——一次调用顶多次查询。"
    )
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = False  # 会阻塞到终态或超时，不是幂等查询
    open_world_hint = False
    args_model = WaitTaskArgs

    def __init__(self, tasks: TaskManager) -> None:
        self._tasks = tasks

    async def run(self, args: WaitTaskArgs, ctx: ToolContext) -> str:
        from ..tools.base import ToolError

        rec = self._tasks.accessible(args.task_id, ctx.session_id)
        if rec is None:
            raise ToolError("unknown task_id: " + args.task_id)
        if rec.status in ("queued", "running"):
            try:
                await asyncio.wait_for(rec.done_event.wait(), timeout=max(1, args.timeout_seconds))
            except TimeoutError:
                lines = [f"task_id: {rec.id}", f"type: {rec.agent_type}",
                         f"status: {rec.status}",
                         f"（等待 {args.timeout_seconds}s 超时，任务仍在 {rec.status}；可再次调用继续等）"]
                if rec.status == "running":
                    lines.append(f"已运行 {rec.duration_s:.0f}s / 轮数上限 {self._tasks._max_iterations}")
                return "\n".join(lines)
        return _format_task_report(self._tasks, rec)
