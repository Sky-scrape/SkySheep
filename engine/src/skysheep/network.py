"""全局出网设置：代理 / 绕行名单 / 自签 CA。

设计要点：
- 本模块只负责「按 NetworkConfig 造 httpx 客户端」，不持有单例、不读 config.toml。
  调用方从 SkySheepConfig.network 拿到配置再传进来，测试可直接构造 NetworkConfig
  注入，无需碰真实配置文件。
- trust_env 一律为 False：系统环境变量（HTTP_PROXY / HTTPS_PROXY / NO_PROXY）
  不得悄悄改变出网行为。用户要代理就在设置页里配，配了什么就是什么——这也让
  「关掉代理」变得可验证，而不是被一个残留的环境变量悄悄接管。
- no_proxy 命中即直连。支持 host、.domain / domain 后缀、CIDR 网段、* 全绕，
  与 curl / requests 的常见写法兼容（暂不支持 host:port 形式）。
- ca_cert 是「追加」语义：在系统默认证书库之上再信任这张证书，对齐
  NODE_EXTRA_CA_CERTS。直接给 httpx 传 verify="path.pem" 是「替换」语义，
  用户配企业根证书后会把公网证书链打断，因此这里显式构造 SSLContext。
- transport 与 proxy 在 httpx 0.27 互斥（传 proxy 会重建 transport）。走代理时
  DNS 由代理解析，调用方传入的钉连 transport 已无意义，本模块直接忽略它——
  这是语义正确的取舍，调用方无需自行判断。
"""

from __future__ import annotations

import ipaddress
import re
import ssl
from pathlib import Path

import httpx
from pydantic import BaseModel

#: 允许的代理协议（与 config.update_provider_in_config 的校验口径一致）
ALLOWED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


class NetworkConfig(BaseModel):
    """config.toml 的 [network] 表：全局出网设置。

    与 ProviderConfig.proxy 的分工：这里是「整台应用怎么出网」的全局口径
    （模型请求、联网搜索、web_fetch、MCP、命令子进程都遵循）；ProviderConfig.proxy
    是单个模型服务的覆盖，只作用于该服务的 SDK 客户端。
    """

    # 代理地址（http:// / https:// / socks5://）。空 = 直连，且不读系统代理
    proxy: str = ""
    # 绕行名单（逗号 / 分号 / 空格分隔）。命中的主机一律直连，即使配了代理
    no_proxy: str = ""
    # 额外信任的 CA 证书文件（PEM）。空 = 只用系统证书库
    ca_cert: str = ""

    def proxy_url(self) -> str:
        """归一后的代理地址（去空白）；空串表示直连。"""
        return self.proxy.strip()

    def no_proxy_rules(self) -> list[str]:
        return parse_no_proxy(self.no_proxy)


def parse_no_proxy(text: str) -> list[str]:
    """把配置里的绕行名单拆成小写规则列表（去重保序）。

    分隔符兼容 curl 的习惯：逗号、分号、空白都算。空项丢弃。
    """
    out: list[str] = []
    for raw in re.split(r"[,;\s]+", text or ""):
        item = raw.strip().lower()
        if item and item not in out:
            out.append(item)
    return out


def match_no_proxy(host: str, rules: list[str]) -> bool:
    """判断主机是否命中绕行名单。

    规则形态（与 curl 对齐）：
    - 「*」：全部直连
    - 「example.com」：精确匹配，且匹配其子域（api.example.com）
    - 「.example.com」/「*.example.com」：匹配该域及其子域
    - 「10.0.0.0/8」：CIDR 网段（IP 主机才判定）

    已知局限：不支持「host:port」形式——代理绕行按主机判定已经够用，
    加端口维度会让规则语义变复杂，且 httpx 的 proxy 本身也不按端口分流。
    """
    if not rules:
        return False
    h = (host or "").strip().lower().strip("[]").rstrip(".")
    if not h:
        return False
    if "*" in rules:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    for rule in rules:
        if rule.startswith("*."):
            rule = rule[1:]  # *.example.com -> .example.com
        if rule.startswith("."):
            base = rule[1:]
            if base and (h == base or h.endswith(rule)):
                return True
            continue
        if rule == h:
            return True
        if ip is not None and "/" in rule:
            try:
                if ip in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                pass  # 写坏的网段规则：跳过，不影响其它规则
            continue
        # 裸域名规则也匹配其子域（curl 行为）；IP 主机不做后缀匹配，
        # 免得 "1.2.3.4" 被 "2.3.4" 这类规则意外命中
        if ip is None and "/" not in rule and h.endswith("." + rule):
            return True
    return False


