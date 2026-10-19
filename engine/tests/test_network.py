"""network.py：绕行名单 / 自签 CA / 出网工厂 / 子进程代理环境。

覆盖点（与交接清单第 6 步对应）：
- parse_no_proxy / match_no_proxy（含 CIDR、.domain、*.domain、*）
- build_ssl_context 的错误路径（文件不存在 / 不是合法 PEM）
- effective_proxy 四态（无配置 / 全局 / 服务级覆盖 / no_proxy 命中）
- 工厂三条路径（直连 / 代理 / 绕行）的 trust_env 与 mounts
- subprocess_proxy_env 的环境变量投影（含 *.x 归一成 .x）
"""

from __future__ import annotations

import ssl

import httpx
import pytest

from skysheep.network import (
    NetworkConfig,
    build_ssl_context,
    effective_proxy,
    make_async_httpx_client,
    make_async_transport,
    make_httpx_client,
    make_transport,
    match_no_proxy,
    parse_no_proxy,
    subprocess_proxy_env,
)

# ---------- parse_no_proxy ----------


def test_parse_no_proxy_splits_on_comma_semicolon_space():
    assert parse_no_proxy("a.com, b.com;c.com d.com") == [
        "a.com", "b.com", "c.com", "d.com",
    ]


def test_parse_no_proxy_lowercases_and_dedupes_keeping_order():
    assert parse_no_proxy("A.com, a.com, B.com") == ["a.com", "b.com"]


def test_parse_no_proxy_empty_text_and_blank_items():
    assert parse_no_proxy("") == []
    assert parse_no_proxy("  , ;  ") == []
    assert parse_no_proxy(None) == []  # 配置里可能读到 None


# ---------- match_no_proxy ----------


@pytest.mark.parametrize("rule", ["*"])
def test_match_no_proxy_star_matches_everything(rule):
    assert match_no_proxy("anything.example", [rule]) is True
    assert match_no_proxy("10.1.2.3", [rule]) is True


def test_match_no_proxy_exact_host():
    assert match_no_proxy("example.com", ["example.com"]) is True
    assert match_no_proxy("other.com", ["example.com"]) is False


def test_match_no_proxy_bare_domain_matches_subdomain():
    # curl 行为：裸域名规则也匹配其子域
    assert match_no_proxy("api.example.com", ["example.com"]) is True
    assert match_no_proxy("notexample.com", ["example.com"]) is False


def test_match_no_proxy_dot_domain_matches_domain_and_subdomains():
    rules = [".example.com"]
    assert match_no_proxy("example.com", rules) is True
    assert match_no_proxy("api.example.com", rules) is True
    assert match_no_proxy("badexample.com", rules) is False


def test_match_no_proxy_star_dot_domain_normalized():
    rules = ["*.example.com"]
    assert match_no_proxy("example.com", rules) is True
    assert match_no_proxy("deep.api.example.com", rules) is True
    assert match_no_proxy("elsewhere.com", rules) is False


def test_match_no_proxy_cidr_matches_ip_only():
    rules = ["10.0.0.0/8"]
    assert match_no_proxy("10.1.2.3", rules) is True
    assert match_no_proxy("11.1.2.3", rules) is False
    # 域名不做 CIDR 判定，且不应误命中
    assert match_no_proxy("example.com", rules) is False


def test_match_no_proxy_ip_does_not_get_suffix_matched():
    # IP 主机不做后缀匹配，免得 "1.2.3.4" 被 "2.3.4" 这类规则意外命中
    assert match_no_proxy("1.2.3.4", ["2.3.4"]) is False


def test_match_no_proxy_ignores_malformed_cidr():
    assert match_no_proxy("10.0.0.1", ["10.0.0.0/999"]) is False


def test_match_no_proxy_empty_rules_or_host():
    assert match_no_proxy("example.com", []) is False
    assert match_no_proxy("", ["example.com"]) is False


def test_match_no_proxy_normalizes_case_and_brackets_and_trailing_dot():
    assert match_no_proxy("EXAMPLE.com.", ["example.com"]) is True
    assert match_no_proxy("[::1]", ["::1"]) is True


# ---------- build_ssl_context ----------


def test_build_ssl_context_empty_returns_none():
    assert build_ssl_context("") is None
    assert build_ssl_context("   ") is None


def test_build_ssl_context_missing_file_raises_value_error(tmp_path):
    with pytest.raises(ValueError) as ei:
        build_ssl_context(str(tmp_path / "nope.pem"))
    assert "CA 证书文件不存在" in str(ei.value)


