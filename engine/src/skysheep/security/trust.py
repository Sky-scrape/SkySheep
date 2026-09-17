"""Workspace trust：打开未知项目时，不自动执行/注入项目自带的配置。

问题：项目目录下的 ``.skysheep/mcp.json`` 与 ``.skysheep/skills/`` 是随仓库一起
分发的**数据**，但前者会被后端在启动阶段直接 ``subprocess`` 拉起（stdio 传输），
后者会被拼进系统提示词。用户只是「打开了一个目录」，就会在没有任何确认的情况下
执行仓库里的本地命令、或让仓库里的文本影响模型行为。这是典型的 workspace trust
类供应链风险（VS Code / Cursor 都出现过同类问题），业界做法是首次打开未知项目时
显式询问是否信任。

设计要点：

- 信任记录存在**用户主目录**（``~/.skysheep/workspace-trust.json``），按项目路径记忆。
  不能放在项目目录里——恶意仓库会连同信任记录一起提交，等于自我授权。
- 记录里存项目级配置的**指纹**（内容哈希）。配置一变指纹就对不上，需要重新确认；
  否则攻击者可以先提交一个无害配置骗取信任，之后再推恶意配置进来。
- 全局配置（``~/.skysheep/mcp.json``、``~/.skysheep/skills/``）不受影响：那是用户
  自己主动写的，信任边界不同，保持自动加载。
- 读不出/写不了信任记录时一律按「未信任」处理（fail closed）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

TRUST_FILE_NAME = "workspace-trust.json"
TRUST_VERSION = 1

# 状态：项目里没有需要信任的配置 / 已信任且指纹一致 / 有配置但（首次或已变更）未信任
STATE_CLEAN = "clean"
STATE_TRUSTED = "trusted"
STATE_PENDING = "pending"


def _norm_root(project_root: str | Path) -> str:
    """项目路径归一化：Windows 大小写不敏感，比较前必须统一。"""
    try:
        resolved = Path(project_root).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(project_root).expanduser()
    return os.path.normcase(str(resolved))


def project_sources(project_root: Path) -> list[Path]:
    """项目里会「自动生效」的配置来源（按固定顺序，保证指纹稳定）。

    只看这两处：项目级 MCP 配置与项目级技能。``.skysheep/skills.json``（停用名单）
    不算——它是用户自己的选择，且恶意仓库把它写成「全部停用」反而更安全。
    """
    root = project_root / ".skysheep"
    out: list[Path] = []
    mcp = root / "mcp.json"
    if mcp.is_file():
        out.append(mcp)
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        try:
            children = sorted(skills_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            children = []
        for child in children:
            md = child / "SKILL.md"
            if md.is_file():
                out.append(md)
    return out


def compute_fingerprint(project_root: Path) -> tuple[str, list[str]]:
    """项目级配置的内容指纹 + 相对路径清单。

    返回 ``("", [])`` 表示项目里没有任何需要信任的配置（此时无需询问）。
    用相对路径参与哈希，避免同一份内容因为克隆目录不同而指纹不同。
    读不出的文件用固定占位符参与哈希：既不会让指纹随机变化，也仍然要求一次信任。
    """
    files = project_sources(project_root)
    if not files:
        return "", []
    digest = hashlib.sha256()
    labels: list[str] = []
    for f in files:
        try:
            rel = f.relative_to(project_root).as_posix()
        except ValueError:
            rel = f.name
        try:
            data = f.read_bytes()
        except OSError:
            data = b"<unreadable>"
        digest.update(rel.encode("utf-8", "replace"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\x00")
        labels.append(rel)
    return digest.hexdigest(), labels


def describe_sources(project_root: Path) -> list[dict]:
    """给确认界面用的摘要：这个项目「想做什么」。

    只读不改，任何解析失败都降级成能看懂的文字——确认界面拿不到信息时，
    用户就无从判断，反而不如直接说明「这项读不出来」。
    """
    items: list[dict] = []
    root = project_root / ".skysheep"
    mcp = root / "mcp.json"
    if mcp.is_file():
        try:
            data = json.loads(mcp.read_text(encoding="utf-8"))
            servers = (data or {}).get("mcpServers") or {}
        except (OSError, ValueError):
            servers = {}
        for name, section in (servers.items() if isinstance(servers, dict) else []):
            cfg = section if isinstance(section, dict) else {}
            if cfg.get("url"):
                detail = "远程地址 " + str(cfg.get("url"))
            elif cfg.get("command"):
                args = cfg.get("args") or []
                arg_text = " ".join(str(a) for a in args) if isinstance(args, list) else ""
                detail = "要执行的命令（" + str(cfg.get("command")) \
                    + ((" " + arg_text) if arg_text else "") + "）"
            else:
                detail = "配置不完整（缺 command / url）"
            items.append({
                "kind": "mcp",
                "name": str(name),
                "detail": detail,
                "readonly": bool(cfg.get("readonly")),
            })
        if not servers:
            items.append({
                "kind": "mcp", "name": "(无法解析)",
                "detail": "项目里的 .skysheep/mcp.json 存在但读不出来", "readonly": False,
            })
    for md in project_sources(project_root):
        if md.name != "SKILL.md":
            continue
        items.append({
            "kind": "skill",
            "name": md.parent.name,
            "detail": "会注入系统提示词的技能 (.skysheep/skills/" + md.parent.name + "/SKILL.md)",
            "readonly": False,
        })
    return items


class WorkspaceTrust:
    """按项目记忆的信任状态。每打开一个项目建一个实例。"""

    def __init__(self, home: Path | str, project_root: Path | str) -> None:
        self.home = Path(home)
        self.project_root = Path(project_root)
        self.path = self.home / TRUST_FILE_NAME

    # ---- 存储 ----

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != TRUST_VERSION:
            return {}
        projects = data.get("projects")
        return projects if isinstance(projects, dict) else {}

    def _save(self, projects: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        payload = {"version": TRUST_VERSION, "projects": projects}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换：写一半断电不会留下坏文件

    # ---- 查询 ----

    def state(self) -> dict:
        """当前状态：``clean`` / ``trusted`` / ``pending``，附指纹与待确认清单。"""
        fingerprint, labels = compute_fingerprint(self.project_root)
        if not fingerprint:
            return {
                "state": STATE_CLEAN, "fingerprint": "", "sources": [],
                "items": [], "trusted_at": None,
            }
        entry = self._load().get(_norm_root(self.project_root))
        recorded = str(entry.get("fingerprint") or "") if isinstance(entry, dict) else ""
        items = describe_sources(self.project_root)
        if recorded and recorded == fingerprint:
            return {
                "state": STATE_TRUSTED, "fingerprint": fingerprint, "sources": labels,
                "items": items,
                "trusted_at": entry.get("trusted_at") if isinstance(entry, dict) else None,
            }
        return {
            "state": STATE_PENDING, "fingerprint": fingerprint, "sources": labels,
            "items": items, "trusted_at": None,
            "changed": bool(recorded),  # 曾经信任过、但配置变了
        }

    def is_trusted(self) -> bool:
        """是否可以把项目级配置当作可信。

        没有项目级配置时返回 True——「无可信任之物」不该拦着用户干活。
        """
        return self.state()["state"] in (STATE_CLEAN, STATE_TRUSTED)

    # ---- 变更 ----

    def grant(self) -> dict:
        fingerprint, _ = compute_fingerprint(self.project_root)
        projects = self._load()
        if not fingerprint:
            projects.pop(_norm_root(self.project_root), None)
        else:
            projects[_norm_root(self.project_root)] = {
                "fingerprint": fingerprint,
                "trusted_at": int(time.time()),
            }
        self._save(projects)
        return self.state()

    def refresh(self) -> dict:
        """用户自己改动了项目级配置后，同步指纹（仅在已信任时生效）。

        用户在设置页主动往当前项目装技能 / 加 MCP 服务时，项目级配置的内容变了，
        指纹也就对不上——若不刷新，用户刚装的东西会立刻回到「待确认」而失效，
        表现就是「装上了却像没生效」。这里把用户自己的改动视为延续既有信任：
        本来就信任才刷新；未信任时保持不变（仍然需要确认，不会因为装了一个
        技能就把仓库里其它东西一并放行）。
        """
        projects = self._load()
        if _norm_root(self.project_root) not in projects:
            return self.state()
        fingerprint, _ = compute_fingerprint(self.project_root)
        if fingerprint:
            projects[_norm_root(self.project_root)] = {
                "fingerprint": fingerprint,
                "trusted_at": int(time.time()),
            }
        else:
            projects.pop(_norm_root(self.project_root), None)
        self._save(projects)
        return self.state()

    def revoke(self) -> dict:
        projects = self._load()
        projects.pop(_norm_root(self.project_root), None)
        self._save(projects)
        return self.state()
