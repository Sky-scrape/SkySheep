"""记忆写入时的冲突标注（B2）：新条目与现有条目高度相似时提示核对。

写入时检测而不只在定期整理时消解：写入当下模型还知道来龙去脉，提示它
顺手删旧条目成本最低。纯 difflib 机械比对，判定交给提示，不拦截写入。
"""

from __future__ import annotations

import pytest

from skysheep.tools import memory as memmod
from skysheep.tools.base import ToolContext
from skysheep.tools.memory import (
    MemoryWriteArgs,
    MemoryWriteTool,
    _conflicting_entries,
)


@pytest.fixture
def mem_file(tmp_path, monkeypatch) -> object:
    """把 memory_path 指到临时文件（绝不碰真实 ~/.skysheep）。"""
    p = tmp_path / "memory.md"
    monkeypatch.setattr(memmod, "memory_path", lambda: p)
    return p


def _tool() -> MemoryWriteTool:
    return MemoryWriteTool()


async def test_similar_entry_flags_conflict(mem_file):
    mem_file.write_text(
        "- [2026-01-01] 用户的项目在 D:\\Work，构建脚本用 uv，主语言是 Python\n",
        encoding="utf-8",
    )
    out = await _tool().run(
        MemoryWriteArgs(action="append", content="用户的项目在 D:\\Work，构建脚本用 uv，主语言是 Python3"),
        ToolContext(working_dir=None),
    )
    assert "remembered" in out
    assert "⚠" in out and "主语言是 Python" in out  # 旧条目被点名
    body = mem_file.read_text(encoding="utf-8")
    assert "Python3" in body  # 新信息照常落盘（不拦截写入）


async def test_dissimilar_entry_no_flag(mem_file):
    mem_file.write_text(
        "- [2026-01-01] 用户喜欢在周末登山\n", encoding="utf-8"
    )
    out = await _tool().run(
        MemoryWriteArgs(action="append", content="用户的代码仓库托管在 GitHub"),
        ToolContext(working_dir=None),
    )
    assert "remembered" in out and "⚠" not in out


async def test_exact_duplicate_still_short_circuits(mem_file):
    mem_file.write_text(
        "- [2026-01-01] 用户喜欢在周末登山\n", encoding="utf-8"
    )
    out = await _tool().run(
        MemoryWriteArgs(action="append", content="用户喜欢在周末登山"),
        ToolContext(working_dir=None),
    )
    assert "already remembered" in out and "⚠" not in out


def test_conflicting_entries_threshold_and_cap():
    lines = [
        "- [2026-01-01] 用户的项目在 D:\\Work，主语言是 Python",
        "- [2026-02-02] 用户喜欢在周末登山",
    ]
    hits = _conflicting_entries("用户的项目在 D:\\Work，主语言是 Python3", lines)
    assert len(hits) == 1 and "Python" in hits[0]
    assert _conflicting_entries("今天天气不错适合出门散步", lines) == []


def test_conflicting_entries_ignores_short_input():
    """过短输入（<4 字符核心）不比对——一句「喜欢茶」拍在长条目上全是噪声。"""
    assert _conflicting_entries("喝茶", ["- [2026-01-01] 用户喜欢喝茶"]) == []