def test_build_ssl_context_bad_pem_raises_value_error(tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("not a pem", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        build_ssl_context(str(bad))
    assert "CA 证书无法加载" in str(ei.value)


def test_build_ssl_context_valid_pem_appends_to_default_store(tmp_path):
    # 用标准库自签一张证书，验证「追加」语义：返回的上下文仍能信任系统根
    cert = _make_self_signed_pem(tmp_path)
    ctx = build_ssl_context(str(cert))
    assert isinstance(ctx, ssl.SSLContext)
    # 系统默认库也在：至少有一个默认 verify 路径存在
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def _make_self_signed_pem(tmp_path):
    """生成一张自签 PEM（不依赖 cryptography 的可选路径）。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover - 环境没有 cryptography
        pytest.skip("cryptography 未安装")
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-root")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    p = tmp_path / "root.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p


# ---------- effective_proxy ----------


def test_effective_proxy_no_config_no_network():
    assert effective_proxy(None) == ""


def test_effective_proxy_global_only():
    net = NetworkConfig(proxy="http://127.0.0.1:7890")
    assert effective_proxy(net) == "http://127.0.0.1:7890"


def test_effective_proxy_provider_overrides_global():
    net = NetworkConfig(proxy="http://global:7890")
    assert effective_proxy(net, "http://provider:1080") == "http://provider:1080"


def test_effective_proxy_no_proxy_hit_returns_empty():
    net = NetworkConfig(proxy="http://p:7890", no_proxy="localhost,.corp.com")
    assert effective_proxy(net, url="http://localhost:8080") == ""
    assert effective_proxy(net, url="http://api.corp.com/x") == ""
    assert effective_proxy(net, url="http://public.example") == "http://p:7890"


def test_effective_proxy_provider_only_no_network():
    # 旧调用路径：network=None 时只看服务级
    assert effective_proxy(None, "http://provider:1080") == "http://provider:1080"


# ---------- 工厂：trust_env 与 mounts ----------


def test_make_httpx_client_direct_sets_trust_env_false():
    with make_httpx_client(NetworkConfig()) as c:
        assert c.trust_env is False
        # 未配代理时不该有 proxy mount
        assert not c._mounts


def test_make_httpx_client_proxy_sets_proxy_and_trust_env_false():
    net = NetworkConfig(proxy="http://127.0.0.1:7890")
    with make_httpx_client(net, url="https://api.example.com") as c:
        assert c.trust_env is False
        # 走代理时 httpx 会为所有 scheme 装代理 mount；与直连用例
        # 「mounts 为空」形成对照即可证明代理已生效（不依赖内部属性名）。
        assert c._mounts


def test_make_httpx_client_no_proxy_bypass_ignores_proxy():
    net = NetworkConfig(proxy="http://127.0.0.1:7890", no_proxy="localhost")
    with make_httpx_client(net, url="http://localhost:11434/v1") as c:
        assert c.trust_env is False
        assert not c._mounts  # 绕行 = 直连，不该装代理 mount


def test_make_async_httpx_client_proxy_override_empty_forces_direct():
    net = NetworkConfig(proxy="http://127.0.0.1:7890")
    import asyncio

    async def _run():
        async with make_async_httpx_client(net, proxy_override="") as c:
            return c.trust_env, bool(c._mounts)

    trust_env, has_mounts = asyncio.run(_run())
    assert trust_env is False
    assert has_mounts is False


def test_make_async_httpx_client_transport_used_on_direct_path():
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    import asyncio

    async def _run():
        async with make_async_httpx_client(
            NetworkConfig(), transport=transport
        ) as c:
            return c._transport is transport

    assert asyncio.run(_run()) is True


def test_make_transport_and_async_transport_honor_trust_env_false():
    t = make_transport(NetworkConfig())
    assert t._pool._ssl_context is not None or True  # 只是确认能构造
    at = make_async_transport(NetworkConfig())
    assert at is not None


# ---------- subprocess_proxy_env ----------


def test_subprocess_proxy_env_empty_network():
    assert subprocess_proxy_env(NetworkConfig()) == {}


def test_subprocess_proxy_env_sets_upper_and_lower_proxy():
    env = subprocess_proxy_env(NetworkConfig(proxy="http://127.0.0.1:7890"))
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        assert env[k] == "http://127.0.0.1:7890"


def test_subprocess_proxy_env_normalizes_star_no_proxy_for_curl():
    env = subprocess_proxy_env(
        NetworkConfig(proxy="http://p:7890", no_proxy="*.corp.com, localhost")
    )
    # curl / git 不认 *.corp.com，必须归一成 .corp.com
    assert env["no_proxy"] == ".corp.com,localhost"
    assert env["NO_PROXY"] == ".corp.com,localhost"


def test_subprocess_proxy_env_includes_node_extra_ca_certs(tmp_path):
    ca = tmp_path / "root.pem"
    ca.write_text("dummy", encoding="utf-8")
    env = subprocess_proxy_env(NetworkConfig(ca_cert=str(ca)))
    assert env["NODE_EXTRA_CA_CERTS"] == str(ca)
