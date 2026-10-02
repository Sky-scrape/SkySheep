"""记忆检索的嵌入增强（记忆二期）：本机 Ollama 向量 → 余弦排序，不可用即回落。

定位是「可选增强」：一期规则评分（tools/memory.py 的 select_relevant）是
基线行为；本机装了 Ollama 且模型就绪时，检索注入改按嵌入余弦相似度排序
（对同义不同词的中文查询明显更准）。没有 Ollama 时绝不引入报错或延迟劣化：
首次查询探测失败即缓存「本机不可用」标记，TTL 之内连探测都不再做（localhost
拒绝连接是毫秒级，探测本身也不构成劣化）；嵌入路径任何一步失败都原路回落
规则评分，不向调用方抛异常。

接线路径：render_memory_section 的检索分支把 select_relevant 换成
select_relevant_auto（本模块），其余行为逐字节不变。
"""

from __future__ import annotations

import hashlib
import math
import time

import httpx

# 本机 Ollama 默认地址；远程机器/自定义端口改这里（如 http://192.168.1.8:11434）
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
# 嵌入模型名：先 `ollama pull nomic-embed-text`，换模型改这里（重启进程即生效，
# 词条向量缓存按文本哈希存内存，不做跨模型失效）
OLLAMA_EMBED_MODEL = "nomic-embed-text"
EMBED_TIMEOUT_S = 2.0     # 单次请求超时：嵌入是增强，绝不能拖慢轮首的系统提示词组装
UNAVAILABLE_TTL_S = 600.0  # 「本机不可用」标记有效期：过期允许再探测一次（装好 Ollama 不重启也能用）
EMBED_MIN_COSINE = 0.35    # 余弦低于它的条目视为不相关（对应规则评分「0 分不返回」的语义）
_EMBED_MAX_ENTRIES = 200   # 条目数超过它直接走规则评分（首查全量嵌向量的代价失控）
_VEC_CACHE_MAX = 1024      # 词条向量缓存上限：超出整体清空（memory.md 以追加为主，损失可接受）

# 探测缓存：None=还没探测过；False=不可用（TTL 内不再探测）；True=可用
_state: dict = {"available": None, "checked_at": 0.0}
_vec_cache: dict[str, list[float]] = {}  # sha256(条目原文) → 向量（条目几乎不变，跨轮复用）


def _mark_available(ok: bool) -> None:
    _state["available"] = ok
    _state["checked_at"] = time.monotonic()


def reset_state() -> None:
    """清空探测缓存与向量缓存（测试用；进程内正常路径不需要）。"""
    _state["available"] = None
    _state["checked_at"] = 0.0
    _vec_cache.clear()


def embeddings_available() -> bool:
    """本机嵌入是否可用（带缓存；首次/TTL 过期时真的探测一次）。"""
    ok = _state["available"]
    if ok is True:
        return True
    if ok is False and time.monotonic() - _state["checked_at"] < UNAVAILABLE_TTL_S:
        return False  # 已知不可用且未过期：零探测、零延迟
    try:
        _embed_via_http(["ok"])
        _mark_available(True)
        return True
    except Exception:
        _mark_available(False)
        return False


def _http_post_json(url: str, payload: dict) -> dict:
    # trust_env=False：本地地址不走系统代理（与 uptodate/远程模块对 localhost 的处理一致）
    with httpx.Client(timeout=EMBED_TIMEOUT_S, trust_env=False) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _embed_via_http(texts: list[str]) -> list[list[float]]:
    """逐条向 Ollama /api/embeddings 拿向量；任何失败抛异常（调用方回落）。"""
    out: list[list[float]] = []
    for t in texts:
        data = _http_post_json(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            {"model": OLLAMA_EMBED_MODEL, "prompt": t},
        )
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise ValueError("ollama embedding 响应里没有向量")
        out.append([float(x) for x in vec])
    return out


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("向量维度不一致或为空")
    # strict=True：长度已在上面校验相等，运行时永不触发（防静默错位配对）
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _vec_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embedded(texts: list[str]) -> list[list[float]] | None:
    """带词条缓存的批量嵌入：未命中缓存的才走 HTTP；失败返回 None 并标记不可用。"""
    out: list[list[float] | None] = []
    todo: list[str] = []
    for t in texts:
        cached = _vec_cache.get(_vec_key(t))
        if cached is not None:
            out.append(cached)
        else:
            out.append(None)
            todo.append(t)
    if todo:
        try:
            fresh = _embed_via_http(todo)
        except Exception:
            _mark_available(False)
            return None
        it = iter(fresh)
        for i, t in enumerate(texts):
            if out[i] is None:
                vec = next(it)
                if len(_vec_cache) >= _VEC_CACHE_MAX:
                    _vec_cache.clear()
                _vec_cache[_vec_key(t)] = vec
                out[i] = vec
    return out  # type: ignore[return-value]


def rank_by_embeddings(entries: list[str], query_text: str) -> list[int] | None:
    """嵌入排序：返回余弦 ≥ EMBED_MIN_COSINE 的条目下标，按分数降序（同分按下标序）。

    任何失败（HTTP、响应异常、维度不一致、条目超量）返回 None——调用方回落
    规则评分。空入参返回 []（无可选）。
    """
    if not entries or not query_text.strip():
        return []
    if len(entries) > _EMBED_MAX_ENTRIES:
        return None
    vecs = _embedded([query_text, *entries])
    if vecs is None:
        return None
    qvec = vecs[0]
    scored: list[tuple[float, int]] = []
    for i, vec in enumerate(vecs[1:]):
        try:
            sim = cosine_similarity(qvec, vec)
        except ValueError:
            return None
        scored.append((-sim, i))
    scored.sort()
    return [i for neg_sim, i in scored if -neg_sim >= EMBED_MIN_COSINE]


def select_relevant_auto(
    entries: list[str], query_text: str, limit: int,
) -> list[int]:
    """检索注入的统一入口：嵌入可用走余弦排序，不可用与一期 select_relevant 完全一致。

    本函数绝不抛异常、不可用时零额外延迟（见模块 docstring）。惰性导入
    select_relevant：memory → 本模块 → memory 会成环。
    """
    from .memory import select_relevant

    if limit <= 0 or not entries or not query_text.strip():
        return select_relevant(entries, query_text, limit)
    if not embeddings_available():
        return select_relevant(entries, query_text, limit)
    try:
        ranked = rank_by_embeddings(entries, query_text)
    except Exception:
        ranked = None
    if ranked is None:
        return select_relevant(entries, query_text, limit)
    return ranked[:limit]
