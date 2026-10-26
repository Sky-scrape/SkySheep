"""tests/ 与 evals/ 共用的用户数据隔离护栏。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_skysheep_home(tmp_path, monkeypatch) -> Path:
    """默认隔离到临时目录；显式夹具仍可覆盖环境变量以测试路径解析。"""
    home = tmp_path / "home"
    monkeypatch.setenv("SKYSHEEP_HOME", str(home))
    return home
