"""团队模板的协作扩展字段（needs / tools / budget）与 teams.json 存储。

从 backend.py 拆出（10k 行巨石）。团队模板仍住 ~/.skysheep/teams.json
（core/team_store.py 的 TeamTemplate），这里用子类承载协作扩展字段，
不改团队内核与基类模板册；后端通过 re-export 继续暴露同名类。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from ...core.collab_templates import CollabTemplate, validate_collab_template
from ...core.team_store import (
    TeamTemplate,
    TeamTemplateStore,
    validate_team_template_name,
)
from ...textio import read_text_file

# ---- 团队模板的协作扩展字段（本期兼容桥）----
# 团队模板仍住 ~/.skysheep/teams.json（core/team_store.py 的 TeamTemplate），
# 本批只给团队模板增加可选的协作字段（needs / tools / budget），不迁库。
# 字段定义放在 backend 侧而不是改 core/team_store.py：本子任务的改动面限定
# 在后端接线，团队内核与模板册由另一路并行落地；用子类承载可选字段既能让
# teams.json 现在就存下这些键（TeamTemplateStore.save 按实例 model_dump），
# 又能在基类将来补上同名字段时自然兼容（子类字段覆盖同义字段，行为不变）。
# 旧文件缺键取默认（read / 空工具 / 预算不限），读取方一律走 getattr 兜底。


class _CollabTeamTemplateBudget(BaseModel):
    """团队模板的运行级预算（0 = 不限；映射口径见 TeamCfgView）。"""

    max_calls: int = 0
    max_tokens: int = 0
    max_seconds: int = 0


class _CollabTeamTemplate(TeamTemplate):
    """TeamTemplate + 协作模板可选字段（只加在模板级，不动成员/总管子字典）。"""

    needs: str = "read"
    # 协作模板的说明（画廊卡副文案）：kind=team 的模板转存 teams.json 时一并
    # 带走，回读时还原；旧文件缺键取空串（读取方 getattr 兜底）。
    desc: str = Field(default="", max_length=2000)
    tools_allow: list[str] = Field(default_factory=list)
    tools_deny: list[str] = Field(default_factory=list)
    budget: _CollabTeamTemplateBudget = Field(
        default_factory=_CollabTeamTemplateBudget
    )


class _CollabTeamTemplateStore(TeamTemplateStore):
    """teams.json 的读写仍走 TeamTemplateStore，只把 load 的解析模型换成
    ``_CollabTeamTemplate``（save/upsert/remove 直接复用基类实现）。

    基类 load 用 TeamTemplate 解析会把协作扩展字段当未知键丢弃，重启后
    needs / tools / budget 静默消失；这里逐字沿用基类的容错姿态（坏文件回空册、
    单条形状不对跳过），只换模型类。
    """

    def load(self) -> None:
        self.templates = []
        if not self.path.exists():
            return
        try:
            # 与 CollabTemplateStore 同口径：手改成 GBK 等非 UTF-8 也不抛
            # UnicodeDecodeError（审查 finding 13）。
            data = json.loads(read_text_file(self.path).text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return  # 坏文件不致命：回空册，下次保存覆盖
        items = data.get("templates") if isinstance(data, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                self.templates.append(_CollabTeamTemplate(**item))
            except ValidationError:
                continue  # 单条形状不对：跳过，不拖垮整份模板册

    def upsert(self, template: TeamTemplate) -> None:
        """保存前补协作扩展字段校验（审查 finding 12）。

        生产写入路径（team_template_save / collab_template_save）已经先跑
        CollabTemplate 校验；这里再兜一道，防止将来新调用点直接把非法
        needs / tools / budget 落进 teams.json。普通 TeamTemplate（没有扩展
        字段）沿用基类口径。
        """
        if isinstance(template, _CollabTeamTemplate):
            # 先按团队模板册口径校验名字（错误类型与旧接口一致），再做
            # 协作扩展字段校验。
            name = validate_team_template_name(template.name)
            budget = template.budget or _CollabTeamTemplateBudget()
            probe = CollabTemplate(
                name=name,
                kind="team",
                roles=[{
                    "key": "team", "name": name,
                    "needs": template.needs,
                    "tools_allow": list(template.tools_allow or []),
                    "tools_deny": list(template.tools_deny or []),
                }],
                budget=budget.model_dump(),
            )
            validate_collab_template(probe, allow_builtin=False)
            role = probe.roles[0]
            template.needs = role.needs
            template.tools_allow = list(role.tools_allow)
            template.tools_deny = list(role.tools_deny)
            template.budget = _CollabTeamTemplateBudget(**probe.budget.model_dump())
        super().upsert(template)
