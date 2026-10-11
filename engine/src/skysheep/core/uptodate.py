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

import re
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


async def fetch_latest_release(latest_url: str = RELEASES_LATEST,
                               timeout_s: float = TIMEOUT_S,
                               transport: httpx.AsyncBaseTransport | None = None,
                               channel: str = "stable") -> dict:
    """拉取最新 release 信息；失败抛 RuntimeError（调用方决定静默还是展示）。

    channel="stable"（默认）走 /releases/latest 的 302 Location；channel="beta"
    走 releases.atom 订阅第一条（含预发布，见模块 docstring）。两者都不跟随
    重定向。transport 仅供测试注入 MockTransport；渠道值不认识时按 stable。
    """
    if channel not in CHANNELS:
        channel = "stable"
    url = latest_url if channel == "stable" else RELEASES_FEED
    try:
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False,
                                     follow_redirects=False,
                                     transport=transport) as client:
            resp = await client.get(url, headers={"Accept": "text/html"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"无法连接更新源：{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"更新源返回 HTTP {resp.status_code}")
    if channel == "beta":
        return release_from_feed(resp.text, url)
    return release_from_redirect(latest_url, str(resp.headers.get("Location") or ""))
