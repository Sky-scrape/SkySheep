"""记忆检索的嵌入增强（记忆二期）：本机 Ollama 向量 → 余弦排序，不可用即回落。

定位是「可选增强」：一期规则评分（tools/memory.py 的 select_relevant）是
基线行为；本机装了 Ollama 且模型就绪时，检索注入改按嵌入余弦相似度排序
（对同义不同词的中文查询明显更准）。没有 Ollama 时绝不引入报错或延迟劣化：
首次查询探测失败即缓存「本机不可用」标记，TTL 之内连探测都不再做（localhost
拒绝连接是毫秒级，探测本身也不构成劣化）；嵌入路径任何一步失败都原路回落
规则评分，不向调用方抛异常。

性能不变量（轮首防冻结）：嵌入是增强，绝不能拖慢轮首的系统提示词组装——
本模块所有 HTTP 都是同步 httpx，调用方（backend 轮首）必须把组装整体丢进
worker 线程（asyncio.to_thread），事件循环上绝不发起嵌入请求；同步侧另有
两道墙钟护栏：词条向量靠后台预热（schedule_warmup）与逐条落缓存增量补齐，
轮首单次调用补嵌的未命中词条超过 _UNCACHED_TURN_MAX 直接回落一期规则评分
（等缓存暖齐自动切回嵌入排序），探测 + 查询向量 + 少量补嵌共享
_TURN_BUDGET_S 总预算，到点即收手。预算耗尽不算「服务不可用」：
不写不可用标记，已拿到的向量留在缓存里接着用。

接线路径：render_memory_section 的检索分支把 select_relevant 换成
select_relevant_auto（本模块），其余行为逐字节不变。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time

import httpx

# 本机 Ollama 默认地址；远程机器/自定义端口改这里（如 http://192.168.1.8:11434）
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
# 嵌入模型名：先 `ollama pull nomic-embed-text`，换模型改这里（重启进程即生效，
# 词条向量缓存按文本哈希存内存，不做跨模型失效）
OLLAMA_EMBED_MODEL = "nomic-embed-text"
EMBED_TIMEOUT_S = 2.0     # 单次请求超时（总耗时另受各调用方的墙钟预算约束，见下）
UNAVAILABLE_TTL_S = 600.0  # 「本机不可用」标记有效期：过期允许再探测一次（装好 Ollama 不重启也能用）
EMBED_MIN_COSINE = 0.35    # 余弦低于它的条目视为不相关（对应规则评分「0 分不返回」的语义）
_EMBED_MAX_ENTRIES = 200   # 条目数超过它直接走规则评分（首查全量嵌向量的代价失控）
_VEC_CACHE_MAX = 1024      # 词条向量缓存上限：超出整体清空（memory.md 以追加为主，损失可接受）
# 轮首墙钟护栏：单次检索调用最多补嵌的未命中词条数（超出当轮回落规则评分，
# 由后台预热把缓存填齐后自动恢复嵌入排序）+ 探测/查询/补嵌共享的总预算。
# 预算按「逐请求前检查」生效，单请求仍受 EMBED_TIMEOUT_S 约束，故最坏
# 超出预算一个单请求超时——都在 worker 线程里，冻结不了事件循环。
_UNCACHED_TURN_MAX = 8
_TURN_BUDGET_S = 3.0
_WARM_BUDGET_S = 60.0      # 后台预热的总预算（不占轮首，宽裕些；到点收手下轮接着补）

# 探测缓存：None=还没探测过；False=不可用（TTL 内不再探测）；True=可用
_state: dict = {"available": None, "checked_at": 0.0}
_vec_cache: dict[str, list[float]] = {}  # sha256(条目原文) → 向量（条目几乎不变，跨轮复用）

# 后台预热单飞：memory.md 可能连续变更（归档提炼/整理/设置页保存），同一时刻
# 只允许一条预热线程（预热线程与调度方跨线程，用锁做 check-and-set）
_warm_lock = threading.Lock()
_warming = False


class _EmbedBudgetExceeded(TimeoutError):
    """墙钟预算耗尽：不算「服务不可用」（区别于 HTTP 失败），缓存保留已拿到的向量。"""


def _mark_available(ok: bool) -> None:
    _state["available"] = ok
    _state["checked_at"] = time.monotonic()


def reset_state() -> None:
    """清空探测缓存与向量缓存（测试用；进程内正常路径不需要）。"""
    global _warming
    _state["available"] = None
    _state["checked_at"] = 0.0
    _vec_cache.clear()
    with _warm_lock:
        _warming = False


def embeddings_available(deadline: float | None = None) -> bool:
    """本机嵌入是否可用（带缓存；首次/TTL 过期时真的探测一次）。

    deadline 是墙钟预算（time.monotonic() 时刻）：到点未探测按「本轮不可用」
    返回 False，但不写不可用标记——预算耗尽说明本轮时间不够，不是 Ollama 没了。
    """
    ok = _state["available"]
    if ok is True:
        return True
    if ok is False and time.monotonic() - _state["checked_at"] < UNAVAILABLE_TTL_S:
        return False  # 已知不可用且未过期：零探测、零延迟
    try:
        _embed_via_http(["ok"], deadline=deadline)
        _mark_available(True)
        return True
    except _EmbedBudgetExceeded:
        return False
    except Exception:
        _mark_available(False)
        return False


def _http_post_json(url: str, payload: dict) -> dict:
    # trust_env=False：本地地址不走系统代理（与 uptodate/远程模块对 localhost 的处理一致）
    with httpx.Client(timeout=EMBED_TIMEOUT_S, trust_env=False) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _embed_via_http(
    texts: list[str], deadline: float | None = None,
) -> list[list[float]]:
    """逐条向 Ollama /api/embeddings 拿向量；任何失败抛异常（调用方回落）。

    deadline 是总墙钟预算（time.monotonic() 时刻，None 不限时）：每个请求前
    检查，到点抛 _EmbedBudgetExceeded——预算只约束「串行发多少个」，单请求
    仍受 EMBED_TIMEOUT_S 约束，所以最坏超出预算一个单请求超时。
    """
    out: list[list[float]] = []
    for t in texts:
        if deadline is not None and time.monotonic() >= deadline:
            raise _EmbedBudgetExceeded("嵌入墙钟预算耗尽")
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


def _embedded(
    texts: list[str], deadline: float | None = None,
) -> list[list[float]] | None:
    """带词条缓存的批量嵌入：未命中缓存的才走 HTTP；失败返回 None。

    逐条嵌、逐条落缓存（而不是攒一批再写）：预算耗尽 / 中途失败时已拿到的
    向量留在缓存里，下轮接着补（断点续嵌）。预算耗尽（_EmbedBudgetExceeded）
    不算「服务不可用」——不写不可用标记，本轮由调用方回落规则评分；其余
    失败照旧标记不可用。
    """
    out: list[list[float]] = []
    for t in texts:
        cached = _vec_cache.get(_vec_key(t))
        if cached is not None:
            out.append(cached)
            continue
        try:
            vec = _embed_via_http([t], deadline=deadline)[0]
        except _EmbedBudgetExceeded:
            return None
        except Exception:
            _mark_available(False)
            return None
        if len(_vec_cache) >= _VEC_CACHE_MAX:
            _vec_cache.clear()
        _vec_cache[_vec_key(t)] = vec
        out.append(vec)
    return out


def rank_by_embeddings(
    entries: list[str], query_text: str, deadline: float | None = None,
) -> list[int] | None:
    """嵌入排序：返回余弦 ≥ EMBED_MIN_COSINE 的条目下标，按分数降序（同分按下标序）。

    任何失败（HTTP、响应异常、维度不一致、条目超量）返回 None——调用方回落
    规则评分。空入参返回 []（无可选）。

    轮首成本护栏：未命中缓存的条目超过 _UNCACHED_TURN_MAX 时直接返回 None，
    当轮回落规则评分——否则（进程刚启动 / 缓存被清空后的）首次检索要在调用
    线程串行补嵌全量条目（最多 _EMBED_MAX_ENTRIES 次 HTTP）；缓存由后台预热
    （schedule_warmup / warm_vec_cache）补齐，之后这里只剩查询向量一次请求。
    """
    if not entries or not query_text.strip():
        return []
    if len(entries) > _EMBED_MAX_ENTRIES:
        return None
    if sum(1 for e in entries if _vec_cache.get(_vec_key(e)) is None) > _UNCACHED_TURN_MAX:
        return None  # 缓存没暖齐：本轮回落规则评分，预热补齐后自动切回嵌入排序
    vecs = _embedded([query_text, *entries], deadline=deadline)
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

    本函数绝不抛异常、不可用时零额外延迟（见模块 docstring）。探测、查询
    向量与少量补嵌词条共享 _TURN_BUDGET_S 墙钟预算，到点即回落——本函数的
    HTTP 全在调用方的 worker 线程里跑（backend 轮首 asyncio.to_thread），
    预算保证慢 Ollama 也只拖慢本轮几秒，绝不冻结事件循环。惰性导入
    select_relevant：memory → 本模块 → memory 会成环。
    """
    from .memory import select_relevant

    if limit <= 0 or not entries or not query_text.strip():
        return select_relevant(entries, query_text, limit)
    deadline = time.monotonic() + _TURN_BUDGET_S
    if not embeddings_available(deadline=deadline):
        return select_relevant(entries, query_text, limit)
    try:
        ranked = rank_by_embeddings(entries, query_text, deadline=deadline)
    except Exception:
        ranked = None
    if ranked is None:
        return select_relevant(entries, query_text, limit)
    return ranked[:limit]


