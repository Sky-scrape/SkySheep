"""Skills 模块测试。"""

from __future__ import annotations

import zipfile

import pytest

from skysheep.skills import SkillLoader
from skysheep.skills.installer import SkillInstallError, install_from_zip, resolve_url
from skysheep.tools.skill import LoadSkillTool, ToolContext, ToolError


def make_skills(tmp_path):
    g = tmp_path / "global_skills"
    p = tmp_path / "proj" / ".skysheep" / "skills"
    (g / "pdf-tools").mkdir(parents=True, exist_ok=True)
    (g / "pdf-tools" / "SKILL.md").write_text(
        "---\nname: pdf-tools\ndescription: 合并、拆分 PDF\n---\n正文指令：用 pypdf 处理。\n",
        encoding="utf-8",
    )
    (p / "repo-audit").mkdir(parents=True, exist_ok=True)
    (p / "repo-audit" / "SKILL.md").write_text(
        "---\nname: repo-audit\ndescription: 审计仓库结构\n---\n审计步骤……\n",
        encoding="utf-8",
    )
    state = tmp_path / "proj" / ".skysheep" / "skills.json"
    return SkillLoader(global_dir=g, project_dir=p, state_path=state)


def test_discover_finds_global_and_project(tmp_path):
    loader = make_skills(tmp_path)
    skills = loader.discover()
    names = {s.name for s in skills}
    assert names == {"pdf-tools", "repo-audit"}
    sources = {s.name: s.source for s in skills}
    assert sources == {"pdf-tools": "global", "repo-audit": "project"}


def test_frontmatter_and_body(tmp_path):
    loader = make_skills(tmp_path)
    loader.discover()
    skill = loader.get("pdf-tools")
    assert skill.description == "合并、拆分 PDF"
    body = loader.load_body("pdf-tools")
    assert "pypdf" in body
    assert "name:" not in body  # frontmatter 已剥离


def test_enable_disable_persists(tmp_path):
    loader = make_skills(tmp_path)
    loader.discover()
    assert loader.set_enabled("pdf-tools", False)
    assert loader.get("pdf-tools").enabled is False
    # 重新发现后状态仍在（skills.json 持久化）
    loader2 = make_skills(tmp_path)
    loader2.discover()
    assert loader2.get("pdf-tools").enabled is False
    assert loader2.get("repo-audit").enabled is True
    # 禁用的技能不能 load
    try:
        loader2.load_body("pdf-tools")
        raise AssertionError("should raise")
    except KeyError:
        pass
    # 重新开启
    assert loader2.set_enabled("pdf-tools", True)
    assert loader2.get("pdf-tools").enabled is True


def test_prompt_section_injection(tmp_path):
    loader = make_skills(tmp_path)
    loader.discover()
    section = loader.render_prompt_section()
    assert "# Skills" in section
    assert "pdf-tools" in section and "合并、拆分 PDF" in section
    loader.set_enabled("pdf-tools", False)
    loader.set_enabled("repo-audit", False)
    loader.discover()
    assert loader.render_prompt_section() == ""


async def test_load_skill_tool(tmp_path):
    loader = make_skills(tmp_path)
    loader.discover()
    tool = LoadSkillTool(loader)
    out = await tool.run(tool.args_model(name="pdf-tools"), ToolContext(working_dir=tmp_path))
    assert "pypdf" in out
    try:
        await tool.run(tool.args_model(name="nope"), ToolContext(working_dir=tmp_path))
        raise AssertionError("should raise")
    except ToolError:
        pass


# ---- 从网址安装：链接解析 / 子目录过滤 / 主机白名单 ----


def test_resolve_url_github_and_gitee():
    # 仓库主页 → 依次猜 main / master
    urls, sub = resolve_url("https://github.com/user/repo")
    assert sub is None
    assert urls == [
        "https://github.com/user/repo/archive/refs/heads/main.zip",
        "https://github.com/user/repo/archive/refs/heads/master.zip",
    ]
    # .git 后缀、www 主机也能认
    urls, _ = resolve_url("https://www.github.com/user/repo.git")
    assert urls[0].startswith("https://github.com/user/repo/archive/refs/heads/")
    # tree 页：分支 + 子目录
    urls, sub = resolve_url("https://github.com/user/repo/tree/dev/skills/pdf")
    assert urls == ["https://github.com/user/repo/archive/refs/heads/dev.zip"]
    assert sub == "skills/pdf"
    # Gitee 走自己的归档直链
    urls, sub = resolve_url("https://gitee.com/u/r/tree/main")
    assert urls == ["https://gitee.com/u/r/repository/archive/main.zip"]
    assert sub is None
    # releases 的 .zip 直链原样返回
    direct = "https://github.com/u/r/releases/download/v1/skill.zip"
    assert resolve_url(direct) == ([direct], None)


def test_resolve_url_rejects_bad_links():
    for bad, why in [
        ("ftp://github.com/u/r", "http"),
        ("https://example.com/a.zip", "GitHub / Gitee"),
        ("https://github.com/u/r/blob/main/README.md", "装不了"),
        ("https://github.com/u", "用户名"),
        ("", "粘贴"),
    ]:
        with pytest.raises(SkillInstallError) as ei:
            resolve_url(bad)
        assert why in str(ei.value), (bad, str(ei.value))


def test_install_from_zip_only_under(tmp_path):
    pack = tmp_path / "p.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("repo-main/skills/aaa/SKILL.md", "---\nname: aaa\ndescription: a\n---\n")
        zf.writestr("repo-main/skills/bbb/SKILL.md", "---\nname: bbb\ndescription: b\n---\n")
        zf.writestr("repo-main/other/ccc/SKILL.md", "---\nname: ccc\ndescription: c\n---\n")
    dest = tmp_path / "skills"
    r = install_from_zip(pack, dest, existing=set(), only_under="skills")
    assert r["installed"] == ["aaa", "bbb"]
    assert (dest / "aaa" / "SKILL.md").is_file()
    assert not (dest / "ccc").exists()

    # 指向不存在的子目录 → 明确报错，不留半个技能
    with pytest.raises(SkillInstallError) as ei:
        install_from_zip(pack, tmp_path / "skills2", existing=set(), only_under="nope")
    assert "没有找到技能" in str(ei.value)
    assert not (tmp_path / "skills2").exists() or list((tmp_path / "skills2").iterdir()) == []
