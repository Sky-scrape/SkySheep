"""技能广场：一份可一键安装的技能索引。

索引优先从 MARKET_URL（可被环境变量 SKYSHEEP_MARKET_URL 覆盖）拉取远程 JSON，
格式为 [{"name","description","url","author"}]，可选 category / version /
updated_at 附加字段；拉不到（离线/还没建仓）就按「随包发布的完整索引 →
精简内置清单」的顺序兜底——条目全部指向受安装器支持的 GitHub 仓库子目录。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

MARKET_URL = os.environ.get(
    "SKYSHEEP_MARKET_URL",
    "https://raw.githubusercontent.com/Sky-scrape/SkySheep/main/market/index.json",
)
TIMEOUT_S = 6.0

# 详情预览与 load_skill 用同一个正文上限：预览不是全文精排，够判断装不装就行
MAX_PREVIEW_CHARS = 20_000

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


def _bundled_index_candidates() -> list[Path]:
    """随包发布的完整索引（仓库根 market/index.json）可能的落点。

    打包后由 PyInstaller datas 放在 <_MEIPASS>/market/index.json；
    源码运行时从本文件逐级向上找到仓库根的 market/index.json。
    """
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "market" / "index.json")
    # 源码运行：本文件在 <仓库>/engine/src/skysheep/skills/ 下，
    # 逐级向上任何一层出现 market/index.json 都认（editable 安装、monorepo 布局都兼容）
    out.extend(parent / "market" / "index.json" for parent in Path(__file__).resolve().parents)
    return out


def bundled_index_items() -> list[dict]:
    """读随包索引，返回清洗后的条目；文件缺失/损坏返回空列表。

    这是离线兜底的第一优先级：skills-gallery 的 20 个场景技能随主仓库发布，
    断网时用户也应该看得到它们，而不是只剩 5 条精简内置推荐。
    """
    for cand in _bundled_index_candidates():
        if not cand.is_file():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else data
        cleaned = _clean_items(items or [])
        if cleaned:
            return cleaned
    return []


def _clean_items(items: list) -> list[dict]:
    """索引条目清洗：字段白名单 + 限长（索引内容不可信，与搜索高亮同一前提）。"""
    cleaned: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        url_i = str(it.get("url") or "").strip()
        if not name or not url_i:
            continue
        entry = {
            "name": name[:60],
            "description": str(it.get("description") or "")[:200],
            "url": url_i,
            "author": str(it.get("author") or "")[:40],
        }
        # 可选字段：有才带出去（第三方旧索引没有这些字段照常工作）
        for key, cap in (("category", 20), ("version", 32), ("updated_at", 32)):
            raw = str(it.get(key) or "").strip()
            if raw:
                entry[key] = raw[:cap]
        cleaned.append(entry)
    return cleaned


def version_newer(a: str, b: str) -> bool:
    """版本号 a 是否比 b 新：点分数字逐段比较，非数字段按 0，缺段补 0。

    只服务「广场版本比本地新 → 提示更新」这一件事，不追求完整 semver：
    '1.2' 对 '1.10' 按数字比较（1.2 更旧），脏数据（空串/乱码）一律按 0。
    """
    def parts(v: str) -> list[int]:
        v = (v or "").strip().lstrip("vV")
        out = []
        for seg in v.split("."):
            digits = "".join(ch for ch in seg if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out or [0]

    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def merge_installed_state(items: list[dict], skills: list) -> None:
    """把本地已装状态并进广场条目（就地修改）：installed / installed_version /
    update_available。

    匹配两条路：① 安装来源标记（installer 写进技能目录的 .source.json，
    记录安装时的广场 url）——最可靠；② 条目 url 末段与技能名同名——兼容
    标记出现之前装的技能（skills-gallery 的目录名与 SKILL.md name 一致），
    同名也确实意味着再装就会撞名，标注为已安装是符合事实的。
    """
    by_url: dict[str, list] = {}
    by_name: dict[str, list] = {}
    for s in skills:
        url = getattr(s, "source_url", "") or ""
        if url:
            by_url.setdefault(url, []).append(s)
        name = (getattr(s, "name", "") or "").lower()
        if name:
            by_name.setdefault(name, []).append(s)
    for it in items:
        seg = str(it.get("url") or "").rstrip("/").split("/")[-1].lower()
        hits = by_url.get(str(it.get("url") or "")) or by_name.get(seg) or []
        local_ver = next((s.version for s in hits if getattr(s, "version", "")), "")
        it["installed"] = bool(hits)
        it["installed_version"] = local_ver
        it["update_available"] = bool(
            it.get("version") and local_ver and version_newer(it["version"], local_ver)
        )


async def _get_index(url: str, timeout_s: float, trust_env: bool) -> httpx.Response:
    """取一次索引。trust_env 决定是否读系统代理（Windows 上来自注册表）。"""
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=trust_env) as client:
        resp = await client.get(url, headers={"Accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp


async def fetch_market_index(url: str = MARKET_URL, timeout_s: float = TIMEOUT_S) -> dict:
    """拉取技能广场索引；远程不可用时按「随包完整索引 → 精简内置」兜底（永不抛错）。

    代理策略：先走系统代理、失败再直连。国内直连 raw.githubusercontent.com 经常
    超时（实测同一台机器时而 0.7s 成功、时而 6s 超时），而装了代理的机器
    （如 127.0.0.1:7890）走代理反而 0.8s 就回来。这里不预设环境哪一种通——
    先按系统配置试，不行再直连，两条都不行才回退内置。
    注：技能下载（installer）走 urllib，本来就会读系统代理，两边保持一致。

    返回 {source: "remote"|"builtin", note: str, items: [{name,description,url,author,...}]}。
    """
    try:
        try:
            resp = await _get_index(url, timeout_s, trust_env=True)
        except Exception:  # noqa: BLE001 - 代理不可用/没配代理时改直连再试一次
            resp = await _get_index(url, timeout_s, trust_env=False)
        data = resp.json()
        items = data.get("items") if isinstance(data, dict) else data
        cleaned = _clean_items(items or [])
        if cleaned:
            return {"source": "remote", "note": "", "items": cleaned}
    except Exception:  # noqa: BLE001 - 离线/索引缺失是常态，静默回退
        pass
    bundled = bundled_index_items()
    if bundled:
        return {
            "source": "builtin",
            "note": "在线索引暂时拉取不到，以下为随包内置的完整索引",
            "items": bundled,
        }
    return {
        "source": "builtin",
        "note": "在线索引暂时拉取不到，以下为内置推荐（也可直接粘贴网址安装）",
        "items": BUILTIN_INDEX,
    }


async def fetch_remote_text(
    urls: list[str], timeout_s: float = TIMEOUT_S, max_chars: int = MAX_PREVIEW_CHARS
) -> tuple[str, bool]:
    """按候选顺序拉取远端文本（广场详情预览用），返回（正文, 是否截断）。

    与索引拉取同一套代理双试；全部候选都失败才抛错。正文截到 max_chars——
    与 load_skill 的正文上限一致，预览不需要更多。
    """
    last_err: Exception | None = None
    for url in urls:
        for trust_env in (True, False):
            try:
                async with httpx.AsyncClient(timeout=timeout_s, trust_env=trust_env) as client:
                    resp = await client.get(url, headers={"Accept": "text/plain"})
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                text = resp.text
                return text[:max_chars], len(text) > max_chars
            except Exception as e:  # noqa: BLE001 - 换下一个候选/直连再试
                last_err = e
    raise RuntimeError("SKILL.md 拉取失败：" + (str(last_err) or "链接不可达"))
