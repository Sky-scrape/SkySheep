"""Skills 模块测试。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from skysheep.skills import SCOPE_ALL, SCOPE_NONE, SCOPE_PROJECTS, SkillLoader
from skysheep.skills.installer import (
    SkillInstallError,
    install_from_zip,
    remove_skill,
    resolve_url,
)
from skysheep.skills.loader import _load_skill_from_dir
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


def test_discover_tolerates_stat_oserror(tmp_path, monkeypatch):
    """不受信任装入点（WinError 448）这类 OSError 会从 is_dir() 冒泡；
    发现循环把读不出来的条目跳过，不拖垮启动加载。"""
    real_is_dir = Path.is_dir

    def boom(self, **kw):
        if self.name == "broken":
            raise OSError(448, "无法遍历该路径，因为它包含不受信任的装入点。")
        return real_is_dir(self, **kw)

    monkeypatch.setattr(Path, "is_dir", boom)
    loader = make_skills(tmp_path)
    (tmp_path / "global_skills" / "broken").mkdir()  # 让 patch 真的命中一个条目
    names = {s.name for s in loader.discover()}
    assert names == {"pdf-tools", "repo-audit"}


def test_load_skill_from_dir_survives_stat_oserror(tmp_path, monkeypatch):
    real_is_file = Path.is_file

    def boom(self, **kw):
        if self.name == "SKILL.md":
            raise OSError(448, "无法遍历该路径，因为它包含不受信任的装入点。")
        return real_is_file(self, **kw)

    monkeypatch.setattr(Path, "is_file", boom)
    assert _load_skill_from_dir(tmp_path / "any", "global") is None


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


def test_skill_description_and_name_are_bounded(tmp_path):
    """技能名/描述会被拼进系统提示词：必须限长并压掉空白，不让外部
    frontmatter 撑破清单排版或把注入内容推到看不见的位置。"""
    g = tmp_path / "global_skills"
    p = tmp_path / "proj" / ".skysheep" / "skills"
    p.mkdir(parents=True, exist_ok=True)
    long_desc = "A" * 5000
    long_name = "n" * 500
    d = g / "bloated"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {long_name}\ndescription: {long_desc}\n---\n正文\n",
        encoding="utf-8",
    )
    # 用连续空行把内容往下推、并在描述里塞换行：都不该出现在清单里
    d2 = g / "multiline"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / "SKILL.md").write_text(
        "---\nname: multi\ndescription: 第一行\n\n\n\n第二行\n---\n正文\n",
        encoding="utf-8",
    )
    loader = SkillLoader(global_dir=g, project_dir=p, state_path=tmp_path / "skills.json")
    loader.discover()

    skill = loader.get("n" * 120)
    assert skill is not None, "超长技能名应被截断到上限，而不是原样保留"
    assert len(skill.name) == 120
    assert len(skill.description) < 1100 and skill.description.endswith("（描述过长已截断）")

    multi = loader.get("multi")
    assert "\n\n\n" not in multi.description  # 连续空行已压缩

    # 清单里每条仍是一行，超长描述不会把后续技能挤没
    section = loader.render_prompt_section()
    for line in section.splitlines():
        assert len(line) < 1200, "单条清单行不应被外部描述撑爆"


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


# ---- 使用范围（scope）：全局技能可限定在哪些项目生效 ----

def make_scoped(tmp_path, project_root):
    """一套带范围配置的 loader（global_dir / scope_path 跨项目共享）。"""
    loader = make_skills(tmp_path)
    loader.scope_path = tmp_path / "skills-scope.json"
    loader.project_root = project_root
    loader.discover()
    return loader

def test_scope_defaults_to_all_projects(tmp_path):
    """没配过范围（含老用户升级）时：全局技能对所有项目生效，行为不变。"""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    loader = make_scoped(tmp_path, proj)
    assert loader.get("pdf-tools").scope == SCOPE_ALL
    assert loader.applies("pdf-tools") is True
    assert loader.applies("repo-audit") is True  # 项目技能在当前项目里
    assert not (tmp_path / "skills-scope.json").exists()  # 不读不写

def test_scope_projects_limits_to_listed_projects(tmp_path):
    """「指定项目」：列在里面的生效，其他项目里既不进提示词也不能 load。"""
    pa, pb = tmp_path / "projA", tmp_path / "projB"
    pa.mkdir(exist_ok=True)
    pb.mkdir(exist_ok=True)
    a = make_scoped(tmp_path, pa)
    a.set_scope("pdf-tools", SCOPE_PROJECTS, [str(pa)])

    b = make_scoped(tmp_path, pb)  # 另一个项目读同一份范围配置
    assert a.applies("pdf-tools") is True
    assert b.applies("pdf-tools") is False
    assert "pdf-tools" not in b.render_prompt_section()
    # 不生效的技能仍要出现在 all() 里，否则在被排除的项目里就再也改不回来了
    assert "pdf-tools" in [s.name for s in b.all()]
    try:
        b.load_body("pdf-tools")
        raise AssertionError("越范围不该允许 load")
    except KeyError:
        pass
    # 预览不受启用/范围限制（否则用户无从判断该不该启用）
    assert "pypdf" in b.raw_text("pdf-tools")

def test_scope_none_disables_everywhere(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    loader = make_scoped(tmp_path, proj)
    loader.set_scope("pdf-tools", SCOPE_NONE)
    assert loader.applies("pdf-tools") is False
    # 全局技能被排除后，提示词里只剩项目技能（repo-audit）
    section = loader.render_prompt_section()
    assert "pdf-tools" not in section
    assert "repo-audit" in section

def test_scope_persists_across_instances(tmp_path):
    """范围存在全局 skills-scope.json，换实例（相当于重开程序）仍在。"""
    pa = tmp_path / "projA"
    pa.mkdir(exist_ok=True)
    make_scoped(tmp_path, pa).set_scope("pdf-tools", SCOPE_PROJECTS, [str(pa)])
    again = make_scoped(tmp_path, pa)
    assert again.get("pdf-tools").scope == SCOPE_PROJECTS
    assert again.applies("pdf-tools") is True
    assert (tmp_path / "skills-scope.json").is_file()

def test_scope_project_skill_is_fixed(tmp_path):
    """项目技能天生只属于一个项目：范围固定，拒绝修改（避免改出无意义的配置）。"""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    loader = make_scoped(tmp_path, proj)
    assert loader.get("repo-audit").scope == "project"
    try:
        loader.set_scope("repo-audit", SCOPE_ALL)
        raise AssertionError("项目技能不该允许改范围")
    except RuntimeError as e:
        assert "本项目" in str(e)

def test_scope_validates_and_normalizes(tmp_path):
    """参数校验与路径归一：非法 mode 报错；空项目列表报错；大小写/重复去重。"""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    loader = make_scoped(tmp_path, proj)
    try:
        loader.set_scope("pdf-tools", "whatever")
        raise AssertionError("非法 mode 应报错")
    except RuntimeError:
        pass
    try:
        loader.set_scope("pdf-tools", SCOPE_PROJECTS, [])
        raise AssertionError("指定项目但一个都没勾，应报错")
    except RuntimeError as e:
        assert "至少" in str(e)
    # 大小写不同 + 重复的同一路径 → 去重后只留一个
    loader.set_scope("pdf-tools", SCOPE_PROJECTS, [str(proj), str(proj).upper(), str(proj)])
    assert len(loader.get("pdf-tools").scope_projects) == 1
    assert loader.applies("pdf-tools") is True

def test_scope_windows_case_insensitive(tmp_path):
    """Windows 上路径大小写不敏感：配置里的大小写不同也要能匹配上。"""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    loader = make_scoped(tmp_path, proj)
    loader.set_scope("pdf-tools", SCOPE_PROJECTS, [str(proj).upper()])
    assert loader.applies("pdf-tools") is True


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


def test_market_index_falls_back_to_direct_when_proxy_broken(monkeypatch):
    """先试系统代理、失败再直连：代理挂了不该让整个广场降级成内置清单。

    拉索引过去写死 trust_env=False（忽略系统代理），而国内直连
    raw.githubusercontent.com 经常超时，表现就是「在线索引暂时拉取不到」。
    """
    import asyncio

    from skysheep.skills import market as mk

    calls: list[bool] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": [{"name": "x", "description": "d", "url": "https://github.com/u/r"}]}

    async def fake_get(url, timeout_s, trust_env):
        calls.append(trust_env)
        if trust_env:
            raise RuntimeError("代理不可用")
        return _Resp()

    monkeypatch.setattr(mk, "_get_index", fake_get)
    r = asyncio.run(mk.fetch_market_index())
    assert calls == [True, False], "应当先试代理、失败再直连"
    assert r["source"] == "remote" and r["items"][0]["name"] == "x"


def test_market_index_prefers_proxy_when_available(monkeypatch):
    """代理可用时一次就拿到，不做多余的直连尝试。"""
    import asyncio

    from skysheep.skills import market as mk

    calls: list[bool] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"items": [{"name": "y", "description": "d", "url": "https://github.com/u/r"}]}

    async def fake_get(url, timeout_s, trust_env):
        calls.append(trust_env)
        return _Resp()

    monkeypatch.setattr(mk, "_get_index", fake_get)
    r = asyncio.run(mk.fetch_market_index())
    assert calls == [True]
    assert r["source"] == "remote" and r["items"][0]["name"] == "y"


def test_market_index_falls_back_to_builtin_when_offline(monkeypatch):
    """两条路都不通才降级内置清单，且带上可读提示（永不抛错）。"""
    import asyncio

    from skysheep.skills import market as mk

    async def fake_get(url, timeout_s, trust_env):
        raise RuntimeError("没网")

    monkeypatch.setattr(mk, "_get_index", fake_get)
    r = asyncio.run(mk.fetch_market_index())
    assert r["source"] == "builtin" and r["items"]
    assert "拉取不到" in r["note"]


def test_delete_forgets_leftover_state(tmp_path):
    """删除技能要抹掉它的遗留状态，否则重装后莫名“装上了却是停用/任何项目都不用”。

    停用名单在 skills.json、范围在 skills-scope.json，两者都以技能名为 key；
    用户在界面上只做了一个「删除」，看不到这两个文件里还留着东西。
    """
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    scope_cfg = tmp_path / "skills-scope.json"
    g = tmp_path / "global_skills"
    (g / "docx").mkdir(parents=True)
    (g / "docx" / "SKILL.md").write_text("---\nname: docx\ndescription: d\n---\nX\n", encoding="utf-8")

    def make():
        loader = SkillLoader(
            global_dir=g,
            project_dir=proj / ".skysheep" / "skills",
            state_path=proj / ".skysheep" / "skills.json",
            scope_path=scope_cfg,
            project_root=proj,
        )
        loader.discover()
        return loader

    loader = make()
    loader.set_enabled("docx", False)
    loader.set_scope("docx", SCOPE_NONE)

    # 删除：后端先删目录（remove_skill），再调 forget 抹状态
    remove_skill("docx", [g])
    loader.forget("docx")
    loader.discover()
    assert json.loads((proj / ".skysheep" / "skills.json").read_text(encoding="utf-8"))["disabled"] == []
    assert json.loads(scope_cfg.read_text(encoding="utf-8"))["scopes"] == {}

    # 重新安装 → 干净状态，直接可用
    (g / "docx").mkdir(parents=True)
    (g / "docx" / "SKILL.md").write_text("---\nname: docx\ndescription: d\n---\nX\n", encoding="utf-8")
    fresh = make().get("docx")
    assert fresh.enabled is True
    assert make().applies("docx") is True


def test_market_index_anthropics_links_are_current():
    """索引里的 anthropics/skills 子目录链接必须指向当前结构。

    上游把技能从 document-skills/ 移到 skills/ 后，广场里这 4 条会全部安装失败
    （报「链接指向的目录里没有找到技能」）。这个错用户很难自己定位，用测试钉住。
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]  # engine/tests → 仓库根
    data = json.loads((root / "market" / "index.json").read_text(encoding="utf-8"))
    anth = [it for it in data["items"] if "anthropics/skills" in it["url"]]
    assert anth, "索引里应有 anthropics/skills 的条目"
    for it in anth:
        assert "document-skills/" not in it["url"], (
            f"「{it['name']}」链接已过期（上游改为 skills/）：{it['url']}"
        )
    # 内置清单与索引保持一致（离线回退时用的是前者）
    from skysheep.skills.market import BUILTIN_INDEX

    builtin = {b["name"]: b["url"] for b in BUILTIN_INDEX}
    for it in data["items"]:
        if it["name"] in builtin:
            assert builtin[it["name"]] == it["url"], f"内置清单与索引不一致：{it['name']}"


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

    # 指向不存在的子目录 → 明确报错，不留半个技能，且提示里要能照看到正确的目录
    with pytest.raises(SkillInstallError) as ei:
        install_from_zip(pack, tmp_path / "skills2", existing=set(), only_under="nope")
    msg = str(ei.value)
    assert "没有技能" in msg
    # 把包里实际找到的技能目录列出来（剥掉 repo-main/ 这层归档包装）
    assert "skills/aaa" in msg and "skills/bbb" in msg and "other/ccc" in msg
    assert "repo-main" not in msg
    assert not (tmp_path / "skills2").exists() or list((tmp_path / "skills2").iterdir()) == []

