"""pipeline_write 工具：任务编排的流水线管理（对标 schedule_write 的会话工具模式）。

流水线解决「多个任务并行跑、最后汇总审查」的先后顺序问题：节点是 DAG 上的
一次无人值守 Agent 运行（与定时任务同一套 headless 门控），依赖全部完成后
才由服务层编排循环自动启动。节点有三类来源（kind）：run=新起无人值守会话
（默认）、task=挂接任务簿里已在跑/已完成的子代理任务（跟随其状态与产出）、
session=在指定已有会话里续跑（带该会话上下文）。定时任务可用 import_cron
把指令与预授权名单复制成 run 节点。

安全边界：本工具只写数据库（READONLY 免确认），**不能启动**流水线——
无人值守运行允许预授权哪些写/执行工具必须由用户在「任务编排」面板
审过节点清单后亲手点「启动」（与 trust.py 不自动执行仓库配置同一姿态）。
数据存项目库（skysheep.db 的 pipelines / pipeline_nodes 表），按项目隔离。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError

PipelineAction = Literal[
    "create", "list", "get", "delete", "add_node", "update_node", "delete_node",
    "import_cron",
]

NODE_STATUS_LABEL = {
    "blocked": "等待依赖",
    "ready": "待运行",
    "running": "运行中",
    "done": "已完成",
    "error": "失败",
    "cancelled": "已取消",
}

NODE_KIND_LABEL = {
    "run": "运行节点",
    "task": "挂接任务",
    "session": "会话续跑",
}

TOOL_HINT = (
    "节点的 allowed_tools 是无人值守运行时的预授权名单（只读工具本就放行，不用列）；"
    "写入/执行类工具不列就会被自动拒绝。创建的流水线是草稿，"
    "要用户在「任务编排」面板点「启动」后才会运行。"
    "max_runs 字段随节点类型双义：普通节点是失败自动重试次数（含首次共 max_runs 次），"
    "迭代（loop）节点是最大迭代轮数（2~10）；门/终止固定 1。"
)
# 设计说明：pipeline_nodes.max_runs 一列三义（重试次数 / 迭代上限 / 门固定 1），
# 是为了不加列而复用「这轮最多跑几次」的语义；若未来需要「迭代节点也要独立配重试」，
# 再拆 retries / loop_max 两列，不要继续往这一列上加含义。


def task_node_fields(rec) -> dict:
    """把任务簿 TaskRecord 映射成节点状态字段（backend 与工具共用一份映射）。"""
    status = {"running": "running", "done": "done",
              "error": "error", "cancelled": "cancelled"}.get(rec.status, "error")
    return {
        "status": status,
        "result": (rec.result or "") if rec.status == "done" else "",
        "last_error": (rec.error or ("任务" + rec.status)) if rec.status != "done" else "",
    }


class PipelineNodeSpec(BaseModel):
    title: str = Field(
        default="",
        description="节点名（动词开头，例如：实现登录接口）；挂接任务时可省略，缺省取任务指令前缀",
    )
    prompt: str = Field(
        default="",
        description="给这一步的完整指令，要自包含（节点看不到其他对话）；挂接任务时可省略",
    )
    task_id: str = Field(
        default="",
        description="挂接任务簿后台任务：给 check_task 用的 task_id，节点跟随该任务的状态与产出",
    )
    after: list[int] = Field(
        default_factory=list,
        description="依赖同次 create 里第 N 个节点（从 0 数）。审查/汇总类节点应依赖它前面的全部节点",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="无人值守预授权工具名单；默认空 = 只读运行",
    )
    dep_mode: Literal["all", "any"] = "all"
    timeout_s: int = Field(
        default=3600,
        description="节点超时秒数（0=不限时，默认 3600）：超时按可重试失败处理，防止单节点卡死占用并发槽",
    )


class PipelineWriteArgs(BaseModel):
    action: PipelineAction = Field(
        description="create 建（含全部节点）/ list 列表 / get 详情 / delete 删除 / "
        "add_node 追加节点 / update_node 改节点 / delete_node 删节点 / "
        "import_cron 把定时任务复制成节点"
    )
    id: int | None = Field(default=None, description="流水线 id（list 外必填）")
    name: str | None = Field(default=None, description="create：流水线名")
    concurrency: int = Field(default=2, description="create：同时运行的节点数上限（1~4）")
    nodes: list[PipelineNodeSpec] = Field(
        default_factory=list, description="create：全部节点，按执行意图排序"
    )
    node_id: int | None = Field(default=None, description="节点操作必填：节点 id")
    title: str | None = Field(default=None, description="节点操作：节点名")
    prompt: str | None = Field(default=None, description="节点操作：完整指令")
    task_id: str | None = Field(
        default=None,
        description="add_node 可选：挂接任务簿后台任务（给 task_id 即可，title/prompt 可省）",
    )
    cron_id: int | None = Field(
        default=None, description="import_cron 必填：要复制定时任务的 id"
    )
    disable_source: bool = Field(
        default=False, description="import_cron 可选：导入后同时停用原定时任务"
    )
    depends_on: list[int] = Field(
        default_factory=list, description="节点操作：依赖的节点 id（真实 id，不是序号）"
    )
    dep_mode: Literal["all", "any"] | None = Field(
        default=None, description="节点操作：all=全部完成才跑（默认），any=任一完成就跑"
    )
    allowed_tools: list[str] = Field(default_factory=list, description="节点操作：预授权名单")
    timeout_s: int | None = Field(
        default=None,
        description="节点操作：超时秒数（0=不限时）；不给 = 不改",
    )
    clear_allowed_tools: bool = Field(
        default=False,
        description="update_node 专用：置 true 清空该节点预授权名单（恢复仅只读运行）；"
        "allowed_tools 给空列表表示「不改」，无法表达清空，用这个字段",
    )
    include_finished: bool = Field(default=True, description="list：是否包含已结束的流水线")


class PipelineWriteTool(Tool):
    name = "pipeline_write"
    description = (
        "管理任务编排流水线：把多个任务排成「并行开发 → 汇总审查」的依赖图，"
        "每个节点是一次无人值守的 Agent 运行，依赖完成后自动开始下一批；"
        "也能把现有的任务纳入编排——挂接任务簿后台任务（add_node 给 task_id）、"
        "在指定会话续跑（后续版本面板入口）、复制定时任务为节点（import_cron）。"
        "用户说「这几个任务并行做，完了帮我总结审查」时用 create 排流水线；"
        "流水线在「任务编排」面板里启动和查看进度。" + TOOL_HINT
    )
    safety = Safety.READONLY
    args_model = PipelineWriteArgs

    def __init__(self, store, project_id_fn, tasks_fn=None) -> None:
        self.store = store  # SessionStore（duck-type，便于测试注入）
        self._project_id_fn = project_id_fn  # () -> int：当前项目 id
        self._tasks_fn = tasks_fn  # () -> TaskManager | None：校验挂接任务存在性

    async def run(self, args: PipelineWriteArgs, ctx: ToolContext) -> str:
        try:
            return await self._dispatch(args)
        except ValueError as e:
            raise ToolError(str(e)) from e

    async def _dispatch(self, args: PipelineWriteArgs) -> str:
        a = args
        if a.action == "create":
            if not a.name or not a.nodes:
                raise ToolError("create 需要 name 和至少一个节点")
            pipe = await self.store.add_pipeline(
                self._project_id_fn(), a.name,
                # after（同批次序号）按 store 约定放进 depends_on，由 store 物化成节点 id
                nodes=[await self._node_dict(n) for n in a.nodes],
                concurrency=a.concurrency,
            )
            return "pipeline created (draft, 等用户启动):\n" + self._fmt(pipe, detail=True)
        if a.action == "list":
            pipes = await self.store.list_pipelines(self._project_id_fn())
            if not a.include_finished:
                pipes = [p for p in pipes if p["status"] in ("draft", "running")]
            if not pipes:
                return "no pipelines"
            return "pipelines:\n" + "\n".join(self._fmt(p) for p in pipes)
        if a.action == "get":
            pipe = await self._get_pipe(a.id)
            self._ensure_owned(pipe)
            return self._fmt(pipe, detail=True)
        if a.action == "delete":
            pipe = await self._get_pipe(a.id)
            self._ensure_owned(pipe)
            ok = await self.store.delete_pipeline(pipe["id"])
            return "deleted" if ok else f"pipeline {a.id} not found"
        if a.action == "import_cron":
            return await self._import_cron(a)
        # ---- 节点操作 ----
        if a.action == "add_node":
            pipe = await self._get_pipe(a.id)
            self._ensure_owned(pipe)
            spec = PipelineNodeSpec(
                title=a.title or "", prompt=a.prompt or "",
                task_id=a.task_id or "", after=[],
                allowed_tools=a.allowed_tools, dep_mode=a.dep_mode or "all",
                timeout_s=a.timeout_s if a.timeout_s is not None else 3600,
            )
            node_dict = await self._node_dict(spec)
            node = await self.store.add_pipeline_node(
                pipe["id"], node_dict["title"], node_dict["prompt"],
                allowed_tools=node_dict["allowed_tools"],
                depends_on=a.depends_on, dep_mode=node_dict["dep_mode"],
                kind=node_dict["kind"], ref_id=node_dict["ref_id"],
                timeout_s=node_dict["timeout_s"],
            )
            if node_dict["kind"] == "task":
                # 挂接即跟随：把任务当前状态落到节点上，下游马上能看见
                rec = self._task_rec(node_dict["ref_id"])
                if rec is not None:
                    node = await self.store.update_pipeline_node(node["id"], **task_node_fields(rec))
            return "node added: " + self._fmt_node(node)
        if a.action == "update_node":
            if not a.node_id:
                raise ToolError("update_node 需要 node_id")
            node = await self.store.get_pipeline_node(a.node_id)
            if node is None:
                raise ToolError(f"节点 {a.node_id} 不存在")
            pipe = await self._get_pipe(node["pipeline_id"])
            self._ensure_owned(pipe)
            if node["status"] == "running":
                raise ToolError("节点正在运行，等它结束再改")
            kw: dict = {}
            if a.title is not None:
                kw["title"] = a.title
            if a.prompt is not None:
                kw["prompt"] = a.prompt
            if a.dep_mode is not None:
                kw["dep_mode"] = a.dep_mode
            if a.allowed_tools:
                kw["allowed_tools"] = a.allowed_tools
            elif a.clear_allowed_tools:
                kw["allowed_tools"] = []  # 显式清空：恢复仅只读运行
            if a.timeout_s is not None:
                kw["timeout_s"] = max(0, int(a.timeout_s))
            if a.depends_on:
                await self._validate_deps(pipe, node["id"], a.depends_on)
                kw["depends_on"] = a.depends_on
            if not kw:
                raise ToolError("update_node 没有给出任何要修改的字段")
            updated = await self.store.update_pipeline_node(node["id"], **kw)
            return "node updated: " + self._fmt_node(updated)
        # delete_node
        if not a.node_id:
            raise ToolError("delete_node 需要 node_id")
        node = await self.store.get_pipeline_node(a.node_id)
        if node is not None:
            pipe = await self._get_pipe(node["pipeline_id"])
            self._ensure_owned(pipe)
            if node["status"] == "running":
                raise ToolError("节点正在运行，不能删除；要中止请先停止整条流水线")
        ok = await self.store.delete_pipeline_node(a.node_id)
        return "deleted" if ok else f"node {a.node_id} not found"

    async def _require_pipe_id(self, pid: int | None) -> int:
        if not pid:
            raise ToolError("需要流水线 id（先用 list 查）")
        return int(pid)

    def _task_rec(self, task_id: str):
        """任务簿任务（不存在返回 None；未注入任务簿时一律视为不存在）。"""
        tasks = self._tasks_fn() if self._tasks_fn else None
        return tasks.status(task_id) if tasks is not None else None

    async def _node_dict(self, n: PipelineNodeSpec) -> dict:
        """节点规格 → store 节点字典；task_id 给出时转为挂接节点并校验任务存在。

        create 批量路径与 add_node 共用：挂接节点 title 缺省取任务指令前缀，
        prompt 可为空（执行体是原任务，不是新指令）。
        """
        if n.task_id:
            rec = self._task_rec(n.task_id)
            if rec is None:
                raise ToolError(f"任务簿里找不到任务 {n.task_id}（挂接只对本机任务簿有效）")
            return {
                "title": n.title or f"挂接任务 {rec.prompt[:40]}",
                "prompt": n.prompt,
                "depends_on": list(n.after),
                "allowed_tools": n.allowed_tools,
                "dep_mode": n.dep_mode,
                "kind": "task", "ref_id": n.task_id,
                "timeout_s": 0,  # 挂接节点不派跑，无超时概念
            }
        if not n.prompt:
            raise ToolError("节点缺少指令（prompt）；只有挂接任务（task_id）可以不带指令")
        return {
            "title": n.title or (n.prompt[:40] or "节点"),
            "prompt": n.prompt,
            "depends_on": list(n.after),
            "allowed_tools": n.allowed_tools,
            "dep_mode": n.dep_mode,
            "kind": "run", "ref_id": "",
            "timeout_s": max(0, int(n.timeout_s or 0)),
        }

    async def _import_cron(self, a: PipelineWriteArgs) -> str:
        """把定时任务的指令与预授权名单复制成一个 run 节点（原任务默认照常周期运行）。"""
        if not a.cron_id:
            raise ToolError("import_cron 需要 cron_id")
        cron = await self.store.get_cron_task(int(a.cron_id))
        if cron is None or cron["project_id"] != self._project_id_fn():
            raise ToolError(f"定时任务 {a.cron_id} 不存在（或不属于当前项目）")
        pipe = await self._get_pipe(a.id)
        self._ensure_owned(pipe)
        node = await self.store.add_pipeline_node(
            pipe["id"],
            a.title or f"定时任务：{cron['name']}",
            a.prompt or cron["prompt"],
            allowed_tools=cron["allowed_tools"],
            depends_on=a.depends_on, dep_mode=a.dep_mode or "all",
        )
        if a.disable_source:
            await self.store.update_cron_task(cron["id"], enabled=0, next_run_at=0)
            tail = "；原定时任务已停用"
        else:
            tail = "；原定时任务仍按周期独立运行"
        return "cron imported: " + self._fmt_node(node) + tail

    async def _get_pipe(self, pid: int | None) -> dict:
        pipe = await self.store.get_pipeline(await self._require_pipe_id(pid))
        if pipe is None:
            raise ToolError(f"pipeline {pid} not found")
        return pipe

    def _ensure_owned(self, pipe: dict) -> None:
        """跨项目不可见不可改（与定时任务的项目归属校验同一姿态）。"""
        if pipe["project_id"] != self._project_id_fn():
            raise ToolError("流水线不存在（或不属于当前项目）")

    async def _validate_deps(self, pipe: dict, node_id: int, depends_on: list[int]) -> None:
        await self.store._check_dep_refs(pipe["id"], depends_on, exclude_id=node_id)
        if await self.store.has_dependency_cycle(pipe["id"], node_id, depends_on):
            raise ToolError("这样的依赖会成环，节点等待关系必须是单向的")

    @staticmethod
    def _fmt(pipe: dict, detail: bool = False) -> str:
        done = sum(1 for n in pipe["nodes"] if n["status"] == "done")
        head = "#{} [{}] {}（{}，节点 {}/{} 完成，并发上限 {}）".format(
            pipe["id"], pipe["status"], pipe["name"],
            NODE_STATUS_LABEL.get(pipe["status"], pipe["status"]),
            done, len(pipe["nodes"]), pipe["concurrency"],
        )
        if not detail:
            return head
        lines = [head]
        for n in pipe["nodes"]:
            dep = "，等 #" + "、#".join(str(d) for d in n["depends_on"]) if n["depends_on"] else ""
            lines.append(f"  {PipelineWriteTool._fmt_node(n)}{dep}")
            if n["result"]:
                lines.append(f"    产出：{n['result'][:200]}")
            if n["last_error"]:
                lines.append(f"    错误：{n['last_error'][:200]}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_node(n: dict) -> str:
        kind = NODE_KIND_LABEL.get(n.get("kind") or "run", "")
        prefix = f"{kind} " if kind and n.get("kind") != "run" else ""
        timeout = n.get("timeout_s")
        timeout_txt = f"，超时 {timeout // 60} 分钟" if isinstance(timeout, int) and timeout > 0 else ""
        return "#{} [{}] {}{}（授权 {}{}）".format(
            n["id"], NODE_STATUS_LABEL.get(n["status"], n["status"]), prefix, n["title"],
            "、".join(n["allowed_tools"]) or "仅只读",
            timeout_txt,
        )
