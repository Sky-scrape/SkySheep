"""评测基线 · 任务编排（流水线）行为场景（fake provider 驱动真实后端编排循环）。

三个种子场景，都走真实的 ServerBackend 编排路径（_pipeline_advance →
_run_pipeline_node → HeadlessGate 无人值守执行），出处见各用例 docstring：

9.  DAG 顺序与失败传播：依赖未完成不派跑；上游执行失败，下游按依赖失败
    级联标 error（节点自己没跑过）；整条流水线收尾为 failed；
10. 节点终态推送开关（关）：成功/依赖级联一字不发，节点自身执行失败必推；
11. 节点终态推送开关（开）：每个节点到终态都推，与收尾推送去重（最后一拍
    让位给整体汇总，同一件事不推两遍）。

推送目标用 mock 渠道（最小 duck-type 替身，对应 channels 契约的
enabled/configured/allowed_ids/send_text——与 tests/test_cron.py 的
_FakeChannel 同一套口径），不连任何真实聊天平台。
"""

from __future__ import annotations

import asyncio
import time

from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend

# 依赖失败型错误的固定前缀（automation.AutomationMixin.DEP_FAIL_MARK 的实际值）
DEP_FAIL_MARK = "依赖的节点"


class _RoutedProvider(FakeProvider):
    """按 prompt 关键词路由脚本：节点产出与调度顺序无关（断言确定性）。

    按消息**结尾**命中 routes 关键词 → 返回对应文本；未命中 → 空产出（节点
    按「没有产出」收尾为可重试失败，默认 max_runs=1 时直接 error）。用结尾
    匹配而不是子串：下游节点的 prompt 头部带着上游产出注入（「前置任务
    「做A」的产出…」），子串匹配会把下游错路由到上游的脚本。
    """

    ROUTES: dict[str, str] = {}

    async def stream(self, messages, tool_schemas, effort=None):
        last = messages[-1].text if messages else ""
        hit = next((v for k, v in self.ROUTES.items()
                    if last.rstrip().endswith(k)), "")
        self.scripted = [[TextBlock(text=hit)]] if hit else [[]]
        async for pe in super().stream(messages, tool_schemas, effort):
            yield pe


async def _make_backend(home, provider) -> ServerBackend:
    """组装真实后端（SKYSHEEP_HOME 已隔离）。预置 ui 偏好：关掉更新检查
    （评测不联网）与系统通知（不打扰真实桌面）。"""
    prefs_dir = home / "home"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui.json").write_text('{"update_check": 0, "notify": 0}', encoding="utf-8")
    be = ServerBackend(
        working_dir=home / "proj",
        provider_factory=lambda: provider,
    )
    await be.setup()
    return be


