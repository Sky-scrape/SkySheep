"""技能广场升级：版本比对 / 已安装与可更新标注 / 分类与版本字段 /
随包完整索引兜底 / 覆盖更新安装 / 详情预览直链 / WS 协议与前端锁定。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest
from test_server import make_client, recv_until

import skysheep.server.backend as backend_mod
from skysheep.messages import TextBlock
from skysheep.skills.installer import (
    SkillInstallError,
    install_from_dir,
    install_from_zip,
    raw_skillmd_urls,
)
from skysheep.skills.loader import Skill, SkillLoader
from skysheep.skills.market import (
    _clean_items,
    bundled_index_items,
    fetch_market_index,
    merge_installed_state,
    version_newer,
)

# ---- 版本比较 ----


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("1.1", "1.0.9", True),
        ("1.2", "1.10", False),  # 数字比较：1.2 比 1.10 旧
        ("v2.0", "1.9.9", True),  # 容忍 v 前缀
        ("1.0.0", "1.0.0", False),
        ("", "1.0.0", False),
        ("1.0.1", "", True),
        ("2.0.beta", "1.9", True),  # 非数字段按 0 → 2.0.0 > 1.9
    ],
)
def test_version_newer(a, b, expected):
    assert version_newer(a, b) is expected


# ---- 索引清洗：可选字段保留、必填字段限长、未知字段丢弃 ----


def test_fetch_market_index_keeps_optional_fields(monkeypatch):
    from skysheep.skills import market as mk

    payload = {"items": [{
        "name": "x" * 80, "description": "d" * 300,
        "url": "https://github.com/u/r/tree/main/s",
        "author": "me", "category": "测试分类",
        "version": "1.0.0", "updated_at": "2026-09-22",
        "evil": "<script>",
    }]}

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    async def fake_get(url, timeout_s, trust_env):
        return _Resp()

    monkeypatch.setattr(mk, "_get_index", fake_get)
    r = asyncio.run(fetch_market_index())
    assert r["source"] == "remote"
    it = r["items"][0]
    assert len(it["name"]) == 60 and len(it["description"]) == 200
    assert it["category"] == "测试分类"
    assert it["version"] == "1.0.0" and it["updated_at"] == "2026-09-22"
    assert "evil" not in it


# ---- 随包完整索引兜底 ----


def test_bundled_index_matches_repo_market_json():
    """随包兜底索引必须与仓库 market/index.json 完全同步（候选路径也要真能找到它）。"""
    bundled = bundled_index_items()
    assert len(bundled) == 25
    assert all(it.get("category") for it in bundled)
    gallery = [it for it in bundled if it["author"] == "skysheep"]
    assert gallery and all(it.get("version") for it in gallery)
    repo = Path(__file__).resolve().parents[2] / "market" / "index.json"
    data = json.loads(repo.read_text(encoding="utf-8"))
    assert bundled == _clean_items(data["items"])


def test_offline_falls_back_to_bundled_full_index(tmp_path, monkeypatch):
    """离线时优先回退随包完整索引；连它都没有才退回精简内置清单。"""
    from skysheep.skills import market as mk

    async def fake_get(url, timeout_s, trust_env):
        raise RuntimeError("没网")

    monkeypatch.setattr(mk, "_get_index", fake_get)
    bundled = tmp_path / "index.json"
    bundled.write_text(json.dumps({"items": [
        {"name": "离线技能", "description": "d", "url": "https://github.com/u/r",
         "author": "me", "category": "离线"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(mk, "_bundled_index_candidates", lambda: [bundled])
    r = asyncio.run(fetch_market_index())
    assert r["source"] == "builtin"
    assert r["items"][0]["name"] == "离线技能" and r["items"][0]["category"] == "离线"

    monkeypatch.setattr(mk, "_bundled_index_candidates", lambda: [tmp_path / "nope.json"])
    r2 = asyncio.run(fetch_market_index())
    assert r2["items"] == mk.BUILTIN_INDEX
    assert "拉取不到" in r2["note"]


# ---- 已安装 / 可更新标注 ----


def _skill(name, version="", source_url=""):
    return Skill(name=name, description="", path=Path(name),
                 version=version, source_url=source_url)


def test_merge_installed_state_by_marker_and_name():
    items = [
        # 无来源标记的旧装技能：按「条目 url 末段与技能名同名」兜底命中
        {"name": "周报生成",
         "url": "https://github.com/Sky-scrape/SkySheep/tree/main/skills-gallery/weekly-report",
         "version": "1.1.0"},
        # 有来源标记：按标记精确命中；索引没写版本号 → 不谈更新
        {"name": "docx 文档技能",
         "url": "https://github.com/anthropics/skills/tree/main/skills/docx"},
        # 没装过
        {"name": "新技能", "url": "https://github.com/a/b/tree/main/new-thing", "version": "2.0"},
    ]
    skills = [
        _skill("weekly-report", "1.0.0"),
        _skill("docx", source_url="https://github.com/anthropics/skills/tree/main/skills/docx"),
    ]
    merge_installed_state(items, skills)
    assert items[0]["installed"] is True
    assert items[0]["installed_version"] == "1.0.0"
    assert items[0]["update_available"] is True  # 索引 1.1.0 > 本地 1.0.0
    assert items[1]["installed"] is True and items[1]["update_available"] is False
    assert items[2]["installed"] is False


def test_merge_same_version_and_local_newer_not_update():
    items = [
        {"name": "x", "url": "https://github.com/a/b/tree/main/x", "version": "1.0.0"},
        {"name": "y", "url": "https://github.com/a/b/tree/main/y", "version": "1.0.0"},
    ]
    merge_installed_state(items, [_skill("x", "1.0.0"), _skill("y", "1.0.1")])
    assert not items[0]["update_available"]  # 同版本
    assert not items[1]["update_available"]  # 本地比索引还新：不提示降级


# ---- loader：frontmatter version 与 .source.json 来源标记 ----


def test_skill_version_and_source_marker(tmp_path):
    g = tmp_path / "g"
    d = g / "abc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: abc\ndescription: 测试\nversion: 1.2.3\n---\n正文\n", encoding="utf-8")
    (d / ".source.json").write_text(
        json.dumps({"market_url": "https://github.com/a/b/tree/main/abc"}), encoding="utf-8")
    loader = SkillLoader(global_dir=g)
    loader.discover()
    s = loader.get("abc")
    assert s.version == "1.2.3"
    assert s.source_url == "https://github.com/a/b/tree/main/abc"

    # 缺 version、标记损坏：安静降级为空，不影响技能发现
    d2 = g / "bad"
    d2.mkdir()
    (d2 / "SKILL.md").write_text("---\nname: bad\ndescription: x\n---\n", encoding="utf-8")
    (d2 / ".source.json").write_text("不是 json", encoding="utf-8")
    loader.discover()
    s2 = loader.get("bad")
    assert s2.version == "" and s2.source_url == ""


# ---- installer：覆盖更新与来源标记 ----


def test_install_overwrite_and_source_marker(tmp_path):
    dest = tmp_path / "dest"
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v1\nversion: 1.0.0\n---\nv1\n", encoding="utf-8")
    assert install_from_dir(src, dest, existing=set())["installed"] == ["demo"]

    # 上游发了新版：默认重名拒绝；overwrite=True 整目录替换并写来源标记
    (src / "SKILL.md").write_text(
        "---\nname: demo\ndescription: v2\nversion: 1.1.0\n---\nv2\n", encoding="utf-8")
    with pytest.raises(SkillInstallError):
        install_from_dir(src, dest, existing={"demo"})
    r = install_from_dir(src, dest, existing={"demo"}, overwrite=True,
                         source_url="https://github.com/a/b/tree/main/demo")
    assert r["installed"] == ["demo"]
    assert "1.1.0" in (dest / "demo" / "SKILL.md").read_text(encoding="utf-8")
    marker = json.loads((dest / "demo" / ".source.json").read_text(encoding="utf-8"))
    assert marker["market_url"] == "https://github.com/a/b/tree/main/demo"

    # loader 能把版本与来源读回来（广场标注的数据链路闭环）
    s = SkillLoader(global_dir=dest).discover()[0]
    assert s.version == "1.1.0" and s.source_url.endswith("/demo")


def test_install_from_zip_writes_source_marker(tmp_path):
    pkg = tmp_path / "pkg" / "zed"
    pkg.mkdir(parents=True)
    (pkg / "SKILL.md").write_text("---\nname: zed\ndescription: z\n---\n", encoding="utf-8")
    zpath = tmp_path / "zed.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(pkg / "SKILL.md", "zed/SKILL.md")
    dest = tmp_path / "out"
    r = install_from_zip(zpath, dest, existing=set(),
                         source_url="https://github.com/a/b/tree/main/zed")
    assert r["installed"] == ["zed"]
    marker = json.loads((dest / "zed" / ".source.json").read_text(encoding="utf-8"))
    assert marker["market_url"].endswith("/zed")


# ---- 详情预览：仓库页 → SKILL.md raw 直链 ----


def test_raw_skillmd_urls():
    assert raw_skillmd_urls("https://github.com/a/b/tree/main/skills/docx") == [
        "https://raw.githubusercontent.com/a/b/main/skills/docx/SKILL.md"]
    # 仓库主页：猜 main / master 两个分支
    assert raw_skillmd_urls("https://github.com/a/b") == [
        "https://raw.githubusercontent.com/a/b/main/SKILL.md",
        "https://raw.githubusercontent.com/a/b/master/SKILL.md"]
    assert raw_skillmd_urls("https://gitee.com/a/b/tree/master/x") == [
        "https://gitee.com/a/b/raw/master/x/SKILL.md"]
    # 只放行白名单托管域；.zip 直链与无关页面没有可预览的 SKILL.md
    for bad in (
        "https://example.com/a/b",
        "https://github.com/a/b/releases/download/v1/x.zip",
        "https://github.com/a/b/wiki",
        "https://github.com/a",
    ):
        with pytest.raises(SkillInstallError):
            raw_skillmd_urls(bad)


# ---- WS 协议：标注 / 刷新参数 / 详情预览 ----


def test_ws_market_annotates_installed_and_detail(home, monkeypatch):
    monkeypatch.delenv("SKYSHEEP_MARKET_URL", raising=False)
    # 预装一个带来源标记与版本的技能（全局技能根：SKYSHEEP_HOME/skills）
    root = home / "home" / "skills" / "weekly-report"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: weekly-report\ndescription: 周报\nversion: 1.0.0\n---\n", encoding="utf-8")
    (root / ".source.json").write_text(json.dumps(
        {"market_url": "https://github.com/Sky-scrape/SkySheep/tree/main/skills-gallery/weekly-report"}),
        encoding="utf-8")

    async def fake_fetch_index():
        return {"source": "remote", "note": "", "items": [{
            "name": "周报生成", "description": "周报", "author": "skysheep",
            "url": "https://github.com/Sky-scrape/SkySheep/tree/main/skills-gallery/weekly-report",
            "category": "办公写作", "version": "1.1.0", "updated_at": "2026-09-22",
        }]}

    async def fake_fetch_text(urls, timeout_s=6.0, max_chars=20000):
        assert urls == ["https://raw.githubusercontent.com/anthropics/skills/main/skills/docx/SKILL.md"]
        return "SKILL 正文", False

    monkeypatch.setattr(backend_mod, "fetch_market_index", fake_fetch_index)
    monkeypatch.setattr(backend_mod, "fetch_remote_text", fake_fetch_text)

    with make_client(home, [[TextBlock(text="ok")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "mk1", "method": "skills.market"})
        r = recv_until(ws, "mk1")["result"]
        assert r["official"] is True  # 默认发布地址 = 官方索引
        it = r["items"][0]
        assert it["installed"] and it["installed_version"] == "1.0.0"
        assert it["update_available"]  # 索引 1.1.0 比本地 1.0.0 新

        ws.send_json({"id": "mk2", "method": "skills.market", "params": {"refresh": True}})
        assert recv_until(ws, "mk2")["ok"]

        ws.send_json({"id": "md1", "method": "skills.market_detail",
                      "params": {"url": "https://github.com/anthropics/skills/tree/main/skills/docx"}})
        d = recv_until(ws, "md1")["result"]
        assert d["content"] == "SKILL 正文" and d["truncated"] is False
        assert d["url"].endswith("/skills/docx")


# ---- 前端锁定：分类筛选 / 安装范围 / 手动刷新 / 详情 / 相关性排序 ----


def test_market_frontend_has_filters_scope_detail_refresh():
    from skysheep.server.app import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function renderMarketInto("):]
    body = body[:body.index("\n// 折叠块")]
    for needle in (
        'id="market-scope"',            # 安装范围选择
        'id="market-refresh"',          # 手动刷新
        'id="market-chips"',            # 分类筛选标签
        "skills.market_detail",         # 详情预览走新 WS 方法
        "data-mode",                    # 安装/更新/重装三种按钮形态
        "name.startsWith",              # 搜索命中按名称相关性排序
        'request("skills.market", { refresh: !!opts.refresh })',
        # 原有约定不回退：外部内容必须转义后再进 HTML
        "escapeHtml(d.content",
    ):
        assert needle in body, f"技能广场缺新节点/逻辑：{needle}"
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for cls in (".market-chip", ".market-toolbar", ".market-detail-pre", ".mi-btns"):
        assert cls in css, f"缺样式：{cls}"
