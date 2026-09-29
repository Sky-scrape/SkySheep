"""engine/tools/check_versions.py 的用例（发布清单 §1 版本号一致的机械化校验）。

两层：
- 真实仓库跑一遍 check()，锁住「五处版本号不漂移」这条不变量（README 路线图
  曾漂到 v1.9 而四处仍是 2.1.0，就是这条要堵的）；
- 临时假仓库验证失配 / [未发布] 跳过 / 标记缺失三类行为，不依赖仓库现状。
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location("check_versions", _TOOLS / "check_versions.py")
assert _SPEC and _SPEC.loader
cv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cv)


def _make_repo(
    tmp_path: Path,
    *,
    version: str = "1.2.3",
    changelog_heads: tuple[str, ...] = ("未发布", "1.2.3"),
    readme_marker: str | None = "v{version}（✅ 当前版本）",
    en_marker: str | None = "v{version} (✅ current release)",
) -> Path:
    """搭一个 check_versions 能吃的最小假仓库，返回仓库根。"""
    (tmp_path / "engine" / "src" / "skysheep").mkdir(parents=True)
    (tmp_path / "engine" / "tools").mkdir(parents=True)
    (tmp_path / "engine" / "pyproject.toml").write_text(
        f'[project]\nname = "fake"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "engine" / "src" / "skysheep" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "engine" / "tools" / "installer.iss").write_text(
        f'#define MyAppVersion "{version}"\n', encoding="utf-8"
    )
    changelog = "# Changelog\n\n" + "".join(
        (f"## [{head}]\n\n- 条目\n\n" if head == "未发布" else f"## [{head}] - 2026-01-01\n\n- 条目\n\n")
        for head in changelog_heads
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for name, marker in (("README.md", readme_marker), ("README.en.md", en_marker)):
        line = f"- **{marker.format(version=version)}**：条目\n" if marker else ""
        (tmp_path / name).write_text(f"# T\n\n## 🗺 路线图\n\n{line}", encoding="utf-8")
    return tmp_path


def test_real_repo_versions_consistent():
    """真实仓库：五处版本号必须一致（漂移即红，README 路线图曾停在 v1.9）。"""
    ok, lines = cv.check()
    assert ok, "\n".join(lines)


def test_mismatch_reported(tmp_path):
    root = _make_repo(tmp_path)
    # 改坏一处：pyproject 与其余四处失配
    (root / "engine" / "pyproject.toml").write_text(
        '[project]\nname = "fake"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    ok, lines = cv.check(root)
    assert ok is False
    report = "\n".join(lines)
    assert "9.9.9" in report and "1.2.3" in report
    assert "pyproject" in report


def test_changelog_skips_unreleased(tmp_path):
    # 文件顺序 = Keep a Changelog 惯例：[未发布] 在最上，发布条目新在上、旧在下
    root = _make_repo(tmp_path, changelog_heads=("未发布", "1.2.3", "1.2.2"))
    versions = cv.read_versions(root)
    assert versions["CHANGELOG.md"] == "1.2.3"  # 取最靠前的发布条目，跳过 [未发布] 与更早条目
    ok, _ = cv.check(root)
    assert ok


def test_only_unreleased_fails(tmp_path):
    root = _make_repo(tmp_path, changelog_heads=("未发布",))
    ok, lines = cv.check(root)
    assert ok is False
    assert "CHANGELOG" in "\n".join(lines)


@pytest.mark.parametrize("which", ["zh", "en"])
def test_missing_readme_marker_fails(tmp_path, which):
    root = _make_repo(
        tmp_path,
        readme_marker=None if which == "zh" else "v1.2.3 (✅ current release)",
        en_marker=None if which == "en" else "v1.2.3（✅ 当前版本）",
    )
    ok, lines = cv.check(root)
    assert ok is False
    assert "README" in "\n".join(lines)


def test_subprocess_survives_cp1252_pipe():
    """非 UTF-8 管道下不得因输出编码崩掉（GitHub windows-latest 的真实形态）。

    runner 系统 Python 的 stdout 是管道时按 locale（cp1252）编码，报行里的 ✓/✗
    曾直接 UnicodeEncodeError——版本完全一致也退出码 1。用 PYTHONIOENCODING 强制
    同样的坏环境，断言脚本能跑完且版本一致时退出码为 0。
    """
    env = {
        k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")
    }
    env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, str(_TOOLS / "check_versions.py")],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("utf-8", "replace")
    assert "版本一致" in out and "✓" in out
