"""场景模板「可更新」检测（skills.gallery.bundled_update_available）与一键更新。

随包内置（2.3.0）之后，官方更新了模板副本、或用户本地改过已装技能时，
skills.gallery 应标 update_available=True，前端给「更新」按钮——更新动作
复用既有 skills.install（overwrite=true，官方来源自动优先包内副本），
装完 update_available 翻回 False。这里同时锁检测口径与翻转闭环。
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
    """包内副本：SKILL.md + 子目录资源（比对应覆盖嵌套文件）。"""
    d = bundle / "gallery" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试技能\n---\n\n正文\n",
        encoding="utf-8",
    )
    (d / "assets").mkdir()
    (d / "assets" / "表样.csv").write_text("月份,金额\n", encoding="utf-8")
    return d


def _copy_skill(src, dest) -> None:
    """把技能目录原样拷到已装位置（等价 installer.copytree 的落点效果）。"""
    import shutil

    shutil.copytree(src, dest)


# ------------------------------------------------ 检测口径：逐文件内容比对


def test_update_available_false_when_identical(bundle):
    """装的就是包内副本（逐文件一致）：不报可更新。"""
    src = _make_bundle_skill(bundle)
    installed = bundle / "installed" / "budget-tracker"
    _copy_skill(src, installed)
    assert gallery.bundled_update_available(BUDGET_SOURCE, installed) is False


def test_update_available_true_when_skill_md_edited(bundle):
    src = _make_bundle_skill(bundle)
    installed = bundle / "installed" / "budget-tracker"
    _copy_skill(src, installed)
    (installed / "SKILL.md").write_text(
        "---\nname: budget-tracker\ndescription: 我改过的技能\n---\n\n正文\n",
        encoding="utf-8",
    )
    assert gallery.bundled_update_available(BUDGET_SOURCE, installed) is True


def test_update_available_detects_nested_and_file_set_diffs(bundle):
    """嵌套资源内容、多文件、少文件都算差异。"""
    src = _make_bundle_skill(bundle)

    edited_asset = bundle / "installed-a" / "budget-tracker"
    _copy_skill(src, edited_asset)
    (edited_asset / "assets" / "表样.csv").write_text("月份,金额,备注\n", encoding="utf-8")
    assert gallery.bundled_differs(edited_asset, src) is True

    extra_file = bundle / "installed-b" / "budget-tracker"
    _copy_skill(src, extra_file)
    (extra_file / "笔记.txt").write_text("本地新增", encoding="utf-8")
    assert gallery.bundled_differs(extra_file, src) is True

    missing_file = bundle / "installed-c" / "budget-tracker"
    _copy_skill(src, missing_file)
    (missing_file / "assets" / "表样.csv").unlink()
    assert gallery.bundled_differs(missing_file, src) is True


def test_update_available_false_without_bundle_or_install(bundle):
    """包内没有可比对基准 / 已装目录不存在：一律不报可更新。"""
    src = _make_bundle_skill(bundle)
    installed = bundle / "installed" / "budget-tracker"
    _copy_skill(src, installed)
    # 清单命中但包内副本没了（缺 SKILL.md，bundled_dir_for 为 None）
    (src / "SKILL.md").unlink()
    assert gallery.bundled_update_available(BUDGET_SOURCE, installed) is False
    # 非官方来源（不在清单里，包内副本还在也不行）
    assert gallery.bundled_update_available(
        "https://github.com/someone/else/tree/main/skills/whatever", installed
    ) is False
    # 已装目录不存在
    assert gallery.bundled_update_available(
        BUDGET_SOURCE, bundle / "installed" / "not-installed"
    ) is False


# ------------------------------------------------ 后端闭环：gallery_skills 标记 + 更新翻回


async def test_gallery_update_available_flips_after_reinstall(home, tmp_path, bundle, monkeypatch):
    """闭环：装完 False → 本地改动后 True（带更新方式说明）→ overwrite 重装翻回 False。"""
    from skysheep.config import skysheep_home
    from skysheep.server.backend import ServerBackend

    _make_bundle_skill(bundle)

    def _fail_url(*args, **kwargs):
        raise AssertionError("更新应走包内副本，不应在线下载")

    monkeypatch.setattr("skysheep.server.backend.install_from_url", _fail_url)

    be = ServerBackend(
        working_dir=tmp_path / "proj",
        provider_factory=lambda: FakeProvider([[TextBlock(text="好")]]),
    )
    await be.setup()

    def _entry(result):
        match = [t for t in result["templates"] if t["name"] == "budget-tracker"]
        assert match
        return match[0]

    # 初始未安装：不标可更新
    entry = _entry(be.gallery_skills())
    assert entry["installed"] is False and entry["update_available"] is False

    # 一键安装（包内副本）：内容一致 → 不标可更新
    await be.install_skill(BUDGET_SOURCE, scope="global")
    entry = _entry(be.gallery_skills())
    assert entry["installed"] is True and entry["update_available"] is False
    assert entry["update_hint"] == ""

    # 本地改了 SKILL.md → 可更新，带更新方式说明
    installed_md = skysheep_home() / "skills" / "budget-tracker" / "SKILL.md"
    installed_md.write_text(
        "---\nname: budget-tracker\ndescription: 本地改过\n---\n\n正文\n",
        encoding="utf-8",
    )
    entry = _entry(be.gallery_skills())
    assert entry["update_available"] is True
    assert "更新" in entry["update_hint"] and entry["update_hint"]

    # 点「更新」（skills.install + overwrite=true，官方来源自动用包内副本）→ 翻回 False
    await be.install_skill(BUDGET_SOURCE, scope="global", overwrite=True)
    entry = _entry(be.gallery_skills())
    assert entry["update_available"] is False and entry["update_hint"] == ""
    # 覆盖后内容与包内副本一致（本地改动被替换）
    assert installed_md.read_text(encoding="utf-8") == (
        "---\nname: budget-tracker\ndescription: 测试技能\n---\n\n正文\n"
    )
    await be.shutdown()