async def _wait_until(pred, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def _wait_pipeline(be, pipe_id: int, wanted, timeout: float = 20.0) -> dict:
    """轮询到流水线进入目标状态，返回最终 pipeline dict。"""
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = await be.store.get_pipeline(pipe_id)
        if last["status"] in wanted:
            return last
        await asyncio.sleep(0.02)
    nodes = [(n["title"], n["status"], (n["last_error"] or "")[:80])
             for n in last["nodes"]] if last else None
    raise AssertionError(
        f"流水线 {pipe_id} 未在 {timeout}s 内进入 {wanted}；"
        f"最后状态={last['status'] if last else None} 节点={nodes}"
    )


class _MockChannel:
    """渠道替身：出站推送路径用到的最小接口（与 tests/test_cron 的 _FakeChannel
    同一套契约：enabled / configured / allowed_ids / send_text）。"""

    name = "feishu"

    def __init__(self, allowed=("owner-1",), *, enabled: bool = True) -> None:
        self.config = {"enabled": enabled, "allowed_ids": list(allowed)}
        self.sent: list[tuple[str, str]] = []

    def configured(self) -> bool:
        return True

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    @property
    def allowed_ids(self) -> set[str]:
        return {str(x) for x in self.config.get("allowed_ids") or []}

    async def send_text(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True


async def test_eval_pipeline_dag_order_and_failure_propagation(home):
    """场景 9（无人值守闭环二期 · 编排语义回归）：DAG 顺序与失败传播。

    用真实后端编排循环跑一条四节点流水线：A（根）→ B（依赖 A）；F（根，
    空产出=执行失败）→ G（依赖 F）。钉住的行为：
    - 依赖未完成不派跑：B 在 A 完成之后才启动（started_at >= A.finished_at）；
    - 上游产出注入下游 prompt（「前置任务」段 + A 的产出正文）；
    - 失败传播：F 自身执行失败（空产出、无重试余量）→ error；G 是**依赖
      失败级联**——last_error 以「依赖的节点」开头、runs=0（一次都没跑过）；
    - 有节点未成功时整条流水线收尾为 failed，而非 done。
    """
    provider = _RoutedProvider([])
    provider.ROUTES = {"做A": "A 的产出", "做B": "B 的产出", "做F": ""}
    be = await _make_backend(home, provider)
    try:
        created = await be.pipeline_create({
            "name": "评测线",
            "nodes": [
                {"title": "做A", "prompt": "做A"},
                {"title": "做B", "prompt": "做B", "after": [0]},
                {"title": "做F", "prompt": "做F"},
                {"title": "下游G", "prompt": "做G", "after": [2]},
            ],
        })
        pipe = created["pipeline"]
        assert pipe["status"] == "draft", "创建出来是草稿，等人启动（不自动跑）"
        a_id, b_id, f_id, g_id = (n["id"] for n in pipe["nodes"])

        await be.pipeline_start({"id": pipe["id"]})
        done = await _wait_pipeline(be, pipe["id"], ("done", "failed", "cancelled"))
        assert done["status"] == "failed", "有节点未成功：收尾必须是 failed"
        by_id = {n["id"]: n for n in done["nodes"]}

        a, b, f, g = by_id[a_id], by_id[b_id], by_id[f_id], by_id[g_id]
        assert a["status"] == "done" and a["result"] == "A 的产出"
        assert b["status"] == "done" and b["result"] == "B 的产出"
        assert b["started_at"] >= a["finished_at"] - 1e-6, (
            "依赖未完成不派跑：B 的启动时刻不得早于 A 的完成时刻"
        )
        # B 的 prompt 带上游产出注入（provider 收到的最后一帧用户消息）
        b_calls = [m for m in provider.calls if m and m[-1].role == "user"
                   and m[-1].text.startswith("你是任务编排流水线中的一个节点")]
        assert b_calls, "B 节点的 prompt 应带「前置任务」注入头"
        blob = b_calls[-1][-1].text
        assert "前置任务" in blob and "A 的产出" in blob, "下游 prompt 注入上游产出"

        assert f["status"] == "error" and "没有产出" in (f["last_error"] or ""), (
            "空产出且无重试余量：F 按自身执行失败收尾"
        )
        assert int(f.get("runs") or 0) == 1
        assert g["status"] == "error", "依赖失败的下游按 error 收尾"
        assert (g["last_error"] or "").startswith(DEP_FAIL_MARK), (
            f"依赖失败型错误的固定前缀：{g['last_error']!r}"
        )
        assert int(g.get("runs") or 0) == 0 and not g.get("started_at"), (
            "级联失败是依赖判定出来的，G 一次都没被派跑"
        )
    finally:
        await be.shutdown()


async def test_eval_pipeline_node_push_switch_off_silent_except_failure(home):
    """场景 10（无人值守闭环二期 · 节点推送开关语义，关）：mock 渠道断言。

    notify_channel=False（默认，防打扰）时：成功节点与依赖级联失败（derived，
    节点自己没跑）一律不推；节点**自身执行失败**必推——无人值守最需要知道的
    正是失败。整体收尾汇总推送维持原行为、不受该开关限制。
    （推送目标用 mock 聊天渠道；通用 Webhook 出站本身由场景 30 以真实
    WebhookChannel + mock 传输覆盖。）
    """
    provider = _RoutedProvider([])
    provider.ROUTES = {"做A": "A 完成", "下游B": "B 完成"}
    be = await _make_backend(home, provider)
    ch = _MockChannel()
    be.channels.channels["feishu"] = ch  # mock 渠道进真实管理器注册表
    try:
        created = await be.pipeline_create({
            "name": "静默线",
            # 下游节点列在依赖之后（after 是同批次序号，前向引用会被丢弃）
            "nodes": [
                {"title": "做A", "prompt": "做A"},
                {"title": "会失败", "prompt": "会失败"},
                {"title": "下游B", "prompt": "下游B", "after": [1]},
            ],
        })
        pipe = created["pipeline"]
        assert pipe["notify_channel"] is False, "开关默认关（防打扰）"
        await be.pipeline_start({"id": pipe["id"]})
        done = await _wait_pipeline(be, pipe["id"], ("done", "failed", "cancelled"))
        assert done["status"] == "failed"
        by_title = {n["title"]: n for n in done["nodes"]}
        assert by_title["会失败"]["status"] == "error"
        assert by_title["下游B"]["status"] == "error", "依赖失败的下游按 error 收尾"
        assert int(by_title["下游B"].get("runs") or 0) == 0, "级联失败没被派跑"
        # 会失败到终态时下游还是 blocked（非全终态）→ 节点行必推，不被收尾去重吞掉
        assert await _wait_until(lambda: len(ch.sent) >= 2), f"应推 2 条，实际 {ch.sent}"
        await asyncio.sleep(0.3)  # 留出「不该发的多发了」的暴露窗口

        node_lines = [t for _chat, t in ch.sent if t.startswith("⚙️")]
        finish = [t for _chat, t in ch.sent if t.startswith(("✅ 流水线", "⚠️ 流水线"))]
        assert len(node_lines) == 1, f"开关关时只有自身执行失败推节点行，实际 {node_lines}"
        assert "会失败" in node_lines[0] and "❌" in node_lines[0]
        assert not any("下游B" in t for t in node_lines), "依赖级联失败（derived）不推节点行"
        assert not any("做A" in t for t in node_lines), "成功节点不推节点行"
        assert finish and all("未成功" in t and "静默线" in t for t in finish), (
            "收尾汇总维持原行为（不受该开关限制）"
        )
        assert len(node_lines) + len(finish) == len(ch.sent), "除节点行与收尾汇总外一字不发"
        assert all(chat == "owner-1" for chat, _t in ch.sent), "推送目标只允许名单 chat"
    finally:
        await be.shutdown()


async def test_eval_pipeline_node_push_switch_on_per_node_with_finish_dedup(home):
    """场景 11（无人值守闭环二期 · 节点推送开关语义，开）：mock 渠道断言。

    notify_channel=True 时每个节点到终态都推一条紧凑行；最后一个到终态的
    节点正好宣告整条流水线结束时**让位**给收尾汇总（去重，不推两遍）。
    结构：A、B 并行，汇总节点依赖两者——汇总完成即收尾，因此节点行只有
    A、B 两条，汇总只有「整体完成」一条。
    """
    provider = _RoutedProvider([])
    provider.ROUTES = {"做A": "A 的产出", "做B": "B 的产出", "汇总": "汇总完成"}
    be = await _make_backend(home, provider)
    ch = _MockChannel(allowed=("owner-1", "owner-2"))  # 两个名单 chat：逐 chat 各发一份
    be.channels.channels["feishu"] = ch
    try:
        created = await be.pipeline_create({
            "name": "推送线", "notify_channel": True,
            "nodes": [
                {"title": "做A", "prompt": "做A"},
                {"title": "做B", "prompt": "做B"},
                {"title": "汇总", "prompt": "汇总", "after": [0, 1]},
            ],
        })
        pipe = created["pipeline"]
        assert pipe["notify_channel"] is True
        await be.pipeline_start({"id": pipe["id"]})
        done = await _wait_pipeline(be, pipe["id"], ("done", "failed", "cancelled"))
        assert done["status"] == "done"
        assert await _wait_until(lambda: len(ch.sent) >= 6), f"应推 3 条 ×2 chat，实际 {ch.sent}"
        await asyncio.sleep(0.3)

        texts = [t for _chat, t in ch.sent]
        # 文案按「条」归并（同一条推给名单里每个 chat 各一份）
        unique = list(dict.fromkeys(texts))
        node_lines = [t for t in unique if t.startswith("⚙️")]
        finish = [t for t in unique if t.startswith(("✅ 流水线", "⚠️ 流水线"))]
        assert len(node_lines) == 2, f"只有先完成的 A/B 推节点行，实际 {node_lines}"
        assert any("做A" in t and "✅" in t and "A 的产出" in t for t in node_lines)
        assert any("做B" in t and "✅" in t for t in node_lines)
        assert not any("「汇总」" in t for t in node_lines), "最后一拍让位给收尾推送（去重）"
        assert len(finish) == 1 and "共 3 个节点" in finish[0] and "✅" in finish[0], (
            "整体收尾推送一条汇总（文案唯一）"
        )
        # 每条文案 × 名单里每个 chat 各一份（收尾推送若因并发补扫重复触发，
        # 重复的是同一文案，不影响「逐 chat 投递」与去重语义的断言）
        assert all(texts.count(t) == 2 for t in unique), (
            f"每条推送投给名单内每个 chat 各一份，实际 {ch.sent}"
        )
    finally:
        await be.shutdown()
