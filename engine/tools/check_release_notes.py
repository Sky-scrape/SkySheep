"""Release 正文自检：把「发出去才发现是占位正文」挡在发布之前。

用法：
    python tools/check_release_notes.py ../docs/release-notes/2.7.0.md --version 2.7.0
    python tools/check_release_notes.py ../docs/release-notes/TEMPLATE-feature.md --template

检查项（真实正文）：
1. 有 H1，且含 v<版本>
2. 含安装包名 ``SkySheep-<版本>-setup.exe``
3. 含 64 位 sha256
4. 含 SmartScreen 说明链接
5. 至少一张图片
6. 没有句子中间的手动换行（GitHub Release 会把单个换行渲染成真实换行）

``--template`` 只校验结构与排版（H1 / 怎么装 / 折叠块 / 图片 / 不折行），
供 ``docs/release-notes/TEMPLATE-*.md`` 使用。

退出码：通过 0；不通过或读不到文件 1（fail closed）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SEMVER = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
_VERSION_IN_H1 = re.compile(rf"v({_SEMVER})")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b", re.IGNORECASE)
_SHA256 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_TABLE = re.compile(r"^\s*\|")
_HTML = re.compile(r"^\s*<")
_THEMATIC = re.compile(r"^\s*([-*_])\1{2,}\s*$")
# 句末标点：行尾落在这些字符上就不算「折行」；冒号算合法收尾（后面常接列表）
_TERMINAL = "。！？…；：.!?;:）」』】》"


def find_hard_wraps(text: str) -> list[int]:
    """返回疑似「句子中间手动换行」的行号（1-based）。

    GitHub 的 Release 正文按 GFM 渲染，段落内单个换行会变成真实换行；
    CHANGELOG 那种为源码阅读而做的硬折行照搬过去就会断句。
    """
    lines = text.splitlines()
    issues: list[int] = []
    in_fence = False
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        if _HEADING.match(line) or _TABLE.match(line) or _THEMATIC.match(line):
            continue
        if _HTML.match(line):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if not nxt:
            continue
        if (
            _FENCE.match(nxt)
            or _HEADING.match(nxt)
            or _LIST.match(nxt)
            or _TABLE.match(nxt)
            or _HTML.match(nxt)
            or _THEMATIC.match(nxt)
        ):
            continue
        if line[-1] not in _TERMINAL:
            issues.append(i + 1)
    return issues


def check_text(text: str, version: str | None, template: bool = False) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    errors: list[str] = []
    lines = text.splitlines()
    h1 = next((ln.strip() for ln in lines if ln.startswith("# ")), "")
    if not h1:
        errors.append("缺一级标题（# SkySheep vX.Y.Z · 主题）")

    if not template:
        if version is None:
            m = _VERSION_IN_H1.search(h1)
            version = m.group(1) if m else None
        if version is None:
            errors.append("标题里读不出版本号，且没有传 --version")
        else:
            if f"v{version}" not in h1:
                errors.append(f"标题里没有 v{version}")
            if f"SkySheep-{version}-setup.exe" not in text:
                errors.append(f"正文没写安装包名 SkySheep-{version}-setup.exe")
            if not _SHA256.search(text):
                errors.append("正文没贴 64 位 sha256")

    if "smartscreen" not in text.lower():
        errors.append("正文没给 SmartScreen 说明链接")
    if not _IMAGE.search(text):
        errors.append("正文没有图片（至少放一张 banner 或功能截图）")
    if "## 怎么装" not in text:
        errors.append("缺「## 怎么装」段")
    if template and "<details>" not in text:
        errors.append("缺工程细节折叠块 <details>")

    for n in find_hard_wraps(text):
        errors.append(f"第 {n} 行像是句子中间的手动换行（Release 页会渲染成真实换行）")
    return errors


def _force_utf8_stdio() -> None:
    """Windows 上接管道时按 locale 编码，✓/✗ 会直接崩；统一重配为 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_release_notes.py",
        description="校验 Release 正文：版本号、安装包名、sha256、图片与折行",
    )
    parser.add_argument("path", help="Release 正文 Markdown 文件")
    parser.add_argument("--version", help="版本号（缺省从 H1 读取）")
    parser.add_argument("--template", action="store_true", help="只校验模板结构与排版")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = parse_args(argv)
    path = Path(args.path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"✗ 读不了 {path}：{exc}")
        return 1
    errors = check_text(text, args.version, args.template)
    if errors:
        print(f"✗ {path} 未通过：")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"✓ {path} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
