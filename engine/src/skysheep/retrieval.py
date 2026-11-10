"""零依赖检索打分：分词 + BM25 + 与向量分数的混合归并。

两个消费方共用同一套实现，避免两处各写一份「差不多」的打分：

- ``docindex``：项目内文档索引的关键词检索（没有 Ollama 也能用）；
- ``tools/memory_embed``：记忆检索的混合排序（BM25 + 嵌入余弦加权，
  本机没有 Ollama 时记忆侧仍走 ``tools/memory.py`` 的一期规则评分）。

为什么自带 BM25 而不引第三方库：项目的依赖面刻意收窄（桌面分发、离线可用），
BM25 的全部逻辑不到 40 行；中文按二元组切分（与记忆检索的既有口径一致），
英文/数字按词切分并统一小写。

口径说明（改排序前先读）：
- 空查询、空文档一律返回全 0 分，不抛异常；
- 归一化按「本批最大分」缩放到 0~1（不是 Z-score）：调用方要的是可加权、
  可解释的相对分，不是统计显著度；
- 同分稳定按原下标升序，保证同一输入两次跑出同一顺序（回归可断言）。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# BM25 常用缺省（k1 控制词频饱和，b 控制文档长度惩罚）
BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(text: str) -> list[str]:
    """检索用词元序列（**含重复**，供 BM25 统计词频）。

    与 ``tools/memory.py`` 的 ``_relevance_tokens`` 同一分词口径，只是
    返回 list 而非 set：ASCII 词转小写，孤立单字母丢弃（噪声），纯数字
    保留（版本号有区分度）；中文按二元组切分，单字串退化为该字（否则
    单字查询永远命中不了）。
    """
    out: list[str] = []
    for w in _ASCII_TOKEN_RE.findall(text):
        w = w.lower()
        if len(w) >= 2 or w.isdigit():
            out.append(w)
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def bm25_scores(
    query: str, docs: list[str], k1: float = BM25_K1, b: float = BM25_B,
) -> list[float]:
    """查询对每个文档的 BM25 分（长度与 docs 对齐，顺序不变）。

    idf 用带平滑的 ``log(1 + (N - df + 0.5) / (df + 0.5))``——文档数很少
    （记忆条目、单文档分块）时 df=N 的词仍有正 idf，不会被判成 0 权重。
    """
    if not docs:
        return []
    q_terms = Counter(tokenize(query))
    if not q_terms:
        return [0.0] * len(docs)
    doc_terms = [Counter(tokenize(d)) for d in docs]
    n = len(docs)
    avgdl = sum(sum(c.values()) for c in doc_terms) / n if n else 0.0
    avgdl = avgdl or 1.0
    df = Counter()
    for c in doc_terms:
        df.update(c.keys())
    idf = {
        t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms
    }
    scores: list[float] = []
    for c in doc_terms:
        dl = sum(c.values()) or 1
        s = 0.0
        for t, qtf in q_terms.items():
            tf = c.get(t)
            if not tf:
                continue
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            s += qtf * idf[t] * (tf * (k1 + 1)) / denom
        scores.append(s)
    return scores


def normalize(scores: list[float]) -> list[float]:
    """按本批最大值缩放到 0~1（全 0 / 负分输入返回全 0）。"""
    top = max(scores) if scores else 0.0
    if top <= 0.0:
        return [0.0] * len(scores)
    return [max(0.0, s) / top for s in scores]


def merge_weighted(*parts: tuple[list[float], float]) -> list[float]:
    """加权合并多路**已归一化**分数；长度不一致按最短对齐（防御性截断）。"""
    if not parts:
        return []
    n = min(len(scores) for scores, _ in parts)
    out = [0.0] * n
    for scores, weight in parts:
        for i in range(n):
            out[i] += scores[i] * weight
    return out


def top_indices(
    scores: list[float], limit: int, min_score: float = 0.0,
) -> list[int]:
    """分数降序取前 limit 个下标；同分按原下标升序（稳定）。"""
    if limit <= 0:
        return []
    ranked = sorted(
        ((i, s) for i, s in enumerate(scores) if s > min_score),
        key=lambda p: (-p[1], p[0]),
    )
    return [i for i, _ in ranked[:limit]]