def test_install_hint_points_at_moved_skill_dir(tmp_path):
    """上游改目录结构时，报错要直接点名该把链接改成什么。

    anthropics/skills 把技能从 document-skills/ 移到了 skills/，索引里的旧链接会全失效。
    只说「没找到」用户无从下手，提示里应给出可直接照抄的正确目录。
    """
    pack = tmp_path / "p.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("skills-main/skills/docx/SKILL.md", "---\nname: docx\ndescription: d\n---\n")
        zf.writestr("skills-main/skills/pdf/SKILL.md", "---\nname: pdf\ndescription: p\n---\n")
    with pytest.raises(SkillInstallError) as ei:
        install_from_zip(
            pack, tmp_path / "skills", existing=set(),
            only_under="document-skills/docx",
        )
    msg = str(ei.value)
    # 末段同名（docx）→ 直接给出该改成哪个目录
    assert "把地址中的目录换成：skills/docx" in msg
    assert "skills-main" not in msg  # 归档包装目录不展示给用户


def test_frontmatter_block_scalar_description(tmp_path):
    """第三方技能大量用 YAML 块标量写描述（description: >- / | 后跟缩进行）——
    此前朴素逐行解析会把值读成字面量 ">-"，描述整段丢失只剩两个字符。"""
    g = tmp_path / "global_skills"
    p = tmp_path / "proj" / ".skysheep" / "skills"
    p.mkdir(parents=True, exist_ok=True)

    d = g / "folded"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: folded\ndescription: >-\n  第一段说明，\n  接着同一句。\n\n  新的一段。\n---\n正文\n",
        encoding="utf-8",
    )
    d2 = g / "literal"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / "SKILL.md").write_text(
        "---\nname: literal\ndescription: |\n  第一行\n  第二行\n---\n正文\n",
        encoding="utf-8",
    )
    d3 = g / "plain"
    d3.mkdir(parents=True, exist_ok=True)
    (d3 / "SKILL.md").write_text("---\nname: plain\ndescription: 单行写法\n---\n正文\n", encoding="utf-8")

    loader = SkillLoader(global_dir=g, project_dir=p, state_path=tmp_path / "skills.json")
    loader.discover()

    folded = loader.get("folded")
    assert folded is not None and folded.description
    assert ">-" not in folded.description, "块标量标记不能原样出现在描述里"
    assert "第一段说明，" in folded.description and "接着同一句" in folded.description

    literal = loader.get("literal")
    assert literal is not None and "第一行" in literal.description and "第二行" in literal.description

    plain = loader.get("plain")
    assert plain is not None and plain.description == "单行写法"


