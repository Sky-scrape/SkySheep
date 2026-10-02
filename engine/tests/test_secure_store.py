"""凭据 DPAPI 加密落盘：secure_store 往返 + config 读写透明化 + 一次性迁移。

Windows 本机直接跑真实 DPAPI（CryptProtectData/CryptUnprotectData）；非
Windows（或 crypt32 不可用）的路径用 monkeypatch sys.platform 覆盖，断言
「不加密、不崩、按明文现状」。所有用例走 SKYSHEEP_HOME 隔离，不碰真实
~/.skysheep。
"""

from __future__ import annotations

import base64
import sys

import pytest

from skysheep import config, secure_store
from skysheep.config import (
    _read_raw_config,
    _write_raw_config,
    config_path,
    load_config,
)
from skysheep.textio import write_text_atomic

DPAPI = pytest.mark.skipif(
    not secure_store.dpapi_available(),
    reason="本机没有 DPAPI（非 Windows 或 crypt32 加载失败）",
)


@pytest.fixture(autouse=True)
def _reset_migration_flag():
    """迁移是模块级 run-once：每个用例前复位，互不串味。"""
    config._migration_attempted = False
    yield
    config._migration_attempted = False


def _write_config(raw_text: str):
    p = config_path()
    write_text_atomic(p, raw_text)
    return p


def _read_config_text() -> str:
    return config_path().read_text(encoding="utf-8")


# ---- secure_store 本体 ----


@DPAPI
def test_roundtrip_real_dpapi():
    enc = secure_store.encrypt_value("sk-test-secret-123")
    assert enc.startswith(secure_store.DPAPI_PREFIX), "密文必须带 dpapi: 前缀"
    assert enc != "sk-test-secret-123", "密文不能等于明文"
    assert secure_store.decrypt_value(enc) == "sk-test-secret-123", "往返必须一致"
    # 中文等多字节字符也走 utf-8 往返
    token = "bot_token-令牌-🔑"
    assert secure_store.decrypt_value(secure_store.encrypt_value(token)) == token


@DPAPI
def test_encrypt_idempotent_and_empty():
    once = secure_store.encrypt_value("sk-x")
    assert secure_store.encrypt_value(once) == once, "对已加密值再加密必须幂等"
    assert secure_store.encrypt_value("") == "", "空值不加密"
    assert secure_store.decrypt_value("plain-key") == "plain-key", "明文直通"


@DPAPI
def test_corrupt_ciphertext_returns_none():
    assert secure_store.decrypt_value("dpapi:!!!not-base64!!!") is None
    garbage = base64.b64encode(b"garbage-bytes-not-dpapi").decode("ascii")
    assert secure_store.decrypt_value("dpapi:" + garbage) is None


def test_channel_secret_key_selection():
    hit = ("app_secret", "secret", "bot_token", "ilink_bot_token", "token", "password")
    miss = ("app_id", "url", "base_url", "cursor", "allowed_ids", "allowed_tools",
            "cli_path", "enabled", "approve_enabled")
    for key in hit:
        assert secure_store.is_channel_secret_key(key), key
    for key in miss:
        assert not secure_store.is_channel_secret_key(key), key


# ---- config 读写透明化 ----


@DPAPI
def test_read_raw_config_transparent_decrypt(home):
    enc = secure_store.encrypt_value("sk-hidden-1")
    _write_config(
        f'[providers.myrelay]\nkind = "openai"\napi_key = "{enc}"\nmodel = "m1"\n'
    )
    _, raw = _read_raw_config()
    assert raw["providers"]["myrelay"]["api_key"] == "sk-hidden-1", (
        "_read_raw_config 必须解密后返回明文（调用方零改动）"
    )


@DPAPI
def test_write_raw_config_encrypts_and_reread_equal(home):
    p, raw = _read_raw_config()
    raw["providers"] = {"myrelay": {"kind": "openai", "api_key": "sk-write-1", "model": "m1"}}
    _write_raw_config(p, raw)
    text = _read_config_text()
    assert "sk-write-1" not in text, "明文不能落盘"
    assert secure_store.DPAPI_PREFIX in text, "落盘值必须带 dpapi: 前缀"
    _, reread = _read_raw_config()
    assert reread["providers"]["myrelay"]["api_key"] == "sk-write-1", "二次读取必须等值"


@DPAPI
def test_load_config_decrypts_provider_server_and_speech(home):
    _write_config(
        f'[providers.myrelay]\nkind = "openai"\n'
        f'api_key = "{secure_store.encrypt_value("sk-load-1")}"\nmodel = "m1"\n'
        f"[server]\ntoken = \"{secure_store.encrypt_value('tok-load-1')}\"\n"
        f"[speech]\nprovider = \"custom\"\nbase_url = \"https://asr.example.com\"\n"
        f"api_key = \"{secure_store.encrypt_value('sk-asr-1')}\"\n"
    )
    cfg = load_config()
    assert cfg.providers["myrelay"].api_key == "sk-load-1"
    assert cfg.server.token == "tok-load-1"
    assert cfg.speech.api_key == "sk-asr-1"


