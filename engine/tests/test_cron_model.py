"""产线级模型：cron_tasks.provider_name / model 的存取、入口校验与执行取用。

覆盖契约（docs/工厂设计.md §8.1 v10）：
- store 往返与清空；
- 入口只校验「服务存在 / 模型名有界」，模型名不按服务列表硬卡
  （OpenAI 兼容端点可以接任意上游模型名，列表只是候选）；
- 团队任务的模型由团队模板决定，不接受这两列；
- 执行侧使用 detached provider，不碰全局「当前服务」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_server import make_client, recv_until

from skysheep.server.backend_parts.automation import AutomationMixin


class _CronModelProbe(AutomationMixin):
    """只借两个纯方法：入口校验与执行侧取 provider。"""

    def __init__(self, providers: dict, provider: str = "GLOBAL") -> None:
        self.cfg = SimpleNamespace(providers=providers)
        self.provider = provider
        self.builds: list[tuple[str, str | None]] = []

    def _build_detached_provider(self, name: str, model: str | None):
        self.builds.append((name, model))
        return f"{name}/{model or 'default'}"


def _call(ws, method: str, params: dict | None = None, mid: str | None = None) -> dict:
    mid = mid or method
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    return recv_until(ws, mid)


async def test_cron_provider_model_roundtrip_and_clear(store, tmp_path):
    proj = await store.get_or_create_project(str(tmp_path / "厂区"), "一号厂")
    task = await store.add_cron_task(
        proj.id, "日报", "干活", "interval", interval_minutes=5,
        provider_name="zhipu", model="glm-4.6")
    assert task["provider_name"] == "zhipu" and task["model"] == "glm-4.6"

    got = await store.get_cron_task(task["id"])
    assert got["provider_name"] == "zhipu" and got["model"] == "glm-4.6"
    updated = await store.update_cron_task(task["id"], provider_name="", model="")
    assert updated["provider_name"] == "" and updated["model"] == ""


def test_cron_model_fields_validation():
    probe = _CronModelProbe({"zhipu": object()})
    assert probe._cron_model_fields({}, is_team=False) == ("", "")
    assert probe._cron_model_fields(
        {"provider_name": "zhipu", "model": "glm-4.6"}, is_team=False,
    ) == ("zhipu", "glm-4.6")
    # 只给模型不给服务：歧义直接拒，不猜全局
    with pytest.raises(RuntimeError, match="先选模型服务"):
        probe._cron_model_fields({"model": "glm-4.6"}, is_team=False)
    with pytest.raises(RuntimeError, match="不存在或已停用"):
        probe._cron_model_fields({"provider_name": "nope"}, is_team=False)
    # 团队任务的总管 / 队员各有 provider，模板说了算
    with pytest.raises(RuntimeError, match="团队模板决定"):
        probe._cron_model_fields({"provider_name": "zhipu"}, is_team=True)
    with pytest.raises(RuntimeError, match="太长"):
        probe._cron_model_fields(
            {"provider_name": "zhipu", "model": "x" * 121}, is_team=False)


def test_cron_line_provider_follows_global_or_detached():
    probe = _CronModelProbe({"zhipu": object()})
    assert probe._cron_line_provider({"provider_name": ""}) == "GLOBAL"
    assert probe._cron_line_provider(
        {"provider_name": "zhipu", "model": "glm-4.6"}) == "zhipu/glm-4.6"
    # 只指定服务 = 用它的默认模型
    assert probe._cron_line_provider({"provider_name": "zhipu"}) == "zhipu/default"
    assert probe.builds == [("zhipu", "glm-4.6"), ("zhipu", None)]


def test_cron_line_provider_missing_service_fails_closed():
    class _Missing(_CronModelProbe):
        def _build_detached_provider(self, name: str, model: str | None):
            raise KeyError(name)

    with pytest.raises(RuntimeError, match="不存在或已停用"):
        _Missing({})._cron_line_provider({"provider_name": "gone"})


def test_context_limit_follows_actual_provider():
    """产线跑在 32k 服务上就不能拿全局 200k 的上限：压缩会触发得太晚。"""
    from skysheep.server.backend import ServerBackend

    cfg = SimpleNamespace(
        context_limit_tokens=100_000,
        providers={
            "small": SimpleNamespace(effective_context_limit=lambda g: 32_000),
            "big": SimpleNamespace(effective_context_limit=lambda g: 200_000),
        },
    )
    dummy = SimpleNamespace(cfg=cfg, provider_name="big")
    dummy._context_limit = lambda: 200_000
    assert ServerBackend._context_limit_for(dummy, SimpleNamespace(name="small")) == 32_000
    assert ServerBackend._context_limit_for(
        dummy, SimpleNamespace(name="unknown")) == 200_000, "认不出来回落全局"


def test_cron_add_validates_and_persists_provider_model(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert _call(ws, "boot")["ok"]
        base = {"name": "日报", "prompt": "汇总", "schedule_type": "interval",
                "interval_minutes": 30}

        bad = _call(ws, "cron.add", {**base, "provider_name": "nope"})
        assert not bad["ok"] and "不存在或已停用" in bad["error"]
        bad = _call(ws, "cron.add", {**base, "model": "glm-4.6"})
        assert not bad["ok"] and "先选模型服务" in bad["error"]

        ok = _call(ws, "cron.add", {
            **base, "provider_name": "zhipu", "model": "glm-4.6"})
        assert ok["ok"], ok
        assert ok["result"]["provider_name"] == "zhipu"
        assert ok["result"]["model"] == "glm-4.6"

        cleared = _call(ws, "cron.update", {
            "id": ok["result"]["id"], "provider_name": "", "model": ""})
        assert cleared["ok"], cleared
        assert cleared["result"]["provider_name"] == ""
        assert cleared["result"]["model"] == ""
