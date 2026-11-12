"""技能装前预览（preview_skill_install）测试。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def backend(tmp_path, monkeypatch):
    """SKYSHEEP_HOME 隔离 + 假 provider 的最小 ServerBackend。"""
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    from skysheep.server.backend import ServerBackend

    b = ServerBackend.__new__(ServerBackend)
    # 只初始化预览路径触达的最小状态
    from skysheep.config import load_config

    b.cfg = load_config()
    from skysheep.skills import SkillLoader

    b.skills = SkillLoader(
        global_dir=tmp_path / "home" / "skills",
        state_path=tmp_path / "home" / "skills.json",
    )
    b.skills.discover()
    b.working_dir = None
    return b


def _make_pack(tmp_path, name="pv-skill", desc="预览测试", extra=""):
    src = tmp_path / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{extra}", encoding="utf-8"
    )
    (src / "data.txt").write_text("x", encoding="utf-8")
    return src


def test_preview_local_pack(backend, tmp_path):
    src = _make_pack(tmp_path)
    r = _run(backend.preview_skill_install(str(src), "global", False))
    assert r["count"] == 1
    it = r["items"][0]
    assert it["name"] == "pv-skill" and it["files"] == 2
    assert it["description"] == "预览测试"
    assert r["clashes"] == [] and r["needs_overwrite"] is False
    # 预览不落盘：技能目录里没有新东西（目录可能尚未创建）
    skills_dir = tmp_path / "home" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.iterdir())


def test_preview_detects_clash(backend, tmp_path):
    from skysheep.skills.installer import install as install_skill

    src = _make_pack(tmp_path)
    backend.skills.global_dir.mkdir(parents=True, exist_ok=True)
    install_skill(str(src), backend.skills.global_dir, existing=set())
    backend.skills.discover()  # install_skill 完整版会重扫；测试里手动补一次
    r = _run(backend.preview_skill_install(str(src), "global", False))
    assert r["clashes"] == ["pv-skill"] and r["needs_overwrite"] is True
    r2 = _run(backend.preview_skill_install(str(src), "global", True))
    assert r2["needs_overwrite"] is False and r2["overwrite"] is True


def test_preview_rejects_empty_and_missing(backend, tmp_path):

    with pytest.raises(RuntimeError, match="缺少来源"):
        _run(backend.preview_skill_install("", "global"))
    with pytest.raises(RuntimeError, match="路径不存在"):
        _run(backend.preview_skill_install(str(tmp_path / "nope"), "global"))
    # 目录里没有 SKILL.md
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(RuntimeError, match="没找到技能"):
        _run(backend.preview_skill_install(str(bare), "global"))


def _make_zip(tmp_path, name="pv-skill", desc="zip 预览", inner=None):
    """zip 根级含技能文件夹的技能包（与界面粘贴本地 .zip 路径同一形态）。

    inner(root) 可以往 zip 根再加别的技能文件夹（如非法名字的坏包）。
    """
    import zipfile

    staging = tmp_path / "_ziproot"
    staging.mkdir(exist_ok=True)
    _make_pack(staging, name=name, desc=desc)
    if inner is not None:
        inner(staging)
    zpath = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for f in sorted(staging.rglob("*")):
            zf.write(f, f.relative_to(staging))
    return zpath


def test_preview_local_zip_unpacks_before_scan(backend, tmp_path):
    # 本地 .zip 不先解压就当目录扫，zip 文件里没有 SKILL.md 子目录，
    # 预览必然报「没找到技能」——界面的两段式导入就永远装不进去（ui-1）
    zpath = _make_zip(tmp_path)
    r = _run(backend.preview_skill_install(str(zpath), "global", False))
    assert r["count"] == 1
    assert r["items"][0]["name"] == "pv-skill"
    # 预览零落盘：暂存目录已清理，技能目录没有新东西
    skills_dir = tmp_path / "home" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.iterdir())


def test_preview_marks_invalid_names(backend, tmp_path):
    # 预览与安装同一套名字校验：安装层必拒的名字（含路径分隔符）不进
    # 「将要安装」清单，转进 invalid 并带原因（ui-2）
    def plant_bad(root):
        bad = root / "bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text(
            "---\nname: bad/path\ndescription: 非法名\n---\n", encoding="utf-8"
        )

    zpath = _make_zip(tmp_path, inner=plant_bad)
    r = _run(backend.preview_skill_install(str(zpath), "global", False))
    names = [it["name"] for it in r["items"]]
    assert names == ["pv-skill"]
    assert len(r["invalid"]) == 1
    assert r["invalid"][0]["name"] == "bad/path"
    assert "路径分隔符" in r["invalid"][0]["reason"]


def _run(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)