@DPAPI
def test_write_does_not_mutate_caller_dict(home):
    """写侧在深拷贝上加密：调用方手里的 raw / 返回值保持明文。"""
    p, raw = _read_raw_config()
    raw["providers"] = {"myrelay": {"kind": "openai", "api_key": "sk-keep-1", "model": "m1"}}
    _write_raw_config(p, raw)
    assert raw["providers"]["myrelay"]["api_key"] == "sk-keep-1", "入参 dict 不能被就地改成密文"


# ---- 一次性迁移 ----


@DPAPI
def test_migration_encrypts_once_and_idempotent(home):
    _write_config(
        'default = "myrelay"\n'
        '[providers.myrelay]\nkind = "openai"\nbase_url = "https://x.example.com"\n'
        'api_key = "sk-migrate-1"\nmodel = "m1"\n'
        "[channels.platforms.feishu]\nenabled = true\n"
        'app_id = "cli_x"\napp_secret = "shsec-migrate-1"\n'
        'url = "https://open.feishu.cn/hook/x"\n'
    )
    before = _read_config_text()
    cfg = load_config()
    text = _read_config_text()
    assert text != before, "首次读到明文凭据必须迁移写回"
    assert "sk-migrate-1" not in text and "shsec-migrate-1" not in text, "明文必须消失"
    assert text.count(secure_store.DPAPI_PREFIX) == 2, "两个凭据字段都要加密"
    assert "cli_x" in text and "https://open.feishu.cn/hook/x" in text, (
        "app_id / url 是标识与地址，保持明文便于排障"
    )
    assert config._migration_attempted, "迁移后 run-once 标志必须置位"
    assert cfg.providers["myrelay"].api_key == "sk-migrate-1"
    assert cfg.channels.platforms["feishu"]["app_secret"] == "shsec-migrate-1"

    # 幂等：已无明文时不再重写（置位与复位标志都一样）
    after_first = _read_config_text()
    load_config()
    assert _read_config_text() == after_first
    config._migration_attempted = False
    load_config()
    assert _read_config_text() == after_first


@DPAPI
def test_migration_also_fires_on_read_raw_config_path(home):
    _write_config(
        '[providers.myrelay]\nkind = "openai"\napi_key = "sk-via-raw"\nmodel = "m1"\n'
    )
    _read_raw_config()
    text = _read_config_text()
    assert "sk-via-raw" not in text and secure_store.DPAPI_PREFIX in text
    assert config._migration_attempted


@DPAPI
def test_migration_skipped_on_non_windows(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _write_config(
        '[providers.myrelay]\nkind = "openai"\napi_key = "sk-linux-plain"\nmodel = "m1"\n'
    )
    cfg = load_config()
    text = _read_config_text()
    assert "sk-linux-plain" in text, "非 Windows 不加密，保持明文现状"
    assert secure_store.DPAPI_PREFIX not in text
    assert cfg.providers["myrelay"].api_key == "sk-linux-plain"
    assert not config._migration_attempted, "非 Windows 不触发迁移"


# ---- 损坏密文与非 Windows 解密 ----


def test_corrupt_ciphertext_blank_on_load_keep_on_raw(home):
    garbage = base64.b64encode(b"garbage-bytes-not-dpapi").decode("ascii")
    _write_config(
        '[providers.relay_a]\nkind = "openai"\napi_key = "dpapi:!!!bad!!!"\nmodel = "m1"\n'
        f'[providers.relay_b]\nkind = "openai"\napi_key = "dpapi:{garbage}"\nmodel = "m1"\n'
    )
    cfg = load_config()
    assert cfg.providers["relay_a"].api_key == "", "坏 base64 必须置空，不能把密文当 Key"
    assert cfg.providers["relay_b"].api_key == "", "解不开的密文必须置空"
    _, raw = _read_raw_config()
    assert raw["providers"]["relay_a"]["api_key"] == "dpapi:!!!bad!!!", (
        "读改写路径保留密文原值：这台机器解不开也不抹掉，写回原样透传"
    )
    assert raw["providers"]["relay_b"]["api_key"] == "dpapi:" + garbage


def test_non_windows_decrypt_and_write(home, monkeypatch):
    """非 Windows：dpapi 值解不开置空（不崩），写侧保持明文。"""
    monkeypatch.setattr(sys, "platform", "linux")
    enc = secure_store.encrypt_value("sk-any")  # 非 Windows：原样返回明文
    assert enc == "sk-any"
    garbage = base64.b64encode(b"garbage").decode("ascii")
    _write_config(
        f'[providers.relay_a]\nkind = "openai"\napi_key = "dpapi:{garbage}"\nmodel = "m1"\n'
    )
    cfg = load_config()
    assert cfg.providers["relay_a"].api_key == "", "没有 DPAPI 时加密值按未配置处理"
    _, raw = _read_raw_config()
    raw["providers"]["relay_a"]["api_key"] = "sk-plain-fallback"
    _write_raw_config(config_path(), raw)
    assert secure_store.DPAPI_PREFIX not in _read_config_text(), "非 Windows 写侧不加密"
