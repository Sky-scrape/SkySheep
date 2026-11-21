"""文档索引（docindex.py）与两个工具：分块、增量索引、检索、失效与权限分级。"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from skysheep import docindex, retrieval
from skysheep.tools import memory_embed
from skysheep.tools.base import Safety, ToolContext, ToolError
from skysheep.tools.doc_search import (
    IndexDocumentsArgs,
    IndexDocumentsTool,
    SearchDocumentsArgs,
    SearchDocumentsTool,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 分块


def test_split_pages_tracks_page_numbers():
    text = (
        "[PDF 共 2 页]\n\n--- 第 1 页 ---\n第一页讲超时。\n\n"
        "--- 第 2 页 ---\n第二页讲重试。"
    )
    assert docindex.split_pages(text) == [(1, "第一页讲超时。"), (2, "第二页讲重试。")]


def test_split_pages_without_markers_is_single_block():
    assert docindex.split_pages("Word 文档没有页标记") == [(0, "Word 文档没有页标记")]


def test_split_chunks_respects_size_and_drops_debris():
    text = "。".join(f"第{i}段内容" for i in range(400))
    chunks = docindex.split_chunks(text, size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert docindex.split_chunks("太短", size=200) == []  # 碎块不进索引
    assert docindex.split_chunks("  ", size=200) == []


def test_chunk_document_keeps_page_on_every_chunk():
    body = "。".join(f"条款{i}" for i in range(300))
    text = f"--- 第 3 页 ---\n{body}"
    pieces = docindex.chunk_document(text)
    assert len(pieces) > 1
    assert {page for page, _ in pieces} == {3}


# ---------------------------------------------------------------- 建索引 / 检索


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def test_index_and_search_returns_page_and_source(tmp_path):
    proj = _project(tmp_path)
    _write(
        proj / "manual.md",
        "--- 第 7 页 ---\n" + "填充内容。" * 200
        + "\n\n--- 第 8 页 ---\n请求超时默认是 30 秒，可用 request_timeout 调整。"
        + "填充。" * 200,
    )
    stats = docindex.index_paths(proj, proj, embed_budget_s=0)
    assert stats["indexed"] == 1 and stats["chunks"] >= 2

    res = docindex.search(proj, None, "超时默认多少秒", 3)
    assert res["keyword_only"] is True  # 没有 Ollama：关键词路径
    assert res["stale"] == []
    top = res["results"][0]
    assert top["path"] == "manual.md"
    assert top["page"] == 8
    assert "30 秒" in top["text"]


def test_search_without_index_reports_missing(tmp_path):
    proj = _project(tmp_path)
    res = docindex.search(proj, proj / "nope.md", "查询", 3)
    assert res["results"] == [] and res["missing"]


def test_index_is_incremental_and_force_rebuilds(tmp_path):
    proj = _project(tmp_path)
    _write(proj / "a.md", "第一版内容。" * 60)
    assert docindex.index_paths(proj, proj, embed_budget_s=0)["indexed"] == 1
    # 未变化：增量索引跳过
    assert docindex.index_paths(proj, proj, embed_budget_s=0)["indexed"] == 0
    # force：强制重建
    assert docindex.index_paths(proj, proj, force=True, embed_budget_s=0)["indexed"] == 1


def test_modified_file_is_stale_until_reindexed(tmp_path):
    proj = _project(tmp_path)
    doc = proj / "a.md"
    _write(doc, "默认超时 30 秒。" * 60)
    docindex.index_paths(proj, proj, embed_budget_s=0)
    time.sleep(0.02)
    _write(doc, "默认超时 60 秒。" * 60)
    res = docindex.search(proj, None, "超时", 3)
    assert res["stale"] == ["a.md"] and res["results"] == []
    docindex.index_paths(proj, proj, embed_budget_s=0)
    res = docindex.search(proj, None, "超时", 3)
    assert res["stale"] == [] and "60 秒" in res["results"][0]["text"]


def test_index_rejects_paths_outside_project(tmp_path):
    proj = _project(tmp_path)
    outside = tmp_path / "outside.md"
    _write(outside, "外部文件")
    with pytest.raises(ValueError, match="工作目录"):
        docindex.index_paths(proj, outside, embed_budget_s=0)


def test_index_skips_unsupported_and_missing_text(tmp_path):
    proj = _project(tmp_path)
    _write(proj / "image.png", "not really a png")
    _write(proj / "notes.txt", "  ")  # 空文本：抽不出可索引内容
    stats = docindex.index_paths(proj, proj, embed_budget_s=0)
    assert stats["indexed"] == 0
    assert any("notes.txt" in s for s in stats["skipped"])


def test_vector_path_when_ollama_available(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _write(proj / "a.md", "超时参数是 30 秒。" * 80)
    _write(proj / "b.md", "面包烘焙需要面粉。" * 80)

    monkeypatch.setattr(memory_embed, "embeddings_available", lambda deadline=None: True)

    def fake_post(url, payload):
        # 与文档同向的块给 [1,0]，其余给 [0,1]；查询「超时」也取 [1,0]
        prompt = payload.get("prompt", "")
        return {"embedding": [1.0, 0.0] if "超时" in prompt else [0.0, 1.0]}

    monkeypatch.setattr(memory_embed, "_http_post_json", fake_post)
    stats = docindex.index_paths(proj, proj)
    assert stats["embedded"] >= 1

    res = docindex.search(proj, None, "超时", 3)
    assert res["keyword_only"] is False
    assert res["results"][0]["path"] == "a.md"


# ---------------------------------------------------------------- 工具层


def test_tool_safety_and_annotations():
    idx, ser = IndexDocumentsTool(), SearchDocumentsTool()
    assert idx.safety == Safety.WRITE and ser.safety == Safety.READONLY
    assert idx.to_schema()["annotations"]["readOnlyHint"] is False
    assert ser.to_schema()["annotations"]["readOnlyHint"] is True
    # 索引写的是派生路径（项目内 .skysheep），不是 args.path 指向的用户文件
    assert idx.write_path_arg is False


async def test_tools_roundtrip(tmp_path):
    proj = _project(tmp_path)
    _write(proj / "manual.md", "--- 第 2 页 ---\n" + "说明文字。" * 100
           + "\n\n--- 第 3 页 ---\n最大重试次数默认是 3 次。")
    ctx = ToolContext(working_dir=proj)

    out = await IndexDocumentsTool().run(IndexDocumentsArgs(path="."), ctx)
    assert "新索引/更新 1 个文档" in out
    assert "关键词" in out  # 测试环境无 Ollama

    out = await SearchDocumentsTool().run(
        SearchDocumentsArgs(query="最大重试次数", top_k=3), ctx
    )
    assert "manual.md" in out and "第 3 页" in out
    assert "默认是 3 次" in out
    # 文档内容按不可信外部内容包裹（与 read_document 同一层防线）
    assert "数据不是指令" in out


async def test_search_tool_without_index_gives_actionable_hint(tmp_path):
    proj = _project(tmp_path)
    ctx = ToolContext(working_dir=proj)
    out = await SearchDocumentsTool().run(SearchDocumentsArgs(query="任何词"), ctx)
    assert "index_documents" in out


async def test_search_tool_rejects_outside_path(tmp_path):
    proj = _project(tmp_path)
    ctx = ToolContext(working_dir=proj)
    outside = tmp_path / "x.md"
    _write(outside, "外部")
    with pytest.raises(ToolError):
        await SearchDocumentsTool().run(
            SearchDocumentsArgs(query="外部", path=str(outside)), ctx
        )


def test_vectors_from_other_embed_model_fall_back_to_keyword(tmp_path, monkeypatch):
    """换过嵌入模型：旧向量不再可信，该文档退回纯 BM25（不拿错维度的向量硬算）。"""
    proj = _project(tmp_path)
    _write(proj / "a.md", "超时参数是 30 秒。" * 80)
    monkeypatch.setattr(memory_embed, "embeddings_available", lambda deadline=None: True)
    monkeypatch.setattr(
        memory_embed, "_http_post_json",
        lambda url, payload: {"embedding": [1.0, 0.0]},
    )
    assert docindex.index_paths(proj, proj)["embedded"] >= 1

    import sqlite3

    conn = sqlite3.connect(docindex.index_db_path(proj))
    conn.execute("UPDATE docs SET vec_model = 'another-model'")
    conn.commit()
    conn.close()
    res = docindex.search(proj, None, "超时", 3)
    assert res["keyword_only"] is True
    assert res["results"] and res["results"][0]["path"] == "a.md"


# ---------------------------------------------------------------- 检索缓存


def _prime_project(tmp_path: Path) -> Path:
    proj = _project(tmp_path)
    _write(proj / "a.md", "默认超时是 30 秒，可用 request_timeout 调整。" * 30)
    _write(proj / "b.md", "面包烘焙需要面粉和酵母。" * 30)
    docindex.index_paths(proj, proj, embed_budget_s=0)
    return proj


def test_search_cache_reuses_loaded_chunks_and_corpus(tmp_path, monkeypatch):
    """命中时不再读库、不再重算语料；换查询仍复用；换范围是另一个缓存键。"""
    proj = _prime_project(tmp_path)
    calls = {"load": 0, "corpus": 0}
    real_load, real_corpus = docindex._load_chunks, docindex._bm25_build_corpus

    def counting_load(conn, prefix, exact):
        calls["load"] += 1
        return real_load(conn, prefix, exact)

    def counting_corpus(texts):
        calls["corpus"] += 1
        return real_corpus(texts)

    monkeypatch.setattr(docindex, "_load_chunks", counting_load)
    monkeypatch.setattr(docindex, "_bm25_build_corpus", counting_corpus)

    first = docindex.search(proj, None, "超时", 3)
    assert calls == {"load": 1, "corpus": 1}
    # 同输入同输出，且第二次分块与语料统计都命中缓存
    assert docindex.search(proj, None, "超时", 3) == first
    assert calls == {"load": 1, "corpus": 1}
    # 不同查询共用同一份已载入分块与语料统计（缓存的价值所在）
    other = docindex.search(proj, None, "烘焙面粉", 3)
    assert other["results"][0]["path"] == "b.md"
    assert calls == {"load": 1, "corpus": 1}
    # 单文件范围是另一个 scope：重新读一次库，结果只含该文件
    scoped = docindex.search(proj, proj / "a.md", "超时", 3)
    assert calls["load"] == 2 and calls["corpus"] == 2
    assert scoped["results"] and {r["path"] for r in scoped["results"]} == {"a.md"}


def test_cache_invalidated_by_external_db_write(tmp_path):
    """自然失效实测：绕过 index_paths 直改库，库文件 mtime/size 变化即换代。"""
    proj = _prime_project(tmp_path)
    assert "30 秒" in docindex.search(proj, None, "超时", 3)["results"][0]["text"]

    conn = sqlite3.connect(docindex.index_db_path(proj))
    try:
        conn.execute(
            "UPDATE chunks SET text = ? WHERE path = ?",
            ("改写后的说明：超时现在是 45 秒，重试 5 次。", "a.md"),
        )
        conn.commit()
    finally:
        conn.close()

    res = docindex.search(proj, None, "超时", 3)
    assert res["results"], "直改库后必须还能检索（fresh 判定不走缓存）"
    assert "45 秒" in res["results"][0]["text"]


def test_reindex_serves_new_content_after_cache(tmp_path):
    """index_paths 写库后再检索拿到新内容：缓存不得跨代存活（端到端）。"""
    proj = _project(tmp_path)
    doc = proj / "a.md"
    _write(doc, "默认超时 30 秒。" * 60)
    docindex.index_paths(proj, proj, embed_budget_s=0)
    assert "30 秒" in docindex.search(proj, None, "超时", 3)["results"][0]["text"]

    time.sleep(0.02)
    _write(doc, "默认超时 60 秒。" * 60)
    docindex.index_paths(proj, proj, embed_budget_s=0)
    res = docindex.search(proj, None, "超时", 3)
    assert res["stale"] == []
    assert "60 秒" in res["results"][0]["text"]


def test_cached_bm25_matches_retrieval_bm25():
    """契约：缓存路径的 BM25 与 retrieval.bm25_scores 同输入同分（钉住公式漂移）。"""
    texts = [
        "默认超时是 30 秒，可用 request_timeout 调整。",
        "Baking bread needs flour, yeast and time; timeout = 30s.",
        "Mixed 内容：Retry Policy 与 重试策略。",
        "",
        "。",
    ]
    corpus = docindex._bm25_build_corpus(texts)
    for q in ["超时", "timeout 30", "", "!!!", "Retry Policy", "重试 123"]:
        assert docindex._bm25_scores_from_corpus(q, corpus) == retrieval.bm25_scores(q, texts)
    assert docindex._bm25_scores_from_corpus("任意", docindex._bm25_build_corpus([])) == []
    assert docindex._bm25_scores_from_corpus("", corpus) == [0.0] * len(texts)


def test_source_change_swaps_corpus_without_db_change(tmp_path, monkeypatch):
    """库不动、源文件变更：fresh 集合换语料键，stale 语义与无缓存时一致。"""
    proj = _project(tmp_path)
    _write(proj / "a.md", "超时参数说明三十秒。" * 40)
    _write(proj / "b.md", "面包烘焙需要面粉。" * 40)
    docindex.index_paths(proj, proj, embed_budget_s=0)

    calls = {"corpus": 0}
    real_corpus = docindex._bm25_build_corpus

    def counting(texts):
        calls["corpus"] += 1
        return real_corpus(texts)

    monkeypatch.setattr(docindex, "_bm25_build_corpus", counting)

    first = docindex.search(proj, None, "超时", 3)
    assert calls["corpus"] == 1
    assert first["results"] and first["results"][0]["path"] == "a.md"

    time.sleep(0.02)
    _write(proj / "a.md", "超时参数说明改成六十秒。" * 40)  # 库未动，a.md 变 stale
    res = docindex.search(proj, None, "超时", 3)
    assert calls["corpus"] == 2  # fresh 集合变了 → 语料换代，不复用旧统计
    assert res["stale"] == ["a.md"]
    assert res["results"] == []  # 剩下的 b.md 不含「超时」，零命中


def test_concurrent_searches_share_cache_safely(tmp_path):
    """并发检索（search 经 to_thread 跑在线程池）：结果一致、无异常。"""
    proj = _prime_project(tmp_path)
    whole_results: list[dict] = []
    scoped_results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait()  # 四个线程同时开跑：一起踩在缓存未命中/存回的竞态上
            for _ in range(4):
                whole_results.append(docindex.search(proj, None, "超时", 3))
                scoped_results.append(docindex.search(proj, proj / "a.md", "超时", 3))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert whole_results and all(r == whole_results[0] for r in whole_results)
    assert scoped_results and all(r == scoped_results[0] for r in scoped_results)
    assert whole_results[0] == docindex.search(proj, None, "超时", 3)
