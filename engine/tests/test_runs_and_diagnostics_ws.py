"""运行中心 / 模型诊断的后端集成（WS 层）与高级设置新字段。

覆盖：runs.list / runs.get / runs.cancel / permission.queue 的 WS 契约、
model.providers_health 快照、model.test 的失败面（假 provider 不联网），
以及 advanced.get/save 对 turn_token_budget 与 fallback_model 的读写。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skysheep.config import set_advanced_settings_in_config
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app


def make_client(home, script, provider=None):
    provider = provider or FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def recv_until(ws, wanted_id=None, events=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def _rpc(ws, mid, method, params=None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    return recv_until(ws, mid)


def test_runs_list_and_health_rpc(home):
    with make_client(home, ["好的"]) as client, client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "r1", "runs.list", {"limit": 10})
        assert r["ok"] is True
        assert isinstance(r["result"]["runs"], list)

        r = _rpc(ws, "r2", "runs.get", {"run_id": "no-such"})
        assert r["ok"] is False

        r = _rpc(ws, "r3", "model.providers_health")
        assert r["ok"] is True
        assert isinstance(r["result"]["providers"], list)

        r = _rpc(ws, "r4", "permission.queue")
        assert r["ok"] is True
        assert isinstance(r["result"]["pending"], list)

        r = _rpc(ws, "r5", "runs.cancel", {"run_id": "no-such"})
        assert r["ok"] is False


def test_run_center_records_turn(home):
    """发一轮消息后运行中心应有该轮的 completed 记录（含工具次数）。"""
    with make_client(home, ["回答好了"]) as client, client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "c1", "chat.send", {"text": "你好"})
        assert r["ok"] is True
        r2 = _rpc(ws, "c2", "runs.list", {"limit": 20})
        runs = r2["result"]["runs"]
        # 发消息前 runs.list 先建了连接——本条用例里第一轮刚跑完，
        # 应该能看到至少一条 completed 的主对话轮
        assert any(x.get("kind") == "turn" and x.get("state") == "completed"
                   for x in runs), runs


def test_model_test_with_fake_provider(home):
    """model.test 对演示模式（fake）服务：构建不出真实测试目标时给可读失败。

    fake 不走网络——测试连接走 config 里的 fake 服务会以 unknown/network
    分类失败返回结构化结果，不抛 500。
    """
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "m1", "model.test", {"name": "fake"})
        assert r["ok"] is True  # handler 本身不抛
        assert r["result"]["ok"] is False  # fake 服务测不出真实连通


def test_model_probe_capabilities_with_fake_provider(home):
    """能力探测对 fake 服务：没有真实接口与 Key，四项都是「没测成」，不报错。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _rpc(ws, "m2", "model.probe_capabilities", {"name": "fake"})
        assert r["ok"] is True
        res = r["result"]
        for key in ("streaming", "tools", "vision", "reasoning"):
            assert res[key]["supported"] is None
            assert res[key]["note"]
        assert res["summary"]
        assert res["latency_ms"] == 0


def test_advanced_turn_budget_roundtrip(home):
    """单轮上限 / 备用模型进高级设置读写（config.toml）。"""
    set_advanced_settings_in_config(
        turn_token_budget=123456,
        fallback_model="zhipu:glm-5.3",
    )
    from skysheep.config import load_config

    cfg = load_config()
    assert cfg.turn_token_budget == 123456
    assert cfg.fallback_model == "zhipu:glm-5.3"

    # 清除：fallback 传空串删键、turn 传 0 关闭
    set_advanced_settings_in_config(turn_token_budget=0, fallback_model="")
    cfg2 = load_config()
    assert cfg2.turn_token_budget == 0
    assert cfg2.fallback_model == ""

    from skysheep.config import ConfigError

    with pytest.raises(ConfigError):
        set_advanced_settings_in_config(fallback_model="no-colon")


def test_fallback_target_parses(home):
    """_fallback_target 的解析：无效/自指/合法三类。"""
    from skysheep.config import set_advanced_settings_in_config

    with make_client(home, []) as client:
        b = client.app.state.backend
        # 未配置
        assert b._fallback_target() is None
        # 合法且非当前服务（当前是 fake）
        set_advanced_settings_in_config(fallback_model="deepseek:deepseek-chat")
        b.cfg = __import__("skysheep.config", fromlist=["load_config"]).load_config()
        t = b._fallback_target()
        assert t == ("deepseek", "deepseek-chat")
        # 指向当前服务（fake）→ None
        set_advanced_settings_in_config(fallback_model="fake:whatever")
        b.cfg = __import__("skysheep.config", fromlist=["load_config"]).load_config()
        assert b._fallback_target() is None
        set_advanced_settings_in_config(fallback_model="")


