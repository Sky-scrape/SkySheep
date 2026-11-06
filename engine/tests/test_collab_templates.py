"""协作模板内核测试：模型 / 校验 / 存储 / 内置模板 / 命令前缀守卫。

覆盖范围与需求对应：
- 内置三张模板（圆桌/团队/对抗）、深拷贝、团队 needs 派生；
- 用户模板 roundtrip、同名覆盖、删除、坏文件与坏条目容错；
- 名称 / 角色数 / 提示词长度 / needs / 工具条目 / 预算的上限校验；
- roundtable / adversarial 只能只读且不能声明工具（本期纯文本）；
- run_command 前缀复用权限门提炼：单词前缀、解释器调用、shell 拼接一律拒；
- schema 里没有 gate 字段（模板数据不能关闭权限门）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skysheep.core.collab_templates import (
    BUILTIN_NAMES,
    COLLAB_TEMPLATE_NAME_MAX,
    CollabAggregator,
    CollabBudget,
    CollabLimits,
    CollabRole,
    CollabTemplate,
    CollabTemplateError,
    CollabTemplateStore,
    builtin_template,
    builtin_templates,
    collect_command_prefixes,
    derive_needs,
    effective_tool_entries,
    is_builtin_template_name,
    normalize_command_prefix,
    validate_collab_template,
    validate_collab_template_name,
)

# ---------------------------------------------------------------- helpers


def role(**overrides) -> CollabRole:
    data = {"key": "r1", "name": "角色一"}
    data.update(overrides)
    return CollabRole(**data)


def template(**overrides) -> CollabTemplate:
    data = {
        "name": "我的圆桌",
        "kind": "roundtable",
        "roles": [role()],
    }
    data.update(overrides)
    return CollabTemplate(**data)


def team_template(**overrides) -> CollabTemplate:
    data = {
        "name": "我的团队",
        "kind": "team",
        "roles": [
            role(key="researcher", name="调研员", needs="read",
                 tools_allow=["read_file", "list_dir"]),
            role(key="builder", name="实施者", needs="write",
                 tools_allow=["read_file", "write_file"]),
        ],
    }
    data.update(overrides)
    return CollabTemplate(**data)


def user_names(store: CollabTemplateStore) -> list[str]:
    return [t.name for t in store.templates]


# ---------------------------------------------------------------- 内置模板


def test_builtin_templates_cover_three_kinds_and_are_deep_copied():
    templates = builtin_templates()
    assert [t.kind for t in templates] == ["roundtable", "team", "adversarial"]
    assert tuple(t.name for t in templates) == BUILTIN_NAMES
    assert all(t.builtin for t in templates)

    # 每次返回独立副本：改一份不污染模块常量与下一次调用
    templates[0].roles.clear()
    templates[0].name = "被改了"
    again = builtin_templates()
    assert again[0].name == BUILTIN_NAMES[0]
    assert len(again[0].roles) == 3


def test_builtin_team_needs_derived_write_and_text_kinds_are_read_only():
    team = builtin_template("团队·交付小组")
    assert team is not None and team.builtin
    assert derive_needs(team.roles) == "write"
    # 内置团队同样不承诺删除：delete_file 在 deny 里，运行时仍逐次确认
    assert "delete_file" in team.roles[1].tools_deny

    for name in ("圆桌·多模型评审", "对抗·代码审查"):
        tpl = builtin_template(name)
        assert tpl is not None
        assert tpl.roles, "内置纯文本模板仍带角色表，便于预览与展示"
        assert all(r.needs == "read" for r in tpl.roles)
        assert all(not r.tools_allow and not r.tools_deny for r in tpl.roles)
        assert not collect_command_prefixes(tpl.roles)


def test_builtin_template_lookup_and_name_predicate():
    assert builtin_template("不存在的内置") is None
    assert is_builtin_template_name(" 圆桌·多模型评审 ") is True
    assert is_builtin_template_name("我的模板") is False


# ---------------------------------------------------------------- 存储


def test_store_roundtrip_preserves_all_fields(tmp_path: Path):
    path = tmp_path / "collab_templates.json"
    store = CollabTemplateStore(path)
    store.load()
    assert [d["name"] for d in store.list()] == list(BUILTIN_NAMES)

    tpl = CollabTemplate(
        name="评审·双模型",
        kind="roundtable",
        desc="两个视角互评",
        roles=[
            CollabRole(key="a", name="甲", provider="p1", model="m1",
                       prompt="只挑逻辑错误"),
            CollabRole(key="b", name="乙", provider="p2", model="m2"),
        ],
        aggregator=CollabAggregator(provider="p3", model="m3", prompt="融合时保留分歧"),
        limits=CollabLimits(debate_rounds=1, member_timeout_s=120),
        budget=CollabBudget(max_calls=9, max_tokens=5000, max_seconds=300),
        created_at=123.0,
    )
    store.upsert(tpl)
    assert path.exists()

    reloaded = CollabTemplateStore(path)
    reloaded.load()
    got = reloaded.get("评审·双模型")
    assert got is not None
    assert got.kind == "roundtable"
    assert got.desc == "两个视角互评"
    assert [r.key for r in got.roles] == ["a", "b"]
    assert got.roles[0].provider == "p1" and got.roles[0].model == "m1"
    assert got.roles[0].prompt == "只挑逻辑错误"
    assert got.roles[1].needs == "read"
    assert got.aggregator.provider == "p3" and got.aggregator.model == "m3"
    assert got.aggregator.prompt == "融合时保留分歧"
    assert got.limits.debate_rounds == 1 and got.limits.member_timeout_s == 120
    assert got.budget.max_calls == 9
    assert got.budget.max_tokens == 5000
    assert got.budget.max_seconds == 300
    assert got.created_at == 123.0
    assert got.builtin is False


def test_same_name_overwrites_in_place_and_stamps_created_at(tmp_path: Path):
    store = CollabTemplateStore(tmp_path / "c.json")
    store.upsert(template(name="甲模板", created_at=1.0))
    store.upsert(template(name="乙模板", created_at=2.0))
    store.upsert(template(name="甲模板", desc="新说明", created_at=0))

    assert user_names(store) == ["甲模板", "乙模板"]  # 位置不变、不重复追加
    got = store.get("甲模板")
    assert got is not None
    assert got.desc == "新说明"
    assert got.created_at > 1.0  # 未填时刻由 store 打上当前时间


def test_remove_deletes_and_persists(tmp_path: Path):
    path = tmp_path / "c.json"
    store = CollabTemplateStore(path)
    store.upsert(template(name="要删的"))
    store.remove("要删的")
    assert user_names(store) == []
    assert CollabTemplateStore(path).list() is not None

    reloaded = CollabTemplateStore(path)
    reloaded.load()
    assert user_names(reloaded) == []

    with pytest.raises(CollabTemplateError, match="找不到协作模板"):
        store.remove("没有的")


def test_builtin_name_cannot_be_overwritten_or_removed(tmp_path: Path):
    store = CollabTemplateStore(tmp_path / "c.json")
    with pytest.raises(CollabTemplateError, match="内置模板名"):
        store.upsert(template(name=BUILTIN_NAMES[0]))
    with pytest.raises(CollabTemplateError, match="内置模板不能删除"):
        store.remove(BUILTIN_NAMES[0])
    assert not (tmp_path / "c.json").exists()  # 校验先失败，不写盘


def test_team_kind_cannot_enter_collab_store(tmp_path: Path):
    store = CollabTemplateStore(tmp_path / "c.json")
    with pytest.raises(CollabTemplateError, match="团队模板"):
        store.upsert(team_template())
    assert store.templates == []


def test_missing_and_corrupt_files_read_as_empty_album(tmp_path: Path):
    missing = CollabTemplateStore(tmp_path / "missing.json")
    missing.load()
    assert missing.templates == [] and missing.skipped == 0

    bad = tmp_path / "bad.json"
    bad.write_text("{ 不是 JSON", encoding="utf-8")
    store = CollabTemplateStore(bad)
    store.load()
    assert store.templates == [] and store.skipped == 0  # 整份坏文件按空册

    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps({"templates": [
        {"name": "好的", "kind": "roundtable",
         "roles": [{"key": "a", "name": "甲"}]},
        "垃圾行",
        {"name": 42},
    ]}, ensure_ascii=False), encoding="utf-8")
    store2 = CollabTemplateStore(rows)
    store2.load()
    assert [t.name for t in store2.templates] == ["好的"]
    assert store2.skipped == 2


def test_load_skips_hand_edited_invalid_entries(tmp_path: Path):
    """手改 JSON 写入非法 needs / kind / 负预算不能进运行期（R23）。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"templates": [
        {"name": "坏-needs", "kind": "roundtable",
         "roles": [{"key": "a", "name": "甲", "needs": "root"}]},
        {"name": "坏-kind", "kind": "x",
         "roles": [{"key": "a", "name": "甲"}]},
        {"name": "坏预算", "kind": "roundtable",
         "roles": [{"key": "a", "name": "甲"}],
         "budget": {"max_calls": -1}},
        {"name": "坏-团队越册", "kind": "team",
         "roles": [{"key": "a", "name": "甲", "needs": "write"}]},
        {"name": "好的", "kind": "roundtable",
         "roles": [{"key": "a", "name": "甲"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    store = CollabTemplateStore(path)
    store.load()
    assert [t.name for t in store.templates] == ["好的"]
    assert store.skipped == 4
    assert store.get("坏-needs") is None


def test_load_duplicate_names_last_wins(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"templates": [
        {"name": "同名", "kind": "roundtable", "desc": "旧",
         "roles": [{"key": "a", "name": "甲"}]},
        {"name": "同名", "kind": "roundtable", "desc": "新",
         "roles": [{"key": "a", "name": "甲"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    store = CollabTemplateStore(path)
    store.load()
    assert len(store.templates) == 1
    assert store.templates[0].desc == "新"


def test_get_returns_copy_not_internal_state(tmp_path: Path):
    store = CollabTemplateStore(tmp_path / "c.json")
    store.upsert(template(name="内部"))
    got = store.get("内部")
    assert got is not None
    got.name = "改了"
    got.roles.clear()
    again = store.get("内部")
    assert again is not None and again.name == "内部" and len(again.roles) == 1


# ---------------------------------------------------------------- 名称与形状校验


def test_name_validation_rejects_paths_colons_and_directory_refs():
    assert validate_collab_template_name(" 评审 ") == "评审"
    for bad in ["", "   ", "a/b", "a\\b", "C:模板", "a:b", "..", ".", " . "]:
        with pytest.raises(CollabTemplateError):
            validate_collab_template_name(bad)
    with pytest.raises(CollabTemplateError, match="过长"):
        validate_collab_template_name("名" * (COLLAB_TEMPLATE_NAME_MAX + 1))
    assert validate_collab_template_name("名" * COLLAB_TEMPLATE_NAME_MAX)


def test_desc_too_long_rejected():
    with pytest.raises(CollabTemplateError, match="说明过长"):
        validate_collab_template(template(desc="说" * 501))


def test_role_count_bounds():
    with pytest.raises(CollabTemplateError, match="至少需要一个角色"):
        validate_collab_template(template(roles=[]))

    thirteen = [role(key=f"r{i}", name=f"角色{i}") for i in range(13)]
    with pytest.raises(CollabTemplateError, match="最多 12 个角色"):
        validate_collab_template(template(roles=thirteen))

    twelve = [role(key=f"r{i}", name=f"角色{i}") for i in range(12)]
    assert len(validate_collab_template(template(roles=twelve)).roles) == 12

    # 内置模板允许 0 角色（走现有自动解析）
    empty_builtin = validate_collab_template(
        template(name="内置空角色", roles=[]), allow_builtin=True,
    )
    assert empty_builtin.roles == []


def test_role_key_and_name_required_and_key_unique():
    with pytest.raises(CollabTemplateError, match="缺少名称"):
        validate_collab_template(template(roles=[role(name="")]))
    with pytest.raises(CollabTemplateError, match="缺少 key"):
        validate_collab_template(template(roles=[role(key="")]))
    with pytest.raises(CollabTemplateError, match="key 重复"):
        validate_collab_template(template(roles=[
            role(key="dup", name="甲"), role(key="dup", name="乙"),
        ]))


def test_role_prompt_length_boundary():
    validate_collab_template(template(roles=[role(prompt="字" * 4000)]))
    with pytest.raises(CollabTemplateError, match="提示词过长"):
        validate_collab_template(template(roles=[role(prompt="字" * 4001)]))
    with pytest.raises(CollabTemplateError, match="收口角色的提示词过长"):
        validate_collab_template(template(
            aggregator=CollabAggregator(prompt="收" * 4001),
        ))


def test_invalid_needs_rejected():
    with pytest.raises(CollabTemplateError, match="needs 只能是"):
        validate_collab_template(template(roles=[role(needs="root")]))


def test_text_kinds_only_allow_read_without_tools():
    with pytest.raises(CollabTemplateError, match="圆桌模板.*只能是只读"):
        validate_collab_template(template(roles=[role(needs="write")]))
    with pytest.raises(CollabTemplateError, match="不能声明工具"):
        validate_collab_template(template(roles=[role(tools_allow=["read_file"])]))

    with pytest.raises(CollabTemplateError, match="对抗模板.*只能是只读"):
        validate_collab_template(template(
            kind="adversarial", roles=[role(needs="exec")],
        ))
    with pytest.raises(CollabTemplateError, match="不能声明工具"):
        validate_collab_template(template(
            kind="adversarial", roles=[role(tools_deny=["delete_file"])],
        ))

    # team 允许 write / exec 与工具声明
    validated = validate_collab_template(team_template(
        roles=[role(name="实施者", needs="write",
                    tools_allow=["read_file", "write_file"])],
    ))
    assert validated.roles[0].needs == "write"


def test_team_duplicate_provider_model_pair_rejected():
    with pytest.raises(CollabTemplateError, match="同一个服务与模型"):
        validate_collab_template(team_template(roles=[
            role(key="a", name="甲", provider="p", model="m"),
            role(key="b", name="乙", provider="p", model="m"),
        ]))
    # 空 provider/model 是「自动补位」，不参与重复判定
    validate_collab_template(team_template(roles=[
        role(key="a", name="甲"), role(key="b", name="乙"),
    ]))


# ---------------------------------------------------------------- 工具条目 / 命令前缀


def test_bare_run_command_rejected_but_prefixed_accepted():
    with pytest.raises(CollabTemplateError, match="不能整工具放行"):
        validate_collab_template(team_template(roles=[
            role(name="实施者", needs="write", tools_allow=["run_command"]),
        ]))

    validated = validate_collab_template(team_template(roles=[
        role(name="实施者", needs="exec",
             tools_allow=["run_command:git status", "run_command:git status"]),
    ]))
    assert validated.roles[0].tools_allow == ["run_command:git status"]

    # deny 里写裸 run_command 是「禁止所有命令」，安全，允许
    validated2 = validate_collab_template(team_template(roles=[
        role(name="实施者", needs="exec", tools_deny=["run_command"]),
    ]))
    assert validated2.roles[0].tools_deny == ["run_command"]


def test_tool_entry_shape_validation():
    with pytest.raises(CollabTemplateError, match="不能为空"):
        validate_collab_template(team_template(roles=[
            role(name="实施者", tools_allow=["  "]),
        ]))
    with pytest.raises(CollabTemplateError, match="英文逗号"):
        validate_collab_template(team_template(roles=[
            role(name="实施者", tools_allow=["read_file,write_file"]),
        ]))
    with pytest.raises(CollabTemplateError, match="过长"):
        validate_collab_template(team_template(roles=[
            role(name="实施者", tools_allow=["x" * 81]),
        ]))


@pytest.mark.parametrize("prefix", [
    "python", "python3", "py", "bash", "sh", "powershell", "pwsh",
    "node", "pytest", "npm",
])
def test_single_word_command_prefix_rejected(prefix: str):
    with pytest.raises(CollabTemplateError, match="只有一个词"):
        normalize_command_prefix(prefix)


@pytest.mark.parametrize("bad", [
    'python -c "print(1)"',
    "python -m pytest",
    "bash setup.sh",
    "node build.js",
    "powershell -File x.ps1",
    "git status; rm -rf /",
    "git status && rm -rf /",
    "git status | cat",
    "docker run",
    "sudo apt install evil",
    "git status --short",  # 提炼结果与所写不一致：写宽了
    "git commit -m x",     # 同上：只接受提炼出的两词前缀
])
def test_unsafe_or_overbroad_command_prefix_rejected(bad: str):
    with pytest.raises(CollabTemplateError):
        normalize_command_prefix(bad)


@pytest.mark.parametrize("bad", [
    "rm -rf", "rm -f", "rm -Rf", "del /f", "del /q", "rmdir /s",
    "find .", "tar -xf", "npm run", "git clean", "taskkill /f",
])
def test_destructive_or_arbitrary_subcommand_prefix_rejected(bad: str):
    """删除 / 任意子命令执行族不能作为运行级免确认前缀（审查 finding 3）。

    两词前缀等于整族放行：``rm -rf`` 能删任意路径、``npm run`` 能跑任意脚本，
    与 delete_file「DANGEROUS 永不自动放行」的保护直接冲突。
    """
    with pytest.raises(CollabTemplateError, match="删除或任意子命令"):
        normalize_command_prefix(bad)


def test_safe_command_prefix_normalized():
    assert normalize_command_prefix("  git status  ") == "git status"
    assert normalize_command_prefix("git diff") == "git diff"
    assert normalize_command_prefix("cargo test") == "cargo test"


def test_collect_command_prefixes_preserves_order_and_dedupes():
    roles = [
        role(key="a", name="甲", needs="exec",
             tools_allow=["run_command:git status", "read_file"]),
        role(key="b", name="乙", needs="exec",
             tools_allow=["run_command:git diff", "run_command:git status"]),
    ]
    assert collect_command_prefixes(roles) == ["git status", "git diff"]
    assert effective_tool_entries(roles) == [
        "run_command:git status", "read_file", "run_command:git diff",
    ]


def test_helpers_accept_model_dump_dicts():
    """backend 传的是 model_dump() 后的 dict：派生函数两种形态都要认。"""
    roles = [
        {"key": "a", "name": "甲", "needs": "write",
         "tools_allow": ["run_command:git status", "delete_file"],
         "tools_deny": ["delete_file"]},
    ]
    assert derive_needs(roles) == "write"
    assert collect_command_prefixes(roles) == ["git status"]
    assert effective_tool_entries(roles) == ["run_command:git status"]


def test_tools_deny_wins_over_allow():
    roles = [
        role(key="a", name="甲", needs="write",
             tools_allow=["read_file", "write_file", "delete_file"],
             tools_deny=["delete_file"]),
        role(key="b", name="乙", needs="read", tools_allow=["read_file", "grep"]),
    ]
    assert effective_tool_entries(roles) == ["read_file", "write_file", "grep"]

    # 裸 run_command 整工具剔除；带前缀的 deny 只剔除同一条前缀
    roles2 = [
        role(key="a", name="甲", needs="exec",
             tools_allow=["run_command:git status", "run_command:git diff"],
             tools_deny=["run_command:git status"]),
    ]
    assert effective_tool_entries(roles2) == ["run_command:git diff"]


# ---------------------------------------------------------------- 预算 / 限额


@pytest.mark.parametrize("budget", [
    CollabBudget(max_calls=-1),
    CollabBudget(max_calls=501),
    CollabBudget(max_tokens=-5),
    CollabBudget(max_seconds=-1),
    CollabBudget(max_seconds=86401),
])
def test_budget_bounds_rejected(budget: CollabBudget):
    with pytest.raises(CollabTemplateError, match="不能为负|不能超过"):
        validate_collab_template(template(budget=budget))


@pytest.mark.parametrize("limits", [
    CollabLimits(max_members=-1),
    CollabLimits(max_members=13),
    CollabLimits(member_timeout_s=-1),
    CollabLimits(debate_rounds=-1),
    CollabLimits(debate_rounds=3),
    CollabLimits(max_findings=-1),
    CollabLimits(role_timeout_s=86401),
])
def test_limits_bounds_rejected(limits: CollabLimits):
    with pytest.raises(CollabTemplateError, match="不能为负|不能超过"):
        validate_collab_template(template(limits=limits))


# ---------------------------------------------------------------- 安全 schema


def test_schema_has_no_gate_field_and_ignores_unknown_keys():
    """模板数据永远不能关闭/降低权限门：schema 里没有任何 gate 相关字段。"""
    fields = set(CollabTemplate.model_fields)
    assert not fields & {"gate", "auto_accept", "permission", "permissions"}
    assert "gate" not in CollabTemplate(**{
        "name": "越权尝试", "kind": "roundtable",
        "roles": [{"key": "a", "name": "甲"}],
        "gate": {"auto_accept_all": True},
    }).model_dump()


def test_collect_command_prefixes_respects_deny():
    """被角色显式 deny 的前缀不得进入运行级授权的放行面（审查 finding 2）。"""
    roles = [
        role(key="a", name="甲", needs="exec",
             tools_allow=["run_command:git status", "run_command:git diff"],
             tools_deny=["run_command:git status"]),
    ]
    assert collect_command_prefixes(roles) == ["git diff"]


def test_load_tolerates_non_utf8_file(tmp_path: Path):
    """手改模板册为 GBK 等非 UTF-8 时按坏文件回空册，不能让启动崩（finding 13）。"""
    path = tmp_path / "collab_templates.json"
    path.write_bytes('{"templates":[{"name":"我的模板"}]}'.encode("gbk"))
    store = CollabTemplateStore(path)
    store.load()
    assert store.templates == []


def test_team_template_persona_length_capped():
    """团队模板成员人设与协作模板 role.prompt 同口径限长（finding 10）。"""
    from pydantic import ValidationError

    from skysheep.core.team_store import TEAM_TEMPLATE_PERSONA_MAX, TeamTemplateMember

    with pytest.raises(ValidationError):
        TeamTemplateMember(name="甲", persona="x" * (TEAM_TEMPLATE_PERSONA_MAX + 1))
