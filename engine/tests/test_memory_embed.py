"""记忆检索嵌入增强（记忆二期 B）：Ollama 余弦排序、不可用回落规则评分。

HTTP 全部 mock（不打真网络）；conftest 的 autouse 夹具已把嵌入默认标成
「不可用」，嵌入路径的用例先重置模块状态（embed_unknown）再 mock HTTP。
回落路径与一期规则评分逐字节一致、零额外 HTTP。
"""

from __future__ import annotations

import time

import pytest

from skysheep.tools import memory_embed
from skysheep.tools.memory import render_memory_section, select_relevant
from skysheep.tools.memory_embed import (
    OLLAMA_BASE_URL,
    OLLAMA_EMBED_MODEL,
    UNAVAILABLE_TTL_S,
    cosine_similarity,
    rank_by_embeddings,
    select_relevant_auto,
)

V_X = [1.0, 0.0]        # 查询方向的基准向量
V_ALIKE = [0.99, 0.14]  # 与查询高度同向（cos ≈ 0.99）
V_MID = [0.7, 0.71]     # 中等相关（cos ≈ 0.71）
V_OTHER = [0.0, 1.0]    # 正交（cos = 0，低于 EMBED_MIN_COSINE，不相关）


@pytest.fixture
def embed_unknown(monkeypatch):
    """把探测缓存重置成「未探测」、向量缓存清空（覆盖 conftest 的默认不可用）。"""
    monkeypatch.setattr(memory_embed, "_state", {"available": None, "checked_at": 0.0})
    monkeypatch.setattr(memory_embed, "_vec_cache", {})


@pytest.fixture
def mem_file(home):
    """全局记忆文件路径（与 test_memory_retrieval.py 同款，SKYSHEEP_HOME 已隔离）。"""
    p = home / "home" / "memory.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _mock_http(monkeypatch, mapping):
    """把 _http_post_json 换成查表假实现，返回记录调用参数的 calls 列表。"""
    calls: list[tuple[str, dict]] = []

    def fake_post(url, payload):
        calls.append((url, payload))
        vec = mapping.get(payload.get("prompt"))
        if vec is None:
            raise RuntimeError(f"unexpected prompt: {payload.get('prompt')!r}")
        return {"embedding": list(vec)}

    monkeypatch.setattr(memory_embed, "_http_post_json", fake_post)
    return calls


