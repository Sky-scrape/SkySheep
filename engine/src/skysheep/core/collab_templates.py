"""协作模板：圆桌 / 团队 / 对抗三种多模型协作的可复用配置。

定位（与「团队模板」的分工）：
- 本模块是**协作模板的模型 + 校验 + 存储内核**，三类 kind 统一一套 schema；
- 团队模板仍住在 ``~/.skysheep/teams.json``（``core/team_store.py``），本期
  不迁移；``CollabTemplateStore`` 只持久化用户自建的 roundtable / adversarial
  模板，kind=team 的用户模板由 backend 写进团队模板册；
- 三个内置模板（圆桌·多模型评审 / 团队·交付小组 / 对抗·代码审查）只存在于
  代码里，永不落盘；用户的同名保存一律拒绝，必须改名。

安全边界（不可协商）：
- schema 里**没有** gate / auto_accept / permission 字段。模板只能声明
  ``needs``（read / write / exec）与工具白名单，运行级授权由 ``GrantedGate``
  与会话权限档位取严，模板数据永远不能关闭或降低权限门；
- ``run_command`` 的工具条目必须写成 ``run_command:<命令前缀>``，且前缀经
  ``normalize_command_prefix`` 复用 ``PermissionGate.rule_for`` 与同一套
  shell 拼接 / 代码执行旗标 / 解释器守卫提炼——本模块不自造宽松匹配；
- roundtable / adversarial 本期仍是纯文本协作（不挂工具），因此这两类的
  roles 只允许 ``needs=read`` 且 tools 为空；team 才允许 write / exec。

存储姿态沿 ``core/team_store.py``：路径由宿主注入、坏文件按空册、逐条校验、
``textio.write_text_atomic`` 原子写、name 唯一键（同名覆盖）。模块本身不触碰
真实 ``~/.skysheep``，单测传 tmp_path 即可。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..security.gate import (
    PermissionGate,
    _interpreter_invocation,
    _prefix_deletes_or_runs_arbitrary,
)
from ..textio import read_text_file, write_text_atomic
from .collab_budget import BudgetExceeded, RunBudget, TeamCfgView

# ---- 模块常量 ----

COLLAB_TEMPLATE_NAME_MAX = 60
COLLAB_KINDS = ("roundtable", "team", "adversarial")
COLLAB_NEEDS = ("read", "write", "exec")
NEEDS_ORDER = {"read": 0, "write": 1, "exec": 2}
ROLE_PROMPT_MAX = 4000
ROLE_COUNT_MAX = 12
DESC_MAX = 500
TOOL_NAME_MAX = 80
COMMAND_PREFIX_MAX = 200
BUDGET_CALLS_MAX = 500
BUDGET_SECONDS_MAX = 86400
PREVIEW_INPUT_FLOOR_TOKENS = 400
PREVIEW_OUTPUT_TOKENS = 800
BUILTIN_NAMES = ("圆桌·多模型评审", "团队·交付小组", "对抗·代码审查")

# 名称展示用（错误文案里区分两类纯文本协作）。
_KIND_LABELS = {"roundtable": "圆桌", "adversarial": "对抗", "team": "团队"}


class CollabTemplateError(Exception):
    """协作模板参数非法（backend 原样回给 WS 调用方的中文错误文案）。"""


# ---- 模型（默认值即「不覆盖」）----


class CollabRole(BaseModel):
    """模板里的一个协作角色。

    ``key`` 只用于模板内定位（表单行 / 唯一性校验），运行时不写进
    ``MemberSpec.role``——避免与圆桌内置身份提示词（MEMBER_ROLES）同名导致
    身份提示词被重复拼一遍。运行时展示名走 ``name``，人设走 ``prompt``。
    """

    key: str = ""
    name: str = ""
    provider: str = ""  # 空 = 自动挑选（按角色顺序从已配置服务里补位）
    model: str = ""
    prompt: str = ""
    needs: str = "read"  # read | write | exec
    # 工具条目：普通工具名，或 "run_command:<命令前缀>"（裸 run_command 被拒）
    tools_allow: list[str] = Field(default_factory=list)
    tools_deny: list[str] = Field(default_factory=list)


class CollabAggregator(BaseModel):
    """收口角色：圆桌融合主席 / 对抗终审裁判 / 团队总管。

    全空 = 用当前会话主模型（团队则退回用户总管）。``prompt`` 非空时追加在
    收口角色的系统提示词之后，不替换既有身份提示词。
    """

    provider: str = ""
    model: str = ""
    prompt: str = ""


class CollabLimits(BaseModel):
    """各协作形态的细分限额；0 = 沿用 ``config.toml`` 对应配置。

    团队忽略本组字段（团队模板不持久化 limits）：团队的上限只由 ``[team]``
    配置与 ``budget.max_calls`` 映射出的全局轮次决定。
    """

    max_members: int = 0
    member_timeout_s: int = 0
    debate_rounds: int = 0
    max_findings: int = 0
    role_timeout_s: int = 0


class CollabBudget(BaseModel):
    """运行级预算；0 = 不限。"""

    max_calls: int = 0  # provider 调用次数（含重试与格式纠错）
    max_tokens: int = 0  # 累计 token（团队映射到运行级 token 累计）
    max_seconds: int = 0  # 墙钟秒


class CollabTemplate(BaseModel):
    """一张协作模板。

    注意 ``builtin`` 只是展示/删除保护标记：落盘的用户模板在 load / upsert
    时一律强制 False，手改 JSON 标成 true 不会获得内置身份。
    """

    name: str
    kind: str = "team"
    desc: str = ""
    roles: list[CollabRole] = Field(default_factory=list)
    aggregator: CollabAggregator = Field(default_factory=CollabAggregator)
    limits: CollabLimits = Field(default_factory=CollabLimits)
    budget: CollabBudget = Field(default_factory=CollabBudget)
    created_at: float = 0.0
    builtin: bool = False


# ---- 名称校验 ----


def validate_collab_template_name(name: str) -> str:
    """校验协作模板名，返回去首尾空白后的名字；不合法抛 CollabTemplateError。

    与团队模板名同一形状约束：非空、限长、不含路径分隔符与冒号（Windows 盘符 /
    NTFS 数据流分隔符）、不是 ``.``/``..`` 一类目录引用。模板住在单个 JSON
    文件里不落目录，但名字是唯一键与展示名，挡住能被误读成路径的输入。
    """
    raw = (name or "").strip()
    if not raw:
        raise CollabTemplateError("协作模板名不能为空")
    if len(raw) > COLLAB_TEMPLATE_NAME_MAX:
        raise CollabTemplateError(
            f"协作模板名过长（最多 {COLLAB_TEMPLATE_NAME_MAX} 字符）：{raw[:80]}…"
        )
    if "/" in raw or "\\" in raw:
        raise CollabTemplateError(f"协作模板名不能包含路径分隔符（/ 或 \\）：{raw}")
    if ":" in raw:
        raise CollabTemplateError(f"协作模板名不能包含冒号（盘符/数据流）：{raw}")
    if raw.strip(". ") == "":
        raise CollabTemplateError(f"协作模板名不能是目录引用：{raw}")
    return raw


def _reject_builtin_name(name: str) -> None:
    if name in BUILTIN_NAMES:
        raise CollabTemplateError(
            f"「{name}」是内置模板名，不能覆盖；请改名后另存（例如「{name} · 我的版本」）"
        )


# ---- 命令前缀校验（复用权限门的规则提炼）----


class _SyntheticRunCommand:
    """给 ``PermissionGate.rule_for`` 用的最小 run_command 工具桩。

    只提供 rule_for 实际读取的两个成员（``name`` 与 ``arg_text``）；不提
    工具实例是为了让校验器可在无 registry / 无会话的纯函数上下文里复用。
    """

    name = "run_command"
    write_path_arg = False

    def arg_text(self, data: dict) -> str:
        return str((data or {}).get("command", ""))


def normalize_command_prefix(prefix: str) -> str:
    """把用户写的命令前缀收敛成权限门认可的安全前缀；不安全直接拒。

    复用 ``PermissionGate.rule_for`` 的提炼口径（它内部已含 shell 拼接、
    解释器代码旗标、任意执行入口、提权形状等全部守卫），要求提炼结果与用户
    所写**逐字一致**且是 ``prefix`` 类：写宽了（``git``）、写窄了
    （``git status --short`` 会被提炼成 ``git status``）都拒，让模板里的
    前缀就是运行时真正比较的那一条。

    另外两道针对「合法但等于任意代码」的拒绝：
    - 单词前缀（``pytest`` / ``python``）等于放行该程序的任意参数；
    - 解释器 / shell 调用（``bash setup.sh`` / ``node build.js``）等于放行
      任意脚本内容，脚本文件本身不在模板里，无法审计；
    - 删除 / 任意子命令执行族（``rm -rf`` / ``del /f`` / ``find .`` /
      ``tar -xf`` / ``npm run``）：两词前缀等于整族放行，且会让 delete_file
      的「DANGEROUS 永不自动放行」被等价命令绕过（审查 finding 3）。
    """
    raw = (prefix or "").strip()
    if not raw:
        raise CollabTemplateError("命令前缀不能为空")
    if len(raw) > COMMAND_PREFIX_MAX:
        raise CollabTemplateError(
            f"命令前缀过长（最多 {COMMAND_PREFIX_MAX} 字符）：{raw[:80]}…"
        )
    rule = PermissionGate.rule_for(
        _SyntheticRunCommand(), {"action": "run", "command": raw}, None,
    )
    if rule.kind != "prefix" or rule.pattern != raw:
        raise CollabTemplateError(
            f"命令前缀「{raw}」不能作为安全前缀（含管道/拼接、解释器代码旗标或高危入口命令）；"
            "请写更具体的安全前缀，例如 git status"
        )
    if len(rule.pattern.split()) < 2:
        raise CollabTemplateError(
            f"命令前缀「{raw}」只有一个词，等于放行该程序的任意参数；"
            "请写到子命令，例如 git status"
        )
    if _interpreter_invocation(rule.pattern):
        raise CollabTemplateError(
            f"命令前缀「{raw}」是解释器/shell 调用，等于放行任意脚本"
            "（python/bash/powershell/node…）；请改为具体脚本文件或不授权执行"
        )
    if _prefix_deletes_or_runs_arbitrary(rule.pattern):
        raise CollabTemplateError(
            f"命令前缀「{raw}」属于删除或任意子命令执行类命令，"
            "不能作为模板的运行级免确认授权；删除/执行请改为逐次确认"
        )
    return rule.pattern


# ---- 工具条目校验 ----


def _normalize_tool_entry(entry: object, *, allow_bare_run_command: bool) -> str:
    """校验一条工具条目，返回规范形态。

    ``allow_bare_run_command`` 只给 tools_deny 用：拒绝清单里写裸
    ``run_command`` 是「禁止所有命令」，语义安全；允许清单里写裸
    ``run_command`` 则是整工具放行，必须拒绝。
    """
    raw = str(entry or "").strip()
    if not raw:
        raise CollabTemplateError("工具条目不能为空")
    if len(raw) > TOOL_NAME_MAX:
        raise CollabTemplateError(
            f"工具条目过长（最多 {TOOL_NAME_MAX} 字符）：{raw[:80]}…"
        )
    if "," in raw:
        raise CollabTemplateError(f"工具条目不能包含英文逗号：{raw}")
    if raw == "run_command":
        if allow_bare_run_command:
            return raw
        raise CollabTemplateError(
            "run_command 必须写成 run_command:<命令前缀>，不能整工具放行"
        )
    if raw.startswith("run_command:"):
        normalized = normalize_command_prefix(raw.split(":", 1)[1])
        return f"run_command:{normalized}"
    if ":" in raw:
        # 只有 run_command 允许冒号前缀；其他工具名必须精确（审查 finding 11）。
        # 否则 write_file:任意后缀 会被后端按 split(':',1)[0] 截断成 write_file，
        # 声明与校验口径不一致，deny 也会因带后缀而变成空操作。
        raise CollabTemplateError(
            f"工具条目必须是精确工具名，只有 run_command 可写冒号前缀：{raw}"
        )
    return raw


def _normalize_tool_list(
    entries: list[str], *, role_name: str, allow_bare_run_command: bool,
) -> list[str]:
    result: list[str] = []
    for entry in entries or []:
        try:
            value = _normalize_tool_entry(entry, allow_bare_run_command=allow_bare_run_command)
        except CollabTemplateError as exc:
            raise CollabTemplateError(f"角色「{role_name}」：{exc}") from exc
        if value not in result:
            result.append(value)
    return result


# ---- 模板校验 ----


def _check_int_range(value: int, label: str, *, maximum: int | None = None) -> None:
    if value < 0:
        raise CollabTemplateError(f"{label}不能为负（当前 {value}）")
    if maximum is not None and value > maximum:
        raise CollabTemplateError(f"{label}不能超过 {maximum}（当前 {value}）")


def validate_collab_template(
    template: CollabTemplate, *, allow_builtin: bool = False,
) -> CollabTemplate:
    """校验并就地规范化一张模板；不合法抛中文 CollabTemplateError。

    ``allow_builtin=True`` 只用于内置模板定义（角色可为 0 = 走现有自动解析；
    内置名不触发保留名拒绝）。用户保存 / 从磁盘加载一律 False。
    """
    name = validate_collab_template_name(template.name)
    if not allow_builtin:
        _reject_builtin_name(name)
    template.name = name
    template.kind = (template.kind or "").strip()
    if template.kind not in COLLAB_KINDS:
        raise CollabTemplateError(
            f"模板类型只能是 {' / '.join(COLLAB_KINDS)}，收到：{template.kind or '空'}"
        )
    template.desc = (template.desc or "").strip()
    if len(template.desc) > DESC_MAX:
        raise CollabTemplateError(
            f"模板说明过长（最多 {DESC_MAX} 字符，当前 {len(template.desc)}）"
        )

    roles = template.roles or []
    if not roles and not allow_builtin:
        raise CollabTemplateError("协作模板至少需要一个角色")
    if len(roles) > ROLE_COUNT_MAX:
        raise CollabTemplateError(
            f"协作模板最多 {ROLE_COUNT_MAX} 个角色（当前 {len(roles)}）"
        )

    seen_keys: set[str] = set()
    seen_pairs: dict[tuple[str, str], str] = {}
    for i, role in enumerate(roles):
        role.name = (role.name or "").strip()
        if not role.name:
            raise CollabTemplateError(f"模板里第 {i + 1} 个角色缺少名称")
        role.key = (role.key or "").strip()
        if not role.key:
            raise CollabTemplateError(f"角色「{role.name}」缺少 key（模板内唯一键）")
        if role.key in seen_keys:
            raise CollabTemplateError(f"角色 key 重复：{role.key}")
        seen_keys.add(role.key)

        role.provider = (role.provider or "").strip()
        role.model = (role.model or "").strip()
        if len(role.prompt or "") > ROLE_PROMPT_MAX:
            raise CollabTemplateError(
                f"角色「{role.name}」的提示词过长（最多 {ROLE_PROMPT_MAX} 字符）"
            )

        role.needs = (role.needs or "").strip()
        if role.needs not in COLLAB_NEEDS:
            raise CollabTemplateError(
                f"角色「{role.name}」的 needs 只能是 read / write / exec，"
                f"收到：{role.needs or '空'}"
            )

        role.tools_allow = _normalize_tool_list(
            role.tools_allow, role_name=role.name, allow_bare_run_command=False,
        )
        role.tools_deny = _normalize_tool_list(
            role.tools_deny, role_name=role.name, allow_bare_run_command=True,
        )

        if template.kind in ("roundtable", "adversarial"):
            label = _KIND_LABELS[template.kind]
            if role.needs != "read":
                raise CollabTemplateError(
                    f"{label}模板的角色「{role.name}」只能是只读（needs=read）："
                    f"本期{label}仍是纯文本协作，不挂工具"
                )
            if role.tools_allow or role.tools_deny:
                raise CollabTemplateError(
                    f"{label}模板的角色「{role.name}」不能声明工具"
                    "（tools_allow / tools_deny 必须为空）："
                    f"本期{label}仍是纯文本协作"
                )

        if template.kind == "team" and role.provider and role.model:
            pair = (role.provider, role.model)
            if pair in seen_pairs:
                raise CollabTemplateError(
                    f"团队模板里的角色「{seen_pairs[pair]}」和「{role.name}」"
                    f"使用同一个服务与模型（{role.provider}/{role.model}），"
                    "请换成不同模型或删除一个"
                )
            seen_pairs[pair] = role.name

    template.aggregator.provider = (template.aggregator.provider or "").strip()
    template.aggregator.model = (template.aggregator.model or "").strip()
    if len(template.aggregator.prompt or "") > ROLE_PROMPT_MAX:
        raise CollabTemplateError(
            f"收口角色的提示词过长（最多 {ROLE_PROMPT_MAX} 字符）"
        )

    _check_int_range(template.limits.max_members, "限额 max_members",
                     maximum=ROLE_COUNT_MAX)
    _check_int_range(template.limits.member_timeout_s, "限额 member_timeout_s",
                     maximum=BUDGET_SECONDS_MAX)
    _check_int_range(template.limits.debate_rounds, "限额 debate_rounds", maximum=2)
    _check_int_range(template.limits.max_findings, "限额 max_findings", maximum=200)
    _check_int_range(template.limits.role_timeout_s, "限额 role_timeout_s",
                     maximum=BUDGET_SECONDS_MAX)

    _check_int_range(template.budget.max_calls, "预算 max_calls",
                     maximum=BUDGET_CALLS_MAX)
    _check_int_range(template.budget.max_tokens, "预算 max_tokens")
    _check_int_range(template.budget.max_seconds, "预算 max_seconds",
                     maximum=BUDGET_SECONDS_MAX)
    return template


# ---- 派生信息（backend / 预览共用，避免两处口径漂移）----


def _role_value(role: CollabRole | dict, field: str, default=""):
    """同时接受 CollabRole 与 model_dump() 后的 dict（backend 传的是后者）。"""
    if isinstance(role, dict):
        value = role.get(field, default)
    else:
        value = getattr(role, field, default)
    return default if value is None else value


def derive_needs(roles: list[CollabRole | dict]) -> str:
    """按角色 needs 取最高档（read < write < exec）；空角色表回 read。"""
    best = "read"
    for role in roles or []:
        need = str(_role_value(role, "needs", "read")).strip()
        if NEEDS_ORDER.get(need, 0) > NEEDS_ORDER[best]:
            best = need
    return best


def collect_command_prefixes(roles: list[CollabRole | dict]) -> list[str]:
    """收集全部 ``run_command:<前缀>`` 条目里的前缀（去重、保序）。

    必须先过 ``effective_tool_entries``：tools_deny 优先，被角色显式拒绝的
    前缀不得进入 GrantedGate 的放行面（审查 finding 2——否则预览里看不到、
    运行时却免确认）。
    """
    prefixes: list[str] = []
    for entry in effective_tool_entries(roles):
        if entry.startswith("run_command:"):
            prefix = entry.split(":", 1)[1]
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


def effective_tool_entries(roles: list[CollabRole | dict]) -> list[str]:
    """合并各角色 tools_allow，再按 tools_deny 剔除（deny 优先）。

    本期团队是**团队级工具并集**、不做逐成员隔离：这个函数的用途是给运行时
    注册表裁剪与预览提供同一份可见工具清单，不是安全边界。deny 语义：
    - 裸工具名（``delete_file`` / ``run_command``）整工具剔除；
    - ``run_command:<前缀>`` 只剔除同一条前缀条目。
    """
    denied_bare = {
        entry.split(":", 1)[0]
        for role in roles or []
        for entry in _role_value(role, "tools_deny", []) or []
        if not entry.startswith("run_command:")
    }
    denied_prefix = {
        entry
        for role in roles or []
        for entry in _role_value(role, "tools_deny", []) or []
        if entry.startswith("run_command:")
    }
    result: list[str] = []
    for role in roles or []:
        for entry in _role_value(role, "tools_allow", []) or []:
            base = entry.split(":", 1)[0]
            if entry in denied_prefix or base in denied_bare:
                continue
            if entry not in result:
                result.append(entry)
    return result


# ---- 内置模板 ----


def _builtin_specs() -> list[CollabTemplate]:
    return [
        CollabTemplate(
            name="圆桌·多模型评审",
            kind="roundtable",
            desc="三个只读模型各自独立作答，再由当前主模型融合成一份最终答案。",
            roles=[
                CollabRole(
                    key="critic", name="批评者",
                    prompt="你负责挑错：重点审查常见错误、边界情况与隐患，"
                           "敢于指出站不住脚的观点，宁可苛刻也不要客气。",
                ),
                CollabRole(
                    key="factcheck", name="事实核查员",
                    prompt="你负责核对事实、数据与引用是否准确；没有把握的内容"
                           "明确标注「不确定」，不要为了完整而编造。",
                ),
                CollabRole(
                    key="practitioner", name="实干者",
                    prompt="你负责给出可落地的步骤、命令与示例，"
                           "以「照着做就能完成」为标准，而不是原理铺陈。",
                ),
            ],
            limits=CollabLimits(debate_rounds=1),
            budget=CollabBudget(max_calls=8, max_seconds=900),
            builtin=True,
        ),
        CollabTemplate(
            name="团队·交付小组",
            kind="team",
            desc="调研 → 实现 → 验收的三人交付小组；写入限工作目录内，删除仍需逐次确认。",
            roles=[
                CollabRole(
                    key="researcher", name="调研员",
                    prompt="你负责调研：先读现有实现，列出事实与约束，不改文件。",
                    needs="read",
                    tools_allow=["read_file", "list_dir", "glob", "grep"],
                ),
                CollabRole(
                    key="builder", name="实施者",
                    prompt="你负责实现：按调研结论改文件，完成后说明改了什么。",
                    needs="write",
                    tools_allow=["read_file", "write_file", "edit_file", "make_dir"],
                    tools_deny=["delete_file"],
                ),
                CollabRole(
                    key="reviewer", name="验收员",
                    prompt="你负责验收：逐项核对交付标准，只报告事实。",
                    needs="read",
                    tools_allow=["read_file", "list_dir", "grep"],
                ),
            ],
            budget=CollabBudget(max_calls=40, max_seconds=3600),
            builtin=True,
        ),
        CollabTemplate(
            name="对抗·代码审查",
            kind="adversarial",
            desc="发现 → 调查 → 建议 → 裁判的对抗式代码审查，终审报告直接进对话。",
            roles=[
                CollabRole(key="finder", name="发现者"),
                CollabRole(key="investigator", name="调查员"),
                CollabRole(key="advisor", name="建议者"),
            ],
            budget=CollabBudget(max_calls=6, max_seconds=1200),
            builtin=True,
        ),
    ]


_BUILTIN_TEMPLATES: list[CollabTemplate] = [
    validate_collab_template(t, allow_builtin=True) for t in _builtin_specs()
]


def builtin_templates() -> list[CollabTemplate]:
    """返回三个内置模板的深拷贝（调用方随便改，不污染模块常量）。"""
    return [t.model_copy(deep=True) for t in _BUILTIN_TEMPLATES]


def builtin_template(name: str) -> CollabTemplate | None:
    """按名取内置模板的深拷贝；不是内置名返回 None。"""
    for template in _BUILTIN_TEMPLATES:
        if template.name == name:
            return template.model_copy(deep=True)
    return None


def is_builtin_template_name(name: str) -> bool:
    return (name or "").strip() in BUILTIN_NAMES


# ---- 存储 ----


class CollabTemplateStore:
    """``~/.skysheep/collab_templates.json`` 的读写与内存态。

    文件形状 ``{"templates": [CollabTemplate…]}``；只存用户自建的
    roundtable / adversarial 模板（kind=team 走 teams.json）。坏文件（不存在 /
    非 JSON / 条目形状不对 / 手改出非法值）不致命：回空册或跳过坏条目并累计到
    ``skipped``，下次保存整体覆盖。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.templates: list[CollabTemplate] = []
        # load 期被跳过的坏条目数（backend 的 template_list 原样回给前端）
        self.skipped = 0

    # ---- 读写 ----

    def load(self) -> None:
        self.templates = []
        self.skipped = 0
        if not self.path.exists():
            return
        try:
            # 走 textio 的编码探测：手改文件存成 GBK 等非 UTF-8 时也能读，
            # 不再把 UnicodeDecodeError 抛到 backend.setup() 导致启动失败
            # （审查 finding 13；textio 探测不出时也不抛，交给 JSON 解析兜底）。
            data = json.loads(read_text_file(self.path).text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return  # 坏文件不致命：回空册，下次保存覆盖
        items = data.get("templates") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        positions: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                self.skipped += 1
                continue
            try:
                template = CollabTemplate(**item)
                # 磁盘内容永远不获得内置身份；团队模板不属于本册
                template.builtin = False
                if template.kind == "team":
                    raise CollabTemplateError("团队模板不属于协作模板册")
                validate_collab_template(template, allow_builtin=False)
            except (ValidationError, CollabTemplateError):
                self.skipped += 1
                continue
            # 手改出同名条目时后写的生效（位置保持在首次出现处）
            if template.name in positions:
                self.templates[positions[template.name]] = template
            else:
                positions[template.name] = len(self.templates)
                self.templates.append(template)

    def save(self) -> None:
        data = {"templates": [t.model_dump() for t in self.templates]}
        write_text_atomic(
            self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )

    # ---- 查询 ----

    def list(self) -> list[dict]:
        """内置三张（在前）+ 用户模板；每条都是独立 dict，调用方可随意改。"""
        return [t.model_dump() for t in builtin_templates()] + [
            t.model_dump() for t in self.templates
        ]

    def get(self, name: str) -> CollabTemplate | None:
        """先查内置、再查用户；返回深拷贝，避免调用方改到模板册内部状态。"""
        raw = (name or "").strip()
        found = builtin_template(raw)
        if found is not None:
            return found
        for template in self.templates:
            if template.name == raw:
                return template.model_copy(deep=True)
        return None

    # ---- 增删 ----

    def upsert(self, template: CollabTemplate) -> None:
        """保存模板：同名覆盖原条目（位置不变），新名追加。

        内置名与 kind=team 一律拒绝；``builtin`` 强制 False；``created_at``
        未填（<=0）时由本方法打上当前时刻。
        """
        if (template.kind or "").strip() == "team":
            raise CollabTemplateError(
                "团队模板请保存到团队模板册（teams.json），不能写进协作模板册"
            )
        validate_collab_template(template, allow_builtin=False)
        template.builtin = False
        if template.created_at <= 0:
            template.created_at = time.time()
        for i, old in enumerate(self.templates):
            if old.name == template.name:
                self.templates[i] = template
                self.save()
                return
        self.templates.append(template)
        self.save()

    def remove(self, name: str) -> None:
        """按名删除用户模板；内置与不存在的名字都抛中文错误（不静默）。"""
        raw = (name or "").strip()
        if is_builtin_template_name(raw):
            raise CollabTemplateError(f"内置模板不能删除：{raw}")
        before = len(self.templates)
        self.templates = [t for t in self.templates if t.name != raw]
        if len(self.templates) == before:
            raise CollabTemplateError(f"找不到协作模板: {raw}")
        self.save()


__all__ = [
    "BUILTIN_NAMES",
    "BUDGET_CALLS_MAX",
    "BUDGET_SECONDS_MAX",
    "BudgetExceeded",
    "COLLAB_KINDS",
    "COLLAB_NEEDS",
    "COLLAB_TEMPLATE_NAME_MAX",
    "COMMAND_PREFIX_MAX",
    "CollabAggregator",
    "CollabBudget",
    "CollabLimits",
    "CollabRole",
    "CollabTemplate",
    "CollabTemplateError",
    "CollabTemplateStore",
    "DESC_MAX",
    "NEEDS_ORDER",
    "PREVIEW_INPUT_FLOOR_TOKENS",
    "PREVIEW_OUTPUT_TOKENS",
    "ROLE_COUNT_MAX",
    "ROLE_PROMPT_MAX",
    "RunBudget",
    "TOOL_NAME_MAX",
    "TeamCfgView",
    "builtin_template",
    "builtin_templates",
    "collect_command_prefixes",
    "derive_needs",
    "effective_tool_entries",
    "is_builtin_template_name",
    "normalize_command_prefix",
    "validate_collab_template",
    "validate_collab_template_name",
]
