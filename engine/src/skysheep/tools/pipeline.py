"""pipeline_write 工具：任务编排的流水线管理（对标 schedule_write 的会话工具模式）。

流水线解决「多个任务并行跑、最后汇总审查」的先后顺序问题：节点是 DAG 上的
一次无人值守 Agent 运行（与定时任务同一套 headless 门控），依赖全部完成后
才由服务层编排循环自动启动。

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
]

NODE_STATUS_LABEL = {
    "blocked": "等待依赖",
    "ready": "待运行",
    "running": "运行中",
    "done": "已完成",
    "error": "失败",
    "cancelled": "已取消",
}

TOOL_HINT = (
    "节点的 allowed_tools 是无人值守运行时的预授权名单（只读工具本就放行，不用列）；"
    "写入/执行类工具不列就会被自动拒绝。创建的流水线是草稿，"
    "要用户在「任务编排」面板点「启动」后才会运行。"
)


class PipelineNodeSpec(BaseModel):
    title: str = Field(description="节点名（一句话，动词开头，例如：实现登录接口）")
    prompt: str = Field(description="给这一步的完整指令，要自包含（节点看不到其他对话）")
    after: list[int] = Field(
        default_factory=list,
        description="依赖同次 create 里第 N 个节点（从 0 数）。审查/汇总类节点应依赖它前面的全部节点",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="无人值守预授权工具名单；默认空 = 只读运行",
    )
    dep_mode: Literal["all", "any"] = "all"


class PipelineWriteArgs(BaseModel):
    action: PipelineAction = Field(
        description="create 建（含全部节点）/ list 列表 / get 详情 / delete 删除 / "
        "add_node 追加节点 / update_node 改节点 / delete_node 删节点"
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
    depends_on: list[int] = Field(
        default_factory=list, description="节点操作：依赖的节点 id（真实 id，不是序号）"
    )
    dep_mode: Literal["all", "any"] | None = Field(
        default=None, description="节点操作：all=全部完成才跑（默认），any=任一完成就跑"
    )
    allowed_tools: list[str] = Field(default_factory=list, description="节点操作：预授权名单")
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
        "每个节点是一次无人值守的 Agent 运行，依赖完成后自动开始下一批。"
        "用户说「这几个任务并行做，完了帮我总结审查」时用 create 排流水线；"
        "流水线在「任务编排」面板里启动和查看进度。" + TOOL_HINT
    )
    safety = Safety.READONLY
    args_model = PipelineWriteArgs

    def __init__(self, store, project_id_fn) -> None:
        self.store = store  # SessionStore（duck-type，便于测试注入）
        self._project_id_fn = project_id_fn  # () -> int：当前项目 id

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
                nodes=[{
                    "title": n.title, "prompt": n.prompt,
                    "depends_on": list(n.after),
                    "allowed_tools": n.allowed_tools, "dep_mode": n.dep_mode,
                } for n in a.nodes],
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
        # ---- 节点操作 ----
        if a.action == "add_node":
            if not a.title or not a.prompt:
                raise ToolError("add_node 需要 title 和 prompt")
            pipe = await self._get_pipe(a.id)
            self._ensure_owned(pipe)
            node = await self.store.add_pipeline_node(
                pipe["id"], a.title, a.prompt,
                allowed_tools=a.allowed_tools,
                depends_on=a.depends_on, dep_mode=a.dep_mode or "all",
            )
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
        return "#{} [{}] {}（授权 {}）".format(
            n["id"], NODE_STATUS_LABEL.get(n["status"], n["status"]), n["title"],
            "、".join(n["allowed_tools"]) or "仅只读",
        )
