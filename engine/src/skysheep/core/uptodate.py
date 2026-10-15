"""更新检查：对照 GitHub Releases 的最新版本号，提示用户有新版可升级。

克制原则：启动后在后台查一次（几秒超时、失败完全静默——离线/仓库不存在
都不打扰），结果随 boot 快照带到前端展示；设置 · 关于里可手动再查。

不走 api.github.com：匿名接口按出口 IP 每小时 60 次限流，国内共享出口很
容易被同 IP 的其他人耗尽，一超就 403。改请求 releases/latest 页面本身——
仓库有 release 时它 302 到 /releases/tag/<tag>，Location 里就带版本号，
页面跳转没有这份配额。代价是拿不到 release 说明与附件清单：说明置空，
安装包地址按发布约定（SkySheep-<版本>-setup.exe，见 installer.iss）拼出。

渠道（2026-10，配合「更新可信度」落地）：
- stable（默认）：上面的 302 路径。GitHub 的 /releases/latest 天然排除预发布，
  稳定渠道不受测试版干扰；
- beta：改读 releases.atom 订阅（同样不走 api.github.com，无该配额问题），
  条目按时间倒序，第一条就是最新 release（含预发布）。

两个更新源 URL 都是硬编码常量、不进 config.toml——更新来源固定是安全姿态：
不让「改配置指向别处」成为供应链投毒的入口；tag 合法性由 is_safe_tag 把关，
更新源被控时宁可拒绝更新也不装来路不明的包。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from urllib.parse import unquote, urljoin

import httpx

RELEASES_LATEST = "https://github.com/Sky-scrape/SkySheep/releases/latest"
RELEASES_FEED = "https://github.com/Sky-scrape/SkySheep/releases.atom"
DOWNLOAD_BASE = "https://github.com/Sky-scrape/SkySheep/releases/download"
TIMEOUT_S = 6.0
SETUP_ASSET_SUFFIX = "-setup.exe"

# 渠道白名单：稳定版走 /releases/latest，测试版走 releases.atom（见模块 docstring）
CHANNELS = ("stable", "beta")

_TAG_IN_LOCATION_RE = re.compile(r"/releases/tag/([^/?#]+)")
# Atom 条目里的 tag 链接（releases/tag/<tag>）；tag 本体的合法性仍由 _TAG_SAFE_RE 把关
_TAG_IN_FEED_RE = re.compile(r"releases/tag/([^<>\"'\s]+)")
# 完整版本形态：v 前缀 + 数字主版本 + 可选 -预发布后缀（2.6.0-beta.1）。
# 旧实现整串抓数字，"2.6.0-beta1" 会把后缀里的 1 也算进版本段，预发布
# 与正式版永远比不出先后——beta 渠道上「装了 beta1 就再也收不到正式版」。
_VERSION_FULL_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$")
# tag/version 只允许这些字符：它们会被拼进文件路径与 `cmd /c` 命令行，
# 一旦出现 `"` `&` `%` `/` `\` 就能注入额外命令或改写落点。更新源被控时
# 这是纵深防御的最后一道（宁可不更新，也不要装一个来路不明的包）。
_TAG_SAFE_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,64}$")


def is_safe_tag(tag: str) -> bool:
    """tag/version 是否能安全地拼进路径与命令行。"""
    return bool(_TAG_SAFE_RE.match(tag or ""))


def _split_version(v: str) -> tuple[tuple[int, ...], str | None]:
    """'v2.6.0-beta.1' -> ((2, 6, 0), 'beta.1')；'2.6.0' -> ((2, 6, 0), None)。

    解析不出（含空串）返回 ((), None)——调用方把它当「不是版本号」处理。
    """
    m = _VERSION_FULL_RE.match((v or "").strip())
    if not m:
        return (), None
    return tuple(int(x) for x in m.group(1).split(".")), m.group(2)


def _pre_key(identifier: str) -> tuple[int, int, str]:
    """semver 预发布标识符的排序键：数字标识符 < 字母数字标识符（semver §11）。"""
    if identifier.isdigit():
        return (0, int(identifier), "")
    return (1, 0, identifier)


def _compare_prerelease(a: str | None, b: str | None) -> int:
    """比较两个预发布后缀：a 新于 b 返回 1、旧于返回 -1、同级 0。

    semver 规则：正式版（无后缀）新于一切预发布；两个预发布逐标识符比，
    数字按数值比；一方标识符更多且前缀全等时多者新。
    """
    if a == b:
        return 0
    if a is None:
        return 1
    if b is None:
        return -1
    pa, pb = a.split("."), b.split(".")
    for x, y in zip(pa, pb, strict=False):
        kx, ky = _pre_key(x), _pre_key(y)
        if kx != ky:
            return -1 if kx < ky else 1
    if len(pa) != len(pb):
        return 1 if len(pa) > len(pb) else -1
    return 0


def is_newer_version(remote: str, local: str) -> bool:
    """remote 版本是否严格新于本地版本。

    主版本数字段逐位比较、缺省段当 0；数字段相等时按 semver 预发布规则
    分先后——正式版新于预发布，预发布之间按后缀标识符比。这样 beta 渠道
    的 2.6.0-beta1 → 2.6.0 会被识别为更新（旧实现两者视为相等，beta 用户
    永远收不到「转正」推送）。
    """
    rt, rp = _split_version(remote or "")
    lt, lp = _split_version(local or "")
    if not rt:
        return False
    n = max(len(rt), len(lt))
    rt += (0,) * (n - len(rt))
    lt += (0,) * (n - len(lt))
    if rt != lt:
        return rt > lt
    return _compare_prerelease(rp, lp) > 0


def release_from_redirect(latest_url: str, location: str) -> dict:
    """从 /releases/latest 的跳转目标还原 release 信息（纯函数，便于测试）。

    没有 /releases/tag/<tag> 形态的跳转目标（仓库还没有 release 时
    /releases/latest 直接 200）抛 RuntimeError。
    """
    target = urljoin(latest_url, location or "")
    m = _TAG_IN_LOCATION_RE.search(target)
    if not m:
        raise RuntimeError("更新源没有版本号")
    tag = unquote(m.group(1))
    version = tag.lstrip("vV")
    # 先校验再拼路径：不合法的 tag 直接当作「更新源异常」，不进文件系统也不进命令行
    if not is_safe_tag(tag) or not is_safe_tag(version):
        raise RuntimeError("更新源返回的版本号含非法字符，已忽略")
    return {"version": version, "tag": tag, "url": target, "notes": "",
            "setup_url": f"{DOWNLOAD_BASE}/{tag}/SkySheep-{version}{SETUP_ASSET_SUFFIX}"}


def release_from_feed(feed_text: str, feed_url: str) -> dict:
    """从 releases.atom 订阅正文还原最新 release（含预发布；纯函数，便于测试）。

    Atom 条目按时间倒序，正文里第一个 releases/tag/<tag> 链接就是最新一条
    （feed 自身的 /releases 链接不带 /tag/，不会误中）。没有 tag（仓库还没
    有 release）或 tag 非法都抛 RuntimeError——beta 渠道与稳定渠道同款
    fail closed：更新源看不懂就不更新。
    """
    m = _TAG_IN_FEED_RE.search(feed_text or "")
    if not m:
        raise RuntimeError("更新源没有版本号")
    tag = unquote(m.group(1))
    version = tag.lstrip("vV")
    # 先校验再拼路径：不合法的 tag 直接当作「更新源异常」，不进文件系统也不进命令行
    if not is_safe_tag(tag) or not is_safe_tag(version):
        raise RuntimeError("更新源返回的版本号含非法字符，已忽略")
    return {"version": version, "tag": tag, "url": feed_url, "notes": "",
            "setup_url": f"{DOWNLOAD_BASE}/{tag}/SkySheep-{version}{SETUP_ASSET_SUFFIX}"}


# ---- 出网回退（2026-10，接「系统代理用户检查更新 ConnectTimeout」的反馈）----
#
# 直连是更新链的首选路径，也是绝大多数用户的现状；但 httpx 不读 Windows 系统代理，
# trust_env=False 又特意不读环境变量（与 web_fetch 同口径：不让环境里的代理/netrc
# 静默插手更新链），于是「只有系统代理一条路」的机器会 ConnectTimeout。这里把回退
# 做成显式的第二段：连接建立不起来（ConnectError / ConnectTimeout）时按序试
# 环境变量代理 → Windows 系统代理。回退只改「从哪里出去」：更新源 URL 仍是硬编码
# 常量，TLS 照常校验（代理只是 CONNECT 隧道），不构成供应链入口；HTTP 状态错误
# （限流页、404 等）说明链路已通，不属于连不上，不触发回退。

_PROXY_ENV_KEYS = (
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "HTTP_PROXY", "http_proxy",
)


def _proxy_url_from_proxy_server(value: str) -> str | None:
    """把注册表 ProxyServer 的取值归一成 httpx 可用的代理 URL；看不懂返回 None。

    三种常见形态：``host:port``（对全部协议生效）；``http=…;https=…;socks=…``
    （按协议分列，依次取 https / http / socks）；已带 scheme 的原样返回。
    """
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return None
    if "://" in raw:
        return raw
    if "=" in raw:
        per: dict[str, str] = {}
        for part in raw.split(";"):
            scheme, _, addr = part.partition("=")
            scheme, addr = scheme.strip().lower(), addr.strip().rstrip("/")
            if scheme and addr:
                per[scheme] = addr
        for scheme, prefix in (("https", "http://"), ("http", "http://"), ("socks", "socks5://")):
            addr = per.get(scheme)
            if addr:
                return prefix + addr
        return None
    return "http://" + raw


def system_proxy_url() -> str | None:
    """读 Windows 系统代理（HKCU Internet Settings）；未启用、未配置或非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    return _proxy_url_from_proxy_server(server if isinstance(server, str) else "")