def test_agent_reports_turn_summary(home):
    """TurnFinished 事件带轮摘要字段（tools_used / model_retries）。"""
    with make_client(home, ["好的"]) as client, client.websocket_connect("/ws") as ws:
        _rpc(ws, "e1", "chat.send", {"text": "hi"})
        # 事件帧在 _rpc 里被丢弃；重新连一发并收集
    with make_client(home, ["好的"]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "e2", "method": "chat.send", "params": {"text": "hi"}})
        finished = None
        while True:
            frame = ws.receive_json()
            if "event" in frame:
                if frame["event"] == "turn_finished":
                    finished = frame["data"]
                continue
            if frame.get("id") == "e2":
                break
        assert finished is not None
        assert "tools_used" in finished
        assert "tools_total_ms" in finished
        assert "model_retries" in finished


def test_provider_management_paths(home):
    """模型服务增删启停的边界分支（WS 层，走完整后端）。

    覆盖：演示模式写入 kind=fake 服务并切为当前服务 → 停用当前服务（was_active）、
    重新启用、删除当前服务、内置预设删除=隐藏且可恢复、保存未知服务报错、
    fake 协议的能力探测提前返回、测试连接对已配置服务解析 Key 并登记失败健康灯。
    """
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 演示模式：写入 kind=fake 的 demo 服务并切过去（成为当前服务）
        r = _rpc(ws, "d1", "demo.enable")
        assert r["ok"] is True
        assert r["result"]["provider"] == "demo"

        # fake 协议的能力探测：走「没有真实接口」提前返回分支
        r = _rpc(ws, "d2", "model.probe_capabilities", {"name": "demo"})
        assert r["ok"] is True
        assert r["result"]["summary"] == "该服务类型不支持能力探测。"

        # 停用正在使用的服务：was_active True
        r = _rpc(ws, "d3", "config.set_provider_enabled", {"name": "demo", "enabled": False})
        assert r["ok"] is True
        assert r["result"]["was_active"] is True

        # 重新启用（走 restore 路径），此时已不是当前服务
        r = _rpc(ws, "d4", "config.set_provider_enabled", {"name": "demo", "enabled": True})
        assert r["ok"] is True
        assert r["result"]["was_active"] is False

        # 保存当前服务的字段并设为默认：走「固化当前模型 + 重建」路径
        r = _rpc(ws, "d5", "config.save_provider",
                 {"name": "demo", "set_default": True, "supports_vision": False})
        assert r["ok"] is True
        assert r["result"]["saved"] == "demo"

        # 测试连接走已配置服务：解析出 Key（走 resolve_api_key），
        # 地址指向 discard 端口保证快速失败、不触外网，失败登记健康灯
        r = _rpc(ws, "d6", "model.test",
                 {"name": "demo", "base_url": "http://127.0.0.1:9"})
        assert r["ok"] is True
        assert r["result"]["ok"] is False
        health = _rpc(ws, "d7", "model.providers_health")
        assert health["ok"] is True
        rows = {x["name"]: x for x in health["result"]["providers"]}
        assert rows["demo"]["level"] == "down"

        # 删掉当前服务：was_active True
        r = _rpc(ws, "d8", "config.delete_provider", {"name": "demo"})
        assert r["ok"] is True
        assert r["result"]["was_active"] is True

        # 内置预设删除 = 隐藏（可恢复），删非当前服务时 was_active False
        r = _rpc(ws, "d9", "config.delete_provider", {"name": "zhipu"})
        assert r["ok"] is True
        assert r["result"]["hidden"] is True
        assert r["result"]["was_active"] is False
        r = _rpc(ws, "d10", "config.restore_provider", {"name": "zhipu"})
        assert r["ok"] is True

        # 保存未知服务：报错（不静默）
        r = _rpc(ws, "d11", "config.save_provider", {"name": "no-such", "model": "x"})
        assert r["ok"] is False
