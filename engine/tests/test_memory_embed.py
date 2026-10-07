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
    """嵌入可用且缓存暖齐：查询与条目零词元交集（规则全 0 分）也能按余弦选出子集。

    轮首成本护栏（未命中词条超限当轮回落）的新行为：缓存没暖齐时当轮回落
    一期整块注入，warm_vec_cache 预热补齐词条向量后自动切回嵌入排序；标记
    不可用后同样输入回落一期行为（整块兜底）。
    """
    raw = _over_threshold_memory()
    mem_file.write_text(raw, encoding="utf-8")
    query = "物理世界的本质是什么"  # 与任何条目都没有二元组交集 → 规则全 0 分
    mapping = {"ok": V_X, query: V_X}
    for ln in raw.splitlines():
        mapping[ln] = V_X if "量子" in ln else V_OTHER
    _mock_http(monkeypatch, mapping)

    # 缓存没暖齐（20 条全部未命中，超过轮首补嵌上限）：当轮回落一期整块注入
    cold = render_memory_section(query_text=query)
    assert "按相关性注入" not in cold
    assert "无关记忆0" in cold and "量子叠加态" in cold

    # 后台预热（memory.md 变更 / 轮首触发）：全部词条向量进缓存，下轮切嵌入
    assert memory_embed.warm_vec_cache() == 20
    section = render_memory_section(query_text=query)
    assert "已按相关性注入 1 条" in section  # 嵌入把规则找不到的条目选了出来
    assert "用户在研究量子叠加态与观测问题" in section
    assert "无关记忆0" not in section  # 无关条目不占预算

    # 标记不可用 → 同样输入回落规则评分：全 0 分整块兜底（一期行为）
    memory_embed._mark_available(False)
    fallback = render_memory_section(query_text=query)
    assert "按相关性注入" not in fallback
    assert "无关记忆0" in fallback and "量子叠加态" in fallback


# ---------------------------------------------------------------- 轮首防冻结：预算 / 未命中上限 / 后台预热


class _FakeClock:
    """假单调钟：每次读取前进一格（模拟「每个 HTTP 请求各耗时一段墙钟」）。"""

    def __init__(self, start: float = 0.0, step: float = 0.6):
        self.now = start
        self.step = step

    def monotonic(self) -> float:
        now = self.now
        self.now += self.step
        return now


def test_embed_via_http_deadline_checked_before_request(embed_unknown, monkeypatch):
    """墙钟预算在发请求前检查：预算已过零 HTTP（预算约束串行个数，不是单请求超时）。"""
    calls = _mock_http(monkeypatch, {"你好": V_X})
    past = time.monotonic() - 1.0  # 已过期
    with pytest.raises(memory_embed._EmbedBudgetExceeded):
        memory_embed._embed_via_http(["你好"], deadline=past)
    assert calls == []


def test_embed_budget_exceeded_keeps_partial_and_availability(embed_unknown, monkeypatch):
    """预算到点收手：不算服务不可用（不写标记），已拿到的向量逐条留缓存（断点续嵌）。

    对照组：真正的 HTTP 失败仍照旧标记不可用。
    """
    clock = _FakeClock(start=0.0, step=0.6)  # 请求序列各读一次钟：0.0 / 0.6 / 1.2
    monkeypatch.setattr(memory_embed, "time", clock)
    calls = _mock_http(monkeypatch, {"甲": V_X, "乙": V_ALIKE, "丙": V_OTHER})
    out = memory_embed._embedded(["甲", "乙", "丙"], deadline=1.0)
    assert out is None
    assert len(calls) == 2  # 丙没发请求（预算在请求前检查）
    assert memory_embed._state["available"] is None  # 预算耗尽 ≠ 不可用
    assert memory_embed._vec_cache[memory_embed._vec_key("甲")] == V_X
    assert memory_embed._vec_cache[memory_embed._vec_key("乙")] == V_ALIKE

    # 对照：真失败（连接拒绝）→ 标记不可用
    monkeypatch.setattr(memory_embed, "time", _FakeClock())
    monkeypatch.setattr(
        memory_embed, "_http_post_json",
        lambda url, payload: (_ for _ in ()).throw(ConnectionError("refused")),
    )
    assert memory_embed._embedded(["丁"]) is None
    assert memory_embed._state["available"] is False


