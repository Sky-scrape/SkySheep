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


# ---- 手改 config.toml 不炸启动（M13 口径延伸到小节模型构造） ----


def test_load_config_wraps_wrong_value_types_as_config_error(home):
    """小节已知字段的值类型写错：报带小节名的 ConfigError，不再裸抛 ValidationError。"""
    import pytest

    from skysheep.config import ConfigError, config_path

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[providers.deepseek]\nmodels = "deepseek-chat"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config()
    assert "[providers.deepseek]" in str(ei.value)

    # 已过滤未知键的小节（roundtable）同样要套住值类型错误
    p.write_text('[roundtable]\nmax_members = "abc"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config()
    assert "[roundtable]" in str(ei.value)

    p.write_text("[memory_maintenance]\ninterval_hours = \"abc\"\n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config()
    assert "[memory_maintenance]" in str(ei.value)


def test_load_config_still_ignores_unknown_keys(home):
    """未知键依旧忽略（向前兼容旧文件），正常配置不受影响。"""
    from skysheep.config import config_path

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '[providers.deepseek]\nsome_future_field = 1\nmodels = ["deepseek-chat"]\n'
        '[server]\nanother_future_flag = true\n',
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.providers["deepseek"].models == ["deepseek-chat"]
    assert cfg.server.lan is False


def test_set_provider_models_rejects_empty_list(home):
    """空模型列表显式报 ConfigError（不变量修复路径取 [0] 曾踩空 IndexError）。"""
    import pytest

    from skysheep.config import ConfigError, set_provider_models_in_config

    with pytest.raises(ConfigError, match="至少保留一个启用的模型"):
        set_provider_models_in_config("customprov", [])

    # 正常写入：models 落盘，当前模型被删时换到列表第一个（不变量保持）
    from skysheep.config import config_path

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[providers.customprov]\nkind = "openai"\nmodel = "m1"\n', encoding="utf-8")
    set_provider_models_in_config("customprov", ["m2", "m3"])
    import tomllib

    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    section = raw["providers"]["customprov"]
    assert section["models"] == ["m2", "m3"]
    assert section["model"] == "m2", "当前模型被删后应换到已启用列表第一个"
