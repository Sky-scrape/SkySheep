"""子代理：主 Agent 可派生的独立上下文 Agent。

安全模型（M2）：
- 子代理只做只读研究（read/list/glob/grep/load_skill）；
- 写工具对 task 型子代理开放，但 SubagentGate 会自动拒绝所有需确认的操作——
  因为子代理运行期间主循环被占住，无法弹确认（会死锁）；
  需要写文件/跑命令时子代理应在报告中说明，由主 Agent 请求用户确认后执行；
- 子代理工具集里没有 spawn_agent，禁止递归派生。

两种运行方式：
- 同步（explore 默认）：spawn_agent 阻塞到完成，报告作为 tool_result 返回；
- 后台（background=true）：立即返回 task_id，主 Agent 用 check_task 轮询。

宿主可注入三个钩子：
- usage_recorder：任务结束后把 token 用量记入 usage_log（子代理是独立 Agent
  实例，不记账就绕过了用量页与每日预算护栏）；
- event_emitter：把子代理的事件流（过滤后）转发给前端直播；
- max_concurrent：后台任务并发上限，防止模型一口气派出大量任务烧钱。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field

from ..models.base import Provider
from ..security.gate import Decision, PermissionGate
from ..tools import GlobTool, GrepTool, ListDirTool, ReadFileTool, Safety, Tool, ToolRegistry
from ..tools.base import ToolContext
from .agent import Agent
from .prompt import SUBAGENT_PROMPT

# 只读研究型工具集（explore / task 共用基础）
RESEARCH_TOOL_NAMES = ("read_file", "list_dir", "glob", "grep")


def build_subagent_registry(agent_type: str, extra_tools: list[Tool] | None = None) -> ToolRegistry:
    by_name = {t.name: t for t in (ReadFileTool(), ListDirTool(), GlobTool(), GrepTool())}
    tools: list[Tool] = [by_name[n] for n in RESEARCH_TOOL_NAMES]
    if agent_type == "task" and extra_tools:
        # task 型可写文件/生成文档（但写操作会被 SubagentGate 自动拒绝，见模块 docstring）
        for t in extra_tools:
            if t.safety == Safety.READONLY or t.name in ("write_file", "edit_file", "write_document"):
                tools.append(t)
    return ToolRegistry(tools)


class SubagentGate(PermissionGate):
    """只读自动放行；需确认的操作生成"已预拒绝"的 PendingPermission。

    预拒绝（future 已完成）让主循环的 `await pending.wait()` 立即返回 DENY，
    既不死锁，又能在事件流里留下"子代理尝试了被拒操作"的痕迹。
    """

    async def authorize(self, tool, input_dict):
        if tool.safety == Safety.READONLY:
            return None
        pending = await super().authorize(tool, input_dict)
        if pending is not None:
            pending.resolve(Decision.DENY)
        return pending


class SubagentPlan:
    """一次子代理运行的完整输入。

    宿主（server backend）把「自定义子代理定义 / 内置覆盖」解析成它，
    TaskManager 拿到非 None 的计划就照此运行：registry=None 表示沿用
    agent_type 的内置工具集，system_extra 追加到子代理系统提示词之后。
    """

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry | None = None,
        system_extra: str = "",
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.system_extra = system_extra


async def run_subagent(
    *,
    provider: Provider,
    working_dir: Path,
    agent_type: str,
    prompt: str,
    max_iterations: int = 25,
    extra_tools: list[Tool] | None = None,
    registry: ToolRegistry | None = None,
    system_extra: str = "",
    restrict_to_workdir: bool = False,
    on_event: Callable[[object], Awaitable[None]] | None = None,
) -> tuple[str, Agent]:
    """运行一个子代理到结束，返回（最终报告文本, agent 实例）。

    registry 不传时按 agent_type 用内置只读工具集；宿主（设置页的自定义
    子代理）可以传入自己组装的注册表。system_extra 追加在子代理系统提示词
    之后，用来注入自定义子代理的描述与专项指令。on_event 传入时每产生一个
    引擎事件就回调一次（宿主据此向前端直播子代理的工作过程）。
    """
    agent = Agent(
        provider=provider,
        registry=registry or build_subagent_registry(agent_type, extra_tools),
        gate=SubagentGate(),
        working_dir=working_dir,
        max_iterations=max_iterations,
        restrict_to_workdir=restrict_to_workdir,
    )
    agent.set_system(
        SUBAGENT_PROMPT.format(agent_type=agent_type, workdir=str(working_dir))
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
        self.status = "running"  # running | done | error | cancelled
        self.result: str | None = None
        self.result_delivered = False  # check_task 已把完整报告投递进主上下文
        self.error: str | None = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.provider_name = ""
        self.provider_model = ""
        self.asyncio_task: object | None = None  # 后台任务句柄（cancel_all 真取消用）


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
    usage_recorder(session_id, provider, model, in_tok, out_tok) 在任务结束后记账；
    event_emitter(dict) 把直播事件广播给前端（同步回调，内部自行 create_task）。
    """

    def __init__(
        self,
        provider_factory: Callable[[], Provider],
        working_dir: Path,
        max_iterations: int = 25,
        store=None,
        provider_resolver: Callable[[str, str, str], Provider] | None = None,
        registry_resolver=None,
        max_concurrent: int = 3,
        usage_recorder: Callable[..., object] | None = None,
        event_emitter: Callable[[dict], None] | None = None,
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
        self._extra: list[Tool] | None = None
        self._tasks: dict[str, TaskRecord] = {}
        self._restrict_to_workdir = False
        self._active_sid = ""  # 当前正在跑的轮次所属会话（派生时归属用量）

    def set_max_iterations(self, value: int) -> None:
        """设置页改了「子代理迭代轮数」后热更新，不用重启。"""
        self._max_iterations = max(1, min(100, int(value)))

    def set_restrict_to_workdir(self, value: bool) -> None:
        """主 Agent 的「仅允许访问工作目录」开关同样约束子代理。"""
        self._restrict_to_workdir = bool(value)

    def set_active_session(self, session_id: str) -> None:
        """轮次开始/结束时由宿主调用：派生出的任务把用量记到这个会话名下。"""
        self._active_sid = session_id or ""

    # ---- 自定义子代理的解析 ----

    def known_agent_type(self, agent_type: str) -> bool:
        """内置 explore/task，或启用中的自定义子代理名。"""
        if agent_type in ("explore", "task"):
            return True
        return self._store is not None and self._store.get_custom(agent_type, enabled_only=True) is not None

    def list_custom(self) -> list[tuple[str, str]]:
        """启用中的自定义子代理 [(名称, 描述)]，给 spawn_agent 的说明文字用。"""
        if self._store is None:
            return []
        return [(d.name, d.description) for d in self._store.custom if d.enabled]

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
                provider=self._provider_resolver(d.provider, d.model, ""),
                registry=self._registry_resolver(d.tools),
                system_extra="\n\n".join(parts),
            )
        ov = self._store.builtin.get(agent_type)
        if ov is not None and (ov.provider or ov.model or ov.reasoning):
            return SubagentPlan(
                provider=self._provider_resolver(ov.provider, ov.model, ov.reasoning),
                registry=None,
                system_extra="",
            )
        return None

    def _extra_tools_for_task(self) -> list[Tool]:
        """task 型子代理可尝试写文件/生成文档（写入动作会被 SubagentGate 自动拒绝）。"""
        if self._extra is None:
            from ..tools import EditFileTool, WriteDocumentTool, WriteFileTool

            self._extra = [WriteFileTool(), EditFileTool(), WriteDocumentTool()]
        return self._extra

    def _new_record(self, agent_type: str, prompt: str) -> TaskRecord:
        task_id = uuid.uuid4().hex[:10]
        rec = TaskRecord(task_id, agent_type, prompt, session_id=self._active_sid)
        self._tasks[task_id] = rec
        return rec

    def _check_concurrency(self) -> None:
        running = sum(1 for r in self._tasks.values() if r.status == "running")
        if running >= self._max_concurrent:
            raise SubagentLimitError(
                f"子代理并发已达上限（{self._max_concurrent} 个），"
                "等运行中的任务完成后再派，或改用后台任务轮询"
            )

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
                rec.tokens_in, rec.tokens_out,
            )
        except Exception:
            pass  # 记账失败不拖垮任务本身

    async def run_sync(self, agent_type: str, prompt: str) -> str:
        self._check_concurrency()
        rec = self._new_record(agent_type, prompt)
        try:
            result, _ = await self._run_one(rec)
            rec.result = result
            rec.status = "done"
            return result
        except Exception as e:
            rec.status = "error"
            rec.error = str(e)
            raise

    def start_background(self, agent_type: str, prompt: str) -> str:
        self._check_concurrency()
        rec = self._new_record(agent_type, prompt)
        task = asyncio_create(self._run_background(rec))
        rec.asyncio_task = task
        # 任务还没来得及开跑就被取消时，协程收尾不会执行——在这里兜底落终态
        task.add_done_callback(lambda t: self._finalize_if_unfinished(rec, t))
        return rec.id

    def _finalize_if_unfinished(self, rec: TaskRecord, task) -> None:
        if rec.status == "running" and task.cancelled():
            rec.status = "cancelled"
            rec.error = "用户取消"
            self._notify_finished(rec)

    async def _run_one(self, rec: TaskRecord) -> tuple[str, Agent]:
        """按定义跑一次：有解析结果（自定义/覆盖）就照计划跑，否则走内置默认。

        结束后把 token 用量记到任务上并交给宿主记账（含被取消时的已耗部分）。
        """
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
        extra = self._extra_tools_for_task() if (plan is None and rec.agent_type == "task") else None

        agent: Agent | None = None
        try:
            result, agent = await run_subagent(
                provider=provider,
                working_dir=self._working_dir,
                agent_type=rec.agent_type,
                prompt=rec.prompt,
                max_iterations=self._max_iterations,
                registry=plan.registry if plan is not None else None,
                system_extra=plan.system_extra if plan is not None else "",
                extra_tools=extra,
                restrict_to_workdir=self._restrict_to_workdir,
                on_event=self._make_forwarder(rec),
            )
            rec.tokens_in = agent.total_in_tokens
            rec.tokens_out = agent.total_out_tokens
            await self._record_usage(rec)
            return result, agent
        except asyncio.CancelledError:
            # 被取消：已消耗的 token 也要入账，然后原样上抛
            if agent is not None:
                rec.tokens_in = agent.total_in_tokens
                rec.tokens_out = agent.total_out_tokens
                await self._record_usage(rec)
            raise

    async def _run_background(self, rec: TaskRecord) -> None:
        try:
            result, _ = await self._run_one(rec)
            rec.result = result
            rec.status = "done"
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.error = "用户取消"
        except Exception as e:
            rec.status = "error"
            rec.error = str(e)
        self._notify_finished(rec)

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
        """任务簿快照：running 在前，其余按创建序倒序（新任务先看到）。

        session_id 给出时只返回该会话的任务（安全审查 B13：远程客户端只能
        看到自己正在交互的会话，不能枚举其他会话的 prompt/result）；
        None = 不过滤（本机任务簿保持全局视图）。
        """
        recs = [
            r for r in self._tasks.values()
            if session_id is None or r.session_id == session_id
        ]
        recs.sort(key=lambda r: (r.status != "running",))
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
        }

    def status(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def cancel_all(self, session_id: str | None = None) -> None:
        """真取消：后台任务拿到 CancelledError 后收尾（记账 + 广播终态）。

        同步任务跑在调用方（主轮）的任务里，随聊天框「停止」自然中断，这里不碰。
        session_id 给出时只取消该会话派生的任务（远程客户端的隔离，B13）。
        """
        for rec in self._tasks.values():
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
                self._notify_finished(rec)


def asyncio_create(coro: Awaitable) -> object:
    return asyncio.get_running_loop().create_task(coro)


# ---- 给主 Agent 用的工具 ----


class SpawnAgentArgs(BaseModel):
    agent_type: str = Field(
        default="explore",
        description="explore=只读调研（推荐）；task=额外可尝试写文件，"
        "但子代理内的写/命令操作会被自动拒绝（无确认通道）",
    )
    prompt: str = Field(description="给子代理的完整任务说明，要自包含（子代理看不到当前对话）")
    background: bool = Field(default=False, description="true=后台运行立即返回 task_id，用 check_task 查询")


class SpawnAgentTool(Tool):
    name = "spawn_agent"
    safety = Safety.READONLY
    args_model = SpawnAgentArgs

    def __init__(self, tasks: TaskManager) -> None:
        self._tasks = tasks
        self.description = (
            "派生一个子代理去完成独立子任务（如：调研代码结构、在多个文件里搜集信息）。"
            "子代理看不到主对话，所以 prompt 必须自包含。"
            "适合耗时的探索性工作，避免污染主上下文。"
        )
        custom = tasks.list_custom()
        if custom:
            self.description += "\n可用的自定义子代理：" + "；".join(
                f"{name}（{desc}）" if desc else name for name, desc in custom
            )

    async def run(self, args: SpawnAgentArgs, ctx: ToolContext) -> str:
        from ..tools.base import ToolError

        if not self._tasks.known_agent_type(args.agent_type):
            available = ["explore", "task"] + [n for n, _ in self._tasks.list_custom()]
            raise ToolError(
                f"未知的子代理类型「{args.agent_type}」，可用：{('、'.join(available))}"
            )
        try:
            if args.background:
                task_id = self._tasks.start_background(args.agent_type, args.prompt)
                return f"background subagent started, task_id={task_id}; poll it with check_task"
            result = await self._tasks.run_sync(args.agent_type, args.prompt)
        except SubagentLimitError as e:
            raise ToolError(str(e)) from e
        return result


class CheckTaskArgs(BaseModel):
    task_id: str = Field(description="spawn_agent(background=true) 返回的 task_id")


class CheckTaskTool(Tool):
    name = "check_task"
    description = "查询后台子代理任务的状态与结果。status=running 时可稍后再查。"
    safety = Safety.READONLY
    args_model = CheckTaskArgs

    def __init__(self, tasks: TaskManager) -> None:
        self._tasks = tasks

    async def run(self, args: CheckTaskArgs, ctx: ToolContext) -> str:
        rec = self._tasks.status(args.task_id)
        if rec is None:
            from ..tools.base import ToolError

            raise ToolError("unknown task_id: " + args.task_id)
        lines = [f"task_id: {rec.id}", f"type: {rec.agent_type}", f"status: {rec.status}"]
        if rec.status == "done" and rec.result is not None:
            if rec.result_delivered:
                # 报告可能数千到上万 token；模型「稍后再查」的惯性会让同一份
                # 长报告反复注入上下文——已投递就只回显开头防丢线索
                lines.append(
                    "（完整结果已在此前的查询里投递进上下文，不再重复。开头回显："
                    + rec.result[:200] + "）"
                )
            else:
                lines.append("--- result ---\n" + rec.result)
                rec.result_delivered = True
        if rec.error:
            lines.append("--- error ---\n" + rec.error)
        return "\n".join(lines)
