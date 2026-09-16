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
"""

from __future__ import annotations

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
) -> tuple[str, Agent]:
    """运行一个子代理到结束，返回（最终报告文本, agent 实例）。

    registry 不传时按 agent_type 用内置只读工具集；宿主（设置页的自定义
    子代理）可以传入自己组装的注册表。system_extra 追加在子代理系统提示词
    之后，用来注入自定义子代理的描述与专项指令。
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
    def __init__(self, task_id: str, agent_type: str, prompt: str) -> None:
        self.id = task_id
        self.agent_type = agent_type
        self.prompt = prompt
        self.status = "running"  # running | done | error
        self.result: str | None = None
        self.error: str | None = None


class TaskManager:
    """后台子代理任务簿。

    plan_resolver / registry_resolver / provider_resolver 由宿主（server backend）
    注入，用来把「设置页里定义的自定义子代理」和「内置子代理的模型/思考强度
    覆盖」解析成一次真实运行；不注入时全部走内置默认（explore/task + 主 provider）。
    """

    def __init__(
        self,
        provider_factory: Callable[[], Provider],
        working_dir: Path,
        max_iterations: int = 25,
        store=None,
        provider_resolver: Callable[[str, str, str], Provider] | None = None,
        registry_resolver=None,
    ) -> None:
        self._provider_factory = provider_factory
        self._working_dir = working_dir
        self._max_iterations = max_iterations
        self._store = store
        self._provider_resolver = provider_resolver
        self._registry_resolver = registry_resolver
        self._extra: list[Tool] | None = None
        self._tasks: dict[str, TaskRecord] = {}
        self._restrict_to_workdir = False

    def set_max_iterations(self, value: int) -> None:
        """设置页改了「子代理迭代轮数」后热更新，不用重启。"""
        self._max_iterations = max(1, min(100, int(value)))

    def set_restrict_to_workdir(self, value: bool) -> None:
        """主 Agent 的「仅允许访问工作目录」开关同样约束子代理。"""
        self._restrict_to_workdir = bool(value)

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
        rec = TaskRecord(task_id, agent_type, prompt)
        self._tasks[task_id] = rec
        return rec

    async def run_sync(self, agent_type: str, prompt: str) -> str:
        rec = self._new_record(agent_type, prompt)
        try:
            result, _ = await self._run_one(agent_type, prompt)
            rec.result = result
            rec.status = "done"
            return result
        except Exception as e:
            rec.status = "error"
            rec.error = str(e)
            raise

    def start_background(self, agent_type: str, prompt: str) -> str:
        rec = self._new_record(agent_type, prompt)
        asyncio_create(self._run_background(rec))
        return rec.id

    async def _run_one(self, agent_type: str, prompt: str) -> tuple[str, Agent]:
        """按定义跑一次：有解析结果（自定义/覆盖）就照计划跑，否则走内置默认。"""
        plan = None
        try:
            plan = self._plan_for(agent_type)
        except Exception as e:  # 定义解析失败（如模型没配 Key）→ 记为任务错误
            raise RuntimeError(f"子代理「{agent_type}」启动失败：{e}") from e
        if plan is not None:
            return await run_subagent(
                provider=plan.provider,
                working_dir=self._working_dir,
                agent_type=agent_type,
                prompt=prompt,
                max_iterations=self._max_iterations,
                registry=plan.registry,
                system_extra=plan.system_extra,
                restrict_to_workdir=self._restrict_to_workdir,
            )
        extra = self._extra_tools_for_task() if agent_type == "task" else None
        return await run_subagent(
            provider=self._provider_factory(),
            working_dir=self._working_dir,
            agent_type=agent_type,
            prompt=prompt,
            max_iterations=self._max_iterations,
            extra_tools=extra,
            restrict_to_workdir=self._restrict_to_workdir,
        )

    async def _run_background(self, rec: TaskRecord) -> None:
        try:
            result, _ = await self._run_one(rec.agent_type, rec.prompt)
            rec.result = result
            rec.status = "done"
        except Exception as e:
            rec.status = "error"
            rec.error = str(e)

    def list_tasks(self, limit: int = 30) -> list[dict]:
        """任务簿快照：running 在前，其余按创建序倒序（新任务先看到）。"""
        recs = list(self._tasks.values())
        recs.sort(key=lambda r: (r.status != "running",))
        out = []
        for r in recs[-limit:][::-1]:
            out.append({
                "id": r.id,
                "agent_type": r.agent_type,
                "prompt": r.prompt[:160],
                "status": r.status,
                "result": (r.result or "")[:800],
                "error": (r.error or "")[:300],
            })
        return out

    def status(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def cancel_all(self) -> None:
        for rec in self._tasks.values():
            if rec.status == "running":
                rec.status = "error"
                rec.error = "cancelled on session exit"


def asyncio_create(coro: Awaitable) -> None:
    import asyncio

    asyncio.get_running_loop().create_task(coro)


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
        if args.background:
            task_id = self._tasks.start_background(args.agent_type, args.prompt)
            return f"background subagent started, task_id={task_id}; poll it with check_task"
        result = await self._tasks.run_sync(args.agent_type, args.prompt)
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
            lines.append("--- result ---\n" + rec.result)
        if rec.error:
            lines.append("--- error ---\n" + rec.error)
        return "\n".join(lines)
