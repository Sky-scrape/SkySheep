"""场景模板打包内副本（skills.gallery.bundled_dir_for）与官方来源优先本地安装。

回归 2026-10 审查项 7：此前安装版只打包清单（gallery_manifest.json），20 个
模板的安装一律在线从 GitHub 下载整仓归档——中文网络环境经常失败，且装一个
模板就要拉一次整仓 zip（无缓存）。现在 SkySheep.spec 把 skills-gallery/ 收进
包内 skysheep/skills/gallery/，官方来源优先本地拷贝、在线只作更新回退。
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeProvider

from skysheep.messages import TextBlock
from skysheep.skills import gallery

BUDGET_SOURCE = (
    "https://github.com/Sky-scrape/SkySheep/tree/main/skills-gallery/budget-tracker"
)


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    """假 manifest + 假打包内副本目录（结构与真实 datas 落位一致）。"""
    manifest = {"skills": [{
        "dir": "budget-tracker", "name": "budget-tracker",
        "display_name": "记账与预算表", "description": "记账技能",
        "source": BUDGET_SOURCE,
    }]}
    monkeypatch.setattr(gallery, "MANIFEST_PATH", tmp_path / "gallery_manifest.json")
    (tmp_path / "gallery_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(gallery, "GALLERY_BUNDLE_DIR", tmp_path / "gallery")
    return tmp_path


def _make_bundle_skill(bundle, name="budget-tracker"):
    d = bundle / "gallery" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试技能\n---\n\n正文\n",
        encoding="utf-8",
    )
    return d


def test_bundled_dir_matches_source_url(bundle):
    d = _make_bundle_skill(bundle)
    assert gallery.bundled_dir_for(BUDGET_SOURCE) == d
    # 尾斜杠容忍
    assert gallery.bundled_dir_for(BUDGET_SOURCE + "/") == d
    # 直接给目录名也认（清单 dir 字段）
    assert gallery.bundled_dir_for("budget-tracker") == d


def test_bundled_dir_none_for_unknown_source(bundle):
    _make_bundle_skill(bundle)
    # 非官方来源（不在清单里）
    assert gallery.bundled_dir_for(
        "https://github.com/someone/else/tree/main/skills/whatever"
    ) is None
    assert gallery.bundled_dir_for("") is None


def test_bundled_dir_none_when_bundle_missing_or_incomplete(bundle):
    # 清单命中但包内没有副本
    assert gallery.bundled_dir_for(BUDGET_SOURCE) is None
    # 副本目录存在但缺 SKILL.md（坏副本不装，宁走网络）
    (bundle / "gallery" / "budget-tracker").mkdir(parents=True)
    assert gallery.bundled_dir_for(BUDGET_SOURCE) is None


def test_bundled_dir_none_without_manifest(bundle, monkeypatch):
    """清单读取失败（空列表）：没有可匹配的条目，一律回落在线。"""
    _make_bundle_skill(bundle)
    (bundle / "gallery_manifest.json").write_text("not json", encoding="utf-8")
    assert gallery.bundled_dir_for(BUDGET_SOURCE) is None


async def test_install_official_source_prefers_bundle(home, tmp_path, bundle, monkeypatch):
    """官方模板一键安装走打包内副本：装进技能目录且不联网。"""
    from skysheep.config import skysheep_home
    from skysheep.server.backend import ServerBackend

    _make_bundle_skill(bundle)

    def _fail_url(*args, **kwargs):
        raise AssertionError("包内副本可用时不应在线下载")

    monkeypatch.setattr("skysheep.server.backend.install_from_url", _fail_url)

    be = ServerBackend(
        working_dir=tmp_path / "proj",
        provider_factory=lambda: FakeProvider([[TextBlock(text="好")]]),
    )
    await be.setup()
    result = await be.install_skill(BUDGET_SOURCE, scope="global")
    assert "budget-tracker" in result["installed"]
    installed = skysheep_home() / "skills" / "budget-tracker" / "SKILL.md"
    assert installed.is_file()
    # 装完即可用：技能清单里能看到
    assert any(s.name == "budget-tracker" for s in be.skills.all())
    await be.shutdown()


async def test_install_falls_back_to_url_without_bundle(home, tmp_path, bundle, monkeypatch):
    """包内没有副本（清单命中但 datas 缺失）：回落在线下载（更新回退路径）。"""
    from skysheep.server.backend import ServerBackend

    calls = []

    def _fake_url(source, root, *, existing, overwrite):
        calls.append((source, str(root), overwrite))
        return {"installed": ["budget-tracker"], "count": 1, "dest": str(root)}

    monkeypatch.setattr("skysheep.server.backend.install_from_url", _fake_url)

    be = ServerBackend(
        working_dir=tmp_path / "proj",
        provider_factory=lambda: FakeProvider([[TextBlock(text="好")]]),
    )
    await be.setup()
    result = await be.install_skill(BUDGET_SOURCE, scope="global")
    assert len(calls) == 1 and calls[0][0] == BUDGET_SOURCE
    assert result["installed"] == ["budget-tracker"]
    await be.shutdown()


async def test_install_non_gallery_url_still_goes_online(home, tmp_path, monkeypatch):
    """非清单内的网址来源（第三方技能包）：行为不变，照走在线下载。"""
    from skysheep.server.backend import ServerBackend

    calls = []

    def _fake_url(source, root, *, existing, overwrite):
        calls.append(source)
        return {"installed": [], "count": 0, "dest": str(root)}

    monkeypatch.setattr("skysheep.server.backend.install_from_url", _fake_url)

    be = ServerBackend(
        working_dir=tmp_path / "proj",
        provider_factory=lambda: FakeProvider([[TextBlock(text="好")]]),
    )
    await be.setup()
    # 真实清单未被打补丁（模块常量指向源码树内文件），随便一个不在清单里的 URL
    await be.install_skill("https://example.com/some-skill.zip", scope="global")
    assert calls == ["https://example.com/some-skill.zip"]
    await be.shutdown()
