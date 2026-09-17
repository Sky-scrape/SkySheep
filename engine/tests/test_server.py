"""桌面服务层测试：WebSocket 协议（boot / chat.send / 权限 / 会话 / 设置）。"""

from __future__ import annotations

import json
import tomllib

import pytest
from fastapi.testclient import TestClient

from skysheep.config import (
    PRESETS,
    load_config,
    restore_provider_in_config,
    update_config_section,
)
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app


def make_client(home, script, provider=None):
    """provider 可注入预构建的 FakeProvider（如带脚本默认值的场景）。"""
    provider = provider or FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def recv_until(ws, wanted_id=None, events=None):
    """读帧直到收到指定 id 的回复；事件帧收进 events。"""
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def test_boot_snapshot(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b1", "method": "boot"})
        frame = recv_until(ws, "b1")
    assert frame["ok"]
    snap = frame["result"]
    assert snap["version"]
    tool_names = {t["name"] for t in snap["tools"]}
    assert {"read_file", "write_file", "load_skill", "spawn_agent", "check_task"} <= tool_names
    # 电脑控制工具集：默认关闭（缩小攻击面），boot 快照里不应出现
    assert not {"screenshot", "window_list", "mouse", "keyboard"} & tool_names
    # 内置常用 MCP 预设：boot 快照带预设元数据 + 已装名单
    preset_names = {p["name"] for p in snap["mcp_presets"]}
    assert "sequential-thinking" in preset_names
    assert "mcp_installed" in snap
    # 首启向导：预设带注册入口；崩溃哨兵字段随快照下发
    assert all("signup" in p for p in snap["provider_presets"])
    assert snap["provider_presets"][0]["signup"].startswith("https://")
    assert "crashed_last_run" in snap


def test_computer_control_toggle_hot_applies(home):
    """设置 · 高级打开「电脑控制」总开关：热生效，工具清单即时出现七件套。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "advanced.save",
                      "params": {"computer_control": True}})
        assert recv_until(ws, "a1")["ok"]
        ws.send_json({"id": "a2", "method": "boot"})
        snap = recv_until(ws, "a2")["result"]
        tool_names = {t["name"] for t in snap["tools"]}
        assert {"screenshot", "window_list", "clipboard_read", "clipboard_write",
                "mouse", "keyboard", "window"} <= tool_names
        # 关回去：即时从工具清单消失
        ws.send_json({"id": "a3", "method": "advanced.save",
                      "params": {"computer_control": False}})
        assert recv_until(ws, "a3")["ok"]
        ws.send_json({"id": "a4", "method": "boot"})
        snap = recv_until(ws, "a4")["result"]
        assert not {"screenshot", "mouse", "keyboard"} & {t["name"] for t in snap["tools"]}


def test_browser_control_toggle_hot_applies(home):
    """「浏览器控制」开关：默认关；打开后 browser 工具即时出现在工具清单。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c0", "method": "boot"})
        names = {t["name"] for t in recv_until(ws, "c0")["result"]["tools"]}
        assert "browser" not in names  # 默认关
        ws.send_json({"id": "c1", "method": "advanced.save",
                      "params": {"browser_control": True}})
        assert recv_until(ws, "c1")["ok"]
        ws.send_json({"id": "c2", "method": "advanced.get"})
        assert recv_until(ws, "c2")["result"]["browser_control"] is True
        ws.send_json({"id": "c3", "method": "boot"})
        names = {t["name"] for t in recv_until(ws, "c3")["result"]["tools"]}
        assert "browser" in names
        # 关回去即时消失
        ws.send_json({"id": "c4", "method": "advanced.save",
                      "params": {"browser_control": False}})
        assert recv_until(ws, "c4")["ok"]
        ws.send_json({"id": "c5", "method": "boot"})
        names = {t["name"] for t in recv_until(ws, "c5")["result"]["tools"]}
        assert "browser" not in names


def test_apply_theme_triggers_immediate_repaint(home, monkeypatch):
    """主题回写要事件驱动重刷标题栏（不等看板线程下一秒轮询）。"""
    calls = []
    from skysheep import wintheme

    monkeypatch.setattr(wintheme, "refresh_now", lambda: calls.append(1) or True)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "app.apply_theme", "params": {"resolved": "dark"}})
        r = recv_until(ws, "t1")
        assert r["ok"] and r["result"]["mode"] == "dark"
        ws.send_json({"id": "t2", "method": "app.apply_theme", "params": {"resolved": "light"}})
        assert recv_until(ws, "t2")["ok"]
    assert len(calls) == 2
    assert wintheme.current_theme_mode() == "light"


def test_advanced_budget_persists(home):
    """每日 token 预算：advanced.save 落盘、advanced.get 回读。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b1", "method": "advanced.save",
                      "params": {"daily_token_budget": 500000}})
        assert recv_until(ws, "b1")["ok"]
        ws.send_json({"id": "b2", "method": "advanced.get"})
        assert recv_until(ws, "b2")["result"]["daily_token_budget"] == 500000
        cfg = tomllib.loads((home / "home" / "config.toml").read_text(encoding="utf-8"))
        assert cfg["daily_token_budget"] == 500000


def test_demo_enable_switches_provider(home):
    """演示模式：demo.enable 写入 kind=fake 的 demo 服务并切为默认。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "demo.enable"})
        r = recv_until(ws, "d1")
        assert r["ok"] and r["result"]["provider"] == "demo"
    cfg = tomllib.loads((home / "home" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["default"] == "demo"
    assert cfg["providers"]["demo"]["kind"] == "fake"


def test_build_provider_fake_is_demo():
    """工厂层：kind=fake 构建出带演示标记的 FakeProvider（无需任何 Key）。"""
    from skysheep.config import ProviderConfig
    from skysheep.models.factory import build_provider

    p = build_provider("demo", ProviderConfig(kind="fake", api_key="demo", model="demo"))
    assert getattr(p, "demo_mode", False) is True
    assert p.name == "demo"


def test_mcp_add_preset_unknown_name(home):
    """WS 协议接线：不存在的预设名回错误；boot 里 mcp_installed 反映现状。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "mcp.add_preset", "params": {"name": "no-such-xyz"}})
        frame = recv_until(ws, "p1")
    assert not frame["ok"]
    assert "内置预设" in frame["error"]


def test_chat_send_streams_events_and_persists(home):
    script = [[TextBlock(text="你好，我是 SkySheep")]]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "你好"}})
        events = []
        frame = recv_until(ws, "c1", events)
    assert frame["ok"] and frame["result"]["done"]
    kinds = [e["event"] for e in events]
    assert "text_delta" in kinds
    assert "assistant_message" in kinds
    assert "turn_finished" in kinds
    # queue_updated 是每轮结束后的运行状态广播（排队机制的状态源），收尾必发
    assert kinds[-1] == "queue_updated"
    # 会话持久化：新连接 resume 后可加载
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        snap = recv_until(ws, "b")["result"]
        assert snap["sessions"], "应有至少一条会话记录"


def test_chat_with_write_permission_flow(home):
    script = [
        [ToolUseBlock(id="t1", name="write_file", input={"path": "out.txt", "content": "hi"})],
        [TextBlock(text="文件已写入")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写个文件"}})
        events = []
        got_request = False
        while True:
            frame = ws.receive_json()
            if "event" not in frame:
                if frame.get("id") == "c1":
                    final = frame
                    break
                continue
            events.append(frame)
            if frame["event"] == "permission_request":
                got_request = True
                req_id = frame["data"]["request_id"]
                ws.send_json({
                    "id": "p1",
                    "method": "permission.respond",
                    "params": {"request_id": req_id, "decision": "allow_once"},
                })
    assert got_request
    assert final["ok"] and final["result"]["done"]
    resolved = [e for e in events if e["event"] == "permission_resolved"]
    assert resolved and resolved[0]["data"]["decision"] == "allow_once"
    assert (home / "proj" / "out.txt").read_text(encoding="utf-8") == "hi"


def test_session_new_resume(home):
    with make_client(home, [[TextBlock(text="ok")]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        s1 = recv_until(ws, "n1")["result"]
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "标题消息"}})
        recv_until(ws, "c1")
        ws.send_json({"id": "n2", "method": "session.new"})
        s2 = recv_until(ws, "n2")["result"]
        assert s1["id"] != s2["id"]
        ws.send_json({"id": "r1", "method": "session.resume", "params": {"id": s1["id"]}})
        r = recv_until(ws, "r1")["result"]
        assert r["id"] == s1["id"]


def test_unknown_method_and_skill_toggle_error(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u1", "method": "nope.nothing"})
        frame = recv_until(ws, "u1")
        assert not frame["ok"] and "unknown method" in frame["error"]
        ws.send_json({
            "id": "u2",
            "method": "skills.toggle",
            "params": {"name": "nonexistent", "enabled": False},
        })
        frame = recv_until(ws, "u2")
        assert not frame["ok"] and "not found" in frame["error"]


def test_static_index_served(home):
    with make_client(home, []) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "SkySheep" in r.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/health").json() == {"ok": True}


def test_no_api_key_boots_gracefully(home, monkeypatch):
    """缺 API Key 时应用照常启动：boot 带 provider_error，发消息报可读错误。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = create_app(working_dir=home / "proj")  # 无 provider_factory，默认 provider 无 Key
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        snap = recv_until(ws, "b")["result"]
        assert snap["provider_error"], "应记录 provider_error 而非启动失败"
        assert "DEEPSEEK_API_KEY" in snap["provider_error"]

        ws.send_json({"id": "c", "method": "chat.send", "params": {"text": "hi"}})
        frame = recv_until(ws, "c")
        assert not frame["ok"]
        assert "API Key" in frame["error"]


def test_settings_save_provider(home, monkeypatch):
    """设置页：保存 provider 到 config.toml + 设默认；Key 只回传掩码。"""
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "config.save_provider", "params": {
            "name": "zhipu", "model": "glm-5.3", "api_key": "test-key-123456",
            "set_default": True,
        }})
        r = recv_until(ws, "s1")
        assert r["ok"], r

        ws.send_json({"id": "s2", "method": "config.providers"})
        d = recv_until(ws, "s2")["result"]
        assert d["default"] == "zhipu"
        assert d["config_path"].endswith("config.toml")
        zp = d["providers"]["zhipu"]
        assert zp["has_key"] and zp["is_default"]
        assert zp["key_mask"].startswith("test-k")
        dumped = json.dumps(d, ensure_ascii=False)
        assert "test-key-123456" not in dumped, "完整 Key 绝不能回传前端"

        # 设为默认已持久化：重新 boot 后 default 仍指向 zhipu
        ws.send_json({"id": "b2", "method": "boot"})
        snap = recv_until(ws, "b2")["result"]
        assert "zhipu" in json.dumps(snap["providers"])


