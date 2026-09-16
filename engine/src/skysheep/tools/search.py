"""内容搜索工具：正则 grep（纯 Python 实现，无外部依赖）。"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError, rel_path, resolve_path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".mypy_cache", ".idea", ".vscode",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".avi", ".sqlite", ".db",
}
MAX_FILE_SIZE = 2_000_000
MAX_MATCHES = 200
MAX_PER_FILE = 20


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python 正则表达式")
    path: str = Field(default=".", description="搜索的文件或目录")
    include: str = Field(default="", description="可选的文件名 glob 过滤，如 *.py")


class GrepTool(Tool):
    name = "grep"
    description = (
        "在文件内容中搜索正则表达式，返回 文件:行号:内容。"
        "默认跳过 .git、node_modules 等目录和二进制文件。"
    )
    safety = Safety.READONLY
    args_model = GrepArgs

    async def run(self, args: GrepArgs, ctx: ToolContext) -> str:
        try:
            rx = re.compile(args.pattern)
        except re.error as e:
            raise ToolError("invalid regex: " + str(e)) from e
        base = resolve_path(ctx, args.path)

        if base.is_file():
            files: list[Path] = [base]
        elif base.is_dir():
            files = sorted(base.rglob("*"))
        else:
            raise ToolError("path not found: " + rel_path(ctx, base))

        total = 0
        out: list[str] = []
        # 遵循项目忽略文件（.skysheepignore / .gitignore / .env 内建默认）
        try:
            from ..core.ignore import IgnoreRules

            ignore = IgnoreRules.load(ctx.working_dir)
        except OSError:
            ignore = None
        for f in files:
            if total >= MAX_MATCHES:
                out.append(f"... stopped at {MAX_MATCHES} matches")
                break
            if not f.is_file() or f.suffix.lower() in BINARY_EXT:
                continue
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            if ignore is not None:
                try:
                    rel_norm = f.resolve().relative_to(
                        Path(ctx.working_dir).resolve()
                    ).as_posix()
                except (OSError, ValueError):
                    rel_norm = None
                if rel_norm and ignore.matches(rel_norm):
                    continue
            if args.include and not f.match(args.include):
                continue
            try:
                if f.stat().st_size > MAX_FILE_SIZE:
                    continue
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            per_file = 0
            for i, line in enumerate(text.splitlines(), 1):
                if per_file >= MAX_PER_FILE:
                    out.append(rel_path(ctx, f) + ": ... more matches omitted")
                    break
                if rx.search(line):
                    out.append(f"{rel_path(ctx, f)}:{i}:{line.strip()[:300]}")
                    per_file += 1
                    total += 1
                    if total >= MAX_MATCHES:
                        break
        if not out:
            return "(no matches)"
        return "\n".join(out)
