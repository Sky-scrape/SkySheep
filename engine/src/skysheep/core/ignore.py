"""项目忽略文件：让 @ 文件索引 / 文件树 / glob / grep 遵循用户的 ignore 规则。

对标三家 CLI 的 .claudeignore / .codexignore / .geminiignore。读取顺序与合并：

    项目/.skysheepignore → 项目/.gitignore（存在才读）→ 内建安全默认

内建默认始终生效：.env / .env.* 不进索引与搜索（防密钥被扫进上下文）。
显式点名（read_file 等）不受影响——与 Claude Code 一致，ignore 只约束
「发现类」操作（glob/grep/@索引/文件树），不设读写禁令。

语法为简化 gitignore：# 注释、空行跳过、`!` 取反（后匹配者优先）、
结尾 `/` 表示仅目录、开头 `/` 或中间含 `/` 表示锚定到项目根（`*` 不跨
路径段）、其余按文件/目录名在任意层级匹配（fnmatch 通配）；目录被忽略
时其下所有内容一并忽略。
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

IGNORE_FILE = ".skysheepignore"
# 内建默认：密钥文件与 git 元数据永远不进「发现」结果
BUILTIN_PATTERNS = (".env", ".env.*", ".git")


class _Rule:
    __slots__ = ("pattern", "dir_only", "anchored", "negated")

    def __init__(self, raw: str) -> None:
        pat = raw.strip()
        self.negated = pat.startswith("!")
        if self.negated:
            pat = pat[1:]
        self.dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        self.anchored = "/" in pat
        self.pattern = pat.lstrip("/")

    def hit(self, rel: str, is_dir: bool) -> bool:
        """rel：posix 相对路径；is_dir=True 时 dir_only 规则才可命中。"""
        if self.dir_only and not is_dir:
            return False
        if self.anchored:
            return fnmatch(rel, self.pattern) or fnmatch(rel, self.pattern + "/*")
        parts = rel.split("/")
        if fnmatch(parts[-1], self.pattern):
            return True
        # 目录名模式：位于命中目录之下的内容一并忽略（node_modules 风格）
        return any(fnmatch(p, self.pattern) for p in parts[:-1])


class IgnoreRules:
    """编译后的忽略规则集；`matches` 语义 = 后匹配的规则覆盖先前的（gitignore 同款）。"""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    @classmethod
    def load(cls, working_dir: Path) -> IgnoreRules:
        lines: list[str] = []
        for name in (IGNORE_FILE, ".gitignore"):
            p = Path(working_dir) / name
            try:
                if p.is_file():
                    lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                continue
        lines.extend(BUILTIN_PATTERNS)
        rules = [_Rule(ln) for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        return cls(rules)

    def matches(self, rel: str, is_dir: bool = False) -> bool:
        """rel 是否被忽略（posix 相对路径；目录可传 is_dir=True 或以 / 结尾）。"""
        rel = rel.replace("\\", "/").strip("/")
        if not rel:
            return False
        is_dir = is_dir or rel.endswith("/")
        rel = rel.rstrip("/")
        ignored = False
        for rule in self._rules:
            if self._rule_hits(rule, rel, is_dir):
                # 后匹配优先：普通规则 = 忽略；`!` 取反规则 = 放行
                ignored = not rule.negated
        return ignored

    def _rule_hits(self, rule: _Rule, rel: str, is_dir: bool) -> bool:
        if rule.hit(rel, is_dir):
            return True
        # 位于命中目录之下的内容一并忽略
        parts = rel.split("/")
        for i in range(1, len(parts)):
            if rule.hit("/".join(parts[:i]), is_dir=True):
                return True
        return False

    def filter_paths(self, paths: list[str]) -> list[str]:
        return [p for p in paths if not self.matches(p)]
