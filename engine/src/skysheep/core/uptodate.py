"""更新检查：对照 GitHub Releases 的最新版本号，提示用户有新版可升级。

克制原则：启动后在后台查一次（几秒超时、失败完全静默——离线/仓库不存在
都不打扰），结果随 boot 快照带到前端展示；设置 · 关于里可手动再查。
"""

from __future__ import annotations

import re

import httpx

# 开源发布后的 Releases API；仓库未定稿前允许用环境变量覆盖
DEFAULT_RELEASES_API = "https://api.github.com/repos/Sky-scrape/SkySheep/releases/latest"
RELEASES_PAGE = "https://github.com/Sky-scrape/SkySheep/releases"
TIMEOUT_S = 6.0

_VERSION_RE = re.compile(r"\d+")


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


async def fetch_latest_release(api_url: str = DEFAULT_RELEASES_API,
                               timeout_s: float = TIMEOUT_S) -> dict:
    """拉取最新 release 信息；失败抛 RuntimeError（调用方决定静默还是展示）。"""
    try:
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
            resp = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"无法连接更新源：{type(e).__name__}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"更新源返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
        tag = str(data.get("tag_name") or "").strip()
        html_url = str(data.get("html_url") or RELEASES_PAGE)
        body = str(data.get("body") or "")
    except ValueError as e:
        raise RuntimeError("更新源返回的不是 JSON") from e
    if not tag:
        raise RuntimeError("更新源没有版本号")
    return {"version": tag.lstrip("vV"), "tag": tag, "url": html_url,
            "notes": body.strip()[:400]}
