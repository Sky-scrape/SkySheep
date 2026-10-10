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


def _run(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)
