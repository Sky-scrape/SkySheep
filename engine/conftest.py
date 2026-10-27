"""tests/ 与 evals/ 共用的用户数据隔离护栏 + pytest 测试路径兼容。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_skysheep_home(tmp_path, monkeypatch) -> Path:
    """默认隔离到临时目录；显式夹具仍可覆盖环境变量以测试路径解析。"""
    home = tmp_path / "home"
    monkeypatch.setenv("SKYSHEEP_HOME", str(home))
    return home


def _relativize_engine_prefix(arg: str) -> str:
    """改写「cwd=engine 下不存在、去掉 engine/ 前缀后存在」的测试路径参数。

    CI/脚本容易按仓库根的相对写法在 engine/ 目录里跑
    `pytest engine/tests/test_xxx.py`——参数相对 cwd 解析不到文件，
    pytest 直接报 file or directory not found（exit 4）。只改写这种参数；
    本就存在的路径与真写错的路径原样保留，not found 报错行为不变。
    """
    path_part, sep, node = arg.partition("::")
    p = Path(path_part)
    if p.is_absolute() or p.parts[:1] != ("engine",):
        return arg
    stripped = Path(*p.parts[1:])
    if p.exists() or not stripped.exists():
        return arg
    return str(stripped) + sep + node


def pytest_sessionstart(session) -> None:
    """收集前把仓库根相对写法的测试路径参数改写为 engine 内相对路径。"""
    args = session.config.args
    for i, arg in enumerate(args):
        if isinstance(arg, str) and not arg.startswith("-"):
            args[i] = _relativize_engine_prefix(arg)