def build_ssl_context(ca_cert: str) -> ssl.SSLContext | None:
    """把 ca_cert 追加到系统默认证书库，返回 SSLContext；空配置返回 None。

    文件不存在或不是合法 PEM 时抛 ValueError（中文可读）——这属于配置错误，
    由调用方决定是否包成 ConfigError。不缓存：证书文件很小，每次重建的代价
    远低于「用户在设置页改了证书但进程里还是旧的那张」带来的困惑。
    """
    path = (ca_cert or "").strip()
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        raise ValueError(
            "CA 证书文件不存在：" + path + "（请在 设置 · 网络 里重新选择，或清空该配置）"
        )
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(cafile=str(p))
    except (ssl.SSLError, OSError) as e:
        raise ValueError("CA 证书无法加载（" + path + "）：" + str(e)) from e
    return ctx


#: 子进程里应被覆盖的代理类环境变量（大小写各一份，兼容 curl / git / requests）
PROXY_ENV_NAMES = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def subprocess_proxy_env(network: NetworkConfig) -> dict[str, str]:
    """把全局出网设置翻译成子进程可用的环境变量。

    与 httpx 工厂的 trust_env=False 同一口径：子进程的代理行为只能来自
    设置页里的配置，不能来自宿主机残留的 HTTP_PROXY。调用方（
    tools/shell.child_environment）负责先清掉继承环境里的同名变量，再把
    本函数返回的键值合并进去。

    大小写各设一份：curl / git / wget 认小写，部分程序只认大写；NO_PROXY
    规范成逗号分隔（与 curl 的 no_proxy 格式一致）。CA 证书通过
    NODE_EXTRA_CA_CERTS 交给 Node（对齐 config 里 ca_cert 的「追加信任」语义）。
    """
    out: dict[str, str] = {}
    proxy = network.proxy_url()
    if proxy:
        for name in ("http_proxy", "https_proxy", "all_proxy"):
            out[name] = proxy
            out[name.upper()] = proxy
    rules = network.no_proxy_rules()
    if rules:
        # curl / git 的 no_proxy 认 `.example.com` 与 `example.com`，不认 `*.example.com`；
        # 这里把通配前缀归一掉，保证子进程与 httpx 侧的绕行判定一致。
        normalized = [r[1:] if r.startswith("*.") else r for r in rules]
        joined = ",".join(normalized)
        out["no_proxy"] = joined
        out["NO_PROXY"] = joined
    ca = (network.ca_cert or "").strip()
    if ca:
        out["NODE_EXTRA_CA_CERTS"] = ca
    return out


def _verify(network: NetworkConfig) -> bool | ssl.SSLContext:
    """httpx 的 verify 取值：True（系统库）或追加了自签 CA 的 SSLContext。"""
    ctx = build_ssl_context(network.ca_cert)
    return True if ctx is None else ctx


def make_transport(network: NetworkConfig) -> httpx.HTTPTransport:
    """造一个遵守全局 CA 设置的同步 transport（给需要自管 transport 的调用方）。"""
    return httpx.HTTPTransport(verify=_verify(network), trust_env=False)


def make_async_transport(network: NetworkConfig) -> httpx.AsyncHTTPTransport:
    """造一个遵守全局 CA 设置的异步 transport（web_fetch 装钉连 backend 用）。"""
    return httpx.AsyncHTTPTransport(verify=_verify(network), trust_env=False)


def _host_of(url: str) -> str:
    try:
        return httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001 - 坏 URL 交给 httpx 在请求时报可读错误
        return ""