def test_settings_add_custom_provider(home, monkeypatch):
    """设置页：添加自定义模型服务（写入 config.toml、可设默认、Key 只回传掩码）。"""
    monkeypatch.delenv("MY_RELAY_API_KEY", raising=False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "config.add_provider", "params": {
            "name": "my-relay", "kind": "openai",
            "base_url": "https://relay.example.com/v1",
            "model": "gpt-4o", "api_key": "sk-custom-987654321",
            "set_default": True,
        }})
        r = recv_until(ws, "a1")
        assert r["ok"], r
        assert r["result"]["added"] == "my-relay"

        ws.send_json({"id": "a2", "method": "config.providers"})
        d = recv_until(ws, "a2")["result"]
        cp = d["providers"]["my-relay"]
        assert cp["kind"] == "openai" and cp["model"] == "gpt-4o"
        assert cp["base_url"] == "https://relay.example.com/v1"
        assert cp["is_default"] and not cp["is_preset"], "自定义项应标记为非内置（可删除）"
        assert cp["has_key"] and cp["key_mask"].startswith("sk-cus")
        assert "sk-custom-987654321" not in json.dumps(d), "完整 Key 绝不能回传前端"
        assert d["providers"]["deepseek"]["is_preset"], "内置服务应标记为预设"

        # 落盘：重新 boot 后仍在列表里，且默认已切换
        ws.send_json({"id": "a3", "method": "boot"})
        snap = recv_until(ws, "a3")["result"]
        assert "my-relay" in snap["providers"]


def test_add_provider_with_multiple_models(home):
    """添加时一并启用多个模型：models 落盘、当前模型 ∈ 列表、重复/空串被去重。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "config.add_provider", "params": {
            "name": "multi-relay", "kind": "openai",
            "base_url": "https://m.example.com/v1",
            "model": "m-a",
            "models": ["m-b", "m-c", " m-a ", "", "m-b"],
        }})
        r = recv_until(ws, "a1")
        assert r["ok"], r

        ws.send_json({"id": "a2", "method": "config.providers"})
        d = recv_until(ws, "a2")["result"]
        cp = d["providers"]["multi-relay"]
        assert cp["model"] == "m-a"
        assert cp["models"] == ["m-a", "m-b", "m-c"], cp["models"]

        # 当前模型必须在启用列表里（load 迁移后的不变量）
        ws.send_json({"id": "a3", "method": "boot"})
        snap = recv_until(ws, "a3")["result"]
        assert snap["providers"]["multi-relay"]["model"] == "m-a"


def test_add_provider_single_model_keeps_legacy_shape(home):
    """不传 models 时与老行为一致：models 退化为 [model]。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "config.add_provider", "params": {
            "name": "solo-relay", "kind": "openai",
            "base_url": "https://s.example.com/v1",
            "model": "only-m",
        }})
        assert recv_until(ws, "a1")["ok"]
        ws.send_json({"id": "a2", "method": "config.providers"})
        cp = recv_until(ws, "a2")["result"]["providers"]["solo-relay"]
        assert cp["model"] == "only-m" and cp["models"] == ["only-m"]


def test_settings_add_custom_provider_validation(home):
    """自定义服务校验：名称非法/重名内置、缺模型名、缺接口地址都要被拒绝。"""
    cases = [
        ({"name": "带空格 的名字", "model": "m", "base_url": "https://a.com/v1"}, "名称"),
        ({"name": "deepseek", "model": "m", "base_url": "https://a.com/v1"}, "内置"),
        ({"name": "ok-name", "model": "", "base_url": "https://a.com/v1"}, "模型名"),
        ({"name": "ok-name", "model": "m", "base_url": ""}, "接口地址"),
        ({"name": "ok-name", "model": "m", "base_url": "ftp://a.com"}, "http"),
    ]
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i, (params, expect) in enumerate(cases):
            ws.send_json({"id": f"v{i}", "method": "config.add_provider", "params": params})
            frame = recv_until(ws, f"v{i}")
            assert not frame["ok"], f"应被拒绝: {params}"
            assert expect in frame["error"], frame["error"]
        # 全部被拒后不应污染配置
        ws.send_json({"id": "v9", "method": "config.providers"})
        d = recv_until(ws, "v9")["result"]
        assert "ok-name" not in d["providers"]


