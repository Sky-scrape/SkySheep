"""内容搜索工具：正则 grep（纯 Python 实现，无外部依赖）。"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
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
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = GrepArgs

    async def run(self, args: GrepArgs, ctx: ToolContext) -> str:
        try:
            rx = re.compile(args.pattern)
        except re.error as e:
            raise ToolError("invalid regex: " + str(e)) from e
        base = resolve_path(ctx, args.path)
        if not base.is_file() and not base.is_dir():
            raise ToolError("path not found: " + rel_path(ctx, base))
        # 遵循项目忽略文件（.skysheepignore / .gitignore / .env 内建默认）
        try:
            from ..core.ignore import IgnoreRules

            ignore = IgnoreRules.load(ctx.working_dir)
        except OSError:
            ignore = None
        # 整个扫描（目录遍历 + 逐文件读取）放线程：大项目根上一次同步扫描
        # 会把事件循环卡住数秒，流式输出与其他会话一起停摆
        return await asyncio.to_thread(self._scan, rx, base, ctx, ignore, args.include)

    @staticmethod
    def _scan(
        rx: re.Pattern,
        base: Path,
        ctx: ToolContext,
        ignore: object | None,
        include: str,
    ) -> str:
        total = 0
        out: list[str] = []
        for f in GrepTool._iter_files(base, ctx, ignore):
            if total >= MAX_MATCHES:
                out.append(f"... stopped at {MAX_MATCHES} matches")
                break
            if f.suffix.lower() in BINARY_EXT:
                continue
            if include and not f.match(include):
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

    @staticmethod
    def _iter_files(base: Path, ctx: ToolContext, ignore: object | None) -> Iterator[Path]:
        """惰性产出待搜索的文件：os.walk 剪枝，跳过的目录根本不进遍历。

        旧实现 sorted(base.rglob("*")) 会先把整棵树（含 .git / node_modules
        的全部条目）物化成列表再逐个过滤，大项目上白列几十万条；剪枝后这些
        子树一次都不进。匹配 MAX_MATCHES 提前 break 时也不会再往下走。
        """
        if base.is_file():
            yield base
            return
        workroot = Path(ctx.working_dir).resolve()

        def rel_norm(p: Path) -> str | None:
            try:
                return p.resolve().relative_to(workroot).as_posix()
            except (OSError, ValueError):
                return None

        for dirpath, dirnames, filenames in os.walk(base):
            kept: list[str] = []
            for d in sorted(dirnames):
                if d in SKIP_DIRS or d.startswith(".git"):
                    continue
                if ignore is not None:
                    rn = rel_norm(Path(dirpath) / d)
                    if rn is not None and ignore.matches(rn, is_dir=True):
                        continue
                kept.append(d)
            dirnames[:] = kept
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if ignore is not None:
                    rn = rel_norm(p)
                    if rn is not None and ignore.matches(rn):
                        continue
                if p.is_file():
                    yield p
