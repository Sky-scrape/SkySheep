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

「范围」与「启用」是两个正交的轴，各自都有真实用途：

- **范围（scope）**：这个技能属于哪些项目。只对全局技能有意义（项目级技能天生只
  属于一个项目），配置存在全局的 skills-scope.json 里，跨项目统一管理。三种模式：
  `all`（默认）/ `projects`（勾选的项目）/ `none`（任何项目都不用）。
- **启用（enabled）**：在**当前项目**里临时用不用，沿用上面的 skills.json。

技能实际生效（进系统提示词、允许 load_skill）的条件是 `enabled and applies`，
其中 applies 由范围与当前项目决定。

渐进式披露：系统提示词只注入「名称 + description」清单（省 token），
模型需要时用 load_skill 工具读取完整正文。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel

# 范围模式：所有项目 / 仅指定项目 / 任何项目都不用
SCOPE_ALL = "all"
SCOPE_PROJECTS = "projects"
SCOPE_NONE = "none"
SCOPE_MODES = (SCOPE_ALL, SCOPE_PROJECTS, SCOPE_NONE)


class Skill(BaseModel):
    name: str
    description: str = ""
    path: Path  # SKILL.md 路径
    source: str = "global"  # global | project
    enabled: bool = True
    # 使用范围（只对 source == "global" 有意义；项目级技能由 loader 置为 "project"）
    scope: str = SCOPE_ALL
    scope_projects: list[str] = []


def _norm_path(p: str | Path) -> str:
    """路径归一化：Windows 上大小写不敏感，比较前必须统一。"""
    try:
        resolved = Path(p).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(p).expanduser()
    return os.path.normcase(str(resolved))


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
        scope_path: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.global_dir = global_dir
        self.project_dir = project_dir
        self.state_path = state_path
        # 使用范围配置（跨项目共享）：global_dir 的兄弟文件；缺省时范围功能退化为「所有项目」
        self.scope_path = scope_path
        # 当前项目根目录：判定全局技能是否适用。显式传入（不从 project_dir 反推，
        # 否则 project_dir 为空时无法判断）。
        self.project_root = project_root
        self._skills: dict[str, Skill] = {}
        self._scopes: dict[str, dict] = {}

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
        self._apply_scopes()
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

    # ---- 使用范围（只对全局技能有意义） ----

    def _load_scopes(self) -> dict[str, dict]:
        """读 skills-scope.json；坏文件/缺失一律当作「都是所有项目」。"""
        if not self.scope_path or not self.scope_path.exists():
            return {}
        try:
            data = json.loads(self.scope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("scopes") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            mode = str(cfg.get("mode") or SCOPE_ALL)
            if mode not in SCOPE_MODES:
                mode = SCOPE_ALL
            projects = cfg.get("projects")
            paths = [str(p) for p in projects if str(p).strip()] \
                if isinstance(projects, list) else []
            out[str(name)] = {"mode": mode, "projects": paths}
        return out

    def _save_scopes(self) -> None:
        if not self.scope_path:
            return
        self.scope_path.parent.mkdir(parents=True, exist_ok=True)
        self.scope_path.write_text(
            json.dumps({"scopes": self._scopes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _apply_scopes(self) -> None:
        """把持久化的范围套到已发现的技能上。

        项目级技能的范围不由配置决定（天生只属于当前项目），但也要显式
        标记为 "project"，这样界面能区分「仅本项目」与「所有项目」。
        """
        self._scopes = self._load_scopes()
        for name, skill in self._skills.items():
            if skill.source == "project":
                skill.scope = "project"
                skill.scope_projects = []
                continue
            cfg = self._scopes.get(name)
            if cfg:
                skill.scope = cfg["mode"]
                skill.scope_projects = list(cfg["projects"])
            else:
                skill.scope = SCOPE_ALL
                skill.scope_projects = []

    def applies(self, name: str) -> bool:
        """这个技能在当前项目是否适用（只看范围，不看 enabled）。"""
        skill = self._skills.get(name)
        if skill is None:
            return False
        if skill.source == "project":
            return True  # 项目级技能就是在当前项目里被发现的
        if skill.scope == SCOPE_ALL:
            return True
        if skill.scope == SCOPE_NONE:
            return False
        if self.project_root is None:
            return False  # 没给项目根目录时，指定项目范围无法判定 → 保守不适用
        mine = _norm_path(self.project_root)
        return any(_norm_path(p) == mine for p in skill.scope_projects)

    def scope_of(self, name: str) -> dict:
        """给界面用的范围描述。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError("skill not found: " + name)
        return {
            "mode": skill.scope,
            "projects": list(skill.scope_projects),
            "applies": self.applies(name),
        }

    def set_scope(self, name: str, mode: str, projects: list[str] | None = None) -> dict:
        """设置全局技能的使用范围。项目级技能天生只属于一个项目，拒改。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError("skill not found: " + name)
        if skill.source == "project":
            raise RuntimeError(
                f"「{name}」是本项目技能，只在本项目生效，不能改使用范围；"
                "如需跨项目使用，请把它装到全局（导入时选「全局」）"
            )
        if mode not in SCOPE_MODES:
            raise RuntimeError("范围只能是 all / projects / none: " + str(mode))
        # 去重（Windows 大小写不敏感），保留用户原始写法便于界面回显
        cleaned: list[str] = []
        seen: set[str] = set()
        for p in projects or []:
            raw = str(p).strip()
            if not raw:
                continue
            key = _norm_path(raw)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(raw)
        if mode == SCOPE_PROJECTS and not cleaned:
            raise RuntimeError("「指定项目」至少要勾选一个项目")
        skill.scope = mode
        skill.scope_projects = cleaned if mode == SCOPE_PROJECTS else []
        self._scopes[name] = {"mode": skill.scope, "projects": list(skill.scope_projects)}
        self._save_scopes()
        return self.scope_of(name)

    # ---- 读取 ----

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def enabled_skills(self) -> list[Skill]:
        """当前项目实际生效的技能（启用 且 范围适用）。

        这是真正会进系统提示词的那批；界面要展示全部技能时用 all()。
        """
        return [s for s in self._skills.values() if s.enabled and self.applies(s.name)]

    def load_body(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError("skill not found: " + name)
        if not skill.enabled:
            raise KeyError("skill is disabled: " + name)
        if not self.applies(name):
            raise KeyError("skill is out of scope for this project: " + name)
        return self.read_body(name)

    def read_body(self, name: str) -> str:
        """读技能正文（剥掉 frontmatter），不检查启用/范围。

        给界面预览用：停用的技能也要能看内容，否则用户无从判断该不该启用。
        """
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError("skill not found: " + name)
        _, body = _parse_frontmatter(skill.path.read_text(encoding="utf-8", errors="replace"))
        return body

    def raw_text(self, name: str) -> str:
        """读 SKILL.md 原文（含 frontmatter），给预览展示完整文件。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError("skill not found: " + name)
        return skill.path.read_text(encoding="utf-8", errors="replace")

    # ---- 系统提示词注入 ----

    def render_prompt_section(self) -> str:
        skills = self.enabled_skills()
        if not skills:
            return ""
        lines = ["", "# Skills", "可用技能（需要时先用 load_skill 读取完整指令再行动）："]
        for s in skills:
            lines.append("- {}: {}".format(s.name, s.description or "(no description)"))
        return "\n".join(lines) + "\n"
