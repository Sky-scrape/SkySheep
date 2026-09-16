"""Skills 技能包：可分享的提示词包 + 可选资源，Proma / Agent Skills 风格。

目录约定：
    skills/<name>/SKILL.md
        ---
        name: pdf-tools
        description: 合并、拆分、提取 PDF 内容
        ---
        （正文：给模型的操作指令，可引用同目录下的脚本/资源）

两级来源：全局 ~/.skysheep/skills/ + 项目 <root>/.skysheep/skills/；
项目级开关持久化在 <root>/.skysheep/skills.json（{"disabled": [...]}）。

渐进式披露：系统提示词只注入「名称 + description」清单（省 token），
模型需要时用 load_skill 工具读取完整正文。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class Skill(BaseModel):
    name: str
    description: str = ""
    path: Path  # SKILL.md 路径
    source: str = "global"  # global | project
    enabled: bool = True


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md 头部 frontmatter（--- 包围的 key: value 行）。"""
    body = text
    meta: dict[str, str] = {}
    if text.startswith("---"):
        lines = text.splitlines()
        if len(lines) > 1 and lines[0].strip() == "---":
            end = None
            for i, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    end = i
                    break
            if end is not None:
                for line in lines[1:end]:
                    if ":" in line:
                        key, _, value = line.partition(":")
                        meta[key.strip().lower()] = value.strip()
                body = "\n".join(lines[end + 1 :])
    return meta, body.strip()


def _load_skill_from_dir(skill_dir: Path, source: str) -> Skill | None:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or skill_dir.name
    description = meta.get("description")
    if not description and body:
        description = body.splitlines()[0][:120]
    return Skill(name=name, description=description or "", path=md, source=source)


class SkillLoader:
    def __init__(
        self,
        global_dir: Path | None = None,
        project_dir: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.global_dir = global_dir
        self.project_dir = project_dir
        self.state_path = state_path
        self._skills: dict[str, Skill] = {}

    # ---- 发现 ----

    def discover(self) -> list[Skill]:
        self._skills = {}
        for source, base in (("global", self.global_dir), ("project", self.project_dir)):
            if not base or not base.is_dir():
                continue
            for skill_dir in sorted(base.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill = _load_skill_from_dir(skill_dir, source)
                if skill and skill.name not in self._skills:
                    self._skills[skill.name] = skill
        self._apply_state()
        return self.all()

    def _apply_state(self) -> None:
        disabled: set[str] = set()
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                disabled = set(data.get("disabled", []))
            except (OSError, json.JSONDecodeError):
                pass
        for name, skill in self._skills.items():
            skill.enabled = name not in disabled

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self._skills:
            return False
        self._skills[name].enabled = enabled
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            disabled = sorted(n for n, s in self._skills.items() if not s.enabled)
            self.state_path.write_text(
                json.dumps({"disabled": disabled}, ensure_ascii=False), encoding="utf-8"
            )
        return True

    # ---- 读取 ----

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def enabled_skills(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.enabled]

    def load_body(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError("skill not found: " + name)
        if not skill.enabled:
            raise KeyError("skill is disabled: " + name)
        _, body = _parse_frontmatter(skill.path.read_text(encoding="utf-8", errors="replace"))
        return body

    # ---- 系统提示词注入 ----

    def render_prompt_section(self) -> str:
        skills = self.enabled_skills()
        if not skills:
            return ""
        lines = ["", "# Skills", "可用技能（需要时先用 load_skill 读取完整指令再行动）："]
        for s in skills:
            lines.append("- {}: {}".format(s.name, s.description or "(no description)"))
        return "\n".join(lines) + "\n"
