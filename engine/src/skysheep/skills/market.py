"""技能广场：一份可一键安装的技能索引。

索引优先从 MARKET_URL（可被环境变量 SKYSHEEP_MARKET_URL 覆盖）拉取远程 JSON，
格式为 [{"name","description","url","author"}]；拉不到（离线/还没建仓）就用
下面这份内置清单兜底——条目全部指向受安装器支持的 GitHub 仓库子目录。
"""

from __future__ import annotations

import os

import httpx

MARKET_URL = os.environ.get(
    "SKYSHEEP_MARKET_URL",
    "https://raw.githubusercontent.com/Sky-scrape/SkySheep/main/market/index.json",
)
TIMEOUT_S = 6.0

BUILTIN_INDEX: list[dict] = [
    {
        "name": "docx 文档技能",
        "description": "创建、编辑、分析 Word 文档（含修订、批注、格式保留）",
        "url": "https://github.com/anthropics/skills/tree/main/skills/docx",
        "author": "anthropics",
    },
    {
        "name": "pdf 文档技能",
        "description": "处理 PDF：填表单、合并拆分、提取文本与表格",
        "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
        "author": "anthropics",
    },
    {
        "name": "pptx 幻灯片技能",
        "description": "创建与编辑 PowerPoint 演示文稿（版式、缩略图、批注）",
        "url": "https://github.com/anthropics/skills/tree/main/skills/pptx",
        "author": "anthropics",
    },
    {
        "name": "xlsx 表格技能",
        "description": "创建与编辑 Excel 表格（公式、图表、条件格式）",
        "url": "https://github.com/anthropics/skills/tree/main/skills/xlsx",
        "author": "anthropics",
    },
    {
        "name": "anthropics/skills 全仓库",
        "description": "官方技能仓库整包：一次安装仓库里发现的全部技能",
        "url": "https://github.com/anthropics/skills",
        "author": "anthropics",
    },
]


async def _get_index(url: str, timeout_s: float, trust_env: bool) -> httpx.Response:
    """取一次索引。trust_env 决定是否读系统代理（Windows 上来自注册表）。"""
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=trust_env) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp


async def fetch_market_index(url: str = MARKET_URL, timeout_s: float = TIMEOUT_S) -> dict:
    """拉取技能广场索引；远程不可用时回退内置清单（永不抛错）。

    代理策略：先走系统代理、失败再直连。国内直连 raw.githubusercontent.com 经常
    超时（实测同一台机器时而 0.7s 成功、时而 6s 超时），而装了代理的机器
    （如 127.0.0.1:7890）走代理反而 0.8s 就回来。这里不预设环境哪一种通——
    先按系统配置试，不行再直连，两条都不行才回退内置清单。
    注：技能下载（installer）走 urllib，本来就会读系统代理，两边保持一致。

    返回 {source: "remote"|"builtin", note: str, items: [{name,description,url,author}]}。
    """
    try:
        try:
            resp = await _get_index(url, timeout_s, trust_env=True)
        except Exception:  # noqa: BLE001 - 代理不可用/没配代理时改直连再试一次
            resp = await _get_index(url, timeout_s, trust_env=False)
        data = resp.json()
        items = data.get("items") if isinstance(data, dict) else data
        cleaned: list[dict] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            url_i = str(it.get("url") or "").strip()
            if not name or not url_i:
                continue
            cleaned.append({
                "name": name[:60],
                "description": str(it.get("description") or "")[:200],
                "url": url_i,
                "author": str(it.get("author") or "")[:40],
            })
        if cleaned:
            return {"source": "remote", "note": "", "items": cleaned}
        return {"source": "builtin", "note": "远程索引为空，以下为内置推荐", "items": BUILTIN_INDEX}
    except Exception:  # noqa: BLE001 - 离线/索引缺失是常态，静默回退
        return {
            "source": "builtin",
            "note": "在线索引暂时拉取不到，以下为内置推荐（也可直接粘贴网址安装）",
            "items": BUILTIN_INDEX,
        }
