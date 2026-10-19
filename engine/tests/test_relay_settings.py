"""真实配置文件及 ASGI WS 的接力草稿闭环；全部使用隔离 home。"""
import copy
import tomllib

import pytest
from test_server import make_client, recv_until

from skysheep.config import config_path, load_config, set_advanced_settings_in_config
from skysheep.core.relay import RelayConfig
from skysheep.server.backend_parts.relay import RelaySettingsMixin


def rpc(ws, method, params=None):
    ws.send_json({"id": method, "method": method, "params": params or {}})
    return recv_until(ws, method)


def test_ws_save_needs_permission_and_survives_restart(home):
    set_advanced_settings_in_config(max_iterations=55)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        first = rpc(ws, "relaycfg.get")["result"]
        assert first["status"] == "draft"
        assert first["capabilities"]["execution"] is False
        draft = first["config"]
        draft["slots"]["B"]["role"] = "研究 <script>仅作为文本</script>"
        draft["slots"]["A"]["role"] = "复核"
        draft["slots"]["B"]["screen_preference"] = "left-monitor"
        draft["workflow"]["nodes"][0]["slot"] = "B"
        draft["workflow"]["nodes"][1]["slot"] = "A"
        prepared = rpc(ws, "relaycfg.save", {"config": draft})["result"]
        assert prepared["needs_confirm"]
        assert load_config().relay.revision == 0
        done = rpc(ws, "relaycfg.save", {
            "permission_id": prepared["permission_id"], "decision": "allow_once",
        })
        assert done["ok"], done
        assert done["result"]["saved"]
        expected = done["result"]["config"]
        assert expected["revision"] == 1
        assert rpc(ws, "relaycfg.get")["result"]["config"] == expected
        assert not rpc(ws, "relaycfg.save", {
            "permission_id": prepared["permission_id"], "decision": "allow_once",
        })["ok"]  # 权限令牌只能消费一次
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        assert rpc(ws, "relaycfg.get")["result"]["config"] == expected
    with config_path().open("rb") as stream:
        raw = tomllib.load(stream)
    assert raw["max_iterations"] == 55
    assert raw["relay"] == expected


@pytest.mark.parametrize("decision", ["deny", "allow_always", "ALLOW_FUTURE", "forged", None])
async def test_unknown_or_non_once_decision_never_writes(home, decision):
    backend = RelaySettingsMixin()
    before = config_path().read_bytes() if config_path().exists() else None
    prepared = await backend.save_relay_settings({"config": RelayConfig().model_dump()})
    result = await backend.save_relay_settings({
        "permission_id": prepared["permission_id"], "decision": decision,
    })
    assert result["saved"] is False
    assert (config_path().read_bytes() if config_path().exists() else None) == before


async def test_stale_confirmation_cannot_overwrite_and_snapshot_cannot_be_changed(home):
    backend = RelaySettingsMixin()
    draft = RelayConfig().model_dump()
    first = await backend.save_relay_settings({"config": draft})
    other = await backend.save_relay_settings({"config": copy.deepcopy(draft)})
    draft["slots"]["A"]["role"] = "不能注入已经校验的快照"
    await backend.save_relay_settings({"permission_id": first["permission_id"], "decision": "allow_once"})
    assert load_config().relay.slots["A"].role == "研究"
    with pytest.raises(ValueError, match="relay_conflict"):
        await backend.save_relay_settings({"permission_id": other["permission_id"], "decision": "allow_once"})
    with pytest.raises(ValueError, match="relay_conflict"):
        await backend.save_relay_settings({"config": RelayConfig().model_dump()})
    assert load_config().relay.revision == 1


async def test_write_failure_keeps_disk_and_memory(home, monkeypatch):
    import skysheep.server.backend_parts.relay as relay

    backend = RelaySettingsMixin()
    original = backend.relay_settings()["config"]
    prepared = await backend.save_relay_settings({"config": original})
    before = config_path().read_bytes() if config_path().exists() else None

    def fail(*args):
        raise OSError("injected write failure")

    monkeypatch.setattr(relay, "update_config_section", fail)
    with pytest.raises(OSError, match="injected"):
        await backend.save_relay_settings({
            "permission_id": prepared["permission_id"], "decision": "allow_once",
        })
    assert backend.cfg.relay.revision == 0
    assert (config_path().read_bytes() if config_path().exists() else None) == before


async def test_pending_expiry_and_unknown_provider(home, monkeypatch):
    import skysheep.server.backend_parts.relay as relay

    backend = RelaySettingsMixin()
    draft = RelayConfig().model_dump()
    draft["slots"]["A"].update(provider="does-not-exist", model="custom")
    with pytest.raises(ValueError, match="未配置"):
        await backend.save_relay_settings({"config": draft})
    prepared = await backend.save_relay_settings({"config": RelayConfig().model_dump()})
    monkeypatch.setattr(relay.time, "monotonic", lambda: 10**12)
    with pytest.raises(ValueError, match="relay_permission_expired"):
        await backend.save_relay_settings({
            "permission_id": prepared["permission_id"], "decision": "allow_once",
        })
    assert load_config().relay.revision == 0


@pytest.mark.parametrize("method", ["relaycfg.get", "relaycfg.save"])
def test_remote_dispatch_forbidden(home, method, monkeypatch):
    monkeypatch.setattr("skysheep.server.app._client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        response = rpc(ws, method)
    assert not response["ok"]
    assert "本机" in response["error"]


def test_ws_unsupported_does_not_write(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        draft = rpc(ws, "relaycfg.get")["result"]["config"]
        before = config_path().read_bytes() if config_path().exists() else None
        draft["slots"]["A"]["execution_mode"] = "execute"
        response = rpc(ws, "relaycfg.save", {"config": draft})
        assert not response["ok"]
        assert "execution_mode" in response["error"]
        assert (config_path().read_bytes() if config_path().exists() else None) == before
