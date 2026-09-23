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
    # 技能版本（frontmatter 的 version 字段，可选）：技能广场拿它和索引版本比，提示可更新
    version: str = ""
    # 安装来源（installer 写进技能目录的 .source.json，可选）：广场条目据此判定「已安装」
    source_url: str = ""


def _norm_path(p: str | Path) -> str:
    """路径归一化：Windows 上大小写不敏感，比较前必须统一。"""
    try:
        resolved = Path(p).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(p).expanduser()
    return os.path.normcase(str(resolved))


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md 头部 frontmatter（--- 包围的 key: value 行）。

    支持值写在行内的普通写法，也支持 YAML 块标量（description: >- / | 之类，
    内容在后续缩进行）——技能广场与第三方包大量使用这种写法，此前会被读成
    字面量 ">-"，描述整段丢失。
    """
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
                fm = lines[1:end]
                i = 0
                while i < len(fm):
                    line = fm[i]
                    if ":" in line:
                        key, _, value = line.partition(":")
                        key = key.strip().lower()
                        value = value.strip()
                        if value in (">", ">-", ">+", "|", "|-", "|+"):
                            # 块标量：取后续缩进行。> 折叠为空格，| 保留换行；
                            # 回到零缩进的非空行即块结束。首尾空白交给 sanitize。
                            block: list[str] = []
                            i += 1
                            while i < len(fm):
                                nxt = fm[i]
                                if not nxt.strip():
                                    block.append("")
                                elif len(nxt) - len(nxt.lstrip()) == 0:
                                    break
                                else:
                                    block.append(nxt.strip())
                                i += 1
                            joiner = " " if value.startswith(">") else "\n"
                            meta[key] = joiner.join(block).strip()
                            continue
                        if key:
                            meta[key] = value
                    i += 1
                body = "\n".join(lines[end + 1 :])
    return meta, body.strip()


# 技能 description 会直接拼进系统提示词的 Skills 清单（见 render_prompt_section）。
# 技能包（尤其从技能广场 / 第三方仓库装的）里的 frontmatter 由外部内容决定，
# 超长描述会挤占上下文，也能用大量空白把注入内容推到看不见的位置——与 MCP 工具
# 描述同一类风险，所以用同一套限长与压空白处理（见 mcp/client._sanitize_description）。
# 正文另有 load_skill 的 20000 字符上限，不受这里影响。
MAX_SKILL_DESCRIPTION_CHARS = 1000

# 技能名同样会拼进系统提示词的 Skills 清单（作为 load_skill 的参数），也一并限长。
MAX_SKILL_NAME_CHARS = 120

# version / 来源标记只进界面展示与广场比对，不进系统提示词，限个合理长度防脏数据即可。
MAX_SKILL_VERSION_CHARS = 32

# 安装来源标记：installer 从网址安装时写进技能目录，记录安装时的广场条目 url。
# 独立小文件而不是改 SKILL.md——技能正文是第三方内容，我们不修改它。
SOURCE_MARKER = ".source.json"


def _sanitize_name(name: str) -> str:
    """技能名归一：去掉换行 / 首尾空白，并限长。

    frontmatter 的 name 是外部内容，换行可以在一行清单里制造额外条目；
    过长的名字也会把同一行的描述挤出视线。名字本身要能作为 load_skill 的参数，
    所以只做形状清理，不改字符。
    """
    raw = " ".join((name or "").split())
    if len(raw) > MAX_SKILL_NAME_CHARS:
        raw = raw[:MAX_SKILL_NAME_CHARS]
    return raw


def _sanitize_description(text: str) -> str:
    """技能描述的展示清理：限长 + 压掉多余空白行。

    只做形状约束（长度、空白），不尝试识别「恶意指令」——那种判断不适合放在按
    长度/格式的过滤里，会既漏又误伤；真正的边界是技能来源是否可信。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    # 连续空行压成一个，避免用大量空行把内容推到看不见的位置
    lines = [ln.rstrip() for ln in raw.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if not ln:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(ln)
    text_out = "\n".join(out).strip()
    if len(text_out) > MAX_SKILL_DESCRIPTION_CHARS:
        text_out = text_out[:MAX_SKILL_DESCRIPTION_CHARS] + " …（描述过长已截断）"
    return text_out


def _read_source_marker(skill_dir: Path) -> str:
    """读技能目录的安装来源标记（.source.json 的 market_url）；没有/坏了返回空串。"""
    try:
        data = json.loads((skill_dir / SOURCE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    url = data.get("market_url") if isinstance(data, dict) else None
    return str(url or "").strip()[:500]


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
    version = " ".join((meta.get("version") or "").split())[:MAX_SKILL_VERSION_CHARS]
    return Skill(
        name=_sanitize_name(name),
        description=_sanitize_description(description or ""),
        path=md, source=source,
        version=version,
        source_url=_read_source_marker(skill_dir),
    )


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
        """发现全局与项目技能。

        同名优先级：**全局优先，项目级同名技能被遮蔽**（安全审查低危项：此前
        没写明，且与 MCP「项目覆盖全局」的惯例相反）。这是有意的：项目技能来自
        仓库，只有在工作区被信任后才会被发现；让仓库里的技能覆盖用户自己的全局
        技能，等于把「项目能改模型行为」的边界又扩大一圈。需要按项目定制时，
        给技能换个名字，或用技能范围的 scope 机制限定全局技能的生效项目。
        """
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
        self._write_disabled()
        return True

    def _write_disabled(self) -> None:
        """把「当前项目停用名单」写回 skills.json（只记已发现且被停用的技能）。"""
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        disabled = sorted(n for n, s in self._skills.items() if not s.enabled)
        self.state_path.write_text(
            json.dumps({"disabled": disabled}, ensure_ascii=False), encoding="utf-8"
        )

    def forget(self, name: str) -> None:
        """抹掉一个技能留下来的一切状态记录（删除技能时调）。

        停用名单在 skills.json、范围在 skills-scope.json，两者都以技能名为 key。
        删除时不清理的话，同名技能重新安装后会莫名“装上了却是停用/任何项目都不用”
        （用户看不到任何提示，很难自己想明白）——重装的技能应当从干净状态开始。
        """
        changed = False
        if name in self._skills:
            del self._skills[name]
            changed = True
        if name in self._scopes:
            del self._scopes[name]
            self._save_scopes()
            changed = True
        if changed:
            self._write_disabled()

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
            # 名称/描述在加载时已做限长与压空白（见 _sanitize_name/_sanitize_description），
            # 一行一条，不让外部 frontmatter 撑破清单排版
            lines.append("- {}: {}".format(s.name, s.description or "(no description)"))
        return "\n".join(lines) + "\n"
