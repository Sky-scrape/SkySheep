"""engine 根 conftest 的测试路径兼容：仓库根相对写法在 engine/ 内也能跑。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def _load_root_conftest():
    spec = importlib.util.spec_from_file_location(
        "engine_root_conftest", ENGINE_ROOT / "conftest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_relativize_only_rewrites_missing_engine_prefixed_paths(monkeypatch):
    """仅当原路径不存在且去掉 engine/ 前缀后存在时才改写；其余原样保留。"""
    module = _load_root_conftest()
    monkeypatch.chdir(ENGINE_ROOT)
    rewritten = str(Path("tests/test_roundtable.py"))
    # 仓库根相对写法 → engine 内相对路径
    assert module._relativize_engine_prefix("engine/tests/test_roundtable.py") == rewritten
    # 带节点 id 也支持
    assert (
        module._relativize_engine_prefix("engine/tests/test_roundtable.py::test_x")
        == rewritten + "::test_x"
    )
    # 本就存在的路径 / 真写错的路径 / 选项参数：原样保留（not found 行为不变）
    assert module._relativize_engine_prefix("tests/test_roundtable.py") == "tests/test_roundtable.py"
    assert (
        module._relativize_engine_prefix("engine/tests/no_such_file.py")
        == "engine/tests/no_such_file.py"
    )
    assert module._relativize_engine_prefix("-m") == "-m"


def test_sessionstart_rewrites_config_args(monkeypatch):
    """pytest_sessionstart 就地改写 config.args，选项参数不受影响。"""
    module = _load_root_conftest()
    monkeypatch.chdir(ENGINE_ROOT)
    config = SimpleNamespace(args=["engine/tests/test_roundtable.py", "-m", "not e2e"])
    module.pytest_sessionstart(SimpleNamespace(config=config))
    assert config.args == [str(Path("tests/test_roundtable.py")), "-m", "not e2e"]


def test_repo_root_relative_test_path_collects_from_engine_dir():
    """端到端：cwd=engine 下按仓库根相对写法跑 pytest 不再 file not found。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "engine/tests/test_roundtable.py",
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ENGINE_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "test_roundtable" in proc.stdout