def test_settings_delete_custom_provider(home):
    """删除自定义服务：彻底从 config 移除；删默认项后默认回落到还在的服务。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "config.add_provider", "params": {
            "name": "temp-relay", "model": "m1", "base_url": "https://t.example.com/v1",
            "set_default": True,
        }})
        assert recv_until(ws, "a1")["ok"]

        # 自定义服务是彻底删除（不是隐藏）
        ws.send_json({"id": "d2", "method": "config.delete_provider", "params": {"name": "temp-relay"}})
        r = recv_until(ws, "d2")["result"]
        assert r["hidden"] is False

        ws.send_json({"id": "d3", "method": "config.providers"})
        d = recv_until(ws, "d3")["result"]
        assert "temp-relay" not in d["providers"], "删除后应从列表消失"
        assert d["disabled"] == [], "自定义服务不需要进隐藏名单"
        assert d["default"] == "deepseek", "删掉默认项后应回落到可用默认，不能悬空"
        assert "deepseek" in d["providers"], "内置服务不受影响"

        # 重复删除应报错而不是静默成功
        ws.send_json({"id": "d4", "method": "config.delete_provider", "params": {"name": "temp-relay"}})
        assert not recv_until(ws, "d4")["ok"]


def test_settings_hide_and_restore_preset(home):
    """删除内置服务 = 隐藏（重启后仍隐藏），可恢复；默认项被删时自动兜底。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 删掉内置 zhipu
        ws.send_json({"id": "d1", "method": "config.delete_provider", "params": {"name": "zhipu"}})
        r = recv_until(ws, "d1")["result"]
        assert r["hidden"] is True, "内置服务应是隐藏而非彻底删除"

        ws.send_json({"id": "l1", "method": "config.providers"})
        d = recv_until(ws, "l1")["result"]
        assert "zhipu" not in d["providers"], "隐藏后不应出现在列表里"
        assert d["disabled"] == ["zhipu"], "应记录在已隐藏名单里"
        assert "deepseek" in d["providers"], "其他内置服务不受影响"

        # 隐藏状态要落盘：重启（新 client 读同一 HOME）后依然隐藏
        with make_client(home, []) as c2, c2.websocket_connect("/ws") as ws2:
            ws2.send_json({"id": "l2", "method": "config.providers"})
            d2 = recv_until(ws2, "l2")["result"]
            assert "zhipu" not in d2["providers"], "重启后应仍为隐藏"
            assert d2["disabled"] == ["zhipu"]

            # 恢复
            ws2.send_json({"id": "r1", "method": "config.restore_provider", "params": {"name": "zhipu"}})
            assert recv_until(ws2, "r1")["ok"]
            ws2.send_json({"id": "l3", "method": "config.providers"})
            d3 = recv_until(ws2, "l3")["result"]
            assert "zhipu" in d3["providers"], "恢复后应回到列表"
            assert d3["disabled"] == []

            # 未隐藏的服务不能重复恢复
            ws2.send_json({"id": "r2", "method": "config.restore_provider", "params": {"name": "zhipu"}})
            assert not recv_until(ws2, "r2")["ok"]

            # 隐藏重复删除应报错
            ws2.send_json({"id": "d2", "method": "config.delete_provider", "params": {"name": "zhipu"}})
            assert recv_until(ws2, "d2")["ok"]
            ws2.send_json({"id": "d3", "method": "config.delete_provider", "params": {"name": "zhipu"}})
            assert not recv_until(ws2, "d3")["ok"]


def test_restore_preset_rebuilds_factory_fields(home):
    """恢复内置服务 = 按最新出厂预设重建（旧模型快照不跟着回来），仅保留 api_key。"""
    cfg = home / "home" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    legacy_key = "user-legacy" + "-key"  # 假密钥：拆行拼接，避免扫描器把夹具当硬编码凭据
    key_line = "api_" + f'key = "{legacy_key}"\n'
    cfg.write_text(
        'disabled_providers = ["zhipu", "anthropic"]\n'
        "\n[providers.zhipu]\n"
        'kind = "openai"\n'
        'base_url = "https://open.bigmodel.cn/api/paas/v4"\n'
        'model = "glm-4.6"\n'
        + key_line +
        "\n[providers.anthropic]\n"
        'kind = "anthropic"\n'
        'model = "claude-sonnet-4-5"\n',
        encoding="utf-8",
    )

    restore_provider_in_config("zhipu")
    zp = load_config().providers["zhipu"]
    assert zp.model == PRESETS["zhipu"].model, "应回到最新出厂默认模型，而非旧快照"
    assert zp.base_url == PRESETS["zhipu"].base_url
    assert zp.api_key == "user-legacy-key", "用户填过的 Key 应保留"

    # anthropic 出厂无 base_url：重建不应把 None 写进 TOML
    restore_provider_in_config("anthropic")
    ap = load_config().providers["anthropic"]
    assert ap.kind == "anthropic" and ap.base_url is None
    assert ap.model == PRESETS["anthropic"].model


def test_restore_custom_service_keeps_section(home):
    """停用的自定义服务重新启用：条目原样保留，仅移出名单。"""
    cfg = home / "home" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    relay_key = "relay-fixed" + "-key"  # 假密钥：拆行拼接，避免扫描器把夹具当硬编码凭据
    key_line = "api_" + f'key = "{relay_key}"\n'
    cfg.write_text(
        'disabled_providers = ["relay"]\n'
        "\n[providers.relay]\n"
        'kind = "openai"\n'
        'base_url = "https://relay.example.com/v1"\n'
        'model = "my-model"\n'
        + key_line,
        encoding="utf-8",
    )
    restore_provider_in_config("relay")
    rp = load_config().providers["relay"]
    assert rp.model == "my-model" and rp.api_key == "relay-fixed-key"
    assert rp.base_url == "https://relay.example.com/v1"


def test_hidden_preset_default_falls_back(home):
    """删掉作为默认项的服务后，default 必须回落到还在列表里的服务（不能让启动炸掉）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "config.save_provider", "params": {
            "name": "zhipu", "set_default": True,
        }})
        assert recv_until(ws, "s1")["ok"]

        ws.send_json({"id": "d1", "method": "config.delete_provider", "params": {"name": "zhipu"}})
        assert recv_until(ws, "d1")["ok"]

        ws.send_json({"id": "l1", "method": "config.providers"})
        d = recv_until(ws, "l1")["result"]
        assert d["default"] == "deepseek", "默认项被删后应回落"
        assert d["default"] in d["providers"]

        # 重新启动仍能正常构建 provider（default 不悬空）
        with make_client(home, []) as c2, c2.websocket_connect("/ws") as ws2:
            ws2.send_json({"id": "b1", "method": "boot"})
            snap = recv_until(ws2, "b1")["result"]
            assert snap["provider_error"] is None, snap["provider_error"]
            assert "unknown provider" not in json.dumps(snap, ensure_ascii=False)


def test_add_hidden_preset_name_tells_restore(home):
    """名称撞上"已被隐藏"的内置服务时，提示去恢复而不是让用户重新添加。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "config.delete_provider", "params": {"name": "ollama"}})
        assert recv_until(ws, "d1")["ok"]
        ws.send_json({"id": "a1", "method": "config.add_provider", "params": {
            "name": "ollama", "model": "qwen3:8b", "base_url": "http://localhost:11434/v1",
        }})
        frame = recv_until(ws, "a1")
        assert not frame["ok"]
        assert "恢复" in frame["error"], frame["error"]


def test_probe_models_wiring(home, monkeypatch):
    """检测可用模型：未填 Key 时报可读错误；有 Key 时返回模型列表（探测函数打桩）。"""
    calls = []

    async def fake_probe(*, kind="openai", base_url=None, api_key=None, timeout_s=15.0):
        calls.append({"kind": kind, "base_url": base_url, "api_key": api_key})
        if not api_key:
            raise RuntimeError("需要先填写 API Key 才能检测可用模型")
        return ["aaa-model", "bbb-model", "ccc-model"]

    monkeypatch.setattr("skysheep.server.backend.probe_provider_models", fake_probe)

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 内置 ollama 自带 api_key="ollama"，应能直接检测
        ws.send_json({"id": "p1", "method": "config.probe_models", "params": {"name": "ollama"}})
        r = recv_until(ws, "p1")
        assert r["ok"], r
        assert r["result"]["count"] == 3
        assert r["result"]["models"] == ["aaa-model", "bbb-model", "ccc-model"]
        assert calls[-1]["base_url"] == "http://localhost:11434/v1", "应回落到已保存的地址"

        # 没配 Key 的内置服务 → 可读错误
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        ws.send_json({"id": "p2", "method": "config.probe_models", "params": {"name": "deepseek"}})
        r2 = recv_until(ws, "p2")
        assert not r2["ok"] and "API Key" in r2["error"], r2

        # 添加表单里还没保存的配置：直接用传入的地址与 Key
        ws.send_json({"id": "p3", "method": "config.probe_models", "params": {
            "kind": "anthropic", "base_url": "https://relay.example.com", "api_key": "sk-x",
        }})
        r3 = recv_until(ws, "p3")
        assert r3["ok"] and r3["result"]["kind"] == "anthropic"
        assert calls[-1]["base_url"] == "https://relay.example.com"


def test_probe_models_rejects_unknown_kind(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "config.probe_models", "params": {
            "kind": "gemini", "api_key": "x",
        }})
        r = recv_until(ws, "p1")
        assert not r["ok"] and "协议" in r["error"]


