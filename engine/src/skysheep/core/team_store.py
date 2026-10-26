"""团队模板的模型、名称校验与持久化。

路径由宿主注入，坏文件按空模板册处理，写入经 textio 原子替换；模块本身
不访问真实用户目录，单测可使用临时路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..textio import write_text_atomic


class TeamError(Exception):
    """团队参数或状态非法（backend 原样回给 WS 调用方的错误文案）。"""


# ---- 团队模板（三期）：~/.skysheep/teams.json 的读写助手 ----
# 与子代理定义（subagent_store.SubagentStore，~/.skysheep/subagents.json）同姿态：
# 路径构造注入（backend 用 skysheep_home() / "teams.json" 装配）、坏文件容错、
# textio 原子写；模块层不触碰真实 ~/.skysheep，单测传 tmp_path。
# name 是唯一键，同名保存 = 覆盖（前端「另存为模板」对已有名直接更新）。

TEAM_TEMPLATE_NAME_MAX = 60  # 模板名长度上限（与 skills 的限长校验同一精神）


class TeamTemplateMember(BaseModel):
    """模板里的队员行：(provider, model) + 成员名 + 一句话人设。

    只存标识不存 Provider 实例——应用模板时宿主按 (provider, model) 走
    既有的成员解析路径（缺 Key 等构建失败照常降级 provider=None 入册）。
    """

    provider: str = ""
    model: str = ""
    name: str = ""
    persona: str = ""


class TeamTemplateDirector(BaseModel):
    """模板里的总管行；provider/model 均空 = 用户总管（director_mode=user 的常态）。"""

    provider: str = ""
    model: str = ""


class TeamTemplate(BaseModel):
    """团队模板（三期）：把一次建队配置存成可复用的具名条目。

    name 是唯一键；members 有序（应用时按此顺序走 create_team 的同名补序、
    保留字回退与 max_members 截断，模板本身不做成员数校验——上限由应用时
    的既有路径兜住）。
    """

    name: str
    director_mode: str = "user"  # user | ai
    director: TeamTemplateDirector = Field(default_factory=TeamTemplateDirector)
    members: list[TeamTemplateMember] = Field(default_factory=list)
    created_at: float = 0.0  # 模板创建/覆盖时刻（epoch 秒；宿主保存时填）


def validate_team_template_name(name: str) -> str:
    """校验模板名，返回规范后的名字（去首尾空白）；不合法抛 TeamError。

    仿 skills 的 _validate_skill_name 精神：非空、限长、不含路径分隔符与
    冒号（Windows 盘符 / NTFS 数据流分隔符）、不是 `.`/`..` 一类目录引用。
    模板虽住在单个 JSON 文件里不落目录，但名字直接当唯一键与文件语境的
    展示名，形状上挡住一切能被误读成路径的输入。
    """
    raw = (name or "").strip()
    if not raw:
        raise TeamError("团队模板名不能为空")
    if len(raw) > TEAM_TEMPLATE_NAME_MAX:
        raise TeamError(
            f"团队模板名过长（最多 {TEAM_TEMPLATE_NAME_MAX} 字符）：{raw[:80]}…"
        )
    if "/" in raw or "\\" in raw:
        raise TeamError(f"团队模板名不能包含路径分隔符（/ 或 \\）：{raw}")
    if ":" in raw:
        raise TeamError(f"团队模板名不能包含冒号（盘符/数据流）：{raw}")
    if raw.strip(". ") == "":
        # `.`、`..`、纯点纯空格：目录引用或无意义名
        raise TeamError(f"团队模板名不能是目录引用：{raw}")
    return raw


class TeamTemplateStore:
    """~/.skysheep/teams.json 的读写与内存态（三期：建队模板）。

    文件结构 {"templates": [TeamTemplate…]}；坏文件（不存在/非 JSON/条目
    形状不对）不致命：回空列表或跳过坏条目，下次保存整体覆盖（沿
    subagent_store 的容错姿态）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.templates: list[TeamTemplate] = []

    # ---- 读写 ----

    def load(self) -> None:
        self.templates = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # 坏文件不致命：回空列表，下次保存覆盖
        items = data.get("templates") if isinstance(data, dict) else None
        for item in items or []:
            if isinstance(item, dict):
                try:
                    self.templates.append(TeamTemplate(**item))
                except ValidationError:
                    continue  # 单条形状不对：跳过，不拖垮整份模板册

    def save(self) -> None:
        data = {"templates": [t.model_dump() for t in self.templates]}
        # 原子写（沿 subagents.json 同款）：写一半被中断不留半截 JSON
        write_text_atomic(
            self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )

    # ---- 模板增删查（name 唯一键）----

    def list(self) -> list[dict]:
        return [t.model_dump() for t in self.templates]

    def upsert(self, template: TeamTemplate) -> None:
        """保存模板：同名覆盖原条目（位置不变），新名追加。"""
        name = validate_team_template_name(template.name)
        if template.director_mode not in ("user", "ai"):
            raise TeamError(
                f"模板的总管形态只能是 user / ai，收到：{template.director_mode}"
            )
        template.name = name
        for i, old in enumerate(self.templates):
            if old.name == name:
                self.templates[i] = template
                self.save()
                return
        self.templates.append(template)
        self.save()

    def remove(self, name: str) -> None:
        """按名删除模板；不存在时抛 TeamError（与子代理删除同姿态，不静默）。"""
        before = len(self.templates)
        self.templates = [t for t in self.templates if t.name != name]
        if len(self.templates) == before:
            raise TeamError("找不到团队模板: " + name)
        self.save()
