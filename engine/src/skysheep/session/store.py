"""SQLite 会话持久化：项目 / 会话 / 消息 / 白名单规则。

消息以归一化 Message 的 JSON 形式存储；加载后可无损恢复 Agent 历史。
注意：查询一律使用占位符参数，不拼接 SQL。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..messages import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE TABLE IF NOT EXISTS whitelist_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snippets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cron_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL DEFAULT 'interval',
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    time_of_day TEXT NOT NULL DEFAULT '',
    weekday INTEGER NOT NULL DEFAULT -1,
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    next_run_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    start_at REAL NOT NULL,
    remind INTEGER NOT NULL DEFAULT 1,
    remind_before INTEGER NOT NULL DEFAULT 0,
    reminded INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_start ON schedules(start_at);
"""


@dataclass
class Project:
    id: int
    root_path: str
    name: str
    created_at: float


@dataclass
class Session:
    id: str
    project_id: int | None
    title: str
    created_at: float
    updated_at: float
    pinned: int = 0
    summary: str = ""


def parse_backup_stamp(stamp: str) -> float | None:
    """把备份文件名里的时间戳（`%Y%m%d-%H%M%S`，后面可能跟「-恢复前」）解析成时刻。

    必须按文件名解析、不能拿 mtime 当备份时间：备份是 `shutil.copy2` 复制出来的，
    会连源库的修改时间一起带过来，于是 20 份备份的 mtime 全都一样（都是源库最后
    一次写入的那一刻），列表看起来像一堆重复项。

    解析不了（老文件、手改过名字）返回 None，调用方退回文件 mtime。
    """
    try:
        return time.mktime(time.strptime(stamp[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


class SessionStore:
    BACKUP_KEEP = 20
    # 恢复前自动留的安全副本后缀（列表里单独标记，方便用户认出「这是恢复动作留下的」）
    SAFETY_TAG = "-恢复前"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self.backup_created: str | None = None

    def _rolling_backup(self) -> str | None:
        """打开数据库前先滚动备份（保留最近 BACKUP_KEEP 份），防止误删无法恢复。"""
        import shutil

        if not self.path.exists():
            return None
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"{self.path.stem}-{stamp}.db"
        try:
            if not target.exists() and self.path.stat().st_size > 0:
                shutil.copy2(self.path, target)
                self.backup_created = str(target)
        except OSError:
            return None
        # 只保留最近的 N 份
        backups = sorted(backup_dir.glob(self.path.stem + "-*.db"))
        for old in backups[: max(0, len(backups) - self.BACKUP_KEEP)]:
            try:
                old.unlink()
            except OSError:
                pass
        return self.backup_created

    # ---- 会话库备份：列出 / 恢复（设置 · 关于里的「从备份恢复」） ----

    def backup_dir(self) -> Path:
        return self.path.parent / "backups"

    def list_backups(self) -> list[dict]:
        """可用备份列表（新的在前）：备份时刻、大小、路径。

        `taken` 是从文件名时间戳解析出的**备份时刻**，界面按它排序和显示。不要拿 `mtime`
        当备份时间：备份用 `shutil.copy2` 复制，会连源库的修改时间一起带过来，于是所有备份
        都显示成「源库最后一次写入」那一刻，列表看起来像一堆重复项。`safety=True` 的是恢复
        动作自动留下的「恢复前」副本，界面上单独标注。
        """
        d = self.backup_dir()
        if not d.is_dir():
            return []
        items: list[dict] = []
        for f in d.glob(self.path.stem + "-*.db"):
            try:
                st = f.stat()
            except OSError:
                continue
            stamp = f.stem[len(self.path.stem) + 1:]
            items.append({
                "name": f.name,
                "path": str(f),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "taken": parse_backup_stamp(stamp) or st.st_mtime,
                "stamp": stamp,
                "safety": stamp.endswith(self.SAFETY_TAG),
                "current": False,
            })
        try:
            cur = self.path.stat()
            items.append({
                "name": self.path.name,
                "path": str(self.path),
                "size": cur.st_size,
                "mtime": cur.st_mtime,
                "taken": cur.st_mtime,  # 当前库没有备份名字，只能用它自己的修改时间
                "stamp": "当前",
                "safety": False,
                "current": True,
            })
        except OSError:
            pass
        # 当前数据永远排第一；其余按备份时刻（不是 mtime）由新到旧
        items.sort(key=lambda x: (not x["current"], -x["taken"]))
        return items

    async def restore_backup(self, name: str) -> dict:
        """用某个备份覆盖当前会话库（重启前先给现状留一份备份，可再换回来）。

        会关闭再重开数据库连接；调用方（backend）负责随后刷新内存里的会话状态。
        """
        import shutil

        d = self.backup_dir()
        # 只接受纯文件名：带路径分隔符/上级目录的输入直接拒绝，不做静默归一化
        if Path(name).name != name or not name.endswith(".db"):
            raise ValueError("备份文件名不合法")
        src = (d / name).resolve()
        try:
            src.relative_to(d.resolve())
        except ValueError:
            raise ValueError("备份文件名不合法") from None
        if not src.is_file():
            raise FileNotFoundError("备份不存在：" + name)
        # 先给"当前"存一份，用户万一恢复错了还能回来
        safety = None
        if self.path.exists() and self.path.stat().st_size > 0:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safety = d / f"{self.path.stem}-{stamp}{self.SAFETY_TAG}.db"
            try:
                shutil.copy2(self.path, safety)
            except OSError:
                safety = None
        if self._db is not None:
            await self._db.close()
            self._db = None
        shutil.copy2(src, self.path)
        await self.connect()
        return {
            "restored": src.name,
            "safety_copy": str(safety) if safety else "",
        }

    async def connect(self) -> SessionStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rolling_backup()
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # 旧库迁移：sessions 补 pinned 列（已存在则忽略）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：schedules 补 remind_before 列（0.5.0 建的表没有；0 = 到点提醒）
        try:
            await self._db.execute(
                "ALTER TABLE schedules ADD COLUMN remind_before INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 旧库迁移：sessions 补 summary 列（会话摘要：最近一轮助手回答的开头，侧栏直接看进展）
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ---- projects ----

    async def get_or_create_project(self, root_path: str, name: str = "") -> Project:
        assert self._db
        root_path = str(Path(root_path).resolve())
        cur = await self._db.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        )
        row = await cur.fetchone()
        if row:
            return Project(row["id"], row["root_path"], row["name"], row["created_at"])
        cur = await self._db.execute(
            "INSERT INTO projects (root_path, name, created_at) VALUES (?, ?, ?)",
            (root_path, name or Path(root_path).name, time.time()),
        )
        await self._db.commit()
        return Project(cur.lastrowid, root_path, name or Path(root_path).name, time.time())

    async def list_projects(self) -> list[Project]:
        assert self._db
        cur = await self._db.execute("SELECT * FROM projects ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [Project(r["id"], r["root_path"], r["name"], r["created_at"]) for r in rows]

    async def delete_project(self, project_id: int) -> int:
        """删除项目记录及其全部会话（含消息）与白名单规则，返回删除的行数（0=不存在）。
        只清数据库记录，电脑上的项目文件夹不受影响。"""
        assert self._db
        await self._db.execute(
            "DELETE FROM messages WHERE session_id IN "
            "(SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        await self._db.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM whitelist_rules WHERE project_id = ?", (project_id,))
        cur = await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()
        return cur.rowcount

    # ---- sessions ----

    async def create_session(self, project_id: int | None, title: str = "") -> Session:
        assert self._db
        now = time.time()
        sid = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO sessions (id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (sid, project_id, title, now, now),
        )
        await self._db.commit()
        return Session(sid, project_id, title, now, now)

    async def get_session(self, session_id: str) -> Session | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return Session(
            row["id"], row["project_id"], row["title"],
            row["created_at"], row["updated_at"], row["pinned"], row["summary"],
        )

    async def list_sessions(self, project_id: int | None = None, limit: int = 50) -> list[Session]:
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions ORDER BY pinned DESC, updated_at DESC LIMIT ?", (limit,)
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ?"
                " ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (project_id, limit),
            )
        rows = await cur.fetchall()
        return [
            Session(
                r["id"], r["project_id"], r["title"],
                r["created_at"], r["updated_at"], r["pinned"], r["summary"],
            )
            for r in rows
        ]

    async def find_last_user_seq(self, session_id: str) -> int | None:
        """最后一条 user 消息的 seq（消息级重生成/编辑的默认锚点）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq FROM messages WHERE session_id = ? AND role = 'user'"
            " ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return row["seq"] if row else None

    async def search_messages_by_seq(self, session_id: str, before_seq: int, role: str) -> int | None:
        """锚点之前（seq 更小）最近一条指定角色的消息 seq。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq FROM messages WHERE session_id = ? AND role = ? AND seq < ?"
            " ORDER BY seq DESC LIMIT 1",
            (session_id, role, before_seq),
        )
        row = await cur.fetchone()
        return row["seq"] if row else None

    async def max_seq(self, session_id: str) -> int | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT MAX(seq) AS m FROM messages WHERE session_id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return row["m"] if row and row["m"] is not None else None

    async def get_message_at(self, session_id: str, seq: int) -> dict | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, role, content FROM messages WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            plain = Message.model_validate_json(row["content"]).to_plain()
        except Exception:
            plain = ""
        return {"seq": row["seq"], "role": row["role"], "text": plain}

    async def truncate_from(self, session_id: str, seq: int, include_self: bool) -> int:
        """删除 seq 在锚点之后（或含锚点）的消息，返回删除条数。

        锚点之后可能残留比锚点大的 seq，用 > / >= 锚点即可（seq 单调递增）。
        """
        assert self._db
        op = ">=" if include_self else ">"
        cur = await self._db.execute(
            f"DELETE FROM messages WHERE session_id = ? AND seq {op} ?",
            (session_id, seq),
        )
        await self._db.commit()
        return cur.rowcount

    async def copy_messages_between(
        self, src_session: str, dst_session: str, upto_seq: int
    ) -> int:
        """把 seq <= upto_seq 的消息复制到新会话（分叉用），返回复制条数。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, role, content, created_at FROM messages"
            " WHERE session_id = ? AND seq <= ? ORDER BY seq ASC",
            (src_session, upto_seq),
        )
        rows = await cur.fetchall()
        n = 0
        for r in rows:
            await self._db.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (dst_session, r["seq"], r["role"], r["content"], r["created_at"]),
            )
            n += 1
        await self._db.commit()
        return n

    async def get_session_title(self, session_id: str) -> str:
        """轻量取标题（多会话管线收尾用，不拉全部列）。"""
        assert self._db
        cur = await self._db.execute("SELECT title FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return (row["title"] if row else "") or ""

    async def set_summary(self, session_id: str, summary: str) -> None:
        """更新会话摘要（最近一轮助手回答的开头，侧栏展示用）。"""
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET summary = ? WHERE id = ?", (summary, session_id)
        )
        await self._db.commit()

    async def set_pinned(self, session_id: str, pinned: bool) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET pinned = ? WHERE id = ?", (1 if pinned else 0, session_id)
        )
        await self._db.commit()

    async def latest_session(self, project_id: int | None) -> Session | None:
        """取项目下最近活跃的会话（用于启动时"接着上次继续"）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id IS NULL"
                " ORDER BY pinned DESC, updated_at DESC LIMIT 1"
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ?"
                " ORDER BY pinned DESC, updated_at DESC LIMIT 1",
                (project_id,),
            )
        row = await cur.fetchone()
        if not row:
            return None
        return Session(
            row["id"], row["project_id"], row["title"],
            row["created_at"], row["updated_at"], row["pinned"], row["summary"],
        )

    async def count_empty_sessions(self, project_id: int | None) -> int:
        """统计没有任何消息的会话（"空会话"）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id IS NULL"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages)"
            )
        else:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages)",
                (project_id,),
            )
        row = await cur.fetchone()
        return int(row[0])

    async def delete_empty_sessions(self, project_id: int | None, keep_id: str | None = None) -> int:
        """删除空会话（保留 keep_id 指定的当前会话与置顶会话），返回删除数量。"""
        assert self._db
        keep = keep_id or ""
        if project_id is None:
            cur = await self._db.execute(
                "DELETE FROM sessions WHERE project_id IS NULL AND pinned = 0"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages) AND id != ?",
                (keep,),
            )
        else:
            cur = await self._db.execute(
                "DELETE FROM sessions WHERE project_id = ? AND pinned = 0"
                " AND id NOT IN (SELECT DISTINCT session_id FROM messages) AND id != ?",
                (project_id, keep),
            )
        await self._db.commit()
        return cur.rowcount or 0

    async def move_session(self, session_id: str, project_id: int | None) -> None:
        """把会话移动到另一个项目（project_id 为 NULL 表示移入快聊）。"""
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET project_id = ? WHERE id = ?", (project_id, session_id)
        )
        await self._db.commit()

    async def set_title(self, session_id: str, title: str) -> None:
        assert self._db
        await self._db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        await self._db.commit()

    async def touch(self, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )
        await self._db.commit()

    async def delete_session(self, session_id: str) -> None:
        """删除会话及其全部消息。"""
        assert self._db
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

    # ---- messages ----

    async def append_message(self, session_id: str, message: Message) -> int:
        assert self._db
        cur = await self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        seq = row[0]
        cur = await self._db.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, message.role, message.model_dump_json(), time.time()),
        )
        await self._db.commit()
        message.seq = seq  # 回填给内存历史/前端 brief 用
        return cur.lastrowid

    async def load_messages(self, session_id: str) -> list[Message]:
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, content FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            m = Message.model_validate_json(r["content"])
            m.seq = r["seq"]  # 回填真实序号（1 基；截断后仍单调用于锚点比较）
            out.append(m)
        return out

    async def search_messages(
        self, project_id: int | None, query: str, limit: int = 20, scope: str = "project"
    ) -> list[dict]:
        """跨会话搜索消息内容（LIKE 匹配，大小写不敏感看 SQLite 默认 ASCII 规则）。

        每个会话只取最新排序下最先命中的片段，返回 [{session_id,title,snippet,…}]。
        scope="all" 时忽略项目过滤，在所有项目（含快聊）里找——用户在多个项目之间
        往往记不住某件事是在哪个项目里聊的。结果附带项目名，便于在列表里区分。
        """
        assert self._db
        like = f"%{query}%"
        if scope == "all":
            cond, args = "1 = 1", ()
        else:
            cond = "s.project_id IS NULL" if project_id is None else "s.project_id = ?"
            args = (project_id,) if project_id is not None else ()
        cur = await self._db.execute(
            "SELECT m.session_id, s.title, s.updated_at, m.content, s.project_id,"
            "       p.name AS project_name, p.root_path AS project_path"
            " FROM messages m"
            " JOIN sessions s ON s.id = m.session_id"
            " LEFT JOIN projects p ON p.id = s.project_id"
            f" WHERE {cond} AND m.content LIKE ?"
            " ORDER BY s.pinned DESC, s.updated_at DESC, m.seq",
            args + (like,),
        )
        rows = await cur.fetchall()
        results: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            if r["session_id"] in seen:
                continue
            seen.add(r["session_id"])
            try:
                plain = Message.model_validate_json(r["content"]).to_plain()
            except Exception:
                plain = str(r["content"])
            low, q = plain.lower(), query.lower()
            pos = low.find(q)
            start = max(0, pos - 40)
            fragment = plain[start : pos + len(query) + 80].replace("\n", " ").strip()
            results.append(
                {
                    "session_id": r["session_id"],
                    "title": r["title"],
                    "snippet": ("…" if start else "") + fragment + "…",
                    "updated_at": r["updated_at"],
                    "project_id": r["project_id"],
                    "project_name": r["project_name"] or "快聊",
                    "project_path": r["project_path"] or "",
                }
            )
            if len(results) >= limit:
                break
        return results

    # ---- whitelist rules ----

    async def add_rule(self, project_id: int, tool: str, kind: str, pattern: str = "") -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO whitelist_rules"
            " (project_id, tool, kind, pattern, created_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, tool, kind, pattern, time.time()),
        )
        await self._db.commit()

    async def list_rules(self, project_id: int) -> list[dict]:
        assert self._db
        # 新规则在前（id 倒序）：设置页白名单列表与最近一次「总是允许」的操作对得上
        cur = await self._db.execute(
            "SELECT id, tool, kind, pattern, created_at FROM whitelist_rules"
            " WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "tool": r["tool"],
                "kind": r["kind"],
                "pattern": r["pattern"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def clear_rules(self, project_id: int, kind: str = "") -> int:
        """清空项目的白名单规则（可按 kind 过滤），返回删除条数。"""
        assert self._db
        if kind:
            cur = await self._db.execute(
                "DELETE FROM whitelist_rules WHERE project_id = ? AND kind = ?",
                (project_id, kind),
            )
        else:
            cur = await self._db.execute(
                "DELETE FROM whitelist_rules WHERE project_id = ?", (project_id,)
            )
        await self._db.commit()
        return cur.rowcount or 0

    async def remove_rule(self, rule_id: int) -> None:  # pragma: no cover
        assert self._db
        await self._db.execute("DELETE FROM whitelist_rules WHERE id = ?", (rule_id,))
        await self._db.commit()

    # ---- schedules（用户级日程，存在全局库里，切项目不丢） ----

    @staticmethod
    def _schedule_row(r) -> dict:
        return {
            "id": r["id"],
            "title": r["title"],
            "notes": r["notes"],
            "start_at": r["start_at"],
            "remind": bool(r["remind"]),
            "remind_before": int(r["remind_before"]),
            "reminded": bool(r["reminded"]),
            "done": bool(r["done"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    async def add_schedule(
        self,
        title: str,
        start_at: float,
        notes: str = "",
        remind: bool = True,
        remind_before: int = 0,
    ) -> dict:
        assert self._db
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO schedules (title, notes, start_at, remind, remind_before, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, notes, start_at, 1 if remind else 0, max(0, int(remind_before)), now, now),
        )
        await self._db.commit()
        return await self.get_schedule(cur.lastrowid)  # type: ignore[return-value]

    async def get_schedule(self, schedule_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        row = await cur.fetchone()
        return self._schedule_row(row) if row else None

    async def list_schedules(self, include_done: bool = False, limit: int = 500) -> list[dict]:
        assert self._db
        sql = "SELECT * FROM schedules"
        if not include_done:
            sql += " WHERE done = 0"
        sql += " ORDER BY start_at LIMIT ?"
        cur = await self._db.execute(sql, (limit,))
        rows = await cur.fetchall()
        return [self._schedule_row(r) for r in rows]

    async def update_schedule(
        self,
        schedule_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        start_at: float | None = None,
        remind: bool | None = None,
        remind_before: int | None = None,
        done: bool | None = None,
    ) -> dict | None:
        """按传入字段部分更新；start_at 变化时重置已提醒标记（新时间要重新提醒）。"""
        assert self._db
        row = await self.get_schedule(schedule_id)
        if not row:
            return None
        sets: list[str] = []
        args: list = []
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if notes is not None:
            sets.append("notes = ?")
            args.append(notes)
        if start_at is not None:
            sets.append("start_at = ?")
            args.append(start_at)
            sets.append("reminded = 0")
        if remind is not None:
            sets.append("remind = ?")
            args.append(1 if remind else 0)
        if remind_before is not None:
            sets.append("remind_before = ?")
            args.append(max(0, int(remind_before)))
        if done is not None:
            sets.append("done = ?")
            args.append(1 if done else 0)
        if not sets:
            return row
        sets.append("updated_at = ?")
        args.extend([time.time(), schedule_id])
        await self._db.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", args)
        await self._db.commit()
        return await self.get_schedule(schedule_id)

    async def delete_schedule(self, schedule_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self._db.commit()
        return bool(cur.rowcount)

    async def due_schedules(self, now: float | None = None) -> list[dict]:
        """到点未提醒的日程（含提前量：start_at - remind_before 分钟 <= now）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM schedules WHERE remind = 1 AND reminded = 0 AND done = 0"
            " AND (start_at - remind_before * 60) <= ? ORDER BY start_at",
            (now if now is not None else time.time(),),
        )
        rows = await cur.fetchall()
        return [self._schedule_row(r) for r in rows]

    async def mark_schedule_reminded(self, schedule_id: int) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE schedules SET reminded = 1, updated_at = ? WHERE id = ?",
            (time.time(), schedule_id),
        )
        await self._db.commit()

    # ---- 用量统计（usage_log）：每轮对话的 token 消耗 ----

    async def add_usage(
        self, session_id: str, provider: str, model: str,
        in_tokens: int, out_tokens: int,
    ) -> None:
        if in_tokens <= 0 and out_tokens <= 0:
            return
        assert self._db
        await self._db.execute(
            "INSERT INTO usage_log (session_id, provider, model, ts, in_tokens, out_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, provider, model, time.time(), in_tokens, out_tokens),
        )
        await self._db.commit()

    async def usage_stats(self, days: int = 14) -> dict:
        """按天聚合 + 按会话聚合（近 N 天）。日期用本地时区。

        注意：date() 必须带 'unixepoch' 修饰符——ts 是 Unix 秒，
        直写 date(ts,'localtime') 在部分 SQLite 构建上会解析成错误年份。
        """
        assert self._db
        since = time.time() - days * 86400
        by_day, by_session, by_provider = [], [], []
        try:
            cur = await self._db.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day,"
                " SUM(in_tokens) AS it, SUM(out_tokens) AS ot"
                " FROM usage_log WHERE ts >= ? GROUP BY day ORDER BY day DESC LIMIT ?",
                (since, days),
            )
            by_day = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute(
                "SELECT l.session_id AS sid, MAX(s.title) AS title,"
                " SUM(l.in_tokens) AS it, SUM(l.out_tokens) AS ot, MAX(l.ts) AS last_ts"
                " FROM usage_log l LEFT JOIN sessions s ON s.id = l.session_id"
                " WHERE l.ts >= ? GROUP BY l.session_id"
                " ORDER BY last_ts DESC LIMIT 12",
                (since,),
            )
            by_session = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute(
                "SELECT provider, MAX(model) AS model, SUM(in_tokens) AS it, SUM(out_tokens) AS ot"
                " FROM usage_log WHERE ts >= ? GROUP BY provider",
                (since,),
            )
            by_provider = [dict(r) for r in await cur.fetchall()]
        except Exception:
            pass
        return {"by_day": by_day, "by_session": by_session, "by_provider": by_provider}

    async def usage_today(self) -> int:
        """今天（本地时区零点起）累计消耗的 token 总量，供每日预算护栏判断。"""
        assert self._db
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        try:
            cur = await self._db.execute(
                "SELECT COALESCE(SUM(in_tokens + out_tokens), 0) FROM usage_log WHERE ts >= ?",
                (midnight,),
            )
            row = await cur.fetchone()
            return int(row[0] or 0)
        except Exception:
            return 0

    # ---- 快捷指令（snippets）：用户自定义提示词模板，/ 菜单置顶展示 ----

    async def list_snippets(self) -> list[dict]:
        assert self._db
        cur = await self._db.execute("SELECT * FROM snippets ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_snippet(self, name: str, content: str) -> dict:
        assert self._db
        cur = await self._db.execute(
            "INSERT INTO snippets (name, content, created_at) VALUES (?, ?, ?)",
            (name[:40], content[:8000], time.time()),
        )
        await self._db.commit()
        row = await (await self._db.execute(
            "SELECT * FROM snippets WHERE id = ?", (cur.lastrowid,)
        )).fetchone()
        return dict(row)

    async def update_snippet(self, snippet_id: int, name: str, content: str) -> bool:
        assert self._db
        cur = await self._db.execute(
            "UPDATE snippets SET name = ?, content = ? WHERE id = ?",
            (name[:40], content[:8000], snippet_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def delete_snippet(self, snippet_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
        await self._db.commit()
        return cur.rowcount > 0

    # ---- 定时任务（cron_tasks）：无人值守的周期 Agent 任务 ----

    async def list_cron_tasks(self, project_id: int | None = None) -> list[dict]:
        assert self._db
        if project_id is None:
            cur = await self._db.execute("SELECT * FROM cron_tasks ORDER BY created_at DESC")
        else:
            cur = await self._db.execute(
                "SELECT * FROM cron_tasks WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
        rows = await cur.fetchall()
        return [self._cron_row(r) for r in rows]

    async def get_cron_task(self, task_id: int) -> dict | None:
        assert self._db
        cur = await self._db.execute("SELECT * FROM cron_tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return self._cron_row(row) if row else None

    @staticmethod
    def _cron_row(r) -> dict:
        return {
            "id": r["id"],
            "project_id": r["project_id"],
            "name": r["name"],
            "prompt": r["prompt"],
            "schedule_type": r["schedule_type"],
            "interval_minutes": r["interval_minutes"],
            "time_of_day": r["time_of_day"],
            "weekday": r["weekday"],
            "allowed_tools": [t for t in (r["allowed_tools"] or "").split(",") if t],
            "enabled": bool(r["enabled"]),
            "last_run_at": r["last_run_at"],
            "last_status": r["last_status"],
            "last_result": r["last_result"],
            "next_run_at": r["next_run_at"],
            "created_at": r["created_at"],
        }

    async def add_cron_task(
        self,
        project_id: int,
        name: str,
        prompt: str,
        schedule_type: str,
        interval_minutes: int = 0,
        time_of_day: str = "",
        weekday: int = -1,
        allowed_tools: list[str] | None = None,
    ) -> dict:
        assert self._db
        tools = ",".join(allowed_tools or [])
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO cron_tasks (project_id, name, prompt, schedule_type, interval_minutes,"
            " time_of_day, weekday, allowed_tools, created_at, next_run_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, name, prompt, schedule_type, interval_minutes, time_of_day,
             weekday, tools, now, 0.0),
        )
        await self._db.commit()
        return await self.get_cron_task(cur.lastrowid)

    async def update_cron_task(self, task_id: int, **kw) -> dict | None:
        assert self._db
        allowed = ("name", "prompt", "schedule_type", "interval_minutes", "time_of_day",
                   "weekday", "allowed_tools", "enabled", "next_run_at",
                   "last_run_at", "last_status", "last_result")
        fields, args = [], []
        for k, v in kw.items():
            if k not in allowed:
                continue
            if k == "allowed_tools":
                v = ",".join(v or [])
            fields.append(f"{k} = ?")
            args.append(v)
        if not fields:
            return await self.get_cron_task(task_id)
        args.append(task_id)
        await self._db.execute(
            f"UPDATE cron_tasks SET {', '.join(fields)} WHERE id = ?", args
        )
        await self._db.commit()
        return await self.get_cron_task(task_id)

    async def delete_cron_task(self, task_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM cron_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def due_cron_tasks(self, now: float | None = None) -> list[dict]:
        """到点且启用的任务（按 next_run_at 升序）。"""
        assert self._db
        n = now if now is not None else time.time()
        cur = await self._db.execute(
            "SELECT * FROM cron_tasks WHERE enabled = 1 AND next_run_at > 0 AND next_run_at <= ?"
            " ORDER BY next_run_at ASC",
            (n,),
        )
        rows = await cur.fetchall()
        return [self._cron_row(r) for r in rows]

    @staticmethod
    def compute_next_run(row: dict, now: float | None = None) -> float:
        """按调度类型计算下次运行时间（相对 last_run_at / 当前时间）。"""
        n = now if now is not None else time.time()
        stype = row.get("schedule_type", "interval")
        if stype == "interval":
            step = max(1, int(row.get("interval_minutes") or 0))
            base = row.get("last_run_at") or n
            return base + step * 60
        try:
            hh, mm = map(int, str(row.get("time_of_day") or "00:00").split(":"))
        except ValueError:
            return n + 24 * 3600
        if stype == "daily":
            lt = time.localtime(n)
            today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            return today if today > n else today + 24 * 3600
        if stype == "weekly":
            wd = int(row.get("weekday") if row.get("weekday") is not None else -1)
            if wd < 0:
                return n + 7 * 24 * 3600
            lt = time.localtime(n)
            today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            days_ahead = (wd - lt.tm_wday + 7) % 7
            cand = today + days_ahead * 24 * 3600
            if cand <= n:
                cand += 7 * 24 * 3600
            return cand
        return n + 24 * 3600


def export_messages_text(messages: list[Message]) -> str:
    """导出会话为可读文本（/export 命令用）。"""
    lines = []
    for m in messages:
        if m.role == "system":
            continue
        lines.append(f"[{m.role.upper()}] {m.to_plain()}")
    return "\n\n".join(lines)


def messages_to_json(messages: list[Message]) -> str:
    return json.dumps([m.model_dump() for m in messages], ensure_ascii=False, indent=2)
