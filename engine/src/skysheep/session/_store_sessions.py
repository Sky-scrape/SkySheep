"""SessionStore 的会话、消息读写、分叉复制、全文检索与轮耗时样本查询方法。"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterable

import aiosqlite

from ..messages import Message
from ._store_types import session_from_row

logger = logging.getLogger("skysheep.store")


def _message_duration_seconds(content_json: str) -> float | None:
    """从消息 JSON 里取实测轮耗时（秒）；没有该字段（旧消息）返回 None。

    直接解 JSON 而不是走 Message.model_validate_json：这里只关心一个标量，
    历史行里可能有旧版本写下的结构，完整校验失败就会丢掉整条样本。
    """
    try:
        raw = json.loads(content_json)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    ms = raw.get("duration_ms") or 0
    try:
        sec = int(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return sec if 1 <= sec <= 7200 else None


class SessionsMixin:
    SEARCH_SELECT = (
        "SELECT m.session_id, s.title, s.updated_at, m.content, s.project_id,"
        "       p.name AS project_name, p.root_path AS project_path,"
        "       m.seq AS seq, m.role AS role"
        " FROM messages m"
        " JOIN sessions s ON s.id = m.session_id"
        " LEFT JOIN projects p ON p.id = s.project_id"
    )
    SEARCH_SQL_LIMIT = 500

    async def create_session(self, project_id: int | None, title: str = "") -> object:
        assert self._db
        now = time.time()
        sid = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO sessions (id, project_id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sid, project_id, title, now, now),
        )
        await self._db.commit()
        from ._store_types import Session

        return Session(sid, project_id, title, now, now)

    async def get_session(self, session_id: str):
        assert self._db
        cur = await self._db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return session_from_row(row) if row else None

    async def get_session_for_project(
        self, session_id: str, project_id: int | None
    ):
        """按 id 取会话并校验项目归属：不属于该项目的（含快聊边界）返回 None。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?"
            " AND ((? IS NULL AND project_id IS NULL) OR project_id = ?)",
            (session_id, project_id, project_id),
        )
        row = await cur.fetchone()
        return session_from_row(row) if row else None

    async def list_sessions(self, project_id: int | None = None, limit: int = 50) -> list:
        """侧栏会话列表（已归档的不在其中，见 list_archived_sessions）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT ?", (limit,)
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (project_id, limit),
            )
        rows = await cur.fetchall()
        return [session_from_row(r) for r in rows]

    async def list_session_ids(self) -> set[str]:
        """库内全部会话 id（含归档与各项目）：运行快照清理孤儿记录时判存活用。"""
        assert self._db
        cur = await self._db.execute("SELECT id FROM sessions")
        return {str(r["id"]) for r in await cur.fetchall()}

    async def list_quick_sessions(self, limit: int = 50) -> list:
        """只取快聊会话（project_id IS NULL），最近活跃在前。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE project_id IS NULL AND archived = 0"
            " ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [session_from_row(r) for r in rows]

    async def list_sessions_by_project(
        self, limit_per_project: int = 50
    ) -> dict[int | None, list]:
        """分组侧栏用：每个项目各自取最近未归档会话（置顶在前）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM ("
            "  SELECT s.*, ROW_NUMBER() OVER ("
            "    PARTITION BY project_id ORDER BY pinned DESC, updated_at DESC) AS _rn"
            "  FROM sessions s WHERE s.archived = 0"
            ") WHERE _rn <= ? ORDER BY project_id, pinned DESC, updated_at DESC",
            (limit_per_project,),
        )
        rows = await cur.fetchall()
        out: dict[int | None, list] = {}
        for r in rows:
            s = session_from_row(r)
            out.setdefault(s.project_id, []).append(s)
        return out

    @staticmethod
    def _archived_filter(project_id: int | None, include_projectless: bool) -> tuple[str, tuple]:
        if project_id is None:
            return "archived = 1", ()
        if include_projectless:
            return "archived = 1 AND (project_id = ? OR project_id IS NULL)", (project_id,)
        return "project_id = ? AND archived = 1", (project_id,)

    async def list_archived_sessions(
        self, project_id: int | None = None, limit: int = 100,
        include_projectless: bool = False,
    ) -> list:
        """已归档会话（归档弹窗用），最近活跃在前。"""
        assert self._db
        where, args = self._archived_filter(project_id, include_projectless)
        cur = await self._db.execute(
            f"SELECT * FROM sessions WHERE {where} ORDER BY updated_at DESC LIMIT ?",
            (*args, limit),
        )
        rows = await cur.fetchall()
        return [session_from_row(r) for r in rows]

    async def count_archived_sessions(
        self, project_id: int | None = None, include_projectless: bool = False
    ) -> int:
        """当前项目（或全部项目）的归档会话数，侧栏归档入口的角标用。"""
        assert self._db
        where, args = self._archived_filter(project_id, include_projectless)
        cur = await self._db.execute(
            f"SELECT COUNT(*) AS n FROM sessions WHERE {where}", args
        )
        row = await cur.fetchone()
        return int(row["n"] or 0) if row else 0

    async def set_archived(self, session_id: str, archived: bool) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET archived = ? WHERE id = ?", (1 if archived else 0, session_id)
        )
        await self._db.commit()

    async def stale_session_ids(
        self, cutoff: float, exclude_ids: Iterable[str] = (), limit: int = 200
    ) -> list[str]:
        """自动归档候选：未归档、未置顶、最后更新早于 cutoff 的会话 id（最旧在前）。"""
        assert self._db
        exclude = [str(s) for s in exclude_ids if s][:50]
        if exclude:
            ph = ",".join("?" * len(exclude))
            cur = await self._db.execute(
                "SELECT id FROM sessions WHERE archived = 0 AND pinned = 0"
                f" AND updated_at < ? AND id NOT IN ({ph})"
                " ORDER BY updated_at ASC LIMIT ?",
                (cutoff, *exclude, limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT id FROM sessions WHERE archived = 0 AND pinned = 0"
                " AND updated_at < ? ORDER BY updated_at ASC LIMIT ?",
                (cutoff, limit),
            )
        return [r["id"] for r in await cur.fetchall()]

    async def get_session_memory_digested(self, session_id: str) -> bool:
        """归档自动记忆是否已对该会话提炼过（取消归档再归档不重复提炼）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT memory_digested FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return bool(row and row[0])

    async def mark_session_memory_digested(self, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET memory_digested = 1 WHERE id = ?", (session_id,)
        )
        await self._db.commit()

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

    async def search_messages_by_seq(
        self, session_id: str, before_seq: int, role: str
    ) -> int | None:
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

    async def get_full_message_at(self, session_id: str, seq: int) -> Message | None:
        """按会话与序号读取完整消息，供需验证持久化元数据的服务端操作使用。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT seq, content FROM messages WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        )
        row = await cur.fetchone()
        if not row:
            return None
        message = Message.model_validate_json(row["content"])
        message.seq = row["seq"]
        return message

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
        except Exception:  # noqa: BLE001
            plain = ""
        return {"seq": row["seq"], "role": row["role"], "text": plain}

    async def truncate_from(self, session_id: str, seq: int, include_self: bool) -> int:
        """删除 seq 在锚点之后（或含锚点）的消息，返回删除条数。"""
        assert self._db
        op = ">=" if include_self else ">"

        async def txn(conn: aiosqlite.Connection) -> int:
            await self._fts_delete(conn, f"session_id = ? AND seq {op} ?", (session_id, seq))
            cur = await conn.execute(
                f"DELETE FROM messages WHERE session_id = ? AND seq {op} ?",
                (session_id, seq),
            )
            return cur.rowcount

        return await self._write_txn(txn)

    async def copy_messages_between(
        self, src_session: str, dst_session: str, upto_seq: int
    ) -> int:
        """把 seq <= upto_seq 的消息复制到新会话（分叉用），返回复制条数。"""
        assert self._db

        async def txn(conn: aiosqlite.Connection) -> int:
            cur = await conn.execute(
                "SELECT seq, role, content, created_at FROM messages"
                " WHERE session_id = ? AND seq <= ? ORDER BY seq ASC",
                (src_session, upto_seq),
            )
            rows = await cur.fetchall()
            n = 0
            for r in rows:
                cur2 = await conn.execute(
                    "INSERT INTO messages (session_id, seq, role, content, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (dst_session, r["seq"], r["role"], r["content"], r["created_at"]),
                )
                if cur2.lastrowid is not None:
                    await self._fts_insert(conn, int(cur2.lastrowid), r["content"])
                n += 1
            return n

        return await self._write_txn(txn)

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

    async def set_tags(self, session_id: str, tags: list[str] | str) -> str:
        """设置会话标签，返回归一化后的存库字符串。"""
        assert self._db
        items: list[str] = []
        raw = tags if isinstance(tags, str) else ",".join(str(t) for t in tags)
        for piece in str(raw).replace("，", ",").split(","):
            t = piece.strip()[:24]
            if t and t not in items:
                items.append(t)
            if len(items) >= 12:
                break
        value = ",".join(items)
        await self._db.execute(
            "UPDATE sessions SET tags = ? WHERE id = ?", (value, session_id)
        )
        await self._db.commit()
        return value

    async def list_all_tags(self, project_id: int | None) -> list[dict]:
        """当前项目下出现过的标签及各自会话数（侧栏分组头部用）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT tags FROM sessions WHERE project_id IS NULL AND tags != ''"
            )
        else:
            cur = await self._db.execute(
                "SELECT tags FROM sessions WHERE project_id = ? AND tags != ''", (project_id,)
            )
        rows = await cur.fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            for t in str(r["tags"] or "").split(","):
                t = t.strip()
                if t:
                    counts[t] = counts.get(t, 0) + 1
        return [
            {"tag": t, "count": n}
            for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    async def latest_session(self, project_id: int | None):
        """取项目下最近活跃的会话（用于启动时"接着上次继续"；已归档不参与）。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id IS NULL AND archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT 1"
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND archived = 0"
                " ORDER BY pinned DESC, updated_at DESC LIMIT 1",
                (project_id,),
            )
        row = await cur.fetchone()
        return session_from_row(row) if row else None

    async def count_empty_sessions(self, project_id: int | None) -> int:
        """统计能被 delete_empty_sessions 清掉的空会话数。"""
        assert self._db
        if project_id is None:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id IS NULL"
                " AND pinned = 0 AND archived = 0"
                " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"
            )
        else:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_id = ?"
                " AND pinned = 0 AND archived = 0"
                " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)",
                (project_id,),
            )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def delete_empty_sessions(
        self, project_id: int | None, keep_id: str | None = None
    ) -> list[str]:
        """删除同一项目里没有任何消息的空会话，返回被删的 session id 列表。"""
        assert self._db
        keep = keep_id or ""
        proj_clause = "project_id IS NULL" if project_id is None else "project_id = ?"
        params = (project_id, keep) if project_id is not None else (keep,)
        cur = await self._db.execute(
            f"SELECT id FROM sessions WHERE {proj_clause} AND pinned = 0"
            " AND archived = 0"
            " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"
            " AND id != ?",
            params,
        )
        ids = [r["id"] for r in await cur.fetchall()]
        if ids:
            async def txn(conn: aiosqlite.Connection) -> None:
                for i in range(0, len(ids), 500):
                    chunk = ids[i : i + 500]
                    marks = ",".join("?" * len(chunk))
                    await conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", chunk)

            await self._write_txn(txn)
        return ids

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
        """删除会话及其全部消息（走独立写事务，FTS/消息/会话/渠道绑定同生同死）。"""
        assert self._db

        async def txn(conn: aiosqlite.Connection) -> None:
            await self._fts_delete(conn, "session_id = ?", (session_id,))
            await conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await conn.execute(
                "DELETE FROM channel_bindings WHERE session_id = ?", (session_id,)
            )

        await self._write_txn(txn)

    async def append_message(self, session_id: str, message: Message) -> int:
        """追加一条消息，返回消息行 id。"""
        assert self._db
        content_json = message.model_dump_json()

        async def txn(conn: aiosqlite.Connection) -> tuple[int, int | None]:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            )
            row = await cur.fetchone()
            seq = row[0]
            cur = await conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, message.role, content_json, time.time()),
            )
            await self._fts_insert(conn, int(cur.lastrowid), content_json)
            return seq, cur.lastrowid

        seq, row_id = await self._write_txn(txn)
        if seq is None or row_id is None:
            raise RuntimeError(
                f"消息未写入会话库（会话 {session_id}）：写入返回空行号"
            )
        async with self._writer_lock:
            conn = self._writer or self._db
            cur = await conn.execute("SELECT 1 FROM messages WHERE id = ?", (row_id,))
            exists = await cur.fetchone()
        if exists is None:
            logger.error(
                "消息落库后读不回来：session=%s rowid=%s db=%s",
                session_id, row_id, self.path,
            )
            raise RuntimeError(
                "消息落库后读不回来：数据库可能被替换或写入未生效"
                f"（会话 {session_id}，库 {self.path.name}）"
            )
        message.seq = seq
        return row_id

    async def replace_message(self, session_id: str, message: Message) -> bool:
        """按 (session_id, seq) 重写一条已落库消息的内容。"""
        assert self._db
        if message.seq is None:
            return False
        content_json = message.model_dump_json()

        async def txn(conn) -> bool:
            cur = await conn.execute(
                "UPDATE messages SET content = ? WHERE session_id = ? AND seq = ?",
                (content_json, session_id, message.seq),
            )
            return cur.rowcount > 0

        return await self._write_txn(txn)

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
            m.seq = r["seq"]
            out.append(m)
        return out

    async def load_messages_window(
        self, session_id: str, *, before_seq: int | None = None, limit: int = 200,
    ) -> tuple[list[Message], int, int]:
        """窗口化读取 user/assistant 消息（大会话历史窗口化的存储半边）。"""
        assert self._db
        limit = max(1, int(limit))
        conds = ["session_id = ?", "role IN ('user', 'assistant')"]
        args: list = [session_id]
        if before_seq is not None:
            conds.append("seq < ?")
            args.append(int(before_seq))
        where = " AND ".join(conds)
        cur = await self._db.execute(
            f"SELECT seq, content FROM messages WHERE {where}"
            " ORDER BY seq DESC LIMIT ?",
            (*args, int(limit)),
        )
        rows = await cur.fetchall()
        win: list[Message] = []
        for r in reversed(rows):
            m = Message.model_validate_json(r["content"])
            m.seq = r["seq"]
            win.append(m)
        bound = rows[-1]["seq"] if rows else (int(before_seq) if before_seq is not None else 0)
        cur = await self._db.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN seq < ? THEN 1 ELSE 0 END) AS before_cnt"
            " FROM messages WHERE session_id = ? AND role IN ('user', 'assistant')",
            (bound, session_id),
        )
        row = await cur.fetchone()
        total = int(row["total"] or 0)
        offset = int(row["before_cnt"] or 0)
        return win, total, offset

    async def search_messages(
        self, project_id: int | None, query: str, limit: int = 20, scope: str = "project"
    ) -> list[dict]:
        """跨会话搜索消息内容（优先 FTS5，回落 LIKE）。"""
        assert self._db
        query = (query or "").strip()
        if not query:
            return []
        if self.fts_ready:
            rows = await self._search_fts(project_id, query, scope)
        else:
            rows = await self._search_like(project_id, query, scope)

        results: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            if r["session_id"] in seen:
                continue
            seen.add(r["session_id"])
            plain = self._plain_text(r["content"])
            low, q = plain.lower(), query.lower()
            pos = low.find(q)
            if pos < 0:
                pos = 0
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
                    "seq": r["seq"],
                    "role": r["role"],
                }
            )
            if len(results) >= limit:
                break
        return results

    async def search_session_history(
        self, session_id: str, query: str, limit: int = 8, max_chars: int = 1200
    ) -> list[dict]:
        """在单个会话的历史里按关键词召回，多条命中都返回。"""
        assert self._db
        query = (query or "").strip()
        if not query or not session_id:
            return []
        escaped = (
            query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        want = max(1, min(50, int(limit)))
        like = f"%{escaped}%"
        out: list[dict] = []
        scanned = 0
        while len(out) < want and scanned < self.SEARCH_SQL_LIMIT:
            take = min(100, self.SEARCH_SQL_LIMIT - scanned)
            cur = await self._db.execute(
                "SELECT seq, role, content, created_at FROM messages"
                " WHERE session_id = ? AND content LIKE ? ESCAPE '\\'"
                " ORDER BY seq DESC LIMIT ? OFFSET ?",
                (session_id, like, take, scanned),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            scanned += len(rows)
            for r in rows:
                plain = self._plain_text(r["content"])
                if query.lower() not in plain.lower():
                    continue
                out.append({
                    "seq": int(r["seq"]),
                    "role": r["role"],
                    "text": plain[:max_chars],
                    "truncated": len(plain) > max_chars,
                    "created_at": float(r["created_at"]),
                })
                if len(out) >= want:
                    break
            if len(rows) < take:
                break
        return out

    def _scope_condition(self, project_id: int | None, scope: str) -> tuple[str, tuple]:
        if scope == "all":
            return "s.archived = 0", ()
        cond = "s.project_id IS NULL" if project_id is None else "s.project_id = ?"
        args = (project_id,) if project_id is not None else ()
        return f"({cond}) AND s.archived = 0", args

    async def _search_fts(self, project_id: int | None, query: str, scope: str) -> list:
        """FTS5 查询；短查询/语法异常或虚表缺失时自动退到 LIKE。"""
        if len(query) < 3:
            return await self._search_like(project_id, query, scope)
        cond, args = self._scope_condition(project_id, scope)
        try:
            match = '"' + query.replace('"', '""') + '"'
            cur = await self._db.execute(
                self.SEARCH_SELECT
                + " JOIN messages_fts f ON f.rowid = m.id"
                + f" WHERE {cond} AND messages_fts MATCH ?"
                " ORDER BY s.pinned DESC, s.updated_at DESC, m.seq"
                " LIMIT ?",
                args + (match, self.SEARCH_SQL_LIMIT),
            )
            return await cur.fetchall()
        except sqlite3.OperationalError as e:
            if "fts5" not in str(e).lower() and "syntax" not in str(e).lower():
                logger.warning("FTS 查询失败，退回 LIKE 扫描：%s", e)
            return await self._search_like(project_id, query, scope)
        except Exception:  # noqa: BLE001
            return await self._search_like(project_id, query, scope)

    async def _search_like(self, project_id: int | None, query: str, scope: str) -> list:
        """LIKE 全扫描：FTS 不可用时的兜底。"""
        cond, args = self._scope_condition(project_id, scope)
        escaped = (
            query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        cur = await self._db.execute(
            self.SEARCH_SELECT
            + f" WHERE {cond} AND m.content LIKE ? ESCAPE '\\'"
            " ORDER BY s.pinned DESC, s.updated_at DESC, m.seq"
            " LIMIT ?",
            args + (f"%{escaped}%", self.SEARCH_SQL_LIMIT),
        )
        return await cur.fetchall()

    async def recent_turn_seconds(self, project_id: int | None, limit: int = 30) -> list[float]:
        """本项目最近 N 次「用户消息→助手回复」的实测耗时（秒），新→旧。"""
        assert self._db
        cond = "s.project_id IS NULL" if project_id is None else "s.project_id = ?"
        args = () if project_id is None else (project_id,)
        cur = await self._db.execute(
            "SELECT a.content AS acontent, a.created_at - u.created_at AS dur"
            " FROM messages u"
            " JOIN sessions s ON s.id = u.session_id"
            " JOIN messages a"
            "   ON a.session_id = u.session_id AND a.seq = u.seq + 1 AND a.role = 'assistant'"
            f" WHERE u.role = 'user' AND {cond}"
            " ORDER BY u.created_at DESC"
            " LIMIT ?",
            args + (limit * 2,),
        )
        rows = await cur.fetchall()
        out: list[float] = []
        for r in rows:
            real = _message_duration_seconds(r["acontent"])
            if real is not None:
                out.append(real)
            elif 1 <= r["dur"] <= 7200:
                out.append(r["dur"])
            if len(out) >= limit:
                break
        return out

    async def last_assistant_text(self, session_id: str) -> str:
        """读取会话最新一条非空 assistant 文本（逆序扫描，避免全量加载消息历史）。"""
        assert self._db
        cur = await self._db.execute(
            "SELECT content FROM messages"
            " WHERE session_id = ? AND role = 'assistant'"
            " ORDER BY seq DESC",
            (session_id,),
        )
        async for row in cur:
            text = (Message.model_validate_json(row["content"]).text or "").strip()
            if text:
                return text
        return ""
