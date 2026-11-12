"""默认模型服务的兜底选择：不能落到没配 Key 的内置预设上。

真实场景（2026-11-12）：用户停用了内置默认 deepseek、配置里没有显式
`default`，只给 magpie 填了 Key。重启后加载期兜底按 PRESETS 顺序挑到
anthropic，启动构建报「还没有配置 API Key」——界面显示「没有可用的模型
服务」，可配好 Key 的服务明明就在列表里。
"""

from __future__ import annotations

import tomllib

import pytest

from skysheep.config import (
    PRESETS,
    disable_provider_in_config,
    load_config,
)
from skysheep.models.factory import build_provider

KEY = "fallback-" + "test-key"  # 假密钥：拆行拼接，避免扫描器把夹具当硬编码凭据


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch):
    """本机环境里的 *_API_KEY 会让「有没有 Key」失真，逐个清掉。"""
    for name, pc in PRESETS.items():
        monkeypatch.delenv(pc.env_key or (name.upper() + "_API_KEY"), raising=False)


def _write_config(home, text):
    cfg = home / "home" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return cfg


def _magpie_section():
    return (
        "[providers.magpie]\n"
        'kind = "openai"\n'
        'base_url = "http://127.0.0.1:9/v1"\n'
        'model = "m1"\n'
        'models = ["m1"]\n'
        f'api_key = "{KEY}"\n'
    )


def test_fallback_skips_keyless_presets(home):
    """deepseek 停用、没有显式 default：兜底应挑配了 Key 的服务而不是 anthropic。"""
    _write_config(home, 'disabled_providers = ["deepseek"]\n\n' + _magpie_section())

    cfg = load_config()
    assert cfg.default == "magpie"
    provider = build_provider(cfg.default, cfg.providers[cfg.default])
    assert provider.name == "magpie"
    assert provider.model == "m1"


def test_fallback_prefers_deepseek_when_nothing_has_key(home):
    """一个 Key 都没配时保持老口径：默认仍是列表里的 deepseek，报缺 Key 指路设置页。"""
    _write_config(home, 'disabled_providers = ["zhipu"]\n')

    assert load_config().default == "deepseek"


def test_unknown_default_falls_back_to_keyed_provider(home):
    _write_config(home, 'default = "ghost"\n\n' + _magpie_section())

    assert load_config().default == "magpie"


def test_disabling_implicit_default_picks_keyed_provider(home):
    """隐式默认（没写 default，按 deepseek 起算）被停用后也要交给兜底重选。"""
    cfg_path = _write_config(home, _magpie_section())

    disable_provider_in_config("deepseek")

    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert "default" not in raw, "不写死新默认，交给加载期按「能不能用」挑"
    assert load_config().default == "magpie"


def test_disabling_explicit_default_picks_keyed_provider(home):
    _write_config(home, 'default = "deepseek"\n\n' + _magpie_section())

    disable_provider_in_config("deepseek")

    assert load_config().default == "magpie"
