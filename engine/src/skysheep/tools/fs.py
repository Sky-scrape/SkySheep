"""文件系统工具：读 / 写 / 编辑 / 列目录 / glob / 移动 / 删除 / 建目录。"""

from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from ..textio import read_text_file, to_lf, write_text_file
from .base import (
    ChangeRecorder,
    Safety,
    Tool,
    ToolContext,
    ToolError,
    check_write_size,
    rel_path,
    resolve_path,
    truncate_output,
)

MAX_FILE_CHARS = 200_000
DEFAULT_READ_LIMIT = 2000
MAX_DIFF_LINES = 240

# 图片扩展名：read_file 遇到它们时指引改用 read_image，而不是吐出乱码
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _load_for_edit(path: Path, shown: str):
    """读文本文件用于编辑；二进制 / 编码不明时给出可执行的指引而不是静默替换。"""
    try:
        loaded = read_text_file(path)
    except OSError as e:
        raise ToolError("cannot read " + shown + ": " + str(e)) from e
    if loaded.binary:
        if path.suffix.lower() in IMAGE_SUFFIXES:
            raise ToolError(
                f"{shown} 是图片，read_file 读不了。请用 read_image 工具，"
                "它会把图像附加到对话里让你直接看到。"
            )
        raise ToolError(
            f"{shown} 是二进制文件（含 NUL 字节），不能按文本读写。"
            "图片用 read_image；PDF/Word/Excel 用 read_document；需要执行请用 run_command。"
        )
    if not loaded.certain:
        raise ToolError(
            f"{shown} 的文本编码无法确定（既不是 UTF-8 也不是 GB18030），"
            "为避免写回时损坏内容，编辑操作已拒绝。请先用系统编辑器确认编码，"
            "或把文件另存为 UTF-8 后重试。"
        )
    return loaded


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
        "读取文本文件内容，带行号返回。会自动识别 UTF-8 / GB18030 等编码，非 UTF-8 文件也能正确显示。"
        "大文件可用 offset/limit 分段读取；读之前若不知道文件结构，先用 list_dir 或 glob 探索。"
        "图片用 read_image，PDF/Word/Excel/PPT 用 read_document。"
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
            loaded = read_text_file(p)
        except OSError as e:
            raise ToolError("cannot read " + shown + ": " + str(e)) from e
        if loaded.binary:
            if p.suffix.lower() in IMAGE_SUFFIXES:
                raise ToolError(
                    f"{shown} 是图片，read_file 读不了。请用 read_image 工具，"
                    "它会把图像附加到对话里让你直接看到。"
                )
            raise ToolError(
                f"{shown} 是二进制文件（含 NUL 字节），不能按文本读取。"
                "图片用 read_image；PDF/Word/Excel/PPT 用 read_document。"
            )
        raw = loaded.text
        notes: list[str] = []
        if not loaded.certain:
            notes.append(
                f"编码无法确定，已按 {loaded.encoding} 尽力展示（可能有替换字符）——"
                "只读可以，不要编辑这个文件，否则会损坏原内容。"
            )
        if loaded.encoding != "utf-8" and loaded.certain:
            notes.append(f"编码 {loaded.encoding}，编辑时会按原编码写回。")
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
        header = "".join(f"[{n}]\n" for n in notes)
        return truncate_output(header + numbered)


class WriteFileArgs(BaseModel):
    path: str = Field(description="目标文件路径")
    content: str = Field(description="完整文件内容（整体覆盖写入）")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "把完整内容写入文件（整体覆盖）。会自动创建父目录。"
        "覆盖已有文件时沿用原文件的编码与行尾符（LF / CRLF 不被静默改写）。"
        "修改已有文件前应先 read_file 了解现状；小改动用 edit_file 更安全。"
    )
    safety = Safety.WRITE
    write_path_arg = True  # 写目标 = args.path（「自动允许写入」档据此判定目录边界）
    args_model = WriteFileArgs
    last_diff = ""

    def __init__(self, recorder: ChangeRecorder | None = None) -> None:
        self.recorder = recorder

    async def run(self, args: WriteFileArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        check_write_size(args.content, shown)
        if self.recorder is not None:
            self.recorder.record(p)  # 检查点：记下覆盖前的原始内容
        # 覆盖已有文件：沿用它的编码与行尾符；新建文件用 UTF-8 + LF。
        encoding, newline, old = "utf-8", "\n", ""
        if p.exists():
            loaded = _load_for_edit(p, shown)
            encoding, newline = loaded.encoding, loaded.newline
            old = loaded.text
        try:
            write_text_file(p, args.content, encoding, newline)
        except OSError as e:
            raise ToolError("cannot write " + shown + ": " + str(e)) from e
        self.last_diff = make_diff(old, to_lf(args.content), shown)
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
    write_path_arg = True
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
        loaded = _load_for_edit(p, shown)
        content = loaded.text
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
        check_write_size(new_content, shown, previous_len=len(content))
        try:
            write_text_file(p, new_content, loaded.encoding, loaded.newline)
        except OSError as e:
            raise ToolError("cannot write " + shown + ": " + str(e)) from e
        self.last_diff = make_diff(content, new_content, shown)
        replaced = count if args.replace_all else 1
        return f"edited {shown}: {replaced} replacement(s)"


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="目录路径，默认工作目录")


class MoveFileArgs(BaseModel):
    source: str = Field(description="源路径（文件或目录）")
    destination: str = Field(
        description="目标路径。目标已存在且是目录时，源会被移进去（保留原文件名）"
    )
    overwrite: bool = Field(default=False, description="目标已存在同名文件时是否覆盖（默认否，报错）")


