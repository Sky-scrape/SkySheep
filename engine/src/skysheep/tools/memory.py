"""memory_write 工具：Agent 的用户级全局记忆（对标 Claude Code / ZCode 的 memory）。

AGENTS.md 是项目级、手动编辑的项目约定；本工具补的是「跨项目的用户记忆」：
Agent 在对话中学到值得长期记住的事实（用户偏好、常用环境、背景信息）时自己
写入 ~/.skysheep/memory.md，系统提示词每一轮都注入该文件（限长）。

安全边界：只能写 SkySheep 自己的记忆文件（路径固定、不接受任何路径参数），
与 schedule_write 写应用自有存储同理，READONLY 免确认。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError

MemoryAction = Literal["append", "list", "delete"]

MAX_MEMORY_CHARS = 4000          # 注入系统提示词的上限
MAX_MEMORY_FILE_CHARS = 200_000  # 文件本身的上限（防无限膨胀）


def memory_path() -> Path:
    from ..config import skysheep_home

    return skysheep_home() / "memory.md"


def load_memory_text() -> str:
    """读取记忆文本（超长截断）；没有文件返回空串。"""
    p = memory_path()
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:MAX_MEMORY_CHARS]
    except OSError:
        return ""


def render_memory_section() -> str:
    """系统提示词的记忆段落；无记忆时返回空串。"""
    text = load_memory_text().strip()
    if not text:
        return ""
    return f"\n# User memory（跨项目的用户记忆，管理用 memory_write）\n{text}\n"


class MemoryWriteArgs(BaseModel):
    action: MemoryAction = Field(description="append 追加一条 / list 查看全部 / delete 按内容删除")
    content: str = Field(default="", description="append：要记住的内容（一句话，具体、可长期有效）")
    match: str = Field(default="", description="delete：要删除条目里包含的原文片段")


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "管理你的跨项目用户记忆（用户偏好、常用环境、长期有效的事实）。"
        "当用户说「记住我喜欢…」「以后都用…」，或你发现值得长期记住的信息时用 append；"
        "用户要求忘记某事时用 delete。记忆会自动注入你之后的每一轮对话。"
    )
    safety = Safety.READONLY  # 只写 SkySheep 自有记忆文件，不需要确认
    args_model = MemoryWriteArgs

    async def run(self, args: MemoryWriteArgs, ctx: ToolContext) -> str:
        p = memory_path()
        if args.action == "append":
            content = (args.content or "").strip()
            if not content:
                raise ToolError("append 需要 content（要记住的内容）")
            lines = self._read_lines(p)
            if any(content in ln for ln in lines):
                return "already remembered（已有相同内容的记忆，不重复追加）"
            lines.append(f"- [{date.today().isoformat()}] {content}")
            self._write_lines(p, lines)
            return f"remembered: {content[:80]}"
        if args.action == "list":
            lines = self._read_lines(p)
            if not lines:
                return "(memory is empty)"
            return "\n".join(lines)
        # delete
        match = (args.match or "").strip()
        if not match:
            raise ToolError("delete 需要 match（要删除条目包含的片段）")
        lines = self._read_lines(p)
        kept = [ln for ln in lines if match not in ln]
        removed = len(lines) - len(kept)
        if not removed:
            return f"no memory entry contains {match!r}"
        self._write_lines(p, kept)
        return f"forgot {removed} entr{'y' if removed == 1 else 'ies'}"

    @staticmethod
    def _read_lines(p: Path) -> list[str]:
        try:
            return p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    @staticmethod
    def _write_lines(p: Path, lines: list[str]) -> None:
        # 容量护栏：超限从最旧的条目开始丢
        while lines and sum(len(ln) + 1 for ln in lines) > MAX_MEMORY_FILE_CHARS:
            lines.pop(0)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError as e:
            raise ToolError(f"cannot write memory file: {e}") from e
