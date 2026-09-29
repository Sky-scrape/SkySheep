"""版本号一致性自检——发布清单 §1「版本号四处一致」的机械化（2026-09 审查发现 27）。

比对五处版本号是否一致：

1. ``engine/pyproject.toml``           的 ``version``
2. ``engine/src/skysheep/__init__.py`` 的 ``__version__``
3. ``engine/tools/installer.iss``      的 ``MyAppVersion``（决定安装包文件名，漂移即更新链路断裂）
4. ``CHANGELOG.md``                    最新的「## [x.y.z]」发布条目（跳过 [未发布]；基准来源）
5. ``README.md`` / ``README.en.md``    路线图的「✅ 当前版本 / (✅ current release)」标记

文件路径相对本脚本自身解析（脚本固定在 ``engine/tools/`` 下），不依赖工作目录，
CI 与本机均可直接跑：``python tools/check_versions.py``。

退出码：一致 0；不一致或任一来源读不到 / 解析不出 1（fail closed）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 脚本位于 <repo>/engine/tools/：parents[0]=tools、parents[1]=engine、parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 路线图条目形如「**v2.1.0（✅ 当前版本）**」「**v2.1.0 (✅ current release)**」
_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+){1,2})")
# CHANGELOG 发布条目标题「## [2.1.0] - 2026-09-27」；[未发布] 不是版本形态，自然被跳过
_CHANGELOG_HEADING_RE = re.compile(r"(?m)^## \[([^\]]+)\]")
_RELEASE_NAME_RE = re.compile(r"^\d+(?:\.\d+){1,2}$")


def _extract(text: str, pattern: re.Pattern[str], label: str, hint: str) -> str:
    m = pattern.search(text)
    if not m:
        raise ValueError(f"{label} 里找不到版本号（预期形态：{hint}）")
    return m.group(1)


def _read(root: Path, rel: Path, label: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读不了 {label}（{rel}）：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}（{rel}）不是合法 UTF-8：{exc}") from exc


def read_versions(root: Path | None = None) -> dict[str, str]:
    """从仓库根 ``root`` 读取五处版本号，返回 ``{来源: 版本}``；读不到/解析不出抛 ``ValueError``。"""
    root = root or _REPO_ROOT
    return {
        "engine/pyproject.toml": _extract(
            _read(root, Path("engine/pyproject.toml"), "engine/pyproject.toml"),
            re.compile(r'(?m)^version\s*=\s*"([^"]+)"'),
            "engine/pyproject.toml",
            '行首 version = "x.y.z"',
        ),
        "engine/src/skysheep/__init__.py": _extract(
            _read(
                root,
                Path("engine/src/skysheep/__init__.py"),
                "engine/src/skysheep/__init__.py",
            ),
            re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"'),
            "engine/src/skysheep/__init__.py",
            '__version__ = "x.y.z"',
        ),
        "engine/tools/installer.iss": _extract(
            _read(root, Path("engine/tools/installer.iss"), "engine/tools/installer.iss"),
            re.compile(r'(?m)^#define MyAppVersion\s+"([^"]+)"'),
            "engine/tools/installer.iss",
            '#define MyAppVersion "x.y.z"',
        ),
        "CHANGELOG.md": _latest_changelog_release(
            _read(root, Path("CHANGELOG.md"), "CHANGELOG.md")
        ),
        "README.md": _readme_marker_version(
            _read(root, Path("README.md"), "README.md"),
            "✅ 当前版本",
            "README.md",
        ),
        "README.en.md": _readme_marker_version(
            _read(root, Path("README.en.md"), "README.en.md"),
            "✅ current release",
            "README.en.md",
        ),
    }


def _latest_changelog_release(text: str) -> str:
    for m in _CHANGELOG_HEADING_RE.finditer(text):
        name = m.group(1).strip()
        if _RELEASE_NAME_RE.match(name):
            return name
    raise ValueError("CHANGELOG.md 里找不到「## [x.y.z]」发布条目（只有 [未发布]？）")


def _readme_marker_version(text: str, marker: str, label: str) -> str:
    for line in text.splitlines():
        if marker in line:
            m = _VERSION_RE.search(line)
            if not m:
                raise ValueError(f"{label} 路线图的「{marker}」行里解析不出版本号：{line.strip()}")
            return m.group(1)
    raise ValueError(f"{label} 路线图里找不到「{marker}」标记行")


def check(root: Path | None = None) -> tuple[bool, list[str]]:
    """比对全部来源（基准 = CHANGELOG 最新发布条目）。返回 ``(是否一致, 报告行)``。"""
    try:
        versions = read_versions(root)
    except ValueError as exc:
        return False, [f"✗ {exc}"]
    expected = versions["CHANGELOG.md"]
    lines: list[str] = []
    ok = True
    for label, ver in versions.items():
        same = ver == expected
        ok = ok and same
        lines.append(f"{'✓' if same else '✗'} {label}: {ver}")
    lines.append(
        f"{'✓ 版本一致：' if ok else '✗ 版本不一致，基准取 CHANGELOG 最新发布条目：'}{expected}"
    )
    return ok, lines


def _force_utf8_stdio() -> None:
    """把标准流重配为 UTF-8，防「版本一致也红」。

    GitHub windows-latest 的 runner 系统 Python 接管道时按 locale（cp1252）编码 stdout，
    本脚本报行里的 ✓/✗ 会直接 UnicodeEncodeError——比对还没生效进程就异常退出。
    errors="replace" 保证任何环境都不因输出编码崩掉；个别嵌入环境不支持重配时跳过，
    只影响可读性不影响判定。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> int:
    _force_utf8_stdio()
    ok, lines = check()
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
