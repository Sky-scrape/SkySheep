"""load_skill 工具：渐进式披露——模型按需读取技能完整指令。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..skills.loader import SkillLoader
from .base import Safety, Tool, ToolContext, ToolError, truncate_output

MAX_SKILL_CHARS = 20_000


class LoadSkillArgs(BaseModel):
    name: str = Field(description="技能名称（见系统提示词中的 Skills 清单）")


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "读取一个技能的完整指令。执行技能相关任务前应先加载。"
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = LoadSkillArgs

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    async def run(self, args: LoadSkillArgs, ctx: ToolContext) -> str:
        try:
            body = self._loader.load_body(args.name)
        except KeyError as e:
            raise ToolError(str(e).strip("'\"")) from e
        return truncate_output(body, MAX_SKILL_CHARS)
