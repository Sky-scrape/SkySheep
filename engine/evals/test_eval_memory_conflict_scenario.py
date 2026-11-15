"""评测基线 · 记忆写入冲突标注（真实 memory_write 经权限确认）。

场景 38：memory_write append 与现有条目高度相似的内容——新条目照常落盘，
但工具结果必须点名旧条目提示核对（写入时检测，不再只等定期整理兜底）。
"""

from __future__ import annotations

from evals._harness import finished, make_agent, run_eval_turn
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.tools.memory import memory_path

OLD_ENTRY = "- [2026-01-01] 用户的项目在 D:\\Work，构建脚本用 uv，主语言是 Python"
NEW_CONTENT = "用户的项目在 D:\\Work，构建脚本用 uv，主语言是 Python3"


async def test_eval_memory_write_flags_similar_conflict(home):
    """场景 38（记忆冲突侧）：相似条目 → ⚠ 提示 + 旧条目点名 + 新条目照常落盘。"""
    proj = home / "proj"
    mem = memory_path()
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(OLD_ENTRY + "\n", encoding="utf-8")

    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="memory_write",
                      input={"action": "append", "content": NEW_CONTENT})],
        [TextBlock(text="已记住。")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(
        agent, "记住：用户的项目在 D:\\Work，构建脚本用 uv，主语言是 Python3",
        decide=lambda ev: "allow_once",  # memory_write 落引擎主目录，逐次确认
    )
    fin = finished(events)["t1"]
    assert not fin.is_error
    assert "remembered" in fin.preview
    assert "⚠" in fin.preview and "主语言是 Python" in fin.preview, (
        "相似旧条目必须在结果里被点名（写入时冲突标注）"
    )
    body = mem.read_text(encoding="utf-8")
    assert "Python3" in body, "新信息照常落盘（标注不拦截写入）"
    assert OLD_ENTRY in body, "旧条目不动，删不删由模型/用户决定"


async def test_eval_memory_write_dissimilar_no_flag(home):
    """对照侧：不相似的正常写入不带 ⚠（提示只在真相似时出现，不做噪音）。"""
    proj = home / "proj2"
    mem = memory_path()
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text("- [2026-01-01] 用户喜欢在周末登山\n", encoding="utf-8")

    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="memory_write",
                      input={"action": "append", "content": "用户的代码仓库托管在 GitHub"})],
        [TextBlock(text="已记住。")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(
        agent, "记住：用户的代码仓库托管在 GitHub",
        decide=lambda ev: "allow_once",
    )
    fin = finished(events)["t1"]
    assert "remembered" in fin.preview and "⚠" not in fin.preview
