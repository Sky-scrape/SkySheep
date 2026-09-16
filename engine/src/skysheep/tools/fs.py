"""文件系统工具：读 / 写 / 编辑 / 列目录 / glob。"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .base import (
    ChangeRecorder,
    Safety,
    Tool,
    ToolContext,
    ToolError,
    rel_path,
    resolve_path,
    truncate_output,
)

MAX_FILE_CHARS = 200_000
DEFAULT_READ_LIMIT = 2000
MAX_DIFF_LINES = 240


def make_diff(old: str, new: str, path_label: str) -> str:
    """统一 diff，超长截断；无变化返回空串。"""
    if old == new:
        return ""
    delta = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="a/" + path_label, tofile="b/" + path_label, lineterm="",
    )
    lines = list(delta)
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + ["... (diff truncated)"]
    return "\n".join(lines)


class ReadFileArgs(BaseModel):
    path: str = Field(description="文件路径（相对当前工作目录或绝对路径）")
    offset: int = Field(default=1, ge=1, description="起始行号（1-based）")
    limit: int = Field(default=DEFAULT_READ_LIMIT, ge=1, description="最多读取行数")


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取文本文件内容，带行号返回。大文件可用 offset/limit 分段读取；"
        "读之前若不知道文件结构，先用 list_dir 或 glob 探索。"
    )
    safety = Safety.READONLY
    args_model = ReadFileArgs

    async def run(self, args: ReadFileArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if not p.exists():
            raise ToolError("file not found: " + shown)
        if p.is_dir():
            raise ToolError(shown + " is a directory, use list_dir instead")
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError("cannot read " + shown + ": " + str(e)) from e
        if len(raw) > MAX_FILE_CHARS and args.offset == 1 and args.limit == DEFAULT_READ_LIMIT:
            raw = raw[:MAX_FILE_CHARS]
            raw += f"\n\n... [file truncated at {MAX_FILE_CHARS} chars, use offset/limit to read more]"
        lines = raw.splitlines()
        start = args.offset - 1
        chunk = lines[start : start + args.limit]
        if not chunk:
            return f"[no content at line {args.offset}; file has {len(lines)} lines]"
        numbered = "\n".join(
            f"{start + i + 1:>6}\t{line}" for i, line in enumerate(chunk)
        )
        return truncate_output(numbered)


class WriteFileArgs(BaseModel):
    path: str = Field(description="目标文件路径")
    content: str = Field(description="完整文件内容（整体覆盖写入）")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "把完整内容写入文件（整体覆盖）。会自动创建父目录。"
        "修改已有文件前应先 read_file 了解现状。"
    )
    safety = Safety.WRITE
    args_model = WriteFileArgs
    last_diff = ""

    def __init__(self, recorder: ChangeRecorder | None = None) -> None:
        self.recorder = recorder

    async def run(self, args: WriteFileArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if self.recorder is not None:
            self.recorder.record(p)  # 检查点：记下覆盖前的原始内容
        old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.content, encoding="utf-8")
        except OSError as e:
            raise ToolError("cannot write " + shown + ": " + str(e)) from e
        self.last_diff = make_diff(old, args.content, shown)
        n = len(args.content.splitlines())
        return f"wrote {n} lines ({len(args.content)} chars) to {shown}"


class EditFileArgs(BaseModel):
    path: str = Field(description="要编辑的文件")
    old_string: str = Field(description="要被替换的原文（必须与文件内容精确匹配且唯一，除非 replace_all）")
    new_string: str = Field(description="替换后的内容")
    replace_all: bool = Field(default=False, description="替换所有出现（默认仅当唯一匹配时替换）")


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "对文件做精确字符串替换，适合小改动。old_string 必须与文件内容完全一致；"
        "为唯一匹配请带上足够的上下文行。新建文件请用 write_file。"
    )
    safety = Safety.WRITE
    args_model = EditFileArgs
    last_diff = ""

    def __init__(self, recorder: ChangeRecorder | None = None) -> None:
        self.recorder = recorder

    async def run(self, args: EditFileArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if not p.exists() or p.is_dir():
            raise ToolError("file not found: " + shown)
        if self.recorder is not None:
            self.recorder.record(p)  # 检查点：记下编辑前的原始内容
        content = p.read_text(encoding="utf-8", errors="replace")
        if args.old_string not in content:
            raise ToolError("old_string not found in file; read the file first and copy exact text")
        count = content.count(args.old_string)
        if count > 1 and not args.replace_all:
            raise ToolError(
                f"old_string matches {count} locations; add surrounding context to make it unique "
                "or set replace_all=true"
            )
        if args.old_string == args.new_string:
            raise ToolError("old_string and new_string are identical")
        new_content = content.replace(args.old_string, args.new_string)
        p.write_text(new_content, encoding="utf-8")
        self.last_diff = make_diff(content, new_content, shown)
        replaced = count if args.replace_all else 1
        return f"edited {shown}: {replaced} replacement(s)"


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="目录路径，默认工作目录")


class ListDirTool(Tool):
    name = "list_dir"
    description = "列出目录内容（一层），标注目录/文件与大小。"
    safety = Safety.READONLY
    args_model = ListDirArgs

    async def run(self, args: ListDirArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        if not p.is_dir():
            raise ToolError("not a directory: " + rel_path(ctx, p))
        try:
            entries = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            raise ToolError("cannot list " + rel_path(ctx, p) + ": " + str(e)) from e
        if not entries:
            return "(empty directory)"
        lines = []
        for e in entries[:500]:
            if e.is_dir():
                lines.append(e.name + "/")
            else:
                lines.append(f"{e.name}  ({e.stat().st_size:,} B)")
        if len(entries) > 500:
            lines.append(f"... and {len(entries) - 500} more entries")
        return "\n".join(lines)


class GlobArgs(BaseModel):
    pattern: str = Field(description="glob 模式，如 src/**/*.py")
    path: str = Field(default=".", description="起始目录")


class GlobTool(Tool):
    name = "glob"
    description = (
        "按 glob 模式查找文件路径，结果按修改时间排序（新的在前）。"
        "找文件比 list_dir 更高效。遵循 .skysheepignore 忽略规则。"
    )
    safety = Safety.READONLY
    args_model = GlobArgs

    async def run(self, args: GlobArgs, ctx: ToolContext) -> str:
        base = resolve_path(ctx, args.path)
        if not base.is_dir():
            raise ToolError("not a directory: " + rel_path(ctx, base))
        matches = [m for m in base.glob(args.pattern) if m.is_file()]
        matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
        # 遵循项目忽略文件（.skysheepignore / .gitignore / .env 内建默认）
        try:
            from ..core.ignore import IgnoreRules

            ignore = IgnoreRules.load(ctx.working_dir)
        except OSError:
            ignore = None
        if ignore is not None:
            keep = []
            for m in matches:
                try:
                    rel = m.resolve().relative_to(Path(ctx.working_dir).resolve()).as_posix()
                except (OSError, ValueError):
                    keep.append(m)
                    continue
                if not ignore.matches(rel):
                    keep.append(m)
            matches = keep
        if not matches:
            return "(no matches)"
        shown = [rel_path(ctx, m) for m in matches[:200]]
        out = "\n".join(shown)
        if len(matches) > 200:
            out += f"\n... and {len(matches) - 200} more"
        return out