def warm_vec_cache() -> int:
    """后台预热词条向量缓存（同步阻塞函数，调用方必须丢线程：asyncio.to_thread）。

    读当前 memory.md、按检索路径同一口径切条目，把未命中的向量补进
    _vec_cache，返回本次实际补嵌的词条数。与排序路径同口径的快速放弃：
    没有记忆文件、本机不可用（探测失败，含 TTL 内的零探测缓存）、条目数超
    _EMBED_MAX_ENTRIES（排序本就走规则评分）都直接返回 0、零 HTTP。
    总墙钟预算 _WARM_BUDGET_S，到点收手——已嵌的留下，下次触发接着补。
    """
    try:
        from .memory import memory_path, split_memory_entries

        raw = memory_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    entries = split_memory_entries(raw)
    if not entries or len(entries) > _EMBED_MAX_ENTRIES:
        return 0
    if not embeddings_available():
        return 0
    missing = [e for e in entries if _vec_cache.get(_vec_key(e)) is None]
    if not missing:
        return 0
    _embedded(missing, deadline=time.monotonic() + _WARM_BUDGET_S)
    return sum(1 for e in missing if _vec_cache.get(_vec_key(e)) is not None)


def schedule_warmup() -> None:
    """有事件循环时后台预热词条向量缓存（spawn_bg + asyncio.to_thread）。

    memory.md 变更 / 轮首检索各触发一次；正在预热中直接让路（单飞），没有
    运行中的事件循环（纯同步调用方、测试环境）是零开销空操作。预热是纯
    增强：任何失败静默放弃，绝不影响调用方。
    """
    global _warming
    with _warm_lock:
        if _warming:
            return
        _warming = True
    coro = asyncio.to_thread(_warm_once)
    try:
        from ..bgtasks import spawn_bg  # 惰性导入：本模块被 tools/memory 顶层引用，不倒挂启动依赖

        spawn_bg(coro)
    except RuntimeError:
        coro.close()  # 无事件循环：丢弃（避免「coroutine was never awaited」告警）
        with _warm_lock:
            _warming = False


def _warm_once() -> None:
    try:
        warm_vec_cache()
    except Exception:
        pass  # 预热是纯增强：失败静默（与归档提炼同一姿态），绝不影响主路径
    finally:
        global _warming
        with _warm_lock:
            _warming = False
