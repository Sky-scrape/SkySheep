"""压缩落盘（Spill）与历史召回（search_history）的测试。

这两件事是一套的：压缩把原文从窗口里挪走，必须同时留一条能走回去的路，
否则「摘要丢掉的细节」就等于永久丢失。
"""

from __future__ import annotations

import pytest
from conftest import FakeProvider

from skysheep.core import Agent
from skysheep.core.context import compact_history
from skysheep.messages import Message, TextBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools
from skysheep.tools.base import ToolContext, ToolError
from skysheep.tools.history import SearchHistoryArgs, SearchHistoryTool


def _agent(provider, workdir, session_id="sess-spill"):
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=PermissionGate(),
        working_dir=workdir,
        max_iterations=5,
        session_id=session_id,
    )


async def test_compaction_spills_transcript_and_leaves_pointer(tmp_path):
    """压缩后：原文落在 .skysheep/transcripts/ 下，摘要里有可续读的路径。"""
    provider = FakeProvider([[TextBlock(text="摘要：讨论过部署脚本")]])
    agent = _agent(provider, tmp_path)
    agent.set_system("sys")
    agent.history += [
        Message.user("部署脚本放在 scripts/release.py"),
        Message.assistant([TextBlock(text="记下了，改动在 release.py 第 42 行")]),
        Message.user("报错 ENOENT: /opt/app/conf.yaml"),
        Message.assistant([TextBlock(text="缺配置文件")]),
        Message.user("最后一句"),
    ]
    ev = await compact_history(agent, keep_recent=1)
    assert ev is not None

    files = list((tmp_path / ".skysheep" / "transcripts").glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    # 关键细节必须还在原文里（摘要里可能被模型丢掉，原文不能丢）
    assert "scripts/release.py" in body
    assert "ENOENT: /opt/app/conf.yaml" in body
    assert "[USER]" in body and "[ASSISTANT]" in body

    summary = agent.history[1].text
    assert "摘要：讨论过部署脚本" in summary
    assert ".skysheep/transcripts/" in summary, "摘要必须带可续读的指针"
    assert "search_history" in summary


async def test_compaction_survives_unwritable_workdir(tmp_path, monkeypatch):
    """落盘失败不能拖垮压缩：没有指针也要照常摘要（宁可丢细节，不可丢这一轮）。"""
    provider = FakeProvider([[TextBlock(text="摘要内容")]])
    agent = _agent(provider, tmp_path)
    agent.set_system("sys")
    agent.history += [Message.user("a"), Message.assistant([TextBlock(text="b")]),
                      Message.user("c"), Message.assistant([TextBlock(text="d")])]

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("skysheep.core.context.write_text_atomic", boom)
    ev = await compact_history(agent, keep_recent=1)
    assert ev is not None
    assert "摘要内容" in agent.history[1].text
    assert ".skysheep/transcripts/" not in agent.history[1].text


async def test_search_session_history_returns_multiple_hits(store, tmp_path):
    """单会话召回：同一会话的多条命中都要给（跨会话搜索是每会话一条，语义不同）。"""
    sess = await store.create_session(None, title="召回测试")
    await store.append_message(sess.id, Message.user("先改 scripts/release.py"))
    await store.append_message(sess.id, Message.assistant([TextBlock(text="改好了 release.py")]))
    await store.append_message(sess.id, Message.user("无关的一句"))

    rows = await store.search_session_history(sess.id, "release.py")
    assert len(rows) == 2
    assert {r["role"] for r in rows} == {"user", "assistant"}
    assert all("release.py" in r["text"] for r in rows)

    # JSON 键名里的字样不算命中（SQL 侧 LIKE 会连 JSON 结构一起匹配）
    assert await store.search_session_history(sess.id, "role") == []


async def test_search_history_tool_formats_hits(store, tmp_path):
    sess = await store.create_session(None, title="工具测试")
    await store.append_message(sess.id, Message.user("错误码 E1024 出在 upload.ts"))
    tool = SearchHistoryTool(store)
    ctx = ToolContext(tmp_path, session_id=sess.id)
    out = await tool.run(SearchHistoryArgs(query="E1024"), ctx)
    assert "E1024" in out and "upload.ts" in out
    assert "[user]" in out

    none = await tool.run(SearchHistoryArgs(query="不存在的词"), ctx)
    assert "没有找到" in none


async def test_search_history_tool_requires_session(tmp_path):
    tool = SearchHistoryTool(store=None)
    with pytest.raises(ToolError):
        await tool.run(SearchHistoryArgs(query="x"), ToolContext(tmp_path))
