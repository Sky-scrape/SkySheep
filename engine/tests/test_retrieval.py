"""检索打分（retrieval.py）：分词口径、BM25、归一化与加权合并。"""

from __future__ import annotations

import pytest

from skysheep.retrieval import (
    bm25_scores,
    merge_weighted,
    normalize,
    tokenize,
    top_indices,
)

# ---------------------------------------------------------------- 分词


def test_tokenize_ascii_lowercase_and_noise():
    assert tokenize("Hello World port=8790 x") == ["hello", "world", "port", "8790"]
    # 孤立单字母丢弃；纯数字保留（版本号/端口有区分度）
    assert "a" not in tokenize("a b 1")
    assert "1" in tokenize("a b 1")


def test_tokenize_cjk_bigrams_and_single_char():
    assert tokenize("超时参数") == ["超时", "时参", "参数"]
    # 单字查询退化为该字本身，否则永远命中不了
    assert tokenize("猫") == ["猫"]


def test_tokenize_mixed_and_empty():
    assert tokenize("v2.6.9 修复 gate.py") == ["v2", "6", "9", "gate", "py", "修复"]
    assert tokenize("") == []
    assert tokenize("   ") == []


# ---------------------------------------------------------------- BM25


def test_bm25_ranks_matching_doc_first():
    docs = ["默认超时是 30 秒", "烘焙面包需要面粉", "超时参数可以调整"]
    scores = bm25_scores("超时参数", docs)
    assert scores[0] > 0 and scores[2] > 0
    assert scores[1] == 0
    assert scores[2] > scores[0]  # 两个查询词都命中 > 只命中一个


def test_bm25_empty_inputs():
    assert bm25_scores("查询", []) == []
    assert bm25_scores("", ["文档"]) == [0.0]
    assert bm25_scores("完全无关", ["文档"]) == [0.0]


def test_bm25_rare_term_outweighs_common_term():
    """df 越小 idf 越高：只在一个文档里出现的词比到处都有的词更有区分度。"""
    docs = ["通用词 通用词 通用词 稀有词", "通用词 通用词 通用词 通用词", "通用词 通用词"]
    scores = bm25_scores("稀有词", docs)
    assert scores[0] > 0
    assert scores[1] == scores[2] == 0
    common = bm25_scores("通用词", docs)
    assert common[0] > 0 and common[1] > 0


# ---------------------------------------------------------------- 归一化 / 合并


def test_normalize_scales_to_unit_range():
    assert normalize([0.0, 2.0, 4.0]) == [0.0, 0.5, 1.0]
    assert normalize([0.0, 0.0]) == [0.0, 0.0]
    assert normalize([]) == []


def test_merge_weighted_combines_two_signals():
    merged = merge_weighted(([1.0, 0.0], 0.5), ([0.0, 1.0], 0.5))
    assert merged == pytest.approx([0.5, 0.5])
    # 长度不一致按最短对齐（防御性截断，不越界）
    assert merge_weighted(([1.0, 2.0], 0.5), ([1.0], 0.5)) == [1.0]


def test_top_indices_stable_and_filtered():
    scores = [0.2, 0.9, 0.0, 0.9]
    assert top_indices(scores, 3) == [1, 3, 0]  # 同分按原下标升序
    assert top_indices(scores, 10, min_score=0.5) == [1, 3]
    assert top_indices(scores, 0) == []
    assert top_indices([0.0, 0.0], 5) == []