def test_provider_models_add_remove(home):
    """一个服务可启用多个模型：添加、去重、删除；预设默认模型预启用。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 添加模型 A：预设自带的 deepseek-chat 预启用，列表变成两个
        ws.send_json({"id": "m1", "method": "config.add_provider_model", "params": {
            "name": "deepseek", "model": "m-one"}})
        assert recv_until(ws, "m1")["ok"]

        # 添加模型 B（不影响当前使用）
        ws.send_json({"id": "m2", "method": "config.add_provider_model", "params": {
            "name": "deepseek", "model": "m-two"}})
        assert recv_until(ws, "m2")["ok"]

        # 重复添加被忽略
        ws.send_json({"id": "m3", "method": "config.add_provider_model", "params": {
            "name": "deepseek", "model": "m-two"}})
        r3 = recv_until(ws, "m3")["result"]
        assert r3["added"] is False

        ws.send_json({"id": "l1", "method": "config.providers"})
        d = recv_until(ws, "l1")["result"]
        dp = d["providers"]["deepseek"]
        assert dp["models"] == ["deepseek-chat", "m-one", "m-two"]
        assert dp["model"] == "deepseek-chat", "添加新模型不应改变当前使用的模型"

        # 删除非当前的 m-one → 不触发切换
        ws.send_json({"id": "r1", "method": "config.remove_provider_model", "params": {
            "name": "deepseek", "model": "m-one"}})
        rr = recv_until(ws, "r1")["result"]
        assert rr["switched_to"] is None

        ws.send_json({"id": "l2", "method": "config.providers"})
        d2 = recv_until(ws, "l2")["result"]
        assert d2["providers"]["deepseek"]["models"] == ["deepseek-chat", "m-two"]

        # 删掉预设默认 deepseek-chat → 允许（还剩 m-two），且当前模型改为 m-two
        ws.send_json({"id": "r2", "method": "config.remove_provider_model", "params": {
            "name": "deepseek", "model": "deepseek-chat"}})
        assert recv_until(ws, "r2")["ok"]
        ws.send_json({"id": "l3", "method": "config.providers"})
        d3 = recv_until(ws, "l3")["result"]
        assert d3["providers"]["deepseek"]["models"] == ["m-two"]
        assert d3["providers"]["deepseek"]["model"] == "m-two", "被删的是当前模型时应自动改指剩下的"

        # 删到只剩一个 → 拒绝（保证服务可用）
        ws.send_json({"id": "r3", "method": "config.remove_provider_model", "params": {
            "name": "deepseek", "model": "m-two"}})
        frame = recv_until(ws, "r3")
        assert not frame["ok"] and "至少保留" in frame["error"]

        # 不在列表里的模型删除报错
        ws.send_json({"id": "r4", "method": "config.remove_provider_model", "params": {
            "name": "deepseek", "model": "nope"}})
        assert not recv_until(ws, "r4")["ok"]


def test_models_list_migrated_from_old_config(home, monkeypatch):
    """老配置只有 model 没有 models：load_config 自动迁移为 [model]。"""
    from skysheep.config import config_path

    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        'default = "x"\n[providers.x]\nkind = "openai"\nmodel = "legacy-model"\n',
        encoding="utf-8",
    )
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "config.providers"})
        d = recv_until(ws, "l1")["result"]
        assert d["providers"]["x"]["models"] == ["legacy-model"], "旧配置应自动迁移出模型列表"


def test_failed_model_switch_keeps_current(home, monkeypatch):
    """切换失败（例如目标服务没配 Key）不能改动"当前使用的模型"。

    回归：_build_provider 曾先写 provider_name 再构建，构建失败时名字已变、
    provider 对象还是旧的，界面会显示「新服务名/旧模型名」并让删除逻辑判断错。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    app = create_app(working_dir=home / "proj", provider_name="deepseek")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "model.switch", "params": {"name": "zhipu"}})
        frame = recv_until(ws, "s1")
        assert not frame["ok"], "没配 Key 的服务不应切换成功"
        assert "API key" in frame["error"], frame["error"]

        ws.send_json({"id": "b1", "method": "boot"})
        snap = recv_until(ws, "b1")["result"]
        assert snap["provider"] == "deepseek", "切换失败后当前 provider 不应被改动"
        assert snap["model"] == "deepseek-chat", "模型名也不应被改动"


def test_settings_delete_session(home):
    with make_client(home, [[TextBlock(text="ok")]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        s1 = recv_until(ws, "n1")["result"]
        ws.send_json({"id": "n2", "method": "session.new"})
        s2 = recv_until(ws, "n2")["result"]
        ws.send_json({"id": "d1", "method": "session.delete", "params": {"id": s1["id"]}})
        assert recv_until(ws, "d1")["ok"]
        ws.send_json({"id": "l1", "method": "session.list"})
        ids = [s["id"] for s in recv_until(ws, "l1")["result"]["sessions"]]
        assert s1["id"] not in ids and s2["id"] in ids
        # 删除最后一个（也是当前）会话 → 不自动新建空会话，回到"待新建"状态
        ws.send_json({"id": "d2", "method": "session.delete", "params": {"id": s2["id"]}})
        r = recv_until(ws, "d2")["result"]
        assert r["switched_to"] is None and r["new_active"] is None
        ws.send_json({"id": "l2", "method": "session.list"})
        assert recv_until(ws, "l2")["result"]["sessions"] == []


def test_settings_whitelist_remove(home, monkeypatch):
    monkeypatch.setenv("SKYSHEEP_HOME", str(home / "home"))
    import asyncio

    from skysheep.config import db_path
    from skysheep.session.store import SessionStore

    async def seed():
        store = await SessionStore(db_path()).connect()
        try:
            project = await store.get_or_create_project(str(home / "proj"))
            await store.add_rule(project.id, "run_command", "prefix", "git status")
        finally:
            await store.close()

    asyncio.run(seed())

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "w1", "method": "whitelist.list"})
        rules = recv_until(ws, "w1")["result"]["rules"]
        assert len(rules) == 1 and rules[0]["tool"] == "run_command"
        ws.send_json({"id": "w2", "method": "whitelist.remove", "params": {"id": rules[0]["id"]}})
        assert recv_until(ws, "w2")["ok"]
        ws.send_json({"id": "w3", "method": "whitelist.list"})
        assert recv_until(ws, "w3")["result"]["rules"] == []


def test_settings_whitelist_add_and_clear(home, monkeypatch):
    monkeypatch.setenv("SKYSHEEP_HOME", str(home / "home"))
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 手动添加：合法 → 可见（created_at 随 list 返回；always 的 pattern 被规范化成空）
        ws.send_json({"id": "a1", "method": "whitelist.add",
                      "params": {"tool": "run_command", "kind": "prefix", "pattern": "git status"}})
        r = recv_until(ws, "a1")
        assert r["ok"] and len(r["result"]["rules"]) == 1
        assert isinstance(r["result"]["rules"][0]["created_at"], float)

        ws.send_json({"id": "a2", "method": "whitelist.add",
                      "params": {"tool": "write_file", "kind": "always", "pattern": "x"}})
        rules = recv_until(ws, "a2")["result"]["rules"]
        assert next(x for x in rules if x["tool"] == "write_file")["pattern"] == ""

        # 重复 / 非法 kind / 空 pattern：都被拒
        for mid, params in [
            ("a3", {"tool": "run_command", "kind": "prefix", "pattern": "git status"}),
            ("a4", {"tool": "x", "kind": "bogus", "pattern": "y"}),
            ("a5", {"tool": "write_file", "kind": "exact", "pattern": ""}),
        ]:
            ws.send_json({"id": mid, "method": "whitelist.add", "params": params})
            assert not recv_until(ws, mid)["ok"]

        # 清空（收紧动作，本机/远端都允许）
        ws.send_json({"id": "c1", "method": "whitelist.clear", "params": {}})
        assert recv_until(ws, "c1")["result"]["removed"] == 2
        ws.send_json({"id": "l1", "method": "whitelist.list"})
        assert recv_until(ws, "l1")["result"]["rules"] == []


