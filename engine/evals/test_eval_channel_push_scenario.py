"""评测基线 · 渠道出站推送开关语义场景（mock 聊天渠道；定时任务路径）。

场景 28：聊天渠道侧的出站推送开关语义——定时任务终态推送（无人值守闭环
第一期）。通用 Webhook 渠道本身由场景 30 以真实 WebhookChannel 覆盖；本场景
钉的是聊天渠道的目标口径（notify_channel 开关 + 允许名单）：

- notify_channel=False（默认，防打扰）：成功/无产出都一字不发；
- notify_channel=True：终态摘要推到渠道**允许名单**里的每个 chat（名单为空
  的渠道拒发一切）；摘要紧凑，带任务名、状态与耗时；
- 推送是附赠动作：推送与否不影响任务行回写（last_status / next_run 照常）。

复用流水线场景文件的 mock 渠道与后端组装辅助；推送目标判定走真实的
_cron_push_targets_of → push_text_to_targets（automation.py 的公共底座，
定时任务与流水线节点共用）。
"""

from __future__ import annotations

import asyncio

from evals.test_eval_pipeline_scenarios import (
    _make_backend,
    _MockChannel,
    _RoutedProvider,
    _wait_until,
)


async def test_eval_cron_channel_push_switch_semantics(home):
    """场景 28（渠道出站推送开关 · mock 渠道）：开关语义与空名单拒发。

    三个定时任务、两个 mock 渠道（feishu 名单 owner-1；empty 名单为空）：
    - 关开关任务跑成功：任何渠道都收不到（防打扰默认）；
    - 开开关任务跑成功：feishu 收到一条紧凑摘要（任务名+成功+耗时），
      名单为空的渠道一条也收不到；
    - 开开关但无产出（empty 状态）：同一开关、照常推送「完成（无产出）」；
    - 任务行回写不受推送影响：last_status 与 next_run_at 照常落库。
    """
    provider = _RoutedProvider([])
    provider.ROUTES = {"汇总今日进展": "今日三件事都完成了。", "会空转": ""}
    be = await _make_backend(home, provider)
    feishu = _MockChannel(allowed=("owner-1",))
    empty_roster = _MockChannel(allowed=())
    be.channels.channels["feishu"] = feishu
    be.channels.channels["empty"] = empty_roster
    try:
        pid = be.project.id
        t_off = await be.store.add_cron_task(
            pid, "关开关任务", "汇总今日进展", "interval", 30,
            allowed_tools=[], notify_channel=False)
        t_on = await be.store.add_cron_task(
            pid, "开开关任务", "汇总今日进展", "interval", 30,
            allowed_tools=[], notify_channel=True)
        t_noout = await be.store.add_cron_task(
            pid, "无产出任务", "会空转", "interval", 30,
            allowed_tools=[], notify_channel=True)

        # 1) 开关关：跑成功也一字不发
        await be._run_cron_task(t_off["id"], force=True)
        await asyncio.sleep(0.3)  # 留出「不该发的多发了」的暴露窗口
        assert feishu.sent == [], f"开关关不得推送，实际 {feishu.sent}"
        row = await be.store.get_cron_task(t_off["id"])
        assert row["last_status"] == "ok", "推送与否不影响任务行回写"
        assert row["next_run_at"] > 0, "成功后照常重排下次运行"

        # 2) 开关开：摘要推到名单 chat；空名单渠道拒发一切
        await be._run_cron_task(t_on["id"], force=True)
        assert await _wait_until(lambda: len(feishu.sent) >= 1), "开关开应推送一条摘要"
        chat_id, text = feishu.sent[0]
        assert chat_id == "owner-1", "推送目标是渠道允许名单"
        assert "开开关任务" in text and "成功" in text and "耗时" in text
        assert "今日三件事都完成了。" in text and len(text) < 2000, "摘要紧凑（≤500 字结果段）"
        assert empty_roster.sent == [], "名单为空的渠道拒发一切"
        row = await be.store.get_cron_task(t_on["id"])
        assert row["last_status"] == "ok"

        # 3) 无产出（empty 状态）与成功同一开关：照常推送
        await be._run_cron_task(t_noout["id"], force=True)
        assert await _wait_until(lambda: len(feishu.sent) >= 2), "无产出终态也推送"
        _chat, text2 = feishu.sent[-1]
        assert "无产出任务" in text2 and "无产出" in text2
        assert empty_roster.sent == [], "全程只有名单内的渠道收到"
        assert len(feishu.sent) == 2, f"三条任务只推两条（关开关的静默），实际 {feishu.sent}"
    finally:
        await be.shutdown()
