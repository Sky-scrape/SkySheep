"""评测基线夹具（engine/evals/）。

与 tests/conftest.py 平行：evals 不在 tests/ 目录树内，本文件以
``evals.conftest`` 导入（evals 是包，见 __init__.py），两边互不干扰。
驱动 Agent 循环的辅助函数在 evals/_harness.py（conftest 不能被测试文件
按顶层名安全导入——顶层名 conftest 属于 tests/conftest.py）。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """隔离的 SkySheep home + 临时项目目录（proj/）——与 tests/conftest.py 同款。

    SKYSHEEP_HOME 指向临时目录：memory_write / 任务簿持久化 / 会话库等一切
    引擎主目录读写都落在 tmp，绝不触碰真实 ~/.skysheep。
    """
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    return tmp_path


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """本目录收集到的用例自动打 eval 标记：新场景文件漏写 pytestmark 也不会漏跑。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent
    for item in items:
        if Path(str(item.fspath)).is_relative_to(root):
            item.add_marker(pytest.mark.eval)
