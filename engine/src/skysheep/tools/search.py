"""内容搜索工具：正则 grep（内置纯 Python 实现；本机有 ripgrep 时可选加速）。

加速仅在「config [search] use_ripgrep 开（默认开）+ 本机 PATH 上有 rg +
模式为纯 ASCII」时启用，其余一切情况走内置实现，缺省语义零变化。

rg 路径与内置实现的已知取舍（如实记录）：
- GBK 等非 UTF-8 文件里的 ASCII 模式按字节匹配：GBK 对 ASCII 兼容、通常
  能命中，但个别双字节序列的尾字节恰好等于 ASCII 字节时存在误报可能；
  rg 输出按 UTF-8 解码，命中行里的非 ASCII（GBK 中文）内容会显示为替换
  字符（内置路径经 textio 解码能正确显示）。
- 忽略规则是映射对齐而非完全一致：SKIP_DIRS、``.git*`` 目录前缀与内建
  ``.env`` / ``.env.*`` 按内置行为映射成 --glob，working_dir 的
  .skysheepignore 经 --ignore-file 传入，.gitignore 由 rg 自行读取；
  嵌套 ignore 文件、负模式等边角与 IgnoreRules 有差异。
- 二进制文件靠 rg 自身的 NUL 探测抑制，没有内置路径「按扩展名跳过」那层
  （无 NUL 的假二进制文件 rg 会搜）。
- 文件恰好有 MAX_PER_FILE 条匹配时 rg 路径也会加 omitted 注记：
  --max-count 截断后无从区分「恰好 N 条」与「更多」，内置路径只在超过时注记。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from ..textio import decode_bytes
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
# rg 子进程超时（秒）：超时视为 rg 不可用，回退内置实现
RG_TIMEOUT = 30


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
        if ctx.use_ripgrep and args.pattern.isascii():
            # rg 可选加速：仅纯 ASCII 模式走 rg（非 ASCII 模式必须保留
            # textio 的 GB18030 解码语义，直接走内置路径）。返回 None =
            # rg 不可用或执行失败，回退内置实现，缺省语义零变化。
            rg_out = await asyncio.to_thread(
                self._scan_rg, args.pattern, base, ctx, args.include
            )
            if rg_out is not None:
                return rg_out
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
    def _scan_rg(pattern: str, base: Path, ctx: ToolContext, include: str) -> str | None:
        """ripgrep 加速扫描；不可用或失败返回 None（调用方回退内置实现）。

        回退规则：rg 不在 PATH（shutil.which）；subprocess 抛 OSError /
        TimeoutExpired（超时 RG_TIMEOUT 秒）；退出码 2（含 Rust 正则方言
        解析失败——lookahead 等 Python 正则特性 rg 不支持）。退出码 0（有
        匹配）/ 1（无匹配）都算成功。子进程直接跑（不经 shell），cwd=base
        所在目录、target 为 "." 或文件名。二进制文件由 rg 自身的 NUL 探测
        抑制（输出里的杂行在 _format_rg_output 按格式不符跳过）。
        """
        rg = shutil.which("rg")
        if rg is None:
            return None
        cmd: list[str] = [
            rg,
            "--line-number", "--no-heading", "--color", "never",
            "--no-config", "--no-require-git", "--hidden",
            "--max-filesize", str(MAX_FILE_SIZE),
            "--max-count", str(MAX_PER_FILE),
        ]
        # 忽略规则映射对齐（见模块 docstring 的取舍说明）：SKIP_DIRS 逐个
        # 映射；内置路径按 startswith(".git") 剪枝（.github 等一并跳过）、
        # 内建默认忽略 .env / .env.*，这里一并映射保持两路结果一致
        for d in sorted(SKIP_DIRS):
            cmd += ["--glob", f"!**/{d}/**"]
        cmd += [
            "--glob", "!.git",
            "--glob", "!**/.git/**",
            "--glob", "!**/.git*/**",
            "--glob", "!**/.env",
            "--glob", "!**/.env.*",
        ]
        if include:
            cmd += ["--glob", include]
        if ctx.working_dir is not None:
            ignore_file = Path(ctx.working_dir) / ".skysheepignore"
            if ignore_file.is_file():
                cmd += ["--ignore-file", str(ignore_file)]
        if base.is_dir():
            cwd, target = base, "."
        else:
            cwd, target = base.parent, base.name
        # -e 隔离模式本身：前导连字符的模式（如 "-dash"）不会被吃成参数
        cmd += ["-e", pattern, "--", target]
        try:
            proc = subprocess.run(  # noqa: S603 - exe 来自 shutil.which("rg")，参数为列表不经 shell
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",  # rg 输出是 UTF-8；不指定会用 Windows 本地编码造成二次乱码
                errors="replace",
                timeout=RG_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in (0, 1):
            return None
        return GrepTool._format_rg_output(proc.stdout, base, ctx)

    @staticmethod
    def _format_rg_output(stdout: str, base: Path, ctx: ToolContext) -> str:
        """把 rg 输出后处理成与内置 _scan 相同的「路径:行号:内容」格式。

        rg 的相对路径（相对 cwd=base）反斜杠转正斜杠、剥掉 target="." 时的
        "./" 前缀，再拼上 rel_path 前缀（base 即工作目录时前缀为空）；逐行
        split(":", 2) 解析，不足三段的杂行跳过；content strip()[:300]；每文件
        MAX_PER_FILE 条后加一次 omitted 注记；总量 MAX_MATCHES 后加 stopped
        注记；空结果返回 "(no matches)"。
        """
        prefix = rel_path(ctx, base).replace("\\", "/")
        if prefix == ".":
            prefix = ""
        total = 0
        out: list[str] = []
        per_file = 0
        cur_file: str | None = None
        for line in stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue  # 二进制提示等杂行：不足「路径:行号:内容」三段，跳过
            fpath, lineno, content = parts
            fpath = fpath.replace("\\", "/")
            if fpath.startswith("./"):
                fpath = fpath[2:]  # target 为 "." 时 rg 输出带 ./ 前缀，剥掉与内置一致
            full = f"{prefix}/{fpath}" if prefix else fpath
            if full != cur_file:
                cur_file = full
                per_file = 0
            out.append(f"{full}:{lineno}:{content.strip()[:300]}")
            per_file += 1
            total += 1
            if total >= MAX_MATCHES:
                out.append(f"... stopped at {MAX_MATCHES} matches")
                break
            if per_file >= MAX_PER_FILE:
                # rg --max-count 已按每文件上限截断，无从区分「恰好 N 条」与
                # 「更多」：到量即注记（恰好 N 条的文件也会带，见模块 docstring）
                out.append(f"{full}: ... more matches omitted")
        if not out:
            return "(no matches)"
        return "\n".join(out)

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
                raw = f.read_bytes()
            except OSError:
                continue
            # 按 textio 的编码探测解码（UTF-8 → GB18030，round-trip 校验）：
            # 旧实现固定按 UTF-8 读，GBK 中文文件整片漏检（读出来全是替换字符，
            # 匹配不上任何中文模式）——安全审查低危项。二进制在这里被跳过，
            # 解不出编码的文件退回替换字符（至少 ASCII 部分仍可搜到）。
            loaded = decode_bytes(raw)
            if loaded.binary:
                continue
            text = loaded.text
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
