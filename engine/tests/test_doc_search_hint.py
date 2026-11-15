"""search_documents 零命中路由提示（C2，Q7 的轻量落地）：索引在但没查到时
给换路提示，不让模型在「换词重试」上空转——该联网的联网、该通读的通读。
"""

from __future__ import annotations

from pathlib import Path

from skysheep import docindex
from skysheep.tools.base import ToolContext
from skysheep.tools.doc_search import SearchDocumentsArgs, SearchDocumentsTool


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(working_dir=tmp_path)


async def test_zero_hits_with_index_gives_routing_hint(tmp_path, monkeypatch):
    def fake_search(*_a, **_kw):  # 同步函数：真实 docindex.search 走 to_thread
        return {"results": [], "missing": [], "stale": [], "keyword_only": False}

    monkeypatch.setattr(docindex, "search", fake_search)
    monkeypatch.setattr(docindex, "index_summary", lambda _d: {"docs": 3})
    out = await SearchDocumentsTool().run(
        SearchDocumentsArgs(query="超时参数默认值"), _ctx(tmp_path)
    )
    assert "没有命中" in out
    assert "web_search" in out and "read_document" in out


async def test_zero_hits_without_index_still_points_to_indexing(tmp_path, monkeypatch):
    def fake_search(*_a, **_kw):
        return {"results": [], "missing": [], "stale": [], "keyword_only": False}

    monkeypatch.setattr(docindex, "search", fake_search)
    monkeypatch.setattr(docindex, "index_summary", lambda _d: {"docs": 0})
    out = await SearchDocumentsTool().run(
        SearchDocumentsArgs(query="超时参数默认值"), _ctx(tmp_path)
    )
    assert "还没有文档索引" in out
    assert "index_documents" in out and "web_search" not in out.split("还没有文档索引")[0]
