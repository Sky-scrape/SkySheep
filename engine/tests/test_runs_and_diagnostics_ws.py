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
