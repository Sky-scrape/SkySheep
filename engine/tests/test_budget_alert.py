"""预算告警推渠道：档位越线、同日去重、跨天重置、渠道关跳过、状态损坏兜底。

推送目标复用 automation._cron_push_targets 的口径（含 Webhook 广播语义），
这里用假渠道替身验证「什么时候推、推什么、发过记档位」；状态文件走
SKYSHEEP_HOME 隔离，用量走临时 SessionStore。
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from skysheep.server.backend_parts import budget_alert as ba
from skysheep.server.backend_parts.automation import AutomationMixin
from skysheep.server.backend_parts.budget_alert import (
    alert_body,
    crossed_tiers,
    due_tiers,
    load_state,
    save_state,
)

BUDGET = 10_000


# ---- 替身小件：渠道 / 管理器 / 只背两个方法的宿主 ----


class _FakeChannel:
    def __init__(self, name="feishu", enabled=True, configured=True,
                 allowed=("u1",), pure=False):
        self.name = name
        self.enabled = enabled
        self._configured = configured
        self.allowed_ids = set(allowed)
        self.pure_outbound = pure
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def configured(self) -> bool:
        return self._configured

    async def send_text(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        if self.fail:
            raise RuntimeError("端点不可达")
        return True


class _FakeMgr:
    def __init__(self, *channels):
        self.channels = {c.name: c for c in channels}


def _host(store, mgr, budget: int = BUDGET):
    """只背预算告警用到的两个 mixin 方法，不背整个 backend。"""

    class _Host:
        _cron_push_targets = AutomationMixin._cron_push_targets
        _check_budget_alerts = AutomationMixin._check_budget_alerts

    h = _Host()
    h.cfg = SimpleNamespace(daily_token_budget=budget)
    h.store = store
    h.channels = mgr
    return h


async def _drain_bg():
    """让 spawn_bg 出去的推送任务跑完（假渠道 send_text 内部无 await）。"""
    for _ in range(3):
        await asyncio.sleep(0)


async def _seed_usage(store, tokens: int) -> None:
    await store.add_usage("s1", "prov", "model", tokens, 0)


# ---- 档位与文案（纯函数） ----


@pytest.mark.parametrize("used,expect", [
    (0, []), (7_999, []), (8_000, [80]), (9_999, [80]),
    (10_000, [80, 100]), (12_345, [80, 100]),
])
def test_crossed_tiers(used, expect):
    assert crossed_tiers(used, BUDGET) == expect


def test_crossed_tiers_zero_budget_never_crosses():
    assert crossed_tiers(999_999, 0) == []


def test_due_tiers_subtracts_sent():
    assert due_tiers(9_000, BUDGET, []) == [80]
    assert due_tiers(9_000, BUDGET, [80]) == []
    assert due_tiers(11_000, BUDGET, [80]) == [100]


def test_alert_body_fields():
    body = alert_body(80, 8_500, BUDGET)
    assert "今日用量：约 8,500 tokens" in body
    assert "每日预算：10,000 tokens（已用 85%）" in body
    assert "已达 80%" in body
    assert "建议" in body and "设置 · 高级" in body
    over = alert_body(100, 10_050, BUDGET)
    assert "已达上限" in over and "护栏" in over


# ---- 越线触发 / 同日去重 / 跨天重置（走 mixin 方法） ----


async def test_crossing_pushes_once_per_tier(store, home):
    ch = _FakeChannel()
    wh = _FakeChannel(name="webhook", pure=True)
    host = _host(store, _FakeMgr(ch, wh))

    # 85%：只推 80% 档；聊天渠道逐 chat_id，webhook 广播整段一条
    await _seed_usage(store, 8_500)
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 1 and ch.sent[0][0] == "u1"
    assert "已达 80%" in ch.sent[0][1]
    assert len(wh.sent) == 1 and wh.sent[0][0] == ""
    state = load_state()
    assert state["sent"] == [80] and state["date"] == ba.today_str()

    # 105%：补推 100% 档（80% 已发不重推）
    await _seed_usage(store, 2_000)
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 2 and "已达上限" in ch.sent[1][1]
    assert load_state()["sent"] == [80, 100]


async def test_same_day_same_tier_dedup(store, home):
    ch = _FakeChannel()
    host = _host(store, _FakeMgr(ch))
    await _seed_usage(store, 9_000)
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 1

    ch.sent.clear()
    await host._check_budget_alerts()  # 用量继续涨但没过新档
    await _drain_bg()
    assert ch.sent == [], "同日同档最多一条"


async def test_cross_day_reset(store, home):
    ch = _FakeChannel()
    host = _host(store, _FakeMgr(ch))
    await _seed_usage(store, 11_000)
    await host._check_budget_alerts()
    await _drain_bg()
    assert load_state()["sent"] == [80, 100]

    # 日期翻篇：已发档位不作数，今天再越线照发
    save_state({"date": "2026-01-01", "sent": [80, 100]})
    ch.sent.clear()
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 2
    state = load_state()
    assert state["date"] == ba.today_str() and state["sent"] == [80, 100]


async def test_push_failure_still_claimed_no_retry(store, home):
    """推送失败只记日志：档位当天已认领，不做全天重试（同日报口径）。"""
    ch = _FakeChannel()
    ch.fail = True
    host = _host(store, _FakeMgr(ch))
    await _seed_usage(store, 9_000)
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 1, "失败的那次尝试也发生过"
    assert load_state()["sent"] == [80]

    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 1, "不重试"


async def test_state_save_failure_only_logs(store, home, monkeypatch, caplog):
    """状态写盘失败只记日志：不抛、不推（档位记不下，硬推会每轮重复告警）。

    主调用点挂在 send() 收尾段且自身无兜底，_check_budget_alerts 若上抛
    会腰斩检查点保存、队列交棒与运行位释放——这里锁「永不抛」的承诺
    （触发面是 write_text_atomic 可抛的 OSError：磁盘满/目录权限/文件被锁）。
    """
    import skysheep.server.backend_parts.automation as automation_mod

    def _boom(state):
        raise OSError("disk full")

    monkeypatch.setattr(automation_mod, "save_budget_alert_state", _boom)
    ch = _FakeChannel()
    host = _host(store, _FakeMgr(ch))
    await _seed_usage(store, 9_000)
    with caplog.at_level(logging.WARNING, logger="skysheep.security"):
        await host._check_budget_alerts()  # 不应抛
    await _drain_bg()
    assert ch.sent == [], "写盘失败就不推"
    assert "写盘失败" in caplog.text
    assert not ba.state_path().exists(), "失败时不应留下半个状态文件"


# ---- 渠道未启用 / 无目标 / 预算未设：静默跳过 ----


async def test_no_targets_or_budget_skips_silently(store, home):
    await _seed_usage(store, 11_000)

    for mgr in (
        None,                                        # 渠道管理器未就绪
        _FakeMgr(),                                  # 没配任何渠道
        _FakeMgr(_FakeChannel(enabled=False)),       # 渠道关着
        _FakeMgr(_FakeChannel(configured=False)),    # 渠道没配齐
        _FakeMgr(_FakeChannel(allowed=())),          # 聊天渠道名单为空（拒绝一切）
    ):
        host = _host(store, mgr)
        await host._check_budget_alerts()
        await _drain_bg()
        assert not ba.state_path().exists(), f"{mgr!r} 应静默跳过且不记档位"

    # 预算未设（0 = 不限制）：有渠道也不告警
    ch = _FakeChannel()
    host = _host(store, _FakeMgr(ch), budget=0)
    await host._check_budget_alerts()
    await _drain_bg()
    assert ch.sent == [] and not ba.state_path().exists()


async def test_channel_enabled_later_same_day_still_alerts(store, home):
    """跳过不记档位：当天中途启用渠道，越线照样补一条（不丢提醒）。"""
    host = _host(store, None)
    await _seed_usage(store, 9_000)
    await host._check_budget_alerts()
    await _drain_bg()

    ch = _FakeChannel()
    host = _host(store, _FakeMgr(ch))
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 1 and "已达 80%" in ch.sent[0][1]


# ---- 状态文件：损坏兜底重建 / 原子形状 ----


def test_state_corrupt_rebuilds_as_empty(home):
    ba.state_path().parent.mkdir(parents=True, exist_ok=True)
    ba.state_path().write_text("不是 JSON", encoding="utf-8")
    assert load_state(today=ba.today_str()) == {"date": "", "sent": []}
    ba.state_path().write_text('{"date": 7, "sent": "80"}', encoding="utf-8")
    assert load_state(today=ba.today_str()) == {"date": "", "sent": []}


async def test_corrupt_state_flow_rebuilds_and_pushes(store, home):
    """状态损坏按没发过重建：越线照常推、文件重建成合法形状。"""
    ba.state_path().parent.mkdir(parents=True, exist_ok=True)
    ba.state_path().write_text('{"date": "2026-01-01", "sent": [80, 100],', encoding="utf-8")
    ch = _FakeChannel()
    host = _host(store, _FakeMgr(ch))
    await _seed_usage(store, 9_000)
    await host._check_budget_alerts()
    await _drain_bg()
    assert len(ch.sent) == 1
    raw = json.loads(ba.state_path().read_text(encoding="utf-8"))
    assert raw["date"] == ba.today_str() and raw["sent"] == [80]


def test_state_unknown_fields_dropped(home):
    save_state({"date": "2026-10-02", "sent": [80, 100, 33, "x"], "extra": 1})
    assert load_state() == {"date": "2026-10-02", "sent": [80, 100]}
