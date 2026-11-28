"""SessionStore 数据模型与跨模块共享哨兵/转换助手。"""

from __future__ import annotations

import json
from dataclasses import dataclass

# usage_stats 的 project_id 哨兵：None 在项目语义里表示快聊（合法过滤目标），
# 因此「不过滤」用独立哨兵表示，避免与 NULL 项目混淆（安全审查 B14 的过滤参数）。
_ALL = object()

_RELAY_UNSET = object()


def _relay_json_load(text, default):
    try:
        value = json.loads(text) if text else default
    except (ValueError, TypeError):
        return default
    return value if value is not None else default


@dataclass
class Project:
    id: int
    root_path: str
    name: str
    created_at: float
    # 软移除时间戳（0 = 正常显示）。项目行仍在，会话/搜索/恢复都靠它
    removed_at: float = 0.0


def project_from_row(r) -> Project:
    """从一行 projects 记录构造 Project（兼容尚未补 removed_at 列的旧库行）。"""
    return Project(
        r["id"], r["root_path"], r["name"], r["created_at"],
        r["removed_at"] if "removed_at" in r.keys() else 0.0,
    )


@dataclass
class Session:
    id: str
    project_id: int | None
    title: str
    created_at: float
    updated_at: float
    pinned: int = 0
    summary: str = ""
    # 用户给会话打的标签（自由文本，逗号分隔存库；侧栏按标签分组）。
    # 用字符串而非单独的表：一个会话标签数很少，分组只需前缀匹配/包含，
    # 不需要为它维护关联表与外键。
    tags: str = ""
    # 归档：1 = 已归档（侧栏默认不显示，可从归档弹窗恢复）
    archived: int = 0


def session_from_row(r) -> Session:
    """从一行 sessions 记录构造 Session（兼容缺列的旧库行）。"""
    return Session(
        r["id"], r["project_id"], r["title"],
        r["created_at"], r["updated_at"], r["pinned"], r["summary"],
        r["tags"] if "tags" in r.keys() else "",
        r["archived"] if "archived" in r.keys() else 0,
    )
