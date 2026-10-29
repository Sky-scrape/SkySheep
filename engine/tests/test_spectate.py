"""远程观战（T4）回归：摘要 compose、mixin 装配、local_only 注册、推送计数。"""

from __future__ import annotations

import asyncio

from skysheep.server._dispatch_automation import METHODS
from skysheep.server.backend_parts.spectate import SpectateMixin, compose_spectate_digest


def test_compose_digest_counters_and_attention():
    state = {
        "counters": {"running": 1, "queued": 2, "done_today": 3, "tokens_today": 400},
        "attention_total": 1,
        "attention": [{"text": "work · 每日巡检：上次运行失败"}],
        "projects": [
            {"id": 1, "name": "work", "lines": [{}, {}]},
            {"id": 2, "name": "demo", "lines": []},
        ],
    }
    text = compose_spectate_digest(state, [], now=1735000000)
    assert text.startswith("【SkySheep 观战快照】")
    assert "在跑 1" in text and "排队 2" in text and "今日完成 3" in text
    assert "需留意 1" in text
    assert "work 2 条" in text and "demo 0 条" in text
    assert "需留意：work · 每日巡检：上次运行失败" in text


def test_compose_digest_teams_and_resident():
    teams = [
        {"name": "晨会队", "members": [1, 2], "tasks": [{}, {}], "resident": True},
        {"name": "杂务队", "members": [1], "tasks": []},
        "junk",  # 非字典条目忽略
    ]
    text = compose_spectate_digest({"counters": {}, "projects": []}, teams, now=1735000000)
    assert "晨会队（成员 2 · 工单 2 张）" in text
    assert "杂务队（成员 1 · 工单 0 张）" in text
    assert "其中常驻 1 支" in text


def test_spectate_methods_registered_local_only():
    assert METHODS["spectate.snapshot"].local_only is True
    assert METHODS["spectate.send"].local_only is True


def test_spectate_send_counts_delivered():
    class _Chan:
        name = "fake"
        enabled = True
        pure_outbound = True

        async def configured(self):
            return True

        async def send_text(self, chat_id, body):
            return "观战" in body

    class _Mgr:
        channels = {"c": _Chan()}

    class _Be(SpectateMixin):
        channels = _Mgr()
        _teams = {}

        async def factory_state(self):
            return {"counters": {}, "projects": [], "attention": []}

    out = asyncio.run(_Be().spectate_send())
    assert out["delivered"] == 1