def _env_proxy_url() -> str | None:
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def proxy_candidates() -> list[httpx.Proxy]:
    """直连失败后的回退候选，按序去重；一条都没有就返回空表（行为与旧版一致）。

    环境变量代理在前（开发态的显式配置），Windows 系统代理在后（最终用户的常见
    形态）。socks 候选在缺 socksio 依赖时构造会抛 ImportError——跳过该候选，
    不连累其余路径。
    """
    urls: list[str] = []
    for url in (_env_proxy_url(), system_proxy_url()):
        if url and url not in urls:
            urls.append(url)
    out: list[httpx.Proxy] = []
    for url in urls:
        try:
            out.append(httpx.Proxy(url))
        except Exception:  # noqa: BLE001  环境缺口（如缺 socks 依赖）：弃用该候选
            continue
    return out


def _is_connect_failure(e: BaseException) -> bool:
    """「连接根本建立不起来」才是回退条件；HTTP 状态错误与读写超时不重试。"""
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))


def client_kwargs(timeout_s: float, transport: httpx.AsyncBaseTransport | None,
                  proxy: httpx.Proxy | None, follow_redirects: bool = False) -> dict:
    """更新链 httpx 客户端的统一构造参数：trust_env 恒为 False（环境不静默插手），
    transport（测试注入）与 proxy（出网回退）互斥——httpx 里 transport 会整体
    接管出网，proxy 只在真实网络下有意义。"""
    kwargs: dict = {"timeout": timeout_s, "trust_env": False,
                    "follow_redirects": follow_redirects}
    if transport is not None:
        kwargs["transport"] = transport
    elif proxy is not None:
        kwargs["proxy"] = proxy
    return kwargs


