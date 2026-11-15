"""A3 模型自动分层路由：档位选择、三条不改路由的边界、WS 轮内接线。

路由本身是纯决策（`_apply_model_routing`），这里不跑真模型：provider 用
哨兵替换，断言「该换的换了、不该换的一律不换」；接线再用一条 WS 轮次确认
提示事件真的发到了前端。
"""

from __future__ import annotations

import types

import pytest

from skysheep.config import ProviderConfig, load_config
from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend


def _mk_backend(tmp_path, monkeypatch) -> ServerBackend:
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    be = ServerBackend(
        working_dir=proj,
        provider_factory=lambda: FakeProvider([[TextBlock(text="好")]]),
    )
    be.cfg = load_config()
    be.cfg.providers["light"] = ProviderConfig(kind="fake", api_key="k", model="lm")
    be.cfg.providers["heavy"] = ProviderConfig(kind="fake", api_key="k", model="hm")
    return be


class _Sentinel:
    """够用的假 provider：路由只把它挂到 agent 上，不真的调用。"""

    supports_reasoning = False
    demo_mode = False

    def __init__(self, tag: str) -> None:
        self.tag = tag


async def _route(be, text, sid="s1", history=None, regenerate=False):
    notices: list[str] = []

    async def emit(ev) -> None:
        # 管线里的 emit 收事件 dict（NoticeEvent.model_dump()）
        notices.append(str((ev.get("message") if isinstance(ev, dict) else getattr(ev, "message", "")) or ""))

    prev = _Sentinel("prev")
    agent = types.SimpleNamespace(history=list(history or []), provider=prev)
    built: list[str] = []

    def build(name, model):
        built.append(f"{name}/{model or ''}")
        return _Sentinel(name)

    be._build_detached_provider = build  # type: ignore[method-assign]
    out_prev, out_now = await be._apply_model_routing(
        agent, sid, text, regenerate=regenerate, emit=emit,
    )
    return agent, prev, out_prev, out_now, notices, built


@pytest.mark.asyncio
async def test_routing_disabled_changes_nothing(tmp_path, monkeypatch):
    be = _mk_backend(tmp_path, monkeypatch)
    be.cfg.routing.enabled = False
    agent, prev, out_prev, out_now, notices, built = await _route(be, "你好")
    assert (out_prev, out_now) == (None, None)
    assert agent.provider is prev and not built and not notices


@pytest.mark.asyncio
async def test_routing_low_uses_light_and_notifies(tmp_path, monkeypatch):
    be = _mk_backend(tmp_path, monkeypatch)
    be.cfg.routing.enabled = True
    be.cfg.routing.light_provider = "light"
    agent, prev, out_prev, out_now, notices, built = await _route(be, "你好")
    assert built == ["light/"]           # 模型留空 = 服务默认
    assert out_prev is prev and agent.provider is out_now and out_now is not prev
    assert any("轻量模型" in n for n in notices), notices


@pytest.mark.asyncio
async def test_routing_high_uses_heavy(tmp_path, monkeypatch):
    be = _mk_backend(tmp_path, monkeypatch)
    be.cfg.routing.enabled = True
    be.cfg.routing.heavy_provider = "heavy"
    be.cfg.routing.heavy_model = "hm-2"
    agent, prev, out_prev, out_now, notices, built = await _route(
        be, "排查这个并发 bug 并重构，给出架构方案与边界条件",
    )
    assert built == ["heavy/hm-2"]
    assert any("主力模型" in n for n in notices), notices


@pytest.mark.asyncio
async def test_routing_skips_when_session_model_overridden(tmp_path, monkeypatch):
    be = _mk_backend(tmp_path, monkeypatch)
    be.cfg.routing.enabled = True
    be.cfg.routing.light_provider = "light"
    be._session_models["s1"] = ("light", "")   # 用户手动选过模型
    agent, prev, out_prev, out_now, _notices, built = await _route(be, "你好")
    assert (out_prev, out_now) == (None, None) and not built
    assert agent.provider is prev


@pytest.mark.asyncio
async def test_routing_skips_unknown_service_and_medium(tmp_path, monkeypatch):
    be = _mk_backend(tmp_path, monkeypatch)
    be.cfg.routing.enabled = True
    be.cfg.routing.light_provider = "ghost"     # 不在 providers 里
    _a, _p, out_prev, out_now, _n, built = await _route(be, "你好")
    assert (out_prev, out_now) == (None, None) and not built

    # medium（无明显轻/重信号）不路由，即使两档都配了
    be.cfg.routing.light_provider = "light"
    be.cfg.routing.heavy_provider = "heavy"
    _a, _p, out_prev, out_now, _n, built = await _route(be, "把纪要整理到文档里")
    assert built == [] or (out_prev, out_now) == (None, None)


def test_routing_advanced_settings_roundtrip(home):
    """advanced.save 写 [routing] 并回读；未知服务被入口拒绝。"""
    from test_server import make_client, recv_until

    from skysheep.config import update_provider_in_config

    # 配一个独立的假服务（隔离 HOME，不碰真实配置）
    update_provider_in_config("fastfake", kind="fake", api_key="k", model="fast-1")

    with make_client(home, [[]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g0", "method": "advanced.get", "params": {}})
        got = recv_until(ws, "g0")["result"]
        assert got["routing_enabled"] is False

        ws.send_json({"id": "s1", "method": "advanced.save", "params": {
            "routing_enabled": True,
            "routing_light_provider": "fastfake",
            "routing_light_model": "fast-1",
            "routing_heavy_provider": "nope",
            "routing_heavy_model": "",
        }})
        bad = recv_until(ws, "s1")
        assert not bad["ok"] and "未知服务" in str(bad.get("error"))

        ws.send_json({"id": "s2", "method": "advanced.save", "params": {
            "routing_enabled": True,
            "routing_light_provider": "fastfake",
            "routing_light_model": "fast-1",
        }})
        ok = recv_until(ws, "s2")
        assert ok["ok"], ok.get("error")
        assert ok["result"]["routing_enabled"] is True
        assert ok["result"]["routing_light_provider"] == "fastfake"
        assert ok["result"]["routing_light_model"] == "fast-1"


def test_routing_notice_reaches_frontend_and_model_unchanged(home):
    """接线：轻活消息真的触发路由提示，且轮末会话模型没有被改成轻量模型。"""
    from test_server import make_client, recv_until

    from skysheep.config import update_provider_in_config

    update_provider_in_config(
        "fastfake", kind="fake", api_key="k", model="fast-1",
    )
    from skysheep.config import update_config_section

    update_config_section("routing", {
        "enabled": True, "light_provider": "fastfake", "light_model": "",
    })
    scripts = [[[TextBlock(text="（路由档回答）")]], [[TextBlock(text="（默认档回答）")]]]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        events: list = []
        ws.send_json({"id": "m1", "method": "chat.send", "params": {"text": "你好"}})
        frame = recv_until(ws, "m1", events)
        assert frame["ok"], frame.get("error")
        sid = frame["result"]["session_id"]
        notices = [
            str((e.get("data") or {}).get("message") or "")
            for e in events if e.get("event") == "notice"
        ]
        assert any("轻量模型" in n for n in notices), notices
        # 会话模型仍是默认那个（路由只是本轮临时换 provider）
        ws.send_json({"id": "mg", "method": "session.model_get",
                      "params": {"session_id": sid}})
        got = recv_until(ws, "mg")["result"]
        assert got["name"] != "fastfake" and got["overridden"] is False, got
