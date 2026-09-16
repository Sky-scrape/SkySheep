"""子代理定义的持久化：~/.skysheep/subagents.json。

设置页里的「子代理」管理的就是这份文件：
- builtin：两个内置子代理（general-purpose=task / Explore=explore）的覆盖项
  （用哪个模型、思考强度），留空表示跟随主对话当前设置；
- custom：用户自建的子代理——名称、描述、专项提示词、工具范围（"all" /
  "readonly" / 工具名列表）、使用的模型（provider+model，留空跟随主对话）。

安全边界不变：无论工具给多少，子代理内一切需要确认的操作都会被 SubagentGate
自动拒绝（子代理没有确认通道），spawn_agent/check_task 也永远不进子代理工具集。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ..config import REASONING_EFFORTS

SUBAGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
BUILTIN_AGENT_TYPES = ("task", "explore")
BUILTIN_DISPLAY = {"task": "general-purpose", "explore": "Explore"}
TOOL_POLICY_ALL = "all"
TOOL_POLICY_READONLY = "readonly"


class SubagentDefError(Exception):
    pass


class SubagentDef(BaseModel):
    name: str
    description: str = ""
    prompt: str = ""
    # "all"=主 Agent 的全部工具（除派生工具）；"readonly"=只读；
    # 或工具名列表（自定义勾选）
    tools: str | list[str] = TOOL_POLICY_READONLY
    provider: str = ""   # 留空 = 跟随主对话当前模型
    model: str = ""
    enabled: bool = True


class BuiltinOverride(BaseModel):
    provider: str = ""
    model: str = ""
    reasoning: str = ""  # 留空 = 跟随全局；auto/low/medium/high


def validate_subagent_name(name: str, *, allow_builtin: bool = False) -> str:
    name = (name or "").strip()
    if not SUBAGENT_NAME_RE.match(name):
        raise SubagentDefError(
            "子代理名称只能用字母、数字、下划线、短横线（需以字母或数字开头）: "
            + (name or "(空)")
        )
    if not allow_builtin and name in BUILTIN_AGENT_TYPES:
        raise SubagentDefError(f"「{name}」是内置子代理名，请换一个")
    return name


class SubagentStore:
    """~/.skysheep/subagents.json 的读写与内存态。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.builtin: dict[str, BuiltinOverride] = {
            t: BuiltinOverride() for t in BUILTIN_AGENT_TYPES
        }
        self.custom: list[SubagentDef] = []

    # ---- 读写 ----

    def load(self) -> None:
        self.builtin = {t: BuiltinOverride() for t in BUILTIN_AGENT_TYPES}
        self.custom = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # 坏文件不致命：回到默认，下次保存覆盖
        for t in BUILTIN_AGENT_TYPES:
            section = (data.get("builtin") or {}).get(t)
            if isinstance(section, dict):
                try:
                    self.builtin[t] = BuiltinOverride(**section)
                except ValidationError:
                    pass
        for item in data.get("custom") or []:
            if isinstance(item, dict):
                try:
                    self.custom.append(SubagentDef(**item))
                except ValidationError:
                    continue

    def save(self) -> None:
        data = {
            "builtin": {t: ov.model_dump() for t, ov in self.builtin.items()},
            "custom": [d.model_dump() for d in self.custom],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # ---- 内置覆盖 ----

    def set_override(
        self, agent_type: str, *, provider: str, model: str, reasoning: str
    ) -> BuiltinOverride:
        if agent_type not in BUILTIN_AGENT_TYPES:
            raise SubagentDefError("未知的内置子代理: " + str(agent_type))
        reasoning = (reasoning or "").strip().lower()
        if reasoning and reasoning not in REASONING_EFFORTS:
            raise SubagentDefError("思考强度只支持 留空 / " + " / ".join(REASONING_EFFORTS))
        ov = BuiltinOverride(
            provider=(provider or "").strip(),
            model=(model or "").strip(),
            reasoning=reasoning,
        )
        self.builtin[agent_type] = ov
        self.save()
        return ov

    # ---- 自定义子代理 ----

    def get_custom(self, name: str, *, enabled_only: bool = False) -> SubagentDef | None:
        for d in self.custom:
            if d.name == name and (d.enabled or not enabled_only):
                return d
        return None

    def upsert_custom(self, d: SubagentDef) -> None:
        for i, old in enumerate(self.custom):
            if old.name == d.name:
                self.custom[i] = d
                self.save()
                return
        self.custom.append(d)
        self.save()

    def remove_custom(self, name: str) -> bool:
        before = len(self.custom)
        self.custom = [d for d in self.custom if d.name != name]
        if len(self.custom) != before:
            self.save()
            return True
        raise SubagentDefError("找不到子代理: " + name)