class MoveFileTool(Tool):
    name = "move_file"
    description = (
        "移动或重命名文件 / 目录（同一操作：目标不同名即重命名）。"
        "目标已存在的目录会把源移进去；覆盖已有文件需显式 overwrite=true。"
        "本操作会进检查点，可用「撤销本轮改动」回滚。整理文件夹用这个，"
        "不要用 run_command 调 mv / move（那样会绕过权限门且无法回滚）。"
    )
    safety = Safety.WRITE
    write_path_arg = True  # 写目标是 destination：自动允许写入档按它判目录边界
    write_target_arg = "destination"
    guard_path_args = ("source",)  # 源也必须落在工作目录内，否则「移出去」会被自动放行
    args_model = MoveFileArgs

    def __init__(self, recorder: ChangeRecorder | None = None) -> None:
        self.recorder = recorder

    def arg_text(self, input_dict: dict) -> str:
        return f"{input_dict.get('source', '')} -> {input_dict.get('destination', '')}"

    async def run(self, args: MoveFileArgs, ctx: ToolContext) -> str:
        src = resolve_path(ctx, args.source)
        src_shown = rel_path(ctx, src)
        if not src.exists():
            raise ToolError("source not found: " + src_shown)
        dst = resolve_path(ctx, args.destination)
        # 目标是已存在的目录 → 移进去，保留原名（与 mv 语义一致）
        if dst.is_dir():
            dst = dst / src.name
        dst_shown = rel_path(ctx, dst)
        if _is_within(src, dst):
            raise ToolError(
                f"目标 {dst_shown} 在源 {src_shown} 内部，移动会造成自包含递归，已拒绝"
            )
        if dst.exists() and not args.overwrite:
            raise ToolError(
                f"目标已存在：{dst_shown}\n"
                "（如需覆盖请显式传 overwrite=true；不动已有文件时先换个目标名）"
            )
        # 检查点：记下改前状态——移动是「源消失 + 目标出现」两处变化，两边都要记，
        # 回滚时才能同时还原源、清掉目标。目标已有内容时也一并记下（覆盖可回滚）。
        if self.recorder is not None:
            self.recorder.record(src)
            self.recorder.record(dst)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if args.overwrite and dst.exists():
                if dst.is_dir() and not src.is_dir():
                    raise ToolError(f"目标 {dst_shown} 是目录，源是文件，无法覆盖")
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(src), str(dst))
        except OSError as e:
            raise ToolError(f"cannot move {src_shown} → {dst_shown}: {e}") from e
        kind = "目录" if dst.is_dir() else "文件"
        return f"moved {kind} {src_shown} → {dst_shown}（可「撤销本轮改动」回滚）"


class DeleteFileArgs(BaseModel):
    path: str = Field(description="要删除的文件或目录路径")
    recursive: bool = Field(
        default=False, description="删除非空目录时必须显式传 true（默认只删文件与空目录）"
    )


class DeleteFileTool(Tool):
    name = "delete_file"
    description = (
        "删除文件或目录。删非空目录必须显式 recursive=true；路径先看准（用 list_dir / glob）。"
        "本操作会进检查点，可用「撤销本轮改动」恢复；但检查点只保留有限轮次，"
        "重要文件请先确认或备份。删除是破坏性操作，不要为了「清理」批量删除用户文件。"
    )
    safety = Safety.DANGEROUS
    write_path_arg = True
    args_model = DeleteFileArgs

    def __init__(self, recorder: ChangeRecorder | None = None) -> None:
        self.recorder = recorder

    async def run(self, args: DeleteFileArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if not p.exists():
            raise ToolError("path not found: " + shown)
        if p.is_dir():
            try:
                empty = not any(p.iterdir())
            except OSError as e:
                raise ToolError("cannot read " + shown + ": " + str(e)) from e
            if not empty and not args.recursive:
                raise ToolError(
                    f"{shown} 是非空目录。确认要连内容一起删除时，显式传 recursive=true"
                )
        if self.recorder is not None:
            self._record_tree(p)
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError as e:
            raise ToolError("cannot delete " + shown + ": " + str(e)) from e
        return f"deleted {shown}（可「撤销本轮改动」恢复）"

    def _record_tree(self, p: Path) -> None:
        """目录删除：把目录下每个文件都记进检查点，回滚时能整体还原。"""
        if p.is_file():
            self.recorder.record(p)
            return
        for child in sorted(p.rglob("*")):
            if child.is_file():
                self.recorder.record(child)


class MakeDirArgs(BaseModel):
    path: str = Field(description="要创建的目录路径（父目录会自动创建）")


class MakeDirTool(Tool):
    name = "make_dir"
    description = "创建目录（含父目录）。已有同名目录时直接返回提示，不报错。"
    safety = Safety.WRITE
    write_path_arg = True
    args_model = MakeDirArgs

    async def run(self, args: MakeDirArgs, ctx: ToolContext) -> str:
        p = resolve_path(ctx, args.path)
        shown = rel_path(ctx, p)
        if p.is_file():
            raise ToolError(f"{shown} 已存在且是文件，无法建为目录")
        existed = p.is_dir()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ToolError("cannot create " + shown + ": " + str(e)) from e
        return f"{'目录已存在' if existed else 'created directory'} {shown}"


def _is_within(parent: Path, child: Path) -> bool:
    """child 是否就是 parent、或在 parent 内部（用于拦住自包含移动）。"""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


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
