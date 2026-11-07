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
- 指纹不只覆盖配置本体，还覆盖配置**引用的执行载荷**：mcp.json 里 stdio 服务器
  的 command/args 指向的项目内本地文件、技能目录里 SKILL.md 同目录的实际内容。
  否则攻击者可以先提交无害脚本骗取信任，之后只把脚本换血（配置没变、指纹没变，
  恶意代码照常执行）——载荷换血必须和配置变更一样被看见。
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

from ..textio import write_text_atomic

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


def _project_local_path(project_root: Path, raw: str) -> Path | None:
    """把 command/args 里的一个字符串解析成「项目内的本地文件路径」。

    相对路径按项目根解析（trust 关心的是随仓库分发的东西）；绝对路径只在仍
    落在项目根内时才算。返回绝对路径；项目外/解析不了返回 None。包含判定用
    ``os.path.abspath`` + ``relpath``（不解析符号链接）：项目内的符号链接文件
    也算项目内——读取内容时自然哈希到链接目标的字节，那才是真正会执行的东西。
    """
    text = raw.strip()
    if not text or "\n" in text or "\r" in text:
        return None
    p = Path(text)
    if not p.is_absolute():
        p = project_root / p
    try:
        abs_p = Path(os.path.abspath(str(p)))
        abs_root = Path(os.path.abspath(str(project_root)))
        rel = os.path.relpath(abs_p, abs_root)
    except (OSError, ValueError):
        return None
    if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
        return None
    return abs_p


def _mcp_payload_files(project_root: Path, mcp_path: Path) -> list[Path]:
    """项目级 mcp.json 里 stdio 服务器引用的项目内本地文件。

    stdio 服务器会被 subprocess 拉起，command/args 指向的项目内脚本就是真正会
    执行的载荷——只哈希配置本体的话，攻击者可以先交一份无害脚本骗取信任，再把
    脚本换血（配置没变、指纹没变，恶意代码照常执行）。只纳入**存在**的项目内
    文件（存在才纳入：不存在的路径让指纹跟着瞬态垃圾摇摆没有意义）；解析失败
    按没有载荷处理——mcp.json 本体已参与哈希，它一变指纹照样变。
    """
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    candidates: list[str] = []
    for section in servers.values():
        if not isinstance(section, dict):
            continue
        command = section.get("command")
        if not command:
            continue  # 远程（url）服务器没有本地执行载荷
        if isinstance(command, str):
            candidates.append(command)
        args = section.get("args")
        if isinstance(args, list):
            candidates.extend(a for a in args if isinstance(a, str))
    out: list[Path] = []
    for raw in candidates:
        p = _project_local_path(project_root, raw)
        if p is None:
            continue
        try:
            exists = p.is_file()
        except OSError:
            exists = True  # stat 失败按「存在」保守处理，让占位符参与哈希
        if exists:
            out.append(p)
    return out


def _skill_payload_files(skill_dir: Path) -> list[Path]:
    """技能目录里 SKILL.md 同目录（含子目录）的实际内容。

    SKILL.md 正文会指导 Agent 去跑目录里的脚本：只哈希 SKILL.md 的话，信任后把
    脚本换血，指纹毫无察觉。目录整体做内容清单（跳过作为来源的 SKILL.md 本体，
    不重复哈希）；符号链接目录不深入（防循环、防越界），读不动的条目跳过，
    不让单个怪文件拖垮指纹计算。
    """
    out: list[Path] = []
    top_md = skill_dir / "SKILL.md"
    for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p == top_md:
                continue
            try:
                regular = p.is_file()
            except OSError:
                regular = False
            if regular:
                out.append(p)
    return out