def _client_kwargs(
    network: NetworkConfig,
    *,
    url: str | None,
    transport: object | None,
    timeout: float | httpx.Timeout | None,
    headers: dict[str, str] | None,
    follow_redirects: bool,
    proxy_override: str | None = None,
) -> dict:
    """组装 Client / AsyncClient 的公共参数。

    直连与代理两条路径都在这里定死 trust_env=False，调用方无从漏掉。
    """
    kw: dict = {
        "timeout": timeout,
        "headers": headers,
        "follow_redirects": follow_redirects,
    }
    proxy = network.proxy_url() if proxy_override is None else str(proxy_override).strip()
    bypass = bool(url) and match_no_proxy(_host_of(url), network.no_proxy_rules())
    if proxy and not bypass:
        kw["proxy"] = proxy
        kw["trust_env"] = False
        # 走代理：忽略调用方的 transport（见模块 docstring 的说明）
    else:
        kw["trust_env"] = False
        if transport is not None:
            kw["transport"] = transport
        verify = _verify(network)
        if verify is not True and transport is None:
            kw["verify"] = verify
    return kw


def make_httpx_client(
    network: NetworkConfig,
    *,
    url: str | None = None,
    transport: object | None = None,
    timeout: float | httpx.Timeout | None = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    proxy_override: str | None = None,
) -> httpx.Client:
    """按全局出网设置造同步客户端。

    url：本次请求的目标（用于判定 no_proxy）。多目标场景可传 None，
        此时配了代理就走代理、没配就直连。
    transport：仅在直连且未配 CA 时生效；走代理时被忽略（见模块 docstring）。
    """
    return httpx.Client(**_client_kwargs(
        network, url=url, transport=transport, timeout=timeout,
        headers=headers, follow_redirects=follow_redirects,
        proxy_override=proxy_override,
    ))


def make_async_httpx_client(
    network: NetworkConfig,
    *,
    url: str | None = None,
    transport: object | None = None,
    timeout: float | httpx.Timeout | None = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    proxy_override: str | None = None,
) -> httpx.AsyncClient:
    """按全局出网设置造异步客户端（参数语义同 make_httpx_client）。"""
    return httpx.AsyncClient(**_client_kwargs(
        network, url=url, transport=transport, timeout=timeout,
        headers=headers, follow_redirects=follow_redirects,
        proxy_override=proxy_override,
    ))


# ---------- httpx2 工厂（anthropic SDK 1.5+ 强制绑定 httpx2） ----------
# httpx2 是 mcp 可选依赖的传递依赖，未必装了；且导入有成本。调用方要 httpx2
# 客户端时才惰性导入——与 mcp/client.py 的 # noqa: PLC0415 同一惯例。


def make_async_httpx2_client(
    network: NetworkConfig,
    *,
    url: str | None = None,
    transport: object | None = None,
    timeout: object | None = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    proxy_override: str | None = None,
):
    """按全局出网设置造 httpx2 异步客户端（参数语义同 make_async_httpx_client）。

    anthropic SDK 1.5 起强制要求 httpx2.AsyncClient，传 httpx 会抛 TypeError；
    openai SDK 两者都收。因此调用方按目标 SDK 选工厂，语义完全一致。
    """
    import httpx2  # noqa: PLC0415 - 惰性导入，见上方注释

    kw: dict = {
        "timeout": timeout,
        "headers": headers,
        "follow_redirects": follow_redirects,
    }
    proxy = network.proxy_url() if proxy_override is None else str(proxy_override).strip()
    bypass = bool(url) and match_no_proxy(_host_of(url), network.no_proxy_rules())
    if proxy and not bypass:
        kw["proxy"] = proxy
        kw["trust_env"] = False
    else:
        kw["trust_env"] = False
        if transport is not None:
            kw["transport"] = transport
        verify = _verify(network)
        if verify is not True and transport is None:
            kw["verify"] = verify
    return httpx2.AsyncClient(**kw)


def effective_proxy(
    network: NetworkConfig | None, provider_proxy = "", *, url: str | None = None
) -> str:
    """算出某次请求实际该用的代理地址（空串 = 直连）。

    优先级：服务级 provider_proxy > 全局 network.proxy。两处都配时服务级胜出——
    用户为某个服务单独填代理，意图就是「这个服务走别的出口」。no_proxy 命中时
    一律返回空串（即使配了代理也直连）。

    network 为 None 时退化为「只看服务级」，供尚未接全局配置的旧调用路径使用。
    """
    proxy = str(provider_proxy or "").strip()
    if not proxy and network is not None:
        proxy = network.proxy_url()
    if not proxy:
        return ""
    if network is not None and url and match_no_proxy(_host_of(url), network.no_proxy_rules()):
        return ""
    return proxy