def test_select_relevant_auto_budget_falls_back_without_unavailable(embed_unknown, monkeypatch):
    """轮首墙钟预算：探测吃掉预算后当轮回落规则评分，且不写「不可用」标记。

    假钟每次读取前进 2s、预算 3s：探测成功刚好花完预算，查询/词条一个都不发。
    """
    monkeypatch.setattr(memory_embed, "time", _FakeClock(step=2.0))
    entries = ["苹果条", "香蕉条"]
    calls = _mock_http(
        monkeypatch,
        {"ok": V_X, "苹果": V_X, "苹果条": V_ALIKE, "香蕉条": V_OTHER},
    )
    assert select_relevant_auto(entries, "苹果", 3) == select_relevant(entries, "苹果", 3)
    assert len(calls) == 1  # 只发了探测
    assert memory_embed._state["available"] is True

    # 时钟正常（不 mock）时同样输入走嵌入排序：预算不再误伤
    monkeypatch.setattr(memory_embed, "time", time)
    assert select_relevant_auto(entries, "苹果", 3) == [0]


def test_rank_falls_back_when_too_many_uncached_entries(embed_unknown, monkeypatch):
    """轮首成本护栏：未命中词条超限当轮回落（零 HTTP），不一次性串行补嵌全量。"""
    n = memory_embed._UNCACHED_TURN_MAX
    entries = [f"条目{i}" for i in range(n + 1)]
    calls = _mock_http(monkeypatch, {})  # 任何 HTTP 都算测试失败：这里根本不该发
    assert rank_by_embeddings(entries, "查询") is None
    assert calls == []

    # 未命中 ≤ 上限：放行嵌入排序（只补嵌未命中的词条）
    for e in entries[:n]:
        memory_embed._vec_cache[memory_embed._vec_key(e)] = V_X
    monkeypatch.setattr(
        memory_embed, "_http_post_json",
        lambda url, payload: {"embedding": list(V_ALIKE)},
    )
    assert rank_by_embeddings(entries, "查询") is not None


def test_warm_vec_cache_fills_and_skips(mem_file, embed_unknown, monkeypatch):
    """预热：读 memory.md 按检索同口径补齐词条向量；没文件/不可用/条目超量直接 0。"""
    # 没有记忆文件：连探测都不做
    _forbid_http(monkeypatch)
    assert memory_embed.warm_vec_cache() == 0

    # 有文件但标记不可用（TTL 内零探测）：零 HTTP
    mem_file.write_text("- [2026-01-01] 苹果条\n- [2026-01-02] 香蕉条\n", encoding="utf-8")
    memory_embed._mark_available(False)
    assert memory_embed.warm_vec_cache() == 0

    # 可用：补齐全量并返回补嵌条数；再预热全部命中 → 0、零额外 HTTP
    mapping = {
        "ok": V_X,
        "- [2026-01-01] 苹果条": V_ALIKE,
        "- [2026-01-02] 香蕉条": V_OTHER,
    }
    calls = _mock_http(monkeypatch, mapping)
    memory_embed._mark_available(True)
    assert memory_embed.warm_vec_cache() == 2
    assert len(calls) == 2
    assert memory_embed.warm_vec_cache() == 0
    assert len(calls) == 2

    # 条目超量：与排序路径同口径放弃（排序本就走规则评分，预热不白做）
    n = memory_embed._EMBED_MAX_ENTRIES
    raw = "\n".join(
        f"- [2026-01-{i % 28 + 1:02d}] 条目{i}" for i in range(n + 1)
    )
    mem_file.write_text(raw, encoding="utf-8")
    assert memory_embed.warm_vec_cache() == 0
    assert len(calls) == 2


def test_schedule_warmup_without_loop_is_noop(embed_unknown, monkeypatch):
    """无运行中的事件循环（纯同步调用方/测试环境）：调度是零开销空操作。"""
    warmed: list[int] = []
    monkeypatch.setattr(memory_embed, "warm_vec_cache", lambda: warmed.append(1))
    memory_embed.schedule_warmup()
    assert warmed == []
    # 单飞标记复位：下次有循环时还能正常调度
    assert memory_embed._warming is False


async def test_schedule_warmup_spawns_background_once(embed_unknown, monkeypatch):
    """有事件循环：预热走后台线程（spawn_bg + to_thread），进行中再调度直接让路。"""
    import asyncio

    from skysheep.bgtasks import pending_count

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    def slow_warm():
        calls.append(1)
        started.set()
        return release.wait(5)  # 挂住预热线程，给「单飞让路」留出观察窗口

    monkeypatch.setattr(memory_embed, "warm_vec_cache", slow_warm)
    memory_embed.schedule_warmup()
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert len(calls) == 1  # 预热已在后台线程开跑
        assert pending_count() >= 1  # spawn_bg 强引用在册
        memory_embed.schedule_warmup()  # 预热进行中再调度：单飞让路，不起第二线程
        assert len(calls) == 1
    finally:
        release.set()
        # 等预热线程收尾、单飞标记复位，不污染同进程的后续用例
        for _ in range(250):
            if not memory_embed._warming:
                break
            await asyncio.sleep(0.02)
        assert memory_embed._warming is False