async def fetch_latest_release(latest_url: str = RELEASES_LATEST,
                               timeout_s: float = TIMEOUT_S,
                               transport: httpx.AsyncBaseTransport | None = None,
                               channel: str = "stable",
                               candidates_fn: Callable[[], list[httpx.Proxy]] = proxy_candidates,
                               ) -> dict:
    """拉取最新 release 信息；失败抛 RuntimeError（调用方决定静默还是展示）。

    channel="stable"（默认）走 /releases/latest 的 302 Location；channel="beta"
    走 releases.atom 订阅第一条（含预发布，见模块 docstring）。两者都不跟随
    重定向。先直连，连接建立不起来时按 candidates_fn() 的顺序回退出网路径
    （见上方「出网回退」注）。transport 仅供测试注入 MockTransport——注入后
    它接管每次尝试的出网（httpx 里 transport 与 proxy 互斥，proxy 不参与），
    回退因此等价于对同一张 Mock 网络重试，用例可复现；candidates_fn 是回退
    链的测试钩子（默认 proxy_candidates）。渠道值不认识时按 stable。
    """
    if channel not in CHANNELS:
        channel = "stable"
    url = latest_url if channel == "stable" else RELEASES_FEED
    attempts: list[httpx.Proxy | None] = [None]
    last_exc: httpx.HTTPError | None = None
    idx = 0
    while idx < len(attempts):
        proxy = attempts[idx]
        idx += 1
        try:
            async with httpx.AsyncClient(**client_kwargs(timeout_s, transport, proxy)) as client:
                resp = await client.get(url, headers={"Accept": "text/html"})
        except httpx.HTTPError as e:
            if not _is_connect_failure(e):
                raise RuntimeError(f"无法连接更新源：{type(e).__name__}") from e
            last_exc = e
            if len(attempts) == 1:
                attempts.extend(candidates_fn())
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"更新源返回 HTTP {resp.status_code}")
        if channel == "beta":
            return release_from_feed(resp.text, url)
        return release_from_redirect(latest_url, str(resp.headers.get("Location") or ""))
    raise RuntimeError(f"无法连接更新源：{type(last_exc).__name__}") from last_exc