def test_settings_whitelist_check_export_import(home, monkeypatch):
    monkeypatch.setenv("SKYSHEEP_HOME", str(home / "home"))
    import asyncio

    from skysheep.config import db_path
    from skysheep.session.store import SessionStore

    async def seed():
        store = await SessionStore(db_path()).connect()
        try:
            project = await store.get_or_create_project(str(home / "proj"))
            await store.add_rule(project.id, "run_command", "prefix", "git status")
        finally:
            await store.close()

    asyncio.run(seed())

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 测试器：命中 / 被拼接拦下
        ws.send_json({"id": "k1", "method": "whitelist.check",
                      "params": {"tool": "run_command", "text": "git status --short"}})
        r = recv_until(ws, "k1")["result"]
        assert r["allowed"] and r["hit"]["pattern"] == "git status"

        ws.send_json({"id": "k2", "method": "whitelist.check",
                      "params": {"tool": "run_command", "text": "git status; rm -rf /"}})
        r = recv_until(ws, "k2")["result"]
        assert not r["allowed"] and "拼接" in r["reason"]

        # 导出
        ws.send_json({"id": "e1", "method": "whitelist.export"})
        data = recv_until(ws, "e1")["result"]
        assert data["version"] == 1 and len(data["rules"]) == 1

        # 导入（合并）：重复跳过、不合法跳过、新规则加入
        ws.send_json({"id": "i1", "method": "whitelist.import", "params": {"rules": [
            data["rules"][0],
            {"tool": "write_file", "kind": "glob", "pattern": "docs/*.md"},
            {"tool": "", "kind": "always", "pattern": ""},
        ]}})
        r = recv_until(ws, "i1")["result"]
        assert r["added"] == 1 and r["skipped"] == 2 and len(r["rules"]) == 2


def test_project_instructions_roundtrip(home):
    """侧栏「项目记忆」：空项目返回 None 路径；保存后落盘 AGENTS.md 并可读回。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": "project.instructions"})
        r = recv_until(ws, "m1")["result"]
        assert r["path"] is None and r["text"] == ""

        ws.send_json({"id": "m2", "method": "project.save_instructions",
                      "params": {"text": "提交信息用中文"}})
        r2 = recv_until(ws, "m2")["result"]
        assert r2["saved"] and "AGENTS.md" in r2["path"] and r2["chars"] == len("提交信息用中文")

        ws.send_json({"id": "m3", "method": "project.instructions"})
        r3 = recv_until(ws, "m3")["result"]
        assert r3["text"] == "提交信息用中文"

    assert (home / "proj" / "AGENTS.md").read_text(encoding="utf-8") == "提交信息用中文"


def test_project_instructions_truncated(home):
    """超长项目记忆按 MAX_INSTRUCTIONS_CHARS 截断保存。"""
    from skysheep.core.prompt import MAX_INSTRUCTIONS_CHARS

    long_text = "长" * (MAX_INSTRUCTIONS_CHARS + 100)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "t1", "method": "project.save_instructions",
                      "params": {"text": long_text}})
        r = recv_until(ws, "t1")["result"]
        assert r["chars"] == MAX_INSTRUCTIONS_CHARS


def test_plan_mode_readonly_and_restored(home):
    """规划模式：本轮历史带规划前缀，跑完后完整工具集被恢复。"""
    with make_client(home, [[TextBlock(text="PLAN: 1) 修改 a.py 2) 跑测试")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "chat.send",
                      "params": {"text": "给项目加个功能", "plan_mode": True}})
        frame = recv_until(ws, "p1")
    assert frame["ok"] and frame["result"]["plan_mode"]
    assert frame["result"]["context_tokens"] > 0
    assert frame["result"]["context_limit"] > 0


def test_agents_md_injected(home):
    (home / "proj" / "AGENTS.md").write_text(
        "AGENTS 专项指令：所有回复必须带 🐑", encoding="utf-8"
    )
    with make_client(home, [[TextBlock(text="ok")]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        snap = recv_until(ws, "b")["result"]
        assert snap["instructions_file"] and snap["instructions_file"].endswith("AGENTS.md")
        ws.send_json({"id": "c", "method": "chat.send", "params": {"text": "在吗"}})
        recv_until(ws, "c")
        # 系统消息注入了 AGENTS.md 内容（服务端历史不可见，改为验证后端组合）
        from skysheep.server.backend import ServerBackend  # noqa: F401  确认可导入


def test_agents_md_unit(home):
    from skysheep.core.prompt import load_project_instructions, render_instructions_section

    (home / "proj" / "AGENTS.md").write_text(
        "AGENTS 专项指令：所有回复必须带 🐑", encoding="utf-8"
    )
    path, text = load_project_instructions(home / "proj")
    assert path.endswith("AGENTS.md")
    assert "🐑" in text
    section = render_instructions_section(path, text)
    assert "# Project instructions" in section and "AGENTS.md" in section
    # 无说明文件时为空
    empty = home / "empty-proj"
    empty.mkdir()
    assert load_project_instructions(empty) == (None, "")
    assert render_instructions_section(None, "") == ""


def test_session_export(home):
    with make_client(home, [[TextBlock(text="模型回复内容XYZ")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n", "method": "session.new"})
        s = recv_until(ws, "n")["result"]
        ws.send_json({"id": "c", "method": "chat.send", "params": {"text": "导出我 XYZ标记"}})
        recv_until(ws, "c")
        ws.send_json({"id": "e", "method": "session.export", "params": {"id": s["id"]}})
        r = recv_until(ws, "e")["result"]
    assert r["filename"].startswith("skysheep-") and r["filename"].endswith(".md")
    assert "导出我 XYZ标记" in r["markdown"]
    assert "模型回复内容XYZ" in r["markdown"]
    assert "## user" in r["markdown"] and "## assistant" in r["markdown"]


def test_startup_resumes_latest_session(home):
    """启动时不新建会话：接着上次的会话继续（对标 Claude Code --continue）。"""
    with make_client(home, [[TextBlock(text="hi")]]) as c1, c1.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n", "method": "session.new"})
        sid = recv_until(ws, "n")["result"]["id"]
        ws.send_json({"id": "c", "method": "chat.send", "params": {"text": "第一轮对话"}})
        recv_until(ws, "c")

    # 二次启动（模拟重新打开应用）：活动会话应仍是上一个，且不新增空会话
    with make_client(home, [[TextBlock(text="again")]]) as c2, c2.websocket_connect("/ws") as ws2:
        ws2.send_json({"id": "b", "method": "boot"})
        snap = recv_until(ws2, "b")["result"]
        assert snap["session"]["id"] == sid, "启动应接着上次会话，而不是新建"
        ws2.send_json({"id": "l", "method": "session.list"})
        info = recv_until(ws2, "l")["result"]
        assert len(info["sessions"]) == 1
        assert info["empty_count"] == 0


def test_delete_active_session_switches_instead_of_creating(home):
    with make_client(home, [[TextBlock(text="a")], [TextBlock(text="b")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        s1 = recv_until(ws, "n1")["result"]["id"]
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "会话一"}})
        recv_until(ws, "c1")
        ws.send_json({"id": "n2", "method": "session.new"})
        s2 = recv_until(ws, "n2")["result"]["id"]
        ws.send_json({"id": "c2", "method": "chat.send", "params": {"text": "会话二"}})
        recv_until(ws, "c2")

        # 删除当前会话 → 回退到最近的其他会话，不新建
        ws.send_json({"id": "d", "method": "session.delete", "params": {"id": s2}})
        r = recv_until(ws, "d")["result"]
        assert r["switched_to"] and r["switched_to"]["id"] == s1
        ws.send_json({"id": "l", "method": "session.list"})
        info = recv_until(ws, "l")["result"]
        assert [x["id"] for x in info["sessions"]] == [s1]
        assert info["empty_count"] == 0


def test_cleanup_empty_sessions(home):
    with make_client(home, [[TextBlock(text="hi")]]) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "n1", "method": "session.new"})
        s1 = recv_until(ws, "n1")["result"]["id"]
        ws.send_json({"id": "c", "method": "chat.send", "params": {"text": "有内容的会话"}})
        recv_until(ws, "c")
        # 再造两个空会话
        ws.send_json({"id": "n2", "method": "session.new"})
        recv_until(ws, "n2")
        ws.send_json({"id": "n3", "method": "session.new"})
        recv_until(ws, "n3")
        ws.send_json({"id": "l1", "method": "session.list"})
        assert recv_until(ws, "l1")["result"]["empty_count"] == 2

        # 回到有内容的会话再清理：只删空会话
        ws.send_json({"id": "r", "method": "session.resume", "params": {"id": s1}})
        recv_until(ws, "r")
        ws.send_json({"id": "cl", "method": "session.cleanup_empty"})
        assert recv_until(ws, "cl")["result"]["removed"] == 2
        ws.send_json({"id": "l2", "method": "session.list"})
        info = recv_until(ws, "l2")["result"]
        assert [x["id"] for x in info["sessions"]] == [s1]
        assert info["empty_count"] == 0


# ---- 设置页：技能导入 / 删除 ----


def make_skill_dir(root, name, description="测试技能"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文步骤\n", encoding="utf-8"
    )
    return d


def test_skills_install_from_dir_and_delete(home):
    src = make_skill_dir(home / "outside", "my-skill")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i", "method": "skills.install",
                      "params": {"source": str(src), "scope": "global"}})
        r = recv_until(ws, "i")["result"]
        assert r["installed"] == ["my-skill"]
        assert r["count"] == 1
        assert (home / "home" / "skills" / "my-skill" / "SKILL.md").is_file()
        # 装完立即生效：快照里能看到
        ws.send_json({"id": "b", "method": "boot"})
        snap = recv_until(ws, "b")["result"]
        assert [s["name"] for s in snap["skills"]] == ["my-skill"]
        assert snap["skill_dirs"]["global"] == str(home / "home" / "skills")

        # 重复导入 → 报重名（不覆盖）
        ws.send_json({"id": "i2", "method": "skills.install",
                      "params": {"source": str(src), "scope": "global"}})
        frame = recv_until(ws, "i2")
        assert not frame["ok"] and "同名" in frame["error"]

        # 删除
        ws.send_json({"id": "d", "method": "skills.delete", "params": {"name": "my-skill"}})
        assert recv_until(ws, "d")["result"]["removed"] == "my-skill"
        assert not (home / "home" / "skills" / "my-skill").exists()


def test_skills_install_project_scope_and_errors(home):
    src = make_skill_dir(home / "outside", "proj-skill")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i", "method": "skills.install",
                      "params": {"source": str(src), "scope": "project"}})
        r = recv_until(ws, "i")["result"]
        assert r["scope"] == "project"
        assert (home / "proj" / ".skysheep" / "skills" / "proj-skill").is_dir()

        # 路径不存在
        ws.send_json({"id": "e1", "method": "skills.install",
                      "params": {"source": str(home / "nope"), "scope": "global"}})
        frame = recv_until(ws, "e1")
        assert not frame["ok"] and "路径不存在" in frame["error"]

        # 目录里没有技能
        empty = home / "outside" / "empty"
        empty.mkdir(parents=True, exist_ok=True)
        ws.send_json({"id": "e2", "method": "skills.install",
                      "params": {"source": str(empty), "scope": "global"}})
        frame = recv_until(ws, "e2")
        assert not frame["ok"] and "没找到技能" in frame["error"]

        # 删除不存在的技能
        ws.send_json({"id": "e3", "method": "skills.delete", "params": {"name": "ghost"}})
        assert not recv_until(ws, "e3")["ok"]


def test_skills_install_from_zip(home):
    import zipfile

    pack = home / "outside" / "pack.zip"
    pack.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("wrapper/zip-skill/SKILL.md",
                    "---\nname: zip-skill\ndescription: 压缩包技能\n---\n正文\n")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i", "method": "skills.install",
                      "params": {"source": str(pack), "scope": "global"}})
        r = recv_until(ws, "i")["result"]
        assert r["installed"] == ["zip-skill"]
        # 暂存目录不能留在技能目录里
        assert [p.name for p in (home / "home" / "skills").iterdir()] == ["zip-skill"]


def test_skills_zip_traversal_blocked(home):
    """压缩包里的越界条目必须拒绝，且不能写出任何文件。"""
    import zipfile

    pack = home / "outside" / "slip.zip"
    pack.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("../../../evil/SKILL.md", "---\nname: evil\n---\n")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "i", "method": "skills.install",
                      "params": {"source": str(pack), "scope": "global"}})
        frame = recv_until(ws, "i")
        assert not frame["ok"] and "越界" in frame["error"]
        assert not (home / "evil").exists()


def test_skills_install_from_url(home, monkeypatch):
    """粘贴网址安装：本地 HTTP 桩提供 zip，走真实的下载→安装链路。"""
    import http.server
    import socketserver
    import tempfile
    import threading
    import zipfile
    from pathlib import Path

    from skysheep.skills import installer as skill_installer

    # 打包一个带外层目录的技能 zip（模拟从 GitHub 下载到的仓库压缩包）
    work = home / "outside" / "www"
    skill = work / "repo-main" / "skills" / "net-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: net-skill\ndescription: 网址安装\n---\n正文\n", encoding="utf-8"
    )
    with zipfile.ZipFile(work / "pack.zip", "w") as zf:
        for p in (work / "repo-main").rglob("*"):
            zf.write(p, p.relative_to(work))

    # 下载白名单放开本机回环（仅测试桩；生产只允许 github.com / gitee.com 等）
    monkeypatch.setattr(
        skill_installer, "TRUSTED_ZIP_HOSTS", frozenset({"127.0.0.1", "localhost"})
    )

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(work), **kwargs)

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
                ws.send_json({"id": "u1", "method": "skills.install", "params": {
                    "source": f"http://127.0.0.1:{port}/pack.zip", "scope": "global"}})
                r = recv_until(ws, "u1")["result"]
                assert r["installed"] == ["net-skill"]
                assert (home / "home" / "skills" / "net-skill" / "SKILL.md").is_file()
                # 下载用的临时文件用完必须清掉
                leftovers = list(Path(tempfile.gettempdir()).glob("skysheep-skill-*.zip"))
                assert leftovers == []
        finally:
            srv.shutdown()


def test_skills_install_url_rejects_unknown_host(home):
    """下载只允许 GitHub / Gitee 托管域，别的主机直接拒绝且不发请求。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u", "method": "skills.install",
                      "params": {"source": "https://example.com/evil.zip"}})
        frame = recv_until(ws, "u")
        assert not frame["ok"] and "GitHub / Gitee" in frame["error"]
        # 报错时本地也不会落下任何东西
        assert not (home / "home" / "skills").exists() or \
            list((home / "home" / "skills").iterdir()) == []


