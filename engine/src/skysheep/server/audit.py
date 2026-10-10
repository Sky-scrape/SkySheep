"""扩展变更审计：MCP / 技能 / Mod 的安装、更新、删除、启停留痕。

append-only JSONL（每行一个 JSON 对象），落 ~/.skysheep/audit.jsonl。
只记事实（何时、对什么、做了什么、结果概要），不记敏感值——不落令牌、
不落 env/headers 内容、不落完整命令行（command 属于命令清单已在配置里）。

用途：「这一阵子装过什么、动过什么」的回溯。设置页按最近在前展示，
读取侧只回最近 AUDIT_READ_LIMIT 条，文件本身不主动截断（量级很小：
每条百来字节，日常使用一年也就几十 KB）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..textio import write_text_atomic

AUDIT_FILE = "audit.jsonl"
AUDIT_READ_LIMIT = 500

# 审计动作白名单：写入侧只接受这些 kind/action 组合，防调用方拼出脏数据
_KINDS = ("mcp", "skill", "mod")
_ACTIONS = ("install", "update", "remove", "enable", "disable")


def audit_path(home: Path) -> Path:
    return home / AUDIT_FILE


def record_audit(
    home: Path,
    *,
    kind: str,
    action: str,
    name: str,
    scope: str = "global",
    detail: str = "",
) -> dict | None:
    """追加一条审计记录，返回写入的对象（失败返回 None，不影响主流程）。

    审计是旁路：写失败只静默放弃，绝不让「留痕失败」把用户的安装/删除
    操作变成报错。追加用「读旧 + 原子重写」而非 open(append)：Windows 上
    多进程同时 append 可能交错；本文件写入频率极低（用户手动操作才触发），
    原子重写的开销可以忽略，换来的是每行完整性有保障。
    """
    if kind not in _KINDS or action not in _ACTIONS:
        return None
    entry = {
        "ts": time.time(),
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "action": action,
        "name": " ".join(str(name or "").split())[:200],
        "scope": scope if scope in ("global", "project") else "global",
        "detail": " ".join(str(detail or "").split())[:300],
    }
    path = audit_path(home)
    try:
        old = ""
        if path.exists():
            try:
                old = path.read_text(encoding="utf-8")
            except OSError:
                old = ""
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        write_text_atomic(path, old + line)
        return entry
    except OSError:
        return None


def read_audit(home: Path, *, limit: int = AUDIT_READ_LIMIT) -> list[dict]:
    """读最近 limit 条（最近在前）。坏行跳过，读失败回空。"""
    path = audit_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("kind") in _KINDS:
            out.append(item)
    return list(reversed(out[-max(1, min(limit, AUDIT_READ_LIMIT)):]))
