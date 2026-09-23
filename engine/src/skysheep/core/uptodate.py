"""更新检查：对照 GitHub Releases 的最新版本号，提示用户有新版可升级。

克制原则：启动后在后台查一次（几秒超时、失败完全静默——离线/仓库不存在
都不打扰），结果随 boot 快照带到前端展示；设置 · 关于里可手动再查。

不走 api.github.com：匿名接口按出口 IP 每小时 60 次限流，国内共享出口很
容易被同 IP 的其他人耗尽，一超就 403。改请求 releases/latest 页面本身——
仓库有 release 时它 302 到 /releases/tag/<tag>，Location 里就带版本号，
页面跳转没有这份配额。代价是拿不到 release 说明与附件清单：说明置空，
安装包地址按发布约定（SkySheep-<版本>-setup.exe，见 installer.iss）拼出。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin

import httpx

RELEASES_LATEST = "https://github.com/Sky-scrape/SkySheep/releases/latest"
RELEASES_PAGE = "https://github.com/Sky-scrape/SkySheep/releases"
DOWNLOAD_BASE = "https://github.com/Sky-scrape/SkySheep/releases/download"
TIMEOUT_S = 6.0
SETUP_ASSET_SUFFIX = "-setup.exe"

_VERSION_RE = re.compile(r"\d+")
_TAG_IN_LOCATION_RE = re.compile(r"/releases/tag/([^/?#]+)")
# tag/version 只允许这些字符：它们会被拼进文件路径与 `cmd /c` 命令行，
# 一旦出现 `"` `&` `%` `/` `\` 就能注入额外命令或改写落点。更新源被控时
# 这是纵深防御的最后一道（宁可不更新，也不要装一个来路不明的包）。
_TAG_SAFE_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,64}$")


def is_safe_tag(tag: str) -> bool:
    """tag/version 是否能安全地拼进路径与命令行。"""
    return bool(_TAG_SAFE_RE.match(tag or ""))


def _version_tuple(v: str) -> tuple[int, ...]:
    """'v0.7.1-beta2' -> (0, 7, 1, 2)：只取数字段，用于比较。"""
    return tuple(int(x) for x in _VERSION_RE.findall(v)[:6])


def is_newer_version(remote: str, local: str) -> bool:
    """remote 版本是否严格新于本地版本（数字段逐位比较，缺省段当 0）。"""
    rt, lt = _version_tuple(remote or ""), _version_tuple(local or "")
    if not rt:
        return False
    n = max(len(rt), len(lt))
    rt += (0,) * (n - len(rt))
    lt += (0,) * (n - len(lt))
    return rt > lt


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


async def fetch_latest_release(latest_url: str = RELEASES_LATEST,
                               timeout_s: float = TIMEOUT_S,
                               transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """拉取最新 release 信息；失败抛 RuntimeError（调用方决定静默还是展示）。

    不跟随重定向——版本号就在 302 的 Location 里，跟着跳反而多一跳。
    transport 仅供测试注入 MockTransport。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False,
                                     follow_redirects=False,
                                     transport=transport) as client:
            resp = await client.get(latest_url, headers={"Accept": "text/html"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"无法连接更新源：{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"更新源返回 HTTP {resp.status_code}")
    return release_from_redirect(latest_url, str(resp.headers.get("Location") or ""))
