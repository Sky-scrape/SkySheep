"""内置预设重构：默认页签的 10 家厂商、label 显示名与老配置兼容。

- 预设顺序/名单对齐界面「默认」页签（用户指定顺序），label 提供中文/品牌显示名；
- openrouter / siliconflow 移出预设：老配置里的条目保留为自定义服务（Key 不丢）；
- providers_detail 回传 label，前端列表与顶栏模型菜单都用它显示。
"""

from __future__ import annotations

from skysheep.config import PRESETS, load_config

# 用户指定的默认页签顺序（末尾附加的 ollama 是本地模型入口，不占厂商位）
EXPECTED_ORDER = [
    "anthropic", "openai", "google", "xai", "minimax",
    "deepseek", "zhipu", "moonshot", "qwen", "mimo",
]


def test_preset_order_and_labels():
    keys = [k for k in PRESETS if k in EXPECTED_ORDER]
    assert keys == EXPECTED_ORDER, f"预设顺序应与界面默认页签一致：{keys}"
    assert PRESETS["zhipu"].label == "智谱"
    assert PRESETS["moonshot"].label == "Kimi"
    assert PRESETS["mimo"].label == "小米 Mimo"
    assert PRESETS["anthropic"].label == "Anthropic"
    assert PRESETS["deepseek"].label == "Deepseek"
    # 聚合平台移出预设（老配置里的条目自动归自定义页签）
    assert "openrouter" not in PRESETS and "siliconflow" not in PRESETS
    # 本地模型入口保留
    assert "ollama" in PRESETS
    # 新增预设协议与端点
    assert PRESETS["anthropic"].kind == "anthropic"
    for key in ("openai", "google", "xai", "minimax", "qwen", "mimo"):
        assert PRESETS[key].kind == "openai", f"{key} 走 OpenAI 兼容协议"
        assert PRESETS[key].base_url, f"{key} 应带 base_url"


def test_label_field_defaults_and_merge(home):
    # 预设自带 label；自定义服务不填 label 时后端/前端回落到配置键名
    cfg = load_config()
    assert cfg.providers["zhipu"].label == "智谱"

    from skysheep.config import _read_raw_config, _write_raw_config
    p, raw = _read_raw_config()
    raw.setdefault("providers", {})["myrelay"] = {
        "kind": "openai", "base_url": "https://relay.example.com/v1",
        "api_key": "sk-x", "model": "m1",
    }
    _write_raw_config(p, raw)

    cfg = load_config()
    assert cfg.providers["myrelay"].label == ""
    assert cfg.providers["zhipu"].label == "智谱", "文件条目不覆盖预设 label"


def test_providers_detail_exposes_label(home):
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "config.providers", "params": {}})
        got = recv_until(ws, "p1")["result"]["providers"]
        assert got["zhipu"]["label"] == "智谱"
        assert got["mimo"]["label"] == "小米 Mimo"
        assert got["deepseek"]["is_preset"] is True