def test_remove_skill_handles_readonly_git_files(tmp_path):
    """git 克隆安装的技能带 .git（pack/idx 文件带只读位）——Windows 上普通
    rmtree 会报 WinError 5 拒绝访问，删除前必须清只读位。"""
    import os
    import stat

    from skysheep.skills.installer import remove_skill, rmtree_force

    root = tmp_path / "skills"
    d = root / "self-improvement"
    pack_dir = d / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: self-improvement\ndescription: x\n---\n正文\n", encoding="utf-8"
    )
    pack = pack_dir / "pack-1d687ce60241f44008f4eed22f58a87d754234ab.idx"
    pack.write_bytes(b"\xf7tOc")  # 真实 idx 魔数
    os.chmod(pack, stat.S_IREAD)  # git 写 pack 文件后置只读位
    os.chmod(pack_dir, stat.S_IREAD)  # 目录也可能带只读位

    res = remove_skill("self-improvement", [root])
    assert res["removed"] == "self-improvement"
    assert not d.exists()

    # rmtree_force 单独用：目录不存在时安静返回
    rmtree_force(tmp_path / "no-such-dir")


# --------------------------------------------------------------- H1：技能名路径穿越
#
# SKILL.md frontmatter 的 name 是技能包作者可控的外部内容，而安装落点是
# `dest_root / name`、overwrite 时先 rmtree 再写——名字带 `..`/分隔符/盘符就能
# 在技能目录之外任意删除+写入。下面三个用例卡住这条链。