def _forbid_http(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("不可用标记存在时不应发起任何 HTTP")

    monkeypatch.setattr(memory_embed, "_http_post_json", boom)


# ---------------------------------------------------------------- 余弦与请求形状


def test_cosine_similarity():
    assert cosine_similarity([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine_similarity([0, 0], [1, 0]) == 0.0  # 零向量不除零
    with pytest.raises(ValueError):
        cosine_similarity([1, 0], [1, 0, 0])  # 维度不一致


def test_embed_via_http_request_shape(embed_unknown, monkeypatch):
    """/api/embeddings 请求：默认地址、模型名常量、prompt 传原文；向量解析成 float。"""
    calls = _mock_http(monkeypatch, {"你好": V_X})
    vecs = memory_embed._embed_via_http(["你好"])
    assert vecs == [V_X]
    (url, payload), = calls
    assert url == f"{OLLAMA_BASE_URL}/api/embeddings"
    assert payload["model"] == OLLAMA_EMBED_MODEL
    assert payload["prompt"] == "你好"

    # 响应异常（空/缺向量）：抛错 → 调用方回落
    monkeypatch.setattr(
        memory_embed, "_http_post_json", lambda url, payload: {"embedding": []}
    )
    with pytest.raises(ValueError):
        memory_embed._embed_via_http(["你好"])


# ---------------------------------------------------------------- 余弦排序


def test_rank_by_embeddings_orders_and_cutoffs(embed_unknown, monkeypatch):
    """按余弦降序；低于 EMBED_MIN_COSINE 的条目不返回（对应规则评分「0 分不返回」）。"""
    mapping = {"查询": V_X, "苹果条": V_ALIKE, "香蕉条": V_OTHER, "苹果苹果条": V_MID}
    _mock_http(monkeypatch, mapping)
    entries = ["苹果条", "香蕉条", "苹果苹果条"]
    # cos: 条目0 ≈0.99、条目2 ≈0.71、条目1 = 0 → [0, 2]
    assert rank_by_embeddings(entries, "查询") == [0, 2]
    assert rank_by_embeddings([], "查询") == []
    assert rank_by_embeddings(entries, "  ") == []

    # 维度不一致：宁可回落也不给错排
    monkeypatch.setattr(
        memory_embed, "_http_post_json",
        lambda url, payload: {"embedding": [1.0, 0.0, 0.0]},
    )
    assert rank_by_embeddings(["条目"], "查询") is None


def test_embed_vec_cache_reuses_http(embed_unknown, monkeypatch):
    """词条向量按文本哈希缓存：同批文本第二次排序零 HTTP。"""
    calls = _mock_http(monkeypatch, {"查询": V_X, "苹果条": V_ALIKE, "香蕉条": V_OTHER})
    entries = ["苹果条", "香蕉条"]
    assert rank_by_embeddings(entries, "查询") == [0]
    first_calls = len(calls)
    assert first_calls == 3  # 查询 + 2 个未命中词条
    assert rank_by_embeddings(entries, "查询") == [0]
    assert len(calls) == first_calls  # 全部命中缓存，零额外 HTTP


# ---------------------------------------------------------------- 回落与可用性缓存


def test_select_relevant_auto_unavailable_matches_rule(monkeypatch):
    """嵌入不可用（conftest 默认态）：与一期 select_relevant 逐项一致、零 HTTP。"""
    _forbid_http(monkeypatch)
    entries = ["- 苹果 香蕉", "- 苹果", "- 香蕉 苹果 樱桃", "- 无关条目"]
    for q in ("苹果", "香蕉 樱桃", "量子纠缠薛定谔方程", ""):
        assert select_relevant_auto(entries, q, 3) == select_relevant(entries, q, 3)
    assert select_relevant_auto([], "苹果", 3) == []


def test_unavailable_marker_caches_and_expires(embed_unknown, monkeypatch):
    """首查探测失败 → 标记不可用，TTL 内零 HTTP；过期后允许再探测一次。"""
    http_calls: list = []

    def failing_post(url, payload):
        http_calls.append((url, payload))
        raise ConnectionError("connection refused")

    monkeypatch.setattr(memory_embed, "_http_post_json", failing_post)
    entries = ["- 苹果条", "- 香蕉条"]
    # 首查：探测失败 → 回落规则评分（不抛错）
    assert select_relevant_auto(entries, "苹果", 3) == select_relevant(entries, "苹果", 3)
    assert memory_embed._state["available"] is False
    assert len(http_calls) == 1  # 只探测了一次

    # TTL 内：连探测都不做
    assert select_relevant_auto(entries, "苹果", 3) == select_relevant(entries, "苹果", 3)
    assert len(http_calls) == 1

    # TTL 过期：允许再探测（装好 Ollama 不重启也能用）
    memory_embed._state["checked_at"] = time.monotonic() - (UNAVAILABLE_TTL_S + 1)
    assert select_relevant_auto(entries, "苹果", 3) == select_relevant(entries, "苹果", 3)
    assert len(http_calls) == 2


def test_probe_success_and_available_ranking_differs_from_rule(embed_unknown, monkeypatch):
    """探测成功后按余弦排序；与规则评分的次序可以合法地不同。"""
    _mock_http(monkeypatch, {"ok": V_X, "苹果 手机": V_X,
                             "苹果 苹果条": V_ALIKE, "苹果手机条": V_MID})
    assert memory_embed.embeddings_available() is True
    entries = ["苹果 苹果条", "苹果手机条"]
    # 规则评分：条目1 命中 苹果/果手/手机 3 个词元 > 条目0 的 1 个 → [1, 0]
    assert select_relevant(entries, "苹果 手机", 3) == [1, 0]
    # 嵌入余弦：条目0 ≈0.99 > 条目1 ≈0.71 → [0, 1]
    assert select_relevant_auto(entries, "苹果 手机", 3) == [0, 1]


# ---------------------------------------------------------------- render 集成


def _over_threshold_memory() -> str:
    lines = []
    for i in range(20):
        if i == 7:
            lines.append("- [2026-01-08] 用户在研究量子叠加态与观测问题")
        else:
            lines.append(f"- [2026-01-{i + 1:02d}] 无关记忆{i} " + "其他内容" * 20)
    raw = "\n".join(lines)
    assert len(raw) > 1600  # 超过 MEMORY_RETRIEVAL_THRESHOLD 才走检索分支
    return raw


def test_render_uses_embedding_subset_and_falls_back(mem_file, embed_unknown, monkeypatch):
    """嵌入可用：查询与条目零词元交集（规则全 0 分）也能按余弦选出子集；
    标记不可用后同样输入回落一期行为（整块兜底）。"""
    raw = _over_threshold_memory()
    mem_file.write_text(raw, encoding="utf-8")
    query = "物理世界的本质是什么"  # 与任何条目都没有二元组交集 → 规则全 0 分
    mapping = {"ok": V_X, query: V_X}
    for ln in raw.splitlines():
        mapping[ln] = V_X if "量子" in ln else V_OTHER
    _mock_http(monkeypatch, mapping)

    section = render_memory_section(query_text=query)
    assert "已按相关性注入 1 条" in section  # 嵌入把规则找不到的条目选了出来
    assert "用户在研究量子叠加态与观测问题" in section
    assert "无关记忆0" not in section  # 无关条目不占预算

    # 标记不可用 → 同样输入回落规则评分：全 0 分整块兜底（一期行为）
    memory_embed._mark_available(False)
    fallback = render_memory_section(query_text=query)
    assert "按相关性注入" not in fallback
    assert "无关记忆0" in fallback and "量子叠加态" in fallback