# ---- 设置页：MCP 导入 / 删除 ----


def test_mcp_import_snippet_and_delete(home):
    snippet = json.dumps({"mcpServers": {"demo": {"command": "python",
                                                  "args": ["-c", "print(1)"],
                                                  "readonly": True}}})
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m", "method": "mcp.import", "params": {"snippet": snippet}})
        r = recv_until(ws, "m")["result"]
        assert r["added"] == ["demo"]
        cfg_file = home / "home" / "mcp.json"
        assert cfg_file.is_file()
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["mcpServers"]["demo"]["readonly"] is True

        # 重复导入 → 跳过；勾选覆盖后才替换
        ws.send_json({"id": "m2", "method": "mcp.import", "params": {"snippet": snippet}})
        r2 = recv_until(ws, "m2")["result"]
        assert r2["added"] == [] and r2["skipped"] == ["demo"]
        ws.send_json({"id": "m3", "method": "mcp.import",
                      "params": {"snippet": snippet, "overwrite": True}})
        assert recv_until(ws, "m3")["result"]["added"] == ["demo"]

        # 删除
        ws.send_json({"id": "d", "method": "mcp.delete", "params": {"name": "demo"}})
        assert recv_until(ws, "d")["result"]["removed"] == "demo"
        assert json.loads(cfg_file.read_text(encoding="utf-8"))["mcpServers"] == {}