_EVIL_NAMES = [
    "..\\..\\evil",
    "../../evil",
    "..",
    ".",
    "C:\\Windows\\Temp\\evil",
    "a/b",
    "a\\b",
    "...",
]


def _zip_with_skill(path, name: str):
    """造一个最小技能 zip：外层套一层目录，frontmatter 的 name 由参数决定。"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pkg/SKILL.md", f"---\nname: {name}\ndescription: x\n---\n正文\n")
    return path


@pytest.mark.parametrize("evil", _EVIL_NAMES)
def test_install_from_zip_rejects_traversing_skill_name(tmp_path, evil):
    """恶意 name 的技能包必须装不进去，且技能目录之外一个字节都不动。"""
    dest = tmp_path / "skills"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("别动我", encoding="utf-8")

    zp = _zip_with_skill(tmp_path / "evil.zip", evil)
    # overwrite=True 是市场「更新/重装」走的路径：不卡名字就是先删后写
    with pytest.raises(SkillInstallError):
        install_from_zip(zp, dest, existing=set(), overwrite=True)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "别动我"
    assert list(dest.iterdir()) == []  # 暂存目录也已在 finally 里清掉


@pytest.mark.parametrize("evil", _EVIL_NAMES)
def test_remove_skill_rejects_traversing_name(tmp_path, evil):
    """remove_skill 的 name 来自界面/请求参数，不得 rmtree 到候选根之外。"""
    root = tmp_path / "skills"
    (root / "good").mkdir(parents=True)
    (root / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: x\n---\n", encoding="utf-8"
    )
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "SKILL.md").write_text("---\nname: v\ndescription: x\n---\n", encoding="utf-8")

    with pytest.raises(SkillInstallError):
        remove_skill(evil, [root])
    assert victim.exists() and (victim / "SKILL.md").exists()

    # 正常名字仍然能删（别把合法路径一并卡死）
    assert remove_skill("good", [root])["removed"] == "good"
    assert not (root / "good").exists()


def test_skill_name_shape_and_target_containment(tmp_path):
    """单元级：名字形状校验 + 落点包含性校验。"""
    from skysheep.skills.installer import _skill_target, _validate_skill_name

    dest = tmp_path / "skills"
    dest.mkdir()
    assert _skill_target(dest, "pdf-tools") == (dest / "pdf-tools").resolve()
    assert _validate_skill_name("  pdf-tools  ") == "pdf-tools"

    for bad in _EVIL_NAMES + ["", "   ", "a:b", "x" * 200]:
        with pytest.raises(SkillInstallError):
            _validate_skill_name(bad)

    # 名字合法但 resolve 后跑到根外（符号链接等）也要拦：直接造一个指向外部的链接
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (dest / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本平台不允许创建目录符号链接")
    with pytest.raises(SkillInstallError):
        _skill_target(dest, "link")
