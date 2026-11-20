"""``check_sw_version.py`` 的用例：SW_VERSION 解析、壳文件判定与 0/1 退出。"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location("check_sw_version", _TOOLS / "check_sw_version.py")
assert _SPEC and _SPEC.loader
csv = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = csv
_SPEC.loader.exec_module(csv)

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="需要 git 才能建临时仓库")

_STATIC = Path("engine/src/skysheep/server/static")


def _git(p: Path, *args: str) -> str:
    """在临时仓库里跑 git（check=True：失败即让用例报错）。"""
    proc = subprocess.run(  # noqa: S603 - 参数固定，无 shell
        ["git", *args],
        cwd=p,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def _commit(p: Path, message: str) -> str:
    _git(p, "add", "-A")
    _git(p, "commit", "-m", message)
    return _git(p, "rev-parse", "HEAD").strip()


def _init_repo(p: Path) -> str:
    """建一个最小仓库：壳文件 + 一个引擎文件，返回初始提交 sha（作为 --base）。"""
    _git(p, "init")
    _git(p, "config", "user.email", "t@example.com")
    _git(p, "config", "user.name", "t")
    static = p / _STATIC
    static.mkdir(parents=True)
    (static / "sw.js").write_text(
        'const SW_VERSION = "skysheep-shell-v1";\n', encoding="utf-8"
    )
    (static / "app.js").write_text("// v1\n", encoding="utf-8")
    core = p / "engine/src/skysheep/core"
    core.mkdir(parents=True)
    (core / "agent.py").write_text("# v1\n", encoding="utf-8")
    return _commit(p, "init")


def test_parse_sw_version():
    assert csv.parse_sw_version(
        'const SW_VERSION = "skysheep-shell-v137";'
    ) == "skysheep-shell-v137"
    assert csv.parse_sw_version("const SW_VERSION = 'skysheep-shell-v2';") == "skysheep-shell-v2"
    assert csv.parse_sw_version("// 没有版本号") is None


def test_shell_files_changed():
    assert csv.shell_files_changed("engine/src/skysheep/server/static/app.js\n")
    assert not csv.shell_files_changed("")
    assert not csv.shell_files_changed("engine/src/skysheep/core/agent.py\n")


def test_no_shell_change_is_ok(tmp_path):
    base = _init_repo(tmp_path)
    (tmp_path / "engine/src/skysheep/core/agent.py").write_text("# v2\n", encoding="utf-8")
    _commit(tmp_path, "只改引擎")
    code, lines = csv.check(base, tmp_path)
    assert code == 0
    assert "没有前端壳文件改动" in lines[0]


def test_shell_change_without_version_bump_warns(tmp_path):
    base = _init_repo(tmp_path)
    (tmp_path / _STATIC / "app.js").write_text("// v2\n", encoding="utf-8")
    _commit(tmp_path, "改前端")
    code, lines = csv.check(base, tmp_path)
    assert code == 1
    assert "SW_VERSION 没变" in "\n".join(lines)


def test_shell_change_with_version_bump_is_ok(tmp_path):
    base = _init_repo(tmp_path)
    (tmp_path / _STATIC / "app.js").write_text("// v2\n", encoding="utf-8")
    (tmp_path / _STATIC / "sw.js").write_text(
        'const SW_VERSION = "skysheep-shell-v2";\n', encoding="utf-8"
    )
    _commit(tmp_path, "改前端并升版本")
    code, lines = csv.check(base, tmp_path)
    assert code == 0
    assert "v1" in lines[0] and "v2" in lines[0]


def test_main_exit_codes_default_warn_strict_fail(tmp_path):
    base = _init_repo(tmp_path)
    (tmp_path / _STATIC / "app.js").write_text("// v2\n", encoding="utf-8")
    _commit(tmp_path, "改前端")
    assert csv.main(["--base", base, "--cwd", str(tmp_path)]) == 0
    assert csv.main(["--base", base, "--cwd", str(tmp_path), "--strict"]) == 1