def project_sources(project_root: Path) -> list[Path]:
    """项目里会「自动生效」的配置来源及其引用的执行载荷（按固定顺序，保证指纹稳定）。

    配置来源只看这两处：项目级 MCP 配置与项目级技能。``.skysheep/skills.json``
    （停用名单）不算——它是用户自己的选择，且恶意仓库把它写成「全部停用」
    反而更安全。

    每份配置之后紧跟它引用的执行载荷（指纹与逐来源哈希共用这一份清单，天然
    同口径）：mcp.json 的 stdio command/args 指向的项目内本地文件（存在才纳入）、
    技能目录里 SKILL.md 同目录的实际内容。载荷换血必须和配置变更一样触发重确认。
    """
    root = project_root / ".skysheep"
    out: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        # mcp 的 args 可能指到技能目录里的脚本：按归一化绝对路径去重，只哈希一次
        key = os.path.normcase(os.path.abspath(str(p)))
        if key not in seen:
            seen.add(key)
            out.append(p)

    mcp = root / "mcp.json"
    if mcp.is_file():
        _add(mcp)
        for p in _mcp_payload_files(project_root, mcp):
            _add(p)
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        try:
            children = sorted(skills_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            children = []
        for child in children:
            md = child / "SKILL.md"
            if md.is_file():
                _add(md)
                for p in _skill_payload_files(child):
                    _add(p)
    return out


def _rel_label(f: Path, project_root: Path) -> str:
    """来源文件参与哈希用的相对路径标签（compute_fingerprint/source_hashes 同口径）。

    载荷文件按项目根解析后是绝对路径，而调用方传进来的 project_root 可能是
    相对路径：两种基准都试一遍，真不在项目里的（理论上不会发生）退回文件名。
    """
    for base in (project_root, Path(os.path.abspath(str(project_root)))):
        try:
            return f.relative_to(base).as_posix()
        except ValueError:
            continue
    return f.name


MAX_HASH_BYTES = 2 * 1024 * 1024


def _hashable_bytes(path: Path) -> bytes:
    """信任指纹用的文件字节：单文件最多读 MAX_HASH_BYTES。

    指纹在用户确认信任**之前**计算（WorkspaceTrust.state()），而载荷清单来自
    项目里的 mcp.json/技能目录——恶意项目可以指向一个 GB 级文件，让引擎在
    打开项目时全量 read_bytes 做哈希（内存放大 + 事件循环同步阻塞）。超过
    上限只哈希前缀 + 文件大小；超大文件本来就不是可信配置/脚本的常态。
    """
    try:
        with path.open("rb") as f:
            data = f.read(MAX_HASH_BYTES + 1)
    except OSError:
        return b"<unreadable>"
    if len(data) <= MAX_HASH_BYTES:
        return data
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    return data[:MAX_HASH_BYTES] + f"<truncated size={size}>".encode("ascii")


def compute_fingerprint(project_root: Path) -> tuple[str, list[str]]:
    """项目级配置与其引用执行载荷的内容指纹 + 相对路径清单。

    返回 ``("", [])`` 表示项目里没有任何需要信任的配置（此时无需询问）。
    用相对路径参与哈希，避免同一份内容因为克隆目录不同而指纹不同。
    读不出的文件用固定占位符参与哈希：既不会让指纹随机变化，也仍然要求一次信任。
    清单里包含配置引用的载荷（stdio 脚本、技能目录内容）：载荷换血指纹必变。
    """
    files = project_sources(project_root)
    if not files:
        return "", []
    digest = hashlib.sha256()
    labels: list[str] = []
    for f in files:
        rel = _rel_label(f, project_root)
        data = _hashable_bytes(f)
        digest.update(rel.encode("utf-8", "replace"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\x00")
        labels.append(rel)
    return digest.hexdigest(), labels


def source_hashes(project_root: Path) -> dict[str, str]:
    """每个来源文件（配置本体及其引用的载荷）的内容哈希（相对路径 → sha256）。

    用于「谁变了」的判定；与 compute_fingerprint 共用 project_sources 这一份
    清单与 _rel_label 这一套标签，三处（指纹/逐来源哈希/refresh 比对）天然同口径。
    """
    out: dict[str, str] = {}
    for f in project_sources(project_root):
        rel = _rel_label(f, project_root)
        out[rel] = hashlib.sha256(_hashable_bytes(f)).hexdigest()
    return out


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
    """按项目记忆的信任状态。每打开一个项目建一个实例。

    ``project_root=None`` 是无项目态（引擎没开项目的兜底；快聊/远程连接
    的会话无项目记录，也归此类）：没有项目级配置，无可信任之物，state 恒
    为 clean，变更操作一律 no-op。
    """

    def __init__(self, home: Path | str, project_root: Path | str | None) -> None:
        self.home = Path(home)
        self.project_root = Path(project_root) if project_root is not None else None
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
        payload = {"version": TRUST_VERSION, "projects": projects}
        # 原子写统一走 textio.write_text_atomic（建父目录、mkstemp 唯一临时名、
        # rename 前 fsync、失败清理临时文件）：手写 tmp.write_text + os.replace
        # 用的是固定 tmp 名，并发写同一份信任记录会互相覆盖半成品，且缺 fsync
        # 与失败清理。
        write_text_atomic(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- 查询 ----

    def state(self) -> dict:
        """当前状态：``clean`` / ``trusted`` / ``pending``，附指纹与待确认清单。"""
        if self.project_root is None:
            return {
                "state": STATE_CLEAN, "fingerprint": "", "sources": [],
                "items": [], "trusted_at": None,
            }
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
        if self.project_root is None:
            return self.state()
        fingerprint, _ = compute_fingerprint(self.project_root)
        projects = self._load()
        if not fingerprint:
            projects.pop(_norm_root(self.project_root), None)
        else:
            projects[_norm_root(self.project_root)] = {
                "fingerprint": fingerprint,
                "trusted_at": int(time.time()),
                # 逐来源哈希：refresh(touched=...) 据此判断「变的是不是只有
                # 用户刚动的那个来源」（见 refresh 的洗白说明）
                "sources": source_hashes(self.project_root),
            }
        self._save(projects)
        return self.state()

    def refresh(self, touched: str | Path | None = None) -> dict:
        """用户自己改动了项目级配置后，同步指纹（仅在已信任时生效）。

        用户在设置页主动往当前项目装技能 / 加 MCP 服务时，项目级配置的内容变了，
        指纹也就对不上——若不刷新，用户刚装的东西会立刻回到「待确认」而失效，
        表现就是「装上了却像没生效」。这里把用户自己的改动视为延续既有信任：
        本来就信任才刷新；未信任时保持不变（仍然需要确认，不会因为装了一个
        技能就把仓库里其它东西一并放行）。

        touched（安全审查低危项「refresh 洗白边角」）：本次操作实际写入的来源
        （项目级 mcp.json 或项目级技能目录）。旧实现无条件把**全部**来源重新
        指纹并延续信任——若在用户上次信任之后，别的来源被第三方改过（git pull
        带来恶意 .skysheep/mcp.json、或被注入的 Agent 写的），用户随手装一个
        技能就会连它一起洗白并自动连接。现在逐来源比对：只有「变了的来源都落在
        touched 之内」才延续信任，否则保持 pending 让用户重新确认。
        touched=None（调用方未指明）时按旧行为延续——此时无法区分是谁改的，
        但至少不再扩大范围：只有本来就信任的项目才会走到这里。

        逐来源比对覆盖配置引用的执行载荷（stdio 脚本、技能目录内容）：
        载荷换血也是「来源变了」，同样走 touched 前缀判定——mcp.json 指向的
        项目内脚本不在技能目录前缀内，用户装技能不会把它一并洗白。
        """
        projects = self._load()
        if self.project_root is None or _norm_root(self.project_root) not in projects:
            return self.state()
        entry = projects.get(_norm_root(self.project_root)) or {}
        prev_sources = entry.get("sources") if isinstance(entry, dict) else None
        fingerprint, _ = compute_fingerprint(self.project_root)
        if touched is not None and (not isinstance(prev_sources, dict) or not prev_sources):
            # 旧格式记录（无逐来源哈希，1.9 前的 grant 只存整体指纹）：无法区分
            # 「用户刚动的」与「第三方改的」。只认「内容没变」——指纹一致才延续
            # 信任，并顺带把记录升级成带 sources 的新格式；内容有变一律保持
            # pending，让用户重新确认一次（审查 P2-6：legacy 记录曾会被全量洗白）。
            recorded_fp = str(entry.get("fingerprint") or "")
            if fingerprint and fingerprint != recorded_fp:
                return self.state()
        elif touched is not None and isinstance(prev_sources, dict) and prev_sources:
            try:
                rel = Path(touched).expanduser().resolve().relative_to(
                    self.project_root.resolve()
                ).as_posix()
            except (OSError, RuntimeError, ValueError):
                rel = Path(str(touched)).name
            now_sources = source_hashes(self.project_root)
            changed = {
                name for name, digest in now_sources.items()
                if prev_sources.get(name) != digest
            }
            # touched 可能是目录（技能目录）：按前缀判定「这一来源下的文件」
            prefix = rel.rstrip("/") + "/"
            stray = sorted(
                name for name in changed
                if name != rel and not name.startswith(prefix)
            )
            if stray:
                # 别的来源也变了：不延续信任（可能正是被第三方塞进来的改动）
                return self.state()
        if fingerprint:
            projects[_norm_root(self.project_root)] = {
                "fingerprint": fingerprint,
                "trusted_at": int(time.time()),
                "sources": source_hashes(self.project_root),
            }
        else:
            projects.pop(_norm_root(self.project_root), None)
        self._save(projects)
        return self.state()

    def revoke(self) -> dict:
        if self.project_root is None:
            return self.state()
        projects = self._load()
        projects.pop(_norm_root(self.project_root), None)
        self._save(projects)
        return self.state()


def list_trusted(home: Path | str) -> list[dict]:
    """全部已信任项目的清单（设置页管理用）：路径 + 信任时刻。

    只读信任记录，不逐项目重算指纹——管理页只要「记过什么」，状态是否
    仍有效等真正打开该项目时自然会见分晓。
    """
    path = Path(home) / TRUST_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("version") != TRUST_VERSION:
        return []
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return []
    out: list[dict] = []
    for key, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        out.append({
            # 存的是 normcase 后的路径，仅作展示（Windows 上小写化）；撤销按同键删除
            "path": str(key),
            "fingerprint": str(entry.get("fingerprint") or ""),
            "trusted_at": entry.get("trusted_at"),
        })
    out.sort(key=lambda e: e.get("trusted_at") or 0, reverse=True)
    return out


def revoke_by_path(home: Path | str, project_root: str | Path) -> bool:
    """按路径撤销信任（设置页管理用）：不要求该项目是「当前项目」。

    返回是否真的删了一条记录；路径不存在于记录中时为 False。
    """
    path = Path(home) / TRUST_FILE_NAME
    projects: dict = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            projects = data["projects"]
    except (OSError, ValueError):
        return False
    key = _norm_root(project_root)
    if key not in projects:
        return False
    projects.pop(key, None)
    payload = {"version": TRUST_VERSION, "projects": projects}
    # 与 _save 同一标准：原子写走 textio.write_text_atomic，不再手写固定 tmp 名
    # 的 write_text + os.replace。
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return True