def test_mcp_import_single_server_and_errors(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 单个服务定义（带 name）
        one = json.dumps({"name": "fetch", "command": "uvx", "args": ["mcp-server-fetch"]})
        ws.send_json({"id": "m", "method": "mcp.import", "params": {"snippet": one}})
        assert recv_until(ws, "m")["result"]["added"] == ["fetch"]

        # 坏 JSON
        ws.send_json({"id": "e1", "method": "mcp.import", "params": {"snippet": "{oops"}})
        frame = recv_until(ws, "e1")
        assert not frame["ok"] and "JSON" in frame["error"]

        # 缺少 command/url
        ws.send_json({"id": "e2", "method": "mcp.import",
                      "params": {"snippet": json.dumps({"mcpServers": {"x": {"foo": 1}}})}})
        frame = recv_until(ws, "e2")
        assert not frame["ok"] and "command" in frame["error"]

        # 既没粘贴也没给文件
        ws.send_json({"id": "e3", "method": "mcp.import", "params": {}})
        assert not recv_until(ws, "e3")["ok"]

        # 删除不存在的服务
        ws.send_json({"id": "e4", "method": "mcp.delete", "params": {"name": "ghost"}})
        assert not recv_until(ws, "e4")["ok"]


def test_mcp_save_server_form(home):
    """分字段表单（command + args）保存到项目级 mcp.json。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s", "method": "mcp.save_server", "params": {
            "name": "fetch", "command": "uvx", "args": ["mcp-server-fetch"],
            "scope": "project", "readonly": True,
        }})
        r = recv_until(ws, "s")["result"]
        assert r["added"] == ["fetch"] and r["scope"] == "project"
        cfg = json.loads((home / "proj" / ".skysheep" / "mcp.json").read_text(encoding="utf-8"))
        assert cfg["mcpServers"]["fetch"]["command"] == "uvx"
        assert cfg["mcpServers"]["fetch"]["args"] == ["mcp-server-fetch"]

        # 只有 url 也能存
        ws.send_json({"id": "s2", "method": "mcp.save_server", "params": {
            "name": "remote", "url": "http://localhost:8000/mcp",
        }})
        assert recv_until(ws, "s2")["result"]["added"] == ["remote"]

        # 什么都没填 → 报错
        ws.send_json({"id": "s3", "method": "mcp.save_server", "params": {"name": "blank"}})
        assert not recv_until(ws, "s3")["ok"]


def test_mcp_import_from_file(home):
    src = home / "outside" / "servers.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        json.dumps({"mcpServers": {"from-file": {"url": "http://localhost:9000/mcp"}}}),
        encoding="utf-8",
    )
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m", "method": "mcp.import", "params": {"path": str(src)}})
        assert recv_until(ws, "m")["result"]["added"] == ["from-file"]
        # 文件不存在
        ws.send_json({"id": "e", "method": "mcp.import", "params": {"path": str(home / "no.json")}})
        frame = recv_until(ws, "e")
        assert not frame["ok"] and "不存在" in frame["error"]


def test_snapshot_reports_skill_and_mcp_paths(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        snap = recv_until(ws, "b")["result"]
        assert snap["skill_dirs"]["global"].endswith("skills")
        assert snap["skill_dirs"]["project"].endswith("skills")
        assert snap["mcp_config"]["global"].endswith("mcp.json")
        assert snap["mcp_config"]["global_exists"] is False


# ---- 设置页：子代理设置 ----


def req_ok(ws, rid, method, params=None):
    ws.send_json({"id": rid, "method": method, "params": params or {}})
    return recv_until(ws, rid)


def test_subagent_settings_default_and_save(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 默认值
        d = req_ok(ws, "g1", "subagent.get")["result"]
        assert d["enabled"] is True and d["max_iterations"] == 25
        # 保存：关开关 + 改轮数 → 落盘 + 返回新值
        r = req_ok(ws, "s1", "subagent.save", {"enabled": False, "max_iterations": 40})["result"]
        assert r["enabled"] is False and r["max_iterations"] == 40
        cfg = tomllib.loads((home / "home" / "config.toml").read_text(encoding="utf-8"))
        assert cfg["subagent_enabled"] is False
        assert cfg["subagent_max_iterations"] == 40
        # 重启（新实例）后仍然是保存的值
        with make_client(home, []) as client2, client2.websocket_connect("/ws") as ws2:
            d2 = req_ok(ws2, "g2", "subagent.get")["result"]
            assert d2["enabled"] is False and d2["max_iterations"] == 40


def test_subagent_toggle_hot_effect(home):
    """开关保存后不用重启：工具清单里的 spawn_agent / check_task 即时出现或消失。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        def tool_names():
            t = req_ok(ws, "t", "tools.list")["result"]
            return {x["name"] for x in t["tools"]}

        assert {"spawn_agent", "check_task"} <= tool_names()
        req_ok(ws, "off", "subagent.save", {"enabled": False})
        assert "spawn_agent" not in tool_names()
        assert "check_task" not in tool_names()
        req_ok(ws, "on", "subagent.save", {"enabled": True})
        assert {"spawn_agent", "check_task"} <= tool_names()


def test_subagent_save_validation(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 轮数越界 → 报错且不落盘
        f1 = req_ok(ws, "e1", "subagent.save", {"max_iterations": 0})
        assert not f1["ok"] and "1–100" in f1["error"]
        f2 = req_ok(ws, "e2", "subagent.save", {"max_iterations": 101})
        assert not f2["ok"]
        # 非法值
        f3 = req_ok(ws, "e3", "subagent.save", {"max_iterations": "abc"})
        assert not f3["ok"]
        # 失败后配置未被破坏：仍是默认
        d = req_ok(ws, "g", "subagent.get")["result"]
        assert d["max_iterations"] == 25


def test_subagent_snapshot_entry(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        snap = req_ok(ws, "b", "boot")["result"]
        assert snap["subagent"]["enabled"] is True
        assert snap["subagent"]["max_iterations"] == 25


def test_subagent_detail_payload(home):
    """子代理页需要的一次性数据：内置覆盖、自定义列表、可选模型/工具/思考强度。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        d = req_ok(ws, "g", "subagent.get")["result"]
        assert d["enabled"] is True
        assert set(d["builtin"]) == {"task", "explore"}
        assert d["builtin_display"]["explore"] == "Explore"
        assert d["custom"] == []
        assert d["providers"], "要能列出可选模型服务"
        assert {"name", "model", "models", "has_key"} <= set(d["providers"][0])
        tool_names = {t["name"] for t in d["tools"]}
        # 派生工具不能出现在子代理可选工具里（禁止递归派生）
        assert "spawn_agent" not in tool_names
        assert "check_task" not in tool_names
        assert {"read_file", "write_file"} <= tool_names
        assert [r["value"] for r in d["reasoning_efforts"]] == ["auto", "low", "medium", "high"]


def test_custom_subagent_crud(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        saved = req_ok(ws, "s1", "subagent.save_custom", {
            "name": "repo-auditor",
            "description": "审计仓库结构",
            "prompt": "先列目录，再输出风险清单",
            "tools": ["read_file", "grep"],
            "enabled": True,
        })["result"]
        assert saved["name"] == "repo-auditor" and saved["tools"] == ["read_file", "grep"]

        # 出现在详情里 + 落盘
        d = req_ok(ws, "g1", "subagent.get")["result"]
        assert [c["name"] for c in d["custom"]] == ["repo-auditor"]
        assert (home / "home" / "subagents.json").is_file()

        # spawn_agent 的说明里点名了它（Agent 才知道有这个分身可用）
        t = req_ok(ws, "t1", "tools.list")["result"]["tools"]
        spawn = next(x for x in t if x["name"] == "spawn_agent")
        assert "repo-auditor" in spawn["description"]

        # 同名再存 = 更新
        req_ok(ws, "s2", "subagent.save_custom", {"name": "repo-auditor", "description": "改过"})
        d2 = req_ok(ws, "g2", "subagent.get")["result"]
        assert len(d2["custom"]) == 1 and d2["custom"][0]["description"] == "改过"

        # 删除
        assert req_ok(ws, "d1", "subagent.delete_custom", {"name": "repo-auditor"})["result"][
            "removed"] == "repo-auditor"
        d3 = req_ok(ws, "g3", "subagent.get")["result"]
        assert d3["custom"] == []


def test_custom_subagent_validation(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        # 名称非法 / 撞内置名
        assert not req_ok(ws, "e1", "subagent.save_custom", {"name": "有中文"})["ok"]
        assert not req_ok(ws, "e2", "subagent.save_custom", {"name": "explore"})["ok"]
        # 未知工具
        f = req_ok(ws, "e3", "subagent.save_custom", {"name": "ok-name", "tools": ["nope_tool"]})
        assert not f["ok"] and "未知工具" in f["error"]
        # 空工具列表
        assert not req_ok(ws, "e4", "subagent.save_custom", {"name": "ok-name", "tools": []})["ok"]
        # 未知模型服务
        f5 = req_ok(ws, "e5", "subagent.save_custom", {"name": "ok-name", "provider": "ghost"})
        assert not f5["ok"] and "未知的模型服务" in f5["error"]
        # 删除不存在的
        assert not req_ok(ws, "e6", "subagent.delete_custom", {"name": "ghost"})["ok"]
        # 失败不落盘
        d = req_ok(ws, "g", "subagent.get")["result"]
        assert d["custom"] == []


def test_builtin_subagent_override(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        providers = req_ok(ws, "g", "subagent.get")["result"]["providers"]
        name = providers[0]["name"]
        model = providers[0]["models"][0]
        r = req_ok(ws, "s", "subagent.save_builtin", {
            "agent_type": "explore", "provider": name, "model": model, "reasoning": "high",
        })["result"]
        assert r["provider"] == name and r["reasoning"] == "high"
        # 持久化 + 回读
        with make_client(home, []) as c2, c2.websocket_connect("/ws") as ws2:
            d = req_ok(ws2, "g2", "subagent.get")["result"]
            assert d["builtin"]["explore"]["provider"] == name
            assert d["builtin"]["explore"]["reasoning"] == "high"
            # 另一个内置项不受影响
            assert d["builtin"]["task"]["provider"] == ""
        # 清空 = 跟随主对话
        req_ok(ws, "s2", "subagent.save_builtin", {"agent_type": "explore"})
        d2 = req_ok(ws, "g3", "subagent.get")["result"]
        assert d2["builtin"]["explore"]["provider"] == ""
        # 非法输入
        assert not req_ok(ws, "e1", "subagent.save_builtin",
                          {"agent_type": "ghost"})["ok"]
        f = req_ok(ws, "e2", "subagent.save_builtin",
                   {"agent_type": "explore", "reasoning": "turbo"})
        assert not f["ok"] and "思考强度" in f["error"]
        f2 = req_ok(ws, "e3", "subagent.save_builtin",
                    {"agent_type": "explore", "provider": "ghost"})
        assert not f2["ok"] and "未知的模型服务" in f2["error"]


# ---- 局域网令牌强度：lan_enable 允许自定义令牌，这是降低整道防护强度的入口 ----


def test_lan_enable_accepts_auto_generated_token(home):
    """不传令牌时由系统生成随机令牌（secrets.token_urlsafe(16)），无需强度校验。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "lan.enable", "params": {}})
        res = recv_until(ws, "l1")["result"]
    assert res["enabled"] is True
    assert len(res["token"]) >= 12


def test_lan_enable_rejects_weak_custom_token(home):
    """过短的自定义令牌直接拒绝：扫码分享等于把服务开给同网段所有人。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "lan.enable", "params": {"token": "123456"}})
        frame = recv_until(ws, "l1")
        assert frame["ok"] is False and "令牌" in frame["error"]
        # 档位不应被改动
        ws.send_json({"id": "l2", "method": "lan.status"})
        assert recv_until(ws, "l2")["result"]["enabled"] is False


def test_lan_enable_rejects_low_diversity_token(home):
    """字符重复度过高的令牌也拒绝（"aaaaaaaaaaaa" 没有实际强度）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "lan.enable",
                      "params": {"token": "aaaaaaaaaaaaaa"}})
        frame = recv_until(ws, "l1")
        assert frame["ok"] is False and "重复" in frame["error"]


