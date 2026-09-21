"""todo_write 工具：Agent 多步任务的任务清单（对标 Claude Code / ZCode 的 Todo）。

清单状态存在工具实例里；Agent 循环在 todo_write 执行后产出 TodoUpdated
事件，前端侧栏实时渲染进度。仅记录，不做任何危险操作，READONLY 免确认。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext

TodoStatus = Literal["pending", "in_progress", "completed"]

MAX_TODOS = 50


class TodoItem(BaseModel):
    content: str = Field(description="任务内容（一句话，动词开头）")
    status: TodoStatus = "pending"


class TodoWriteArgs(BaseModel):
    todos: list[TodoItem] = Field(
        description="完整任务清单（每次全量覆盖）：至少含第一步；只标一个 in_progress"
    )


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "维护当前任务的待办清单。多步任务开始时写入全部步骤（一个 in_progress，其余 pending）；"
        "每完成一步就更新状态；全部完成时全部标 completed。简单任务（1-2 步）不要使用。"
    )
    safety = Safety.READONLY
    # 每次全量覆盖清单，同参数重复写入结果一致
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = TodoWriteArgs

    def __init__(self) -> None:
        self.items: list[dict] = []

    async def run(self, args: TodoWriteArgs, ctx: ToolContext) -> str:
        todos = args.todos[:MAX_TODOS]
        self.items = [{"content": t.content, "status": t.status} for t in todos]
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines = ["{} {}".format(
            {"pending": "□", "in_progress": "▶", "completed": "✓"}.get(t["status"], "□"),
            t["content"],
        ) for t in self.items]
        return f"todo updated ({done}/{len(self.items)} done)\n" + "\n".join(lines)