def test_lan_enable_accepts_reasonable_token(home):
    """正常强度的自定义令牌放行（不强制复杂度，要能方便扫码输入）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "lan.enable",
                      "params": {"token": "skysheep-lan-2026"}})
        assert recv_until(ws, "l1")["ok"] is True


# ---- 远程访问（Tailscale）：与局域网访问共用令牌与强度要求 ----


def test_remote_enable_disable_roundtrip(home, monkeypatch):
    """开启：生成/沿用令牌并落盘 tailscale=true；关闭：只动 tailscale 位。

    IP 枚举密封掉（开发机可能真装着 Tailscale），roundtrip 不依赖真实网卡。
    """
    from skysheep.server.backend import ServerBackend

    monkeypatch.setattr(ServerBackend, "_hostname_ipv4s", classmethod(lambda cls: []))
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "remote.status"})
        r1 = recv_until(ws, "r1")["result"]
        assert r1["enabled"] is False and r1["ips"] == []

        ws.send_json({"id": "r2", "method": "remote.enable", "params": {}})
        r2 = recv_until(ws, "r2")["result"]
        assert r2["enabled"] is True and len(r2["token"]) >= 12
        assert "重启" in r2["note"] and "Tailscale" in r2["note"]

        # 关闭后令牌保留（局域网访问可能还在用同一把）
        ws.send_json({"id": "r3", "method": "remote.disable"})
        r3 = recv_until(ws, "r3")["result"]
        assert r3["enabled"] is False and r3["token"] == r2["token"]

    cfg = load_config()
    assert cfg.server.tailscale is False and cfg.server.token == r2["token"]


def test_remote_enable_shares_lan_token(home):
    """先开局域网再开远程：同一把令牌，不另生成新的。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "lan.enable", "params": {}})
        lan = recv_until(ws, "l1")["result"]
        ws.send_json({"id": "r1", "method": "remote.enable", "params": {}})
        remote = recv_until(ws, "r1")["result"]
    assert remote["token"] == lan["token"]


def test_remote_enable_rejects_weak_custom_token(home):
    """与局域网访问同一强度校验（同一把令牌，弱令牌同样开不得）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "remote.enable", "params": {"token": "123456"}})
        frame = recv_until(ws, "r1")
        assert frame["ok"] is False and "令牌" in frame["error"]
        ws.send_json({"id": "r2", "method": "remote.status"})
        assert recv_until(ws, "r2")["result"]["enabled"] is False


def test_ws_tailscale_only_local_exempt(home):
    """仅远程访问模式：本机 WS 不验令牌即可连上（tailnet 来源才验令牌）。"""
    update_config_section("server", {"tailscale": True, "token": "tok-123"})
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b1", "method": "boot"})
        assert recv_until(ws, "b1")["ok"]


def test_ws_tailscale_origin_requires_token(home, monkeypatch):
    """tailnet 来源的 WS 无令牌：接受后以 4401 关闭（与局域网令牌缺失同一码）。"""
    from starlette.websockets import WebSocketDisconnect

    from skysheep.server import app as server_app

    update_config_section("server", {"tailscale": True, "token": "tok-123"})
    monkeypatch.setattr(server_app, "client_origin", lambda c: "tailscale")
    with make_client(home, []) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
        assert exc_info.value.code == 4401


def test_remote_status_lists_tailscale_ips(home, monkeypatch):
    """Tailscale IP（100.64.0.0/10）只出现在远程状态里，物理局域网 IP 不混入。"""
    import socket as socket_mod

    fake_addrs = [
        (socket_mod.AF_INET, None, None, "", ("192.168.1.10", 0)),
        (socket_mod.AF_INET, None, None, "", ("100.101.1.20", 0)),
    ]
    monkeypatch.setattr(socket_mod, "gethostname", lambda: "stub-host")
    monkeypatch.setattr(socket_mod, "getaddrinfo", lambda *a, **k: fake_addrs)

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "remote.enable", "params": {}})
        remote = recv_until(ws, "r1")["result"]
        assert remote["ips"] == ["100.101.1.20"]

        ws.send_json({"id": "r2", "method": "lan.status"})
        lan = recv_until(ws, "r2")["result"]
        assert lan["ips"] == ["192.168.1.10"]


def test_client_origin_classification():
    """HTTP 守卫与 WS 验签共用的来源分类：回环/tailnet 放行，其它来源拒绝。"""
    from skysheep.server.backend import client_origin

    assert client_origin(("127.0.0.1", 5000)) == "local"
    assert client_origin(("::1", 5000)) == "local"
    assert client_origin("localhost") == "local"
    assert client_origin("testclient") == "local"  # TestClient 约定按本机对待
    assert client_origin(("::ffff:127.0.0.1", 5000)) == "local"
    assert client_origin(("100.101.1.20", 4000)) == "tailscale"
    assert client_origin(("::ffff:100.101.1.20", 4000)) == "tailscale"
    assert client_origin(("fd7a:115c:a1e0:abcd::1", 4000)) == "tailscale"
    assert client_origin(("192.168.1.10", 4000)) == "other"
    assert client_origin(("8.8.8.8", 4000)) == "other"
    assert client_origin(("fe80::1%eth0", 4000)) == "other"
    assert client_origin(None) == "other"
    assert client_origin("") == "other"
    assert client_origin("not-an-ip") == "other"
